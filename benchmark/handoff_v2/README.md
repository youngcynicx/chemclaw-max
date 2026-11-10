# 文字模拟纵向留出案例 v2

六页纸本式文字模拟记录，按三个阶段提供，每阶段两页。所有标签、身份、操作与读数均为模拟；不是真实湿实验，不涉及图片或 OCR。此案例只用于小规模开发闭环与可追溯交接验证，不单凭一次成功主张泛化或多 Agent 优势。

`input/` 是唯一内部模型输入；`external-tasks/` 是外部研究任务，不含答案；`evaluation/rubric.json` 只供开发评测，不交给任何被测 Agent。评分检查实际阶段快照与原始答复，失败和额外修复成本单独保留。三阶段名称特意不使用旧运行器的 correction 分支。

`freeze_manifest.json` 冻结原文、任务、rubric 与外部研究任务的 SHA-256；开始实际运行前也冻结当时产品代码与提示哈希。结束后再次核对。输入或评分有改动时需作为新案例版本，不能覆盖原运行记录。

无模型校验：

```powershell
.venv\Scripts\python.exe scripts/run_longitudinal_case.py --validate-only
```

首次真实运行（会调用模型；按需选择条件）：

```powershell
.venv\Scripts\python.exe -u scripts/run_longitudinal_case.py --config D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json --run-dir work/handoff-v2-frozen-2026-10-24 --conditions direct single multi --keep-serving
```

三条件默认全选，所有状态型条件在各自独立数据库使用新项目 `simulated_film_longitudinal_v2`。每条件三次原始任务；direct 每阶段只看累计原文和本阶段研究问题，single/multi 使用真实 Pi/Core，single 复用原先单 Agent 的相同能力并集。一次运行目录只执行一次，不实现 resume、自动重试、审计补救或答案驱动写入。

`--keep-serving` 在任务结束后保留案例只读访问，给运行目录创建 `stop` 文件即可关闭这些案例服务。stop 在模型调用之间生效，不中断已启动模型或撤销写入。不要停止主项目的 4174/8876 服务。运行期间不修改产品代码、提示或冻结输入。

产物：`run.json`、各条件 `results.json`、各阶段 `.db`、各阶段 `*-readback.json`、实际 Pi 回执（含其完整工具轨迹）和保留数据库中的委派运行回执、`preflight-freeze.json` / `source-hash-postcheck.json`。reader JSON 含凭据，不可公开。完整调用/token 未完整可观测时为 null，不能从根运行或工具数推算；direct 三次宿主调用不等于所有条件相同推理成本。
