# HuntWeave（界巡）

面向自有或明确授权目标的 Agent 安全测试平台。LLM 负责研究决策，程序约束目标范围、权限、预算和执行，原始证据经过独立复审与人工确认。

**当前为 P0 开发预览：已实现三服务启动、访问门槛、数据库迁移和进程健康监控。登录页面、Run、Agent 研究循环及工具执行尚未实现，部署后不能发起安全测试。**

## 部署环境

当前部署基线为 **Windows 11 x86_64 + Docker Desktop WSL2 后端 + Linux containers**。源码直接放在 Windows 目录中，例如 `D:\apps\HuntWeave`；任何新 Windows 机器都按下文独立部署。Linux/macOS 和远程生产部署尚未纳入本轮验收。

Docker Desktop 使用自己的 `docker-desktop` 环境运行 Linux 容器。无需安装 Kali WSL，也无需启用某个用户发行版的 WSL Integration；后续产品使用的 Kali 工具镜像由项目单独构建。

准备以下软件：

- [Git for Windows](https://git-scm.com/downloads/win)。
- [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)，以下命令在 PowerShell 7 中执行。
- [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)，选择 WSL2 后端和 Linux containers，并启动 Docker Desktop。

未安装 WSL 时，在管理员 PowerShell 中按 [Microsoft WSL 安装说明](https://learn.microsoft.com/windows/wsl/install)启用 WSL2；需要重启时先重启，再启动 Docker Desktop。Docker 的 WSL2 后端设置见[官方说明](https://docs.docker.com/desktop/features/wsl/)。

确认 Docker 已运行：

```powershell
docker version
docker compose version
docker info --format '{{.OSType}}'
```

`docker version` 应同时显示 Client 和 Server，最后一条命令应输出 `linux`。部署使用容器内的 Python 和 PostgreSQL，无需在 Windows 安装 Python、Node.js 或数据库。

已验证环境：Docker Desktop 4.94.0、Engine 29.8.2、Compose 5.5.1、WSL 3.0.1。详见[启动验证记录](./docs/validation/0001-startup.md)。这些是验证记录，不表示任意其他版本的隔离能力已经通过验收。

## 首次部署

### 1. 获取源码

当前可运行代码位于 [PR #7](https://github.com/kksty/HuntWeave/pull/7) 的 `codex/p0-startup` 分支，尚未合并到 `main`。暂时按以下命令获取；合并后可改为获取 `main`。

```powershell
New-Item -ItemType Directory -Force D:\apps | Out-Null
Set-Location D:\apps
git clone --branch codex/p0-startup --single-branch https://github.com/kksty/HuntWeave.git
Set-Location HuntWeave
```

`D:\apps` 可替换为本机目录。已有该分支的工作区直接进入仓库根目录，从下一步开始。

### 2. 初始化与启动

```powershell
./deploy/Initialize-Development.ps1
docker compose -f deploy/compose.yaml up -d --build --wait --wait-timeout 150
```

初始化脚本适用于当前部署，也可用于准备新的工作区。它分别生成平台访问密钥、Runner 内部 token 和数据库密码，存入 `runtime/secrets/`；每个值来自 32 字节密码学随机数，不打印秘密值、不覆盖已有文件。不同机器的新部署应分别初始化。

首次启动会联网拉取固定 digest 的基础镜像、按 `backend/uv.lock` 构建应用镜像，并创建数据库与证据卷。PostgreSQL 初始化账号后，app 先完成业务 Alembic 和独立 LangGraph checkpoint 迁移，再启动 API 与 agentd。Runner 使用专用 token，未挂载平台访问密钥或 Docker socket。

### 3. 确认启动结果

```powershell
docker compose -f deploy/compose.yaml ps
Invoke-RestMethod http://127.0.0.1:8000/health/live
```

三个服务应为 `healthy`，存活接口返回 `status: alive`。当前访问主页、业务路径或 API 文档返回 401 是预期行为：登录功能仍在实施。缺失或空访问密钥时，业务路径返回 503 / `access_key_missing`，app 不满足就绪条件。

默认只发布本机 `127.0.0.1:8000`；数据库和 Runner 不向宿主发布端口。部署到另一台 Windows 机器后，应在那台机器上访问 localhost，当前配置不提供跨机器 Web 访问或 HTTPS 生产入口。

## 启停与更新

以下命令均在仓库根目录执行。

```powershell
# 启动已有部署
docker compose -f deploy/compose.yaml up -d --wait --wait-timeout 150

# 查看服务状态与最近日志
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 100 app runner postgres

# 停止服务并保留容器与数据
docker compose -f deploy/compose.yaml stop

# 移除本项目容器和网络，持久卷仍保留
docker compose -f deploy/compose.yaml down
```

Docker Desktop 需先处于运行状态。停止后，再运行 `up` 即可启动；迁移会核对已有版本并可重复执行。

更新同一分支的代码并重新构建：

```powershell
git pull --ff-only
docker compose -f deploy/compose.yaml up -d --build --wait --wait-timeout 150
```

源码没有实时挂载到容器，修改后也使用上述构建命令生效。更新不会自动轮换 secrets 或升级固定的数据库主版本。已有卷的数据库密码在首次初始化时确定，不能只替换密码文件来完成轮换。

需要调整 Web 端口时，在仓库根目录创建被 Git 忽略的 `.env`，例如：

```dotenv
HUNTWEAVE_WEB_PORT=8080
```

此后命令显式指定该配置文件：

```powershell
docker compose --env-file .env -f deploy/compose.yaml up -d --build --wait --wait-timeout 150
Invoke-RestMethod http://127.0.0.1:8080/health/live
```

其余 Compose 命令也加上 `--env-file .env`。项目默认名称为 `huntweave`；同一台机器换一个源码目录不会自动得到独立数据库或第二套部署。

## 数据与换机器

| 内容 | 保存位置 | 随 Git 获取源码 |
| --- | --- | --- |
| 源码、规格、共享开发技能、依赖锁与构建配方 | 仓库 | 是 |
| 平台密钥、Runner token、数据库密码 | `runtime/secrets/` | 否 |
| 业务数据和 LangGraph checkpoints | Docker 卷 `huntweave_postgres_data` | 否 |
| 原始证据归档 | Docker 卷 `huntweave_evidence` | 否 |

仓库可以放在 D 盘，Docker 镜像和数据卷的磁盘位置则由 Docker Desktop 管理；改变仓库目录不会移动这些数据。普通 `down` 保留卷，`down -v` 会删除本项目持久卷和其中的数据。

在新机器获取源码并初始化，会得到一套新的部署，不会自动带上旧机器的数据库、密钥或证据。如果需要迁移已有数据，应同时保留匹配的 secrets、PostgreSQL 一致性备份和证据归档；不能用新生成的数据库密码接管旧卷，也不能把复制运行中的 PGDATA 当作完整备份。完整备份恢复与跨机器迁移验收仍待后续实现，当前不提供已经验证过的一键迁移命令。

## 常见启动问题

| 现象 | 处理 |
| --- | --- |
| 找不到 `docker`，或没有 Server 信息 | 确认 Docker Desktop 已安装并运行；重新打开 PowerShell，再检查 Linux containers / WSL2 后端 |
| 首次镜像或依赖下载失败 | 确认本机和 Docker 构建过程可访问 Docker Hub、GHCR 和 PyPI；修复网络后重新运行构建命令 |
| 提示 secret 文件缺失 | 全新部署先执行初始化脚本；已有数据库的部署需恢复原来匹配的 secrets |
| app 为 `unhealthy` | 查看 app/postgres/runner 日志；启动前迁移失败、数据库或 Runner 不可用、agentd 退出、缺密钥都会影响就绪状态 |
| 8000 端口已被占用 | 按上文通过 `.env` 调整端口，并用新端口检查存活接口 |
| 主页返回 401 | 当前登录 UI 尚未实现，基础部署状态通过 `ps` 和 `/health/live` 检查 |
| 执行能力显示 `environment_unsupported` | 宿主真实隔离 profile 尚未通过验收，当前工程栈只报告能力状态 |

## 项目资料与进度

整体流程为 `Collector → Worker × N → Reviewer → 人工复审`。技术栈为 Python、FastAPI、Vue 3 / TypeScript、PostgreSQL、SQLAlchemy / Alembic、LangGraph OSS 和 Docker Compose。产品目标从手动粘贴 IP 开始，在授权范围内自主选择工具和研究方法，并形成可追溯证据。

- [项目总纲](./PROJECT.md)：产品与架构基线。
- [开发 Agent 约定](./AGENTS.md)、[ADR 索引](./docs/adr/README.md)、[P0 规格](./docs/specs/0001-foundation.md)：工程约束与验收。
- [GitHub Issues](https://github.com/kksty/HuntWeave/issues)：实现任务与进度。
- [P0-A 启动验证](./docs/validation/0001-startup.md)：已验证命令、结果及当前限制。

P0 剩余工作为宿主隔离探针、登录与授权 Run、确定性 Agent/假动作、时间线、暂停取消和恢复。P1 接入真实工具执行，P2 完成完整 Agent MVP；后续功能在实现并验证后更新本 README 的部署和使用说明。

## 许可证

本项目采用 [GNU General Public License v3.0](./LICENSE) 授权（GPL-3.0-only）。共享开发技能的来源与适用 MIT 许可证保留在 [.agents/sources/](./.agents/sources/)。
