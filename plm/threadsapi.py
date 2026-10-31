"""HTTP for the review model (stage 13): proposals, reviews, threads, timeline.

Mounted by ONE call in `plm/api.py` (`threadsapi.install(app)`), the
gitapi / checksapi pattern. The verbs are `plm.ops` (submit / decide /
withdraw — unchanged, frozen stage-4 routes still work) and `plm/proposals.py`
/ `plm/threads.py`; this file is routing, identity and response shape only.

EVERY READ TAKES A SCOPE, not one id: `?proposal=3`, `?proposal=3,4,9`,
`?release=12`. A stage-14 change page mounts the same panels over N proposals.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from . import db, ops, proposals, threads


def _api():
    from . import api       # lazily: api imports this module while building app
    return api


def _ids(raw: str) -> list[int]:
    out = []
    for x in (raw or "").replace("#", "").replace("p", "").split(","):
        x = x.strip()
        if x:
            if not x.isdigit():
                raise HTTPException(400, f"not an id: {x!r}")
            out.append(int(x))
    return out


def _scope(proposal: str = "", release: str = "", change: str = "") -> list:
    """`?change=CHG-n` (stage 14) is the change's own conversation PLUS every
    member proposal's threads — the change page's one panel."""
    sc = [("proposal", i) for i in _ids(proposal)] + \
         [("release", i) for i in _ids(release)]
    if change:
        from . import changes
        with db.connect() as conn:
            for raw in change.split(","):
                try:
                    cid = changes.parse_id(raw)
                    changes.row(conn, cid)
                except changes.ChangeError as exc:
                    raise HTTPException(exc.code, str(exc)) from None
                sc.append(("change", cid))
                sc += [("proposal", r[0]) for r in conn.execute(
                    "SELECT id FROM proposals WHERE change_id=? ORDER BY id",
                    (cid,))]
    if not sc:
        raise HTTPException(400, "name a scope: ?proposal=<id>[,<id>…] and/or "
                                 "?release=<id>")
    # The pull request page asks for `change=<n>&proposal=<member>…`; the
    # change already brings its members, so a subject named twice is listed
    # (and counted) once.
    return list(dict.fromkeys(sc))


def _guard(fn):
    try:
        return fn()
    except threads.ThreadError as exc:
        raise HTTPException(exc.code, str(exc)) from None
    except ops.OpsError as exc:
        raise HTTPException(exc.code, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except Exception as exc:
        from . import changes, releasesets
        if isinstance(exc, (changes.ChangeError, releasesets.SetError)):
            raise HTTPException(exc.code, str(exc)) from None
        raise


def _identity(request: Request) -> str:
    return (getattr(request.state, "plm_identity", None) or {}).get(
        "via", "unverified")


# -- the proposal view: everything the page needs but the diff ----------------

def view(conn, pid: int) -> dict:
    """The proposal page's payload. Shaped like the stage-4 round-keyed one
    (`/api/repo/{n}/proposal/{approval_id}`: number, approval, item, revision,
    git, gate, timeline, threads, checks, explode, git_mode) so the page's
    header, gate and decision bar read either — plus `proposal` (the object,
    its rounds and its SHA-bound reviews)."""
    from . import gitapi, gitmode, gitrepo, context
    P = proposals.get(conn, pid)
    number = P["item"]["number"]
    rev = P["revision"] or {
        "id": None, "version": P["version"], "summary": P["title"],
        "author": P["author"], "git_commit": P["head_sha"],
        "git_tag": P["merged_tag"],
        "state": {"withdrawn": "WITHDRAWN", "merged": "RELEASED"}.get(
            P["state"], "DRAFT")}
    rev = {k: rev.get(k) for k in ("id", "version", "state", "summary", "author",
                                   "git_commit", "git_tag")}
    rnd = P["current_round"] or (P["rounds"][-1] if P["rounds"] else None)
    approval = None
    if rnd:
        approval = dict(rnd)
        approval["explode_waiver"] = ops.explode_waiver_of(approval.get("note") or "")
        approval["comments"] = context.counts(conn, "approval", [rnd["id"]]).get(
            rnd["id"], 0)
    tc = threads.counts(conn, [("proposal", pid)]).get(("proposal", pid),
                                                       threads.ZERO)
    out = {"number": number, "proposal": P, "approval": approval,
           "item": P["item"], "revision": rev,
           "timeline": proposals.timeline(conn, [("proposal", pid)]),
           "threads": {"built": True, **tc},
           "explode": ops.explode_block(conn, number, rev["version"])
           if rev.get("id") else None,
           "git_mode": gitmode.mode_for(P["item"].get("project") or "", conn)}
    if out["git_mode"] == "off":
        out["git"] = {"origin": False,
                      "why": "git-first is off for this project — the registry "
                             "is the record (PLM_GIT_MODE=off)"}
        return out
    try:
        info = gitrepo.refs(number)
    except gitrepo.RepoError as exc:
        out["git"] = {"origin": False, "why": str(exc)}
        return out
    decided = "approved" if P["state"] in ("merged", "approved") else ""
    refs_ = gitapi._proposal_refs(info, {**rev, "git_commit": P["head_sha"]
                                         if not decided else rev["git_commit"]},
                                  decided)
    branch = (P["head_ref"] or "").replace("refs/heads/", "")
    if not decided and branch and any(d["ref"] == branch
                                      for d in info.get("drafts") or []):
        refs_.update(head=branch, branch=branch, open=True)
    elif not decided and P["head_sha"]:
        refs_.update(head=P["head_sha"], branch="", open=False)
    rb = P.get("review_base") or {}
    if not decided and rb.get("sha"):
        # stage 19: diff against the REVIEW BASELINE (the draft/tag the head
        # descends from, or `-o base=`), named by its ref while the ref still
        # points there — not "the newest release, else any other draft"
        named = ""
        if rb.get("ref"):
            try:
                named = rb["ref"] if gitrepo.resolve(
                    gitrepo.repo_path(number), rb["ref"]) == rb["sha"] else ""
            except gitrepo.RepoError:
                named = ""
        refs_["base"] = (named.replace("refs/heads/", "").replace("refs/tags/", "")
                         if named else rb["sha"])
    origin = False
    if P["head_sha"]:
        try:
            gitrepo.resolve(gitrepo.repo_path(number), P["head_sha"])
            origin = True
        except gitrepo.RepoError:
            origin = False
    # stage 19 (handoff finding 2): the two identities a reviewer sees —
    # who WROTE the commit (git's author, whatever the author's git config
    # said) and who PUSHED it (the actor plm verified from the bearer token)
    author = ""
    if origin:
        try:
            author = gitrepo.git(gitrepo.repo_path(number), "log", "-1",
                                 "--format=%an <%ae>", P["head_sha"],
                                 check=False).strip()
        except gitrepo.RepoError:
            author = ""
    out["identities"] = {
        "commit_author": author, "pusher": P["author"],
        "say": ("The commit author is what the author's git config said; the "
                "pusher is the actor plm verified from the token that pushed "
                "it — they differ legitimately, and only the pusher is "
                "authenticated.")}
    out["git"] = {"origin": origin, "clone_url": info["clone_url"],
                  "default_release": info["default_release"],
                  "default_ref": info["default_ref"],
                  "head_sha": P["head_sha"], "keep_ref": P["head_keep_ref"],
                  **refs_}
    out["gate"] = gitapi.gate_for(
        number, refs_["head"] or info["default_ref"],
        (approval or {}).get("target_state") or P["target_maturity"] or "PROTOTYPE")
    return out


# -- bodies --------------------------------------------------------------------

class PSubmitReq(BaseModel):
    target_state: str = "PROTOTYPE"
    note: str = ""
    actor: str = ""
    explode_waiver: str = ""


class ReviewReq(BaseModel):
    verdict: str = Field(pattern="^(approve|reject|comment)$")
    body: str = ""
    head_sha: str = Field(default="", description="the sha you looked at — a "
                          "decision on a head that has since moved is refused")
    actor: str = ""


class ThreadReq(BaseModel):
    subject_type: str = "proposal"
    subject_id: int
    body: str
    anchor: dict = Field(default_factory=lambda: {"kind": "subject"})
    title: str = ""
    blocking: bool = False
    suggestion: dict | None = None
    actor: str = ""


class ReplyReq(BaseModel):
    body: str
    actor: str = ""


class ResolveReq(BaseModel):
    reason: str = ""
    addressed_by: str = ""
    actor: str = ""


class ActorReq(BaseModel):
    actor: str = ""
    body: str = ""


def install(app) -> None:

    # ---- proposals ---------------------------------------------------------
    @app.get("/api/proposals")
    def api_proposals(item: str = "", state: str = "", change_id: int | None = None,
                      ids: str = "", limit: int = 100):
        """Proposals, newest activity first. `state` is a csv of draft, open,
        approved, merged, rejected, withdrawn. `change_id` is the stage-14 seam."""
        with db.connect() as conn:
            return _guard(lambda: {"proposals": proposals.listing(
                conn, item=item, states=[x for x in state.split(",") if x],
                change_id=change_id, ids=_ids(ids), limit=min(limit, 500))})

    @app.get("/api/proposals/by-approval/{approval_id}")
    def api_proposal_by_approval(approval_id: int):
        """Which proposal a round belongs to — round links (`/N/proposal/<id>`,
        the hash era's `#/item/N/proposal/<id>`) are approval ids, and redirect
        through this to `/N/pull/<pid>`."""
        with db.connect() as conn:
            p = proposals.for_approval(conn, approval_id)
            if not p:
                raise HTTPException(404, f"no approval {approval_id}")
            return proposals.brief(conn, p)

    @app.get("/api/proposals/{pid}")
    def api_proposal(pid: int):
        with db.connect() as conn:
            return _guard(lambda: view(conn, pid))

    @app.post("/api/proposals/{pid}/submit")
    def api_proposal_submit(pid: int, req: PSubmitReq, request: Request):
        """Open a round on this proposal — `ops.submit_for_release` on its
        revision, the one submit every door uses."""
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            def go():
                P = proposals.get(conn, pid)
                if not P["revision"]:
                    raise threads.ThreadError(
                        f"proposal #{pid} has no draft to submit "
                        f"({P['state']})", 409)
                return ops.submit_for_release(
                    conn, P["item"]["number"], version=P["revision"]["version"],
                    target_state=req.target_state, note=req.note, actor=actor,
                    explode_waiver=req.explode_waiver)
            out = _guard(go)
        return A._decided(out)

    @app.post("/api/proposals/{pid}/reviews")
    def api_proposal_review(pid: int, req: ReviewReq, request: Request):
        """A review, bound to `head_sha`.

        `approve` / `reject` from somebody policy admits DECIDES the open round
        (`ops.decide_approval`, the merge on approve). From somebody it does not
        — an agent, by default — it is recorded as an ADVISORY review: stamped,
        kept, never counted (§2.2, "bots review, bots never count", which is a
        policy SETTING per the DECIDED box). `comment` never decides."""
        A = _api()
        with db.connect() as conn:
            claimed = req.actor
            if req.verdict == "comment":
                actor = A._actor_for_write(request, claimed)
                out = _guard(lambda: {"ok": True, "review": proposals.add_review(
                    conn, pid, actor=actor, verdict="comment", body=req.body,
                    head_sha=req.head_sha, identity=_identity(request))})
                return out
            actor = A._require_verified_actor(request, claimed)

            def go():
                P = proposals.get(conn, pid)
                rnd = P["current_round"]
                user = db.get_user(conn, actor) or {}
                if req.head_sha and P["head_sha"] and not P["head_sha"].startswith(
                        req.head_sha.lower()):
                    raise ops.OpsError(
                        f"you reviewed {req.head_sha[:10]}, but #{pid}'s head is "
                        f"now {P['head_sha'][:10]} — reload and look again.", 409)
                if not rnd:
                    if user.get("kind") == "bot":
                        return {"ok": True, "advisory": True,
                                "review": proposals.add_review(
                                    conn, pid, actor=actor, verdict=req.verdict,
                                    body=req.body, head_sha=req.head_sha,
                                    identity=_identity(request))}
                    raise ops.OpsError(
                        f"#{pid} is a {P['state']} proposal with no open round — "
                        "an approve or reject decides a ROUND. Submit it first "
                        "(POST /api/proposals/<id>/submit), or leave a comment.",
                        409)
                try:
                    return ops.decide_approval(
                        conn, rnd["id"],
                        decision="approved" if req.verdict == "approve"
                        else "rejected", actor=actor, note=req.body,
                        identity_via=_identity(request),
                        head_sha=req.head_sha)
                except ops.OpsError as exc:
                    if exc.code == 403 and user.get("kind") == "bot":
                        r = proposals.add_review(
                            conn, pid, actor=actor, verdict=req.verdict,
                            body=req.body, head_sha=req.head_sha,
                            identity=_identity(request))
                        return {"ok": True, "advisory": True, "review": r,
                                "why": str(exc)}
                    raise
            out = _guard(go)
        return A._decided(out)

    # ---- threads -----------------------------------------------------------
    @app.get("/api/threads/anchors")
    def api_thread_anchors():
        """The ANCHORS registry: what a thread can point at, and when it goes
        outdated."""
        return {"anchors": threads.anchors(), "subjects": list(threads.SUBJECTS),
                "reserved": list(threads.RESERVED_SUBJECTS)}

    @app.get("/api/threads")
    def api_threads(proposal: str = "", release: str = "", change: str = "",
                    state: str = ""):
        """Threads on a scope, each with its anchor resolved (path + line now,
        the key's value now, the mesh point in model space + the camera) and
        `outdated` — computed from git against the subject's head, never
        stored. `state` = open | resolved | outdated."""
        sc = _scope(proposal, release, change)
        with db.connect() as conn:
            rows = _guard(lambda: threads.listing(conn, sc, state=state))
            return {"threads": rows,
                    "counts": {f"{k[0]}:{k[1]}": v for k, v in
                               threads.counts(conn, sc).items()}}

    @app.get("/api/threads/{tid}")
    def api_thread(tid: int):
        with db.connect() as conn:
            return _guard(lambda: threads.get(conn, tid))

    @app.post("/api/threads")
    def api_thread_open(req: ThreadReq, request: Request):
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            out = _guard(lambda: threads.open_thread(
                conn, subject_type=req.subject_type, subject_id=req.subject_id,
                body=req.body, actor=actor, anchor=req.anchor, title=req.title,
                blocking=req.blocking, suggestion=req.suggestion))
        return A._decided(out)

    @app.post("/api/threads/{tid}/reply")
    def api_thread_reply(tid: int, req: ReplyReq, request: Request):
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            return _guard(lambda: threads.reply(conn, tid, body=req.body,
                                                actor=actor))

    @app.post("/api/threads/{tid}/resolve")
    def api_thread_resolve(tid: int, req: ResolveReq, request: Request):
        """Resolve (the raiser or an approver), or CLAIM it addressed with the
        commit (anybody else). Shipping over a BLOCKING thread is a decision of
        record, so it needs a server-established identity."""
        A = _api()
        actor = A._require_verified_actor(request, req.actor)
        with db.connect() as conn:
            out = _guard(lambda: threads.resolve(
                conn, tid, actor=actor, reason=req.reason,
                addressed_by=req.addressed_by))
        return A._decided(out)

    @app.post("/api/threads/{tid}/reopen")
    def api_thread_reopen(tid: int, req: ActorReq, request: Request):
        A = _api()
        actor = A._require_verified_actor(request, req.actor)
        with db.connect() as conn:
            return _guard(lambda: threads.reopen(conn, tid, actor=actor,
                                                 body=req.body))

    @app.post("/api/threads/{tid}/apply")
    def api_thread_apply(tid: int, req: ActorReq, request: Request):
        """Apply a suggested change: ONE commit on the draft through the git
        write path, then the thread is resolved with that commit."""
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            out = _guard(lambda: threads.apply_suggestion(conn, tid, actor=actor))
        return A._decided(out)

    # ---- the timeline ------------------------------------------------------
    @app.get("/api/timeline")
    def api_timeline(proposal: str = "", release: str = "", change: str = ""):
        """§2.8 — commits, pushes, checks, threads, reviews, dismissals and
        decisions, interleaved, over a scope."""
        if change and not proposal and not release:
            from . import changes
            with db.connect() as conn:
                return {"timeline": _guard(lambda: changes.timeline(
                    conn, changes.parse_id(change)))}
        sc = _scope(proposal, release, change)
        with db.connect() as conn:
            return {"timeline": _guard(lambda: proposals.timeline(conn, sc))}
