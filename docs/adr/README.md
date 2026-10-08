# 架构决策索引

当前实现依据为 [PROJECT.md 0.7](../../PROJECT.md)。ADR 解释取舍与历史，不要求将不同版本的结论同时实现。下一步开发见 [P0 规格](../specs/0001-foundation.md)。

| ADR | 状态 | 仍然有效的部分 | 已被替代的部分 |
| --- | --- | --- | --- |
| [0001 项目名称](./0001-project-name.md) | 有效 | HuntWeave / 界巡 / huntweave；名称初查有时间和范围限制 | 无 |
| [0002 自主执行与访问](./0002-autonomous-runtime-and-access.md) | 部分历史 | 自主联网获取工具、通用工具优先、全局密钥入口 | 默认 root；按 0004/0005 和当前总纲执行 |
| [0003 单机技术栈与工具](./0003-single-host-stack-and-tool-design.md) | 部分历史 | 单机优先；协议适配器可选；研究方法由 Agent 选择 | SQLite、两个常驻容器、默认 root |
| [0004 工具生命周期与运行底座](./0004-tool-lifecycle-runtime-and-observability.md) | 部分历史 | PostgreSQL、LangGraph、普通用户/有限提权、透明执行与恢复 | 严格单容器、统一 Debian、动态环境普遍长期保存 |
| [0005 Compose、Kali 与选择性保留](./0005-compose-kali-tool-retention-and-development.md) | 有效 | 三常驻服务、Kali 工具环境、临时/缓存/选择性工具库、下游自行构建 | 无 |
| [0006 Windows Docker 开发入口](./0006-windows-docker-development.md) | 有效 | Windows 工作区、Docker Desktop WSL2 后端；Kali WSL 非依赖；隔离 profile 独立验收 | 总纲早期的原生 Linux 优先路线 |

0.7 是当前选型的实施契约整理：六个代码 Module、状态归属、事件顺序、执行收尾与 P0 验收写入总纲，不新增一套部署架构。后续若变更核心选型，新增 ADR 并同步此索引和总纲；普通实现细节与验证结果放对应功能规格。
