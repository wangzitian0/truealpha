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
# 按完整 token `_issue<N>_` 匹配，不是 `issue<N>` 子串：后者会让 issue12 命中
# issue123，把别人占着的问题读成自己的。worktree 命名规范本来就是这个 token
# （见下方 `<repo>_issue<N>_<slug>`），对齐它即可。-F 关掉正则，免得 issue 号
# 周围的字符被当成元字符。
git worktree list | grep -F "_issue<N>_"
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

**`<path>` 不是随便填的。** 仓库自己的规则随 git 检出，放哪都在；但**跨仓库那几层规则
不入库**（它们含只属于本机的名字与路径，而仓库可能是公开的），只能靠**目录继承**或
**该 checkout 上的生成文件**送达。两者都跟位置有关：

- 建在跨仓库规则目录**之下** → 靠继承拿到，不用额外做什么；
- 建在别处（临时目录、宿主自己的 worktree 目录） → **两条通道都不成立**，
  这个 worktree 里的会话全程没有跨仓库规则，**而且没有任何提示**。

**开工前先确认它到了，别事后才发现。** 判据不需要猜：问当前会话「已加载的指令文本里
有没有出现过某条只存在于跨仓库规则里的字符串」——有就是到了，没有就是没到。
没到就别在这个 worktree 里干活，先把位置换对或让生成文件到位。

规则没到 ≠ 规则不适用。少掉的往往正是攻坚纪律、红线与协作分层——**最不该靠临场发挥的那部分**。

**位置具体该放哪、建错了怎么补，属于实现，由本环境的 `local.md` 提供**——它随环境变化；判据不会。没有 `local.md` 的环境按自己那套规则分发方式替换即可。

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
