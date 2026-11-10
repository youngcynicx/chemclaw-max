import test from "node:test";
import assert from "node:assert/strict";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall, getCurrentTools } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

const reply = (value, status = 200) => new Response(JSON.stringify(value), { status });
const text = (value) => fauxAssistantMessage(fauxText(value));
const call = (name, args = {}) => fauxAssistantMessage(fauxToolCall(name, args));
const read = () => call("evidence.page.read", { page_id: "actual-source" });
const finish = (status = "completed") => call("agent.respond", { status, text: `Model-declared ${status} after checking actual receipts.` });
const specs = [
  { type: "function", function: { name: "evidence.page.read", mutates: false, description: "read",
    parameters: { type: "object", properties: { page_id: { type: "string" } }, required: ["page_id"] } } },
  { type: "function", function: { name: "example.write", mutates: true, description: "write",
    parameters: { type: "object", properties: {} } } },
];
const payload = (saved, durable = true) => ({ session_id: "status-finalization", message: "Read the sources and deliver the result.",
  durable, context: { run_id: "status-run" }, ...(saved ? { resume: { checkpoints: { "status-run": saved } } } : {}) });
function factory(responses, observe) {
  return () => {
    const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test", reasoning: false,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(responses);
    return { model: faux.getModel(), streamFn: (model, context, options) => {
      observe?.(context); return faux.provider.streamSimple(model, context, options);
    } };
  };
}
function fixture({ failCheckpoint, review = false, readsFail = 1 } = {}) {
  const state = { reads: 0, writes: 0, requests: 0, saved: null, checkpoints: [] };
  state.fetch = async (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/v2/agent/tools") return reply({ tools: specs });
    if (path === "/v2/agent/checkpoint") {
      const body = JSON.parse(options.body);
      if (failCheckpoint?.(body)) return reply({ error: { code: "fixture_checkpoint_failure" } }, 500);
      state.saved = structuredClone(body.checkpoint); state.checkpoints.push(state.saved);
      return reply({ ok: true });
    }
    if (path.endsWith("evidence.page.read")) {
      state.reads += 1;
      if (state.reads <= readsFail) throw new TypeError("fetch failed");
      return reply({ action_status: "applied", result: { source: "actually read" } });
    }
    if (path.endsWith("example.write")) {
      state.writes += 1;
      return reply(review ? { action_status: "proposed", needs_confirmation: true }
        : { action_status: "applied", object_refs: [{ kind: "event", id: "already-written" }] });
    }
    throw new Error(`Unexpected gateway request ${path}`);
  };
  return state;
}
function runtime(state, responses, options = {}) {
  return createRuntime({ gatewayUrl: "http://core", token: "fixture-token", fetchImpl: state.fetch,
    modelFactory: factory(responses, () => { state.requests += 1; }), ...options });
}
function assertFinalization(context) {
  assert.deepEqual(getCurrentTools(context.messages).map((tool) => tool.name).sort(), ["agent.respond", "evidence.page.read"]);
  const prompt = JSON.stringify(context.messages.at(-1).content);
  assert.ok(prompt.includes("missing_status_declaration"));
  assert.ok(prompt.includes("有效自然正文"));
  assert.ok(prompt.includes("不能新增"));
}

test("durable mixed read receipts require model status declaration without replaying a write", async () => {
  const state = fixture();
  const result = await runtime(state, [call("example.write"), read(), read(), text("A substantive natural result."),
    (context) => { assertFinalization(context); return call("example.write"); }, finish()])(payload());
  assert.equal(result.status, "completed");
  assert.equal(result.completion.mode, "explicit");
  assert.equal(result.completion.repair_reason, "missing_status_declaration");
  assert.equal(result.completion.repair_attempts, 1);
  assert.equal(state.writes, 1); assert.equal(state.reads, 2);
  assert.ok(result.toolCalls.some((entry) => entry.name === "evidence.page.read" && entry.isError));
  assert.ok(result.object_refs.some((ref) => ref.id === "already-written"));
});

for (const status of ["partial", "failed"]) test(`model can explicitly retain ${status} after mixed receipts`, async () => {
  const state = fixture();
  const result = await runtime(state, [read(), read(), text("Result with possible gaps."), finish(status)])(payload());
  assert.equal(result.status, status);
  assert.equal(result.text, `Model-declared ${status} after checking actual receipts.`);
  assert.equal(result.completion.repair_reason, "missing_status_declaration");
});

test("another natural body cannot locally clear failures or open unbounded finalization", async () => {
  const state = fixture();
  const result = await runtime(state, [read(), read(), text("Original body."), text("Another natural body."),
    () => { throw new Error("No third completion round"); }])(payload());
  assert.equal(state.requests, 4);
  assert.equal(result.status, "partial");
  assert.equal(result.text, "Another natural body.");
  assert.equal(result.completion.status_declaration, "missing");
  assert.equal(result.completion.repair_attempts, 1);
  const finished = result.events.find((event) => event.type === "completion_repair_finished");
  assert.equal(finished.valid, false); assert.equal(finished.code, "missing_status_declaration");
});

test("non-durable historical audit remains partial without an extra model round", async () => {
  const state = fixture();
  const result = await runtime(state, [read(), read(), text("Legacy body."),
    () => { throw new Error("Non-durable audit must not gain automatic rounds"); }])(payload(undefined, false));
  assert.equal(result.status, "partial"); assert.equal(result.completion.mode, "legacy");
  assert.equal(result.completion.repair_attempts, 0); assert.equal(state.requests, 3);
});

test("zero remaining budget preserves natural body and partial status without another request", async () => {
  const state = fixture();
  await runtime(state, [read(), read(), text("Saved natural result.")], { maxCompletionRepairs: 0 })(payload());
  const saved = structuredClone(state.saved); saved.settled = false; delete saved.result;
  const requests = state.requests;
  const result = await runtime(state, [() => { throw new Error("No remaining tool budget"); }], { maxToolCalls: 0 })(payload(saved));
  assert.equal(result.status, "partial"); assert.equal(result.text, "Saved natural result.");
  assert.equal(result.completion.status_declaration, "missing");
  assert.equal(result.completion.repair_attempts, 0); assert.equal(state.requests, requests);
});

test("pending status finalization resumes its original reason and counted attempt without replay", async () => {
  let fail = true;
  const state = fixture({ failCheckpoint: (body) => fail && body.event?.type === "agent_started"
    && body.checkpoint.completion_repair?.pending });
  await assert.rejects(runtime(state, [call("example.write"), read(), read(), text("Natural body."), finish()])(payload()),
    (error) => error.code === "checkpoint_unavailable");
  const saved = structuredClone(state.saved);
  assert.equal(saved.completion_repair.reason, "missing_status_declaration");
  assert.equal(saved.completion_repair.attempts, 1);
  assert.equal(saved.completion_repair.pending, true);
  fail = false;
  const result = await runtime(state, [(context) => { assertFinalization(context); return finish(); }])(payload(saved));
  assert.equal(result.status, "completed");
  assert.equal(result.completion.repair_attempts, 1);
  assert.equal(result.completion.repair_reason, "missing_status_declaration");
  assert.equal(state.writes, 1); assert.equal(state.reads, 2);
});

test("settled natural partial checkpoint is retained without regenerating or replaying anything", async () => {
  const state = fixture();
  const first = await runtime(state, [read(), read(), text("Retained natural result.")], { maxCompletionRepairs: 0 })(payload());
  const run = createRuntime({ gatewayUrl: "http://core", token: "fixture-token",
    fetchImpl: async () => { throw new Error("Settled receipts must not replay"); },
    modelFactory: () => { throw new Error("Settled history must not regenerate"); } });
  const restored = await run(payload(state.saved));
  assert.equal(first.status, "partial"); assert.equal(restored.status, "partial");
  assert.equal(restored.text, first.text);
  assert.equal(first.completion.status_declaration, "missing");
  assert.equal(restored.completion.status_declaration, "missing");
  const withoutSavedFlag = structuredClone(state.saved);
  delete withoutSavedFlag.result.completion.status_declaration;
  const recomputed = await run(payload(withoutSavedFlag));
  assert.equal(recomputed.status, "partial");
  assert.equal(recomputed.completion.status_declaration, "missing");
});

test("settled natural success does not inherit an unsupported saved missing-status diagnostic", async () => {
  const state = fixture({ readsFail: 0 });
  await runtime(state, [read(), text("Verified natural success.")], { maxCompletionRepairs: 0 })(payload());
  const saved = structuredClone(state.saved);
  saved.result.completion.status_declaration = "missing";
  const run = createRuntime({ gatewayUrl: "http://core", token: "fixture-token",
    fetchImpl: async () => { throw new Error("Settled receipts must not replay"); },
    modelFactory: () => { throw new Error("Settled history must not regenerate"); } });
  const restored = await run(payload(saved));
  assert.equal(restored.status, "completed");
  assert.equal(restored.text, "Verified natural success.");
  assert.equal(restored.completion.status_declaration, undefined);
  assert.equal(state.reads, 1);
});

test("explicit completion cannot elevate an outstanding Core approval", async () => {
  const state = fixture({ review: true });
  const result = await runtime(state, [call("example.write"), read(), read(), text("A body awaiting approval."), finish()])(payload());
  assert.equal(result.completion.mode, "explicit");
  assert.equal(result.status, "needs_review");
  assert.equal(state.writes, 1);
});

test("model-declared completed cannot elevate an actually partial child", async () => {
  const state = fixture({ readsFail: 0 });
  const childResponse = finish("partial"), responseCall = childResponse.content[0];
  const childResult = { status: "partial", text: responseCall.arguments.text,
    completion: { mode: "explicit", protocol_version: 3 }, object_refs: [] };
  const childCheckpoint = { version: 3, completion_protocol_version: 3, turn_start: 0, settled: true,
    messages: [{ role: "user", content: [{ type: "text", text: "Child task" }], timestamp: 1 }, childResponse],
    final_response: responseCall.arguments, result: childResult,
    tool_calls: [{ name: "agent.respond", toolCallId: responseCall.id, state: "completed", isError: false,
      arguments: responseCall.arguments, result: { status: "response_recorded" } }] };
  const originalFetch = state.fetch;
  state.fetch = async (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
    if (path === "/v2/agent/delegate") return reply({ agent_id: "b_query", task_id: "child-task", run_id: "child-run",
      result: childResult, checkpoint: childCheckpoint });
    return originalFetch(url, options);
  };
  const result = await runtime(state, [read(), call("agent.delegate", { agent_id: "b_query", message: "Inspect source." }),
    text("Parent natural result."), (context) => { assertFinalization(context); return finish(); }],
    { role: "orchestrator" })(payload());
  assert.equal(result.completion.mode, "explicit");
  assert.equal(result.delegations[0].status, "partial");
  assert.equal(result.status, "partial");
});

test("missing final response and missing status declaration remain distinct diagnostics", async () => {
  const state = fixture();
  const result = await runtime(state, [text(""), finish()])(payload());
  assert.equal(result.status, "completed");
  assert.equal(result.completion.repair_reason, "missing_final_response");
});
