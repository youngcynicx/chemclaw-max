import test from "node:test";
import assert from "node:assert/strict";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall, getCurrentTools } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

const reply = (value, status = 200) => new Response(JSON.stringify(value), { status });
const text = (value) => fauxAssistantMessage(fauxText(value));
const call = (name, args = {}) => fauxAssistantMessage(fauxToolCall(name, args));
const finish = () => call("agent.respond", { status: "completed", text: "verified final result" });
const specs = [
  { type: "function", function: { name: "example.write", mutates: true, description: "write",
    parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "evidence.page.read", mutates: false, description: "read",
    parameters: { type: "object", properties: { page_id: { type: "string" } }, required: ["page_id"] } } },
];
const payload = (checkpoint) => ({ session_id: "repair", message: "verify the experiment source", durable: true,
  context: { run_id: "repair-run" }, ...(checkpoint ? { resume: { checkpoints: { "repair-run": checkpoint } } } : {}) });
function factory(responses, observe) {
  return () => {
    const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test", reasoning: false,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(responses);
    return { model: faux.getModel(), streamFn: (model, context, options) => {
      observe?.(context);
      return faux.provider.streamSimple(model, context, options);
    } };
  };
}
function fixture({ failCheckpoint } = {}) {
  const state = { writes: 0, reads: 0, saved: null, operations: [] };
  state.fetch = async (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/v2/agent/tools") return reply({ tools: specs });
    if (path === "/v2/agent/checkpoint") {
      const body = JSON.parse(options.body);
      if (failCheckpoint?.(body)) return reply({ error: { code: "fixture_failure" } }, 500);
      state.saved = structuredClone(body.checkpoint); return reply({ ok: true });
    }
    state.operations.push({ path, id: options.headers["X-Operation-Id"] });
    if (path.endsWith("example.write")) state.writes += 1;
    else if (path.endsWith("evidence.page.read")) state.reads += 1;
    else throw new Error(`unexpected ${path}`);
    return reply({ action_status: "applied", object_refs: [{ kind: "event", id: "existing-event" }] });
  };
  return state;
}
const create = (fixture, responses, options = {}) => createRuntime({ gatewayUrl: "http://core", token: "fixture-token",
  fetchImpl: fixture.fetch, modelFactory: factory(responses), ...options });
function assertReadRepairTools(context) {
  const names = getCurrentTools(context.messages).map((tool) => tool.name).sort();
  assert.deepEqual(names, ["agent.respond", "evidence.page.read"]);
}

test("ordinary visible stop is delivered naturally without an unnecessary repair turn", async () => {
  const state = fixture();
  let requests = 0;
  const run = create(state, [], { modelFactory: factory([text("substantive body without formal submission"), () => {
    throw new Error("normal stop must not open a repair turn");
  }], () => { requests += 1; }) });
  const result = await run(payload());
  assert.equal(result.status, "completed");
  assert.equal(result.text, "substantive body without formal submission");
  assert.equal(result.completion.mode, "natural");
  assert.equal(result.completion.repair_attempts, 0); assert.equal(requests, 1);
  assert.ok(!result.events.some((event) => event.type === "completion_repair_started"));
});

test("length thinking-only output can be repaired with source read then explicit respond", async () => {
  const state = fixture();
  const cut = fauxAssistantMessage("", { stopReason: "length" });
  cut.content = [{ type: "thinking", thinking: "PRIVATE_FIXTURE_THINKING" }];
  const run = create(state, [cut, (context) => {
    assertReadRepairTools(context);
    const prompt = JSON.stringify(context.messages.at(-1).content);
    for (const expected of ["实际读取", "真实工具回执", "历史摘要", "不能新增", "agent.respond"]) assert.ok(prompt.includes(expected));
    return call("evidence.page.read", { page_id: "actual-page" });
  }, finish()], { maxTaskContinuations: 0 });
  const result = await run(payload());
  assert.equal(result.status, "completed");
  assert.equal(state.reads, 1); assert.equal(state.writes, 0);
  assert.equal(result.completion.repair_attempts, 1);
  assert.ok(!JSON.stringify(result).includes("PRIVATE_FIXTURE_THINKING"));
});

test("one unsuccessful empty repair remains failed and cannot open unbounded turns", async () => {
  const state = fixture(); let requests = 0;
  const run = create(state, [],
    { modelFactory: factory([text(""), text("")], () => { requests += 1; }) });
  const result = await run(payload());
  assert.equal(result.status, "failed");
  assert.equal(result.error_code, "missing_final_response");
  assert.equal(result.completion.repair_attempts, 1); assert.equal(requests, 2);
  assert.ok(!result.text.includes("second unsubmitted answer"));
});

test("configured maximum two repair turns is bounded and rejects unsupported limits", async () => {
  const state = fixture();
  const result = await create(state, [text(""), text(""), finish()], { maxCompletionRepairs: 2 })(payload());
  assert.equal(result.status, "completed"); assert.equal(result.completion.repair_attempts, 2);
  assert.throws(() => create(state, [], { maxCompletionRepairs: 3 }), /0, 1, or 2/);
});

test("repair cannot repeat a successful write even if model attempts a new write call", async () => {
  const state = fixture();
  const original = call("example.write"), id = original.content[0].id;
  const result = await create(state, [original, text(""), (context) => {
    assertReadRepairTools(context); return call("example.write");
  }, call("evidence.page.read", { page_id: "actual-page" }), finish()])(payload());
  assert.equal(state.writes, 1); assert.equal(state.reads, 1);
  assert.equal(state.operations.filter((operation) => operation.path.endsWith("example.write")).length, 1);
  assert.equal(state.operations[0].id, `repair-run/${id}`);
  assert.equal(result.status, "completed");
});

test("exhausted shared tool budget does not open a repair model turn", async () => {
  const state = fixture(); let requests = 0;
  const run = create(state, [], { maxToolCalls: 1, modelFactory: factory([call("example.write"), finish()], () => { requests += 1; }) });
  const result = await run(payload());
  assert.notEqual(result.status, "completed"); assert.equal(requests, 1);
  assert.equal(result.completion.repair_attempts, 0);
  assert.equal(state.writes, 1);
});

test("scheduled repair resumes same counted attempt and does not replay prior write", async () => {
  let fail = true;
  const state = fixture();
  // Runtime progress uses agent_started for a fresh run and agent_resumed for
  // restored sessions, including the repair prompt's Agent lifecycle.
  const actualFetch = state.fetch;
  state.fetch = async (url, options) => {
    if (new URL(url).pathname === "/v2/agent/checkpoint") {
      const body = JSON.parse(options.body);
      if (fail && body.event?.type === "agent_started" && body.checkpoint.completion_repair?.pending) {
        return reply({ error: { code: "fixture_failure" } }, 500);
      }
    }
    return actualFetch(url, options);
  };
  await assert.rejects(create(state, [call("example.write"), text(""), finish()])(payload()),
    (error) => error.code === "checkpoint_unavailable");
  const saved = structuredClone(state.saved);
  assert.equal(saved.completion_repair.attempts, 1);
  assert.equal(saved.completion_repair.pending, true);
  assert.equal(saved.completion_repair.prompt_recorded, false);
  assert.equal(state.writes, 1);
  fail = false;
  const result = await create(state, [(context) => { assertReadRepairTools(context); return finish(); }])(payload(saved));
  assert.equal(result.status, "completed");
  assert.equal(result.completion.repair_attempts, 1);
  assert.equal(state.writes, 1);
});

test("repair uses original deadline instead of starting a new timeout window", async () => {
  const state = fixture();
  const run = create(state, [text(""), async (_context, options) => {
    await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
    return fauxAssistantMessage("", { stopReason: "aborted", errorMessage: "repair deadline" });
  }], { timeoutMs: 150 });
  const start = Date.now();
  const result = await run(payload());
  assert.equal(result.error_code, "run_timeout");
  assert.equal(result.completion.repair_attempts, 1);
  assert.ok(Date.now() - start < 800);
});

test("resumed repair with no remaining tool budget does not make another model request", async () => {
  const state = fixture(); let requests = 0;
  const run = create(state, [], { maxToolCalls: 0, modelFactory: factory([finish()], () => { requests += 1; }) });
  const checkpoint = { version: 2, turn_start: 0, tool_calls: [],
    completion_repair: { attempts: 1, pending: true, prompt_recorded: true },
    messages: [{ role: "user", content: [{ type: "text", text: "original task" }], timestamp: 1 }, text("body"),
      { role: "user", content: [{ type: "text", text: "scheduled protocol repair" }], timestamp: 2 }] };
  const result = await run(payload(checkpoint));
  assert.equal(requests, 0);
  assert.equal(result.error_code, "tool_budget_exhausted");
  assert.equal(result.completion.repair_attempts, 1);
});

test("failed repair checkpoint cannot reset its attempt limit on recovery", async () => {
  let fail = true;
  const state = fixture({ failCheckpoint: (body) => fail && body.event?.type === "run_finished" });
  await assert.rejects(create(state, [text(""), text("")])(payload()),
    (error) => error.code === "checkpoint_unavailable");
  const saved = structuredClone(state.saved);
  assert.equal(saved.completion_repair.attempts, 1);
  assert.equal(saved.completion_repair.pending, false);
  fail = false; let requests = 0;
  const result = await create(state, [], { modelFactory: factory([finish()], () => { requests += 1; }) })(payload(saved));
  assert.equal(requests, 0);
  assert.equal(result.error_code, "missing_final_response");
  assert.equal(result.completion.repair_attempts, 1);
});
