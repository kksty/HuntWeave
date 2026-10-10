# P0-B 身份与授权假 Run 验证

日期：2026-10-09（Asia/Shanghai）。对应 Issue #3，依据 P0-B 规格与 PROJECT 0.7.2。本轮实现登录与持久演示记录，未执行目标测试。

## 环境与入口

Windows 工作区（仓库根目录）；Docker Engine 29.7.2 / Compose 5.4.0 / Linux containers。本地及容器 Python 3.12.15，原 Python 依赖保持 `backend/uv.lock`；本地 Node 24.20.0，容器前端构建使用固定 digest 的 Node 22。Vue、Pinia、Vue Router、Vite、TypeScript、Playwright 由 `frontend/package-lock.json` 固定。

独立临时项目 `huntweave-p0b-checks` 使用自身 PostgreSQL/证据卷与专用网络，Web 为 localhost:18000；验收结束后已删除其容器/卷/网络，本机只保留 localhost:8000 的 `huntweave` 三服务。身份与 Run 故障测试只有显式设置 `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 才执行；会重置测试表，不在开发数据库运行。浏览器回归只追加假记录，可在开发组执行。完整命令集中维护于 README。

中文工作区路径下，Compose 同时构建 app/runner 曾出现 `x-docker-expose-session-sharedkey` 含不可打印字符的构建会话错误。单独 `build app` 后 `up --no-build` 已通过。迁移新增 `0002_identity_runs`，保留业务/checkpoint schema 与运行账号隔离；健康检查使用动态读取的密钥文件。

## 行为与结果

| 行为 | 结果 |
| --- | --- |
| 三服务启动 | 全新验收项目初始化、迁移，app/postgres/runner 均 healthy；仅发布 Web |
| 页面及文件访问门槛 | 未认证 HTML 主页面/Run 深链进入独立登录页；API、业务 JS/CSS、文档、证据及事件路径返回 401；登录资源无业务 bundle |
| 会话与秘密 | 随机令牌仅在 Cookie，数据库存摘要；HttpOnly/SameSite=Strict；HTTPS 使用 Secure/__Host- 且无 Domain；响应、校验错误不回显密钥；无 localStorage/sessionStorage 登录凭据 |
| Origin / CSRF | 登录检查配置的 Origin；业务变更还检查 CSRF，缺失/错误均拒绝；校验错误不回显输入 |
| 到期 / 退出 / 轮换 | 闲置和绝对到期分别拒绝；退出与重新登录撤销旧令牌；重建 app 后会话仍可读取；原密钥文件变更后下一请求拒绝旧会话并持久化版本 |
| 猜测限速 | 来源 IP / 全局窗口持久化，转发头不能更换来源；重建 app 保持限速；过窗口恢复，不无限延长封锁；请求体含 chunked 均限制登录 4 KiB |
| 目标预览 | 保留错误行号和原值，IPv4/IPv6 规范化与去重；URL、端口、域名、CIDR、zone ID 拒绝；私有 IP 可用；受保护地址和当前未启用隔离的 IPv6 明确阻断 |
| 端口和授权 | common-tcp-v1、custom-tcp-v1、all-tcp-v1 展开具体 TCP 端口；期限、授权说明、预算/profile/config 固定在授权与 Run；过期、未来未生效范围及非法预算拒绝创建 Run |
| 创建与状态并发 | 同幂等键同请求返回同 Run、内容不同 409；8 个并发创建得到唯一记录；2 个并发 start 只有一个成功，旧状态版本 409；排队不调用 Runner。界面同一快照重试创建复用原 Run，重新保存快照时分配新的请求键 |
| 浏览器路径 | 登录 → 项目 → 错误 IP / IPv6 阻断 → 合并重复 IP → 展开端口 → 授权快照 → draft Run → queued → 刷新持久读取 → 退出，全程通过；项目文本中的脚本按纯文本展示 |
| 检查数量 | Linux 容器 52 项 pytest 全部通过；Windows 28 项纯单元通过；2 个 Playwright Chromium 流程通过；前端类型/生产构建、Ruff 与严格 mypy 通过 |

浏览器截图为 `runtime/validation/p0-b-workspace.png`，仅包含文档示例 IP 与假记录，未纳入 Git；认证 Cookie、密钥与浏览器 trace/video 未归档。产品 UI 使用 HuntWeave 品牌和中文功能文字。

## 代码复审

使用仓库 `code-review` 技能，以 `5b3a3e4` 为基线，对实现提交 `401110b` 分别进行 Standards / Spec 复审。Standards 未发现明确约定违例或可执行的代码异味；Spec 发现一项 P2：重新保存不变配置创建了新授权快照，却复用旧 Run 请求键，导致界面持续 409。

修复为每次成功保存新快照后生成新幂等键，同一快照的创建重试保留原键。浏览器回归同时验证重试不新增 Run、重新保存后可创建另一 Run；修复后 2 项浏览器流程在唯一的 `huntweave` 开发组通过。直接提交到 main，不创建 PR。严格类型检查需使用 `backend/pyproject.toml` 的 Linux 平台设置：`backend/.venv/Scripts/mypy --config-file backend/pyproject.toml backend/src`，避免按 Windows 标准库误报容器内 POSIX 调用。

## 当前限制

Run 仅能创建为 draft、版本化进入 queued 并读取；假执行器、LangGraph 研究图、证据/SSE、暂停取消恢复仍由 #4/#5 交付。真实执行和假动作能力继续返回未就绪，不从排队状态推断已执行。既有 SSE 尚不存在，#4 必须落实最长 60 秒会话复查后才能宣布流失效验收通过。

验证了 HTTPS Cookie 配置与远程 HTTP 拒绝降级；远程 TLS/反向代理生产部署、Linux 原生宿主、完整 P0 故障矩阵仍待验收。访问密钥轮换使用独立本地入口，不轮换数据库密码。

## 本记录的更正

**「猜测限速」一行（第 22 行）所记的行为已不存在，且不再作为要求。** 2026-10-10 按用户明确要求取消登录/密钥提交的频率限制：密钥是部署生成的至少 32 字节随机值，按来源 IP 的通量上限对「猜测它」没有实际防护收益，代价是少量输错后把操作员锁在自己的平台之外、并让任何脚本化使用依赖与内容无关的计时器。请求级滥用防护改由反向代理层承担（那一层能看到真实来源地址、覆盖整个入口而非单个接口）。

该行其余部分仍成立：登录请求体上限 4 KiB（含 chunked）、转发头只按已配置代理采信（本行原表述为「转发头不能更换来源」，在新形态下对应「不据转发头做任何判定」）。`login_buckets` 表与其计数、`AppSettings` 的三个限速字段与 429 `rate_limited` 分支随迁移 `0007_drop_login_throttle` 删除；`docs/specs/0001-foundation.md` 第 4 节与 `PROJECT.md` §9.3 已同步改写，`PROJECT.md` 版本记为 0.9.6。
