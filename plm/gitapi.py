"""`/api/repo/*` — the read API the repo page is built on (the repo API reference).

A separate module mounted by one hook in plm/api.py, for two reasons: the diff
against master stays a single import plus a single call, and the git-first read
surface is a complete thing somebody can delete in one commit if the POC is
rolled back.

Every route READS, with one exception added in stage 5: `POST
/api/repo/{n}/actions/{name}` hands ONE file at ONE ref to a registered
integration (a blob URL, by reference — never bytes). It writes nothing here;
what it does downstream is the integration's business. The data comes from plm/gitrepo.py
(the bare repos); the ONLY thing that reaches outside them is `/external`, which
streams a `[[files.external]]` file (`vault`, plm/storage.py) through plm.

The contract is documented in the repo API reference with a real example response per route.
Stage 2's UI builds against that document, so a change here is a change there.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import actions as actionmod
from . import context, db, gitindex as gitindexmod, gitrepo, ops, roles, storage

#: A preview is something a person is LOOKING at, so it is bounded by what a
#: browser will render rather than by what git can hand over.
PREVIEW_MAX = int(os.environ.get("PLM_PREVIEW_MAX", str(16 * 1024 * 1024)))


def _file_chunks(path: Path, chunk: int = 1 << 16):
    with path.open("rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                return
            yield buf

# ⛔ H2 — THIS MODULE NO LONGER HAS A MEDIA TABLE. It had one: a second,
# divergent copy of `plm/api.py`'s, with `".svg": "image/svg+xml"` in it and
# `image/` in its INLINE set — which re-opened, on the newest surface in the
# system, exactly the hole `plm/api.py:1379` had closed on purpose and
# documented in its own words. The rule is `plm/roles.py` now (`serve_media`,
# `may_preview`, `preview_media`), beside the registry that already decides what
# a file IS, and there is nothing here for a second opinion to live in.
#
# The DEFAULT IS `attachment` + `nosniff`. Rendering happens on `/preview`,
# which opts in per media type and answers under a sandbox CSP
# (`plm/websec.py`). A format nobody has thought about is a download.


def blob_headers(name: str, commit: str = "", *, disposition: str = "attachment",
                 extra: dict | None = None) -> dict:
    """The headers every raw-bytes response in this module carries."""
    h = {"Content-Disposition": roles.content_disposition(disposition, name),
         "X-Content-Type-Options": "nosniff",
         # A blob at a RESOLVED commit is immutable, and the UI fetches the same
         # mesh twice for an overlay.
         "Cache-Control": "private, max-age=600"}
    if commit:
        h["X-Plm-Commit"] = commit
    h.update(extra or {})
    return h


# -- one block on every file row ----------------------------------------------
#
# ⭐ EVERY FILE PAYLOAD THIS MODULE SERVES CARRIES THE SAME FOUR THINGS: what it
# is (`kind`), which room opens it (`preview` / `view`), which widgets can diff
# it (`diff`), and what you may DO with it (`actions`). Before stage 5 a git
# tree entry carried NONE of them — `f.actions` existed only on database rows —
# so the newest surface in the whole system was the one that could not print.
#
# The decision lives in `plm/roles.py` + `plm/actions.py`; this is only the
# place that knows the CONTEXT a repo row sits in (there is a ref behind it, it
# is or is not in the tree, that branch is or is not a draft, and whether an
# integration is registered to receive anything).

def ref_ctx(ref: str, *, tracked: bool = True, **extra) -> dict:
    """The context a row at a git ref sits in — what `actions.for_file` needs to
    answer "can I do this HERE", as opposed to "does this apply to this file"."""
    # `integration` is deliberately NOT here: whether anything is registered to
    # receive a file is the registry's own question (plm/actions.py), asked once
    # and cached, so every surface gets the same answer.
    ctx = {"ref": ref, "tracked": tracked,
           "draft": str(ref or "").startswith("draft/")}
    ctx.update(extra)
    return ctx


def decorate(f: dict, ctx: dict) -> dict:
    """Add `{kind, preview, view, diff, actions}` to one file row, in place."""
    f.update(roles.media_block(str(f.get("name") or f.get("path") or ""),
                               str(f.get("media") or ""),
                               kind=str(f.get("kind") or ""),
                               type_=str(f.get("type") or "")))
    f["actions"] = actionmod.for_file(f, ctx)
    f["menu"] = actionmod.menu_for(f, ctx)
    return f


def _roles_at(number: str, ref: str) -> dict:
    """path -> declared role, from `plm.toml` at that ref. The manifest is where
    a git tree keeps what the database keeps in `files.role`, and without it a
    tree row could not be classified at all."""
    try:
        man = gitrepo.manifest_at(number, ref)["manifest"]
    except gitrepo.RepoError:
        return {}
    files = (man.get("files") or {})
    out = {}
    for role, v in files.items():
        if role == "external" or not isinstance(v, list):
            continue
        for p in v:
            out.setdefault(str(p), role)
    return out


def _externals_at(number: str, ref: str) -> list[dict]:
    """`[[files.external]]` as file rows: files the manifest declares and the
    tree does not hold. They are part of the part, so they are
    listed with everything else — marked, and with their own actions."""
    try:
        man = gitrepo.manifest_at(number, ref)["manifest"]
    except gitrepo.RepoError:
        return []
    ctx = ref_ctx(ref, tracked=False)
    out = []
    for e in ((man.get("files") or {}).get("external") or []):
        name = str(e.get("name") or "")
        row = {**e, "name": name, "path": name, "type": "external",
               "source": "external",
               "url": gitrepo._ext_url(number, ref, name),
               "size": e.get("size"), "role": e.get("role") or ""}
        out.append(decorate(row, ctx))
    return out


# -- the proposal: a draft branch plus the human decision pending on it -------
#
# Everything above is pure git. A proposal is the one place the repo and the
# registry have to be read together — the branch is in git, the decision is a
# row — so these two routes are the seam, and they are still READ-ONLY: the
# write verbs (submit / decide / withdraw) are `plm/api.py`'s and are not
# duplicated here.

# `events.type` -> what the timeline calls it. Anything not in this map is
# rendered as itself rather than dropped: an event nobody has styled yet is
# still part of the record.
TIMELINE_KIND = {
    "git.index": "pushed",
    "submit": "submitted",
    "release": "approved",
    "release.rejected": "rejected",
    "release.withdrawn": "withdrawn",
    "git.withdraw": "branch-deleted",
}


def _draft_branch_for(info: dict, commit: str) -> str:
    """Which `draft/*` branch has this commit as its tip, if any.

    A git-origin DRAFT's branch is not stored anywhere: the commit is the fact
    and the branch is a pointer at it, which is also why the branch can be
    deleted at approval without losing the revision.
    """
    for d in info.get("drafts") or []:
        if d.get("commit") == commit:
            return d["ref"]
    return ""


def _proposal_refs(info: dict, rev: dict, decided: str) -> dict:
    """What this proposal should be diffed as, before and after the decision.

    While it is open: the draft branch against the newest release (or `main`).
    Once it is approved the branch is gone and `main` has moved onto it, so
    `main...` would be empty — the honest range is then the tag it became
    against the release before it, which is the same change, still readable.
    """
    commit = (rev.get("git_commit") or "").strip()
    releases = info.get("releases") or []
    branch = _draft_branch_for(info, commit)
    tag = (rev.get("git_tag") or "").strip()
    if decided == "approved" and tag:
        i = next((n for n, r in enumerate(releases) if r["ref"] == tag), -1)
        base = releases[i + 1]["ref"] if 0 <= i < len(releases) - 1 else ""
        return {"base": base, "head": tag, "branch": "", "open": False}
    head = branch or commit
    base = info.get("default_release") or ""
    if not base:
        others = [d for d in (info.get("drafts") or []) if d["ref"] != branch]
        base = others[0]["ref"] if others else ""
    return {"base": base, "head": head, "branch": branch, "open": True}


def _timeline(conn, revision_id: int, item_id: int, since: float,
              approval: dict) -> list[dict]:
    """The forge timeline (PLM_REVIEW_MODEL §2.8): `events` given a shape.

    `events` is already the audit log and is append-only; what it lacked was an
    ordering and a vocabulary. Comments on the approval are folded in at their
    own timestamps, so the page reads as one conversation rather than a log and
    a comment box that happen to sit near each other.

    ⚠️ `revision_id` alone is NOT enough. Withdrawing a pushed draft DELETES its
    revision row (`gitindex.withdraw_draft`) and SQLite hands the freed rowid to
    the next INSERT, so the events of a retired draft can surface under a
    brand-new one — measured, not theoretical. The item and the revision's own
    creation time fence it off.
    """
    out = []
    for r in conn.execute(
            "SELECT id, type, at, actor, data FROM events "
            "WHERE revision_id=? AND (item_id=? OR item_id IS NULL) AND at>=? "
            "ORDER BY at", (revision_id, item_id, since - 1)):
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        out.append({"kind": TIMELINE_KIND.get(r["type"], r["type"]),
                    "type": r["type"], "at": r["at"], "actor": r["actor"],
                    "data": data})
    for c in approval.get("context") or []:
        out.append({"kind": "commented", "type": "context",
                    "at": c.get("created_at"), "actor": c.get("actor") or "",
                    "data": {"body": c.get("body") or ""}})
    out.sort(key=lambda e: e.get("at") or 0)
    return out


class RepoActionReq(BaseModel):
    """Which file, at which ref. The file is the SUBJECT of the action and the
    caller names it — an action never guesses which file was meant."""
    ref: str = "HEAD"
    path: str = ""
    #: `[[files.external]]` rows are named by the manifest, not by a tree path.
    external: bool = False
    actor: str = ""
    #: Build the payload and stop at the wire. Also forced by
    #: `PLM_ACTIONS_DRY_RUN` — see the route.
    dry_run: bool = False


def gate_for(number: str, head: str, target_state: str) -> dict:
    """The release gate, BEFORE the click (§5.1). `pins` is every `parts/*`
    gitlink at head; `unreleased` is the subset that refuses an approval at
    RELEASED — the same question `gitindex.release_gate` asks at the moment of
    the decision, asked early so it is a warning, not a trap. Shared by the
    round-keyed route and the proposal route (plm/threadsapi.py)."""
    try:
        pins = gitrepo.pins_at(number, head)
    except gitrepo.RepoError:
        pins = []
    unreleased = [p for p in pins
                  if p["path"].startswith("parts/") and not p["released"]]
    return {"target_state": target_state, "pins": pins, "unreleased": unreleased,
            "blocks": bool(unreleased) and target_state == "RELEASED",
            "warns": bool(unreleased) and target_state != "RELEASED"}


def _project_of_number(conn, number: str) -> str:
    """An item's project — or a pull request's, for its own checks."""
    from . import checks
    return checks.subject(number).project(conn)


def install(app) -> None:
    """Mount the git-first read routes on an existing FastAPI app."""

    @app.exception_handler(gitrepo.RepoError)
    async def _repo_error(request, exc: gitrepo.RepoError):
        return JSONResponse(status_code=exc.code, content={"detail": str(exc)})

    @app.middleware("http")
    async def _git_mode_gate(request, call_next):
        """⭐ PLM_GIT_MODE (stage 9): the repo surface exists only where git is
        on. `/api/repo/<N>/…` for an item whose project is `off` answers 404
        with the reason — the same status `master` gives for a route it does
        not have, plus a sentence. The PROPOSAL route is exempt: an approval is
        registry state in every mode, and it is the page the Approve button is
        on."""
        path = request.url.path
        if path.startswith("/api/repo/"):
            parts = path.split("/")
            number = parts[3] if len(parts) > 4 else ""
            if number and parts[4] != "proposal":
                from . import gitmode
                with db.connect() as conn:
                    e = gitmode.explain(_project_of_number(conn, number), conn)
                if e["mode"] == "off":
                    return JSONResponse(status_code=404, content={
                        "detail": f"{number}: {e['label']} (PLM_GIT_MODE=off, "
                                  f"from {e['source']})",
                        "git_mode": "off"})
        return await call_next(request)

    @app.get("/api/repo")
    def api_repo_index():
        """Every generated repo, so a UI can build a picker without the DB."""
        repos = gitrepo.known_repos()
        return {"repos": repos, "count": len(repos),
                "clone_base": gitrepo.CLONE_BASE,
                "catalog": gitrepo.CATALOG_REPO}

    @app.get("/api/repo/{number}/refs")
    def api_repo_refs(number: str):
        out = gitrepo.refs(number)
        # PLM_GIT_MODE (stage 9): which world this repo is in, and — in `read`,
        # where the repo is a projection that follows legacy writes — whether
        # it is current. "syncing… as of <time>" is shown rather than old data
        # presented as now (plm/gitsync.py computes it from the event log).
        from . import gitmode, gitsync
        with db.connect() as conn:
            e = gitmode.explain(_project_of_number(conn, number), conn)
            out["git_mode"] = e["mode"]
            out["git_mode_label"] = e["label"]
            if e["mode"] == "read":
                out["sync"] = gitsync.status(conn, number)
        return out

    @app.get("/api/actions")
    def api_actions():
        """The two registries as data: every action, what it applies to and
        what it needs, plus the media-kind table that says how each kind is
        shown and diffed. The docs and the MCP read this instead of keeping a
        second copy of the vocabulary (same idea as `/api/roles`)."""
        return actionmod.describe()

    @app.get("/api/repo/{number}/tree")
    def api_repo_tree(number: str, ref: str = "HEAD", path: str = "",
                      last_commit: bool = True):
        out = gitrepo.tree(number, ref, path, last_commit=last_commit)
        role_of = _roles_at(number, out["ref"])
        for e in out["entries"]:
            if e["type"] == "tree":
                continue
            e["role"] = role_of.get(e["path"], "")
            e["url"] = gitrepo._blob_url(number, ref, e["path"])
            decorate(e, ref_ctx(ref, tracked=e["type"] == "blob"))
        # External rows belong to the part just as much as the tree does,
        # and they are declared at the ROOT of the manifest — so they ride with
        # the root listing rather than being merged in by every client.
        out["external"] = _externals_at(number, ref) if not path else []
        return out

    @app.get("/api/repo/{number}/blob")
    def api_repo_blob(number: str, ref: str = "HEAD", path: str = "",
                      download: bool = False):
        """The bytes at a ref. Always a DOWNLOAD (H2); rendering is /preview.

        ⭐ STREAMED ABOVE 8 MB (H5). `gitrepo.blob()` read the whole object into
        a `bytes` and `Response(content=…)` copied it again — the largest blob
        in these repos is 85,349,432 bytes, so one unauthenticated GET cost
        170-250 MB RSS and a dozen concurrent ones were an OOM on a box whose
        8 GB swap has been full before. `gitrepo.blob_stream` copies the shape
        `storage._git_stream` already had, with a byte ceiling.
        """
        info = gitrepo.blob_info(number, ref, path)
        media = roles.serve_media(path)
        # §6c-1: the tree is stable, the DOWNLOAD is labelled. At a release tag
        # the file leaves as `<NUMBER>_v<version>_<stem>.<ext>`; everywhere else
        # it keeps the name the tree gives it. `media` is still decided by the
        # real path, never by the label.
        headers = blob_headers(gitrepo.download_name(number, ref, path),
                               info["commit"])
        if info["size"] is not None and info["size"] <= gitrepo.BLOB_INLINE_MAX:
            data, commit = gitrepo.blob(number, ref, path)
            headers["Content-Length"] = str(len(data))
            return Response(content=data, media_type=media, headers=headers)
        it, commit, size = gitrepo.blob_stream(number, ref, path)
        if size is not None:
            headers["Content-Length"] = str(size)
        return StreamingResponse(it, media_type=media, headers=headers)

    @app.get("/api/repo/{number}/preview")
    def api_repo_preview(number: str, ref: str = "HEAD", path: str = ""):
        """The bytes at a ref, RENDERED — the sandboxed half of H2's fix.

        A separate route, not a query parameter on `/blob`, for three reasons:
        it is visible in an access log, `plm/websec.py` keys its CSP off the
        path, and a production deployment can put it on a separate origin with
        one nginx `location` block, which is the only real sandbox there is.

        What it will render is `roles.PREVIEW_MEDIA` and nothing else. An SVG is
        served as `image/svg+xml` here — and ONLY here — under
        `Content-Security-Policy: … script-src 'none'; sandbox`, so the document
        that can carry script is the one served into a context that has none.
        """
        info = gitrepo.blob_info(number, ref, path)
        media = roles.serve_media(path)
        if not roles.may_preview(media):
            raise HTTPException(
                415, f"plm will not render {media} in a browser. "
                     f"GET /api/repo/{number}/blob?ref={ref}&path={path} "
                     "downloads it.")
        if info["size"] is not None and info["size"] > PREVIEW_MAX:
            raise HTTPException(
                413, f"{path} is {info['size']:,} bytes — too large to preview "
                     f"(limit {PREVIEW_MAX:,}). Download it instead.")
        data, commit = gitrepo.blob(number, ref, path, max_bytes=PREVIEW_MAX)
        return Response(content=data,
                        media_type=roles.preview_media(media),
                        headers=blob_headers(path, commit,
                                             disposition="inline"))

    @app.get("/api/repo/{number}/external")
    def api_repo_external(number: str, ref: str = "HEAD", name: str = ""):
        """Stream a file named in this ref's `[[files.external]]`.

        The tree does not hold it, and the 3-D overlay still needs the bytes —
        so plm serves them from wherever the row says (`local`, `vault`;
        plm/storage.py). The manifest is the authorisation: only a key this
        ref actually declares is fetchable, which is why `name=` is a file
        NAME and not a storage key.
        """
        man = gitrepo.manifest_at(number, ref)["manifest"]
        rows = (man.get("files") or {}).get("external") or []
        row = next((e for e in rows if e.get("name") == name), None)
        if not row:
            raise HTTPException(
                404, f"{name!r} is not in [[files.external]] at {ref}")
        media = roles.serve_media(name)
        # ⛔ H1 — the manifest does not choose the backend or the key. The row
        # was stored through `storage.external_key`, and it is re-derived here
        # rather than trusted, so a repo whose rows predate that check cannot
        # point this at somebody else's object either.
        try:
            backend, key = gitindexmod.external_location(
                number, gitrepo.manifest_at(number, ref)["manifest"], row)
            kind, obj = storage.resolve({"backend": backend, "key": key,
                                         "path": row.get("name", name),
                                         "sha256": row.get("sha256", "")})
        except storage.StorageError as exc:
            raise HTTPException(502, str(exc)) from exc
        headers = blob_headers(name, extra={
            "X-Plm-Sha256": row.get("sha256", "")})
        if kind == "file":
            if not obj.is_file():
                raise HTTPException(404, f"bytes missing for {name}")
            # ⛔ H5 — streamed, not `obj.read_bytes()`. This had the same shape
            # as the blob route and the same cost.
            size = obj.stat().st_size
            headers["Content-Length"] = str(size)
            return StreamingResponse(_file_chunks(obj), media_type=media,
                                     headers=headers)
        return StreamingResponse(obj, media_type=media, headers=headers)

    @app.get("/api/repo/{number}/manifest")
    def api_repo_manifest(number: str, ref: str = "HEAD"):
        return gitrepo.manifest_at(number, ref)

    @app.get("/api/repo/{number}/commits")
    def api_repo_commits(number: str, ref: str = "HEAD", limit: int = 50,
                         path: str = ""):
        return gitrepo.commits(number, ref, limit, path)

    @app.get("/api/repo/{number}/drafts")
    def api_repo_drafts(number: str):
        """Every `draft/*` branch as a proposal row — the Drafts tab.

        Git says what the branch is and how far it has travelled; the registry
        says whether anybody has been asked to decide about it. A draft with no
        `proposal` is GitHub's draft PR: pushed, diffable, not in the queue, and
        one button away from being in it.
        """
        out = gitrepo.drafts(number)
        with db.connect() as conn:
            item = conn.execute("SELECT id FROM items WHERE number=?",
                                (number,)).fetchone()
            revs = {}
            if item:
                revs = {(r["git_commit"] or ""): dict(r) for r in conn.execute(
                    "SELECT id, version, state, git_commit, summary, author "
                    "FROM revisions WHERE item_id=?", (item["id"],))}
            for d in out["drafts"]:
                rev = revs.get(d["commit"])
                # A DRAFT `split` replayed carries a VAULT sha in `git_commit`,
                # so matching on the commit is what separates "this branch IS
                # the revision" from "this branch merely has that version".
                if not rev:
                    rev = next((r for r in revs.values()
                                if r["version"] == d.get("version")
                                and r["state"] in ("DRAFT", "IN_REVIEW")), None)
                    d["git_origin"] = False
                else:
                    d["git_origin"] = True
                d["revision"] = ({"id": rev["id"], "version": rev["version"],
                                  "state": rev["state"]} if rev else None)
                d["proposal"] = None
                # STAGE 13 (additive): the proposal object for this BRANCH —
                # #n, its state and its thread counts (open / blocking), which
                # the Drafts tab badges. Found by branch first: a draft nobody
                # submitted is a DRAFT proposal with no round yet.
                from . import pending, proposals
                proposals.ensure(conn)
                pr = conn.execute(
                    "SELECT * FROM proposals WHERE item_id=? AND head_ref=? "
                    "ORDER BY id DESC LIMIT 1",
                    (item["id"], "refs/heads/" + d["ref"]
                     if not d["ref"].startswith("refs/") else d["ref"])
                ).fetchone() if item else None
                if not pr and rev:
                    pr = proposals.for_revision(conn, rev["id"])
                d["review"] = proposals.brief(conn, dict(pr)) if pr else None
                # the push that opens a pull request for this branch — the
                # same line the yellow banner carries (pending.push_line)
                d["push"] = pending.push_line(d.get("summary") or "", d["ref"])
                if not rev:
                    continue
                a = conn.execute(
                    "SELECT * FROM approvals WHERE revision_id=? "
                    "ORDER BY id DESC LIMIT 1", (rev["id"],)).fetchone()
                if a:
                    row = dict(a)
                    row["comments"] = context.counts(
                        conn, "approval", [row["id"]]).get(row["id"], 0)
                    d["proposal"] = row
                d["state"] = rev["state"] or d.get("state") or "DRAFT"
        return out

    @app.get("/api/repo/{number}/proposal/{approval_id}")
    def api_repo_proposal(number: str, approval_id: int):
        """One proposal, everything the decision needs except the diff itself.

        The diff is `/compare?base=&head=` — the same call the compare view has
        always made, with the refs this route resolves. Two reads of the same
        range would be two answers to one question.
        """
        with db.connect() as conn:
            a = conn.execute(
                """SELECT a.*, r.version, r.state, r.summary, r.author,
                          r.git_commit, r.git_tag, r.item_id, r.created_at,
                          i.number, i.name, i.kind, i.project
                   FROM approvals a
                   JOIN revisions r ON r.id=a.revision_id
                   JOIN items i ON i.id=r.item_id
                   WHERE a.id=?""", (approval_id,)).fetchone()
            if not a:
                raise HTTPException(404, f"no approval {approval_id}")
            if a["number"] != number:
                raise HTTPException(
                    404, f"approval {approval_id} is {a['number']}'s, not "
                         f"{number}'s")
            row = dict(a)
            # The explode gate's waiver, parsed out of the note it is recorded
            # in (master's format; ops.explode_waiver_of is its one reader),
            # and the gate's current answer — so the page can show both.
            row["explode_waiver"] = ops.explode_waiver_of(row.get("note") or "")
            explode = ops.explode_block(conn, number, a["version"])
            row["comments"] = context.counts(
                conn, "approval", [approval_id]).get(approval_id, 0)
            row["context"] = (context.entries(conn, "approval", approval_id)
                              ["entries"] if row["comments"] else [])
            timeline = _timeline(conn, row["revision_id"], row["item_id"],
                                 row.pop("created_at") or 0, row)
            # STAGE 13 (additive): the PROPOSAL this round belongs to — the UI
            # redirects a round link (`/N/proposal/<approval id>`, and the
            # hash era's `#/item/N/proposal/<approval id>`) to `/N/pull/<pid>`.
            from . import proposals
            prop = proposals.for_approval(conn, approval_id)
            pbrief = proposals.brief(conn, prop) if prop else None

        item = {"number": row.pop("number"), "name": row.pop("name"),
                "kind": row.pop("kind"), "project": row.pop("project") or ""}
        rev = {"id": row["revision_id"], "version": row.pop("version"),
               "state": row.pop("state"), "summary": row.pop("summary"),
               "author": row.pop("author"),
               "git_commit": row.pop("git_commit") or "",
               "git_tag": row.pop("git_tag") or ""}
        row.pop("item_id", None)

        out = {"number": number, "approval": row, "item": item,
               "revision": rev, "timeline": timeline, "explode": explode,
               # Honest placeholders: PLM_REVIEW_MODEL §2.4 and §2.6 are
               # designed, not built. An empty list with a reason is a promise;
               # invented rows would be a lie on a page people sign from.
               "threads": {"count": row["comments"], "built": True,
                           **((pbrief or {}).get("threads") or {})},
               "proposal": pbrief,
               "checks": {"checks": [], "built": False}}
        from . import gitmode
        with db.connect() as conn:
            out["git_mode"] = gitmode.mode_for(_project_of_number(conn, number),
                                               conn)
        if out["git_mode"] == "off":
            out["git"] = {"origin": False,
                          "why": "git-first is off for this project — the "
                                 "registry is the record (PLM_GIT_MODE=off)"}
            return out
        try:
            info = gitrepo.refs(number)
        except gitrepo.RepoError as exc:
            out["git"] = {"origin": False, "why": str(exc)}
            return out
        decided = row["decision"] or ""
        refs_ = _proposal_refs(info, rev, decided)
        # `revisions.git_commit` holds a VAULT sha for everything `split`
        # replayed, so "it resolves as a commit in THIS repo" is the test for a
        # git origin — not "the column is non-empty" (plm/ops.git_origin says
        # the same thing the same way).
        origin = False
        if rev["git_commit"]:
            try:
                gitrepo.resolve(gitrepo.repo_path(number), rev["git_commit"])
                origin = True
            except gitrepo.RepoError:
                origin = False
        out["git"] = {
            "origin": origin, "clone_url": info["clone_url"],
            "default_release": info["default_release"],
            "default_ref": info["default_ref"], **refs_,
        }
        out["gate"] = gate_for(number, refs_["head"] or info["default_ref"],
                               row["target_state"])
        return out

    @app.get("/api/repo/{number}/compare")
    def api_repo_compare(number: str, base: str = "", head: str = "",
                         max_patch: int = 60000, max_files: int = 300):
        # ⚠️ M8 — `max_files` / `max_patch` are CLAMPED, in `gitrepo.compare`
        # (which every caller goes through, including the release-notes draft
        # and the MCP) rather than here. `?max_files=1000000&max_patch=1000000000`
        # used to run `_patch()` per file across a whole history on an
        # unauthenticated GET; the ceilings are gitrepo.MAX_FILES_CAP /
        # MAX_PATCH_CAP and the response reports what was actually applied.
        if not base or not head:
            raise HTTPException(400, "base= and head= are both required")
        out = gitrepo.compare(number, base, head, max_patch=max_patch,
                              max_files=max_files)
        # A compare row is a file row: same block, same menu, same widgets. The
        # ref a row's actions act at is HEAD — "compare with…" and "print" mean
        # the version you are looking at, not the one it replaced. A deleted
        # file is only at base, so that row points there instead.
        for f in out["files"]:
            decorate(f, ref_ctx(base if f["status"] == "deleted" else head))
        for p in out["pairs"]:
            side = p.get("head") or p.get("base") or {}
            p.setdefault("path", side.get("path", ""))
            decorate(p, ref_ctx(head, tracked=side.get("source") != "external"))
        return out

    @app.post("/api/repo/{number}/actions/{name}")
    def api_repo_action(number: str, name: str, req: RepoActionReq):
        """Run a registered action on a file IN THE REPO, addressed by
        `number + ref + path` — the git-first twin of
        `POST /api/files/{id}/actions/{name}`.

        ⭐ This is what makes the repo tree a first-class surface instead of a
        read-only curiosity: a mesh at a tag can be sent to the build plate
        without anybody first finding the database row that happens to mirror
        it. The integration receives a blob URL — BY REFERENCE, never bytes —
        exactly as the database path does.

        ⛔ The action must be one the REGISTRY offers for this file, and it must
        be enabled. Not a convenience check: it is what stops `/actions/print`
        becoming a way to post arbitrary files at arbitrary integrations by
        guessing at names.
        """
        spec = actionmod.known(name)
        if not spec:
            raise HTTPException(404, f"no such action: {name}")
        if spec.get("type") != "integration":
            raise HTTPException(
                400, f"{name} is a {spec.get('type')} action — it is performed "
                     "by the client, not by the server")
        ref, path = req.ref or "HEAD", (req.path or "").strip()
        if not path:
            raise HTTPException(400, "path= is required")

        # ⭐ CLASSIFY FROM THE BYTES, not from the name. This is the one place
        # that can afford to: one file, one click. (The tree listing falls back
        # to the extension because sniffing every row would be a blob read per
        # row — and `roles.sniff_media` records which of the two it was.)
        if req.external:
            row = next((e for e in _externals_at(number, ref)
                        if e["name"] == path), None)
            if not row:
                raise HTTPException(
                    404, f"{path!r} is not in [[files.external]] at {ref}")
            f = {"name": path, "path": path, "role": row.get("role", ""),
                 "media": roles.sniff_media(None, path)[0], "source": "external",
                 "size": row.get("size")}
            url, tracked = row["url"], False
        else:
            data, commit = gitrepo.blob(number, ref, path)
            f = {"name": Path(path).name, "path": path,
                 "role": _roles_at(number, ref).get(path, ""),
                 "media": roles.sniff_media(data, path, len(data))[0],
                 "size": len(data)}
            url, tracked = gitrepo._blob_url(number, ref, path), True

        offered = {a["name"]: a for a in
                   actionmod.menu_for(f, ref_ctx(ref, tracked=tracked))}
        if name not in offered:
            raise HTTPException(
                409, f"{offered.get(name, {}).get('label') or name} does not "
                     f"apply to {path}")
        if not offered[name]["enabled"]:
            raise HTTPException(409, offered[name]["reason"]
                                or "not available for this file")

        man = gitrepo.manifest_at(number, ref)["manifest"]
        base = os.environ.get("PLM_WEB_BASE", "").rstrip("/")
        payload = {"action": name, "number": number,
                   "name": str(man.get("name") or ""),
                   "version": str(man.get("version") or ""),
                   "state": str(man.get("state") or ""),
                   "actor": req.actor or "", "path": path,
                   "ref": ref, "commit": gitrepo.resolve(
                       gitrepo.repo_path(number), ref),
                   "files": [{"path": path, "size": f.get("size"),
                              "role": f.get("role") or "",
                              "media": f.get("media") or "",
                              "url": base + url if base else url}]}

        with db.connect() as conn:
            ig = conn.execute(
                "SELECT * FROM integrations WHERE name=? AND enabled=1",
                (spec["integration"],)).fetchone()
        if not ig:
            raise HTTPException(
                404, f"no enabled integration named {spec['integration']}")

        # ⛔ THE POC DOES NOT PRINT, and that guard lives in ONE place now
        # (C3). `actions.send()` holds the dry-run check, the scheme/host
        # allowlist, the timeout and the credential-from-a-file, and it is the
        # only outbound HTTP call in plm — `dispatch_event` goes through it too,
        # which is the hole this review found.
        try:
            res = actionmod.send(ig, payload,
                                 what=f"{Path(path).name} at {ref}",
                                 actor=req.actor,
                                 force_dry_run=bool(req.dry_run))
        except actionmod.SendError as exc:
            if 400 <= exc.code < 500:
                # A downstream REFUSAL is an answer, not a transport failure —
                # it travels with its own status and its own details, which are
                # often a list the UI can turn into a picker.
                body = exc.body if isinstance(exc.body, dict) else {}
                raise HTTPException(status_code=exc.code, detail={
                    "error": str(exc), "code": body.get("code") or "",
                    "details": body.get("details") or {}}) from None
            raise HTTPException(exc.code, str(exc)) from None
        try:
            cfg = json.loads(ig["config"] or "{}")
        except (ValueError, TypeError):
            cfg = {}
        out = {"ok": True, "integration": ig["name"],
               "to": res["to"], "result": res["result"],
               "link": cfg.get("queue") or ""}
        if res.get("dry_run"):
            out["dry_run"] = True
            out["payload"] = res["payload"]
        return out
