import { projects, samples as sampleSeed, evidence as evidenceSeed, inventory, tasks as taskSeed, activity, resources } from './data.js';
import { icon } from './icons.js';
import { CoreClient } from './core-client.js';
import { renderMarkdown } from './markdown.js';
import { MODEL_DEFAULTS, MODEL_TEMPLATES, MODEL_FIELDS, publicModelSettings, modelPayload, modelSchemes, saveModelScheme } from './model-settings.js';

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const h = (v = '') => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = v => JSON.parse(JSON.stringify(v));
const client = new CoreClient();
const storageKey = 'chemclaw.synthetic-workspace.v1';
const nav = [['overview','overview','研究总览'],['knowledge','book','知识与样品'],['evidence','evidence','证据工作区'],['inventory','box','库存与物料'],['agents','agents','Agent 协作'],['resources','calendar','仪器与预约']];
let samples = clone(sampleSeed), evidence = clone(evidenceSeed), tasks = clone(taskSeed);
const state = { view: 'overview', projectId: projects[0].id, evidenceId: 'EV-026', filter: 'all', query: '', mode: 'demo', live: {}, mobile: false, editing: null, selectedField: null, versions: [], uploaded: [], chat: [], busy: false, taskChat: [], taskFiles: [], taskBusy: false, taskSessionId: null, evidenceSelected: null, evidencePage: null, evidenceProposal: null, evidencePreview: null, evidenceBusy: false, editItemKey: null };
let activeOverlay = null, lastFocus = null, searchTimer = null, searchSeq = 0, uploadedFile = null, contextEpoch = 0;
// Job identity is recovered from Core under its authenticated principal/project.
// Credentials, attachment bytes, and task text are never persisted by this UI.
Object.assign(state, {taskJobs: [], taskJobId: null, taskDraft: '', taskPending: null, taskPollError: '', taskCancelBusy: false, taskContinuePending:null});
let taskPollTimer = null, taskPollGeneration = 0;
let detailRequestSeq = 0, evidenceDetailSeq = 0;
Object.assign(state, {liveSampleId:null, correctingEvent:null, correctingTranscript:null});

try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  if (saved?.schema === 1) {
    for (const item of saved.evidence || []) {
      const target = evidence.find(e => e.id === item.id);
      if (!target || !Array.isArray(item.fields)) continue;
      for (const f of item.fields) {
        const original = target.fields.find(x => x.id === f.id);
        if (original && typeof f.value === 'string' && f.value.length <= 300) original.value = f.value;
      }
      if (item.status === 'confirmed') target.status = 'confirmed';
    }
    state.versions = Array.isArray(saved.versions) ? saved.versions.slice(-60) : [];
  }
} catch { /* Browser storage is optional. */ }

const project = () => projects.find(p => p.id === state.projectId) || projects[0];
const projectSamples = () => samples.filter(s => s.projectId === state.projectId);
const projectEvidence = () => evidence.filter(e => projectSamples().some(s => s.id === e.sampleId));
const projectTasks = () => tasks.filter(t => t.projectId === state.projectId || t.scope === 'lab_shared');
const pending = () => projectEvidence().filter(e => e.status === 'pending');
const lowStock = () => inventory.filter(i => i.amount < i.threshold);
const btn = (action, label, type = 'secondary', glyph = '', extra = '') => `<button type="button" class="btn btn-${type}" data-action="${action}" ${extra}>${glyph ? icon(glyph,15) : ''}${label}</button>`;
const badge = (text, color = '', dot = false) => `<span class="pill ${color}">${dot ? '<i class="status-dot"></i>' : ''}${h(text)}</span>`;
const closeButton = () => `<button class="icon-btn" data-action="close" aria-label="关闭">${icon('close',19)}</button>`;
const taskStatus = t => t.status === 'done' ? ['已完成','green'] : t.status === 'waiting' ? ['待你处理','orange'] : ['进行中','blue'];
const sampleBadge = s => badge(s.status, s.status === '待复核' ? 'orange' : 'green', true);

function persist() {
  try { localStorage.setItem(storageKey, JSON.stringify({schema:1,evidence:evidence.filter(e=>evidenceSeed.some(x=>x.id===e.id)),versions:state.versions})); }
  catch { toast('浏览器存储不可用，当前修改仅在本次打开期间保留。','alert'); }
}
function syncEvidenceState() {
  for (const s of samples) {
    const related=evidence.filter(e=>e.sampleId===s.id);
    if(related.length)s.status=related.some(e=>e.status==='pending')?'待复核':'进行中';
    const edits=state.versions.filter(v=>related.some(e=>e.id===v.id));
    s.events=s.events.filter(e=>!e.id.startsWith('local-revision-'));
    if(edits.length){const latest=edits.at(-1),e=related.find(e=>e.id===latest.id);s.events.unshift({id:'local-revision-'+latest.at,date:latest.at.slice(0,10),time:new Date(latest.at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}),title:e.status==='confirmed'?'本地演示核对已完成':'候选字段有本地修订',body:'原始页面保持 v1。以下为当前浏览器的字段版本；历史事件继续保留。未写入 Core，未扣减库存。',type:'review',sourceId:e.id,fields:e.fields.map(f=>({label:f.label,value:f.value+' '+f.unit}))});s.summary=e.status==='confirmed'?'此记录已在本浏览器完成演示核对。当前字段与修订版本可回查；原始记录继续保留。':'存在本地字段修订，等待再次对照原页核实。';}
  }
  const e=evidence.find(e=>e.id==='EV-026'),t=tasks.find(t=>t.id==='TASK-1042');
  if(e&&t){const base=taskSeed.find(t=>t.id==='TASK-1042');Object.assign(t,clone(base));if(e.status==='confirmed'){t.status='done';t.progress=100;t.steps.forEach(s=>s.state='done');t.output='本浏览器中的演示核对已完成。字段当前版本与原页分别保留；未写入 Core，未调整库存。';}}
}
function resetContext() {contextEpoch++;detailRequestSeq++;stopJobPolling();state.chat=[];state.live={};state.taskChat=[];state.taskFiles=[];state.taskSessionId=null;state.taskBusy=false;state.taskJobs=[];state.taskJobId=null;state.taskPending=null;state.taskDraft='';state.taskPollError='';state.taskCancelBusy=false;state.taskContinuePending=null;state.liveSampleId=null;state.correctingEvent=null;state.correctingTranscript=null;}
function toast(message, glyph = 'check') {
  const el = document.createElement('div'); el.className = 'toast';
  el.innerHTML = `${icon(glyph,18)}<span>${h(message)}</span>`; $('#toast-root').append(el);
  setTimeout(() => el.remove(), 4800);
}

function render() {
  const live = state.mode === 'live';
  $('#app').innerHTML = `
    <div class="mobile-scrim ${state.mobile?'open':''}" data-action="mobile-close"></div>
    <aside class="sidebar ${state.mobile?'open':''}" aria-label="主导航">
      <a class="brand" href="#overview" aria-label="ChemClaw 研究总览"><img class="brand-mark" src="./assets/favicon.svg" width="30" height="30" alt=""><span>ChemClaw<span style="color:#cbe58d">.</span></span></a>
      <p class="brand-caption">THE SHARED LAB</p>
      <button class="workspace-switch" data-action="project"><span class="workspace-avatar">${live?'C':project().code[0]}</span><span class="workspace-name">${live?h(client.projectId):h(project().name)}<small>${live?'CORE / TASK SESSION':'RESEARCH WORKSPACE'}</small></span>${icon('chevronDown',12)}</button>
      <p class="nav-label">WORKSPACE</p>
      <nav class="nav">${nav.map(([id,glyph,title])=>`<a class="nav-item ${state.view===id?'active':''}" href="#${id}" ${state.view===id?'aria-current="page"':''} title="${title}">${icon(glyph,18)}<span>${title}</span>${id==='evidence'&&!live&&pending().length?`<small class="nav-count">${pending().length.toString().padStart(2,'0')}</small>`:''}</a>`).join('')}</nav>
      <div class="nav-spacer"></div>
      <button class="nav-item" data-action="settings" title="连接与设置">${icon('settings',18)}<span>连接与设置</span></button>
      <div class="sidebar-foot">
        <div class="connection-card"><span class="eyebrow">${live?'LOCAL CORE':'YOUR LAB, CONNECTED'}</span><div class="connection-title"><i class="status-dot" style="color:#b4d779"></i>${live?'已连接 · 受控任务':'演示工作区'}</div><p>${live?'读取研究上下文，委托 Agent 处理任务。':'从一页纸开始，让人和 Agent\n共享同一份研究上下文。'}</p><button class="btn" data-action="settings">${live?'查看连接':'连接你的 Core'} ${icon('arrow',13)}</button></div>
        <div class="sidebar-user"><span class="avatar">研</span><span>研究者<small>${live?'本机连接会话':'合成示例 · 本地交互'}</small></span><button class="icon-btn" data-action="help" aria-label="使用说明">${icon('help',15)}</button></div>
      </div>
    </aside>
    <div class="shell">
      <header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" data-action="mobile-open" aria-label="打开导航">${icon('menu',19)}</button><span>工作空间</span><span class="crumb-divider">/</span><strong>${nav.find(n=>n[0]===state.view)?.[2] || '研究总览'}</strong></div><div class="topbar-actions"><button class="global-search" data-action="search" aria-label="全局搜索">${icon('search',14)}<span>搜索知识、样品、物料</span><kbd>⌘ K</kbd></button><button class="mode-badge" data-action="settings"><i class="status-dot"></i>${live?'CORE · 已连接':'演示模式'}</button><button class="icon-btn notification-wrap" data-action="activity" aria-label="工作动态">${icon('bell',18)}</button><span class="avatar" aria-label="研究者">研</span></div></header>
      <main class="main view-enter" id="main-content" tabindex="-1">${viewContent()}<footer class="app-footer"><span>${live?'本机 Core 读取 · 当前项目明确授权':'合成示例数据 · 仅用于体验交互，非真实实验记录'}</span><span>CHEMCLAW / HUMAN + AGENT</span></footer></main>
    </div><button class="assistant-fab" data-action="assistant">${icon('spark',17)}问问研究助手 ${icon('arrow',14)}</button>`;
  syncResponsiveNavigation();
  initTaskComposer();
}
function syncResponsiveNavigation(){
  const mobile=matchMedia('(max-width:640px)').matches,aside=$('.sidebar');
  if(aside){aside.inert=mobile&&!state.mobile;aside.setAttribute('aria-hidden',String(mobile&&!state.mobile));}
  const menu=$('.mobile-menu');if(menu)menu.setAttribute('aria-expanded',String(state.mobile));
}

function pageHeading(title, subtitle, actions = '') {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${actions}</div></div>`;
}
function taskFilesHtml() {
  if (!state.taskFiles.length) return '';
  return state.taskFiles.map((f, i) => `<span class="task-file-chip">${icon('file',13)}${h(f.name)}<button type="button" data-action="task-remove-file" data-index="${i}" aria-label="移除 ${h(f.name)}">${icon('close',11)}</button></span>`).join('');
}

function taskCapabilityIssue() {
  if (client.modelConfigured && !client.features.agentRuntime) {
    return '当前 Core 缺少自主 Agent 会话能力。请更新 Core 并加载主 Agent；也可清除模型后使用基础模式。';
  }
  if (!client.modelConfigured && !client.features.task) {
    return '当前连接缺少基础任务能力或成员权限，请检查 Core 版本与项目身份。';
  }
  return '';
}

function taskState(status) {
  const labels = {
    completed: ['已完成', 'green'], applied: ['已执行', 'green'],
    needs_review: ['待你核对', 'orange'], clarification_needed: ['需要补充信息', 'orange'],
    awaiting_confirmation: ['待你确认', 'orange'], awaiting_approval: ['待审批', 'orange'],
    partial: ['部分完成', 'orange'], failed: ['处理失败', 'orange'],
    rejected: ['未执行', 'orange'], running: ['进行中', 'blue'], queued: ['排队中', 'blue'],
    cancelling: ['正在停止', 'orange'], cancelled: ['已停止', 'orange'], interrupted: ['执行已中断', 'orange'],
  };
  return labels[status] || [status || '', ''];
}

function taskReceiptsHtml(message) {
  const events = Array.isArray(message.events) ? message.events.slice(0, 80) : [];
  const delegations = Array.isArray(message.delegations) ? message.delegations.slice(0, 20) : [];
  if (!events.length && !delegations.length && !message.runId) return '';
  const eventLabels = { tool_start: '调用工具', tool_end: '工具返回', tool_result: '工具返回', tool_call_requested: '调用工具', tool_result_received: '工具返回', delegation: '委派任务', delegation_started: '开始委派', delegation_completed: '委派结束', assistant_message: '助手已返回', agent_start: '开始处理', agent_end: '处理结束', error: '执行异常' };
  return `<details class="task-receipts"><summary>查看执行回执${delegations.length ? ` · ${delegations.length} 次委派` : ''}${events.length ? ` · ${events.length} 条记录` : ''}</summary>
    ${message.runId ? `<small class="task-objects">运行 ${h(message.runId)}</small>` : ''}
    ${delegations.length ? `<ul class="task-delegations">${delegations.map(d => {
      const [label, color] = taskState(d.status);
      return `<li><strong>${h(d.agent || d.agent_id || '专业 Agent')}</strong>${label ? badge(label, color) : ''}${d.text ? `<p>${h(d.text)}</p>` : ''}${d.run_id ? `<small>${h(d.run_id)}</small>` : ''}</li>`;
    }).join('')}</ul>` : ''}
    ${events.length ? `<ol class="task-events">${events.map(e => {
      const [label] = taskState(e.status || (e.isError ? 'failed' : ''));
      const agent = e.agent || e.agent_id || '';
      return `<li><span>${h(agent)}${agent ? ' · ' : ''}${h(eventLabels[e.type] || e.type || '执行记录')}${e.tool ? ` · ${h(e.tool)}` : ''}${label ? ` · ${h(label)}` : ''}</span>${e.text ? `<p>${h(e.text)}</p>` : ''}</li>`;
    }).join('')}</ol>` : ''}
  </details>`;
}

function taskWarningsHtml(warnings) {
  if (!Array.isArray(warnings) || !warnings.length) return '';
  const labels = {
    agent_runtime_failed: 'Agent 运行中断，请检查执行回执后继续。',
    model_reply_failed: '模型回复失败，当前显示 Core 返回的结果。',
    model_classify_failed: '模型解析失败，当前任务使用基础规则处理。',
    parser_not_configured: '部分附件已保存原件，尚未配置对应解析器。',
  };
  return `<div class="task-warnings" role="status">${warnings.slice(0, 12).map(w => `<p>${h(labels[w] || w)}</p>`).join('')}</div>`;
}

function knowledgeDeliveryHtml(delivery) {
  if (!delivery || typeof delivery !== 'object') return '';
  const labels = {verified:'知识已写入并读回',saved_unverified:'知识已写入，尚未读回',read_only:'已读取已有知识',not_written:'本轮没有知识写入回执'};
  const color = delivery.state === 'verified' || delivery.state === 'read_only' ? 'green' : 'orange';
  return `<div class="knowledge-delivery">${badge(labels[delivery.state] || '知识交付回执',color)}<p>${h(delivery.notice || '')}</p>${Array.isArray(delivery.pending) && delivery.pending.length ? `<details><summary>查看未知项与执行备注</summary><pre>${h(JSON.stringify(delivery.pending,null,2))}</pre></details>` : ''}</div>`;
}
function taskMessagesHtml() {
  if (!state.taskChat.length) return `<div class="task-empty">${icon('spark',19)}<span>交代目标，Agent 会选择工具、协调专业角色，并返回结果和待办。</span></div>`;
  return state.taskChat.map((m, messageIndex) => {
    const isUser = m.role === 'user';
    const [statusLabel, color] = taskState(m.status);
    const objects = (Array.isArray(m.objects) ? m.objects : []).slice(0, 8).map(o => o.kind === 'evidence_page'
      ? `<button class="btn btn-quiet" data-action="task-evidence" data-id="${h(o.id)}">查看原件与核对 ${h(o.id)} ${icon('arrow',11)}</button>`
      : `<span>${h(o.kind)}:${h(o.id)}</span>`).join(' ');
    const modelBadge = m.modelUsed ? badge(m.runtime === 'pi' ? 'Pi · Agent' : 'LLM', 'blue') : '';
    return `<article class="task-message ${isUser ? 'user' : ''}">
      <div class="task-message-meta">${isUser ? '你' : `${icon('agents',12)} ${h(m.agent || '研究助手')}`}${statusLabel ? badge(statusLabel, color) : ''}${modelBadge}</div>
      ${isUser ? `<p>${h(m.text).replace(/\n/g, '<br>')}</p>` : `<div class="reply-tools">${btn('task-read','展开阅读','quiet','',`data-index="${messageIndex}"`)}${btn('task-copy','复制原文','quiet','',`data-index="${messageIndex}"`)}</div><div class="markdown-body">${renderMarkdown(m.text)}</div>${knowledgeDeliveryHtml(m.knowledgeDelivery)}`}
      ${objects ? `<div class="task-objects">${objects}</div>` : ''}
      ${!isUser && m.coreText && m.coreText !== m.text ? `<small class="task-objects">Core 原文：${h(m.coreText)}</small>` : ''}
      ${!isUser ? taskWarningsHtml(m.warnings) + taskReceiptsHtml(m) : ''}
    </article>`;
  }).join('') + (state.taskBusy ? `<div class="task-running" role="status">${icon('agents',16)}${state.taskJobId ? (client.modelConfigured ? '任务由后台继续处理，刷新页面后可恢复。' : '模型已清除，任务已暂停；重新配置模型后可恢复。') : 'Agent 正在处理，请稍候。'}</div>` : '');
}

const jobActive = job => ['queued', 'running', 'cancelling'].includes(job?.status);
const asyncJobsEnabled = () => client.modelConfigured && client.features.agentJobs;
const jobHistoryEnabled = () => Boolean(client.features.agentJobs);
const canContinueJob = job => job && ['completed','partial','failed','needs_review','clarification_needed'].includes(job.status) && (job.context?.pages?.length || job.result?.capture_progress?.target_page_ids?.length);
const taskSubmitLabel = () => state.taskBusy ? (state.taskJobId && !client.modelConfigured ? '等待模型配置' : '处理中…') : '交给 Agent';
function taskJobsHtml() {
  if (!jobHistoryEnabled()) return '';
  const current = state.taskJobs.find(job => job.job_id === state.taskJobId);
  return `<div class="task-job-toolbar"><strong>后台任务</strong>${btn('task-refresh','刷新任务','quiet')}${btn('task-new','新会话','quiet')}${current && jobActive(current) ? btn('task-cancel', state.taskCancelBusy ? '正在请求停止…' : '停止任务', 'secondary', '', state.taskCancelBusy ? 'disabled' : '') : ''}${canContinueJob(current) ? btn('task-continue', state.taskContinuePending ? '重试继续整理' : '继续整理未完成项', 'secondary', '', state.taskBusy || !asyncJobsEnabled() || state.taskPending ? 'disabled' : '') : ''}</div>
    <div class="task-job-list">${state.taskJobs.slice(0, 20).map(job => { const [label, color] = taskState(job.status); return `<button type="button" class="task-job ${job.job_id === state.taskJobId ? 'selected' : ''}" data-action="task-job" data-id="${h(job.job_id)}"><span>${h((job.text || '附件任务').slice(0, 70))}</span>${badge(label,color)}</button>`; }).join('') || '<small>还没有后台任务。</small>'}</div>
    ${state.taskPending ? `<div class="task-warnings" role="status">提交尚未确认，可重试同一请求。${btn('task-retry','重试提交','quiet','',state.taskBusy || !asyncJobsEnabled() ? 'disabled' : '')}</div>` : ''}
    ${state.taskPollError ? `<div class="task-warnings" role="status">${h(state.taskPollError)} ${btn('task-refresh','重新连接','quiet')}</div>` : ''}`;
}

function taskComposer() {
  const live = state.mode === 'live';
  const capabilityIssue = live ? taskCapabilityIssue() : '';
  return `<section class="task-composer" id="task-composer" aria-label="统一任务会话">
    <div class="task-composer-head">
      <div><span class="eyebrow">UNIFIED TASK SESSION</span><h2>交代目标，让 Agent 协作完成。</h2><p>${live ? (client.modelConfigured ? '主 Agent 自主选择工具、委派专业角色，再根据执行结果继续处理；需要你核对时会明确提出。' : '查询记录、记录库存取用、拖入实验记录，或说明预约需求；配置模型后可启用自主 Agent 协作。') : '整理实验记录、关联样品、检查资料缺口；连接 Core 并配置模型后交给 Agent。'}</p></div>
      ${badge(live ? (capabilityIssue ? 'CORE · 缺少会话能力' : client.modelConfigured ? 'PI · ' + (client.modelName || '已配置') : 'CORE · 基础模式') : '演示模式 · 请先连接', live && !capabilityIssue ? 'green' : 'orange')}
    </div>
    <form data-form="task" class="task-form" autocomplete="off">
      <label class="task-drop" id="task-drop" for="task-file">
        ${icon('upload',22)}
        <span><strong>拖入实验记录，或点击选择文件</strong><small>支持图片、PDF、Office、CSV/JSON、CIF/PDB、序列与谱图等常见科研格式；文本自动转写，其他格式保存原件</small></span>
        <input type="file" id="task-file" multiple accept=".png,.jpg,.jpeg,.gif,.bmp,.tif,.tiff,.webp,.heic,.pdf,.txt,.md,.markdown,.csv,.tsv,.json,.jsonl,.xml,.html,.htm,.yaml,.yml,.tex,.rtf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.zip,.gz,.tar,.7z,.cif,.pdb,.mol,.sdf,.smi,.fasta,.fa,.fastq,.fq,.vcf,.bam,.mzml,.mzxml,.jdx,.dx,.fcs,.cdf,.nc,.h5,.hdf5,.ab1,.spc,.raw,.dta,.wiff,.baf,image/*,application/pdf,text/*">
      </label>
      <div class="task-files" id="task-files">${taskFilesHtml()}</div>
      <div class="task-input-row">
        <textarea name="task-text" id="task-text" rows="3" maxlength="4000" placeholder="例如：整理这些实验笔记，关联已有样品，检查还缺哪些表征资料。">${h(state.taskDraft)}</textarea>
        <button class="btn btn-primary task-send" type="submit" ${state.taskBusy || capabilityIssue ? 'disabled' : ''}>${icon('send',16)}${taskSubmitLabel()}</button>
      </div>
    </form>
    <div class="task-suggestions">
      <button type="button" data-action="task-suggestion" data-text="查询 SAMPLE-002 的用量">查询 SAMPLE-002 的用量</button>
      <button type="button" data-action="task-suggestion" data-text="SAMPLE-002 实际取用 8 g 氯化钠">记录一次实际取用</button>
      <button type="button" data-action="task-suggestion" data-text="预约 NMR-400 明天上午 9 点">预约 NMR-400</button>
      <span class="task-note">${live ? (client.modelConfigured ? '模型负责规划与协作；Core 核验权限、证据和版本。待确认事项可在同一会话继续补充。' : '当前使用基础规则；在“连接与设置”配置模型可启用 Agent。') : '连接 Core 后，可在同一会话交代目标并补充信息。'}</span>
    </div>
    ${capabilityIssue ? `<div class="task-warnings" role="status">${h(capabilityIssue)} ${btn('settings','连接与设置','quiet','settings')}</div>` : ''}
    <div id="task-job-panel">${live ? taskJobsHtml() : ''}</div>
    <div class="task-feed" id="task-feed" aria-live="polite">${taskMessagesHtml()}</div>
  </section>`;
}

function renderTaskFiles() {
  const target = $('#task-files');
  if (target) target.innerHTML = taskFilesHtml();
}

async function readTaskFiles(fileList) {
  const epoch = contextEpoch;
  const files = [...(fileList || [])];
  for (const file of files) {
    if (state.taskFiles.length >= 5) { toast('最多上传 5 个附件。', 'alert'); break; }
    if (file.size > 20 * 1024 * 1024) { toast(`${file.name} 超过 20 MB。`, 'alert'); continue; }
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
      reader.readAsDataURL(file);
    }).catch(() => null);
    if (epoch !== contextEpoch) return;
    if (!dataUrl) { toast(`无法读取 ${file.name}。`, 'alert'); continue; }
    const comma = dataUrl.indexOf(',');
    state.taskFiles.push({
      name: file.name,
      mime: file.type || 'application/octet-stream',
      data_b64: dataUrl.slice(comma + 1),
      size: file.size,
    });
  }
  renderTaskFiles();
}

function initTaskComposer() {
  const zone = $('#task-drop'), input = $('#task-file');
  if (!zone || !input) return;
  const stop = e => { e.preventDefault(); e.stopPropagation(); };
  ['dragenter', 'dragover'].forEach(name => zone.addEventListener(name, e => { stop(e); zone.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach(name => zone.addEventListener(name, e => { stop(e); zone.classList.remove('dragging'); }));
  zone.addEventListener('drop', e => readTaskFiles(e.dataTransfer?.files));
  input.addEventListener('change', () => { readTaskFiles(input.files); input.value = ''; });
}

function stopJobPolling() { taskPollGeneration++; clearTimeout(taskPollTimer); taskPollTimer = null; }
function updateTaskPanel() {
  const panel = $('#task-job-panel'), feed = $('#task-feed'), send = $('.task-send');
  if (panel) panel.innerHTML = taskJobsHtml();
  if (feed) feed.innerHTML = taskMessagesHtml();
  if (send) { send.disabled = state.taskBusy || Boolean(taskCapabilityIssue()); send.innerHTML = `${icon('send',16)}${taskSubmitLabel()}`; }
}
function resultMessage(result) {
  return {role:'assistant', agent:result.agent, intent:result.intent, status:result.status,
    text:result.text || '任务已处理。', coreText:result.core_text || null,
    objects:result.object_refs || result.data?.object_refs || [], modelUsed:Boolean(result.modelUsed),
    runtime:result.runtime, runId:result.run_id, events:result.events, delegations:result.delegations, warnings:result.warnings, knowledgeDelivery:result.knowledge_delivery};
}
function rememberJob(job) {
  const previous = state.taskJobs.find(item => item.job_id === job.job_id);
  const events = new Map((previous?.events || []).map(event => [event.seq, event]));
  for (const event of job.events || []) events.set(event.seq, event);
  const merged = {...previous, ...job, events:[...events.values()].sort((a,b) => a.seq-b.seq)};
  // Preserve server ordering while updating an older task in an existing session.
  state.taskJobs = previous ? state.taskJobs.map(item => item.job_id === job.job_id ? merged : item) : [merged, ...state.taskJobs].slice(0, 60);
  return merged;
}
function showJob(job) {
  state.taskJobId = job.job_id;
  state.taskSessionId = job.session_id;
  state.taskBusy = jobActive(job);
  const sessionJobs = state.taskJobs.filter(item => item.session_id === job.session_id).reverse();
  state.taskChat = sessionJobs.flatMap(item => {
    const messages = [{role:'user', text:item.text || '请整理所附文件。'}];
    if (item.result) messages.push(resultMessage(item.result));
    else {
      const last = item.events?.at(-1);
      const progress = jobActive(item) ? (last?.tool ? `正在使用工具 ${last.tool}。` : last?.agent_id ? `${last.agent_id} 正在处理任务。` : '任务已进入后台队列。') : item.status === 'cancelled' ? '任务已停止。已完成的操作可在回执中查看。' : '任务执行中断，请查看回执后继续。';
      messages.push({role:'assistant', status:item.status, text:progress, events:item.events});
    }
    return messages;
  });
  updateTaskPanel();
}
function pollJob(jobId) {
  stopJobPolling();
  const generation = taskPollGeneration, epoch = contextEpoch;
  const tick = async () => {
    if (generation !== taskPollGeneration || epoch !== contextEpoch || state.mode !== 'live') return;
    try {
      const previous = state.taskJobs.find(job => job.job_id === jobId);
      const response = await client.job(jobId, previous?.last_event_seq || 0);
      if (generation !== taskPollGeneration || epoch !== contextEpoch || state.mode !== 'live') return;
      const job = rememberJob(response.job);
      state.taskPollError = '';
      showJob(job);
      if (!jobActive(job)) { state.taskCancelBusy = false; updateTaskPanel(); return; }
    } catch (error) {
      if (generation !== taskPollGeneration || epoch !== contextEpoch || state.mode !== 'live') return;
      state.taskPollError = error.message + '；任务可在恢复连接后继续查看。';
      updateTaskPanel();
    }
    taskPollTimer = setTimeout(tick, state.taskPollError ? 4000 : 1200);
  };
  taskPollTimer = setTimeout(tick, 300);
}
async function restoreJobs() {
  if (state.mode !== 'live' || !jobHistoryEnabled()) return;
  const epoch = contextEpoch;
  try {
    const response = await client.jobs();
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    // Only the authenticated Core list determines which tasks are visible.
    const previous = state.taskJobs;
    state.taskJobs = [];
    for (const job of [...(response.jobs || [])].reverse()) {
      const prior = previous.find(item => item.job_id === job.job_id);
      rememberJob({...job, events:job.events?.length ? job.events : prior?.events || []});
    }
    state.taskPollError = '';
    const continued=state.taskContinuePending && state.taskJobs.find(job=>job.request_id===state.taskContinuePending.requestId && job.session_id===state.taskContinuePending.sessionId);
    if(continued) state.taskContinuePending=null;
    const selected = continued || state.taskJobs.find(job => job.job_id === state.taskJobId) || state.taskJobs[0];
    if (selected) { showJob(selected); if (jobActive(selected)) pollJob(selected.job_id); }
    else { stopJobPolling(); state.taskJobId=null; state.taskSessionId=null; state.taskChat=[]; state.taskBusy=false; updateTaskPanel(); }
  } catch (error) {
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    state.taskPollError = error.message;
    updateTaskPanel();
  }
}
async function submitBackgroundTask(payload, submittedFiles, retry = false) {
  const epoch = contextEpoch;
  const key = JSON.stringify(payload);
  if (!retry && state.taskPending?.key !== key) state.taskPending = {key, payload, submittedFiles, requestId:crypto.randomUUID()};
  const pending = state.taskPending;
  if (!pending) return;
  state.taskBusy = true; state.taskPollError = ''; updateTaskPanel();
  try {
    const response = await client.submitJob({...pending.payload, requestId:pending.requestId});
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    const job = rememberJob(response.job);
    state.taskPending = null; state.taskDraft = '';
    state.taskFiles = state.taskFiles.filter(file => !pending.submittedFiles.includes(file));
    const textarea = $('#task-text'); if (textarea) textarea.value='';
    renderTaskFiles(); showJob(job);
    if (jobActive(job)) pollJob(job.job_id);
  } catch (error) {
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    state.taskBusy=false;
    state.taskPollError=error.message;
    updateTaskPanel();
  }
}
async function cancelCurrentJob() {
  if (!state.taskJobId || state.taskCancelBusy) return;
  const epoch = contextEpoch, jobId=state.taskJobId;
  state.taskCancelBusy=true; updateTaskPanel();
  try {
    const response = await client.cancelJob(jobId);
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    const job=rememberJob(response.job);
    if (state.taskJobId === jobId) { showJob(job); if(jobActive(job)) pollJob(jobId); }
  } catch(error) { if(epoch===contextEpoch && state.mode==='live') {state.taskPollError=error.message; updateTaskPanel();} }
  finally { if(epoch===contextEpoch){state.taskCancelBusy=false;updateTaskPanel();} }
}

async function continueCurrentJob() {
  const source=state.taskJobs.find(job=>job.job_id===state.taskJobId);
  if (!canContinueJob(source) || state.taskBusy || !asyncJobsEnabled() || state.taskPending) return;
  if(state.taskContinuePending && state.taskContinuePending.sourceJobId!==source.job_id) {toast('上一次续作尚未确认，请先返回原任务刷新或重试。','alert');return;}
  const epoch=contextEpoch;
  const pending=state.taskContinuePending || {sourceJobId:source.job_id, sessionId:source.session_id, requestId:`continuation:${source.job_id}`};
  state.taskContinuePending=pending;
  state.taskBusy=true; state.taskPollError=''; updateTaskPanel();
  try {
    const response=await client.continueJob(pending.sourceJobId,{requestId:pending.requestId,sessionId:pending.sessionId});
    if(epoch!==contextEpoch || state.mode!=='live') return;
    state.taskContinuePending=null;
    const job=rememberJob(response.job);
    showJob(job);
    if(jobActive(job)) pollJob(job.job_id);
  } catch(error) {
    if(epoch!==contextEpoch || state.mode!=='live') return;
    if(['invalid_continuation_request','invalid_scope','continuation_source_active','continuation_sources_missing','continuation_transcript_missing','continuation_requires_review','continuation_scope_too_large','idempotency_conflict','model_not_configured','not_found'].includes(error.code)) state.taskContinuePending=null;
    state.taskBusy=false; state.taskPollError=error.message; updateTaskPanel();
  }
}

async function submitTask(form) {
  const textarea = $('textarea[name="task-text"]', form);
  const text = (textarea?.value || '').trim();
  if (!text && !state.taskFiles.length) { toast('请输入任务，或拖入一份记录。', 'alert'); return; }
  if (state.mode !== 'live') { toast('请先在设置中连接本机 Core。', 'link'); return; }
  const capabilityIssue = taskCapabilityIssue();
  if (capabilityIssue) { toast(capabilityIssue, 'alert'); return; }
  if (state.taskBusy) return;
  if (state.taskContinuePending) { toast('请先刷新任务或重试继续整理，确认上一次提交结果。','alert'); return; }
  const epoch = contextEpoch;
  const submittedFiles = [...state.taskFiles];
  const attachments = submittedFiles.map(({ name, mime, data_b64 }) => ({ name, mime, data_b64 }));
  const userText = text || `请整理这些文件：${attachments.map(a => a.name).join('、')}`;
  state.taskDraft = text;
  if (asyncJobsEnabled()) {
    await submitBackgroundTask({text:userText, attachments, ...(state.taskSessionId ? {sessionId:state.taskSessionId} : {})}, submittedFiles);
    return;
  }
  state.taskChat.push({ role: 'user', text: userText + (attachments.length ? `\n[附件 ${attachments.length} 个]` : '') });
  state.taskBusy = true;
  render();
  try {
    const result = await client.task({
      text: userText,
      attachments,
      sessionId: state.taskSessionId,
    });
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    state.taskSessionId = result.session_id || state.taskSessionId;
    state.taskDraft = '';
    state.taskChat.push({
      role: 'assistant',
      agent: result.agent,
      intent: result.intent,
      status: result.status,
      text: result.text || '任务已处理。',
      coreText: result.core_text || null,
      objects: result.object_refs || result.data?.object_refs || [],
      modelUsed: Boolean(result.modelUsed),
      runtime: result.runtime,
      runId: result.run_id,
      events: result.events,
      delegations: result.delegations,
      warnings: result.warnings,
      knowledgeDelivery: result.knowledge_delivery,
    });
    const failed = Array.isArray(result.data?.failed_attachments) ? result.data.failed_attachments : [];
    const failedIndices = new Set(failed.map(item => item.index).filter(index => Number.isInteger(index) && index >= 0 && index < submittedFiles.length));
    const addedFiles = state.taskFiles.filter(file => !submittedFiles.includes(file));
    state.taskFiles = [...submittedFiles.filter((file, index) => failedIndices.has(index)), ...addedFiles];
    if (failedIndices.size) toast(`${failedIndices.size} 个附件未保存，已保留在上传区，可重试。`, 'alert');
  } catch (err) {
    if (epoch !== contextEpoch || state.mode !== 'live') return;
    state.taskChat.push({ role: 'assistant', agent: 'orchestrator', status: 'failed', text: err.message + '\n未使用演示数据替代真实结果。' });
  } finally {
    if (epoch === contextEpoch) {
      state.taskBusy = false;
      render();
    }
  }
}

async function selectEvidencePage(pageId) {
  if (!pageId) return;
  const epoch = contextEpoch;
  const sequence = ++evidenceDetailSeq;
  state.evidenceSelected = pageId;
  state.evidencePage = null;
  state.evidenceProposal = null;
  state.evidencePreview = null;
  state.evidenceBusy = true;
  render();
  const entry = (state.live.evidence?.pages || []).find(p => p.page_id === pageId) || {};
  try {
    const [pageResult, previewResult] = await Promise.all([
      client.page(pageId),
      client.pagePreview(pageId).catch(() => null),
    ]);
    if (epoch !== contextEpoch || sequence !== evidenceDetailSeq) return;
    state.evidencePage = pageResult.page || null;
    state.evidencePreview = previewResult;
    if (entry.latest_proposal_id) {
      const proposalResult = await client.proposal(entry.latest_proposal_id);
      if (epoch !== contextEpoch || sequence !== evidenceDetailSeq) return;
      state.evidenceProposal = proposalResult.proposal || null;
    }
  } catch (err) {
    if (epoch === contextEpoch && sequence === evidenceDetailSeq) state.evidencePage = { error: err.message };
  } finally {
    if (epoch === contextEpoch && sequence === evidenceDetailSeq) { state.evidenceBusy = false; render(); }
  }
}

async function refreshEvidenceDetail() {
  await loadLive('evidence');
  if (state.evidenceSelected) await selectEvidencePage(state.evidenceSelected);
}

function proposalItemView(item, proposal) {
  if(item.current_event)item={...item,...item.current_event};
  const receipt=(proposal.knowledge_events||[]).find(record=>record.item_key===item.item_key);
  const eventId=item.event_id||receipt?.event_id;
  return `<article class="review-item"><div class="review-item-head"><strong>${h(itemSection(item))}</strong>${badge(eventId?'已采纳 · 可纠错':'历史提取条目',eventId?'green':'')}${item.unknown?badge('未知信息保留','orange'):''}</div>
    ${recordSourceNotice(item)}${fieldsHtml(item.fields)}${item.raw_text?`<p class="record-original">${h(item.raw_text)}</p>`:''}
    <div class="review-actions">${eventId?btn('live-event','查看知识与版本','quiet','clock',`data-id="${h(eventId)}"`)+btn('event-correct','质疑 / 纠错','secondary','',`data-id="${h(eventId)}"`):'<span class="subtle">暂无关联知识记录；可刷新或重新提取。</span>'}</div></article>`;
}

function fieldText(value) {
  if(value===null||value===undefined)return '未知';
  if(typeof value!=='object')return String(value);
  if(Array.isArray(value))return value.map(fieldText).join('、');
  const primary=['value','normalized','raw'].find(key=>value[key]!==undefined&&value[key]!==null);
  if(primary){const display=value[primary],unit=typeof value.unit==='object'?fieldText(value.unit):value.unit;
    const extra=Object.entries(value).filter(([key,child])=>key!==primary&&!['unit','evidence_refs','uncertain'].includes(key)&&JSON.stringify(child)!==JSON.stringify(display)).map(([key,child])=>`${key}: ${fieldText(child)}`);
    return [`${fieldText(display)}${unit?' '+unit:''}`,...extra].join('；');}
  return Object.entries(value).filter(([key])=>key!=='evidence_refs').map(([key,child])=>`${key}: ${fieldText(child)}`).join('；');
}
function fieldsHtml(fields, limit=Infinity) {
  if(!fields||typeof fields!=='object')return '';
  const hidden=['section','section_title','title','record_scope','source_kind','source_notice','sample_label','related_sample_labels','sample_labels','disputed','human_question','unknowns'];
  if(Array.isArray(fields.measurements)&&fields.measurements.length)hidden.push('amount','unit');
  const labels={measurements:'记录参数',amount:'用量',unit:'单位',temperature:'温度',duration:'时长',conditions:'实验条件',notes:'补充说明'};
  const measurements=Array.isArray(fields.measurements)?fields.measurements:[];
  const modes={plan:'计划值',reported:'记录值',computed:'计算值',unknown:'未知'};
  const measured=measurements.slice(0,limit).map(value=>{const context=value.conditions&&Object.keys(value.conditions).length?fieldText(value.conditions):'';
    return `<div><dt>${h(value.metric||'记录参数')}${value.modality?` · ${h(modes[value.modality]||value.modality)}`:''}</dt><dd>${h(value.raw||`${fieldText(value.value)}${value.unit?' '+fieldText(value.unit):''}`)}${value.stage?`<small>${h(value.stage)}</small>`:''}${context?`<small>${h(context)}</small>`:''}</dd></div>`;}).join('');
  const entries=Object.entries(fields).filter(([key,value])=>key!=='measurements'&&!hidden.includes(key)&&!(Array.isArray(value)&&!value.length));
  return measured||entries.length?`<dl class="knowledge-fields">${measured}${entries.slice(0,limit).map(([key,value])=>`<div><dt>${h(labels[key]||key)}</dt><dd>${h(fieldText(value))}</dd></div>`).join('')}</dl>`:'';
}
function itemSection(record) {
  return fieldText(record.section||record.section_title||record.fields?.section||record.fields?.section_title||({operation:'操作记录',observation:'观察记录',measurement:'测量记录',characterization:'表征记录',synthesis:'制备记录',note:'补充记录'}[record.event_kind])||record.event_kind||'研究记录');
}
const recordDisputed = record => record.fields?.disputed===true || record.fields?.human_question?.status==='open';
function recordSourceNotice(record) {
  const notice=record.fields?.source_notice||record.source_notice,kind=record.fields?.source_kind||record.source_kind;
  const names={simulated_record:'模拟资料',synthetic_ui:'界面示例',reported_record:'原始实验记录',non_experimental:'非实验内容',unknown:'来源性质未明确'};
  return notice||kind?`<div class="record-source-notice">${kind?badge(names[kind]||fieldText(kind),'orange'):''}${notice?`<p>${h(fieldText(notice))}</p>`:''}</div>`:'';
}
function sampleRecords(sample) {
  if(Array.isArray(sample?.records))return sample.records;
  if(Array.isArray(sample?.knowledge?.records))return sample.knowledge.records;
  return Array.isArray(sample?.knowledge)?sample.knowledge:[];
}
function recordSources(record) {
  const pages=new Map((record.source_pages||[]).map(page=>[page.page_id,page]));
  const refs=[...(Array.isArray(record.evidence_refs)?record.evidence_refs:[]),...(Array.isArray(record.provenance?.evidence_refs)?record.provenance.evidence_refs:[])];
  if(record.page_id)refs.unshift({page_id:record.page_id});
  const unique=new Map();
  for(const ref of refs){const id=typeof ref==='string'?(ref.startsWith('pg_')?ref:null):ref?.page_id||(['evidence_page','page'].includes(ref?.kind)?ref.id:null);if(id&&!unique.has(id)){const page=pages.get(id);unique.set(id,{id,label:ref?.filename||ref?.label||(page?.title?`${page.title} · 第${page.page_order}页`:id)});}}
  return [...unique.values()];
}
function sourceButtons(records, limit=Infinity) {
  const sources=new Map();for(const record of records)for(const source of recordSources(record))sources.set(source.id,source);
  return [...sources.values()].slice(0,limit).map(source=>`<button class="source-link" data-action="task-evidence" data-id="${h(source.id)}">${icon('link',11)}${h(source.label)} ${icon('arrow',11)}</button>`).join('');
}
function recordHtml(record) {
  return `<article class="knowledge-record"><div class="knowledge-record-head"><strong>${h(itemSection(record))}</strong>${badge(record.revision>1?'已更新 · v'+record.revision:'已采纳 · 可纠错','green')}${recordDisputed(record)?badge('有质疑','orange'):''}</div>
    <p class="record-date">${h(record.event_time_start||record.knowledge_as_of||'时间未记录')}${record.event_id?' · '+h(record.event_id):''}</p>
    ${recordSourceNotice(record)}${record.source_stale?`<div class="notice orange">${icon('alert',14)}<p>来源转写已纠错，这条知识可能受影响，请对照来源修正。</p></div>`:''}
    ${record.unknown||record.unknowns?.length?`<p class="record-unknown">未知信息已保留${record.unknowns?.length?'：'+h(fieldText(record.unknowns)):''}</p>`:''}
    ${fieldsHtml(record.fields)}${record.raw_text?`<p class="record-original">${h(record.raw_text)}</p>`:''}
    <div class="record-actions">${sourceButtons([record])}${record.event_id?btn('event-correct','质疑 / 纠错','quiet','',`data-id="${h(record.event_id)}"`)+btn('live-event','修订历史','quiet','clock',`data-id="${h(record.event_id)}"`):''}</div></article>`;
}
function liveKnowledgeHtml(result) {
  const all=Array.isArray(result.samples)?result.samples:[],list=all.filter(sample=>sampleRecords(sample).length>0),emptySamples=all.filter(sample=>!sampleRecords(sample).length),query=state.query.trim().toLocaleLowerCase();
  const filtered=list.filter(sample=>!query||`${sample.label||''} ${sample.sample_id||''} ${sample.summary||''} ${sampleRecords(sample).map(record=>record.raw_text||fieldText(record.fields)).join(' ')}`.toLocaleLowerCase().includes(query));
  const unassigned=Array.isArray(result.unassigned_records)?result.unassigned_records:[];
  const sourceNotes=Array.isArray(result.source_notes)?result.source_notes:[];
  return `<div class="knowledge-hub"><div class="knowledge-policy">${icon('book',19)}<div><strong>资料默认采纳，发现问题随时纠错。</strong><p>每个样品汇总已有记录与来源；修改会保留历史版本，未知信息继续标记。</p></div>${badge('默认采纳 · 可纠错','green')}</div>
    <div class="sample-knowledge-toolbar"><span>${list.length} 个样品</span><label class="sample-search">${icon('search',14)}<input id="view-query" value="${h(state.query)}" placeholder="搜索样品、标签或记录内容" aria-label="搜索样品资料"></label></div>
    ${unassigned.length?`<details class="unassigned-records"><summary>${unassigned.length} 条资料尚未关联样品 · 查看与纠错</summary><p class="prose">原件与知识已保留，样品身份不明时不会猜测归属。</p>${unassigned.map(recordHtml).join('')}</details>`:''}
    ${sourceNotes.length?`<details class="unassigned-records source-notes"><summary>来源说明 / 非样品内容 · ${sourceNotes.length} 条</summary><p class="prose">由提取结果明确标记的页面说明与来源信息保留在这里。</p>${sourceNotes.map(recordHtml).join('')}</details>`:''}
    ${emptySamples.length?`<details class="unassigned-records"><summary>暂无关联资料的历史标签 · ${emptySamples.length} 个</summary><p class="prose">纠正归属后，旧标签仍保留，便于回查。</p>${emptySamples.map(sample=>btn('live-sample',sample.label||sample.sample_id,'quiet','',`data-id="${h(sample.sample_id)}"`)).join('')}</details>`:''}
    <div class="sample-knowledge-grid">${filtered.map(sample=>{const records=sampleRecords(sample),sources=new Set(records.flatMap(record=>recordSources(record).map(source=>source.id)));
      return `<article class="sample-knowledge-card"><button class="sample-card-open" data-action="live-sample" data-id="${h(sample.sample_id)}"><div class="sample-card-head"><span class="sample-motif">${icon('beaker',23)}</span><span>${badge(records.some(record=>record.source_stale)?'来源有修订':'已采纳 · 可纠错',records.some(record=>record.source_stale)?'orange':'green')}${records.some(recordDisputed)?badge('有质疑','orange'):''}</span></div><h2>${h(sample.label||sample.sample_id)}</h2><p class="sample-card-id">${h(sample.sample_id)}</p><div class="sample-card-counts"><span>${h(sample.event_count??records.length)} 条记录</span><span>${h(sample.source_count??sources.size)} 个来源</span><span>${h(sample.last_event_at||'时间未记录')}</span></div>
        ${recordSourceNotice(records.find(record=>record.fields?.source_notice||record.fields?.source_kind)||{})}${sample.summary?`<p class="sample-card-summary">${h(sample.summary)}</p>`:''}
        ${records.slice(0,2).map(record=>`<section class="sample-record-preview"><h3>${h(itemSection(record))}</h3>${fieldsHtml(record.fields,4)}${record.raw_text?`<p>${h(record.raw_text.slice(0,180))}${record.raw_text.length>180?'…':''}</p>`:''}</section>`).join('')||'<p class="sample-card-summary">尚无关联记录。上传原件后，提取的资料会汇总到这里。</p>'}
        <span class="sample-card-more">查看完整资料与时间线 ${icon('arrow',15)}</span></button><div class="sample-card-sources">${sourceButtons(records,3)||'<span class="subtle">暂无原件来源链接</span>'}</div></article>`;}).join('')||empty(query?'没有匹配的样品':'当前项目暂无样品',query?'换个标签、编号或记录关键词。':'上传记录后，样品资料会持续积累。')}</div></div>`;
}
function showLiveSample(id) {
  const sample=(state.live.samples?.samples||[]).find(item=>item.sample_id===id);if(!sample)return;
  state.liveSampleId=id;
  const records=sampleRecords(sample),groups=new Map();
  for(const record of records){const section=itemSection(record);if(!groups.has(section))groups.set(section,[]);groups.get(section).push(record);}
  modal(h(sample.label||id),`${h(id)} · 样品资料`, `<div class="detail-meta">${badge('默认采纳 · 可纠错','green')}<span>${records.length} 条知识记录 · ${h(sample.source_count??new Set(records.flatMap(record=>recordSources(record).map(source=>source.id))).size)} 个来源</span></div>${sample.summary?`<p class="prose">${h(sample.summary)}</p>`:''}
    <div class="sample-sections">${[...groups].map(([section,items])=>`<section><h3>${h(section)} <small>${items.length}</small></h3>${items.map(recordHtml).join('')}</section>`).join('')||empty('暂无关联资料','新提取的样品资料会自动汇入。')}</div>
    <details class="sample-timeline"><summary>完整时间线</summary>${[...records].sort((a,b)=>String(a.event_time_start||a.knowledge_as_of||'').localeCompare(String(b.event_time_start||b.knowledge_as_of||''))).map(record=>`<div><time>${h(record.event_time_start||record.knowledge_as_of||'时间未知')}</time><strong>${h(itemSection(record))}</strong><p>${h(record.raw_text||fieldText(record.fields))}</p>${sourceButtons([record])}</div>`).join('')}</details>`, 'drawer');
}

function eventRecord(head, id) {
  const payload=head?.field_json||head||{};
  return {...payload,...head,event_id:head?.entity_id||head?.event_id||id,
    fields:payload.fields||head?.fields||{},raw_text:payload.raw_text||head?.raw_text||'',
    event_kind:payload.event_kind||head?.event_kind,page_id:payload.page_id||head?.page_id};
}
async function showLiveEvent(id) {
  const epoch=contextEpoch;
  modal('知识记录与修订历史',h(id),loading(),'drawer');
  const ticket=detailRequestSeq;
  try{
    const result=await client.event(id,true);if(epoch!==contextEpoch||ticket!==detailRequestSeq||state.mode!=='live')return;
    const revisions=result.revisions||result.history||(result.event?[result.event]:[result]);
    modal('知识记录与修订历史',h(id),`<p class="prose">默认采纳后的修改以新版本追加，原件与历史记录保留。</p>${revisions.map(head=>recordHtml(eventRecord(head,id))).join('')}`,'drawer');
  }catch(error){if(epoch===contextEpoch&&ticket===detailRequestSeq)modal('读取失败',h(id),liveError(error.message),'drawer');}
}
function editableFields(value,path=[],result=[]) {
  if(value&&typeof value==='object'){
    for(const key of Object.keys(value)){if(['__proto__','constructor','prototype'].includes(key))continue;editableFields(value[key],[...path,key],result);}
  }else result.push({path,value});
  return result;
}
function correctionFieldLabel(path) {
  const labels={measurements:'参数',conditions:'条件',metric:'指标',modality:'记录性质',raw:'原文表达',stage:'阶段',unit:'单位',value:'数值',normalized:'数值',amount:'用量',section:'分类',title:'标题',related_sample_labels:'相关样品',unknowns:'未知信息'};
  return path.map(key=>/^\d+$/.test(key)?`第 ${Number(key)+1} 项`:labels[key]||key).join(' / ');
}
function correctionFieldInput(leaf,index) {
  const modes={plan:'计划值',reported:'记录值',computed:'计算值',unknown:'未知'};
  if(leaf.path.at(-1)==='modality'&&modes[leaf.value])return `<select class="form-control" id="correction-field-${index}" name="field-${index}">${Object.entries(modes).map(([value,label])=>`<option value="${value}" ${value===leaf.value?'selected':''}>${label}</option>`).join('')}</select>`;
  return `<input class="form-control" id="correction-field-${index}" name="field-${index}" value="${h(leaf.value===null?'null':leaf.value)}">`;
}
async function showEventCorrection(id) {
  const epoch=contextEpoch;
  modal('质疑 / 纠正知识记录',h(id),loading(),'drawer');
  const ticket=detailRequestSeq;
  try{
    const result=await client.event(id);if(epoch!==contextEpoch||ticket!==detailRequestSeq||state.mode!=='live')return;
    const record=eventRecord(result.event||result,id),leaves=editableFields(record.fields).filter(leaf=>!['sample_label','disputed','human_question','source_kind','source_notice','record_scope'].includes(leaf.path[0])&&!leaf.path.includes('evidence_refs')&&!(record.fields.measurements?.length&&['amount','unit'].includes(leaf.path[0]))).slice(0,80);
    if(!Number.isInteger(record.revision))throw new Error('记录缺少当前版本，无法安全提交纠错。请刷新后重试。');
    const openSample=(state.live.samples?.samples||[]).find(sample=>sample.sample_id===state.liveSampleId&&sampleRecords(sample).some(item=>item.event_id===id));
    state.correctingEvent={id,record,leaves,sampleId:openSample?.sample_id||null};
    const label=record.fields.sample_label,currentLabel=typeof label==='object'?(label?.normalized??label?.value??label?.raw??''):label||'';
    modal('质疑 / 纠正知识记录',`${h(id)} · 当前 v${record.revision}`,`<div class="notice">${icon('clock',15)}<p>保存将追加纠错版本。原始文件和此前记录继续保留；来源变化不会被悄悄覆盖。</p></div>${sourceButtons([record])}
      <form data-form="event-correction" data-id="${h(id)}"><div class="form-group"><label for="correction-reason">纠错原因或质疑说明</label><input class="form-control" id="correction-reason" name="reason" required maxlength="1000" placeholder="例如：对照原页，温度应为150 ℃"></div>
      <div class="form-group"><label for="correction-sample-label">主样品标签</label><input class="form-control" id="correction-sample-label" name="sample_label" value="${h(currentLabel)}" maxlength="300" placeholder="例如：Zr-BDC-02；留空表示未归属"><small>将记录关联到对应样品；身份不明时留空。</small></div>
      <label class="dispute-toggle"><input type="checkbox" name="disputed" ${recordDisputed(record)?'checked':''}>这条信息有疑问</label>
      <div class="form-group"><label for="correction-raw">记录内容</label><textarea class="form-control" id="correction-raw" name="raw_text" rows="5">${h(record.raw_text)}</textarea></div>
      ${leaves.length?`<details class="correction-fields" open><summary>结构化字段 · 可逐项修改</summary>${leaves.map((leaf,index)=>`<div class="form-group"><label for="correction-field-${index}">${h(correctionFieldLabel(leaf.path))}</label>${correctionFieldInput(leaf,index)}</div>`).join('')}</details>`:''}
      <div id="event-correction-error"></div><div class="form-actions">${btn('close','取消')}<button class="btn btn-primary" type="submit">保存纠错版本</button></div></form>`,'drawer');
  }catch(error){if(epoch===contextEpoch&&ticket===detailRequestSeq)modal('读取失败',h(id),liveError(error.message),'drawer');}
}
async function submitEventCorrection(form) {
  const editing=state.correctingEvent;if(!editing||editing.id!==form.dataset.id)return;
  const epoch=contextEpoch,ticket=detailRequestSeq,fd=new FormData(form),errorNode=$('#event-correction-error'),button=$('[type="submit"]',form);
  try{
    const reason=String(fd.get('reason')||'').trim();if(!reason)throw new Error('请填写纠错原因。');
    const fields=clone(editing.record.fields);
    editing.leaves.forEach((leaf,index)=>{let value=String(fd.get('field-'+index)??'');
      if(typeof leaf.value==='number'){if(!value.trim()||!Number.isFinite(Number(value)))throw new Error(leaf.path.join(' / ')+' 需要有效数值。');value=Number(value);}
      else if(typeof leaf.value==='boolean'){if(!['true','false'].includes(value))throw new Error(leaf.path.join(' / ')+' 需要true或false。');value=value==='true';}
      else if(leaf.value===null&&value==='null')value=null;
      let target=fields;for(const key of leaf.path.slice(0,-1))target=target[key];target[leaf.path.at(-1)]=value;
    });
    const sampleLabel=String(fd.get('sample_label')||'').trim();
    fields.sample_label=sampleLabel?{...(typeof fields.sample_label==='object'?fields.sample_label:{}),normalized:sampleLabel}:{normalized:null};
    if(fd.get('disputed')){fields.disputed=true;fields.human_question={text:reason,status:'open'};}
    else if(recordDisputed(editing.record)){fields.disputed=false;fields.human_question={...fields.human_question,text:fields.human_question?.text||reason,status:'resolved',resolution:reason};}
    if(button)button.disabled=true;
    const result=await client.correctEvent({eventId:editing.id,expectedRevision:editing.record.revision,reason,changes:{raw_text:String(fd.get('raw_text')||''),fields}});
    if(epoch!==contextEpoch||ticket!==detailRequestSeq||state.mode!=='live')return;
    if((result.outcome||result.result?.outcome)==='parked_as_pending')throw new Error('记录已有新版本，本次纠错已保留为待处理事项；请读取最新记录后核对。');
    closeOverlay();state.correctingEvent=null;await loadLive('knowledge');
    if(epoch!==contextEpoch||state.mode!=='live')return;
    if(editing.sampleId)showLiveSample(editing.sampleId);
    toast('纠错版本已保存，样品资料已更新。');
  }catch(error){if(epoch===contextEpoch&&ticket===detailRequestSeq&&errorNode)errorNode.innerHTML=`<div class="form-error">${h(error.message)}</div>`;}
  finally{if(button?.isConnected)button.disabled=false;}
}
function showTranscriptCorrection(blockId) {
  const page=state.evidencePage,block=page?.transcript?.blocks?.find(item=>item.block_id===blockId);if(!block)return;
  const revision=page.transcript.active_revision??page.active_revision;
  state.correctingTranscript={pageId:page.page_id,blockId,revision};
  modal('纠正原页转写',`${h(page.page_id)} · 转写 v${h(revision)}`,`<div class="notice">${icon('evidence',15)}<p>只修正此转写块，原件不会改变。保存新转写版本后，受影响知识会标记来源已更新。</p></div><form data-form="transcript-correction"><div class="form-group"><label for="transcript-correct-text">正确转写</label><textarea class="form-control" id="transcript-correct-text" name="text" rows="7" required>${h(block.text)}</textarea></div><div class="form-group"><label for="transcript-correct-reason">纠错原因</label><input class="form-control" id="transcript-correct-reason" name="reason" required maxlength="1000"></div><div id="transcript-correction-error"></div><div class="form-actions">${btn('close','取消')}<button class="btn btn-primary" type="submit">保存转写纠错</button></div></form>`,'drawer');
}
async function submitTranscriptCorrection(form) {
  const editing=state.correctingTranscript;if(!editing)return;
  const epoch=contextEpoch,ticket=detailRequestSeq,fd=new FormData(form),errorNode=$('#transcript-correction-error'),button=$('[type="submit"]',form);
  try{
    const reason=String(fd.get('reason')||'').trim(),text=String(fd.get('text')||'');if(!reason||!text.trim())throw new Error('请填写正确转写与纠错原因。');
    if(!Number.isInteger(editing.revision))throw new Error('缺少转写版本，请刷新后重试。');
    if(button)button.disabled=true;
    await client.correctTranscript({pageId:editing.pageId,expectedRevision:editing.revision,reason,corrections:[{block_id:editing.blockId,text}]});
    if(epoch!==contextEpoch||ticket!==detailRequestSeq||state.mode!=='live')return;
    closeOverlay();state.correctingTranscript=null;await refreshEvidenceDetail();
    if(epoch===contextEpoch)toast('转写纠错已保存；受影响知识已标记，可继续修正。');
  }catch(error){if(epoch===contextEpoch&&ticket===detailRequestSeq&&errorNode)errorNode.innerHTML=`<div class="form-error">${h(error.message)}</div>`;}
  finally{if(button?.isConnected)button.disabled=false;}
}

function evidenceDetailHtml(page,selected,proposal,preview) {
  const blocks=page.transcript?.blocks||[],items=proposal?.items||[];
  const previewHtml=preview?.materialized&&preview.data_b64?`<img class="evidence-preview" src="data:${h(preview.mime||'image/png')};base64,${h(preview.data_b64)}" alt="${h(selected.filename||'原件预览')}">`:`<div class="notice orange">${icon('evidence',15)}<p>${preview?.materialized===false?'预览尚未生成；原图已保存。':'预览读取失败或暂不可用。'}</p></div>`;
  return `<div class="evidence-detail-head"><div><span class="eyebrow">EVIDENCE / ${h(page.mime)}</span><h2>${h(selected.filename||page.page_id)}</h2><p>${h(page.notebook_code)} · 第 ${h(page.page_order)} 页 · 转写 v${h(page.transcript?.active_revision??page.active_revision??0)}</p></div><div class="evidence-detail-actions">${btn('evidence-refresh','刷新','quiet','refresh')}${page.mime?.startsWith('image/')&&client.modelConfigured?btn('evidence-ocr','重新提取','secondary','spark'):''}</div></div>
    <div class="notice knowledge-policy">${icon('check',16)}<p>提取资料默认采纳，无需逐项确认。发现识别或归属问题，可纠正转写或知识记录；原件及历史版本始终保留。</p>${badge('默认采纳 · 可纠错','green')}</div>
    <div class="evidence-detail-grid"><section class="panel evidence-original"><div class="panel-head"><h3>原件预览</h3><span class="count-label">${h(page.mime)}</span></div>${previewHtml}</section>
    <section class="panel"><div class="panel-head"><h3>原页转写</h3><span class="count-label">${blocks.length} 块</span></div>${blocks.length?`<div class="transcript-list">${blocks.map(block=>`<article class="transcript-line"><p>${h(block.text||'[unclear]')}</p><button class="btn btn-quiet" data-action="transcript-correct" data-id="${h(block.block_id)}">纠正转写</button></article>`).join('')}</div>`:empty('暂无转写',page.mime?.startsWith('image/')?'配置视觉模型后点击“重新提取”。':'原件已保存，该格式暂无自动解析器。')}</section></div>
    <section class="panel evidence-knowledge"><div class="panel-head"><h3>提取的知识记录</h3>${proposal?badge(proposal.status==='applied'?'已采纳 · 可纠错':'历史提取条目',proposal.status==='applied'?'green':''):''}</div>${proposal?`<div class="review-list">${items.length?items.map(item=>proposalItemView(item,proposal)).join(''):empty('暂无提取条目','可以重新提取或补充记录。')}</div>`:empty('暂无提取的知识记录','原件已保留；提取完成后将在此显示，可随后纠错。')}</section>`;
}

function viewContent() {
  if (state.mode==='live') return liveView();
  return ({overview:overviewView,knowledge:knowledgeView,evidence:evidenceView,inventory:inventoryView,agents:agentsView,resources:resourcesView}[state.view] || overviewView)();
}
function overviewView() {
  const ps=projectSamples(), pe=projectEvidence(), wait=projectTasks().filter(t=>t.status==='waiting').length;
  return `${pageHeading('研究，从这里继续。','你的记录、样品与协作，连接在同一个工作空间。',`<div class="date-label">THURSDAY<strong>15 OCT, 2026 · 示例日</strong></div>${btn('upload','导入记录','primary','plus')}`)}${taskComposer()}
    <section class="hero" aria-label="可交互的研究知识关系图"><div class="hero-copy"><div class="eyebrow">FROM PAPER TO KNOWLEDGE</div><h2>实验有迹可循，<br><span>知识持续生长。</span></h2><p>把纸上的一次观察，连接成可回查、可协作、<br>不断积累的实验室共同记忆。</p><div class="hero-actions">${btn('upload','整理一份记录','primary','upload')}<button class="text-link" data-action="sample" data-id="${h(ps[0].id)}">继续研究 ${icon('arrow',14)}</button></div></div>
    <div class="network"><svg class="network-lines" viewBox="0 0 480 282" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><g class="network-orbit"><circle class="network-circle" cx="240" cy="141" r="108"/><circle class="network-circle" cx="240" cy="141" r="81" stroke-dasharray="1 7"/><circle cx="341" cy="104" r="3" fill="#9eb67d"/><circle cx="158" cy="211" r="2" fill="#b7c4a0"/></g><path class="network-line" d="M240 46V105M100 141h95M285 141h95M240 180v58"/><circle class="network-circle" cx="240" cy="141" r="135" stroke-dasharray="1 10"/></svg>
    <button class="network-node top" data-action="sample" data-id="${h(ps[0].id)}"><span class="node-icon">${icon('beaker',14)}</span><strong>研究样品</strong><small>${h(ps[0].id)} · ${h(ps[0].status)}</small></button>
    <button class="network-node left" data-action="goto" data-view="evidence"><span class="node-icon">${icon('evidence',14)}</span><strong>原始证据</strong><small>${pe.length.toString().padStart(2,'0')} 份记录 · 可回查</small></button>
    <button class="network-core" data-action="sample" data-id="${h(ps[0].id)}" aria-label="打开样品知识时间线">${icon('link',24)}<strong>共同记忆</strong><small>CONNECTED LAB</small></button>
    <button class="network-node right" data-action="goto" data-view="inventory"><span class="node-icon">${icon('box',14)}</span><strong>库存物料</strong><small>台账独立 · 单位保留</small></button>
    <button class="network-node bottom" data-action="goto" data-view="agents"><span class="node-icon">${icon('agents',14)}</span><strong>Agent 协作</strong><small>人机协同 · 持续积累</small></button><span class="network-tag">A LIVING RESEARCH CONTEXT</span></div></section>
    <div class="stats-strip">${[['evidence','evidence','待复核记录',pending().length,'需要你确认'],['knowledge','beaker','研究样品',ps.length,'持续积累中'],['agents','agents','协作任务',projectTasks().length,`${wait} 项待处理`],['inventory','box','库存提醒',lowStock().length,'余量低于阈值']].map(([view,glyph,title,value,tail])=>`<button class="stat-item" data-action="goto" data-view="${view}"><span class="stat-icon">${icon(glyph,19)}</span><span class="stat-text"><span class="stat-title">${title}</span><span class="stat-value mono">${String(value).padStart(2,'0')}<small class="stat-tail">${tail}</small></span></span>${icon('chevron',15).replace('<svg','<svg class="stat-arrow"')}</button>`).join('')}</div>
    <div class="work-grid"><section class="panel"><div class="panel-head"><h2>需要你看看 <span class="count-label">${pending().length.toString().padStart(2,'0')}</span></h2><button class="btn btn-quiet" data-action="goto" data-view="evidence">证据工作区 ${icon('arrow',13)}</button></div>${pending().length?pending().map(e=>`<button class="review-row" data-action="evidence" data-id="${h(e.id)}"><span class="doc-thumb">${e.asset?`<img src="${h(e.asset)}" alt="合成笔记缩略图">`:icon('file',22)}</span><span class="review-row-content"><strong class="row-title">${h(e.title)}</strong><span class="row-desc">${h(e.sampleId)} · ${e.fields.some(f=>f.confidence<80)?'存在用量差异，等待原页核对':'候选字段已就绪，等待逐项确认'}</span></span><span class="review-row-end">${badge(e.fields.some(f=>f.confidence<80)?'用量待核对':'待确认','orange')}<span class="small">${h(e.date.slice(5).replace('-',' / '))}</span></span></button>`).join(''):`<div class="empty-state" style="padding:28px">${icon('check',25)}<p>当前项目的演示记录均已复核。</p></div>`}${pending().length?`<div class="review-insight"><span class="eyebrow">NEXT STEP / 本次复核关注</span><p>先确认<strong>计划值与实测值</strong>的区别，再回到原页核对。记录中的不确定性会被保留，直到得到实验者确认。</p><button class="btn btn-quiet" data-action="evidence" data-id="${h(pending()[0].id)}">开始逐项核对 ${icon('arrow',12)}</button></div>`:''}<div class="panel-footer"><span>原始记录保留，每次修订可回查</span>${icon('shield',12)}</div></section>
    <section class="panel"><div class="panel-head"><h2>最近研究 <span class="eyebrow">RECENT SAMPLES</span></h2><button class="btn btn-quiet" data-action="goto" data-view="knowledge">全部样品 ${icon('arrow',13)}</button></div>${ps.map((s,i)=>`<button class="sample-row" data-action="sample" data-id="${h(s.id)}"><span class="sample-index">0${i+1}</span><span class="sample-glyph">${icon('beaker',17)}</span><span class="sample-row-content"><strong class="row-title">${h(s.name)}</strong><small class="row-desc">${h(s.id)} · ${s.events.length} 条关联记录</small><span class="progress-mini">${[0,1,2,3,4,5].map(n=>`<span class="${n<s.events.length+1?'filled':''}"></span>`).join('')}</span></span>${sampleBadge(s)}${icon('chevron',13)}</button>`).join('')}<div class="panel-footer"><span>${h(project().code)} · 当前项目</span><span>来源与版本始终相连</span></div></section></div>
    <div class="section-label">协作概览 <span class="eyebrow">AGENTS AT WORK</span><button class="btn btn-quiet" data-action="goto" data-view="agents">查看任务 ${icon('arrow',13)}</button></div><div class="agents-strip">${projectTasks().slice(0,3).map(agentCard).join('')}</div>`;
}
function agentCard(t) {
  const [label,color]=taskStatus(t);
  return `<button class="agent-card" data-action="task" data-id="${h(t.id)}"><div class="agent-card-top"><span class="agent-mark">${t.agent.slice(0,1)}</span><h3>${h(t.agent)}</h3>${badge(label,color,true)}</div><p>${h(t.title)}</p><div class="agent-progress"><i style="width:${t.progress}%"></i></div><div class="agent-card-foot"><span>${h(t.id)}</span><span>${t.progress}%</span></div></button>`;
}
function knowledgeView() {
  const filtered=projectSamples().filter(s=>(state.filter==='all'||(state.filter==='review'?s.status==='待复核':s.status!=='待复核')) && `${s.name} ${s.id} ${s.tags.join(' ')}`.toLowerCase().includes(state.query.toLowerCase()));
  return `${pageHeading('知识与样品','从一份记录到一段研究，所有事实都能找到来源。',btn('export','导出项目','secondary','download')+btn('upload','加入新记录','primary','plus'))}<div class="knowledge-intro"><div><span class="eyebrow">${h(project().code)} / KNOWLEDGE BASE</span><h2>每个样品，都有一段完整的研究历程。</h2><p>计划、观察与已确认事实分别保留，沿时间线回到原始证据。</p></div>${icon('book',52)}</div>${toolbar([['all','全部样品'],['review','待复核'],['active','进行中']],'搜索样品、标签或编号')}<div class="knowledge-grid">${filtered.map(s=>`<button class="knowledge-card" data-action="sample" data-id="${h(s.id)}"><div class="knowledge-card-top"><span class="sample-motif">${icon('beaker',23)}</span>${sampleBadge(s)}</div><div class="eyebrow">${h(s.id)}</div><h2>${h(s.name)}</h2><p>${h(s.summary)}</p><div class="knowledge-tags">${s.tags.map(t=>`<span>${h(t)}</span>`).join('')}</div><div class="knowledge-card-foot"><span>${s.events.length} 条记录 · ${h(s.owner)}</span>${icon('arrow',16)}</div></button>`).join('')||empty('暂无匹配样品','换个关键词，或清除筛选。')}</div>`;
}
function toolbar(items, placeholder) {
  return `<div class="toolbar"><div class="tabs" role="group" aria-label="筛选">${items.map(([id,label])=>`<button class="tab ${state.filter===id?'active':''}" data-action="filter" data-filter="${id}" aria-pressed="${state.filter===id}">${label}</button>`).join('')}</div><label class="search-input">${icon('search',15)}<input id="view-query" value="${h(state.query)}" placeholder="${placeholder}" aria-label="${placeholder}"></label></div>`;
}
function evidenceView() {
  const list=projectEvidence();let e=list.find(x=>x.id===state.evidenceId)||list[0];state.evidenceId=e?.id;
  if(!e)return `${pageHeading('证据工作区','记录原貌，保留来源。',btn('upload','导入记录','primary','plus'))}${empty('还没有原始记录','导入一份笔记，为这个项目开始建立证据。')}`;
  const revisions=state.versions.filter(v=>v.id===e.id);
  return `${pageHeading('证据工作区','原页与字段并排核对，让每一条知识有据可循。',btn('upload','导入记录','primary','plus'))}<div class="notice">${icon('shield',16)}<p>当前为合成示例。转录和字段由示例提供；修改与确认仅保留在本浏览器，不调用 OCR、不写入 Core。</p></div><div class="evidence-layout"><aside class="evidence-list" aria-label="证据列表">${list.map(x=>`<button class="evidence-list-item ${x.id===e.id?'active':''}" data-action="evidence" data-id="${h(x.id)}"><span class="eyebrow">${h(x.id)} / ${h(x.date)}</span><strong>${h(x.title)}</strong>${badge(x.status==='confirmed'?'已确认':'待复核',x.status==='confirmed'?'green':'orange')} <small>${h(x.sampleId)}</small></button>`).join('')}</aside><section class="evidence-work"><div class="evidence-title"><div><h2>${h(e.title)}</h2><p>${h(e.sampleId)} · ${e.asset?'原页 v1':'文本 / 文件草稿'} · 字段 v${revisions.length+1}</p></div><div style="display:flex;gap:9px;align-items:center">${badge(e.status==='confirmed'?'已确认':'待复核',e.status==='confirmed'?'green':'orange')}${btn('transcript','查看转录','secondary','file',`data-id="${h(e.id)}"`)}</div></div><div class="evidence-panes"><div class="source-pane"><div class="pane-label"><span>01 / ${e.id.startsWith('LOCAL-')?'本地记录':'原始页面 · 合成示例'}</span><button class="icon-btn" data-action="zoom" data-id="${h(e.id)}" aria-label="放大原始页面">${icon('external',13)}</button></div><div class="source-sheet-wrap" style="position:relative">${e.asset?`<img class="source-sheet" src="${h(e.asset)}" alt="${h(e.title)}的合成原始页面" data-action="zoom" data-id="${h(e.id)}">`:`<div class="transcript" style="margin-top:0">${e.fileName?`<p>${icon('file',19)} ${h(e.fileName)}</p><p class="small subtle">文件已选择，当前尚未预览或转录。</p>`:h(e.transcript)}</div>`}<div id="source-highlight" style="display:none;position:absolute;border:2px solid #b8c46a;background:#d6ed9b33;border-radius:4px;pointer-events:none"></div></div><p class="source-note">点击字段查看对应原文 · 点击页面放大</p></div><div class="fields-pane"><div class="pane-label"><span>02 / 候选字段</span><span>逐项对照原页</span></div>${e.fields.map(f=>`<div class="field-item ${state.selectedField===f.id?'selected':''}" data-field="${h(f.id)}"><div class="field-label"><span>${h(f.label)}</span>${badge(e.status==='confirmed'?'已核对':f.status==='review'?'需核对':'待对照',f.status==='review'?'orange':'')}</div><div class="field-value"><button data-action="field-source" data-id="${h(f.id)}" style="text-align:left;padding:0">${h(f.value)} <span class="unit">${h(f.unit)}</span></button><button class="field-edit" data-action="field-edit" data-id="${h(f.id)}">修改</button></div>${state.editing===f.id?`<form class="field-editor" data-form="field" data-id="${h(f.id)}"><input name="value" value="${h(f.value)}" aria-label="${h(f.label)}的值" required maxlength="300"><button type="submit">保存</button></form>`:''}<button class="field-source" data-action="field-source" data-id="${h(f.id)}">${icon('link',10)} ${h(f.sourceText)}</button></div>`).join('')}<div class="evidence-versions"><button class="btn btn-quiet" data-action="versions" data-id="${h(e.id)}">${icon('clock',12)}${revisions.length?'查看 '+revisions.length+' 次本地修订':'尚无本地修订'} ${icon('arrow',12)}</button></div></div></div><div class="evidence-work-footer"><p>${e.status==='confirmed'?'已确认状态仅属于演示数据。':'确认会保留字段版本；历史记录不触发库存扣减。'}</p>${btn('review-confirm',e.status==='confirmed'?'查看确认记录':'核对并确认','primary','check',`data-id="${h(e.id)}"`)}</div></section></div>`;
}
function inventoryView() {
  const list=inventory.filter(i=>(state.filter==='all'||(state.filter==='low'?i.amount<i.threshold:i.category===state.filter))&&`${i.name} ${i.formula} ${i.id} ${i.location}`.toLowerCase().includes(state.query.toLowerCase()));
  return `${pageHeading('库存与物料','每一种物料、每一个单位，都清晰可查。',btn('export-inventory','导出台账','secondary','download'))}<div class="metric-row">${[['物料种类',inventory.length,'项'],['余量提醒',lowStock().length,'项'],['台账范围','LAB','实验室共享']].map(([a,b,c])=>`<div class="metric-card"><span class="eyebrow">${a}</span><div class="metric-number">${b}<small>${c}</small></div></div>`).join('')}</div>${toolbar([['all','全部物料'],['low','余量提醒'],['溶剂','溶剂'],['前驱体','前驱体'],['耗材','耗材']],'搜索物料、编号或存放位置')}<section class="panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>物料 / CHEMICAL</th><th>当前余量</th><th>存放位置</th><th>类别</th><th>状态</th><th>版本</th><th></th></tr></thead><tbody>${list.map(i=>`<tr data-action="chemical" data-id="${h(i.id)}" tabindex="0" role="button" aria-label="查看${h(i.name)}详情"><td class="name-cell"><strong>${h(i.name)}</strong><small>${h(i.formula)} · ${h(i.id)}</small></td><td><span class="amount ${i.amount<i.threshold?'low':''}">${h(i.amount)}</span><span class="unit">${h(i.unit)}</span><div class="stock-meter ${i.amount<i.threshold?'low':''}"><i style="width:${Math.min(100,i.amount/i.threshold*35)}%"></i></div></td><td>${h(i.location)}</td><td>${h(i.category)}</td><td>${badge(i.amount<i.threshold?'余量偏低':'库存充足',i.amount<i.threshold?'orange':'green',true)}</td><td class="subtle mono">r${i.revision}</td><td>${icon('chevron',13)}</td></tr>`).join('')}</tbody></table></div>${!list.length?empty('没有匹配的物料','试试其他名称或类别。'):''}<div class="panel-footer"><span>${list.length} 项物料 · 演示台账</span><span>不跨量纲相加 · 不由笔记自动扣减</span></div></section>`;
}
function agentsView() {
  return `${pageHeading('Agent 协作','每一次交接都有状态，每一个结论都有出处。',btn('assistant','向助手提问','primary','spark'))}<div class="notice">${icon('agents',16)}<p>当前项目与实验室共享任务 · 演示快照。任务执行、事实确认与库存变更分别记录。任务完成不代表已发送外部消息。</p></div><div class="task-grid">${[['running','正在进行','blue'],['waiting','等待你处理','orange'],['done','已经完成','green']].map(([status,title,color])=>`<section class="task-column"><div class="task-column-head"><strong>${badge(title,color,true)}</strong><span>${projectTasks().filter(t=>t.status===status).length.toString().padStart(2,'0')}</span></div>${projectTasks().filter(t=>t.status===status).map(t=>`<button class="task-full" data-action="task" data-id="${h(t.id)}"><div class="task-top">${icon('agents',15)}${h(t.agent)}<span class="eyebrow">${h(t.id)}</span></div><h3>${h(t.title)}</h3><p>${h(t.description)}</p><div class="agent-progress"><i style="width:${t.progress}%"></i></div><div class="task-bottom"><span>${t.steps.filter(s=>s.state==='done').length} / ${t.steps.length} 步骤</span><span class="mono">${h(t.time)}</span></div></button>`).join('')}</section>`).join('')}</div>`;
}
function resourceSlot(resource,hour) {
  const minute=t=>{const [h,m]=t.split(':').map(Number);return h*60+m;};
  const start=hour*60,end=(hour+1)*60;
  const overlaps=resource.slots.filter(s=>minute(s.start)<end&&minute(s.end)>start);
  return overlaps.find(s=>s.state==='busy')||overlaps.find(s=>s.state==='tentative')||overlaps.find(s=>s.state==='free'&&minute(s.start)<=start&&minute(s.end)>=end)||{state:'closed',title:'未开放'};
}
function resourcesView() {
  return `${pageHeading('仪器与预约','把研究计划与设备时段放在一起。',btn('booking-list','本地预约草稿','secondary','calendar'))}<div class="notice orange">${icon('calendar',16)}<p>仅演示排期与草稿。选择可用时段可以建立本地草稿；正式预约功能尚未开放，草稿不会占用设备时段。</p></div><div class="resource-grid">${resources.map(r=>`<section class="resource-card"><div class="resource-card-head"><span class="eyebrow">${h(r.id)}</span>${badge(r.status,r.status==='使用中'?'orange':'green',true)}</div><h2>${h(r.name)}</h2><p>${h(r.type)} · ${h(r.location)}</p><div class="eyebrow" style="font-size:8px">15 OCT / THURSDAY · 合成示例</div><div class="schedule">${[9,10,11,12,13,14,15,16].map(hour=>{const slot=resourceSlot(r,hour);const busy=slot.state==='busy'||slot.state==='closed';return `<button class="${busy?'busy':slot.state==='tentative'?'tentative':''}" data-action="booking" data-id="${h(r.id)}" data-hour="${hour}" ${busy?'disabled':''} aria-label="${h(r.name)} ${hour}:00 ${slot.state==='closed'?'未开放':busy?'已占用':slot.state==='tentative'?'已有计划':'查看时段'}">${hour}:00</button>`}).join('')}</div><div class="resource-card-foot"><span>UTC+08:00 · 时段以草稿为准</span><span>点击空闲时段 ${icon('arrow',12)}</span></div></section>`).join('')}</div>`;
}
function empty(title, desc) {return `<div class="empty-state">${icon('search',29)}<h2>${title}</h2><p>${desc}</p></div>`;}

function liveView() {
  const title=nav.find(n=>n[0]===state.view)?.[2]||'研究总览';
  const head=pageHeading(title,`Core / ${h(client.projectId)} · 本机研究工作台`,btn('live-refresh','刷新读取','secondary','refresh'));
  const notice=`<div class="notice blue">${icon('shield',16)}<p>此视图仅展示真实 Core 返回的数据。化学品库存为实验室共享；任务与检索限定项目 ${h(client.projectId)}。读取失败会显示错误。</p></div>`;
  if(state.view==='inventory') {
    const result=state.live.inventory;
    return head+notice+(result?.error?liveError(result.error):!result?loading():`<section class="panel"><div class="table-wrap"><table class="data-table"><thead><tr><th>物料</th><th>当前余量</th><th>存放位置</th><th>版本</th><th>范围</th></tr></thead><tbody>${(result.items||[]).map(i=>`<tr><td class="name-cell"><strong>${h(i.name)}</strong><small>${h(i.chemical_id||i.id)} · ${h(i.formula)}</small></td><td><span class="amount">${h(i.amount)}</span><span class="unit">${h(typeof i.unit==='object'?i.unit.symbol||JSON.stringify(i.unit):i.unit)}</span></td><td>${h(i.location)}</td><td>${h(i.revision)}</td><td>${badge('实验室共享')}</td></tr>`).join('')}</tbody></table></div>${!(result.items||[]).length?empty('Core 未返回物料','请检查当前身份的库存读取权限。'):''}</section>`);
  }
  if(state.view==='agents') {
    const result=state.live.tasks;
    return head+notice+(result?.error?liveError(result.error):!result?loading():`<section class="panel"><div class="panel-head"><h2>当前项目任务</h2><span class="count-label">${(result.tasks||[]).length}</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>任务 ID</th><th>标题</th><th>状态</th><th>详情</th></tr></thead><tbody>${(result.tasks||[]).map((t,i)=>`<tr data-action="live-task" data-index="${i}" tabindex="0" role="button"><td>${h(t.task_id||t.id)}</td><td>${h(t.title||t.task_type||t.kind||'Core 任务')}</td><td>${badge(t.status||t.state||'未提供')}</td><td>${icon('arrow',14)}</td></tr>`).join('')}</tbody></table></div>${!(result.tasks||[]).length?empty('当前项目暂无可见任务','这里只显示该项目中由 Core 授权返回的任务。'):''}</section>`);
  }
  if(state.view==='knowledge'){
    const result=state.live.samples;
    if(!client.features.samples)return head+notice+`<div class="live-block">${icon('book',40)}<h2>该 Core 未提供样品列表读取</h2><p>当前 Core 版本 ${h(client.coreVersion||'未知')} 未声明 query.samples.list。仍可使用全局检索查看已授权证据候选。</p>${btn('search','检索 Core','primary','search')}</div>`;
    return head+notice+(result?.error?liveError(result.error):!result?loading():liveKnowledgeHtml(result));
  }
  if(state.view==='resources'){
    if(!client.features.resources)return head+notice+`<div class="live-block">${icon('calendar',40)}<h2>该 Core 未提供资源读取</h2><p>当前 Core 版本 ${h(client.coreVersion||'未知')} 未声明 resource.list；预约提交仍未开放。</p></div>`;
    const res=state.live.resources, bk=state.live.bookings;
    return head+notice+`<section class="panel"><div class="panel-head"><h2>资源</h2><span class="count-label">${(res?.resources||[]).length}</span></div>${res?.error?liveError(res.error):!res?loading():`<div class="table-wrap"><table class="data-table"><thead><tr><th>资源</th><th>类型</th><th>项目</th><th>状态</th><th>容量</th></tr></thead><tbody>${(res.resources||[]).map(r=>`<tr><td><strong>${h(r.name)}</strong><small>${h(r.resource_id)}</small></td><td>${h(r.kind)}</td><td>${h(r.project_id||'实验室共享')}</td><td>${badge(r.status||'unknown',r.status==='active'?'green':'orange')}</td><td>${h(r.capacity)}</td></tr>`).join('')}</tbody></table></div>${!(res.resources||[]).length?empty('暂无可见资源','Core 未返回所选项目或共享资源。'):''}`}</section><section class="panel" style="margin-top:18px"><div class="panel-head"><h2>预约记录</h2><span class="count-label">${(bk?.bookings||[]).length}</span></div>${bk?.error?liveError(bk.error):!bk?loading():`<div class="table-wrap"><table class="data-table"><thead><tr><th>预约</th><th>资源</th><th>状态</th><th>开始（UTC 存储）</th><th>结束</th></tr></thead><tbody>${(bk.bookings||[]).map(b=>`<tr><td>${h(b.booking_id)}</td><td>${h(b.resource_id)}</td><td>${badge(b.status||'unknown',b.status==='confirmed'?'green':'orange')}</td><td>${h(b.slot_start)}</td><td>${h(b.slot_end)}</td></tr>`).join('')}</tbody></table></div>${!(bk.bookings||[]).length?empty('暂无预约记录','当前连接只读，不能在网页提交预约。'):''}`}</section>`;
  }
  if(state.view==='overview')return head+notice+taskComposer()+`<section class="hero" style="display:block"><div class="hero-copy"><div class="eyebrow">CONNECTED TO YOUR LAB</div><h2>工作台已连接<br><span>你的本机 Core。</span></h2><p>库存、项目样品、任务、证据检索与预约记录来自真实 Core；所有读取都限定在项目 ${h(client.projectId)}。可通过上方任务会话委托 Agent，执行结果与待确认事项会分别显示。</p><div class="hero-actions">${btn('goto','查看库存','primary','box','data-view="inventory"')}${btn('goto','查看样品','secondary','book','data-view="knowledge"')}${btn('search','检索记录','quiet','search')}</div></div></section><div class="section-label">当前连接</div><div class="panel" style="padding:24px"><p class="prose">项目：${h(client.projectId)}<br>Core 版本：${h(client.coreVersion||'未提供')}<br>模式：读取 + 受控任务会话 · 凭据由本地桥管理<br>能力：${Object.entries(client.features||{}).map(([k,v])=>`${h(k)}=${v?'✓':'—'}`).join(' · ')||'未提供'}<br>缺失读取能力：${h((client.missingReadCapabilities||[]).join(', ')||'无')}<br>证据原图：在证据工作区按项目权限读取</p><pre class="live-result">${h(JSON.stringify(state.live.health||{},null,2))}</pre></div>`;
  if(state.view==='evidence'){
    if(!client.features.evidence)return head+notice+`<div class="live-block">${icon('evidence',40)}<h2>该 Core 未提供证据列表</h2><p>当前 Core 版本 ${h(client.coreVersion||'未知')} 未声明 evidence.pages.list。</p></div>`;
    const listing=state.live.evidence;
    if(!listing)return head+notice+loading();
    if(listing.error)return head+notice+liveError(listing.error);
    const pages=listing.pages||[];
    const selected=pages.find(p=>p.page_id===state.evidenceSelected)||null;
    const page=state.evidencePage, proposal=state.evidenceProposal, preview=state.evidencePreview;
    const listHtml=pages.length?pages.map(p=>`<button type="button" class="evidence-list-item ${p.page_id===state.evidenceSelected?'active':''}" data-action="evidence-select" data-id="${h(p.page_id)}"><div class="evidence-list-thumb">${icon(p.mime&&p.mime.startsWith('image/')?'evidence':'file',16)}</div><div><strong>${h(p.filename||p.page_id)}</strong><small>${h(p.notebook_code)} · 第 ${h(p.page_order)} 页 · ${h(p.status)}</small><span>${p.preview_materialized?'预览 ✓':'无预览'} · ${h(p.proposal_count)} 个提案</span></div></button>`).join(''):empty('暂无证据记录','上传照片或文件后，原件、转写和提案会出现在这里。');
    let detail='';
    if(!selected){
      detail=`<div class="live-block">${icon('evidence',36)}<h2>选择左侧记录</h2><p>查看原件、已采纳知识与纠错记录。</p></div>`;
    } else if(state.evidenceBusy || !page){
      detail=loading();
    } else if(page.error){
      detail=liveError(page.error);
    } else {
      detail=evidenceDetailHtml(page,selected,proposal,preview);
    }
    return head+notice+`<div class="evidence-workbench"><aside class="evidence-list"><div class="evidence-list-head"><span class="eyebrow">RECENT EVIDENCE</span><strong>${pages.length} 条记录</strong></div>${listHtml}</aside><div class="evidence-main">${detail}</div></div>`;
  }
  return head+notice+`<div class="live-block">${icon('calendar',40)}<h2>正式预约暂未开放</h2><p>当前连接仅支持读取资源与预约记录，不能在网页提交预约；演示工作区可以体验预约草稿。</p>${btn('demo','返回演示工作区','quiet','arrow')}</div>`;
}
const loading=()=>`<div class="loading-line">${icon('refresh',18)}正在读取 Core…</div>`;
const liveError=msg=>`<div class="notice orange">${icon('alert',17)}<p>${h(msg)} · 未加载演示数据。</p></div>`;
async function loadLive(view=state.view) {
  if(state.mode!=='live')return;
  const epoch=contextEpoch;
  const keys=view==='inventory'?['inventory']
    :view==='agents'?['tasks']
    :view==='overview'?['health']
    :view==='evidence'?(client.features.evidence?['evidence']:[])
    :view==='knowledge'?(client.features.samples?['samples']:[])
    :view==='resources'?[...(client.features.resources?['resources']:[]),...(client.features.bookings?['bookings']:[])]
    :[];
  if(!keys.length)return;
  for(const key of keys)state.live[key]=null;
  render();
  for(const key of keys){
    try {
      const result=await client[key]();
      if(state.mode==='live'&&epoch===contextEpoch)state.live[key]=result;
    } catch(err) {
      if(state.mode==='live'&&epoch===contextEpoch)state.live[key]={error:err.message};
    }
  }
  if(state.mode==='live'&&epoch===contextEpoch)render();
  if(view==='evidence'&&state.live.evidence&&!state.live.evidence.error){
    const pages=state.live.evidence.pages||[];
    const target=pages.find(page=>page.page_id===state.evidenceSelected)||pages[0];
    if(target&&state.evidencePage?.page_id!==target.page_id)await selectEvidencePage(target.page_id);
  }
}
function navigate(view, options={}) {
  if(!nav.some(n=>n[0]===view))view='overview';
  state.view=view;state.filter='all';state.query='';state.mobile=false;state.editing=null;
  if(options.evidenceId)state.evidenceId=options.evidenceId;
  if(location.hash.slice(1)!==view)history.replaceState(null,'','#'+view);
  closeOverlay(false);render();window.scrollTo({top:0,behavior:'instant'});
  if(state.mode==='live')loadLive(view);
}

function openOverlay(content, kind='modal', focusSelector) {
  detailRequestSeq++;
  lastFocus=document.activeElement;activeOverlay=kind;
  $('#overlay-root').innerHTML=`<div class="overlay ${kind==='drawer'?'drawer-overlay':''}" data-dismiss="true"><section class="${kind==='drawer'?'drawer':'modal'}" role="dialog" aria-modal="true" aria-label="${kind==='drawer'?'详细信息':'工作台对话框'}" tabindex="-1">${content}</section></div>`;
  document.body.style.overflow='hidden';$('#app').inert=true;
  requestAnimationFrame(()=>($(focusSelector||'input:not([type="file"]),button,textarea,select', $('#overlay-root'))||$('[role="dialog"]')).focus());
}
function closeOverlay(restore=true) {
  if(state.busy){toast('正在测试连接，请等待本机 Core 响应后关闭。','clock');return;}
  detailRequestSeq++;
  $('#overlay-root').innerHTML='';document.body.style.overflow='';$('#app').inert=false;activeOverlay=null;
  searchSeq++;clearTimeout(searchTimer);
  if(restore&&lastFocus?.isConnected)lastFocus.focus();
}
function modal(title,subtitle,body,kind='modal') {
  openOverlay(`<div class="modal-header"><div><h2>${title}</h2><p>${subtitle}</p></div>${closeButton()}</div><div class="modal-body">${body}</div>`,kind);
}
function showSample(id) {
  const s=samples.find(x=>x.id===id);if(!s)return;
  modal(h(s.name),`${h(s.id)} / RESEARCH TIMELINE`,`<span class="eyebrow">SAMPLE KNOWLEDGE</span><div class="detail-meta">${sampleBadge(s)}${badge(s.owner,'outline')}${badge(projects.find(p=>p.id===s.projectId)?.name,'outline')}</div><p class="prose">${h(s.summary)}</p><div class="timeline">${s.events.map(e=>`<article class="timeline-event"><span class="time">${h(e.date)} / ${h(e.time)}</span><h3>${h(e.title)}</h3><p>${h(e.body)}</p><div class="timeline-fields">${e.fields.map(f=>`<div class="timeline-field">${h(f.label)}<strong>${h(f.value)}</strong></div>`).join('')}</div><button class="source-link" data-action="evidence" data-id="${h(e.sourceId)}">${icon('link',11)}${h(e.sourceId)} · 原始来源 v1 ${icon('arrow',11)}</button></article>`).join('')}</div>${btn('export-sample','导出样品记录','secondary','download',`data-id="${h(s.id)}"`)}`,'drawer');
}
function showTask(id) {
  const t=tasks.find(x=>x.id===id);if(!t)return;const [label,color]=taskStatus(t);
  modal(h(t.title),`${h(t.id)} / ${h(t.agent)}`,`<div class="detail-meta">${badge(label,color,true)}${badge('演示工作流','outline')}</div><p class="prose">${h(t.description)}</p><div class="task-steps">${t.steps.map((s,i)=>`<div class="task-step ${s.state}"><span class="step-symbol">${s.state==='done'?icon('check',12):i+1}</span><span>${h(s.label)}</span>${s.state==='current'?badge('当前步骤','outline'):''}</div>`).join('')}</div><div class="task-output"><span class="eyebrow">RESULT / HANDOFF</span><p style="margin-top:8px">${h(t.output)}</p></div><div class="notice" style="margin-top:20px">${icon('shield',15)}<p>这是任务快照。事实确认、库存操作与消息送达具有各自的状态；此页面没有启动真实 Agent。</p></div>${t.id==='TASK-1042'?btn('evidence','前往核对原页','primary','evidence','data-id="EV-026"'):t.id==='TASK-1038'?btn('upload','加入表征文件','primary','upload'):btn('goto','查看相关知识','secondary','book','data-view="knowledge"')}`,'drawer');
}
function showChemical(id) {
  const i=inventory.find(x=>x.id===id);if(!i)return;
  modal(h(i.name),`${h(i.id)} / INVENTORY RECORD`,`<div style="font:500 38px Manrope,sans-serif;margin:8px 0 25px;color:#77915c">${h(i.formula)}</div><div class="detail-meta">${badge(i.category,'outline')}${badge(i.hazard,'outline')}${badge(i.amount<i.threshold?'余量低于阈值':'库存充足',i.amount<i.threshold?'orange':'green')}</div><div class="timeline-fields">${[['当前余量',i.amount+' '+i.unit],['提醒阈值',i.threshold+' '+i.unit],['存放位置',i.location],['记录版本','revision '+i.revision]].map(([label,value])=>`<div class="timeline-field">${label}<strong>${h(value)}</strong></div>`).join('')}</div><div class="notice" style="margin-top:22px">${icon('box',16)}<p>演示台账 · 实验室共享库存。纸笔记录的整理与确认不会自动创建历史领用，也不会扣减这里的余量。</p></div>${btn('assistant-inventory','询问库存情况','primary','spark')}`,'drawer');
}

function showUpload() {
  if(state.mode==='live'){toast('当前连接为只读。请返回演示工作区体验本地导入。','alert');return;}
  uploadedFile=null;
  modal('加入一份实验记录','IMPORT / 记录是知识的起点',`<form data-form="upload"><div class="upload-drop" id="upload-drop">${icon('upload',29)}<h3>拖入笔记，或点击选择文件</h3><p>支持常见科研格式 · 单文件不超过 20 MB</p><input type="file" id="upload-file" accept=".png,.jpg,.jpeg,.gif,.bmp,.tif,.tiff,.webp,.pdf,.txt,.md,.csv,.tsv,.json,.xml,.yaml,.yml,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.cif,.pdb,.mol,.sdf,.fasta,.fastq,.vcf,.mzml,.mzxml,.jdx,.fcs,.cdf,.nc,.h5,.hdf5,image/*,application/pdf,text/*" aria-label="选择一份实验记录"></div><div id="file-info"></div><div class="form-row" style="margin-top:22px"><div class="form-group"><label for="record-title">记录名称</label><input class="form-control" id="record-title" name="title" placeholder="例如：薄膜生长 · 实验笔记" required maxlength="100"></div><div class="form-group"><label for="record-sample">关联样品</label><select class="form-control" name="sample" id="record-sample">${projectSamples().map(s=>`<option value="${h(s.id)}">${h(s.id)}</option>`).join('')}</select></div></div><div class="form-group"><label for="record-note">手动转录 / 补充说明</label><textarea class="form-control" rows="4" id="record-note" name="note" placeholder="可以先输入一段文字。图片与 PDF 不会被自动转录，也不会上传云端。" maxlength="5000"></textarea></div><div class="notice" style="margin-bottom:0">${icon('shield',16)}<p>文件只在当前浏览器中预览。此演示不上传文件、不运行 OCR；需要手动输入，或保留“待转录”状态。</p></div><div id="upload-error"></div><div class="form-actions">${btn('close','取消') }<button class="btn btn-primary" type="submit">${icon('plus',15)}建立本地草稿</button></div></form>`);
}
function fileSelected(file) {
  if(!file)return;
  if(file.size>15*1024*1024){toast('文件超过 15 MB，请选择更小的文件。','alert');return;}
  const allowed=/\.(png|jpe?g|webp|pdf|txt|md)$/i.test(file.name);
  if(!allowed){toast('请选择图片、PDF 或文本文件。','alert');return;}
  uploadedFile=file;
  $('#file-info').innerHTML=`<div class="file-selected">${icon('file',17)}<span>${h(file.name)} · ${(file.size/1024).toFixed(1)} KB</span>${badge('仅本机')}</div>`;
  if(!$('#record-title').value)$('#record-title').value=file.name.replace(/\.[^.]+$/,'');
}
function submitUpload(form) {
  const fd=new FormData(form),title=fd.get('title').trim(),note=fd.get('note').trim();
  if(!uploadedFile&&!note){$('#upload-error').innerHTML='<div class="form-error">请选择文件，或输入一段手动转录。</div>';return;}
  const id='LOCAL-'+Date.now().toString(36).toUpperCase();
  const safeImage=uploadedFile&&['image/png','image/jpeg','image/webp'].includes(uploadedFile.type);
  const e={id,title,sampleId:fd.get('sample'),date:new Date().toISOString().slice(0,10),status:'pending',confidence:null,asset:safeImage?URL.createObjectURL(uploadedFile):null,fileName:uploadedFile?.name,transcript:note||'待转录：文件已在本浏览器选择。此演示未执行 OCR。',fields:note?[{id:id+'-note',label:'手动输入的记录',value:note,unit:'',sourceText:'由当前用户手动输入 · 未经来源验证',status:'review',confidence:0}]:[]};
  evidence.unshift(e);state.uploaded.push(id);
  const sample=samples.find(s=>s.id===e.sampleId);sample.events.unshift({id:id+'-event',date:e.date,time:new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}),title:'新增本地记录草稿',body:'当前浏览器中的临时导入，未上传至 Core。'+(note?'已附手动转录。':'等待转录。'),type:'evidence',sourceId:id,fields:[{label:'状态',value:'本地草稿 · 未发布'}]});
  uploadedFile=null;syncEvidenceState();navigate('evidence',{evidenceId:id});toast('已建立本地草稿；刷新页面后上传文件需重新选择。');
}
function showReview(id) {
  const e=evidence.find(x=>x.id===id);if(!e)return;
  if(e.status==='confirmed'){modal('确认记录','DEMONSTRATION / 仅浏览器状态',`<div class="notice">${icon('check',16)}<p>此条示例已确认。原始页面 v1 与本地修订历史继续保留。没有写入 Core，也没有改变真实库存。</p></div>${btn('versions','查看修订历史','secondary','clock',`data-id="${h(e.id)}"`)}`);return;}
  if(!e.fields.length){toast('这份记录尚待转录，请先补充可核对的字段。','alert');return;}
  modal('确认这一份记录','REVIEW / 保留来源与版本',`<form data-form="confirm" data-id="${h(e.id)}"><p class="prose">请对照原始页面检查每个字段。若原文不清晰，保留待复核状态，向实验者核实后再确认。</p><div class="confirm-list" style="margin-top:20px">${e.fields.map(f=>`<label><input type="checkbox" name="checked" value="${h(f.id)}" required><span>${h(f.label)}：<strong>${h(f.value)} ${h(f.unit)}</strong><small style="display:block;color:#9baa8b;font-size:9px">${h(f.sourceText)}</small></span></label>`).join('')}</div><div class="notice orange" style="margin:22px 0 0">${icon('alert',16)}<p>此确认仅用于交互演示，不能作为真实实验结论。计划值与实测值继续分别保留，不触发库存扣减。</p></div><div class="form-actions">${btn('close','暂不确认')}<button class="btn btn-primary" type="submit">${icon('check',14)}保存演示确认</button></div></form>`);
}
function confirmEvidence(id) {
  const e=evidence.find(x=>x.id===id);if(!e)return;e.status='confirmed';
  state.versions.push({id,at:new Date().toISOString(),action:'演示确认',fields:clone(e.fields)});syncEvidenceState();persist();closeOverlay();render();toast('演示记录已确认。原页与修订历史已保留。');
}
function showVersions(id) {
  const versions=state.versions.filter(v=>v.id===id);
  modal('记录修订历史',`${h(id)} / LOCAL VERSIONS`,`<div class="notice">${icon('clock',16)}<p>原始页面始终为 v1。以下为本浏览器保存的演示字段修订；不会回写原始记录。</p></div><div class="timeline">${versions.length?versions.slice().reverse().map((v,i)=>`<div class="timeline-event"><span class="time">v${versions.length-i+1} · ${h(new Date(v.at).toLocaleString('zh-CN'))}</span><h3>${h(v.action)}</h3><p>${h(v.detail||'人工逐项核对后的演示确认。')}</p></div>`).join(''):`<div class="timeline-event"><h3>原始示例 v1</h3><p>尚无本地修订。</p></div>`}</div>`);
}
function showSettings(error='') {
  if(state.mode==='demo'&&!['127.0.0.1','localhost','[::1]'].includes(location.hostname)){
    modal('在你的本机连接实验室','ONLINE PREVIEW / 合成示例',`<div class="notice">${icon('link',16)}<p>线上版本用于体验界面。本机工作台提供 Core 连接，凭据由本地服务管理。</p></div><div class="prose"><h3>打开本机工作台</h3><p>运行随源码提供的 start.ps1，然后打开本机预览地址，即可在设置中填写 Core 地址和项目。</p><p>线上演示中的字段修订保存在当前浏览器；你可以随时恢复示例。</p></div><div class="form-actions">${btn('reset-demo','重置演示','secondary','refresh')}${btn('close','继续体验','primary','arrow')}</div>`);return;
  }
  if(state.mode==='live'){
    modal('Core 已连接','CONNECTION / LOCAL, CONTROLLED',`<div class="notice">${icon('link',16)}<p>当前项目：${h(client.projectId)}。真实读取与演示数据完全分开。</p></div><p class="prose">支持库存、项目样品、任务、证据检索与首页统一任务会话。后台任务会在本地桥内存中保留执行所需凭据；断开网页连接后仍会继续。清除模型将暂停任务，重新配置后可恢复。</p><div class="divider"></div><div class="model-status"><span class="eyebrow">REAL MODEL</span><strong>${client.modelConfigured?h(client.modelName||'已配置')+(client.visionModel?(' · OCR '+h(client.visionModel)):''):'未配置真实模型'}</strong><p>${client.modelConfigured?('主 Agent 负责规划、选择工具与委派专业角色；Core 核验权限、证据与版本。'):'未配置时首页任务框使用确定性规则解析，不调用大模型。'}</p><div style="display:flex;gap:9px;margin-top:12px">${btn('model-config',client.modelConfigured?'修改模型配置':'配置真实模型','secondary','spark')}${client.modelConfigured?btn('model-clear','清除模型','quiet','close'):''}</div></div><div class="form-actions">${btn('demo','断开并返回演示','primary','logout')}</div>`);return;
  }
  modal('连接你的实验室','CONNECT / 在本机继续工作',`<div class="notice">${icon('shield',16)}<p>通过本地服务连接 Core，读取研究记录并提交受控任务。只接受本机地址，具体可执行操作由当前身份权限决定。</p></div><form data-form="connect" autocomplete="off"><div class="form-group"><label for="core-endpoint">Core 地址</label><input class="form-control" name="endpoint" id="core-endpoint" value="http://127.0.0.1:8765" required placeholder="http://127.0.0.1:8765"></div><div class="form-row"><div class="form-group"><label for="core-project">项目 ID</label><input class="form-control" name="projectId" id="core-project" required placeholder="Core 中的明确项目 ID"></div><div class="form-group"><label for="core-key">API key</label><input class="form-control" name="apiKey" id="core-key" type="password" autocomplete="off" required placeholder="仅保留在连接会话内存"></div></div><p class="small subtle">静态线上预览只运行演示。真实连接请在本机启动随附前端服务；无需将 Core 暴露到公网。</p><div id="connect-error">${error?`<div class="form-error">${h(error)}</div>`:''}</div><div class="form-actions">${btn('close','继续演示')}<button class="btn btn-primary" type="submit">${icon('link',15)}测试并连接</button></div></form><div class="divider"></div><div style="display:flex;justify-content:space-between;align-items:center"><span class="small subtle">本浏览器的演示修改</span>${btn('reset-demo','重置演示','quiet','refresh')}</div>`);
}
let modelSettingsSeq = 0;
const selectOptions = (values, selected) => values.map(([value,label]) => `<option value="${h(value)}" ${value===selected?'selected':''}>${h(label)}</option>`).join('');
function modelSettingsBody(current = {}, error = '') {
  const c = {...MODEL_DEFAULTS,...publicModelSettings(current),...(current.configured?{visionBaseUrl:current.visionBaseUrl||'',visionModel:current.visionModel||''}:{})};
  const schemes = modelSchemes(localStorage);
  const field = (name,label,placeholder='',type='text') => `<div class="form-group"><label for="setting-${name}">${label}</label><input id="setting-${name}" class="form-control" name="${name}" type="${type}" value="${h(c[name] ?? '')}" placeholder="${h(placeholder)}" ${['baseUrl','model','maxTokens','contextWindow'].includes(name)?'required':''} ${type==='number'?'min="1" step="1"':''}></div>`;
  const select = (name,label,values) => `<div class="form-group"><label for="setting-${name}">${label}</label><select id="setting-${name}" class="form-control" name="${name}">${selectOptions(values,c[name])}</select></div>`;
  return `<div class="notice">${icon('shield',16)}<p>密钥只提交给本机桥，不写入浏览器存储。命名方案仅保存地址、模型和参数。修改端点后请重新填写对应密钥。</p></div>
    <form data-form="model" autocomplete="off"><fieldset class="model-fieldset"><details class="model-advanced"><summary>快速切换 · 模板与本机方案</summary>
    <div class="model-scheme-row"><div class="form-group"><label for="model-template">配置模板</label><select id="model-template" class="form-control">${MODEL_TEMPLATES.map((item,index)=>`<option value="${index}">${h(item.name)}</option>`).join('')}</select></div>${btn('model-template-load','填入模板','secondary')}</div>
    <div class="model-scheme-row"><div class="form-group"><label for="model-scheme">本机命名方案</label><select id="model-scheme" class="form-control"><option value="">选择已保存方案</option>${schemes.map((item,index)=>`<option value="${index}">${h(item.name)}</option>`).join('')}</select></div>${btn('model-scheme-load','填入方案','secondary')}${btn('model-scheme-delete','删除','quiet')}</div>
    <p class="small subtle">填入模板或方案后，点击底部“测试并启用”才会切换。主模型与视觉模型将分别测试。</p></details>
    <h3 class="model-section-title">主模型</h3>
    ${select('protocol','API 协议',[['openai-chat','Chat Completions · /chat/completions'],['openai-responses','Responses · /responses']])}
    ${field('baseUrl','服务 Base URL','https://…/v1')}${field('model','模型 ID','填写服务提供的模型名称')}
    <div class="form-group"><label for="model-key">主模型 API key</label><input class="form-control" name="apiKey" id="model-key" type="password" autocomplete="new-password" placeholder="${current.keyConfigured?'已有密钥；同一端点留空保留':'远程端点填写密钥；本机可留空'}"><small>同端点留空保留已配置密钥；新远程端点必须填写。本机免密钥服务可勾选下项。</small><label class="model-checkbox"><input type="checkbox" name="clearMainKey">本机服务不使用已存密钥</label></div>
    ${current.configured?`<p class="small subtle">当前已启用的思考设置：${h(current.effectiveReasoningEffort === "provider_default" ? "服务默认" : current.effectiveReasoningEffort || current.reasoningEffort || "自动")} · ${h(current.effectiveReasoningFormat || current.reasoningFormat || "自动适配")}。以下参数测试成功后替换。</p>`:""}
    <details class="model-advanced" open><summary>推理与输出预算</summary><div class="form-row">${select('reasoningEffort','思考强度',[['auto','自动 · 按接口适配'],['off','关闭（须服务支持）'],['minimal','minimal'],['low','low'],['medium','medium'],['high','high'],['xhigh','xhigh'],['max','max']])}${select('reasoningFormat','思考兼容格式',[['auto','自动适配'],['none','不发送思考参数'],['openai','OpenAI reasoning_effort'],['zai','ZAI / GLM thinking'],['deepseek','DeepSeek thinking'],['qwen','Qwen enable_thinking'],['openrouter','OpenRouter reasoning']])}</div><p class="small subtle">自动模式适合初次连接；显式强度须模型支持。原生 GLM-5.3 支持 low / high / max；Qwen 开关格式支持自动或关闭。Responses 支持自动、OpenAI 或不发送。</p><div class="form-row">${field('maxTokens','单次最大输出 tokens','','number')}${field('contextWindow','上下文窗口 tokens','','number')}</div></details>
    <details class="model-advanced" ${c.visionBaseUrl||c.visionModel?'open':''}><summary>独立视觉模型 · 图片识别（可选）</summary><p class="small subtle">视觉调用使用 Chat Completions。地址留空时使用主端点；模型留空时使用主模型，须支持图片。独立端点的密钥不会从主端点自动转发。</p>${field('visionBaseUrl','视觉 Base URL','留空使用主模型端点')}${field('visionModel','视觉模型 ID','留空使用主模型 ID')}<div class="form-group"><label for="vision-key">视觉 API key</label><input class="form-control" name="visionApiKey" id="vision-key" type="password" autocomplete="new-password" placeholder="${current.visionKeyConfigured?'已有视觉密钥；同端点留空保留':'同主端点可复用；独立远程端点填写'}"><label class="model-checkbox"><input type="checkbox" name="clearVisionKey">本机视觉服务不使用已存密钥</label></div></details>
    <div class="model-scheme-row"><div class="form-group"><label for="model-scheme-name">保存当前参数为本机方案</label><input id="model-scheme-name" class="form-control" maxlength="60" placeholder="例如：日常整理 / 本机视觉"></div>${btn('model-scheme-save','保存方案','secondary')}</div>
    <div id="model-error" role="alert">${error?`<div class="form-error">${h(error)}</div>`:''}</div><div class="form-actions">${btn('close','取消')}<button class="btn btn-primary" type="submit">${icon('spark',15)}测试并启用</button></div></fieldset></form>`;
}
async function showModelSettings() {
  const seq = ++modelSettingsSeq;
  modal('模型与接口设置','主模型 · 视觉接口 · 思考设置',`<div id="model-loading-${seq}" class="loading-line" role="status">正在读取当前模型配置…</div>`);
  $('.modal')?.classList.add('model-settings-modal');
  try {
    const current = await client.modelStatus();
    if (!document.getElementById(`model-loading-${seq}`)) return;
    $('.modal-body').innerHTML = modelSettingsBody(current);
  } catch (err) {
    if (!document.getElementById(`model-loading-${seq}`)) return;
    $('.modal-body').innerHTML = modelSettingsBody(client.modelConfig || {}, `当前配置读取失败：${err.message}。可保留输入后重试。`);
  }
}
function fillModelForm(settings) {
  const form = $('form[data-form="model"]'); if (!form) return;
  for (const [name,value] of Object.entries(publicModelSettings(settings))) { const input=form.elements.namedItem(name); if(input)input.value=String(value); }
  for (const name of ['apiKey','visionApiKey']) form.elements.namedItem(name).value='';
  for (const name of ['clearMainKey','clearVisionKey']) form.elements.namedItem(name).checked=false;
  $('#model-error').innerHTML='';
  toast('参数已填入；测试并启用后生效，密钥未从方案读取。');
}
function modelSchemeAction(action) {
  const form=$('form[data-form="model"]'); if(!form)return;
  try {
    if(action==='model-template-load'){fillModelForm(MODEL_TEMPLATES[Number($('#model-template').value)]);return;}
    const schemes=modelSchemes(localStorage), index=$('#model-scheme').value;
    if(action==='model-scheme-load'){if(index==='')throw Error('请先选择一个命名方案。');fillModelForm(schemes[Number(index)]);return;}
    let updated;
    if(action==='model-scheme-save') updated=saveModelScheme(localStorage,$('#model-scheme-name').value,modelPayload(new FormData(form)));
    if(action==='model-scheme-delete') {if(index==='')throw Error('请先选择要删除的方案。');updated=schemes.filter((_,i)=>i!==Number(index));localStorage.setItem('chemclaw.model-schemes.v1',JSON.stringify(updated));}
    if(updated){$('#model-scheme').innerHTML='<option value="">选择已保存方案</option>'+updated.map((item,i)=>`<option value="${i}">${h(item.name)}</option>`).join('');toast(action==='model-scheme-save'?'方案已保存，仅含非秘密参数。':'方案已删除。');}
  }catch(err){$('#model-error').innerHTML=`<div class="form-error">${h(err.message)}</div>`;}
}
async function submitModelConfig(form) {
  const button=$('[type="submit"]',form),fieldset=$('fieldset',form);
  let payload;
  try {payload=modelPayload(new FormData(form));payload.reuseApiKey=!form.elements.namedItem('clearMainKey').checked;payload.reuseVisionApiKey=!form.elements.namedItem('clearVisionKey').checked;}
  catch(err){$('#model-error').innerHTML=`<div class="form-error">${h(err.message)}</div>`;return;}
  const epoch=contextEpoch;
  state.busy=true;fieldset.disabled=true;button.textContent='正在测试主模型与视觉连接…';$('#model-error').innerHTML='';
  try {
    const result=await client.configureModel(payload);
    state.busy=false;
    if(epoch!==contextEpoch||!form.isConnected)return;
    closeOverlay();render();await restoreJobs();toast(`模型已启用：${result.model}${result.visionTest==='ok'?' · 视觉测试通过':''}`);
  }catch(err){
    state.busy=false;
    if(form.isConnected){$('#model-error').innerHTML=`<div class="form-error">${h(String(err.message).replace(/[。.!！]+$/, ""))}。原配置未替换，请修改后重试。</div>`;fieldset.disabled=false;button.innerHTML=icon('spark',15)+'测试并启用';}
  }finally{payload.apiKey='';payload.visionApiKey='';}
}

function showTaskReader(index) {
  const message=state.taskChat[Number(index)];if(!message||message.role==='user')return;
  modal('研究助手答复','阅读视图 · 可横向查看表格',`<div class="reply-tools">${btn('task-copy','复制原文','secondary','',`data-index="${Number(index)}"`)}</div><div class="markdown-body answer-reading">${renderMarkdown(message.text)}</div>${knowledgeDeliveryHtml(message.knowledgeDelivery)}`);
  $('.modal')?.classList.add('answer-reader');
}
async function copyTaskReply(index) {
  const text=state.taskChat[Number(index)]?.text;if(!text)return;
  try {await navigator.clipboard.writeText(text);toast('答复原文已复制。');}
  catch {modal('复制答复原文','浏览器未开放剪贴板；可全选复制',`<textarea class="form-control copy-reply" readonly aria-label="答复原文">${h(text)}</textarea>`);$('.modal')?.classList.add('answer-reader');$('.copy-reply')?.select();}
}

async function submitConnect(form) {
  const fd=new FormData(form),button=$('[type="submit"]',form);button.disabled=true;button.textContent='正在连接…';$('#connect-error').innerHTML='';
  state.busy=true;
  try{const r=await client.connect({endpoint:fd.get('endpoint'),apiKey:fd.get('apiKey'),projectId:fd.get('projectId')});resetContext();state.mode='live';state.live={health:r.health};state.busy=false;navigate('overview');await restoreJobs();toast('已连接本机 Core，可读取记录并提交受控任务。');}
  catch(err){if(form.isConnected){$('#connect-error').innerHTML=`<div class="form-error">${h(err.message)}</div>`;button.disabled=false;button.innerHTML=icon('link',15)+'测试并连接';}state.busy=false;}
}
async function demoMode(){resetContext();try{await client.disconnect();}catch{}state.mode='demo';navigate('overview');toast('已返回合成示例工作区。');}
function showProject() {
  if(state.mode==='live'){showSettings();return;}
  modal('切换研究项目','WORKSPACE / 演示项目',`${projects.map(p=>`<button class="project-option ${p.id===state.projectId?'active':''}" data-action="project-select" data-id="${h(p.id)}"><span class="workspace-avatar">${p.code[0]}</span><span><strong>${h(p.name)}</strong><p>${h(p.description)}</p></span>${p.id===state.projectId?icon('check',17):icon('arrow',17)}</button>`).join('')}`);
}

function showSearch() {
  openOverlay(`<div class="command-input">${icon('search',21)}<input id="global-query" placeholder="${state.mode==='live'?'检索当前 Core 项目的记录…':'试试“用量”“MOF-026”或“乙醇”…'}" aria-label="全局搜索" autocomplete="off"><button data-action="close" aria-label="关闭搜索"><kbd>ESC</kbd></button></div><div class="command-results" id="command-results"></div><div class="search-footer"><span>${state.mode==='live'?'Core 项目范围 · 真实返回':'合成示例 · 样品 / 证据 / 库存 / 任务'}</span><span>↑ ↓ 选择 · Enter 打开</span></div>`);
  $('[role="dialog"]').classList.add('search-modal');renderSearch('');
}
async function renderSearch(q) {
  const target=$('#command-results');if(!target)return;const seq=++searchSeq;
  if(state.mode==='live'){
    if(!q.trim()){target.innerHTML='<div class="empty-state" style="padding:35px"><p>输入关键词，检索当前项目的证据候选。</p></div>';return;}
    target.innerHTML=loading();
    try{const r=await client.search(q);if(seq!==searchSeq||!$('#command-results'))return;state.live.search=r;const candidates=Array.isArray(r.candidates)?r.candidates:[];target.innerHTML=candidates.map((c,i)=>`<button class="command-result" data-action="live-search-result" data-index="${i}">${icon('evidence',19)}<span class="command-result-content">${h(c.title||c.object_id||c.sample_id||c.event_id||c.claim_id||'证据候选 '+(i+1))}<small>${h(c.text||c.chunk||c.summary||c.snippet||'查看 Core 返回的来源和字段')}</small></span>${icon('chevron',13)}</button>`).join('')||empty('没有匹配的候选','请尝试其他关键词。');}
    catch(err){if(seq===searchSeq&&$('#command-results'))target.innerHTML=liveError(err.message);}return;
  }
  const match=v=>!q||v.toLowerCase().includes(q.toLowerCase());
  const items=[...projectSamples().filter(s=>match(s.id+s.name+s.summary+s.tags.join(''))).map(s=>({action:'sample',id:s.id,title:s.name,desc:s.id+' · '+s.summary,type:'样品',glyph:'beaker'})),...projectEvidence().filter(e=>match(e.id+e.title+e.transcript)).map(e=>({action:'evidence',id:e.id,title:e.title,desc:e.sampleId+' · 原始记录',type:'证据',glyph:'evidence'})),...inventory.filter(i=>match(i.name+i.formula+i.id)).map(i=>({action:'chemical',id:i.id,title:i.name,desc:i.amount+' '+i.unit+' · '+i.location,type:'库存',glyph:'box'})),...projectTasks().filter(t=>match(t.title+t.description+t.id)).map(t=>({action:'task',id:t.id,title:t.title,desc:t.agent+' · '+t.id,type:'任务',glyph:'agents'}))].slice(0,15);
  target.innerHTML=items.map(r=>`<button class="command-result" data-action="${r.action}" data-id="${h(r.id)}">${icon(r.glyph,19)}<span class="command-result-content">${h(r.title)}<small>${h(r.desc.slice(0,56))}</small></span><span class="command-result-type">${r.type}</span>${icon('arrow',13)}</button>`).join('')||empty('没有找到匹配内容','换个关键词试试。');
}
function showAssistant(initial='') {
  openOverlay(`<div class="modal-header"><div><h2>研究助手 <span class="pill outline">${state.mode==='live'?'Core 引用回答':'演示交互'}</span></h2><p>RESEARCH COPILOT / ASK WITH EVIDENCE</p></div>${closeButton()}</div><div class="modal-body" id="chat-body"></div><div class="chat-compose"><form class="chat-form" data-form="chat"><input name="message" aria-label="向研究助手提问" placeholder="${state.mode==='live'?'询问当前项目记录…':'例如，批次 026 的用量是多少？'}" required maxlength="500"><button type="submit" aria-label="发送问题">${icon('send',17)}</button></form><p>${state.mode==='live'?'确定性引用回答：不调用模型；来源不支持时显示 needs_review / partial。':'预设示例回答 · 未连接大模型 · 只从合成记录中引用来源'}</p></div>`,'drawer');
  $('.drawer').classList.add('assistant-drawer');renderChat();if(initial)answerChat(initial);
}
function renderChat(){
  const body=$('#chat-body');if(!body)return;
  body.innerHTML=state.chat.length?state.chat.map(m=>`<div class="chat-message ${m.role==='user'?'user':''}">${m.role==='assistant'?'<div class="eyebrow">CHEMCLAW / '+(state.mode==='live'?'CORE ANSWER':'DEMO RESPONSE')+'</div>':''}${m.role==='assistant'?`<div class="markdown-body">${renderMarkdown(m.text)}</div>`:h(m.text).replace(/\n/g,'<br>')}${m.source?`<button class="source-link" data-action="${state.mode==='live'?'live-event':'evidence'}" data-id="${h(m.source)}">${icon('link',11)}${h(m.source)} ${icon('arrow',11)}</button>`:''}</div>`).join(''):`<div class="chat-intro">${icon('spark',33)}<h3>好的问题，<br>从可靠的记录开始。</h3><p>用一句话找到研究上下文。${state.mode==='live'?'确定性引用回答：不调用模型；无来源或来源不支持时显示缺口。':'这里的演示回答只引用合成示例，不生成新的实验结论。'}</p>${state.mode==='demo'?`<div class="chat-suggestions">${(state.projectId==='p-mof'?['批次 026 的用量是多少？','哪些库存需要补充？','批次 025 的薄膜观察是什么？']:['水相浸泡后的材料稳定吗？','哪些库存需要补充？']).map(q=>`<button data-action="chat-suggestion" data-query="${h(q)}">${h(q)}${icon('arrow',14)}</button>`).join('')}</div>`:''}</div>`;
  body.scrollTop=body.scrollHeight;
}
async function answerChat(q){
  const epoch=contextEpoch;
  state.chat.push({role:'user',text:q});renderChat();
  if(state.mode==='live'){
    try{
      if(client.features.answer){
        const r=await client.answer(q);if(epoch!==contextEpoch)return;
        const claims=Array.isArray(r.claims)?r.claims:[], gaps=Array.isArray(r.gaps)?r.gaps:[];
        const unsupported=claims.filter(c=>c.verify&&c.verify.source_supports===false).length;
        const head=`状态：${r.status||'unknown'} · claims ${claims.length} · 来源不支持 ${unsupported} · 缺口 ${gaps.length}`;
        const body=r.rendered_text||claims.map(c=>c.statement).filter(Boolean).join('\n')||'没有可用的引用式回答。';
        state.chat.push({role:'assistant',text:head+'\n\n'+body,source:(claims.find(c=>c.object_type==='experiment_event')||{}).object_id});
      }else{
        const r=await client.search(q);if(epoch!==contextEpoch)return;const list=r.candidates||[];
        state.chat.push({role:'assistant',text:list.length?`Core 返回 ${list.length} 条证据候选。以下是原始检索字段（未生成结论）：\n`+JSON.stringify(list.slice(0,3),null,2):'当前项目未找到匹配记录。未生成无来源的答案。'});
      }
    }
    catch(err){if(epoch!==contextEpoch)return;state.chat.push({role:'assistant',text:err.message+'\n读取失败，未加载演示数据。'});}renderChat();return;
  }
  let reply;
  if(/用量|026|0.5|0.8/.test(q)){
    if(state.projectId!=='p-mof')reply={text:'批次 MOF-026 属于另一个演示项目。请切换至“MOF 薄膜研究”查看它的记录。'};
    else{const e=evidence.find(e=>e.id==='EV-026'),mass=e.fields.find(f=>f.id==='f-026-mass'),plan=e.fields.find(f=>f.id==='f-026-plan');reply={text:`MOF-026 当前字段版本：\n\n计划用量：${plan.value} ${plan.unit}。\n实测候选：${mass.value} ${mass.unit}。\n状态：${e.status==='confirmed'?'已完成本浏览器的演示核对':'仍待对照原页核实'}。\n\n原始示例笔记始终保留疑似 0.5 g；本地修改单独记录，不能改写原文。不同事实类型分别保留，未扣减库存。`,source:'EV-026'};}
  }
  else if(/库存|补充|乙醇|耗材/.test(q))reply={text:'演示台账中有 2 项低于各自阈值：\n\n• 乙醇：85 mL，提醒阈值 100 mL。\n• 玻璃基底：8 片，提醒阈值 10 片。\n\n没有执行采购或库存调整。不同物料与单位分别呈现。'};
  else if(/025|薄膜观察/.test(q))reply={text:'批次 MOF-025 的示例记录描述为“连续浅白色薄膜”。外观观察不能替代结晶质量、膜厚或产率测量。',source:'EV-025'};
  else if(/水|稳定|浸泡|XRD/.test(q)&&state.projectId!=='p-aqua')reply={text:'相关浸泡记录属于“水相稳定性”演示项目。请切换项目查看观察与待补充证据。'};
  else if(/水|稳定|浸泡|XRD/.test(q))reply={text:'目前不能从现有记录确认结构稳定性。\n\n已记录：浸泡 24 h，pH 6.8，边缘轻微发白。\n缺失：浸泡后的 XRD 原始数据与可比测试条件。\n\n定性观察不等同于结构稳定结论。建议补充表征证据。',source:'EV-008'};
  else reply={text:'这是一个使用预设回答的交互演示，目前没有连接大模型。\n\n你可以询问“批次 026 的用量”“哪些库存需要补充”或“水相浸泡是否稳定”，体验带来源的回答；也可以使用全局搜索查看其他记录。'};
  state.chat.push({role:'assistant',...reply});renderChat();
}

function showActivity(){
  if(state.mode==='live'){modal('工作动态','CORE / 当前连接状态',`<p class="prose">当前 Core 没有供此页面使用的动态订阅接口。请查看项目任务；这里不显示演示动态。</p>${btn('goto','查看任务','primary','agents','data-view="agents"')}`);return;}
  modal('工作动态','15 OCT 2026 / 合成示例',activity.map(a=>`<article class="all-activity"><span class="all-activity-icon">${icon(a.type==='inventory'?'box':a.type==='review'?'alert':'evidence',17)}</span><div><h3>${h(a.title)}</h3><p>${h(a.body)}</p><time>${h(a.time)} · 示例时间</time></div></article>`).join(''),'drawer');
}
const bookings=[];
function showBooking(id,hour) {
  const r=resources.find(x=>x.id===id);const slot=resourceSlot(r,hour);if(slot.state==='busy'||slot.state==='closed')return;
  const existing=bookings.find(b=>b.id===id&&b.hour===hour);
  if(existing){toast('该时段已有本地草稿，可在“本地预约草稿”查看。','alert');return;}
  modal('建立预约草稿',`${h(r.name)} / DEMONSTRATION`,`${slot?.state==='tentative'?`<div class="notice orange">${icon('alert',16)}<p>此时段已有示例计划：${h(slot.title)}。需核实时段，不能在演示中确认。</p></div>`:''}<form data-form="booking" data-id="${h(id)}" data-hour="${hour}"><div class="timeline-fields"><div class="timeline-field">设备<strong>${h(r.name)}</strong></div><div class="timeline-field">时间<strong>10 月 15 日 ${hour}:00—${hour+1}:00</strong></div></div><div class="form-group" style="margin-top:18px"><label for="booking-purpose">使用目的</label><input id="booking-purpose" class="form-control" name="purpose" required maxlength="200" placeholder="例如：MOF-025 薄膜表征"></div><p class="prose">日期为演示日，时区 UTC+08:00。这里只创建浏览器内存中的草稿，未占用真实设备时段。</p><div class="form-actions">${btn('close','取消')}<button class="btn btn-primary" type="submit" ${slot?.state==='tentative'?'disabled':''}>保存本地草稿</button></div></form>`);
}
function showBookings(){modal('本地预约草稿','DRAFTS / 未提交 Core',bookings.length?bookings.map(b=>`<div class="all-activity"><div><h3>${h(resources.find(r=>r.id===b.id)?.name)}</h3><p>示例日 10 月 15 日 ${b.hour}:00—${b.hour+1}:00 · UTC+08:00</p><p>${h(b.purpose)}</p>${badge('本地草稿 · 未占用','orange')}</div><button class="icon-btn" data-action="booking-delete" data-index="${bookings.indexOf(b)}" aria-label="删除草稿">${icon('close',14)}</button></div>`).join(''):empty('还没有预约草稿','选择设备的可用时段开始。'));}
function download(name,data){const blob=new Blob([typeof data==='string'?data:JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('演示记录已导出，文件包含来源与演示标识。');}
function showHelp(){modal('让研究上下文保持连贯','CHEMCLAW / HUMAN + AGENT',`<div class="prose"><p>这个工作台把现有架构的证据、知识、库存、Agent 任务和仪器整合为一个入口。</p><h3>从一份记录开始</h3><p>导入文件或手动输入 → 在证据工作区对照原页 → 沿样品时间线回查来源 → 与研究助手协作。</p><h3>当前交付</h3><p>演示数据全部合成。文件预览在本机，图片不会自动发送给模型。字段修订保留在浏览器；上传文件与预约草稿仅在本次打开期间保留。</p><h3>连接实际系统</h3><p>通过随附本地服务连接 Core，可读取库存、项目任务与检索候选。原图读取、真实 OCR、业务写入与微信 / QQ 入口需后续接入。</p></div>`);}

document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');
  if(!el){if(event.target.matches('[data-dismiss]'))closeOverlay();return;}
  const a=el.dataset.action,id=el.dataset.id;
  if(el.disabled)return;
  switch(a){
    case 'close':closeOverlay();break;
    case 'goto':navigate(el.dataset.view);break;
    case 'mobile-open':state.mobile=true;render();break;
    case 'mobile-close':state.mobile=false;render();break;
    case 'sample':showSample(id);break;
    case 'task':showTask(id);break;
    case 'chemical':showChemical(id);break;
    case 'evidence':{const e=evidence.find(x=>x.id===id);const s=samples.find(x=>x.id===e?.sampleId);if(s)state.projectId=s.projectId;state.selectedField=null;navigate('evidence',{evidenceId:id});break;}
    case 'filter':state.filter=el.dataset.filter;render();break;
    case 'upload':showUpload();break;
    case 'field-edit':state.editing=id;render();requestAnimationFrame(()=>$('form[data-form="field"] input')?.focus());break;
    case 'field-source':{state.selectedField=id;const e=evidence.find(x=>x.id===state.evidenceId);const f=e?.fields.find(x=>x.id===id);$$('.field-item').forEach(x=>x.classList.toggle('selected',x.dataset.field===id));const hi=$('#source-highlight');if(f?.region&&e.asset){const [x,y,w,t]=f.region;Object.assign(hi.style,{display:'block',left:x+'%',top:y+'%',width:w+'%',height:t+'%'});}else{hi.style.display='none';toast('原文：'+(f?.sourceText||'手动输入；暂无页面坐标'),'link');}break;}
    case 'transcript':{const e=evidence.find(x=>x.id===id);modal('原页转录',`${h(id)} / 合成示例或手动输入`,`<p class="prose">此转录不是实际 OCR 结果。保留原始文字，字段修订单独记录。</p><div class="transcript">${h(e.transcript)}</div>`);break;}
    case 'zoom':{const e=evidence.find(x=>x.id===id);if(!e?.asset)return;modal('原始页面',`${h(e.id)} / ${e.id.startsWith('LOCAL-')?'本机文件':'合成示例'}`,`<img src="${h(e.asset)}" alt="${h(e.title)}完整页面">`);$('.modal').classList.add('image-modal');break;}
    case 'review-confirm':showReview(id);break;
    case 'versions':showVersions(id);break;
    case 'settings':showSettings();break;
    case 'model-config':showModelSettings();break;
    case 'model-template-load':case 'model-scheme-load':case 'model-scheme-save':case 'model-scheme-delete':modelSchemeAction(a);break;
    case 'task-read':showTaskReader(el.dataset.index);break;
    case 'task-copy':await copyTaskReply(el.dataset.index);break;
    case 'model-clear':{try{await client.clearModel();closeOverlay();render();await restoreJobs();toast('模型配置已清除；后台任务暂停，重新配置后可恢复。');}catch(err){toast(err.message,'alert');}break;}
    case 'demo':await demoMode();break;
    case 'project':showProject();break;
    case 'project-select':resetContext();state.projectId=id;navigate(state.view);toast('已切换到 '+project().name);break;
    case 'search':showSearch();break;
    case 'evidence-select':selectEvidencePage(id);break;
    case 'task-evidence':state.evidenceSelected=id;state.evidencePage=null;state.evidenceProposal=null;navigate('evidence');break;
    case 'live-sample':showLiveSample(id);break;
    case 'event-correct':await showEventCorrection(id);break;
    case 'transcript-correct':showTranscriptCorrection(id);break;
    case 'evidence-refresh':refreshEvidenceDetail();break;
    case 'evidence-ocr':{try{const result=await client.ocr(state.evidenceSelected);await refreshEvidenceDetail();toast(result.warning||'转写已保存并默认采纳；样品资料正在后台整理。',result.knowledgeQueued===false?'alert':undefined);}catch(err){toast(err.message,'alert');}break;}
    case 'task-remove-file':state.taskFiles.splice(Number(el.dataset.index),1);renderTaskFiles();break;
    case 'task-refresh':await restoreJobs();break;
    case 'task-job':{const job=state.taskJobs.find(item=>item.job_id===id);if(job){stopJobPolling();state.taskPollError='';showJob(job);if(jobActive(job))pollJob(id);}break;}
    case 'task-new':stopJobPolling();state.taskJobId=null;state.taskSessionId=null;state.taskChat=[];state.taskBusy=false;state.taskPending=null;state.taskContinuePending=null;state.taskPollError='';updateTaskPanel();break;
    case 'task-cancel':await cancelCurrentJob();break;
    case 'task-continue':await continueCurrentJob();break;
    case 'task-retry':if(asyncJobsEnabled()&&!state.taskBusy&&state.taskPending)await submitBackgroundTask(state.taskPending.payload,state.taskPending.submittedFiles,true);break;
    case 'task-suggestion':{const input=$('#task-text');if(input){input.value=el.dataset.text||'';state.taskDraft=input.value;input.focus();}break;}
    case 'assistant':showAssistant();break;
    case 'assistant-inventory':showAssistant('哪些库存需要补充？');break;
    case 'chat-suggestion':answerChat(el.dataset.query);break;
    case 'activity':showActivity();break;
    case 'help':showHelp();break;
    case 'live-refresh':loadLive();break;
    case 'live-timeline':showLiveSample(id);break;
    case 'live-event':await showLiveEvent(id);break;
    case 'live-page':{try{const r=await client.page(id);modal('页面元数据与转写',`Core / ${h(id)} · 只读`,`<div class="notice orange">${icon('evidence',15)}<p>此接口返回页面元数据与当前转写，不返回原图字节。</p></div><pre class="live-result">${h(JSON.stringify(r,null,2))}</pre>`,'drawer');}catch(err){modal('读取失败','Core / 只读',liveError(err.message),'drawer');}break;}
    case 'live-task':{const t=state.live.tasks?.tasks[Number(el.dataset.index)];modal('Core 任务详情','原始返回字段 · 只读',`<pre class="live-result">${h(JSON.stringify(t,null,2))}</pre>`,'drawer');break;}
    case 'live-search-result':{const c=state.live.search?.candidates[Number(el.dataset.index)];const extra=c?.object_type==='experiment_event'?`<div style="display:flex;gap:9px;margin-top:14px"><button class="btn btn-primary" data-action="live-event" data-id="${h(c.object_id)}">读取事件与修订</button>${c.page_id?`<button class="btn btn-quiet" data-action="live-page" data-id="${h(c.page_id)}">读取页面转写</button>`:''}</div>`:'';modal('Core 证据候选','原始返回 · 尚未做来源支持验证',`<div class="notice orange">${icon('evidence',16)}<p>检索命中本身不等于已证实结论。当前仅展示候选的 Core 字段。</p></div><pre class="live-result">${h(JSON.stringify(c,null,2))}</pre>${extra}`,'drawer');break;}
    case 'booking':showBooking(id,Number(el.dataset.hour));break;
    case 'booking-list':showBookings();break;
    case 'booking-delete':bookings.splice(Number(el.dataset.index),1);showBookings();toast('已删除本地预约草稿。');break;
    case 'export':download('ChemClaw-DEMO-'+project().code.replace(/\W/g,'')+'.json',{synthetic:true,project:project(),samples:projectSamples(),evidence:projectEvidence().map(({asset,...rest})=>rest),note:'合成示例，非真实实验数据'});break;
    case 'export-inventory':download('ChemClaw-DEMO-inventory.json',{synthetic:true,scope:'lab_shared',items:inventory});break;
    case 'export-sample':download('ChemClaw-DEMO-'+id+'.json',{synthetic:true,sample:samples.find(s=>s.id===id)});break;
    case 'reset-demo':modal('重置演示修改','RESET / 仅影响本浏览器',`<p class="prose">将清除本浏览器保存的演示字段修订与确认状态。真实 Core 不受影响。</p><div class="form-actions">${btn('close','取消')}${btn('reset-confirm','重置示例','danger','refresh')}</div>`);break;
    case 'reset-confirm':{for(const e of evidence)if(e.asset?.startsWith('blob:'))URL.revokeObjectURL(e.asset);samples=clone(sampleSeed);evidence=clone(evidenceSeed);tasks=clone(taskSeed);state.versions=[];state.uploaded=[];state.chat=[];bookings.length=0;localStorage.removeItem(storageKey);navigate('overview');toast('演示工作区已恢复初始状态。');break;}
  }
});
document.addEventListener('submit',event=>{
  const form=event.target.closest('[data-form]');if(!form)return;event.preventDefault();
  const type=form.dataset.form;
  if(type==='upload')submitUpload(form);
  if(type==='task')submitTask(form);
  if(type==='event-correction')submitEventCorrection(form);
  if(type==='transcript-correction')submitTranscriptCorrection(form);
  if(type==='model')submitModelConfig(form);
  if(type==='connect')submitConnect(form);
  if(type==='confirm')confirmEvidence(form.dataset.id);
  if(type==='field'){
    const e=evidence.find(x=>x.id===state.evidenceId),f=e?.fields.find(x=>x.id===form.dataset.id),value=new FormData(form).get('value').trim();
    if(!f||!value)return;
    const old=f.value;f.value=value;f.status='review';e.status='pending';state.versions.push({id:e.id,at:new Date().toISOString(),action:'手动修订候选字段',detail:f.label+'：'+old+' → '+value,fields:clone(e.fields)});state.editing=null;syncEvidenceState();persist();render();toast('字段修订已保存；原文未改变，仍需对照核实。');
  }
  if(type==='chat'){const input=$('input[name="message"]',form),q=input.value.trim();if(q){input.value='';answerChat(q);}}
  if(type==='booking'){bookings.push({id:form.dataset.id,hour:Number(form.dataset.hour),purpose:new FormData(form).get('purpose').trim()});closeOverlay();toast('已保存本地预约草稿，未占用真实设备。');}
});
document.addEventListener('input',event=>{
  if(event.target.name==='baseUrl'&&event.target.closest('form[data-form="model"]')) { const key=$('#model-key');if(key)key.value=''; }
  if(event.target.name==='visionBaseUrl'&&event.target.closest('form[data-form="model"]')) { const key=$('#vision-key');if(key)key.value=''; }
  if(event.target.id==='view-query'){const input=event.target,pos=input.selectionStart;state.query=input.value;render();const replacement=$('#view-query');replacement.focus();replacement.setSelectionRange(pos,pos);}
  if(event.target.id==='global-query'){clearTimeout(searchTimer);searchTimer=setTimeout(()=>renderSearch(event.target.value),state.mode==='live'?350:60);}
});
document.addEventListener('change',event=>{if(event.target.id==='upload-file')fileSelected(event.target.files[0]);});
document.addEventListener('input',event=>{if(event.target.id==='task-text')state.taskDraft=event.target.value;});
document.addEventListener('dragover',event=>{const drop=event.target.closest('#upload-drop');if(drop){event.preventDefault();drop.classList.add('dragover');}});
document.addEventListener('dragleave',event=>event.target.closest('#upload-drop')?.classList.remove('dragover'));
document.addEventListener('drop',event=>{const drop=event.target.closest('#upload-drop');if(drop){event.preventDefault();drop.classList.remove('dragover');fileSelected(event.dataTransfer.files[0]);}});
document.addEventListener('keydown',event=>{
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();showSearch();return;}
  if(event.key==='Escape'){if(activeOverlay)closeOverlay();else if(state.mobile){state.mobile=false;render();}return;}
  if(event.key==='Enter'&&event.target.matches('tr[role="button"]')){event.preventDefault();event.target.click();}
  if(activeOverlay){
    const dialog=$('[role="dialog"]'),focusable=$$('button:not([disabled]),input:not([disabled]),textarea,select,a[href],[tabindex="0"]',dialog).filter(x=>x.offsetParent!==null);
    if(event.key==='Tab'&&focusable.length){const first=focusable[0],last=focusable.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}
    if($('#global-query')&&['ArrowDown','ArrowUp'].includes(event.key)){const results=$$('.command-result');if(!results.length)return;event.preventDefault();const index=results.indexOf(document.activeElement);results[(index+(event.key==='ArrowDown'?1:-1)+results.length)%results.length].focus();}
  }
});
window.addEventListener('hashchange',()=>{const view=location.hash.slice(1);if(nav.some(n=>n[0]===view))navigate(view);});
window.addEventListener('resize',syncResponsiveNavigation);
$('.skip-link').addEventListener('click',event=>{event.preventDefault();$('#main-content').focus();});
window.addEventListener('beforeunload',()=>{for(const e of evidence)if(e.asset?.startsWith('blob:'))URL.revokeObjectURL(e.asset);});
async function bootstrapSession() {
  try {
    const result = await client.bootstrap();
    if (!result.connected) return;
    state.mode = 'live';
    state.live = { health: result.health || {} };
    if (result.projectId) state.projectId = result.projectId;
    render();
    await restoreJobs();
    await loadLive(state.view);
  } catch {
    /* Static preview or no bridge: stay in demo mode. */
  }
}

syncEvidenceState();
state.view=nav.some(n=>n[0]===location.hash.slice(1))?location.hash.slice(1):'overview';render();
bootstrapSession();



