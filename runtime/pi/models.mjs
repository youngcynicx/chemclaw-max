import { createModels, createProvider, fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { openAIResponsesApi } from "@earendil-works/pi-ai/api/openai-responses.lazy";
import { createProviderObserver } from "./provider-observability.mjs";

const GLM53_EFFORTS = ["low", "high", "max"];
const EFFORTS = ["auto", "off", "minimal", "low", "medium", "high", "xhigh", "max"];
const FORMATS = ["auto", "none", "openai", "zai", "deepseek", "qwen", "openrouter"];
const LOOPBACK = ["localhost", "127.0.0.1", "[::1]"];
function positiveInteger(env, name, fallback) {
  const value = env[name];
  if (value === undefined) return fallback;
  if (typeof value !== "string" || !/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) {
    throw new Error(`${name} must be a positive integer`);
  }
  return Number(value);
}

export function modelSettings(env = process.env) {
  if (!env.PI_BASE_URL || !env.PI_MODEL) throw new Error("PI_BASE_URL / PI_MODEL 未设置");
  const protocol = env.PI_PROTOCOL === "openai-chat" ? "openai-completions" : env.PI_PROTOCOL || "openai-completions";
  if (!["openai-completions", "openai-responses"].includes(protocol)) {
    throw new Error("PI_PROTOCOL must be openai-completions or openai-responses");
  }
  let url;
  try { url = new URL(env.PI_BASE_URL); } catch { throw new Error("PI_BASE_URL must be a valid URL"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash
      || (url.protocol === "http:" && !LOOPBACK.includes(url.hostname))) {
    throw new Error("PI_BASE_URL must use HTTPS (or loopback HTTP) without credentials/query/fragment");
  }
  let path = url.pathname.replace(/\/+$/, "");
  for (const [suffix, expected] of [["/chat/completions", "openai-completions"], ["/responses", "openai-responses"]]) {
    if (path.endsWith(suffix)) {
      if (protocol !== expected) throw new Error("PI_PROTOCOL does not match the full endpoint path");
      path = path.slice(0, -suffix.length);
      break;
    }
  }
  const baseUrl = `${url.origin}${path}`;
  const modelId = env.PI_MODEL;
  const glm53Native = ["open.bigmodel.cn", "api.z.ai"].includes(url.hostname.toLowerCase())
    && /^glm-5\.3(?:-[a-z0-9][a-z0-9._-]*)?$/i.test(modelId);
  if (glm53Native && protocol !== "openai-completions") {
    throw new Error("Native GLM-5.3 requires the openai-completions protocol");
  }
  const maxTokens = positiveInteger(env, "PI_MAX_TOKENS", 16384);
  const contextWindow = positiveInteger(env, "PI_CONTEXT_WINDOW", 128000);
  if (maxTokens > contextWindow) throw new Error("PI_MAX_TOKENS must not exceed PI_CONTEXT_WINDOW");
  const requestedEffort = env.PI_REASONING_EFFORT ?? "auto";
  const reasoningFormat = env.PI_REASONING_FORMAT ?? "auto";
  if (!EFFORTS.includes(requestedEffort)) throw new Error(glm53Native
    ? "GLM-5.3 PI_REASONING_EFFORT must be low, high, or max; thinking cannot be disabled"
    : "Unsupported PI_REASONING_EFFORT");
  if (!FORMATS.includes(reasoningFormat)) throw new Error("Unsupported PI_REASONING_FORMAT");
  let effectiveFormat = reasoningFormat;
  let reasoningEffort = requestedEffort === "auto" ? undefined : requestedEffort;
  if (glm53Native) {
    reasoningEffort ??= "low";
    if (!GLM53_EFFORTS.includes(reasoningEffort)) {
      throw new Error("GLM-5.3 PI_REASONING_EFFORT must be low, high, or max; thinking cannot be disabled");
    }
    if (!["auto", "zai"].includes(reasoningFormat)) throw new Error("Native GLM-5.3 requires auto/zai reasoning format");
    effectiveFormat = "zai";
  } else {
    if (protocol === "openai-responses" && !["auto", "none", "openai"].includes(reasoningFormat)) {
      throw new Error("Responses protocol supports only auto/none/openai reasoning format");
    }
    if (reasoningEffort !== undefined) {
      if (reasoningFormat === "none") throw new Error("none reasoning format requires auto effort");
      if (reasoningFormat === "auto") {
        const knownOpenAI = url.hostname === "api.openai.com" && /^(?:o[134](?:-|$)|gpt-5(?:[.-]|$))/.test(modelId);
        if (protocol === "openai-responses" || knownOpenAI) effectiveFormat = "openai";
        else throw new Error("PI_REASONING_EFFORT has no supported capability mapping; choose PI_REASONING_FORMAT explicitly");
      }
      if (effectiveFormat === "qwen" && reasoningEffort !== "off") {
        throw new Error("qwen reasoning format maps only the thinking switch; use auto or off effort");
      }
    }
  }
  if (!env.PI_API_KEY && !LOOPBACK.includes(url.hostname)) throw new Error("Remote model endpoint requires PI_API_KEY");
  const maxTokensField = url.hostname === "api.openai.com" && /^(?:o[134](?:-|$)|gpt-5(?:[.-]|$))/.test(modelId)
    ? "max_completion_tokens" : "max_tokens";
  return {baseUrl, modelId, api:protocol, maxTokens, contextWindow,
    reasoningEffort, reasoningFormat, effectiveFormat, maxTokensField,
    profile:glm53Native ? "glm-5.3-native" : "compatible"};
}

export function reasoningPayload(settings) {
  const {reasoningEffort:effort, effectiveFormat:fmt, api} = settings;
  if (effort === undefined) return {};
  const value = effort === "off" ? "none" : effort;
  if (api === "openai-responses" || fmt === "openrouter") return {reasoning:{effort:value}};
  if (fmt === "openai") return {reasoning_effort:value};
  if (fmt === "qwen") return {enable_thinking:false};
  return {thinking:{type:effort === "off" ? "disabled" : "enabled"},
    ...(effort === "off" ? {} : {reasoning_effort:value})};
}

export function realModel(env = process.env, observation = {}) {
  const settings = modelSettings(env);
  const {baseUrl, modelId, api, maxTokens, contextWindow, reasoningEffort, effectiveFormat, maxTokensField} = settings;
  const apiKey = env.PI_API_KEY || "";
  // Pi's OpenAI SDK requires a key to construct a client. For authenticated-free
  // loopback servers only, use an internal sentinel and remove Authorization at
  // the final fetch boundary. It is never sent, not even as an empty Bearer.
  const sdkKey = apiKey || "chemclaw-local-no-auth";
  const active = reasoningEffort !== undefined;
  const provider = createProvider({
    id: "chemclaw-lab", name: "ChemClaw-Lab model", baseUrl,
    auth: {apiKey:{name:"ChemClaw-Lab model API key",
      resolve:async()=>({auth:{apiKey:sdkKey},source:"ChemClaw-Lab model settings"})}},
    models: [{
      id:modelId, name:modelId, api, provider:"chemclaw-lab", baseUrl,
      reasoning:active, input:["text"],
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0}, contextWindow,maxTokens,
      compat:{thinkingFormat:["auto","none"].includes(effectiveFormat) ? "openai" : effectiveFormat,
        supportsReasoningEffort:active, maxTokensField,
        supportsDeveloperRole:new URL(baseUrl).hostname === "api.openai.com",
        supportsStore:false},
      thinkingLevelMap:{off:"none", minimal:"minimal",low:"low",medium:"medium",high:"high",xhigh:"xhigh",max:"max"},
    }],
    api:api === "openai-responses" ? openAIResponsesApi() : openAICompletionsApi(),
  });
  const models = createModels();
  models.setProvider(provider);
  const model = models.getModel("chemclaw-lab", modelId);
  if (!model) throw new Error("Configured model could not be registered");
  const streamFn = (selectedModel, context, options = {}) => {
    const forwarded = {...options, maxTokens:options.maxTokens ?? maxTokens};
    if (active) forwarded.reasoning = reasoningEffort;
    else delete forwarded.reasoning;
    const callerHook = options.onPayload;
    forwarded.onPayload = async (payload, payloadModel) => {
      const replacement = await callerHook?.(payload, payloadModel);
      const finalPayload = replacement === undefined ? payload : replacement;
      if (!finalPayload || typeof finalPayload !== "object" || Array.isArray(finalPayload)) {
        throw new Error("Model payload hook must return an object or undefined");
      }
      const {reasoning_effort, reasoning, thinking, enable_thinking, ...other} = finalPayload;
      return {...other, ...reasoningPayload(settings)};
    };
    if (!apiKey) {
      const fetchImpl = options.fetch || globalThis.fetch;
      forwarded.fetch = async (input, init) => {
        const request = input instanceof Request && !init ? input : new Request(input, init);
        const headers = new Headers(request.headers);
        headers.delete("authorization");
        return fetchImpl(new Request(request, {headers}));
      };
    }
    return models.streamSimple(selectedModel, context, forwarded);
  };
  const providerObserver = createProviderObserver(observation);
  return {models, model, streamFn: providerObserver.wrapStream(streamFn), settings, providerObserver};
}

// Offline smoke only. Production planning is performed by the selected model.
export function mockModel(role, roles = []) {
  const faux = fauxProvider({ provider: "chemclaw-mock", models: [{
    id: "mock-agent", name: "Mock agent", reasoning: false,
    input: ["text"], contextWindow: 128000, maxTokens: 8192,
  }] });
  const scripted = (context) => {
    const messages = context?.messages || [];
    const last = messages.at(-1) || {};
    if (last.role === "user") {
      if (role === "orchestrator" && roles.length) {
        const query = roles.find((item) => item.agent_id === "b_query") || roles[0];
        return fauxAssistantMessage(fauxToolCall("agent.delegate", {
          agent_id: query.agent_id, message: "请基于任务上下文检索记录并返回带来源的回答。",
        }));
      }
      const call = role === "b_capture"
        ? ["evidence.page.read", { page_id: "" }]
        : role === "alice_inventory"
          ? ["inventory.query.list", { kind: "chemicals" }]
          : ["query.events.search", { project_ids: [], text: "" }];
      return fauxAssistantMessage(fauxToolCall(...call));
    }
    if (last.role === "toolResult" && last.toolName === "agent.respond") {
      return fauxAssistantMessage(fauxText("离线模拟：任务结果已返回。"));
    }
    return fauxAssistantMessage(fauxToolCall("agent.respond", {
      status:"completed",
      text:`离线模拟：已收到工具回执，请以其中的实际状态为准：${JSON.stringify(last.content ?? last).slice(0, 1200)}`,
    }));
  };
  faux.setResponses(Array.from({ length: 400 }, () => scripted));
  return { model: faux.getModel(), streamFn: (model, context, options) =>
    faux.provider.streamSimple(model, context, options) };
}
