# Issue tracker：GitHub

本仓库使用 `kksty/HuntWeave` 的 GitHub Issues 跟踪规格与实施任务，所有操作使用 `gh` CLI。

## 基本操作

- 创建：`gh issue create --title "..." --body-file <file>`
- 读取：`gh issue view <number> --json number,title,body,labels,comments`
- 评论：`gh issue comment <number> --body-file <file>`
- 标签：`gh issue edit <number> --add-label "..."`
- 关闭：`gh issue close <number> --comment "..."`

多行正文使用临时文件和 `--body-file`，避免 Shell 转义改变内容。仓库由当前 Git remote 推断。

## 任务关系

- 一个实施切片对应一个 Issue。
- 子任务优先使用 GitHub sub-issue；不可用时在正文顶部写 `Part of #<parent>`。
- 阻塞关系优先使用 GitHub 原生 issue dependencies；不可用时在正文写 `Blocked by: #<number>`。
- 可执行前沿是所有阻塞项已关闭、尚未被领取的开放 Issue。
- 领取任务时将 Issue 指派给当前执行者；完成后留下验证结果并关闭。

## 阶段与里程碑

- 阶段进度用 **milestone** 表达（当前为 `P1：真实执行接入、透明控制台与选择性保留`），成员是阶段内的前置修复与 tracer 切片；满足规格的退出条件时关闭里程碑。
- 里程碑只携带标题、成员与计数，**不写状态叙述**：阶段、能力与下一实施项仍只在 `docs/STATUS.md` 维护，两者不互为替代。
- 规格（epic）类 Issue 的交付物是规格与边界本身，可以在其实施切片仍开放时关闭；此时必须把正文里的 epic 级门槛（如「退出条件」）显式移交到里程碑与 `docs/STATUS.md`，并在关闭评论里说明移交位置。
- 已完成的阶段工作若仍需体现在进度里，可作为已关闭成员加入里程碑；否则里程碑只挂剩余工作，进度显示为 `0/N`。

## 结案

- 关闭前在 Issue 上留下**验证依据**：对照验收标准的逐条结论、实现提交、验证记录路径与已知限制；正文与既有评论不改写。
- 记不清的取舍不要写成已确认：「待人工确认」的条目随 Issue 关闭时注明后续归属（由哪个切片复核或修正），保留原表述。
- 关闭 Issue 不等于阶段交付：阶段、能力与下一实施项只在 `docs/STATUS.md` 更新，Issue 关闭后同步该文件。
- 代码提交先推送到 `origin/main` 再关闭引用它的 Issue，避免关闭说明指向本地提交。

## Triage

PR 不作为需求分流入口。`triage` 使用 `docs/agents/triage-labels.md` 中的标签处理 Issue。

## Wayfinder

`wayfinder` 使用一个带 `wayfinder:map` 标签的父 Issue 保存地图，并把决策任务建为子 Issue。子任务使用 `wayfinder:<type>` 标签，其中 type 为 `research`、`prototype`、`grilling` 或 `task`。
