# P1-① 能力就绪状态与操作员控制冲突：诚实性验证

日期：2026-10-10（Asia/Shanghai）。对应 Issue [#10](https://github.com/kksty/HuntWeave/issues/10) 与 [#11](https://github.com/kksty/HuntWeave/issues/11)，依据 P1 规格 [第 3.4、3.5 节](./../specs/0002-real-execution.md)与第 4 节①档，以及 [ADR-0010](./../adr/0010-real-execution-boundary-and-gate.md)。本轮不接入真实执行端（仍属 #16–#18），验收只用既有假执行账本、一次性栈与浏览器流程。

## 问题与判定

- **#10**：`GET /api/v1/system/capabilities` 存在但前端不消费；`RunView.execution_ready: bool = True` 是硬编码默认值且从不被置否；能力契约用 `Literal[False]`/`Literal["environment_unsupported"]` 把状态写死。后果是执行端不可用或门槛未满足时界面仍显示就绪。裁决：契约改为可表达的状态且原因码可空，`real_execution_ready` 按 ADR-0010 四项门槛逐项计算，前端消费能力接口并在页面标注执行模式与未逐项满足的门槛，`execution_ready` 随执行端观测变化。
- **#11**：`RunConsole.vue` 的 `control()` 收到 `version_conflict` 后用重新读取的版本**静默重发**一次暂停/取消/结束。`PROJECT.md` §13.3 的版本校验正是防止操作员的决定被执行在他没看到的状态上。裁决：冲突后先展示新状态（状态、阶段、版本），由操作员显式确认后才重新发送。

## 实现

| 改动点 | 行为 |
| --- | --- |
| 能力契约（`contracts/capabilities.py`） | `real_execution_ready: bool`、`reason_code: str \| None`、`observed_at: datetime \| None`、`mode`、`execution_profile` 与 `gates: list[ReadinessGate]`；新增 `Capabilities.unobserved(reason_code)` 表达「未取得受信结论」，该状态不含观测时间 |
| 门槛判定（`execution/capabilities.py`） | `evaluate(fake_execution_ready, now)` 返回四项 ADR-0010 门槛。门槛 2 读取契约模型字段（`real_execution_ready` 是否为真实 `bool`、`reason_code` 是否可空），门槛 3 读取本镜像所服务的控制台构建产物（`huntweave.config.CONSOLE_BUILD` 下 `assets/*.js` 是否请求 `/api/v1/system/capabilities`，按进程记忆一次）；门槛 1、4 来自代码内事实（`PROFILE_REVALIDATED`、`REVERT_ENTRY_AVAILABLE`），由 #16/#18 的复验记录与回退入口替换，不接受任何环境变量单独开启真实执行。四项全满足**且执行链可用**才为 `real_execution_ready=true` 且 `reason_code=null` |
| 运行视图默认值（`contracts/runs.py`） | `execution_ready` 默认改为 `False`：未向执行端观测过的响应不得声称就绪，控制侧再以当前观测覆盖 |
| 执行端上报（`execution/server.py`） | `/v1/capabilities` 改为实际观测：能打开账本 → `fake_execution_ready=true`；`mkdir`/租约失败（`OSError`、`RunnerRejected`）→ `false`，`reason_code=runner_state_unavailable`。顶层未就绪原因仍取 ADR-0010 的 P1 结论 `environment_unsupported`，具体缺项在 `gates` 内逐条给出 |
| 执行端健康检查（`execution/healthcheck.py`） | 除可达外还要求 `fake_execution_ready`：账本不可用时容器不得被报告为健康 |
| 控制侧观测（`api/readiness.py`） | `CapabilityProbe` 缓存最近一次能力结论（默认 5 秒），仅用于展示；`httpx`/校验失败记为 `runner_unavailable` 且 `observed_at=null`，不据失败推断就绪 |
| 控制侧组合（`api/app.py`） | `create_app(..., capability_reader=...)` 新增注入点（与既有 `observation_reader` 同形）；所有返回 `RunView` 的响应（列表、创建、读取、入队、pause/resume/cancel/close、snapshot 内嵌 run、核对结果内嵌 run）经 `observed()` 写入当前 `execution_ready`；`/api/v1/system/capabilities` 返回观测结果或显式观测缺口；SSE 心跳不再固定声明 `demonstration:true`，改为携带当前 `mode` |
| 控制台消费（`App.vue`、`RunConsole.vue`、`workspace.ts`） | `workspace.load()` 读取能力接口并每 10 秒刷新；模式、两项就绪结论与阻塞原因以 store 计算属性集中表达，顶栏徽标、平台提示（含能力观测时间）与「真实执行就绪门槛（逐项）」折叠区都由响应渲染；控制台提示与执行端不可用告警同源；新增各门槛与未就绪原因的中文文案 |
| 控制冲突（`RunConsole.vue`） | `control()` 收到 `version_conflict` 时不再重发：读取新状态并展示「状态已变化，操作未执行」面板（含点击时版本、当前状态/阶段/版本），仅在操作员点击「按新状态确认…（版本 N）」后以新版本重新发送；`放弃` 直接撤销 |

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.8.2、Compose v5.5.1）、宿主 Node v26.9.0、`backend/.venv` Python 3.12.14（pytest 9.1.1）。本地检查在 `backend/` 目录执行；一次性栈为独立项目 `huntweave-p0-checks`（`HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1`、Web 端口 18000、独立卷与网络）。

| 行为 | 结果 |
| --- | --- |
| 契约仍能表达未就绪（门槛 2） | `test_capabilities.py`：默认模型判定为真；定义 `real_execution_ready: Literal[False]`、`reason_code: Literal["environment_unsupported"]` 的子类时判定为假 |
| 控制台消费能力接口（门槛 3） | 该检查在空构建目录下为假，写入含 `/api/v1/system/capabilities` 的产物后为真（门槛函数按进程记忆，检查显式清缓存后重读） |
| 门槛未满足时保持演示模式 | 固定未满足的门槛后 `real_execution_ready=false`、`mode=demonstration`、`reason_code=environment_unsupported`，未满足门槛逐条带原因码、已满足门槛的原因码为 null |
| 四项全满足才开放 | 固定四项为满足时 `real_execution_ready=true`、`reason_code=null`、`mode=real`；未满足回退入口时 `deployment_revert` 为假、原因码 `revert_path_missing` |
| 执行链不可用时真实执行同样关闭 | 固定四项门槛为满足但 `fake_execution_ready=false`：`real_execution_ready=false`、`mode=demonstration`、`reason_code=runner_state_unavailable`（四项门槛本身仍全部为满足，故不出现「已就绪但执行端不可用」的展示矛盾） |
| 执行端账本不可用 | 以「父路径是文件」的目录创建 Runner：`fake_execution_ready=false`、`reason_code=runner_state_unavailable`；可用目录下 `observed_at` 非空且四项门槛齐全 |
| 控制侧不据失败推断就绪 | 观测函数抛 `httpx.ConnectError` 时 `CapabilityProbe.current()` 等于 `Capabilities.unobserved("runner_unavailable")`、`observed_at=null`、`fake_execution_ready=false`；连续三次读取只向执行端请求一次 |
| 运行视图随执行端观测变化 | 一次性栈集成检查：可用的注入源下 `execution_ready=true`；注入 `runner_unavailable` 时单条读取、列表读取与 snapshot 内嵌 run 全为 `false`，能力响应 `observed_at=null` |
| 线上能力响应（dev 栈实测） | `mode=demonstration`、`fake_execution_ready=true`、`real_execution_ready=false`、`reason_code=environment_unsupported`、`observed_at=2026-10-09T16:34:03.393419Z`；门槛逐项为 `profile_revalidation=false/profile_unvalidated`、`contract_expressiveness=true`、`console_consumption=true`、`deployment_revert=false/revert_path_missing` |
| 本地纯检查 | `ruff check src tests` 全部通过；`mypy --config-file pyproject.toml src` 在 37 个源文件上无问题；`python -m pytest -m "not integration" -q` → **78 项通过、4 项跳过、38 项按标记排除**（本轮前为 68 项通过） |
| 一次性栈后端检查 | `python -m pytest -p no:cacheprovider tests` → **120 项全部通过**（记录 `0008` 为 109 项；本轮新增 11 项 = `test_capabilities.py` 9 项 + Runner 边界 1 项 + 身份/Run 集成 1 项） |
| 启动故障探针 | `deploy/verify_startup.py --project huntweave-p0-checks --web-port 18000` → 5 项全 PASS（含「Runner 停止时就绪检查拒绝」与 supervisor 恢复健康） |
| 真实进程与卷故障注入 | `deploy/verify_p0.py --project huntweave-p0-checks --base-url http://127.0.0.1:18000 --no-console-fixture` → 7 项探针全 PASS（app 重启续跑、事件分页一致、证据缺失与恢复、Runner 重启后 unknown 不重放、核对后受限结束、停库后租约到期停止、检查点写失败不重复动作） |
| 浏览器流程 | `frontend/tests/authorized-run.spec.ts` 8 条（其中核对路径因未带 fixture 跳过）：第 1 次调用 5 通过 1 跳过 2 失败，第 2 次调用 2 通过；失败原因为按 IP 登录限速（见下），非功能缺陷 |
| 本轮新增浏览器断言 | 能力模式与原因由接口响应渲染（对照响应的 `reason_code`、`mode` 与门槛条数，未满足门槛数与响应一致）；把能力响应替换为 `runner_unavailable` 后页面显示「执行端不可达」与「执行端不可用」，顶栏不声明真实执行、控制台不显示就绪；**版本冲突为真实 409**：冻结控制台所读的 snapshot（`page.route` 固定应答）让操作员停留在旧版本，同时由调度真的推进 Run（`page.request` 轮询确认版本已前进），点击「取消 Run」提交旧版本 → 服务端返回 `version_conflict`；面板出现时解开冻结，断言面板中「当前版本 > 点击时版本」，且在确认前请求数仍为 1，确认后请求数 ≥ 2 并最终出现「已取消」 |
| 截图 | `runtime/validation/p1-capability-honesty.png`、`p1-capability-unavailable.png`、`p1-version-conflict.png`（`runtime/` 不入 Git） |
| 开发入口 | 前端改动按记录 `0009` 的新规则由宿主 `npm run build` + 同步进容器（本轮用 `docker cp` 直接同步同一份 `frontend/dist`，容器内 `index.html` 指向的产物与宿主一致）；`huntweave-control:p0` 镜像重建后核对内容：`contracts/capabilities.py` 含 `real_execution_ready: bool`、`execution/capabilities.py` 存在、`frontend/dist/assets/*.js` 命中 `/api/v1/system/capabilities`，`huntweave-checks:p0` 含 `tests/test_capabilities.py`（不只看构建退出码） |
| 两轴代码审查 | 对本轮改动跑 Standards 与 Spec 两轴（并行子代理，对照 `AGENTS.md`、`PROJECT.md`、`GLOSSARY.md`、ADR-0010 与 Issue #10/#11 正文）。**采纳**：`RunView.execution_ready` 契约默认值由 `true` 改为 `false`（未观测过的响应不得声称就绪）；删除未被任何 Compose 文件设置的 `HUNTWEAVE_REAL_EXECUTION_ENABLED` 开关，门槛 4 只由回退入口决定；`real_execution_ready` 追加执行链可用条件，避免「已就绪 + 执行端不可用」的矛盾展示；`CONSOLE_BUILD` 收敛为 `huntweave.config` 单一来源；就绪词汇集中到 store 计算属性；SSE 心跳不再固定声明 `demonstration:true`；文档引用由 `0009` 更正为 `0010`；浏览器冲突检查改为真实 409。**未采纳**（见「待人工确认」）：顶层原因码改为逐门槛原因码；门槛 2、3 改为构建期断言。 |

镜像重建与一次性项目均未触碰日常 `huntweave` 项目的持久卷；故障注入只作用于 `huntweave-p0-checks`（`deploy/verify_*.py` 拒绝其他项目名）。

## 未达成与限制

- **两项构建门槛是结构性判定**：门槛 2 读契约模型、门槛 3 读所服务的控制台构建产物。它们能证明契约不再是写死的字面量、且所服务的产物确实请求了能力接口，但不能证明界面渲染正确——后者由浏览器检查覆盖。失败方向偏保守：产物缺失或未同步时门槛保持未满足，真实执行继续关闭。
- **顶层原因码仍是 `environment_unsupported`**：ADR-0010 把该值定为 P1 期间的默认结论，因此本轮保留它作为顶层原因，把具体缺项放进 `gates`。spec 0002 第 3.4 节要求未就绪原因按实际来源区分，本轮在门槛层满足该要求，顶层语义是否随之细分见「待人工确认」。
- **`execution_ready` 仍是「假执行链路就绪」**（P0 规格第 1 节）：它现在随执行端观测变化，但所有 Run 的 `execution_profile` 仍是 `fake-p0-v1`，真实执行接入后是否需要按 Run 的执行 profile 判定见「待人工确认」。
- **能力观测有 5 秒展示缓存**：`CapabilityProbe` 只服务界面与响应渲染；派发路径不读该缓存，每次调用仍直接提交给执行端并由其账本结算。执行端挂起时，每个 5 秒窗口最多引入一次 3 秒超时等待。
- **能力请求会创建执行端状态目录**：`fake_execution_ready` 通过打开账本观测，首次能力请求因此具备与首次提交相同的建目录副作用；`mkdir` 失败时报 `runner_state_unavailable`。
- **门槛 1、4 仍是代码内事实**：`PROFILE_REVALIDATED`/`REVERT_ENTRY_AVAILABLE` 目前是写死在 `execution/capabilities.py` 的两个占位事实（各带替换来源注释），由 #16/#18 的靶场复验记录与回退入口替换。本轮把它们改成可计算值会先于对应切片发明判定口径，故保留；这是「状态仍写进代码」的剩余部分，见「待人工确认」。
- **Runner 健康语义变严**：账本不可用时 Runner 容器报不健康（此前只看端点可达）。这会让 `depends_on` 的 app 启动等待失败，属有意为之，但未在失败宿主上实测。
- **浏览器流程是否需分两次调用（后续已调整）**：原按 IP 登录限速 5 次/60 秒使本记录当时的套件需要分批；限速已按 0.9.6 取消，当前每项业务检查使用独立会话。见本记录末「本记录的更正」与 `0016`。
- **浏览器使用 Microsoft Edge（`channel: msedge`）**：Playwright CDN 在本机下载 Chromium（197.5 MiB）多次停顿（一次停在 51.9 MB、一次 0 字节），改为用已安装的 Edge 执行本轮浏览器检查（临时配置，未入库）。README 的入口仍是 `npx playwright install chromium`。
- **核对浏览器路径本轮跳过**：`verify_p0.py` 以 `--no-console-fixture` 运行，第 4 条核对流程按设计跳过；该路径由记录 `0006` 覆盖，本轮未改核对逻辑。
- **未验证**：Linux 原生宿主；真实执行端接入后的就绪（门槛 1、4 仍为假，`real_execution_ready` 因此在该宿主上永远为假）；`execution_ready` 与调度路径的关系（本轮明确不改派发行为，`execution_ready` 仅用于展示，符合 spec 0002 第 3.4 节「界面显示只是结果」）。
- **前端原因码文案仅补本轮相关项**：新增门槛与未就绪原因的中文文案；②档「前端原因码文案与后端实际发出不一致」的逐条对齐仍归 [#19](https://github.com/kksty/HuntWeave/issues/19)。

## 待人工确认

- [ ] 顶层 `reason_code` 是否应从 `environment_unsupported` 改为第一个未满足门槛的原因码（如 `profile_unvalidated`）。本轮按 ADR-0010 的 P1 结论保留顶层值，逐项原因在 `gates` 内；若要细分，需同步 ADR-0010 与 `docs/STATUS.md`。
- [ ] 门槛 2、3 用「读取契约模型 + 读取所服务的控制台构建产物」判定是否可接受，或应改为显式的构建/CI 断言（例如构建期写入能力契约版本并由镜像校验）。
- [ ] 门槛 1、4 的两个代码内事实（`PROFILE_REVALIDATED`、`REVERT_ENTRY_AVAILABLE`）是否应在 #16/#18 之前改为可计算值（如复验记录文件存在性、回退入口存在性），以免「状态写进代码」再现。
- [x] 浏览器套件是否改为共享一次登录会话（Playwright `storageState`）——共享方案仅为中间尝试，当前已由每项独立 API 会话替代（见 `0016`）；限速亦已取消。
- [ ] `fake_execution_ready` 的观测副作用（首次能力请求创建 runner 状态目录）与 Runner 健康语义变严是否可接受。
- [ ] `execution_ready` 是否保持「假执行链路就绪」的含义，或在真实执行接入后改为按 Run 的执行 profile 判定（并相应更新 P0 规格第 1 节与 `docs/STATUS.md`）。

## 本记录的更正

**第 60、71 行所述的按 IP 登录限速与其影响已不存在。** 2026-10-10 按用户明确要求取消登录/密钥提交的频率限制（见 `PROJECT.md` 0.9.6 与 §9.3）：Key 即时校验，错误次数不造成锁定或等待；额外公网请求控制由部署者按需配置。`login_buckets` 表、`AppSettings` 的三个限速字段与 429 `rate_limited` 分支已随迁移 `0007_drop_login_throttle` 删除。

取消限速期间曾尝试 `global-setup.ts` + `storageState` 整套共享会话，这使退出或重新登录可以影响后续检查，且保存 Cookie 不会主动打开页面。该中间方案已删除；当前由 `frontend/tests/fixtures.ts` 为每项业务检查独立登录并打开工作台，入口检查保持匿名，实际结果见 [0016](./0016-development-validation-feedback.md)。第 61 行「使用 Microsoft Edge」是本记录当时的临时做法；后续浏览器检查使用本机已安装的 Chromium。

原结论中与本次取消无关的部分不变：门槛 1、4 当时是代码内事实、能力观测的 5 秒展示缓存、Runner 健康语义变严等均在后续切片中分别处置（`0011`/`0012`/`0013`/`0015`）。
