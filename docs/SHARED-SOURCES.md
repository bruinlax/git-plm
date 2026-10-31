# Shared sources — one source file, many repos

Stage 15 (2026-10-05). Code: `plm/sources.py`; the
`shared-source` and `source-present` checks in `plm/checks.py`; `lib/<N>` at
the push door (`plm/gitvalidate.py`), in keep-refs (`plm/gitkeep.py`), in the
index (`plm/gitindex.py` → `source_pins`) and in changes (`plm/changes.py`:
impact, merge order, re-pin).

## The problem, as a cold agent met it

The migration put each revision's generating source INTO that revision's repo
— the right default (a clone must regenerate itself). But a parametric model
that drives many items became many copies. Cold run 1 was asked to lengthen
the camper rack's rear cross rail by 10 mm. It changed `rack_y` in
`camper_rack_v1_as_built.scad`, updated the copy in the rail, the weldment and
the top assembly (and the project folder), and submitted. Nothing told it — or
The maintainer — that seven other items still carry the old file and will regenerate
the old rack.

## Measured on the 2026-10-05 prod snapshot (`rehearsal-15/repos-a`)

Every design item's shipping ref (`main`, else its newest draft); a SOURCE is
`[source] file`, `[files].source`, or any `.scad` in the tree.

| | |
|---|---|
| design items with a repo | 202 |
| … with a source file | 149 |
| source BLOBS carried by ≥ 2 items (same path, same bytes) | **24** |
| items carrying at least one shared blob | **82** of 149 |

Per project:

| project | items | with a source | shared blobs | items carrying one | paths in drift* |
|---|---|---|---|---|---|
| camper-conduit-rack | 53 | 53 | 8 | **45** | 2 |
| lantern-gobo-bearing | 82 | 49 | 11 | 22 | 7 |
| gate-status-indicator | 27 | 19 | 2 | 5 | 3 |
| bike-projector | 11 | 11 | 1 | 4 | 1 |
| burning-man-luminous-tree | 6 | 5 | 1 | 3 | 1 |
| swd-probe | 4 | 4 | 1 | 3 | 0 |
| lounge-mattress-backrest | 7 | 6 | 0 | 0 | 1 |
| beach-chair-cover | 6 | 1 | 0 | 0 | 0 |
| burning-man-light-field | 1 | 1 | 0 | 0 | 0 |

\* the same path at two or more different blobs across the project's items —
mostly items released at different times; each is a place a shared source
already diverged.

The biggest families (items sharing one blob at one path):

| items | project | source | blob |
|---|---|---|---|
| 10 | camper-conduit-rack | `camper_rack_v2.scad` | 179b044870 (+ 5 more items at 4a0f2d9442, 2 singles) |
| 10 | camper-conduit-rack | `camper_rack_v1_as_built.scad` | 45c302830e |
| 9 | camper-conduit-rack | `receiver_arm_drill_jig.scad` | c7fc90f874 (+ 4 at 5b9b89f6d6) |
| 4 | lantern-gobo-bearing | `suspension_post_hanger_interface.scad` | 6d802802d6 |
| 4 | lantern-gobo-bearing | `cad/lantern_gobo_bearing.scad` | dea2cf417e |
| 4 | lantern-gobo-bearing | `cad/kinetic_moire_lantern.scad` | 96f972daaf |
| 4 | bike-projector | `groundlight_hardware.scad` | ac7c217455 |
| 3 | swd-probe | `swd_probe_case.scad` | 261565694b |

(The census is plain `git` over the bare repos, stdlib only.)

## Layer 1 — now, for every project, no migration: blob identity

When a change modifies (or removes) a source file, every OTHER item of the
project that carries the same path at the PRE-change blob is affected, exactly
like a parent that pins a changed child:

- `get_change` → `impact` lists it, kind `shared-source`: "CCR-PRT-0015 carries
  `camper_rack_v1_as_built.scad` at the pre-change blob 45c302830e (changed by
  CCR-PRT-0017, CCR-ASM-0008, CCR-ASM-0001) — not in this change."
- Dispositions are the existing ones: `bump` reads **re-vendor in this
  change** (then push it with `-o change=CHG-n`; the pre-flight refuses a
  `bump` with no member), `use_as_is`, `no_impact`. `draft_dispositions`
  drafts `use_as_is` with the facts; a human confirms. A MEMBER whose head
  still carries the old copy is flagged on the change ("re-vendor it").
- The `shared-source` check (advisory) on every commit: each source file and
  who else in the project ships the same path — the same blob (in step) or a
  different one (DRIFT).

Proved on a COPY of the reseeded POC: run 1
reproduced the way the new instructions say — the three pushes accepted with
one `ℹ` line each, `get_change` lists the 7 other carriers, `draft_dispositions`
drafts all 7, `submit_change` (no waiver) answers `say: CHG-22 is waiting for
your approval`, pre-flight clean.

## Layer 2 — the pattern: the source is an item, consumers pin it at `lib/<N>`

A shared parametric source becomes ONE item; each consumer pins it as a git
submodule at `lib/<NUMBER>` — the same mechanism as a BOM child at
`parts/<NUMBER>`, with the same rules:

- **push door**: `lib/<N>` must be an item with a repo, the pinned commit must
  exist there, `.gitmodules` must declare it with a relative URL; a `[[bom]]`
  line pointing at `lib/` is REFUSED (a source is not product structure);
- **keep-refs**: a `lib/` pin is kept reachable like any gitlink;
- **index**: a SOURCE pin (`source_pins`: consumer revision, path, source item,
  commit, version) — never a BOM line, never in rolled quantities or build
  plans; printed in the push output (`plm:   source    lib/… [a SOURCE pin —
  not a BOM line]`);
- **`[source] file = "lib/<N>/<file>.scad"`**: `source-present` and
  `render-matches-source` resolve it in the pinned source repo (the render
  extracts each `lib/` pin — `git archive` leaves a submodule empty);
- **changes**: a consumer pinning the source's DRAFT head is ordered after it
  and re-pinned to its release tag at merge (`Plm-Repin: lib/<N> <draft> ->
  <tag sha>`), and a change to the source lists every consumer through
  where-used impact.

### Which kind: `document` — no new kind

"Kind is a fact" asks what the thing IS. A parametric source library is
controlled information that other items reference and regenerate from; it is
never built, bought, assembled or printed itself. That is exactly the
behaviour class of `document` (DOC): non-physical, referenced, versioned and
released like everything else, absent from build plans and purchasing. A new
`source` kind (class SRC) would add a class token to `config.KIND_BY_CLASS`
and an allow-list decision at every surface — for zero difference in
behaviour today — and change the class menu master serves in `off`. Revisit
only if a source ever needs behaviour a document must not have (e.g. its own
render check without a mesh).

### A library another project owns

Stage 16: `swd_probe_case.scad` (SWD-PRT-0001/0002,
SWD-ASM-0001) does `include <../../gate-status-indicator/cad/gate_oled_puck.scad>`
— a library that lives in ANOTHER project's folder. Measured on the POC
(2026-10-05 snapshot): six gate-status-indicator items vendor a
`gate_oled_puck.scad` (GSI-ASM-0001, GSI-PRT-0004/0009/0010/0011/0012), in
**five different versions**, and every one of them `include`s
`ws2812_ring.scad`, which no repo holds — it is registered only as a vault file
of GSI-PUR-0002, a purchased item (catalog kinds have no repo, so nothing can
pin it). Run 3 found the file by hand and copied both into three swd-probe
repos: three more copies, a sixth version waiting to happen.

**Decision — the pin for a cross-project library:**

1. **Its home is a `document` item in the project that OWNS it** (here:
   gate-status-indicator), holding the library AND what it includes
   (`gate_oled_puck.scad` + `ws2812_ring.scad`). Never a part that happens to
   vendor it: pinning a part ties every consumer to that part's revisions
   (each gobo-plate tweak would move the library) and drags its meshes into
   every clone.
2. **A consumer in another project pins a RELEASE TAG** of it —
   `git submodule add ../GSI-DOC-0001.git lib/GSI-DOC-0001 && git -C
   lib/GSI-DOC-0001 checkout v0.1.0` — never a draft head or a branch tip.
   Within ONE project a consumer may pin the library's draft head inside the
   same change (the merge re-pins it to the tag, stage 14); across projects
   the two are different decisions by different owners, so the pin must be a
   thing that cannot move.
3. **Moving the pin is the consumer's change**, made deliberately (the
   library's own change lists every consumer — any project — through
   where-used impact, with a disposition each, exactly like a parent).
4. Until the home exists, the consumer keeps its out-of-repo include and SAYS
   so; it does not copy. `source-present` prints exactly this: it names every
   holder (ref, tag, blob), says when the copies DIFFER, checks whether a
   holder's copy is complete (its own includes resolve in its own repo), and
   prints the `git submodule add` + include line for the best complete holder
   (a `document` first) — or, when no copy is complete, "the library needs a
   home first … do NOT copy the files into this repo"; when NO item holds
   the file, "add the file and say so". An include that was already outside
   in the release the draft started from is marked **pre-existing** — not the
   change's to fix.

Making GSI's home is a gate-status-indicator change the maintainer approves (create the
document item, push the two files, release v0.1.0, then the consumers pin it);
it was not done here — the swd-probe change under review (CHG-66) is left as
the agent made it.

### The migration recipe for one family — proved on a copy

`camper_rack_v1_as_built.scad`, the family of 10:

```bash
# 1. the source becomes an item — plm owns the number
create_item(name="Camper rack v1 as-built record model (shared source)",
            kind="document", project="camper-conduit-rack", prefix="CCR")   # -> CCR-DOC-0001
git clone <base>/CCR-DOC-0001.git work/CCR-DOC-0001 && cd work/CCR-DOC-0001   # draft/v0.1.0
git show <a consumer>:camper_rack_v1_as_built.scad > camper_rack_v1_as_built.scad
#    plm.toml: [source] file = "camper_rack_v1_as_built.scad"; [files] source = [...]
git commit -am "Shared source: the camper rack v1 as-built record model, one home"
git push -o change=new -o title="Camper rack: one shared source" origin HEAD:refs/heads/draft/v0.1.0
# 2. each consumer, CHILDREN BEFORE PARENTS (a parent in the change pins each
#    member child's draft head — stage 14's rule)
git switch -c draft/shared-source origin/main
git rm camper_rack_v1_as_built.scad
git submodule add ../CCR-DOC-0001.git lib/CCR-DOC-0001
git -C lib/CCR-DOC-0001 fetch origin draft/v0.1.0 && git -C lib/CCR-DOC-0001 checkout <its head>
#    plm.toml: [source] file = "lib/CCR-DOC-0001/camper_rack_v1_as_built.scad";
#    drop it from [files].source; ONE patch bump of `version`; nothing else
git commit -am "Generate from the shared source lib/CCR-DOC-0001 instead of a vendored copy (geometry unchanged)"
git push -o change=CHG-n origin HEAD:refs/heads/draft/shared-source
# 3. submit_change -> a human approves ONE decision
```

What it produced on the copy: 11 members (the source + 10 consumers), merge
order `CCR-DOC-0001` first, then each part before its weldment, `CCR-ASM-0001`
last; 20 re-pins (10 `lib/` + 10 `parts/`); every consumer's checks green on
the new source path (`source-present` pass, `render-matches-source` pass
0.000 %); `source_pins` 10 rows, BOM lines pointing at the source 0; approved
by one decision — `CCR-PRT-0017 v1.0.1`'s release commit pins
`lib/CCR-DOC-0001` at exactly `CCR-DOC-0001 v0.1.0^{commit}` (`Plm-Repin: lib/
CCR-DOC-0001 <draft> -> <tag> v0.1.0`) and `git clone --recurse-submodules`
of the rail carries `lib/CCR-DOC-0001/camper_rack_v1_as_built.scad`. A later
change to the SOURCE alone (`rack_y +10 mm` on `CCR-DOC-0001` v0.1.1) lists
all 10 consumers in its impact panel through where-used — the run-1 situation,
now complete by construction.

## Not done (deliberately), and open

- **No mass migration.** 24 shared blobs, 82 items. The recipe is per family,
  a change the maintainer approves; Layer 1 tells everybody where drift is meanwhile.
- **§5.1 does not gate `lib/` pins** (a RELEASED consumer may pin a source at a
  draft commit — the merge re-pins it when both are in one change). Recommend:
  include `lib/` in `release_gate` once a family has been migrated for real —
  and for a CROSS-PROJECT library (above) make "the pin is a release tag" a
  refusal at RELEASED, since no change of the consumer's will ever re-pin it.
- **Where the development copy lives.** A project folder that keeps the
  source at its root (the camper rack does) should point at the item once a
  family is migrated; until then the instructions allow copying an edited
  shared source back there, uncommitted, and saying so.
- **Includes between sources** (`include <lib/CCR-DOC-0001/x.scad>` from
  another source item) work — a `lib/` pin inside a source item is a pin like
  any other — but no family needs it yet; not exercised.
