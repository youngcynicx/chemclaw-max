// Pi owns planning and tool-result iteration. Core owns authorization and writes.
import { readFile } from "node:fs/promises";
import { Agent } from "@earendil-works/pi-agent-core";
import { fauxAssistantMessage, fauxText } from "@earendil-works/pi-ai";
import { realModel, mockModel } from "./models.mjs";
import { knowledgeDelivery, receiptSummary } from "./knowledge-delivery.mjs";
import { captureProgress } from "./capture-progress.mjs";
import { closingReserve, runtimeBudgetHint } from "./runtime-budget.mjs";
import { decodeCheckpoint, decodeResume } from "./checkpoint-codec.mjs";
import { continuationState, continuationAllowed, continuationPrompt, sameCompletedWrite, successfulReceipt } from "./task-continuation.mjs";
import { summarizeProviderRequests } from "./provider-observability.mjs";
import { withContract, boundedSnapshot, toolResultMessage, COMPLETION_PROTOCOL_VERSION,
  currentTurnStart, evaluateCompletion } from "./recovery.mjs";

const promptRoot = new URL("./prompts/", import.meta.url);
const HUMAN_TOOLS = new Set([
  "inventory.event.resolve", "inventory.approval.grant", "memory.proposal.confirm_items",
  "memory.proposal.item.update", "evidence.transcript.correct", "memory.clarification.answer",
  "task.interaction.answer", "resource.booking.confirm", "research.milestone.decide",
  "assistant.agent.start", "assistant.agent.finish",
]);

export async function loadPrompt(role) {
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(role)) throw new Error("invalid agent role");
  // Unknown configured roles must supply their own prompt; never silently become observers.
  const [common, specialized] = await Promise.all([
    readFile(new URL("common.md", promptRoot), "utf8"),
    readFile(new URL(`${role}.md`, promptRoot), "utf8"),
  ]);
  return `${common}\n\n${specialized}`;
}

export function redact(value, secrets = []) {
  if (typeof value === "string") {
    return secrets.filter(Boolean).reduce((text, secret) => text.split(secret).join("[redacted]"), value);
  }
  if (Array.isArray(value)) return value.map((item) => redact(item, secrets));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !/^(run_token|agent_token|api_key|authorization)$/i.test(key))
    .map(([key, item]) => [key, redact(item, secrets)]));
  return value;
}

// Parent planning needs an actionable receipt, not a recursively duplicated
// transcript. Full child diagnostics stay in the host's public delegations list.
export function delegationReceipt(result) {
  const fullText = String(result.text || "");
  return {
    agent_id: result.agent_id, run_id: result.run_id, task_id: result.task_id,
    text: fullText.length > 8000 ? `${fullText.slice(0, 8000)}\n（完整结果见子任务详情）` : fullText,
    status: result.status, object_refs: result.object_refs || [],
    ...(result.error ? { error: result.error } : {}),
    ...(result.error_code ? { error_code: result.error_code } : {}),
    ...(result.completion ? { completion: result.completion } : {}),
    ...(result.knowledge_delivery ? { knowledge_delivery: result.knowledge_delivery } : {}),
    ...(result.provider_usage ? { provider_usage: result.provider_usage } : {}),
    ...(result.needs_provider_quota ? { needs_provider_quota: true, provider_resource_failure: result.provider_resource_failure } : {}),
  };
}

// Derive review/error state from receipts, never from an assistant's success claim.
export function summarizeReceipts(toolCalls, delegations, error, declaredStatus) {
  const values = [];
  const refs = [];
  const executions = [];
  const collectExecutions = (receipt) => {
    if (!receipt || typeof receipt !== "object") return;
    executions.push(receipt);
    (receipt.toolCalls || []).forEach((call) => collectExecutions(call.result));
    (receipt.delegations || []).forEach(collectExecutions);
  };
  const visit = (value) => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) { value.forEach(visit); return; }
    values.push(value);
    if (Array.isArray(value.object_refs)) refs.push(...value.object_refs);
    for (const [key, child] of Object.entries(value)) if (key !== "object_refs") visit(child);
  };
  toolCalls.forEach((call) => { visit(call.result); collectExecutions(call.result); });
  delegations.forEach((result) => { visit(result); collectExecutions(result); });
  // A retrieved historical task may itself be failed. Only execution envelopes
  // can fail this run; business data remains available for the model to explain.
  const failed = error || executions.some((value) => value.ok === false
    || ["failed", "cancelled", "partial"].includes(value.status)
    || ["failed", "rejected", "forbidden", "budget_exhausted"].includes(value.action_status));
  const succeeded = executions.some((value) => value.ok === true || value.action_status === "applied");
  const clarify = values.some((value) => value.action_status === "clarification_needed"
    || ["clarification_needed", "awaiting_clarification"].includes(value.status));
  const review = values.some((value) => value.needs_confirmation === true
    || ["proposed", "pending", "needs_confirmation"].includes(value.action_status)
    || ["needs_review", "needs_confirmation", "pending_confirmation", "awaiting_confirmation",
      "proposed"].includes(value.status));
  const fatalChild = delegations.some((result) => result.error
    || ["failed", "partial"].includes(result.status));
  // The model can recognize a successful recovery. Core pending states and
  // actual runtime/child failures remain hard bounds on that declaration.
  const unresolvedFailure = error || fatalChild || (failed && (!declaredStatus || !succeeded));
  const status = unresolvedFailure ? (succeeded ? "partial" : "failed")
    : declaredStatus === "failed" || declaredStatus === "partial" ? declaredStatus
      : clarify ? "clarification_needed" : review ? "needs_review"
        : declaredStatus || "completed";
  return { status, object_refs: [...new Map(refs.map((ref) => [JSON.stringify(ref), ref])).values()] };
}

function settledCompletionResult(saved, { durable, context = {} }) {
  const calls = saved.tool_calls || [], children = saved.delegations || [];
  const knowledge = knowledgeDelivery(calls, children, context, saved.final_response?.knowledge_intent);
  const progress = captureProgress(calls, children, context, { previous: saved.capture_progress });
  if (durable && saved.result.status === "cancelled") {
    // Host shutdown can settle the checkpoint before Core finishes the job.
    // Keep that boundary terminal: Core accepts partial/failed, never cancelled
    // through ordinary completion. Explicit Core cancellation is claim-fenced.
    const code = "interrupted_checkpoint";
    const summary = summarizeReceipts(calls, children, code);
    const refs = [...summary.object_refs, ...(Array.isArray(saved.result.object_refs) ? saved.result.object_refs : [])]
      .filter((ref) => ref && typeof ref === "object" && !Array.isArray(ref)
        && typeof ref.kind === "string" && ref.kind.trim()
        && typeof ref.id === "string" && ref.id.trim());
    return { ...saved.result, ...summary, knowledge_delivery: knowledge, capture_progress: progress,
      text: "宿主执行中断，本轮未形成有效最终答复；已完成操作和真实回执已保留，未重新执行工具。",
      error: code, error_code: code, toolCalls: calls, delegations: children,
      object_refs: [...new Map(refs.map((ref) => [
        JSON.stringify(Object.entries(ref).sort(([a], [b]) => a.localeCompare(b))), ref,
      ])).values()],
      completion: { ...saved.result.completion, recovered_interruption: true },
      recovery: { ...saved.result.recovery, code, reason: "host_interruption",
        original_status: saved.result.status, original_code: saved.result.error_code || null,
        message: "系统宿主中断的 settled checkpoint；仅恢复终态，不继续模型生成。" } };
  }
  const verified = evaluateCompletion(saved.messages || [], currentTurnStart(saved.messages || [], saved.turn_start),
    calls, saved.final_response, { durable });
  const attempts = saved.completion_repair?.attempts;
  if (Number.isSafeInteger(attempts) && attempts >= 0) verified.diagnostic.repair_attempts = attempts;
  if (saved.completion_repair?.reason) verified.diagnostic.repair_reason = saved.completion_repair.reason;
  if (Number.isSafeInteger(saved.task_continuation?.attempts)) {
    verified.diagnostic.task_continuation_attempts = saved.task_continuation.attempts;
  }
  const naturalVerified = verified.valid && verified.diagnostic.mode === "natural"
    && saved.version === 3 && saved.completion_protocol_version === COMPLETION_PROTOCOL_VERSION
    && saved.result.completion?.protocol_version === COMPLETION_PROTOCOL_VERSION
    && saved.result.completion.mode === "natural" && saved.result.text === verified.text
    && Number.isInteger(saved.turn_start) && saved.turn_start >= 0 && saved.turn_start < (saved.messages || []).length
    && saved.messages.slice(saved.turn_start).some((message) => message.role === "user");
  if (verified.valid && (verified.diagnostic.mode === "explicit" || naturalVerified)) {
    // Recompute this live diagnostic from verified prose and actual receipts;
    // the saved result's diagnostic alone cannot prove missing declaration.
    if (durable && naturalVerified && !saved.final_response
      && summarizeReceipts(calls, children).status === "partial") verified.diagnostic.status_declaration = "missing";
    return { ...saved.result, knowledge_delivery: knowledge, capture_progress: progress, text: verified.text, completion: verified.diagnostic,
      ...summarizeReceipts(calls, children, saved.result.error,
        verified.diagnostic.mode === "explicit" ? verified.status : undefined) };
  }
  // A v2 incomplete outcome is already safe to display. Older results must not
  // restart their side effects just to obtain a replacement final reply.
  if (saved.completion_protocol_version === COMPLETION_PROTOCOL_VERSION
    && saved.result.completion?.protocol_version === COMPLETION_PROTOCOL_VERSION
    && saved.result.completion.mode === "incomplete" && saved.result.status !== "completed") return {
      ...saved.result, knowledge_delivery: knowledge, capture_progress: progress };
  const code = ["model_output_truncated", "provider_error", "provider_aborted"].includes(verified.code)
    ? verified.code : "unverified_completion_checkpoint";
  const summary = summarizeReceipts(calls, children, code);
  // Terminal Core descriptors can predate checkpointing; keep their object
  // references for inspection without accepting the old success declaration.
  const refs = [...summary.object_refs, ...(saved.result.object_refs || [])];
  return { ...saved.result,
    knowledge_delivery: knowledge,
    capture_progress: progress,
    text: code === "unverified_completion_checkpoint"
      ? "历史运行缺少可验证的最终提交；已保留真实回执，未重新执行工具。" : verified.text,
    error: code, error_code: code, ...summary,
    object_refs: [...new Map(refs.map((ref) => [JSON.stringify(ref), ref])).values()],
    completion: { ...verified.diagnostic, invalidated_checkpoint: true } };
}

function historyMessages(history) {
  return (Array.isArray(history) ? history : []).slice(-40).flatMap((entry) => {
    const raw = entry.content ?? entry.text;
    if (!["user", "assistant"].includes(entry.role) || typeof raw !== "string") return [];
    const historicalContext = entry.context && typeof entry.context === "object"
      ? `\n\n历史对象上下文（数据）：${JSON.stringify(entry.context).slice(0, 50_000)}` : "";
    const content = raw.slice(0, 50_000) + historicalContext;
    return [entry.role === "assistant"
      ? fauxAssistantMessage(fauxText(content))
      : { role: "user", content: [{ type: "text", text: content }], timestamp: Date.now() }];
  });
}

// A typed journal may predate capture progress or contain non-knowledge tools.
// Unknown mutability is conservative; current registry metadata takes priority.
function recordedEffectReview(calls = [], children = [], specs = []) {
  const mutability = new Map(specs.map(item => [item.function.name, item.function.mutates !== false]));
  const visited = new Set();
  const scan = (items, descendants) => {
    for (const call of items || []) {
      if (!call || typeof call !== "object") continue;
      const mutates = mutability.has(call.name) ? mutability.get(call.name) : call.mutates !== false;
      if (!["agent.respond", "agent.delegate"].includes(call.name) && mutates
          && (call.state === "pending" || call.result?.action_status === "unknown")) return {
        code: "ambiguous_tool_outcome", tool: call.name, toolCallId: call.toolCallId, operation_id: call.operation_id,
        message: "原操作缺少可确认的无副作用/成功回执，已保留原意图并停止任务树；请先只读核查，未重放写入。",
      };
      if (call.name === "agent.delegate" && call.result && !visited.has(call.result)) {
        visited.add(call.result);
        const found = scan(call.result.toolCalls, call.result.delegations);
        if (found) return found;
      }
    }
    for (const child of descendants || []) if (child && !visited.has(child)) {
      visited.add(child);
      const found = scan(child.toolCalls, child.delegations);
      if (found) return found;
    }
  };
  return scan(calls, children);
}

export function createRuntime({ gatewayUrl, token, role = "b_query", timeoutMs = 165000,
  mock = false, fetchImpl = fetch, modelFactory, maxToolCalls = 60, maxCompletionRepairs = 1,
  maxTaskContinuations = 1, closingReserveMs, now = () => Date.now() }) {
  if (!token) throw new Error("AGENT_TOKEN is required");
  if (!Number.isInteger(maxCompletionRepairs) || maxCompletionRepairs < 0 || maxCompletionRepairs > 2) {
    throw new Error("maxCompletionRepairs must be 0, 1, or 2");
  }
  if (!Number.isInteger(maxTaskContinuations) || maxTaskContinuations < 0 || maxTaskContinuations > 2) {
    throw new Error("maxTaskContinuations must be 0, 1, or 2");
  }
  const configuredReserve = closingReserve(timeoutMs, closingReserveMs);
  if (typeof now !== "function") throw new Error("now must be a clock function");

  return async function run(payload, { signal: externalSignal } = {}) {
    const controller = new AbortController();
    let deadline = now() + timeoutMs;
    let workDeadline = deadline - configuredReserve;
    const tree = { controller, deadline, used: 0, limit: maxToolCalls, children: 0,
      secrets: [token, process.env.PI_API_KEY], agents: new Set(), journals: new Map(), workClosed: false };
    const abort = () => { controller.abort(); tree.agents.forEach((agent) => agent.abort()); };
    const fenceUnknownEffect = (review, originRunId) => {
      if (!tree.recoveryReview || (review.code === "ambiguous_tool_outcome"
          && tree.recoveryReview.code !== "ambiguous_tool_outcome")) {
        tree.recoveryReview = { ...review, origin_run_id: originRunId };
      }
      // Stop all inference/queued effects while letting already-issued Core
      // requests settle. No child may leave its parent free to write again.
      tree.agents.forEach((agent) => agent.abort());
    };
    // Timers can be delayed by the event loop. Enforce the shared absolute
    // boundary synchronously before starting another provider/tool effect.
    const enforceDeadline = () => { if (now() >= deadline) abort(); };
    // Soft closure interrupts model streams, not the transport of an already
    // issued mutation. Its receipt can still settle before the hard deadline.
    const closeWork = () => {
      if (tree.workClosed) return;
      tree.workClosed = true;
      tree.agents.forEach((agent) => agent.abort());
    };
    const enforceWorkDeadline = () => {
      enforceDeadline();
      if (workDeadline < deadline && now() >= workDeadline) closeWork();
    };
    const budgetHint = () => runtimeBudgetHint(deadline, workDeadline, now(), tree.limit - tree.used);
    const workBudgetError = () => Object.assign(new Error("Work budget ended; reserved time is for settling receipts and delivery."),
      { code: "work_budget_exhausted" });
    let workTimer;
    let timer = setTimeout(abort, timeoutMs);
    externalSignal?.addEventListener("abort", abort, { once: true });
    if (externalSignal?.aborted) abort();
    const clean = (value) => redact(value, tree.secrets);
    const checkpointUnavailable = (cause) => {
      if (!tree.checkpointFailure) {
        const terminal = ["checkpoint_size_limit", "checkpoint_invalid"].includes(cause?.code);
        const failure = new Error(terminal ? "Durable checkpoint cannot be stored safely."
          : "Durable checkpoint is unavailable; reclaim the same job before continuing.");
        failure.code = terminal ? cause.code : "checkpoint_unavailable";
        failure.retryable = !terminal;
        tree.checkpointFailure = failure;
        // A child persistence failure fences the entire tree. Keep the Core
        // lease recoverable and do not let parallel model/tool work continue.
        abort();
      }
      return tree.checkpointFailure;
    };

    async function gateway(path, runToken, body, { signal = controller.signal,
      operationId = `agtop-${crypto.randomUUID()}` } = {}) {
      const response = await fetchImpl(`${gatewayUrl}${path}`, {
        method: body === undefined ? "GET" : "POST", signal,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${runToken}`,
          "X-Operation-Id": operationId },
        ...(body === undefined ? {} : { body: JSON.stringify({ ...body,
          ...(path.startsWith("/v2/agent/tools/") ? { operation_id: operationId } : {}) }) }),
      });
      const result = await response.json().catch(() => null);
      if (!response.ok || !result) {
        const err = new Error(result?.error?.message || `gateway HTTP ${response.status}`);
        err.code = result?.error?.code || "GATEWAY_ERROR";
        err.details = result?.error;
        err.httpStatus = response.status;
        throw err;
      }
      return result;
    }

    async function execute({ runToken, agentRole, message, history = [], context = {},
      depth = 0, manifest = {}, parentSignal = controller.signal }) {
      const runId = context.run_id;
      const durable = Boolean(payload.durable && runId);
      let restored;
      try { restored = durable ? decodeCheckpoint(payload.resume?.checkpoints?.[runId], runId) : undefined; }
      catch (cause) { throw checkpointUnavailable(cause); }
      const toolCalls = structuredClone(restored?.tool_calls || []);
      const events = structuredClone(restored?.events || []);
      const delegations = structuredClone(restored?.delegations || []);
      tree.journals.set(runId || agentRole, { run_id: runId, toolCalls, delegations });
      const rememberDelegation = (result) => {
        const index = delegations.findIndex((item) => item.run_id === result.run_id);
        if (index < 0) delegations.push(result);
        else delegations[index] = { ...delegations[index], ...result };
      };
      let agent;
      let providerObserver;
      const providerRequests = structuredClone(restored?.provider_requests || []);
      const observations = () => [...new Map([...providerRequests, ...(providerObserver?.records() || []),
        ...delegations.flatMap((child) => child.provider_requests || [])].map((row) => [row.request_id, row])).values()];
      const observeProvider = (record) => {
        providerRequests.push(record);
        if (record.error_class === "provider_quota_exhausted" && !tree.providerResourceFailure) {
          tree.providerResourceFailure = { code: "provider_quota_exhausted", provider_error_code: record.provider_error_code,
            http_status: record.http_status, request_id: record.request_id };
          abort();
        }
      };
      const observeProviderStart = async (record) => {
        assertWorkActive();
        providerRequests.push(record);
        await checkpoint({ type: "provider_request_started", request_id: record.request_id,
          model: record.model, retry_index: record.retry_index });
        // Persistence can consume the last remaining milliseconds. Do not
        // touch the provider after a late checkpoint acknowledgement.
        assertWorkActive();
      };
      let error;
      let errorCode;
      let finalResponse = restored?.final_response;
      let turnStart = currentTurnStart(restored?.messages || [], restored?.turn_start);
      const savedRepair = restored?.completion_repair;
      const continuation = continuationState(restored?.task_continuation);
      const verifiedContinuationBudget = !restored || Number.isFinite(restored.execution_budget?.deadline_ms);
      const repair = { attempts: Number.isSafeInteger(savedRepair?.attempts) && savedRepair.attempts >= 0
        ? savedRepair.attempts : 0,
        pending: savedRepair?.pending === true, prompt_recorded: savedRepair?.prompt_recorded === true,
        reason: ["missing_final_response", "model_output_truncated", "missing_status_declaration"].includes(savedRepair?.reason)
          ? savedRepair.reason : undefined };
      let recoveryReview;
      let registryTools = [];
      const pendingEffects = new Set();
      const enterUnknownReview = (review) => {
        if (review.toolCallId) pendingEffects.add(review.toolCallId);
        recoveryReview ||= review;
        fenceUnknownEffect(review, runId);
      };
      let checkpointQueue = Promise.resolve();
      const checkpointFailed = (cause) => {
        if (cause?.httpStatus === 413) cause = { code: "checkpoint_size_limit" };
        else if ([400, 422].includes(cause?.httpStatus)) cause = { code: "checkpoint_invalid" };
        return checkpointUnavailable(cause);
      };
      const checkpoint = (event, extra = {}) => {
        if (!durable) return Promise.resolve();
        if (tree.checkpointFailure) throw tree.checkpointFailure;
        if (event) events.push({ ...event, run_id: runId, agent_id: agentRole, timestamp: Date.now() });
        // Snapshot synchronously: concurrent calls cannot mutate a queued payload.
        let value;
        try {
          value = boundedSnapshot({ version: 3, completion_protocol_version: COMPLETION_PROTOCOL_VERSION,
            turn_start: turnStart, run_id: runId, role: agentRole,
            messages: agent?.state.messages || restored?.messages || [], tool_calls: toolCalls,
            events, delegations, final_response: finalResponse, completion_repair: repair,
            task_continuation: continuation, provider_requests: observations(),
            capture_progress: captureProgress(toolCalls, delegations, context,
              { previous: restored?.capture_progress || context.capture_continuation?.previous_progress }),
            runtime_budget_hint: budgetHint(),
            execution_budget: { deadline_ms: deadline, remaining_tool_calls: Math.max(0, tree.limit - tree.used),
              delegations_started: tree.children, work_deadline_ms: workDeadline,
              closing_reserve_ms: deadline - workDeadline }, ...extra }, clean);
        } catch (error) { throw checkpointFailed(error); }
        const progress = event ? clean(events.at(-1)) : undefined;
        checkpointQueue = checkpointQueue.then(async () => {
          if (tree.checkpointFailure) throw tree.checkpointFailure;
          const receipt = await gateway("/v2/agent/checkpoint", runToken,
            { checkpoint: value, ...(progress ? { event: progress } : {}) },
            { signal: AbortSignal.timeout(5000) });
          if (receipt.ok !== true) throw new Error("invalid checkpoint acknowledgement");
        }).catch((error) => { throw checkpointFailed(error); });
        return checkpointQueue;
      };
      let childTimer;
      const localController = new AbortController();
      const localAbort = () => { localController.abort(); agent?.abort(); };
      parentSignal.addEventListener("abort", localAbort, { once: true });
      if (parentSignal.aborted) localAbort();
      // A shorter role limit is local; the root deadline is governed by its
      // one shared controller. A second local timer at the same root boundary
      // can fire first and otherwise let the parent resume after child abort.
      if (manifest.timeout_s && manifest.timeout_s * 1000 < deadline - now()) {
        childTimer = setTimeout(localAbort, manifest.timeout_s * 1000);
      }
      const localSignal = localController.signal;
      const assertLocalActive = () => {
        enforceDeadline();
        if (localSignal.aborted) throw new Error("run deadline exceeded");
      };
      const assertWorkActive = () => {
        assertLocalActive();
        if (tree.recoveryReview) throw Object.assign(new Error("Unconfirmed tool effect requires review before more work."),
          { code: "ambiguous_tool_outcome" });
        enforceWorkDeadline();
        if (tree.workClosed) throw workBudgetError();
      };
      const request = (path, body, operationId) => {
        assertLocalActive();
        return gateway(path, runToken, body, { signal: localSignal, ...(operationId ? { operationId } : {}) });
      };
      try {
        if (restored?.settled && restored.result) {
          const recorded = recordedEffectReview(toolCalls, delegations);
          if (recorded || restored.result.recovery?.code === "ambiguous_tool_outcome"
              || restored.result.error_code === "ambiguous_tool_outcome") {
            enterUnknownReview(recorded || restored.result.recovery || { code: "ambiguous_tool_outcome",
              message: "已有子运行包含效果未知的写入，任务树已停止继续工作；请核对实际对象。" });
          }
          const settled = settledCompletionResult(restored, { durable, context });
          return clean(recoveryReview ? { ...settled, status: "needs_review", error_code: recoveryReview.code,
            recovery: recoveryReview, text: receiptSummary(settled.knowledge_delivery,
              { reason: recoveryReview.message, progress: { ...settled.capture_progress, requires_review: true } }),
            capture_progress: { ...settled.capture_progress, requires_review: true } } : settled);
        }
        if (continuation.pending && !verifiedContinuationBudget) {
          recoveryReview = { code: "unverified_continuation_budget",
            message: "旧检查点缺少原任务剩余预算，已停止自动续做；已保存对象和回执保留，未重新执行。" };
          throw new Error("unverified_continuation_budget");
        }
        const registry = await request("/v2/agent/tools");
        registryTools = registry.tools || [];
        // Older checkpoints could record an HTTP200 unknown mutation as a
        // completed tool call. Its effect is still unknown, even with a local
        // toolResult present; preserve the intent and stop before any model.
        const unknownMutation = toolCalls.find((call) => !call.isError && call.result?.action_status === "unknown"
          && registry.tools?.some((spec) => spec.function.name === call.name && spec.function.mutates !== false));
        if (unknownMutation) {
          unknownMutation.state = "pending";
          enterUnknownReview({ code: "ambiguous_tool_outcome", tool: unknownMutation.name,
            toolCallId: unknownMutation.toolCallId, operation_id: unknownMutation.operation_id,
            message: "已有写入回执标记效果未知，已保留原操作并停止自动续做；请核对实际对象。" });
          throw new Error("ambiguous_tool_outcome");
        }
        if (depth === 0 && Number.isFinite(registry.budget_tool_calls)) {
          tree.limit = Math.min(tree.limit, registry.budget_tool_calls - (registry.used_tool_calls || 0));
        }
        const canDelegate = agentRole === "orchestrator" || manifest.max_delegation_depth > 0;
        const roles = canDelegate && depth < 3 ? (await request("/v2/agent/roles")).roles || [] : [];
        const tools = (registry.tools || []).filter((spec) => !HUMAN_TOOLS.has(spec.function.name))
          .map(({ function: fn }) => ({
            name: fn.name, label: fn.name, description: fn.description, parameters: fn.parameters,
            execute: async (id, params) => {
              // Refuse before entering the mutation-transport try/catch:
              // a local refusal is not an ambiguous remote side effect.
              if (fn.mutates !== false) assertWorkActive();
              let result;
              const operationId = runId ? `${runId}/${id}` : `agtop-${crypto.randomUUID()}`;
              try {
                result = await request(`/v2/agent/tools/${encodeURIComponent(fn.name)}`,
                  { params: params || {} }, operationId);
                if (typeof result.action_status !== "string" || !result.action_status.trim()) {
                  const err = new Error("gateway returned no valid action_status");
                  err.code = "INVALID_GATEWAY_RECEIPT";
                  throw err;
                }
                if (fn.mutates !== false && result.action_status === "unknown") {
                  const err = new Error("gateway could not determine the mutation outcome");
                  err.code = "AMBIGUOUS_GATEWAY_RECEIPT";
                  throw err;
                }
              }
              catch (err) {
                const rejected = err.httpStatus >= 400 && err.httpStatus < 500;
                // A server error or malformed response may follow a committed
                // effect. Only an explicit 4xx refusal permits model correction.
                if (fn.mutates !== false && !rejected) {
                  enterUnknownReview({ code: "ambiguous_tool_outcome", tool: fn.name,
                    toolCallId: id, operation_id: operationId,
                    ...(err.httpStatus ? { http_status: err.httpStatus } : {}),
                    message: "写入请求未获得有效回执，无法确认是否生效；已停止任务树后续工作，请核对实际对象。" });
                }
                if (err.details || rejected) throw new Error(JSON.stringify(clean({ ...(err.details || {}),
                  code: err.code, tool: fn.name, missing_fields: err.details?.missing_fields || [],
                  expected_schema: err.details?.expected_schema || fn.parameters,
                  received_arguments: params })));
                throw err;
              }
              const receipt = clean(result);
              return { content: [{ type: "text", text: JSON.stringify(receipt) }], details: receipt };
            },
          }));
        tools.push({
          name: "agent.respond", label: "提交本轮最终答复",
          description: "将用户实际看到的完整答案或受托交付物提交为最终答复和状态。普通过程消息不会作为最终答案交付，不能用已完成摘要替代正文。没有业务副作用，不能确认提案或改变工具结果。此调用结束本轮 Agent；不要与其他工具并行提交。",
          parameters: { type: "object", properties: {
            status: { type: "string", enum: ["completed", "needs_review", "clarification_needed", "partial", "failed"] },
            text: { type: "string", description: "用户实际看到的最终答案全文。包含所要求的完整时间线、分析或表格及来源；不能写成‘正文见上’、‘表格已交付’或仅概述完成情况。保留关键参数的样品、温度、压力、阶段与单位，标识推断及未知。" },
            knowledge_intent: { type: "string", enum: ["organize", "query", "none"],
              description: "由你依据用户目标判断：organize=整理/建档/补充实验知识，需要真实写入或确认已有记录；query=查询/对比已有来源，不重复建档；none=其他任务。此声明不证明写入已生效，宿主另按真实回执报告。" },
          }, required: ["status", "text"], additionalProperties: false },
          execute: async (id, params) => {
            if (!params.text?.trim()) throw new Error("final response text is required");
            finalResponse = clean({ status: params.status, text: params.text,
              ...(params.knowledge_intent ? { knowledge_intent: params.knowledge_intent } : {}) });
            const receipt = { status: "response_recorded" };
            return { content: [{ type: "text", text: JSON.stringify(receipt) }], details: receipt };
          },
        });
        if (roles.length) tools.push({
          name: "agent.delegate", label: "委派专业 Agent",
          description: `自主选择专业角色处理子任务。仅创建范围受限的子运行，不能替用户确认。可用角色：${JSON.stringify(clean(roles))}`,
          parameters: { type: "object", properties: {
            agent_id: { type: "string", enum: roles.map((item) => item.agent_id) },
            message: { type: "string", description: "完整子任务及相关对象 ID、依赖结果和完成条件。" },
          }, required: ["agent_id", "message"], additionalProperties: false },
          execute: async (id, params) => {
            assertWorkActive();
            if (!roles.some((item) => item.agent_id === params.agent_id)) throw new Error("unknown delegate role");
            if (tree.children >= 8) throw new Error("delegation budget exhausted");
            tree.children += 1;
            // Secret-bearing dispatch response is never included in tool results.
            const child = await request("/v2/agent/delegate", { ...params,
              ...(runId ? { delegation_id: `${runId}/${id}` } : {}) });
            const resumedChild = payload.resume?.runs?.[child.run_id];
            if (!child.run_token && resumedChild?.run_token) child.run_token = resumedChild.run_token;
            if (Object.hasOwn(child, "checkpoint")) {
              try {
                if (child.checkpoint === null) throw { code: "checkpoint_invalid" };
                child.checkpoint = decodeCheckpoint(child.checkpoint, child.run_id);
              } catch (cause) { throw checkpointFailed(cause); }
              payload.resume ||= {};
              payload.resume.checkpoints ||= {};
              payload.resume.checkpoints[child.run_id] = child.checkpoint;
            }
            if (!child.run_token && child.result) {
              const previous = child.checkpoint || payload.resume?.checkpoints?.[child.run_id] || {};
              const recorded = recordedEffectReview(previous.tool_calls || child.result.toolCalls,
                previous.delegations || child.result.delegations, registryTools);
              const result = clean({ agent_id: child.agent_id, run_id: child.run_id,
                task_id: child.task_id,
                ...settledCompletionResult({ ...previous, result: previous.result || child.result }, { durable, context }) });
              if (recorded || result.recovery?.code === "ambiguous_tool_outcome" || result.error_code === "ambiguous_tool_outcome"
                  || result.capture_progress?.requires_review) {
                const review = recorded || result.recovery || { code: "ambiguous_tool_outcome",
                  message: "已有子运行包含效果未知的写入，已停止任务树后续工作；请核对实际对象。" };
                fenceUnknownEffect(review, child.run_id);
                recoveryReview ||= tree.recoveryReview;
                result.status = "needs_review"; result.error_code = review.code; result.recovery = review;
                result.capture_progress = { ...result.capture_progress, requires_review: true };
              }
              rememberDelegation(result);
              const receipt = delegationReceipt(result);
              return { content: [{ type: "text", text: JSON.stringify(receipt) }], details: receipt };
            }
            if (!child.run_token || !child.run_id) throw new Error("invalid child dispatch receipt");
            tree.secrets.push(child.run_token);
            if (durable) await checkpoint({ type: "delegation_started", child_agent_id: child.agent_id, child_run_id: child.run_id });
            else events.push({ type: "delegation_started", agent_id: child.agent_id, run_id: child.run_id });
            const childResult = await execute({ runToken: child.run_token,
              agentRole: child.agent_id, message: params.message, depth: depth + 1,
              context: { ...context, task_id: child.task_id, run_id: child.run_id,
                parent_run_id: context.run_id }, manifest: child.manifest || {}, parentSignal: localSignal });
            const result = clean({ agent_id: child.agent_id, run_id: child.run_id,
              task_id: child.task_id, ...childResult });
            try {
              // Cleanup gets a short independent timeout even if the model exhausted its deadline.
              await gateway("/v2/agent/delegate/complete", runToken, { run_id: child.run_id,
                claim_epoch: child.claim_epoch,
                status: childResult.error || ["failed", "partial"].includes(childResult.status) ? "failed" : "succeeded",
                result: { ...delegationReceipt(result), events: result.events,
                  provider_requests: result.provider_requests || [] } },
              { signal: AbortSignal.timeout(5000) });
            } catch (finishError) {
              result.error = `child completion failed: ${finishError.code || finishError.message}`;
              result.status = "partial";
            }
            rememberDelegation(result);
            if (durable) await checkpoint({ type: "delegation_completed", child_agent_id: child.agent_id,
              child_run_id: child.run_id, status: result.status });
            else events.push({ type: "delegation_completed", agent_id: child.agent_id,
              run_id: child.run_id, status: result.status });
            const receipt = delegationReceipt(result);
            return { content: [{ type: "text", text: JSON.stringify(receipt) }], details: receipt };
          },
        });
        const withBudgetHint = (result) => ({ ...result,
          // Keep the authoritative receipt unchanged in the journal. The
          // model gets fresh timing data in its view of each tool result.
          content: [{ type: "text", text: JSON.stringify({ ...result.details, runtime_budget_hint: budgetHint() }) }],
        });
        const contractTools = tools.map((tool) => withContract({ ...tool,
          execute: async (id, args, ...rest) => {
            const cached = toolCalls.find((call) => call.toolCallId === id && call.state === "completed");
            if (cached) {
              if (cached.name !== tool.name || JSON.stringify(cached.arguments) !== JSON.stringify(clean(args))) {
                throw new Error("tool_call_id_collision: refusing to reuse an operation with different arguments");
              }
              if (cached.isError) throw new Error(JSON.stringify(cached.result));
              return withBudgetHint({ details: cached.result });
            }
            const readOnly = (registry.tools || []).some((item) => item.function.name === tool.name && item.function.mutates === false);
            const mutation = (registry.tools || []).some((item) => item.function.name === tool.name && item.function.mutates !== false);
            if (continuation.pending && (mutation || tool.name === "agent.delegate")) {
              if (!continuation.readback_checked) throw new Error("continuation_readback_required: read existing objects and sources before additional writes or delegation");
              if (mutation) {
                const prior = sameCompletedWrite(toolCalls, tool.name, clean(args));
                if (prior) {
                  const reused = { ...prior.result, continuation_reused_receipt: true,
                    reused_operation_id: prior.operation_id, reused_tool_call_id: prior.toolCallId };
                  return withBudgetHint({ details: reused });
                }
              }
            }
            const result = await tool.execute(id, args, ...rest);
            if (continuation.pending && readOnly && successfulReceipt(result.details, { readOnly: true })) continuation.readback_checked = true;
            return withBudgetHint(result);
          },
        }, clean));
        const readNames = new Set((registry.tools || []).filter((item) => item.function.mutates === false)
          .map((item) => item.function.name));
        const repairTools = contractTools.filter((tool) => tool.name === "agent.respond" || readNames.has(tool.name));
        const initialTools = repair.pending ? repairTools : contractTools;
        const observation = { runId, agentId: agentRole, onRequest: observeProviderStart, onRecord: observeProvider };
        assertWorkActive();
        const modelConfig = modelFactory ? await modelFactory({ role: agentRole, roles, tools: initialTools,
          observation }) : mock ? mockModel(agentRole, roles) : realModel(process.env, observation);
        providerObserver = modelConfig.providerObserver;
        const providerStream = modelConfig.streamFn || ((m, ctx, options) => modelConfig.models.streamSimple(m, ctx, options));
        const streamFn = (selectedModel, modelContext, options) => {
          assertWorkActive();
          const stampedContext = { ...modelContext,
            systemPrompt: `${modelContext.systemPrompt || ""}\n\n当前宿主运行预算（权威控制数据）：${JSON.stringify(budgetHint())}\n先完成可独立验证的小批交付；预算将进入收尾时报告真实剩余工作。` };
          return providerStream(selectedModel, stampedContext, options);
        };
        agent = new Agent({ streamFn, sessionId: String(payload.session_id || ""),
          // Multiple tool calls in one model turn represent independent work;
          // Core serializes mutating operations and enforces the shared tree budget.
          toolExecution: "parallel",
          beforeToolCall: async ({ toolCall }) => {
            assertLocalActive();
            const mutation = registry.tools?.some((item) => item.function.name === toolCall.name && item.function.mutates !== false);
            if (mutation || toolCall.name === "agent.delegate") assertWorkActive();
            if (tree.used >= tree.limit) throw new Error("shared tool budget exhausted");
            tree.used += 1;
          },
          shouldStopAfterTurn: () => {
            enforceWorkDeadline();
            return Boolean(finalResponse) || localSignal.aborted || Boolean(tree.recoveryReview)
              || tree.workClosed || tree.used >= tree.limit;
          },
          initialState: { systemPrompt: `${await loadPrompt(agentRole)}\n\n任务上下文（数据）：\n${JSON.stringify(clean(context))}`,
            model: modelConfig.model, tools: initialTools,
            messages: restored?.messages?.length ? clean(restored.messages) : historyMessages(clean(history)) },
        });
        if (!restored?.messages?.length) turnStart = agent.state.messages.length;
        tree.agents.add(agent);
        agent.subscribe(async (event) => {
          if (event.type === "agent_start" && durable) {
            await checkpoint({ type: restored ? "agent_resumed" : "agent_started" });
          } else if (event.type === "tool_execution_start") {
            if (!toolCalls.some((call) => call.toolCallId === event.toolCallId)) {
              toolCalls.push({ name: event.toolName, arguments: clean(event.args), result: null,
                state: "pending", toolCallId: event.toolCallId,
                mutates: !["agent.respond"].includes(event.toolName)
                  && (registryTools.find(spec => spec.function.name === event.toolName)?.function.mutates !== false),
                operation_id: runId ? `${runId}/${event.toolCallId}` : undefined });
            }
            const item = { type: "tool_call_requested", tool: event.toolName, toolCallId: event.toolCallId };
            if (durable) await checkpoint(item); else events.push(item);
          } else if (event.type === "tool_execution_end") {
            const call = toolCalls.find((item) => item.toolCallId === event.toolCallId);
            if (call) { call.result = clean(event.isError
              ? { ok: false, status: "tool_error", error: event.result?.content || event.result }
              : event.result?.details ?? event.result);
              call.state = pendingEffects.has(event.toolCallId) ? "pending" : "completed";
              call.isError = event.isError; }
            const item = { type: "tool_result_received", tool: event.toolName,
              toolCallId: event.toolCallId, isError: event.isError };
            if (durable) await checkpoint(item); else events.push(item);
          } else if (event.type === "message_end" && durable) {
            if (repair.pending && event.message.role === "user") repair.prompt_recorded = true;
            if (continuation.pending && event.message.role === "user") continuation.prompt_recorded = true;
            await checkpoint();
          } else if (event.type === "agent_end") {
            if (durable) await checkpoint({ type: "assistant_message" });
            else events.push({ type: "assistant_message" });
          }
        });
        const repairControlsAvailable = () => !localSignal.aborted && !tree.checkpointFailure && !tree.providerResourceFailure
          && !recoveryReview && !tree.recoveryReview && !tree.workClosed && tree.used < tree.limit && now() < workDeadline;
        // A natural final body is valid. When mixed execution receipts still
        // project partial, only the model can declare whether the task actually
        // recovered. Never infer recovery from prose, a retry, or a later success.
        const needsStatusDeclaration = (outcome) => durable && outcome.valid
          && outcome.diagnostic.mode === "natural" && !finalResponse
          && summarizeReceipts(toolCalls, delegations).status === "partial";
        const rejectTruncatedCalls = async () => {
          const terminal = agent.state.messages.findLast((entry) => entry.role === "assistant");
          if (terminal?.stopReason !== "length") return;
          const resultIds = new Set(agent.state.messages.filter((entry) => entry.role === "toolResult").map((entry) => entry.toolCallId));
          for (const block of terminal.content || []) {
            if (block.type !== "toolCall") continue;
            let saved = toolCalls.find((call) => call.toolCallId === block.id);
            if (saved?.state === "pending") throw new Error("pending_tool_outcome_requires_review");
            if (!saved) {
              saved = { name: block.name, toolCallId: block.id, arguments: clean(block.arguments),
                operation_id: `${runId}/${block.id}`, state: "completed", isError: true,
                result: { ok: false, status: "tool_error", error: { code: "model_output_truncated",
                  message: "Truncated tool arguments were not executed; submit a complete call." } } };
              toolCalls.push(saved);
            }
            if (!resultIds.has(block.id)) agent.state.messages = [...agent.state.messages, toolResultMessage(saved)];
          }
        };
        const submitContinuationPrompt = async (resume = false) => {
          if (!resume) {
            continuation.attempts += 1;
            continuation.pending = true;
            continuation.prompt_recorded = false;
            continuation.readback_checked = false;
          }
          await rejectTruncatedCalls();
          agent.state.tools = contractTools;
          await checkpoint({ type: resume ? "task_continuation_resumed" : "task_continuation_started",
            attempt: continuation.attempts, reason: "model_output_truncated" });
          await agent.prompt(continuationPrompt(continuation.attempts, toolCalls, delegations));
          await agent.waitForIdle();
        };
        const finishContinuation = async () => {
          continuation.pending = false;
          const outcome = evaluateCompletion(agent.state.messages, turnStart, toolCalls, finalResponse, { durable });
          await checkpoint({ type: "task_continuation_finished", attempt: continuation.attempts,
            valid: outcome.valid, ...(outcome.code ? { code: outcome.code } : {}) });
        };
        const submitRepairPrompt = async (resume = false, reason = "missing_final_response") => {
          if (!resume) {
            repair.attempts += 1;
            repair.pending = true;
            repair.prompt_recorded = false;
            repair.reason = reason;
          }
          reason = repair.reason || reason;
          agent.state.tools = repairTools;
          await checkpoint({ type: resume ? "completion_repair_resumed" : "completion_repair_started",
            attempt: repair.attempts, reason });
          await agent.prompt(`宿主完成协议修复（同一任务，第 ${repair.attempts} 次；不是新的研究请求）。\n`
            + (reason === "missing_status_declaration"
              ? "上一轮已有有效自然正文，但真实执行回执包含失败与成功，尚未声明失败是否已解决（missing_status_declaration）。请逐项核对失败和后续回执：不能仅因有后续成功或已有正文就认定失败已恢复。"
              : `上一轮未形成有效最终提交（${reason}）。`)
            + "请先核对本任务原始要求、实际读取的来源页面以及真实工具回执。"
            + "必要时用当前提供的只读工具读取已有页面或提案，保留来源引用；不要把历史摘要、计划句或上一轮正文扩写成已核验的实验参数。"
            + "当前阶段仅核验和补全最终协议，不能新增、重放或重复业务写入，也不能重新委派。已有操作是否完成只能依据实际回执；"
            + "缺少真实交付物或证据时，应明确partial/failed/clarification_needed/needs_review，不要宣称完成。"
            + "核验后必须调用agent.respond提交用户要求的实际交付物（如时间线、分析或表格），保留真实对象引用、未完成项及状态；不能用已完成摘要替代交付物，也不能只输出普通正文或计划。"
            + "复用本任务已有上下文和剩余预算，不复述任何内部上下文包装或隐藏推理。");
          await agent.waitForIdle();
        };
        const finishRepair = async () => {
          repair.pending = false;
          const outcome = evaluateCompletion(agent.state.messages, turnStart, toolCalls, finalResponse, { durable });
          const missingStatus = needsStatusDeclaration(outcome);
          await checkpoint({ type: "completion_repair_finished", attempt: repair.attempts,
            valid: outcome.valid && !missingStatus,
            ...(missingStatus ? { code: "missing_status_declaration" } : outcome.code ? { code: outcome.code } : {}) });
        };
        assertLocalActive();
        const promptNotRecorded = restored?.messages?.length && toolCalls.length === 0 && !finalResponse
          && !restored.messages.slice(turnStart).some((item) => item.role === "user");
        if (continuation.pending && !continuation.prompt_recorded && repairControlsAvailable()) {
          await submitContinuationPrompt(true);
        } else if (repair.pending && !repair.prompt_recorded && repairControlsAvailable()) {
          await submitRepairPrompt(true);
        } else if (promptNotRecorded) {
          // agent_start is checkpointed before Pi appends the new user prompt.
          // A crash in that small window has no tool intent to replay.
          turnStart = agent.state.messages.length;
          await agent.prompt(message);
        } else if (restored?.messages?.length) {
          // Complete interrupted assistant batches from the receipt journal.
          // Never ask the model to recreate calls that already executed.
          const resultIds = new Set(agent.state.messages.filter((m) => m.role === "toolResult").map((m) => m.toolCallId));
          for (const savedMessage of [...agent.state.messages]) {
            if (savedMessage.role !== "assistant") continue;
            for (const block of savedMessage.content || []) {
              if (block.type !== "toolCall") continue;
              const saved = toolCalls.find((call) => call.toolCallId === block.id);
              // An error transcript is not a business receipt. Unknown writes
              // retain pending intent even if Pi emitted a local error message.
              if (resultIds.has(block.id) && saved?.state !== "pending") continue;
              const spec = registry.tools.find((item) => item.function.name === block.name)?.function;
              if (saved?.state === "completed") agent.state.messages = [...agent.state.messages, toolResultMessage(saved)];
              else if (savedMessage.stopReason === "length") {
                // Pi never executes calls from a truncated assistant message.
                // A restored pre-tool snapshot must keep that same guarantee.
                const rejected = saved || { name: block.name, toolCallId: block.id,
                  arguments: clean(block.arguments), operation_id: `${runId}/${block.id}` };
                Object.assign(rejected, { state: "completed", isError: true,
                  result: { ok: false, status: "tool_error", error: { code: "model_output_truncated",
                    message: "Truncated tool arguments were not executed; submit a complete call." } } });
                if (!saved) toolCalls.push(rejected);
                agent.state.messages = [...agent.state.messages, toolResultMessage(rejected)];
                await checkpoint({ type: "tool_result_received", tool: block.name, toolCallId: block.id, isError: true });
              }
              // No journal entry means durable intent was never recorded: the
              // awaited pre-execution checkpoint prevented this call running.
              else if (!saved || block.name === "agent.delegate" || block.name === "agent.respond" || spec?.mutates === false) {
                const target = agent.state.tools.find((tool) => tool.name === block.name);
                if (!target) throw new Error("recovery_tool_unavailable");
                const args = target.prepareArguments(block.arguments);
                let call = saved;
                if (!call) { call = { name: block.name, arguments: clean(args), state: "pending",
                  toolCallId: block.id, operation_id: `${runId}/${block.id}`, result: null }; toolCalls.push(call); }
                await checkpoint({ type: "tool_call_resuming", tool: block.name, toolCallId: block.id });
                try {
                  const result = await target.execute(block.id, args);
                  call.result = clean(result.details); call.isError = false;
                } catch (err) { call.result = { ok: false, status: "tool_error", error: String(err.message) }; call.isError = true; }
                call.state = pendingEffects.has(block.id) ? "pending" : "completed";
                await checkpoint({ type: "tool_result_received", tool: block.name, toolCallId: block.id, isError: call.isError });
                agent.state.messages = [...agent.state.messages, toolResultMessage(call)];
              } else {
                enterUnknownReview({ code: "ambiguous_tool_outcome", tool: block.name,
                  toolCallId: block.id, operation_id: `${runId}/${block.id}`,
                  message: "工具可能已执行，但回执尚未持久化；需要核对实际对象后再继续，未重复执行。" });
                break;
              }
              resultIds.add(block.id);
            }
            if (recoveryReview) break;
          }
          if (!recoveryReview && !tree.recoveryReview && !finalResponse) {
            if (agent.state.messages.at(-1)?.role !== "assistant"
              && (!repair.pending || repairControlsAvailable())) await agent.continue();
          }
        } else await agent.prompt(message);
        await agent.waitForIdle();
        if (continuation.pending) await finishContinuation();
        if (repair.pending) await finishRepair();
        let outcome = evaluateCompletion(agent.state.messages, turnStart, toolCalls, finalResponse, { durable });
        while (durable && repairControlsAvailable()) {
          if (continuationAllowed({ outcome, continuation, repair, maximum: maxTaskContinuations,
            available: repairControlsAvailable(), verifiedBudget: verifiedContinuationBudget, finalResponse, toolCalls })) {
            await submitContinuationPrompt();
            await finishContinuation();
          } else if ((needsStatusDeclaration(outcome)
            || (!outcome.valid && ["missing_final_response", "model_output_truncated"].includes(outcome.code)))
            && ["stop", "length"].includes(outcome.diagnostic.stopReason) && repair.attempts < maxCompletionRepairs) {
            await submitRepairPrompt(false, needsStatusDeclaration(outcome) ? "missing_status_declaration" : outcome.code);
            await finishRepair();
          } else break;
          outcome = evaluateCompletion(agent.state.messages, turnStart, toolCalls, finalResponse, { durable });
        }
        const lastMessage = agent.state.messages.at(-1);
        recoveryReview ||= tree.recoveryReview;
        enforceDeadline();
        if (tree.providerResourceFailure && !recoveryReview) { error = "provider resources unavailable"; errorCode = "provider_quota_exhausted"; }
        else if (localSignal.aborted && !recoveryReview) { error = "run deadline exceeded"; errorCode = "run_timeout"; }
        else if (tree.workClosed && !recoveryReview && !outcome.valid) { error = "work budget ended; receipts preserved"; errorCode = "work_budget_exhausted"; }
        else if (agent.state.errorMessage) { error = String(agent.state.errorMessage); errorCode = "provider_error"; }
        else if (lastMessage?.role === "assistant" && ["error", "aborted"].includes(lastMessage.stopReason)) {
          error = String(lastMessage.errorMessage || "interrupted provider response");
          errorCode = lastMessage.stopReason === "aborted" ? "provider_aborted" : "provider_error";
        }
        else if (!finalResponse && tree.used >= tree.limit && agent.state.messages.at(-1)?.role !== "assistant") {
          error = "shared tool budget exhausted"; errorCode = "tool_budget_exhausted";
        }
      } catch (err) {
        recoveryReview ||= tree.recoveryReview;
        enforceDeadline();
        error = String(err.code || err.message || err).slice(0, 500);
        errorCode = tree.providerResourceFailure ? "provider_quota_exhausted"
          : localSignal.aborted ? "run_timeout" : tree.workClosed ? "work_budget_exhausted"
            : typeof err.code === "string" ? err.code : "runtime_error";
      }
      finally {
        clearTimeout(childTimer);
        parentSignal.removeEventListener("abort", localAbort);
        if (agent) tree.agents.delete(agent);
      }
      const completion = evaluateCompletion(agent?.state.messages || restored?.messages || [], turnStart,
        toolCalls, finalResponse, { durable });
      completion.diagnostic.repair_attempts = repair.attempts;
      if (repair.reason) completion.diagnostic.repair_reason = repair.reason;
      completion.diagnostic.task_continuation_attempts = continuation.attempts;
      if (durable && completion.valid && completion.diagnostic.mode === "natural" && !finalResponse
        && summarizeReceipts(toolCalls, delegations).status === "partial") completion.diagnostic.status_declaration = "missing";
      if (!error && !recoveryReview && !completion.valid) { error = completion.text; errorCode = completion.code; }
      const knowledge = knowledgeDelivery(toolCalls, delegations, context, finalResponse?.knowledge_intent);
      const progress = captureProgress(toolCalls, delegations, context,
        { previous: restored?.capture_progress || context.capture_continuation?.previous_progress });
      const recorded = recordedEffectReview(toolCalls, delegations, registryTools);
      // Expiry may prevent registry lookup before recovery inspects a known
      // pending write. Its durable intent still outranks a timeout label.
      if ((recorded || progress.requires_review) && recoveryReview?.code !== "ambiguous_tool_outcome") {
        const pending = progress.unresolved_writes[0] || {};
        recoveryReview = recorded || { code: "ambiguous_tool_outcome", tool: pending.tool,
          toolCallId: pending.tool_call_id, operation_id: pending.operation_id,
          message: "已有写入效果未知，已保留原意图；即使预算已过期也须先核对实际对象，未重放写入。" };
        fenceUnknownEffect(recoveryReview, runId);
        errorCode = recoveryReview.code;
      }
      if (recoveryReview?.code === "ambiguous_tool_outcome") progress.requires_review = true;
      const baseText = recoveryReview?.message ?? (completion.valid && !error ? completion.text
        : errorCode === "run_timeout" ? "本轮运行超时，未形成有效最终答复；已有工具回执已保留。"
          : errorCode === "work_budget_exhausted" ? "本轮已进入预算收尾，未形成完整最终答复；已有工具回执已保留。"
          : errorCode === "provider_quota_exhausted" ? "模型额度不足，本任务树已停止后续模型调用；已保存来源与工具回执。"
          : completion.text || "本轮未完成；已有工具回执已保留。");
      const text = completion.valid && !error && !recoveryReview ? baseText
        : receiptSummary(knowledge, { reason: baseText, progress });
      const result = clean({ text, toolCalls, events, delegations,
        provider_requests: observations(), provider_usage: summarizeProviderRequests(observations()),
        runtime_budget_hint: budgetHint(),
        ...(tree.providerResourceFailure ? { provider_resource_failure: tree.providerResourceFailure,
          needs_provider_quota: true } : {}),
        knowledge_delivery: knowledge, capture_progress: progress,
        ...(error ? { error, error_code: errorCode || "runtime_error" } : {}),
        completion: completion.diagnostic,
        ...summarizeReceipts(toolCalls, delegations, error, finalResponse?.status),
        ...(recoveryReview ? { status: "needs_review", recovery: recoveryReview,
          error_code: recoveryReview.code } : {}),
        ...(externalSignal?.aborted ? { status: "cancelled", error: "run cancelled", error_code: "run_cancelled" } : {}) });
      await checkpoint({ type: "run_finished", status: result.status }, { settled: true, result });
      return result;
    }

    try {
      if (payload.durable && payload.resume) payload.resume = decodeResume(payload.resume, payload);
      // New checkpoints preserve the original shared deadline and remaining
      // local budget across host recovery. Legacy snapshots remain readable,
      // but cannot opt into the newly introduced task continuation policy.
      const budgets = Object.values(payload.resume?.checkpoints || {}).map((saved) => saved.execution_budget).filter(Boolean);
      const savedWorkDeadlines = [];
      let legacyWorkBudget = false;
      for (const budget of budgets) {
        if (!Number.isFinite(budget.deadline_ms) || !Number.isSafeInteger(budget.remaining_tool_calls)
          || budget.remaining_tool_calls < 0 || !Number.isSafeInteger(budget.delegations_started) || budget.delegations_started < 0) {
          throw checkpointUnavailable({ code: "checkpoint_invalid" });
        }
        deadline = Math.min(deadline, budget.deadline_ms);
        tree.limit = Math.min(tree.limit, budget.remaining_tool_calls);
        tree.children = Math.max(tree.children, budget.delegations_started);
        if (budget.work_deadline_ms === undefined && budget.closing_reserve_ms === undefined) legacyWorkBudget = true;
        else {
          if (!Number.isFinite(budget.work_deadline_ms) || !Number.isFinite(budget.closing_reserve_ms)
            || budget.closing_reserve_ms < 0 || budget.work_deadline_ms > budget.deadline_ms
            || budget.deadline_ms - budget.work_deadline_ms !== budget.closing_reserve_ms) {
            throw checkpointUnavailable({ code: "checkpoint_invalid" });
          }
          savedWorkDeadlines.push(budget.work_deadline_ms);
        }
      }
      // Legacy runs keep their old hard-only policy. New snapshots never gain
      // a fresh work window or reserve after a host recovery/config change.
      workDeadline = Math.min(deadline - (legacyWorkBudget ? 0 : configuredReserve), ...savedWorkDeadlines);
      const previousResourceFailure = Object.values(payload.resume?.checkpoints || {})
        .flatMap((saved) => saved.provider_requests || []).find((row) => row.error_class === "provider_quota_exhausted");
      if (previousResourceFailure) {
        tree.providerResourceFailure = { code: "provider_quota_exhausted", provider_error_code: previousResourceFailure.provider_error_code,
          http_status: previousResourceFailure.http_status, request_id: previousResourceFailure.request_id };
        abort();
      }
      tree.deadline = deadline;
      clearTimeout(timer);
      if (now() >= deadline) abort();
      else timer = setTimeout(abort, deadline - now());
      clearTimeout(workTimer);
      enforceWorkDeadline();
      if (!tree.workClosed && workDeadline < deadline) workTimer = setTimeout(closeWork, Math.max(0, workDeadline - now()));
      return await execute({ runToken: token, agentRole: role, message: payload.message,
        history: payload.history, context: payload.context || {} });
    } catch (cause) {
      const failure = tree.checkpointFailure || cause;
      if (failure.retryable === false && ["checkpoint_size_limit", "checkpoint_invalid"].includes(failure.code)) {
        abort();
        const journals = [...tree.journals.values()];
        const calls = journals.flatMap((journal) => journal.toolCalls);
        const children = journals.flatMap((journal) => journal.delegations);
        const summary = summarizeReceipts(calls, children, failure.code);
        const result = clean({ status: summary.status, error_code: failure.code,
          text: "任务检查点超过安全存储限制或格式损坏，本轮已停止；已写入的记录和原回执保留，未重新执行工具。",
          runtime_error: { code: failure.code, retryable: false }, object_refs: [], toolCalls: [], delegations: [], events: [],
          data: { checkpoint_failure: { root_run_id: String(payload.context?.run_id || "").slice(0, 256),
            object_ref_count: summary.object_refs.length, tool_call_count: calls.length,
            complete_receipts: "Core operation results and durable checkpoints" } } });
        // Bound the entire control receipt, including arbitrary reference/ID strings.
        const append = (key, item) => {
          result[key].push(clean(item));
          if (Buffer.byteLength(JSON.stringify(result), "utf8") > 64 * 1024) result[key].pop();
        };
        summary.object_refs.slice(0, 256).forEach((ref) => append("object_refs", ref));
        for (const journal of journals) for (const call of journal.toolCalls) append("toolCalls", {
          run_id: journal.run_id, name: call.name, toolCallId: call.toolCallId,
          operation_id: call.operation_id, state: call.state, isError: Boolean(call.isError) });
        children.forEach(({ agent_id, run_id, task_id, status }) => append("delegations", { agent_id, run_id, task_id, status }));
        failure.result = result;
      }
      throw failure;
    } finally {
      clearTimeout(timer);
      clearTimeout(workTimer);
      externalSignal?.removeEventListener("abort", abort);
    }
  };
}
