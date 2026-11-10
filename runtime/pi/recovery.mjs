// Durable data only: credentials stay in the process and Core claim response.
import { validateToolArguments } from "@earendil-works/pi-ai";
import { encodeCheckpoint } from "./checkpoint-codec.mjs";

export const COMPLETION_PROTOCOL_VERSION = 3;
const FINAL_STATUSES = new Set(["completed", "needs_review", "clarification_needed", "partial", "failed"]);

// These counters are public diagnostics. Never read or forward thinking blocks.
export function completionUsage(usage) {
  return Object.fromEntries(["input", "output", "reasoning", "cacheRead", "cacheWrite", "totalTokens"]
    .filter((key) => Number.isFinite(usage?.[key])).map((key) => [key, usage[key]]));
}

export function currentTurnStart(messages, savedStart) {
  if (Number.isInteger(savedStart) && savedStart >= 0 && savedStart <= messages.length) return savedStart;
  const user = messages.findLastIndex((message) => message.role === "user");
  // A checkpoint with no current user boundary cannot prove an old final reply.
  return user < 0 ? messages.length : user;
}

export function evaluateCompletion(messages, turnStart, calls, finalResponse, { durable = false } = {}) {
  const current = messages.slice(turnStart);
  const terminal = current.findLast((message) => message.role === "assistant");
  const stopReason = terminal?.stopReason ?? "none";
  const diagnostic = { protocol_version: COMPLETION_PROTOCOL_VERSION, mode: "incomplete",
    stopReason, usage: completionUsage(terminal?.usage) };
  const failed = (code, text) => ({ valid: false, code, text, diagnostic });
  if (stopReason === "length") return failed("model_output_truncated", "模型输出达到上限，本轮未形成完整结果；已有工具回执已保留。");
  if (["error", "aborted"].includes(stopReason)) return failed(
    stopReason === "aborted" ? "provider_aborted" : "provider_error", "模型生成中断，本轮未形成完整结果；已有工具回执已保留。");
  const blocks = Array.isArray(terminal?.content) ? terminal.content : [];
  const responseCall = blocks.find((block) => block.type === "toolCall" && block.name === "agent.respond"
    && block.arguments?.status === finalResponse?.status && block.arguments?.text === finalResponse?.text);
  const responseReceipt = responseCall && calls.find((call) => call.toolCallId === responseCall.id
    && call.name === "agent.respond" && call.state === "completed" && !call.isError
    && call.result?.status === "response_recorded");
  if (FINAL_STATUSES.has(finalResponse?.status) && typeof finalResponse?.text === "string"
    && finalResponse.text.trim() && responseReceipt) {
    return { valid: true, text: finalResponse.text, status: finalResponse.status,
      diagnostic: { ...diagnostic, mode: "explicit", toolCallId: responseCall.id } };
  }
  // A complete current-turn text is a natural final answer. Its prose remains
  // the model's responsibility; historical/partial/tool-planning messages are
  // never fallback candidates and receipts still constrain task status.
  const text = blocks.filter((block) => block.type === "text").map((block) => block.text).join("");
  if (terminal === current.at(-1) && stopReason === "stop" && text.trim()
    && !blocks.some((block) => block.type === "toolCall")
    && !calls.some((call) => call.state === "pending")) {
    return { valid: true, text, status: "completed",
      diagnostic: { ...diagnostic, mode: durable ? "natural" : "legacy" } };
  }
  return failed("missing_final_response", "本轮未提交有效的最终答复；已有工具回执已保留，不能视为任务完成。");
}

export function missingFields(schema, value, path = "") {
  const result = [];
  if (schema?.type === "object" && (value === undefined || value === null)) value = {};
  if (schema?.type === "object" && value && typeof value === "object" && !Array.isArray(value)) {
    for (const key of schema.required || []) if (value[key] === undefined) result.push(`${path}${key}`);
    for (const [key, child] of Object.entries(schema.properties || {})) {
      if (value[key] !== undefined) result.push(...missingFields(child, value[key], `${path}${key}.`));
    }
  } else if (schema?.type === "array" && Array.isArray(value)) {
    value.forEach((item, index) => result.push(...missingFields(schema.items, item, `${path}${index}.`)));
  }
  return result;
}

// Use Pi's installed validator; add diagnostic context, never fill in values.
export function withContract(tool, clean = (value) => value) {
  return { ...tool, prepareArguments(args) {
    try { return validateToolArguments(tool, { name: tool.name, arguments: args }); }
    catch (error) {
      throw new Error(JSON.stringify(clean({ code: "schema_violation", tool: tool.name,
        message: error.message, missing_fields: missingFields(tool.parameters, args),
        expected_schema: tool.parameters, received_arguments: args,
        recovery: "Correct the argument structure using this schema before retrying; do not change unrelated facts." })));
    }
  } };
}

export function boundedSnapshot(value, clean) {
  return encodeCheckpoint(value, clean);
}

export function toolResultMessage(call) {
  return { role: "toolResult", toolCallId: call.toolCallId, toolName: call.name,
    content: [{ type: "text", text: JSON.stringify(call.result) }],
    details: call.result, isError: Boolean(call.isError), timestamp: Date.now() };
}
