# R00 施工基线（2026-10-17）

## 1. 路径

| 角色 | 实际路径 |
|---|---|
| 原始交付包 | `D:\Chem\ChemClaw-Lab-S10-Delivery.zip`（未覆盖） |
| 工作副本 WORK_ROOT | `D:\Chem\ChemClaw-Lab-S10-Repair` |
| 测试/临时目录 TEST_ROOT | `D:\Chem\ChemClaw-Lab-S10-Repair-test` |
| 验收产物 | `outputs/repair/` |

原交付包含 `node_modules`、示例数据库和少量历史产物；本轮以独立副本施工，未把审查机路径写入产品代码。

## 2. 环境

- Windows 10 (AMD64)，默认 Shell `cmd.exe`
- Python **3.12.13**（uv 管理的虚拟环境 `.venv`）
- pytest **9.1.1**
- 包版本：`chemclaw-lab 0.4.1`，schema **v11 → v12**
- Node：v22.14.0（通过 `CHEMCLAW_NODE_EXE` 发现，用于 Pi smoke/live）

环境创建命令：

```powershell
uv venv .venv --python 3.12
uv pip install -e . -r requirements-dev.txt --python .venv\Scripts\python.exe
```

## 3. 原始基线复现

```powershell
$env:PYTHONPATH = "$PWD\src"
.venv\Scripts\python.exe -m pytest tests -q `
  --basetemp D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest-temp `
  -o cache_dir=D:\Chem\ChemClaw-Lab-S10-Repair-test\pytest-cache
```

实际结果：

```text
268 passed in 213.02s
```

与交付说明的 268 passed 一致。

## 4. 当前源码指纹

- `src/chemclaw_lab/**/*.py`：41 个文件，SHA-256 `5862a8785a3302a6fb91bd8b01abc2ee14881f4fbf625bb624fcb1e5bb4d2476`
- `tests/**/*.py`：33 个文件，SHA-256 `f0ff8b3f59bd4de3d0623ecd422f59eb1658c4f5afef1ae3bf9941ad18fec2dd`

指纹对应当前工作副本，包含本轮修复与新增测试。

## 5. 修复后验证

```text
337 passed in 268.12s
```

其中：
- 原测试 268 条；
- 新增 `tests/repair/` 回归 69 条（63 条 G1/前端/会话契约 + 6 条 worker E2E）。

库存/基准：

```text
case_accuracy 0.933333 (140/150)
unsafe_write_rate 0.0
incorrect_write_rate 0.0
idempotency_violation_rate 0.0
trace_completeness_rate 1.0
```

产物：
- `outputs/repair/pytest_2026-10-17.xml`
- `outputs/repair/repair_baseline.{json,csv,md}`

## 6. 真实模型与前端

- `glm-5.3-flash` 的 S06/S08/S09 live 全部 `LIVE OK`；
- 前端桥 22 项测试通过，连接真实 Core 的 12 条读取路由通过；
- Chrome headless + CDP 完成首页连接、自然语言任务分派与文件上传测试；
- 详细记录：`docs/repair/live_pi_s06_s08_s09_2026-10-18.md`、`docs/repair/frontend_integration_cn.md`。

## 7. 说明

- 旧测试中的错误预期已按新安全语义修改，并保留原因注释：`user_version 11 → 12`、未到期 run 不可接管、发送超时后不自动重发等。
- `D:\Chem\ChemClaw-Lab-S10-Repair-test` 下的 pytest basetemp 与基准输出是可重建的测试产物，不属于产品数据。
