# The review model — the best of git forges and Teamcenter

*2026-09-17. Scoping, per the maintainer: "our approvals / comments / events etc — all
this should start to mirror the best of git and Teamcenter." Companion to
`PLM_GIT_FIRST.md`. **Status: DESIGN. Nothing here is built** except where it
says "exists".*

Once design data lives in git (decided the same day: "git under the hood for
everything going forward"), what remains in the database is **forge state** —
the conversation and the process *about* the design. This document says what
that state should look like. It builds on three August designs rather than
replacing them: `PLM_IDENTITY.md` (release gate — built), `PLM_MARKUP.md`
(redlines with coordinates — designed), `PLM_CHANGES.md` (ECO — designed).

## 1. Two traditions, and what each is actually good at

| | Git forges (GitHub / GitLab / Gerrit) | Teamcenter-class PLM |
|---|---|---|
| Unit of change | the **pull request**: one reviewable, discussable, mergeable thing | the **change object** (ECR → ECN): a *reason* that spans many items |
| What approval binds to | an **exact commit SHA**; new pushes dismiss stale approvals | a signoff on a workflow task — bound to a *revision*, weakly to bytes |
| Who must approve | CODEOWNERS + branch protection (N reviews, required checks) | workflow templates + **signoff profiles / quorum** by release level |
| Discussion | threads **anchored to a line**, resolvable, "outdated" when it moves | markup on the viz (JT) file, largely detached from the record |
| Automated judgement | **status checks** on every push; merge blocked until green | validation rules / handlers, usually at release time only |
| Knowing the blast radius | weak — a repo doesn't know who depends on it | **where-used, impact analysis, dispositions** — its best idea |
| After release | tags/releases; little notion of "status" | **release status**, supersession, obsolescence, effectivity, baselines |
| Record | a readable **timeline** per PR | a complete **audit trail** with identity-verified signatures |
| Failure mode | knows nothing about product structure | process so heavy people route around it |

**The synthesis:** take the forge's *unit and ergonomics* (proposal, SHA-bound
reviews, anchored threads, checks, timeline) and Teamcenter's *structure and
rigour* (change objects with impact analysis, quorum by maturity, release
status, signatures). Git makes the Teamcenter half *stronger* than Teamcenter's
own: a signoff bound to a commit SHA is a signature over the exact bytes of the
part, its BOM pins, and its metadata — something a revision-level signoff never
was.

## 2. The objects

### 2.1 Proposal — the PR, and the workflow process

Today a "PR" is *a DRAFT revision + a pending `approvals` row*, and every submit
makes a new row. Git-first breaks that in three places: several drafts can be
open at once; a draft is re-pushed during review; and what was approved must be
provably what was merged. So the proposal becomes a first-class, long-lived
object — **one proposal per draft branch, surviving every review round**:

```
proposals   id, number (#123, per registry), item_id,
            head_ref (draft/…), head_sha, base_ref, base_sha,
            title, body_md, target_maturity,
            state  draft | open | approved | merged | rejected | withdrawn,
            author, change_id (→ §2.5), created_at, merged_sha, merged_tag
```

Additive migration: `approvals` rows become the proposal's **rounds**
(`approvals.proposal_id`), so nothing already recorded is lost. Git side:
`refs/plm/proposals/<n>/head` keeps the branch's commits reachable after merge
(GitHub's `refs/pull/N/head`) — the review history outlives the draft branch.

- **Draft proposals** (GitHub "draft PR") = pushed `draft/*` not yet submitted:
  visible, diffable, commentable, not in the queue.
- **Merge = approval** (unchanged, non-negotiable). **Merge method: keep the
  pushed commits** — fast-forward when possible, a real merge otherwise — then
  one *release commit* on top and the annotated tag (as built in POC stage 3).
  ~~Squash by default~~ was considered and dropped: squashing re-authors the
  bytes, which breaks "the commit you pushed IS the revision" and the SHA-bound
  approval that rests on it. The revision ledger is `git tag` /
  `git log --first-parent main`, not a squashed history.

### 2.1b Release — the promotion, a second object (the maintainer, 2026-09-17)

> "approvals should take the best of git and a PLM like Teamcenter — basically
> pull requests, and also like releases."

Plain git has neither: it has tags. **Pull requests, reviews, comments, required
approvers, protected tags and Releases are all forge features** — and the
forges keep *two* objects where we had one:

| | **Proposal** (pull request) | **Release** (promotion) |
|---|---|---|
| The question | "is this change right?" | "is this revision ready to build / buy against?" |
| Acts on | a `draft/*` branch → merge + version tag | an **existing tagged revision** — *no file changes* |
| Who decides | policy (auto-merge ≤ prototype) or a human | **always a human** |
| Carries | diff, threads, checks, timeline | release notes, attached build artifacts, evidence, comments |
| Gate | valid manifest, required checks | every child pin RELEASED, no open *blocking* thread, checks green |
| Forge analogue | PR / MR | GitHub Release + environment approval; GitLab protected tag + release evidence |
| Teamcenter analogue | change implementation | **release status applied to a revision** |

Why it must be separate: today only a DRAFT can be submitted
(`ops.submit_for_release`), so taking a part from PROTOTYPE to RELEASED means
minting a *new revision whose design did not change* — a version bump that
records a decision, not a design. That is wrong in Teamcenter terms (status is
applied *to* a revision) and wrong in git terms (you don't commit to cut a
release). Real life is: iterate as prototypes — many merged proposals, many
pre-release tags — then **promote one that was built and proven.**

```
releases    id, item_id, version, tag_sha,           -- the revision being promoted
            from_maturity, to_maturity,              -- prototype → released
            state  requested | approved | rejected | withdrawn,
            notes_md,                                -- release notes
            evidence_sha256, evidence_key,           -- §evidence
            requested_by, decided_by, decided_at, decision_note
```

- **Promotion never touches the tree or the tag.** The decision is forge state,
  mirrored into `refs/notes/plm` on the release commit (`[status] maturity =
  "released"`, approver, date, release id) so a clone can see it and
  `--rebuild` can re-derive it. `vX.Y.Z` stays the one immutable name.
- **Release notes are generated, then edited** (GitHub's auto-notes): the
  proposals merged since the last RELEASED revision — titles, authors, pin
  bumps with the child's reasons, attribute deltas — drafted by an agent, signed
  off by the human.
- **Assets** (§6b of `PLM_GIT_FIRST`): built STL/3MF, drawings PDF, rolled BOM
  export, slicer report — attached to the release, hashed, immutable.
- **Evidence** (GitLab's release evidence; Teamcenter's audit package): at
  approval, freeze one JSON document — the manifest, the full pin closure
  (every descendant's number, version, commit), check results, the proposals
  and approvers that led here, asset hashes — store it, record its sha256 in the
  note. "What exactly did we release, and who said so?" becomes one file whose
  hash is in git.
- **Pre-release / Latest** flags fall out: maturity below released = pre-release;
  "Latest" = newest RELEASED, which is what BOM `track = "release"` should
  follow when a parent is itself heading for RELEASED.
- **A release is a small WORKFLOW with recorded signoffs, not one click**
  (the maintainer: "we'd need a workflow and approvals for the release process — the
  git / GitLab / Gitea type concepts"). The forge ideas: *protected tags*,
  *environments with required reviewers*, *approval rules* (N approvals from
  named people/roles), *required status checks*. The Teamcenter ideas: a
  *workflow template chosen by release level*, *signoff tasks with a quorum*,
  and *do-tasks* (a checklist somebody attests to). Combined, per maturity
  transition, as data:

  ```python
  RELEASE_WORKFLOWS = {                      # a registry that glosses; per-project override
    ("prototype", "released"): {
      "approvals":  {"required": 1, "roles": ["approver"]},   # quorum
      "checks":     ["manifest", "pins-released"],              # must be green
      "checklist":  [                                           # attested, by name
        "Built/printed at least once at this revision",
        "Fit-checked against mating parts / the assembly",
        "BOM is purchasable (every buy line has a live source)",
        "Instructions reviewed",
      ],
    },
    ("released", "obsolete"): {"approvals": {"required": 1, "roles": ["approver"]}},
  }
  ```

  ```
  release_signoffs  id, release_id, kind (approval | checklist), item (text),
                    signer, signer_kind (human|bot), verdict, note,
                    tag_sha, identity, signed_at
  ```

  **Attested by reference (stage 10, built).** A line listed under the
  workflow's `evidence` (`"Fit-checked against mating parts / the assembly":
  "coupon_for"`) may be signed with `evidence = "<COUPON>@v<x>"` — accepted
  only for a claim of that type pinned at the tag's exact commit (or the commit
  the release commit was made from) with `result = "pass"`. The signoff row gains
  `evidence`; the evidence file gains `evidence_claims` (both absent when
  unused, so earlier evidence files hash the same). No such claim → by hand.

  States: `requested → in_review → approved | rejected | withdrawn`. The release
  becomes `approved` only when the quorum of **human** approvals is met, every
  required check is green and every checklist line is attested. Each signoff is
  its own row **bound to the tag SHA** with the verified identity — the
  e-signature. **Agents may attest checklist lines they can prove** (e.g.
  "BOM is purchasable", with the evidence linked) and may never count toward the
  approval quorum. One approver in a small deployment; the quorum is still a
  number, because that is the difference between a gate and a habit. The
  signoffs, in order, ARE the release's timeline and go into the evidence file.
- **The shortcut stays:** a proposal may target `released` directly (a human
  approves; it merges *and* cuts the release in one decision). The new
  capability is promotion *without* a change.
- **Comments** live on both objects, same thread model (§2.4) — a release
  thread is where "printed 12, all fit" gets recorded against the revision.
- **After release** — superseded (derived), obsolete / hold (decisions) — §2.7.

### 2.2 Review — the signoff

```
reviews     id, proposal_id, reviewer, kind (human | bot),
            verdict  approve | reject | comment,
            head_sha,            -- what they actually looked at
            body_md, created_at, identity (oidc sub | token), dismissed_at
```

- **Bound to `head_sha`.** A push after an approval **dismisses** it (forge
  "dismiss stale reviews") — the approval in the record is always an approval
  of the bytes that shipped. This is the e-signature Teamcenter wants, for free.
- **Verdicts: keep August's position — no `changes_requested`.** "Reject" means
  *back to draft, here are the redlines* (`PLM_MARKUP.md` §5); the specifics
  live in threads, not in a fourth state. What changes: a reject no longer ends
  the object — the **same proposal** reopens on the next push, so the whole
  back-and-forth reads as one timeline.
- **Bots review, bots never count.** An agent reviewer (AllSpice's "DRCY") is
  valuable — it reads the diff, runs the numbers, leaves threads — and its
  verdict is recorded as `kind = bot` and **excluded from quorum**. Charter:
  humans approve.

> **DECIDED 2026-09-17 (the maintainer): "we don't need to be hard about bots not
> approving for now — sometimes I just want things built. That should be a
> setting."** Everywhere this document says *bots never approve*, read: **who may
> satisfy an approval is a policy setting** — `approver_kinds` per project and
> target maturity (§2.3): `("human",)` by default for RELEASED, `("human","bot")`
> when the project is in "just build it" mode, `auto` (the policy itself) at
> PROTOTYPE and below. One place decides; every door (API/MCP `decide`, release
> signoffs, legacy `checkin`, auto-merge) asks it. The *record* never softens: a
> bot's approval is stamped `BOT`, an automatic one `AUTO`, so the ledger always
> says whether a human looked. Switchable by env default + per-project override,
> readable at `GET /api/policy`, shown on the decision bar.

### 2.3 Review policy — CODEOWNERS meets signoff profiles

Who must approve, and what must be green, **by target maturity** — Teamcenter's
"release level decides the workflow", expressed like branch protection:

| Target | Human approvals | Required checks | Child rule (`PLM_GIT_FIRST` §5.1) |
|---|---|---|---|
| concept / design | 1 | manifest lint | none |
| prototype | 1 | lint · BOM resolves · mesh present | warn on unreleased pins |
| released | 1 (+ owner, if the item has one) | all of the above · render matches source · no open blocking thread | **every pin on a release tag** |

- Policy is **process**, so its defaults live in the forge (per project). An
  item may *tighten* it in-repo — `[review] owners = ["alice"]` in `plm.toml`,
  the CODEOWNERS move — because "this part needs a second look" is a fact about
  the part and should travel with a fork.
- Small-deployment reality: one approver. Model quorum anyway; it costs a column and
  it is the difference between a gate and a habit.
- **Auto-merge** (forge idea worth stealing): an approver may approve *ahead of*
  green checks — "merge when the render check passes."

#### What "just build it" means (stage 16b, decided after cold run 5)

A project in "just build it" mode (`policy.auto` for a maturity) removes the
**human**, not the **decisions** a human would have been shown. A change
merges by policy only when, at submit time (`ops.auto_hold`, both the
change-of-many and the change-of-one path):

1. **every impact row has a disposition** — each parent that pins what the
   change touched and each item that carries a source file it changed, NOT in
   the change, says `bump` / `use_as_is` / `no_impact`. A disposition drafted
   by a bot (`draft_dispositions`) counts: the note is the record of why, and
   it is visible on the change page. Run 5's CHG-156 merged with GSI-ASM-0009
   (pins the bumped GSI-ASM-0001) undecided — decided by silence;
2. **the decision's own pre-flight passes** — no failing or missing REQUIRED
   check, no pending check, no stale pin, no `bump` disposition without its
   proposal, no blocking thread. Asked before merging, so a refusal HOLDS the
   change open for a human with the reasons instead of rolling the submission
   back.

**Amber is allowed** (an advisory check that warned or was skipped): the
change merges, and the `say` line the agent relays reads "merged by policy —
nobody reviewed it — merged with N amber check(s): …", so the human learns of
it in the one sentence they read. A held change's `say` reads "waiting for your
approval — 'just build it' would have merged it, but: …"; after the agent
records the missing dispositions, `submit_change` again merges it by policy.
Demos that switch a project to this mode restore its policy on exit
(`the policy demo library`).

### 2.4 Threads — comments, context and markup become one thing

Today: `context` (append-only "why", targets registry — exists) and `markups`
(anchored redlines — designed). Forges have one concept for both: **a thread
with an anchor**. Unify on that, keeping `context.TARGETS` as the registry:

| Anchor | Forge equivalent | Goes "outdated" when… |
|---|---|---|
| proposal / item / revision / relation / approval (exists) | PR conversation | never |
| `file + line @ sha` | line comment | that hunk changes in a later push |
| `plm.toml` key — an attribute, a `[[bom]]` line, a pin | *(none — ours)* | that key's value changes |
| **3-D point / face on a mesh `@ sha`** (`PLM_MARKUP.md`) | *(none — ours)* | that mesh blob changes |
| image region `@ sha` | image comment | that image blob changes |

- Git dissolves the markup design's "hard problem" (anchoring across
  revisions): an anchor is `(commit, path, locator)`, and *stale* is no longer a
  geometric guess — it is `git diff` saying that blob changed. Carry-forward
  becomes: unchanged blob ⇒ the thread still applies, exactly.
- Threads are **resolvable** (by the raiser — August's rule), and
  `addressed_by` names the commit that fixed it. **Suggested changes** (forge):
  a reviewer proposes a parameter edit in a thread; the author — usually an
  agent — applies it as a commit with one call.
- **Blocking is opt-in per thread, not global.** August was right that a redline
  must not silently block release. Keep that default, and add the one thing it
  lacked: the raiser may mark a thread `blocking`, which the `released` policy
  honours. An approver can still ship over it — by resolving it *with a reason*,
  which is the recorded decision August wanted to keep possible.
- Still impossible to approve without seeing them: open-thread counts on every
  queue row and in every submit response (August §5, unchanged).

### 2.5 Change — the ECO is a set of proposals

> **Named "pull request" (the maintainer, 2026-10-16: "if you call it 'pull' then let's
> just call it pull request").** The object below is what every person and
> agent now reads as a **pull request**, numbered `#380`; `CHG-380` is its
> legacy label and stays accepted everywhere (API paths, MCP arguments,
> `-o change=`). In the code and the API it is still a *change* (`/api/changes`,
> `submit_change`, `changes.py`), for compatibility: `/api/pulls/…` is a path
> alias of `/api/changes/…`; the page is `/pulls/<n>[/files|/checks|/commits]`
> (`/changes/CHG-n…` 301s there); the list is `/pulls` (Open / Closed), and an
> item's own list is its **Pull requests** tab (`/<N>/pulls`). The push option
> is `-o pr=new|<n>` (`-o change=` still works). A member proposal reads `p<n>`
> where it sits inside a pull request, so `#` means one thing.

`PLM_CHANGES.md` stands; git supplies its mechanics:

- A **change** (ECN-####) has a problem statement, problem items, and the
  proposals that solve it — across repos. It is the forge's missing object:
  "a stack of PRs with a reason."
- **Impact analysis is a graph query over gitlinks**: who pins this part, at
  which commit, transitively. The index already holds it.
- **Dispositions** per affected parent stay exactly as designed —
  `revise | use_as_is | no_impact`, agent-drafted, human-decided. `revise`
  now means *the bot opens a bump proposal on that parent* (the Dependabot
  move) **attached to the same change**, so one change page shows the child fix
  and every consequence, each with its own diff.
- **Implementing a change** merges its proposals bottom-up (children before
  parents, so parents can pin real release tags) and stamps the ECN into each
  tag annotation. A change is "done" when every affected item is dispositioned.
- **Problem reports** = issues on an item ("v0.3.0 cracked at the snap rail").
  An issue can spawn a change. Today's shouted tags (`UNVERIFIED`, `MEASURE`)
  are issues that had nowhere to live.

### 2.6 Checks — CI for physical things

```
checks      item_id, sha, name, status (queued|running|pass|fail|warn|skipped),
            summary, details_md, url, source (integration name), started/ended
```

Run on every push, keyed by SHA, shown on the proposal, required or not by
policy (§2.3). Posted through the existing **integrations** seam — a check
runner is just an integration that answers a `push` event with a check result
(GitHub's Checks API shape; native labels).

First checks, in value order: **manifest lint** (exists, in pre-receive) ·
**BOM resolves / pins released** · **render matches source** (re-render
`[source]`, compare to the committed mesh — catches a hand-edited STL) ·
**geometry facts** (bbox, volume, mass at declared material → written back as
attributes with `source = check`) · **slicer estimate** (time, grams, supports;
the shop-floor service) · **fit against siblings** (interference in the assembly frame) ·
thumbnail. A failed check is a thread-shaped thing: it can point at a file.

**Render input snapshots.** The render check extracts the reviewed
commit and recursively materializes `parts/<NUMBER>` and `lib/<NUMBER>` from
PLM's local repository store at their gitlink commits, then smudges LFS meshes.
It never fetches `.gitmodules` URLs or substitutes a child's current branch.
Archive traversal and escaping links are refused; expansion is bounded to 16
levels and 128 repository occurrences. Missing pins or unsafe dependencies
surface as check errors, never a passing render. The render cache includes all
input tree entries and pins, so changed imported meshes or child commits cannot
reuse an earlier result. `catalog` metadata is not a CAD dependency.

**`verified-by-coupon` (stage 10, built, advisory).** The coupons whose
`coupon_for` claim pins THIS commit, and what they found — applies only when
one does. The claim lives in the coupon's repo, so like `explodes` this check
reads the registry's derived index (`relations.claims_on`); a coupon index
re-queues it on its target's commit. Any `fail` with no `pass` → fail; both →
warn naming both; no result yet → warn. Advisory: the release checklist (§2.1b)
is where a passing coupon is USED.

### 2.7 Release status — what git doesn't have

A tag says *this was released*. Teamcenter also knows what it is **now**:
superseded, obsolete, on hold. That is forge state (a later decision about an
immutable thing), mirrored to `refs/notes/plm` so a clone can see it:

- `superseded` is **derived** (a newer release exists) — never stored.
- `obsolete` / `hold` are **decisions** — recorded, signed, reasoned, evented.
- **Baselines are free**: a release tag on a top assembly pins every descendant
  by gitlink. "What exactly was the lantern on Aug 19?" is `git checkout`.
- Effectivity (date / serial) and variants: not until a real one exists
  (unchanged from `PLM_BRANCHING.md`).

### 2.8 Timeline, audit, notification

`events` (exists, append-only) is already the audit log. What it lacks is
*shape*: render it **per proposal** as the forge timeline — pushed · check
failed · thread opened · review requested · approved @ sha · merged · tagged —
and per item as the activity feed. Every entry carries the verified identity
(OIDC `sub` or token → user, already built) and, where relevant, the SHA.

Notification is the human's entry point (charter: humans approve, purchase,
visualize): **"#41 needs your approval — 3 files, +1 pin bump, checks green"**
with the compare link, via comms (Slack/iMessage). The approval should be
doable from that link on a phone: compare view → threads → Approve.

The links are real paths (stage 22, `docs/PLM_GIT_FIRST.md` §6c-0): a proposal
is `/<N>/pull/<pid>`, a pull request `/pulls/<n>` (was `/changes/CHG-<n>`, which
redirects), a release
`/<N>/releases/tag/<vX.Y.Z>`, a release set `/release-sets/<n>` — what every
`url` / `page` field and every notification carries.

### 2.9 Diff forms — one registration per file type (built, 2026-10-16)

Maintainer: *"for each file that changed — if it's an stl or something I should be
able to see not only the text changes — but the overlay as a different 'form'
of that change."* A changed file can be READ in several forms; which forms a
file has is a fact about its TYPE, so it is a registry, like `MEDIA_KINDS`,
`ACTIONS` and the checks — never an `if (ext === 'stl')` at a call site.

`web/diffwidgets.js` (`window.PLM_DIFF`) is the one implementation. Every
surface that shows a changed file — the change page's **Files changed** tab,
the compare view, the proposal page — draws its rows through it.

```js
PLM_DIFF.register({
  id: 'mesh', kinds: ['mesh'],               // one registration per file type
  match: (file) => kindOf(file) === 'mesh',  // first match wins; fallback last
  forms: [{ id: 'overlay', label: 'Overlay', when?: (file) => bool,
            render(container, ctx) { … } }, …],
  defaultForm: (ctx) => ctx.file.same_content ? 'pointer' : 'overlay',
})
```

`file` is a compare row (`gitrepo.compare` + `PLM_DIFF.rows`: both sides'
paths and URLs, sizes, patch, `ident_base/head` = blob + LFS pointer,
`same_content`, plm.toml's flat `fields`); `ctx` = `{number, cmp, members,
review}`. `PLM_DIFF.choose(file, q)` is pure (node-tested): `q.form` when the
row offers it, `overlay` when `q.mesh` names the file, else the row's default
— which is how the compare page's `?file=` / `?mesh=` / `&form=` land on a form.
`row(file, ctx, {q})` draws one row (status, path, role — source / mesh / viz /
doc / plm.toml / submodule pin —, ± or size delta, one toggle per form, View
file, ⋯); `card(title, file, ctx)` a featured card.

| type (server `kind`) | forms | default |
|---|---|---|
| mesh | **Overlay** (base red, head green, one scene — app.js `overlayHost`), **Stats** (volume, bbox, triangles, centre of mass; measured in the browser, the geometry-facts check as fallback), **Pointer** (the LFS pointer git sees) | Overlay; Pointer when the bytes are identical ("no geometry change") or the format is not STL |
| image | Side by side, Swipe, Onion skin, **Difference** (canvas pixel diff) | Side by side |
| text, pattern | Unified, Split | Unified |
| markdown | Unified, Split, Rendered | Unified |
| manifest (plm.toml) | **Fields** (changed keys by name: `version 0.2.0 → 0.2.1; bom[20].qty 2 → 4`), Design (the redlines), Text | Fields |
| submodule (gitlink) | Pin (`CID-PRT-0001: v0.1.0 → draft @ 9435388 (member #836)` + the child's commits) | Pin |
| anything else | Unified/Split when git reads it as text, else Size (size delta + blob) | by row |

**One WebGL context.** The Overlay form never makes a renderer: it docks the
single dark room (`#viewer`) into the row that asks; a row takes it when it has
sat in view and nothing else visible holds it, and the row it left keeps a
"Show the 3-D diff" button. N meshes on a Files tab are N rows, one context.

`roles.MEDIA_KINDS[kind]["diff"]` is the same list of form ids as data for
agents and `/api/actions`; `tests/test_diff_forms.py` holds it equal to the
registry. Adding a differ is one `register` call and that tuple.

**The page it serves.** The change page is a pull request: header (title,
CHG-n, state, "<author> wants to merge N members (…) into main", policy;
Approve/Refuse only for a viewer the decision would admit, else "Waiting on
<approver>", with the pre-flight verdict), four tabs as real paths
(`/pulls/n`, `/commits`, `/checks`, `/files`; `/<N>/pull/<n>/<tab>`
scoped to one member) and a sidebar (approvers + policy, members, release set,
project, waiting on). The repo home and the project page carry GitHub's
banners (`GET /api/items/<N>/pending`, `/api/projects/<p>/pending` —
`plm/pending.py`), and the masthead count is a bell listing what waits on you
(`/api/waiting`, `can_decide` per row).

**Complete design.** Every PR tab also presents complete assembly snapshots
above the review content, with other members beneath. These links use the
member's immutable review head (or merged tag/SHA), never the moving default
branch. The shared repository tree includes unchanged files and pinned BOM
children and its shared file previews. Multiple assemblies
remain separate entry points; membership does not invent a BOM hierarchy. A
part-only PR says there is no assembly member. Member-scoped PR pages retain
all snapshot links and a return to the whole PR. Full-SHA item routes select
revision geometry only when `revision.git_commit` matches exactly. Unmatched
historical snapshots show their repository tree without borrowing the latest
revision’s geometry or revision actions.

**Sides and colours.** Every surface that shows two revisions together names
them **old** and **new** (never base/head), in one helper —
`PLM_DIFF.sideLabel(side, 'short'|'full', path)` over `PLM_DIFF.sides(ctx)`,
which takes what the page knows (`ctx.sides`: the change page's `review_base`
and member head) over what git resolved (`ctx.cmp.base/head`). The visible
label is short — swatch + `old v0.4.0` / `new v0.4.1` (the word alone without
a version); the full line `old · v0.4.0 · draft/v0.4.0 @ 07f61ab · 2026-10-13 ·
assembly.stl` is the tooltip and opens under the legend's ⓘ (a tag equal to
the version is not repeated; a ref that is only a sha shows as the commit).
The Overlay's Bodies list reads `old · assembly.stl` / `new · assembly.stl`.
Four CSS tokens on `web/index.html`'s `:root` carry every diff colour, read by
three.js and the pixel-difference canvas through `PLM_DIFF.tok`:
`--plm-old` #3E6FB0 (steel blue) and `--plm-new` #CC7A12 (amber) answer
*which revision*; `--plm-removed` #CF222E and `--plm-added` #1A7F37 (GitHub's
red/green) answer *what happened* to volume, lines and pixels. The side hues
are deliberately not red/green. The one legend — `■ old v… ■ new v… ■ removed
■ added ⓘ` — sits in the Overlay bar and panel, the compare page header and
the proposal header. In the Overlay the new body is amber and the old one a
blue ghost; where one surface stands proud of the other (a per-pixel
comparison of the two bodies' depths, `vCompareOn` in `web/app.js`) old-in-front
is removed (red) and new-in-front is added (green). Stats deltas are green when
they grew and red when they shrank; image and split-diff columns carry the side
colours, and changed text lines keep red/green.

### 2.10 The physical-diff check — a picture of the change (built, 2026-10-16)

Maintainer: *"i'd like to be able to quickly review the physical changes to the
stls"* — and then: *"lets mirror what github does — like it's really meta data
AROUND a pull request."* So the picture is the **output of a check**, exactly as
GitHub's Checks API carries one, and nothing else: no cover image, no
`picture_url` field.

**Field for field (GitHub Checks API → plm):**

| GitHub check run | plm `checks` row |
|---|---|
| `head_sha` | `sha` — for a commit check the commit; for a pull request's own check the **head-set key** (below) |
| `name` | `name` = `physical-diff` |
| `status` queued / in_progress / completed + `conclusion` success / neutral / failure | one `status`: queued · running · pass · warn · skipped (neutral) · fail · error |
| `output.title` / `output.summary` | `summary` (one line: "5 meshes changed") / `details_md` (one line per mesh with volume and bbox deltas, the legend, what did not render) |
| `output.images[] {alt, image_url, caption}` | `images` (JSON, the additive `images_json` column) — same keys |
| `details_url` | the pull request's Checks tab, `/pulls/<n>/checks` |
| `app` | `source` = `builtin` |

**The subject is the pull request.** Every other check measures one commit of
one item; this one measures the pull request across its members, so its row's
`number` is `CHG-n` and its `sha` is a hash of the sorted `(item, head,
review-baseline)` triples (`checks.set_key`). Everything else is the existing
machinery, one more registration (`applies: "change"`): the same queue and
runner thread, the same relay outbox from the MCP process, the same row,
`GET /api/repo/CHG-n/checks` (the board), `POST /api/repo/CHG-n/checks/run`
(Re-run, forces a render), and one new read: `GET /api/repo/<subject>/checks/
<name>/images/<i>.png` (any row's images; 404 + `Retry-After: 10` while it
renders — a finished row of the CURRENT key whose file the cache lost is
re-rendered on demand, once: the route claims the row `queued` and only the
claim that wins enqueues; a superseded key is 410, never rendered). The
subject is resolved in one place, `checks.subject()`.

**When.** Asked for, key-less, on submit (`changes.on_submitted`) and whenever
the head set may have moved — a member joined, was re-pushed or re-baselined
(`changes.recheck`, the hook that already re-measures `shared-source`) — and on
a page read that finds no row for the current key. Key-less means **idempotent**:
a head set with a finished row and its images on disk is never rendered again;
a moved one gets a new row. A request for a superseded key marks its unfinished
row `skipped — superseded`. Images are derived data under
`PLM_CACHE_DIR/checks-images/CHG-n/<key>/<check name>/<i>.png` (by name, so a
second image-emitting check never overwrites this one; the last 3 sets per
pull request and name; dropped when the pull request closes; the store held
under `PLM_CHECKS_IMAGES_MAX_MB`), never in the DB, never in a repo. The meshes
a picture fetched are cached by (item, commit, path) in `checks-meshes/`
(`PLM_CHECKS_MESH_CACHE_MB`).

**What.** For each member, `gitrepo.compare(baseline, head)`'s `meshes` — the
Files tab's own mesh chooser (renames paired, primary first) — minus the ones
whose bytes did not change (a print mesh and its posed viz with the same bytes
share one tile), plus — when an assembly member moved `parts/*` pins — the
assembly **posed**: each child's mesh at its pin (`Ctx.mesh_path`: viz > mesh >
artifact), the re-pinned ones amber (with their old pose as a blue ghost), the
rest grey ghosts. Formats: what `roles.is_scad_importable` says (STL, 3MF).
Each tile is rendered by `thumbs.render_png` — the thumbnails' own OpenSCAD
call and render slots (PREVIEW mode, the same camera for every tile,
`--viewall` + orthographic) — from a generated `.scad` (`checks.physdiff_scad`,
string-tested); up to 3 tiles at once inside the one job. Captions' bbox and
volume reuse the commit's `geometry-facts` row, else a bbox/volume-only pass. Pillow
composites the tiles under a header ("#483 · title" · legend · "3 members · 5
meshes changed") at 1200 px wide, ≥ 630 tall; up to 12 tiles, "+n more" after.
`images[0]` is the composite, then one image per tile (served at ~440 px; the
800×600 render is the composite's source). Without Pillow the tiles alone are
the images.

**The colours are §2.9's.** New = amber `#CC7A12`, translucent so the change
shows through it; **removed** (old surface proud of new) `#CF222E` and
**added** (new proud of old) `#1A7F37`, solid. Removed = `difference() {old;
grow(eps) new}` and added the reverse, where `grow` unions six ±eps translated
copies (eps = 0.08 mm, or 0.04 % of the bbox diagonal): OpenCSG's depth-buffer
CSG paints the faces both revisions share as stripes, and the grow removes them
while keeping any real change bigger than eps. When the difference is over the
tile budget (20 s) the tile falls back to old = blue ghost `#3E6FB0`, new =
amber, no red/green, and says so. Measured on CHG-483 (3 members, 6 changed
mesh files → 5 tiles): 1.9-4.4 s per diff tile, 0.4 s the posed one, 12-14 s
for the whole picture, under software GL.

**The verdict.** `pass` = every changed mesh rendered; `warn` names the ones
that did not (an LFS object missing, a format OpenSCAD cannot import, the
2-minute budget); `skipped` = no mesh changed ("no geometry changed — text/BOM
only"). Advisory — never required by policy.

**Where it shows, GitHub's way.** The Checks tab (the pull request's own board
first, its images under the row, the composite large); the Conversation tab's
checks card has the row "physical-diff — 5 meshes changed · Details"; the
timeline gets the bot's line ("plm-checks · physical-diff passed · 5 meshes
changed") with the first image inline (event `checks.completed`). The bell and
banners carry no image. `review.requested` carries the rows in `checks`
(`image_url` absolute). MCP: `get_change` lists the checks (images in the full
form, `compact=false`); `get_checks('CHG-n')` is the board with its images.
`python -m plm.checks CHG-n [--force]` runs it in-process for an operator.

**The hero card.** The Conversation tab opens, right after the description,
with the picture of the *completed* thing (the maintainer: "can a pull request by
default show the visual diff of the completed assembly as one of the first
cards"): for each root member — one no other member waits on — the tile of its
primary mesh (`primary: true` in `facts.tiles`; rows from before the flag use
the first non-assembly diff tile, then the posed assembly). `checks.hero_tiles`
picks it, `GET /api/changes/…` carries it as `checks.hero`
(`{item, path, mode, label, image_url, old, new, compare_mesh}`), and
`web/changes.js` draws it with the legend, the volume/bbox delta, "open in 3D"
(the compare page's `?mesh=`) and "all tiles →" (the Checks tab). While the
check is queued or running a slim "rendering the visual diff…" card stands in;
when it is skipped there is no card.

## 3. What this deliberately does not take

- **From Teamcenter:** a workflow *designer* and per-site state machines (policy
  is a table, not a diagramming tool); check-out locks on text (branches do it;
  LFS locks only for opaque CAD); revision-level signoffs detached from bytes.
- **From forges:** fork-to-propose (AGit `refs/for/main` needs no fork);
  reactions, stars, project boards (an external tracker handles work); approvals by bots;
  merge queues (one approver, no contention).

## 4. Build order (each ships alone)

0. **Release as its own object** (§2.1b): promote an existing tagged revision,
   human-only, notes + evidence — and approval policy by maturity for proposals
  .
1. **Proposal object + SHA-bound reviews + stale dismissal** — the backbone;
   falls out of the stage-3 write path.
2. **Proposal page**: timeline + the existing compare view + Approve/Reject,
   phone-sized. Notification with the link.
3. **Threads** on proposal / file-line / manifest-key (extends `context`), then
   the **3-D anchor** — i.e. build `PLM_MARKUP.md` on the git anchor.
4. **Checks** table + manifest/BOM/pins checks, then render-matches-source.
5. **Policy by maturity** (required checks, release-pin rule, blocking threads).
6. **Changes** (ECO) over proposals + impact analysis + bump-proposal bots.
7. Release status + notes mirror; issues / problem reports.

## 5. Open questions for the maintainer

1. ~~Squash by default~~ — resolved: no (§2.1); pushed commits are preserved.
2. **Blocking threads** (§2.4): opt-in per thread, approver can override with a
   reason — or keep August's "never blocks" absolutely?
3. **Bot reviewer**: want an agent reviewing every proposal automatically
   (advisory, never counts)?
4. **Where do notifications go** — Slack, iMessage, both; and should "Approve"
   work from the phone without a laptop?
