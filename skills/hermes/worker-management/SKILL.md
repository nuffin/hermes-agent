---
name: worker-management
description: "Create, configure, and maintain worker profiles for multi-agent collaboration. Each worker is a focused agent instance with its own profile, skills, and personality."
version: 1.0.0
author: Hermes Agent Contributors
license: MIT
category: hermes
platforms: [linux, macos]
metadata:
  hermes:
    tags: [worker, profile, agent, management, multi-agent]
---

# Worker Management

Create and manage named worker profiles — dedicated agent instances with
their own skills, config, and personality (SOUL). Workers run autonomously
on kanban boards or via delegation.

## When to Use

- You need a dedicated agent for a specific role (reviewer, tester, researcher)
- You want to parallelize across multiple agents working on the same project
- You're setting up a multi-worker pipeline

## Quick Reference

```bash
# List existing workers
hermes profile list

# Create a worker profile (no bundled skills — clean slate)
hermes profile create <name> --no-skills

# Run a worker
hermes -p <worker-name> chat
```

## Procedure

### 1. Create a worker profile

```bash
hermes profile create <worker-name> --no-skills
```

The `--no-skills` flag creates a profile with an empty skills directory,
suitable for a focused agent. Add only the skills the worker needs.

### 2. Assign a personality (SOUL.md)

Each worker should have a SOUL.md that defines its role and behavioral
constraints:

```markdown
# Identity
You are a [role]. Your job is to [responsibility].

## Constraints
- Only perform [scope] tasks
- Report back to the orchestrator when done
- Never modify system configuration
```

Place it at `~/.hermes/profiles/<worker-name>/SOUL.md`.

### 3. Add skills

Workers typically need a small set of focused skills:

```bash
# From the bundled set
hermes -p <worker-name> skills install <skill-name>

# Or point to a shared skill directory via config.yaml:
# skills:
#   external_dirs:
#     - ~/shared-skills/
```

### 4. Configure environment

```bash
# Copy API keys
cp ~/.hermes/.env ~/.hermes/profiles/<worker-name>/.env

# Or set per-worker keys for different rate limits/quotas
```

## Pitfalls

- Workers share the same Hermes binary — profile isolation is config-only
- Each worker consumes API tokens independently
- Workers started via kanban dispatch inherit the parent's `.env`
