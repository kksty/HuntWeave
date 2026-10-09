# P0-A 工程启动验证

日期：2026-10-08（Asia/Shanghai）。对应 GitHub Issue #1。仅覆盖三服务启动、访问门槛、迁移与进程健康；宿主出口隔离与目标进程回收由 Issue #2 验证，本记录不证明真实安全测试可运行。

## 环境与固定版本

- Windows 11 Pro，build 26200；源码位于 Windows 工作区（仓库根目录）。
- Docker Desktop 4.94.0，Engine 29.8.2，Compose 5.5.1，Linux containers / WSL2 后端。
- WSL 3.0.1，内核 `6.18.40.1-microsoft-standard-WSL2`；单独的 Kali 发行版未参与构建或执行。
- 容器 Python 3.12.15 / Debian bookworm；本地单元验证 Python 3.12.14。
- PostgreSQL 17.11；FastAPI 0.143.0、SQLAlchemy 2.0.54、Alembic 1.20.0、LangGraph 1.2.14、Postgres checkpointer 3.1.2、psycopg 3.3.6。
- Python 依赖由 `backend/uv.lock` 固定到版本和制品 hash；基础镜像 tag/digest 位于 `deploy/Dockerfile` 和 `deploy/compose.yaml`。

## 已验证入口

原始验证使用当时的 Windows 入口；2026-10-09 将辅助入口替换为共用 Python。当前在仓库根目录执行以下等效命令，Linux 将 python 改为 python3：

```powershell
python deploy/initialize.py
docker compose -f deploy/compose.yaml up -d --build --wait --wait-timeout 150
docker compose -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify build checks
docker compose -f deploy/compose.yaml -f deploy/compose.verify.yaml --profile verify run --rm --no-deps checks
docker run --rm huntweave-checks:p0
python deploy/verify_startup.py
```

`checks` 是验证时按需运行的容器，基础 Compose 仍只有 app/postgres/runner 三个常驻服务。运行验证前应先启动基础栈。

## 结果

| 行为 | 结果与观察 |
| --- | --- |
| 全新卷构建、初始化和启动 | 三服务全部 healthy；仅发布 `127.0.0.1:8000`，数据库与 Runner 无宿主发布端口 |
| 配置缺失 | 容器镜像中移除 access-key 文件配置后，业务、文档与证据路径均返回 503 / `access_key_missing`；最小 liveness 仍可读取 |
| 未建立会话 | 配置密钥后业务路径返回 401；直接发送全局密钥为 Bearer 不授予业务访问 |
| Runner 内部认证 | 无 token、错误 token 和平台密钥均被拒绝，内部专用 token 才能查询能力 |
| 未实现真实执行 | 返回 `real_execution_ready=false`、`environment_unsupported`；当时固定假动作也尚未实现，`fake_execution_ready=false`（该行写于 P0-C 之前；P0-C/D 交付后假执行链路已就绪，`fake_execution_ready=true`，真实执行仍为 `environment_unsupported`，见 [P0-C/D 验证记录](./0005-p0-execution-and-recovery.md)） |
| 重复迁移 | 业务 Alembic 与 checkpoint 迁移连跑两次仍通过就绪检查 |
| 运行账号权限 | app 不能 CREATE TABLE 或读取 checkpoint；checkpoint 不能读取业务 heartbeat，均为 PostgreSQL `42501` |
| 进程故障 | 主动终止 agentd 后 supervisor 检测到必需进程退出，app 退出、Docker 重启，迁移后恢复 healthy |
| Runner 故障 | 停止 Runner 后 app readiness 返回非零 / `readiness_failed`；恢复 Runner 后重新 healthy |
| 文件与秘密边界 | app 证据挂载实际写入返回 EROFS；Runner 为 UID 10001，未挂平台全局密钥、Docker socket 或开发技能 |
| 验证结果 | 5 个 HTTP 边界单元测试在 Windows 和 Linux 容器均通过；5 个真实服务/数据库集成测试通过；Ruff 和严格 mypy 通过 |

业务与 checkpoint 的迁移账号拥有各自 schema，运行账号只有各自数据的操作权限。检查点迁移遵循 [PostgresSaver setup 约定](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/__init__.py)，使用 autocommit、dict_row 和独立 search_path；迁移版本以锁定依赖的实际迁移列表为准。构建按 [uv Docker 流程](https://docs.astral.sh/uv/guides/integration/docker/) 用锁文件安装依赖。

## 限制

本切片没有登录会话、Vue 页面、Run 创建、工具动作派发和研究图，agentd 当前仅维护真实数据库心跳。相关实现对应 #3–#5。隔离 profile 尚未验证，默认 Runner 没有 Docker 管理权限。全部 P0 故障矩阵、UI 路径和部署验收对应 #6。

验证脚本会暂时停止本项目的 Runner 并触发 app 重启。开发栈仅绑定 localhost HTTP；远程部署的 HTTPS/生产会话配置在登录切片中实施。数据库 secret 仅在全新卷初始化时创建角色，已有卷的密码轮换需单独迁移，不能只替换文件。
