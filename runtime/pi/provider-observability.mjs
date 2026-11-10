import { randomUUID } from "node:crypto";

// This module sees response bytes only in flight. Never retain response bodies,
// messages, headers, URLs, error text, image data or reasoning in an audit row.
const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const token = (value) => typeof value === "string" && /^[a-zA-Z0-9_.:/-]{1,192}$/.test(value) ? value : null;
const STOP_REASONS = new Set(["stop", "length", "tool_calls", "function_call", "end_turn", "max_tokens", "completed", "incomplete", "failed", "cancelled", "content_filter", "network_error", "error", "aborted", "http_error", "transport_error"]);
export function providerErrorCode(value) {
  const code = value?.error?.code ?? value?.code;
  if (String(code) === "1113") return "1113";
  return ["insufficient_quota", "rate_limit_exceeded", "invalid_api_key"].includes(code) ? code : null;
}
export function classifyProviderError(status, code, aborted = false) {
  if (aborted) return "provider_aborted";
  if (code === "1113" || code === "insufficient_quota") return "provider_quota_exhausted";
  if (status === 429) return "provider_rate_limited";
  if (status === 401 || status === 403) return "provider_unauthorized";
  return status >= 400 ? "provider_rejected" : "provider_transport_error";
}
export function safeProviderUsage(raw) {
  const usage = raw && typeof raw === "object" ? raw : {};
  return {
    input_tokens: number(usage.prompt_tokens ?? usage.input_tokens),
    output_tokens: number(usage.completion_tokens ?? usage.output_tokens),
    total_tokens: number(usage.total_tokens),
    cache_read_tokens: number(usage.prompt_tokens_details?.cached_tokens ?? usage.input_tokens_details?.cached_tokens ?? usage.cached_tokens),
    cache_write_tokens: number(usage.cache_creation_input_tokens),
    reasoning_tokens: number(usage.completion_tokens_details?.reasoning_tokens ?? usage.output_tokens_details?.reasoning_tokens),
  };
}
export function summarizeProviderRequests(rows) {
  const unique = new Map();
  for (const row of rows || []) if (row?.request_id) unique.set(row.request_id, row);
  const intents = [...unique.values()];
  const records = intents.filter((row) => row.phase !== "not_sent");
  const fields = Object.keys(safeProviderUsage(null));
  const sum = (key) => records.length && records.every((row) => number(row.usage?.[key]) !== null)
    ? records.reduce((total, row) => total + row.usage[key], 0) : null;
  const currencies = new Set(records.map((row) => row.cost_currency));
  const costKnown = records.length && currencies.size === 1 && !currencies.has(null)
    && records.every((row) => number(row.cost) !== null);
  return { request_count: records.length, retry_count: records.filter((row) => row.retry_index > 0).length,
    request_intent_count: intents.length,
    unfinished_requests: records.filter((row) => row.phase === "started").length,
    not_sent_requests: intents.filter((row) => row.phase === "not_sent").length,
    usage: Object.fromEntries(fields.map((key) => [key, sum(key)])),
    usage_observed_requests: records.filter((row) => Object.values(row.usage || {}).some((value) => value !== null)).length,
    quota_exhausted_requests: records.filter((row) => row.error_class === "provider_quota_exhausted").length,
    // Pi's configured zero prices are not evidence of a free provider request.
    cost: costKnown ? records.reduce((total, row) => total + row.cost, 0) : null,
    cost_currency: costKnown ? [...currencies][0] : null,
  };
}

export function createProviderObserver({runId, agentId, onRequest, onRecord, now = Date.now, idFactory = randomUUID} = {}) {
  const rows = new Map();
  let observationErrors = 0;
  function wrapStream(streamFn) {
    return (model, context, options = {}) => {
      const callId = idFactory();
      let attempt = 0;
      const fetchImpl = options.fetch || globalThis.fetch;
      const fetch = async (input, init) => {
        const start = now();
        const row = {schema_version: 1, request_id: idFactory(), call_id: callId, phase: "started",
          run_id: token(runId), agent_id: token(agentId), model: token(model?.id),
          retry_index: attempt++, started_at: new Date(start).toISOString(), duration_ms: null,
          http_status: null, stop_reason: null, error_class: null, provider_error_code: null,
          usage: safeProviderUsage(null), cost: null, cost_currency: null};
        let settled = false;
        let networkStart = null;
        const signal = init?.signal ?? input?.signal ?? options.signal;
        const finish = (reason) => {
          if (settled) return;
          settled = true;
          if (reason) row.stop_reason = reason;
          row.duration_ms = networkStart === null ? null : Math.max(0, now() - networkStart);
          if (signal?.aborted) {
            row.stop_reason = "aborted";
            row.error_class = "provider_aborted";
          } else if (row.http_status >= 400 || row.provider_error_code) {
            row.error_class = classifyProviderError(row.http_status, row.provider_error_code);
          }
          row.phase = "finished";
          const safe = structuredClone(row);
          rows.set(row.request_id, safe);
          try { Promise.resolve(onRecord?.(structuredClone(safe))).catch(() => { observationErrors++; }); }
          catch { observationErrors++; }
        };
        // Persist a request intent before touching the network. This proves an
        // intended request, not that the provider accepted or billed it.
        rows.set(row.request_id, structuredClone(row));
        try { if (onRequest) await onRequest(structuredClone(row)); }
        catch (error) {
          row.phase = "not_sent";
          row.error_class = "provider_observation_failed";
          rows.set(row.request_id, structuredClone(row));
          observationErrors++;
          throw error;
        }
        let response;
        networkStart = now();
        try { response = await fetchImpl(input, init); }
        catch (error) {
          row.error_class = classifyProviderError(null, null, signal?.aborted || error?.name === "AbortError");
          finish(row.error_class === "provider_aborted" ? "aborted" : "transport_error");
          throw error;
        }
        row.http_status = response.status;
        let providerDone = false;
        const parse = (data) => {
          if (data === "[DONE]") { providerDone = true; return; }
          if (!data) return;
          try {
            const payload = JSON.parse(data);
            const result = payload.response ?? payload;
            const usage = result.usage ?? result.choices?.[0]?.usage;
            if (usage) row.usage = safeProviderUsage(usage);
            if (number(usage?.cost) !== null) row.cost = usage.cost;
            if (typeof usage?.cost_currency === "string" && /^[A-Z]{3}$/.test(usage.cost_currency)) row.cost_currency = usage.cost_currency;
            const reason = result.choices?.[0]?.finish_reason ?? result.status;
            if (STOP_REASONS.has(reason)) row.stop_reason = reason;
            const code = providerErrorCode(result);
            if (code) row.provider_error_code = code;
            // Only an explicit amount and currency; never use configured prices.
          } catch { /* non-JSON event or malformed response: provider handles it */ }
        };
        if (!response.body) { finish(response.ok ? null : "http_error"); return response; }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const sse = response.headers.get("content-type")?.includes("text/event-stream");
        let pending = "";
        let overflow = false;
        function inspect(chunk, final = false) {
          const text = decoder.decode(chunk, {stream: !final});
          if (overflow) return;
          pending += text;
          if (sse) {
            let lineEnd;
            while ((lineEnd = pending.indexOf("\n")) >= 0) {
              const line = pending.slice(0, lineEnd).replace(/\r$/, "");
              pending = pending.slice(lineEnd + 1);
              if (line.startsWith("data:")) parse(line.slice(5).trim());
            }
          }
          // Bounded in-memory parsing only; oversized text never becomes a row.
          if (pending.length > 4 * 1024 * 1024) { pending = ""; overflow = true; }
          if (final && pending) { parse(sse ? pending.replace(/^data:\s*/, "") : pending); pending = ""; }
        }
        const body = new ReadableStream({
          async pull(controller) {
            try {
              const next = await reader.read();
              if (next.done) { inspect(undefined, true); finish(response.ok ? null : "http_error"); controller.close(); }
              else { inspect(next.value); controller.enqueue(next.value); }
            } catch (error) {
              row.error_class = classifyProviderError(null, null, signal?.aborted || error?.name === "AbortError");
              finish(row.error_class === "provider_aborted" ? "aborted" : "transport_error");
              controller.error(error);
            }
          },
          async cancel(reason) {
            if (!providerDone) row.error_class = "provider_aborted";
            finish(providerDone ? null : "aborted"); await reader.cancel(reason);
          },
        });
        return new Response(body, {status: response.status, statusText: response.statusText, headers: response.headers});
      };
      return streamFn(model, context, {...options, fetch});
    };
  }
  return {wrapStream, records: () => structuredClone([...rows.values()]), summary: () => ({...summarizeProviderRequests([...rows.values()]), observation_errors: observationErrors})};
}
