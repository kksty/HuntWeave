# P1 #20 选择性保留：候选统计、固定与回收预览

日期：2026-10-10（Asia/Shanghai）。对应 [#20](https://github.com/kksty/HuntWeave/issues/20)（父 Issue [#14](https://github.com/kksty/HuntWeave/issues/14)），阻塞边 #17、#19 均已关闭。规格依据：[0002 §3.8](../specs/0002-real-execution.md)、[PROJECT.md §10.5](../../PROJECT.md)、[ADR-0005](../adr/0005-compose-kali-tool-retention-and-development.md)、[ADR-0014](../adr/0014-execution-lifecycle-and-environment-identity.md)。实现提交 `219fa58`。

## 问题与判定

**一次调用结束后，它的私有工作区不再当场销毁。** 交付前 `RealRunner._release` 在回收实例时把容器、每会话网络与工作区卷一起删除，于是「选择性保留」没有可保留的对象：PROJECT.md §10.5 称为「一次性任务环境」的那个制品，在任何策略能对它作决定之前就消失了。裁决：真实执行器回收实例时**保留**该卷，把它记入实例账本的 `retained` 列表，再由保留账本登记为可回收的私有复现制品；容器与网络照旧回收，受管资源历史保持完整（`0011` 的账本语义不变）。清理时机由策略决定，不由调用结束决定。

**回退不删除保留制品，但必须点名它们。** `begin_revert` 只回收在途实例，不把操作员的复现材料当作回退的副作用删掉；`RevertReport.retained` 与归档到证据目录的对账记录都会列出仍然保留的制品，因此「撤除管理能力」不会被读成「主机已经空了」。这是刻意取舍：回退后需要重新启用管理才能回收这些卷。

**环境键取自不可变清单，不含宿主引擎。** `environment_key(manifest)` 哈希固定 profile 的 id 与版本、解析后的基础/工具镜像 digest、工具清单与 CPU 架构；容器引擎版本被刻意排除——引擎升级是宿主的变化，不是换了一个工具。候选资格按**窗口内去重后的独立 Run 数**判定（默认 30 天 3 个 Run）：使用记录按 `环境键|Run` 记账，同一 Run 内的重试只增加 `successful_calls`，不增加 `successful_runs`。

**回收预览是「会做什么」，不是「做了什么」。** 选择顺序：TTL 到期先删，容量压力下按最久未保留优先，且只在可回收项内选择。被固定（版本或单个制品）或仍被实例持有的制品列为受保护项并给出规则原因码；把全部可回收项删完仍超容量时报告 `retention_capacity_insufficient` 与差额，而不是删掉受保护材料凑数。策略默认每 300 秒（`HUNTWEAVE_RETENTION_SWEEP_SECONDS`，`0` 关闭）在 Runner 内自行执行一次：先用账本记录的体积判断是否有该回收的内容，有才去读运行时体积并执行，因此无人盯守时 TTL 与容量真的约束磁盘，而空转不会往决定记录里灌条目。自动回收与手动回收走**同一份预览**，固定与在用保护对它同样有效。

**删除只走受信管理器的所有权与在用校验。** 新增两个只读操作：`volume_facts`（按名读回一个卷及其标签——按标签选择会把「标签已被改掉」的卷读成「不存在」，那正是所有权检查要抓的情形）与 `volume_usage`（Docker 没有单卷体积接口，只能读 `/system/df` 的用量报告，且只保留账本已命名的卷）。`release_artifact` 先要求该卷确在实例的 `retained` 里，再核对标签与名字前缀；实例仍持有（未确认停止）报 `retention_artifact_in_use`，不是本项目所有报 `ownership_mismatch`，不在保留列表里报 `retention_artifact_not_retained`。删一次性环境不动证据、环境清单与调用历史。

**不发布。** `shared_tool_version` 恒为 `false`，且没有任何写入路径可以提升私有工作区；候选视图把「未发布为共享工具版本」直接写在条目上。

**控制面只代理与留痕。** 操作员的每个固定/取消固定/删除/回收决定在业务库 `retention_decisions` 留一行（**执行端决定 id 为主键**，重发不产生第二条），并在每个受影响 Run 的时间线上追加一条 `retention_decision` 事件（来源标识去重）。`actor` 由服务端会话决定，客户端正文里的同名字段不会被采信；备注取操作员原文。执行端的拒绝保持它自己的原因码与状态码，不被改写成控制面的说法。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `execution/retention.py`（新增） | 保留账本（`runner_state/retention.json`）：版本描述、按 Run 去重的使用记录、保留制品、固定、决定；`preview()` 选择与保护判定、`pin`/`unpin`/`delete_artifacts`/`sweep`；`environment_key()`；`remove_artifact(manager)` 把删除交给受信管理器 |
| `contracts/retention.py`（新增） | 版本化视图：`RetentionView`/`ToolVersionView`/`RetainedArtifact`/`ProtectedArtifact`/`RetentionPreview`/`RetentionDecisionView`/`RetentionReport`/`RetentionRequest`/`RetentionLimits`（含 `from_env`）与 `RetentionView.unavailable()` |
| `execution/sandbox.py` | `reclaim_instance(..., retain_volumes=)` 保留私有工作区并记入 `InstanceRecord.retained`；`ReclamationReport.retained`；`RevertReport.retained` 与归档对账；`in_use_volumes()`；`volume_usage()`（按账本限定范围）；`release_artifact()`（按名读回 + 所有权 + 在用 + retained 三重校验，幂等）；`ContainerRuntime` 新增 `volume_facts` 与 `volume_usage` |
| `execution/dockerruntime.py` | `volume_facts` 按名读回单卷；`volume_usage` 读 `/system/df` 并只保留被问到的卷，引擎不报体积时回答 `None`（未测量，不是 0） |
| `execution/real.py` | 成功结算后为环境键记一次使用；释放时把工作区交保留账本；账本写不下时把卷交回管理器，仍不行则按原路径回收（主机上不留无人命名的资源） |
| `execution/server.py` | `/v1/retention` 读、`versions|artifacts` 的 `pin`/`unpin`/`delete`、`sweep`；保留账本按需打开；无人值守回收看门狗（间隔 0 时只保留手动回收） |
| `config.py` | `HUNTWEAVE_RETENTION_SWEEP_SECONDS`（默认 300，`0` 关闭） |
| `runs/retention.py`（新增） | `RetentionService.record()`：业务侧决定记录 + 受影响 Run 的时间线事件（幂等） |
| `runs/events.py`（新增） | `append_event()`：两个服务共用的追加协议（游标加锁、来源去重） |
| `storage/models.py`、`migrations/versions/0008_retention_decisions.py` | `retention_decisions` 表；`BUSINESS_REVISIONS` 同步为 `0008_retention_decisions` |
| `api/app.py` | `/api/v1/retention` 读与七个动作路由；写动作经 `RetentionService` 留痕；执行端拒绝保持原原因码与状态码；执行端没有该接口时报 `retention_unsupported` |
| `execution/client.py` | `retention()`、`retention_action()`、`sweep_retention()` |
| `frontend/src/workspace.ts`、`RunConsole.vue`、`style.css`、`tests/retention.spec.ts` | 控制台「选择性保留」区块（候选版本、持有的制品、回收预览与受保护项、容量阻断、决定记录、两步确认的回收）；12＋1 条原因码文案 |
| `lab/isolation/action.py`、`lab/isolation/retention.py`（新增）、`lab/isolation/Dockerfile`、`deploy/verify_retention.py`（新增） | 动作探针按新生命周期收紧「没有留下东西」的断言并在清理时释放保留卷；新增保留探针与入口；探针镜像提供工具用户可写的 `/workspace` |

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.8.2）、`backend/.venv` Python 3.12.14、宿主 Python 3.14.7（探针入口）。四份探针报告均在提交 `219fa58` 上、工作树干净时取得（`source_revision=219fa58…`、`source_tree_dirty=false`）。

| 行为 | 结果 |
| --- | --- |
| 保留探针（本切片新增） | `python deploy/verify_retention.py` → **10 项检查全部通过**、`leftovers: 0`、`cleanup_failures: 0`；报告 `runtime/sandbox/27150fd29aee44be8144bcdfbb844a8d/report.json`，sha256 `5ddfecf31ad2b0d55ced68455c37fcc49df2cd923d79c1259359be4856025680` |
| 候选按独立 Run 计数 | 探针：三个独立 Run 各一次成功调用 → 1 个版本、`successful_runs=3`、`successful_calls=3`、`threshold_runs=3`、`window_days=30`、`candidate=true`；随后在同一 `run_id` 内再成功一次 → `successful_calls=4`、`successful_runs` 仍为 3。单元检查：2 个 Run × 3 次调用 → `successful_runs=2`/`calls=6`/非候选，补第 3 个 Run 才成为候选；窗口外的使用不计入 |
| 一次调用留下被账本命名的私有工作区 | 探针：4 次调用后 Docker SDK 都能按项目标签找到工作区卷，`GET /v1/retention` 为每次调用列出 `state=retained` 的制品（`resource_name` 与卷名一致），容器的会话网络均为 0；每次体积实测 **262144 B**（未测量数 0） |
| 固定保护自动回收 | 探针（容量设为 1 B 以构造真实压力）：固定一个制品后 `protected` 给出 `retention_artifact_pinned`、`to_delete` 不含它；执行回收后其余 3 个未引用制品被删除、固定制品及其卷仍在。整版本固定同样生效（`retention_version_pinned`）。单元检查覆盖 TTL 到期、最久未保留优先、未固定即被回收、固定项可被显式删除 |
| 容量不足是阻断而不是删受保护材料 | 探针：`capacity_bytes=1`、`retained_bytes=262144`、`blocked=true`、`reason_code=retention_capacity_insufficient`、`needed_bytes=262143`；回收未删除任何制品；固定制品卷与探针自己的外部标签卷都仍在。单元检查：可回收项删完仍超容量时报差额；无容量压力时新鲜制品不被选中 |
| 只回收本项目无引用制品 | 单元检查：标签被改成他项目的卷报 `ownership_mismatch` 且不被删除；实例仍持有（未确认停止）报 `retention_artifact_in_use`；不在 `retained` 里报 `retention_artifact_not_retained`；第二次释放报同名原因而不是静默成功。探针：回收后探针自己的外部标签卷仍在 |
| 删一次性环境不删必要证据与清单引用 | 探针：显式删除后卷从 Docker 消失、`deleted` 含该制品、转录/stdout/stderr 三份证据仍在磁盘且 sha256 与调用结果一致、`GET /v1/runs/{id}/sandbox-state` 仍报该实例（`reclaimed`）与其环境清单（`sandbox-egress-v1`、gateway/tool 两个镜像）。单元检查另有证据文件逐一存在与实例清单不变的断言 |
| 不发布共享工具版本 | 探针：每个版本的 `shared_tool_version` 均为 `false`；`POST /v1/retention/publish` → 501 `execution_not_implemented`。单元检查：固定与删除之后 `shared_tool_version` 仍恒为 false，缓存之外没有任何提升路径 |
| 无人值守回收 | 单元检查：设 `HUNTWEAVE_RETENTION_CAPACITY_BYTES=1`、`HUNTWEAVE_RETENTION_SWEEP_SECONDS=1`，Runner 启动看门狗后未介入即回收超容量制品，决定记录 `actor=None` 且带 `deleted_artifacts`；间隔为 0 时只保留手动回收 |
| 留痕 | 集成检查（一次性栈）：决定落 `retention_decisions` 一行，`operator_session_id` 取会话、备注取操作员原文、受影响 Run 各得一条 `retention_decision` 事件；同一决定重发不新增行、不新增事件；执行端拒绝（409）时业务侧不留记录 |
| 环境键语义 | 单元检查：引擎版本变化不改键；架构、profile 版本、镜像 digest、工具清单任一变化即改键 |
| 控制台 | 浏览器：`npx playwright test --max-failures=0` → **15 项通过、1 项跳过（37.5 秒）**，其中新增保留检查 3 项（区块陈述执行端结论、回收需二次确认；无管理能力时显示观测缺口而不是「缓存为空」；容量阻断显示差额与原因，回收后阻断仍在）。跳过的 1 项是需要 `HUNTWEAVE_E2E_RECONCILE_RUN_ID` 的核对流程（`0016` 记录同一入口）。本次浏览器检查临时使用本机已安装的 Edge（`channel: 'msedge'`，未入库，与 `0010` 的做法一致） |
| 本地纯检查 | `ruff check src tests` 通过；`mypy --config-file pyproject.toml src` 在 **49** 个源文件上无问题；`python -m pytest -m "not integration" -q` → **246 项通过、7 项跳过、41 项按标记排除** |
| 一次性栈检查 | `huntweave-p0-checks`：23 项迁移后 **287 项通过、2 项跳过**（`tests/test_startup_integration.py` 除外）、启动集成 **5 项通过**；`deploy/verify_startup.py --project huntweave-p0-checks --web-port 18000` → 5 项 PASS（exit 0）。迁移 `0008_retention_decisions` 在真实启动路径上应用成功，开发库 `alembic_version` 亦为 `0008_retention_decisions` |
| 前序探针未回归 | `verify_lifecycle` → **29 项通过**（报告 `runtime/sandbox/753659b00c3e4e0b8c94da2d7816a6ba/report.json`，sha256 `3003a1cbab8aaade24c474cd5eb1b38a26306f76749ff05d4a084b43645ab671`）；`verify_egress` → **30 项通过**（`runtime/sandbox/2d85cf5d4dd9497ba753bd88311bbffc/report.json`，sha256 `2dc2e091afe5ad3c1cf83013f4fdb6044ce4b962cf05386d9f99a49e6d999093`）；`verify_action` → **18 项通过**（`runtime/sandbox/9c89006161f24de08e0b2dd4a8d89929/report.json`，sha256 `fa77a97c36a0300265f5e64020e14801defce3e6128a3aa3cf5a891a60505963`），三份报告 `leftovers: 0`，动作探针另报 `surviving_project_resources: 0`、`cleanup_failures: 0` |

## 未达成与限制

- **真实执行仍未开放**：本切片的保留行为经 Runner 自己的 HTTP 表面在 `lab/` 靶场验证，不是一次应用层真实 Run；创建真实 Run 仍被四项门槛拒绝（`0013` 不变）。
- **TTL 到期由注入时间覆盖**：真实时间尺度内等不到 7 天，靶场探针验证的是容量压力、固定保护、显式删除与候选计数；TTL 分支由单元检查用注入时刻断言（到期即入 `to_delete`，回收原因记为 `retention_ttl_expired`）。
- **回退不回收保留制品**：这是刻意取舍（不在回退时销毁复现材料）。撤除管理能力后需重新启用管理才能回收；`RevertReport.retained` 与归档对账会列出它们，但当前没有「回退前清空保留缓存」的入口。
- **体积未知时容量只对已测量的制品强制**：引擎不报体积的制品显示为未测量并单独计数，不按 0 计；此时 `blocked` 只能对已测量的字节作答，视图始终带 `unmeasured_artifacts`，不会声称容量已被满足。
- **「活跃任务/复审租约」在 P1 的实现口径**：每个调用有独立实例与私有工作区，因此「在用」＝未确认停止的实例仍持有该卷（`manager.in_use_volumes()`）。P1 没有复审租约概念，也没有会话级共享环境；P2 引入会话级环境或复审租约后，选择逻辑必须改查任务/租约登记，而不是只看实例状态。
- **容量阻断只阻断「显示」，不阻断整次回收**：`blocked` 表示「即使在可回收集合内尽可能回收也降不到容量以内」，此时回收仍会删除可回收项并保留受保护项，报告与视图继续显示缺口。这是刻意的：把整个回收操作拒绝掉会让可释放的空间无法从界面释放。
- **保留区是部署级、界面入口在 Run 页**：保留视图与固定/删除都是部署范围的事实，控制台区块挂在 `RunConsole.vue`（`/runs/{id}`）；PROJECT.md §11 设想的「环境/工具设置页」尚未建立，届时该区块应移到一个部署级页面。
- **`execution/dockerruntime.py` 仍无容器外检查覆盖**：本切片新增的 `volume_facts` 与 `volume_usage` 只在靶场探针上被真实执行（`0011`–`0013` 的同一限制）。
- **无人值守间隔不是长时验证**：`HUNTWEAVE_RETENTION_SWEEP_SECONDS` 默认 300 秒只在单元检查里以 1 秒间隔验证其行为；长时间运行下的回收节奏与并发（与 #21 的压力验收相邻）未验证。
- **探针镜像需要可写的 `/workspace`**：Docker 的具名卷挂在镜像里不存在的路径上会以 root 拥有者出现，profile 的普通用户写不进去（实测 `Permission denied`），于是卷里没有字节、引擎不报体积、容量压力无法构造。`lab/isolation/Dockerfile` 因此建立并授权该目录；真实工具镜像应自行提供，这属于部署侧约定，记录在此以免被当作探针镜像的随意改动。

## 两轴复审与处置

对照 `AGENTS.md`／`PROJECT.md`／`0002` 规格／`GLOSSARY.md`／ruff 与 strict mypy 配置（Standards 轴，含 Fowler 坏味道基线）与 #20 验收标准及规格 §3.8／§5 第六行（Spec 轴）两轴并行复审 `0b048c1...219fa58`，处置如下（发现即修）：

| 复审发现 | 处置 |
| --- | --- |
| 提交随附的文档引用了尚不存在的 `docs/validation/0017-…md`（证据链条断裂） | 本记录随实现之后的文档提交落地，先前的文档改动不单独提交 |
| 默认开启的无人值守回收只写在未提交的文档里（规格未授权该行为） | 规格 §3.8 与 README 随本记录一并提交，写明间隔、默认值与「0 关闭」语义；该行为在 §3.8 的策略范围内，不构成新的产品边界，故不另立 ADR |
| `runner_reason` 把写动作的 404/501 映射成 `sandbox_state_unsupported`，而该码的文案说的是「读取接口」（拒绝被说成另一种缺口） | 新增 `retention_unsupported` 并用于读与写的 404/501，文案说明「该执行端没有保留策略接口」；机械检查两个方向都通过 |
| `runs/retention.py::_event` 与 `OrchestrationService._event` 逐字重复 | 抽出 `runs/events.py::append_event()`（游标加锁 + 来源去重），两个服务共用；集成检查覆盖事件顺序与去重 |
| `in_use_volumes(manager)` 抬手越过管理器读它的锁与账本（Feature Envy） | 改为 `SandboxManager.in_use_volumes()`：这是管理器对自己账本的问题 |
| `unobserved_retention(management: str)` 用 `# type: ignore` 掩盖类型；`execution_retention_action` 把任何未知 kind 都当成 artifacts 发出去 | `management` 采用 `SandboxManagement`；kind/action 改为显式映射表，未知取值以 `invalid_request` 拒绝而不是默认成另一个目标 |
| `sweep()` 用 `all([])` 把「没有选中任何制品」也算成 TTL 到期原因 | `expired_only` 要求选择集非空；空回收不再被标注为 TTL 原因 |
| 账本写不下时先保留卷再回退，若回退也失败则卷留在主机上而没有任何账本命名它（静默遗留） | `_discard()`：先交回管理器（重新做所有权校验），仍不成则按无保留路径回收整个实例；主机上不留无人命名的资源 |
| 看门狗 `except Exception: continue` 与释放路径的吞掉失败没有留下任何事实 | 看门狗的失败仍不终止线程（下一次读账本重来），但没有该回收的内容时不写任何决定（避免用空转条目冲掉操作员的决定记录）；释放路径的失败改为走 `_discard()` 的显式降级，而不是静默 `continue` |
| Spec 轴：「活跃任务与复审租约」被当作已交付，而实现只看实例是否持有卷 | 记录采用并写明 P1 口径（每次调用独立实例与工作区，无复审租约）；P2 引入会话级环境或租约后必须改查任务/租约登记，列入限制 |
| Spec 轴：容量阻断只是显示，回收仍会删可回收项并返回 200 | 保留该语义并在本记录写明理由（拒绝整次回收会让可释放空间无法释放）；界面在回收前后都显示阻断与差额，报告另列真实删除与释放 |
| Spec 轴：未知体积按 0 求和，容量结论可能不完整；预览未列证据引用 | 未知体积始终以 `unmeasured_artifacts` 单列（限制中写明容量只对已测量字节作答）；证据从不被制品回收触碰，由单元检查与探针各验一次，预览因此列制品而非证据 |
| Standards 轴：部署级保留区块挂在 Run 页（Divergent Change） | 记录为限制：本版没有环境/工具设置页，区块暂挂在 Run 页，建立该页时应迁移 |
