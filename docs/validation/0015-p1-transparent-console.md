# P1 #19 透明控制台：验证

日期：2026-10-10（Asia/Shanghai）。对应 [#19](https://github.com/kksty/HuntWeave/issues/19)。随本条交付的事后收紧已先由 [验证记录 `0014`](./0014-p1-stop-confirmation-and-cwd.md) 落地（停止确认与调用结局分离、动作工作目录可见）；本记录覆盖 #19 本身的验收标准，以及实施中发现的三个既有缺陷。

## 问题与判定

本条要解决的是「平台说的事情有没有事实支撑」。四类事实此前要么不存在，要么只有后端字典里的字段而界面读不到：

- **执行端对一次调用的自述不存在。** 界面只能显示票据参数，无法显示真正运行的 argv、工作目录、执行身份、实例与环境版本——`0014` 只补上了 cwd 一项，其余没有来源。
- **Run 级容器与网关状态没有读取入口。** 受信管理器记录了实例与放行规则，但控制面没有任何只读路径能把它们呈现给操作员；「已回收」在界面上无事实可依。
- **证据的三个分支不可达。** 截断与脱敏在代码里没有实现，存储满与归档失败只会在适配器里以未分类异常出现，界面也没有任何可读表述。
- **原因码与界面不对齐。** 后端能发出 138 个原因码，前端只映射 35 个，其中 1 个还是后端永不发出的；界面还会把裸标识符直接显示给操作员。

裁决：

- **执行端自述写进调用本身。** `ExecutionRecord.runtime`（`CallRuntime`）与 `ExecutionRecord.progress`（由事件列表读出的时长/静默/归档字节）成为调用的事实字段；`real` 侧在 `_announce` 里把 argv、profile 工作目录、普通用户、实例身份、环境清单的镜像 digest 与工具清单、网络模式、网关与授权端点一并写出，`fake` 侧用同一形状声明「本进程内固定夹具、无 argv、无实例」。控制面把两者存在 `tool_calls.runtime`／`tool_calls.progress`（迁移 `0006_call_runtime`），**不在读取时按当前 profile 重算**——部署的 profile 会变，旧调用跑过什么不能跟着变。
- **容器与网关状态由执行端给出，控制面只投影。** Runner 新增 `GET /v1/runs/{id}/sandbox-state`，由 `execution/operator.py` 把受信管理器的会话、实例与运行时审计投影成版本化视图；停止只在 `stop_confirmed_at` 存在时报告 `confirmed`，`creating`／`interrupted`／无确认的 `reclaimed` 一律 `unconfirmed`。app 通过独立的只读读取拿到它，读不到（未启用管理能力、执行端不可达、旧 Runner 无该接口）时回 `available=false` 加原因码——**这是观测缺口，不是「没有资源在运行」**。
- **归档自带三种诚实标注与一种拒绝。** `execution/archive.py` 负责：超配额时按 UTF-8 边界截断并标注；脱敏命中时替换为 `[redacted:<pattern>]` 并记录命中数量（**不保留原值**——留下被脱敏内容的副本等于没有脱敏）；写入前检查可用空间，空间不足或目录不可写时以 `evidence_storage_full`／`evidence_archive_unwritable` 拒绝，单件超限以 `evidence_artifact_too_large` 拒绝。拒绝会**阻断该次执行**：调用以 `failed` 结束并带上归档自己的原因码，不写任何 `ToolResult`。
- **原因码映射与后端实际发出的码闭合。** 前端 `messages` 覆盖后端能声明的全部原因码，未知码回退为通用中文句子并附标识符，不再把裸码当作解释；`backend/tests/test_reason_codes.py` 从后端源码里按「构造拒绝、写入 payload、`_interrupt` 的第三个参数、能力门槛、契约 Literal」等位置机械收集候选码，要求每个码要么有文案、要么被显式说明为「不是原因码」，并反向要求界面不保留永不可达的条目。
- **输入超限与 IPv6 各自报自己的原因。** 预览先逐行分类，再对所有行都可接受的清单量上限；超出上限的行按行号给出 `target_limit_exceeded`，不再整体抛 422 掩盖行级信息，也不会让「计入额度但不可执行的 IPv6」被上限抢先报出。
- **界面分区陈述，不给单一健康灯。** 玻璃鱼缸顶部并列显示执行／研究／复审／交付四个视图、部署的容器管理能力、以及本次 Run 仍占用执行额度的调用数；停止未确认、实例隔离与未决调用各自常驻且不可折叠；不再出现任何百分比形式的「进度」或「安全」。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `contracts/execution.py` | 新增 `CallRuntime`（argv/cwd/user/实例/环境清单/网络模式/网关/授权端点）、`CallProgress`（由事件读出的时长、静默、归档字节、动作时限）与 `CallProgressView`（同一视图的传输形状），并把 `runtime`/`progress` 挂到 `ExecutionRecord`；`ExecutionResult` 增加 `duration_ms`／`truncated`／`redacted`；证据条目增加 `truncated`／`redacted` |
| `execution/archive.py`（新增） | `EvidenceArchive`：脱敏模式表、按配额截断、写入前空间检查、越根路径拒绝、按路径确定性生成证据 ID；`ArchiveRejected` 携带原因码 |
| `execution/ledger.py` | `_archive`／`_output` 改走归档；输出超配额时写 `execution_output_truncated` 并在 `complete` 上标注；脱敏写 `execution_output_redacted`；`progress` 由事件列表读出（`PROGRESS_STATUS` 表把调用状态映射到进度状态）；归档拒绝在 `_execute` 里捕获为 `execution_evidence_refused` + 该调用的失败结局 |
| `execution/real.py` | `_announce` 写出完整运行环境陈述；`_announce_completion` 与终态迁移在同一记录上一并写出 `execution_command_completed`（含真实耗时）；（顺带）取消/超时路径的命令在时间上不再早于取消请求被读取 |
| `execution/fake.py` | 用同一形状声明固定夹具的运行环境（argv 为空、无实例、无网络）；完成事件与终态一并写出 |
| `execution/sandbox.py` | `CommandResult` 增加 `duration_ms`；新增 `SandboxRunState`（会话、实例、可选审计）与 `SandboxManager.run_state`；`run_command` 的 workdir 仍在 profile 内显式传入 |
| `execution/dockerruntime.py` | `exec_in_tool` 实测命令墙钟耗时并回填 `duration_ms` |
| `execution/operator.py`（新增） | `project_run_state`／`unavailable_run_state`：把管理器记录投影为 `RunRuntimeView`；`_stop_state` 只在 `stop_confirmed_at` 存在时报 `confirmed`；回收预览只列范围、不执行 |
| `execution/server.py` | 新增 `GET /v1/runs/{run_id}/sandbox-state`（只读投影，未启用管理能力时回观测缺口） |
| `execution/client.py` | `RunnerClient.run_runtime(run_id)` |
| `api/app.py` | 快照响应带 `runtime`；读取失败按 404/501 与不可达分别给 `sandbox_state_unsupported`／`runner_unavailable` 并回 `available=false` |
| `runs/orchestration.py` | `_keep_runtime` 把 runtime／progress 存到调用行；`_call_view` 增加 `parameters_hash`／`runtime`／`progress`；`accept` 无条件保留执行端最新的进程与连接观测（含已结算的调用）；快照不再把派生字段 `demonstration` 交给严格契约；`_verdict_result` 同样排除派生字段 |
| `contracts/orchestration.py` | `ToolCallView` 增加 `parameters_hash`／`runtime`／`progress`；`RunSnapshot` 增加 `runtime`；`EvidenceView` 声明 `execution_profile`／`demonstration`（原先以未声明字段返回，接口 500） |
| `storage/models.py` + `migrations/versions/0006_call_runtime.py` | `tool_calls` 增加 `runtime`／`progress`（JSONB，可空——迁移前的调用没有这份陈述，不为它们伪造一份） |
| `storage/database.py` | `BUSINESS_REVISION` 补齐到 `0006_call_runtime`（`0005` 加迁移时漏改，就绪门槛自 #17 起一直失败） |
| `runs/inputs.py` + `contracts/runs.py` | 逐行分类后再量上限；`TargetPreview` 增加 `target_limit`／`over_limit` |
| `runs/service.py` | 超限以 `target_limit_exceeded` 拒绝，其余非法输入仍是 `invalid_targets` |
| `frontend/src/workspace.ts` | 原因码映射覆盖后端全部可发出的码（含执行端、归档、沙箱、启动诊断）；`reasonText` 未知码回退通用中文句子；新增 `managementLabels`、`RunRuntime`／`CallRuntime`／`CallProgress`／`EvidenceMeta` 类型 |
| `frontend/src/RunConsole.vue` | 四视图与占用计数分区（无单灯、无百分比）；停止未确认、实例隔离、未决调用常驻；会话容器与网关区块（实例状态、放行目标、放行/撤销时刻、环境清单与镜像 digest、受管资源、回收预览）；调用卡片显示 argv／参数 hash／cwd／执行身份／环境版本／实例／网络模式，并对没有执行端陈述的旧调用明说；证据条目的截断／脱敏／不可读标注与分段读取 |
| `frontend/src/App.vue` | 目标预览按行给出原因，超限单独提示；能力提示增加容器管理能力一行 |
| `frontend/src/style.css` | 新增状态网格、会话/实例卡片、调用进度与绑定行的样式 |

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.7.2）、`backend/.venv` Python 3.12、宿主 Python 3.14.5、一次性项目 `huntweave-p0-checks`（Web `127.0.0.1:18000`）。日常 `huntweave` 组未被注入故障。

| 行为 | 结果 |
| --- | --- |
| 本地纯检查 | `ruff check src tests` 通过；`mypy --config-file pyproject.toml src` 在 45 个源文件上无问题；`pytest -m "not integration" -q` → **217 项通过、7 项跳过、38 项按标记排除** |
| 一次性栈全量后端检查 | `docker compose ... run --rm --no-deps checks python -m pytest tests -q` → **260 项通过、2 项跳过**；连续两次全绿 |
| 迁移在一次性库上应用 | `alembic_version = 0006_call_runtime`；`huntweave.tool_calls` 出现 `runtime` 与 `progress` 列；`verify_business_schema` 通过（此前 `BUSINESS_REVISION` 停在 `0004_reconciliation`，就绪门槛自 #17 起一直失败，本次补齐） |
| 启动探针 | `python deploy/verify_startup.py --project huntweave-p0-checks --web-port 18000` → 全部通过（缺密钥镜像阻断业务资源、Runner 无非 root 平台密钥/socket/开发技能、证据归档对 app 只读、Runner 不可用时就绪拒绝、agentd 退出后 supervisor 重启并恢复健康） |
| 执行端自述进入调用 | 单元检查：真实动作的 `record.runtime` argv 为 `["sh","-c","id"]`、cwd 等于 profile 的 `workspace.mount`、user 等于 profile 的普通用户、`network_mode="internal"`、`authorized` 只含票据目标、镜像 digest 与工具清单非空、`parameters_hash` 与票据一致；固定夹具则声明无 argv／无实例／无出口 |
| 时长与静默由记录读出 | 单元检查：`progress.status` 为 `ended`、`started_at`／`last_output_at`／`output_bytes` 非空、`timeout_seconds` 取票据值；重启后同一调用的记录逐字段相等（重读不产生新的时长） |
| 截断分支可达并标注 | 单元检查：配额内可写；超配额的单件以 `evidence_artifact_too_large` 拒绝且不落盘；转录写满后不再增长并返回 `None`；真实路径下 `result.truncated` 与证据条目的 `truncated` 同时为真 |
| 脱敏分支可达并标注 | 单元检查：`Authorization: Bearer …` 与 `password=…` 被替换为 `[redacted:<pattern>]`，命中计数按模式给出，原值不在归档中；先脱敏后截断（反过来会留下半个秘密） |
| 存储满分支可达并阻断 | 单元检查：`disk_usage.free` 为 0 时在任何写入之前以 `evidence_storage_full` 拒绝，目录中不产生文件；注入同一拒绝后调用以该原因码 `failed`、`result` 为空、含 `execution_evidence_refused` 事件 |
| 停止确认只由受信记录给出 | 单元检查（`test_operator_view.py`）：运行中的实例报 `running`；管理器写入确认后报 `confirmed`；`stop` 失败时实例留在 `ready` 且报 `running`、回收预览要求确认；手工构造成 `reclaimed` 但无 `stop_confirmed_at` 的记录仍报 `unconfirmed`；创建中断的实例报 `unconfirmed`；运行时可审计时 `audit` 为 `None` 并报 `sandbox_runtime_unreachable`，但账本里的实例仍然列出 |
| 控制面读到的是执行端原话 | 集成检查：`accepted` 记录携带的 runtime／progress 落库后出现在快照调用视图（argv／cwd／user／网络模式／工具清单／时限），`parameters_hash` 与票据一致；没有 runtime 的旧调用在快照里是 `null` 而不是按 profile 补出来的值 |
| 证据接口按契约返回 | 集成检查：真实归档文件经 `EvidenceView`（严格契约）读回，正文、可用性、`demonstration` 与调用到证据的一跳关系都成立（该路由此前因未声明字段返回 500） |
| 目标输入按下限与 IPv6 分别报 | 单元检查：100 个目标可预览；追加第 101 个时该行给 `target_limit_exceeded`、`over_limit=1`、只在第 101 行；第 100 行是不可执行的 IPv6 时它报 `ipv6_environment_unsupported`、第 101 行才报超限 |
| 前后端原因码闭合 | 单元检查：后端按构造位置收集到的候选码全部有文案或已被说明为「不是原因码」；界面不保留后端永不发出的条目；界面源码缺失时（部署镜像只带 `dist`）改为核对**被服务的构建产物**是否含每个码的文案——容器内运行的就是这一支 |
| 浏览器流程（新增 4 项） | `frontend/tests/console-transparency.spec.ts` 4 项全部通过（逐项 `--grep` 复跑亦全部通过）：四视图分区且无百分比；观测缺口按缺口显示；停止未确认与实例隔离常驻、回收预览要求确认、调用卡片显示 `sh -c id` 与 `/workspace`；截断/脱敏被标注且无执行端陈述的调用明说无法确认；超限与 IPv6 各自按行报出 |
| 浏览器流程（既有） | 受登录限速（每 IP 每 60 秒 5 次）约束逐项复跑，共 **7 项通过**：`authorized-run.spec.ts` 的「深度链接与登录」「预览/幂等/入队/重载」「暂停/恢复预览/原始证据/人工结束」「执行模式与就绪门槛」「取消等待执行端确认」「能力未就绪不显示为就绪」「版本冲突经操作员确认后重发」；「核对裁定」在未设置 `HUNTWEAVE_E2E_RECONCILE_RUN_ID` 时按设计跳过 |
| 界面截图（不进 Git） | `runtime/validation/p1-console-state-grid.png`（四视图分区与观测缺口）、`p1-console-stop-unconfirmed.png`（停止未确认与实例隔离） |

## 未达成与限制

- **真实执行仍未开放**：ADR-0010 的四项门槛中 profile 复验与回退入口仍未满足，产品继续拒绝创建真实 Run；本条的容器/网关区块因此在实际部署里显示为观测缺口（这正是验收标准 1 要求的行为，但它只能在靶场探针与单元检查下验证「有实例时怎么显示」）。
- **应用层真实 Run 路径仍未端到端验证**：与 `0013`/`0014` 相同，真实调用的 runtime 陈述只在靶场的真实执行器上验证（`verify_action.py` 覆盖执行侧），控制面把 runtime 落库并在快照中呈现由集成检查覆盖，两者不是一次完整的真实 Run。
- **执行端自述仍不等于字节级证明**：argv 与 cwd 是执行端「它决定并写下」的事实，靶场已实测容器真的从 profile 目录启动（`0014`）；但命令内部若自行 `cd`，记录反映的是调用起点。
- **`progress` 的 `elapsed_ms` 只在结束时冻结**：运行中的调用每次读取按「上一次写出」重算，因此同一个运行中的调用两次读取会得到不同的时长（这是刻意的：读者问的是「还在跑吗」）。已结束的调用逐字段稳定。
- **容量与背压未实现**：界面只显示「仍占用执行额度的调用数」，没有全局物理额度的实际计数与控制槽模型——那归 [#21](https://github.com/kksty/HuntWeave/issues/21)；本条只保证「停止未确认」这一事实可被读取且不被渲染成已释放。
- **六轴主张状态编码、`lapsed`、确认等级与冻结交付**属 `docs/specs/0007`（P2-F），不在本条范围。
- **浏览器全量套件受登录限速限制**：一次调用跑完 11 项会触发限速（既有行为，`0010` 已记录），因此按批次复跑；这不是本条的回归。
- **本机 Compose Watch 未能启用**：`docker compose ... watch` 与 dev overlay 的 `up` 在本机因镜像构建阶段的中文路径问题失败（`x-docker-expose-session-sharedkey contains value with non-printable ASCII characters`），因此验证用一次性项目重建镜像而非 `sync+restart`；`0014` 之前的记录用的是一次性栈，结论不受影响。

## 顺带修复的既有缺陷

实施中在验收路径上撞到三处**与 #19 无关但阻塞验收**的缺陷，一并修复并留下对应检查，不另开切片：

| 缺陷 | 影响 | 处置 |
| --- | --- | --- |
| `BUSINESS_REVISION` 停在 `0004_reconciliation`，#17 加迁移 `0005` 时未同步 | 一次性栈与开发栈的就绪检查自 #17 起一直失败（`readiness_failed`），只是此前没有在升级后的库上跑过启动探针 | 补齐为 `0006_call_runtime`，并在该常量处写明「加迁移必须同时改这里」 |
| `EvidenceView` 未声明 `execution_profile`／`demonstration`，而证据路由以未声明字段返回 | `/api/v1/evidence/{id}` 直接 500，界面拿不到任何原始证据 | 两个字段声明进契约，并新增「按浏览器收到的视图校验读取路径」的集成检查 |
| 调用结算后执行端仍可能迟到更新停止确认，而 `accept` 在已有 `ToolResult` 时提前返回 | 重启前未确认停止的调用在之后成功完成后仍被读成「停止未确认」，界面会多报一条停止未确认 | `accept` 无条件保留执行端最新的进程与连接观测 |

## 两轴复审与处置

改动经 Standards（`AGENTS.md`／`PROJECT.md`／`0002` 规格／ruff 与 mypy 配置 ＋ Fowler 坏味道基线）与 Spec（`0002` 规格 §2 第 11–22 条、§3.1、§3.3；`0006` §7；`0007` §3；`PROJECT.md` §12.1、§12.2、§12.4 与 #19 的验收标准及其 2026-10-10 补充）两轴并行复审，处置如下（发现即修，未留待后续切片）：

| 复审发现 | 处置 |
| --- | --- |
| 一个执行器写完终态后还追加事件，读者可能在两次读取之间看到不同记录（既有 `test_fake_runner` 因此在容器上约四成概率失败） | 固定夹具与真实执行器都把「命令结束」与终态写在一起；`settled` 辅助函数改为等最后一个事件而不只是等状态 |
| `test_real_execution` 的「取消时命令从未启动」是与取消请求的真实竞态，断言了一个产品并不保证的性质 | 改为断言两者成立时都必须成立的性质：调用结束时其进程为已停止（`process_active is False`），并在注释里写明这是竞态 |
| `real.py` 的 `_announce_completion` 用 call_id 重新读记录，可能给另一条记录的终态追加事件 | 改为从终态迁移返回的记录上追加，并核对状态一致 |
| 前端把未知原因码原样显示给操作员 | `reasonText` 回退为通用中文句子；需要引用的位置（中断、恢复预览）另给标识符，不代替句子 |
| 会话容器区块最初试图从 profile 推断「网络模式」，与执行端记录可能不一致 | 改为只陈述 profile 的 id，并列出执行端记录的放行目标与放行/撤销时刻 |
| `RunSnapshot`／`ReconciliationResult` 收到派生字段 `demonstration` 时直接校验失败 | 控制面不再把派生字段当列传出去；契约仍由 `RunView` 按 `execution_profile` 重算 |

## 待人工确认

- 界面文案的措辞（尤其「停止未确认」与「实例隔离」两段）是否符合操作员的阅读习惯，交由人工复审确认；本记录只保证措辞与实际事实一致。
- 回收预览的呈现形式（是否需要在同一区块直接提供受信回收入口）属于 [#20](https://github.com/kksty/HuntWeave/issues/20) 的范围，本条只提供只读预览。

## 后续更正：登录与浏览器验证入口

2026-10-10：本记录中「全量浏览器套件受登录限速限制、需要分批复跑」描述的是当时的行为。登录限速已按用户要求移除，当前入口与开发验证顺序遵循 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md)。旧记录保留当时的结果；新的测试等待、会话隔离与实际验证结果见 [0016](./0016-development-validation-feedback.md)。
