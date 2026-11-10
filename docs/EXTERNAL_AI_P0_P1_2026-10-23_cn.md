# 外部 AI 接口 P0/P1 交付与接入说明

日期：2026-10-23。承接 `EXTERNAL_AI_CONTEXT_PLAN_2026-10-23_cn.md`。本轮实现读取基础修复与最小样品上下文接口；人的界面未改动。未新增模型框架、数据库服务或第三方运行依赖。

## 1. 已交付的行为

- 普通只读能力每次重新执行、检查当前权限，不查/存永久 `operation_results` 读取缓存。旧缓存行保留，不再用于读请求。客户端使用UUID请求标识，重连不碰撞。HTTP读取可省略operation_id；写入仍要求幂等ID。
- 写入幂等继续保留，回放前核对嵌套项目及可识别源对象的当前归属。运行令牌、写入fencing、取消、heartbeat和后台任务路径仍有回归覆盖。
- 新增 `query.sample.context`：按项目和样品ID/唯一标签，返回明确关联的记录、精确引用原文、修订索引、共同事件、来源说明及质量缺口。一次聚合使用同一个SQLite只读事务，不调用内部LLM，不创建样品、提案、事件或库存交易。
- `query.samples.list` 新增 `view=catalog`：紧凑发现、标签/别名查询、稳定ID排序、limit/offset。默认展开视图保留用于现有工作台；q/sample_id/limit/offset必须显式使用catalog，避免参数被忽略。
- `memory.event.read` 支持精确revision、knowledge_as_of或完整include_history，三种选择器互斥。当前权限始终复核，旧版本与其来源也重新授权。保留原有序列化来源列兼容，并提供解析对象。
- `evidence.page.read` 支持精确transcript_revision和block_ids。不会把旧引用替换为新原文。历史转写按实际激活的成功/needs_human记录映射，失败尝试不占版本号，异常映射明确报错。
- 统一读取输入/输出契约，HTTP/Core、MCP、Pi共享；修正answer.render参数、事件历史参数等差异。MCP返回structuredContent和输出schema，同时保留文本兼容。普通HTTP旧页面读取保持原视图；显式agent视图无存储路径或图片blob。
- 事件、时间线、搜索的历史时间比较支持ISO T/Z、时区和SQLite时间格式；历史全文引用返回实际转写版本。

## 2. 当前可用入口

本机 Core：`http://127.0.0.1:8876`

| 用途 | 入口 |
|---|---|
| 能力发现 | `GET /v2/capabilities` |
| 样品聚合 | `POST /v2/capabilities/query.sample.context/invoke` |
| 紧凑目录 | `POST /v2/capabilities/query.samples.list/invoke`，view=catalog |
| 版本化事件 | `POST /v2/capabilities/memory.event.read/invoke` |
| 版本化原文 | `POST /v2/capabilities/evidence.page.read/invoke` |
| MCP统一入口 | `POST /v2/mcp`，工具名与Core能力名一致 |

所有入口使用 `Authorization: Bearer <专属主体凭据>`。现有MCP适配和真实本机HTTP调用已验证；尚未宣称某个第三方AI产品或官方MCP SDK已经完成接入。当前服务仍监听本机地址，未开放远程端口。

## 3. 独立只读身份

本轮已为 `proj_lab` 发行一个独立 `service + viewer` 身份；私有配置：

`D:\Chem\ChemClaw-Lab-S10-Repair-test\external-ai-reader-p1-2026-10-23.json`

配置包含base_url、project_id、principal_id、credential_id及API key；仅在本地程序中读取，不复制到聊天、文档、命令行参数或源码交付包。其权限为既有viewer读取能力集合，不能进行知识、库存或预约写入，不复用人的工作台密钥。

为另一个已存在项目发行独立身份的脚本：

```powershell
# 从源码根目录执行；只向指定已有库/项目新增service viewer，拒绝覆盖配置。
.venv\Scripts\python.exe scripts/create_external_reader.py --db D:\path\existing.db --project-id EXISTING_PROJECT --config D:\private\new-reader.json
```

生成过程只在配置文件保存明文key，数据库保存hash；未知库/项目、配置已存在、文件或提交失败均有测试。可通过 `auth.revoke_credential` 与配置中的credential_id撤销此凭据；项目成员关系变更同样会在后续读取重新生效。

## 4. Python 外部客户端

`ReadOnlyQueryClient` 已扩展。外部本机Agent可导入该客户端，直接消费JSON数据，无需打开网页或使用聊天历史。

```python
import json
from pathlib import Path
from chemclaw_lab.clients import ReadOnlyQueryClient

cfg = json.loads(Path(
    r"D:\Chem\ChemClaw-Lab-S10-Repair-test\external-ai-reader-p1-2026-10-23.json"
).read_text(encoding="utf-8"))
client = ReadOnlyQueryClient(cfg["base_url"], cfg["api_key"])

# 先发现可选对象；已知道唯一标签或ID时可直接调用下面的context。
directory = client.list_samples(project_ids=[cfg["project_id"]], q="实际样品标签")

context = client.get_sample_context(
    cfg["project_id"],
    sample_label="实际样品标签",  # 或sample_id；两者不能同时传
    budget={"max_bytes": 1048576},
)
```

对应HTTP参数示意：

```json
{
  "params": {
    "project_id": "proj_lab",
    "sample_id": "smp_从目录取得的真实ID",
    "include": ["events", "cited_blocks", "provenance", "revision_index", "relations", "source_context"],
    "snapshot": {"mode": "current"},
    "budget": {"max_bytes": 64000}
  }
}
```

同一参数通过MCP：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "query.sample.context",
    "arguments": {"project_id": "proj_lab", "sample_label": "实际样品标签"}
  }
}
```

样品标签和别名按存储内容精确解析；同名/别名歧义返回候选。catalog支持大小写不敏感的候选搜索，选定后优先使用sample_id。找不到与无权限的项目分别返回明确状态/错误，不猜测或跨项目合并。

## 5. 如何消费返回值

`result.schema_version = sample_context_v1`。

- `sample / resolution`：已解析身份或歧义候选。
- `events`：完整可扩展payload/fields、样品链接、来源、具体未知项和当前/所选版本。保留分段条件、计划/实做及模拟来源，未建立本地化学关键词分类器。
- `evidence`：原始引用的page、transcript_revision、block及文本、质量标记。active_revision表示当前原件转写，实际读取版本取transcript_revision。
- `revision_index`：有权限的修订索引。旧版本权限不足时标history_visibility=restricted，不返回其内容。
- `relations`：基于已有样品链接的shared_event关系；同一共同事件使用同一个ID。此版本没有自动推断通用关系图。
- `source_context`：相关来源声明及必要页面上下文，和样品事实分开。缺block但有明确页/版本时，保留精确版整页作为上下文并报告缺口。
- `quality`：来源过时、质疑、缺失引用、关联页未归属记录等。未归属记录不自动归给本样品。
- `coverage`：所请求部分的总量、返回量、遗漏和计量字节数。`knowledge_complete=null`表示未作语义整理完整性的保证；`status=complete`只表示本次请求范围的数据取得完整。

默认请求全部六项include。provenance是events/source_context的附加信息，不能单独请求。未指定include的部分不构成化学领域的“没有这类数据”结论。

字节预算4096–1048576，默认64000；按紧凑UTF-8编码的result对象计量，排除HTTP/MCP envelope。超预算保留整条对象，不删条目中的关键字段来伪造完整。返回partial和遗漏计数，或metadata自身放不下时明确budget_too_small。

预算不足时可扩大后重读，并传入首次 `snapshot.fingerprint` 作为 `expected_fingerprint`。记录、来源、身份、关联或相关权限视图变化会返回409 snapshot_changed，不能静默混合新旧上下文。精确事件revision和原文revision/block也可独立读取。

例如：

```python
full = client.get_sample_context(
    cfg["project_id"], sample_id=context["sample"]["sample_id"],
    budget={"max_bytes": 1048576},
    expected_fingerprint=context["snapshot"]["fingerprint"],
)
event = client.read_event("evt_真实ID", revision=1)
page = client.read_page("pg_真实ID", transcript_revision=1, block_ids=["真实块ID"])
```

as-of请求：`snapshot={"mode":"as_of","knowledge_as_of":"2026-10-21T08:00:00Z"}`。历史模式固定事件知识截止点；身份与来源当前状态仍明确标current，未将其描述为全系统时间旅行快照。当前修订与原页旧值可以同时返回，保留纠错原因。

## 6. 实测与回归

隔离两页模拟资料验证：共同操作/计划、独立数值、跨页返工、未知/null、模拟来源、质量20→22 mg的人工纠错、旧原文保留、语义扩展字段、别名歧义、跨项目拒绝、budget部分返回与指纹扩读。真实HTTP客户端与HTTP MCP返回相同数据指纹；业务表与永久读取回执没有新增。

常用服务真实验收摘要：`work/external-ai-p0-p1-2026-10-23/main-acceptance-summary.json`。

- 独立service凭据可发现新能力和紧凑目录，写请求由服务端返回403。
- 主项目一个较大样品默认64,000字节预算明确返回partial；扩大预算后读取17事件、42引用块，约98KB，HTTP/MCP指纹一致。现场单次读取约0.14–0.18秒，仅为本机当前数据的观察，不是规模化性能保证。
- 同一项目也有零关联知识的既有样品：可返回身份和空记录，knowledge_complete仍为null，未伪造实验事实。
- 主库样品、事件版本、提案、证据页、物料及库存交易六张业务表全行摘要不变；新增的是独立读取身份与私有配置。没有清库或导入验收资料到主项目。
- 工作台/Core健康检查正常，人的连接和模型配置保持可用。常规上下文读取内部模型调用数为0；启动器自己的连接探测不属于研究查询。

| 相关测试范围 | 结果 |
|---|---:|
| repair：新接口/契约/来源/权限/默认采纳/后台任务/编排 | 154通过 |
| reader发行、证据审核和前端契约补充组 | 11通过 |
| 旧S02/S05/S06/S09 | 87通过 |
| Python前端桥 | 52通过 |
| Pi运行时 | 107通过 |

最后针对context、读取契约与reader发行的56项又通过。上述是相关检查，不是全仓库全部测试。少数旧测试fixture显式设置review以继续测试旧审核流程，产品默认知识采纳没有改变。repair与旧根目录测试分组运行，避免既有conftest装载冲突。

## 7. 尚未包含的P2能力

- 固定快照游标分页与大规模增量同步；catalog offset是当前目录分页，发生并发变更时不是固定快照。
- 超过最大响应预算的大资料完整分页；当前会明确partial，不能声称全部资料已返回。
- 多样品批量上下文、通用关系图、外部产物与样品的统一类型化关联。
- LLM对未归属或隐含跨页关联的显式扩展任务；普通context只读已有明确关联。
- 特定外部强AI产品的实际工具调用适配与远程部署。当前已提供可调用的本机接口与客户端，不需要先开放网络。
