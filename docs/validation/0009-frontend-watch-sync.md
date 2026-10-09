# 前端产物同步开发入口验证

日期：2026-10-10。环境：Windows 11、Docker Desktop（Engine 29.8.2、Compose 5.5.1）、宿主 Node v26.9.0、Vite 8.3.4。

本记录验证开发入口的一处修订（对应 [0003](./0003-compose-watch.md) 之后的改动）：前端源码与构建配置不再触发 app 镜像重建，改为宿主构建产物同步进容器。入口仍为 `docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up --build --watch`，但需先在 `frontend/` 产出 `dist`。

## 变更内容

- `deploy/compose.dev.yaml`：app 的前端规则由 `frontend/src`、`index.html`、`package.json`、`package-lock.json`、`tsconfig.json`、`vite.config.ts` 六条 `rebuild` 改为 `../frontend/dist → /opt/huntweave/frontend/dist` 一条 `sync`（带 `initial_sync`）。后端源码、迁移、`pyproject.toml`、`uv.lock`、Dockerfile、`profiles/common-tcp-v1.json` 的规则保持不变。
- `frontend/package.json`：新增 `build:watch`（`vite build --watch`）；`build` 仍为 `vue-tsc --noEmit && vite build`，类型检查与生产构建口径不变。

## 行为与结果

验证在 `HEAD` 的临时 worktree（`D:\code\HuntWeave-watchcheck`，`git worktree add --detach`）中进行：前端依赖通过 junction 复用主工作区 `node_modules`，secrets 由 `deploy/initialize.py` 新生成，`frontend/dist` 先执行一次宿主构建。一次性栈用 `--project-name huntweave-watchcheck`、`HUNTWEAVE_WEB_PORT=18001`、已有 `huntweave-control:dev`（`--no-build`）启动，app/postgres/runner 三者 `healthy`。

| 操作 | 实测结果 |
| --- | --- |
| 宿主 `vite build`（一次） | 进程总耗时 344 ms，其中构建 126 ms |
| 宿主 `vite build --watch` | 启动构建 112 ms；改源码后增量重建 46 ms（另一次 44 ms） |
| watch 附着时的前端 `initial_sync` | 日志 `Syncing service "app" after 4 changes were detected` |
| watch 附着时的后端同步 | 日志 `Syncing service "app" after 15 changes were detected`、`Syncing service "runner" after 9 changes were detected`，随后 `service(s) ["app" "runner"] restarted` |
| 受控前端改动（`src/main.ts` 探针 `9f3a` → `11c7`） | 宿主产物 `assets/index-DRIP9zFF.js` → `assets/index-UOioyMhZ.js`；容器 `index.html` 引用同步为 `assets/index-UOioyMhZ.js`，容器内该 JS 命中探针字符串 |
| 同一次前端改动期间的服务状态 | app `StartedAt` 保持 `2026-10-09T16:39:47.641264131Z`、runner 保持 `2026-10-09T16:39:47.093842118Z`，服务镜像 ID 保持 `sha256:e4a5e9a069f715182f6e59d54d93190fdfb9ff7b294ce15ad99d0b1508a23dea`，`RestartCount=0` |
| `frontend/dist` 缺失时启动 watch | `GetFileAttributesEx D:\...\frontend\dist: The system cannot find the file specified.`，命令以 exit 1 结束；容器继续提供上一次同步的产物 |
| 前端构建失败（探针写入破坏了 `App.vue` 编码） | watch 报告 `Could not load src/App.vue ... stream did not contain valid UTF-8`，容器保持上一次成功产物，未重启 |

`docker images` 在该轮验证前后一致：`huntweave-control:dev` 为 `e4a5e9a069f7`（未被重建），`huntweave-control:p0` 为 `9001daac9b9c`。验证结束后一次性项目 `down -v`、worktree `git worktree remove --force` 删除，未残留容器、卷或网络，主工作区的 `huntweave` 项目与服务镜像未被改动。

## 未达成与限制

- 验证用已有 `huntweave-control:dev` 启动（`--no-build`），因此「前端依赖变化后镜像内 `npm ci` / `vite build`」这一段未在本轮复测；生产镜像仍由 `deploy/Dockerfile` 的 `frontend` 阶段构建，与本入口无关。
- 同步不删除容器内旧文件：容器 `assets/` 同时保留镜像自带的 `index-x4sux1NE.js` 与本轮同步进来的新产物，`index.html` 只引用当前文件，无功能影响；容器 recreate 后回到镜像内容。
- 类型检查不在 watch 路径内，`vue-tsc --noEmit` 仍只在 `npm run build` 与 CI 执行。
- 验证在独立 worktree 完成，未在主工作区复测（当时该工作区正由另一会话改动源码）。
- 本轮未验证 Linux 原生宿主；与 0003 相同，结论只在上述 Windows 环境实测。

## 本记录的更正

2026-10-10（本记录提交后）。「行为与结果」表的「watch 附着时的前端 `initial_sync`」一行不能作为**附着时初始同步**的证据：该行引用的日志（`Syncing service "app" after 4 changes were detected`）在本次复核中只在**改动被检出**时出现，附着时没有对应输出。在独立一次性项目（`huntweave-watchsync`、Web 端口 18001、`deploy/compose.dev.yaml` 覆盖、镜内容与宿主 `frontend/dist` 不同）上做了三次受控复核：

| 复核 | 结果 |
| --- | --- |
| 附着前在宿主 `frontend/dist` 写入 `watch-probe.txt`，再 `up --build --watch` | 容器内该文件**不存在**；随后改写该文件 → 同步进容器，app `StartedAt`、镜像 ID、`RestartCount=0` 均未变 |
| 附着期间在宿主新建 `new-file-probe.txt` | 同步进容器（新增文件同样按改动同步） |
| 停止 watch，在 `frontend/dist` 写入 `initial-probe.txt` 后重新附着 | 约 150 秒轮询内仍未同步；同一轮在 `backend/src` 写入 `zz_watch_probe.txt`（同为 `initial_sync: true` 的 `sync+restart` 规则）同样未同步、app 未重启 |
| watch 未附着时移走 `frontend/dist` 再 `up --watch` | 以 `GetFileAttributesEx D:\code\HuntWeave\frontend\dist: The system cannot find the file specified.` 退出，exit 1（该项结论不变） |

因此：本记录「前端改动同步不重启服务、不重建镜像」「`dist` 缺失时 `up --watch` 直接失败」「前端构建失败时容器保留上一次成功产物」三项结论仍然成立；**「附着时把启动前已存在的后端源码与前端产物同步进容器」不成立**——容器在附着后仍提供镜像内的构建产物，直到第一次改动被同步。README 的开发模式说明已按实测改正（含「附着后再保存一次前端文件」的操作提示），`deploy/compose.dev.yaml` 与 `frontend/package.json` 的改动本身不受影响。

