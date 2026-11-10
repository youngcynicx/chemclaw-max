import { publicModelSettings } from './model-settings.js';
/** Same-origin local bridge client. API keys never enter browser storage. */
export class CoreClient {
  constructor() {
    this.connected = false;
    this.projectId = null;
    this.csrfToken = null;
    this.coreVersion = null;
    this.features = {};
    this.missingReadCapabilities = [];
    this.modelConfigured = false;
    this.modelName = null;
    this.modelBaseUrl = null;
    this.visionModel = null;
    this.modelConfig = null;
  }

  async _session() {
    if (this.csrfToken) return;
    let response;
    try {
      response = await fetch('/api/session', { credentials: 'same-origin', cache: 'no-store' });
    } catch {
      throw new Error('无法访问本地连接桥。请先运行随项目提供的 dev_server.py。');
    }
    let session;
    try { session = await response.json(); } catch { /* Static hosting may return index.html. */ }
    if (!response.ok || session?.bridge !== 'chemclaw_read_bridge_v1' || !session.csrfToken) {
      throw new Error('当前是静态演示预览。连接本机 Core 请在本地运行 dev_server.py，再打开本地地址。');
    }
    this.csrfToken = session.csrfToken;
    this.modelConfigured = Boolean(session.modelConfigured);
    this.modelName = session.modelName || null;
    this.visionModel = session.visionModel || null;
  }

  async _request(path, body) {
    await this._session();
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        credentials: 'same-origin', cache: 'no-store',
        headers: { 'X-ChemClaw-Session': this.csrfToken, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new Error('本地连接中断，请检查前端服务状态。');
    }
    let result;
    try { result = await response.json(); } catch { throw new Error('本地桥返回了无法识别的响应。'); }
    if (!response.ok || result.error) {
      if (response.status === 401 || result.error?.code === 'not_connected') {
        this.connected = false;
        this.projectId = null;
        this.csrfToken = null;
      }
      const error = new Error(result.error?.message || 'Core 读取失败。');
      error.code = result.error?.code || 'bridge_request_failed';
      throw error;
    }
    return result;
  }

  async connect({ endpoint, apiKey, projectId }) {
    const result = await this._request('/api/connect', { endpoint, apiKey, projectId });
    this.connected = true;
    this.projectId = result.projectId;
    this.coreVersion = result.coreVersion || null;
    this.features = result.features || {};
    this.missingReadCapabilities = result.missingReadCapabilities || [];
    return result;
  }

  async disconnect() {
    try {
      if (this.csrfToken) await this._request('/api/disconnect', {});
    } finally {
      this.connected = false;
      this.projectId = null;
      this.csrfToken = null;
      this.coreVersion = null;
      this.features = {};
      this.missingReadCapabilities = [];
      this.modelConfigured = false;
      this.modelName = null;
      this.modelBaseUrl = null;
      this.visionModel = null;
      this.modelConfig = null;
    }
    return { connected: false };
  }

  async bootstrap() {
    const result = await this._request('/api/bootstrap');
    if (result.connected) {
      this.connected = true;
      this.projectId = result.projectId;
      this.coreVersion = result.coreVersion || null;
      this.features = result.features || {};
      this.missingReadCapabilities = result.missingReadCapabilities || [];
      this.modelConfigured = Boolean(result.modelConfigured);
      this.modelName = result.modelName || null;
      this.modelBaseUrl = result.modelBaseUrl || null;
      this.visionModel = result.visionModel || null;
    }
    return result;
  }

  inventory() { return this._request('/api/core/inventory'); }
  tasks() { return this._request('/api/core/tasks'); }
  search(q) { return this._request(`/api/core/search?q=${encodeURIComponent(q)}`); }
  health() { return this._request('/api/core/health'); }
  evidence() { return this._request('/api/core/evidence'); }
  pagePreview(pageId) { return this._request(`/api/core/page/preview?page_id=${encodeURIComponent(pageId)}`); }
  ocr(pageId) { return this._request('/api/core/ocr', { pageId }); }
  proposal(proposalId) { return this._request(`/api/core/proposal?proposal_id=${encodeURIComponent(proposalId)}`); }
  review(payload) { return this._request('/api/core/review', payload); }
  samples() { return this._request('/api/core/samples'); }
  timeline(sampleId) { return this._request(`/api/core/timeline?sample_id=${encodeURIComponent(sampleId)}`); }
  event(eventId, history = false) {
    return this._request(`/api/core/event?event_id=${encodeURIComponent(eventId)}&history=${history ? 1 : 0}`);
  }
  correctEvent(payload) { return this._request('/api/core/event/correct', payload); }
  correctTranscript(payload) { return this._request('/api/core/transcript/correct', payload); }
  page(pageId) { return this._request(`/api/core/page?page_id=${encodeURIComponent(pageId)}`); }
  answer(q) { return this._request(`/api/core/answer?q=${encodeURIComponent(q)}`); }
  resources() { return this._request('/api/core/resources'); }
  bookings(status) {
    return this._request(`/api/core/bookings${status ? `?status=${encodeURIComponent(status)}` : ''}`);
  }
  providers() { return this._request('/api/core/providers'); }
  submitJob(payload) { return this._request('/api/core/jobs', payload); }
  jobs() { return this._request('/api/core/jobs'); }
  job(jobId, after = 0) { return this._request(`/api/core/jobs/${encodeURIComponent(jobId)}?after=${encodeURIComponent(after)}`); }
  cancelJob(jobId) { return this._request(`/api/core/jobs/${encodeURIComponent(jobId)}/cancel`, {}); }
  continueJob(jobId, payload) { return this._request(`/api/core/jobs/${encodeURIComponent(jobId)}/continue`, payload); }
  task({ text, attachments = [], sessionId = null }) {
    return this._request('/api/core/session/message', {
      text,
      attachments,
      ...(sessionId ? { sessionId } : {}),
    });
  }
  applyModelStatus(result) {
    this.modelConfigured = Boolean(result.configured);
    this.modelName = result.model || null;
    this.modelBaseUrl = result.baseUrl || null;
    this.visionModel = result.visionModel || null;
    this.modelConfig = {...publicModelSettings(result), configured:this.modelConfigured,
      keyConfigured:Boolean(result.keyConfigured), visionKeyConfigured:Boolean(result.visionKeyConfigured), visionTest:result.visionTest || null};
    return result;
  }
  async configureModel(payload) {
    const result = await this._request('/api/model/configure', {...publicModelSettings(payload),
      apiKey:String(payload.apiKey || ''),visionApiKey:String(payload.visionApiKey || ''),
      ...(payload.reuseApiKey === false ? {reuseApiKey:false} : {}),
      ...(payload.reuseVisionApiKey === false ? {reuseVisionApiKey:false} : {}),
    });
    this.applyModelStatus({...result,configured:result.configured ?? true});
    return result;
  }
  async clearModel() {
    const result = await this._request('/api/model/clear', {});
    this.modelConfigured = false;
    this.modelName = null;
    this.modelBaseUrl = null;
    this.visionModel = null;
    this.modelConfig = null;
    return result;
  }
  async modelStatus() { return this.applyModelStatus(await this._request('/api/model/status')); }
}
