---
name: start
description: >-
  工作流入口。认领或新建 issue、建 worktree、扫 handover 断点、加载上下文。
  当用户说"开始/start/接手/继续"或 Agent 接到新任务时激活。
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

### 0. 存量武器库检视 (Inventory First — 严禁盲目造轮子)

开工前必须先盘点目标仓库的既有资产与工具链，杜绝无意义的重复造轮子和环境污染：

1. **扫描存量工具**：
   ```bash
   ls tools/                           # 查看已有 CLI、探针与门禁工具
   ls libs/                            # 查看领域公共契约与基础库
   ```
2. **存量优先（Inventory First）**：
   - 凡涉及环境巡检、容器探测、部署验证、门禁核验，必须优先检索并复用 `tools/` 与 `libs/` 中已有成熟工具（例如 `infra_probe_runner.py`、`stability_report.py`、`pr_merge_gate.py`、`harness.py` 等）。
   - 严禁在未核实存量的情况下手写功能重复的脚本，或在技能（Skill Markdown）中硬编码无单测、无 CI 保护的裸 Bash 运维命令。
3. **查阅权威真源 (SSOT)**：
   - 阅读 `docs/ssot/MANIFEST.yaml` 与目标模块 SSOT，确认系统已有契约与信号注册表（如 `watchdog-signals.yaml`）。

### 1. 扫 Handover

检查是否有前次会话留下的断点记录：

```bash
# 有跨会话记忆库时：按 "handover issue-<N>" 检索交接断点
# 没有时退化为：gh issue view <N> --json comments
```

- 有 handover → 加载并向用户摘要：做了什么、卡在哪、关键决策、当前状态
- 无 handover → 静默继续

### 2. 扫 Worktree 与活跃进程检查

检查目标 issue 是否已被其他窗口/agent 占据，或存在后台残留进程：

```bash
# 1. 检查是否有对应 issue 前缀的 worktree
# 按完整 token `_issue<N>_` 匹配，不是 `issue<N>` 子串：后者会让 issue12 命中 issue123
git worktree list | grep -F "_issue<N>_"

# 2. 检查是否有针对该 worktree 的后台活跃子进程或马仔
ps aux | grep -F "_issue<N>_" | grep -v grep || true
```

- 已被占据且有活跃进程 → 警告用户，提示切换到该窗口或先 close
- 未被占据 → 继续
- **死绝证明铁律（替身准入防碰撞）**：
  若先前马仔疑似假死（>2 分钟无 Tool Call 且无文件变动），**严禁在未获“死绝证明”前于同一 worktree 强起新替身**！
  - 必须执行 `kill -9 <PID>` 彻底销毁旧进程树，并确认 PID 彻底释放；
  - 若无法物理证明旧进程已死亡，必须新建独立带后缀 worktree（如 `<repo>_issue<N>_<slug>-v2`）进行物理隔离，彻底阻断 100 分钟后僵尸苏醒双写踩踏。

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
5. **挂载调度监控 (Watch List)**：派发后台马仔或执行并发子任务时，自动注册进调度清单，遵守 2 分钟无 Tool Call 且无文件变动判定 STALL 的标准，及时干预。
