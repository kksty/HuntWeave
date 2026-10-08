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

## Triage

PR 不作为需求分流入口。`triage` 使用 `docs/agents/triage-labels.md` 中的标签处理 Issue。

## Wayfinder

`wayfinder` 使用一个带 `wayfinder:map` 标签的父 Issue 保存地图，并把决策任务建为子 Issue。子任务使用 `wayfinder:<type>` 标签，其中 type 为 `research`、`prototype`、`grilling` 或 `task`。
