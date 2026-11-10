# 仓库整理与工程验证 — 2026-11-10

## 交付来源

产品源目录为 `D:/Chem/ChemClaw-Lab-S10-Repair`。依据用户指定的 10 月 29 日快速整理报告，纳入后续 10 月 30 日英文六图回归。该源目录没有 Git 历史，因此此仓库保存可校验的当前快照，不宣称恢复了历史提交。

两份提示 `runtime/pi/prompts/orchestrator.md`、`runtime/pi/prompts/b_capture.md` 及 `scripts/benchmark_fast_capture.py` 与 `fast-closure-2026-10-29-candidate1` 的原 SHA256 完全一致。Core、工作台业务代码和产品运行时未改动。

## 本次整理的改动

- 重新编写入口 README，保留旧版为 `README_UPSTREAM.md`；补充测试说明、离线测试入口和 SHA256 清单验证工具。
- 将源码、角色清单、前端静态源文件、测试、合成六图输入与评分材料放入对应目录；筛选历史证据并保留原报告的失败和限制。
- 一个前端集成测试改用已有 `find_node()`，移除作者机器的 Node 绝对路径。
- 四个 Node HTTP 集成测试用 `fileURLToPath()` 解析子进程入口，修复中文/空格目录中的 URL 编码路径导致启动失败。产品逻辑没有因此修改。
- `.gitignore` 排除本机依赖、数据库、配置、凭证、工作目录和缓存，同时明确保留必需的 `frontend/dist/` 静态源文件。

## 本次实际测试

环境：Windows、Python 3.12.13、Node 24.22.1。Python 使用原项目已安装的虚拟环境，并明确将新仓库 `src/` 放入 `PYTHONPATH`；Node 依赖复制自原项目已安装的 `node_modules`，仅用于本地验证，不提交。保留 `package-lock.json` 和 `uv.lock`。本次没有验证干净网络环境下的依赖重装。

| 测试组 | 实际结果 | 最终日志 |
| --- | --- | --- |
| Core pytest | 632 passed，443.79 秒 | `core-tests.log` |
| 前端 Python | 94 passed，97 subtests passed，20.49 秒 | `frontend-tests-final.log` |
| Pi Node | 236 passed，1 skipped，0 failed；总 237 项 | `node-tests-final.log` |
| 前端任务 UI | 14 项通过 | `ui-tests.log` |
| Markdown / 模型设置 | 12 项通过 | `markdown-tests.log` |

所有日志位于 `evidence/repository-validation-2026-11-10/`。Node 跳过项是可选的历史 P01 原始检查点复现，需要不随仓库分发的隔离评测数据库。不能将本次结果写为 237 项全部执行通过，也不把子测试、重跑和历史测试计数相加。

初次前端运行时 Node 依赖未就绪，1 项集成测试失败；依赖到位后运行最终完整前端测试通过。初次 Node 运行有 4 项子进程启动超时，定位为 URL pathname 未解码；上述路径修复后完整 Node 套件通过。初始日志保留在 `initial-attempts/`，不隐藏失败、不修改断言来绕过问题。

这是离线/本地受控工程验证，没有新模型调用、新 OCR、六图重跑、重新评分或生产数据库修改。历史模型缺陷仍以原报告为准。

## 排除项与证据完整性

不分发生产/测试数据库、reader 凭证、私有配置、raw checkpoints、模型隐藏推理、缓存、虚拟环境和 node_modules。既有六图公开证据包按原清单提取；单页证据采用明确文件清单。已检查已知本机凭证精确匹配和常见凭证格式；命中的代码字面量均为验证脱敏/拒绝策略的测试假值。

`SNAPSHOT_MANIFEST.json` 覆盖本次交付的所有文件（不包括清单自身）。`scripts/verify_snapshot.py` 验证这些文件的 SHA256。原有证据内部 manifest、版本 hash 和时间记录保留，新增整理不改写历史结论。

六图笔记是合成材料。当前低推理配置的六图结果为 4 completed / 2 partial；已知 OCR 下划线误当删除线、假纠错历史、P06 检查点解码失败和部分过度科学解释均保留。此仓库名称中的 max 不改变这些事实。
