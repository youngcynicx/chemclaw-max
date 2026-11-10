import test from "node:test";
import assert from "node:assert/strict";
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";
import { closingReserve, runtimeBudgetHint } from "./runtime-budget.mjs";

const response = (data, status = 200) => new Response(JSON.stringify(data), { status });
const call = (name, args = {}, id = name) => fauxAssistantMessage(fauxToolCall(name, args, { id }));
const saved = (budget) => ({ version: 3, completion_protocol_version: 3, run_id: "budget-root", role: "orchestrator",
  turn_start: 0, messages: [], tool_calls: [], events: [], delegations: [], execution_budget: {
    deadline_ms: budget.deadline_ms, remaining_tool_calls: 10, delegations_started: 0, ...budget } });
const payload = (cp) => ({ durable: true, session_id: "budget-session", message: "Organize this source and report the actual receipts.",
  context: { run_id: "budget-root", pages: [{ page_id: "page-1" }] },
  ...(cp ? { resume: { checkpoints: { "budget-root": cp } } } : {}) });

function fixture(steps, options = {}) {
  const state = { writes: 0, models: [], checkpoints: [], completedChildren: [], hints: [] };
  const writeName = options.knowledgeWrite ? "memory.proposal.create" : "example.write";
  const event = { entity_id: "saved-one", revision: 1, field_json: { sample_ids: ["sample-one"] },
    evidence_refs: [{ page_id: "page-1", transcript_revision: 1, block_id: "block-one" }] };
  const tools = [{ type: "function", function: { name: writeName, mutates: true,
    description: "Save one fixture object", parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "example.read", mutates: false,
    description: "Read one fixture object", parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "memory.event.read", mutates: false,
    description: "Read the current fixture event", parameters: { type: "object", properties: {} } } }];
  const fetchImpl = async (url, init) => {
    const path = new URL(url).pathname, body = init.body ? JSON.parse(init.body) : null;
    if (path === "/v2/agent/checkpoint") { state.checkpoints.push(body.checkpoint); return response({ ok: true }); }
    if (path === "/v2/agent/tools") return response({ tools });
    if (path === "/v2/agent/roles") return response({ roles: [{ agent_id: "b_capture" }] });
    if (path === "/v2/agent/delegate") return response({ run_id: "budget-child", task_id: "child-task", agent_id: "b_capture",
      run_token: "fixture-child-secret", claim_epoch: 1, manifest: { timeout_s: 900 } });
    if (path === "/v2/agent/delegate/complete") { state.completedChildren.push(body); options.onChildComplete?.(); return response({ ok: true }); }
    if (path.endsWith(writeName)) {
      state.writes++;
      await options.onWrite?.(init.signal);
      if (options.unknownWrite) return response({ action_status: "unknown" });
      return response({ ok: true, action_status: "applied", object_refs: [{ kind: "experiment_event", id: "saved-one", revision: 1 }],
        ...(options.knowledgeWrite ? { result: { knowledge: { adopted: [{ ...event, event_id: event.entity_id }] } } } : {}) });
    }
    if (path.endsWith("memory.event.read")) return response({ ok: true, action_status: "applied", result: event });
    if (path.endsWith("example.read")) return response({ ok: true, action_status: "applied", result: { observed: true } });
    throw new Error(`Unexpected fixture gateway path: ${path}`);
  };
  const modelFactory = ({ role }) => {
    const faux = fauxProvider({ provider: `budget-${role}`, models: [{ id: "fixture", name: "fixture", reasoning: false,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    const sequence = Array.isArray(steps) ? steps : steps[role];
    faux.setResponses((sequence || []).map(step => async (context, streamOptions) => {
      state.models.push(role); state.hints.push(context.systemPrompt);
      return typeof step === "function" ? step(context, streamOptions) : step;
    }));
    return { model: faux.getModel(), streamFn: (model, context, streamOptions) => faux.provider.streamSimple(model, context, streamOptions) };
  };
  state.run = createRuntime({ gatewayUrl: "http://fixture-core", token: "fixture-root-secret", role: "orchestrator",
    fetchImpl, modelFactory, timeoutMs: 1000, closingReserveMs: 200, ...options.runtime });
  return state;
}

test("default reserve stays within the original timeout and can be disabled explicitly", () => {
  assert.equal(closingReserve(900000), 120000);
  assert.equal(closingReserve(165000), 24750);
  assert.equal(closingReserve(350), 0);
  assert.equal(closingReserve(900000, 0), 0);
  for (const invalid of [-1, NaN, Infinity, 1000]) assert.throws(() => closingReserve(1000, invalid));
  assert.deepEqual(runtimeBudgetHint(1000, 800, 850, 3), {
    shared_absolute_deadline_ms: 1000, work_deadline_ms: 800, remaining_ms: 150, remaining_seconds: 0,
    closing_reserve_ms: 200, available_work_ms: 0, phase: "closing", remaining_tool_calls: 3 });
});

test("safe-boundary closure preserves an issued write and prevents the next model call", async () => {
  let clock = 100000;
  const state = fixture([call("example.write"), () => { throw new Error("Must not start another inference"); }],
    { runtime: { now: () => clock }, onWrite: () => { clock += 850; } });
  const result = await state.run(payload());
  assert.equal(state.writes, 1); assert.equal(state.models.length, 1);
  assert.equal(result.error_code, "work_budget_exhausted");
  assert.equal(result.status, "partial");
  assert.ok(result.object_refs.some(ref => ref.id === "saved-one"));
  assert.ok(state.hints[0].includes('"available_work_ms":800'));
  assert.equal(state.checkpoints.at(-1).execution_budget.work_deadline_ms, 100800);
  assert.ok(!JSON.stringify(result).includes("fixture-root-secret"));
});

test("soft timer ends in-flight reasoning without consuming the hard closing reserve", { timeout: 5000 }, async () => {
  const state = fixture([async (_context, options) => {
    if (!options.signal.aborted) await new Promise(resolve => options.signal.addEventListener("abort", resolve, { once: true }));
    return fauxAssistantMessage("", { stopReason: "aborted" });
  }], { runtime: { timeoutMs: 1000, closingReserveMs: 500 } });
  const start = Date.now();
  const result = await state.run(payload());
  assert.equal(result.error_code, "work_budget_exhausted");
  assert.ok(Date.now() - start < 950);
  assert.ok(result.runtime_budget_hint.remaining_ms > 0);
  assert.equal(state.models.length, 1); assert.equal(state.writes, 0);
});

test("soft closure does not abort transport for a mutation already issued", { timeout: 5000 }, async () => {
  let transportAborted;
  const state = fixture([call("example.write"), fauxAssistantMessage("not reached")], {
    runtime: { timeoutMs: 1000, closingReserveMs: 500 },
    onWrite: async signal => {
      await new Promise(resolve => setTimeout(resolve, 650));
      transportAborted = signal.aborted;
    },
  });
  const result = await state.run(payload());
  assert.equal(transportAborted, false); assert.equal(state.writes, 1);
  assert.equal(result.status, "partial"); assert.equal(result.error_code, "work_budget_exhausted");
  assert.equal(result.toolCalls.find(item => item.name === "example.write").state, "completed");
  assert.ok(result.object_refs.some(ref => ref.id === "saved-one"));
});

test("delayed timer cannot permit a new mutation after the work boundary", async () => {
  let clock = 100000;
  const state = fixture([() => { clock += 850; return call("example.write"); }], { runtime: { now: () => clock } });
  const result = await state.run(payload());
  assert.equal(state.writes, 0); assert.equal(result.error_code, "work_budget_exhausted");
  assert.notEqual(result.status, "completed");
});

test("delayed timer cannot create a new delegation after the work boundary", async () => {
  let clock = 100000;
  const state = fixture([() => { clock += 850; return call("agent.delegate", { agent_id: "b_capture", message: "late work" }); }],
    { runtime: { now: () => clock } });
  const result = await state.run(payload());
  assert.deepEqual(state.models, ["orchestrator"]); assert.equal(result.delegations.length, 0);
  assert.equal(result.error_code, "work_budget_exhausted");
});

test("children share both root deadlines and cannot continue parent inference after soft closure", async () => {
  let clock = 100000;
  const state = fixture({
    orchestrator: [call("agent.delegate", { agent_id: "b_capture", message: "write one object" }), fauxAssistantMessage("not reached")],
    b_capture: [call("example.write")],
  }, { runtime: { now: () => clock }, onWrite: () => { clock += 850; } });
  const result = await state.run(payload());
  assert.deepEqual(state.models, ["orchestrator", "b_capture"]);
  const budgets = state.checkpoints.map(cp => cp.execution_budget);
  assert.equal(new Set(budgets.map(b => b.deadline_ms)).size, 1);
  assert.equal(new Set(budgets.map(b => b.work_deadline_ms)).size, 1);
  assert.equal(result.status, "partial"); assert.equal(state.writes, 1);
  assert.equal(state.completedChildren.length, 1);
});

test("resume keeps the original expired work boundary and starts no model", async () => {
  const cp = saved({ deadline_ms: 101600, work_deadline_ms: 100900, closing_reserve_ms: 700 });
  const state = fixture([fauxAssistantMessage("not reached")], { runtime: { now: () => 101000 } });
  const result = await state.run(payload(cp));
  assert.equal(state.models.length, 0); assert.equal(result.error_code, "work_budget_exhausted");
  assert.equal(state.checkpoints.at(-1).execution_budget.deadline_ms, 101600);
  assert.equal(state.checkpoints.at(-1).execution_budget.work_deadline_ms, 100900);
});

test("legacy checkpoints retain hard-only execution rather than gaining a new reserve policy", async () => {
  const cp = saved({ deadline_ms: 101600 });
  const state = fixture([fauxAssistantMessage("Legacy run completed within its original deadline.")],
    { runtime: { now: () => 101000 } });
  const result = await state.run(payload(cp));
  assert.equal(result.status, "completed"); assert.equal(state.models.length, 1);
  assert.equal(result.runtime_budget_hint.closing_reserve_ms, 0);
});

test("corrupt soft-budget metadata is refused before any model call", async () => {
  const cp = saved({ deadline_ms: 101600, work_deadline_ms: 101700, closing_reserve_ms: 100 });
  const state = fixture([fauxAssistantMessage("not reached")], { runtime: { now: () => 101000 } });
  await assert.rejects(state.run(payload(cp)), { code: "checkpoint_invalid" });
  assert.equal(state.models.length, 0); assert.equal(state.writes, 0);
});

test("unknown mutation outcome remains reviewable at soft closure, never counted as a successful write", async () => {
  let clock = 100000;
  const state = fixture([call("example.write")], { runtime: { now: () => clock },
    unknownWrite: true, onWrite: () => { clock += 850; } });
  const result = await state.run(payload());
  assert.equal(result.status, "needs_review"); assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(result.toolCalls[0].state, "pending"); assert.equal(state.writes, 1);
});

test("known saved knowledge appears in soft-budget delivery without inventing a readback", async () => {
  let clock = 100000;
  const state = fixture([call("memory.proposal.create")], { knowledgeWrite: true,
    runtime: { now: () => clock }, onWrite: () => { clock += 850; } });
  const result = await state.run(payload());
  assert.equal(result.status, "partial");
  assert.equal(result.knowledge_delivery.state, "saved_unverified");
  assert.match(result.text, /知识写入回执1条，其中0条/);
  assert.match(result.text, /saved-one/);
  assert.equal(state.checkpoints.at(-1).capture_progress.saved_event_refs[0].event_id, "saved-one");
});

test("a successful child readback survives parent closing and is visible with its genuine references", async () => {
  let clock = 100000;
  const state = fixture({
    orchestrator: [call("agent.delegate", { agent_id: "b_capture", message: "save and read back one fact" }), fauxAssistantMessage("not reached")],
    b_capture: [call("memory.proposal.create"), call("memory.event.read"), fauxAssistantMessage("The fixture object was saved and read back.")],
  }, { knowledgeWrite: true, runtime: { now: () => clock }, onChildComplete: () => { clock += 850; } });
  const result = await state.run(payload());
  assert.equal(result.status, "partial");
  assert.equal(result.delegations[0].status, "completed");
  assert.equal(result.knowledge_delivery.state, "verified");
  assert.match(result.text, /知识写入回执1条，其中1条/);
  assert.match(result.text, /saved-one/);
  assert.equal(result.capture_progress.verified_event_refs[0].event_id, "saved-one");
  assert.deepEqual(state.models, ["orchestrator", "b_capture", "b_capture", "b_capture"]);
});
