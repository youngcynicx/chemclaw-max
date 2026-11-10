import test from "node:test";
import assert from "node:assert/strict";
import { captureProgress } from "./capture-progress.mjs";

const ref = (page_id = "page-a", block_id = "block-a", transcript_revision = 1) => ({ page_id, block_id, transcript_revision });
const event = (event_id = "event-a", revision = 1, refs = [ref()]) => ({ event_id, revision,
  page_id: refs[0]?.page_id, evidence_refs: refs });
const call = (name, result, extra = {}) => ({ name, state: "completed", result, ...extra });
const applied = (result, object_refs = []) => ({ action_status: "applied", result, object_refs });
const write = (...events) => call("memory.proposal.create", applied({ proposal: {
  proposal_id: "proposal-a", page_id: "page-a", items: events.map((entry) => ({ event_id: entry.event_id,
    event_revision: entry.revision, evidence_refs: entry.evidence_refs })),
}, knowledge: { adopted: events } }));
const read = (...events) => call("query.samples.list", applied({ samples: [{ sample_id: "sample-a", records: events }] }));
const context = (revision = 1, blockIds = ["block-a", "block-b"]) => ({ pages: [{ page_id: "page-a" }],
  source_pages: [{ page_id: "page-a", transcript_revision: revision,
    blocks: blockIds.map((block_id) => ({ block_id, text: "source facts stay in the LLM context" })) }] });
const page = (result, pageId = "page-a") => result.pages.find((entry) => entry.page_id === pageId);

test("source inventory alone remains unlinked with no manufactured saved knowledge", () => {
  const result = captureProgress([], [], context());
  assert.deepEqual(result.target_page_ids, ["page-a"]);
  assert.deepEqual(result.saved_event_refs, []);
  assert.deepEqual(result.verified_event_refs, []);
  assert.deepEqual(page(result).unlinked_block_ids, ["block-a", "block-b"]);
  assert.equal(page(result).source_inventory_complete, true);
  assert.equal(result.knowledge_complete, null);
  assert.equal(page(result).knowledge_complete, null);
  assert.doesNotMatch(JSON.stringify(result), /source facts stay/);
});

test("applied writes without readback save object refs but do not advance block linkage", () => {
  const result = captureProgress([write(event())], [], context());
  assert.deepEqual(result.saved_event_refs, [{ event_id: "event-a", revision: 1 }]);
  assert.deepEqual(result.verified_event_refs, []);
  assert.deepEqual(page(result).linked_block_ids, []);
});

test("only actual matching readbacks advance linked blocks; full links still do not prove semantics", () => {
  const a = event(), b = event("event-b", 2, [ref("page-a", "block-b")]);
  const result = captureProgress([write(a, b), read(a, b)], [], context());
  assert.deepEqual(page(result).linked_block_ids, ["block-a", "block-b"]);
  assert.deepEqual(page(result).unlinked_block_ids, []);
  assert.equal(page(result).knowledge_complete, null);
  assert.equal(result.knowledge_complete, null);
});

test("partial readback leaves the other batch source blocks available to continue", () => {
  const a = event(), b = event("event-b", 1, [ref("page-a", "block-b")]);
  const result = captureProgress([write(a, b), read(a)], [], context());
  assert.deepEqual(page(result).saved_event_refs, [{ event_id: "event-a", revision: 1 }, { event_id: "event-b", revision: 1 }]);
  assert.deepEqual(page(result).linked_block_ids, ["block-a"]);
  assert.deepEqual(page(result).unlinked_block_ids, ["block-b"]);
});

test("existing current objects can be reused from read-only receipts without pretending to write", () => {
  const result = captureProgress([read(event())], [], context());
  assert.deepEqual(result.saved_event_refs, []);
  assert.deepEqual(result.verified_event_refs, [{ event_id: "event-a", revision: 1 }]);
  assert.deepEqual(page(result).linked_block_ids, ["block-a"]);
});

test("a newer applied revision invalidates an older readback's block linkage", () => {
  const result = captureProgress([read(event()), write(event("event-a", 2))], [], context());
  assert.deepEqual(result.saved_event_refs, [{ event_id: "event-a", revision: 2 }]);
  assert.deepEqual(result.verified_event_refs, []);
  assert.deepEqual(page(result).linked_block_ids, []);
});

test("a genuinely shared current event links both source pages without duplicating the object", () => {
  const ctx = context();
  ctx.source_pages.push({ page_id: "page-b", transcript_revision: 2, blocks: [{ block_id: "shared-b" }] });
  const shared = event("shared", 3, [ref(), ref("page-b", "shared-b", 2)]);
  const result = captureProgress([write(shared), read(shared)], [], ctx);
  assert.equal(result.verified_event_refs.length, 1);
  assert.deepEqual(page(result).linked_block_ids, ["block-a"]);
  assert.deepEqual(page(result, "page-b").linked_block_ids, ["shared-b"]);
  assert.deepEqual(result.target_page_ids, ["page-a"], "historical context pages do not expand the user's target page");
});

test("new source revision invalidates old block cursors while preserving object IDs as readback hints", () => {
  const prior = captureProgress([write(event()), read(event())], [], context());
  const result = captureProgress([read(event())], [], context(2, ["new-block"]), { previous: prior });
  assert.equal(page(result).source_changed, true);
  assert.equal(page(result).previous_transcript_revision, 1);
  assert.deepEqual(page(result).linked_block_ids, []);
  assert.deepEqual(page(result).unlinked_block_ids, ["new-block"]);
  assert.deepEqual(page(result).stale_reference_event_ids, ["event-a"]);
  assert.deepEqual(result.previous_event_refs, [{ event_id: "event-a", revision: 1 }]);
});

test("same-version previous cursor is not self-authenticating current readback evidence", () => {
  const prior = captureProgress([write(event()), read(event())], [], context());
  const result = captureProgress([], [], context(), { previous: prior });
  assert.deepEqual(result.saved_event_refs, []);
  assert.deepEqual(result.verified_event_refs, []);
  assert.deepEqual(page(result).linked_block_ids, []);
  assert.equal(page(result).source_changed, false);
  assert.equal(result.previous_event_refs.length, 1);
});

test("out-of-inventory block references and unknown revisions never advance the cursor", () => {
  const result = captureProgress([read(event("outside", 1, [ref("page-a", "made-up")]),
    event("unversioned", 1, [{ page_id: "page-a", block_id: "block-a" }]))], [], context());
  assert.deepEqual(page(result).linked_block_ids, []);
  assert.deepEqual(page(result).unlinked_block_ids, ["block-a", "block-b"]);
});

test("pending and explicit unknown writes preserve reconciliation intent and never count as saved", () => {
  const pending = write(event()); pending.state = "pending"; pending.operation_id = "operation-pending";
  const unknown = call("memory.event.correct", { action_status: "unknown", result: event("maybe") },
    { operation_id: "operation-unknown", toolCallId: "call-unknown" });
  const result = captureProgress([pending, unknown], [], context());
  assert.equal(result.requires_review, true);
  assert.equal(result.unresolved_writes.length, 2);
  assert.equal(result.unresolved_writes[0].operation_id, "operation-pending");
  assert.deepEqual(result.saved_event_refs, []);
  assert.deepEqual(page(result).linked_block_ids, []);
});

test("definite rejected writes do not become unknown effects or successful progress", () => {
  const result = captureProgress([call("memory.proposal.create", { action_status: "rejected", ok: false,
    result: { knowledge: { adopted: [event()] } } }, { isError: true })], [], context());
  assert.equal(result.requires_review, false);
  assert.deepEqual(result.saved_event_refs, []);
});

test("empty or proposed proposals do not become saved knowledge or a completed empty page", () => {
  const result = captureProgress([call("memory.proposal.create", applied({ proposal: { proposal_id: "empty", items: [] } })),
    call("memory.proposal.create", { action_status: "proposed", result: { proposal: { items: [event()] } } })], [], context(1, []));
  assert.deepEqual(result.saved_event_refs, []);
  assert.deepEqual(page(result).source_block_ids, []);
  assert.equal(page(result).knowledge_complete, null);
});

test("delegation traversal only uses retained typed tool calls and aggregates a child once", () => {
  const child = { run_id: "child", text: "all pages complete", toolCalls: [write(event()), read(event())], delegations: [] };
  const delegated = call("agent.delegate", { run_id: "child", object_refs: [{ kind: "event", id: "invented" }] });
  const result = captureProgress([delegated], [child], context());
  assert.equal(result.saved_event_refs.length, 1);
  assert.deepEqual(page(result).linked_block_ids, ["block-a"]);
  assert.deepEqual(captureProgress([delegated], [{ ...child, toolCalls: [] }], context()).verified_event_refs, []);
});

test("omitted sources have no complete inventory until a real full page read supplies it", () => {
  const ctx = { pages: ["page-a"], source_omitted_pages: ["page-a"] };
  const missing = captureProgress([], [], ctx);
  assert.equal(page(missing).source_inventory_complete, false);
  assert.equal(page(missing).transcript_revision, null);
  const sourceRead = call("evidence.page.read", applied({ page: { page_id: "page-a", active_revision: 2,
    transcript: { transcript_revision: 2, active_revision: 2, is_current: true, blocks: [{ block_id: "new" }] } } }));
  const result = captureProgress([sourceRead], [], ctx);
  assert.equal(page(result).source_inventory_complete, true);
  assert.deepEqual(page(result).source_block_ids, ["new"]);
});

test("partial and historical page reads cannot claim a whole current source inventory", () => {
  const partial = call("evidence.page.read", applied({ page: { page_id: "page-a", active_revision: 1,
    transcript: { transcript_revision: 1, blocks: [{ block_id: "block-a" }] } } }),
    { arguments: { block_ids: ["block-a"] } });
  assert.equal(page(captureProgress([partial], [], { pages: ["page-a"] })).source_inventory_complete, false);
  const old = call("evidence.page.read", applied({ page: { page_id: "page-a", active_revision: 2,
    transcript: { transcript_revision: 1, is_current: false, blocks: [{ block_id: "old" }] } } }));
  const result = captureProgress([old], [], context(2, ["new"]));
  assert.deepEqual(page(result).source_block_ids, ["new"]);
  assert.equal(page(result).transcript_revision, 2);
});

test("a new full page read replaces old-version inventory; subset read never discards known blocks", () => {
  const fresh = call("evidence.page.read", applied({ page: { page_id: "page-a", active_revision: 3,
    transcript: { transcript_revision: 3, blocks: [{ block_id: "new-a" }, { block_id: "new-b" }] } } }));
  const subset = call("evidence.page.read", applied({ page: { page_id: "page-a", active_revision: 3,
    transcript: { transcript_revision: 3, blocks: [{ block_id: "new-a" }] } } }),
    { arguments: { block_ids: ["new-a"] } });
  const result = captureProgress([fresh, subset, read(event())], [], context());
  assert.equal(page(result).transcript_revision, 3);
  assert.equal(page(result).source_inventory_complete, true);
  assert.deepEqual(page(result).unlinked_block_ids, ["new-a", "new-b"]);
});

test("explicit historical event reads do not stand in for current readback targets", () => {
  const historical = call("memory.event.read", applied({ ...event(), entity_id: "event-a" }), { arguments: { revision: 1 } });
  const result = captureProgress([historical], [], context());
  assert.deepEqual(result.verified_event_refs, []);
  assert.deepEqual(page(result).linked_block_ids, []);
});

test("grouped related event refs do not inherit another record's evidence blocks", () => {
  const grouped = { ...event(), related_events: [{ event_id: "event-b", revision: 1 }] };
  const result = captureProgress([read(grouped)], [], context());
  assert.equal(result.verified_event_refs.length, 2);
  assert.deepEqual(page(result).linked_block_ids, ["block-a"]);
  assert.deepEqual(page(result).unlinked_block_ids, ["block-b"]);
});

test("a corrected current event never borrows an old proposal item's original block citations", () => {
  const current = event("event-a", 3);
  const proposal = call("memory.proposal.read", applied({ proposal: { proposal_id: "proposal-a", page_id: "page-a",
    items: [{ event_id: "event-a", event_revision: 1, evidence_refs: [ref("page-a", "block-b")], current_event: current }] } }));
  const result = captureProgress([proposal], [], context());
  assert.deepEqual(result.verified_event_refs, [{ event_id: "event-a", revision: 3 }]);
  assert.deepEqual(page(result).linked_block_ids, ["block-a"]);
  assert.deepEqual(page(result).unlinked_block_ids, ["block-b"]);
});
