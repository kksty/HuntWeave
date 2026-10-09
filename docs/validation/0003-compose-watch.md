# Compose Watch 开发入口验证

日期：2026-10-09。环境：Windows 11、Docker Desktop 4.94.0、Compose 5.5.1。Linux 共用配置，原生宿主尚未验证。

入口：`docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up --build --watch`。

| 修改 | 实际结果 |
| --- | --- |
| backend/src 中加入临时注释 | Watch 检测并同步 app/runner，两个服务重启；容器内读取到新内容，健康恢复 |
| pyproject.toml 中加入临时注释 | 自动重建两服务镜像，容器 ID 变化，健康恢复 |
| 迁移文件中加入临时注释 | 仅 app 重启，app StartedAt 变化，runner StartedAt 保持不变 |
| 恢复基础配置 | app/runner 的 ReadonlyRootfs=true，三服务 healthy |

同步目录实际由 UID/GID 10001:10001 持有。Compose 配置确认源码/迁移使用 sync+restart 和 initial_sync，依赖文件及 Dockerfile 使用 rebuild；仅监测指定路径，不包含 secrets、runtime、证据或开发技能。基础 Compose 不定义 Watch。

手动 Watch 重启通过日志和 StartedAt 验证；Docker RestartCount 不统计该类手动重启，不能作为本项断言。所有临时注释已恢复，验证用 Watch 进程已退出。开发模式不是进程内无中断热替换，服务重启会中断请求和运行进程；数据库与证据卷保留。

> 2026-10-10 起前端路径改为产物同步（宿主 `npm run build:watch` → `frontend/dist` 同步进容器，不再触发镜像重建或重启）；该修订的实测结果见 [0009](./0009-frontend-watch-sync.md)，上表仍为 2026-10-09 对后端源码、迁移、依赖锁与基础配置的实测结果。
