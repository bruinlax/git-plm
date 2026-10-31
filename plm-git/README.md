# plm-git — `git clone` a part, `git push` a proposal

> **Status: PROOF OF CONCEPT, both directions.** The read path is live and
> private-network-only with no auth. The **write path is built and exercised, on the
> SANDBOX only** — `PLMGIT_ALLOW_PUSH=0` everywhere else, and pointing it at the
> system of record takes two more deliberate flags. Do not point anything
> load-bearing at it yet.

```console
$ git clone http://localhost:8099/git/KML-PRT-1009.git
Cloning into 'KML-PRT-1009'...

$ cd KML-PRT-1009 && git log --oneline | head -3
f880836 Controlled source/instructions update
0395172 plm: check in KML-PRT-1009 v0.7.1 — Geometry-equivalent patch to v0.7.0 …
e4931ed plm: check in KML-PRT-1009 v0.7.0 — Replaces the light three-pad post …
```

That is a physical part, with its real engineering history, in a plain git repo,
fetched by unmodified `git`. No plm client, no plugin, no export button.

And the other direction, against the sandbox:

```console
$ git -c http.extraHeader="Authorization: Bearer $PLM_TOKEN" push origin HEAD:refs/for/main
remote: plm: revise KML-PRT-1012 patch (v0.3.1 -> v0.3.2) as agent
remote: plm: DRAFT v0.3.2 open (revision id 231), files/BOM/attributes carried forward
remote: plm: put_files 3 file(s) onto v0.3.2 (inline text, staged upload handle)
remote:   vault         20,182 B  …/versions/v0.3.2/lantern_gobo_bearing.scad
remote:   lfs        1,772,018 B  …_bottom-insert-thin-carrier.stl
remote: plm: submit_for_release -> IN_REVIEW (target PROTOTYPE)
remote: proposed:  KML-PRT-1012 v0.3.2  DRAFT -> IN_REVIEW (awaiting a human approver)
remote: git push means PROPOSED. A human approving in plm is the merge.
```

Not one byte of that was written by plm-git. plm wrote all of it, through the same
`revise` / `put_file` / `submit_for_release` pipeline the UI and the MCP use.

**Agents: read [`docs/AGENT-INSTRUCTIONS.md`](docs/AGENT-INSTRUCTIONS.md)** — when
to clone instead of calling the MCP, the push flow step by step, and the refusal
catalogue.

---

## The vision

plm is for physical things what git is for software. It already has the parts
that matter: identity (numbers plm owns), revisions, BOMs, approvals, a release
gate, and one git vault underneath holding every controlled byte. What it did
not have is the *transport* — the part where you get a thing onto your machine,
work on it, and hand it back.

For software that transport is settled, and it is git. **The value here is not a
new idea; it is the opposite of a new idea.** Clone, branch, log, diff, blame,
bisect, PR, merge: these are the best-understood workflows in engineering. Entire
ecosystems are built on them, and — the part that matters for an AI-first PLM —
**every LLM already speaks them fluently, with no plm-specific training at
all.** An agent that can `git clone KML-PRT-1009` inherits, for free:

- `git diff` / `git log -p` over CAD source, and every diff viewer ever written
- `git blame` on a `.scad` line — *which revision introduced this wall
  thickness, and why* (the check-in message is right there)
- `git bisect` over design history — *the last version that printed cleanly*
- CI hooks: push → render → slice → check → open the DRAFT
- ordinary code-review tooling, pointed at geometry changes
- worktrees, stashes, `git fetch`, submodules, `git subtree` — the whole toolbox

None of that has to be built, documented, or explained to a model. It is already
there, and the model already knows it.

**This is the plm2 charter applied to transport.** plm stays the system of record
and the release gate: it owns numbers, revisions, states, approvals, and the
vault. git becomes the *familiar local workshop* — the place an agent or a human
does the messy work with tools they already have — and plm remains the only door
back in. Nothing about being clonable weakens the gate; a clone is a read, and the
write path is a `git push` that lands in **plm's approval queue**, never in the
vault. `git push` means *proposed*; a human approving is the merge.

The mental model, and the reason "GitHub for parts" is the right analogy:
GitHub's value was never the git protocol. It was that the protocol was already
universal, and GitHub gave it identity, review, and a release process. plm has
the identity, review and release process. This adds the universal protocol.

---

## What this POC actually proves

Five claims about reading, each demonstrated by `demo/demo.sh` against the
running service:

| # | Claim | Evidence |
|---|---|---|
| 1 | A part number is clonable over vanilla smart HTTP | `git clone http://127.0.0.1:8099/git/KML-PRT-1009.git` succeeds in ~1 s, 25 MB, 68 files |
| 2 | The clone carries the part's **real** plm history | 16 commits, the plm check-in messages verbatim, original authors and dates, plus trailers pointing back at the monorepo commit |
| 3 | The content is **byte-identical** to the vault | the extracted repo's root tree object == the vault's tree object for that folder (`2f49968b…` both sides) — same git objects, nothing re-hashed or re-serialised |
| 4 | The per-part repo tracks the vault automatically | rewind a cache to before a check-in → next `git clone` rebuilds it unprompted (5 commits → 6), and is a no-op when nothing moved |
| 5 | Failure modes are clean | unknown number → `remote: 404 unknown part` and `fatal: repository … not found` |

Plus one thing that is not on the list because it is the whole point: after
`git clone`, `git log -- versions/v0.7.1`, `git diff --stat HEAD~3 HEAD`, and
`git blame` on the `.scad` all just work, and nobody wrote any code for them.

And four more, from the write path, all demonstrated by `demo/push-demo.sh`
against the sandbox:

| # | Claim | Evidence |
|---|---|---|
| 6 | A `git push` becomes a **DRAFT revision plus a submitted approval**, written entirely by plm | `revise` → `put_files` → `add_context` → `submit`; the vault commit is authored by `plm`, and `plm-git` never touches the working tree |
| 7 | Binaries move **by reference**, not through a JSON body | `.scad` inline → `backend=vault`; STL/PNG → PUT to plm's upload inbox → `backend=lfs`, `key=<sha256>` |
| 8 | A refusal costs nothing | six refusal paths, each ending `Nothing was written… No ref moved, and plm was not touched` — because pre-receive runs in git's quarantine |
| 9 | The "pull request" comes free | `git fetch origin '+refs/plm/*:refs/plm/*'` then `git diff -M -C --find-copies-harder <base>..refs/plm/drafts/<N>/v<ver>` → `versions/{v0.3.1 => v0.3.2}/lantern_gobo_bearing.scad │ 2 +-` |

### Running the demos

```bash
docker compose up -d --build       # plm-git :8099 (read-only), plm-git-sandbox :8199
./demo/demo.sh                     # the read path; or ./demo/demo.sh http://localhost:8099
PLM_TOKEN=<token> ./demo/push-demo.sh    # the write path — SANDBOX ONLY
```

`demo.sh` is read-only against the vault and against plm. `push-demo.sh` creates
real DRAFT revisions and real approval requests, so it checks
`/healthz → push.target_is_sandbox` first and **refuses to run** against anything
that is not a sandbox. Neither script approves anything: that is a human's job.

---

## How it works

```
        plm  ──items.folder──▶  resolve.py        "KML-PRT-1009" -> a vault path
         │                          │
   commits │                        ▼
         ▼                     extract.py         mirror clone -> replay -> bare repo
  manufacturing/  ──clone──▶  ~/.cache/plm-git/vault-mirror.git
   (ONE git repo,                  │
    read-only here)                ▼
                             ~/.cache/plm-git/repos/KML-PRT-1009.git
                                   │
                                   ▼
                              server.py  ──CGI──▶  git http-backend
                                   │
                                   ▼
                        git clone http://…:8099/git/KML-PRT-1009.git


and the other direction — note which arrow touches the vault:

  git push …:refs/for/main
         │
         ▼
    server.py  (token -> plm actor)  ──CGI──▶  git receive-pack
                                                    │  quarantine
                                        ┌───────────┴───────────┐
                                   pre-receive              post-receive
                                   (hook.py: no)            (push.py: propose)
                                        │                        │
                                   quarantine                    │ HTTP
                                    discarded                    ▼
                                                        plm  ──git commit──▶ vault
```

**1. Resolve (`plm_git/resolve.py`).** A number is not a path. plm owns identity,
so we ask it: `GET /api/items/{number}` → `folder`, `former_numbers`. Fallback is
plm's SQLite index opened `mode=ro`, so plm-git can never write to the registry.
Building the path from the number ourselves would break the moment a slug changes
or an item is renumbered — both of which plm does routinely
(`plm/vault.py:move_folder`, `plm/ops.py:renumber_item`).

**2. Mirror (`extract.ensure_mirror`).** `git clone --mirror --no-hardlinks` of
the vault into the cache, then `git fetch` to keep it current. `--no-hardlinks`
is deliberate: the cache must not share object *files* with the vault's `.git`,
so no extraction bug can reach live CAD. Everything downstream reads the mirror,
never the live working tree.

**3. Replay (`extract.replay`).** For each vault commit, oldest first, find this
item's folder *in that commit*, take its tree, and `git commit-tree` it onto the
previous one with the original author/committer/date/message. Commits whose
subtree is unchanged are skipped — they did not touch this part. Two trailers go
on every commit so the extraction stays reconcilable with the monorepo:

```
Plm-Item: KML-PRT-1009
Plm-Vault-Commit: e3627fd18343d022b2efc5dbf24d3da09768e02e
Plm-Vault-Path: projects/lantern-gobo-bearing/parts/KML-PRT-1009_integrated-light-engine-spider
```

Because the trees are reused verbatim, extraction is byte-exact *and*
deterministic: the same vault state always produces the same commit SHAs.

**4. Publish.** `git init --bare`, then fetch the one replay ref out of the
mirror — which transfers exactly the objects that ref reaches, i.e. the part, not
the 150 MB monorepo — then `gc`, then atomic rename into place. A `plm-git.json`
stamp in the bare repo records the folder, the prefixes seen, the staleness key,
the tip and the vault HEAD.

**5. Serve (`plm_git/server.py`).** `git http-backend` *is* the smart HTTP
server; it speaks protocol v0/v1/v2, ref advertisement, negotiation and sideband.
So the Python is only a CGI bridge (`GIT_PROJECT_ROOT`, `GIT_HTTP_EXPORT_ALL=1`,
`PATH_INFO`, `GIT_PROTOCOL` from the `Git-Protocol` header — drop that last one
and every clone silently downgrades to v0). Request bodies go through a temp file
so there is no pipe deadlock, and the packfile streams back chunked.

**6. Freshness.** The staleness key is *the newest vault commit touching any of
this part's historical prefixes*, checked on the **ref-advertisement request** —
the first thing every `clone`/`fetch` does. So a clone always gets current bytes
and no human ever runs a build step. Three tiers, cheapest first: no stamp →
rebuild; vault HEAD unchanged since the build → zero git calls; otherwise one
`rev-list -1`. The vault `fetch` itself is rate-limited by
`PLMGIT_FRESHNESS_TTL` (30 s), and that TTL covers **only** the fetch — never the
staleness comparison, so a known-stale cache is never served just because it was
checked a moment ago. (An earlier draft got this backwards; the demo caught it.)

Stdlib only, on purpose: the deployable is `python:3.12-slim` + `git`. No pip, no
wheels, no dependency drift.

---

## Why not `git subtree split`

`git subtree split -P <prefix>` is the obvious tool, and it is genuinely fast
(0.67 s on this vault). It is also **prefix-literal**, and this vault has real
folder renames in its past. Measured on the actual repo:

| Part | `git subtree split` | plm-git replay | What subtree missed |
|---|---|---|---|
| KML-PRT-1009 | **14** commits, oldest `64020b8 2026-08-08 Integrate lantern diffuser hanger into spider` | **16** commits, oldest `63c3a00 2026-08-08 Add versioned stationary light engine test set` | the two commits from when the folder was `parts/KML-PRT-1009` and `…_integral-core-diffuser-spider` |
| KML-PRT-1002 | **1** commit, `ff6c2d5 2026-08-08 Normalize lantern PLM item folders` | **1** commit, `fe5ec58 2026-08-07 lantern: add versioned individual part vault` | the part's actual creation commit — subtree shows it being "born" in a bookkeeping rename a day later |

That KML-PRT-1002 row is the argument. Subtree split does not lose *content* —
it loses *provenance*, and provenance is the reason a PLM exists. Two other
findings from the same experiment:

- **`git subtree split` cannot run in a bare repo**: `fatal: … cannot be used
  without a working tree`. Using it would force a full work-tree clone of every
  project in the vault instead of a bare mirror.
- Stitching the pieces back needs one split per historical prefix plus `--onto`
  grafting, per part, re-derived whenever a rename appears. `git commit-tree` —
  the same primitive subtree itself uses — is less code and rename-aware by
  construction.

**How renames are handled, honestly.** The lineage is found by *anchoring on the
item's numbers* (current + `former_numbers`) rather than on a literal path: in
each commit, look for a directory named `<NUMBER>` or `<NUMBER>_<anything>`. That
survives slug changes and renumbers. It needs one disambiguation rule, because
the vault also keeps an archived pre-plm registry at
`projects/<p>/old/local-registry/parts/<NUMBER>_<slug>/` which matches the number
just as well: candidates are ranked by how much of the *current,
plm-authoritative* folder path they share, so the live lineage
(`projects/<p>/parts/…`) beats the archive, which diverges a level higher.

Verified lineage for KML-PRT-1009 — three prefixes, all recovered:

```
$ python3 -m plm_git.extract --lineage KML-PRT-1009
KML-PRT-1009  anchors=['KML-PRT-1009']
  92d167b6b6  projects/lantern-gobo-bearing/parts/KML-PRT-1009
  bc8ee4baa6  projects/lantern-gobo-bearing/parts/KML-PRT-1009_integral-core-diffuser-spider
  9546a3ef04  projects/lantern-gobo-bearing/parts/KML-PRT-1009_integrated-light-engine-spider
```

Where the heuristic can still be wrong is documented under *Punts*, below.

---

## Push — built, and running on the sandbox

The reason it is not a simple `git push` is the whole thesis: **a push into the
vault would bypass the release gate.** So a push does not fast-forward anything.
It becomes a *proposal*, and plm decides.

> **A PR is an approval.** That single sentence is the mapping. The git
> vocabulary and the PLM vocabulary describe the same object from two sides, and
> the point is that everyone — human, agent, CI — already knows the git side.

```
git push origin HEAD:refs/for/main
    │
    ├─ THE DOOR (plm_git/server.py). Every receive-pack request — the ref
    │  advertisement as well as the pack POST — must carry a bearer token that
    │  maps to a plm actor holding the `editor` role. Reads stay anonymous.
    │
    ├─ QUARANTINE. git lands the objects in its own quarantine store
    │  (GIT_QUARANTINE_PATH). No served ref has moved, and if validation says no,
    │  git deletes the quarantine — a rejected push leaves not one object behind.
    │
    ├─ pre-receive = THE CHEAP "NO" (plm_git/push.py analyze + check_plm_state)
    │     · refs/heads/*, refs/tags/*, refs/plm/drafts/* → always refused
    │     · exactly one ref, no deletions, built on the current main
    │     · a commit subject that reads like a revision summary
    │     · no symlinks, no submodules, nothing escaping the part folder
    │     · exactly ONE new versions/v<x.y.z>/ directory, nothing else changed,
    │       no deletions, no basename collisions, per-file and total size caps
    │     · the item exists in plm, has a folder, has revisions
    │     · the version does NOT exist in plm (versions are immutable) and IS
    │       one major/minor/patch bump from plm's latest
    │     · plm has no open DRAFT/IN_REVIEW on the part, and no checkout held
    │       by somebody else
    │
    └─ post-receive = plm DOES THE WRITE (plm_git/push.py propose)
          1. POST /api/items/<n>/revise   -> a DRAFT at the proposed version,
             with files, BOM and attributes carried forward by plm's own
             revise(). (NOT checkin() — checkin mints a bare version, and an
             assembly pushed that way would silently lose its BOM.)
          2. POST /api/uploads + PUT /api/uploads/<token> (the bytes, to
             plm's own inbox), then POST /api/items/<n>/files with
             {handle, sha256, size} for each binary; text files ride inline.
             Bytes never travel in a JSON body.
          3. POST /api/items/<n>/context — who pushed, which commit, what moved.
          4. POST /api/items/<n>/submit  -> IN_REVIEW, an approval in the queue.
          Then: file the accepted commit as
          refs/plm/incoming/<NUMBER>/<actor>/<ts>, delete refs/for/main, and
          record the proposal in the ledger under $PLMGIT_CACHE/proposals/.
```

**Approval = merge.** A human approver decides in plm. A bot calling
`POST /api/approvals/<id>/decide` gets `agent is not an approver` — 403, from
plm, which owns the gate. On prod, `PLM_REQUIRE_OIDC_APPROVALS=1` additionally
means no `curl` can claim to be the maintainer. Nobody merges by pushing: pushing to
`refs/plm/drafts/*` is refused with that sentence in it.

**Fetchable proposals — the PR view, for free.** Every open DRAFT is advertised
read-only as `refs/plm/drafts/<NUMBER>/v<version>`, so a reviewer who has never
touched the part can do:

```console
$ git fetch origin '+refs/plm/*:refs/plm/*'
$ git diff -M -C --find-copies-harder <base>..refs/plm/drafts/KML-PRT-1012/v0.3.2
 versions/v0.3.2/MANIFEST.sha256                       |  2 ++
 versions/v0.3.2/VERSION.md                            | 17 +++++++++++++++++
 versions/{v0.3.1 => v0.3.2}/lantern_gobo_bearing.scad |  2 +-
```

The copy-detection flags are not optional: a revision is a *new version folder*,
so a plain `git diff` reports 478 added lines instead of the one line that
changed. With them, ordinary git tells you the actual design change.

`refs/plm/incoming/…` is the other half — the tree as the pusher actually handed
it over, before plm rewrote it into its own layout. `git diff <draft> <incoming>`
is "what plm changed on the way in", which is how the flattening below is visible
rather than surprising.

Both survive a rebuild: incoming refs are re-fetched from the previous repo
(their objects cannot be re-derived), and draft refs are re-derived from the
ledger by matching each proposal's vault commit against the `Plm-Vault-Commit`
trailer on the replayed commits. A draft ref is deleted as soon as plm says the
revision is no longer DRAFT/IN_REVIEW — the ref exists to review something
pending.

### Two things about the vault, stated plainly

**The vault stays linear and plm-authored.** Every commit in it still comes from
plm's own check-in code, so the registry and the vault cannot disagree. plm-git's
vault mount is still `:ro`, *with push on* — it never writes CAD, it asks plm to.

**`git push` never means "released."** It means "proposed". That is the correct
meaning for a system whose job is to gate physical fabrication.

### Enabling push against prod — the checklist

Three independent switches, and the code refuses at the door if any is missing.
None of them is a default.

| # | Flag | What it means |
|---|---|---|
| 1 | `PLMGIT_ALLOW_PUSH=1` | receive-pack is served at all, and generated repos get `http.receivepack=true` |
| 2 | `PLMGIT_PUSH_PLM_URL=<instance>` | which plm receives the DRAFTs. Defaults to the sandbox |
| 3 | `PLMGIT_PUSH_ALLOW_PROD=1` | required when the target does not look like a sandbox |
| + | `PLMGIT_PUSH_TOKENS_FILE=…` | `<token>=<plm username>` lines; without it every push is a 401 |

Before flipping them on the system of record, in this order:

1. **Put plm-git on the service framework.** It still runs its own compose from
   the code repo with no `service.yaml` and no deploy folder — the thing plm
   itself stopped doing on 2026-08-12. A service that writes to the vault must not
   be the one service nobody can restore from backup.
2. **Decide identity properly.** The token map is honest but it is plm-git's own
   scheme. Prod plm has OIDC; the push path should verify a *provider* token
   against plm (`GET /auth/whoami` with the Bearer) and use the username plm
   resolves, so there is one identity plane and not two. The token map stays as
   the bot fallback.
3. **Rebuild the prod image** (`docker build -t plm-git:latest .`) — the prod
   container has no code bind-mount, so it is still running the pre-push build.
   That is why prod could not have accepted a push during this work even by
   accident.
4. **Route it.** `client_max_body_size 0`, `proxy_request_buffering off` and a
   generous `proxy_read_timeout` are already in the documented nginx block below;
   push needs all three, and a 200 MB packfile needs the first one.
5. **Cap and watch.** `PLMGIT_PUSH_MAX_*` default to plm's own limits. There are
   no metrics; a push failing halfway leaves a DRAFT that a human has to notice.
6. **Rehearse a rejection.** Run `demo/push-demo.sh` against the sandbox after
   any change to the hooks. It exercises the happy path and six refusals.

---

## What it punts on

Named, not hidden. Each of these is a real gap, not a rough edge.

**`main` carries DRAFTs, because the vault does.** This is the biggest honest
gap the write path exposed, and it is a *finding*, not a bug in the code. plm
commits to the vault at **check-in**, not at release — so the moment a proposal
becomes a DRAFT, the extracted `main` carries it, and `git diff main...FETCH_HEAD`
against a fresh clone is empty. The release gate is the revision's **state**, not
the branch. The right fix exists and is not built: `revisions.git_commit` already
maps every vault commit to a revision, so the replay could emit `main` from
*released* revisions only and leave DRAFT commits reachable exclusively from
`refs/plm/drafts/*`. Then the git-side story ("main is what is released, drafts are
proposals") would be true rather than nearly true. Until then the push output
prints the base commit to diff against, and says why.

**Offloaded binaries are Git LFS objects.** With `PLM_BLOBSTORE_DEFAULT=lfs`, a
check-in's STL/PNG go to plm's LFS store and the tree carries a pointer — so a
`git clone` with git-lfs gets the mesh, and `GIT_LFS_SKIP_SMUDGE=1` clones the
text only (docs/LARGE-FILES.md).

**plm flattens version folders.** `plm/vault.py write_checkin` takes the
*basename*, so a pushed `versions/v0.3.2/cad/x.scad` lands as
`versions/v0.3.2/x.scad`. Pushes are accepted and the move is reported line by
line in the push output and in the context entry (and colliding basenames are
refused), so nothing is silent — but the first round-trip through plm-git
reorganises a legacy part's folders. It converges after one revision. The
alternatives are to teach plm nested paths or to refuse nested trees; reporting
was chosen because refusing would break the natural `cp -r versions/v0.3.0
versions/v0.3.1` move on every hand-built part in the vault.

**Deletions and file removals are refused, not translated.** A push that removes
a controlled file is refused with a pointer at plm's `DELETE /api/files/<id>`.
Correct-by-default, and incomplete: there is no way to say "this revision drops
that artifact" through git.

**Auth / identity: two planes, not one.** The read path has no auth at all —
anyone who can reach `:8099` can clone any part, which is the same
trusted-network posture the other internal services started from and is *only*
acceptable while the service is private-network-only. The write path has real auth, but
it is plm-git's own token map (`<token>=<plm username>`), not plm's OIDC. plm-git
does verify the mapped actor against plm (`/api/users`, `editor` role required)
so it cannot invent authority — but there are two credential stores where there
should be one. The prod fix is in the enabling checklist above.

**Only one proposal at a time, and no merge semantics.** Concurrency is handled
by refusing: a proposal must be built on the current `main` (so the second of two
racing pushes is told to rebase), and plm allows one open DRAFT per item (so a
second proposal is refused until the first is decided). Nobody has to resolve a
conflict — which is the same thing as saying **nothing merges**. Two engineers
who both want to change a part serialise, exactly as they do in plm today. Real
concurrent proposals would need plm to allow parallel DRAFTs on one item, which is
a data-model decision (Teamcenter's answer is branches/variants), not a transport
one.

**No LFS, no partial clone.** A per-file cap of 192 MiB (plm's own
`PLM_RAW_UPLOAD_MAX`) and 256 MiB per push, enforced in pre-receive with a
message that says git is the wrong transport for objects that size. Nothing
supports `git-lfs`, and a part whose history holds many large meshes will make
`git clone` slow — the offload above accidentally mitigates this for new
revisions.

**The write path is not on the service framework.** Its config lives in the code
repo's compose, there is no `service.yaml`, no deploy folder, no backup of the
proposal ledger under `$PLMGIT_CACHE/proposals/` (which is the only record of
*what was pushed* as distinct from what plm accepted). A service that writes to
the vault should not be the one nobody can restore.

**A push that fails halfway leaves a DRAFT.** post-receive cannot fail a push, so
if plm refuses at `put_files` or `submit` the DRAFT `revise` already created stays
— visible, unsubmitted, and named in the output, with the withdraw command. That
is deliberate (silently deleting a revision to tidy up is a worse lie than an
untidy registry), but it means a human has to notice.

**No CI, no rendering, no checks.** The obvious next thing after "push works" is
"push runs the slicer" — a `post-receive` that renders the `.scad`, compares it to
the pushed STL and attaches the result as a context entry or an attribute. The
seam is there (plm's `set_attributes` and the integrations framework); nothing
uses it.

**Folder renames mid-history are heuristic, not proven.** The number-anchored +
longest-common-prefix rule recovers every rename in this vault's actual history,
and it degrades sensibly. But it can be fooled:

- A **renumber whose old number was reused** by a different item would splice two
  unrelated histories together. plm allocates numbers monotonically, so this is
  currently impossible rather than merely unlikely — but it is an assumption, not
  a check.
- If an item's folder were moved to a *different project*, the LCP rule could
  briefly prefer a same-project decoy (like the `old/local-registry` archive) for
  the commits around the move.
- A **rename in the same commit as a content change** is recorded as one commit
  in the extracted history (correct, but the rename is invisible unless you read
  the `Plm-Vault-Path` trailer).
- History is **linearised**: merges in the vault would be flattened. The vault has
  zero merge commits today (plm commits sequentially), so this costs nothing yet.

The honest mitigation is that the trailers make every extracted commit traceable
to the monorepo commit it came from, so a bad lineage is *diagnosable* rather than
silent — and a rebuild is cheap and deterministic.

**Monorepo-scale performance.** Lineage discovery is `git ls-tree -r -d` per
commit: O(commits × repo breadth). At today's 113 commits / 150 MB that is
0.8–3 s per part, and a cached, unchanged part costs 0.02 s. It will not hold at
10⁴ commits. The fix is known and not done: walk `git log --raw` once to get
changed paths per commit instead of listing every tree, and keep a persistent
prefix-lineage index instead of rediscovering it per build.

**Cache is not garbage-collected.** Generated repos accumulate in
`~/.cache/plm-git/repos/`; nothing prunes them. The mirror is a full second copy
of the vault (~150 MB).

**No assemblies-as-submodules.** The obvious next move — an assembly cloning as a
repo whose BOM lines are git submodules pinned to each child's revision commit —
is not built. plm has the BOM (`/api/items/{n}/bom`) and now has stable per-part
commits, so the pieces exist.

**Concurrency and robustness.** Per-part in-process locking only, so two
processes (host CLI + container) could race a rebuild; the atomic rename limits
the damage to wasted work. No metrics, no structured logs, no readiness probe
beyond `/healthz`. A vault fetch mid-check-in can see a slightly older ref — it
self-corrects on the next request.

**Only `upload-pack` is exercised.** `git clone`, `git fetch`, shallow clone and
protocol v2 work. `git archive --remote` and dumb HTTP are untested.

---

## Integration into plm proper

The service is a plm satellite, not a new stack: it already joins plm's own
compose network (`plm_default`) and resolves plm by name. Two things remain.

### 1. Route it on the existing vhost — `plm.example.com/git/`

Recommended over a new `git.plm.example.com`: one hostname, one cert, one DNS
record, and the URL reads correctly — the part registry serves the part repos.
Needs one `location` block in
your nginx site config for `plm.example.com`
(**plm-side nginx change — noted, not made**):

```nginx
# git smart HTTP for per-part repos -> localhost:8099 (plm-git)
location /git/ {
    proxy_pass http://<plm-git-host>:8099;   # private network address
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # git needs these three or clones break in confusing ways:
    proxy_set_header Git-Protocol      $http_git_protocol;  # else v2 -> v0
    proxy_buffering                    off;   # sideband progress must stream
    proxy_request_buffering            off;   # large POSTs (push, later)
    client_max_body_size               0;     # packfiles
    proxy_read_timeout                 300s;
    proxy_send_timeout                 300s;
}
```

Then `git clone https://plm.example.com/git/KML-PRT-1009.git` — and every `nginx
-t` / reload / rollback step in
`devops/docs/projects/2026-08-11-plm-doempke-com/` applies unchanged. The
alternative, `git.plm.example.com`, is worth it only if git traffic ever needs
its own rate limits or its own auth realm.

An interim option needing no plm-side change at all: keep it private-network-only at
`http://localhost:8099/git/…`, which is what the POC does. Machine plane only,
no humans typing it.

### 2. Identity: reads anonymous, writes tokened

- **Reads (today).** No auth, matching the other internal services. Acceptable
  *only* while exposure is private-network-only.
- **Writes (today).** A bearer token, consumed the ordinary git way — no plugin,
  no plm client:

  ```bash
  git -c http.extraHeader="Authorization: Bearer $PLM_TOKEN" \
      push origin HEAD:refs/for/main
  # or once per clone
  git config --local http.extraHeader "Authorization: Bearer $PLM_TOKEN"
  # or let a credential helper answer the 401 (Basic, password = the token)
  git config --global credential.https://plm.example.com.helper '!plm-credential'
  ```

  plm-git maps the token to a plm username (`PLMGIT_PUSH_TOKENS_FILE`), then asks
  **plm** whether that user exists and holds `editor`. It never invents an actor
  and never grants a role. The resolved name becomes `REMOTE_USER` plus
  `PLMGIT_ACTOR*` in the hook environment, and plm records it as the DRAFT's
  author.

  A 401 carries `WWW-Authenticate: Basic`, which is what makes credential helpers
  work — without it git cannot tell which scheme to use and simply hangs. Agents
  should set `GIT_TERMINAL_PROMPT=0` so a missing token fails immediately.

- **Next.** One credential plane, not two: verify a *provider* access token
  against plm (`GET /auth/whoami` with the Bearer — plm already introspects and
  caches, `plm/identity.py`) and use the username plm resolves; keep the token map
  as the bot fallback. Approvals do not move: human OIDC in plm,
  `PLM_REQUIRE_OIDC_APPROVALS=1`.

### Recommended order

1. Land the read path: the nginx `/git/` location, plus bearer verification.
   Low risk, immediately useful — agents and humans can clone parts.
2. Fix the two known scaling items (`git log --raw` lineage walk, persistent
   prefix index) before the vault is large enough for it to hurt.
3. Make `main` mean *released* (see the first entry under *What it punts on*).
   That is the one change that turns "nearly true" into "true", and it is the
   prerequisite for the git-side story being safe to teach widely.
4. Then the write path on prod, per the *Enabling push* checklist — framework
   first, identity second.
5. Assemblies as submodules, once per-part commits are trusted in anger.

---

## Layout

```
plm-git/
├── README.md              this
├── docs/AGENT-INSTRUCTIONS.md   what an AGENT needs: when to clone, how to
│                                propose, the refusal catalogue. Ready to paste
│                                into plm's MCP instructions.
├── sample.env             every knob, documented; copy to .env
├── docker-compose.yml     plm-git (:8099, read-only) + plm-git-sandbox (:8199,
│                          push ENABLED against plm-sandbox)
├── Dockerfile             python:3.12-slim + git. no pip.
├── demo/demo.sh           the read path, end to end
├── demo/push-demo.sh      the write path, end to end (sandbox only; refuses
│                          to run against a non-sandbox plm)
└── plm_git/
    ├── config.py          all host-specific values, nowhere else
    ├── resolve.py         number -> vault folder (plm API, then DB mode=ro)
    ├── extract.py         mirror -> lineage -> replay -> bare repo (+ CLI),
    │                      hook install, proposal/draft refs
    ├── server.py          git-http-backend CGI bridge, freshness trigger, and
    │                      THE DOOR for receive-pack (token -> plm actor)
    ├── push.py            the write path: gate, identity, validation, proposal
    ├── hook.py            pre-receive / post-receive, as one module
    └── plmapi.py          the only way plm-git writes: plm's own HTTP API
```

### CLI

```bash
python3 -m plm_git.extract KML-PRT-1009              # build/refresh
python3 -m plm_git.extract KML-PRT-1009 --force      # rebuild regardless
python3 -m plm_git.extract KML-PRT-1009 --lineage    # show the folder lineage
```

### HTTP

| Route | Purpose |
|---|---|
| `GET /` | plain-text index of what is cached, and the push status |
| `GET /healthz` | liveness + cache summary + `push` (enabled? writing to WHAT? which actors?) |
| `GET /api/parts` | every plm item with a vault folder |
| `GET /api/parts/<NUMBER>` | resolution, staleness, and the proposal ledger for one part |
| `POST /api/parts/<NUMBER>/refresh` | force a rebuild |
| `* /git/<NUMBER>.git/…` | git smart HTTP — `git clone` and `git push` land here |

### Refs a per-part repo serves

| Ref | Who writes it | What it is |
|---|---|---|
| `refs/heads/main` | plm-git, from the vault | the part as the vault has it (**including open DRAFTs** — see *punts*) |
| `refs/plm/drafts/<N>/v<ver>` | plm-git, from the ledger | the PR view of an open proposal; deleted when plm decides |
| `refs/plm/incoming/<N>/<actor>/<ts>` | post-receive | the tree as the pusher handed it over — the receipt |
| `refs/for/main` | transient | where you push. Hidden from `upload-pack`, deleted after every push |

### Safety invariants

- The vault is mounted **read-only — with push enabled too.** plm-git never
  writes CAD; it asks plm to, over HTTP. If this service ever needs write access
  to a vault, something has gone wrong in the design.
- Every extraction runs against a `--no-hardlinks` mirror clone, so the cache
  shares no object files with live CAD.
- plm's SQLite index is only ever opened `mode=ro`.
- With `PLMGIT_ALLOW_PUSH=0` (the default), receive-pack is refused at the router
  **and** `http.receivepack=false` in every generated repo — two independent
  reasons a push cannot land.
- With push on, a rejected push leaves **zero** objects: pre-receive runs inside
  git's quarantine, and a non-zero exit deletes it.
- `refs/heads/*` and `refs/plm/drafts/*` are refused by `pre-receive` *and* by an
  independent `update` hook, plus `receive.denyDeletes` and
  `receive.denyNonFastForwards`.
- The only commits plm-git creates live inside the generated per-part repos.
