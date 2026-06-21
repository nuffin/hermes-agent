---
name: troupe-orchestrator
description: "Orchestrate multi-worker collaboration — define troupes (groups of workers with distinct roles), scenes (workflows with stage-by-stage execution), and coordinate handoffs between workers."
version: 1.0.0
author: Hermes Agent Contributors
license: MIT
category: hermes
platforms: [linux, macos]
metadata:
  hermes:
    tags: [troupe, orchestration, worker, multi-agent, scene, workflow]
---

# Troupe Orchestrator

Orchestrate multiple workers in a coordinated workflow. A **troupe** is a
group of workers with defined roles; a **scene** is a multi-stage workflow
that assigns stages to different workers.

## When to Use

- You have multiple workers and need to coordinate them
- A task requires different specialities (research → plan → execute → review)
- You want to define reusable team configurations

## Directory Structure

Troupe configurations live in `~/.hermes/troupe/`:

```
~/.hermes/troupe/
├── roster.yaml          Worker definitions and roles
├── scenes.yaml          Scene/workflow definitions
└── souls/               Worker personality files (referenced by roster)
    ├── worker-a.md
    └── worker-b.md
```

## Roster Format (`roster.yaml`)

```yaml
workers:
  - name: worker-a
    role: researcher
    description: "Research and information gathering"
    soul: souls/worker-a.md

  - name: worker-b
    role: reviewer
    description: "Code review and quality assurance"
    soul: souls/worker-b.md

  - name: worker-c
    role: implementer
    description: "Feature implementation and testing"
    soul: souls/worker-c.md
```

## Scene Format (`scenes.yaml`)

A scene defines a staged workflow, each stage assigned to a worker:

```yaml
scenes:
  - name: standard-dev-cycle
    description: "Research → implement → review workflow"
    stages:
      - stage: research
        worker: worker-a
        prompt: "Research the requirements and produce a specification"
        handoff: "Pass specification to implementer"

      - stage: implement
        worker: worker-c
        prompt: "Implement the feature per the specification"
        handoff: "Pass implementation to reviewer"

      - stage: review
        worker: worker-b
        prompt: "Review the implementation for quality and correctness"
        handoff: "Report findings back to orchestrator"
```

## Procedure

### 1. Define workers

Create `~/.hermes/troupe/roster.yaml` with the workers your workflow needs.

### 2. Create worker profiles

```bash
for w in worker-a worker-b worker-c; do
  hermes profile create "$w" --no-skills
done
```

### 3. Assign souls

Create `~/.hermes/troupe/souls/<worker-name>.md` for each worker with
role-specific instructions and constraints.

### 4. Define scenes

Create `~/.hermes/troupe/scenes.yaml` with the stages your workflow needs.

### 5. Run a scene

```bash
# Launch the first stage worker
hermes -p <first-worker> -q "<scene stage prompt>"

# After handoff, launch the next worker with the previous output
hermes -p <next-worker> -q "<previous output> + <next stage prompt>"
```

Kanban workers automate this handoff — the orchestrator reads the board
state and dispatches the next worker when a card is completed.

## Pitfalls

- Workers don't share session context — pass artifacts explicitly via kanban
  cards or shared files
- Scene definitions are advisory — workers follow the prompts in their SOUL
- troupe directory is not auto-created — run `mkdir -p ~/.hermes/troupe/souls`
