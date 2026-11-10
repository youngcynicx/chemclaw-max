// Structural resume hints from typed Core receipts. A cited block is not a
// semantic-completeness claim; chemistry and the remaining facts stay with LLM.
import { knowledgeReceiptCalls } from "./knowledge-delivery.mjs";

const WRITES = new Set(["memory.proposal.create", "memory.proposal.apply", "memory.event.correct"]);
const READS = new Set(["memory.proposal.read", "memory.event.read", "query.samples.list", "query.sample.timeline", "query.events.search"]);
const list = (value) => Array.isArray(value) ? value : [];
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const id = (value) => typeof value === "string" && value.length > 0 && value.length <= 512
  && !/[\u0000-\u001f]/.test(value) ? value : null;
const revision = (value) => Number.isSafeInteger(value) && value > 0 ? value : null;
const unique = (values) => [...new Set(values.filter((value) => id(value)))];
const applied = (call) => call.state !== "pending" && !call.isError
  && call.result?.ok !== false && call.result?.action_status === "applied";
const eventRef = (value) => ({ event_id: value.event_id, revision: value.revision });

export const CAPTURE_PROGRESS_MAX_BYTES = 32768;
const counts = (value) => ({
  target_pages: list(value.target_page_ids).length, pages: list(value.pages).length,
  source_blocks: list(value.pages).reduce((n, page) => n + list(page.source_block_ids).length, 0),
  linked_blocks: list(value.pages).reduce((n, page) => n + list(page.linked_block_ids).length, 0),
  saved_events: list(value.saved_event_refs).length, verified_events: list(value.verified_event_refs).length,
  previous_events: list(value.previous_event_refs).length, unresolved_writes: list(value.unresolved_writes).length,
});

// This is a bounded index of the journal, not a lossy replacement for it.
// Omission is explicit and always weakens, never promotes, verification.
export function boundCaptureProgress(value, maxBytes = CAPTURE_PROGRESS_MAX_BYTES) {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 4096 || maxBytes > 262144) throw new Error("Invalid capture progress size budget");
  const size = (item) => Buffer.byteLength(JSON.stringify(item), "utf8");
  if (size(value) <= maxBytes) return value;
  const result = structuredClone(value), original = counts(value);
  result.truncated = true;
  result.knowledge_complete = null;
  result.total_counts = original;
  result.unresolved_write_count = original.unresolved_writes;
  result.requires_review = value.requires_review === true || original.unresolved_writes > 0;
  result.target_page_ids_complete = true;
  result.truncation_notice = "控制进度索引已裁剪；完整回执仍在原journal/checkpoint。继续时重新读取当前来源与对象，不把省略或已有引用当作完成。";
  const updateCounts = () => {
    const kept = counts(result);
    result.omitted_counts = Object.fromEntries(Object.entries(original)
      .map(([key, total]) => [key, Math.max(0, total - kept[key])]));
  };
  for (const page of result.pages) {
    page.saved_event_refs = [];
    page.verified_event_refs = [];
    page.stale_reference_event_ids = [];
    page.event_ref_inventory_omitted = true;
    page.source_inventory_complete = false;
    // An index without its supporting event refs cannot establish which of
    // the original linked blocks remains backed by retained references.
    page.linked_block_ids = [];
    page.unlinked_block_ids = [...page.source_block_ids];
  }
  updateCounts();
  if (size(result) > maxBytes) {
    for (const page of result.pages) {
      page.source_block_count = page.source_block_ids.length;
      page.block_inventory_omitted = true;
      page.source_block_ids = []; page.linked_block_ids = []; page.unlinked_block_ids = [];
    }
    updateCounts();
  }
  // Logarithmic batches bound expensive reserialization for very large inputs.
  const arrays = ["previous_event_refs", "saved_event_refs", "verified_event_refs", "unresolved_writes"];
  while (size(result) > maxBytes && arrays.some(key => result[key].length)) {
    for (const key of arrays) result[key] = result[key].slice(0, Math.floor(result[key].length / 2));
    updateCounts();
  }
  if (size(result) > maxBytes) {
    const targets = new Set(result.target_page_ids);
    result.pages = result.pages.filter(page => targets.has(page.page_id));
    updateCounts();
  }
  while (size(result) > maxBytes && (result.pages.length || result.target_page_ids.length)) {
    result.pages = result.pages.slice(0, Math.floor(result.pages.length / 2));
    result.target_page_ids = result.target_page_ids.slice(0, Math.floor(result.target_page_ids.length / 2));
    result.target_page_ids_complete = false;
    updateCounts();
  }
  if (size(result) > maxBytes) throw new Error("Capture progress header exceeds its size budget");
  return result;
}

export function captureProgress(toolCalls = [], delegations = [], context = {}, { previous } = {}) {
  const calls = knowledgeReceiptCalls(toolCalls, delegations);
  const sources = new Map(), writes = new Map(), reads = new Map();
  const ensureSource = (pageId) => {
    if (!id(pageId)) return;
    if (!sources.has(pageId)) sources.set(pageId, { page_id: pageId, transcript_revision: null,
      blocks: new Set(), complete: false });
    return sources.get(pageId);
  };
  const source = (value, full = false) => {
    if (typeof value === "string") { ensureSource(value); return; }
    const page = object(value), transcript = object(page.transcript);
    const pageId = id(page.page_id || transcript.page_id);
    if (!pageId) return;
    const state = ensureSource(pageId);
    const rev = revision(transcript.transcript_revision ?? page.transcript_revision
      ?? transcript.revision ?? page.active_revision ?? transcript.active_revision);
    const active = revision(page.active_revision ?? transcript.active_revision);
    if (!rev || transcript.is_current === false || (active && active !== rev)) return;
    if (state.transcript_revision && rev < state.transcript_revision) return;
    const blocks = transcript.blocks ?? page.blocks;
    const blockIds = unique(list(blocks).map((block) => block?.block_id));
    const complete = full && Array.isArray(blocks) && blockIds.length === blocks.length;
    if (state.transcript_revision !== rev) {
      state.transcript_revision = rev;
      state.blocks = new Set(blockIds);
      state.complete = complete;
    } else if (complete) {
      state.blocks = new Set(blockIds);
      state.complete = true;
    } else blockIds.forEach((blockId) => state.blocks.add(blockId));
  };
  list(context.pages).forEach((page) => source(page, Array.isArray(page?.blocks) || Array.isArray(page?.transcript?.blocks)));
  list(context.source_pages).forEach((page) => source(page, true));
  list(context.source_omitted_pages).forEach((page) => source(page));
  for (const call of calls) if (call.name === "evidence.page.read" && applied(call)) {
    const data = object(call.result?.result);
    source(data.page || data, call.arguments?.block_ids === undefined);
  }
  const targetPageIds = unique(list(context.pages).map((page) => typeof page === "string" ? page : page?.page_id));

  const fact = (target, record, fallback = {}) => {
    record = object(record);
    const eventId = id(record.event_id || record.entity_id || fallback.event_id);
    const rev = revision(record.revision ?? fallback.revision);
    if (!eventId || !rev) return;
    const fields = object(record.field_json);
    const refs = [...list(record.evidence_refs), ...list(fields.evidence_refs), ...list(fallback.evidence_refs)]
      .map((ref) => ({ page_id: id(ref?.page_id), transcript_revision: revision(ref?.transcript_revision), block_id: id(ref?.block_id) }))
      .filter((ref) => ref.page_id && ref.transcript_revision && ref.block_id);
    const pageIds = unique([record.page_id, fields.page_id, fallback.page_id, ...refs.map((ref) => ref.page_id)]);
    const old = target.get(eventId);
    if (old && rev < old.revision) return;
    const value = old?.revision === rev ? old : { event_id: eventId, revision: rev, page_ids: [], refs: [] };
    value.page_ids = unique([...value.page_ids, ...pageIds]);
    value.refs = [...new Map([...value.refs, ...refs].map((ref) => [JSON.stringify(ref), ref])).values()];
    target.set(eventId, value);
  };
  const recordFact = (target, record, fallback = {}) => {
    fact(target, record, fallback);
    // Grouped cards may prove related revisions, but only their own explicit
    // source references can project block coverage for a related event.
    for (const related of list(record?.related_events)) fact(target, related);
  };
  const unresolved = [];
  for (const call of calls) {
    const writing = WRITES.has(call.name), reading = READS.has(call.name);
    if (!writing && !reading) continue;
    if (writing && (call.state === "pending" || call.result?.action_status === "unknown"
      || ["tool_outcome_unknown", "mutating_effect_unknown"].includes(call.result?.error?.code))) {
      unresolved.push({ tool: call.name, operation_id: id(call.operation_id), tool_call_id: id(call.toolCallId),
        reason: "write_outcome_unknown" });
    }
    if (!applied(call)) continue;
    // Historical reads are useful data, but cannot establish the current
    // objects that a new organize task should reuse without rechecking.
    if (reading && (call.arguments?.knowledge_as_of !== undefined
      || (call.name === "memory.event.read" && call.arguments?.revision !== undefined))) continue;
    const target = writing ? writes : reads, data = object(call.result?.result);
    for (const ref of list(call.result?.object_refs)) if (["experiment_event", "event"].includes(ref.kind)) {
      fact(target, { event_id: ref.id, revision: ref.revision });
    }
    const proposal = object(data.proposal);
    for (const item of list(proposal.items)) if (id(item?.event_id)) {
      const current = object(item.current_event);
      const rev = revision(current.revision ?? item.event_revision);
      const canReuseItemRefs = !current.revision || current.revision === item.event_revision;
      recordFact(target, current, { event_id: item.event_id, revision: rev, page_id: proposal.page_id,
        evidence_refs: canReuseItemRefs ? item.evidence_refs : [] });
    }
    for (const entry of list(proposal.knowledge_events)) fact(target, entry, { page_id: proposal.page_id });
    if (writing) {
      list(data.knowledge?.adopted).forEach((entry) => recordFact(target, entry, { page_id: proposal.page_id }));
      recordFact(target, data.event);
      if (call.name === "memory.event.correct") recordFact(target, data);
    } else if (call.name === "memory.proposal.read") {
      // Proposal item.current_event above is the authoritative readback.
    } else if (call.name === "memory.event.read") {
      recordFact(target, list(data.revisions).at(-1) || data);
    } else if (call.name === "query.samples.list") {
      for (const sample of list(data.samples)) list(sample.records).forEach((entry) => recordFact(target, entry));
      list(data.unassigned_records).forEach((entry) => recordFact(target, entry));
      list(data.source_notes).forEach((entry) => recordFact(target, entry));
    } else if (call.name === "query.sample.timeline") {
      list(data.timeline).forEach((entry) => recordFact(target, entry));
    } else if (call.name === "query.events.search") {
      for (const entry of list(data.candidates)) if (entry.object_type === "experiment_event") {
        recordFact(target, { event_id: entry.object_id, revision: entry.revision,
          page_id: entry.page_id, field_json: entry.payload?.fields, evidence_refs: entry.payload?.evidence_refs });
      }
    }
  }
  const saved = [...writes.values()];
  const verified = [...reads.values()].filter((entry) => !writes.has(entry.event_id)
    || entry.revision >= writes.get(entry.event_id).revision);
  const previousRows = list(previous?.pages);
  const previousEvents = new Map();
  for (const entry of [...list(previous?.saved_event_refs), ...list(previous?.verified_event_refs),
    ...previousRows.flatMap((page) => [...list(page.saved_event_refs), ...list(page.verified_event_refs)])]) {
    if (id(entry?.event_id) && revision(entry?.revision)) previousEvents.set(entry.event_id,
      { event_id: entry.event_id, revision: Math.max(entry.revision, previousEvents.get(entry.event_id)?.revision || 0) });
  }
  const pages = [...sources.values()].map((state) => {
    const old = previousRows.find((page) => page?.page_id === state.page_id);
    const matching = verified.filter((entry) => entry.page_ids.includes(state.page_id));
    const linked = new Set();
    const stale = new Set();
    for (const entry of matching) for (const ref of entry.refs) if (ref.page_id === state.page_id) {
      if (ref.transcript_revision !== state.transcript_revision) stale.add(entry.event_id);
      else if (state.blocks.has(ref.block_id)) linked.add(ref.block_id);
    }
    return { page_id: state.page_id, transcript_revision: state.transcript_revision,
      source_block_ids: [...state.blocks], source_inventory_complete: state.complete,
      saved_event_refs: saved.filter((entry) => entry.page_ids.includes(state.page_id)).map(eventRef),
      verified_event_refs: matching.map(eventRef), linked_block_ids: [...linked],
      unlinked_block_ids: [...state.blocks].filter((blockId) => !linked.has(blockId)),
      stale_reference_event_ids: [...stale],
      previous_transcript_revision: revision(old?.transcript_revision),
      source_changed: Boolean(revision(old?.transcript_revision) && old.transcript_revision !== state.transcript_revision),
      knowledge_complete: null };
  });
  return boundCaptureProgress({ version: 1, target_page_ids: targetPageIds.length ? targetPageIds : [...sources.keys()],
    pages, saved_event_refs: saved.map(eventRef), verified_event_refs: verified.map(eventRef),
    previous_event_refs: [...previousEvents.values()], unresolved_writes: unresolved,
    requires_review: unresolved.length > 0, knowledge_complete: null,
    scope: "事件读回与当前来源版本/块的引用覆盖，仅作续作线索；不是语义完整性判定。旧游标只供定位对象，需重新读回；已关联块仍可能有未整理事实。" });
}
