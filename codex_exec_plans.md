# Codex Execution Plans

ExecPlans are detailed, executable design documents that let a single agent or
new teammate deliver major changes to the TUXEDO stack without prior context.
They combine architectural intent, concrete tasks, validation criteria, and a
running change log so that work can pause and resume safely.

## When to Create an ExecPlan
- Cross-workspace features touching both EV and Rover UIs or back-end services.
- Any change that spans more than one sprint day, requires research, or alters
  interfaces shared between teams (e.g., TSS telemetry schema updates).
- Risky refactors, dependency upgrades, or security-sensitive flows.
- Whenever `AGENTS.md` directs you to produce an ExecPlan (e.g., a user request
  flagged as complex or multi-hour).

If a task is small enough to finish in a single focused session with trivial
review, an ExecPlan is optional. When in doubt, author the plan—reviewers can
trim scope later.

## Workflow Overview
1. **Orient** – Audit the relevant workspaces (`apps/`, `services/`, `libs/`),
   outline the current behavior, and capture constraints such as Node 20,
   workspace npm usage, and simulator dependencies.
2. **Draft** – Populate the template (below) with repository-specific context,
   naming exact files and functions that will change. Reference the intended
   user outcome before listing tasks.
3. **Review & Iterate** – Circulate the plan with stakeholders across UI,
   services, and mission ops. Update the `Decision Log` with every change to the
   plan.
4. **Execute & Maintain** – While implementing, keep the `Progress`, `Concrete
   Steps`, and `Surprises & Discoveries` sections current. Each stop in work
   must leave the plan executable for the next person.
5. **Close Out** – Record results and follow-up items in `Outcomes &
   Retrospective`, then link the plan in the corresponding PR or milestone.

## Required Sections and Expectations
- **Purpose / Big Picture** – State the user-visible capability this change
  unlocks and where it surfaces (e.g., EV HUD route guidance, AIA voice prompt).
- **Progress** – Checkbox list with UTC timestamps; split partial work so it’s
  obvious what remains. Example: `- [ ] (2026-01-08 14:35Z) Integrate
  libs/tss-client rate limiter (remaining: unit tests)`.
- **Surprises & Discoveries** – Bullet observations with evidence (logs,
  transcript snippets, benchmark numbers).
- **Decision Log** – Every design choice, along with rationale and author.
- **Context and Orientation** – Short tour of relevant files and current
  behavior. Mention key configs (env vars, fixtures, simulator assets).
- **Plan of Work** – Ordered prose describing edits. Include file paths, target
  exports, or component names (e.g., `apps/ev-ui/src/map/NavigationPanel.tsx`).
- **Concrete Steps** – Shell commands with working directories and expected
  output. Sample commands for this repo typically include `npm install`,
  `npm run lint -w <workspace>`, `npx tsc --noEmit --project libs/tss-client`,
  or `npm test -w apps/pr-ui` once scripts exist.
- **Validation and Acceptance** – Define behavioral checks (UI demo steps,
  simulator runs, or test suites). State what fails before the work and passes
  after.
- **Idempotence and Recovery** – Describe safe reruns, migrations to revert,
  or cleanup commands.
- **Artifacts and Notes** – Capture critical diffs or telemetry snippets that
  prove the change works; keep them concise.
- **Interfaces and Dependencies** – Document new API shapes, TypeScript types,
  message schemas, or third-party packages introduced.
- **Outcomes & Retrospective** – Summarize successes, remaining risks, and
  lessons. Attach follow-up issues if needed.

## ExecPlan Template

Use this template verbatim when creating a new plan. Save the plan under
`docs/exec_plans/<feature-name>.md` unless a project lead specifies otherwise.
Ensure timestamps are UTC and update the living sections continuously.

```md
# <Action-Oriented Title>

This ExecPlan follows `docs/codex_exec_plans.md`. Update every section as work
progresses; the document must remain self-contained.

## Purpose / Big Picture

<Explain the user-facing capability unlocked by this change and how to observe it
in the TUXEDO environment.>

## Progress

- [ ] (<UTC timestamp>) <Step summary; include remaining work if partial.>

## Surprises & Discoveries

- Observation: <Unexpected behavior or insight.>
  Evidence: <Logs, measurements, or links to artifacts.>

## Decision Log

- Decision: <What was decided.>
  Rationale: <Why this choice was made.>
  Date/Author: <UTC timestamp, Name.>

## Outcomes & Retrospective

<Summarize achieved results, validation status, follow-ups, or open risks.>

## Context and Orientation

- Code paths: `<repo path>` – <Current behavior.>
- Assets/configs: `<repo path or env var>` – <Relevance to the change.>
- Dependencies: <Important libraries, services, or simulators involved.>

## Plan of Work

1. <File and change description, e.g., “Update `libs/tss-client/src/index.ts`
   to extend telemetry schema with battery health fields.”>
2. <Next edit.>

## Concrete Steps

1. `(<workdir>) <command>`  
   Expected: <Key output signature or check.>

## Validation and Acceptance

- <Describe manual flows or tests to run, expected results, and failure modes
  they address.>

## Idempotence and Recovery

- <List rerunnable steps or rollback commands.>

## Artifacts and Notes

- <Paste essential diffs, telemetry samples, or screenshots (described in text).>

## Interfaces and Dependencies

- <Document TypeScript types, REST/WebSocket payloads, or config keys that must
  exist after the implementation.>
```

Maintain the plan in the same PR branch as the implementation. Every commit that
moves the work forward should update the plan accordingly so reviewers can
reconstruct the thought process and remaining scope. When the effort concludes,
archive the final ExecPlan alongside the code changes for future reference.
