// Lossless storage encoding only. Receipt contents remain visible to Core's
// secret validator; neither model context nor business facts are summarized.
import { createHash } from "node:crypto";

export const CHECKPOINT_BYTES = 1_800_000;
export const EXPANDED_CHECKPOINT_BYTES = 8 * 1024 * 1024;
const FORMAT = "json-references-v1";
const bytes = (value) => Buffer.byteLength(JSON.stringify(value), "utf8");
export function checkpointError(code) {
  const error = new Error(code === "checkpoint_size_limit"
    ? "Durable checkpoint exceeds the bounded storage budget."
    : "Durable checkpoint encoding is invalid.");
  error.code = code;
  error.retryable = false;
  return error;
}

export function encodeCheckpoint(value, clean) {
  let raw;
  try { raw = clean(JSON.parse(JSON.stringify(value))); }
  catch { throw checkpointError("checkpoint_invalid"); }
  if (bytes(raw) <= CHECKPOINT_BYTES) return raw;
  if (bytes(raw) > EXPANDED_CHECKPOINT_BYTES) throw checkpointError("checkpoint_size_limit");
  const seen = new Map(), refs = [];
  const visit = (node, path) => {
    if (path.length > 128) throw checkpointError("checkpoint_invalid");
    if (node && typeof node === "object" || typeof node === "string") {
      const text = JSON.stringify(node);
      if (Buffer.byteLength(text, "utf8") >= 1024) {
        const hash = createHash("sha256").update(text).digest("hex");
        const matches = seen.get(hash) || [];
        const prior = matches.find((candidate) => JSON.stringify(candidate.node) === text);
        // Test before walking children: a replaced parent must never leave
        // references to discarded child paths.
        if (prior) { refs.push([path, prior.path]); return null; }
        matches.push({ node, path }); seen.set(hash, matches);
      }
    }
    if (Array.isArray(node)) return node.map((item, index) => visit(item, [...path, index]));
    if (node && typeof node === "object") return Object.fromEntries(Object.entries(node)
      .map(([key, item]) => [key, visit(item, [...path, key])]));
    return node;
  };
  const packed = { checkpoint_encoding: FORMAT, run_id: raw.run_id, version: raw.version,
    data: visit(raw, []), refs };
  if (bytes(packed) > CHECKPOINT_BYTES) throw checkpointError("checkpoint_size_limit");
  return packed;
}

export function decodeCheckpoint(value, expectedRunId) {
  if (value === undefined) return value;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw checkpointError("checkpoint_invalid");
  if (!value || !Object.hasOwn(value, "checkpoint_encoding")) {
    if (expectedRunId && value?.run_id !== undefined && value.run_id !== expectedRunId) throw checkpointError("checkpoint_invalid");
    return value;
  }
  if (value.checkpoint_encoding !== FORMAT) throw checkpointError("checkpoint_invalid");
  try {
    if (bytes(value) > CHECKPOINT_BYTES || !Array.isArray(value.refs) || value.refs.length > 8192
      || !value.data || typeof value.data !== "object") throw checkpointError("checkpoint_invalid");
    const data = JSON.parse(JSON.stringify(value.data));
    const order = new Map(); let count = 0;
    const index = (node, path) => {
      if (path.length > 128 || ++count > 100000) throw checkpointError("checkpoint_invalid");
      order.set(JSON.stringify(path), count);
      if (Array.isArray(node)) node.forEach((item, i) => index(item, [...path, i]));
      else if (node && typeof node === "object") Object.entries(node).forEach(([k, v]) => index(v, [...path, k]));
    };
    index(data, []);
    const validPath = (path) => Array.isArray(path) && path.length > 0 && path.length <= 128
      && path.every((key) => typeof key === "string" || Number.isSafeInteger(key) && key >= 0);
    const get = (path) => path.reduce((node, key) => {
      if (!node || typeof node !== "object" || !Object.hasOwn(node, key)) throw checkpointError("checkpoint_invalid");
      return node[key];
    }, data);
    const prefix = (a, b) => a.length <= b.length && a.every((key, i) => key === b[i]);
    let previous = 0;
    // Validate all locations against the untouched packed tree. Strict DFS
    // order ensures a source is materialized before any later copy reads it.
    for (const ref of value.refs) {
      if (!Array.isArray(ref) || ref.length !== 2 || !ref.every(validPath)) throw checkpointError("checkpoint_invalid");
      const [target, source] = ref, to = order.get(JSON.stringify(target)), from = order.get(JSON.stringify(source));
      if (!to || !from || to <= previous || from >= to || prefix(source, target) || prefix(target, source)
        || get(target) !== null || get(source) === null) throw checkpointError("checkpoint_invalid");
      previous = to;
    }
    let expanded = bytes(data);
    for (const [target, source] of value.refs) {
      const original = get(source);
      expanded += bytes(original) - 4; // destination is the literal null
      if (expanded > EXPANDED_CHECKPOINT_BYTES) throw checkpointError("checkpoint_size_limit");
      const parent = get(target.slice(0, -1));
      Object.defineProperty(parent, target.at(-1), { value: structuredClone(original), enumerable: true,
        writable: true, configurable: true });
    }
    count = 0;
    index(data, []); // Expanded copies must obey the same structure bounds.
    if (bytes(data) > EXPANDED_CHECKPOINT_BYTES || data.run_id !== value.run_id || data.version !== value.version
      || expectedRunId && data.run_id !== expectedRunId) throw checkpointError("checkpoint_invalid");
    return data;
  } catch (error) {
    if (error?.code === "checkpoint_size_limit") throw error;
    throw checkpointError("checkpoint_invalid");
  }
}

export function decodeResume(resume, payload = {}) {
  if (!resume) return resume;
  if (typeof resume !== "object" || Array.isArray(resume)
    || Object.hasOwn(resume, "checkpoints") && (!resume.checkpoints
      || typeof resume.checkpoints !== "object" || Array.isArray(resume.checkpoints))) {
    throw checkpointError("checkpoint_invalid");
  }
  const checkpoints = {};
  let expanded = bytes({ ...payload, resume: { ...resume, checkpoints } });
  for (const [id, packed] of Object.entries(resume.checkpoints || {})) {
    if (packed === undefined) throw checkpointError("checkpoint_invalid");
    const value = decodeCheckpoint(packed, id);
    expanded += bytes(value) + Buffer.byteLength(JSON.stringify(id), "utf8") + 2;
    if (expanded > EXPANDED_CHECKPOINT_BYTES) throw checkpointError("checkpoint_size_limit");
    Object.defineProperty(checkpoints, id, { value, enumerable: true, configurable: true, writable: true });
  }
  return { ...resume, checkpoints };
}
