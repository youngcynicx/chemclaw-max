/**
 * ChemClaw Studio · synthetic demonstration workspace.
 * Every person, measurement and record below is fictional.
 * This module must never be used as an inventory or scientific source of truth.
 */

export const projects = [
  { id: 'p-mof', name: 'MOF 薄膜研究', code: 'MOF / 026', description: '合成示例 · 从一页实验笔记，到可回查的研究知识。' },
  { id: 'p-aqua', name: '水相稳定性', code: 'AQUA / 008', description: '合成示例 · 追踪浸泡条件、表征结果与样品变化。' },
];

export const samples = [
  {
    id: 'MOF-026', name: 'UiO 薄膜 · 批次 026', projectId: 'p-mof', status: '待复核', owner: '林研究员', updated: '10 分钟前',
    summary: '试剂用量存在两处不同记录。计划为 0.8 g，手写记录疑似 0.5 g，等待原始记录核对。', tags: ['UiO 系列', '薄膜生长', '用量待复核'],
    events: [
      { id: 'evt-026-03', date: '2026-10-15', time: '10:42', title: '发现用量差异', body: '计划单的 0.8 g 与手写笔记疑似 0.5 g 分别保留。原记录未被覆盖；库存没有发生扣减。', type: 'review', sourceId: 'EV-026', fields: [{ label: '事实类型', value: '实测记录 · 未确认' }, { label: '手写值', value: '0.5 g（疑似）' }, { label: '计划值', value: '0.8 g' }] },
      { id: 'evt-026-02', date: '2026-10-15', time: '09:40', title: '薄膜生长记录已整理', body: '工作液体积与保温条件已从合成示例笔记整理为候选字段，等待逐项确认。', type: 'evidence', sourceId: 'EV-026', fields: [{ label: '工作液体积', value: '20 mL' }, { label: '计划保温', value: '80 °C / 2 h' }, { label: '实测保温', value: '79–81 °C / 118 min' }] },
      { id: 'evt-026-01', date: '2026-10-14', time: '16:20', title: '建立样品与实验计划', body: '计划生成独立版本。计划值不会作为实测结果，也不会自动触发试剂领用。', type: 'plan', sourceId: 'EV-026', fields: [{ label: '计划前驱体用量', value: '0.8 g' }, { label: '基底', value: '玻璃片 / 4 枚' }] },
    ],
  },
  {
    id: 'MOF-025', name: 'UiO 薄膜 · 批次 025', projectId: 'p-mof', status: '进行中', owner: '陈研究员', updated: '42 分钟前',
    summary: '薄膜生长记录已核对，已关联首轮 XRD 表征。下一步为水相浸泡对比。', tags: ['UiO 系列', 'XRD', '待表征'],
    events: [
      { id: 'evt-025-03', date: '2026-10-15', time: '09:18', title: '原始记录已确认', body: '人工逐项核对了工作液体积、保温时间与笔记中的外观描述。这里只展示合成示例确认状态。', type: 'confirmed', sourceId: 'EV-025', fields: [{ label: '事实类型', value: '实验者记录 · 已确认' }, { label: '记录版本', value: 'v2' }, { label: '库存影响', value: '无关联库存操作' }] },
      { id: 'evt-025-02', date: '2026-10-14', time: '15:32', title: '形成连续浅白色薄膜', body: '外观为实验者的定性描述；不据此推断结晶质量、膜厚或产率。', type: 'observation', sourceId: 'EV-025', fields: [{ label: '外观记录', value: '连续浅白色薄膜' }, { label: '工作液体积', value: '20 mL' }] },
      { id: 'evt-025-01', date: '2026-10-14', time: '09:10', title: '批次开始', body: '将样品标识与笔记来源绑定，保留计划和实测两个命名空间。', type: 'plan', sourceId: 'EV-025', fields: [{ label: '计划保温', value: '80 °C / 2 h' }, { label: '实测保温', value: '80 °C / 120 min' }] },
    ],
  },
  {
    id: 'AQUA-008', name: '水相稳定性 · 对照 008', projectId: 'p-aqua', status: '进行中', owner: '林研究员', updated: '1 小时前',
    summary: '24 小时浸泡后的笔记已整理。pH 为原记录实测值；稳定性结论仍需表征支持。', tags: ['水相浸泡', '24 h', '结论待补充'],
    events: [
      { id: 'evt-008-02', date: '2026-10-15', time: '09:05', title: '24 小时观察已整理', body: '笔记中记载“边缘轻微发白”。尚无浸泡后 XRD 数据，系统不会自动生成“结构稳定”结论。', type: 'observation', sourceId: 'EV-008', fields: [{ label: '实测 pH', value: '6.8' }, { label: '实际浸泡时间', value: '24 h' }, { label: '观察', value: '边缘轻微发白' }] },
      { id: 'evt-008-01', date: '2026-10-14', time: '09:00', title: '启动水相对照', body: '关联 MOF-025 的对照片与浸泡条件，后续表征作为新的独立证据加入。', type: 'plan', sourceId: 'EV-008', fields: [{ label: '计划浸泡', value: '24 h / 室温' }, { label: '介质', value: '去离子水，20 mL' }] },
    ],
  },
];

// region is a manually authored illustration highlight [x%, y%, width%, height%].
// It locates text in our synthetic SVG page; it is not an OCR detection result.
export const evidence = [
  {
    id: 'EV-026', title: '薄膜生长 · 实验笔记 026', sampleId: 'MOF-026', date: '2026-10-15', status: 'pending', confidence: 87,
    asset: '/assets/notebook-01.svg',
    transcript: '15 OCT 2026 / Film growth — batch 026\nPlan: precursor 0.8 g; working solution 20 mL.\nActual note: precursor 0.5 g [?] (please verify original).\nMeasured: 79–81 °C, 118 min.\nObservation: thin white film; edges uneven.\nDo not merge planned and observed quantities.\n\n[合成示例：此转录由示例数据提供，未调用 OCR。]',
    fields: [
      { id: 'f-026-mass', label: '前驱体实测用量', value: '0.5', unit: 'g', confidence: 62, sourceText: 'Actual note: precursor 0.5 g [?]', status: 'review', region: [14, 34.7, 53, 4.7] },
      { id: 'f-026-plan', label: '前驱体计划用量', value: '0.8', unit: 'g', confidence: 98, sourceText: 'Plan: precursor 0.8 g', status: 'verified', region: [14, 21.8, 36, 4.7] },
      { id: 'f-026-volume', label: '工作液体积', value: '20', unit: 'mL', confidence: 97, sourceText: 'Working solution: 20 mL', status: 'verified', region: [14, 26.1, 42, 4.7] },
      { id: 'f-026-temp', label: '实测温度范围', value: '79–81', unit: '°C', confidence: 94, sourceText: 'Measured: 79–81 °C', status: 'verified', region: [14, 39, 38, 4.7] },
      { id: 'f-026-time', label: '实测保温时间', value: '118', unit: 'min', confidence: 96, sourceText: '118 min', status: 'verified', region: [49, 39, 19, 4.7] },
    ],
  },
  {
    id: 'EV-025', title: '薄膜生长 · 实验笔记 025', sampleId: 'MOF-025', date: '2026-10-14', status: 'confirmed', confidence: 98,
    asset: '/assets/notebook-02.svg',
    transcript: '14 OCT 2026 / Film growth — batch 025\nWorking solution: 20 mL.\nPlanned: 80 °C / 2 h.\nMeasured: 80 °C / 120 min.\nObservation: continuous pale-white film.\n4 glass substrates; label MOF-025.\nFollow-up: XRD, then water immersion comparison.\n\n[合成示例：此转录由示例数据提供，未调用 OCR。]',
    fields: [
      { id: 'f-025-volume', label: '工作液体积', value: '20', unit: 'mL', confidence: 99, sourceText: 'Working solution: 20 mL', status: 'verified', region: [14, 21.8, 59, 4.7] },
      { id: 'f-025-temp', label: '实测保温温度', value: '80', unit: '°C', confidence: 98, sourceText: 'Measured: 80 °C / 120 min', status: 'verified', region: [14, 34.7, 73, 4.7] },
      { id: 'f-025-time', label: '实测保温时间', value: '120', unit: 'min', confidence: 99, sourceText: 'Measured: 80 °C / 120 min', status: 'verified', region: [14, 34.7, 73, 4.7] },
      { id: 'f-025-observation', label: '外观描述', value: '连续浅白色薄膜', unit: '', confidence: 97, sourceText: 'Observation: continuous pale-white film.', status: 'verified', region: [14, 43.3, 76, 9] },
    ],
  },
  {
    id: 'EV-008', title: '水相浸泡 · 24 h 观察', sampleId: 'AQUA-008', date: '2026-10-15', status: 'pending', confidence: 95,
    asset: '/assets/notebook-03.svg',
    transcript: '15 OCT 2026 / Aqueous stability — control 008\nSource sample: MOF-025.\nDI water: 20 mL, room temperature.\nElapsed: 24 h. Measured pH: 6.8.\nObservation: slight whitening at edges.\nPost-immersion XRD: not yet available.\nConclusion: pending further characterization.\n\n[合成示例：此转录由示例数据提供，未调用 OCR。]',
    fields: [
      { id: 'f-008-duration', label: '实际浸泡时间', value: '24', unit: 'h', confidence: 99, sourceText: 'Elapsed: 24 h', status: 'verified', region: [14, 34.7, 44, 4.7] },
      { id: 'f-008-ph', label: '实测 pH', value: '6.8', unit: '', confidence: 98, sourceText: 'Measured pH: 6.8', status: 'verified', region: [14, 39, 51, 4.7] },
      { id: 'f-008-observation', label: '外观观察', value: '边缘轻微发白', unit: '', confidence: 95, sourceText: 'slight whitening at edges.', status: 'verified', region: [14, 47.6, 77, 4.7] },
      { id: 'f-008-conclusion', label: '稳定性结论', value: '待补充表征', unit: '', confidence: 91, sourceText: 'Conclusion: pending characterization.', status: 'review', region: [14, 77.6, 78, 4.7] },
    ],
  },
];

export const inventory = [
  { id: 'CHEM-001', name: 'N,N-二甲基甲酰胺', formula: 'C₃H₇NO', location: '试剂柜 A · 02', amount: 360, unit: 'mL', threshold: 100, hazard: '常规管理', revision: 12, category: '溶剂' },
  { id: 'CHEM-002', name: '乙醇', formula: 'C₂H₆O', location: '防火柜 B · 01', amount: 85, unit: 'mL', threshold: 100, hazard: '易燃', revision: 8, category: '溶剂' },
  { id: 'CHEM-003', name: '四氯化锆', formula: 'ZrCl₄', location: '干燥柜 C · 03', amount: 12.5, unit: 'g', threshold: 5, hazard: '遇水敏感', revision: 5, category: '前驱体' },
  { id: 'CHEM-004', name: '对苯二甲酸', formula: 'C₈H₆O₄', location: '试剂柜 A · 04', amount: 28.6, unit: 'g', threshold: 10, hazard: '常规管理', revision: 6, category: '配体' },
  { id: 'CHEM-005', name: '乙酸', formula: 'C₂H₄O₂', location: '酸类柜 D · 01', amount: 120, unit: 'mL', threshold: 50, hazard: '腐蚀性', revision: 9, category: '调节剂' },
  { id: 'CHEM-006', name: '去离子水', formula: 'H₂O', location: '样品准备区', amount: 1800, unit: 'mL', threshold: 500, hazard: '常规管理', revision: 3, category: '溶剂' },
  { id: 'CHEM-007', name: '玻璃基底', formula: '25 × 25 mm', location: '耗材柜 E · 02', amount: 8, unit: '片', threshold: 10, hazard: '常规管理', revision: 4, category: '耗材' },
  { id: 'CHEM-008', name: '氯化钠', formula: 'NaCl', location: '试剂柜 A · 06', amount: 240, unit: 'g', threshold: 50, hazard: '常规管理', revision: 2, category: '无机盐' },
];

export const tasks = [
  {
    id: 'TASK-1042', projectId: 'p-mof', title: '核对批次 026 的用量差异', agent: 'Knowledge Agent', status: 'waiting', progress: 66,
    description: '记录中同时出现计划值 0.8 g 和疑似实测值 0.5 g。已暂停事实发布，等待研究员核对原始笔记。', time: '10:42',
    steps: [{ label: '关联原始证据', state: 'done' }, { label: '整理候选字段', state: 'done' }, { label: '人工核对用量', state: 'current' }, { label: '发布确认版本', state: 'pending' }],
    output: '待确认字段：前驱体实测用量。计划值独立保留；未提交库存调整。',
  },
  {
    id: 'TASK-1041', projectId: 'p-aqua', title: '整理 24 h 浸泡观察', agent: 'Evidence Agent', status: 'running', progress: 72,
    description: '演示工作流：将浸泡时间、pH 与外观观察连接到 AQUA-008，并标注缺失的表征证据。', time: '10:35',
    steps: [{ label: '保存原始页面', state: 'done' }, { label: '提取示例字段', state: 'done' }, { label: '检查证据支持', state: 'current' }, { label: '生成复核草稿', state: 'pending' }],
    output: '当前可支持：24 h，pH 6.8，边缘轻微发白。结构稳定性结论仍为空。',
  },
  {
    id: 'TASK-1040', projectId: null, scope: 'lab_shared', title: '检查试剂与耗材余量', agent: 'Inventory Agent', status: 'done', progress: 100,
    description: '演示库存视图中有 2 项低于各自阈值。体积、质量和数量分开呈现，不进行跨单位求和。', time: '10:20',
    steps: [{ label: '读取演示台账', state: 'done' }, { label: '按物品单位检查', state: 'done' }, { label: '生成补充提醒', state: 'done' }],
    output: '乙醇 85 mL / 阈值 100 mL；玻璃基底 8 片 / 阈值 10 片。未采购、未扣减库存。',
  },
  {
    id: 'TASK-1039', projectId: 'p-mof', title: '更新薄膜样品时间线', agent: 'Knowledge Agent', status: 'done', progress: 100,
    description: '将批次 025 已确认的记录与原始页面关联；保留版本与确认来源。', time: '09:18',
    steps: [{ label: '解析样品标识', state: 'done' }, { label: '关联已确认记录', state: 'done' }, { label: '更新演示索引', state: 'done' }],
    output: 'MOF-025 时间线已更新。每条字段均可返回合成示例笔记。',
  },
  {
    id: 'TASK-1038', projectId: 'p-aqua', title: '等待浸泡后 XRD 证据', agent: 'Research Agent', status: 'waiting', progress: 25,
    description: '未获得表征数据，暂不能回答“材料在水中是否结构稳定”。可先查看已有观察与待补充项。', time: '09:05',
    steps: [{ label: '建立表征问题', state: 'done' }, { label: '补充 XRD 原始文件', state: 'current' }, { label: '人工审阅对比', state: 'pending' }],
    output: '缺失：浸泡前后可对比 XRD、测试条件与样品映射。系统未生成替代数据。',
  },
];

export const activity = [
  { id: 'act-01', time: '10:42', title: '有一项用量需要你核对', body: 'MOF-026 · 0.5 g 与计划 0.8 g 已分别保留。', type: 'review' },
  { id: 'act-02', time: '10:35', title: '一页笔记正在整理', body: 'AQUA-008 · pH、浸泡时间与原始来源已关联。', type: 'evidence' },
  { id: 'act-03', time: '10:20', title: '发现 2 项余量提醒', body: '乙醇与玻璃基底低于演示阈值。', type: 'inventory' },
  { id: 'act-04', time: '09:18', title: '批次 025 的记录已确认', body: '新增已确认版本；原始页面与旧版本继续保留。', type: 'confirmed' },
];

export const resources = [
  { id: 'RES-XRD', name: 'X 射线衍射仪', type: '表征设备', status: '可预约', location: '表征室 · 201', slots: [{ start: '09:00', end: '10:30', title: '标准样校准', state: 'busy' }, { start: '10:30', end: '12:00', title: '可用时段', state: 'free' }, { start: '14:00', end: '16:00', title: 'MOF 薄膜表征（示例计划）', state: 'tentative' }] },
  { id: 'RES-OVEN', name: '恒温干燥箱', type: '实验设备', status: '使用中', location: '合成室 · 103', slots: [{ start: '09:00', end: '12:00', title: '薄膜生长 · MOF-026', state: 'busy' }, { start: '13:00', end: '17:00', title: '可用时段', state: 'free' }] },
  { id: 'RES-BALANCE', name: '分析天平', type: '称量设备', status: '可使用', location: '样品准备区', slots: [{ start: '09:00', end: '09:30', title: '日常校准', state: 'busy' }, { start: '09:30', end: '17:00', title: '可用时段', state: 'free' }] },
];
