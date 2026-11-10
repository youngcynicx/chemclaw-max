import test from "node:test";
import assert from "node:assert/strict";
import { encodeCheckpoint, decodeCheckpoint, decodeResume, CHECKPOINT_BYTES,
  EXPANDED_CHECKPOINT_BYTES } from "./checkpoint-codec.mjs";
const identity = (value) => value;
const terminal = (code) => (error) => error.code === code && error.retryable === false;

test("large repeated receipts round-trip without dropping messages, fields or completion", () => {
  const receipt = { fields: { text: "汉字🙂\\\"".repeat(30000), unknown: null }, action_status: "applied" };
  const original = { version: 3, run_id: "one", messages: [receipt, { extra: receipt }, receipt],
    tool_calls: [{ result: receipt }], result: { toolCalls: [{ result: receipt }] }, settled: true };
  const packed = encodeCheckpoint(original, identity);
  assert.equal(packed.checkpoint_encoding, "json-references-v1");
  assert.ok(Buffer.byteLength(JSON.stringify(packed)) <= CHECKPOINT_BYTES);
  const restored = decodeCheckpoint(packed, "one");
  assert.deepEqual(restored, JSON.parse(JSON.stringify(original)));
  restored.messages[0].fields.unknown = "changed";
  assert.equal(restored.messages[2].fields.unknown, null, "restore copies JSON values, never mutable aliases");
});

test("reserved-looking business values, nulls and prototype keys remain literal data", () => {
  const business = JSON.parse('{"__proto__":{"polluted":true},"constructor":"literal","$ref":0,"checkpoint_encoding":"business","data":null}');
  const long = { value: "x".repeat(600000), business };
  const value = { version: 3, run_id: "scope", rows: [long, long, long, long] };
  const decoded = decodeCheckpoint(encodeCheckpoint(value, identity), "scope");
  assert.deepEqual(decoded, JSON.parse(JSON.stringify(value)));
  assert.equal({}.polluted, undefined);
  assert.equal(Object.hasOwn(decoded.rows[0].business, "__proto__"), true);
});

test("small legacy snapshots are unchanged; exact UTF8 storage boundary is respected", () => {
  for (const version of [1, 2, 3]) {
    const value = { version, run_id: "legacy", rows: [null, false, "汉🙂"] };
    assert.deepEqual(encodeCheckpoint(value, identity), value);
    assert.equal(decodeCheckpoint(value, "legacy"), value);
  }
  const base = { run_id: "one", value: "" };
  const overhead = Buffer.byteLength(JSON.stringify(base));
  base.value = "x".repeat(CHECKPOINT_BYTES - overhead);
  assert.equal(Buffer.byteLength(JSON.stringify(encodeCheckpoint(base, identity))), CHECKPOINT_BYTES);
  base.value += "x";
  assert.throws(() => encodeCheckpoint(base, identity), terminal("checkpoint_size_limit"));
});

test("cleaning precedes interning so secrets cannot hide inside references", () => {
  const part = { api_key: "private", text: "same".repeat(200000) };
  const packed = encodeCheckpoint({ run_id: "one", values: [part, part, part] }, (value) => {
    value.values.forEach((item) => delete item.api_key); return value;
  });
  assert.ok(!JSON.stringify(packed).includes("private"));
  assert.deepEqual(decodeCheckpoint(packed).values[0], { text: part.text });
});

test("corrupt references fail closed before mutating the supplied checkpoint", () => {
  const value = { checkpoint_encoding: "json-references-v1", run_id: "one", version: 3,
    data: { run_id: "one", version: 3, original: { v: "source" }, duplicate: null },
    refs: [[["duplicate"], ["original"]]] };
  const corruptions = [
    (x) => x.refs[0][1] = ["absent"],
    (x) => x.refs[0][1] = ["duplicate"],
    (x) => x.refs[0][1] = ["toString"],
    (x) => x.data.duplicate = "not a placeholder",
    (x) => x.run_id = "another-run",
    (x) => x.version = 2,
    (x) => x.refs.push(x.refs[0]),
    (x) => { x.data = { run_id: "one", version: 3, first: null, later: "value" }; x.refs = [[["first"], ["later"]]]; },
  ];
  for (const corrupt of corruptions) {
    const bad = structuredClone(value); corrupt(bad); const before = JSON.stringify(bad);
    assert.throws(() => decodeCheckpoint(bad, "one"), terminal("checkpoint_invalid"));
    assert.equal(JSON.stringify(bad), before);
  }
  assert.throws(() => decodeCheckpoint(value, "wrong-scope"), terminal("checkpoint_invalid"));
});

test("expanded depth, multiplicity and aggregate resume remain bounded", () => {
  const data = { run_id: "one", version: 3, original: "x".repeat(900000) };
  const refs = [];
  for (let i = 0; i < 10; i++) { data[`copy${i}`] = null; refs.push([[`copy${i}`], ["original"]]); }
  assert.throws(() => decodeCheckpoint({ checkpoint_encoding: "json-references-v1", run_id: "one", version: 3,
    data, refs }), terminal("checkpoint_size_limit"));
  const checkpoints = {};
  for (let i = 0; i < 5; i++) {
    const id = `run${i}`, part = { text: "abc".repeat(200000) };
    checkpoints[id] = encodeCheckpoint({ run_id: id, version: 3, values: [part, part, part] }, identity);
  }
  assert.throws(() => decodeResume({ checkpoints }), terminal("checkpoint_size_limit"));
  let nested = null;
  for (let i = 0; i < 130; i++) nested = { nested };
  assert.throws(() => decodeCheckpoint({ checkpoint_encoding: "json-references-v1", data: nested, refs: [] }), terminal("checkpoint_invalid"));
  assert.equal(EXPANDED_CHECKPOINT_BYTES, 8 * 1024 * 1024);
});

test("explicitly corrupt resume maps and depth amplified through references fail closed", () => {
  for (const checkpoints of [null, [], 42, "bad"]) {
    assert.throws(() => decodeResume({ checkpoints }), terminal("checkpoint_invalid"));
  }
  let source = "end";
  for (let i = 0; i < 126; i++) source = { next: source };
  const data = { run_id: "one", version: 3, source, nested: { nested: { copy: null } } };
  assert.throws(() => decodeCheckpoint({ checkpoint_encoding: "json-references-v1", run_id: "one", version: 3,
    data, refs: [[["nested", "nested", "copy"], ["source"]]] }), terminal("checkpoint_invalid"));
});
