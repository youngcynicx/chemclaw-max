import test from "node:test";
import assert from "node:assert/strict";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { knowledgeDelivery, receiptSummary } from "./knowledge-delivery.mjs";
import { createRuntime, delegationReceipt } from "./runtime.mjs";

const ref = (kind, id, revision = 1) => ({ kind, id, revision });
const call = (name, result, extra = {}) => ({ name, state: "completed", result, ...extra });
const applied = (result, object_refs = []) => ({ action_status: "applied", result, object_refs });
const event = (id, revision = 1) => ({ event_id: id, revision, page_id: "page-a", sample_id: "sample-a",
  evidence_refs: [{ page_id: "page-a", transcript_revision: 1, block_id: "block-a" }] });
const create = (ids = ["event-a"]) => call("memory.proposal.create", applied({
  proposal: { proposal_id: "proposal-a", page_id: "page-a", knowledge_events: ids.map((event_id) => ({ event_id, revision: 1 })) },
  knowledge: { adopted: ids.map((id) => ({ ...event(id), sample_ids: ["sample-a"] })) },
}, [ref("memory_proposal", "proposal-a"), ...ids.map((id) => ref("experiment_event", id))]));
const cards = (records = [event("event-a")]) => call("query.samples.list", applied({
  samples: [{ sample_id: "sample-a", records }], unassigned_records: [],
}, [ref("sample", "sample-a")]));

test("source text and a successful natural answer never manufacture saved samples", () => {
  const result = knowledgeDelivery([], [], { pages: [{ page_id: "page-a" }],
    source_pages: [{ page_id: "page-a", blocks: [{ text: "已创建 sample-a / event-a" }] }] });
  assert.equal(result.state, "not_written");
  assert.equal(result.intent, "unspecified");
  assert.deepEqual(result.event_ids, []);
  assert.deepEqual(result.sample_ids, []);
  assert.deepEqual(result.unrecorded_page_ids, ["page-a"]);
  assert.match(result.notice, /聊天答复不能证明/);
});

test("adopted objects require matching actual readback, with partial readback visible", () => {
  const result = knowledgeDelivery([create(["event-a", "event-b"]), cards()], [],
    { pages: [{ page_id: "page-a" }, { page_id: "page-b" }] }, "organize");
  assert.equal(result.state, "saved_unverified");
  assert.deepEqual(result.event_ids, ["event-a", "event-b"]);
  assert.deepEqual(result.verified_event_ids, ["event-a"]);
  assert.deepEqual(result.verified_sample_ids, ["sample-a"]);
  assert.deepEqual(result.unrecorded_page_ids, ["page-b"]);
  assert.equal(result.write_attempts, 1);
  assert.match(result.notice, /2条事件，其中1条已读回/);
});

test("existing-card read-only query proves saved objects without claiming new writes", () => {
  const result = knowledgeDelivery([cards()], [], {}, "query");
  assert.equal(result.state, "read_only");
  assert.equal(result.write_attempts, 0);
  assert.deepEqual(result.event_ids, []);
  assert.deepEqual(result.sample_ids, ["sample-a"]);
  assert.equal(result.readback_performed, true);
});

test("declared organize intent can finish by reusing already persisted knowledge", () => {
  const result = knowledgeDelivery([cards()], [], {}, "organize");
  assert.equal(result.state, "read_only");
  assert.equal(result.intent, "organize");
  assert.deepEqual(result.pending, []);
});

test("failed, pending and unapplied review receipts cannot masquerade as adopted knowledge", () => {
  const result = knowledgeDelivery([
    call("memory.proposal.create", { action_status: "proposed", object_refs: [ref("experiment_event", "fake")] }),
    call("memory.proposal.create", applied({}), { state: "pending" }),
    call("memory.proposal.create", applied({}), { isError: true }),
  ], [], {}, "organize");
  assert.equal(result.state, "not_written");
  assert.deepEqual(result.event_ids, []);
  assert.equal(result.pending.length, 3);
  assert.equal(result.write_attempts, 3);
});

test("persisted proposal without adopted events remains not written knowledge", () => {
  const result = knowledgeDelivery([call("memory.proposal.create", applied({
    proposal: { proposal_id: "review-p", page_id: "page-a", status: "awaiting_confirmation", items: [] },
  }, [ref("memory_proposal", "review-p")]))], [], {}, "organize");
  assert.equal(result.state, "not_written");
  assert.deepEqual(result.proposal_ids, ["review-p"]);
});

test("completed specialist prose and refs alone cannot prove a knowledge write", () => {
  const child = { run_id: "child", status: "completed", text: "已创建全部样品。",
    object_refs: [ref("experiment_event", "invented")], toolCalls: [] };
  const result = knowledgeDelivery([call("agent.delegate", child)], [child], {}, "organize");
  assert.equal(result.state, "not_written");
  assert.deepEqual(result.event_ids, []);
});

test("real delegated write and readback are aggregated once with an actionable parent receipt", () => {
  const child = { run_id: "child", status: "completed", text: "sample-a", toolCalls: [create(), cards()],
    knowledge_delivery: knowledgeDelivery([create(), cards()], [], {}, "organize") };
  const result = knowledgeDelivery([call("agent.delegate", delegationReceipt(child))], [child], {}, "organize");
  assert.equal(result.state, "verified");
  assert.deepEqual(result.event_ids, ["event-a"]);
  assert.equal(result.write_attempts, 1);
  assert.equal(delegationReceipt(child).knowledge_delivery.state, "verified");
});

test("a stale event read does not verify a later correction revision", () => {
  const result = knowledgeDelivery([
    call("memory.event.correct", applied({ event_id: "event-a", revision: 3 }, [ref("experiment_event", "event-a", 3)])),
    call("memory.event.read", applied({ entity_id: "event-a", revision: 2, field_json: event("event-a", 2) })),
  ], [], {}, "organize");
  assert.equal(result.state, "saved_unverified");
  assert.deepEqual(result.verified_event_ids, []);
});

test("card readback includes standalone source notes across pages", () => {
  const written = create(["event-a", "source-note"]);
  written.result.result.proposal.page_id = "page-b";
  const note = { ...event("source-note"), page_id: "page-b", sample_id: null,
    evidence_refs: [{ page_id: "page-b", transcript_revision: 1, block_id: "block-b" }] };
  const read = cards();
  read.result.result.source_notes = [note];
  const result = knowledgeDelivery([written, read], [], { pages: ["page-a", "page-b"] }, "organize");
  assert.equal(result.state, "verified");
  assert.deepEqual(result.verified_event_ids, ["event-a", "source-note"]);
  assert.deepEqual(result.unrecorded_page_ids, []);
  assert.deepEqual(result.verified_sample_ids, ["sample-a"]);
});

test("search candidates verify typed event revisions and ignore transcript hits", () => {
  const search = call("query.events.search", applied({ candidates: [
    { object_type: "transcript_block", object_id: "not-an-event", revision: 10, page_id: "page-c",
      payload: { fields: { sample_id: "fake-sample" } } },
    { object_type: "experiment_event", object_id: "event-a", revision: 2, page_id: "page-b",
      payload: { fields: { sample_id: "sample-a", sample_ids: ["sample-a", "sample-b"] },
        evidence_refs: [{ page_id: "page-b", transcript_revision: 1, block_id: "block-b" }] } },
  ] }));
  const correction = call("memory.event.correct", applied({ event_id: "event-a", revision: 2 },
    [ref("experiment_event", "event-a", 2)]));
  const result = knowledgeDelivery([correction, search], [], {}, "organize");
  assert.equal(result.state, "verified");
  assert.deepEqual(result.read_event_revisions, { "event-a": 2 });
  assert.deepEqual(result.sample_ids, ["sample-a", "sample-b"]);
  assert.deepEqual(result.recorded_page_ids, ["page-b"]);
  assert.deepEqual(knowledgeDelivery([search], [], {}, "query").state, "read_only");
});

test("stale search candidates cannot verify corrections", () => {
  const result = knowledgeDelivery([
    call("memory.event.correct", applied({ event_id: "event-a", revision: 3 })),
    call("query.events.search", applied({ candidates: [
      { object_type: "experiment_event", object_id: "event-a", revision: 2, payload: { fields: {} } },
    ] })),
  ]);
  assert.equal(result.state, "saved_unverified");
  assert.deepEqual(result.event_revisions, { "event-a": 3 });
  assert.deepEqual(result.verified_event_ids, []);
});

test("grouped card records only verify related events with explicit revisions", () => {
  const written = create(["event-a", "event-b"]);
  written.result.object_refs = [ref("experiment_event", "event-a"), ref("experiment_event", "event-b", 2)];
  written.result.result.proposal.knowledge_events[1].revision = 2;
  written.result.result.knowledge.adopted[1].revision = 2;
  const record = { ...event("event-a"), related_event_ids: ["event-a", "event-b"] };
  const legacy = knowledgeDelivery([written, cards([record])]);
  assert.equal(legacy.state, "saved_unverified");
  assert.deepEqual(legacy.verified_event_ids, ["event-a"]);
  record.related_events = [{ event_id: "event-a", revision: 1 }, { event_id: "event-b", revision: 1 }];
  assert.equal(knowledgeDelivery([written, cards([record])]).state, "saved_unverified");
  record.related_events[1].revision = 2;
  const result = knowledgeDelivery([written, cards([record])]);
  assert.equal(result.state, "verified");
  assert.deepEqual(result.read_event_revisions, { "event-a": 1, "event-b": 2 });
});

test("unversioned event references do not prove a version readback", () => {
  const result = knowledgeDelivery([
    call("memory.proposal.create", applied({}, [{ kind: "experiment_event", id: "event-a" }])),
    call("memory.event.read", applied({}, [{ kind: "experiment_event", id: "event-a" }])),
  ]);
  assert.equal(result.state, "saved_unverified");
  assert.deepEqual(result.verified_event_ids, []);
});

test("verified writes retain visible partial failures and pages without event receipts", () => {
  const written = create();
  written.result.result.knowledge.skipped = [{ item_key: "skipped-item", reason: "explicitly_declined" }];
  const result = knowledgeDelivery([written, cards(),
    call("memory.proposal.create", { action_status: "failed", ok: false }),
  ], [], { pages: ["page-a", "page-b"] }, "organize");
  assert.equal(result.state, "verified", "state describes written event readback, not full task completion");
  assert.equal(result.failed_write_attempts, 1);
  assert.equal(result.skipped_item_count, 1);
  assert.match(result.notice, /1次写入未确认或未应用、1项未采纳/);
  assert.match(result.notice, /1页尚无知识事件关联回执/);
  assert.deepEqual(result.unrecorded_page_ids, ["page-b"]);
});

test("a repeated proposal with later corrected knowledge reports reuse at its current revision", () => {
  const written = create();
  written.result.result.idempotent = true;
  written.result.result.knowledge.adopted[0].existing = true;
  written.result.result.knowledge.adopted[0].revision = 3;
  written.result.result.proposal.knowledge_events[0].revision = 3;
  const result = knowledgeDelivery([written, cards([event("event-a", 3)])]);
  assert.equal(result.state, "verified");
  assert.deepEqual(result.reused_event_ids, ["event-a"]);
  assert.deepEqual(result.event_revisions, { "event-a": 3 });
  assert.match(result.notice, /1条事件为已存在知识的复用回执/);
});

test("real knowledge readback through a proposal retains multiblock source and unresolved gaps", () => {
  const written = create();
  written.result.unknowns = ["operator_not_recorded"];
  written.result.result.knowledge.skipped = [{ item_key: "rejected-fact", reason: "explicitly_declined" }];
  const result = knowledgeDelivery([written, call("memory.proposal.read", applied({ proposal: {
    proposal_id: "proposal-a", page_id: "page-a", items: [{ event_id: "event-a", event_revision: 1,
      current_event: { ...event("event-a"), sample_ids: ["sample-a", "sample-b"] },
      evidence_refs: [{ page_id: "page-b", block_id: "title-b", transcript_revision: 1 }] }],
  } }))], [], {}, "organize");
  assert.equal(result.state, "verified");
  assert.deepEqual(result.sample_ids, ["sample-a", "sample-b"]);
  assert.ok(result.recorded_page_ids.includes("page-b"));
  assert.equal(result.pending.length, 2);
});

const reply = (value) => new Response(JSON.stringify(value));
const aiCall = (name, args) => fauxAssistantMessage(fauxToolCall(name, args));
const aiText = (text) => fauxAssistantMessage(fauxText(text));
function modelFactory(scripts) {
  return ({ role }) => {
    const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test", reasoning: false,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(scripts[role]);
    return { model: faux.getModel(), streamFn: (m, context, options) => faux.provider.streamSimple(m, context, options) };
  };
}
const spec = (name, mutates) => ({ type: "function", function: { name, description: name, mutates,
  parameters: { type: "object", properties: {}, additionalProperties: true } } });

test("Pi micro-record cycle delegates capture, preserves staged conditions, reads back, then queries without rewriting", async () => {
  const operations = [], source = "S-R1先90℃2h，后120℃3h，累计5h；明日测光谱。";
  const item = { item_key: "staged-treatment", event_kind: "operation", raw_text: source,
    fields: { sample_label: { normalized: "S-R1" }, stages: [
      { temperature: { normalized: 90, unit: "℃" }, duration: { normalized: 2, unit: "h" } },
      { temperature: { normalized: 120, unit: "℃" }, duration: { normalized: 3, unit: "h" } },
    ], total_duration: { normalized: 5, unit: "h" } }, occurred_at: null,
    evidence_refs: [{ page_id: "page-a", transcript_revision: 1, block_id: "block-a" }] };
  let persisted = false;
  const gateway = async (url, options) => {
    const path = new URL(url).pathname, body = options.body ? JSON.parse(options.body) : {};
    if (path === "/v2/agent/tools") return reply({ tools: [spec("query.samples.list", false),
      spec("memory.proposal.create", true), spec("memory.proposal.read", false)] });
    if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_capture" }] });
    if (path === "/v2/agent/delegate") return reply({ task_id: "task-child", run_id: "run-child", run_token: "child-secret",
      agent_id: "b_capture", claim_epoch: 1, manifest: { max_delegation_depth: 0 } });
    if (path === "/v2/agent/delegate/complete") return reply({ run: { status: "succeeded" } });
    operations.push(path);
    if (path.endsWith("memory.proposal.create")) {
      assert.deepEqual(body.params.items[0], item);
      assert.equal(persisted, false, "the micro record is written just once");
      persisted = true;
      return reply(create().result);
    }
    if (path.endsWith("query.samples.list")) return reply(persisted ? cards().result : applied({ samples: [] }));
    if (path.endsWith("memory.proposal.read")) return reply(applied({ proposal: {
      proposal_id: "proposal-a", page_id: "page-a", items: [{ ...item, event_id: "event-a", event_revision: 1,
        current_event: event("event-a") }], knowledge_events: [{ event_id: "event-a", revision: 1 }],
    } }));
    throw new Error(`unexpected ${path}`);
  };
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", role: "orchestrator", fetchImpl: gateway,
    modelFactory: modelFactory({ orchestrator: [aiCall("agent.delegate", { agent_id: "b_capture", message: "整理page-a，先查已有、只补缺失并读回验收。" }),
      (context) => {
        const receipt = JSON.parse(context.messages.at(-1).content[0].text);
        assert.equal(receipt.knowledge_delivery.state, "verified");
        return aiCall("agent.respond", { status: "completed", knowledge_intent: "organize", text: "S-R1已归卡；90℃2h→120℃3h，累计5h。明日光谱仍是计划。" });
      }], b_capture: [aiCall("query.samples.list", {}),
      aiCall("memory.proposal.create", { project_id: "project-a", page_id: "page-a", transcript_revision: 1,
        profile_id: "chemistry_notebook", items: [item] }),
      aiCall("memory.proposal.read", { proposal_id: "proposal-a" }), aiCall("query.samples.list", {}),
      aiCall("agent.respond", { status: "completed", knowledge_intent: "organize", text: "读回sample-a/event-a；分段处理及来源保留。" })] }) });
  const result = await run({ session_id: "s", message: "整理记录", context: { project_id: "project-a",
    pages: [{ page_id: "page-a" }], source_pages: [{ page_id: "page-a", transcript_revision: 1, blocks: [{ block_id: "block-a", text: source }] }] } });
  assert.equal(result.status, "completed");
  assert.equal(result.knowledge_delivery.intent, "organize");
  assert.equal(result.knowledge_delivery.state, "verified");
  assert.deepEqual(result.knowledge_delivery.event_ids, ["event-a"]);
  assert.deepEqual(result.knowledge_delivery.verified_sample_ids, ["sample-a"]);
  assert.match(result.text, /90℃2h→120℃3h/);
  const query = createRuntime({ gatewayUrl: "http://core", token: "query-secret", role: "b_query", fetchImpl: gateway,
    modelFactory: modelFactory({ b_query: [aiCall("query.samples.list", {}),
      aiCall("agent.respond", { status: "completed", knowledge_intent: "query", text: "已有S-R1分段条件如上，未重复建档。" })] }) });
  const answered = await query({ session_id: "s", message: "对比已有条件" });
  assert.equal(answered.knowledge_delivery.state, "read_only");
  assert.equal(answered.knowledge_delivery.write_attempts, 0);
  assert.equal(operations.filter((path) => path.endsWith("memory.proposal.create")).length, 1);
});

test("runtime natural completion exposes not_written without failing a legitimate read-only answer", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", role: "orchestrator",
    fetchImpl: async (url) => reply(new URL(url).pathname.endsWith("roles") ? { roles: [] } : { tools: [] }),
    modelFactory: modelFactory({ orchestrator: [aiText("原件显示样品有两步处理。这里仅答复问题。")] }) });
  const result = await run({ session_id: "s", message: "解释原件", context: { pages: [{ page_id: "page-a" }] } });
  assert.equal(result.status, "completed");
  assert.equal(result.knowledge_delivery.intent, "unspecified");
  assert.equal(result.knowledge_delivery.state, "not_written");
});

test("interrupted receipt summary preserves saved/readback objects without fabricating a scientific answer", () => {
  const delivery = knowledgeDelivery([create(["event-a", "event-b"]), cards()], [],
    { pages: ["page-a", "page-b"] }, "organize");
  const before = structuredClone(delivery);
  const text = receiptSummary(delivery, { reason: "预算收尾", progress: { pages: [{ unlinked_block_ids: ["b1", "b2"] }] } });
  assert.match(text, /预算收尾/);
  assert.match(text, /写入回执2条，其中1条/);
  assert.match(text, /proposal-a/);
  assert.match(text, /event-a/);
  assert.match(text, /page-b/);
  assert.match(text, /2个当前来源块/);
  assert.match(text, /不证明整页事实完整/);
  assert.deepEqual(delivery, before, "formatting never changes the result state or receipts");
});

test("no-write and unknown effects stay visible in the interrupted summary", () => {
  const delivery = knowledgeDelivery([call("memory.proposal.create", { action_status: "unknown" }, { state: "pending" })], [],
    { pages: ["page-a"] }, "organize");
  const text = receiptSummary(delivery, { progress: { requires_review: true } });
  assert.match(text, /尚无知识写入回执/);
  assert.match(text, /效果未知/);
  assert.match(text, /不能重放/);
  assert.doesNotMatch(text, /已完成整理|已归卡全部/);
});

test("summary bounds displayed identifiers while preserving actual counts", () => {
  const text = receiptSummary({ state: "saved_unverified", source_page_ids: ["page-a"],
    event_ids: ["one", "two", "three"], verified_event_ids: [] }, { maxRefs: 1 });
  assert.match(text, /写入回执3条/);
  assert.match(text, /one等3项/);
  assert.doesNotMatch(text, /two/);
});

test("summary exposes missing source inventory even when its block list is empty", () => {
  const text = receiptSummary(knowledgeDelivery([], [], { pages: ["page-a"] }, "organize"),
    { progress: { pages: [{ page_id: "page-a", source_inventory_complete: false, unlinked_block_ids: [] }] } });
  assert.match(text, /1页尚无完整来源块清单/);
  assert.match(text, /不能将空清单视为整理完成/);
});
