// Generic execution controls only. Source interpretation remains with the LLM.
export function continuationState(saved) {
  return { attempts: Number.isSafeInteger(saved?.attempts) && saved.attempts >= 0 ? saved.attempts : 0,
    pending: saved?.pending === true, prompt_recorded: saved?.prompt_recorded === true,
    readback_checked: saved?.readback_checked === true };
}

export function continuationAllowed({ outcome, continuation, repair, maximum, available, verifiedBudget,
  finalResponse, toolCalls }) {
  return available && verifiedBudget && !finalResponse && !repair.pending && repair.attempts === 0
    && !continuation.pending && continuation.attempts < maximum
    && outcome.code === "model_output_truncated" && outcome.diagnostic.stopReason === "length"
    && !toolCalls.some((call) => call.state === "pending");
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort()
    .map((key) => [key, canonical(value[key])]));
  return value;
}

export function successfulReceipt(receipt, { readOnly = false } = {}) {
  return receipt?.ok !== false && !receipt?.error
    && (readOnly ? receipt?.action_status === "applied"
      : ["applied", "proposed", "clarification_needed"].includes(receipt?.action_status));
}

export function sameCompletedWrite(calls, name, args) {
  const encoded = JSON.stringify(canonical(args));
  return calls.find((call) => call.name === name && call.state === "completed" && !call.isError && successfulReceipt(call.result)
    && JSON.stringify(canonical(call.arguments)) === encoded);
}

export function continuationPrompt(attempt, calls, delegations) {
  const receipts = calls.filter((call) => call.state === "completed" && !call.isError && successfulReceipt(call.result))
    .map((call) => ({ tool: call.name, toolCallId: call.toolCallId, operation_id: call.operation_id,
      object_refs: call.result?.object_refs || [], action_status: call.result?.action_status }));
  const children = delegations.map((child) => ({ run_id: child.run_id, agent_id: child.agent_id,
    status: child.status, object_refs: child.object_refs || [] }));
  return `原任务有界续做（第 ${attempt} 次，仍为同一任务，不增加原截止时间或工具预算）。\n`
    + "上一轮输出被截断，截断消息中的工具参数未获准执行。请依据原用户目标、原始来源与真实回执判断剩余工作。"
    + "先用授权只读工具核对已有对象和来源，再完成尚未执行的必要步骤；已经完成的写入和子任务不要重复创建。"
    + "以下仅列成功回执索引，完整回执仍在本任务工具历史；没有索引不等于记录不存在。"
    + `\n${JSON.stringify({ completed_receipts: receipts, children })}\n`
    + "相同工具和相同完整参数的已完成写入会返回原回执；这不证明不同表述的事实已被语义去重。"
    + "未知写入结果需要核查，不能猜测未执行后重试。请按需提交小批次、读取实际结果，最后给出简明交付、来源及未完成项。"
    + "本轮允许原授权工具；这是任务执行续做，不是只读完成协议修复。不要复述内部包装或隐藏推理。";
}
