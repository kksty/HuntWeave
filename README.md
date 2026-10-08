# HuntWeave（界巡）

面向自有或明确授权目标的 Agent 安全测试平台。LLM 负责提出假设、选择工具、分析结果和决定下一步，程序负责执行范围、预算、隔离与证据记录，最终结果经过人工复审。

**当前阶段：P0-A 三服务工程启动已实现；完整 P0 闭环仍在开发。**

- 全局开发依据：[项目总纲](./PROJECT.md)
- 开发 Agent 约定：[AGENTS.md](./AGENTS.md)
- 决策与历史替代关系：[ADR 索引](./docs/adr/README.md)
- 下一轮开发入口：[P0 工程与运行契约规格](./docs/specs/0001-foundation.md)

## 已确定的架构

- 工作流：`Collector → Worker × N → Reviewer → 人工复审`
- 技术栈：Python、FastAPI、Vue 3 / TypeScript、PostgreSQL、SQLAlchemy / Alembic、LangGraph OSS、Docker Compose。
- 部署：一个 Compose 入口；app、postgres、runner 三个常驻服务，Kali 工具/准备容器按需创建；控制服务使用 Python/Debian slim，工具镜像单独维护。
- 首版输入：手动粘贴 IP；不包含 SRC 归属和 FOFA 等资产测绘集成。
- Agent 能力：普通用户 Shell，自主联网搜索 CVE/PoC/EXP、安装用户态工具、编写脚本并验证；特权动作用有限票据申请，不开放任意 sudo。
- 协议研究：通用客户端/Shell 即可开展，专用适配器是可选增强；HTTP/HTTPS 与 Redis 用作首批验收样例。
- 未知方法：Agent 根据访问条件自主搜索、理解、编写/安装工具并尝试；MSSQL 等实验样例验证未预编排方法的研究能力，不把协议检查表当作上限。
- 工具来源：核心预装、任务临时环境、限额短期缓存、选择性工具库；高频/成本高/明确固定的能力才保留，一次性 EXP 不永久保存大环境。
- 更新：Run 固定版本；下游可自行构建 toolpack，系统依赖在隔离准备环境处理，无需作者逐次发版。
- 全过程可见：玻璃鱼缸执行台展示 Agent 树、依据摘要、来源、命令/输出、版本、提权、预算、拒绝、中断与恢复；SSE 断线补拉。
- Web 访问：已实现缺密钥时拒绝业务访问的服务端门槛；登录会话、业务 API 与页面按 P0 后续切片实施。

服务与工具隔离仍需实测；默认普通用户，准备任务的按需 root 不获得平台秘密或 Docker socket。模型拒绝、权限阻断与环境缺失分别展示，不假报成功。

下一步按 P0 规格实现“登录 → 手动 IP 与授权 → 假 Run → 实时事件 → 暂停/取消/重启恢复”，同时在隔离靶场验证执行网络与进程回收。P1 接入真实工具，P2 完成 Agent MVP。

## Windows 开发启动

前提：PowerShell 7、Docker Desktop 使用 WSL2 后端和 Linux containers。源码直接在 Windows 工作区开发；单独的 Kali WSL 发行版不是部署依赖。

在仓库根目录执行：

```powershell
./deploy/Initialize-Development.ps1
docker compose -f deploy/compose.yaml up -d --build --wait --wait-timeout 150
Invoke-RestMethod http://127.0.0.1:8000/health/live
```

初始化工具生成 32 字节随机 secrets 并保存到被 Git 忽略的 `runtime/secrets/`，不打印秘密值，也不覆盖已有秘密。PostgreSQL 业务迁移与 LangGraph checkpoint 迁移在 API/agentd 启动前完成。Runner 内部 token 与平台访问密钥分别生成。

当前仅提供最小存活接口；主页及业务路径会返回 401，登录 UI 在后续切片实现。缺失或空访问密钥时业务路径返回 503 / `access_key_missing`。Runner 明确报告真实执行未就绪。

验证命令、测试结果和限制见 [P0-A 启动验证记录](./docs/validation/0001-startup.md)。停止开发栈使用 `docker compose -f deploy/compose.yaml down`；持久卷保留。

## 许可证

本项目采用 [GNU General Public License v3.0](./LICENSE) 授权（GPL-3.0-only）。
