import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRuntime } from "./runtime.mjs";
import { realModel } from "./models.mjs";

// Exercise the actual SDK and observer against loopback HTTP, without credentials
// or external requests. The gateway is a separate in-memory fixture.
async function fixture(t, { failIntent = false } = {}) {
  const requests = [], checkpoints = [];
  const server = createServer((req, res) => {
    requests.push(req.url);
    req.resume();
    res.writeHead(429, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { code: "1113", message: "fixture insufficient quota" } }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const env = { PI_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`, PI_MODEL: "fixture" };
  const fetchImpl = async (url, options = {}) => {
    const path = new URL(url).pathname;
    const body = options.body ? JSON.parse(options.body) : null;
    let result;
    if (path === "/v2/agent/tools") result = { tools: [] };
    else if (path === "/v2/agent/roles") result = { roles: [] };
    else if (path === "/v2/agent/checkpoint") {
      checkpoints.push(structuredClone(body.checkpoint));
      if (failIntent && body.checkpoint.provider_requests?.some(row => row.phase === "started")) {
        return new Response(JSON.stringify({ error: { code: "fixture_checkpoint_failed" } }), { status: 500 });
      }
      result = { ok: true };
    } else throw new Error(`Unexpected gateway request ${path}`);
    return new Response(JSON.stringify(result));
  };
  let modelConfig;
  const run = createRuntime({ gatewayUrl: "http://fixture-core", token: "fixture-token", timeoutMs: 5000,
    fetchImpl, modelFactory: ({ observation }) => (modelConfig = realModel(env, observation)) });
  let result, error;
  try {
    result = await run({ durable: true, session_id: "observer-session", message: "read the available evidence",
      context: { run_id: "observer-root" } });
  } catch (caught) { error = caught; }
  return { result, error, requests, checkpoints, observer: modelConfig.providerObserver };
}

test("actual SDK quota response stops the tree before its automatic HTTP retry", async t => {
  const { result, requests, checkpoints } = await fixture(t);
  assert.equal(requests.length, 1);
  assert.equal(result.error_code, "provider_quota_exhausted");
  assert.equal(result.needs_provider_quota, true);
  assert.equal(result.provider_usage.request_count, 1);
  assert.equal(result.provider_usage.quota_exhausted_requests, 1);
  assert.equal(result.provider_usage.usage.total_tokens, null);
  const started = checkpoints.find(cp => cp.provider_requests?.some(row => row.phase === "started"));
  assert.ok(started, "request intent must be checkpointed before the real HTTP request");
  assert.ok(checkpoints.at(-1).provider_requests.some(row => row.error_class === "provider_quota_exhausted"));
});

test("failed durable request intent prevents an actual SDK HTTP submission", async t => {
  const { error, requests, observer } = await fixture(t, { failIntent: true });
  assert.equal(requests.length, 0);
  assert.equal(error?.code, "checkpoint_unavailable");
  assert.equal(observer.summary().request_count, 0);
  assert.equal(observer.summary().not_sent_requests, 1);
});
