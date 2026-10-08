# HuntWeave（界巡）

面向自有或明确授权目标的 Agent 安全测试平台。LLM 负责提出假设、选择工具、分析结果和决定下一步，程序负责执行范围、预算、隔离与证据记录，最终结果经过人工复审。

**当前阶段：0.7 开发准备基线已整理，应用尚未实现。**

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
- Web 访问：全局密钥登录，服务端统一保护主页、业务 API、事件流与证据；这些功能目前为待实现设计。

服务与工具隔离仍需实测；默认普通用户，准备任务的按需 root 不获得平台秘密或 Docker socket。模型拒绝、权限阻断与环境缺失分别展示，不假报成功。

下一步按 P0 规格实现“登录 → 手动 IP 与授权 → 假 Run → 实时事件 → 暂停/取消/重启恢复”，同时在隔离靶场验证执行网络与进程回收。P1 接入真实工具，P2 完成 Agent MVP。运行命令将在应用实现并验证后补充。

## 许可证

本项目采用 [GNU General Public License v3.0](./LICENSE) 授权（GPL-3.0-only）。
