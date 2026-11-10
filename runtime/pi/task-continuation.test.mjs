import test from "node:test";
import assert from "node:assert/strict";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall, getCurrentTools } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

// No provider, credentials or real database: every effect crosses this fixture.
const ROOT = "continuation-root", CHILD = "continuation-child";
const reply = (body, status = 200) => new Response(JSON.stringify(body), { status });
const text = (value = "") => fauxAssistantMessage(fauxText(value));
const cut = () => fauxAssistantMessage("", { stopReason: "length" });
const call = (name, args = {}, id) => {
  const message = fauxAssistantMessage(fauxToolCall(name, args));
  if (id) message.content[0].id = id;
  return message;
};
const write = (value, id) => call("example.write", { value }, id);
const read = (id = "original-page") => call("evidence.page.read", { page_id: id });
const finish = (status = "completed") => call("agent.respond", { status, text: "source-verified deliverable" });
const specs = [
  { type: "function", function: { name: "example.write", mutates: true, description: "write a named object",
    parameters: { type: "object", properties: { value: { type: "string" } }, required: ["value"], additionalProperties: false } } },
  { type: "function", function: { name: "evidence.page.read", mutates: false, description: "read a source or saved object",
    parameters: { type: "object", properties: { page_id: { type: "string" } }, required: ["page_id"] } } },
];
const toolsIn = (context) => getCurrentTools(context.messages).map((tool) => tool.name).sort();
const fullTools = (context) => assert.deepEqual(toolsIn(context), ["agent.respond", "evidence.page.read", "example.write"]);
const readonlyTools = (context) => assert.deepEqual(toolsIn(context), ["agent.respond", "evidence.page.read"]);
const payload = (saved) => ({ durable: true, session_id: "continuation-session", message: "Read the original source; organize A and B once and verify them.",
  context: { run_id: ROOT }, ...(saved ? { resume: { checkpoints: { [ROOT]: saved } } } : {}) });
const budget = (remaining = 60, deadline = Date.now() + 60_000) => ({ deadline_ms: deadline,
  remaining_tool_calls: remaining, delegations_started: 0 });
const checkpoint = (extra = {}) => ({ version: 3, completion_protocol_version: 3, run_id: ROOT, role: "b_query",
  turn_start: 0, messages: [{ role: "user", content: "original task", timestamp: 1 }, cut()],
  tool_calls: [], events: [], delegations: [], execution_budget: budget(), ...extra });

function fixture({ failCheckpoint, ambiguousWrite, unknownWriteEnvelope, failRead, failReadEnvelope, failFirstWriteEnvelope, coreRemaining, childTimeout = 900 } = {}) {
  const state = { writes: [], reads: [], business: [], checkpoints: [], saved: {}, getTools: 0,
    factories: [], providerCalls: [], remote: [], completedChildren: [] };
  state.fetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (options.signal?.aborted) throw new DOMException("Fixture request aborted", "AbortError");
    const body = options.body ? JSON.parse(options.body) : null;
    if (path === "/v2/agent/tools") {
      state.getTools++;
      return reply({ tools: specs, ...(coreRemaining === undefined ? {} : { budget_tool_calls: coreRemaining, used_tool_calls: 0 }) });
    }
    if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
    if (path === "/v2/agent/checkpoint") {
      if (failCheckpoint?.(body, state)) return reply({ error: { code: "fixture_checkpoint_failure" } }, 500);
      state.checkpoints.push(structuredClone(body));
      state.saved[body.checkpoint.run_id] = structuredClone(body.checkpoint);
      return reply({ ok: true });
    }
    if (path === "/v2/agent/delegate") return reply({ run_id: CHILD, task_id: "child-task", agent_id: "b_query",
      run_token: "fixture-child-token", claim_epoch: 2, manifest: { timeout_s: childTimeout, max_delegation_depth: 0 } });
    if (path === "/v2/agent/delegate/complete") { state.completedChildren.push(body); return reply({ ok: true }); }
    const operation = { path, params: body?.params, operation_id: options.headers["X-Operation-Id"] };
    state.business.push(operation);
    if (path.endsWith("example.write")) {
      state.writes.push(operation);
      if (ambiguousWrite) return reply({ error: { code: "fixture_committed_but_response_lost" } }, 500);
      if (unknownWriteEnvelope) return reply({ action_status: "unknown", object_refs: [{ kind: "event", id: "possibly-written" }] });
      if (failFirstWriteEnvelope && state.writes.length === 1) return reply({ action_status: "rejected",
        error: { code: "fixture_explicit_write_refusal", message: "The operation was explicitly rejected." } });
      return reply({ ok: true, action_status: "applied", object_refs: [{ kind: "event", id: `saved-${body.params.value}` }] });
    }
    if (path.endsWith("evidence.page.read")) {
      state.reads.push(operation);
      if (failRead) return reply({ error: { code: "fixture_source_unavailable" } }, 404);
      if (failReadEnvelope) return reply({ action_status: "failed",
        error: { code: "fixture_source_unavailable", message: "The source could not be read." } });
      return reply({ ok: true, action_status: "applied", object_refs: [{ kind: "page", id: body.params.page_id }],
        result: { text: "Original source data; the caller must interpret it." } });
    }
    throw new Error(`Unexpected fixture path ${path}`);
  };
  return state;
}

function factory(state, responses, { observed = false } = {}) {
  return ({ role, observation }) => {
    state.factories.push(role);
    const faux = fauxProvider({ provider: "continuation-test", models: [{ id: "test", name: "test", reasoning: false,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    const steps = Array.isArray(responses) ? responses : (responses[role] || []);
    faux.setResponses(steps.map((step) => async (context, options) => {
      const row = { schema_version: 1, request_id: `fixture-${role}-${state.providerCalls.length}`, call_id: "fixture-call",
        phase: "started", model: "test", run_id: observation.runId, agent_id: role, retry_index: 0,
        usage: {}, cost: null, cost_currency: null };
      state.providerCalls.push({ role, context: structuredClone(context), observation });
      if (observed) await observation.onRequest(row);
      state.remote.push(role);
      return typeof step === "function" ? step(context, options, observation, row) : step;
    }));
    return { model: faux.getModel(), streamFn: (model, context, options) => faux.provider.streamSimple(model, context, options) };
  };
}
const create = (state, responses, options = {}, observerOptions = {}) => createRuntime({ gatewayUrl: "http://fixture-core", token: "fixture-root-token",
  fetchImpl: state.fetch, modelFactory: factory(state, responses, observerOptions), ...options });
const continuationEvents = (result) => result.events.filter((event) => event.type === "task_continuation_started");

test("fresh length reopens original tools, requires a source read, writes new work and delivers", async () => {
  const state = fixture();
  const thinkingCut = cut(); thinkingCut.content = [{ type: "thinking", thinking: "PRIVATE_CONTINUATION_THINKING" }];
  const result = await create(state, [thinkingCut, (context) => {
    fullTools(context);
    assert.match(JSON.stringify(context.messages.at(-1).content), /原任务有界续做/);
    return read();
  }, write("B"), finish()])(payload());
  assert.equal(result.status, "completed");
  assert.deepEqual(state.writes.map((item) => item.params.value), ["B"]);
  assert.equal(state.reads.length, 1);
  assert.equal(result.completion.task_continuation_attempts, 1);
  assert.equal(result.completion.repair_attempts, 0);
  assert.equal(continuationEvents(result).length, 1);
  assert.ok(!JSON.stringify(result).includes("PRIVATE_CONTINUATION_THINKING"));
  assert.ok(!JSON.stringify(state.saved).includes("fixture-root-token"));
});

test("successful A survives length; readback then equal write with new ID returns old receipt and B executes once", async () => {
  const state = fixture();
  let repeatedReceipt;
  const result = await create(state, [write("A", "first-A"), cut(), read("saved-A"), write("A", "new-A"), (context) => {
    repeatedReceipt = context.messages.findLast((message) => message.role === "toolResult");
    return write("B", "first-B");
  }, finish()])(payload());
  assert.equal(result.status, "completed");
  assert.deepEqual(state.writes.map((item) => item.params.value), ["A", "B"]);
  assert.deepEqual(state.writes.map((item) => item.operation_id), [`${ROOT}/first-A`, `${ROOT}/first-B`]);
  assert.match(JSON.stringify(repeatedReceipt), /saved-A/);
  assert.equal(result.completion.task_continuation_attempts, 1);
});

test("an explicitly rejected HTTP200 write is not mistaken for a successful duplicate receipt", async () => {
  const state = fixture({ failFirstWriteEnvelope: true });
  const result = await create(state, [write("A", "rejected-A"), cut(), read(), write("A", "retry-A"), finish()])(payload());
  assert.deepEqual(state.writes.map((item) => item.operation_id), [`${ROOT}/rejected-A`, `${ROOT}/retry-A`]);
  assert.equal(result.status, "completed");
  const actual = result.toolCalls.find((item) => item.toolCallId === "retry-A");
  assert.equal(actual.result.action_status, "applied");
  assert.ok(!actual.result.continuation_reused_receipt);
});

test("new continuation write before readback is blocked despite being in the authorized tool set", async () => {
  const state = fixture(); let refusal;
  const result = await create(state, [cut(), write("too-early"), (context) => {
    refusal = JSON.stringify(context.messages.at(-1)); return read();
  }, write("B"), finish()])(payload());
  assert.match(refusal, /continuation_readback_required/);
  assert.deepEqual(state.writes.map((item) => item.params.value), ["B"]);
  assert.equal(result.status, "completed");
});

test("failed readback cannot unlock continuation writes", async () => {
  const state = fixture({ failRead: true });
  const result = await create(state, [cut(), read(), write("B"), finish("partial")])(payload());
  assert.equal(state.reads.length, 1); assert.equal(state.writes.length, 0);
  assert.equal(result.status, "failed", "there is no successful business receipt to make this a partial success");
  assert.match(JSON.stringify(result.toolCalls), /continuation_readback_required/);
});

test("HTTP200 failed read receipt cannot unlock continuation writes", async () => {
  const state = fixture({ failReadEnvelope: true });
  const result = await create(state, [cut(), read(), write("B"), finish("partial")])(payload());
  assert.equal(state.reads.length, 1); assert.equal(state.writes.length, 0);
  assert.equal(result.status, "failed");
  assert.match(JSON.stringify(result.toolCalls), /continuation_readback_required/);
});

test("even complete-looking tool arguments in length message never execute", async () => {
  const state = fixture();
  const truncated = write("FORBIDDEN-CUT", "cut-call"); truncated.stopReason = "length";
  const result = await create(state, [truncated, (context) => {
    assert.ok(context.messages.some((message) => message.role === "toolResult" && message.toolCallId === "cut-call"));
    return read();
  }, write("B"), finish()])(payload());
  assert.deepEqual(state.writes.map((item) => item.params.value), ["B"]);
  const rejected = result.toolCalls.find((item) => item.toolCallId === "cut-call");
  assert.equal(rejected.isError, true);
  assert.match(JSON.stringify(rejected.result), /model_output_truncated|output token limit/);
});

test("schema validation is still required for new continuation calls", async () => {
  const state = fixture(); let refusal;
  const result = await create(state, [cut(), read(), call("example.write", {}), (context) => {
    refusal = JSON.stringify(context.messages.at(-1)); return write("B");
  }, finish()])(payload());
  assert.match(refusal, /schema_violation/);
  assert.match(refusal, /value/);
  assert.deepEqual(state.writes.map((item) => item.params.value), ["B"]);
  assert.equal(result.status, "completed");
});

test("same call ID with changed arguments is rejected instead of writing a second object", async () => {
  const state = fixture(); let refusal;
  await create(state, [write("A", "reused-ID"), cut(), read(), write("B", "reused-ID"), (context) => {
    refusal = JSON.stringify(context.messages.at(-1)); return finish("partial");
  }])(payload());
  assert.deepEqual(state.writes.map((item) => item.params.value), ["A"]);
  assert.match(refusal, /tool_call_id_collision/);
});

test("pending mutation checkpoint blocks model continuation and preserves unknown operation", async () => {
  const state = fixture();
  const pending = write("maybe-A", "pending-write");
  const saved = checkpoint({ messages: [{ role: "user", content: "original task" }, pending],
    tool_calls: [{ name: "example.write", toolCallId: "pending-write", operation_id: `${ROOT}/pending-write`,
      arguments: { value: "maybe-A" }, state: "pending", result: null }] });
  const result = await create(state, [finish()])(payload(saved));
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.writes.length, 0);
  assert.equal(result.toolCalls[0].state, "pending");
  assert.equal(result.toolCalls[0].operation_id, `${ROOT}/pending-write`);
});

test("committed write with lost receipt fences the live run and cannot open a continuation", async () => {
  const state = fixture({ ambiguousWrite: true });
  const result = await create(state, [write("A"), cut(), read(), write("B"), finish()])(payload());
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(state.writes.length, 1); assert.equal(state.providerCalls.length, 1);
  assert.equal(continuationEvents(result).length, 0);
});

test("supported unknown mutation envelope fences the run rather than treating it as an explicit refusal or success", async () => {
  const state = fixture({ unknownWriteEnvelope: true });
  const result = await create(state, [write("A", "unknown-A"), cut(), read(), write("A", "unsafe-retry-A"), finish()])(payload());
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(state.writes.length, 1); assert.equal(state.providerCalls.length, 1);
  assert.equal(result.toolCalls[0].state, "pending");
  assert.equal(result.toolCalls[0].operation_id, `${ROOT}/unknown-A`);
  assert.equal(continuationEvents(result).length, 0);
});

test("historical completed journal entry with unknown mutation effect cannot reopen writes after recovery", async () => {
  const state = fixture();
  const original = write("maybe-A", "historical-unknown-A");
  const receipt = { action_status: "unknown", object_refs: [{ kind: "event", id: "possibly-written" }] };
  const saved = checkpoint({
    messages: [{ role: "user", content: "original task" }, original,
      { role: "toolResult", toolName: "example.write", toolCallId: "historical-unknown-A",
        content: [{ type: "text", text: JSON.stringify(receipt) }], details: receipt, isError: false, timestamp: 1 }, cut()],
    tool_calls: [{ name: "example.write", toolCallId: "historical-unknown-A", operation_id: `${ROOT}/historical-unknown-A`,
      arguments: { value: "maybe-A" }, state: "completed", isError: false, result: receipt }],
  });
  const result = await create(state, [read(), write("maybe-A"), finish()])(payload(saved));
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.writes.length, 0);
});

test("after one continuation another length opens only read-only completion repair", async () => {
  const state = fixture(); let refusal;
  const result = await create(state, [cut(), (context) => { fullTools(context); return read(); }, cut(), (context) => {
    readonlyTools(context); return write("FORBIDDEN-REPAIR");
  }, (context) => { refusal = JSON.stringify(context.messages.at(-1)); return finish("partial"); }])(payload());
  assert.equal(result.status, "partial");
  assert.equal(state.writes.length, 0);
  assert.equal(result.completion.task_continuation_attempts, 1);
  assert.equal(result.completion.repair_attempts, 1);
  assert.equal(continuationEvents(result).length, 1);
  assert.match(refusal, /not found|not available|Unknown tool|unknown tool|not a registered/i);
});

test("explicit cancellation before execution makes zero model and business calls", async () => {
  const state = fixture(), controller = new AbortController(); controller.abort();
  const result = await create(state, [cut(), read(), write("B")])(payload(), { signal: controller.signal });
  assert.equal(result.status, "cancelled");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.factories.length, 0);
  assert.equal(state.business.length, 0);
});

test("settled cancelled checkpoint is terminal even if task continuation was pending", async () => {
  const state = fixture();
  const saved = checkpoint({ settled: true, task_continuation: { attempts: 1, pending: true, prompt_recorded: false },
    result: { status: "cancelled", text: "cancelled", error_code: "run_cancelled", completion: { protocol_version: 3, mode: "incomplete" } } });
  const result = await create(state, [read(), write("B"), finish()])(payload(saved));
  assert.notEqual(result.status, "completed");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.factories.length, 0);
  assert.equal(state.getTools, 0); assert.equal(state.business.length, 0);
});

test("continuation scheduling crash resumes the same counted attempt without repeating A", async () => {
  let fail = true;
  const state = fixture({ failCheckpoint: (body) => fail && body.event?.type === "agent_started"
    && body.checkpoint.task_continuation?.pending });
  await assert.rejects(create(state, [write("A"), cut(), read(), write("B"), finish()])(payload()),
    (error) => error.code === "checkpoint_unavailable");
  const saved = structuredClone(state.saved[ROOT]);
  assert.equal(saved.task_continuation.attempts, 1);
  assert.equal(saved.task_continuation.pending, true);
  assert.equal(saved.task_continuation.prompt_recorded, false);
  assert.equal(state.writes.length, 1);
  fail = false;
  const result = await create(state, [(context) => { fullTools(context); return read("saved-A"); }, write("B"), finish()])(payload(saved));
  assert.equal(result.status, "completed");
  assert.equal(result.completion.task_continuation_attempts, 1);
  assert.deepEqual(state.writes.map((item) => item.params.value), ["A", "B"]);
  assert.equal(result.events.filter((item) => item.type === "task_continuation_started").length, 1);
  assert.equal(result.events.filter((item) => item.type === "task_continuation_resumed").length, 1);
});

test("expired persisted deadline does not start another provider or execute tools", async () => {
  const state = fixture();
  const saved = checkpoint({ execution_budget: budget(60, Date.now() - 1),
    task_continuation: { attempts: 1, pending: true, prompt_recorded: false } });
  const result = await create(state, [read(), write("B"), finish()], { timeoutMs: 900000 })(payload(saved));
  assert.equal(result.error_code, "run_timeout");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.factories.length, 0);
  assert.equal(state.business.length, 0);
  assert.equal(state.saved[ROOT].execution_budget.deadline_ms, saved.execution_budget.deadline_ms);
});

test("recorded continuation prompt survives provider-intent crash without duplication or a new attempt", async () => {
  let fail = true;
  const state = fixture({ failCheckpoint: (body) => fail && body.event?.type === "provider_request_started"
    && body.checkpoint.task_continuation?.pending });
  await assert.rejects(create(state, [write("A"), cut(), read(), write("B"), finish()], {}, { observed: true })(payload()),
    (error) => error.code === "checkpoint_unavailable");
  const saved = structuredClone(state.saved[ROOT]);
  assert.equal(saved.task_continuation.attempts, 1);
  assert.equal(saved.task_continuation.pending, true);
  assert.equal(saved.task_continuation.prompt_recorded, true);
  assert.equal(saved.messages.filter((message) => message.role === "user"
    && JSON.stringify(message.content).includes("原任务有界续做")).length, 1);
  fail = false;
  const result = await create(state, [read("saved-A"), write("B"), finish()], {}, { observed: true })(payload(saved));
  assert.equal(result.status, "completed");
  assert.equal(result.completion.task_continuation_attempts, 1);
  assert.deepEqual(state.writes.map((item) => item.params.value), ["A", "B"]);
  assert.equal(state.saved[ROOT].messages.filter((message) => message.role === "user"
    && JSON.stringify(message.content).includes("原任务有界续做")).length, 1);
});

test("continuation write response checkpoint loss becomes unknown intent and is not replayed", async () => {
  let fail = true;
  const state = fixture({ failCheckpoint: (body) => fail && body.event?.type === "tool_result_received"
    && body.event.tool === "example.write" && body.checkpoint.tool_calls.at(-1)?.arguments?.value === "B" });
  await assert.rejects(create(state, [write("A"), cut(), read(), write("B"), finish()])(payload()),
    (error) => error.code === "checkpoint_unavailable");
  const saved = structuredClone(state.saved[ROOT]);
  assert.equal(saved.tool_calls.at(-1).arguments.value, "B");
  assert.equal(saved.tool_calls.at(-1).state, "pending");
  assert.deepEqual(state.writes.map((item) => item.params.value), ["A", "B"]);
  fail = false;
  const before = state.providerCalls.length;
  const result = await create(state, [read(), write("B"), finish()])(payload(saved));
  assert.equal(result.status, "needs_review");
  assert.equal(result.error_code, "ambiguous_tool_outcome");
  assert.equal(state.providerCalls.length, before, "unknown committed effect must not ask a model to retry");
  assert.deepEqual(state.writes.map((item) => item.params.value), ["A", "B"]);
});

test("parallel read and write in one continuation batch cannot bypass the readback gate", async () => {
  const state = fixture(), originalFetch = state.fetch;
  state.fetch = async (url, options) => {
    if (new URL(url).pathname.endsWith("evidence.page.read")) await new Promise((resolve) => setTimeout(resolve, 10));
    return originalFetch(url, options);
  };
  const batch = read(); batch.content.push(fauxToolCall("example.write", { value: "must-wait-for-readback" }));
  const result = await create(state, [cut(), batch, finish("partial")])(payload());
  assert.equal(state.reads.length, 1); assert.equal(state.writes.length, 0);
  assert.match(JSON.stringify(result.toolCalls), /continuation_readback_required/);
  assert.equal(result.status, "partial");
});

test("persisted zero remaining tool budget does not reopen a continuation budget", async () => {
  const state = fixture({ coreRemaining: 100 });
  const result = await create(state, [read(), write("B"), finish()], { maxToolCalls: 100 })(payload(checkpoint({ execution_budget: budget(0) })));
  assert.notEqual(result.status, "completed");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.business.length, 0);
  assert.equal(state.saved[ROOT].execution_budget.remaining_tool_calls, 0);
  assert.equal(result.completion.task_continuation_attempts, 0);
});

test("unknown legacy checkpoint cannot opt into new full-tool task continuation", async () => {
  const state = fixture();
  const saved = checkpoint(); delete saved.execution_budget;
  const result = await create(state, [(context) => { readonlyTools(context); return finish("partial"); }])(payload(saved));
  assert.equal(result.completion.task_continuation_attempts, 0);
  assert.equal(result.completion.repair_attempts, 1);
  assert.equal(state.writes.length, 0);
});

test("unverified legacy budget also blocks a saved pending task continuation", async () => {
  const state = fixture();
  const saved = checkpoint({ task_continuation: { attempts: 1, pending: true, prompt_recorded: false } });
  delete saved.execution_budget;
  const result = await create(state, [read(), write("unsafe-unverified-budget"), finish("partial")])(payload(saved));
  assert.equal(state.writes.length, 0);
  assert.equal(result.events.filter((item) => item.type === "task_continuation_resumed").length, 0);
  assert.notEqual(result.status, "completed");
});

test("continuation keeps its root deadline while waiting for a provider", { timeout: 5000 }, async () => {
  const state = fixture();
  const started = Date.now();
  const result = await create(state, [cut(), async (_context, options) => {
    if (!options.signal.aborted) await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
    return fauxAssistantMessage("", { stopReason: "aborted" });
  }], { timeoutMs: 350 })(payload());
  assert.equal(result.error_code, "run_timeout");
  assert.equal(result.completion.task_continuation_attempts, 1);
  assert.ok(Date.now() - started < 2000, "must retain original short deadline");
  const deadlines = new Set(state.checkpoints.map((entry) => entry.checkpoint.execution_budget.deadline_ms));
  assert.equal(deadlines.size, 1);
});

test("child manifest cannot extend root deadline or create a separate tool budget", { timeout: 5000 }, async () => {
  const state = fixture();
  const started = Date.now();
  const result = await create(state, {
    orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "read and finish" }), finish()],
    b_query: [async (_context, options) => {
      if (!options.signal.aborted) await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
      return fauxAssistantMessage("", { stopReason: "aborted" });
    }],
  }, { role: "orchestrator", timeoutMs: 350, maxToolCalls: 4 })(payload());
  assert.equal(result.error_code, "run_timeout");
  assert.ok(Date.now() - started < 2000);
  assert.equal(state.providerCalls.filter((item) => item.role === "orchestrator").length, 1);
  assert.equal(state.providerCalls.filter((item) => item.role === "b_query").length, 1);
  assert.equal(state.saved[ROOT].execution_budget.deadline_ms, state.saved[CHILD].execution_budget.deadline_ms);
  assert.equal(state.saved[ROOT].execution_budget.remaining_tool_calls, 3);
  assert.equal(state.saved[CHILD].execution_budget.remaining_tool_calls, 3);
});

test("a genuinely shorter child timeout remains local and leaves the parent its original remaining budget", { timeout: 5000 }, async () => {
  const state = fixture({ childTimeout: 0.05 });
  const result = await create(state, {
    orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "bounded short child" }), finish("partial")],
    b_query: [async (_context, options) => {
      if (!options.signal.aborted) await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
      return fauxAssistantMessage("", { stopReason: "aborted" });
    }],
  }, { role: "orchestrator", timeoutMs: 5000 })(payload());
  assert.equal(result.status, "failed", "the failed child supplied no successful business receipt");
  assert.equal(result.error_code, undefined, "a local role timeout must not be relabelled a root deadline");
  assert.equal(result.delegations[0].error_code, "run_timeout");
  assert.deepEqual(state.remote, ["orchestrator", "b_query", "orchestrator"]);
});

test("delayed timer cannot allow a tool effect after the absolute root deadline", { timeout: 5000 }, async () => {
  const state = fixture();
  const result = await create(state, [() => {
    const end = state.saved[ROOT].execution_budget.deadline_ms + 5;
    // Failure injection: synchronous provider processing starves the timer.
    while (Date.now() < end) { /* deliberately keep the timer callback queued */ }
    return write("FORBIDDEN-AFTER-DEADLINE");
  }, finish()], { timeoutMs: 350 })(payload());
  assert.equal(result.error_code, "run_timeout");
  assert.equal(state.remote.length, 1);
  assert.equal(state.writes.length, 0);
});

test("late read receipt cannot start a subsequent model request while the timer callback is queued", { timeout: 5000 }, async () => {
  const state = fixture(), originalFetch = state.fetch;
  state.fetch = async (url, options) => {
    if (new URL(url).pathname.endsWith("evidence.page.read")) {
      const end = state.saved[ROOT].execution_budget.deadline_ms + 5;
      while (Date.now() < end) { /* inject a synchronous gateway response delay */ }
    }
    return originalFetch(url, options);
  };
  const result = await create(state, [read(), write("FORBIDDEN-NEXT-MODEL"), finish()], { timeoutMs: 350 })(payload());
  assert.equal(result.error_code, "run_timeout");
  assert.equal(state.remote.length, 1);
  assert.equal(state.writes.length, 0);
});

test("late provider-intent checkpoint acknowledgement cannot cross the deadline and send the request", { timeout: 5000 }, async () => {
  const state = fixture(), originalFetch = state.fetch;
  state.fetch = async (url, options) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (new URL(url).pathname === "/v2/agent/checkpoint" && body.event?.type === "provider_request_started") {
      const end = body.checkpoint.execution_budget.deadline_ms + 5;
      while (Date.now() < end) { /* the persistence acknowledgement arrives late */ }
    }
    return originalFetch(url, options);
  };
  const result = await create(state, [read(), write("FORBIDDEN-LATE-INTENT"), finish()], { timeoutMs: 350 }, { observed: true })(payload());
  assert.equal(result.error_code, "run_timeout");
  assert.equal(state.providerCalls.length, 1);
  assert.equal(state.remote.length, 0);
  assert.equal(state.business.length, 0);
});

test("root quota observation stops paid calls and remains quota rather than timeout", async () => {
  const state = fixture();
  const result = await create(state, [(_context, _options, observation, row) => {
    observation.onRecord({ ...row, phase: "finished", error_class: "provider_quota_exhausted", provider_error_code: "1113", http_status: 429 });
    return fauxAssistantMessage("", { stopReason: "error", errorMessage: "fixture quota unavailable" });
  }, read(), write("B"), finish()], {}, { observed: true })(payload());
  assert.equal(result.error_code, "provider_quota_exhausted");
  assert.equal(result.needs_provider_quota, true);
  assert.equal(state.remote.length, 1); assert.equal(state.business.length, 0);
  assert.equal(result.provider_resource_failure.provider_error_code, "1113");
});

test("child actual tool calls consume the parent's shared limit", async () => {
  const state = fixture();
  const result = await create(state, {
    orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "read and organize" }), write("parent-forbidden"), finish()],
    b_query: [read(), write("child-forbidden"), finish()],
  }, { role: "orchestrator", maxToolCalls: 2 })(payload());
  assert.equal(state.reads.length, 1); assert.equal(state.writes.length, 0);
  assert.deepEqual(state.remote, ["orchestrator", "b_query"]);
  assert.equal(state.saved[ROOT].execution_budget.remaining_tool_calls, 0);
  assert.equal(state.saved[CHILD].execution_budget.remaining_tool_calls, 0);
  assert.notEqual(result.status, "completed");
});

test("external cancel during continuation is terminal and cannot run subsequent write or repair", async () => {
  const state = fixture(), controller = new AbortController();
  const result = await create(state, [cut(), () => {
    controller.abort();
    return read();
  }, write("forbidden-after-cancel"), finish()])(payload(), { signal: controller.signal });
  assert.equal(result.status, "cancelled");
  assert.equal(result.error_code, "run_cancelled");
  assert.equal(state.remote.length, 2); assert.equal(state.business.length, 0);
  assert.equal(result.completion.task_continuation_attempts, 1);
  assert.equal(result.completion.repair_attempts, 0);
});

test("child quota observation stops its parent tree without a followup model call", async () => {
  const state = fixture();
  const result = await create(state, {
    orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "read sources" }), write("parent-must-not-run"), finish()],
    b_query: [(_context, _options, observation, row) => {
      observation.onRecord({ ...row, phase: "finished", error_class: "provider_quota_exhausted", provider_error_code: "1113", http_status: 429 });
      return fauxAssistantMessage("", { stopReason: "error", errorMessage: "fixture child quota" });
    }, read()],
  }, { role: "orchestrator" }, { observed: true })(payload());
  assert.equal(result.error_code, "provider_quota_exhausted");
  assert.equal(result.needs_provider_quota, true);
  assert.deepEqual(state.remote, ["orchestrator", "b_query"]);
  assert.equal(state.business.length, 0);
  assert.equal(result.delegations[0].error_code, "provider_quota_exhausted");
  assert.equal(result.provider_resource_failure.provider_error_code, "1113");
});

test("persisted quota observation prevents provider calls during same-job recovery", async () => {
  const state = fixture();
  const saved = checkpoint({ provider_requests: [{ request_id: "previous-quota", phase: "finished", error_class: "provider_quota_exhausted",
    provider_error_code: "1113", http_status: 429 }] });
  const result = await create(state, [read(), write("B"), finish()])(payload(saved));
  assert.equal(result.error_code, "provider_quota_exhausted");
  assert.equal(state.providerCalls.length, 0); assert.equal(state.business.length, 0);
});

test("failed provider-intent checkpoint prevents remote request and fences all continuation work", async () => {
  const state = fixture({ failCheckpoint: (body) => body.event?.type === "provider_request_started" });
  await assert.rejects(create(state, [cut(), read(), write("B"), finish()], {}, { observed: true })(payload()),
    (error) => error.code === "checkpoint_unavailable");
  assert.equal(state.providerCalls.length, 1, "one attempted local intent");
  assert.equal(state.remote.length, 0, "no remote provider effect before intent checkpoint acknowledgement");
  assert.equal(state.business.length, 0);
});
