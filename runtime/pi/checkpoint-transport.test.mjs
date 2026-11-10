import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

test("real local host accepts bounded gzip recovery, preserves split UTF8 and terminates deterministic limits", async (t) => {
  let reads = 0, lastCheckpoint;
  const gateway = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    let value;
    if (req.url === "/v2/agent/checkpoint") {
      lastCheckpoint = JSON.parse(Buffer.concat(chunks)).checkpoint; value = { ok: true };
    } else if (req.url === "/v2/agent/tools") value = { tools: [{ type: "function", function: {
      name: "query.events.search", mutates: false, description: "query", parameters: { type: "object", properties: {} } } }] };
    else { reads += 1; value = { action_status: "applied" }; }
    res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(value));
  });
  await new Promise((resolve) => gateway.listen(0, "127.0.0.1", resolve));
  t.after(() => { gateway.closeAllConnections(); gateway.close(); });
  const child = spawn(process.execPath, [fileURLToPath(new URL("./server.mjs", import.meta.url))], {
    windowsHide: true, env: { ...process.env, AGENT_TOKEN: "fixture-secret", AGENT_ROLE: "b_query", PI_MOCK: "1",
      GATEWAY_URL: `http://127.0.0.1:${gateway.address().port}` },
  });
  t.after(() => child.kill());
  const port = await new Promise((resolve, reject) => {
    let output = ""; const timer = setTimeout(() => reject(new Error("startup timeout")), 10000);
    child.once("error", reject);
    child.stdout.on("data", (chunk) => { output += chunk; const match = output.match(/PI_SERVER_PORT=(\d+)/);
      if (match) { clearTimeout(timer); resolve(Number(match[1])); } });
  });
  const send = async (raw, encoding) => {
    const response = await fetch(`http://127.0.0.1:${port}/run`, { method: "POST",
      headers: { "Content-Type": "application/json", ...(encoding ? { "Content-Encoding": encoding } : {}) }, body: raw });
    return { status: response.status, body: await response.json() };
  };
  const payload = { session_id: "ipc", message: "汉🙂字", durable: true, context: { run_id: "root" } };
  const gzip = await send(gzipSync(Buffer.from(JSON.stringify({ ...payload, resume: { checkpoints: {}, padding: "x".repeat(3_000_000) } }))), "gzip");
  assert.equal(gzip.status, 200); assert.equal(gzip.body.status, "completed"); assert.equal(reads, 1);

  const raw = Buffer.from(JSON.stringify(payload));
  const cut = raw.indexOf(Buffer.from("汉")) + 1;
  const fragmented = await new Promise((resolve, reject) => {
    const request = http.request({ host: "127.0.0.1", port, path: "/run", method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": raw.length } }, (response) => {
      const chunks = []; response.on("data", (c) => chunks.push(c));
      response.on("end", () => resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks)) }));
    });
    request.on("error", reject); request.write(raw.subarray(0, cut));
    setTimeout(() => request.end(raw.subarray(cut)), 20);
  });
  assert.equal(fragmented.status, 200);
  assert.ok(JSON.stringify(lastCheckpoint.messages).includes("汉🙂字"));
  const before = reads;
  const oversize = await send(Buffer.from(JSON.stringify({ ...payload, padding: "x".repeat(2_000_000) })));
  assert.equal(oversize.status, 413); assert.equal(oversize.body.error.code, "runtime_request_size_limit");
  assert.equal(oversize.body.error.retryable, false); assert.equal(reads, before);
  const bomb = await send(gzipSync(Buffer.from(JSON.stringify({ ...payload, padding: "x".repeat(8 * 1024 * 1024) }))), "gzip");
  assert.equal(bomb.status, 413); assert.equal(bomb.body.error.retryable, false); assert.equal(reads, before);
  const checkpoint = await send(gzipSync(Buffer.from(JSON.stringify({ ...payload,
    context: { run_id: "root", unique_text: "x".repeat(1_900_000) } }))), "gzip");
  assert.equal(checkpoint.status, 422); assert.equal(checkpoint.body.error.code, "checkpoint_size_limit");
  assert.equal(checkpoint.body.error.retryable, false); assert.equal(checkpoint.body.result.status, "failed");
  assert.ok(!JSON.stringify(checkpoint.body).includes("fixture-secret")); assert.equal(reads, before);
});
