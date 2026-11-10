// Local Pi host. /run remains compatible with legacy {session_id,message} clients.
// Credentials are inherited once through the host environment, never written to disk.
import http from "node:http";
import { createRuntime } from "./runtime.mjs";
import { gunzipSync } from "node:zlib";

const role = process.env.AGENT_ROLE || "b_query";
if (!process.env.AGENT_TOKEN) {
  process.stderr.write("AGENT_TOKEN is required (run token from Core)\n");
  process.exit(2);
}
const requestedTimeout = Number(process.env.PI_RUN_TIMEOUT_MS || 165000);
const run = createRuntime({
  gatewayUrl: process.env.GATEWAY_URL || "http://127.0.0.1:8000",
  token: process.env.AGENT_TOKEN, role,
  timeoutMs: Number.isFinite(requestedTimeout) && requestedTimeout > 0 ? requestedTimeout : 165000,
  ...(process.env.PI_CLOSING_RESERVE_MS !== undefined ? { closingReserveMs: Number(process.env.PI_CLOSING_RESERVE_MS) } : {}),
  mock: process.env.PI_MOCK === "1",
});
let busy = false;
let activeController;
const server = http.createServer(async (request, response) => {
  const path = new URL(request.url, "http://localhost").pathname;
  const send = (status, value) => {
    if (response.destroyed) return;
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(value));
  };
  if (request.method === "GET" && path === "/health") {
    send(200, { ok: true, mock: process.env.PI_MOCK === "1", role }); return;
  }
  if (request.method === "POST" && path === "/cancel") {
    const cancelling = Boolean(activeController);
    activeController?.abort();
    send(200, { ok: true, cancelling }); return;
  }
  if (request.method !== "POST" || path !== "/run") {
    send(404, { error: "not found" }); return;
  }
  if (busy) { send(409, { error: "runtime already processing a scoped run" }); return; }
  const controller = new AbortController();
  activeController = controller;
  response.on("close", () => { if (!response.writableEnded) controller.abort(); });
  busy = true;
  try {
    const chunks = []; let wireBytes = 0;
    const sizeFailure = () => send(413, { error: { code: "runtime_request_size_limit", retryable: false,
      message: "本地运行恢复数据超过安全传输限制，已停止本轮。" } });
    for await (const chunk of request) {
      chunks.push(chunk); wireBytes += chunk.length;
      if (wireBytes > 2_000_000) { sizeFailure(); return; }
    }
    let raw = Buffer.concat(chunks);
    const encoding = request.headers["content-encoding"];
    if (encoding && encoding !== "gzip") { send(400, { error: "unsupported content encoding" }); return; }
    if (encoding === "gzip") {
      try { raw = gunzipSync(raw, { maxOutputLength: 8 * 1024 * 1024 }); }
      catch (error) {
        if (error.code === "ERR_BUFFER_TOO_LARGE") sizeFailure();
        else send(400, { error: "invalid gzip body" });
        return;
      }
    }
    let payload;
    try { payload = JSON.parse(raw.toString("utf8") || "{}"); }
    catch { send(400, { error: "invalid JSON" }); return; }
    if (!payload || typeof payload !== "object" || typeof payload.session_id !== "string"
      || !payload.session_id || typeof payload.message !== "string" || !payload.message.trim()) {
      send(400, { error: "session_id 与 message 必填" }); return;
    }
    // Role and authority always come from the scoped host token, never request fields.
    send(200, await run(payload, { signal: controller.signal }));
  } catch (error) {
    if (["checkpoint_size_limit", "checkpoint_invalid"].includes(error?.code) && error.retryable === false) {
      send(422, { error: { code: error.code, retryable: false,
        message: "任务检查点无法安全保存，本轮已停止；已有记录和回执保留。" },
        ...(error.result ? { result: error.result } : {}) });
      return;
    }
    if (error?.code === "checkpoint_unavailable" && error.retryable === true) {
      // Control-plane failures are not business outcomes. The supervising
      // worker must reclaim the same lease/checkpoint instead of completing it.
      send(503, { error: { code: "checkpoint_unavailable", retryable: true,
        message: "任务进度暂时无法持久保存；请保留当前任务，稍后从已有回执恢复。" } });
      return;
    }
    send(200, { text: "", toolCalls: [], events: [], delegations: [],
      status: "failed", object_refs: [], error: "Pi runtime request failed" });
  } finally { busy = false; activeController = undefined; }
});
server.listen(0, "127.0.0.1", () => {
  process.stdout.write(`PI_SERVER_PORT=${server.address().port}\n`);
});
