---
name: intent-router
description: "Intent router — classify user input, disambiguate, map to the right skill, then execute"
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
          reason: Intent routing + graph-based skill discovery complement each other for broader coverage
          strength: medium
    tags:
      - hermes
      - intent-router
      - 意图路由器
      - classification
      - routing
---

# Intent Router

## Scope

intent-router answers **what** to execute, not **how** to execute it:

```
user input → Phase 0 → Phase 1-3 classification → Phase 4 routing → execute → quality-gate
```

## 🔴 Rule — intent-router loads first, always

**Every user input must go through intent-router Phase 0-4 before any
skill is loaded or any action is taken.** Do not skip classification.

```
1. skill_load("intent-router") → classify / disambiguate / route
2. skill_graph_search(query)  → discover the right skill
3. skill_load("skill-name")   → follow the skill's instructions
4. skill_load("quality-gate") → final validation
```

## 🔴 Rule 2 — Re-classify every turn (Phase 0)

Every user message, even the 2nd, 3rd, Nth in the same session, must
re-run the full classification flow. Intent can change mid-session.

```
receive user input
  ├── Step A: Intent Reset — discard previous classification cache
  ├── Step B: Read current message, determine new intent type
  ├── Step C: Multi-Intent Split — N independent actions? Split.
  └── Step D: Enter Phase 1-4
```

### Multi-Intent Split

Split when a message contains multiple independently executable actions.
Ask yourself: **"Can I split this into N independent operations?"**

| Input | Split? |
|-------|--------|
| "check project structure, add batch delete to user module" | → [info query, feature dev] |
| "investigate this bug, then set log level to DEBUG" | → [debug, config change] |
| "default connect timeout 30s, read timeout 300s" | → same task, no split |
| "use http.client, handle SSL" | → same task, no split |

Split pieces execute **sequentially**; summarise into one coherent reply.

## Phase 1: Input Classification

| Type | Description | Initial route |
|------|-------------|---------------|
| 1A Task mgmt | Task name/path/hash mentioned | `skill_graph_search("task workflow")` |
| 1B Execute | "run"/"do it"/"commit" | → Phase 2, then Phase 4 |
| 1C Design discussion | Suggestion / proposal / question | Discuss only, don't execute |
| 1D Info query | "What is"/"show me"/"check project" | **Search for a skill first, then answer** |
| 1E Meta | Change config/skill/memory | Handle directly |

**Key: 1D info queries must not just "answer directly"**. Many info
queries ("show the project", "explain the architecture") have a matching
domain skill (`project-management-framework`, `domain-analysis`, etc).
Search the graph first, load the skill, then answer following its guidance.

## Phase 2: Intent Resolution (1B only)

| Signal | Action |
|--------|--------|
| "commit" | Git workflow (commit, no push) |
| "push" | Allow push |
| "stop" / "wait" | Stop immediately, wait silently |
| All done | `skill_load("quality-gate")` |

## Phase 3: Pre-flight Check

| Target | Tool | Check |
|--------|------|-------|
| Task directory | task-framework tools | Read TASK_MEMORY.md first |
| Git repo | Git commands | Pre-change sync |
| Skill file | skill_manage | — |
| Rule file | read_file / write_file | — |

## Phase 4: Routing

After classification, use `skill_graph_search(query)` to discover the
right skill. The query should describe the **intent** in natural
language, NOT copy the user's exact words:

| User said | Don't search | Search instead |
|-----------|-------------|----------------|
| "show me the eir project" | "eir 项目" | "project management overview analysis" |
| "what's wrong with this bug" | "这个 bug 怎么回事" | "debug python systematic" |
| "help me design the database" | "帮我设计数据库" | "data model design naming conventions" |

### Routing table

| User intent | Search query |
|------------|--------------|
| View / understand project structure | `skill_graph_search("project management framework overview")` |
| Create / manage tasks | `skill_graph_search("task management and workflow")` |
| Git operations | `skill_graph_search("git commit and push workflow")` |
| Code review | `skill_graph_search("code review pull request")` |
| Write PRD / requirements | `skill_graph_search("product requirements document writing")` |
| Debug a program | `skill_graph_search("debug python systematic")` |
| Design / prototype | `skill_graph_search("design prototype mockup")` |
| Architecture analysis | `skill_graph_search("architecture discovery reverse engineering")` |
| Domain research | `skill_graph_search("domain analysis market research")` |
| Deploy a service | `skill_graph_search("deploy service docker")` |
| Video production | `skill_graph_search("video production screen recording")` |
| Database design | `skill_graph_search("data model design naming conventions")` |

After finding the right skill, call `skill_load(name)` and follow its
instructions.

## Cleanup

After all work is done, always call `skill_load("quality-gate")` for
final validation.
