# Enabling ExecPlans for long-running Codex work

The `gpt-5-codex` model can stay on task for hours when it is given a precise, living specification to follow. This repository now ships such a specification format in [`PLANS.md`](./PLANS.md). Use that file to brief Codex (or any other coding agent) before it begins a multi-step implementation so the agent can reason, course-correct, and document progress without losing track of the goal.

## What changed in this repository

- [`PLANS.md`](./PLANS.md) contains the full rubric for an ExecPlan, including formatting rules, required sections, and the default skeleton to start from. Check it into the repository so every agent and human collaborator can reference the same canonical guidance.
- [`AGENTS.md`](./AGENTS.md) now instructs contributors to create an ExecPlan—following `PLANS.md`—whenever a task involves complex features, risky refactors, or work that obviously spans multiple iterations.

Together these updates teach Codex how to bootstrap a plan, how to keep that plan current while it works, and how to hand it off to the next assignee if needed.

## Working with ExecPlans

1. **Before coding**: Ask the agent to create a new ExecPlan in the repository (usually under a task-specific path) that complies with every requirement in `PLANS.md`. The plan must be self-contained and must describe how the feature will be validated.
2. **During implementation**: The agent should continually revise the ExecPlan—especially the `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` sections—so the document always reflects the latest state of the work.
3. **After completion**: Confirm that the documented validation steps pass. Close the plan with an `Outcomes & Retrospective` update that records what shipped, what remains, and what the next maintainer should know.

Because ExecPlans are written in Markdown and stored alongside the code, they provide both project direction and institutional memory. Treat them as a source of truth: if the implementation diverges, update the plan or revise the code until they align.

## Prompting tips for Codex

- Reference "ExecPlans" explicitly when requesting a plan so the agent consults `PLANS.md` and honors the formatting requirements.
- Remind the agent that `PLANS.md` lives at the repository root and must stay in sync with any new ExecPlan it authors or edits.
- When resuming an in-progress plan, instruct the agent to read the existing ExecPlan from top to bottom and update the living sections before touching code.

By relying on ExecPlans, Codex can confidently tackle long-running efforts, and humans reviewing the work gain a clear, auditable trail of decisions and validations.
