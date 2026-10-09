# P1-① 核对入口：`unknown` 调用的证据裁定验证

日期：2026-10-09（Asia/Shanghai）。对应 Issue [#15](https://github.com/kksty/HuntWeave/issues/15)，依据 P1 规格第 3.3、3.5、5、6 节与 ADR-0010。本轮把「结果未知的调用」从永久停在 `waiting`（`resume`/`close` 被拒绝、`cancel` 无法收敛）变成可被操作员依据证据裁定的对象，并把**裁定**与**执行端停止确认**分开记录。真实执行仍未开放，验收只用固定假执行账本，不连接任何目标。

## 环境与入口

Windows 工作区 `D:\自动化渗透平台`；Docker Desktop Linux containers；一次性验收项目 `huntweave-p0-checks`（自己的 PostgreSQL、证据与 Runner 状态卷，Web `127.0.0.1:18000`）。日常 `huntweave` 组未被注入故障、数据库未清空。新增迁移 `0004_reconciliation` 并把 `BUSINESS_REVISION` 升到该版本；迁移在一次性库上做了一次 `downgrade 0003 → upgrade head` 的往返验证。

```powershell
$env:HUNTWEAVE_WEB_PORT = "18000"; $env:HUNTWEAVE_PUBLIC_ORIGIN = "http://127.0.0.1:18000"
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml build app
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify build checks
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 180

# 后端与契约检查（显式一次性数据库）
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify run --rm --no-deps checks python -m pytest -p no:cacheprovider tests

# 启动与恢复探针（后者会重启 app/Runner 并留下一个待核对 Run，打印其 id）
python deploy/verify_startup.py --project huntweave-p0-checks --web-port 18000
python deploy/verify_p0.py --project huntweave-p0-checks --base-url http://127.0.0.1:18000

# 浏览器路径（把 verify_p0.py 打印的 run_id 传进去）
cd frontend; $env:HUNTWEAVE_E2E_BASE_URL = "http://127.0.0.1:18000"
$env:HUNTWEAVE_E2E_RECONCILE_RUN_ID = "<CONSOLE_RECONCILIATION_RUN>"; npm run test:e2e
```

## 行为与结果

| 行为 | 结果 |
| --- | --- |
| 三种裁定可表达 | 接口与界面都能表达「确认未执行」「确认已执行」「仍未决」；证据不足不强迫二选一（`undetermined` 不改变调用状态） |
| 「确认未执行」不是重试开关 | 无受信记录 → 409 `reconciliation_evidence_missing`；账本证明动作已开始 → 409 `reconciliation_evidence_contradicted`；原控制租约仍有效 → 409 `reconciliation_lease_active`；三者都不写记录、不改调用 |
| 「确认已执行」不伪造结果 | 需账本证明动作已开始；调用进入新终态 `incomplete`，不写 `ToolResult`、不补证据，`redispatch_authorized=false`，研究按步骤继续 |
| 停止确认独立于裁定 | 账本 `observation` 为 `process_active=null`/`connection_open=null` 时报告**未确认**；确认已执行但停止未确认时 `resume` 返回 409 `execution_stop_unconfirmed`、`close` 同样拒绝，`resume-preview` 逐调用给出 `conditions` |
| Run 按证据与资源事实收敛 | 停止已确认而结果仍缺失时，`cancel` 收敛为 `cancelled` 并带 `limited=true`、`incomplete_calls`，`reason_code=result_incomplete`；未核清时停在 `cancelling` 并保留 cancel/查询路径（真实探针覆盖「重启后 unknown → 裁定已执行 → 取消 → 执行端确认停止 → 受限结束」全链路） |
| 重派使用新 `call_id` 并关联原调用 | 裁定授权后在同一决策上重派：新决策记录 + 新 `call_id` + `replaces_call_id` 指向原调用，动作与参数不变；重派前重新校验授权时间窗与预算；图步骤重放不会派发第三次 |
| 核对只新增业务侧记录 | 执行端账本在裁定前后逐字节一致（探针比对 `execution_started` 只出现一次、`unknown` 状态不变）；界面把裁定呈现为「操作员裁定」，执行事实单列 |
| 幂等、冲突与版本 | 同一裁定重复提交幂等返回且只写一条记录与一条事件；改判 409 `reconciliation_conflict`；版本过期 409 `version_conflict`；非未知调用 409 `invalid_call_state` |
| 证据与范围绑定 | 记录绑定操作员会话标识、时间、范围版本、依据证据（含不存在或不属本轮的证据一律拒绝）与作出决定时读取到的执行端观测 |
| 结果计数 | 一次性栈内 91 项后端检查全部通过；`verify_startup.py` 全部通过；`verify_p0.py` 7 项故障/恢复探针全部通过；Playwright 5 项浏览器流程全部通过（含新增核对路径，截图 `runtime/validation/p1-reconciliation.png`）；本地 ruff、严格 mypy、56 项非集成检查与前端类型/生产构建通过 |

## 验证过程中发现并修正的缺陷

1. **核对记录的外键阻断了会话清理**：`reconciliation_decisions.operator_session_id` 原为 `web_sessions` 外键，而访问层会删除过期会话，于是存在任何一条裁定后登录即 `IntegrityError` → 503 `storage_unavailable`。改为按值保存操作员会话标识（会话是会被清理的运行时数据，裁定仍需留名），模型与迁移同步注明原因。集成检查没覆盖这条路径，是真实栈探针发现的。
2. **裁定后调度器不再读取该调用的账本**：`pending()`/`pending_runs()` 只返回非终态调用，而裁定后的调用已是 `incomplete`，于是「取消后由执行端确认停止」永远等不到 → Run 停在 `cancelling`。新增 `reconcilable()`/`reconcilable_runs()`（只覆盖仍在跑的 Run）供调度器使用，`pending()` 保持「在途调用」语义供图使用。
3. **一次 `ServiceError` 会杀死调度器**：窗口查询与逐 Run 对账之间 Run 被清掉时，sweep 抛出的 `ServiceError` 逃出 `agentd` 主循环 → supervisor 退出 → 容器反复重启；claim 分支早已处理同类错误，sweep 分支没有。补齐同样的处理。
4. **既有恢复探针的顺序脆弱断言**：`app_restart_fault` 用 `records[0]` 当作「首个调用」，但账本按 `call_id` 排序、不是创建顺序，断言随 Run id 随机成败。改为「被接受的调用恰好出现一次」。

## 未达成与限制

- **真实执行仍关闭**（`real_execution_ready=false`）：本轮观测与停止确认只由假执行账本提供；真实执行端接入后必须给出等价的 `started`/进程/连接事实（#16、#18）。
- 「确认未执行」的可证性依赖账本在副作用之前写入 `execution_started`；真实动作需要 #18 保证同等前置写入，否则该裁定只能停在 `undetermined`。
- 重派只在同一决策上再派发一次，且仍需 #9 修正后才是逐目标正确的票据绑定；#17 按自身验收条件验证真实动作闭环。
- 界面新路径在 `version_conflict` 后不静默重发；既有控制动作的静默重发仍由 [#11](https://github.com/kksty/HuntWeave/issues/11) 跟踪。
- 浏览器核对路径需要一个「结果未知」的 Run，由 `verify_p0.py` 的核对探针创建并经环境变量传入；浏览器流程自身无法制造该状态。

## 待人工确认

- [ ] 三种裁定、受限结束与 `incomplete` 终态的语义与 `GLOSSARY.md`、`PROJECT.md` §8.2 一致。
- [ ] 新增原因码 `result_incomplete`、`reconciliation_*`、`execution_stop_unconfirmed` 的命名与映射可接受。
- [ ] 缺陷 1–3 的修正范围合理（缺陷 2、3 属既有调度/恢复健壮性问题，随本条一并修）。
