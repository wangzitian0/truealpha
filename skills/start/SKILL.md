---
name: start
description: >-
  工作流入口。认领或新建 issue、建 worktree、扫 handover 断点、加载上下文。
  当用户说"开始/start/接手/继续"或 Agent 接到新任务时激活。
ship: repo
---

# Start — 工作流入口

与 `close` skill 对称：start 负责进入，close 负责退出或挂起。

---

## 触发时机 (When to Use)

1. 用户主动说：`/start`、"开始"、"接手"、"继续上次的"；
2. Agent 接到一个需要写代码的新任务（非纯问答）；
3. 用户指定了一个 issue 编号要求接手。

---

## 流程（按序执行）

### 1. 扫 Handover

检查是否有前次会话留下的断点记录：

```bash
# 有跨会话记忆库时：按 "handover issue-<N>" 检索交接断点
# 没有时退化为：gh issue view <N> --json comments
```

- 有 handover → 加载并向用户摘要：做了什么、卡在哪、关键决策、当前状态
- 无 handover → 静默继续

### 2. 扫 Worktree

检查目标 issue 是否已被其他窗口/agent 占据：

```bash
# 列出所有 worktree，查看是否已有 issue 前缀的 worktree
git worktree list | grep "issue<N>"
# 或直接 ls worktree 目录
ls -d ~/.gemini/antigravity/worktrees/*issue<N>* 2>/dev/null
```

- 已被占据 → 警告用户，提示切换到该窗口或先 close
- 未被占据 → 继续

### 3. 认领 Issue

```bash
# 确认 issue 存在，或新建
gh issue view <N> --json title,state,labels 2>/dev/null || echo "Issue not found"
```

- issue 不存在且任务需要 → 建议新建 issue
- issue 已存在 → 确认 scope 并认领

### 4. 建 Worktree

```bash
# 基于最新 main 创建带 issue 前缀的 worktree
git worktree add <path>/<repo>_issue<N>_<slug> origin/main
```

- 已有 worktree 且是自己的 → 复用，不重建
- 如果当前已在正确 worktree 中 → 跳过

### 5. 输出状态摘要

向用户报告：

```markdown
## 🚀 Start Summary

| 项目 | 状态 |
|---|---|
| Issue | #N: <title> |
| Worktree | <path> |
| Handover | 有/无 (摘要) |
| 已有改动 | `git status -s` |
| 关联 PR | `gh pr list --head <branch>` |
```

---

## 执行准则

1. **不阻塞**：任何一步失败都不拒绝继续工作，只报告缺失项。
2. **幂等**：重复执行 /start 同一 issue 不会创建重复 worktree。
3. **尊重占位**：看到其他 agent 的 worktree 就是看到了 issue 锁，不抢。
4. **先查后做**：recall skill 的原则同样适用——检索结果为空时静默继续。
