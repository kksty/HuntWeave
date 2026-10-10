# 0027：暂停入口、追平事件游标与旧快照回归

日期：2026-10-11（Asia/Shanghai）。关联：[#79](https://github.com/kksty/HuntWeave/issues/79)。遵循 ADR-0018；仅使用一次性 `huntweave-p0-checks` 项目、回环端口 18000、文档保留 IP 与假执行，不连接实际目标。

## 诊断与决定

原始 `authorized-run.spec.ts` 暂停流程未改时复现：5.9 秒后找不到“暂停 Run”。失败页面父面板为 queued，但透明控制台连接中断、无流心跳、保留旧快照。数据库最后进入 awaiting_human 不能排除中间的 UI/SSE 缺陷；actionTimeout 是单次点击等待时间，不能用自动阶段总耗时直接推断点击期间的终态。

最小真实 API 探针先成功启动并暂停假 Run，再从已存在的最后事件继续读取。两轮分别在 `last_cursor == committed == 10` 和 `3` 返回 409 / event_cursor_ahead。根因是 `_contiguous_published` 用后继事件是否存在推断请求游标存在：追平时没有后继，会误拒绝；请求游标自身缺失但后继存在，又会误放行。

本票判为产品缺陷，且原浏览器流程具有调度时序敏感性。修复实际游标判定与界面的版本选择，补受控响应交付回归，不增加演示时长。#43/#21 是否造成自动阶段提速未证明，不能据此归因；容量与速度测量继续归 #32。

## 修改

- `page_events` 独立查验请求游标自身；已有末端允许空页等待，新事件提交后沿同一游标接续。保留过期游标、真正越界、缺失游标及中途断点的拒绝。
- 控制台从父输入和详情快照选择版本较新的 Run；旧快照不覆盖新状态。状态、阶段和控制请求使用同一版本来源，请求发出时固定 seenVersion，409 仍要求操作员显式确认。
- 新增空时间线/已追平 HTTP 检查、缺失游标已有后继的负例、真实 PostgreSQL 末端等待再接续检查。
- 原 SSE 检查依赖错误断流才结束。改为让流真实经过空页轮询后撤销隔离测试会话，验证没有 resync_required、且最终因会话撤销正常结束。
- 新增浏览器回归：扣住真实 draft 快照，在 start 返回后才交付；暂停首次请求发出前继续扣住后续快照，确保旧响应不能靠自动刷新被悄悄修正。真实暂停与恢复预览可达，失败时释放全部响应闸门。
- checks 镜像原先缺少源码契约检查读取的 PROJECT、docs 和 frontend/src。仅在 checks target 补齐这些输入，control 镜像检查确认没有 docs 与 frontend/src。

## 验证

环境：Windows 工作区、Docker Desktop Linux 容器、PostgreSQL 17、检查镜像 Python 3.12、Playwright 1.64。

| 检查 | 实际结果 |
| --- | --- |
| 新增 HTTP 回归，修复前 | 2 failed / 1 passed：末端误拒绝、缺失游标误放行均变红 |
| 本地事件文件，修复后 | 34 passed |
| 本地非集成全量 | 397 passed / 15 skipped / 93 deselected，37.81s |
| ruff / mypy | 通过；58 个源文件类型检查通过 |
| 前端类型与构建 | npm run build 通过 |
| 容器首次全量（不含 startup） | 9 failed / 489 passed / 2 skipped，失败全为缺少文档或 frontend/src；修复检查镜像材料后复跑 |
| 容器全量复跑，含 PostgreSQL 集成 | 500 passed，81.05s；app 停止以避免后台调度器争抢夹具 |
| 原始暂停流程 + 受控旧快照回归 | 2 passed，11.9s |
| 完整浏览器套件 | 16 passed / 1 skipped，31.1s；缺少核对探针 Run 的用例按既有条件跳过 |
| 独立真实暂停 API / 末端探针 | 1 passed，1.2s；paused 且 last_cursor=committed=3 时 HTTP 200 |
| control 镜像材料隔离 | docs、frontend/src 均未进入 control 镜像 |

入口：README 的一次性栈与 checks 命令；浏览器定点命令 `npx playwright test authorized-run.spec.ts --grep 'pause, reload, resume preview|delayed draft snapshot'`，完整命令 `npm run test:e2e:full`，本地诊断材料在 `runtime/validation/issue79-probe/`。

## 限制与后续归属

- 不声称定位了自动阶段变快的机制，也不把模型事务移出或 claim 调度变化当作已证原因。
- #78 保持开放：409 恢复响应、快照身份、字段生产者、清理竞态与服务事实结构性项未在本票处理；快照/增量契约与 #65 协调。0026 验收 3、5 继续为部分，不因本票转为通过。
- 本轮未运行 `test_startup_integration.py`、启动故障探针、沙箱生命周期/出口/取消靶场探针；相关执行代码没有修改。浏览器跳过项不是通过项。
- 一次性容器与测试卷在验证后回收；常驻 :8000 部署没有重启或修改数据。
