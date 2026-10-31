"""HTTP for CHANGES and RELEASE SETS (stage 14).

Mounted by ONE call in `plm/api.py` (`changesapi.install(app)`) — the gitapi /
checksapi / threadsapi pattern. The verbs are `plm.ops` (`decide_change`,
`submit_change`) and `plm/changes.py` / `plm/releasesets.py`; this file is
routing, identity and response shape only. Every write that is a DECISION
(decide a change, decide a release set, confirm a disposition) takes the
server-established identity (`_require_verified_actor`), like every other
decision door.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import changes, db, identity, ops, pending, releasesets


def _api():
    from . import api
    return api


def _guard(fn):
    try:
        return fn()
    except changes.ChangeError as exc:
        raise HTTPException(exc.code, str(exc)) from None
    except releasesets.SetError as exc:
        raise HTTPException(exc.code, str(exc)) from None
    except ops.OpsError as exc:
        raise HTTPException(exc.code, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


def _viewer(request: Request, claimed: str = "") -> str:
    """WHO IS LOOKING, for a read that answers "may you decide this?". The
    identity the server established; under `PLM_AUTH_MODE=trusted-network`
    (which is what that mode means) the name the page claims. Never used to
    authorise anything — the decision itself asks again."""
    actor = _api()._verified_actor(request)
    if actor:
        return actor
    return "" if identity.auth_required() else (claimed or "").strip()


def _identity(request: Request) -> str:
    return (getattr(request.state, "plm_identity", None) or {}).get(
        "via", "unverified")


class ChangeReq(BaseModel):
    title: str
    body: str = ""
    project: str = ""
    actor: str = ""


class MemberReq(BaseModel):
    proposal: int = 0
    number: str = ""
    ref: str = Field(default="", description="draft branch, e.g. draft/v0.2.0")
    actor: str = ""


class SubmitChangeReq(BaseModel):
    actor: str = ""
    note: str = ""
    target_state: str = ""
    explode_waiver: str = ""
    #: seconds to wait for the members' checks before answering (stage 16);
    #: None = the instance default (PLM_SUBMIT_WAIT_S), capped at 300
    wait: float | None = None


class DecideChangeReq(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    actor: str = ""
    note: str = ""
    heads: dict = Field(default_factory=dict,
                        description="{proposal id: sha} as you reviewed them — "
                                    "a member that moved since is refused")


class ChangeReviewReq(BaseModel):
    verdict: str = Field(pattern="^(approve|reject|comment)$")
    body: str = ""
    heads: dict = Field(default_factory=dict)
    actor: str = ""


class DispositionReq(BaseModel):
    parent: str
    disposition: str = Field(pattern="^(bump|use_as_is|no_impact|unchanged_geometry|historical)$")
    note: str = ""
    actor: str = ""


class ActorReq(BaseModel):
    actor: str = ""


class SetReq(BaseModel):
    number: str
    version: str = ""
    notes: str = ""
    actor: str = ""


class AttestReq(BaseModel):
    line: str
    note: str = ""
    releases: list[int] = Field(default_factory=list)
    by_reference: bool = False
    actor: str = ""


class DecideSetReq(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = ""
    attest_rest: bool = True
    actor: str = ""


def _member_pid(conn, req: MemberReq) -> int:
    if req.proposal:
        return req.proposal
    item = db.resolve_item(conn, req.number)
    if not item:
        raise changes.ChangeError(f"unknown item: {req.number}", 404)
    ref = req.ref if req.ref.startswith("refs/heads/") else \
        f"refs/heads/{req.ref}" if req.ref else ""
    q = ("SELECT id FROM proposals WHERE item_id=? AND state NOT IN "
         "('approved','merged','withdrawn')" + (" AND head_ref=?" if ref else "")
         + " ORDER BY id DESC LIMIT 1")
    r = conn.execute(q, (item["id"], *([ref] if ref else []))).fetchone()
    if not r:
        raise changes.ChangeError(
            f"{req.number} has no open proposal{' on ' + req.ref if ref else ''}"
            " — push a draft branch first (a draft branch IS a proposal)", 404)
    return r[0]


def install(app) -> None:
    #: the pull request's routes, mounted TWICE: `/api/changes/…` and
    #: `/api/pulls/…` (its GitHub name) are one set of handlers, not a rewrite
    pulls = APIRouter()

    @pulls.get("")
    def api_changes(request: Request, state: str = "", project: str = "",
                    waiting: bool = False, limit: int = 100, actor: str = ""):
        """Pull requests, newest first — `pending.change_entry` rows (each
        with `open` and the viewer's `can_decide`). `state` is a csv of raw
        states (draft, open, …); `waiting=1` = what is in front of a human."""
        states = ["open"] if waiting else [x for x in state.split(",") if x]
        viewer = _viewer(request, actor)
        with db.connect() as conn:
            return _guard(lambda: {"changes": changes.listing(
                conn, states=states, project=project, limit=min(limit, 500),
                viewer=viewer)})

    @pulls.post("")
    def api_change_open(req: ChangeReq, request: Request):
        """Open a change (draft). Then push drafts into it with
        `git push -o change=CHG-n`, or POST …/members."""
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            c = _guard(lambda: changes.create(conn, title=req.title, body=req.body,
                                              project=req.project, actor=actor,
                                              origin="api"))
            changes.event(conn, "change.opened", c["id"], actor=actor,
                          item_id=None, data={"title": c["title"]})
            return changes.brief(conn, c)

    @pulls.get("/{cid}")
    def api_change(cid: str, request: Request, actor: str = ""):
        """The change page's payload. `viewer` (§2.9, the PR page): who is
        looking, whether they may decide, and who the policy waits on — so the
        page shows Approve only to somebody the decision would admit."""
        viewer = _viewer(request, actor)

        def go():
            v = changes.view(conn, cid)
            e = pending.change_entry(conn, changes.row(conn, cid), viewer)
            v["viewer"] = {"actor": viewer, "can_decide": e["can_decide"],
                           "why_not": e["why_not"], "waiting_on": e["waiting_on"]}
            return v
        with db.connect() as conn:
            return _guard(go)

    @pulls.post("/{cid}/members")
    def api_change_member(cid: str, req: MemberReq, request: Request):
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            return _guard(lambda: changes.add_member(
                conn, cid, _member_pid(conn, req), actor=actor))

    @pulls.post("/{cid}/submit")
    def api_change_submit(cid: str, req: SubmitChangeReq, request: Request):
        """Submit every member not yet in review; the human is asked ONCE
        (`review.requested`, subject: change)."""
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        # stage 16: wait (bounded) for the members' checks OUTSIDE the write
        # transaction, so the answer reports finished checks, not a race
        changes.settle(cid, wait=req.wait)
        with db.connect() as conn:
            out = _guard(lambda: ops.submit_change(
                conn, cid, actor=actor, note=req.note,
                target_state=req.target_state,
                explode_waiver=req.explode_waiver))
        return A._decided(out)

    @pulls.get("/{cid}/preflight")
    def api_change_preflight(cid: str, actor: str = ""):
        """What the decision would say, without deciding: every problem, per
        member, with its fix. Writes nothing."""
        with db.connect() as conn:
            def go():
                pf = changes.preflight(conn, cid, actor=actor or "")
                pf.pop("plan", None)
                return pf
            return _guard(go)

    @pulls.post("/{cid}/decide")
    def api_change_decide(cid: str, req: DecideChangeReq, request: Request):
        """THE decision. Pre-flights every member, then merges in dependency
        order; refusals are the whole teachable message — render verbatim."""
        A = _api()
        actor = A._require_verified_actor(request, req.actor)
        with db.connect() as conn:
            out = _guard(lambda: ops.decide_change(
                conn, cid, decision=req.decision, actor=actor, note=req.note,
                heads=req.heads, identity_via=_identity(request)))
        return A._decided(out)

    @pulls.post("/{cid}/reviews")
    def api_change_review(cid: str, req: ChangeReviewReq, request: Request):
        """`comment` (anyone) or `approve`/`reject` — which DECIDES when the
        reviewer may decide, and is recorded as an ADVISORY signoff (counts=0,
        dismissed by the next push like any other) when policy does not admit
        them (an agent, or a human without the approver role)."""
        A = _api()
        with db.connect() as conn:
            if req.verdict == "comment":
                actor = A._actor_for_write(request, req.actor)
                return {"ok": True, "review": _guard(lambda: changes.add_review(
                    conn, changes.parse_id(cid), actor=actor, verdict="comment",
                    body=req.body, heads=req.heads or None,
                    identity=_identity(request)))}
            actor = A._require_verified_actor(request, req.actor)

            def go():
                try:
                    return ops.decide_change(
                        conn, cid, decision="approved" if req.verdict == "approve"
                        else "rejected", actor=actor, note=req.body,
                        heads=req.heads, identity_via=_identity(request))
                except ops.OpsError as exc:
                    if exc.code != 403:
                        raise
                    r = changes.add_review(
                        conn, changes.parse_id(cid), actor=actor,
                        verdict=req.verdict, body=req.body,
                        heads=req.heads or None, identity=_identity(request))
                    return {"ok": True, "advisory": True, "review": r,
                            "why": str(exc)}
            out = _guard(go)
        return A._decided(out)

    @pulls.post("/{cid}/dispositions")
    def api_change_disposition(cid: str, req: DispositionReq, request: Request):
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            return _guard(lambda: changes.set_disposition(
                conn, cid, req.parent, req.disposition, note=req.note,
                actor=actor))

    @pulls.post("/{cid}/dispositions/draft")
    def api_change_draft_dispositions(cid: str, req: ActorReq, request: Request,
                                      render: bool = False):
        """The agent's legwork: draft a disposition (with the facts as the
        reason) for every affected parent that has none. A human confirms.
        `?render=1` (stage 19): render each source-drift item's own selector
        against both copies; identical drafts `unchanged_geometry`."""
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            return {"drafted": _guard(lambda: changes.draft_dispositions(
                conn, cid, actor=actor, render=render))}

    app.include_router(pulls, prefix="/api/changes")
    app.include_router(pulls, prefix="/api/pulls", include_in_schema=False)

    @app.get("/api/waiting")
    def api_waiting(request: Request, actor: str = ""):
        """The dashboard's "Waiting on you" and the masthead bell: open changes
        and release sets, newest first — one list, two kinds, each one click
        from its page. Each row says whether THIS viewer may decide it
        (`can_decide`) and who the policy waits on (`waiting_on`)."""
        viewer = _viewer(request, actor)
        with db.connect() as conn:
            ch = changes.listing(conn, states=["open"], limit=50, viewer=viewer)
            rs = releasesets.listing(conn, states=list(releasesets.OPEN),
                                     limit=50)
            rows = list(ch)
            rs_ok = pending.release_set_can_decide(conn, viewer)
            for s in rs:
                rows.append({"kind": "release_set", "at": s["requested_at"], **s,
                             "can_decide": rs_ok,
                             "waiting_on": pending.approvers(
                                 conn, {"approver_kinds": ["human"]})})
            rows.sort(key=lambda r: r["at"] or 0, reverse=True)
            return {"waiting": rows, "changes": len(ch), "release_sets": len(rs),
                    "viewer": viewer,
                    "yours": sum(1 for r in rows if r["can_decide"])}

    # ---- what is pending on a repo / a project (the banners) ---------------
    @app.get("/api/items/{number}/pending")
    def api_item_pending(number: str, request: Request, actor: str = "",
                         state: str = "open"):
        """GitHub's repo-home banners for <number>: open pull requests one of
        its draft branches is in (with `can_decide` for the viewer), and draft
        branches pushed into none. `state=all` (or `closed`) is the item's
        Pull requests tab — merged/closed too; every row says `open`.
        Read-only (plm/pending.py)."""
        viewer = _viewer(request, actor)
        with db.connect() as conn:
            return _guard(lambda: pending.for_item(conn, number, viewer, state))

    @app.get("/api/projects/{project}/pending")
    def api_project_pending(project: str, request: Request, actor: str = "",
                            state: str = "open"):
        """The same rows for every item of a project, one row per pull
        request."""
        viewer = _viewer(request, actor)
        with db.connect() as conn:
            return _guard(lambda: pending.for_project(conn, project, viewer,
                                                      state))

    # ---- release sets --------------------------------------------------------
    @app.get("/api/release-sets")
    def api_release_sets(pending: bool = True, limit: int = 100):
        with db.connect() as conn:
            return {"release_sets": releasesets.listing(
                conn, states=list(releasesets.OPEN) if pending else None,
                limit=min(limit, 500))}

    @app.post("/api/release-sets")
    def api_release_set_request(req: SetReq, request: Request):
        """Request the release of `number vX` AND its unreleased closure as ONE
        set — members bottom-up, coupon evidence attested by reference,
        blockers (pins at untagged commits) named with the change to merge."""
        A = _api()
        actor = A._actor_for_write(request, req.actor)
        with db.connect() as conn:
            out = _guard(lambda: releasesets.request(
                conn, req.number, version=req.version, actor=actor,
                notes=req.notes))
        return A._decided(out)

    @app.get("/api/release-sets/{sid}")
    def api_release_set(sid: str):
        with db.connect() as conn:
            return _guard(lambda: releasesets.view(conn, sid))

    @app.post("/api/release-sets/{sid}/attest")
    def api_release_set_attest(sid: str, req: AttestReq, request: Request):
        A = _api()
        actor = A._require_verified_actor(request, req.actor)
        with db.connect() as conn:
            return _guard(lambda: releasesets.attest(
                conn, sid, line=req.line, note=req.note, actor=actor,
                releases=req.releases or None, identity_via=_identity(request),
                by_reference=req.by_reference))

    @app.post("/api/release-sets/{sid}/decide")
    def api_release_set_decide(sid: str, req: DecideSetReq, request: Request):
        A = _api()
        actor = A._require_verified_actor(request, req.actor)
        with db.connect() as conn:
            out = _guard(lambda: releasesets.decide(
                conn, sid, decision=req.decision, actor=actor, note=req.note,
                identity_via=_identity(request), attest_rest=req.attest_rest))
        return A._decided(out)

    @app.get("/api/release-sets/{sid}/manifest")
    def api_release_set_manifest(sid: str):
        """The STORED manifest bytes — never re-serialised (like evidence)."""
        with db.connect() as conn:
            s = _guard(lambda: releasesets.row(conn, sid))
        if not s["manifest_sha256"]:
            raise HTTPException(404, f"release set {s['id']} has no manifest — "
                                     "it is frozen when the set is approved")
        from pathlib import Path
        raw = Path(s["manifest_key"]).read_bytes()
        return Response(raw, media_type="application/json",
                        headers={"X-Plm-Sha256": s["manifest_sha256"]})
