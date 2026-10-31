"""`/api/checks` and `/api/repo/{n}/checks` — the read surface for §2.6.

A separate module mounted by one hook in `plm/api.py`, for the same two reasons
`plm/gitapi.py` is: the diff against master stays an import plus a call, and the
whole feature is a thing somebody can delete in one commit.

Four routes and nothing else:

    GET  /api/checks                         the registry + the policy table
    GET  /api/repo/{n}/checks?sha=|ref=      the rows for one commit + a rollup
    POST /api/repo/{n}/checks/run            re-run, all or by name
    GET  /api/repo/{n}/checks/{name}/images/{i}.png?sha=
                                             a row's `images[i]` (§2.10)

`{n}` is an item number or a pull request (`CHG-380`): a check's SUBJECT. A
pull request's rows are keyed by its head-set key instead of a commit.

⭐ CHECKS ARE NOT POSTED, THEY ARE MEASURED. There is deliberately no route that
accepts a *result*. Everything here re-runs the judgement from the commit, so a
green check cannot be asserted by whoever wants to be released — it can only be
earned by bytes that measure correctly. (§2.6 anticipates an integration posting
results; the `source` column is ready for it, and that door should arrive with
an authenticated integration identity behind it, not before.)
"""
from __future__ import annotations


from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import checks, gitrepo


class RunReq(BaseModel):
    sha: str = ""
    ref: str = ""
    names: list[str] | None = None


def _resolve(subj: "checks.Subject", sha: str, ref: str) -> tuple[str, str]:
    """(sha, the ref it came from) — `Subject.resolve`, the one answer the
    MCP's get_checks gives too: an item's commit, or a pull request's
    (`CHG-n`) current head-set key (§2.10)."""
    from . import changes
    try:
        return subj.resolve(sha, ref)
    except changes.ChangeError as exc:
        raise gitrepo.RepoError(str(exc), exc.code) from None


def install(app) -> None:
    """Mount the checks routes on an existing FastAPI app (the gitapi pattern).

    Also brings the store and the runner up: the table is created from
    `plm/checks.py` rather than from `plm/db.py`'s schema block, and any row
    left `running` by a crash goes back on the queue here — the one moment in
    the process's life when "nothing of mine can still be running" is true.
    """
    try:
        checks.ensure_schema()
        n = checks.requeue_stale()
        if n:
            print(f"[checks] re-queued {n} commit(s) left in flight by a restart")
        # PLM_GIT_MODE (stage 9): checks measure COMMITS, so with git off on
        # every project there is nothing to run; `enqueue()` still starts the
        # thread lazily the first time a project turns git on.
        from . import gitmode
        if gitmode.any_serves():
            checks.start_runner()
    except Exception as exc:                                # pragma: no cover
        print(f"[checks] store/runner not started: {exc}")

    @app.get("/api/checks")
    def api_checks_registry():
        """The registry as data — what each check is, what it applies to, what
        it costs, and which ones policy requires per target maturity. The docs
        and the MCP read this instead of keeping a second copy."""
        return checks.describe()

    @app.get("/api/repo/{number}/checks")
    def api_repo_checks(number: str, sha: str = "", ref: str = "",
                        target: str = "", wait: float = 0.0):
        """Every check for one commit, plus the rollup.

        `target` is the maturity being asked for (a proposal's `target_state`).
        It changes NO row — it decides which checks are *required*, so the same
        board reads amber on a prototype and blocking on a release, which is
        §2.3's table and not a second opinion about the part.

        `wait` (seconds, capped) blocks until nothing is queued or running. A
        `minutes`-cost check makes polling the normal case; an agent that just
        pushed should be able to say "tell me when you know" in one call.
        """
        subj = checks.subject(number)
        number = subj.number
        try:
            commit, used_ref = _resolve(subj, sha, ref)
        except gitrepo.RepoError as exc:
            raise HTTPException(exc.code, str(exc)) from None
        if wait:
            rows = checks.wait_settled(number, commit, wait)
        else:
            with _conn() as conn:
                rows = checks.read(conn, number, commit)
        applicable = checks.applies_to(number, commit)
        return {
            "number": number, "sha": commit, "ref": used_ref,
            "applies": applicable,
            "checks": rows,
            "rollup": checks.rollup(rows, target_maturity=target,
                                    applicable=applicable),
            "pending": checks.pending(number, commit),
        }

    @app.post("/api/repo/{number}/checks/run")
    def api_repo_checks_run(number: str, req: RunReq):
        """Re-run. Answers with what it queued, not with a result — the runner
        is one thread and a `minutes` check is a minute; read the rows back."""
        subj = checks.subject(number)
        number = subj.number
        try:
            commit, used_ref = _resolve(subj, req.sha, req.ref)
        except gitrepo.RepoError as exc:
            raise HTTPException(exc.code, str(exc)) from None
        applicable = checks.applies_to(number, commit)
        want = [n for n in (req.names or applicable) if n in applicable]
        unknown = [n for n in (req.names or []) if n not in checks.CHECKS]
        if unknown:
            raise HTTPException(
                400, f"no such check: {', '.join(unknown)} — "
                     f"GET /api/checks lists them")
        if not want:
            raise HTTPException(
                400, f"none of those checks apply to {commit[:10]}: this "
                     f"commit supports {', '.join(applicable) or '(none)'}")
        if not checks.ENABLED:
            raise HTTPException(
                503, "the check runner is off on this instance (PLM_CHECKS=0). "
                     "Recorded checks are still readable.")
        checks.enqueue(number, commit, names=want)
        return {"number": number, "sha": commit, "ref": used_ref,
                "queued": want}

    _image_route(app)


def _image_route(app) -> None:
    @app.get("/api/repo/{number}/checks/{name}/images/{index}.png")
    def api_check_image(number: str, name: str, index: int, sha: str = ""):
        """One of a check row's `images` — derived PNGs in the cache, never in
        the DB. Missing (pending, or the cache was cleared) → 404 with
        `Retry-After`; a FINISHED row of the CURRENT key is re-rendered once
        (the row is claimed `queued` here, so N polls of N images ask once). A
        superseded pull-request key (a member moved) is 410: never rendered."""
        subj = checks.subject(number)
        try:
            key = sha or _resolve(subj, "", "")[0]
            path = checks.image_dir(subj.number, key, name) / f"{int(index)}.png"
        except (gitrepo.RepoError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from None
        if path.is_file():
            return FileResponse(path, media_type="image/png", headers={
                "Cache-Control": "public, max-age=86400, immutable"})
        if sha and subj.is_change and sha != _resolve(subj, "", "")[0]:
            raise HTTPException(410, f"{subj.number} has moved since {sha[:10]} "
                                     "— its current images are on the board")
        with _conn() as conn:
            row = next((r for r in checks.read(conn, subj.number, key)
                        if r["name"] == name), None)
            busy = row is None or row["status"] in ("queued", "running")
            if not busy and index in checks.image_indices(row) and checks.ENABLED:
                busy = True
                claimed = conn.execute(
                    "UPDATE checks SET status='queued', started_at=NULL WHERE "
                    "number=? AND sha=? AND name=? AND status NOT IN "
                    "('queued','running')", (subj.number, key, name)).rowcount
                conn.commit()
                if claimed:
                    checks.enqueue(subj.number, key, names=[name])
        if not busy:
            raise HTTPException(404, f"{name} on {subj.number} has no image {index}")
        return JSONResponse({"detail": f"{name} on {subj.number} is rendering",
                             "generating": True}, status_code=404,
                            headers={"Retry-After": "10"})


def _conn():
    from . import db
    return db.connect()
