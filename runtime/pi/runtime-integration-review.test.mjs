import test from "node:test";
import assert from "node:assert/strict";
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

const ROOT = "independent-review-root", CHILD = "independent-review-child";
const call = (name, args = {}, id = name) => fauxAssistantMessage(fauxToolCall(name, args, { id }));
const respond = () => call("agent.respond", { status: "completed", text: "This response must not override an unknown mutation." });
const response = (value) => new Response(JSON.stringify(value));
function fixture({ parallelUnknown = false, settledUnknown = false, closeAtUnknown = false, genericUnknown = false } = {}) {
  const writeName = genericUnknown ? "example.write" : "memory.proposal.create";
  let clock = 100000, releaseBoth;
  const both = new Promise(resolve => { releaseBoth = resolve; });
  const state = { models: [], writes: [], delegations: 0, checkpoints: [], finishes: 0 };
  const pending = { name: writeName, state: "completed", isError: false,
    toolCallId: "legacy-child-write", operation_id: `${CHILD}/legacy-child-write`, arguments: {},
    result: { action_status: "unknown" } };
  const oldText = "An older host claimed complete despite an unknown child write.";
  const old = { version: 3, completion_protocol_version: 3, run_id: CHILD, role: "b_capture", settled: true,
    turn_start: 0, messages: [{ role: "user", content: "save one fact", timestamp: 1 }],
    final_response: { status: "completed", text: oldText }, tool_calls: [pending], events: [], delegations: [],
    execution_budget: { deadline_ms: 101000, work_deadline_ms: 100800, closing_reserve_ms: 200,
      remaining_tool_calls: 10, delegations_started: 1 },
    result: { status: "completed", text: oldText, toolCalls: [pending], delegations: [],
      completion: { protocol_version: 3, mode: "explicit" } } };
  const tools = [{ type: "function", function: { name: writeName, mutates: true,
    description: "Fixture mutation", parameters: { type: "object", properties: { marker: { type: "string" } } } } }];
  const fetchImpl = async (url, options) => {
    const path = new URL(url).pathname, body = options.body ? JSON.parse(options.body) : {};
    if (path === "/v2/agent/checkpoint") { state.checkpoints.push(structuredClone(body.checkpoint)); return response({ ok: true }); }
    if (path === "/v2/agent/tools") return response({ tools });
    if (path === "/v2/agent/roles") return response({ roles: [{ agent_id: "b_capture" }] });
    if (path === "/v2/agent/delegate") {
      state.delegations++;
      return response({ run_id: CHILD, task_id: "review-child-task", agent_id: "b_capture", claim_epoch: 1,
        manifest: { timeout_s: 900, max_delegation_depth: 0 },
        ...(settledUnknown ? { checkpoint: old, result: old.result } : { run_token: "independent-child-secret" }) });
    }
    if (path === "/v2/agent/delegate/complete") { state.finishes++; return response({ ok: true }); }
    if (path.endsWith(writeName)) {
      const role = options.headers.Authorization === "Bearer independent-child-secret" ? "child" : "root";
      state.writes.push({ role, operation_id: options.headers["X-Operation-Id"] });
      if (role === "child") {
        if (parallelUnknown) {
          if (state.writes.filter(write => write.role === "child").length === 2) releaseBoth();
          await both;
        }
        if (closeAtUnknown) clock = 100850;
        return response({ action_status: "unknown" });
      }
      return response({ ok: true, action_status: "applied" });
    }
    throw new Error(`Unexpected independent fixture path: ${path}`);
  };
  const modelFactory = ({ role }) => {
    const faux = fauxProvider({ provider: `independent-${role}`, models: [{ id: "fixture", name: "fixture",
      input: ["text"], reasoning: false, contextWindow: 128000, maxTokens: 8192 }] });
    const childWrite = parallelUnknown ? fauxAssistantMessage([
      fauxToolCall(writeName, { marker: "first" }, { id: "unknown-first" }),
      fauxToolCall(writeName, { marker: "second" }, { id: "unknown-second" }),
    ]) : call(writeName, { marker: "child" }, "unknown-child");
    faux.setResponses(role === "orchestrator" ? [
      call("agent.delegate", { agent_id: "b_capture", message: "Save one independently verifiable fact." }),
      call(writeName, { marker: "root-after-unknown" }),
      call("agent.delegate", { agent_id: "b_capture", message: "Unsafe follow-up delegation." }, "unsafe-second-delegate"),
      respond(),
    ] : [childWrite, respond()]);
    return { model: faux.getModel(), streamFn: (model, context, options) => {
      state.models.push(role);
      return faux.provider.streamSimple(model, context, options);
    } };
  };
  state.run = () => createRuntime({ gatewayUrl: "http://independent-core", token: "independent-root-secret",
    role: "orchestrator", timeoutMs: 1000, closingReserveMs: 200, now: () => clock,
    fetchImpl, modelFactory })({ durable: true, session_id: "independent-review-session",
    message: "Organize the source and report actual receipts.", context: { run_id: ROOT, pages: ["page-a"] } });
  return state;
}

test("unknown child mutation fences all subsequent parent inference, writes and delegation", async () => {
  const state = fixture();
  const result = await state.run();
  assert.deepEqual(state.models, ["orchestrator", "b_capture"]);
  assert.deepEqual(state.writes.map(write => write.role), ["child"]);
  assert.equal(state.delegations, 1);
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(result.capture_progress.requires_review, true);
  assert.equal(state.finishes, 1, "safe child completion remains available after the effect fence");
});

test("settled legacy child with unknown write cannot reopen parent model or side effects", async () => {
  const state = fixture({ settledUnknown: true });
  const result = await state.run();
  assert.deepEqual(state.models, ["orchestrator"]);
  assert.deepEqual(state.writes, []);
  assert.equal(state.delegations, 1);
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
});

test("parallel unknown child writes retain every operation as pending, never just the last", async () => {
  const state = fixture({ parallelUnknown: true });
  const result = await state.run();
  assert.deepEqual(state.models, ["orchestrator", "b_capture"]);
  assert.deepEqual(state.writes.map(write => write.role), ["child", "child"]);
  const child = result.delegations.find(entry => entry.run_id === CHILD);
  const unknowns = child.toolCalls.filter(entry => entry.name === "memory.proposal.create");
  assert.equal(unknowns.length, 2);
  assert.ok(unknowns.every(entry => entry.state === "pending"));
  assert.equal(new Set(unknowns.map(entry => entry.operation_id)).size, 2);
  assert.equal(result.capture_progress.unresolved_writes.length, 2);
  assert.equal(result.status, "needs_review");
});

test("unknown child effect wins over a simultaneous soft closing boundary", async () => {
  const state = fixture({ closeAtUnknown: true });
  const result = await state.run();
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.deepEqual(state.writes.map(write => write.role), ["child"]);
  assert.equal(result.capture_progress.requires_review, true);
});

test("an expired hard deadline cannot hide a retained pending knowledge write behind timeout", async () => {
  const clock = Date.now(), root = "expired-pending-root";
  let modelCalls = 0, effectCalls = 0;
  const pending = { name: "memory.proposal.create", toolCallId: "old-pending-write", operation_id: "old-operation",
    state: "pending", arguments: {}, result: null };
  const runtime = createRuntime({ gatewayUrl: "http://expired-fixture", token: "expired-fixture-secret",
    role: "orchestrator", timeoutMs: 900000, now: () => clock,
    modelFactory: async () => { modelCalls++; throw new Error("Must not call the model"); },
    fetchImpl: async (url) => {
      if (new URL(url).pathname === "/v2/agent/checkpoint") return response({ ok: true });
      effectCalls++; throw new Error("Must not issue a tool or lookup after expiry");
    } });
  const cp = { version: 3, completion_protocol_version: 3, run_id: root, role: "orchestrator",
    messages: [], tool_calls: [pending], events: [], delegations: [], turn_start: 0,
    execution_budget: { deadline_ms: clock - 10, remaining_tool_calls: 10, delegations_started: 0 } };
  const result = await runtime({ durable: true, session_id: "expired-fixture-session", message: "Reconcile original receipts.",
    context: { run_id: root }, resume: { checkpoints: { [root]: cp } } });
  assert.equal(modelCalls, 0); assert.equal(effectCalls, 0);
  assert.equal(result.status, "needs_review"); assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(result.capture_progress.requires_review, true); assert.equal(result.toolCalls[0].state, "pending");
  assert.equal(result.runtime_budget_hint.phase, "expired");
  assert.match(result.text, /只读核查/);
});


test("legacy settled unknown generic mutation is fenced independently of knowledge progress", async () => {
  const state = fixture({ settledUnknown: true, genericUnknown: true });
  const result = await state.run();
  assert.deepEqual(state.models, ["orchestrator"]);
  assert.deepEqual(state.writes, []);
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(result.capture_progress.requires_review, true);
});
