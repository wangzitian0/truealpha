---
name: codegraph
description: 在已构建 CodeGraph 索引的仓库中，优先于 grep/find 使用代码图谱检索符号定义、调用路径与依赖拓扑。
ship: repo  # 只讲「优先用代码图谱而非 grep」这条判据与 CLI/MCP 两种通用调用形态，不提任何本环境专有二进制。
---

# CodeGraph — 跨 Agent 代码图谱检索与调用链探测

本 Skill 为当前 Agent（Pi / Codex / Claude Code / Antigravity）提供高精度的代码符号图谱导航能力。在包含 `.codegraph/` 索引目录的代码仓库中，**必须优先于常规文本 grep / find 使用本技能**，以最小 Token 成本一键获取完整的符号实现及其上下游调用路径。

---

## 触发前置条件 (Prerequisite)

执行前必须检查当前仓库根目录是否存在 `.codegraph/` 目录：
- 若存在 `.codegraph/`：**无条件优先使用本技能**；
- 若不存在 `.codegraph/`：跳过 CodeGraph，降级回常规搜索工具（禁止自行触发未授权的全量建库索引）。

---

## 命令与调用方式 (Commands via Bash)

在具备终端 / Bash 执行权限的环境下，直接运行以下 CLI：

```bash
# 1. 语义图谱探索 (推荐)：回答代码问题、查找相关符号的逐字源码及它们之间的调用路径
codegraph explore "<symbol names or question>"

# 示例：
codegraph explore "ServiceRegistry"
codegraph explore "How is the memory distillation pipeline scheduled and executed?"
codegraph explore "ResidentWatcher and its callers"

# 2. 单符号或文件精确查看：返回指定符号的完整源码 + 上游调用者 (callers)
codegraph node <symbol-name-or-file-path>

# 示例：
codegraph node ServiceRegistry
codegraph node libs/service_registry.py
```

---

## 消费与执行准则 (Rules of Engagement)

1. **图谱优先原则 (CodeGraph First)**：遇到“定位某类/函数的定义”、“查找谁调用了某接口”、“梳理模块调用拓扑”等场景，先跑 `codegraph explore`；
2. **免除重复探查**：`codegraph explore` 的输出已经包含了相关符号的逐字实现及调用路径，无需再用 `read_file` 重复读取；
3. **精准溯源**：在回复用户或写计划时，引用 CodeGraph 确认的具体符号与准确行号，避免模糊猜测。
