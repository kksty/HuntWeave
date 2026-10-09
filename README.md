# HuntWeave

面向已授权目标的 Agent 安全测试平台。以 LLM 驱动研究决策，通过受控执行、原始证据、独立复审与人工确认形成可追溯结论。

> 当前阶段、已交付能力、下一实施项与已知限制统一记录在 [docs/STATUS.md](./docs/STATUS.md)。请先核对能力状态再使用执行入口；本文件只说明安装、部署、开发与验证入口，不复述阶段状态。

## 架构

采用单一 Docker Compose 项目，`app`、`postgres`、`runner` 为常驻服务。准备环境与工具会话由受信执行层按需管理。

```mermaid
flowchart LR
    Browser["Web 客户端"] -->|"HTTP · localhost:8000"| App

    subgraph Compose["HuntWeave · Docker Compose"]
        App["app<br/>FastAPI · agentd"]
        PG[("postgres<br/>业务 schema · checkpoint schema")]
        Runner["runner<br/>内部认证接口"]
        Evidence[("原始证据卷")]

        App -->|"状态 · 迁移 · 检查点"| PG
        App -->|"内部 token"| Runner
        Runner -->|"读写挂载"| Evidence
        Evidence -->|"只读挂载"| App
    end

    Runner -.->|"P1：受信管理"| Gateway["每会话网关<br/>NET_ADMIN · 出口白名单"]
    Tool["普通用户工具容器<br/>独立 PID · 文件系统"] -.->|"共享网络命名空间"| Gateway
    Gateway -.->|"授权 IPv4 / TCP"| Target["授权目标"]
```

实线表示已搭建的服务及存储关系；虚线表示待接入产品的执行链路。默认 Runner 未挂载 Docker socket。每会话网关方案已通过本地靶场验证，P1 接入执行票据、租约、资源管理和 Kali 工具环境后复验。

目标工作流：`Collector → Worker × N → Reviewer → 人工复审`。技术栈为 Python、FastAPI、SQLAlchemy / Alembic、PostgreSQL、LangGraph OSS、Vue 3 / TypeScript 和 Docker Compose。

后续架构采用证据驱动的任务规划：服务事实与研究依赖分别记录，Run 内规划按有效事件提出零到多项建议，`runs` 统一提交任务，Worker 保留任务内方法选择；证据显式绑定，复审与报告按输入版本保存。24×7 指一次发布后，同一 Run 在有效授权和预算内持续推进，完成后停止，重启先对账再接续。设计取舍见[架构评估](./docs/research/2026-10-09-architecture-assessment.md)和 [ADR-0011](./docs/adr/0011-planning-authority-and-evidence-revisions.md)，待实现行为见 [P2 规格](./docs/specs/0003-agent-research.md)。

正常交付顺序为 **自主渗透并留证 → AI 复审及有限补证 → 自动执行结束、释放执行资源 → 人工复审**。人工未处理结论时，证据继续按策略保留，自动执行已经停止；AI 复审无法完成等例外会明确记录原因和未决项。

目标验证规则包含默认 RCE 最小只读留证后退出、禁止破坏或删改目标已有数据、必要新增测试数据及遗留披露，见 [PROJECT 第 9.1 节](./PROJECT.md)。规划中的正式漏洞库采用 [CVSS v4.0 与独立准入门槛](./docs/specs/0004-finding-admission.md)，低危/无害信息和未证实版本命中保留研究记录；当前实现状态以 STATUS 为准。

## 环境要求

| 平台 | 容器运行时 | 当前状态 |
| --- | --- | --- |
| Windows 11 x86_64 | Docker Desktop，WSL2 后端，Linux containers | 已完成本机启动及隔离技术验证 |
| Linux x86_64 | Docker Engine + Compose 插件 | 共用部署入口，理论可部署；原生宿主验收待完成 |

宿主依赖：Git、Python 3.10+（仅部署辅助入口）和可访问的 Docker CLI；业务运行依赖全部由容器提供。前端开发与浏览器验证另需 Node 22+。独立的 Kali WSL 发行版不是项目依赖。

安装参考：[Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)、[WSL2](https://learn.microsoft.com/windows/wsl/install)、[Docker Engine](https://docs.docker.com/engine/install/)、[Python](https://www.python.org/downloads/)。

已验证宿主版本以各验证记录的实测值为准：启动与隔离验证为 Docker Desktop 4.94.0 / Engine 29.8.2 / Compose 5.5.1（见 [0001](./docs/validation/0001-startup.md)、[0002](./docs/validation/0002-windows-isolation.md)），P0-B 切片记录为 Engine 29.7.2 / Compose 5.4.0（见 [0004](./docs/validation/0004-identity-runs.md)）。Python 依赖与前端依赖分别固定在 `backend/uv.lock`、`frontend/package-lock.json`，基础镜像及 Node 构建镜像固定 digest。

## 部署

以下命令在仓库根目录执行。Windows 使用 `python`；Linux 使用 `python3`。

```sh
git clone --branch main --single-branch https://github.com/kksty/HuntWeave.git
cd HuntWeave

python deploy/initialize.py
docker compose -f deploy/compose.yaml build app
docker compose -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 150
docker compose -f deploy/compose.yaml ps
```

已有工作区使用 `git pull --ff-only` 更新。首次启动生成独立 secrets、构建镜像、初始化持久卷，并在 API/agentd 启动前完成业务与 checkpoint 迁移。初始化入口保留已有 secrets。

三个服务应为 `healthy`。存活接口：`http://127.0.0.1:8000/health/live`。

浏览器打开 `http://127.0.0.1:8000`，未登录时进入最小登录页。登录密钥由初始化入口生成在 `runtime/secrets/access_key`，在本机自行读取并填入；不要将它发到聊天、URL 或提交到 Git。未认证的 API/业务文件仍返回 401；缺失或空密钥返回 503 / `access_key_missing`。默认仅绑定 localhost，PostgreSQL 与 Runner 不发布宿主端口。

app/runner 共用控制镜像，因此先单独 `build app`，再启动三个服务。本轮 Windows 中文路径下，同时构建两服务触发了 Docker 构建会话错误；以上分步入口已实际验证。

登录后创建项目 → 粘贴并预览 IP → 展开 TCP 端口 → 填写有效期、授权说明和预算 → 保存授权快照 → 选择假输出场景 → 创建假 Run → 加入队列。唯一的调度进程会领取 queued Run，按 Collector → Worker → Reviewer 依次派发固定假动作，逐次把决策摘要、实际调用参数、输出、耗时、证据哈希与事件游标写入持久记录，并在“玻璃鱼缸”页面通过 SSE 展示；自动阶段结束后进入 `awaiting_human`，可查看恢复预览、结束演示或取消。记录在刷新或服务重启后读取一致。单 Run 导入上限为 **100 个 IP**（`PROJECT.md` §12 的保守开发默认值，按去重后的不同目标计数）。非法行必须修正或移除；重复 IP 合并；未启用 IPv6 隔离，因此 IPv6 目标可以预览但不能提交任务。页面始终标注“开发演示 / 假执行”，其输出来自固定假动作，不连接授权 IP，也不形成真实漏洞结论。

### 配置

| 配置 | 默认值 / 位置 |
| --- | --- |
| Compose 项目名 | `huntweave` |
| Web 监听 | `127.0.0.1:8000` |
| Web 端口覆盖 | `HUNTWEAVE_WEB_PORT` |
| 浏览器同源入口 | `HUNTWEAVE_PUBLIC_ORIGIN`，默认 `http://127.0.0.1:<Web 端口>` |
| HTTPS Cookie | `HUNTWEAVE_COOKIE_SECURE=true`，要求 HTTPS Origin；远程 HTTP 配置拒绝启动 |
| 平台、Runner 与数据库 secrets | `runtime/secrets/` |

端口覆盖可写入仓库根目录的 `.env`：

```dotenv
HUNTWEAVE_WEB_PORT=8080
```

使用自定义文件时，在 Compose 命令中指定 `--env-file .env`：

```sh
docker compose --env-file .env -f deploy/compose.yaml build app
docker compose --env-file .env -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 150
```

## 运行维护

### 开发模式

开发时使用 [Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/)（Compose 2.32+）：

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up --build --watch
```

源码变化同步至 app/runner 并重启服务，覆盖 API 和 agentd；迁移变化同步并重启 app，在启动阶段应用迁移。`pyproject.toml`、`uv.lock` 和 Dockerfile 变化触发镜像重建；前端源码/锁文件/构建配置、常用端口 profile 变化重建 app。`initial_sync` 在监测开始时核对已有容器中的后端源码。

开发构建阶段为非 root 用户提供可写源码目录，开发覆盖配置开放容器根文件系统写入；secret、证据挂载及服务权限沿用基础配置。该模式仅用于本地开发，服务重启会中断正在处理的请求和研究进程。

### 部署模式

```sh
# 状态与日志
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 100 app runner postgres

# 停止；持久数据保留
docker compose -f deploy/compose.yaml down

# 更新与重建
git pull --ff-only
docker compose -f deploy/compose.yaml build app
docker compose -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 150
```

基础部署不启用 Watch，源码变更通过重建生效。数据库主版本和 secrets 不随更新自动轮换；已有数据库密码需通过独立迁移流程变更。

轮换平台访问密钥：`python deploy/rotate_access_key.py`。入口在原文件中更新，不打印密钥；下一请求拒绝旧会话，重新在本机读取新密钥后登录。会话闲置 2 小时、绝对 24 小时到期；变更接口检查 Origin 与 CSRF；登录限速以真实连接来源和全局尝试数持久化，不信任转发头。本机开发使用独立 HttpOnly / SameSite=Strict Cookie；HTTPS 配置使用 Secure / __Host- Cookie。反向代理、TLS 证书与远程生产部署验收仍待交付。

| 持久内容 | 位置 |
| --- | --- |
| Secrets | `runtime/secrets/`，不入 Git |
| 业务数据与 checkpoints | `huntweave_postgres_data` Docker 卷 |
| 原始证据 | `huntweave_evidence` Docker 卷 |

`down` 保留卷，`down -v` 删除卷。换机器获取源码并初始化将建立独立环境；迁移已有环境需要同时保留匹配的 secrets、PostgreSQL 一致性备份与证据归档。完整备份恢复流程尚未验收。

## 验证

各切片的实际覆盖、检查计数、失败边界与未达成项见 [验证记录索引](./docs/validation/README.md)，阶段与能力状态见 [STATUS](./docs/STATUS.md)；本节只给出可复现的入口，不复述验收结论。

回归检查通过按需容器执行：

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify build checks
docker compose -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify run --rm --no-deps checks python -m pytest -p no:cacheprovider tests
```

集成检查会清空其测试数据库中的演示表，并真的启动假执行，只允许在显式的一次性数据库上运行。默认命令跳过这些集成项，仍运行启动集成与纯单元检查。完整 P0 验收使用独立项目；Windows PowerShell 在仓库根目录执行：

```powershell
$env:HUNTWEAVE_WEB_PORT = "18000"
$env:HUNTWEAVE_PUBLIC_ORIGIN = "http://127.0.0.1:18000"
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml build app
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify build checks
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 180
docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify run --rm --no-deps checks python -m pytest -p no:cacheprovider tests

python deploy/verify_startup.py --project huntweave-p0-checks --web-port 18000
python deploy/verify_p0.py --project huntweave-p0-checks --base-url http://127.0.0.1:18000

cd frontend
npm ci
npx playwright install chromium
$env:HUNTWEAVE_E2E_BASE_URL = "http://127.0.0.1:18000"
# verify_p0.py 的核对探针会打印 CONSOLE_RECONCILIATION_RUN=<run_id>，用它跑控制台核对路径：
$env:HUNTWEAVE_E2E_RECONCILE_RUN_ID = "<run_id>"
npm run test:e2e
cd ..

docker compose --project-name huntweave-p0-checks -f deploy/compose.yaml down -v
Remove-Item Env:HUNTWEAVE_WEB_PORT, Env:HUNTWEAVE_PUBLIC_ORIGIN, Env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE, Env:HUNTWEAVE_E2E_BASE_URL
```

`verify_p0.py` 只接受 `huntweave-p0-checks` 项目与回环非 8000 端口，会重启 app/Runner、停止 PostgreSQL、撤销 checkpoint 写权限并临时改名一条证据文件，结束后在 `finally` 中恢复；它不发送任何目标流量。它是唯一能对真实进程与卷注入故障的验证层（验收容器无 Docker 访问），因此不与后端检查重复覆盖：后者在被测进程内验证同一行为，这里验证的是重启、停库、撤权与归档损坏之后它仍然成立。核对探针会额外留下一个结果未知的 Run 并打印其 id，供上面的浏览器核对路径使用；只要检查、不需要该 fixture 时加 `--no-console-fixture`。`down -v` 在此只用于删除自己创建的一次性验收项目。测试用文档保留 IP，不连接目标；浏览器截图保存在被忽略的 `runtime/validation/`。

本地纯检查与前端构建（不含容器）。这些命令必须在 `backend/` 目录内执行：pytest 相对 rootdir 解析 `pythonpath`，在仓库根目录直接运行会因找不到 `huntweave` 包而整批收集失败：

```sh
cd backend
.venv/Scripts/python -m pytest -m "not integration" -q   # Linux 为 .venv/bin/python
.venv/Scripts/ruff check src tests
.venv/Scripts/mypy --config-file pyproject.toml src       # 严格类型（Linux 平台设置）
cd ../frontend && npm run build
```

CI（`.github/workflows/checks.yml`）运行同一组纯检查与前端构建，另有 `uv sync --frozen` 校验依赖锁。集成检查、启动/恢复故障探针和浏览器流程需要 Docker 与一次性栈，仍按上文手工执行。

日常本机开发只保留 `huntweave` 一组服务。浏览器检查默认访问 localhost:8000，追加假项目和 Run，不清空数据库；无需保留验收项目。完整故障验收的独立项目仅临时使用，结束后立即删除其容器/测试卷。

启动故障与隔离探针入口：

```sh
python deploy/verify_startup.py
python -m pip install --require-hashes -r deploy/verification-requirements.txt
python deploy/verify_isolation.py
```

故障探针会短暂停止本项目服务；隔离探针创建独立靶场资源，只有可信管理容器获得 Docker API。结果保存在 `runtime/isolation/`，Windows 当前的探针计数、实际宿主版本与报告 hash 见[隔离验证记录](./docs/validation/0002-windows-isolation.md)；Linux 容器测试不替代原生 Linux 宿主验收。

## 项目资料

状态与规格

- [当前状态](./docs/STATUS.md)：阶段、已交付能力与下一实施项的**唯一状态源**
- [项目总纲](./PROJECT.md) · [P0 规格](./docs/specs/0001-foundation.md) · [P1 规格](./docs/specs/0002-real-execution.md) · [P2 规格](./docs/specs/0003-agent-research.md) · [漏洞库准入规格](./docs/specs/0004-finding-admission.md)

决策记录

- [ADR 索引](./docs/adr/README.md) · [真实执行边界与门槛](./docs/adr/0010-real-execution-boundary-and-gate.md) · [计划与证据版本](./docs/adr/0011-planning-authority-and-evidence-revisions.md) · [最小实证与目标数据](./docs/adr/0012-minimal-proof-and-target-data.md) · [严重性与准入](./docs/adr/0013-severity-and-finding-admission.md) · [执行生命周期与环境身份](./docs/adr/0014-execution-lifecycle-and-environment-identity.md)

研究记录

- [架构评估与设计取舍](./docs/research/2026-10-09-architecture-assessment.md)：问题核对、备选方案比较与取舍依据

验证记录

- [验证记录索引](./docs/validation/README.md)：`0001` 启动 · `0002` Windows 隔离 · `0003` Compose Watch · `0004` 身份与假 Run · `0005` P0-C/D 执行与恢复 · `0006` P1 核对入口 · `0007` P1 票据目标绑定 · `0008` P1 目标上限

开发协作

- [Agent 开发约定](./AGENTS.md) · [领域文档与术语](./docs/agents/domain.md) · [Issue 跟踪](./docs/agents/issue-tracker.md) · [Triage 标签](./docs/agents/triage-labels.md)
- [术语表](./GLOSSARY.md) · [GitHub Issues](https://github.com/kksty/HuntWeave/issues) · [P1 里程碑](https://github.com/kksty/HuntWeave/milestone/1)

阶段交付和未开放能力只以 STATUS 为入口；各项验证记录说明实际覆盖与限制。原生 Linux 宿主验收按 ADR-0010 另立切片，不作为 P1 前置。使用和部署说明保留在本文件，产品规则与正式验收分别见总纲和各阶段规格。

## 许可证

[GPL-3.0-only](./LICENSE)。共享开发技能的来源与 MIT 许可证记录位于 [.agents/sources/](./.agents/sources/)。
