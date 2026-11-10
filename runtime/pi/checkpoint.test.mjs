import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

const reply = (value, status = 200) => new Response(JSON.stringify(value), { status });
const action = { type: "function", function: { name: "example.write", mutates: true, description: "fixture",
  parameters: { type: "object", properties: {} } } };
const call = () => fauxAssistantMessage(fauxToolCall("example.write", {}));
const finish = () => fauxAssistantMessage(fauxToolCall("agent.respond", { status: "completed", text: "done" }));
const payload = (checkpoint) => ({ session_id: "checkpoint-test", message: "write one item", durable: true,
  context: { run_id: "root-run" }, ...(checkpoint ? { resume: { checkpoints: { "root-run": checkpoint } } } : {}) });
function factory(responses) {
  return () => {
    const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test", reasoning: false,
      input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(responses);
    return { model: faux.getModel(), streamFn: (model, context, options) => faux.provider.streamSimple(model, context, options) };
  };
}
const controlError = (error) => {
  assert.equal(error.code, "checkpoint_unavailable");
  assert.equal(error.retryable, true);
  assert.ok(!error.message.includes("fixture-secret"));
  return true;
};

test("intent checkpoint failure prevents any write and raises retryable control error", async () => {
  let writes = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "fixture-secret",
    fetchImpl: async (url, options) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [action] });
      if (path === "/v2/agent/checkpoint") {
        const body = JSON.parse(options.body);
        return body.event?.type === "tool_call_requested"
          ? reply({ error: { code: "storage_failed", message: "fixture-secret" } }, 500) : reply({ ok: true });
      }
      writes += 1; return reply({ action_status: "applied" });
    }, modelFactory: factory([call(), finish()]) });
  await assert.rejects(run(payload()), controlError);
  assert.equal(writes, 0);
});

test("receipt checkpoint failure leaves durable intent; recovery reviews without repeating effect", async () => {
  let saved, failReceipt = true, writes = 0, operation;
  const fetchImpl = async (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/v2/agent/tools") return reply({ tools: [action] });
    if (path === "/v2/agent/checkpoint") {
      const body = JSON.parse(options.body);
      if (failReceipt && body.event?.type === "tool_result_received") return reply({ error: { code: "storage_failed" } }, 500);
      saved = structuredClone(body.checkpoint); return reply({ ok: true });
    }
    writes += 1; operation = options.headers["X-Operation-Id"];
    return reply({ action_status: "applied", object_refs: [{ kind: "event", id: "committed-once" }] });
  };
  const create = () => createRuntime({ gatewayUrl: "http://core", token: "fixture-secret", fetchImpl,
    modelFactory: factory([call(), finish()]) });
  await assert.rejects(create()(payload()), controlError);
  assert.equal(writes, 1);
  assert.equal(saved.tool_calls[0].state, "pending");
  assert.equal(saved.tool_calls[0].operation_id, operation);
  const recoverable = structuredClone(saved);
  failReceipt = false;
  const restored = await create()(payload(recoverable));
  assert.equal(restored.status, "needs_review");
  assert.equal(restored.recovery.code, "ambiguous_tool_outcome");
  assert.equal(restored.recovery.operation_id, operation);
  assert.equal(writes, 1, "a persisted pending mutation cannot replay");
});

test("unique oversized durable snapshot is a terminal control failure before any tool runs", async () => {
  let writes = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "fixture-secret",
    fetchImpl: async () => { writes += 1; return reply({ tools: [] }); }, modelFactory: factory([finish()]) });
  await assert.rejects(run({ ...payload(), context: { run_id: "root-run", large_reference: "x".repeat(1_900_000) } }),
    (error) => error.code === "checkpoint_size_limit" && error.retryable === false && error.result.status === "failed");
  assert.equal(writes, 1, "only read-only tool discovery happened");
});

test("real HTTP host returns safe 503 for checkpoint failure, while legacy failures remain 200", async (t) => {
  let mode = "checkpoint", checkpoints = 0;
  const gateway = http.createServer(async (req, res) => {
    for await (const _chunk of req) { /* drain */ }
    let status = 200, value;
    if (req.url === "/v2/agent/checkpoint") {
      checkpoints += 1; status = 500; value = { error: { code: "storage_failed", message: "fixture-secret" } };
    } else if (mode === "legacy-error") {
      status = 500; value = { error: { code: "fixture_gateway_error", message: "fixture error" } };
    } else if (req.method === "GET") value = { tools: [{ type: "function", function: { name: "query.events.search",
      mutates: false, description: "query", parameters: { type: "object", properties: {} } } }] };
    else { status = 400; value = { error: { code: "schema_violation", message: "fixture business refusal" } }; }
    res.writeHead(status, { "Content-Type": "application/json" }); res.end(JSON.stringify(value));
  });
  await new Promise((resolve) => gateway.listen(0, "127.0.0.1", resolve));
  t.after(() => { gateway.closeAllConnections(); gateway.close(); });
  const child = spawn(process.execPath, [fileURLToPath(new URL("./server.mjs", import.meta.url))], {
    windowsHide: true, env: { ...process.env, AGENT_TOKEN: "fixture-secret", AGENT_ROLE: "b_query",
      PI_MOCK: "1", PI_API_KEY: "fixture-private-key", GATEWAY_URL: `http://127.0.0.1:${gateway.address().port}` },
  });
  t.after(() => child.kill());
  const port = await new Promise((resolve, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error("host startup timed out")), 10000);
    child.once("error", reject);
    child.stdout.on("data", (chunk) => {
      output += chunk; const match = output.match(/PI_SERVER_PORT=(\d+)/);
      if (match) { clearTimeout(timeout); resolve(Number(match[1])); }
    });
  });
  const submit = async (value) => {
    const response = await fetch(`http://127.0.0.1:${port}/run`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
    return { status: response.status, body: await response.json() };
  };
  const failedCheckpoint = await submit(payload());
  assert.equal(failedCheckpoint.status, 503);
  assert.equal(failedCheckpoint.body.error.code, "checkpoint_unavailable");
  assert.equal(failedCheckpoint.body.error.retryable, true);
  const publicBody = JSON.stringify(failedCheckpoint.body);
  for (const forbidden of ["fixture-secret", "fixture-private-key", "messages", "thinking", "run_token", "api_key"]) {
    assert.ok(!publicBody.includes(forbidden));
  }
  assert.equal(checkpoints, 1);
  mode = "legacy-error";
  const legacyFailure = await submit({ session_id: "legacy", message: "query" });
  assert.equal(legacyFailure.status, 200);
  assert.equal(legacyFailure.body.status, "failed");
  assert.equal(legacyFailure.body.error_code, "fixture_gateway_error");
  assert.equal(legacyFailure.body.completion.protocol_version, 3);
  mode = "business-error";
  const businessFailure = await submit({ session_id: "legacy", message: "query" });
  assert.equal(businessFailure.status, 200);
  assert.equal(businessFailure.body.status, "failed");
  assert.equal(businessFailure.body.completion.mode, "explicit");
});
