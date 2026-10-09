# P0-C/D 假执行闭环、证据时间线与故障恢复验证

日期：2026-10-09（Asia/Shanghai）。对应 Issue #4、#5，依据 P0 规格 P0-C/D、第 6 节故障矩阵与第 7 节完成定义。本轮把真实 LangGraph、确定性模型 Adapter、持久假 Runner、预算/outbox、原始证据与 SSE 时间线连成 Collector → Worker → Reviewer 闭环，并补齐暂停/取消/对账/恢复与人工结束。仍未执行任何真实目标测试。

## 环境与入口

Windows 工作区（仓库根目录）；Docker Desktop Linux containers。本地 Python 3.12.15（`backend/.venv`，由 `backend/uv.lock` 锁定），容器 Python 3.12.15，Node 24.20.0（容器内前端构建使用固定 digest 的 Node 22）。一次性验收项目 `huntweave-p0-checks` 使用自己的 PostgreSQL/证据/Runner 状态卷，Web 为 `127.0.0.1:18000`；故障注入脚本拒绝其他项目名、拒绝 8000 端口与非回环来源。日常 `huntweave` 组未被注入故障、数据库未清空。

```powershell
# 一次性验收栈：构建 → 全新卷 → 启动
$env:HUNTWEAVE_WEB_PORT = "18000"; $env:HUNTWEAVE_PUBLIC_ORIGIN = "http://127.0.0.1:18000"
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml build app
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify build checks
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml down -v
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 180

# 后端与契约检查（显式一次性数据库）
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify run --rm --no-deps checks python -m pytest -p no:cacheprovider tests

# 启动故障与恢复故障探针
python deploy/verify_startup.py --project huntweave-p0-checks --web-port 18000
python deploy/verify_p0.py --project huntweave-p0-checks --base-url http://127.0.0.1:18000

# 浏览器路径
cd frontend; $env:HUNTWEAVE_E2E_BASE_URL = "http://127.0.0.1:18000"; npx playwright test
```

## 行为与结果

| 行为 | 结果 |
| --- | --- |
| 确定性角色闭环 | 确定性 Adapter 按 Run 记录的演示场景分支：Collector 的 step-0 决策不看场景，`negative` 只在 Worker 步骤内触发结束研究并推进 Reviewer，`positive` 继续独立验证；Adapter 不读取已记录的调用输出，所以这是「配置→决策」而非「输出→决策」（见「本记录的更正」）；决策摘要只记录依据、预期与停止条件，不展示或伪造内部思维链 |
| 事务化意图 | Decision、ToolCall、预算预留、outbox、事件游标在同一短事务提交；重放同一决策只产生一个调用、一条 `tool_planned` 与一次预留（集成检查） |
| Runner 契约 | 耐久账本按 `call_id`+参数 hash 复用记录；同一 `call_id` 换参数拒绝；未认证 401；身份/范围/策略/期限/控制租约过长均拒绝；`renew`/`cancel` 只接受记录中的租约代次 |
| 租约代次按会话比较 | 恢复过的 Collector（代次 2）不再阻断后续 Worker 会话（代次 1）的新调用；同一会话更旧代次仍被拒绝 |
| 未知结果不重派 | Runner 在启动附近重启后，账本把在途调用标为 `unknown`；业务侧记录该判定，不二次启动、不新增记录，Run 进入 `waiting` 并给出核对条件 |
| 控制接口不可达 | Runner 不可达时停止派发与续约，不把“查不到结果”写成 unknown；已接受调用由控制租约兜底，恢复后按原 `call_id` 对账（集成检查覆盖连接错误与 5xx） |
| 调度公平 | 持有未确认调用的 Run 不再被周期性领取，但每轮扫描仍以轮转窗口重新读取其账本；新 Run 不会被卡住的旧 Run 长时间饿死 |
| 证据 | 归档先写文件后记 hash，app 只读挂载；读取时重新校验 sha256 与大小，缺失/哈希不符明确标注 `archive_missing`/`archive_hash_mismatch`；删除后恢复文件，hash 与内容一致 |
| 事件时间线 | 每 Run 按提交顺序游标，重复来源事件按 `source_event_id` 去重；分页历史游标连续、与快照 `cursor` 一致；SSE 重连按 `Last-Event-ID` 补拉并每 15 秒复查会话 |
| 控制与恢复 | 暂停让当前受限动作收尾后收敛为 `paused`；取消由执行端确认后收敛为 `cancelled`，前端不提前假报成功；恢复预览给出上次步骤、待核对调用、剩余预算与授权有效性；仅 `awaiting_human` 可人工结束演示 |
| 版本冲突 | 调度推进产生的版本变化不再让一次明确控制点击静默丢失：界面在 `version_conflict` 后重新读取状态并重发一次，仍失败才提示 |
| 序列化视角 | 新增 `RunSnapshot`/`EventPage`/`ResumePreview`/`EvidenceView` 契约，业务页面所用资源均有版本化 Schema，不再以裸字典对外 |
| 容器检查 | 一次性栈内 77 项 pytest 连续两次全部通过（53 项非集成 + 24 项集成；集成项只有显式设置 `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 才执行） |
| 启动故障探针 | 5 项通过：缺密钥镜像阻断业务资源、Runner 非 root 且无平台密钥/socket、app 证据卷只读、Runner 停止时就绪检查失败、agentd 退出后 supervisor 重启并恢复健康 |
| 恢复故障探针 | 6 项通过：app 重启复用已接受调用并续跑、分页事件与快照一致、证据缺失与恢复、Runner 重启不重放、PostgreSQL 中断后按控制租约到期停止且账本保留、检查点写失败后无重复动作或重复结算 |
| 浏览器路径 | 4 项 Playwright Chromium 流程通过：匿名深链/资源鉴权与会话 Cookie、预览→冻结→幂等创建→排队→刷新、暂停→恢复预览→原始证据→人工结束、取消等待确认且不再派发 |
| 本地检查 | Ruff、严格 mypy（35 个源文件）、51 项纯单元检查（连续三次）与前端 `vue-tsc`/生产构建通过 |

浏览器截图保存在被忽略的 `runtime/validation/`，只含文档示例 IP 与假记录。

## 本轮修复的恢复语义缺陷

独立验收首次运行暴露三处问题，均已修复并补了对应检查：

1. **瞬时不可达被误判为未知**：Runner 暂不可达时，一个从未被接受的意图被写成 `unknown`，而 `unknown` 是不可终态的，导致该 Run 永久卡住。现在只有执行账本给出的判定才会成为 `unknown`，控制面不可达只记录 `execution_unreachable` 事件并保留本地状态。
2. **单个卡住的 Run 饿死调度**：唯一调度进程按创建时间返回第一个可领取的 Run，卡住的 Run 会长期占满调度。现在无法推进的 Run 不参与领取，但调度每轮仍以轮转窗口对它重新对账（`pending_runs` + `sweep`），因此结果一旦可确认仍会自动收敛。
3. **按会话比较租约代次**：恢复会提升角色任务代次，原先跨会话比较使恢复过的 Collector 阻断后续 Worker 的新调用；现在派发与执行端都只按同一会话比较，执行端自身仍拒绝更旧代次。

## 本记录的更正

P0 验收关闭之后，对 `5b3a3e4...8b9dbe3` 做了 Standards 与 Spec 两轴代码审查。以下三处表述与实现不符，在此更正；上方原始观察不改写，更正以本节为准：

1. **确定性分支不是「输出→决策」**：确定性 Adapter 按 Run 记录的 `demonstration_scenario` 分支，并**不读取**已记录的调用输出（`last_output` 被写入但从未被读取）；`negative` 只作用于 Worker 步骤，Collector 的 step-0 决策不看场景。因此「相同初始服务遇不同固定输出产生不同后续研究决策」这条能力**未真正达成**，P0 规格第 5 节 P0-C 行与 issue #4 的同名验收条件不成立，已进入 P1 修复项跟踪。
2. **issue #5 故障矩阵的「租约到期但旧调用仍在运行」行**标为 PASS，但 `deploy/verify_p0.py` 当时没有以该行命名的专用探针（六项分别是 app 重启、分页历史、证据丢失、Runner 重启、PostgreSQL 中断与检查点写失败）。复核 `database_outage_fault` 的断言后更正本节结论：该行「执行端最迟在控制租约到期触发停止」一节**已由「PostgreSQL 中断」探针实际覆盖**——它先等待调用进入 `running`，再停库，随后断言账本记录为 `cancelled` 且 `reason_code=control_lease_expired`，即控制租约在旧调用仍在运行时到期并被停止；该行「核对并回收旧调用后才释放主机锁」一节当时无探针（核对侧现由 #15 的停止确认与收敛闸门覆盖，同 IP 主机锁属 #18/#21）。因此该行应记为「由 PostgreSQL 中断探针间接覆盖」，不是「未探针」；是否需要独立探针留待 P1 决定。P0 留下未探针的是本节第 3 处与上文「事件游标保留期」一行。
3. **能力诚实性只在 API 层**：`GET /api/v1/system/capabilities` 存在，但前端没有任何代码消费它，且 `RunView.execution_ready` 的默认值为 true 且从不被置否——「界面始终标为假执行」当时只是文案，不是被程序约束的状态。

三处均属 P1 修复范围，见 [当前状态](../STATUS.md)；① 档（真实执行前必修）与 ③ 档（文档更正）分别跟踪。

## 当前限制

- 未确认（`unknown`）调用的人工核对入口尚未交付：该 Run 会停留在 `waiting`，`resume` 与 `close` 都会因存在未核对调用被拒绝，`cancel` 也需要执行端确认而无法立即结束。界面明确展示核对条件与原因，不假报成功；操作员工具属于 P1。
- 事件游标保留期与“过期缺口”降为 P2：P0 不做过期淘汰，缺口分支不可触发，P0 规格 §3.3 的这一条 AC 未实现，不按已完成对待；服务端只在游标超前时返回 409，缺口字段与前端处理保留，供引入保留期后使用。
- 证据的 `truncated`/`redacted` 标记字段已建模但假执行不产生截断或脱敏内容；真实执行接入后再验收该分支。
- 真实执行仍被禁用：`fake_execution_ready=true` 只表示固定假动作链路就绪，`real_execution_ready` 保持 false 且原因为 `environment_unsupported`；本机隔离 profile 记录见 `0002-windows-isolation.md`，原生 Linux 宿主验收与 P1 真实接入均未开始。
- 确定性模型 Adapter 是进程内纯函数，因此当前在事务内计算不产生长时间持锁；接入真实模型时必须在事务外计算决策，再在短事务内校验并落库。
- 一次调度进程的自检只覆盖默认单活跃 Run 场景；多活跃 Run 的并发推进由每轮一个领取 + 轮转对账窗口保证，尚未做多 Run 压力验收。
