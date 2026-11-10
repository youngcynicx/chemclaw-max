// Delivery evidence, not intent inference: only typed Core receipts are inspected.
const WRITES = new Set(["memory.proposal.create", "memory.proposal.apply", "memory.event.correct"]);
const READS = new Set(["memory.proposal.read", "memory.event.read", "query.samples.list", "query.sample.timeline", "query.events.search"]);
const INTENTS = new Set(["organize", "query", "none"]);
const strings = (values) => [...new Set(values.filter((value) => typeof value === "string" && value))];
const list = (value) => Array.isArray(value) ? value : [];
const validReceipt = (call) => call.state !== "pending" && !call.isError
  && call.result?.ok !== false && call.result?.action_status === "applied";

// Flatten execution receipts only. Specialist prose or its declared object_refs
// never substitutes for the actual typed tool calls retained by the host.
export function knowledgeReceiptCalls(toolCalls = [], delegations = []) {
  const calls = [], visited = new Set();
  const collect = (items, children) => {
    for (const call of items) {
      if (call.name !== "agent.delegate") calls.push(call);
      else {
        const child = children.find((item) => item.run_id === call.result?.run_id);
        if (child && !visited.has(child)) {
          visited.add(child);
          collect(list(child.toolCalls), list(child.delegations));
        }
      }
    }
    for (const child of children) if (!visited.has(child)) {
      visited.add(child);
      collect(list(child.toolCalls), list(child.delegations));
    }
  };
  collect(toolCalls, delegations);
  return calls;
}

export function knowledgeDelivery(toolCalls = [], delegations = [], context = {}, intent) {
  const sourceIds = strings([...list(context.pages), ...list(context.source_pages)]
    .map((page) => typeof page === "string" ? page : page?.page_id));
  const calls = knowledgeReceiptCalls(toolCalls, delegations);
  const writes = calls.filter((call) => WRITES.has(call.name));
  const eventWrites = new Map(), eventReads = new Map();
  const proposals = new Set(), samples = new Set(), readSamples = new Set(), pages = new Set();
  const pending = [], reusedEvents = new Set();
  let failedWrites = 0, skippedItems = 0;
  let readback = false;
  const add = (set, value) => { if (typeof value === "string" && value) set.add(value); };
  const rememberEvent = (target, id, revision) => {
    if (typeof id !== "string" || !id) return;
    const rev = Number.isInteger(revision) && revision > 0 ? revision : 0;
    target.set(id, Math.max(target.get(id) || 0, rev));
  };
  const sourceOf = (record) => {
    add(pages, record?.page_id);
    list(record?.evidence_refs).forEach((ref) => add(pages, ref.page_id));
  };
  const eventOf = (target, record, fallback = {}) => {
    if (!record || typeof record !== "object") return;
    const id = record.event_id || record.entity_id || fallback.event_id;
    rememberEvent(target, id, record.revision || fallback.revision);
    if (!id) return;
    const payload = record.field_json || record;
    sourceOf(payload);
    sourceOf(record);
    add(samples, payload.sample_id);
    list(payload.sample_ids).forEach((sid) => add(samples, sid));
    // A card can group repeated imports, but only explicit per-event revisions
    // prove a readback. Legacy related_event_ids alone have no revision evidence.
    for (const related of list(record.related_events)) {
      rememberEvent(target, related.event_id, related.revision);
    }
  };
  for (const call of calls) {
    const writing = WRITES.has(call.name), reading = READS.has(call.name);
    if (!writing && !reading) continue;
    if (!validReceipt(call)) {
      if (writing) {
        failedWrites++;
        pending.push({ tool: call.name, reason: call.state === "pending"
          ? "write_outcome_unconfirmed" : "write_not_applied" });
      }
      continue;
    }
    const receipt = call.result, data = receipt.result || {};
    const target = writing ? eventWrites : eventReads;
    if (reading) readback = true;
    if (writing) {
      list(receipt.unknowns).forEach((reason) => {
        if (typeof reason === "string") pending.push({ tool: call.name, reason });
      });
    }
    for (const ref of list(receipt.object_refs)) {
      if (ref.kind === "memory_proposal" || ref.kind === "proposal") add(proposals, ref.id);
      if (["experiment_event", "event"].includes(ref.kind)) rememberEvent(target, ref.id, ref.revision);
      if (ref.kind === "sample") { add(samples, ref.id); if (reading) add(readSamples, ref.id); }
    }
    const proposal = data.proposal;
    if (proposal && typeof proposal === "object") {
      add(proposals, proposal.proposal_id);
      // A saved proposal without adopted events is not a saved knowledge card.
      for (const item of list(proposal.items)) if (item.event_id) {
        eventOf(target, item.current_event, { event_id: item.event_id, revision: item.event_revision });
        rememberEvent(target, item.event_id, item.event_revision);
        sourceOf(item);
        if (reading || eventWrites.has(item.event_id)) add(pages, proposal.page_id);
      }
      for (const event of list(proposal.knowledge_events)) {
        rememberEvent(target, event.event_id, event.revision);
        add(pages, proposal.page_id);
      }
    }
    if (writing) {
      for (const event of list(data.knowledge?.adopted)) {
        eventOf(target, event);
        if (event.existing === true) add(reusedEvents, event.event_id);
        if (event.event_id) add(pages, proposal?.page_id || call.arguments?.page_id);
      }
      list(data.knowledge?.skipped).forEach((entry) => {
        skippedItems++;
        pending.push({ tool: call.name, item_key: entry.item_key, reason: entry.reason || "not_adopted" });
      });
      if (data.idempotent === true) {
        for (const event of list(proposal?.knowledge_events)) add(reusedEvents, event.event_id);
      }
      eventOf(target, data.event);
      // memory.event.correct returns an outcome with event_id/revision.
      if (call.name === "memory.event.correct" && data.event_id) eventOf(target, data);
    } else if (call.name === "query.samples.list") {
      for (const sample of list(data.samples)) {
        add(samples, sample.sample_id); add(readSamples, sample.sample_id);
        list(sample.records).forEach((record) => eventOf(target, record));
      }
      list(data.unassigned_records).forEach((record) => eventOf(target, record));
      list(data.source_notes).forEach((record) => eventOf(target, record));
    } else if (call.name === "query.sample.timeline") {
      add(samples, data.sample?.sample_id); add(readSamples, data.sample?.sample_id);
      list(data.timeline).forEach((record) => eventOf(target, record));
    } else if (call.name === "memory.event.read") {
      const current = list(data.revisions).at(-1) || data;
      eventOf(target, current);
    } else if (call.name === "query.events.search") {
      for (const candidate of list(data.candidates)) {
        // Transcript blocks and other search hits are not persisted knowledge.
        if (candidate.object_type !== "experiment_event") continue;
        eventOf(target, { ...candidate.payload?.fields,
          event_id: candidate.object_id, revision: candidate.revision,
          page_id: candidate.page_id || candidate.payload?.fields?.page_id,
          evidence_refs: candidate.payload?.evidence_refs });
      }
    }
  }
  const verifiedEvents = [...eventWrites].filter(([id, revision]) => revision > 0 && eventReads.has(id)
    && eventReads.get(id) >= revision).map(([id]) => id);
  const state = eventWrites.size ? verifiedEvents.length === eventWrites.size ? "verified" : "saved_unverified"
    : eventReads.size || readSamples.size ? "read_only" : "not_written";
  let notice;
  if (state === "verified") notice = `知识写入回执包含${eventWrites.size}条事件，均已读回核对；样品数量以实际读回为准。`;
  else if (state === "saved_unverified") notice = `知识写入回执包含${eventWrites.size}条事件，其中${verifiedEvents.length}条已读回核对；其余尚未完成读回验收。`;
  else if (state === "read_only") notice = "本轮读取了已有知识，没有新增知识写入回执。";
  else notice = "本轮没有知识写入或已有知识的读回回执；聊天答复不能证明样品已归卡。";
  if (failedWrites || skippedItems) notice += `本轮另有${failedWrites}次写入未确认或未应用、${skippedItems}项未采纳的回执；后续是否补齐需结合具体对象核对。`;
  const unrecordedPages = sourceIds.filter((id) => !pages.has(id));
  if (unrecordedPages.length) notice += `${unrecordedPages.length}页尚无知识事件关联回执；页面关联也不代表整页事实已完整整理。`;
  if (reusedEvents.size) notice += `其中${reusedEvents.size}条事件为已存在知识的复用回执。`;
  return {
    version: 1, intent: INTENTS.has(intent) ? intent : "unspecified", state,
    source_page_ids: sourceIds, recorded_page_ids: [...pages],
    unrecorded_page_ids: unrecordedPages,
    proposal_ids: [...proposals], event_ids: [...eventWrites.keys()], sample_ids: [...samples],
    verified_event_ids: verifiedEvents, verified_sample_ids: [...readSamples],
    event_revisions: Object.fromEntries(eventWrites), read_event_revisions: Object.fromEntries(eventReads),
    reused_event_ids: [...reusedEvents], failed_write_attempts: failedWrites, skipped_item_count: skippedItems,
    write_attempts: writes.length, readback_performed: readback, pending, notice,
  };
}

// A deterministic execution receipt for interrupted/soft-budget runs, not a
// replacement scientific answer or a declaration that an organize goal passed.
export function receiptSummary(delivery = {}, { reason = "运行已停止", progress, maxRefs = 8 } = {}) {
  const take = (values) => strings(list(values)).slice(0, Number.isInteger(maxRefs)
    ? Math.max(0, Math.min(maxRefs, 20)) : 8);
  const show = (label, values) => {
    const all = strings(list(values)), shown = take(all);
    return all.length ? `${label}：${shown.join("、")}${shown.length < all.length ? `等${all.length}项` : ""}。` : "";
  };
  const written = strings(list(delivery.event_ids)), checked = strings(list(delivery.verified_event_ids));
  const pages = strings(list(delivery.source_page_ids));
  const lines = [`${typeof reason === "string" && reason.trim() ? reason.trim() : "运行已停止"}；以下为已保存的真实执行回执。`,
    `本次来源${pages.length}页；知识写入回执${written.length}条，其中${checked.length}条有相应版本读回。`];
  if (delivery.state === "read_only") lines.push("本轮只读了已有知识，没有新增知识写入回执。");
  if (!written.length && delivery.state !== "read_only") lines.push("本轮尚无知识写入回执；原件或转录已保存不等于知识已归卡。");
  for (const entry of [show("提案", delivery.proposal_ids), show("写入事件", written),
    show("已读回事件", checked), show("已读回样品", delivery.verified_sample_ids),
    show("尚无事件关联回执的来源", delivery.unrecorded_page_ids)]) if (entry) lines.push(entry);
  const pending = list(delivery.pending).length;
  if (pending || delivery.failed_write_attempts || delivery.skipped_item_count) {
    lines.push(`回执另保留${pending}项未知或待处理说明、${delivery.failed_write_attempts || 0}次写入未确认或未应用、${delivery.skipped_item_count || 0}项未采纳。`);
  }
  if (progress?.requires_review) lines.push("存在效果未知的写入；继续前需只读核查，不能重放该写入。");
  const incompleteSources = list(progress?.pages).filter((page) => page?.source_inventory_complete !== true).length;
  if (incompleteSources) lines.push(`${incompleteSources}页尚无完整来源块清单；继续前需读取原页，不能将空清单视为整理完成。`);
  const unlinked = list(progress?.pages).reduce((count, page) => count + list(page?.unlinked_block_ids).length, 0);
  if (unlinked) lines.push(`${unlinked}个当前来源块尚无已读回事件引用，可作为继续核查的优先入口。`);
  if (progress?.truncated) lines.push("进度索引因体积限制省略了部分来源或对象；完整回执仍保留，继续时须重新读取，省略不代表已完成。");
  lines.push("事件或块已有引用不证明整页事实完整；未完成范围保留，后续可沿现有来源和对象继续核查。");
  return lines.join("\n");
}
