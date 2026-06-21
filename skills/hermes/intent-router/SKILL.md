---
name: intent-router
description: "意图路由器 — 用户输入进来后先分类、消歧、映射到正确框架，再执行操作"
author: Hauzer S. Lee
license: MIT
category: hermes
platforms:
  - linux
  - macos
version: 2.0.0-standalone
metadata:
  hermes:
    relations:
      - type: complemented_by
        target: skill-graph
        properties:
          reason: 意图路由后通过图谱精准匹配技能，二者配合能覆盖更多场景
          strength: medium
    tags:
      - hermes
      - 意图路由器
      - intent-router
      - 用户输入进来后先分类
      - 映射到正确框架
---

# 意图路由器 (Intent Router)

## 职责边界

intent-router 只回答「执行什么」，不回答「怎么执行」：

```
用户输入 → Phase 0 → Phase 1-3 分类 → Phase 4 路由 → 执行 → quality-gate
```

## 🔴 铁律 — intent-router 永远第一个加载

**收到任何用户输入后，必须先加载 intent-router 走完 Phase 0-4，再执行具体操作。**
不要跳过分类流程直接去搜索或加载具体 skill。

```
1. skill_view("intent-router") → 分类/消歧/路由
2. skill_graph_search(query)  → 发现对应 skill
3. skill_load("skill-name")   → 按指令执行
4. skill_load("quality-gate") → 最终验证
```

## 🔴 铁律 2 — 每轮重新判断意图 (Phase 0)

每次收到用户消息，无论是否是同一 session 的第 2、3、N 句，都必须重新走完整流程。

```
收到用户输入
  ├── Step A: Intent Reset — 抛弃上一轮分类缓存
  ├── Step B: 读当前消息，判断新意图类别
  ├── Step C: Multi-Intent Split — 拆成 N 个独立操作
  └── Step D: 进入 Phase 1-4
```

### Multi-Intent Split

一段话里包含多个可独立执行的操作时要拆分。不是解剖语法，
是问自己**「这段话能不能拆成 N 个独立执行的操作？」**

| 情况 | 判断 |
|------|------|
| "看看项目结构，给 user 模块加上批量删除" | → [1D 信息查询, 1B 功能开发] |
| "查一下这个 bug，然后把日志级别改成 DEBUG" | → [1D 查 bug, 1B 改配置] |
| "默认 connect timeout 30 秒，read timeout 300 秒" | → 同一件事，不拆 |
| "用 http.client 改，注意要处理 SSL" | → 补充说明，不拆 |

拆出来的片段**顺序执行**，回复中汇总成一个连贯响应。

## Phase 1: Input Classification

| 类别 | 说明 | 初始路由 |
|------|------|---------|
| 1A 任务管理 | 提到具体任务名/路径/hash | `skill_graph_search("task workflow")` |
| 1B 执行/操作 | "跑吧"/"做一下"/"commit" | → Phase 2 消歧，然后 Phase 4 路由 |
| 1C 设计讨论 | "我觉得 xxx 可以改成"/提问/观察 | 不执行，只参与讨论 |
| 1D 信息查询 | "这是什么"/"帮我看看 xxx"/"查看项目" | **先搜索相关 skill，再回答** |
| 1E 元操作 | 改 config/skill/memory | 直接处理 |

**关键：1D 信息查询不能只"直接回答"**。很多信息查询（如"查看项目"、"了解架构"）
背后有对应的 domain skill（`project-management-framework`、`domain-analysis` 等）。
先搜图找到合适的 skill，加载后按 skill 的指引来回答。

## Phase 2: Intent Resolution (仅 1B 执行类)

| 信号 | 行为 |
|------|------|
| "commit" | git 工作流（commit 但不 push） |
| "push" / "推送" | 允许 push |
| "停" / "stop" / "等我" | 立刻停，安静等待 |
| 全部完成 | `skill_load("quality-gate")` |

## Phase 3: Pre-flight Check (执行前检查)

| 操作目标 | 工具 | 检查 |
|---------|------|------|
| task 目录 | task-framework 工具 | 先读 TASK_MEMORY.md |
| git 仓库 | git 命令 | pre-change sync |
| skill 文件 | skill_manage | — |
| 规则文件 | read_file / write_file | — |

## Phase 4: Routing

分类后用 `skill_graph_search(query)` 发现对应 skill。
query 应该是自然语言描述意图，而不是直接复制用户的话：

| 用户说 | 不要搜 | 应该搜 |
|--------|--------|--------|
| "查看 eir 项目" | "eir 项目" | "project management overview analysis" |
| "这个 bug 怎么回事" | "这个 bug 怎么回事" | "debug python systematic" |
| "帮我设计数据库" | "帮我设计数据库" | "data model design patterns" |

### 路由表示例

| 用户意图 | 搜索 query |
|----------|-----------|
| 查看/了解项目结构 | `skill_graph_search("project management framework overview")` |
| 任务创建/管理 | `skill_graph_search("task management and workflow")` |
| Git 操作 | `skill_graph_search("git commit and push workflow")` |
| 代码审查 | `skill_graph_search("code review pull request")` |
| 写 PRD/需求 | `skill_graph_search("product requirements document writing")` |
| 调试程序 | `skill_graph_search("debug python systematic")` |
| 设计和原型 | `skill_graph_search("design prototype mockup")` |
| 架构分析 | `skill_graph_search("architecture discovery reverse engineering")` |
| 领域调研 | `skill_graph_search("domain analysis market research")` |
| 部署服务 | `skill_graph_search("deploy service docker")` |
| 视频制作 | `skill_graph_search("video production screen recording")` |
| 数据库设计 | `skill_graph_search("data model design naming conventions")` |

找到后调用 `skill_load(name)` 加载内容，按 skill 指令执行。

## 收尾

所有操作完成后，始终调用 `skill_load("quality-gate")` 做最终验证。
