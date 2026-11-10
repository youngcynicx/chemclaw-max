import test from "node:test";
import assert from "node:assert/strict";
import { captureProgress, boundCaptureProgress } from "./capture-progress.mjs";
import { receiptSummary } from "./knowledge-delivery.mjs";
const size = value => Buffer.byteLength(JSON.stringify(value), "utf8");
const source = (page_id = "p1", blocks = ["b1"]) => ({ page_id, transcript_revision: 1,
  blocks: blocks.map(block_id => ({ block_id })) });

test("four thousand current events produce a bounded index without mutating the full journal", () => {
  const calls = [{ name: "query.samples.list", state: "completed", result: { ok: true, action_status: "applied",
    result: { samples: [{ records: Array.from({ length: 4000 }, (_, n) => ({ entity_id: `event-${n}`, revision: 1,
      evidence_refs: [{ page_id: "p1", transcript_revision: 1, block_id: "b1" }] })) }] } } }];
  const before = JSON.stringify(calls);
  const progress = captureProgress(calls, [], { pages: [{ page_id: "p1" }], source_pages: [source()] });
  assert.ok(size(progress) <= 32768); assert.equal(progress.truncated, true);
  assert.equal(progress.total_counts.verified_events, 4000);
  assert.ok(progress.omitted_counts.verified_events > 0);
  assert.equal(progress.knowledge_complete, null);
  assert.equal(progress.pages[0].source_inventory_complete, false);
  assert.deepEqual(progress.pages[0].linked_block_ids, []);
  assert.equal(JSON.stringify(calls), before);
  assert.match(receiptSummary({}, { progress }), /省略不代表已完成/);
});

test("large block inventories stay explicitly incomplete and preserve their full counts", () => {
  const blocks = Array.from({ length: 10000 }, (_, n) => `block-${n}-` + "x".repeat(95));
  const context = { pages: [{ page_id: "p1" }], source_pages: [source("p1", blocks)] };
  const before = JSON.stringify(context);
  const progress = captureProgress([], [], context);
  assert.ok(size(progress) <= 32768); assert.equal(progress.truncated, true);
  assert.equal(progress.total_counts.source_blocks, 10000);
  assert.equal(progress.pages[0].source_inventory_complete, false);
  assert.equal(progress.pages[0].source_block_count, 10000);
  assert.equal(progress.omitted_counts.source_blocks, 10000);
  assert.equal(progress.knowledge_complete, null); assert.equal(JSON.stringify(context), before);
});

test("omitting unknown-write examples never removes the review fence", () => {
  const calls = Array.from({ length: 4000 }, (_, n) => ({ name: "memory.proposal.create", state: "pending",
    operation_id: `operation-${n}-` + "x".repeat(100), toolCallId: `call-${n}`, result: null }));
  const progress = captureProgress(calls, [], { source_pages: [source()] });
  assert.ok(size(progress) <= 32768); assert.equal(progress.requires_review, true);
  assert.equal(progress.unresolved_write_count, 4000);
  assert.ok(progress.omitted_counts.unresolved_writes > 0);
  assert.equal(progress.saved_event_refs.length, 0);
});

test("very long page IDs expose an incomplete target index rather than silently claiming full scope", () => {
  const pages = Array.from({ length: 100 }, (_, n) => source(`page-${n}-` + "x".repeat(490)));
  const progress = captureProgress([], [], { pages, source_pages: pages });
  assert.ok(size(progress) <= 32768); assert.equal(progress.truncated, true);
  assert.equal(progress.target_page_ids_complete, false);
  assert.equal(progress.total_counts.target_pages, 100);
  assert.ok(progress.omitted_counts.target_pages > 0);
  assert.ok(progress.pages.every(p => p.transcript_revision === 1 && p.knowledge_complete === null));
});

test("bounded index API rejects unsafe limits and leaves small projections unchanged", () => {
  const progress = captureProgress([], [], { source_pages: [source()] });
  assert.deepEqual(boundCaptureProgress(progress), progress);
  for (const limit of [0, 1024, NaN, 262145]) assert.throws(() => boundCaptureProgress(progress, limit));
});
