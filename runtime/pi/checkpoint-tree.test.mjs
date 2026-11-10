import test from "node:test";
import assert from "node:assert/strict";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";
import { createRuntime } from "./runtime.mjs";

const ROOT_RUN = "checkpoint-tree-root";
const CHILD_RUN = "checkpoint-tree-child";
const WRITTEN_REF = { kind: "event", id: "root-written-event" };
const response = (value, status = 200) => new Response(JSON.stringify(value), { status });
const call = (name, args = {}) => fauxAssistantMessage(fauxToolCall(name, args));
const delegate = () => call("agent.delegate", { agent_id: "b_capture", message: "Inspect the child source." });
const finish = () => call("agent.respond", { status: "completed", text: "This must never override checkpoint failure." });
const payload = (checkpoint) => ({ session_id: "checkpoint-tree-session", message: "Process this task once.",
  durable: true, context: { run_id: ROOT_RUN },
  ...(checkpoint !== undefined ? { resume: { checkpoints: { [ROOT_RUN]: checkpoint } } } : {}) });

const specs = [
  { type: "function", function: { name: "example.write", mutates: true, description: "write",
    parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "evidence.page.read", mutates: false, description: "read",
    parameters: { type: "object", properties: { page_id: { type: "string" } }, required: ["page_id"] } } },
];

function fixture({ rejectChildCheckpoint = false, childCheckpoint } = {}) {
  const state = { fetches: 0, writes: 0, reads: 0, delegateRequests: 0,
    rejectedChildCheckpoints: 0, providerRequests: [], checkpoints: [], writeOperationIds: [] };
  state.fetch = async (url, options) => {
    state.fetches += 1;
    const path = new URL(url).pathname;
    if (path === "/v2/agent/tools") return response({ tools: specs });
    if (path === "/v2/agent/roles") return response({ roles: [{ agent_id: "b_capture" }] });
    if (path === "/v2/agent/checkpoint") {
      const body = JSON.parse(options.body);
      if (rejectChildCheckpoint && body.checkpoint.run_id === CHILD_RUN) {
        state.rejectedChildCheckpoints += 1;
        return response({ error: { code: "schema_violation", message: "Permanent child checkpoint refusal." } }, 422);
      }
      state.checkpoints.push(structuredClone(body.checkpoint));
      return response({ ok: true });
    }
    if (path === "/v2/agent/delegate") {
      state.delegateRequests += 1;
      return response({ agent_id: "b_capture", task_id: "checkpoint-child-task", run_id: CHILD_RUN,
        run_token: "fixture-child-token", manifest: {},
        ...(childCheckpoint !== undefined ? { checkpoint: childCheckpoint } : {}) });
    }
    if (path.endsWith("example.write")) {
      state.writes += 1;
      state.writeOperationIds.push(options.headers["X-Operation-Id"]);
      return response({ action_status: "applied", object_refs: [WRITTEN_REF] });
    }
    if (path.endsWith("evidence.page.read")) {
      state.reads += 1;
      return response({ action_status: "applied", result: { source: "Actually read." } });
    }
    throw new Error(`Unexpected gateway request ${path}`);
  };
  return state;
}

function runtime(state, responses) {
  return createRuntime({ gatewayUrl: "http://core", token: "fixture-root-token", role: "orchestrator",
    fetchImpl: state.fetch, modelFactory: ({ role }) => {
      const faux = fauxProvider({ provider: "test", models: [{ id: "test", name: "test", reasoning: false,
        input: ["text"], contextWindow: 128000, maxTokens: 8192 }] });
      faux.setResponses(responses);
      return { model: faux.getModel(), streamFn: (model, context, options) => {
        state.providerRequests.push(role);
        return faux.provider.streamSimple(model, context, options);
      } };
    } });
}

async function terminalFailure(promise) {
  let failure;
  await assert.rejects(promise, (error) => {
    failure = error;
    return error.code === "checkpoint_invalid" && error.retryable === false;
  });
  assert.equal(failure.result.runtime_error.retryable, false);
  assert.equal(failure.result.error_code, "checkpoint_invalid");
  assert.notEqual(failure.result.status, "completed");
  assert.ok(Buffer.byteLength(JSON.stringify(failure.result), "utf8") <= 64 * 1024);
  assert.ok(!JSON.stringify(failure.result).includes("fixture-root-token"));
  assert.ok(!JSON.stringify(failure.result).includes("fixture-child-token"));
  return failure;
}

test("child checkpoint refusal retains root successful write in the terminal tree receipt", async () => {
  const state = fixture({ rejectChildCheckpoint: true });
  const write = call("example.write"), writeId = write.content[0].id;
  const failure = await terminalFailure(runtime(state, [write, delegate(), finish()])(payload()));
  assert.equal(failure.result.status, "partial");
  assert.ok(failure.result.object_refs.some((ref) => ref.kind === WRITTEN_REF.kind && ref.id === WRITTEN_REF.id));
  assert.ok(failure.result.toolCalls.some((entry) => entry.name === "example.write"
    && entry.state === "completed" && entry.operation_id === `${ROOT_RUN}/${writeId}`));
  assert.equal(state.writes, 1);
  assert.deepEqual(state.writeOperationIds, [`${ROOT_RUN}/${writeId}`]);
  assert.equal(state.delegateRequests, 1);
  assert.equal(state.rejectedChildCheckpoints, 1);
  assert.deepEqual(state.providerRequests, ["orchestrator", "orchestrator"],
    "The whole tree must stop before a child generation or parent completion.");
});

for (const [label, checkpoint] of [
  ["unsupported encoding", { checkpoint_encoding: "invalid-format", run_id: CHILD_RUN, data: {} }],
  ["wrong child identity", { version: 3, run_id: "another-child", messages: [], tool_calls: [] }],
]) test(`corrupt dynamic child checkpoint (${label}) cannot become an ordinary recoverable tool error`, async () => {
  const state = fixture({ childCheckpoint: checkpoint });
  const failure = await terminalFailure(runtime(state, [
    call("evidence.page.read", { page_id: "actual-source" }), delegate(), finish(),
  ])(payload()));
  assert.equal(failure.result.status, "partial", "The real successful root read is retained.");
  assert.equal(state.reads, 1);
  assert.equal(state.writes, 0);
  assert.equal(state.delegateRequests, 1);
  assert.deepEqual(state.providerRequests, ["orchestrator", "orchestrator"],
    "No child generation or later parent respond may run after decoding fails.");
});

for (const [label, checkpoint] of [
  ["null", null], ["number", 42], ["string", "invalid checkpoint"], ["boolean", false], ["array", []],
]) test(`explicit ${label} resume checkpoint is rejected before any model or gateway request`, async () => {
  const state = fixture();
  const failure = await terminalFailure(runtime(state, [call("example.write"), finish()])(payload(checkpoint)));
  assert.equal(failure.result.status, "failed");
  assert.equal(state.fetches, 0);
  assert.equal(state.writes, 0);
  assert.equal(state.reads, 0);
  assert.deepEqual(state.providerRequests, []);
});
