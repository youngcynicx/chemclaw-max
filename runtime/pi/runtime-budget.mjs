// Control-plane time only. No task classification or domain interpretation.
export function closingReserve(timeoutMs, configured) {
  const value = configured ?? (timeoutMs >= 60_000 ? Math.min(120_000, Math.floor(timeoutMs * 0.15)) : 0);
  if (!Number.isFinite(value) || value < 0 || value >= timeoutMs) {
    throw new Error("closingReserveMs must be nonnegative and smaller than timeoutMs");
  }
  return Math.floor(value);
}

export function runtimeBudgetHint(deadlineMs, workDeadlineMs, nowMs, remainingTools) {
  const remaining = Math.max(0, deadlineMs - nowMs);
  const work = Math.max(0, workDeadlineMs - nowMs);
  return {
    shared_absolute_deadline_ms: deadlineMs,
    work_deadline_ms: workDeadlineMs,
    remaining_ms: remaining, remaining_seconds: Math.floor(remaining / 1000),
    closing_reserve_ms: Math.max(0, deadlineMs - workDeadlineMs),
    available_work_ms: work,
    phase: remaining === 0 ? "expired" : work === 0 ? "closing" : "work",
    ...(Number.isFinite(remainingTools) ? { remaining_tool_calls: Math.max(0, remainingTools) } : {}),
  };
}
