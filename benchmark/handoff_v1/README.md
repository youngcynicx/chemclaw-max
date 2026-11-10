# 合成样品交接先导案例 v1

本目录输入为 **SIMULATED / text-only synthetic**，不是真实实验结果；不运行 OCR。`input/` 只有三页客观记录及六项任务。评分核对点另放 `evaluation/`，运行器不会读取该目录。

使用仓库虚拟环境执行；配置只提供模型设置，所有案例数据库均在新的运行目录中创建，不能指向业务主库。

```powershell
.\.venv\Scripts\python.exe scripts/run_handoff_case.py --config D:\Chem\ChemClaw-Lab-S10-Repair-test\ui-config.json --run-dir work/handoff-fresh-run --auto-advance
```

可用 `--fixture-dir` 更换输入目录。新运行会将四个输入文件冻结到运行目录的 `fixture/` 并计算 SHA256，拒绝覆盖已有案例数据库。每阶段合并回答两项任务；三个条件各运行三个阶段，不基于答案好坏自动重跑。

需要让独立 AI 分两轮读取时，去掉 `--auto-advance`，并加 `--keep-serving`。运行器在 multi 初始阶段结束后写出 `multi-initial-ready`；此时从 `multi/results.json` 的 `reader_config` 找到本机私有只读配置。完成初始独立读取后，在运行目录创建 `continue-multi` 文件，才会推进 multi 后续阶段。最终状态写出 `multi-final-ready`。`--keep-serving` 会保留隔离 Core，直到创建运行目录中的 `stop` 文件。

私有 reader 配置包含 API key，只供本机读取，不进入分享包；控制台与公开报告不打印密钥。服务重新启动后应使用结果文件记录的新 reader 配置和 endpoint。

产物包括 `pilot.json`、各条件 `results.json`、`case.db` 和每阶段数据库快照。direct 只有回答，不产生研究数据库。single 将三个角色可达工具的并集赋予单一根运行，使用公共与 capture 工作提示且禁止委派；multi 使用 orchestrator/capture/query 的产品提示及角色权限。二者根工具总预算与时限口径相同，实际调用次数、子角色时限和总模型开销不保证相等。provider 模型调用数和 token 用量若公开回执不可得，保留 null。

`--resume` 只恢复停止后的同一运行：已启动的模型 turn 不会重跑；仅发生在模型启动前的宿主失败可以恢复，并保留失败记录。当前首轮在初始交接后更换过隔离服务，未重跑任何已完成模型 turn。为保持首轮各阶段产品行为一致，恢复时曾用 `--legacy-normalizer` 从留存修前源码在隔离进程内加载旧归属形状函数；对应路径与 SHA256 记录在 `pilot.json.product_compatibility_override`。未来新运行默认使用当前产品，应与首轮修前结果分开报告。

首轮全九阶段真实调用完成；脚本已通过 `py_compile` 和 `--help` 检查。首轮单次先导不能用于显著性结论。

外部AI运行器见 `docs/EXTERNAL_HANDOFF_RUNNER_2026-10-23_cn.md`，两轮任务模板在 `external-tasks/`。实测与评分说明见 `docs/HANDOFF_PILOT_RESULTS_2026-10-23_cn.md`；本轮评分由开发Agent完成，不是人类受试者评分。

若持有服务的终端将退出，可先结束该终端服务，再用隐藏后台进程运行 `scripts/serve_handoff_snapshot.py --db CASE_DB --reader-config READER_CONFIG --stop-file NEW_STOP_PATH --ready-file READY_PATH`，在同一reader端口重新提供已保留的状态，无需重跑模型。结束时创建指定stop文件即可。

选择条件子集可加 `--conditions single multi`（只运行单体和多 Agent，不再运行 direct），或 `--conditions single`。不包含 multi 的子集不等待 `continue-multi`；包含 multi 并加 `--auto-advance` 会自动跑完其后续阶段。默认仍运行原来的三个条件。
