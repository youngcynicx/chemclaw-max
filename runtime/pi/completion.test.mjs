import test from "node:test";
import assert from "node:assert/strict";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime as createBaseRuntime } from "./runtime.mjs";
import { evaluateCompletion } from "./recovery.mjs";

// Isolate the completion validator here; repair.test.mjs exercises the default
// bounded LLM repair policy, including its retries and durable recovery.
const createRuntime = (options) => createBaseRuntime({ maxCompletionRepairs: 0, maxTaskContinuations: 0, ...options });

const reply = (value) => new Response(JSON.stringify(value));
const user = (text = "current request") => ({ role: "user", content: [{ type: "text", text }], timestamp: 1 });
const text = (value) => fauxAssistantMessage(fauxText(value));
const call = (name = "example.action", args = {}) => fauxAssistantMessage(fauxToolCall(name, args));
const finalArgs = { status: "completed", text: "current explicit final answer" };
const finish = () => call("agent.respond", finalArgs);
const action = { type: "function", function: { name: "example.action", mutates: true,
  description: "test", parameters: { type: "object", properties: {} } } };
const payload = (checkpoint) => ({ durable: true, session_id: "completion-test", message: "current request",
  context: { run_id: "current-run" },
  ...(checkpoint ? { resume: { checkpoints: { "current-run": checkpoint } } } : {}) });
function modelFactory(scripts) {
  return ({ role }) => {
    const faux = fauxProvider({ provider: "completion-test", models: [{ id: "test", name: "test", reasoning: true,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(Array.isArray(scripts) ? scripts : scripts[role]);
    return { model: faux.getModel(), streamFn: (model, context, options) => faux.provider.streamSimple(model, context, options) };
  };
}
function truncated({ withText = false } = {}) {
  const message = fauxAssistantMessage("", { stopReason: "length" });
  message.content = [{ type: "thinking", thinking: "PRIVATE_TEST_THINKING_DO_NOT_EXPOSE" },
    ...(withText ? [{ type: "text", text: "unfinished partial answer" }] : [])];
  message.usage = { input: 20559, output: 8192, reasoning: 8186, totalTokens: 28751 };
  return message;
}
function gateway({ tool = action, onAction, onCheckpoint } = {}) {
  return async (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/v2/agent/tools") return reply({ tools: [tool] });
    if (path === "/v2/agent/checkpoint") { onCheckpoint?.(JSON.parse(options.body).checkpoint); return reply({ ok: true }); }
    onAction?.(options);
    return reply({ action_status: "applied", object_refs: [{ kind: "event", id: "existing-real-event" }] });
  };
}

test("thinking-only length ends incomplete, preserves receipts, never returns prior planning/history", async () => {
  const plannedRead = call(); plannedRead.content.unshift({ type: "text", text: "old planning statement" });
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", fetchImpl: gateway(),
    modelFactory: modelFactory([plannedRead, truncated()]) });
  const result = await run({ ...payload(), history: [{ role: "assistant", text: "PREVIOUS_ANSWER",
    context: { internal_reference: "PRIVATE_CONTEXT_JSON" } }] });
  assert.equal(result.status, "partial");
  assert.equal(result.error_code, "model_output_truncated");
  assert.equal(result.completion.stopReason, "length");
  assert.equal(typeof result.completion.usage.output, "number");
  assert.ok(result.object_refs.some((ref) => ref.id === "existing-real-event"));
  assert.ok(!JSON.stringify(result).includes("PRIVATE_TEST_THINKING"));
  assert.ok(!result.text.includes("old planning"));
  assert.ok(!result.text.includes("PREVIOUS_ANSWER"));
  assert.ok(!result.text.includes("历史对象上下文"));
});

test("provider diagnostic exposes numeric reasoning usage without hidden thinking text", () => {
  const result = evaluateCompletion([user(), truncated()], 0, [], undefined, { durable: true });
  assert.equal(result.diagnostic.usage.reasoning, 8186);
  assert.equal(result.diagnostic.usage.output, 8192);
  assert.ok(!JSON.stringify(result).includes("PRIVATE_TEST_THINKING"));
});

test("partial visible text with length is not a valid final answer", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", fetchImpl: gateway(),
    modelFactory: modelFactory([truncated({ withText: true })]) });
  const result = await run(payload());
  assert.equal(result.status, "failed");
  assert.equal(result.error_code, "model_output_truncated");
  assert.ok(!result.text.includes("unfinished partial answer"));
});

test("durable current complete stop is natural; legacy current complete text stays compatible", async () => {
  const create = () => createRuntime({ gatewayUrl: "http://core", token: "secret", fetchImpl: gateway(),
    modelFactory: modelFactory([text("complete current text")]) });
  const durable = await create()(payload());
  assert.equal(durable.status, "completed");
  assert.equal(durable.error_code, undefined);
  assert.equal(durable.text, "complete current text");
  assert.equal(durable.completion.mode, "natural");
  const legacy = await create()({ session_id: "legacy", message: "current request" });
  assert.equal(legacy.status, "completed");
  assert.equal(legacy.text, "complete current text");
  assert.equal(legacy.completion.mode, "legacy");
});

test("legacy timeout after tools cannot fall back across current user boundary", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", timeoutMs: 50,
    fetchImpl: gateway(), modelFactory: modelFactory([async (_context, options) => {
      await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
      return fauxAssistantMessage("", { stopReason: "aborted", errorMessage: "request aborted" });
    }]) });
  const result = await run({ session_id: "legacy", message: "current request", history: [
    { role: "assistant", text: "LAST_TURN_ANSWER", context: { object_id: "HISTORY_JSON" } },
  ] });
  assert.notEqual(result.status, "completed");
  assert.equal(result.error_code, "run_timeout");
  assert.ok(!result.text.includes("LAST_TURN_ANSWER"));
  assert.ok(!result.text.includes("HISTORY_JSON"));
  assert.ok(!result.text.includes("历史对象上下文"));
});

test("unverified old settled success is invalidated without replaying any side effect", async () => {
  const readCall = call(), id = readCall.content[0].id;
  const journal = [{ name: "example.action", arguments: {}, toolCallId: id, state: "completed", isError: false,
    result: { action_status: "applied", object_refs: [{ kind: "event", id: "saved-before-repair" }] } }];
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: async () => { throw new Error("must not call tools"); },
    modelFactory: () => { throw new Error("must not regenerate task"); } });
  const result = await run(payload({ version: 1, settled: true,
    messages: [user(), readCall, truncated()], tool_calls: journal,
    result: { status: "completed", text: "false old completion", object_refs: [{ kind: "event", id: "saved-before-repair" }] } }));
  assert.equal(result.status, "partial");
  assert.equal(result.error_code, "model_output_truncated");
  assert.equal(result.completion.invalidated_checkpoint, true);
  assert.ok(result.object_refs.some((ref) => ref.id === "saved-before-repair"));
  assert.ok(!result.text.includes("false old completion"));
});

test("old settled explicit response with a real respond receipt remains valid without replay", async () => {
  const response = finish(), id = response.content[0].id;
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: async () => { throw new Error("must not fetch"); }, modelFactory: () => { throw new Error("must not model"); } });
  const result = await run(payload({ version: 1, settled: true, messages: [user(), response], final_response: finalArgs,
    tool_calls: [{ toolCallId: id, name: "agent.respond", state: "completed", isError: false, arguments: finalArgs,
      result: { status: "response_recorded" } }], result: { status: "completed", text: finalArgs.text, object_refs: [] } }));
  assert.equal(result.status, "completed");
  assert.equal(result.text, finalArgs.text);
  assert.equal(result.completion.protocol_version, 3);
  assert.equal(result.completion.mode, "explicit");
});

test("checkpoint before the current user prompt resumes a fresh turn without using history as final", async () => {
  let observed;
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", fetchImpl: gateway(),
    modelFactory: modelFactory([(context) => {
      observed = context.messages.at(-1);
      return finish();
    }]) });
  const result = await run(payload({ version: 2, turn_start: 1,
    messages: [text("HISTORICAL_REPLY")], tool_calls: [] }));
  assert.equal(observed.role, "user");
  assert.equal(result.status, "completed");
  assert.equal(result.text, finalArgs.text);
  assert.ok(!result.text.includes("HISTORICAL_REPLY"));
});

test("length tool call is rejected by Pi; a later successful respond recovers final completion", async () => {
  const cut = call(); cut.stopReason = "length";
  let executed = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: gateway({ onAction: () => { executed += 1; } }),
    modelFactory: modelFactory([cut, call(), finish()]) });
  const result = await run(payload());
  assert.equal(executed, 1, "only the later complete corrected call executes");
  assert.equal(result.status, "completed");
  assert.equal(result.text, finalArgs.text);
  assert.equal(result.error_code, undefined);
  assert.equal(result.completion.mode, "explicit");
});

test("restored truncated tool arguments also remain unexecuted before model repairs", async () => {
  const cut = call(); cut.stopReason = "length";
  let executed = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: gateway({ onAction: () => { executed += 1; } }), modelFactory: modelFactory([call(), finish()]) });
  const result = await run(payload({ version: 1, messages: [user(), cut], tool_calls: [] }));
  assert.equal(executed, 1, "restored truncated call is rejected, corrected call executes once");
  assert.equal(result.status, "completed");
  assert.equal(result.toolCalls[0].result.error.code, "model_output_truncated");
});

test("truncated child propagates public diagnostics and cannot be promoted by parent respond", async () => {
  let receipt, childCompletion;
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", role: "orchestrator",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
      if (path === "/v2/agent/delegate") return reply({ agent_id: "b_query", task_id: "child-task", run_id: "child-run",
        run_token: "child-secret", claim_epoch: 1, manifest: {} });
      if (path === "/v2/agent/delegate/complete") { childCompletion = JSON.parse(options.body); return reply({ ok: true }); }
      throw new Error(`unexpected ${path}`);
    }, modelFactory: modelFactory({ orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "query" }),
      (context) => { receipt = JSON.parse(context.messages.at(-1).content[0].text); return finish(); }],
      b_query: [truncated()] }) });
  const result = await run(payload());
  assert.notEqual(result.status, "completed");
  assert.equal(receipt.error_code, "model_output_truncated");
  assert.equal(receipt.completion.stopReason, "length");
  assert.equal(childCompletion.status, "failed");
  assert.ok(!JSON.stringify(result).includes("PRIVATE_TEST_THINKING"));
});

test("terminal deduplicated child without a token cannot bypass old completion validation", async () => {
  let receipt;
  const factory = modelFactory([call("agent.delegate", { agent_id: "b_query", message: "query" }),
    (context) => { receipt = JSON.parse(context.messages.at(-1).content[0].text); return finish(); }]);
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret", role: "orchestrator",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
      if (path === "/v2/agent/delegate") return reply({ agent_id: "b_query", task_id: "child-task", run_id: "child-run",
        result: { status: "completed", text: "OLD_FAKE_COMPLETION", object_refs: [{ kind: "event", id: "retained-ref" }] },
        checkpoint: { version: 1, messages: [user(), truncated()], tool_calls: [] } });
      throw new Error(`no replay allowed: ${path}`);
    }, modelFactory: (options) => { assert.equal(options.role, "orchestrator"); return factory(options); } });
  const result = await run(payload());
  assert.notEqual(result.status, "completed");
  assert.equal(receipt.error_code, "model_output_truncated");
  assert.equal(receipt.completion.invalidated_checkpoint, true);
  assert.ok(receipt.object_refs.some((ref) => ref.id === "retained-ref"));
  assert.ok(!receipt.text.includes("OLD_FAKE_COMPLETION"));
});

test("natural completion stays inside the current user turn and excludes unfinished tools", () => {
  const old = text("HISTORY_WITH_CONTEXT_JSON"), current = text("current final answer");
  assert.equal(evaluateCompletion([old, user()], 1, [], undefined, { durable: true }).valid, false);
  const natural = evaluateCompletion([old, user(), current], 1, [], undefined, { durable: true });
  assert.equal(natural.valid, true); assert.equal(natural.text, "current final answer");
  assert.equal(natural.diagnostic.mode, "natural");
  assert.equal(evaluateCompletion([user(), current], 0, [{ state: "pending", name: "example.action" }],
    undefined, { durable: true }).valid, false);
  assert.equal(evaluateCompletion([user(), call()], 0, [], undefined, { durable: true }).valid, false);
  assert.equal(evaluateCompletion([user(), text("")], 0, [], undefined, { durable: true }).valid, false);
});

test("natural prose cannot promote an actual pending proposal to completed", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [action] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      return reply({ action_status: "proposed", needs_confirmation: true,
        object_refs: [{ kind: "proposal", id: "pending-proposal" }] });
    }, modelFactory: modelFactory([call(), text("Here is the current answer; everything is complete.")]) });
  const result = await run(payload());
  assert.equal(result.completion.mode, "natural");
  assert.equal(result.status, "needs_review");
  assert.ok(result.object_refs.some((ref) => ref.id === "pending-proposal"));
});

test("natural prose cannot hide a rejected permission-bound operation", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [action] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      return new Response(JSON.stringify({ error: { code: "forbidden_scope", message: "not authorized" } }), { status: 403 });
    }, modelFactory: modelFactory([call(), text("The final response describes the denied operation.")]) });
  const result = await run(payload());
  assert.equal(result.completion.mode, "natural");
  assert.equal(result.status, "failed");
  assert.equal(result.toolCalls[0].isError, true);
});

test("verified v3 natural checkpoint resumes without model calls or repeated effects", async () => {
  let saved, effects = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: gateway({ onAction: () => { effects += 1; }, onCheckpoint: (checkpoint) => { saved = checkpoint; } }),
    modelFactory: modelFactory([call(), text("complete current natural result")]) });
  const first = await run(payload());
  assert.equal(first.status, "completed"); assert.equal(effects, 1);
  assert.equal(saved.version, 3); assert.equal(saved.completion_protocol_version, 3);
  const resumed = createRuntime({ gatewayUrl: "http://core", token: "secret",
    fetchImpl: async () => { throw new Error("must not fetch"); }, modelFactory: () => { throw new Error("must not generate"); } });
  const result = await resumed(payload(saved));
  assert.equal(result.status, "completed"); assert.equal(result.text, first.text);
  assert.equal(result.completion.mode, "natural"); assert.equal(effects, 1);
  assert.ok(result.object_refs.some((ref) => ref.id === "existing-real-event"));
});

test("old version, missing boundary, or mismatched saved text cannot validate settled natural success", async (t) => {
  const valid = { version: 3, completion_protocol_version: 3, turn_start: 0, settled: true,
    messages: [user(), text("current natural text")], tool_calls: [],
    result: { status: "completed", text: "current natural text", object_refs: [],
      completion: { protocol_version: 3, mode: "natural" } } };
  const old = structuredClone(valid); old.version = 2; old.completion_protocol_version = 2;
  old.result.completion.protocol_version = 2;
  const noBoundary = structuredClone(valid); delete noBoundary.turn_start;
  const wrongText = structuredClone(valid); wrongText.result.text = "STALE_HISTORICAL_RESPONSE";
  for (const [name, checkpoint] of [["v2", old], ["missing turn boundary", noBoundary], ["saved text mismatch", wrongText]]) {
    await t.test(name, async () => {
      const run = createRuntime({ gatewayUrl: "http://core", token: "secret",
        fetchImpl: async () => { throw new Error("no replay"); }, modelFactory: () => { throw new Error("no model"); } });
      const result = await run(payload(checkpoint));
      assert.equal(result.status, "failed");
      assert.equal(result.error_code, "unverified_completion_checkpoint");
      assert.equal(result.completion.invalidated_checkpoint, true);
      assert.ok(!result.text.includes("STALE_HISTORICAL_RESPONSE"));
    });
  }
});
