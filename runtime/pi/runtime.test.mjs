import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime, loadPrompt, summarizeReceipts } from "./runtime.mjs";

const tool = (name, properties = {}) => ({ type: "function", function: {
  name, description: name, parameters: { type: "object", properties },
} });
const reply = (value, status = 200) => new Response(JSON.stringify(value), { status });
const text = (value) => fauxAssistantMessage(fauxText(value));
const call = (name, args) => fauxAssistantMessage(fauxToolCall(name, args));
function scriptedFactory(scripts, seen = []) {
  return ({ role, roles, tools }) => {
    seen.push({ role, roles, tools: tools.map((item) => item.name) });
    const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test",
      reasoning: false, input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
    faux.setResponses(scripts[role]);
    return { model: faux.getModel(), streamFn: (model, context, options) =>
      faux.provider.streamSimple(model, context, options) };
  };
}

test("real Pi loop delegates, observes validation failure, retries, and preserves pending receipt", async () => {
  const requests = [], models = [];
  const parentReceipts = [];
  const observeDelegate = (context) => {
    const last = context.messages.at(-1);
    parentReceipts.push(JSON.parse(last.content[0].text));
  };
  let searches = 0;
  const roles = [
    { agent_id: "b_query", max_delegation_depth: 0 },
    { agent_id: "b_capture", max_delegation_depth: 0 },
  ];
  const fetchImpl = async (url, options) => {
    const path = new URL(url).pathname;
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ path, token: options.headers.Authorization, body });
    if (path === "/v2/agent/tools") return reply({ budget_tool_calls: 30,
      used_tool_calls: 0, tools: [tool("query.events.search", { text: { type: "string" } }),
        tool("memory.proposal.create"), tool("memory.proposal.confirm_items")] });
    if (path === "/v2/agent/roles") return reply({ roles });
    if (path === "/v2/agent/delegate") return reply({ task_id: `task-${body.agent_id}`,
      run_id: `run-${body.agent_id}`, run_token: `secret-${body.agent_id}`, claim_epoch: 1,
      agent_id: body.agent_id, manifest: { timeout_s: 2, max_delegation_depth: 0 } });
    if (path === "/v2/agent/delegate/complete") return reply({ run: { status: body.status } });
    if (path.endsWith("query.events.search")) {
      searches += 1;
      return searches === 1 ? reply({ error: { code: "schema_violation", message: "revise text" } }, 400)
        : reply({ ok: true, action_status: "applied", object_refs: [{ kind: "event", id: "evt-1" }] });
    }
    if (path.endsWith("memory.proposal.create")) return reply({ ok: true,
      action_status: "proposed", needs_confirmation: true,
      object_refs: [{ kind: "proposal", id: "proposal-1" }], result: { unknown_items: ["sample label"] } });
    throw new Error(`unexpected path ${path}`);
  };
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", role: "orchestrator",
    fetchImpl, modelFactory: scriptedFactory({
      orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "检索历史" }),
        (context) => { observeDelegate(context); return call("agent.delegate", { agent_id: "b_capture", message: "整理页面 p1" }); },
        (context) => { observeDelegate(context); return text("已生成待核对提案。"); }],
      b_query: [call("query.events.search", { text: "wrong" }), call("query.events.search", { text: "sample" }), text("evt-1")],
      b_capture: [call("memory.proposal.create", {}), text("proposal-1 待人工核对。")],
    }, models) });
  const result = await run({ session_id: "s1", message: "整理并检查", context: { project_id: "p", pages: [{ page_id: "p1" }] } });
  assert.equal(searches, 2, "provider continued after seeing tool failure");
  assert.equal(result.delegations.length, 2);
  assert.equal(result.status, "partial", "query retry failure remains visible alongside successful work");
  assert.ok(result.object_refs.some((ref) => ref.id === "proposal-1"));
  assert.equal(result.delegations[1].status, "needs_review");
  assert.ok(models.every((entry) => !entry.tools.includes("memory.proposal.confirm_items")));
  assert.ok(requests.filter((entry) => entry.path === "/v2/agent/delegate/complete")
    .every((entry) => entry.token === "Bearer root-secret"));
  assert.ok(requests.some((entry) => entry.path.endsWith("query.events.search") && entry.token === "Bearer secret-b_query"));
  assert.ok(!JSON.stringify(result).includes("secret"));
  assert.ok(result.events.some((event) => event.type === "delegation_completed"));
  assert.equal(result.delegations[0].toolCalls.length, 2, "host retains full child diagnostics");
  assert.equal(parentReceipts.length, 2);
  for (const receipt of parentReceipts) {
    assert.ok(receipt.text);
    assert.ok(!("toolCalls" in receipt));
    assert.ok(!("events" in receipt));
    assert.ok(!("delegations" in receipt));
  }
  for (const request of requests.filter((entry) => entry.path.endsWith("delegate/complete"))) {
    assert.ok(Array.isArray(request.body.result.events));
    assert.ok(!("toolCalls" in request.body.result));
    assert.ok(!("delegations" in request.body.result));
  }
});

test("shared tree budget stops child calls and parent planning", async () => {
  let executed = 0;
  const fetchImpl = async (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/v2/agent/tools") return reply({ tools: [tool("query.events.search")] });
    if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
    if (path === "/v2/agent/delegate") return reply({ task_id: "ct", run_id: "cr",
      run_token: "child-secret", claim_epoch: 1, agent_id: "b_query", manifest: {} });
    if (path === "/v2/agent/delegate/complete") return reply({ run: { status: "failed" } });
    executed += 1;
    return reply({ ok: true, action_status: "applied" });
  };
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", role: "orchestrator",
    maxToolCalls: 2, fetchImpl, modelFactory: scriptedFactory({
      orchestrator: [call("agent.delegate", { agent_id: "b_query", message: "query" }), text("unreachable")],
      b_query: [call("query.events.search", {}), call("query.events.search", {}), text("unreachable")],
    }) });
  const result = await run({ session_id: "s", message: "search" });
  assert.equal(executed, 1);
  assert.match(result.error, /budget/);
  assert.match(result.delegations[0].error, /budget/);
});

test("wall clock timeout aborts in-flight gateway and returns failure", async () => {
  const fetchImpl = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret",
    fetchImpl, timeoutMs: 25, mock: true });
  const started = Date.now();
  const result = await run({ session_id: "s", message: "search" });
  assert.equal(result.status, "failed");
  assert.ok(Date.now() - started < 1000);
});

test("receipt status cannot promote pending work into completed work", () => {
  assert.equal(summarizeReceipts([{ result: { action_status: "proposed" } }], []).status, "needs_review");
  assert.equal(summarizeReceipts([{ result: { action_status: "clarification_needed" } }], []).status, "clarification_needed");
  assert.equal(summarizeReceipts([{ result: { ok: false } }], []).status, "failed");
  assert.equal(summarizeReceipts([{ result: { action_status: "applied",
    result: { proposal: { status: "awaiting_confirmation" } } } }], []).status, "needs_review");
  assert.equal(summarizeReceipts([{ result: { action_status: "applied",
    result: { proposal: { status: "awaiting_clarification" } } } }], []).status, "clarification_needed");
  assert.equal(summarizeReceipts([{ result: { action_status: "applied",
    result: { historical_task: { status: "failed", ok: false } } } }], []).status, "completed");
});

test("role prompt loading rejects path traversal and missing roles", async () => {
  assert.match(await loadPrompt("orchestrator"), /自主/);
  await assert.rejects(loadPrompt("../orchestrator"), /invalid agent role/);
  await assert.rejects(loadPrompt("missing_role"), /ENOENT/);
});

test("Core history text field and client content field both reach the next model turn", async () => {
  let observed;
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", role: "b_query",
    fetchImpl: async () => reply({ tools: [] }),
    modelFactory: scriptedFactory({ b_query: [(context) => {
      observed = context.messages;
      return text("已保留上轮样品上下文。");
    }] }) });
  await run({ session_id: "s", message: "继续整理这个样品", history: [
    { role: "user", text: "样品 U66-D01", context: { pages: [{ page_id: "uploaded-p7" }] } },
    { role: "assistant", content: "已读取 page-1" },
  ] });
  assert.match(JSON.stringify(observed), /U66-D01/);
  assert.match(JSON.stringify(observed), /page-1/);
  assert.match(JSON.stringify(observed), /uploaded-p7/);
});

test("wall clock timeout also cancels the Pi provider signal", async () => {
  let providerAborted = false;
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", timeoutMs: 30,
    fetchImpl: async () => reply({ tools: [] }),
    modelFactory: scriptedFactory({ b_query: [async (_context, options) => {
      await new Promise((resolve) => options.signal.addEventListener("abort", () => {
        providerAborted = true; resolve();
      }, { once: true }));
      return fauxAssistantMessage("", { stopReason: "aborted", errorMessage: "aborted" });
    }] }) });
  const result = await run({ session_id: "s", message: "slow model" });
  assert.equal(providerAborted, true);
  assert.equal(result.status, "failed");
  assert.match(result.error, /deadline/);
});

test("agent.respond lets the model finalize a genuinely recovered tool failure", async () => {
  let searches = 0;
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret",
    fetchImpl: async (url) => {
      if (new URL(url).pathname === "/v2/agent/tools") return reply({ tools: [tool("query.events.search")] });
      searches += 1;
      return searches === 1 ? reply({ error: { code: "schema_violation", message: "correct params" } }, 400)
        : reply({ action_status: "applied", object_refs: [{ kind: "event", id: "recovered-e1" }] });
    }, modelFactory: scriptedFactory({ b_query: [
      call("query.events.search", {}), call("query.events.search", {}),
      call("agent.respond", { status: "completed", text: "已修正检索参数，找到 recovered-e1。" }),
    ] }) });
  const result = await run({ session_id: "s", message: "search" });
  assert.equal(searches, 2);
  assert.equal(result.status, "completed");
  assert.match(result.text, /已修正/);
  assert.equal(result.toolCalls[0].result.ok, false, "recovered error remains diagnostic");
  assert.equal(result.toolCalls.at(-1).name, "agent.respond");
  assert.equal(result.error, undefined);
});

test("agent.respond cannot promote a real pending proposal into completed work", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", role: "b_capture",
    fetchImpl: async (url) => new URL(url).pathname === "/v2/agent/tools"
      ? reply({ tools: [tool("memory.proposal.create")] })
      : reply({ action_status: "applied", result: { proposal: { status: "awaiting_confirmation" } },
        object_refs: [{ kind: "proposal", id: "pending-p1" }] }),
    modelFactory: scriptedFactory({ b_capture: [call("memory.proposal.create", {}),
      call("agent.respond", { status: "completed", text: "已创建 pending-p1。" })] }) });
  const result = await run({ session_id: "s", message: "capture" });
  assert.equal(result.status, "needs_review");
  assert.equal(result.object_refs[0].id, "pending-p1");
});

test("agent.respond cannot hide a fatal child provider failure", async () => {
  const run = createRuntime({ gatewayUrl: "http://core", token: "root-secret", role: "orchestrator",
    fetchImpl: async (url) => {
      const path = new URL(url).pathname;
      if (path === "/v2/agent/tools") return reply({ tools: [tool("query.events.search")] });
      if (path === "/v2/agent/roles") return reply({ roles: [{ agent_id: "b_query" }] });
      if (path === "/v2/agent/delegate") return reply({ task_id: "ct", run_id: "cr",
        run_token: "child-secret", claim_epoch: 1, agent_id: "b_query", manifest: {} });
      if (path === "/v2/agent/delegate/complete") return reply({ run: { status: "failed" } });
      return reply({ action_status: "applied" });
    }, modelFactory: scriptedFactory({
      orchestrator: [call("query.events.search", {}), call("agent.delegate", { agent_id: "b_query", message: "query" }),
        call("agent.respond", { status: "completed", text: "完成。" })],
      b_query: [fauxAssistantMessage("", { stopReason: "error", errorMessage: "provider unavailable" })],
    }) });
  const result = await run({ session_id: "s", message: "search" });
  assert.equal(result.status, "partial");
  assert.equal(result.delegations[0].status, "failed");
  assert.match(result.delegations[0].error, /provider unavailable/);
});

test("legacy HTTP host health and /run remain usable with offline Pi provider", async (t) => {
  const gateway = http.createServer(async (req, res) => {
    assert.equal(req.headers.authorization, "Bearer legacy-test-token");
    const result = req.method === "GET" ? { tools: [tool("query.events.search")], budget_tool_calls: 10 }
      : { ok: true, action_status: "applied", object_refs: [{ kind: "event", id: "legacy-e1" }] };
    // Drain the request to allow a persistent HTTP connection to close cleanly.
    for await (const _chunk of req) { /* no-op */ }
    res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(result));
  });
  await new Promise((resolve) => gateway.listen(0, "127.0.0.1", resolve));
  t.after(() => { gateway.closeAllConnections(); gateway.close(); });
  const child = spawn(process.execPath, [fileURLToPath(new URL("./server.mjs", import.meta.url))], {
    env: { ...process.env, AGENT_TOKEN: "legacy-test-token", AGENT_ROLE: "b_query",
      PI_MOCK: "1", PI_API_KEY: "", GATEWAY_URL: `http://127.0.0.1:${gateway.address().port}` },
    windowsHide: true,
  });
  t.after(() => child.kill());
  const port = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("host startup timed out")), 10000);
    child.once("error", reject);
    child.stdout.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/PI_SERVER_PORT=(\d+)/);
      if (match) { clearTimeout(timer); resolve(Number(match[1])); }
    });
  });
  const health = await fetch(`http://127.0.0.1:${port}/health`).then((res) => res.json());
  assert.equal(health.ok, true);
  const result = await fetch(`http://127.0.0.1:${port}/run`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: "old-session", message: "检索记录" }) })
    .then((res) => res.json());
  assert.equal(result.status, "completed");
  assert.equal(result.toolCalls[0].name, "query.events.search");
  assert.equal(result.object_refs[0].id, "legacy-e1");
});
