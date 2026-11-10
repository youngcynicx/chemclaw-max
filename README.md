# ChemClaw Max

ChemClaw 的最新版源码、工程测试与可追溯开发评测归档。以事件、证据和受控事务组织实验室库存、实验记忆及任务规划；包含 Python Core、Pi 多 Agent 运行时和 Web 工作台。

本仓库于 **2026-11-10** 从 `ChemClaw-Lab-S10-Repair` 整理。产品候选版本为 **`fast-closure-2026-10-29-candidate1`**：两份提示及有界验证脚本的 SHA256 与原始版本记录一致。`max` 是仓库名称，不表示模型推理配置为 max；最新六图回归使用的是 `glm-5.3-flash`、zai/low。

## 当前结果与边界

| 验证 | 已保留的结果 | 必须保留的限制 |
| --- | --- | --- |
| 10 月 29 日单页开发验证 | 候选保存并读回 9 个事件，预定 6 组事实核对通过 | 复用 OCR；单轮开发材料；不证明整体提速 |
| 10 月 30 日英文六图回归 | 72 个事件；4 页 completed、2 页 partial；四道问答 93.67/100 | 已用过的合成材料；非独立盲评；有虚假更正历史和一个检查点解码失败 |
| 本次仓库整理 | 重新运行离线工程测试，补齐测试入口与文件清单 | 不重新调用付费模型，不将历史成绩改成新测试 |

详细报告：[快速整理优化与小例验证](docs/reports/ChemClaw_快速整理优化与小例验证_2026-10-29.md)、[最新版英文六图测试](docs/reports/ChemClaw_最新版英文六图测试报告_2026-10-30.md)。当前内容是研究开发版本，尚不能称全页无误、完整知识覆盖或长任务恢复可靠。

## 目录

```text
src/chemclaw_lab/                  Python Core：鉴权、事件、证据、事务、查询与 API
runtime/pi/                       Pi Agent 运行时、提示、预算/检查点/恢复测试
runtime/agents/                   角色清单
frontend/                         静态工作台、Python bridge 与界面测试
tests/                            Core 单元、集成与修复回归
scripts/                          配置、启动、验证和评测入口
benchmark/materials/mof-handwritten-en/  英文合成六页原图、输入与评分材料
docs/reports/                     关键开发报告
evidence/fast-closure-2026-10-29/   单页三组保留运行
evidence/english-suite-2026-10-30/  六图回归的筛选证据与原答复
evidence/repository-validation-2026-11-10/  本次工程测试日志
```

## 安装与测试

本次验证环境：Windows、Python 3.12、Node 24。建议使用 Node 24 运行完整测试（部分测试导入 `node:sqlite`）。

```sh
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e . -r requirements-dev.txt
cd runtime/pi
npm ci
cd ../..
python scripts/run_offline_tests.py
```

测试需要本机临时端口和临时数据库，不依赖生产数据库或真实模型密钥。安装可选图像处理能力：`python -m pip install -e ".[imaging]"`。具体命令、日志位置、历史评测的复现边界见 [测试说明](docs/TESTING.md)。

Core 最小示例：

```sh
chemclaw init-db
chemclaw seed-demo
chemclaw status
```

Web 工作台参见 [frontend/README.md](frontend/README.md)，Agent 配置参见 [运行时说明](docs/repair/AGENT_RUNTIME_2026-10-20_cn.md)。使用自己的本地配置和新数据库；仓库不会提供原机器的运行身份或模型密钥。

## 版本与证据

- [发布整理说明](docs/REPOSITORY_SNAPSHOT_2026-11-10.md)：来源、工程修正、排除项和当前验证结果。
- `SNAPSHOT_MANIFEST.json`：本次提交内容的逐文件 SHA256；可运行 `python scripts/verify_snapshot.py` 核对。
- `evidence/fast-closure-2026-10-29/final-version.json`：原候选的产品版本标识。
- 历史证据原样保留，内部本机路径是来源记录，部分评测脚本须另行配置后使用。
- 数据库、reader 凭证、私有配置、原始检查点、安装依赖和缓存不上传。英文笔记是合成测试材料。
- 较早的项目介绍保留在 [README_UPSTREAM.md](README_UPSTREAM.md)，其中历史状态与旧路径不代表当前交付状态。

## License

保留原项目 [MIT License](LICENSE)。

