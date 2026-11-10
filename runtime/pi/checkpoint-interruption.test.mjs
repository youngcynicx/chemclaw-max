import test from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

const RUN = "checkpoint-interrupted-root";
const REF = { kind: "experiment_event", id: "preserved-event", revision: 1 };
const EXTRA = { kind: "sample", id: "saved-only-sample" };
const completion = { protocol_version: 3, mode: "incomplete", stopReason: "aborted",
  repair_attempts: 0, usage: { input: 0, output: 0 } };

function checkpoint({ calls = [], children = [], refs = [] } = {}) {
  return { version: 3, completion_protocol_version: 3, run_id: RUN, role: "orchestrator",
    settled: true, messages: [], turn_start: 0, tool_calls: calls, delegations: children,
    result: { status: "cancelled", text: "Original interrupted text must not become a final answer.",
      error: "run cancelled", error_code: "run_cancelled", completion,
      object_refs: refs, toolCalls: calls, delegations: children } };
}

async function restore(saved, { durable = true, runId = RUN } = {}) {
  const state = { modelCalls: 0, networkCalls: 0 };
  const runtime = createRuntime({ gatewayUrl: "http://127.0.0.1:1", token: "offline-interruption-test",
    role: "orchestrator", timeoutMs: 900000,
    modelFactory: async () => { state.modelCalls += 1; throw new Error("Unexpected model generation"); },
    fetchImpl: async () => { state.networkCalls += 1; throw new Error("Unexpected gateway request"); } });
  const before = structuredClone(saved);
  const result = await runtime({ durable, message: "Do not replay any operation.", context: { run_id: runId },
    resume: { checkpoints: { [runId]: saved }, runs: {} } });
  assert.deepEqual(saved, before, "Recovery must not unlock or rewrite the settled checkpoint.");
  assert.deepEqual(state, { modelCalls: 0, networkCalls: 0 });
  return result;
}

test("settled host interruption preserves successful write/read receipts, operation ID and deduplicated refs", async () => {
  const calls = [
    { name: "example.write", toolCallId: "write-once", operation_id: `${RUN}/write-once`,
      state: "completed", result: { ok: true, action_status: "applied", object_refs: [REF] } },
    { name: "evidence.page.read", state: "completed", result: { ok: true, result: { source: "actual source" } } },
    { name: "example.rejected", state: "completed", isError: true, result: { ok: false } },
  ];
  const saved = checkpoint({ calls, refs: [{ revision: 1, id: REF.id, kind: REF.kind }, EXTRA,
    null, "bad", {}, { kind: "sample" }, { kind: "sample", id: "" }] });
  const result = await restore(saved);
  assert.equal(result.status, "partial");
  assert.equal(result.error_code, "interrupted_checkpoint");
  assert.deepEqual(result.toolCalls, calls);
  assert.equal(result.toolCalls[0].operation_id, `${RUN}/write-once`);
  assert.equal(result.object_refs.length, 2);
  assert.ok(result.object_refs.some((ref) => ref.id === REF.id && ref.revision === 1));
  assert.ok(result.object_refs.some((ref) => ref.id === EXTRA.id));
  assert.deepEqual(result.completion, { ...completion, recovered_interruption: true });
  assert.equal(result.recovery.original_status, "cancelled");
  assert.equal(result.recovery.original_code, "run_cancelled");
  assert.equal(result.recovery.reason, "host_interruption");
  assert.match(result.text, /宿主执行中断/);
});

test("settled host interruption without successful receipts fails even when saved refs exist", async () => {
  const result = await restore(checkpoint({ calls: [
    { name: "example.write", state: "completed", isError: true, result: { ok: false } },
  ], refs: [EXTRA] }));
  assert.equal(result.status, "failed");
  assert.deepEqual(result.object_refs, [EXTRA]);
  assert.equal(result.error_code, "interrupted_checkpoint");
});

test("a completed child write remains partial after the settled root host interruption", async () => {
  const children = [{ run_id: "preserved-child", status: "completed", toolCalls: [
    { name: "example.write", state: "completed", operation_id: "preserved-child/write-once",
      result: { action_status: "applied", object_refs: [REF] } },
  ] }];
  const result = await restore(checkpoint({ children, refs: [EXTRA] }));
  assert.equal(result.status, "partial");
  assert.deepEqual(result.delegations, children);
  assert.equal(result.object_refs.length, 2);
});

test("settled cancellation cannot be promoted by a saved completed declaration", async () => {
  const finalResponse = { status: "completed", text: "Earlier declaration." };
  const message = fauxAssistantMessage(fauxToolCall("agent.respond", finalResponse));
  const saved = checkpoint({ calls: [{ name: "agent.respond", toolCallId: message.content[0].id,
    state: "completed", result: { status: "response_recorded" } }] });
  saved.messages = [{ role: "user", content: "Complete this task." }, message];
  saved.final_response = finalResponse;
  saved.result.object_refs = { malformed: true };
  const result = await restore(saved);
  assert.equal(result.status, "failed");
  assert.equal(result.error_code, "interrupted_checkpoint");
  assert.deepEqual(result.object_refs, []);
});

test("non-durable legacy runs still ignore resume checkpoints and produce their ordinary answer", async () => {
  const provider = fauxProvider({ provider: "test", models: [{ id: "test", name: "test", reasoning: false,
    input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
  provider.setResponses([fauxAssistantMessage(fauxText("Ordinary legacy answer."))]);
  const runtime = createRuntime({ gatewayUrl: "http://core", token: "legacy-token",
    fetchImpl: async (url) => new Response(JSON.stringify(new URL(url).pathname.endsWith("/roles")
      ? { roles: [] } : { tools: [] })),
    modelFactory: () => ({ model: provider.getModel(),
      streamFn: (model, context, options) => provider.provider.streamSimple(model, context, options) }) });
  const result = await runtime({ durable: false, message: "Answer normally.", context: { run_id: RUN },
    resume: { checkpoints: { [RUN]: checkpoint() } } });
  assert.equal(result.status, "completed");
  assert.equal(result.text, "Ordinary legacy answer.");
  assert.equal(result.recovery, undefined);
  assert.equal(result.completion.recovered_interruption, undefined);
});

// Optional local reproduction: the ordinary regression suite remains portable.
// Read only the isolated evaluation checkpoint; no model, gateway or database writes.
const realDatabase = new URL("../../work/image-v3-glmmax900-2026-10-26/run/evaluation.db", import.meta.url);
test("actual interrupted P01 checkpoint recovers terminal receipts with zero model/network calls",
  { skip: !existsSync(realDatabase) }, async () => {
    const database = new DatabaseSync(realDatabase, { readOnly: true });
    let row;
    try {
      row = database.prepare("SELECT run_id,checkpoint_json FROM assistant_runtime_checkpoints WHERE run_id=?")
        .get("run_060905293619");
    } finally { database.close(); }
    assert.ok(row, "The isolated P01 root checkpoint must exist.");
    const result = await restore(JSON.parse(row.checkpoint_json), { runId: row.run_id });
    assert.equal(result.status, "partial");
    assert.equal(result.recovery.original_status, "cancelled");
    assert.equal(result.recovery.original_code, "run_cancelled");
    assert.equal(result.object_refs.length, 11);
    assert.equal(result.toolCalls.length, 6);
  });
