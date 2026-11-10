/** Model schemes contain only allowlisted non-secret fields. */
export const MODEL_FIELDS = ['baseUrl','model','protocol','visionBaseUrl','visionModel','reasoningEffort','reasoningFormat','maxTokens','contextWindow'];
export const MODEL_DEFAULTS = {baseUrl:'https://open.bigmodel.cn/api/paas/v4',model:'glm-5.3-flash',protocol:'openai-chat',visionBaseUrl:'',visionModel:'glm-4v-flash',reasoningEffort:'auto',reasoningFormat:'auto',maxTokens:16384,contextWindow:128000};
export const MODEL_TEMPLATES = [
  {name:'GLM 当前端点', ...MODEL_DEFAULTS},
  {name:'兼容 Chat Completions',...MODEL_DEFAULTS,baseUrl:'',model:'',visionModel:'',reasoningEffort:'auto',reasoningFormat:'auto'},
  {name:'Responses API',...MODEL_DEFAULTS,baseUrl:'https://api.openai.com/v1',model:'',protocol:'openai-responses',visionModel:'',reasoningEffort:'auto',reasoningFormat:'openai'},
  {name:'本机兼容服务',...MODEL_DEFAULTS,baseUrl:'http://127.0.0.1:11434/v1',model:'',visionModel:'',reasoningFormat:'none'},
];
export function publicModelSettings(input = {}) {
  const result = {};
  for (const field of MODEL_FIELDS) {
    if (!(field in input)) continue;
    if (['maxTokens','contextWindow'].includes(field)) {
      const value = Number(input[field]); if (Number.isSafeInteger(value) && value > 0) result[field] = value;
    } else if (typeof input[field] === 'string') {
      let value = input[field].slice(0,2048);
      if (['baseUrl','visionBaseUrl'].includes(field) && value.trim()) {
        try { const url = new URL(value); if (url.username || url.password || url.search || url.hash) value = ''; } catch { value = ''; }
      }
      result[field] = value;
    }
  }
  return result;
}
export function modelPayload(formData) {
  for (const key of ['baseUrl','visionBaseUrl']) {
    const value = String(formData.get(key) || '').trim();
    if (!value) continue;
    let url; try { url = new URL(value); } catch { throw new Error('模型地址须为完整的 http 或 https Base URL。'); }
    if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Base URL 不能包含用户名、密码、查询参数或片段；请在密钥栏单独填写密钥。');
  }
  const payload = publicModelSettings(Object.fromEntries(MODEL_FIELDS.map(key => [key,formData.get(key)])));
  for (const key of ['apiKey','visionApiKey']) payload[key] = String(formData.get(key) || '').trim();
  if (!payload.baseUrl?.trim() || !payload.model?.trim()) throw new Error('请填写主模型地址和模型 ID。');
  if (!payload.maxTokens || !payload.contextWindow) throw new Error('输出上限和上下文窗口须为正整数。');
  if (payload.maxTokens > payload.contextWindow) throw new Error('输出上限不能大于上下文窗口。');
  return payload;
}
export function modelSchemes(storage, key = 'chemclaw.model-schemes.v1') {
  try {
    const values = JSON.parse(storage.getItem(key) || '[]');
    return Array.isArray(values) ? values.slice(0,20).filter(value => value && typeof value.name === 'string').map(value => ({name:value.name.slice(0,60),...publicModelSettings(value)})) : [];
  } catch { return []; }
}
export function saveModelScheme(storage, name, settings, key = 'chemclaw.model-schemes.v1') {
  name = String(name || '').trim().slice(0,60); if (!name) throw new Error('请给方案起一个名称。');
  const values = modelSchemes(storage,key).filter(item => item.name !== name);
  values.unshift({name,...publicModelSettings(settings)});
  storage.setItem(key,JSON.stringify(values.slice(0,20))); return values;
}
