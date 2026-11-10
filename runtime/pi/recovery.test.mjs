import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";
import { withContract, boundedSnapshot } from "./recovery.mjs";

const reply = (value) => new Response(JSON.stringify(value));
const spec = (mutates = true) => ({ type: "function", function: { name: "example.action", mutates,
  description: "test", parameters: { type: "object", properties: { kind: { type: "string", enum: ["chemicals"] } }, required: ["kind"] } } });
function factory(responses) {
  return () => {
    const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test",
      reasoning: false, input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(responses);
    return { model: faux.getModel(), streamFn: (model, context, options) => faux.provider.streamSimple(model, context, options) };
  };
}
const call = (args = { kind: "chemicals" }) => fauxAssistantMessage(fauxToolCall("example.action", args));
const finish = () => fauxAssistantMessage(fauxToolCall("agent.respond", { status: "completed", text: "done" }));
const payload = (checkpoint) => ({ durable: true, session_id: "s", message: "do the task", context: { run_id: "root-run" },
  ...(checkpoint ? { resume: { checkpoints: { "root-run": checkpoint } } } : {}) });

test("installed Pi validation returns missing fields and schema to model, then model repairs", async () => {
  let observed, executed = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => {
      if (new URL(url).pathname === "/v2/agent/tools") return reply({ tools: [spec()] });
      executed += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([call({}), (context) => {
      observed = JSON.parse(context.messages.at(-1).content[0].text);
      return call();
    }, finish()]) });
  const result = await run({ session_id: "s", message: "query" });
  assert.deepEqual(observed.missing_fields, ["kind"]);
  assert.equal(observed.code, "schema_violation");
  assert.deepEqual(observed.expected_schema.required, ["kind"]);
  assert.deepEqual(observed.received_arguments, {});
  assert.equal(executed, 1);
  assert.equal(result.status, "completed");
});

test("checkpoint intent precedes action and receipt precedes next provider request", async () => {
  const snapshots = [], operations = [];
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") {
        snapshots.push(JSON.parse(options.body).checkpoint); return reply({ ok: true });
      }
      operations.push(options.headers["X-Operation-Id"]);
      assert.equal(snapshots.at(-1).tool_calls.at(-1).state, "pending");
      assert.equal(snapshots.at(-1).messages.at(-1).role, "assistant");
      return reply({ action_status: "applied", object_refs: [{ kind: "event", id: "saved-1" }] });
    }, modelFactory: factory([call(), () => {
      assert.equal(snapshots.at(-1).tool_calls[0].state, "completed");
      return finish();
    }]) });
  const result = await run(payload());
  assert.equal(result.status, "completed");
  assert.equal(operations[0], `root-run/${result.toolCalls[0].toolCallId}`);
  assert.equal(snapshots.at(-1).settled, true);
  assert.ok(!JSON.stringify(snapshots).includes("test-secret"));
  assert.ok(snapshots.some((snapshot) => snapshot.events.some((event) => event.type === "run_finished")));
});

test("completed tool receipt reconstructs missing tool message without replaying write", async () => {
  const assistant = call(), id = assistant.content[0].id;
  const checkpoint = { version: 1, messages: [{ role: "user", content: "do", timestamp: 1 }, assistant],
    tool_calls: [{ name: "example.action", toolCallId: id, arguments: { kind: "chemicals" }, state: "completed",
      result: { action_status: "applied", object_refs: [{ kind: "event", id: "saved-before-crash" }] } }] };
  let writes = 0, seen;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      writes += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([(context) => { seen = context.messages; return finish(); }]) });
  const result = await run(payload(checkpoint));
  assert.equal(writes, 0);
  assert.equal(result.status, "completed");
  assert.ok(seen.some((message) => message.role === "toolResult" && message.toolCallId === id));
  assert.ok(result.object_refs.some((ref) => ref.id === "saved-before-crash"));
});

test("concurrent tool progress stays paired with its queued checkpoint snapshot", async () => {
  const parallel = call();
  parallel.content.push(fauxToolCall("example.action", { kind: "chemicals" }));
  const progress = [];
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec(false)] });
      if (path === "/v2/agent/checkpoint") {
        const body = JSON.parse(options.body);
        if (body.event) {
          assert.deepEqual(body.event, body.checkpoint.events.at(-1));
          progress.push(body.event);
        }
        await new Promise((resolve) => setTimeout(resolve, 2));
        return reply({ ok: true });
      }
      return reply({ action_status: "applied" });
    }, modelFactory: factory([parallel, finish()]) });
  const result = await run(payload());
  assert.equal(result.status, "completed");
  const receipts = progress.filter((event) => event.type === "tool_result_received" && event.tool === "example.action");
  assert.equal(receipts.length, 2);
  assert.equal(new Set(receipts.map((event) => event.toolCallId)).size, 2);
});

test("ambiguous pending write never replays or asks model to recreate side effect", async () => {
  const assistant = call(), id = assistant.content[0].id;
  let writes = 0, providerCalls = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      writes += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([() => { providerCalls += 1; return finish(); }]) });
  const result = await run(payload({ messages: [{ role: "user", content: "do", timestamp: 1 }, assistant],
    tool_calls: [{ name: "example.action", toolCallId: id, arguments: { kind: "chemicals" }, state: "pending" }] }));
  assert.equal(writes, 0); assert.equal(providerCalls, 0);
  assert.equal(result.status, "needs_review");
  assert.equal(result.recovery.operation_id, `root-run/${id}`);
});

test("local error tool message cannot resolve pending write outcome after crash", async () => {
  const assistant = call(), id = assistant.content[0].id;
  let writes = 0, models = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      writes += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([() => { models += 1; return finish(); }]) });
  const result = await run(payload({ messages: [{ role: "user", content: "do", timestamp: 1 }, assistant,
    { role: "toolResult", toolCallId: id, toolName: "example.action", isError: true,
      content: [{ type: "text", text: "connection lost" }], timestamp: 2 }],
    tool_calls: [{ name: "example.action", toolCallId: id, arguments: { kind: "chemicals" }, state: "pending", isError: true }] }));
  assert.equal(writes, 0); assert.equal(models, 0);
  assert.equal(result.status, "needs_review");
});

test("pending explicitly read-only tool resumes with stable operation id", async () => {
  const assistant = call(), id = assistant.content[0].id;
  let operation;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec(false)] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      operation = options.headers["X-Operation-Id"]; return reply({ action_status: "applied" });
    }, modelFactory: factory([finish()]) });
  const result = await run(payload({ messages: [{ role: "user", content: "do", timestamp: 1 }, assistant],
    tool_calls: [{ name: "example.action", toolCallId: id, arguments: { kind: "chemicals" }, state: "pending" }] }));
  assert.equal(operation, `root-run/${id}`);
  assert.equal(result.status, "completed");
});

test("unstarted mutation with no durable intent can execute once after recovery", async () => {
  const assistant = call(), id = assistant.content[0].id;
  let writes = 0, sawIntent = false;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") {
        const cp = JSON.parse(options.body).checkpoint;
        sawIntent ||= cp.tool_calls.some((item) => item.toolCallId === id && item.state === "pending");
        return reply({ ok: true });
      }
      assert.equal(sawIntent, true); writes += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([finish()]) });
  const result = await run(payload({ messages: [{ role: "user", content: "do", timestamp: 1 }, assistant], tool_calls: [] }));
  assert.equal(writes, 1);
  assert.equal(result.status, "completed");
});

test("restored provider failure cannot silently become a completed task", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => new URL(url).pathname === "/v2/agent/tools" ? reply({ tools: [] }) : reply({ ok: true }),
    modelFactory: factory([finish()]) });
  const result = await run(payload({ messages: [{ role: "user", content: "do", timestamp: 1 },
    fauxAssistantMessage("", { stopReason: "error", errorMessage: "provider unavailable before restart" })], tool_calls: [] }));
  assert.equal(result.status, "failed");
  assert.match(result.error, /provider unavailable/);
});

test("unverified settled checkpoint is invalidated without model or gateway execution", async () => {
  const saved = { text: "already done", status: "completed", object_refs: [], toolCalls: [], events: [], delegations: [] };
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async () => { throw new Error("must not fetch"); }, modelFactory: () => { throw new Error("must not plan"); } });
  const result = await run(payload({ settled: true, result: saved }));
  assert.equal(result.status, "failed");
  assert.equal(result.error_code, "unverified_completion_checkpoint");
  assert.equal(result.completion.invalidated_checkpoint, true);
  assert.notEqual(result.text, saved.text);
});

test("network loss after mutating dispatch stops automatic retries with review evidence", async () => {
  let writes = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      writes += 1; throw new Error("connection lost after send");
    }, modelFactory: factory([call(), call(), finish()]) });
  const result = await run(payload());
  assert.equal(writes, 1);
  assert.equal(result.status, "needs_review");
  assert.equal(result.recovery.code, "ambiguous_tool_outcome");
});

test("server errors and malformed mutation receipts stop automatic retry", async (t) => {
  const cases = [
    ["HTTP500 JSON error after possible commit", () => new Response(JSON.stringify({ error: {
      code: "internal_error", message: "failed after commit" } }), { status: 500 })],
    ["HTTP200 invalid JSON", () => new Response("not JSON", { status: 200 })],
    ["HTTP200 missing action_status", () => reply({ ok: true, result: { id: "possibly-created" } })],
    ["HTTP200 invalid action_status", () => reply({ ok: true, action_status: null })],
  ];
  for (const [name, response] of cases) await t.test(name, async () => {
    let writes = 0;
    const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
      fetchImpl: async (url) => {
        const path = new URL(url).pathname;
        if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
        if (path === "/v2/agent/checkpoint") return reply({ ok: true });
        writes += 1; return response();
      }, modelFactory: factory([call(), call(), finish()]) });
    const result = await run(payload());
    assert.equal(writes, 1);
    assert.equal(result.status, "needs_review");
    assert.equal(result.recovery.code, "ambiguous_tool_outcome");
    assert.equal(result.toolCalls[0].state, "pending");
  });
});

test("explicit HTTP400 mutation refusal remains recoverable by model correction", async () => {
  let writes = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/checkpoint") return reply({ ok: true });
      writes += 1;
      return writes === 1 ? new Response(JSON.stringify({ error: { code: "schema_violation", message: "correct params" } }), { status: 400 })
        : reply({ action_status: "applied" });
    }, modelFactory: factory([call(), call(), finish()]) });
  const result = await run(payload());
  assert.equal(writes, 2);
  assert.equal(result.status, "completed");
  assert.equal(result.recovery, undefined);
});

test("checkpoint refuses oversized transcript instead of silently corrupting recovery", () => {
  assert.throws(() => boundedSnapshot({ messages: ["x".repeat(1_900_000)] }, (value) => value),
    (error) => error.code === "checkpoint_size_limit" && error.retryable === false);
  const tool = withContract({ name: "nested", parameters: { type: "object", properties: { items: { type: "array", items: {
    type: "object", properties: { event_kind: { type: "string" } }, required: ["event_kind"] } } } } });
  assert.throws(() => tool.prepareArguments({ items: [{}] }), /items.0.event_kind/);
});

test("pending delegation resumes same child checkpoint and preserves both timelines", async () => {
  const parentMessage = fauxAssistantMessage(fauxToolCall("agent.delegate", { agent_id: "b_query", message: "query" }));
  const delegateId = parentMessage.content[0].id;
  const childMessage = call(), childId = childMessage.content[0].id;
  const childCheckpoint = { messages: [{ role: "user", content: "query", timestamp: 1 }, childMessage],
    tool_calls: [{ toolCallId: childId, name: "example.action", arguments: { kind: "chemicals" }, state: "completed",
      result: { action_status: "applied", object_refs: [{ kind: "event", id: "prior-child-event" }] } }] };
  const snapshots = [], completed = [];
  let writes = 0, delegated;
  const run = createRuntime({ gatewayUrl: "http://core", token: "test-secret", role: "orchestrator",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname, body = options.body ? JSON.parse(options.body) : null;
      if (path === "/v2/agent/tools") return reply({ tools: [spec()] });
      if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
      if (path === "/v2/agent/checkpoint") { snapshots.push(body.checkpoint); return reply({ ok: true }); }
      if (path === "/v2/agent/delegate") { delegated = body; return reply({ run_id: "child-run", task_id: "child-task",
        agent_id: "b_query", run_token: "child-secret", claim_epoch: 2, checkpoint: childCheckpoint, manifest: {} }); }
      if (path === "/v2/agent/delegate/complete") { completed.push(body); return reply({ ok: true }); }
      writes += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([finish()]) });
  const result = await run(payload({ messages: [{ role: "user", content: "query", timestamp: 1 }, parentMessage],
    tool_calls: [{ name: "agent.delegate", toolCallId: delegateId, arguments: parentMessage.content[0].arguments, state: "pending" }] }));
  assert.equal(result.status, "completed");
  assert.equal(writes, 0);
  assert.equal(delegated.delegation_id, `root-run/${delegateId}`);
  assert.equal(completed.length, 1);
  assert.ok(snapshots.some((snapshot) => snapshot.run_id === "child-run"));
  assert.ok(snapshots.some((snapshot) => snapshot.run_id === "root-run"));
  assert.ok(result.object_refs.some((ref) => ref.id === "prior-child-event"));
  assert.ok(!JSON.stringify(snapshots).includes("child-secret"));
});

test("HTTP cancel endpoint aborts an actual in-flight gateway request", async (t) => {
  let reachedTool;
  const toolReached = new Promise((resolve) => { reachedTool = resolve; });
  const gateway = http.createServer(async (req, res) => {
    for await (const _chunk of req) { /* drain */ }
    if (req.method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ tools: [{ type: "function", function: { name: "query.events.search", mutates: false,
        description: "test query", parameters: { type: "object", properties: {} } } }] }));
    } else reachedTool();
  });
  await new Promise((resolve) => gateway.listen(0, "127.0.0.1", resolve));
  t.after(() => { gateway.closeAllConnections(); gateway.close(); });
  const child = spawn(process.execPath, [fileURLToPath(new URL("./server.mjs", import.meta.url))], {
    env: { ...process.env, AGENT_TOKEN: "cancel-test-token", AGENT_ROLE: "b_query", PI_MOCK: "1", PI_API_KEY: "",
      GATEWAY_URL: `http://127.0.0.1:${gateway.address().port}` }, windowsHide: true,
  });
  t.after(() => child.kill());
  const port = await new Promise((resolve, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error("startup timed out")), 10000);
    child.once("error", reject);
    child.stdout.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/PI_SERVER_PORT=(\d+)/);
      if (match) { clearTimeout(timeout); resolve(Number(match[1])); }
    });
  });
  const response = fetch(`http://127.0.0.1:${port}/run`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: "s", message: "slow query" }) }).then((res) => res.json());
  await Promise.race([toolReached, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error("gateway never called")), 3000); timer.unref();
  })]);
  const cancel = await fetch(`http://127.0.0.1:${port}/cancel`, { method: "POST" }).then((res) => res.json());
  assert.equal(cancel.cancelling, true);
  assert.equal((await response).status, "cancelled");
});
