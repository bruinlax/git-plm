"""plm MCP — the part registry for agents.

FastMCP over streamable-HTTP.
Reads are straight from the SQLite index (fast); writes are the same audited
operations the HTTP API exposes: revise, propagate, checkout, state.

The tool agents will reach for most: what_changed(number) — "what changed in
this part?" — which diffs the two most recent revisions via git (or the vault
folder fallback) and pairs it with the revision summaries.
"""
from __future__ import annotations

import base64
import os
from typing import Any

from fastmcp import FastMCP

from plm import (attributes as attrsmod, config, context as contextmod, db,
                 fab, gitlinks, gitrepo, ops, policy as policymod, reclassify,
                 relations, roles as rolesmod, search as searchmod, snippets,
                 staging, storage, tags as tagsmod, thumbs, vault)

from plm_mcp import instructions as _instructions

mcp = FastMCP(
    "plm",
    instructions=_instructions.for_instance(),
)


# -- the git block, gated by PLM_GIT_MODE (stage 9) -----------------------------
#
# A clone URL is a promise that there is a repository to clone. In a project
# whose mode is `off` there is none, so the block is absent — and on an instance
# where git is off EVERYWHERE (production at rollout step 1) the payload carries
# no `git` / `git_mode` keys at all, exactly as master's did. Where git is on
# somewhere, every item says its `git_mode`, and `git` is null for an `off` one.

def _git_world() -> bool:
    from plm import gitmode
    return gitmode.any_serves()


def _git_brief_into(conn, row: dict) -> None:
    from plm import gitmode
    if not _git_world():
        return
    mode = gitmode.for_item(conn, row.get("number") or "")
    row["git_mode"] = mode
    row["git"] = (gitlinks.git_brief(row.get("number", ""))
                  if mode in ("read", "write") else None)


def _git_block_into(conn, item: dict) -> None:
    from plm import gitmode
    if not _git_world():
        return
    e = gitmode.explain(item.get("project") or "", conn)
    item["git_mode"] = e["mode"]
    item["git"] = (gitlinks.git_block(item["number"])
                   if e["mode"] in ("read", "write") else None)


def _ok(**kw: Any) -> dict:
    return {"ok": True, **kw}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


@mcp.tool
def search(q: str, limit: int = 20) -> dict:
    """Full-text search across part numbers, names, change summaries,
    verification notes, CONTEXT entries and attached markdown docs. Use for
    questions like 'find the part we modified because the deck sagged'.
    Tries all terms first, falls back to any-term, bm25-ranked.

    Each hit carries `git` — `clone_url` and `browse_url` — when that item has a
    repo, so the next step after finding a part can be a clone instead of
    another tool call."""
    with db.connect() as conn:
        hits = searchmod.query(conn, q, min(limit, 100))
        for h in hits:
            _git_brief_into(conn, h)
    return _ok(hits=hits)


@mcp.tool
def add_context(number: str = "", body: str = "", version: str = "",
                actor: str = "mcp", target_type: str = "item",
                target_id: str = "") -> dict:
    """Attach a markdown context entry — the WHY — to an item (version='') or a
    specific revision. Context is append-only and searchable.

    Context is GENERIC: it is comments, and a comment can hang off any object in
    the registry, not just a part. Leave target_type alone and this behaves
    exactly as it always has (number + optional version). To comment on
    something else, pass target_type + target_id:

      relation  — a typed link: why this coupon verifies that revision, why this
                  build is a bench test and not a product. target_id is the
                  relation id from get_item()'s `related` block; read them back
                  with list_related(number, with_comments=True).
      approval  — a release submission: review remarks, the condition you want
                  met. target_id is the approval id from pending_approvals().
      item /    — the classic pair, addressable directly: target_id is an item
      revision    number/id, or a revision id.

    Whatever you attach it to, the entry is indexed for search()."""
    with db.connect() as conn:
        try:
            rec = contextmod.add(conn, body, target_type=target_type,
                                 target=target_id or number, version=version,
                                 actor=actor)
        except contextmod.ContextError as exc:
            return _err(str(exc))
        searchmod.touch(conn, rec["item_id"])
    return _ok(number=number, version=version, context=rec)


@mcp.tool
def context_targets() -> dict:
    """What a context entry (a comment) may be attached to: every target type,
    its label and what it is for. The registry is the single source of truth —
    read it if you are unsure what add_context's target_type accepts."""
    return _ok(targets=contextmod.targets())


@mcp.tool
def find_items(q: str = "", kind: str = "", project: str = "", state: str = "",
               related_to: str = "", relation: str = "",
               direction: str = "both") -> dict:
    """Search the registry. kind: part|assembly|purchased|hardware|firmware|
    document|coupon|asset.

    related_to/relation/direction filter by a typed link instead of by text —
    e.g. related_to='KML-PRT-1011', relation='coupon_for', direction='in'
    answers "which coupons verify this part?" (direction is from the named
    item's point of view: 'in' = links pointing AT it), and
    related_to='CCR-AST-0001', relation='mounts_to', direction='in' answers
    "what bolts to this camper?". Any registry type works the same way; see
    relation_types()."""
    hint: dict = {}
    with db.connect() as conn:
        items = db.list_items(conn, q, kind, project, state)
        if project and not items:
            # stage 15: run 2 asked project='lantern' and got [] — the project
            # is `lantern-gobo-bearing`. A name that is not a project but is
            # part of exactly one is that project, and the answer says so.
            names = [r[0] for r in conn.execute(
                "SELECT DISTINCT project FROM items WHERE project<>''")]
            if project not in names:
                like = sorted(n for n in names if project.lower() in n.lower())
                if len(like) == 1:
                    items = db.list_items(conn, q, kind, like[0], state)
                    hint["project_matched"] = like[0]
                else:
                    hint["no_project_named"] = project
                    hint["projects_like"] = like or sorted(names)
        if related_to:
            try:
                keep = set(relations.related_numbers(
                    conn, related_to, type_=relation, direction=direction))
            except relations.RelationError as exc:
                return _err(str(exc))
            items = [i for i in items if i["number"] in keep]
    # `git`: clone + browse links per row (one stat each, so a listing stays a
    # listing). The full block — refs, counts, compare link — is get_item's.
    with db.connect() as conn:
        for i in items:
            _git_brief_into(conn, i)
    return _ok(items=items, **hint)


@mcp.tool
def get_item(number: str) -> dict:
    """One item: revision ledger (newest first) with each revision's files and
    ATTRIBUTES (mass, material, bounding box… — see set_attributes), where-used,
    sourcing, context (the why-log), `related` (typed links BOTH directions —
    what this verifies, what verifies it, what it was forked from), and — when
    present — `instructions`: the standing INSTRUCTIONS.md carried on the current
    revision. READ INSTRUCTIONS BEFORE WORKING ON THE PART — they carry the
    working agreements (when to rev, who to notify, how to verify).

    `git` is the part's repository: `clone_url` (releases are tags, drafts are
    `draft/*` branches, BOM children are submodules under `parts/`),
    `default_ref` — what a bare clone checks out, NOT always `main` —
    `default_release`, `releases_count`, `drafts_count`, `browse_url` (the page
    to hand a human) and `compare_url` (previous release vs latest, null when
    there is only one release), and `lfs` — whether a clone of `default_ref`
    needs git-lfs for its meshes (`needs_git_lfs`, `files`, `bytes`; with
    `GIT_LFS_SKIP_SMUDGE=1` the clone is text only). If the question is about
    FILES, HISTORY or a DIFF, clone it: `git clone --recurse-submodules
    <clone_url>`. It is null for
    an item with no repo — purchased and hardware items live in `catalog.git`.

    `number` may be the item's number, its integer id, or a number it USED to
    have before a renumber. Item and revision ids come back in the payload;
    plm owns both keys."""
    with db.connect() as conn:
        item = db.get_item(conn, number)
        if not item:
            return _err(f"unknown item: {number}")
        p = thumbs.Presence(conn)
        # Where the part lives as a repo: clone / browse / compare (plm/gitlinks.py).
        # Null when this item has no repo, which is an answer, not a failure.
        _git_block_into(conn, item)
        item["where_used"] = db.where_used(conn, item["id"])
        # Typed relations, already labelled from this item's side, so an agent
        # SEES "verified by KML-CPN-0001 v0.1.0" without a second call.
        item["related"] = relations.for_item(conn, item["id"], presence=p)
        for r in item["revisions"]:
            r["files"] = item["files"].get(r["id"], [])
            # Same field, same meaning as the HTTP payloads (plm/thumbs.py): a
            # URL when this revision has geometry to picture (its own mesh or an
            # inherited one), null when there is nothing to render or to view in
            # 3D. An agent asked to "show Geoff the part" reads this, and a UI
            # built on the MCP never has to probe for a 404.
            r["thumb"] = p.url(item["number"], r["version"], r["id"],
                               item["kind"])
        del item["files"]
        item["context"] = [dict(r) for r in conn.execute(
            """SELECT c.body, c.actor, c.created_at, r.version FROM context c
               LEFT JOIN revisions r ON r.id=c.revision_id
               WHERE c.item_id=? ORDER BY c.created_at DESC""", (item["id"],))]
        # Standing instructions ride the current revision (carried forward by
        # revise; edited on DRAFTs via put_file) — the item itself is just
        # the identity that groups revisions.
        item["instructions"] = None
        if item["revisions"]:
            for f in item["revisions"][0]["files"]:
                name = f["path"].rsplit("/", 1)[-1].lower()
                if name in ("instructions.md", "work_instructions.md"):
                    try:
                        # .md is pinned to the vault, but resolve through the
                        # storage layer anyway so this never depends on it.
                        p = storage.local_copy(dict(f))
                        if p.is_file() and p.stat().st_size < 40_000:
                            item["instructions"] = p.read_text(errors="replace")
                    except (storage.StorageError, vault.VaultError, OSError):
                        pass
                    break
        if _git_world():
            # stage 15: each file's `menu` is the WEB UI's (labels, help text,
            # widget names) — 43 KB of KML-PRT-1012's 74 KB, which a cold
            # agent had to page out of a persisted tool result. Agents get the
            # file facts and `actions` (master's contract); the menu stays on
            # the HTTP payloads the UI reads. Off-everywhere instances keep
            # master's shape.
            for r in item["revisions"]:
                for f in r["files"]:
                    f.pop("menu", None)
        # stage 20: on the legacy path of an instance that knows
        # where git-first lives, say so where the agent is reading
        from plm import routing
        route = routing.for_item(conn, item)
        return _ok(item=item, **({"routing": route} if route else {}))


@mcp.tool
def what_changed(number: str, a: str = "", b: str = "") -> dict:
    """What changed in this part? Defaults to previous→latest revision.
    Text sources (.scad, .py, .csv, .md) come back as unified diffs; binary
    geometry (.stl) is reported as size/hash changes.

    This reads the VAULT. For the git-first answer — manifest delta, BOM child
    pin changes with the children's reasons, and the repo's own diff — use
    compare_revisions(number, base, head), or clone and `git diff <a> <b>`."""
    with db.connect() as conn:
        item = db.get_item(conn, number)
        if not item:
            return _err(f"unknown item: {number}")
        revs = item["revisions"]
        if len(revs) < 2 and not (a and b):
            return _err(f"{number} has {len(revs)} revision(s) — nothing to compare")
        b = b or revs[0]["version"]
        a = a or next((r["version"] for r in revs if r["version"] != b), b)
        ra = db.find_revision(conn, number, a)
        rb = db.find_revision(conn, number, b)
        if not ra or not rb:
            return _err("unknown revision")
        try:
            diff = ops.revision_diff(conn, number, a, b)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(number=number, a=a, b=b,
               a_summary=ra["summary"], b_summary=rb["summary"],
               a_state=ra["state"], b_state=rb["state"], diff=diff)


# A compare answer is a SUMMARY, not a patch dump: enough to see the shape of
# the change, and a clone command for the rest. These two caps are what keeps it
# that way (bytes of diff per file, and bytes of diff in one answer).
_PATCH_PER_FILE = 2000
_PATCH_TOTAL = 12000


@mcp.tool
def list_refs(number: str) -> dict:
    """This item's RELEASES (git tags `vX.Y.Z`) and DRAFTS (git branches
    `draft/vX.Y.Z`), newest version first, each with its commit, date, state,
    maturity and summary — the revision ledger as git holds it.

    `default_ref` is what a bare clone checks out and is NOT always `main`: an
    item with no release has no `main` at all. `default_release` is the newest
    tag, `""` when there is none.

    This is one tool call instead of a clone. When you want the FILES, clone it:
    `git clone --filter=blob:none <clone_url>` then `git tag` / `git branch -r`.
    """
    try:
        r = gitrepo.refs(number)
    except gitrepo.RepoError as exc:
        return _err(str(exc))
    block = gitlinks.git_block(number) or {}
    return _ok(number=r["number"], kind=r["kind"],
               clone_url=r["clone_url"], default_ref=r["default_ref"],
               default_release=r["default_release"],
               releases=r["releases"], drafts=r["drafts"], counts=r["counts"],
               browse_url=block.get("browse_url"),
               hint=f"git clone --recurse-submodules {r['clone_url']}")


@mcp.tool
def compare_revisions(number: str, base: str, head: str) -> dict:
    """What changed between two refs of one item — the same delta the repo page
    shows. `base` and `head` are refs: a release tag (`v0.4.0`), a draft branch
    (`draft/v0.17.0`), or a commit.

    Four independent parts:
      files           per-file status + line counts. `binary: true` means the
                      line counts are meaningless (an ASCII STL yields 90 000
                      "added lines"). Patches are CAPPED — see `hint`.
      manifest_delta  the PLM half, from plm.toml: fields, attributes, BOM
                      lines, file roles, relations, routing, tags.
      submodules      BOM child pin changes, each with the CHILD's own commit
                      subjects between the two pins — a BOM delta with reasons.
      commits         `git log base..head`.

    For the full text diff, clone and run `git diff <base> <head>` — the tree IS
    the revision here, so that plain diff is the real design change (never pass
    `-M -C --find-copies-harder`; that was the old version-folder layout).
    """
    try:
        cmp = gitrepo.compare(number, base, head, max_patch=_PATCH_PER_FILE)
    except gitrepo.RepoError as exc:
        return _err(str(exc))
    budget, dropped, files = _PATCH_TOTAL, 0, []
    for f in cmp["files"]:
        patch = f.get("patch") or ""
        if len(patch) > budget:
            patch, dropped = "", dropped + 1
        else:
            budget -= len(patch)
        files.append({**f, "patch": patch})
    clone = gitrepo.clone_url(number)
    hint = (f"patches capped at {_PATCH_PER_FILE} bytes per file"
            + (f"; {dropped} file(s) have none left" if dropped else "")
            + f". For the full text diff: git clone {clone} && "
              f"git diff {base} {head}")
    return _ok(number=number, base=cmp["base"], head=cmp["head"],
               stats=cmp["stats"], commits=cmp["commits"], files=files,
               manifest_delta=cmp["manifest_delta"],
               submodules=cmp["submodules"],
               clone_url=clone,
               compare_url=gitlinks.compare_url(number, base, head),
               hint=hint)


# -- STAGE 6 HOOK: one tool (PLM_REVIEW_MODEL §2.6; plm/checks.py) ------------
@mcp.tool
def get_checks(number: str, ref: str = "", target_maturity: str = "",
               wait: float = 0) -> dict:
    """What plm MEASURED about one commit — CI for physical things (§2.6).

    `ref` is a tag, a draft branch, or a SHA; it defaults to this item's
    `default_ref`. `number` may also be a pull request (`CHG-380`): its own
    checks — `physical-diff`, whose `images` (GitHub's output.images: alt,
    image_url, caption) picture every changed mesh, old vs new. Every row is keyed by the commit, so a re-push gets a fresh
    set and "the checks were green" always means green on the bytes that
    shipped.

    Each row: `status` (queued|running|pass|fail|warn|skipped|error), a
    `summary`, `details_md`, measured `facts`, and `points_at` — the files the
    finding is about. **A `skipped` row carries the REASON** (no `[source]`, a
    mesh outside the tree, a selector plm cannot reproduce); it is never a
    pass.

    `rollup.state` is the one-word answer, and `target_maturity`
    ("prototype" / "released") decides which checks are REQUIRED (§2.3), so the
    same board reads amber on a prototype and blocking on a release.

    Checks never block a push. Read them after you push and fix the red ones
    yourself; that is the work an approver would otherwise have to send back.

    Checks run in the background after a push (a re-render takes seconds to
    minutes). `wait` = up to that many seconds (max 300) for this commit's
    checks to finish — instead of sleeping and polling. The answer says
    `running` (the names still queued/running) and `done` (true when nothing
    is) — never a partial board passed off as a whole one.
    """
    from plm import changes, checks as checksmod
    subj = checksmod.subject(number)
    number = subj.number
    try:
        sha, r = subj.resolve("", ref)
    except (gitrepo.RepoError, changes.ChangeError) as exc:
        return _err(str(exc))
    applies = checksmod.applies_to(number, sha)
    if wait:
        rows = checksmod.wait_settled(number, sha, wait)
    else:
        with db.connect() as conn:
            rows = checksmod.read(conn, number, sha)
    running = [c["name"] for c in rows if c["status"] in ("queued", "running")]
    if not rows and checksmod.pending(number, sha):
        running = ["(requested, not started)"]
    status = {"done": not running and bool(rows), "running": running}
    if running:
        status["hint"] = (f"still running at {sha[:10]}: {', '.join(running)} — "
                          f"get_checks('{number}', ref='{r}', wait=120)")
    if not rows:
        return _ok(number=number, ref=r, sha=sha, checks=[], applies=applies,
                   rollup=checksmod.rollup([], target_maturity=target_maturity,
                                           applicable=applies),
                   **status,
                   hint=status.get("hint") or
                        f"nothing has been measured at {sha[:10]} yet — POST "
                        f"/api/repo/{number}/checks/run {{\"sha\":\"{sha}\"}} "
                        "queues them")
    return _ok(number=number, ref=r, sha=sha, checks=rows, applies=applies,
               rollup=checksmod.rollup(rows, target_maturity=target_maturity,
                                       applicable=applies),
               **status, registry_url="GET /api/checks")
# -- end stage 6 hook ---------------------------------------------------------


@mcp.tool
def get_bom(number: str, version: str = "", rolled: bool = False) -> dict:
    """Bill of materials for a revision (default latest). rolled=True flattens
    the tree and multiplies quantities.

    Each line carries `thumb`: the child's render URL, or null when that child
    has no geometry at all (a screw, a document). Null means "there is no
    picture and no 3D to view" — never ask for one."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        p = thumbs.Presence(conn)
        lines = p.rows(db.rolled_bom(conn, rev["id"])) if rolled \
            else p.bom(db.get_bom(conn, rev["id"]))
        from plm import requirements
        reqs = requirements.for_revision(conn, rev["id"])
        return _ok(number=number, version=rev["version"], rolled=rolled, bom=lines,
                   # stage 19: purchases not selected yet (additive, only
                   # when the revision has `[[bom]] requirement` lines)
                   **({"requirements": reqs} if reqs else {}))


@mcp.tool
def activity(limit: int = 20) -> dict:
    """Most recent revisions across the registry — what changed lately."""
    with db.connect() as conn:
        return _ok(activity=db.activity(conn, min(limit, 100)))


@mcp.tool
def checkout(number: str, actor: str, note: str = "") -> dict:
    """⚠️ RETIRED — kept so old scripts keep working. It locks nothing.

    Branches replaced locks (docs/PLM_GIT_FIRST.md §5.2): two agents working on
    one part get two `draft/*` branches and a merge, which is better than one of
    them waiting. On any item that lives in a repo this succeeds, records an
    event and blocks nothing, and the answer says `advisory: true`.

    You do not need to call it. Just work: clone, branch, push — or revise() and
    edit the draft.
    """
    with db.connect() as conn:
        try:
            res = ops.claim_lock(conn, number, actor, note=note)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def release_checkout(number: str, actor: str = "mcp", reason: str = "",
                     force: bool = False) -> dict:
    """⚠️ RETIRED alongside checkout() — on a git-first item it releases nothing
    and succeeds, because nothing was held.

    Yours: always. A STALE lock (older than the staleness threshold — the agent
    holding it is gone): always, and you do not need force for it; plm also takes
    a stale lock over by itself the next time anybody writes, with a
    checkout.takeover event. Another actor's LIVE lock: force=True plus a
    reason, and only a HUMAN identity may do it — deciding that somebody else's
    work in progress no longer matters is not a bot's call. list_checkouts()
    shows what is held and what is stale."""
    with db.connect() as conn:
        try:
            res = ops.force_release_checkout(conn, number, actor=actor,
                                            reason=reason, force=force)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def list_checkouts(stale_only: bool = False) -> dict:
    """Every live checkout in the registry, oldest first — with its age and
    whether that age makes it STALE.

    Use it when a write is refused with 'checked out by X': this says whether X
    is still working or died three days ago. A stale lock needs no permission to
    reclaim (release_checkout, or just write — plm takes it over and records
    the takeover); a live one belongs to somebody."""
    with db.connect() as conn:
        rows = db.list_checkouts(conn, stale_only=stale_only)
    return _ok(count=len(rows), stale=sum(1 for r in rows if r["stale"]),
               stale_after_h=round(config.CHECKOUT_STALE_SECONDS / 3600, 1),
               checkouts=rows)


@mcp.tool
def revise(number: str, bump: str = "patch", summary: str = "",
           actor: str = "mcp", rebind: str = "keep") -> dict:
    """Rev an item up: new DRAFT revision seeded from the latest, files and BOM
    carried forward. bump: major|minor|patch. rebind: keep|latest|imprecise."""
    with db.connect() as conn:
        try:
            rev = ops.revise(conn, number, bump=bump, summary=summary,
                             actor=actor, rebind=rebind)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(number=number, revision={"version": rev["version"], "state": rev["state"]})


@mcp.tool
def propagate(number: str, version: str = "", actor: str = "mcp") -> dict:
    """Push a child's new revision up the tree: every ancestor whose latest
    revision still binds an older precise revision gets revised (patch bump)
    with the occurrence rebound. Returns the created revisions."""
    with db.connect() as conn:
        try:
            created = ops.propagate(conn, number, version, actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(number=number,
               created=[{"number": c["number"], "version": c["version"]} for c in created])


@mcp.tool
def create_item(name: str, kind: str, project: str, number: str = "",
                prefix: str = "", actor: str = "mcp",
                idempotency_key: str = "") -> dict:
    """Register a new item. kind: part|assembly|purchased|hardware|firmware|
    document|coupon|asset (`asset` = what you neither build nor buy and bolt TO;
    see add_asset, which registers one and links it in a single call).

    OMIT `number` and pass `prefix` (e.g. GL): plm assigns the next free number
    in the class that matches `kind`. That is the intended path — plm owns
    identity. An explicit `number` is validated server-side and must be
    <PREFIX>-<CLASS>-<SEQ> with CLASS one of PRT/ASM/PUR/HDW/FW/DOC/CPN AND
    agreeing with `kind`; invented classifications (OWNED, FUTURE, ELEC, MECH,
    REF…) are refused. Meaning goes in kind, project, name, attributes, context
    and relations — never in the number.

    RETRY-SAFE. Because plm mints the number, a retry after a timeout
    used to allocate a SECOND one and leave a duplicate shell behind. Now an
    identical create from the same actor within the idempotency window replays
    the first result (`replayed: true`) instead. If you are deliberately
    retrying, pass the same `idempotency_key` and the guarantee holds for a
    day."""
    from plm import routing
    with db.connect() as conn:
        # stage 20: asked BEFORE the item exists — "is this a NEW
        # project on the legacy path?" — and answered with the item, never
        # instead of it
        route = routing.for_create(conn, project)
        try:
            res = ops.create_item(conn, name, kind, project,
                                  number=number, prefix=prefix, actor=actor,
                                  idempotency_key=idempotency_key)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res, **({"routing": route} if route else {}))


@mcp.tool
def checkin(number: str, version: str, summary: str,
            files: list[dict] | None = None, bom: list[dict] | None = None,
            state: str = "PROTOTYPE", ecn: str = "", source: str = "",
            selector: str = "", actor: str = "mcp") -> dict:
    """⚠️ DEPRECATED — prefer `git push HEAD:refs/for/main -o maturity=<m>`.

    On an item that lives in a repo this is a COMPATIBILITY SHIM over git, and
    what it does has changed (stage 3c §1). One `checkin` is now:

      1. ONE commit on `draft/v<version>` — the revision, its files and its
         manifest together, built on the newest release so everything else
         carries forward by being the same tree;
      2. a submission at `state`;
      3. plm's approval POLICY decides. By default it waits for a human and
         comes back `IN_REVIEW`, wrapped in a pull request of one (`change.url` is
         the link to hand the approver). Only a "just build it" project merges it
         immediately, recorded as `approver: policy` — honestly, so the ledger
         says no human looked.

    So READ `state` IN THE ANSWER. It is what actually happened, not what you
    asked for. The old behaviour — mint `PROTOTYPE` with no approval record at
    all — is gone, and it is what 446 of 454 production revisions did.

    Pushing is better than this: your commit is kept byte for byte, so the
    approval is a signature over the exact bytes. See the GIT FIRST section.

    files, one form per entry:
      {name, text, role?}                     small documents (SOPs, notes)
      {name, content_b64, role?}              small binaries only
      {name, handle, sha256, size, role?}     ANYTHING BIG — plm's own upload()
              handle (`filedrop_key` is the legacy alias): plm minted it, so
              plm deletes it after the revision owns its copy.
      {name, url, sha256, size?, role?}       the same thing from any https
              link to the bytes that plm can reach. sha256 is REQUIRED and
              verified while plm streams the link down. plm does not delete
              the source object; it is not plm's.
    Roles: source (CAD) | artifact (print STL/gcode) | viz | drawing | doc —
    include the CAD source AND an STL mesh derivative.
    bom: [{child, version?, qty?, unit?, find?, notes?}] — omit version for an
    imprecise (auto-latest) binding. source/selector record how to regenerate
    (e.g. 'lantern_top_drive.scad', 'drive_part=top_cover')."""
    with db.connect() as conn:
        try:
            payload = staging.normalize(conn, files or [], layer="mcp",
                                        actor=actor)
        except staging.StagingError as exc:
            return _err(str(exc))
        try:
            res = ops.checkin(conn, number, version, state=state, summary=summary,
                              ecn=ecn, author=actor, source=source,
                              selector=selector, files=payload, bom=bom or [])
        except (ops.OpsError, vault.VaultError) as exc:
            return _err(str(exc))
        except storage.StorageError as exc:
            return _err(f"storage failed: {exc}")
        searchmod.touch(conn, number)
    # `res["explode"]` is ops' (`ops._explodes`): "does it still explode?",
    # answered by the viewer's own resolver, on every assembly check-in.
    files_out = res.pop("files", [])
    res.pop("_events", None)     # webhook delivery is the HTTP layer's job
    res.pop("_event", None)
    return _ok(**res, file_urls=[{"name": f["path"].rsplit("/", 1)[-1],
                                  "url": f.get("url"), "backend": f.get("backend") or "vault"}
                                 for f in files_out])


@mcp.tool
def put_file(number: str, name: str, text: str = "", content_b64: str = "",
             handle: str = "", url: str = "", sha256: str = "",
             size: int = 0, version: str = "", role: str = "",
             actor: str = "mcp", message: str = "",
             filedrop_key: str = "") -> dict:
    """Working upload: put ONE file onto a DRAFT revision without a version
    bump — the iterate-freely path. Frozen (non-DRAFT) revisions refuse; use
    revise() first. Git commits the overwrite, so history is kept.

    `text` for a small document, `content_b64` for a small binary, `handle`
    (+ sha256, size) for plm's own upload() handle — the road for every STL,
    3MF and STEP (`filedrop_key`: legacy alias) — or `url` (+ sha256) for any
    https link to the bytes that plm can reach."""
    handle = handle or filedrop_key
    if not (text or content_b64 or handle or url):
        return _err("give text, content_b64, handle (see upload()) or url "
                    "(a link to the bytes)")
    with db.connect() as conn:
        try:
            payload = staging.normalize(
                conn, [{"name": name, "text": text or None,
                        "content_b64": content_b64 or None,
                        "handle": handle, "url": url,
                        "sha256": sha256,
                        "size": size, "role": role}], layer="mcp", actor=actor)
        except staging.StagingError as exc:
            return _err(str(exc))
        try:
            res = ops.upload_files(conn, number, payload, version=version,
                                   actor=actor, message=message)
        except (ops.OpsError, vault.VaultError) as exc:
            return _err(str(exc))
        except storage.StorageError as exc:
            return _err(f"storage failed: {exc}")
        searchmod.touch(conn, number)
    return _ok(**res)


@mcp.tool
def upload(name: str = "", url: str = "", size: int = 0, sha256: str = "",
           expires_in: float = 0, actor: str = "mcp") -> dict:
    """Upload a file to plm — ONE verb, two sources. Nothing binary ever
    passes through the tool call.

    • upload(name, size, sha256) — no url: plm mints an upload in its own
      inbox. Returns {put_url, put_path, handle, expires_at}. PUT your raw
      bytes to it with your plm token —
        curl -f -T model.stl -H "Authorization: Bearer $PLM_TOKEN" '<put_url>'
      — then pass handle=<handle> (+ sha256, size) to
      checkin/put_file/put_viz/share_3d. One PUT per upload: a second is
      refused (drop_upload, then upload again).
    • upload(url=..., sha256=..., name?) — a LINK (any https URL plm can
      reach): plm downloads it server-side right now,
      verifies the sha256 while streaming, stages it, and returns {handle,
      sha256, size} — pass the handle onward exactly the same way. (checkin
      and friends also accept url= directly; use this form when you want the
      fetch verified up front or one download reused across several writes.)

    Use it for every STL, 3MF, STEP, gcode, PNG and anything over ~1 MiB.
    (A link — url= on checkin/put_file/put_viz/share_3d — is the main road;
    the inbox is plm's own convenience beside it.)
    Small text (INSTRUCTIONS.md, notes, .scad) is fine inline — that is the
    only thing inline is for. Unclaimed handles expire (24 h) and are swept.
    Both declared numbers are checked when the bytes land (a mismatch
    refuses the PUT), so a truncated upload never becomes a revision.
    """
    if url:
        with db.connect() as conn:
            try:
                res = staging.stage_from_url(conn, name=name or "file",
                                             url=url, sha256=sha256,
                                             size=size, actor=actor)
            except staging.StagingError as exc:
                return _err(str(exc))
        return _ok(**res)
    if not name:
        return _err("give a name (to mint an upload) or a url (to ingest "
                    "a link)")
    with db.connect() as conn:
        try:
            res = staging.mint(conn, name=name, size=size, sha256=sha256,
                               actor=actor, expires_in=expires_in or None)
        except staging.StagingError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def upload_status(handle: str) -> dict:
    """Did my upload land? Returns the sha256/size plm measured when the PUT
    finished, without checking anything in — the fast way to tell 'my curl
    failed' apart from 'plm rejected my file'."""
    with db.connect() as conn:
        try:
            return _ok(arrived=True, **staging.claim(conn, handle))
        except staging.StagingError as exc:
            if exc.code == 409:
                return _ok(arrived=False, handle=handle, detail=str(exc))
            return _err(str(exc))


@mcp.tool
def list_uploads(include_expired: bool = False) -> dict:
    """Staged uploads waiting to be checked in, newest first — use it to re-find
    a handle you minted earlier in the session."""
    with db.connect() as conn:
        return _ok(uploads=staging.listing(conn, include_expired=include_expired))


@mcp.tool
def drop_upload(handle: str, actor: str = "mcp") -> dict:
    """Abandon a staged upload now rather than waiting for its TTL."""
    with db.connect() as conn:
        try:
            return _ok(**staging.drop(conn, handle, actor=actor))
        except staging.StagingError as exc:
            return _err(str(exc))


@mcp.tool
def put_viz(number: str, name: str, handle: str = "", content_b64: str = "",
            url: str = "", sha256: str = "", size: int = 0, version: str = "",
            actor: str = "mcp", message: str = "",
            filedrop_key: str = "") -> dict:
    """Attach DERIVED visualization geometry (an assembled STL) to ANY revision,
    frozen ones included — derived data is not a design change, the Teamcenter
    JT-on-release pattern.

    Assemblies need this: per-part print STLs are print-oriented and do not
    compose, so the viewer shows nonsense without an assembled mesh. Assembled
    STLs are big, so a reference is the expected form: plm's own upload()
    `handle` (`filedrop_key`: legacy alias), or `url` (+ sha256) to any https
    link plm can reach. `content_b64` still works for something genuinely
    small."""
    handle = handle or filedrop_key
    if not (handle or content_b64 or url):
        return _err("give handle (see upload), url (a link to the bytes) or "
                    "content_b64")
    with db.connect() as conn:
        try:
            payload = staging.normalize(
                conn, [{"name": name, "content_b64": content_b64 or None,
                        "handle": handle, "url": url,
                        "sha256": sha256,
                        "size": size}], layer="mcp", force_role="viz",
                actor=actor)
        except staging.StagingError as exc:
            return _err(str(exc))
        try:
            res = ops.upload_files(conn, number, payload, version=version,
                                   actor=actor, message=message, derived=True)
        except (ops.OpsError, vault.VaultError) as exc:
            return _err(str(exc))
        except storage.StorageError as exc:
            return _err(f"storage failed: {exc}")
    return _ok(**res)


@mcp.tool
def remove_file(number: str = "", name: str = "", version: str = "",
                file_id: int = 0, actor: str = "mcp") -> dict:
    """Take one file off a revision's manifest.

    This removes a POINTER, not content: the vault copy stays in git history and
    an offloaded object keeps its immutable key — so it is a correction to what a
    revision CLAIMS to carry, never a way to lose bytes.

    Reach for it when a carried-forward row is stale in a way no rule can see.
    You usually do NOT need it: uploading a file of the same role+extension
    retires the older generation's row by itself (ops.supersede_inherited), which
    is the intended path and the one that keeps the two facts in step.

    DRAFT revisions: any row. Frozen revisions (IN_REVIEW and past): only rows
    carried in from an OLDER version folder, because a released record does not
    lose its own files — the refusal says so and tells you what to do instead.
    Identify the file by name (with number, plus version for a non-latest one)
    or by file_id from get_item / file_link when two rows share a name."""
    if not (file_id or (number and name)):
        return _err("pass file_id, or number + name (file_link(number) lists "
                    "what a revision carries)")
    with db.connect() as conn:
        try:
            res = ops.remove_file(conn, number, version=version,
                                  file_id=file_id, name=name, actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, res["number"])
    return _ok(**res)


@mcp.tool
def file_link(number: str, name: str = "", version: str = "") -> dict:
    """The plm URL for one of a revision's files (`/api/blob?id=`) — what to
    hand a human or a fabricator who can reach this plm. It opens for nobody
    else; to send a file further, download the bytes and share them another
    way. `name` matches the filename; omit it to list what the revision
    has."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM files WHERE revision_id=?", (rev["id"],))]
    def base(row):
        return row["path"].rsplit("/", 1)[-1]
    if not name:
        return _ok(number=number, version=rev["version"],
                   files=[{"name": base(r), "role": r["role"], "size": r["size"],
                           "backend": r["backend"] or "vault"} for r in rows],
                   hint="call again with name= for a link")
    match = next((r for r in rows if base(r).lower() == name.lower()), None)
    if not match:
        return _err(f"{number} v{rev['version']} has no file named {name!r}")
    return _ok(number=number, version=rev["version"], name=base(match),
               url=f"{config.PUBLIC_URL}/api/blob?id={match['id']}",
               note=f"{match['backend'] or 'vault'}-backed: {storage.LINK_NOTE}")


@mcp.tool
def fork(number: str, new_name: str, version: str = "", reason: str = "",
         actor: str = "mcp", idempotency_key: str = "") -> dict:
    """Save-as: create a NEW item (auto-numbered in the same class) seeded from
    a source revision — files and BOM carried, derived_from relation and a
    context entry recorded. Use for divergent purposes; use revise() for
    ordinary evolution of the same part.

    Retry-safe on the auto-numbered path, like create_item: a repeat replays the
    first fork rather than making a second one."""
    with db.connect() as conn:
        try:
            res = ops.fork(conn, number, new_name, actor=actor,
                           version=version, reason=reason,
                           idempotency_key=idempotency_key)
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, number, res["number"])
    return _ok(**res)


# -- typed relations (docs/PLM_RELATIONS.md) ----------------------------------
# The registry in plm/relations.py drives these tools completely: labels, kind
# rules, revision-level flag — and, below, the list of types an agent reads in
# link_items' own description. Adding a relation type is a dict entry; nothing
# in this file changes.

def _doc(text: str):
    """Set a generated docstring before the tool decorator reads it, so the
    types listed in the description ARE the registry rather than a stale copy."""
    def deco(fn):
        fn.__doc__ = text
        return fn
    return deco


_TYPE_LINES = "\n".join(
    f"      {t['type']} — '{t['label']}' / inverse '{t['inverse_label']}'"
    + (", revision-level (versions pinned; default latest)" if t["rev_level"]
       else ", item-level")
    + f", home: {t['home']}"
    + (f", from-kind must be {'|'.join(t['from_kinds'])}" if t["from_kinds"] else "")
    + (f", to-kind must be {'|'.join(t['to_kinds'])}" if t["to_kinds"] else "")
    + f"\n        {t['hint']}"
    for t in relations.types())


@mcp.tool
def relation_types() -> dict:
    """The relation registry: every link type, both direction labels, which
    item kinds may take part, and whether it pins revisions. Read this before
    link_items if you are unsure what a type means — it is the single source of
    truth, so it is never out of date."""
    return _ok(types=relations.types())


@mcp.tool
@_doc(
    "Link two items with a typed relation — the generic provenance/verification\n"
    "    surface (one row, no new concepts). The label reads FROM → TO:\n"
    "    link_items('coupon_for', 'KML-CPN-0001', 'KML-PRT-1011') records\n"
    "    'CPN-0001 verifies PRT-1011', and PRT-1011 shows 'verified by CPN-0001'.\n\n"
    "    Types (from the registry — relation_types() returns the same thing):\n"
    f"{_TYPE_LINES}\n\n"
    "    Revision-level types pin both ends to a version: pass to_version (and\n"
    "    from_version) or they default to each side's current revision. `note`\n"
    "    is the one line explaining why the link exists.\n\n"
    "    ONE HOME PER CLAIM, WRITTEN BY THE SIDE THAT MAKES IT. To record that X\n"
    "    verifies Y, write it in X (from_number=X): the other direction ('Y is\n"
    "    verified by X') is DERIVED — never record the inverse. Where the claim\n"
    "    lives in git is its type's `home`: revision = the claimant revision's\n"
    "    plm.toml (with the target's version AND commit), item = item.toml on\n"
    "    refs/plm/item, note = a refs/notes/plm note on a released revision.\n"
    "    A coupon_for may carry its outcome: result='pass'|'fail'|'pending',\n"
    "    result_note, measured={name: value} — or record it later with\n"
    "    record_coupon_result().")
def link_items(type: str, from_number: str, to_number: str,
               from_version: str = "", to_version: str = "", note: str = "",
               actor: str = "mcp", result: str = "", result_note: str = "",
               measured: dict | None = None) -> dict:
    with db.connect() as conn:
        try:
            # One code path with the API and the UI (ops.add_relation): a
            # revision-level link is a `[[relations]]` line committed on that
            # revision's draft branch, an item-level one is a registry fact.
            rec = ops.add_relation(conn, type, from_number, to_number,
                                   from_version=from_version,
                                   to_version=to_version, note=note,
                                   actor=actor, result=result,
                                   result_note=result_note, measured=measured)
        except relations.RelationError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(relation=rec)


@mcp.tool
def record_coupon_result(relation_id: int, result: str, result_note: str = "",
                         measured: dict | None = None,
                         actor: str = "mcp") -> dict:
    """Record what a coupon FOUND on the claim that says what it tested:
    result = 'pass' | 'fail' | 'pending', one sentence in result_note, and the
    numbers in `measured` ({"bore_mm": 16.30, "fit": "snug"}). relation_id is
    the coupon_for link (list_related(<coupon>, 'coupon_for', 'out')).

    It is written where the claim lives — the coupon DRAFT's plm.toml (one
    commit) in a git-first project, the row otherwise. On a coupon revision that
    is already RELEASED (print, release, THEN measure) it is a `[[results]]`
    entry in that revision's refs/notes/plm note: the tag does not move, and
    the result counts exactly like one recorded on the draft. A passing coupon on the exact commit of a
    part being promoted can attest the release checklist's fit line by
    reference: sign_release(..., evidence='<COUPON>@v<version>')."""
    with db.connect() as conn:
        try:
            rec = ops.set_relation_result(conn, relation_id, result=result,
                                          result_note=result_note,
                                          measured=measured, actor=actor)
        except relations.RelationError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(relation=rec)


@mcp.tool
def project_readiness(project: str) -> dict:
    """What flipping a project's PLM_GIT_MODE to `read` or `write` would DO,
    and what blocks it — before anybody flips it. Per project: items,
    revisions, drafts, releases; filenames that will be normalised; BOM pins by
    state and the parents that could not be RELEASED today; which sources are
    regenerable; external files; the migrate_cleanup lines still open; every
    relation's old -> new home and the coupons with/without results; open
    drafts/approvals/checkouts; and a verdict per target mode with the
    blockers named. Read-only — the flip itself is the operator's
    `python -m plm.gitmode --project <p> --mode <m>` (the rollout runbook)."""
    from plm import readiness
    with db.connect() as conn:
        try:
            return _ok(**readiness.report(conn, project))
        except LookupError as exc:
            return _err(str(exc))


@mcp.tool
def unlink_items(type: str = "", from_number: str = "", to_number: str = "",
                 relation_id: int = 0, actor: str = "mcp") -> dict:
    """Remove a typed relation: either relation_id (from get_item's `related`)
    or the type + the two numbers. Removing a link is a normal edit — the event
    log keeps the record that it existed."""
    with db.connect() as conn:
        try:
            if relation_id:
                return _ok(removed=[ops.remove_relation(conn, relation_id,
                                                        actor=actor)])
            if not (type and from_number and to_number):
                return _err("give relation_id, or type + from_number + to_number")
            return _ok(removed=ops.remove_relations_between(
                conn, type, from_number, to_number, actor=actor))
        except relations.RelationError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))


@mcp.tool
def list_related(number: str, type: str = "", direction: str = "both",
                 with_comments: bool = False) -> dict:
    """Everything linked to one item, BOTH directions, each entry already
    labelled from this item's point of view (`label`) with the other side's
    number, name, kind, version and state.

    Filter with type (a registry key) and direction ('out' = links this item
    made, 'in' = links pointing at it). "The coupons of KML-PRT-1011" is
    list_related('KML-PRT-1011', 'coupon_for', 'in'); "what does this coupon
    verify" is list_related('KML-CPN-0001', 'coupon_for', 'out').

    An `in` row is DERIVED (`derived: true`): it is recorded in the OTHER
    item (`recorded_in` says where — its plm.toml, item.toml or a note) and
    shown here by index. `claimant_version` is the revision that makes the
    claim ("GL-CPN-0001 v0.1.0 verifies GL-PRT-1001 v0.14.0"); a coupon row
    carries `result` / `result_note` / `measured`.

    Every row carries `comments`: how many context entries are attached to that
    LINK. with_comments=True adds their bodies — that is where the reasoning for
    a link lives ("why is this a bench test and not a product?"), written with
    add_context(target_type='relation', target_id=<the link's id>)."""
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            return _err(f"unknown item: {number}")
        rels = relations.for_item(conn, item["id"])
        rels = [r for r in rels if (not type or r["type"] == type)
                and (direction in ("both", "") or r["direction"] == direction)]
        if with_comments:
            for r in rels:
                r["context"] = contextmod.entries(
                    conn, "relation", r["id"])["entries"] if r["comments"] else []
    return _ok(number=number, related=rels)


@mcp.tool
def add_coupon(name: str, for_number: str, for_version: str = "",
               number: str = "", note: str = "", project: str = "",
               actor: str = "mcp") -> dict:
    """Make a test coupon for ONE revision: registers a `coupon` item
    (auto-numbered in the part's own prefix, e.g. KML-CPN-0001) and links it
    coupon_for that revision — create_item + link_items in one call.

    for_version defaults to the part's current revision, which is what you want
    while iterating; a coupon for v0.3.0 says nothing about v0.4.0. Then treat
    it as a real item: checkin() the coupon's geometry/settings, and
    add_context() the MEASURED RESULT onto it — that is the record that makes
    the next revision's change explicable. Coupons are refused by BOMs on
    purpose: they relate, they don't assemble."""
    with db.connect() as conn:
        try:
            res = ops.add_coupon(conn, name, for_number, for_version=for_version,
                                 number=number, project=project, note=note,
                                 actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, for_number, res["number"])
    return _ok(**res)


@mcp.tool
def add_asset(name: str, for_number: str = "", relation: str = "mounts_to",
              number: str = "", prefix: str = "", project: str = "",
              for_version: str = "", asset_version: str = "", note: str = "",
              actor: str = "mcp") -> dict:
    """Register the thing your part attaches to and link it, in one call: an
    `asset` item (auto-numbered in the AST class, e.g. CCR-AST-0001) plus the
    relation — create_item + link_items, like add_coupon.

    AN ASSET IS WHAT YOU NEITHER BUILD NOR BUY: a camper, a truck bed, a garage
    wall, a bike frame. It is reference geometry and measured dimensions for FIT.
    It is a real item — checkin() its scan/CAD, set_attributes() what you
    measured (overall_length/width/height, mount_spacing, mount_hole_pitch,
    mount_hole_dia, mount_hole_count, load_rating, serial_number — pass the
    UNIT, an asset is often measured in inches), add_context() where it lives —
    but it is NEVER a BOM line, never `purchased`, and never a product.

    `relation` picks which fact you are recording, and the direction follows
    from the meaning:
      mounts_to (default)       the physical INTERFACE — this design bolts to
                                that asset. Item-level: it follows the design
                                across revisions.
      installed_on              the STATE — this article is on that asset RIGHT
                                NOW, pinned to the revision that is on it. Two
                                racks can mounts_to one camper; one is
                                installed_on it.
      integration_envelope_for  the asset's measured geometry CONSTRAINS that
                                design revision. Both ends pinned, because
                                'does it fit?' is about one scan and one design.

    Omit for_number to register a bare asset (pass prefix). Then read it back
    with list_related(number) from either side, or find_items(kind='asset')."""
    with db.connect() as conn:
        try:
            res = ops.add_asset(conn, name, for_number, relation=relation,
                                number=number, prefix=prefix, project=project,
                                for_version=for_version,
                                asset_version=asset_version, note=note,
                                actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, res["number"], for_number)
    return _ok(**res)


# == EDITING A DRAFT's BOM =========================================
# Five verbs. A DRAFT is a WORKING revision: iterate on its structure freely,
# with no revise, no freeze and no re-fork. Every one of them is DRAFT-only
# (except suppression), refuses coupons and assets, and is evented.
# ops.py owns the rules; these are the agent-facing doors to them.

@mcp.tool
def add_bom_line(parent: str, child: str, qty: float = 1, unit: str = "EA",
                 child_version: str = "", find: int | None = None,
                 version: str = "", notes: str = "", actor: str = "mcp") -> dict:
    """Add one occurrence to a DRAFT revision's BOM — the everyday "this
    assembly now contains that part" edit.

    parent/child are item numbers (or ids). `child_version` empty = IMPRECISE:
    the line resolves to the child's latest revision at read time, which is what
    you want while both are moving. Pin it to a version once the child is
    released, or let propagate() do it. `find` defaults to the next free decade,
    keeping the 10/20/30 convention.

    `version` names the parent's DRAFT revision; empty = its latest. A frozen
    parent refuses and tells you to Revise ↑ first, because a structure change
    is a design change and belongs to a revision of its own.

    Retry-safe: adding a child that is already on this BOM at the same binding,
    with an auto find number, is refused with the existing line's id rather than
    quietly creating a duplicate occurrence. More of the same part is a
    QUANTITY — update_bom_line(qty=…) — not a second line."""
    with db.connect() as conn:
        try:
            res = ops.add_bom_line(conn, parent, child, version=version,
                                   child_version=child_version, qty=qty,
                                   unit=unit, find=find, notes=notes,
                                   actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def update_bom_line(parent: str = "", child: str = "", line_id: int = 0,
                    qty: float | None = None, unit: str | None = None,
                    find: int | None = None, notes: str | None = None,
                    version: str = "", actor: str = "mcp") -> dict:
    """Change one occurrence's NUMBERS on a DRAFT: qty, unit, find number, notes.

    "Four of these, not two." The occurrence itself is untouched — same child,
    same assembly — which is exactly what distinguishes this from
    replace_bom_line (different child or binding) and from suppress_bom_line
    (present in the product, absent in this configuration).

    Address the line by (parent, child) or by line_id, which get_bom() returns
    as `id`. Pass only the fields you are changing; omitted ones are left alone.
    A call that changes nothing says so rather than writing an empty event."""
    with db.connect() as conn:
        try:
            res = ops.update_bom_line(conn, line_id, parent=parent, child=child,
                                      version=version, qty=qty, unit=unit,
                                      find=find, notes=notes, actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def replace_bom_line(parent: str = "", child: str = "", line_id: int = 0,
                     new_child: str = "", child_version: str | None = None,
                     qty: float | None = None, version: str = "",
                     actor: str = "mcp") -> dict:
    """Swap what fills one position in a DRAFT's BOM: the child, its pinned
    revision, or both.

    "The weldment we designed replaces the purchased angle bracket." "Pin this
    occurrence to v1.2.0 now that it is released." Same edit — one line's
    binding moves and the assembly's structure does not — so, one verb.

    find number, unit and notes are KEPT: the position in the assembly and the
    note explaining it belong to the parent's design, not to whichever part
    happens to sit there. qty is kept too unless you pass one.

    child_version: omit to leave the binding alone; '1.2.0' to pin it precisely;
    '' (empty string) to make it imprecise (resolve to the child's latest)."""
    with db.connect() as conn:
        try:
            res = ops.replace_bom_line(conn, line_id, parent=parent, child=child,
                                       version=version, new_child=new_child,
                                       child_version=child_version, qty=qty,
                                       actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def remove_bom_line(parent: str = "", child: str = "", line_id: int = 0,
                    version: str = "", actor: str = "mcp") -> dict:
    """Take one occurrence OFF a DRAFT's BOM — "this was NEVER part of it".

    For a line that was a mistake: the wrong child, a duplicate, an occurrence
    that belongs on a different sub-assembly. The row goes; the event log keeps
    what it was.

    NOT for a part that IS part of the product but absent in the configuration
    you are describing — that is suppress_bom_line, which keeps the record. Pick
    by which sentence is true; plm will not guess. Released structure is
    immutable, so a frozen parent refuses and points at both alternatives."""
    with db.connect() as conn:
        try:
            res = ops.remove_bom_line(conn, line_id, parent=parent, child=child,
                                      version=version, actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(**res)


@mcp.tool
def suppress_bom_line(parent: str, child: str, suppressed: bool = True,
                      version: str = "", find: int | None = None,
                      actor: str = "mcp") -> dict:
    """Mark a child's occurrence(s) ABSENT IN THIS CONFIGURATION (150% BOM), or
    restore them.

    The line STAYS in the record and stays visible, flagged; rolled-up
    quantities, build plans and the 3D view skip it while suppressed. That is
    the difference from remove_bom_line, and it matters: suppression is a
    statement about a CONFIGURATION (the boss you print only for the tall
    variant, the bracket a fork inherited and does not use), removal is a
    statement that the occurrence was never part of the product at all.

    Allowed on FROZEN revisions on purpose — a suppression does not change what
    the product is, it records which variant you are describing. Suppresses
    every matching occurrence unless you narrow it with `find`."""
    with db.connect() as conn:
        rev = db.find_revision(conn, parent, version)
        if not rev:
            return _err(f"no revision for {parent} {version or '(latest)'}")
        kid = db.item_by_number(conn, child)
        if not kid:
            return _err(f"unknown child item: {child}")
        sql = ("SELECT id FROM bom WHERE parent_rev_id=? AND child_item_id=?")
        args: list = [rev["id"], kid["id"]]
        if find is not None:
            sql += " AND find_number=?"
            args.append(int(find))
        rows = conn.execute(sql, args).fetchall()
        if not rows:
            return _err(f"{child} is not on {parent} v{rev['version']}'s BOM")
        out = []
        try:
            for r in rows:
                out.append(ops.suppress_bom_line(
                    conn, r["id"], suppressed=suppressed, actor=actor))
        except ops.OpsError as exc:
            return _err(str(exc))
    return _ok(parent=parent, version=rev["version"], child=child,
               suppressed=suppressed, lines=len(rows), changed=out)


@mcp.tool
def set_state(number: str, state: str, version: str = "", actor: str = "mcp") -> dict:
    """Lifecycle transition on a revision (default latest).
    States: DRAFT, PROTOTYPE, RELEASED, SUPERSEDED, OBSOLETE."""
    if state not in config.STATES:
        return _err(f"state must be one of {config.STATES}")
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        # A revision that lives in git is written through git or not at all
        # (ops._stranded) — the same rule as the API's /state.
        try:
            git = ops.set_state_git(conn, rev, state, number=number,
                                    actor=actor) \
                if ops.registry_only_write(conn, rev, number=number) else {}
        except ops.OpsError as exc:
            return _err(str(exc))
        if not git:
            conn.execute("UPDATE revisions SET state=? WHERE id=?", (state, rev["id"]))
        db.add_event(conn, "state", item_id=rev["item_id"], revision_id=rev["id"],
                     actor=actor, data=ops._with_git(
                         {"from": rev["state"], "to": state}, git))
    return _ok(**ops._with_git(
        {"number": number, "version": rev["version"], "state": state}, git))


@mcp.tool
def explode_status(number: str, version: str = "") -> dict:
    """Will this assembly EXPLODE in plm's 3D viewer — and if not, exactly
    which child is why. Computed by the same resolver the viewer uses.

    THE RULE: every first-level BOM child that has a mesh must carry a `viz`
    STL posed in THIS assembly's coordinates (plm has no occurrence transforms,
    so the pose must be baked into the file). A child with only a print mesh,
    or a made part with no mesh at all, collapses the view to one fused body.
    Bought/hardware children with no mesh are fine — they are simply not drawn.
    Check this after every assembly check-in; submit_for_release enforces it."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        return _ok(number=number, version=rev["version"],
                   **thumbs.explode_status(conn, rev["id"]))


@mcp.tool
def submit_for_release(number: str, version: str = "", target_state: str = "PROTOTYPE",
                       note: str = "", actor: str = "mcp", explode_waiver: str = "") -> dict:
    """Submit a DRAFT revision — and let plm's POLICY decide what happens next.

    `target_state`: CONCEPT | DESIGN | PROTOTYPE | RELEASED.

    By default it freezes the revision at IN_REVIEW and WAITS FOR A PERSON, at
    every maturity. In a git-first `write` project the submission is a PULL
    REQUEST (a lone one is wrapped in a pull request of one): the answer's
    `change` carries `ref` (#n), `number` (CHG-n, the legacy label) and `url` —
    give the approver that URL and stop. Work that spans several repos is ONE pull
    request: push them with `-o pr=<n>` and use
    submit_change instead. Only a project in "just build it" mode merges
    without a person (validated, tagged, recorded `approver: policy`).

    READ THE ANSWER: `state` is what happened (`IN_REVIEW` = waiting, a
    granted state = merged) and `policy` carries the rule that decided, its
    source and whether agents may approve at this maturity in this project.
    `git` is present when it merged: the new `main`, the tag, and the commit
    you proposed still in the history byte for byte.

    THE GIT MODE DECIDES WHETHER POLICY RUNS AT ALL (`git_mode` on get_item):
    in a project that is `off` or `read`, nothing auto-merges — every submit
    waits at IN_REVIEW for a human, as plm always did. Policy is a `write`-mode
    behaviour, because an auto-merge is a merge.

    EXPLODE GATE: an assembly with two or more made children (part/assembly)
    is REFUSED unless it explodes in the viewer — every child that has a mesh
    must carry a `viz` posed in the assembly's coordinates. The refusal names
    each child and its fix (see explode_status). `explode_waiver='<why>'`
    submits anyway and records the reason on the approval, where the proposal
    page shows it. The same gate applies to a `git push … refs/for/main`
    (`-o explode_waiver=<why>`) and before any policy auto-merge."""
    with db.connect() as conn:
        try:
            out = ops.submit_for_release(
                conn, number, version=version, target_state=target_state,
                note=note, actor=actor, explode_waiver=explode_waiver)
        except ops.OpsError as exc:
            return _err(str(exc))
        git_world = _item_writes(conn, number)
    _deliver_events(out, git_world)
    return _ok(**{k: v for k, v in out.items() if k != "ok"})


def _item_writes(conn, number: str) -> bool:
    """Is this item in a git-first `write` project? (stage 14)"""
    item = db.resolve_item(conn, number)
    return bool(item) and ops.git_writes(conn, item)


def _deliver_events(out: dict, git_world: bool) -> None:
    """STAGE 13's HUNK, TAKEN IN STAGE 14. This door used to DROP the
    integrations events of a submit / decide ("webhook delivery is
    plm/api.py's plane"), so an AGENT's submission raised no
    `review.requested` — the one notification the pull request workflow exists
    for. In a git-first `write` project they now go out through
    `actions.dispatch` (→ `actions.send()`, the one wire, dry-run in the POC),
    exactly as the HTTP door delivers them. Outside `write` this door stays
    master's (it drops them) — the compatibility harness diffs the events table."""
    evs = out.pop("_events", None) or ([out.pop("_event")]
                                        if out.get("_event") else [])
    out.pop("_event", None)
    if git_world:
        from plm import actions
        for ev in evs:
            actions.dispatch(ev[0], ev[1])


@mcp.tool
def approval_policy(project: str = "") -> dict:
    """Who may say yes here, and to what — READ THIS BEFORE ASSUMING.

    Two independent answers per maturity: whether a decision is needed at all
    (`auto` — by default NO maturity merges by itself; a project in "just
    build it" mode merges CONCEPT/DESIGN/PROTOTYPE approved by the POLICY), and
    — when one is — whether an agent's signature counts. The second is a SETTING the operator
    controls per project, not a law: some projects admit agents at RELEASED.

    Whatever the answer, the record is honest: an approval by an agent is stored
    with `approver_kind = bot` and the tag annotation says so in words. If you
    are allowed to approve, it is a real decision — say WHY in the note."""
    with db.connect() as conn:
        return _ok(project=project or "", policy=policymod.table(project,
                                                                 conn=conn))


@mcp.tool
def request_release(number: str, version: str = "", to_maturity: str = "released",
                    notes: str = "", actor: str = "mcp") -> dict:
    """Ask for an EXISTING tagged revision to be promoted. **No file changes.**

    This is the right way to reach RELEASED. A proposal answers "is this change
    right?"; a release answers "is this revision ready to build against?", and it
    acts on a revision that already exists — you do not mint a version to record
    a decision.

    YOU MAY REQUEST ONE, and normally should: you are what knows the part was
    printed and fitted. What you get back is a WORKFLOW: a checklist somebody has
    to attest line by line, a quorum of approvals, and the released-children
    gate. Attest the lines you can PROVE with sign_release(kind='checklist', …,
    note=<your evidence>); leave the rest to a person.

    `notes` — leave it empty and plm drafts the release notes from what merged
    since the last release (proposal titles, pin bumps with the child's own
    commit subjects, attribute deltas). Edit them; they go into the frozen
    evidence document."""
    with db.connect() as conn:
        # ⭐ SET-AWARE (stage 14): in a git-first `write` project a request to
        # RELEASED is a RELEASE SET over the unreleased closure — one workflow
        # per member, bottom-up, one human decision. Elsewhere it is master's
        # single promotion, unchanged.
        if to_maturity.lower() == "released" and _item_writes(conn, number):
            from plm import releasesets
            try:
                out = releasesets.request(conn, number, version=version,
                                          actor=actor, notes=notes)
            except (releasesets.SetError, ops.OpsError) as exc:
                conn.rollback()            # a refusal leaves no half-made set
                return _err(str(exc))
            _deliver_events(out, True)
            return _ok(**out)
        try:
            out = ops.request_release(conn, number, version=version,
                                      to_maturity=to_maturity, notes=notes,
                                      actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    out.pop("_event", None)
    return _ok(**out)


@mcp.tool
def sign_release(release_id: int, kind: str = "checklist", item: str = "",
                 verdict: str = "", note: str = "", actor: str = "mcp",
                 evidence: str = "") -> dict:
    """Sign one line of a release workflow.

    kind='checklist' — attest a line of the workflow's checklist BY NAME, with
    your evidence in `note`. Agents may and should do this for anything they can
    prove ("0 buy lines at this tag", "rendered from [source] and the mesh
    matches"). It is recorded as `signer_kind = bot`.

    kind='approval' — the DECISION. Whether you may make one is
    approval_policy()'s answer for this project. When you may not, the refusal
    tells you exactly which setting would change that; do not treat it as a
    thing to work around.

    Every signoff is bound to the tag SHA, so it is a signature over the exact
    bytes of the part, its BOM pins and its metadata.

    evidence='GL-CPN-0001@v0.1.0' attests a checklist line BY REFERENCE: the
    workflow says which lines accept which claim (the fit line accepts a
    coupon_for), and the claim must pin THIS commit with result='pass'. The
    release's progress lists the candidates per line (`by_reference`); the
    frozen evidence file resolves the reference. No such claim → attest by
    hand, with your evidence in `note`."""
    with db.connect() as conn:
        try:
            out = ops.sign_release(conn, release_id, kind=kind, item=item,
                                   verdict=verdict, note=note, actor=actor,
                                   evidence=evidence)
        except ops.OpsError as exc:
            return _err(str(exc))
    out.pop("_event", None)
    return _ok(**out)


@mcp.tool
def pending_releases(number: str = "") -> dict:
    """Promotions waiting for a decision — the other half of the queue.

    Each row says how far through its workflow it is: approvals given of
    approvals required, checklist lines attested of lines asked for, and how many
    child pins are blocking it. A blocked one is usually YOUR next job: release
    the child."""
    with db.connect() as conn:
        return _ok(releases=ops.list_releases(conn, pending=True, number=number))


@mcp.tool
def decide_release(release_id: int, decision: str, actor: str,
                   note: str = "") -> dict:
    """Approve or reject a promotion — and attest any checklist line nobody has.

    ⚠️ This is a DECISION, and `plm/policy.py` says who may make one. By default
    that is a human; a project may admit agents, and approval_policy() is where
    that is written down. Refusals name the setting rather than just saying no.

    On approval, in this order: the released-children gate (§5.1 — every
    `parts/*` pin must itself be RELEASED, by status read from git notes), the
    evidence document frozen and hashed, the `[status]` note written on the
    release commit. **No tag moves and nothing is committed on `main`.**"""
    with db.connect() as conn:
        try:
            out = ops.decide_release(conn, release_id, decision=decision,
                                     actor=actor, note=note)
        except ops.OpsError as exc:
            return _err(str(exc))
    out.pop("_event", None)
    return _ok(**out)


@mcp.tool
def withdraw_release(release_id: int, actor: str = "mcp", note: str = "") -> dict:
    """Pull a promotion request back. Nothing was written, so nothing is undone."""
    with db.connect() as conn:
        try:
            out = ops.withdraw_release(conn, release_id, actor=actor, note=note)
        except ops.OpsError as exc:
            return _err(str(exc))
    out.pop("_event", None)
    return _ok(**out)


@mcp.tool
def pending_approvals() -> dict:
    """The release queue: revisions frozen at IN_REVIEW awaiting a decision.
    Deciding requires the approver role (humans — e.g. geoff — not bots).

    Each row carries the comments attached to that submission (`context`) —
    review remarks and conditions, written with
    add_context(target_type='approval', target_id=<approval id>). Read them
    before re-submitting: they are the reason it is still sitting here."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT a.id, a.target_state, a.note, a.requested_by, a.requested_at,
                      r.version, r.summary, i.number, i.name
               FROM approvals a
               JOIN revisions r ON r.id=a.revision_id
               JOIN items i ON i.id=r.item_id
               WHERE a.decision='' ORDER BY a.requested_at DESC""")]
        counts = contextmod.counts(conn, "approval", [r["id"] for r in rows])
        for r in rows:
            r["comments"] = counts.get(r["id"], 0)
            r["context"] = contextmod.entries(
                conn, "approval", r["id"])["entries"] if r["comments"] else []
        return _ok(approvals=rows)


@mcp.tool
def withdraw_approval(approval_id: int, actor: str = "mcp", note: str = "") -> dict:
    """Cancel a pending submission (requester or approver): the revision goes
    back to DRAFT for more work — e.g. after review feedback, before revising.

    A draft that arrived by `git push` keeps its branch: withdrawing is "not
    yet", not "never"."""
    with db.connect() as conn:
        try:
            out = ops.withdraw_approval(conn, approval_id, actor=actor, note=note)
        except ops.OpsError as exc:
            return _err(str(exc))
    out.pop("_event", None)
    return _ok(**{k: v for k, v in out.items() if k != "ok"})


@mcp.tool
def decide_approval(approval_id: int, decision: str, actor: str, note: str = "") -> dict:
    """Approve or reject a pending release. Requires the approver role —
    agents can prepare and submit, but the gate is human unless a bot is
    explicitly granted approver.

    **Approval IS the merge.** For a revision whose bytes are in the item's git
    repo, approving moves `main` onto the draft (fast-forward, or a real
    server-side merge) and writes the annotated tag `vX.Y.Z`. That is why a bot
    cannot do it: nothing else in the system may create a release tag."""
    with db.connect() as conn:
        try:
            out = ops.decide_approval(conn, approval_id, decision=decision,
                                      actor=actor, note=note)
        except ops.OpsError as exc:
            return _err(str(exc))
        git_world = _item_writes(conn, out.get("number") or "")
    _deliver_events(out, git_world)
    return _ok(**{k: v for k, v in out.items() if k != "ok"})


@mcp.tool
def set_material(number: str, material: str = "", mass_g: float | None = None,
                 version: str = "", actor: str = "mcp", source: str = "") -> dict:
    """Record material master data on a revision — material name (e.g.
    'Elegoo PLA') and/or net mass in grams. The slicer knows the grams; post
    them back here after slicing so material rollups stay real. `source`
    documents where the number came from (e.g. 'orca 2.4.2, 0.20mm')."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        # Same rule as the API's /material (ops._stranded).
        try:
            if ops.registry_only_write(conn, rev, number=number):
                res = ops.set_attributes(
                    conn, rev, {**({"material": material} if material else {}),
                                **({"mass_g": mass_g} if mass_g is not None else {})},
                    source=source, actor=actor)
                return _ok(**ops._with_git(
                    {"number": number, "version": rev["version"],
                     "material": material, "mass_g": mass_g}, res.get("git")))
        except (ops.OpsError, attrsmod.AttrError) as exc:
            return _err(str(exc))
        conn.execute("UPDATE revisions SET material=CASE WHEN ?!='' THEN ? ELSE material END, "
                     "mass_g=COALESCE(?, mass_g) WHERE id=?",
                     (material, material, mass_g, rev["id"]))
        db.add_event(conn, "material", item_id=rev["item_id"], revision_id=rev["id"],
                     actor=actor, data={"material": material, "mass_g": mass_g,
                                        "source": source})
    return _ok(number=number, version=rev["version"], material=material, mass_g=mass_g)


@mcp.tool
def set_attributes(number: str, attributes: dict, version: str = "",
                   source: str = "", actor: str = "mcp") -> dict:
    """Record measurable properties on a REVISION — the things CAD and the slicer
    know: mass, material, bounding box, colour, print time.

    attributes: {name: value} or {name: {value, unit}} — e.g.
    {"mass_g": 41.2, "material": "Elegoo PETG", "bbox_x_mm": 92.5,
     "bbox_y_mm": 61.0, "bbox_z_mm": 24.5, "print_time_min": 78,
     "color": "black"}. `source` says which instrument measured them
    ('orca 2.4.2, 0.20mm', 'cadquery export'). An empty value clears one.

    Well-known names (get them, with labels and units, from get_item's
    attributes or the registry): mass_g, material, bbox_x_mm, bbox_y_mm,
    bbox_z_mm, color, print_time_min. For a physical envelope or a mounting
    interface — an `asset` you measured, or your own part's fit-relevant
    dimensions: overall_length, overall_width, overall_height, mount_spacing
    (centre-to-centre of the mounting points), mount_hole_pitch, mount_hole_dia,
    mount_hole_count, load_rating, serial_number. Those carry NO unit in their
    name and NO default unit, so PASS THE UNIT —
    {"overall_width": {"value": 81, "unit": "in"}} — because a tape-measured
    camper is in inches while a slicer's bbox is in mm, and a silent default
    would make one of them a lie. Any other name is accepted — the registry
    improves display, it does not gate what you may record.

    Revision-level, because geometry changes change every one of these; they
    carry forward on revise() and are re-measured when the shape moves.
    material and mass_g are the same two facts set_material() writes (they live
    in revision columns so build plans can roll them up in SQL) — one home per
    fact, one uniform read.

    Read them back through get_item(number): each revision carries
    `attributes`."""
    from plm import attributes as attrmod
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        try:
            # On a revision that lives in its repo this becomes a COMMIT on the
            # draft branch editing one line of plm.toml, then an index pass —
            # so whoever cloned the part gets your measurement with `git pull`.
            res = ops.set_attributes(conn, rev, attributes, source=source,
                                     actor=actor)
        except (attrmod.AttrError, ops.OpsError) as exc:
            return _err(str(exc))
        searchmod.touch(conn, number)
    return _ok(number=number, version=rev["version"], revision_id=rev["id"], **res)


@mcp.tool
def set_product(number: str, product: bool = True, actor: str = "mcp") -> dict:
    """Designate an item as a PRODUCT — the end item the dashboard renders
    (the Teamcenter end-item concept). Product-ness is EXPLICIT data: when you
    create or discover the real top-level thing a project is building (the
    lantern, the projector), flag it. Bench tests and fixtures are NEVER
    products, and coupons and assets are REFUSED — a coupon verifies what you
    build and an asset is what you bolt it to; neither is the thing you build."""
    with db.connect() as conn:
        try:
            res = ops.set_product(conn, number, product, actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
    return {"ok": True, **res}


@mcp.tool
def reclassify_item(number: str, kind: str, note: str = "",
                    actor: str = "mcp") -> dict:
    """Fix what an item IS: change its `kind` (part|assembly|purchased|hardware|
    firmware|document|coupon|asset) and let the number and the vault folder follow.

    Use it when you MEET a misclassified item — the common one being a bench
    test, fit fixture or tolerance ladder registered as a plain `part`, which
    makes it look like product structure to every dashboard and build plan. A
    verification artifact that validates ONE revision is a `coupon`: reclassify
    it, then link_items('coupon_for', <coupon>, <the part>, to_version=…).

    The class token in the number is a projection of `kind`, so plm renumbers
    the item into the right class (GL-PRT-9001 → GL-CPN-0002) and moves its
    vault folder with a git commit. The OLD number keeps resolving forever, BOM
    lines and relations survive (they key on the id), and `note` is recorded as
    context — say why, because a reclassification is a judgement.

    The OTHER common one is a thing that was never yours to build or buy — the
    camper a rack bolts to, a truck bed, a wall — registered as `purchased` or
    dropped into a BOM. That is an `asset`: reclassify_item(number, 'asset')
    renumbers it into the AST class, then link_items('mounts_to', <your design>,
    <the asset>). Take the BOM occurrence out first.

    Refused for an item used in a BOM when the new kind is coupon or asset:
    coupons relate and assets are mounted to — neither assembles. Remove the
    occurrence(s) first (Revise ↑ a frozen parent), and say in `note` what the
    thing actually is."""
    with db.connect() as conn:
        try:
            res = ops.reclassify_item(conn, number, kind, note=note, actor=actor)
        except ops.OpsError as exc:
            return _err(str(exc))
        except vault.VaultError as exc:
            return _err(f"vault move failed: {exc}")
        searchmod.touch(conn, res["number"])
    return _ok(**res)


@mcp.tool
def renumber_item(number: str, new_number: str = "", note: str = "",
                  actor: str = "mcp") -> dict:
    """Fix a bad part number: plm owns identity, so a number that smuggled
    classification into itself (GL-OWNED-001, GL-ELEC-007, GL-FUTURE-002) gets
    replaced by a proper one.

    Omit new_number and plm assigns the next free number in the item's own prefix
    and the class matching its kind. Nothing else about the item changes: BOM
    lines and relations reference the item's ID and survive untouched, the OLD
    number keeps resolving in every lookup (so old links and your own notes still
    work), the vault folder is renamed with a git commit, and a context entry
    records why.

    Use it when you MEET a bad number — do not copy the pattern, and do not
    quietly work around it. If you think the right fix is a new item instead
    (different thing, not a mislabelled one), that is create_item + a relation,
    not this."""
    with db.connect() as conn:
        try:
            res = ops.renumber_item(conn, number, new_number=new_number,
                                    actor=actor, note=note)
        except ops.OpsError as exc:
            return _err(str(exc))
        except vault.VaultError as exc:
            return _err(f"vault move failed: {exc}")
        searchmod.touch(conn, res["number"])
    return _ok(**res)


@mcp.tool
def build_plan(number: str, version: str = "", waste_pct: float = 15.0) -> dict:
    """The shop answer for an assembly: what to PRINT (with geometry status,
    material, mass), what to MAKE (fabricated — each row with its routing and
    the raw stock it is cut off), what to BUY (with sourcing), the material
    rollup in grams including a waste allowance, and — whenever there is
    anything to make — the generated `cutlist` with stick counts and waste. Use
    before ordering filament, steel or hardware."""
    from plm.api import _build_plan
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        return _ok(number=number, version=rev["version"],
                   **_build_plan(conn, rev["id"], waste_pct))


# -- classification: declared file roles and tags -------------------
# Two registries with DELIBERATELY DIFFERENT strictness, and the difference is
# the answer to "encourage or enforce":
#
#   roles  CLOSED and enforced. An unregistered role is refused, because a
#          purpose no surface acts on is a typo.
#   tags   OPEN and encouraged. Any tag is accepted; the registry supplies
#          meaning and marks the CAVEATS. If a new word needed a code change,
#          the person holding the fact would write a sentence instead — and
#          prose is exactly where these facts were already hiding.

@mcp.tool
def classification_registry() -> dict:
    """THE VOCABULARY for what files ARE and what parts CARRY — read before
    declaring either.

    Returns `roles` (each with what it means, the format class it expects,
    whether it can represent a revision's geometry, whether it is release
    evidence) and `tags` (each with its meaning and whether it is a CAVEAT — an
    unresolved risk).

    WHY THIS EXISTS: plm used to work out what a file was from its filename, and
    wrote the guess into a field that looked like a declaration. On 837 files
    that was wrong for about a fifth of them — 60 STLs not marked as geometry,
    122 non-geometry files sitting in the `artifact` junk drawer, 613 with no
    recorded format at all. Format is now MEASURED from the bytes; purpose is
    DECLARED by you, because a PNG can be a drawing, a render, or a photo of a
    cracked weld and only the author knows which."""
    return _ok(roles=rolesmod.describe(), tags=tagsmod.describe(),
               guidance="Declare a role when you upload. Tag the caveats you "
                        "would otherwise have shouted in a summary "
                        "('PROVISIONAL', 'MEASURE', 'LOAD PATH UNPROVEN') — the "
                        "sentence still explains, the tag makes it findable.")


@mcp.tool
def set_file_role(file_id: int, role: str, actor: str = "mcp") -> dict:
    """DECLARE what one file is FOR — mesh, viz, drawing, render, photo, source,
    flat, doc, toolpath, log, archive, video (see classification_registry).

    Use this when you see `role_confirmed: false` on a file: plm inferred that
    role from the filename and has flagged it as its own guess. Confirming an
    inference is a real and useful act — the value may not change, but
    `role_source` becomes 'declared', which is what lets every other surface
    trust it.

    Prefer `mesh` over the legacy `artifact` for geometry, and `render` over
    `drawing` for a preview image — `drawing` means a DIMENSIONED drawing
    somebody could build from, and it counts as release evidence.

    Format is not settable: it is measured from the bytes."""
    with db.connect() as conn:
        try:
            return _ok(**ops.set_file_role(conn, file_id, role, actor=actor))
        except rolesmod.RoleError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))


@mcp.tool
def tag_item(number: str, tags: list[str], note: str = "", version: str = "",
             actor: str = "mcp") -> dict:
    """Tag an item, or ONE revision of it (`version`) — the queryable form of the
    caveats people write in capitals inside a summary.

    Item-level (no `version`) is for what is true of the thing forever:
    playa-exposed, load-bearing, fire-exposed. Revision-level is for this
    revision's state of knowledge: provisional-datum, measure-required,
    load-path-unproven, fit-unverified — the things a later revision RESOLVES.

    `note` holds the why, and should: a tag finds the fact, the sentence explains
    it. Keep the prose in the summary too; this does not replace it.

    Any tag is accepted (normalized to lower-hyphen so one fact keeps one
    spelling). Registered ones gain a meaning and, if they are caveats, show up
    in find_tagged(caveats=True)."""
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            return _err(f"no item {number}")
        rev = None
        if version:
            rev = db.find_revision(conn, number, version)
            if not rev:
                return _err(f"no revision {number} v{version}")
        try:
            with conn:
                out = [ops.set_tag(conn, item, t, rev=rev, note=note,
                                   actor=actor) for t in tags]
        except tagsmod.TagError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, number)
        return _ok(number=number, version=version or "(item-level)", tags=out)


@mcp.tool
def untag_item(number: str, tag: str, version: str = "",
               actor: str = "mcp") -> dict:
    """Remove a tag. Clearing a CAVEAT is how a caveat gets RESOLVED — do it when
    the thing has actually been measured, proven or fitted, not to tidy up. It is
    an ordinary write, so the event log records who decided it."""
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            return _err(f"no item {number}")
        rev = None
        if version:
            rev = db.find_revision(conn, number, version)
            if not rev:
                return _err(f"no revision {number} v{version}")
        try:
            with conn:
                gone = ops.clear_tag(conn, item, tag, rev=rev,
                                     actor=actor)["removed"]
        except tagsmod.TagError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))
        return _ok(number=number, tag=tag, removed=gone)


@mcp.tool
def find_tagged(tag: str = "", caveats: bool = False,
                project: str = "") -> dict:
    """Everything carrying a tag — the report that used to be a grep over prose.

    `caveats=True` answers the question nobody could ask before: what in this
    registry is not yet proven? Unverified datums, unproven load paths,
    unmeasured fits, undocumented as-builts. Run it before releasing anything,
    and before cutting a production batch."""
    with db.connect() as conn:
        return _ok(tags=tagsmod.find(conn, tag=tag, caveats_only=caveats,
                                     project=project))


@mcp.tool
def classification_review(limit: int = 60) -> dict:
    """Files whose classification a human or agent should settle, and why.

    Three issues: `mismatch` (declared purpose conflicts with the measured
    bytes), `legacy` (still parked in the `artifact` junk drawer, which says
    nothing), `unconfirmed` (the format admits more than one meaning — a PNG that
    could be a drawing, a photo or a render — and nobody has said which).

    Each row carries `candidates`, the roles actually worth choosing between.
    Fix one with set_file_role(file_id, role). The backfill deliberately
    did NOT auto-resolve these: guessing in bulk on a controlled record is how
    the original bug got in."""
    with db.connect() as conn:
        rows = reclassify.disagreements(conn)
        return _ok(summary=reclassify.report(conn), total=len(rows),
                   review=rows[:max(0, min(limit, 200))])


# -- fabrication: routings, process, stock, cut lists (plm/fab.py) ----
# The vocabulary is a registry dict; these five tools are its verbs. Adding an
# operation type is a dict entry in plm/fab.py — never a sixth tool.

@mcp.tool
def fab_registry() -> dict:
    """THE FABRICATION VOCABULARY, as data — read this before writing a routing.
    Returns every operation verb (CUT, DRILL, NOTCH, COPE, BEND, DEBURR, WELD,
    FINISH, plus CUT_PROFILE/SCRIBE for the plasma lane) with its well-known
    params and what each one MEANS, the `process` values, the end treatments, the
    stock forms, the stock attribute names, and the length units plm converts
    between. Unknown params are allowed and recorded; unknown VERBS are refused,
    because an operation no shop surface can route is not a record of work."""
    return _ok(**fab.registry())


@mcp.tool
def set_process(number: str, process: str, version: str = "",
                actor: str = "mcp") -> dict:
    """HOW this revision comes into existence: 'print' (additive — also what an
    unset process means) or 'make' (fabricated: cut, drill, notch, cope, weld,
    finish). This is the key the build plan dispatches on, so a fabricated part
    left unset silently appears under PRINT as "no STL".

    THERE IS DELIBERATELY NO 'buy': kind=purchased/hardware already says that,
    and one fact gets one home. And note the word is `process`, not
    `disposition` — that word is taken by the ECN affected-item concept
    (docs/PLM_CHANGES.md).

    DRAFT-only: this is manufacturing definition, not a measurement, so Revise ↑
    a frozen revision first. A weldment ASSEMBLY can be process='make' too, and
    should be — the WELD and FINISH operations belong to the weldment, not to the
    pieces it is welded from."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        try:
            res = ops.set_process(conn, rev, process, actor=actor)
        except fab.FabError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, number)
    return _ok(**res)


@mcp.tool
def set_routing(number: str, operations: list[dict], version: str = "",
                replace: bool = True, actor: str = "mcp") -> dict:
    """Write a revision's ROUTING — the ordered work that turns raw stock into
    this part. `operations` is [{op, params, work_center?, notes?}]; call
    fab_registry() first for the verbs and their params.

      set_routing('CCR-PRT-0003', [
        {'op': 'CUT',   'params': {'length': {'value': 86, 'unit': 'in'},
                                   'end_a': 'square', 'end_b': 'square'},
         'work_center': 'chop saw'},
        {'op': 'NOTCH', 'params': {'profile': 'saddle for 1.660 in OD',
                                   'positions': {'values': [-9, 0, 9], 'unit': 'in'},
                                   'datum': 'rail centreline'}}])

    THREE RULES, each with a refusal attached:
    · EVERY DIMENSION CARRIES ITS UNIT — {'value': 86, 'unit': 'in'}. A bare 86
      is refused, because 86 in and 86 mm are different parts and plm never
      defaults a physical unit. His steel is imperial; the plasma table is metric.
    · EXACTLY ONE stock-consuming operation (CUT / CUT_PROFILE) per revision. A
      cut part is ONE piece of stock; two pieces is two parts, and that is what
      the weldment's BOM is for.
    · A WELD BELONGS TO THE WELDMENT. Put WELD/FINISH on the assembly's revision,
      not on the rail.

    `replace=True` (the default) swaps the whole sequence, so an agent
    regenerating from CAD can call this repeatedly and get the same routing.
    DRAFT-only, like the BOM verbs; carries forward on revise() like files and
    attributes."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        try:
            res = ops.set_routing(conn, rev, operations, actor=actor,
                                  replace=replace)
        except fab.FabError as exc:
            return _err(str(exc))
        except ops.OpsError as exc:
            return _err(str(exc))
        searchmod.touch(conn, number)
    return _ok(revision_id=rev["id"], **res)


@mcp.tool
def get_routing(number: str, version: str = "") -> dict:
    """One revision's routing, in order, each step already glossed with its label
    and a readable one-line summary — plus `process` and the raw stock this part
    is cut from. The read half of set_routing."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        item = db.resolve_item(conn, number)
        return _ok(number=number, version=rev["version"],
                   revision_id=rev["id"],
                   process=fab.process_of(conn, rev["id"]),
                   stock=fab.stock_of(conn, item["id"]) if item else None,
                   operations=fab.get_routing(conn, rev["id"]))


@mcp.tool
def cut_list(number: str, version: str = "", kerf_in: float = 0.0,
             remnant_min_in: float = 0.0, markdown: bool = False) -> dict:
    """THE GENERATED CUT LIST for everything under one revision, with the 1-D
    nest into purchasable stick lengths. This is the artifact somebody used to
    maintain by hand in a CUT_LIST.md.

    Returns `cuts` (every MAKE part that consumes stock: finished length with its
    unit, quantity rolled up through the weldments, end treatments, the kerf and
    WHERE THE KERF CAME FROM, follow-on operations, and the weldment(s) it
    belongs to), `stock` (grouped by stock profile — stick count, per-stick cut
    assignments, remnants, yield and waste %), `weldments` (the joining and
    finishing routings), `issues` (every part plm could NOT nest, named, with the
    call that fixes it) and `algorithm` (first-fit-decreasing, with its limits
    spelled out: deterministic and never under-buying, but NOT optimal).

    `kerf_in` / `remnant_min_in` are FALLBACKS in inches — they never override a
    kerf the stock or the operation already carries. `markdown=True` returns the
    printable page (hand it straight to the shop-floor service print_markdown).

    WHAT IT NEEDS, and every missing piece comes back in `issues` naming the fix:
    process='make' on the part's revision (set_process), a CUT with a
    unit-bearing length (set_routing), a cut_from link to the raw stock
    (link_items('cut_from', part, stock)), and stock_length + stock_profile on
    that stock's revision (set_attributes)."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return _err(f"no revision for {number} {version or '(latest)'}")
        try:
            plan = fab.cut_list(
                conn, rev["id"],
                kerf={"value": kerf_in, "unit": "in"} if kerf_in else None,
                remnant_min={"value": remnant_min_in, "unit": "in"}
                if remnant_min_in else None)
        except fab.FabError as exc:
            return _err(str(exc))
    if markdown:
        return _ok(number=number, version=rev["version"],
                   markdown=fab.markdown(
                       plan, title=f"Cut list — {number} v{rev['version']}"),
                   totals=plan["totals"], issues=plan["issues"])
    return _ok(number=number, version=rev["version"], **plan)


@mcp.tool
def share_3d(name: str, content_b64: str = "", note: str = "",
             ttl_days: float = 7.0, actor: str = "mcp",
             handle: str = "", url: str = "", sha256: str = "",
             size: int = 0, filedrop_key: str = "") -> dict:
    """Share a DISPOSABLE STL as a 3D link — "hey, do you like this?".

    Small STL: pass content_b64. BIG STL: plm's own upload() `handle`
    (+ sha256, size; `filedrop_key` is the legacy alias), or `url` (+ sha256)
    to any https link plm can reach.
    Never base64 a large mesh.
    Either way you get back a URL that opens the model in plm's
    3D viewer (orbit, x-ray, dimensions), plus the expiry. Nothing is tracked:
    no item number, no revision, no file row, no approval — the snippet and its
    bytes delete themselves when the TTL runs out (default 7 days, capped at
    30). Think "3D artifact in the chat", not "part in the registry".

    Use it for: proposing a shape, showing a variant you may discard,
    illustrating a fit problem, before/after of a tweak.

    Do NOT use it for anything that should still exist next month, that someone
    might print from, or that another part will reference — that is a real part:
    create_item() + checkin() with a numbered, versioned revision. `note` is
    the one line the human reads next to the model, so make it the question you
    actually want answered.
    """
    handle = (handle or filedrop_key or "").strip()
    link = (url or "").strip()
    if not handle and not link and not content_b64:
        return _err("give handle from upload(), url (a link to the bytes), "
                    "or content_b64 for a small STL")
    data = b""
    if not handle and not link:
        try:
            data = base64.b64decode(content_b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"content_b64 is not valid base64: {exc}")
    with db.connect() as conn:
        try:
            if link:
                snip = snippets.create_from_url(
                    conn, url=link, sha256=sha256, size=size, name=name,
                    note=note, ttl_days=ttl_days, actor=actor)
            elif handle:
                snip = snippets.create_from_handle(
                    conn, handle=handle, sha256=sha256, size=size,
                    name=name, note=note, ttl_days=ttl_days, actor=actor)
            else:
                snip = snippets.create(conn, name=name, data=data, note=note,
                                       ttl_days=ttl_days, actor=actor)
        except snippets.SnippetError as exc:
            return _err(str(exc))
        except staging.StagingError as exc:
            return _err(str(exc))
    return _ok(snippet=snip, url=snip["url"],
               expires_in_days=snip["expires_in_days"],
               hint="Give the human the URL. It expires by itself; check the "
                    "geometry in as a part if it needs to be kept.")


@mcp.tool
def list_snippets(include_expired: bool = False) -> dict:
    """The disposable 3D snippets currently shared (see share_3d), newest
    first, with their expiry. Use to re-find a link you handed over."""
    with db.connect() as conn:
        return _ok(snippets=snippets.listing(conn,
                                             include_expired=include_expired))


@mcp.tool
def unshare_3d(token: str, actor: str = "mcp") -> dict:
    """Revoke a disposable 3D snippet now instead of waiting for its TTL —
    deletes both the object and the row. `token` is the last path segment of
    the share URL."""
    with db.connect() as conn:
        try:
            return _ok(**snippets.delete(conn, token, actor=actor))
        except snippets.SnippetError as exc:
            return _err(str(exc))
        except storage.StorageError as exc:
            return _err(f"could not delete the snippet object: {exc}")


@mcp.tool
def list_integrations() -> dict:
    """Registered downstream integrations (actions and event webhooks).

    ⛔ REDACTED (C1). This returned raw rows — including the `print` row, whose
    `url` carried the real the shop-floor service API key as a query parameter, on an
    unauthenticated port. `actions.public_row` is the ONE function that decides
    what an integration looks like from outside; `GET /api/integrations` and the
    event payloads use the same one, so the three cannot drift.
    """
    from plm import actions as actionmod
    with db.connect() as conn:
        return _ok(integrations=[actionmod.public_row(r) for r in
                                 conn.execute("SELECT * FROM integrations ORDER BY name")],
                   note="url is scheme+host+path only and secret-shaped config "
                        "keys are redacted: plm does not serve integration "
                        "credentials.")


# =============================================================================
# THE THIRD DOOR (security review C2 applied here).
# =============================================================================
#
# What the review could not finish: "plm_mcp/server.py (126 KB) was not read
# beyond its bind and auth posture (FastMCP, host="0.0.0.0", no auth middleware,
# no bearer check). 'Anonymous full write surface on :8096' is derived from code
# with only the port binding confirmed live. It is a third door onto the same
# ops layer and deserves its own pass."
#
# It was read for stage 7. WHAT WAS THERE:
#
#  1. ⛔ NO AUTHENTICATION AT ALL, on 0.0.0.0, with ~90 tools including
#     `decide_approval`, `checkin`, `put_file`, `submit_for_release`,
#     `rename_item` and `delete_file`. The single biggest finding in this file,
#     and the same one as C2 on the HTTP plane. Fixed below: the SAME token
#     store (plm/tokens.py) the API and the push door use, checked by an ASGI
#     middleware in front of the MCP app, under the same `PLM_AUTH_MODE`.
#  2. ⛔ `list_integrations` served raw rows, i.e. C1's credential, on this port
#     too. Fixed — it goes through `actions.public_row` like every other read
#     surface.
#  3. ✅ INJECTION: nothing here builds a shell string, a path or a ref itself.
#     Every git tool goes through `plm/gitrepo.py` (`safe_ref`, `safe_path`,
#     `repo_path`) and every write goes through `plm/ops.py`, so stage 7's
#     `plm/safegit.py` gate covers this door with no edit here. `get_item`'s
#     one filesystem read is `storage.local_copy` of a row plm wrote, size-
#     capped at 40 KB.
#  4. ✅ UNBOUNDED WORK: `search`, `activity` and `classification_review` all
#     clamp their `limit` (100 / 100 / 200), and `compare_revisions` passes a
#     fixed `_PATCH_PER_FILE`; `gitrepo.compare` now clamps `max_files` and
#     `max_patch` server-side as well (M8), which covers this caller too.
#  5. ✅ `share_3d` / `unshare_3d` write only this instance's own disk
#     (CACHE_DIR/snippets), so they cannot touch another instance's data.
#
# Remaining gap, stated: a token is an ACTOR, and these tools take `actor` as a
# parameter, so an authenticated holder of the agent's token can still pass
# `actor="geoff"` to a tool. The HTTP plane fixes that with
# `IdentityMiddleware._rewrite_actor`; doing the same here means rewriting a
# tool ARGUMENT rather than a JSON body, which is a FastMCP middleware hook and
# a bigger change than this review earns. `policy.may_approve` is unaffected —
# it asks the `users` row for `actor`, and a bot passing `actor="geoff"` to
# `decide_approval` produces a decision recorded as geoff's, which is the hole.
# the design notes carries it as a known gap.

class _McpAuth:
    """Bearer-token gate in front of the MCP app. Pure ASGI, no dependencies.

    Same store, same rule, same env var as the HTTP plane: under
    `PLM_AUTH_MODE=required` a request without a token from
    `PLM_TOKENS_FILE` is 401 with a `WWW-Authenticate`. Under
    `trusted-network` it is waved through, and the banner says so.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from plm import identity, tokens as tokenstore
        if scope["type"] != "http" or not identity.auth_required():
            return await self.app(scope, receive, send)
        headers = [(k.decode("latin-1"), v.decode("latin-1"))
                   for k, v in scope.get("headers") or []]
        actor = tokenstore.actor_for(tokenstore.bearer_of(headers))
        if actor:
            scope.setdefault("state", {})["plm_actor"] = actor
            return await self.app(scope, receive, send)
        body = (
            b'{"error":"plm\'s MCP needs a bearer token. It is the SAME token '
            b'that pushes a revision\\n(PLM_TOKENS_FILE); set it as the '
            b'Authorization header on the MCP transport.\\nThis instance is '
            b'PLM_AUTH_MODE=required."}')
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate",
                                 b'Bearer realm="plm-mcp"'),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# -- STAGE 15: the session's token IS the actor ------------------------------
# Both bots passed `actor="agent"` to every write by hand
# (the default is "mcp", which is not a user, so a submit refuses it). When the
# MCP request carries a bearer token from PLM_TOKENS_FILE — a workspace
# `.mcp.json` with `"headers": {"Authorization": "Bearer ${PLM_TOKEN}"}` — the
# token's actor is what every tool's `actor` argument becomes: filled in when
# omitted (or left at "mcp"), and OVERRIDDEN when it names somebody else, which
# also closes stage 7's "a token holder can pass actor='geoff'" gap for this
# door. No token: nothing changes (master's behaviour; compat.sh's driver
# sends none).
try:
    from fastmcp.server.middleware import Middleware as _Middleware
except Exception:                                          # noqa: BLE001
    _Middleware = None


#: a concise description's ceiling (bytes); the rest is tool_help(name)
CONCISE_MAX = 1400


def _concise(name: str, desc: str, params: set) -> str:
    """The tool's first paragraph, plus each later paragraph that is ABOUT
    one of its own arguments (it opens with the argument's name), capped at
    CONCISE_MAX — and a pointer to the rest. Deterministic, so a client's
    cache of tools/list stays valid."""
    import re as _re
    import textwrap as _tw
    text = _tw.dedent(desc).strip()
    paras = [p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return desc
    names = [x for x in params if x != "actor"]
    about = (_re.compile(r"(?:^[`*\s]*|`)(" + "|".join(map(_re.escape, names))
                         + r")\b(?:`|\s*[=:])", _re.M) if names else None)
    keep = [paras[0]]
    for p in paras[1:]:
        if about is not None and about.search(p):
            keep.append(p)
    out = "\n\n".join(keep)
    if len(out.encode()) > CONCISE_MAX:
        out = out.encode()[:CONCISE_MAX].decode(errors="ignore").rsplit(" ", 1)[0] + " …"
    if out != text:
        out += f"\n\n(More: tool_help('{name}').)"
    return out if len(out) < len(desc) else desc


def _session_actor() -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers
        from plm import tokens as tokenstore
        h = get_http_headers(include_all=True) or {}
        return tokenstore.actor_for(tokenstore.bearer_of(list(h.items()))) or ""
    except Exception:                                      # noqa: BLE001
        return ""


if _Middleware is not None:
    class _ActorFromToken(_Middleware):
        async def on_call_tool(self, context, call_next):
            who = _session_actor()
            if who:
                msg = context.message
                args = dict(msg.arguments or {})
                try:
                    tool = await context.fastmcp_context.fastmcp.get_tool(msg.name)
                    takes = "actor" in ((tool.parameters or {}).get("properties")
                                        or {})
                except Exception:                          # noqa: BLE001
                    takes = "actor" in args
                if takes and args.get("actor") != who:
                    args["actor"] = who
                    msg.arguments = args
            return await call_next(context)

        async def on_list_tools(self, context, call_next):
            """STAGE 16: with a token, `actor` is
            not a parameter at all. Stage 15 made the token decide the actor,
            but every write tool still ADVERTISED `actor` (default "mcp", and
            `required` on a few) and the instructions said "act under your own
            actor name" — so a model passed actor='agent' to every write, by
            hand, forever. A parameter that is always overridden is noise; the
            schema a token-holding session sees no longer has it, so the
            no-actor path is the only one. No token: master's schema.

            STAGE 19 (handoff finding 8): where git serves, each description
            is CONCISE — its first paragraph and the paragraphs about its own
            arguments (`_concise`). The global narrative (relation-type
            registries, the write loop, deprecations) is in the server
            instructions once, and tool_help(name) returns the full text. A
            git-off instance keeps master's descriptions."""
            tools = await call_next(context)
            who = _session_actor()
            git = _git_world()
            if not who and not git:
                return tools
            out = []
            for t in tools:
                upd: dict = {}
                params = dict(t.parameters or {})
                props = dict(params.get("properties") or {})
                if who and "actor" in props:
                    props.pop("actor")
                    params["properties"] = props
                    if "required" in params:
                        params["required"] = [r for r in params["required"]
                                              if r != "actor"]
                    upd["parameters"] = params
                if git and t.description:
                    short = _concise(t.name, t.description, set(props))
                    if short != t.description:
                        upd["description"] = short
                if not upd:
                    out.append(t)
                    continue
                try:
                    out.append(t.model_copy(update=upd))
                except Exception:                          # noqa: BLE001
                    out.append(t)
            return out

    mcp.add_middleware(_ActorFromToken())


from plm_mcp import tools_review; tools_review.install(mcp)  # noqa: E402,E702  STAGE 13 HOOK: the review model's tools (plm_mcp/tools_review.py)
from plm_mcp import tools_changes; tools_changes.install(mcp)  # noqa: E402,E702  STAGE 14 HOOK: changes + release sets (plm_mcp/tools_changes.py)
from plm_mcp import tools_routing; tools_routing.install(mcp)  # noqa: E402,E702  STAGE 20 HOOK: whoami on an `off` instance (plm_mcp/tools_routing.py)


def main() -> None:
    import uvicorn
    from plm import identity, tokens as tokenstore
    db.init()
    port = int(os.environ.get("PORT", "8098"))
    # ⚠️ 0.0.0.0 INSIDE THE CONTAINER is right — the published port is what
    # decides who can reach it, and the compose file binds it to
    # ${PLM_BIND} (127.0.0.1 by default).
    host = os.environ.get("PLM_MCP_HOST", "0.0.0.0")
    if identity.auth_required() and not tokenstore.configured():
        print("plm-mcp: PLM_AUTH_MODE=required and NO tokens are configured — "
              "every call will be 401.\n"
              f"         expected a token map at {tokenstore.TOKENS_FILE}",
              flush=True)
    print(f"plm-mcp: auth_mode={identity.AUTH_MODE} "
          f"tokens={len(tokenstore.load())} on {host}:{port}", flush=True)
    uvicorn.run(_McpAuth(mcp.http_app()), host=host, port=port,
                log_level="info")


if __name__ == "__main__":
    main()
