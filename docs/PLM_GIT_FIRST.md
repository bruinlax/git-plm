# Git-first plm — the map

*2026-09-17. Research + plan, per the maintainer: "github / gitea / gitlab that's focused
on physical objects, not code … the metadata stored in a file in there, like
GitHub does README.md … interactions driven by git, merges, versioning, commits,
branches, forks, submodules … so the operating system and LLMs can use git
natively."*

**Status: PROPOSAL. Nothing here is built except where marked (plm-git POC).**

---

## 1. Where we actually are

Three facts measured today, because the plan hangs on them:

1. **The DB is the source of truth, not git.** Prod holds 282 items / 619
   revisions, and the *design facts* live only in SQLite: 4,559 attributes,
   1,625 BOM lines, 712 context entries, 85 relations, 71 tags, 43 routing ops.
   The vault holds bytes; the original design admitted "the DB is PRIMARY data now —
   never treat it as a disposable index again." A clone of a part today gets the
   `.scad` and none of what plm *knows* about it.
2. **plm-git is a projection, not a foundation.** The POC (`../plm-git`, :8099,
   read path live, push on sandbox only) *replays* the monorepo vault into
   throw-away per-part repos, and a push is *translated* into `revise` /
   `put_file` / `submit` API calls. Git is a costume plm wears. It proved the
   transport and the "push = proposed, approval = merge" mapping — both keep.
3. **The layout fights git.** A revision is a *copied folder*
   (`versions/v0.4.1/`), so `git diff` between revisions needs
   `-M -C --find-copies-harder` to find the one changed line; `main` carries
   DRAFTs because the vault commits at check-in; offloaded STLs are absent from
   a clone; one open DRAFT per item, so nothing ever merges. Every one of those
   is on the POC's own punt list, and every one has the same root cause: git was
   added underneath a DB-shaped model.

**Git-first is one inversion:** the per-item repo becomes the source of truth
for the design; the DB becomes what GitHub's DB is — a derived index plus the
*conversation about* the design.

## 2. The rule that decides everything: does it travel with a clone?

GitHub keeps README, LICENSE, CODEOWNERS and workflows *in the repo*, and
issues, PRs, reviews, stars and permissions *in its database*. Same test here:

| Travels with a clone/fork → **file in the repo** | About the process → **forge DB** |
|---|---|
| identity (number, name, kind, project) | number *allocation* (plm owns identity) |
| file roles: which mesh is primary, source + selector, viz, drawings | users, roles, sessions, tokens |
| attributes (material, mass, bbox…) with their `source` | approvals / reviews (the PR) |
| BOM lines (child, version rule, qty, find no., suppressed, alt group) | comments on links, approvals, items (context *after the fact*) |
| relations (derived_from, coupon_for, cut_from, mounts_to…) | events feed, webhooks / integrations |
| routing operations, tags, supplier sources | lifecycle *decisions* made later (OBSOLETE) |
| README.md (the item page), INSTRUCTIONS.md | search index, where-used, rolled BOM, thumbnails (all derived) |
| the *why* of a change → commit message + PR description | snippets, staging, locks |

The right-hand column is rebuildable-or-conversational. The left-hand column is
the part. **After the flip, `importer` can rebuild every design table from the
repos alone** — which restores the charter line ("derived data is rebuildable,
never precious") that we quietly broke in August.

One refinement so a clone is *self-evidencing* and not merely complete: forge
decisions are **mirrored into git** — an approval becomes an annotated tag whose
message records approver, target state, approval id; post-release measured facts
(slicer mass on a frozen revision) and lifecycle changes go in
`refs/notes/plm`. Notes annotate a commit without changing its SHA, which is
exactly "append a fact to an immutable revision."

### 2.1 Relations — one home per claim (stage 10, built)

"Does it travel with a clone?" answers *where* for a file; for a link between
two items it needs one more question — **whose claim is it, and how long does
it live?** The forges and Teamcenter agree: a link is recorded ONCE, by the
side that makes the claim, in the home whose lifetime matches it; the other
direction is computed ("Used by", where-referenced). And attaching evidence to
a released revision never revises it.

| home | types | where | lifetime |
|---|---|---|---|
| **revision** | `coupon_for`, `integration_envelope_for` | `[[relations]]` in the claimant revision's `plm.toml`: `type, item, version, commit` (+ `result, result_note, measured` for a coupon) | carried forward by revise, editable on a draft, frozen at release |
| **item** | `derived_from`, `bench_test_of`, `mounts_to`, `cut_from`, `instance_of` | `[[relations]]` in `item.toml` on `refs/plm/item` | every revision of the item |
| **note** | `installed_on` | `[[installed]]` in `refs/notes/plm` on the revision it describes | a post-release fact; the tag never moves |

`home` is a field of the registry (`plm/relations.py` `REGISTRY`), never of the
file — the pre-stage-10 `scope = "item"` is gone, read tolerantly in old trees
and linted on push. A revision-home target is pinned by version AND commit; a
push pinning a commit the target repo lacks is refused (the gitlink rule), a
version/commit disagreement is a lint. The inverse is DERIVED and the UI and the
MCP say so ("derived · recorded in GL-CPN-0001 v0.1.0 plm.toml"); to record that
X verifies Y, write it in X. A coupon whose SOURCE includes the part's source is
a different fact — a build dependency — whose home is a `lib/<N>` submodule
(§5.1); the two are never converted into each other automatically.

## 3. The manifest — `plm.toml`

The README.md trick: **convention first, the file only for what convention can't
know.** No `[files] primary`? plm uses the only mesh, else `<NUMBER>*.stl`.
README.md renders as the item page (today's ITEM.md). INSTRUCTIONS.md is the
work instruction. Tag annotations are the changelog (today's VERSION.md).

```toml
schema   = 1
number   = "KML-PRT-1012"      # allocated by plm; pre-receive refuses a mismatch
name     = "Thin rotating carrier"
kind     = "part"
project  = "lantern-gobo-bearing"
version  = "0.4.2"             # the revision this tree claims to be (tag must match)
maturity = "prototype"         # what this revision asks to be released AS
process  = "print"
tags     = ["unverified-datum"]

[source]                       # how to regenerate — today's revisions.source/selector
file      = "thin_carrier.scad"
selector  = 'part="inner_ring"'
generator = "openscad"

[files]
primary = "thin_carrier.stl"   # viewer, thumbnail, assembly composite use THIS
viz     = "thin_carrier_assembled-frame.stl"
drawing = ["carrier_section.png"]

[attributes]
material = { value = "PETG", source = "author" }
mass_g   = { value = 41.2, unit = "g", source = "orca" }

[[relations]]
type = "derived_from"
item = "KML-PRT-1002"
version = "0.2.0"

[[bom]]                        # assemblies only
find  = 10
path  = "parts/KML-PRT-1013"   # a submodule — the gitlink there IS the pinned revision
qty   = 1
track = "release"              # bot opens a bump PR when the child releases ("" = manual)

[[bom]]
find = 20
path = "catalog/HDW-0012.toml" # purchased/hardware: a file in the catalog submodule
qty  = 6

[release]
required = ["thin_carrier.stl", "INSTRUCTIONS.md"]   # release fails without these

[[routing]]                    # fabricated parts only
seq = 10
op  = "CUT"
params = { length_mm = 812, stock = "GL-PUR-0007" }
```

`version` lives in the file on purpose: a zip of the tree knows what it is, and
two parallel drafts that both claim `0.4.2` collide **as a git conflict on that
line** — git surfaces the PLM conflict instead of us inventing a lock for it.

Prior art checked: **Open Know-How** (`okh.toml`, IoP Alliance) is the existing
"manifest in the repo root" standard for open hardware — discovery metadata
(license, function, docs links), no revisions/BOM rules/roles. **gitplm** does
CSV partmaster + BOMs in git with `CCC-NNN-VVVV` numbers — validates "text BOMs
in git" and the catalog-file idea, has no server/gate. Per the
mimic-protocols-not-vocabulary rule: our file is native; an `okh.toml` *export*
is a cheap compat footnote later, not our schema.

## 4. PLM ↔ git vocabulary (the whole product in one table)

| PLM concept | Git-first form | Notes |
|---|---|---|
| item | **a repo** `plm.example.com/git/KML-PRT-1012.git` | flat URL; former numbers redirect, like a GitHub rename |
| project | namespace/org + manifest field | unchanged: "project = namespace only" |
| revision | **annotated tag** `v0.4.1` on `main` | tags immutable server-side = today's 409 |
| version folders `versions/vX/` | **gone** — the tree *is* the current revision | `git diff v0.4.0 v0.4.1` just works; kills the copy-detection hack |
| DRAFT | **a branch** | several at once; one-open-DRAFT limit disappears |
| submit for release | **push `refs/for/main`** (or open a request on a branch) | POC ref kept; it is also Gitea's AGit convention |
| IN_REVIEW | open merge request | fetchable as `refs/plm/drafts/*` (built in POC) |
| approve | **forge merges + tags** | only plm writes `main`/tags; humans approve, bots can't |
| `main` | **released revisions only** | fixes the POC's biggest punt by construction |
| check-out lock | branches for text; **LFS lock** for opaque CAD (`.f3d`) | stranded-checkout class of bug mostly evaporates |
| fork | **server-side fork**: new number, full history, `derived_from` | merge-back becomes *real* for code-CAD: `git pull` from the fork |
| revise-up carries files/BOM/attrs | free — it's the same tree | `ops.revise` copy logic deleted |
| BOM line / shared source | **submodule** (pin = gitlink) + manifest line (qty, find…) | see §5.1 |
| precise / imprecise | git is always precise; "imprecise" = `track = "release"` automation | |
| propagate | **Dependabot**: child release → bot opens bump PRs on parents | same cascade, reviewable |
| release | **tag + Release object** with attached artifacts; maturity < released = *pre-release* | same as code, see §6b |
| context ("why") | commit message + PR description; later comments in DB | FTS indexes both |
| ECO (PLM_CHANGES.md) | a **pull request** (`#n`, legacy label CHG-n): N member proposals across repos, decided together | built, stage 14; named pull request 2026-10-16 (PLM_REVIEW_MODEL §2.5) |
| big binaries / filedrop | **Git LFS, batch API served by plm** (bytes in plm's store — THE object store; filedrop retired 2026-10-16) | see §6 — built, stage 17 |
| integrations / checks | forge-run checks on push: lint manifest, resolve BOM, re-render & compare mesh, thumbnail, mass/bbox | results on the PR + notes |
| visual diff of the physical change | the pull request's own **`physical-diff` check**: old vs new of every changed mesh as check `images` (GitHub's `output.images`) | PLM_REVIEW_MODEL §2.10 |
| MCP (44 tools) | shrinks to what git *can't* do: search, where-used, rolled BOM, approval queue, number allocation | "edit the file and push" replaces most write tools |

## 5. Pushback — five places the git analogy is wrong, and the better shape

Per the charter. Each of these is a pattern that looks natural and isn't.

**5.1 References ARE submodules — the pin lives in git, the line data in the
manifest.** *(Revised 2026-09-17 after the maintainer's steer; the first draft of this
section argued manifest + `bom.lock` and demoted submodules to a generated
"kit" repo. That was wrong, and here is why.)*

The maintainer's model: when one part refers to another — a BOM child, a shared
parametric source — it refers to a **commit** (any "subversion") or to an
**official release** (a tag), exactly as code does. That is precisely what a
gitlink is, and the hardware-on-git world already works this way (KiCad shared
libraries as submodules: "any given revision of your project is always
associated with a matching revision of the library… nothing gets updated until
you go into the submodule and pull"). My lockfile was a second, invented home
for a fact git already stores natively. One home per fact says: **the gitlink
is the pin.**

So:

- A repo-backed reference is a **real submodule** at a conventional path
  (`parts/KML-PRT-1013/`, `lib/lantern-core/`) — *a reference that needs the
  other repo's BYTES to build*; a CLAIM about another item (a coupon verifies a
  part) is a `[[relations]]` line pinned by commit, never a submodule (§2.1).
  Built: `parts/` (BOM) and `catalog`; `lib/` is designed and **not yet
  accepted by the push door** (stage 10 open question). Added with a **relative URL**
  (`../KML-PRT-1013.git`) so the structure survives a host move or a GitHub
  mirror. Pinned at a release tag's commit, or at any commit while work is in
  flight.
- `plm.toml` carries what a gitlink can't: `[[bom]] path = "parts/KML-PRT-1013"`
  plus qty / find / suppressed / alt group. No version field on those lines —
  that would be the second home.
- **Bump = native git** (`git -C parts/KML-PRT-1013 checkout v0.4.0 && git add`),
  and `git diff --submodule=log v0.8.0 v0.9.0` on an assembly prints *each
  child's commit subjects between the two pins* — a BOM delta with reasons,
  that nobody had to build.
- **"Imprecise" stops being a data state and becomes automation.** Git is always
  precise. A line marked `track = "release"` means the bot opens a bump PR on
  the parent when the child releases (Dependabot/Renovate) — which *is*
  `propagate`, made reviewable.
- **The release rule is the same for code and hardware:** you can't ship a
  release that depends on unreleased work. A revision whose submodules point at
  untagged commits can be a DRAFT and can be reviewed; it cannot be *released*
  at a maturity above its least-mature child. Unreleased pins display Go-style
  (`0.3.2-0.20260917-a1b2c3d`: "after 0.3.1, not a release") so they sort and
  read correctly.
- **Shared sources** (`lantern_gobo_bearing.scad` feeding many parts via
  `selector`) become a library repo + submodule — the same mechanism, no
  special case. Vendored copies remain legal for parts that want to be
  self-contained.

What survives from the pushback: **no repo-per-screw** (§5.4) — purchased and
hardware items live in a catalog repo, which an assembly pulls in as *one*
submodule (`catalog/`), so even those lines are pinned by a gitlink. And
submodules' known sharp edges become our checklist, not a reason to avoid them:
detached HEAD in children, clones need `--recurse-submodules`, and **LFS files
inside submodules can stay as pointers** unless fetched recursively (a
long-standing git-lfs gotcha) — the agent instructions and the repo template
have to cover all three. The diamond case (one part reached through two
sub-assemblies at two different pins) is *real structure*, and plm flags it on
the PR rather than hiding it.

**5.2 Branches are drafts, never variants.** PLM_BRANCHING.md said "git branches
are NOT the fork mechanism." That was right for a monorepo vault and is now
*half* superseded: branches become the DRAFT mechanism. The half that survives
is the Teamcenter lesson — one number, one answer to "what is current" (`main`).
A divergent purpose is still a **fork = new number**. Long-lived `van-version`
branches are refused by policy.

**5.3 Binary CAD does not merge.** Merges are real for `.scad`, CadQuery,
`plm.toml`, markdown — which is nearly everything in this shop, making it the
*best case* for git-PLM. STL/STEP/`.f3d` get `merge=binary` + LFS locks;
a conflicting binary means "rebase and regenerate," said plainly by the hook.
(This is where Onshape-style branching earns its keep with a real geometry
kernel; we don't pretend to have one.)

**5.4 Not repo-per-screw.** Design items (part, assembly, coupon, firmware,
document, asset) are repos. Purchased + hardware items live in **catalog
repos** — one TOML per item, many per repo (gitplm's partmaster, but
file-per-item so diffs and blame stay meaningful).

**5.5 `git push` never means released.** Unchanged from the POC and
non-negotiable: the gate is plm's, a human approves, the forge does the merge.
Git-first changes what is *behind* the gate, not whether there is one.

**5.6 A purchase REQUIREMENT is a BOM line with nothing selected (stage 19).**
§5.1 makes every `[[bom]]` line a pin, and §5.4 makes a bought thing a
`catalog/<N>.toml` line — so the manifest could say "this strap, from this
supplier" and could not say "a 25 mm non-stretch strap is needed and nobody
has picked one". CHG-379 (camper-ipad-dock) hit exactly that: the strap,
buckle, cable and liners were in the reference scenes and in prose, the BOM
held only the four printed parts, and a green explode check read as
order-ready. The shape:

```toml
[[bom]]
find = 50
requirement = "Non-stretch webbing strap, 25 mm, >= 600 mm, sewn loop"
qty = 1
unit = "EA"
catalog = ["HDW-0031", "HDW-0032"]   # optional CANDIDATES — never a selection
notes = "length set by the over-top anchors; verify on the closed iPad"

[release]
required = ["purchase-requirements"]  # optional: a release waits for them
```

* **No `path`** = nothing selected: no gitlink, no catalog pin, no child item,
  no `bom` row. The push door accepts it (and refuses an empty `requirement`,
  or `catalog` candidates on a selected line); the indexer stores it in
  `bom_requirements` (plm/requirements.py — its own table, created only where
  git is in use). Rolled BOMs, where-used, explode and the mesh checks never
  see it, by construction.
* **Visible where the buying happens**: the build plan (`requirements`, a
  "requirement — not selected" row in the buy table), the BOM tab (its own
  table under the BOM), `get_bom` (`requirements`), and the push output
  (`plm:   requires …`).
* **The gate**: the `purchase-requirements` check WARNS on every commit that
  has any ("3 purchase requirements unresolved (not selected)"), which puts
  them in the change's warnings and the release gate's amber list. It is
  REQUIRED — a release is refused — only when the revision's own `[release]
  required` names `purchase-requirements` (`checks.manifest_required`). §3's
  `[release] required` is otherwise a list of artifacts; the reserved name is
  the one entry that is a check.
* **Selecting** one is replacing the line with an ordinary `path =
  "catalog/<N>.toml"` line (register the catalog item first). Never fabricate a
  catalog item to make a BOM look complete — that is the lie this exists to
  stop.

## 6. Large files: LFS in front of plm's object store

The POC's most surprising punt — "clone a part, get no mesh" — has a standard
answer. An LFS pointer is `sha256 + size`, which is **exactly the
handle's verification**; plm only has to speak the batch endpoint, in front of
its own content-addressed store (docs/LARGE-FILES.md). Result: `git clone` gets everything, `GIT_LFS_SKIP_SMUDGE=1` gets the
fast text-only clone agents usually want, and the LFS lock API replaces
`checkouts` for opaque files. Cost: `git-lfs` is not installed on the host
(checked) and is needed wherever parts are cloned — one static binary in
`~/bin`, plus the agent containers.

Policy stays: the released tag **contains the mesh that was approved** (what
gets printed must be the bytes that were reviewed), and a check re-renders from
`[source]` to prove they match.

**Built (stage 17, 2026-10-05) — `docs/LARGE-FILES.md` has every decision and
its reason; the short form:**

1. **Store:** plm's own content-addressed store (`PLM_LFS_ROOT`,
   `objects/<aa>/<bb>/<sha256>`, local disk), backed up by plm's sidecar as
   `DATA_TREES` beside the repos' `GIT_TREES` bundles. NOT the filedrop inbox
   (its TTL would rot a release), and not plm's filedrop bucket: design data
   is service data, and plm's filedrop use was retired outright on 2026-10-16
   (docs/LARGE-FILES.md decision 0).
2. **Transport:** the git service answers `<repo>/info/lfs/objects/batch`
   (download + upload, `basic`), `GET`/`PUT objects/<oid>`, `verify`, and
   `locks/verify` ("no locks"). Reads anonymous like clone; uploads through the
   push door's token. Uploads are hashed while they stream; a mismatch is 422
   and nothing is kept. Dedupe across items is free.
3. **Policy:** meshes are LFS **at any size** — `.gitattributes` cannot express
   "above 1 MiB", and a mesh that is a pointer in one commit and a blob in the
   next makes an LFS clone's `git status` lie; sources, manifests and docs are
   **never** LFS; any other file above `PLM_LFS_THRESHOLD_BYTES` (1 MiB) is,
   with its own `.gitattributes` line. plm writes `.gitattributes`
   (create_item, `split --lfs`, plm's own commits).
4. **Push door:** refuses a pointer whose object is missing (`git lfs push
   --object-id origin <oid>`), a NEW plain blob the policy says is LFS above
   the threshold (the `git lfs track` fix — this replaced "LFS is Phase 4"),
   and a pointer standing in for a source; lints a small mesh that arrived
   plain; grandfathers blobs already in a parent tree.
5. **Readers:** indexer (`backend = 'lfs'`, key = sha256), storage, every
   `/api/repo` file route, `/api/blob`, checks (`Ctx.blob`, the smudged render
   tree), release evidence (the object's sha256 + `lfs: true`), `get_item`'s
   `git.lfs`.
6. **Migration:** `split --lfs`, a recorded layout like `--normalize-names`
   (a root recorded before stage 17 is not LFS, so the live POC and `gitsync`
   are unchanged). "Clone a part, get no mesh" is over for a migrated repo;
   `GIT_LFS_SKIP_SMUDGE=1` is the fast text-only clone.
7. **Not built:** LFS locks (the `checkouts` retirement), store GC,
   re-splitting the live POC's existing repos. (A filedrop-backed store is not
   coming: filedrop was retired from plm 2026-10-16,.)

## 6b. Releases — the same process for hardware as for code

Maintainer: "we have official releases, which is the same for code as it is for
hardware — so that process should kinda be the same." It maps one-to-one, and
AllSpice's *Hardware releases 101* lands in the same place:

| Code forge | plm |
|---|---|
| commit | any "subversion" — referenceable, never official |
| tag | the immutable revision `v0.4.1` (tags are pointers; never move) |
| **Release** (tag + notes + attached assets) | the released revision: tag annotation = release notes, **generated artifacts attached as release assets** |
| pre-release flag | maturity below `released` (concept / design / prototype) |
| "production releases come from `main` only" | unchanged — only plm merges to `main` and tags |

The useful idea to steal is AllSpice's line that *rendered/derived files belong
to the release, not to every commit*. For code-CAD that means a part may either
**commit** its mesh (LFS — required when there is no regenerable source:
imported STEP, downloaded STL, Fusion export) or **build** it: the release
check renders from `[source]`, attaches the STL/3MF/PNG to the Release, and
records their sha256 in the tag annotation. Either way the approved bytes are
immutable and hashed — the rule "what gets printed is what was reviewed" holds
— and day-to-day branches stop carrying 2 MB meshes on every commit.
`[release] required` (stolen from gitplm) fails a release that is missing a
declared artifact.

**Evidence by reference (stage 10, built).** A release's checklist line may be
attested by pointing at a claim instead of by a person's word alone: the
workflow (`plm/release.py` `RELEASE_WORKFLOWS[…]["evidence"]`) names which line
accepts which claim type — "Fit-checked against mating parts" accepts a passing
`coupon_for` — and the signoff stores `evidence = "GL-CPN-0001@v0.1.0"`, valid
only if that claim pins the EXACT commit being released (or the commit plm's
release commit was reviewed as) with `result = "pass"`. The frozen evidence file
resolves it (`evidence_claims`: coupon, commit, result, measured). The coupon's
repo is untouched and so is the part's: nothing was revised to attach it.

## 6c. The part page is a repo page (hard UI requirement)

Maintainer: "when I go to a part I should be able to see / dropdown the versions /
see the tree like in GitHub." The current item page keeps everything it has
(title block, render, 3D viewer, BOM card, approvals, related items) and gains
the forge furniture, all read straight from the bare repo (`git ls-tree`,
`cat-file`, `log` — no new data to maintain):

- **Ref dropdown**, two tabs like GitHub's: **Releases** (tags, newest first,
  maturity stamp on each) and **Drafts** (branches + open proposals). Picking a
  ref re-renders the *whole page* at that ref — render, BOM, attributes,
  README — because all of it now comes from that tree's `plm.toml`.
- **File tree** with last-commit message and age per row; click-through file
  view reuses the existing lightbox (markdown, images, text, **STL → 3D**).
  Submodule rows show as `parts/KML-PRT-1013 @ v0.3.1` and link to that part
  at that ref.
- **Commits** list, **compare** view (`v0.4.0...v0.4.1`: text diff + BOM delta
  + 3D overlay), **blame** on source files.
- **Clone button** with the URL, the LFS/partial-clone variants, and the
  one-line token setup.
- README.md rendered under the tree, as on GitHub.

**Learned from the POC (2026-09-17), the maintainer's review:**

- **The repo bar goes at the TOP of the item page** — ref dropdown, release /
  draft / commit counts as links, Compare, Clone — and **every commit and every
  ledger row links to its diff**. Follow GitHub's information architecture, in
  our design language. The first build put the git surface below the fold and
  the compare view was only reachable by URL.
- **Diff ANY file, in the way that suits the file** — "just like a regular git
  diff on GitHub, but for a part maybe you look at the text difference or the
  graphical difference, whether it's the main part or not." Text → unified
  diff; **mesh → 3D red/green overlay for that file**; image → 2-up / swipe /
  onion-skin; binary → size delta. The top-of-page geometry diff is a
  *chooser defaulting to* `[files] primary`, not the only mesh you can see —
  and it says which file it is showing and why.
- **`primary` is the display geometry, which is not always the file you want to
  diff.** Convention is `viz` > `mesh` > `artifact`; on GSI-PRT-0004 that picks
  the *posed visualization* copy over the printable mesh. Correct for a
  thumbnail, surprising for a review.

### 6c-0. The addresses are GitHub's (stage 22, 2026-10-16)

Maintainer: *"please move to real paths."* The UI's URLs are real paths, verb for
verb with GitHub (the table lives in `web/routes.js`, the server's copy in
`plm/webroutes.py`):

| page | path |
|---|---|
| repo home / a tab | `/<N>` · `/<N>/bom` `/releases` `/drafts` `/history` `/record` `/context` (`?ref=<ref>` to read it at a ref) |
| a ref, a directory, a file | `/<N>/tree/<ref>[/<dir>]` · `/<N>/blob/<ref>/<path>` |
| compare | `/<N>/compare/<base>...<head>[?file=…\|mesh=…]` |
| proposal | `/<N>/pull/<pid>` (`/<N>/pull/p<pid>` = always the proposal; `/<N>/proposal/<approval id>` = a round, redirects) |
| change, per repo | `/<N>/pull/<n>` when `<N>` is a member of CHG-`<n>` (a collision with proposal #`<n>` prefers the change) |
| release | `/<N>/releases/tag/<vX.Y.Z>` (newest record of that tag) · `/<N>/releases/<id>` |
| pull request · release set | `/pulls/<n>` (`/changes/CHG-<n>` redirects; list `/pulls`, an item's `/<N>/pulls`) · `/release-sets/<n>` |
| the rest | `/` · `/registry` · `/approvals` (= `/pulls`) · `/search?q=` · `/projects/<p>` · `/snip/<token>[/info]` |

A ref's own slash (`draft/v0.4.1`) is written literally and read back
longest-first against the repo's refs, as GitHub does. Every old `#/…` link
(`/#/item/N/…`, `/#/change/CHG-n`, …) still lands: the router maps it to its
path and replaces the history entry. The server answers every UI path with the
shell and anything else with a small 404 page, so a typo does not boot the app.

### 6c-1. Stable filenames — the version leaves the filename too

The folder era left its mark on file *names*: `GSI-PRT-0004_v0.2.0_floor_plate.stl`
→ `GSI-PRT-0004_v0.3.0_floor_plate.stl`. With the version in the name, git sees
one file deleted and another added; there is no per-file history, no `git log
-- floor_plate.stl`, no blame, and no file-level diff without a pairing
heuristic. **In a git-first repo the tag carries the version and the repo
carries the number, so the file is just `floor_plate.stl`.** Then a revision is
a *modification*, and every git tool works on the file unaided.

- New check-ins: the repo template and the manifest lint refuse a version token
  or the item number in a tracked filename (with the fix-it in the message).
- Release assets (§6b) are where the stamped name belongs:
  `GSI-PRT-0004_v0.3.0_floor_plate.stl` is a fine name for the *download*.
- Migration (`split`): normalise names when replaying history, so old revisions
  gain per-file history retroactively. The POC pairs files by manifest role +
  version-stripped name instead; that heuristic is a bridge, not the design.

## 6d. "A nice git interface" — the checklist

What makes a forge feel native to stock `git`, an OS, and an LLM with no plm
training. ✅ = POC already does it.

| Need | Shape |
|---|---|
| Smart HTTP, protocol v2 | ✅ `git http-backend` behind `plm.example.com/git/` |
| One identity plane | personal/bot **access tokens minted in plm** (GitHub-PAT style), sent as Basic password so credential helpers just work; retire plm-git's own token map |
| Propose by push | ✅ `refs/for/main`; add **push options** (`-o title= -o maturity=`) — Gitea AGit-compatible, needs `receive.advertisePushOptions` |
| Teachable refusals | ✅ `remote:` messages that say what to do next |
| Fast clones | **partial clone** (`uploadpack.allowFilter`) → `git clone --filter=blob:none`; free with http-backend |
| Big files | ✅ LFS batch API served by plm-git over plm's own store (§6, stage 17); LFS locks API not yet |
| Immutable releases | hooks refuse tag update/delete and any direct push to `main` ✅ (heads) |
| New part | `create_item` (API/MCP) allocates the number and creates the repo **from a template** (`plm.toml`, `.gitattributes` with LFS + `merge=binary`, `.gitignore`, README) — number ownership means no push-to-create |
| Download without git | `git archive` zip per ref + release assets |
| Meaningful binary diffs | optional `diff=stl` textconv script (bbox, volume, triangle count) so plain `git diff` on a mesh prints "volume 41.2→43.0 cm³"; server compare view does it regardless |
| Events | existing `integrations` webhooks gain push / tag / proposal events |
| Import from outside | any git URL can be forked in (GitHub-hosted hardware); missing `plm.toml` → conventions + a generated one in the first DRAFT (Kitspace's "manifest optional, auto-detect" posture) |
| Offsite | **design data is plm's SERVICE data**: the repos are backed up by plm's own `backup` sidecar as git (`GIT_TREES`: verified bundles of every ref and every commit a gitlink or registry row may pin, incremental) and ride the backup chain from there. **GitHub is for code** (plm's own repo, like any other code repo) — never for design data |
| SSH | not now; HTTPS + tokens covers agents and humans on the private network |

## 6e. Prior art, read properly — what to steal, what to skip

**AllSpice Hub** (commercial "git for hardware", electronics-focused; a **Gitea
fork** plus a forked Actions runner and a Python API wrapper, `py-allspice`).
- *Steal:* the **design review = PR with rendered visual diffs** (red/yellow/
  green redlines of schematic, PCB, BOM) so reviewers need no CAD seat — our
  equivalent is the 3D overlay + BOM delta. **Automation as small composable
  actions** over the repo (generate-bom, DigiKey report, post-review-comment) —
  matches our integrations seam. **Releases with attached generated files,
  pre-release flag** (§6b). An AI reviewer ("DRCY") that comments on the review
  — for us that is just an agent with the clone.
- *Skip:* it is ECAD-native (Altium/OrCAD/KiCad renderers) with nothing for
  mechanical; no item/BOM-structure/where-used model — a repo is a project, not
  a part; their own write-ups do not solve binary merge either ("not
  addressed") — they lean on review, as we will.

**gitplm** (small Go CLI; PLM-by-convention in one git repo).
- *Steal:* **IPN ≠ MPN** — CAD references the internal number, suppliers hang
  off it as multiple rows with a **priority** column (our `sources` table →
  `[[source]]` blocks in catalog TOMLs, lowest priority wins). **Declarative
  release config** with `copy` / `hooks` / **`required`** — releases are
  generated, "never hand-scrub a CAD-generated BOM". **Serving the catalog to
  CAD over HTTP** (KiCad HTTP-library endpoint) — the mechanical analogue, a
  catalog endpoint CAD scripts can query, is a cheap later win.
- *Skip:* version-in-the-part-number (`CCC-NNN-VVVV`) — tags do that job;
  release directories of copied files + symlinks to child releases — that is
  the `versions/vX/` pattern we are deleting; no server, no gate, no review.

**Kitspace** (`kitspace.yaml`): manifest is **optional** — sensible
auto-detection first, file only to override; `multi:` lets one repo hold several
projects (our catalog repos). Confirms the README.md-style posture in §3.

**Open Know-How** (`okh.toml`): discovery metadata standard for open hardware;
an export target, not a schema to adopt.

**Onshape**: real branch/merge for mechanical CAD, possible only because they
own the geometry kernel. The honest boundary of what git alone gives us (§5.3).

Net: nobody has shipped *item-per-repo + BOM-as-submodules + a release gate +
mechanical review*. The pieces are all proven separately; the combination is
the product.

## 7. Build the forge, or adopt one? (the one real decision)

**AllSpice Hub — the commercial "git for hardware" — is a Gitea fork.** That is
the strongest signal available about where a product-grade version of this ends
up. Gitea as the *engine* would give us LFS + locks, forks, PRs with line
comments, protected branches/tags, AGit `refs/for/*`, OIDC, tokens/SSH, an
Actions runner. Nothing like it ran in the original deployment (only bare mirrors).

**DECIDED 2026-09-17 (the maintainer: "I like our current site — I just want to make
sure we have a nice git interface"): extend plm-git and fold it into plm.** The
site stays the product; git becomes its interface (§6d). Everything
that makes this a *PLM* is custom hooks either way (number ownership, tag
immutability, manifest validation, approval-is-merge, bom.lock, geometry diff);
the POC already owns smart-HTTP, quarantine validation and draft refs; and
Gitea would put a second UI, a second permission DB and a second thing to
restore in front of a product you like as it is. What we'd hand-build: LFS
batch + locks (~small, over filedrop), server-side merge on bare repos
(`git merge-tree`), fork (a bare clone + a number).
**Revisit trigger:** the day this needs SSH, multi-user orgs, line-level review
comments or repo-defined CI runners — i.e. the day it stops being a small single-operator deployment —
swap the engine for Gitea behind the same `plm.toml` contract. Keeping the
contract *in the repo* is what makes that swap cheap.

## 8. Phases

Each phase ships alone and is reversible until Phase 3.

**Phase 0 — clean the floor (precondition, not optional).** The plm repo has 19
modified files / ~5.7k lines uncommitted since August; the manufacturing vault
has 350 uncommitted paths (308 untracked). A git-first PLM cannot sit on a
dirty vault. Commit both. Freeze `plm.toml` schema v1. Decide §7 and §9.

**Phase 1 — manifest as export (zero behaviour change).** Every write op also
writes `plm.toml` into the item folder, generated from the DB. Acceptance test
is the whole point: `importer` rebuilds a scratch DB **from manifests alone**
and the design tables diff empty against prod. Valuable even if we stopped
here — it is the backup story the DB-primary era lost.

**Phase 2 — real repos.** Split the vault into true per-item bare repos (the
POC's replay, run once, kept) under `/srv/plm/repos` — local disk,
because git object dirs on the synced volume corrupt — protected by plm's own
backup sidecar as git (E2, `GIT_TREES`; stage 11), so nothing lives only on
local disk. (A post-receive mirror-push to a NAS share, once
planned here, is superseded: a bare mirror is a second copy of design data
outside the service, and git object dirs do not belong on a network share.) History rewrite: each `versions/vX/` folder becomes a
commit whose tree is that folder's contents, tagged `vX`; `main` = released
tags only. Catalog repos for purchased/hardware. plm reads through the repos;
writes still go through ops. **The repo page ships here** (§6c: ref dropdown,
file tree, commits, clone button) — it is read-only over the new repos, so it
is the first visible payoff and needs nothing from the later phases. Partial
clone and plm-minted access tokens (§6d) land with it.

**Phase 3 — the flip.** The manifest becomes the source, the DB the index.
`ops.py` write paths become *commits by plm-bot on a draft branch* (one code
path: op → commit → indexer → DB; API, MCP and UI all ride it). Push stops being
translated into API calls: pre-receive validates `plm.toml`, post-receive
indexes. `revise`'s carry-forward code, version-folder logic and the
supersession rule are deleted, not ported.

> **DECIDED 2026-09-17 (the maintainer):** "it should be git under the hood for
> everything now going forward — if you want to keep the legacy ways (/api)
> open we can, we just change the backend way of doing it." So Phase 3 is not
> optional and not partial: **every design write is a commit**, whichever door
> it came through (`git push`, API, MCP, UI), and the indexer is the only
> writer of design rows. The API/MCP/UI keep their shapes. What stays in the
> database is forge state — proposals, reviews, threads, checks, events, users
> — specified in `PLM_REVIEW_MODEL.md` ("the best of git and Teamcenter").
> The vault is retired at the end of this phase. POC stage 3
> (``) builds the machinery — indexer, commit
> primitive, `git` storage backend, approve = merge + tag — and converts the
> first legacy write; stage 3b converts the rest mechanically.

**Phase 4 — LFS over filedrop + locks.** §6. Retire `checkouts`. *(Stage 17
built the LFS half over plm's own store; locks are open — docs/LARGE-FILES.md.
The filedrop-backed store will not be built: filedrop was retired from plm
2026-10-16,.)*

**Phase 5 — the review experience.** Parallel drafts, server-side merge,
checks (manifest lint, BOM resolve, re-render compare, thumbnail, mass/bbox).
The plm-native payoff, AllSpice's "redlines" for mechanical: **3D overlay diff**
of old vs proposed mesh in the existing viewer, next to the text diff and the
BOM delta. This is the human surface the charter reserves for humans.

**Phase 6 — the ecosystem moves.** Forks with history; BOM lines and shared
sources as submodules (migration writes gitlinks at today's resolved
versions); propagate as bump-PRs; former-number redirects. (No push-mirror of
part repos to GitHub: design data is protected by the service's backup, E2;
GitHub is for code.)

**Phase 7 — ECO as a cross-repo change set** (PLM_CHANGES.md, re-expressed).

## 9. Open questions

Resolved 2026-09-17: engine = extend plm-git (§7); manifest = **`plm.toml`**;
references and shared sources = submodules pinned to a commit or a release
(§5.1); releases follow the code process (§6b); part page = repo page (§6c).

Still open:

1. **The working directory after the split.** It stops being the vault.
   Lean: project folders stay as the pre-part scratch/development area; agents
   `git clone` parts into a gitignored `work/` on demand.
2. **Post-release facts** (slicer mass on a frozen revision): git notes (lean),
   or do they wait for the next revision?
3. **Commit the mesh or build it at release** (§6b) as the *default* for
   code-CAD parts. Lean: build-at-release where `[source]` renders headlessly,
   commit otherwise.
4. **GitHub home for the plm code itself** — account, one repo or two
   (plm + plm-git), and the ~5.7k uncommitted lines that must be committed
   first.
5. **Where prod's LFS bytes live** (stage 17). Lean: plm's own store on local
   disk, protected by plm's sidecar (`DATA_TREES`) — as built in staging — and
   (filedrop's `plm` bucket was the fallback if the store outgrew the disk;
   retired 2026-10-16, a bigger disk is the answer now).
6. **Migrate with `split --lfs`** (stage 17's rehearsal numbers, the design notes).
   Lean: yes, at rollout step 2 — the migration is the one moment every
   repo's SHAs are new anyway.
7. **git-lfs on the server host** — one static binary in `~/bin`
   (docs/LARGE-FILES.md § Install; not installed by any agent).

## Sources

- plm-git POC: `../plm-git/README.md` (claims 1–9, punt list)
- AllSpice Hub (Gitea fork, hardware design review, BOM/schematic redlines):
  https://www.allspice.io/post/why-use-git-for-hardware-pros-cons ·
  https://learn.allspice.io/docs/design-review-quickstart
- Open Know-How manifest: https://github.com/iop-alliance/OpenKnowHow
- gitplm (CSV partmaster/BOM in git): https://github.com/git-plm/gitplm
- Onshape git-style branching/merging for CAD:
  https://www.onshape.com/en/blog/git-style-version-control-cad-data-management
- AllSpice hardware releases: https://learn.allspice.io/docs/hardware-releases-101
- AllSpice automation repos (py-allspice, generate-bom, act fork):
  https://github.com/AllSpiceIO
- Kitspace manifest: https://github.com/kitspace/kitspace-v2
- Gitea AGit flow (`refs/for/<branch>`, push options):
  https://docs.gitea.com/usage/agit
- Git LFS batch API: https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md
- LFS-in-submodules pointer gotcha:
  https://github.com/git-lfs/git-lfs/discussions/4797
- KiCad libraries as submodules (pin-to-commit practice):
  https://github.com/imciner2/KiCad-Libraries/blob/master/UsingGitSubmodule.md
- Go modules pseudo-versions (display form for unreleased pins):
  https://go.dev/ref/mod
