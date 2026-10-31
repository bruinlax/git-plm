# Change management — propagation that can't be automatic

*2026-08-11. Design note, per the maintainer: "a lot of times propagation can't happen
automatically — the part changed. Build in helpers. Replicate modern PLMs and
make it AI-friendly. Change notes are really kinda context."*

## How the grown-up PLMs do it

Teamcenter / Windchill / Arena all converge on the same shape. Change is not a
cascade — it's an **object** with a lifecycle:

1. **ECR (change request)** — the problem, in prose. "Bearings crack the boss
   when pressed cold."
2. **ECO / ECN (change order/notice)** — the decision vehicle. It carries:
   - **Problem items** — what triggered it.
   - **Impact analysis** — a *where-used walk that proposes candidates*, it
     never revises anything. Every parent that binds the changed item shows up
     as an "affected item" awaiting judgment.
   - **Dispositions** — a human (or agent) marks each affected item:
     `revise` (this parent genuinely needs a new revision — sometimes real
     redesign), `use-as-is` (old revision stays valid), `no-impact`
     (imprecise binding or irrelevant), plus a note saying why.
   - **Solution items** — the new revisions created under the change.
3. **Workflow** — DRAFT → IN REVIEW → APPROVED → IMPLEMENTED. Only
   *implemented* changes stamp the revisions with the ECN.

The maintainer's lantern ledgers already speak this language: every revision row carries
an `ecn` (ECN-0001…ECN-0028), and `plm/version_events.csv` is a change log.
plm's current `propagate` is the *mechanical* corner of this (rebind + patch
bump); the ECO layer is the judgment wrapper around it.

## plm design (to build next)

```
changes       id, number (ECN-####, auto), title, reason_md, state
              (DRAFT|IN_REVIEW|APPROVED|IMPLEMENTED|CANCELED), actor, created_at
change_items  change_id, item_id, role (problem|affected|solution),
              disposition (pending|revise|use_as_is|no_impact),
              from_version, to_version, note
```

Flow and helpers:

- `POST /api/changes {title, reason, problem: [numbers]}` → creates the change
  **and runs impact analysis**: where-used closure of the problem items, each
  ancestor recorded as `affected/pending`. Nothing is revised.
- `POST /api/changes/{ecn}/disposition {number, disposition, note}` — marking
  `revise` may optionally call the existing `ops.revise` (rebind helper) and
  records the new revision as a `solution` item; `use_as_is` / `no_impact`
  just record the judgment + note.
- `POST /api/changes/{ecn}/state` — the workflow. IMPLEMENTED stamps every
  solution revision's `ecn` field and closes the loop.
- UI: a **Changes** tab; a change page looks like the item page — title block,
  problem statement (markdown), affected-items checklist with disposition
  buttons, solution revisions. The dashboard feed gains change events.
- The change's `reason_md` and per-item disposition notes are **context** —
  same append-only philosophy, same FTS indexing, so "find the change where we
  decided not to rev the dome" is a search away.

## The AI-friendly part (where agents live in this)

The disposition queue is the natural agent surface:

- MCP: `open_change(title, reason, problem_items)` → returns the affected list;
  `draft_dispositions(ecn)` → an agent walks each affected item (reads its BOM,
  context, geometry summary) and *proposes* dispositions with reasons;
  `disposition(...)`, `implement_change(ecn)`.
- Humans stay the approval gate (IN_REVIEW → APPROVED); agents do the legwork
  of impact reading and drafting — which is exactly the part that's tedious.
- Everything is already JSON + markdown + git underneath, so a session can be
  pointed at a change number and reconstruct the entire story.

## What stays as-is

- Imprecise BOM lines (`child_version=''`) still resolve to latest with no
  ceremony — development mode.
- `propagate` remains as the mechanical bulk tool; under the ECO layer it
  becomes the implementation step for lines dispositioned `revise`-with-rebind.
