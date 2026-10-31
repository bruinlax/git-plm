# Reading a part with git — the git-first agent guide

**Layout: git-first POC (branch `git-first`, 2026-09-17).** Every item is a real
repository whose **tree IS the revision**. Releases are tags, drafts are
branches, BOM children are submodules.

> **The old version of this file is gone, deliberately.** It documented the
> *August replay layout* — `versions/vX.Y.Z/` folders, `git diff -M -C
> --find-copies-harder`, and the `refs/for/main` push flow — and an agent that
> found it would follow instructions that are wrong for these repos. It still
> exists where it is still true: on branch `main`, which is what production's
> `plm-git` (:8099) and `plm-git-sandbox` (:8199) serve. `git show
> main:plm-git/docs/AGENT-INSTRUCTIONS.md` if you need it.
>
> **Tell the two apart in one command:** if a clone has `versions/v0.3.1/` in it,
> you are on the old service. If the repo root holds `plm.toml` and the part's
> files directly, you are here.

**Reads are anonymous. Writes need a token — and a push is a real write.**
Since stage 3 this transport accepts `receive-pack`, and *what you push is
kept*: plm does not re-author your bytes, so the SHA you pushed is the SHA of
the revision. See **[Pushing a revision](#pushing-a-revision)** below. A clone is
still a read: it locks nothing and changes nothing.

| Surface | Where |
|---|---|
| git smart HTTP (clone) | `http://localhost:8194/git/<NUMBER>.git` |
| web UI + read API | `http://localhost:8094` (`/api/repo/*`, see `the repo API reference`) |
| MCP | `http://localhost:8096/mcp` — `get_item`, `list_refs`, `compare_revisions` |

---

## The model in five facts

1. **An item is a repo.** `<clone_base>/<NUMBER>.git`. Purchased and hardware
   items are the exception: they have no repo of their own and live as one TOML
   each inside `catalog.git`.
2. **Releases are annotated tags** `vX.Y.Z`. The annotation carries the state,
   the maturity and the release notes.
3. **Drafts are branches** `draft/vX.Y.Z` — DRAFT and IN_REVIEW revisions, several
   at a time, each branched off the revision it was revised *from*.
4. **`main` is released work only, and may not exist.** 59 items here have no
   release at all; their HEAD points at the newest draft branch. Never hard-code
   `main` — use `default_ref` from `get_item(...).git` or `list_refs(...)`.
5. **The tree is the revision.** No version folders. `git diff v0.4.0 v0.4.1` is
   the real design change.

…and two refs that are not fetched by a default clone, because facts that are
not *about one revision's tree* do not live in one (stage 3c):

6. **`refs/plm/item` holds `item.toml`** — the item's name, kind, project, the
   product flag, item-level tags and relations. A rename is ONE commit there,
   whatever the branch topology, and `git log refs/plm/item` is every rename
   with who and when. The copy in a revision's `plm.toml` is a **snapshot** so a
   zip of a tag still knows what it is; when they disagree the item ref is
   right, plm corrects the snapshot in the release commit, and a push that has
   the old name gets a LINT, not a refusal. You may not push to it.

   ```bash
   git fetch origin refs/plm/item:refs/plm/item
   git show refs/plm/item:item.toml
   ```

7. **`refs/notes/plm` holds what arrived after a revision was frozen** — the
   mass the slicer measured on the first real print, an `unverified-datum`
   caveat, a BOM line suppressed on one build, and the lifecycle decision itself
   (`[status]`: is this revision RELEASED *now*, or obsolete, or on hold?). A
   note never changes a tree or moves a tag, so `git cat-file -p v0.4.0` is the
   same bytes before and after.

   ```bash
   git fetch origin refs/notes/plm:refs/notes/plm
   git log --notes=plm main
   ```

   ⭐ **The release gate reads `[status]`, not the existence of a tag.** A
   PROTOTYPE tag is still a tag. That is why a parent can be refused promotion
   by a child that looks released.

## Vocabulary — both words for the same thing

| plm says | git says |
|---|---|
| item | repository |
| revision, **release** | commit, **annotated tag `vX.Y.Z`** |
| **draft** (DRAFT / IN_REVIEW) | branch `draft/vX.Y.Z` |
| BOM child (occurrence) | **submodule** at `parts/<NUMBER>`, gitlink = the pin |
| purchased / hardware line | a TOML in the `catalog` submodule |
| what plm knows about the part | `plm.toml` at the repo root |
| state / maturity | tag annotation, overlaid by `[status]` in `refs/notes/plm` |
| item master data (name, kind, project) | `item.toml` on `refs/plm/item` |
| purchased / hardware ITEM | `<NUMBER>.toml` in `catalog.git` — no repo of its own |
| **release** (the promotion) | a decision, recorded as a note — **no ref moves** |

---

## A worked session — real commands, real output

### 1. Where is the part? (MCP)

`get_item("KML-PRT-1012")` now carries a `git` block:

```json
"git": {
  "clone_url": "http://localhost:8194/git/KML-PRT-1012.git",
  "default_ref": "main",
  "default_release": "v0.4.1",
  "releases_count": 5,
  "drafts_count": 0,
  "browse_url":  "http://localhost:8094/#/item/KML-PRT-1012/ref/v0.4.1",
  "compare_url": "http://localhost:8094/#/item/KML-PRT-1012/compare/v0.4.0...v0.4.1"
}
```

`browse_url` is the link you hand the maintainer. `compare_url` is "what changed in the
latest release", already pointed at the right pair.

### 2. Clone it and look at the releases

```console
$ git clone --recurse-submodules http://localhost:8194/git/KML-PRT-1012.git
$ cd KML-PRT-1012 && ls
INSTRUCTIONS.md
KML-PRT-1012_v0.4.1_PRIMARY-VIEW_round-enclosed-flange-inserts.stl
plm.toml
README.md
thin_carrier_round_enclosed_flange_inserts.scad

$ git tag
v0.1.0
v0.2.0
v0.3.0
v0.4.0
v0.4.1
```

The whole part is at the root — that is the point. `git log --oneline` is the
revision ledger, verbatim from plm's check-ins:

```console
$ git log --oneline
abda201 Correct revision packaging and make the KML-PRT-1013 flange mounts explicitly round: …
65b5ade Replaces incomplete Ø90 captive-nut cutouts with complete Ø10 local bosses …
c652e6f Replaces Ø96 through holes with bottom-loaded M3 inserts and blind screw relief
2a721ec Restores 113 by 113 by 10 mm carrier with internal manifold overlaps
29666a3 Creates manifold 21 mm flush carrier with independent underside shell …
```

The tag annotation carries the lifecycle words, and the commit carries trailers:

```console
$ git show --no-patch v0.4.1
tag v0.4.1
Tagger: plm-split <plm@example.com>
Date:   Wed Aug 12 17:56:25 2026 +0000

KML-PRT-1012 v0.4.1 — PROTOTYPE

Maturity: prototype
State: PROTOTYPE
…
    Plm-Item: KML-PRT-1012
    Plm-Version: 0.4.1
    Plm-State: PROTOTYPE
```

### 3. "What changed in the source between v0.4.0 and v0.4.1?"

```console
$ git diff v0.4.0 v0.4.1 --stat
 INSTRUCTIONS.md                                    |     4 +-
 ..._PRIMARY-VIEW_round-enclosed-flange-inserts.stl | 91604 +++++++++++++++++++
 plm.toml                                           |    43 +-
 ...thin_carrier_round_enclosed_flange_inserts.scad |    21 +-
 4 files changed, 91647 insertions(+), 25 deletions(-)

$ git diff v0.4.0 v0.4.1 -- '*.scad'
diff --git a/thin_carrier_pulley_insert_fix.scad b/thin_carrier_round_enclosed_flange_inserts.scad
similarity index 78%
rename from thin_carrier_pulley_insert_fix.scad
rename to thin_carrier_round_enclosed_flange_inserts.scad
@@ -13,6 +14,7 @@ pulley_insert_depth = 6.7;
+pulley_insert_facets = 96;
@@ -39,7 +41,8 @@ module pulley_insert_boss(angle) {
         cylinder(d = pulley_insert_boss_diameter,
-                 h = carrier_total_thickness);
+                 h = carrier_total_thickness,
+                 $fn = pulley_insert_facets);
```

Twenty-one lines, and they say the change: the insert bosses and bores gained
`$fn = 96` so the mounts are explicitly **round** rather than faceted. That is
the answer, from stock git, with no flags.

Two things to read correctly:

- **The 91 604-line STL is noise.** ASCII meshes diff as text. Filter with a
  pathspec (`-- '*.scad'`) or read `binary: true` off the API/`compare_revisions`
  and ignore the line counts.
- **`git diff`'s own rename detection is on and is right** — v0.4.1 renamed its
  source file, and git says so. What is *gone* is the `-M -C
  --find-copies-harder` incantation the version-folder layout needed. Do not
  pass it; the API deliberately runs `--no-renames` for the same reason.

A clean text-only example with no rename, if you want one:
`GSI-PRT-0004`, `git diff v0.2.0 v0.3.0 -- gate_light_puck.scad` → +44 / −12 on
a stable filename.

### 4. `plm.toml` — what plm knows, travelling with the clone

```toml
schema = 1
number = "KML-PRT-1012"
name = "Thin rotating carrier"
kind = "part"
version = "0.4.1"
state = "PROTOTYPE"
maturity = "prototype"
summary = "Correct revision packaging and make the KML-PRT-1013 flange mounts explicitly round: …"

[source]
file = "lantern_gobo_bearing.scad"
selector = "part=inner_ring"

[files]
primary = "KML-PRT-1012_v0.4.1_PRIMARY-VIEW_round-enclosed-flange-inserts.stl"
source = ["thin_carrier_round_enclosed_flange_inserts.scad"]
doc = ["INSTRUCTIONS.md"]

[[files.external]]
name = "KML-PRT-1012_v0.4.1_round-enclosed-flange-inserts.stl"
role = "artifact"
backend = "filedrop"
key = "plm/KML-PRT-1012/v0.4.1/KML-PRT-1012_v0.4.1_round-enclosed-flange-inserts.stl"
sha256 = "14ceff0c338d80ec08995b23483bba17acbdb29d6e812cc5dadf7b18d3de4f3b"
size = 2204359

[attributes]
mass_g = { value = "26.66", unit = "g" }
print_time_min = { value = "56.2", unit = "min", source = "the shop-floor service orca 2.4.2, 0.20mm Standard, Elegoo PLA" }
```

Sections: `[source]` (how to regenerate), `[files]` + `[[files.external]]`,
`[attributes]`, `[[bom]]`, `[[relations]]`, `[[routing]]`, `tags`.
`git show v0.4.1:plm.toml` reads it at any ref without checking anything out.

**`[[files.external]]` files are NOT in the clone.** They were offloaded to
filedrop. A clone that looks like it is missing its STL is not broken. Fetch by
*name* through plm — the manifest at that ref is the authorisation, so no
filedrop credential is involved:

```
http://localhost:8094/api/repo/KML-PRT-1012/external?ref=v0.4.1&name=<name>
http://localhost:8094/api/repo/KML-PRT-1012/blob?ref=v0.4.1&path=<path>
```

### 5. An assembly — the BOM is submodules

```console
$ git clone --recurse-submodules http://localhost:8194/git/KML-ASM-1000.git
$ cd KML-ASM-1000 && git submodule status
 e854774232228e87d583410f169c4c47b004b901 catalog (e854774)
 b75306ecfc0fc508d329758a0470699d8719db65 parts/KML-ASM-1100 (v0.4.0)
 dc669d64fea26cca10ad4adf95beb653d5e00198 parts/KML-ASM-4000 (v0.9.0)
 02a1bb6bf4aa512a49fb5530e6090cd9bdd44ffa parts/KML-PRT-1001 (v0.6.0)
 c69c51c034e5c7adfe6fdcf3a89c27a66ce04c34 parts/KML-PRT-1013 (v0.2.0)
```

Each `parts/<NUMBER>` is the child repo, **pinned by the gitlink** — that pin is
the BOM version, and it is the only home for it. The matching `[[bom]]` lines
carry everything a gitlink cannot:

```toml
[[bom]] find = 10,  path = "parts/KML-PRT-1001",     qty = 1.0, unit = "EA", notes = "Stationary flange with direct-seat M8 axle-head notches"
[[bom]] find = 20,  path = "parts/KML-ASM-1100",     qty = 1.0, unit = "EA", notes = "Reusable cage and bottom-insert carrier module"
[[bom]] find = 90,  path = "catalog/KML-PUR-0004.toml", qty = 1.0, unit = "EA", notes = "280 mm closed-loop GT2 belt"
[[bom]] find = 100, path = "catalog/KML-HDW-0023.toml", qty = 4.0, unit = "EA", notes = "M5 x 55 top-down cage and deck screws"
```

*(shown one line per row for brevity; in the file each `[[bom]]` is its own
table.)* **No `version` key** — the gitlink is the pin.

`catalog/` is **one** submodule holding all 86 purchased + hardware items, one
TOML each (no repo per screw). It has no tags, so `git submodule status` shows
its short SHA rather than a version; it is pinned to "the catalog as it stood
when this revision was made".

```console
$ cat catalog/KML-PUR-0004.toml
number = "KML-PUR-0004"
name = "280 mm closed-loop GT2 belt"
kind = "purchased"
[[source]]
supplier = "Amazon"
mpn = "280 mm GT2 closed loop 6 mm wide"
url = "https://www.amazon.com/dp/B07RK1TX8C"
status = "PREFERRED"
```

**The BOM delta, with the children's reasons** — the thing nobody had to build:

```console
$ git diff --submodule=log v0.14.4 v0.16.1 -- parts catalog
Submodule parts/KML-ASM-4000 ed90923..96ced4e:
  > Bind KML-FW-4001 v0.6.5 confirmed selected-overlay scene; mechanics, wiring, and binary unchanged
  > Bind KML-FW-4001 v0.6.4 self-clearing four-point LED scene; hardware and geometry unchanged
```

That is the child's own commit subjects between the two pins. `git -C
parts/KML-ASM-4000 log --oneline` goes deeper.

Sharp edges, all three real:

- **Without `--recurse-submodules` the `parts/*` directories are empty.**
- Submodules check out **detached HEAD**. That is normal; you are at a pin.
- A pin that is not on a tag is an **unreleased child**, displayed Go-style
  (`0.3.2-0.20260819-a1b2c3d`). A revision cannot be released above its
  least-mature child.

### 6. An item with no release

```console
$ git clone http://localhost:8194/git/CCR-ASM-0004.git && cd CCR-ASM-0004
$ git tag                      # nothing: no release
$ git branch -r
  origin/HEAD -> origin/draft/v0.3.0
  origin/draft/v0.1.0
  origin/draft/v0.2.0
  origin/draft/v0.3.0
$ git rev-parse --abbrev-ref HEAD
draft/v0.3.0
```

There is no `main`. Code that assumed one would have 404-ed on 59 of the 178
repos. Use `default_ref`.

---

## git or MCP?

They do not overlap. Guessing is how an agent invents a fact.

| You want | Use |
|---|---|
| a part's files at any revision | `git clone`, `git show <ref>:<path>` |
| what really changed between two revisions | `git diff <base> <head>` |
| who changed this line, and in which revision | `git blame`, `git log -p` |
| the last revision that printed cleanly | `git bisect` |
| this part's releases / drafts / history | `git tag`, `git branch -r`, `git log` |
| the BOM delta with the children's reasons | `git diff --submodule=log` |
| **find** parts (symptom, reason, free text) | `search()`, `find_items()` |
| where-used — who consumes this part | `get_item()` → `where_used` |
| a **rolled-up** BOM with multiplied quantities | `get_bom(rolled=True)` |
| the approval queue, submitting, deciding | `pending_approvals()`, `submit_for_release()` |
| a **new item** and its **number** | `create_item()` — plm owns numbers |
| attributes, typed relations, context (the why) | `get_item()`, `set_attributes()`, `add_context()` |
| anything cross-repo or registry-wide | the MCP — a clone is one part |
| **every write** | the MCP |

Two MCP tools sit on the boundary, for when a clone is more than the question
deserves:

- **`list_refs(number)`** — releases and drafts with commit, date, state,
  maturity and summary; `default_ref`, `default_release`, counts, `clone_url`.
- **`compare_revisions(number, base, head)`** — per-file status and line counts,
  the `plm.toml` delta (fields, attributes, BOM, file roles, relations, routing,
  tags), submodule pin changes with the child commit subjects, and
  `git log base..head`. Patch text is capped at 2 000 bytes per file and 12 000
  in total: for the full diff, clone and run `git diff`.

---

<a id="pushing-a-revision"></a>
## Pushing a revision — a worked session

**The one sentence: a pushed commit IS the revision.** plm does not re-author
your bytes into a layout of its own. It keeps the commit and *indexes* it —
reads `plm.toml` and the tree at that SHA and brings the registry up to date. So
`revisions.git_commit` is the SHA you pushed, and `git log` is the revision
ledger, not a mirror of one.

### The credential

Reads are anonymous. A write is attributable or it is refused: plm records the
pushing actor as the revision's author, and "anonymous" is not somebody you can
ask about a design decision.

```bash
export PLM_TOKEN=…                       # a plm bot/personal token
git -c http.extraHeader="Authorization: Bearer $PLM_TOKEN" push …
# or, once per clone:
git config --local http.extraHeader "Authorization: Bearer $PLM_TOKEN"
```

### The session

```console
$ git clone http://localhost:8194/git/GSI-PRT-0004.git && cd GSI-PRT-0004
$ git switch -c draft/taller-pillars
$ $EDITOR gate_light_puck.scad plm.toml     # bump version = "0.7.0"
$ git commit -am "Raise ring pillars 1 mm for the soldered-header clearance"
$ git push origin HEAD:refs/heads/draft/taller-pillars
remote: plm-git: GSI-PRT-0004 v0.7.0 looks like a valid revision (minor from v0.6.0, 5 file(s), 0 gitlink(s)) — indexing it
remote:
remote: plm: indexing GSI-PRT-0004 c435cc2950 from refs/heads/draft/taller-pillars
remote: ──────────────────────────────────────────────────────────────────────
remote: plm: DRAFT v0.7.0 (revision id 621) — the commit you pushed IS the revision, c435cc2950
remote: plm:   summary   Raise ring pillars 1 mm for the soldered-header clearance
remote: plm:   files     5 (backend `git` — nothing was copied anywhere)
remote: plm:   attrs 10  routing 0  tags 0  relations 0
remote: ──────────────────────────────────────────────────────────────────────
remote: draft:     GSI-PRT-0004 v0.7.0 on refs/heads/draft/taller-pillars
remote: submit it when you want a decision:
remote:   git push origin HEAD:refs/for/main          # -o title=… -o maturity=…
remote: review:    http://localhost:8094/#/item/GSI-PRT-0004
remote: compare:   http://localhost:8094/#/item/GSI-PRT-0004/compare/main...draft/taller-pillars
remote:
remote: git push means PROPOSED. Approval is what moves `main` and writes the tag.
 * [new branch]      HEAD -> draft/taller-pillars
```

Re-push the branch as often as you like: it re-indexes the same DRAFT. Delete
the branch (`git push origin :refs/heads/draft/taller-pillars`) and the DRAFT is
withdrawn — that is how you say "never mind".

When it is ready for a human:

```console
$ git push -o topic=taller-pillars -o title="Header clearance" origin HEAD:refs/for/main
remote: submitted: GSI-PRT-0004 v0.7.0  DRAFT -> IN_REVIEW  (approval 15, target PROTOTYPE)
remote: a human approving in plm is the merge; bots never approve.
```

`refs/for/main` is the Gerrit/Gitea AGit convention: it ensures the draft branch
exists and submits it. Push options are honoured — `-o topic=<name>` names the
branch, `-o title=<text>` becomes the submission note, `-o maturity=RELEASED`
asks for a full release instead of PROTOTYPE.

### What the plm.toml has to say

Three fields you must set, and three plm takes from the commit.

```toml
version = "0.7.0"     # exactly one bump over the newest release tag
state   = "DRAFT"     # and delete `maturity` — you ask, the approver answers
number  = "GSI-PRT-0004"   # must match the repo you are pushing to
```

`summary`, `author` and `date` are the **commit's**: `git log` already says what
changed, who and when, and a copy in the file goes stale the first time you
amend. plm indexes git's values and warns if your file disagrees — keep them in
step if you want the file and the registry to read identically.

### Approval is the merge

A human approves in plm (`decide_approval`, or the queue in the browser). That
one operation:

1. fast-forwards `main` onto your commit, or does a real server-side merge
   (`git merge-tree --write-tree`) — a conflict is refused outright, with
   "rebase onto main", rather than half-merged;
2. writes a **release commit** setting `state`/`maturity` in `plm.toml` to what
   was granted, so the tree at the tag says what it IS. Your commit is its
   parent, unmodified;
3. writes the annotated tag `vX.Y.Z` — approver, state, maturity, approval id,
   decision note, date;
4. deletes your draft branch.

```console
$ git pull -p --tags
$ git log --oneline -3
3ceb471 Release GSI-PRT-0004 v0.7.0 as PROTOTYPE
c435cc2 Raise ring pillars 1 mm for the soldered-header clearance   <- yours, untouched
c809fea Release GSI-PRT-0004 v0.6.0 as PROTOTYPE
```

### Who may approve — and when nobody has to (stage 3c)

Two separate questions, both answered by `plm/policy.py`. Read the answer;
do not assume it.

**Does this maturity need a decision at all?** At **concept, design and
prototype: no.** A proposal that validates merges the moment it is submitted,
approved by the POLICY and recorded as such — `approver: policy`,
`Approver-Kind: policy` in the tag annotation, and the words *"no human reviewed
this revision"* underneath it. That is not a loophole; it is the rule the maintainer
wrote, and it is why you can iterate at speed. **RELEASED always needs a
decision.**

**Whose signature counts, when one is needed?** A SETTING, per project and
maturity — not a law. By default `human`; a project may admit agents. Ask:

```console
$ curl -s $PLM/api/policy | jq '.policy[] | {target_state, label, source}'
```

or `approval_policy()` through the MCP. When the answer admits you, approving is
a **real decision**: it is recorded with `approver_kind = bot` and the tag says
an agent decided and no human reviewed it — so say WHY in the note. When it does
not, the refusal names the setting that would change that; that is information
for the human, not something to route around.

Nothing else in the system may create a tag or move `main`.

### Changing a BOM: move the gitlink

There is no BOM verb for this, because the gitlink IS the pin (§5.1):

```bash
git -C parts/GSI-ASM-0002 fetch origin --tags
git -C parts/GSI-ASM-0002 checkout v0.4.1
git add parts/GSI-ASM-0002
```

Keep `[[bom]]` and the gitlinks in step — every line needs a gitlink and every
gitlink needs a line, and the push is refused if they disagree. Drop
`track = "release"` from a line you are pinning **deliberately**: `track` means
"follow the child's releases", so a line that carries it is recorded as
imprecise and the version you just chose is not what the registry stores.

**The release gate (§5.1): you cannot release above your least-mature child.**
An approval at RELEASED is refused while any `parts/*` gitlink is on a commit
that is not a release tag, and the refusal names the offenders. PROTOTYPE only
warns — a prototype may depend on prototypes.

### The refusal catalogue

A `pre-receive` refusal happens while your objects are still in git's
**quarantine**, so nothing is kept: no ref moves, no revision is created, the
registry is untouched. Read the message; it always ends with the fix.

| You did | plm says |
|---|---|
| pushed to `main` | `main` carries released revisions only, and plm is the only thing that moves it |
| pushed a tag | a tag IS a release, and plm writes them (the annotation records who approved it) |
| pushed `refs/heads/<anything else>` | a branch in a part repo means one thing: a proposed revision (§5.2 — a divergent purpose is a **fork**, a new number) |
| pushed `refs/plm/item` | READ it, don't write it — identity is plm's; the refusal names the API verb for each field |
| pushed `refs/notes/plm` | the release gate READS notes, so a note anybody could write would be a gate anybody could open — the refusal names the verb that writes each kind |
| pushed `refs/plm/*` (anything else) | plm's own namespace |
| `plm.toml` does not parse | with the parser's line and column, and the one-liner to check it locally |
| `number` is somebody else's | plm owns identity; `create_item` allocates numbers, a push never can |
| `version` is not a single bump | the three legal next versions, spelled out |
| `version` is already a tag | revisions are immutable; here are the free ones |
| another draft claims that version | **the git-visible PLM conflict** — bump or rebase |
| `state`/`maturity` set on a branch | `git push` never means released (§5.5) |
| `[files]` names a path that is not there | add it, drop the line, or declare it `[[files.external]]` |
| a gitlink the child's repo does not have | a pin nobody can fetch is a BOM line that resolves to nothing |
| a `[[bom]]` line with no gitlink, or the reverse | one says which revision, the other says how many |
| a file over the size cap | **git is the wrong transport above this size** — there is no LFS yet (§6, Phase 4); attach it through plm and declare it external |
| a symlink in the tree | a released tree has to mean the same thing on every machine |
| no token | reads are anonymous; writing needs a name |

Warnings (printed, never blocking): a **new** filename carrying the version or
the item number (§6c-1 — the tag carries the version, the repo carries the
number, so the file is just `floor_plate.stl`), a missing `[source] file`, and a
`summary`/`author`/`date` that disagrees with the commit.

## Two objects: the proposal, and the release (stage 3c)

A **proposal** asks *is this change right?* — a draft branch, a merge, a version
tag, and the policy above decides whether a person has to look.

A **release** asks *is this revision ready to build against?* It acts on an
**existing tag**, changes no files, mints no version, and is always a decision.
**Do not revise a part in order to release it** — that used to be the only way
and it put a version bump in the history that recorded a decision rather than a
design.

```console
$ curl -s -X POST $PLM/api/items/GSI-PRT-0004/releases \
    -d '{"version":"0.7.0","to_maturity":"released","actor":"agent"}'
```
…or `request_release(number, version)` through the MCP. **You may request one,
and usually should**: you are what knows the part was printed and fitted.

What comes back is a **workflow**, not a yes/no:

| | |
|---|---|
| **checks** | `manifest` · `pins-released` — the released-children gate |
| **checklist** | attested lines: built at this revision, fit-checked, BOM purchasable, instructions reviewed |
| **approvals** | a quorum, whose kinds the policy decides |

**Attest the lines you can prove**, with your evidence in the note — that is
exactly what an agent is good for:

```console
$ curl -s -X POST $PLM/api/releases/12/signoffs -d '{
    "kind":"checklist",
    "item":"BOM is purchasable (every buy line has a live source)",
    "verdict":"attest",
    "note":"every catalog/*.toml at this tag has a live [[source]] url",
    "actor":"agent"}'
```

Each signoff is its own row **bound to the tag sha**, so it is a signature over
the exact bytes of the part, its BOM pins and its metadata.

On approval, in this order: the released-children gate, the evidence document
frozen and hashed, and a `[status]` note written on the release commit. **No tag
moves and nothing is committed on `main`** — check it yourself:

```console
$ git fetch origin refs/notes/plm:refs/notes/plm
$ git log --notes=plm -1 v0.7.0
...
    [status]
    state = "RELEASED"
    maturity = "released"
    approver = "alice"
    release = 12
    evidence_sha256 = "5cae2652b3da6e95a9afc93947117870abea0fb3f4ac0b8eafa4173621c24ec3"
$ curl -s $PLM/api/releases/12/evidence | sha256sum      # the same hash
```

The evidence is one deterministic JSON document — the manifest at the tag, the
full pin closure with every descendant's maturity, the approvals that led here,
every signoff, each file's sha256. It is served **byte for byte as it was
hashed**, never re-rendered.

**The gate, if you hit it:** every `parts/*` pin must itself be RELEASED, by
STATUS. Release the child first (the same call, one level down); if its released
revision is a different one, bump the parent's gitlink and push that.

## Check-out and check-in are retired (stage 3c)

- **`checkout` / `release_checkout` lock nothing.** They still exist and still
  succeed so old scripts keep working, and they answer `advisory: true`.
  Branches replaced locks: two agents on one part get two `draft/*` branches and
  a merge, which is better than one of them waiting. **You do not need to call
  them.**
- **`checkin` is a compatibility shim** over exactly what this document
  describes: one commit on `draft/v<version>` carrying the revise, the files and
  the manifest together, then a submission, then the policy. Prefer the push —
  your commit is kept byte for byte, which is what makes the approval a
  signature over the bytes you wrote. Read `state` in the answer: it says what
  actually happened (`PROTOTYPE` = the policy merged it, `IN_REVIEW` = waiting).

## A purchased item is a file, not a repo

`create_item` for a `purchased` or `hardware` kind writes `<NUMBER>.toml` into
`catalog.git` — §5.4's "not repo-per-screw". Its attributes, renames and
reclassification are commits there too, so `git log -- HDW-0012.toml` is that
screw's history and `git blame` means something. An assembly that buys it pins
`catalog` as ONE submodule, and `add_bom_line` bumps that pin to a commit that
contains the file — otherwise the push validator would rightly refuse the line.

### Big files are still not in the clone

Unchanged from the read path: meshes offloaded to filedrop live under
`[[files.external]]` and are absent from the tree. You may keep those blocks in
your draft's `plm.toml` — plm indexes them as-is — but you cannot ADD bytes to
filedrop by pushing. Until LFS lands, a new large binary is attached through
plm's staged upload.

