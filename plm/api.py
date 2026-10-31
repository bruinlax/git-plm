"""plm HTTP API — the part registry.

Every read is served from the SQLite index; the filesystem is only touched to
stream a blob or compute a diff. No printer, no slicer: those are downstream
consumers, not the core.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import (attributes, config, context, db, fab, gc, gitvalidate, identity,
               ops, policy, reclassify, relations, roles, search, snippets,
               staging, storage, tags, thumbs, vault, webroutes, websec)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

#: A preview is something a person is LOOKING at, so it is bounded by what a
#: browser will render rather than by what the store can hand over.
PREVIEW_MAX = int(os.environ.get("PLM_PREVIEW_MAX", str(16 * 1024 * 1024)))

app = FastAPI(title="plm", version="0.1.0",
              description="Database-driven part registry with git underneath.")

# Resolve the caller's verified identity once per request, override any
# client-supplied `actor` with it, and bounce un-authenticated humans to login.
# See plm/identity.py for why this is middleware and not twelve call sites.
identity.install(app)

# ⛔ M11 — nosniff, Referrer-Policy, X-Frame-Options and a CSP on every
# response, with a sandboxed policy on the blob and preview routes. There was no
# middleware here at all and no `<meta http-equiv>` in web/index.html, so
# `application/octet-stream` and `text/plain` blobs were exposed to MIME
# sniffing and there was no second line of defence behind H2 / M6 / M10.
# ⚠️ ORDER MATTERS: Starlette runs middleware outermost-first in REVERSE
# registration order, so registering this after IdentityMiddleware puts it
# OUTSIDE — which is what makes a 401 from the identity layer carry the headers
# too.
websec.install(app)

# -- GIT-FIRST POC HOOK (the design docs) ----------------------------------------
# The whole `/api/repo/*` read surface — refs, tree, blob, manifest, commits,
# compare — lives in plm/gitapi.py and is mounted by this ONE call, so the diff
# against master is two lines and a rollback is deleting them. Nothing else in
# this file knows the generated repos exist.
from . import gitapi  # noqa: E402  (after `app`, by construction)

gitapi.install(app)

# -- STAGE 6 HOOK: checks (PLM_REVIEW_MODEL §2.6) -----------------------------
from . import checksapi  # noqa: E402  (same pattern, same reason, as gitapi)

checksapi.install(app)

# -- STAGE 13 HOOK: the review model — proposals, reviews, threads ------------
from . import threadsapi  # noqa: E402  (same pattern as gitapi / checksapi)

threadsapi.install(app)

# -- STAGE 14 HOOK: changes (the project-level PR) and release sets -----------
from . import changesapi  # noqa: E402  (same pattern as gitapi / checksapi)

changesapi.install(app)


@app.on_event("startup")
def _startup() -> None:
    db.init()

    # Warm the search index off the request path: the FTS table persists in
    # the DB, so a rebuild is only needed when it's empty (fresh DB) — and
    # reading vault markdown can be slow on the synced volume, so it must
    # never block serving.
    def warm() -> None:
        with db.connect() as conn:
            search.ensure(conn)
            if not conn.execute("SELECT COUNT(*) FROM search").fetchone()[0]:
                search.reindex(conn)

    threading.Thread(target=warm, daemon=True).start()

    # Everything that expires by itself — disposable 3D snippets and staged
    # uploads that were never checked in — gets one sweep now and then a
    # periodic one, so nothing outlives its TTL just because it fell due while
    # the process was restarting (plm/gc.py).
    gc.start_all()

    # Stage 15: the addresses plm hands out (clone_url, change URLs) must answer
    # from where agents run. Probed in the background; a dead base is shouted in
    # the log and reported in GET /api/gitmode. No-op where git is off.
    from . import addresses
    addresses.start()


def _fail(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail=msg)


# A refused role is the CALLER's mistake, not a server fault, and it can surface
# from any write that carries files — checkin, upload, raw PUT. One
# handler covers every one of them, including the ones not written yet; catching
# it per-endpoint is how the raw-upload path came to return a 500 with an
# unparseable body while checkin returned a clean 400.
@app.exception_handler(roles.RoleError)
async def _role_error(request: Request, exc: roles.RoleError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(gitvalidate.Refused)
def _refused(request: Request, exc: gitvalidate.Refused):
    """A push-validator refusal, reached through the API instead of a push.

    `gitwrite.commit` runs the SAME checks a `git push` faces — that is the
    point of one code path — so an API write can hit one. It is a 409 with the
    whole teachable paragraph, not a 500: the text names what is wrong and the
    fix, and the UI renders it verbatim (`refusalBox`).
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(tags.TagError)
async def _tag_error(request: Request, exc: tags.TagError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# -- identity: who is really calling ------------------------------------------

def _verified_actor(request: Request) -> str | None:
    """The username behind a verified OIDC identity, or None."""
    return getattr(request.state, "plm_actor", None)


def _require_verified_actor(request: Request, claimed: str) -> str:
    """For decisions of record: an identity the SERVER established, or nothing.

    The release gate is the one place a client-supplied name is not good enough.
    A bot may submit, revise and upload all day; deciding an approval is a
    statement about *who accepted the risk*, so it needs an identity the server
    established itself — an OIDC session or a machine token from
    `plm/tokens.py` (stage 7: the token plane counts, because the server is the
    one that resolved it).

    Under `PLM_AUTH_MODE=trusted-network` this still falls back to the claimed
    name, which is what that mode MEANS. Under `required` — the default — the
    middleware has already refused an unauthenticated write, so reaching the
    fallback at all means OIDC-specific enforcement
    (`PLM_REQUIRE_OIDC_APPROVALS`) is the thing asking.
    """
    actor = _verified_actor(request)
    if actor:
        return actor
    if getattr(request.state, "plm_service", ""):
        # The git container, holding the index token. It authenticated the
        # pusher itself (plm_git.push.identify) and is naming them; see
        # identity.service_identity for why that is the one carve-out.
        return claimed
    if identity.auth_required() or identity.require_oidc_approvals():
        _fail("this action is a decision of record and needs an identity this "
              "server established.\n"
              "  a human: sign in at /auth/login\n"
              "  an agent: present its plm token — "
              "Authorization: Bearer $PLM_TOKEN\n"
              "A client-supplied 'actor' is not sufficient here.", 403)
    return claimed


def _actor_for_write(request: Request, claimed: str) -> str:
    """The actor an ordinary write is attributed to.

    H4's half of the fix. `submit_for_release` can AUTO-MERGE and write a
    release tag for CONCEPT/DESIGN/PROTOTYPE (plm/policy.py — Geoff's call, kept),
    which makes a submit a release decision at three of four maturities. With C2
    fixed there is always a verified identity behind a write under
    `PLM_AUTH_MODE=required`, so the auto-merge is attributable; this is the
    function that says so out loud rather than leaving it implied by the
    middleware.
    """
    actor = _verified_actor(request)
    if actor:
        return actor
    if getattr(request.state, "plm_service", ""):
        return claimed
    if identity.auth_required():
        _fail("this write needs an identity this server established "
              "(Authorization: Bearer $PLM_TOKEN, or a session from "
              "/auth/login).", 401)
    return claimed


# -- auth routes (the human plane) --------------------------------------------

@app.get("/auth/login")
def auth_login(next: str = "/"):
    if not identity.configured():
        _fail("OIDC is not configured on this instance (OIDC_CLIENT_ID / "
              "OIDC_CLIENT_SECRET are unset).", 503)
    # Same-origin relative only, fragment allowed — identity.safe_next is the
    # one guard (begin_login applies it again; so does the callback's read).
    next = identity.safe_next(next)
    with db.connect() as conn:
        try:
            url = identity.begin_login(conn, next)
        except identity.IdentityError as exc:
            _fail(str(exc), 503)
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
def auth_callback(code: str = "", state: str = "", error: str = "",
                  error_description: str = ""):
    if error:
        return HTMLResponse(
            f"<h1>Sign-in failed</h1><p><code>{error}</code></p>"
            f"<p>{error_description}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h1>Sign-in failed</h1><p>Missing code or state.</p>",
                            status_code=400)
    with db.connect() as conn:
        try:
            session_id, redirect_to = identity.complete_login(conn, code, state)
        except identity.IdentityError as exc:
            return HTMLResponse(f"<h1>Sign-in failed</h1><p>{exc}</p>", status_code=403)
        row = identity.session(conn, session_id)
        db.add_event(conn, "identity.login", actor=row["sub"],
                     data={"email": row["email"], "via": "oidc"})
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(identity.SESSION_COOKIE, session_id, httponly=True,
                        secure=True, samesite="lax", max_age=identity.SESSION_TTL,
                        path="/")
    return response


@app.get("/auth/logout")
def auth_logout(request: Request):
    with db.connect() as conn:
        identity.end_session(conn, request.cookies.get(identity.SESSION_COOKIE, ""))
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(identity.SESSION_COOKIE, path="/")
    return response


@app.get("/auth/whoami")
def auth_whoami(request: Request) -> dict:
    """Who the server thinks you are — and, crucially, whether it is verified.

    Stage 19 (handoff finding 2): a bearer token says `via: "token"` and
    `token: {actor, known}`. A token that maps to an actor with NO users row
    (CHG-379's session: a `codex` token, no `codex` user) used to come back
    `verified: false, actor: null` with nothing to say why — indistinguishable
    from sending no token at all. It now names the actor and the fix."""
    ident = getattr(request.state, "plm_identity", None)
    actor = _verified_actor(request)
    via = (ident or {}).get("via")
    out = {"verified": bool(actor), "actor": actor,
           "via": "token" if via == "machine-token" else via,
           "oidc": identity.describe()}
    if not actor:
        # The name the UI acts as when nobody is signed in (trusted-network):
        # the instance's seeded admin, never a name baked into the client.
        out["fallback_actor"] = (os.environ.get("PLM_ADMIN") or "admin").strip()
    unknown = getattr(request.state, "plm_token_unknown", "")
    if via == "machine-token":
        out["token"] = {"actor": actor, "known": True}
    elif unknown:
        out["via"] = "token"
        out["token"] = {
            "actor": unknown, "known": False,
            "why": f"the token maps to the plm actor {unknown!r}, and this "
                   "instance has no such user — nothing this token sends is "
                   "attributed to it, and writes carrying it are refused",
            "fix": f"an operator adds the user: INSERT INTO users (username, "
                   f"kind, roles, display, created_at) VALUES ('{unknown}', "
                   "'bot', 'editor', '…', strftime('%s','now'))"}
    if actor:
        with db.connect() as conn:
            user = db.get_user(conn, actor)
        out["user"] = {"username": user["username"], "kind": user["kind"],
                       "roles": user["roles"], "display": user["display"],
                       "email": user["email"], "sub": user["sub"]} if user else None
        out["can_approve"] = bool(user and "approver" in
                                  [r.strip() for r in user["roles"].split(",") if r])
    return out


# Deliver an event to every enabled integration subscribed to it — through
# `actions.send()`, host-only event rows (C3/M4). The implementation moved to
# `plm.actions.dispatch` in stage 13 so the MCP process (an agent resubmitting,
# resolving the last thread) reaches the SAME delivery instead of dropping the
# event; this name stays because eleven call sites use it.
from .actions import dispatch as dispatch_event  # noqa: E402


# -- registry reads -----------------------------------------------------------

@app.get("/api/stats")
def api_stats() -> dict:
    with db.connect() as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "label": config.LABEL,
            "items": one("SELECT COUNT(*) FROM items"),
            "revisions": one("SELECT COUNT(*) FROM revisions"),
            "bom_lines": one("SELECT COUNT(*) FROM bom"),
            "files": one("SELECT COUNT(*) FROM files"),
            "projects": [r[0] for r in conn.execute(
                "SELECT DISTINCT project FROM items WHERE project!='' ORDER BY project")],
            "kinds": {r[0]: r[1] for r in conn.execute(
                "SELECT kind, COUNT(*) FROM items GROUP BY kind")},
            # contention is a measurement, not a rumour. These count
            # what THIS process has seen since it started — how often a write
            # had to wait for SQLite's single writer, and the worst wait.
            "sqlite": {**db.STATS,
                       "busy_timeout_ms": config.BUSY_TIMEOUT_MS,
                       "write_retries": config.WRITE_RETRIES},
            "checkouts": {
                "active": one("SELECT COUNT(*) FROM checkouts WHERE released_at IS NULL"),
                "stale": len(db.list_checkouts(conn, stale_only=True)),
                "stale_after_h": round(config.CHECKOUT_STALE_SECONDS / 3600, 1)},
            # how much of the estate's classification is MEASURED, how
            # much is still plm's own guess, and how much a human should look
            # at. Published because "nobody knows what these files are" was
            # invisible for the entire life of the registry — finding it meant
            # counting 837 rows by hand.
            "classification": {
                "no_media": one("SELECT COUNT(*) FROM files WHERE media=''"),
                "media_sniffed": one(
                    "SELECT COUNT(*) FROM files WHERE media_source='sniffed'"),
                "role_declared": one(
                    "SELECT COUNT(*) FROM files WHERE role_source='declared'"),
                "role_unconfirmed": one(
                    "SELECT COUNT(*) FROM files "
                    "WHERE role_source IN ('','extension')"),
                "legacy_role": one(
                    "SELECT COUNT(*) FROM files WHERE role='artifact'"),
            },
            "tags": tags.stats(conn),
        }


@app.get("/api/items")
def api_items(q: str = "", kind: str = "", project: str = "", state: str = "") -> list[dict]:
    with db.connect() as conn:
        return db.list_items(conn, q, kind, project, state)


@app.get("/api/items/{number}")
def api_item(number: str) -> dict:
    """One item, addressed by EITHER its number or its integer id — plm owns the
    ids as well as the numbers, so both are first-class keys (`/api/items/95` and
    `/api/items/GL-PRT-1001` are the same read). A number the item USED to have
    resolves too (items.former_numbers). Every revision carries its `id` and its
    merged `attributes`.

    Every row a UI draws carries `thumb`: the thumbnail URL, or null when the
    revision has no geometry at all (see plm/thumbs.py — presence is data, not
    something a client discovers by collecting 404s). Revisions, BOM lines,
    where-used parents and relations all use that one field name; a null there
    also means "no 3D to view"."""
    with db.connect() as conn:
        item = db.get_item(conn, number)
        if not item:
            _fail(f"unknown item: {number}", 404)
        # ONE presence memo for the whole payload: the same child revision shows
        # up on several BOM lines and in where-used, and is answered once.
        p = thumbs.Presence(conn)
        for rev in item["revisions"]:
            rev["thumb"] = p.url(item["number"], rev["version"], rev["id"],
                                 item["kind"])
        item["where_used"] = p.rows(db.where_used(conn, item["id"]),
                                    version="parent_version")
        # BOM for every revision that has one, keyed by revision id — the UI
        # shows the selected revision's BOM without another round-trip.
        item["bom"] = {}
        for rev in item["revisions"]:
            lines = db.get_bom(conn, rev["id"])
            if lines:
                item["bom"][rev["id"]] = p.bom(lines)
        item["context"] = context.for_item(conn, item["id"])
        # Typed relations, both directions, already labelled from this item's
        # point of view (plm/relations.py) — the Related items card renders
        # this list and knows nothing about any particular relation type.
        item["related"] = relations.for_item(conn, item["id"], presence=p)
        # Legacy shape, kept for existing callers: derived_from/forks are just
        # the derived_from rows of `related` seen from each side.
        item["derived_from"] = [dict(r) for r in conn.execute(
            """SELECT rel.relation, rel.target_version, rel.note, t.number, t.name
               FROM relations rel JOIN items t ON t.id=rel.target_item_id
               WHERE rel.item_id=?""", (item["id"],))]
        item["forks"] = [dict(r) for r in conn.execute(
            """SELECT rel.relation, rel.target_version, i.number, i.name
               FROM relations rel JOIN items i ON i.id=rel.item_id
               WHERE rel.target_item_id=? AND rel.relation='derived_from'""",
            (item["id"],))]
        # PLM_GIT_MODE for this item's project (stage 9, additive): the UI
        # lays the page out from it — no repo routes asked, so no 404s in the
        # console, when git-first is `off` here.
        from . import gitmode
        item["git_mode"] = gitmode.for_item(conn, item)
        return item


# -- revisions and attributes (id-addressed) ----------------------------------
# The revision is the core data type, so it gets its own key: an integer id.
# Every read that returns a revision includes that id, and these endpoints take
# it — number+version stays available for the callers (and humans) who think in
# numbers. plm/attributes.py owns the attribute surface itself.

class AttributesPatch(BaseModel):
    attributes: dict = Field(
        description="{name: value} or {name: {value, unit?, source?}}; "
                    "an empty value clears the attribute")
    source: str = Field(default="", description="where these came from, e.g. 'orca 2.4.2 slice'")
    actor: str = ""


@app.get("/api/attributes/registry")
def api_attribute_registry() -> list[dict]:
    """The well-known attribute names: label, default unit, value kind, and
    whether the fact is column-backed. A gloss for display, never a whitelist —
    arbitrary names are always accepted."""
    return attributes.registry()


@app.get("/api/revisions/{revision_id}")
def api_revision(revision_id: int) -> dict:
    """One revision by id: its own fields, the identity of its item, its files,
    its BOM and its merged attributes. `thumb` (here and on every BOM line) is
    the render's URL, or null when there is no geometry to render."""
    with db.connect() as conn:
        rev = db.get_revision(conn, revision_id)
        if not rev:
            _fail(f"no revision with id {revision_id}", 404)
        p = thumbs.Presence(conn)
        rev["thumb"] = p.url(rev["number"], rev["version"], rev["id"], rev["kind"])
        p.bom(rev["bom"])
        return rev


def _resolve_rev(conn, revision_id: int = 0, number: str = "", version: str = "") -> dict:
    """A revision from either key: an id, or number(+version). One helper, so
    every write endpoint accepts both without repeating itself."""
    if revision_id:
        rev = db.get_revision(conn, revision_id)
        if not rev:
            _fail(f"no revision with id {revision_id}", 404)
        return rev
    if not number:
        _fail("give revision_id, or number (+optional version)")
    rev = db.find_revision(conn, number, version)
    if not rev:
        _fail(f"no revision for {number} {version or '(latest)'}", 404)
    return rev


@app.patch("/api/revisions/{revision_id}/attributes")
def api_revision_attributes(revision_id: int, req: AttributesPatch) -> dict:
    """Set attributes on one revision — mass, material, bounding box, colour,
    print time, or anything else the instrument that measured it wants to
    record. material/mass_g are routed to their columns (one home per fact)."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id)
        try:
            # Git under the hood: when this revision's dataset is a commit, the
            # write becomes a commit on its draft branch and an index pass.
            # Same response either way — see ops.set_attributes.
            res = ops.set_attributes(conn, rev, req.attributes,
                                     source=req.source, actor=req.actor)
        except attributes.AttrError as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, rev["number"])
    return {"ok": True, "revision_id": rev["id"], "number": rev["number"],
            "version": rev["version"], **res}


@app.patch("/api/items/{number}/attributes")
def api_item_attributes(number: str, req: AttributesPatch, version: str = "") -> dict:
    """The number+version address for the same write (empty version = latest)."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, number=number, version=version)
        try:
            res = ops.set_attributes(conn, rev, req.attributes,
                                     source=req.source, actor=req.actor)
        except attributes.AttrError as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    return {"ok": True, "revision_id": rev["id"], "number": number,
            "version": rev["version"], **res}


# -- classification: what a file IS, and cross-cutting tags ---------
# plm/roles.py owns the file-role vocabulary (closed, validated) and the format
# sniffer; plm/tags.py owns the open tag vocabulary. These are addresses.
#
# The asymmetry is deliberate and is the answer to "encourage or enforce":
# ENFORCED on roles, because a purpose no surface acts on is a typo, and
# ENCOURAGED on tags, because the moment a new word needs a code change the
# person holding the fact writes a sentence instead — and prose is where these
# facts were already hiding.

class RolePut(BaseModel):
    role: str = Field(description="a key from GET /api/roles — what this file "
                                  "is FOR. Declaring it records role_source="
                                  "'declared', which is what distinguishes it "
                                  "from plm's own inference.")
    actor: str = ""


class TagPut(BaseModel):
    tags: list[str] = Field(description="tag names; normalized to "
                                        "lower-hyphen so one fact has one "
                                        "spelling")
    note: str = Field(default="", description="WHY — the sentence the tag "
                                              "makes findable")
    version: str = Field(default="", description="set to scope these tags to "
                                                 "ONE revision; omit for "
                                                 "item-level tags that apply "
                                                 "to every revision")
    actor: str = ""


@app.get("/api/roles")
def api_roles() -> list[dict]:
    """The file-role registry: what each role MEANS, what format class it
    expects, whether it is pinned to the vault, whether it can represent a
    revision's geometry, and whether it is release evidence.

    Unlike the attribute registry this one IS a gate — an unregistered role is
    refused at the write."""
    return roles.describe()


@app.get("/api/tags/registry")
def api_tag_registry() -> list[dict]:
    """The well-known tags: label, meaning, and whether the tag is a CAVEAT (an
    unresolved risk). A gloss for display and reporting, never a whitelist —
    any tag may be written."""
    return tags.describe()


@app.get("/api/tags")
def api_tags(tag: str = "", caveats: bool = False, project: str = "") -> dict:
    """Everything carrying a tag — the report that used to be a grep over prose.

    `caveats=true` answers "what in this registry is not yet proven?" across the
    whole estate in one query."""
    with db.connect() as conn:
        return {"tags": tags.find(conn, tag=tag, caveats_only=caveats,
                                  project=project)}


@app.post("/api/items/{number}/tags")
def api_add_tags(number: str, req: TagPut) -> dict:
    """Tag an item, or one of its revisions (`version`)."""
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            _fail(f"no item {number}", 404)
        rev = _resolve_rev(conn, number=number, version=req.version) \
            if req.version else None
        out = []
        try:
            with conn:
                for t in req.tags:
                    # A revision-scoped tag on a git-origin draft is a manifest
                    # edit; an item-level one is item master data (ops.set_tag).
                    out.append(ops.set_tag(conn, item, t, rev=rev,
                                           note=req.note, actor=req.actor))
        except tags.TagError as exc:
            _fail(str(exc))
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    return {"ok": True, "number": number, "tags": out}


@app.delete("/api/items/{number}/tags/{tag}")
def api_clear_tag(number: str, tag: str, version: str = "",
                  actor: str = "") -> dict:
    """Remove a tag. Clearing a CAVEAT is how a caveat is resolved — an ordinary
    write, so the event log records who decided it and when."""
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            _fail(f"no item {number}", 404)
        rev = _resolve_rev(conn, number=number, version=version) \
            if version else None
        try:
            with conn:
                res = ops.clear_tag(conn, item, tag, rev=rev, actor=actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    return {"ok": True, "removed": res["removed"], "number": number,
            "tag": tag, **({"git": res["git"]} if res.get("git") else {})}


@app.put("/api/files/{file_id}/role")
def api_set_file_role(file_id: int, req: RolePut) -> dict:
    """DECLARE what a file is for — the confirm step.

    This is the loop that drains the review queue: plm's own inference is marked
    a guess, and one call turns a guess into a stated fact. It changes
    `role_source` to 'declared', which is the whole point; the role VALUE may
    even stay the same, and confirming an inference is a real and useful act."""
    with db.connect() as conn:
        try:
            res = ops.set_file_role(conn, file_id, req.role, actor=req.actor)
        except roles.RoleError as exc:
            _fail(str(exc))
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, **res}


@app.get("/api/classification/review")
def api_classification_review(limit: int = 200) -> dict:
    """Rows whose classification a human should look at, and why — the queue the backfill deliberately did NOT auto-resolve.

    Three issues, and they are different problems: `mismatch` (declared purpose
    conflicts with the measured bytes), `legacy` (still in the `artifact` junk
    drawer, which says nothing), `unconfirmed` (the format admits more than one
    meaning and nobody has said which — a PNG that could be a drawing, a photo
    or a render)."""
    with db.connect() as conn:
        items = reclassify.disagreements(conn)
        return {"summary": reclassify.report(conn),
                "review": items[:max(0, limit)], "total": len(items)}


# -- fabrication: routings, process, stock and the cut list ----------
# plm/fab.py owns the vocabulary and the arithmetic; these are addresses. A
# routing is REVISION content, so it lives beside the attribute endpoints and
# takes the same two addresses (revision id, or number+version).

class RoutingPut(BaseModel):
    operations: list[dict] = Field(
        description="ordered [{op, params?, work_center?, notes?}] — `op` from "
                    "GET /api/fab/registry; dimensions are {value, unit} and "
                    "the unit is NOT optional")
    replace: bool = Field(
        default=True,
        description="true (default) swaps the whole sequence — a routing is a "
                    "list and is edited as a list, which also makes the write "
                    "idempotent for an agent regenerating from CAD. false "
                    "appends after the current last step.")
    actor: str = ""


class ProcessPatch(BaseModel):
    process: str = Field(
        description="'' | print | make. There is deliberately no 'buy': that is "
                    "what kind=purchased/hardware already says.")
    actor: str = ""


@app.get("/api/fab/registry")
def api_fab_registry() -> dict:
    """The fabrication vocabulary as data: operation verbs with their well-known
    params, the process values, end treatments, stock forms, the stock attribute
    names and the length units plm will convert between. Adding a verb is a dict
    entry in plm/fab.py and it appears here — the registry IS the feature."""
    return fab.registry()


@app.get("/api/revisions/{revision_id}/operations")
def api_operations(revision_id: int) -> dict:
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id)
        return {"revision_id": rev["id"], "number": rev["number"],
                "version": rev["version"],
                "process": fab.process_of(conn, rev["id"]),
                "operations": fab.get_routing(conn, rev["id"])}


@app.put("/api/revisions/{revision_id}/operations")
def api_operations_put(revision_id: int, req: RoutingPut) -> dict:
    """Write a revision's routing. DRAFT-only: a routing is manufacturing
    DEFINITION, not a measurement, so it changes on a working revision or not at
    all — the same discipline the five BOM verbs have."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id)
        try:
            # Git under the hood: on a revision that lives in a repo the
            # routing is `[[routing]]` in plm.toml, committed on its draft
            # branch. Same validation, same response (ops.set_routing).
            res = ops.set_routing(conn, rev, req.operations,
                                  actor=req.actor, replace=req.replace)
        except fab.FabError as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, rev["number"])
    return {"ok": True, "revision_id": rev["id"], **res}


@app.put("/api/items/{number}/operations")
def api_item_operations_put(number: str, req: RoutingPut, version: str = "") -> dict:
    """The number+version address for the same write (empty version = latest)."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, number=number, version=version)
        try:
            res = ops.set_routing(conn, rev, req.operations,
                                  actor=req.actor, replace=req.replace)
        except fab.FabError as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    return {"ok": True, "revision_id": rev["id"], **res}


@app.patch("/api/items/{number}/process")
def api_set_process(number: str, req: ProcessPatch, version: str = "") -> dict:
    """How this revision comes into existence: printed, or made (fabricated).
    This is the `_build_plan` dispatch key, which is why it is a validated
    revision column and not a free-text attribute."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, number=number, version=version)
        try:
            res = ops.set_process(conn, rev, req.process, actor=req.actor)
        except fab.FabError as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    return {"ok": True, "revision_id": rev["id"], **res}


@app.get("/api/items/{number}/cutlist")
def api_cutlist(number: str, version: str = "", revision_id: int = 0,
                kerf: float = 0.0, kerf_unit: str = "",
                remnant_min: float = 0.0, remnant_min_unit: str = "") -> dict:
    """The generated cut list for everything under one revision, plus the 1-D
    nest into purchasable stick lengths.

    `kerf`/`remnant_min` are FALLBACKS with an explicit unit — they never
    override a value the stock or the operation already carries, and passing a
    number without a unit is refused rather than assumed (plm/attributes.py's
    no-default-unit rule, which exists because his stock is imperial)."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id, number=number,
                           version=version)
        k = {"value": kerf, "unit": kerf_unit} if kerf else None
        r = {"value": remnant_min, "unit": remnant_min_unit} if remnant_min else None
        try:
            plan = fab.cut_list(conn, rev["id"], kerf=k, remnant_min=r)
        except fab.FabError as exc:
            _fail(str(exc), exc.code)
    return {"number": rev.get("number", number), "revision_id": rev["id"],
            "version": rev["version"], **plan}


@app.get("/api/items/{number}/cutlist.md", response_class=PlainTextResponse)
def api_cutlist_md(number: str, version: str = "") -> str:
    """The same cut list as markdown — one page, one generator, so the API, the
    MCP and the shop-floor service's print_markdown all put the same table on paper. This
    is what replaces the hand-maintained CUT_LIST.md."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, number=number, version=version)
        plan = fab.cut_list(conn, rev["id"])
        title = f"Cut list — {rev.get('number', number)} v{rev['version']}"
    return fab.markdown(plan, title=title)


# -- typed relations (docs/PLM_RELATIONS.md) -----------------------------------
# One table, one registry, no per-type endpoints: adding a relation type is a
# dict entry in plm/relations.py and every surface below picks it up.

class RelationCreate(BaseModel):
    type: str = Field(description="registry key, e.g. coupon_for (GET /api/relations/types)")
    from_: str = Field(alias="from", description="the item the label reads FROM")
    to: str = Field(description="the item it points at")
    from_version: str = Field(default="", description="revision-level types: defaults to latest")
    to_version: str = Field(default="", description="revision-level types: defaults to latest")
    note: str = ""
    actor: str = ""
    # stage 10: a `results` type's outcome (coupon_for) — pass | fail | pending
    result: str = ""
    result_note: str = ""
    measured: dict | None = None

    model_config = {"populate_by_name": True}


class RelationResult(BaseModel):
    result: str = Field(default="", description="pass | fail | pending")
    result_note: str = ""
    measured: dict | None = Field(default=None,
                                  description="free table: name -> value")
    actor: str = ""


def _rel_fail(exc: relations.RelationError):
    _fail(str(exc), exc.code)


@app.get("/api/relations/types")
def api_relation_types() -> list[dict]:
    """The relation registry: type, both labels, kind constraints, rev_level.
    Clients render and validate from this — never from a hard-coded list."""
    return relations.types()


@app.get("/api/items/{number}/relations")
def api_item_relations(number: str) -> dict:
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            _fail(f"unknown item: {number}", 404)
        return {"number": number, "relations": relations.for_item(conn, item["id"]),
                "types": relations.types()}


@app.post("/api/relations")
def api_relation_add(req: RelationCreate) -> dict:
    with db.connect() as conn:
        try:
            # A revision-level link travels with a clone of that revision, so
            # on a git-origin draft it lands in `[[relations]]` (ops.add_relation).
            rec = ops.add_relation(conn, req.type, req.from_, req.to,
                                   from_version=req.from_version,
                                   to_version=req.to_version, note=req.note,
                                   actor=req.actor, result=req.result,
                                   result_note=req.result_note,
                                   measured=req.measured)
        except relations.RelationError as exc:
            _rel_fail(exc)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    dispatch_event("relation", {"type": req.type, "from": rec["from"],
                                "to": rec["to"], "to_version": rec["to_version"],
                                "actor": req.actor})
    return {"ok": True, "relation": rec}


@app.post("/api/relations/{relation_id}/result")
def api_relation_result(relation_id: int, req: RelationResult) -> dict:
    """What a coupon FOUND, on the claim that says what it tested (stage 10).
    Written where the claim lives — the coupon draft's plm.toml line, or the
    row on a legacy revision (ops.set_relation_result)."""
    with db.connect() as conn:
        try:
            rec = ops.set_relation_result(
                conn, relation_id, result=req.result,
                result_note=req.result_note, measured=req.measured,
                actor=req.actor)
        except relations.RelationError as exc:
            _rel_fail(exc)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, "relation": rec}


@app.delete("/api/relations/{relation_id}")
def api_relation_delete(relation_id: int, actor: str = "") -> dict:
    with db.connect() as conn:
        try:
            rec = ops.remove_relation(conn, relation_id, actor=actor)
        except relations.RelationError as exc:
            _rel_fail(exc)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, "removed": rec}


class CouponCreate(BaseModel):
    name: str
    version: str = Field(default="", description="the revision it verifies; empty = latest")
    number: str = Field(default="", description="explicit coupon number; omit to auto-assign CPN")
    note: str = ""
    actor: str = ""


@app.post("/api/items/{number}/coupon")
def api_coupon_add(number: str, req: CouponCreate) -> dict:
    """Register a coupon for one revision of this item: a `coupon` item plus the
    coupon_for link, in one call (ops.add_coupon — the same path the MCP uses)."""
    with db.connect() as conn:
        try:
            res = ops.add_coupon(conn, req.name, number, for_version=req.version,
                                 number=req.number, note=req.note, actor=req.actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number, res["number"])
    return {"ok": True, **res}


class AssetCreate(BaseModel):
    name: str
    relation: str = Field(
        default="mounts_to",
        description="mounts_to | installed_on | integration_envelope_for")
    number: str = Field(default="", description="explicit asset number; omit to auto-assign AST")
    prefix: str = Field(default="", description="number prefix when this asset relates to nothing yet")
    project: str = ""
    version: str = Field(default="", description="revision of THIS item to pin (revision-level relations)")
    asset_version: str = Field(default="", description="revision of the asset to pin")
    note: str = ""
    actor: str = ""


@app.post("/api/items/{number}/asset")
def api_asset_add(number: str, req: AssetCreate, request: Request) -> dict:
    """Register the thing this item attaches to: an `asset` item (class AST) plus
    the link, in one call (ops.add_asset — the same path the MCP uses).

    An asset is what you neither build nor buy — a camper, a truck bed, a garage
    wall. It is reference geometry for fit, never a BOM line and never a purchase."""
    with db.connect() as conn:
        try:
            res = ops.add_asset(
                conn, req.name, number, relation=req.relation,
                project=req.project, prefix=req.prefix, number=req.number,
                for_version=req.version, asset_version=req.asset_version,
                note=req.note,
                actor=_verified_actor(request) or req.actor or "api")
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number, res["number"])
    return {"ok": True, **res}


@app.get("/api/items/{number}/bom")
def api_bom(number: str, version: str = "", rolled: bool = False,
            revision_id: int = 0) -> list[dict]:
    """A revision's BOM. Address it by number(+version), by item id, or by
    revision_id — the lines carry `id`, `child_item_id` and (rolled) `item_id`.

    Every line also carries `thumb` (URL, or null for 'no geometry'), so the
    tree's lazy child loads draw their placeholders without a single request."""
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id, number=number,
                           version=version)
        p = thumbs.Presence(conn)
        if rolled:
            return p.rows(db.rolled_bom(conn, rev["id"]))
        return p.bom(db.get_bom(conn, rev["id"]))


@app.get("/api/search")
def api_search(q: str, limit: int = 30) -> list[dict]:
    """Hits carry `thumb` like every other row surface: the URL when that
    revision has geometry, null when it has none."""
    with db.connect() as conn:
        return thumbs.Presence(conn).rows(search.query(conn, q, min(limit, 100)))


# -- context: comments on anything (plm/context.py) ---------------------------
# The item/revision endpoints are the ORIGINAL surface and keep their exact
# shape; they are now thin wrappers over the same generic write path the
# /api/context pair uses, so validation cannot drift between them.

class ContextAdd(BaseModel):
    body: str
    version: str = Field(default="", description="attach to this revision; empty = item-level")
    actor: str = ""


class ContextPost(BaseModel):
    body: str
    target_type: str = Field(
        default="item",
        description="context target type (GET /api/context/targets): item, "
                    "revision, relation, approval")
    target_id: str = Field(
        default="",
        description="which one: an item number or id, a revision id, a "
                    "relation id, an approval id")
    version: str = Field(default="",
                         description="item targets only: attach to this revision")
    actor: str = ""


def _ctx_fail(exc: context.ContextError):
    _fail(str(exc), exc.code)


@app.get("/api/items/{number}/context")
def api_context_get(number: str) -> list[dict]:
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            _fail(f"unknown item: {number}", 404)
        return context.for_item(conn, item["id"])


@app.post("/api/items/{number}/context")
def api_context_add(number: str, req: ContextAdd) -> dict:
    with db.connect() as conn:
        try:
            rec = context.add(conn, req.body, target_type="item", target=number,
                              version=req.version, actor=req.actor)
        except context.ContextError as exc:
            _ctx_fail(exc)
        search.touch(conn, number)
    return {"ok": True, "number": number, "version": req.version, "context": rec}


@app.get("/api/context/targets")
def api_context_targets() -> list[dict]:
    """What a context entry may be attached to: type, label, whether it is the
    classic item/revision form, and what the type is for. Clients render and
    validate from this — adding a target type is a dict entry in
    plm/context.py."""
    return context.targets()


@app.get("/api/context")
def api_context_generic(target_type: str = "item", target_id: str = "",
                        version: str = "") -> dict:
    """Every comment on one target, plus what that target is. `target_type=item`
    with an item number is the same log the item page shows."""
    with db.connect() as conn:
        try:
            return context.entries(conn, target_type, target_id, version)
        except context.ContextError as exc:
            _ctx_fail(exc)


@app.post("/api/context")
def api_context_generic_add(req: ContextPost) -> dict:
    """Attach a comment to any registered target — a relation, an approval, an
    item, a revision. One write path, one validation."""
    with db.connect() as conn:
        try:
            rec = context.add(conn, req.body, target_type=req.target_type,
                              target=req.target_id, version=req.version,
                              actor=req.actor)
        except context.ContextError as exc:
            _ctx_fail(exc)
        search.touch(conn, rec["item_id"])
    return {"ok": True, "context": rec}


def _top_items(conn, project: str, kind: str) -> list[dict]:
    """The roots of one project: items of `kind` that no BOM uses, and that have
    a revision to show. `kind='assembly'` AND unused (never merely 'unused'), so
    a coupon can never surface even though it is by construction absent from
    every BOM (docs/PLM_RELATIONS.md) — keep the kind predicate.

    LOAD-BEARING, and more so since assets. Every non-structural kind
    is by construction absent from every BOM, so "unused" alone selects exactly
    the wrong rows: a camper registered as an asset is unused in every BOM
    forever and would front the project card. Callers pass 'assembly' or
    'part' — never a bare unused-items query."""
    return [dict(r) for r in conn.execute("""
        SELECT i.id, i.number, i.name, i.kind, i.project, i.updated_at,
               r.id AS revision_id, r.version, r.state, r.created_at,
               (SELECT COUNT(*) FROM bom WHERE parent_rev_id=r.id) AS direct_lines
        FROM items i JOIN revisions r ON r.id=i.latest_rev_id
        WHERE i.project=? AND i.kind=?
          AND i.id NOT IN (SELECT DISTINCT child_item_id FROM bom)
        ORDER BY i.number""", (project, kind))]


class KindChange(BaseModel):
    kind: str = Field(
        description="part|assembly|purchased|hardware|firmware|document|"
                    "coupon|asset")
    note: str = Field(default="", description="why — recorded as context")
    renumber: bool = Field(
        default=True,
        description="reassign the number so its class token matches the new kind")
    actor: str = ""


@app.patch("/api/items/{number}/kind")
def api_set_kind(number: str, req: KindChange, request: Request) -> dict:
    """Change what an item IS (ops.reclassify_item). The number's class token
    and the vault folder are projections of `kind`, so this renumbers and moves
    the folder with it; the old number keeps resolving."""
    with db.connect() as conn:
        try:
            res = ops.reclassify_item(
                conn, number, req.kind, note=req.note, renumber=req.renumber,
                actor=_verified_actor(request) or req.actor or "api")
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except vault.VaultError as exc:
            _fail(f"vault move failed: {exc}", 500)
        search.touch(conn, res["number"])
    return {"ok": True, **res}


class ProductFlag(BaseModel):
    product: bool
    actor: str = ""


@app.patch("/api/items/{number}/product")
def api_set_product(number: str, req: ProductFlag, request: Request) -> dict:
    """Designate (or undesignate) an item as a PRODUCT — the end item the
    dashboard renders. Explicit data, never inferred from topology.

    Refused for a non-structural kind (coupon, asset): they are barred from BOMs,
    and this is the other door onto the dashboard (ops.set_product)."""
    with db.connect() as conn:
        try:
            res = ops.set_product(
                conn, number, req.product,
                actor=_verified_actor(request) or req.actor or "api")
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, **res}


@app.get("/api/products")
def api_products() -> list[dict]:
    """What am I working on? ONE GROUP PER PROJECT — the answer to a question
    about projects, not about assemblies.

    The old shape returned every top-level assembly as its own product, which
    told the truth about the structure and lied about the work: three related
    lantern bench-test assemblies read as three products, and a young project
    whose top item is still a PART (bike-projector, GL-PRT-1001) did not appear
    at all. So the group is the project; inside it:

    - `top_items` — the roots: assemblies no BOM uses. A project that has none
      yet falls back to its unused PARTS with revisions, so work in progress
      surfaces before it has an assembly to hang off.
    - `primary` — the most recently active root: what the card renders.
    - `counts` — the shop questions for the primary, plus how many roots there
      are, so the card can say 'and 2 more'.

    Non-structural kinds (coupons, assets) stay excluded structurally (by kind),
    never by a filter someone can forget: the flagged query filters them and
    ops.set_product refuses to flag them in the first place, so a camper cannot
    become a product card by either route.

    2026-08-12 (Geoff): the card face is a designated PRODUCT — the Teamcenter
    end-item concept, an EXPLICIT items.product flag — never a guess from BOM
    topology (the guess put a bench-test rig on the lantern's card). Where a
    project has flagged products, they ARE the cards; the topology walk remains
    only as the bootstrap fallback for projects nobody has flagged yet."""
    with db.connect() as conn:
        projects = [r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM items WHERE project!='' ORDER BY project")]
        out = []
        p = thumbs.Presence(conn)     # one memo for the whole dashboard
        # LOAD-BEARING predicate: `kind NOT IN (non-structural)`. A product is
        # something the project BUILDS, so no coupon and no asset can front a
        # card even if something flagged one before ops.set_product refused it.
        bars = config.NON_STRUCTURAL_KINDS
        holes = ",".join("?" * len(bars))
        for project in projects:
            flagged = [dict(r) for r in conn.execute(f"""
                SELECT i.id, i.number, i.name, i.kind, i.project, i.updated_at,
                       r.id AS revision_id, r.version, r.state, r.created_at,
                       (SELECT COUNT(*) FROM bom WHERE parent_rev_id=r.id) AS direct_lines
                FROM items i JOIN revisions r ON r.id=i.latest_rev_id
                WHERE i.project=? AND i.product=1 AND i.kind NOT IN ({holes})
                ORDER BY i.number""", (project, *bars))]
            fallback = False
            if flagged:
                tops = flagged
            else:
                tops = _top_items(conn, project, "assembly")
                if not tops:
                    # No top assembly yet: a young project is still real work.
                    tops = _top_items(conn, project, "part")
                    fallback = bool(tops)
            if not tops:
                continue
            for t in tops:
                t["last_activity"] = max(t["created_at"] or 0, t["updated_at"] or 0)
            tops.sort(key=lambda t: t["last_activity"], reverse=True)
            # The card face and every row under it: `thumb` or null. The card
            # never guesses that a top item has a picture.
            p.rows(tops)
            primary = tops[0]
            # cutlist=False: the dashboard COUNTS, it does not nest. A project
            # card asking "how many to make" must not pay for a bin-packing run.
            plan = _build_plan(conn, primary["revision_id"], cutlist=False)
            item_count = conn.execute(
                "SELECT COUNT(*) FROM items WHERE project=?", (project,)).fetchone()[0]
            out.append({
                "project": project,
                "primary": primary,
                "top_items": tops,
                "fallback_kind": "part" if fallback else "",
                "counts": {"top_items": len(tops), "items": item_count,
                           "n_print": len(plan["print"]),
                           "n_make": len(plan["make"]),
                           "n_buy": len(plan["buy"])},
                "last_activity": primary["last_activity"],
            })
        out.sort(key=lambda g: g["last_activity"], reverse=True)
        return out


def _build_plan(conn, rev_id: int, waste_pct: float = 15.0,
                presence: thumbs.Presence | None = None,
                cutlist: bool = True) -> dict:
    """Split a rolled-up BOM into the shop questions: what gets PRINTED (with
    geometry status, material, mass), what gets MADE (fabricated — cut, drilled,
    notched, welded, with its routing), what gets BOUGHT (with sourcing), and a
    material rollup — Σ(qty × mass) per material plus a waste allowance for
    supports/brims/failures. Mass comes from the slicer via set-material.

    Pass `presence` when the rows will be RENDERED: it stamps each row's
    `thumb`. Callers that only count (the project cards) leave it out and pay
    nothing.

    THE PREDICATES BELOW ARE ALLOW-LISTS, AND THAT IS LOAD-BEARING. Nothing
    reaches a shop bucket except by matching a rule that names it, so a kind (or
    a process) that must never reach the shop cannot reach it by being forgotten
    here. That is the second wall behind the BOM guards (ops.NOT_IN_BOM): assets
    and coupons never enter a BOM, so db.rolled_bom cannot yield them, and if one
    ever did it would still not be printed, made, bought or counted into the
    material rollup. Never turn any branch into a deny-list ('everything that is
    not a document is bought'). ADDED THE MAKE BUCKET, AND IT KEPT THAT PROPERTY by adding a second
    ALLOW-LISTED axis rather than inverting anything:

        kind=part      + process=make            -> MAKE   (fabricated piece)
        kind=part      + process in ('', print)  -> PRINT  (unchanged default)
        kind=assembly  + process=make            -> MAKE   (a WELDMENT: its own
                                                    joining/finishing routing)
        kind=purchased|hardware                  -> BUY    (unchanged)
        anything else                            -> neither (unchanged)

    `process=''` means "not stated", and for a part that reads as PRINT — which
    is exactly what the plan did before this column existed, so every lantern
    part behaves identically with zero rows changed. There is deliberately no
    `process='buy'`: `kind` already says that (see plm/fab.py). And an assembly
    with no `process` still falls through to neither bucket, as it always has."""
    to_print, to_make, to_buy = [], [], []
    materials: dict[str, dict] = {}
    for line in db.rolled_bom(conn, rev_id):
        bound = None
        process = ""
        if line["kind"] in ("part", "assembly"):
            bound = db.find_revision(conn, line["number"], line["version"]) \
                or db.find_revision(conn, line["number"])
            process = (bound.get("process") or "") if bound else ""
        if line["kind"] in ("part", "assembly") and process == "make":
            # FABRICATED. The routing comes with the row because "what work does
            # this need" is the question a make list exists to answer, and
            # `stock` closes the traceability chain the row is part of:
            # weldment (line.paths) -> this cut part -> its raw stock.
            routing = fab.get_routing(conn, bound["id"]) if bound else []
            stock = fab.stock_of(conn, line["item_id"]) if line.get("item_id") else None
            cut = next((o for o in routing if o["consumes_stock"]), None)
            to_make.append({**line, "process": process, "operations": routing,
                            "op_count": len(routing),
                            "cut": cut, "stock": stock,
                            "material": (bound["material"] if bound else "") or "",
                            "weldment": line["kind"] == "assembly"})
        elif line["kind"] == "part":
            # `has_stl` is deliberately the part's OWN mesh — "is this printable
            # today" — where `thumb` is "is there anything to picture" and may
            # come from a child. Same table, two different questions.
            has_stl = bool(bound and thumbs.has_own_stl(conn, bound["id"]))
            material = (bound["material"] if bound else "") or ""
            mass = bound["mass_g"] if bound else None
            to_print.append({**line, "has_stl": has_stl,
                             "material": material, "mass_g": mass})
            key = material or "(material unknown)"
            m = materials.setdefault(key, {"material": key, "net_g": 0.0,
                                           "parts": 0, "unknown_mass": 0})
            m["parts"] += 1
            if mass:
                m["net_g"] += mass * (line["qty"] or 1)
            else:
                m["unknown_mass"] += 1
        elif line["kind"] in ("purchased", "hardware"):
            src = conn.execute(
                "SELECT supplier, manufacturer, mpn, url FROM sources WHERE item_id=? LIMIT 1",
                (line["item_id"],)).fetchone() if line.get("item_id") else None
            to_buy.append({**line, "source": dict(src) if src else None})
    for m in materials.values():
        m["net_g"] = round(m["net_g"], 1)
        m["with_waste_g"] = round(m["net_g"] * (1 + waste_pct / 100), 1)
    if presence:
        presence.rows(to_print)
        presence.rows(to_make)
        presence.rows(to_buy)
    out = {"print": to_print, "make": to_make, "buy": to_buy,
           "waste_pct": waste_pct,
           "materials": sorted(materials.values(), key=lambda m: -m["net_g"])}
    # THE CUT LIST IS REACHABLE FROM THE PLAN, not a separate errand: a MAKE list
    # without stick counts stops one question short of the trip to the steel
    # yard. Computed only when there is something to cut, so a lantern plan pays
    # nothing at all for this.
    if to_make and cutlist:
        out["cutlist"] = fab.cut_list(conn, rev_id)
    # stage 19 (handoff finding 6): purchases nobody has SELECTED yet — the
    # `[[bom]] requirement = …` lines (plm/requirements.py). Additive, and only
    # when there are some: a green plan must not read as order-ready while
    # the strap and the cable are still prose.
    from . import requirements
    reqs = requirements.for_revision(conn, rev_id)
    if reqs:
        out["requirements"] = reqs
        out["requirements_say"] = (
            f"{len(reqs)} purchase requirement{'s' if len(reqs) != 1 else ''} "
            "— not selected (this revision's [[bom]] requirement lines)")
    return out


@app.get("/api/revreport")
def api_revreport(pair: list[str] = Query(default=[]),
                  watch: list[str] = Query(default=[]),
                  disposition: list[str] = Query(default=[]),
                  change: str = "", format: str = "json"):
    """The revision report from git (stage 19, plm/revreport.py): `pair` =
    `N@before..after` (repeat), `change=CHG-n` = every member baseline -> head,
    `watch` = `N[@ref]` shown as inspected, `disposition` =
    `N=REPRINT|REWORK|REUSE|FIT-TEST-ONLY|N/A[:why]`. `format=md` returns the
    skill's three sections as markdown."""
    from fastapi.responses import PlainTextResponse
    from . import changes, revreport
    try:
        pairs: list = []
        chg = ""
        if change:
            pairs, chg = revreport.from_change(change)
        pairs += [revreport._parse_pair(p) for p in pair]
        if not pairs and not watch:
            _fail("give pair=N@before..after (repeatable), change=CHG-n or "
                  "watch=N", 400)
        rep = revreport.report(
            pairs, watches=[(w.partition("@")[0], w.partition("@")[2])
                            for w in watch],
            dispositions=revreport._parse_disp(disposition), change=chg)
    except revreport.ReportError as exc:
        _fail(str(exc), 400)
    except changes.ChangeError as exc:
        _fail(str(exc), getattr(exc, "code", 400))
    except LookupError as exc:
        _fail(str(exc), 404)
    if format == "md":
        return PlainTextResponse(revreport.markdown(rep),
                                 media_type="text/markdown; charset=utf-8")
    return rep


@app.get("/api/items/{number}/requirements")
def api_requirements(number: str, version: str = "",
                     revision_id: int = 0) -> dict:
    """A revision's purchase REQUIREMENTS (stage 19): `[[bom]]` lines with
    `requirement` and no `path` — what must be bought, nothing selected."""
    from . import requirements
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id, number=number,
                           version=version)
        reqs = requirements.for_revision(conn, rev["id"])
        return {"number": rev.get("number", number), "version": rev["version"],
                "requirements": reqs, "unresolved": len(reqs)}


@app.get("/api/items/{number}/plan")
def api_plan(number: str, version: str = "", waste_pct: float = 15.0,
             revision_id: int = 0) -> dict:
    with db.connect() as conn:
        rev = _resolve_rev(conn, revision_id=revision_id, number=number,
                           version=version)
        return {"number": rev.get("number", number), "revision_id": rev["id"],
                "version": rev["version"],
                **_build_plan(conn, rev["id"], waste_pct,
                              presence=thumbs.Presence(conn))}


class MaterialReq(BaseModel):
    version: str = ""
    material: str = ""
    mass_g: float | None = None
    actor: str = ""
    source: str = Field(default="", description="where the number came from, e.g. 'orca 2.4.2 slice'")


@app.post("/api/items/{number}/material")
def api_material(number: str, req: MaterialReq) -> dict:
    """Record material master data on a revision — typically posted back by
    the slicer (it knows the grams), or set by hand/agent."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, req.version)
        if not rev:
            _fail(f"no revision for {number} {req.version or '(latest)'}", 404)
        # A revision that lives in git: refused outside `write`; in it, the
        # same write as `set_attributes` (a commit on a draft, a note on a
        # frozen revision) — never the row alone (ops._stranded).
        try:
            if ops.registry_only_write(conn, rev, number=number):
                res = ops.set_attributes(
                    conn, rev, {**({"material": req.material} if req.material else {}),
                                **({"mass_g": req.mass_g} if req.mass_g is not None else {})},
                    source=req.source, actor=req.actor)
                return ops._with_git(
                    {"ok": True, "number": number, "version": rev["version"],
                     "material": req.material, "mass_g": req.mass_g},
                    res.get("git"))
        except attributes.AttrError as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        conn.execute("UPDATE revisions SET material=CASE WHEN ?!='' THEN ? ELSE material END, "
                     "mass_g=COALESCE(?, mass_g) WHERE id=?",
                     (req.material, req.material, req.mass_g, rev["id"]))
        db.add_event(conn, "material", item_id=rev["item_id"], revision_id=rev["id"],
                     actor=req.actor, data={"material": req.material,
                                            "mass_g": req.mass_g, "source": req.source})
    return {"ok": True, "number": number, "version": rev["version"],
            "material": req.material, "mass_g": req.mass_g}


@app.get("/api/activity")
def api_activity(limit: int = 30, offset: int = 0) -> dict:
    """The feed, paginated. `stl_count` is the row's own meshes; `thumb` is the
    render's URL or null — the feed used to guess from stl_count/kind and asked
    anyway.

    ⚠️ RETURNS AN OBJECT, not a bare list as it used to. The pager needs `total`
    to know when it has reached the end, and a bare array has nowhere to put it —
    a "next" button that only discovers the end by fetching an empty page is one
    that is always enabled. `activity` holds what the list used to be, so a caller
    reading `data.activity` gets exactly the old rows.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with db.connect() as conn:
        rows = thumbs.Presence(conn).rows(db.activity(conn, limit, offset))
        total = db.activity_count(conn)
    return {"activity": rows, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total}


@app.get("/api/items/{number}/thumb")
def api_thumb(number: str, version: str = ""):
    """The PNG itself. Clients should reach this ONLY via a `thumb` URL handed
    to them by a read (a null there means there is nothing to render): a 404
    here means "not rendered YET", which is what the client's retry is for."""
    p = thumbs.thumb_for(number, version)
    if not p:
        _fail("no renderable geometry for this revision", 404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/items/{number}/explode")
def api_explode(number: str, version: str = "") -> dict:
    """Will this revision EXPLODE in the viewer, and if not, which child is
    why (plm/thumbs.explode_status — the same resolver the viewer uses)."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            _fail(f"no revision for {number} {version or '(latest)'}", 404)
        return {"number": number, "version": rev["version"],
                **thumbs.explode_status(conn, rev["id"])}


@app.get("/api/items/{number}/geometry")
def api_geometry(number: str, version: str = "") -> list[dict]:
    """The bodies to view for one revision: its own primary STL, else ONE
    primary STL per unsuppressed BOM occurrence, recursing through nested
    sub-assemblies until real parts are found. (Revisions often carry
    variant/coupon STLs beside the real part — only primaries are returned.)

    Each body carries `url` — the backend-aware way to fetch its bytes
    (/api/blob?id= for offloaded rows, ?path= for vault rows) — so the viewer
    never has to know where a mesh lives."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            _fail(f"no revision for {number} {version or '(latest)'}", 404)
        bodies = thumbs.geometry_bodies(conn, rev["id"])
    # IS THIS ACTUALLY AN ASSEMBLED VIEW? plm's BOM lines carry no occurrence
    # transform, so every body reached through the BOM is drawn at its own
    # modelled origin (thumbs.placement). When the parts were modelled
    # individually that is a heap, not an assembly, and the viewer has to SAY so
    # rather than let it read as the real shape of the product.
    #
    # Stamped onto every body rather than wrapped in an envelope: the response is
    # a bare list that the viewer already consumes, and changing its shape to
    # carry one caption is not worth breaking the one thing the user is currently
    # looking at. The rule still lives on the server — the client renders it.
    place = thumbs.placement(bodies)
    for b in bodies:
        b["url"] = (f"/api/blob?id={b['id']}"
                    if (b.get("backend") or "vault") != "vault"
                    else "/api/blob?path=" + quote(b["path"]))
        b["assembled"] = place["assembled"]
        b["placement_note"] = place["note"]
    return bodies


@app.get("/api/files/{file_id}")
def api_file(file_id: int) -> dict:
    """One file's RECORD (never its bytes): what it is, which revision owns it,
    and the backend-aware URL to fetch it from.

    The web viewer's file previews are a routed state, so a reload has to be able
    to rebuild the header — name, role, size — from the id in the URL alone."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT f.*, r.version, r.state, i.number, i.name AS item_name,
                      i.folder FROM files f
               JOIN revisions r ON r.id=f.revision_id
               JOIN items i ON i.id=r.item_id WHERE f.id=?""",
            (file_id,)).fetchone()
        if not row:
            _fail(f"no such file id: {file_id}", 404)
        gen = ops.file_generation(row, row["folder"] or "", row["number"])
    out = {"id": row["id"], "revision_id": row["revision_id"],
           "number": row["number"], "item_name": row["item_name"],
           "version": row["version"], "state": row["state"],
           "name": row["path"].rsplit("/", 1)[-1], "path": row["path"],
           "role": row["role"], "size": row["size"], "sha256": row["sha256"],
           "media": row["media"] or "", "backend": row["backend"] or "vault",
           "url": f"/api/blob?id={row['id']}",
           "from_version": gen, "inherited": bool(gen) and gen != row["version"]}
    # The same block every other file payload carries: what it is, which room
    # opens it, which widgets diff it, and what you may do with it. One row
    # fetched by id must not know less about itself than the same row inside
    # `get_item` does (plm/db._file_out).
    return db._file_out(out, row["state"] or "")


@app.delete("/api/files/{file_id}")
def api_file_delete(file_id: int, request: Request, actor: str = "") -> dict:
    """Take one file off a revision's manifest (ops.remove_file).

    Removes a POINTER, not content: the vault copy stays in git history and an
    offloaded object keeps its key. DRAFTs freely; on a frozen revision only
    rows carried in from an older version folder, and the refusal teaches why."""
    who = _verified_actor(request) or actor or "api"
    with db.connect() as conn:
        try:
            res = ops.remove_file(conn, file_id=file_id, actor=who)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, res["number"])
    dispatch_event("file.remove", {"number": res["number"],
                                   "version": res["version"],
                                   "name": res["removed"], "actor": who})
    return {"ok": True, **res}


@app.get("/api/files/{file_id}/link")
def api_file_link(file_id: int, expires_in: int = 3600) -> dict:
    """A URL for one file's bytes: `/api/blob?id=` — plm serves every row's
    bytes itself, so reaching plm is the access control.
    `expiring_url` stays in the response (always None) for existing clients;
    `expires_in` is accepted and ignored."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row:
        _fail(f"no such file id: {file_id}", 404)
    return {"ok": True, "id": file_id, "url": f"/api/blob?id={file_id}",
            "expiring_url": None,
            "note": f"{row['backend'] or 'vault'}-backed: {storage.LINK_NOTE}"}


@app.get("/api/items/{number}/diff", response_class=PlainTextResponse)
def api_diff(number: str, a: str, b: str) -> str:
    with db.connect() as conn:
        try:
            return ops.revision_diff(conn, number, a, b)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)


# ⛔ H2 / M11 — ONE TABLE, AND THE DEFAULT IS `attachment`. This was
# `INLINE_MEDIA` plus a comment explaining, correctly, why `.svg` was absent —
# and `plm/gitapi.py` then grew a second table with `.svg` in it, on the newest
# surface in the system. The rule lives in `plm/roles.py` now
# (`serve_media` / `may_preview` / `preview_media`), the same module that
# decides what a file IS, and every blob route in plm serves
# `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`.
# Rendering is `/api/preview`, which opts in per media type and answers under a
# sandbox CSP (plm/websec.py).


def _blob_media(name: str) -> str:
    """The content type for a blob. `roles.serve_media` and nothing else."""
    return roles.serve_media(name)


@app.get("/api/blob")
def api_blob(path: str = "", id: int = 0):
    """Stream a file's bytes from wherever they live (plm/storage.py).
    id= resolves the files row and dispatches on its backend; path= is the
    legacy vault-only form every existing link uses — unchanged."""
    if id:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE id=?", (id,)).fetchone()
        if not row:
            _fail(f"no such file id: {id}", 404)
        try:
            kind, obj = storage.resolve(dict(row))
        except (storage.StorageError, vault.VaultError) as exc:
            _fail(str(exc), 502)
        name = row["path"].rsplit("/", 1)[-1]
        media = _blob_media(name)
        if kind == "file":
            if not obj.is_file():
                _fail(f"bytes missing for file id {id}", 404)
            return FileResponse(obj, media_type=media,
                                headers=_blob_headers(name))
        return StreamingResponse(obj, media_type=media,
                                 headers=_blob_headers(name))
    if not path:
        _fail("pass path= or id=", 400)
    try:
        full = vault.safe_path(path)
    except vault.VaultError as exc:
        _fail(str(exc), 403)
    if not full.is_file():
        _fail(f"no such file: {path}", 404)
    return FileResponse(full, media_type=_blob_media(full.name),
                        headers=_blob_headers(full.name))


def _blob_headers(name: str, *, disposition: str = "attachment") -> dict:
    return {"Content-Disposition": roles.content_disposition(disposition, name),
            "X-Content-Type-Options": "nosniff"}


@app.get("/api/preview")
def api_preview(path: str = "", id: int = 0):
    """The rendering half of `/api/blob` — sandboxed, and opt-in per type.

    Same split as `/api/repo/{n}/preview`: a separate PATH, so the CSP is keyed
    off it (plm/websec.py), it shows up in an access log, and a production
    deployment can put it on another origin with one nginx location block.
    """
    if id:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE id=?", (id,)).fetchone()
        if not row:
            _fail(f"no such file id: {id}", 404)
        name = row["path"].rsplit("/", 1)[-1]
        try:
            kind, obj = storage.resolve(dict(row))
        except (storage.StorageError, vault.VaultError) as exc:
            _fail(str(exc), 502)
        if kind != "file" or not obj.is_file():
            _fail("this file is not on local disk; download it instead", 415)
        full = obj
    else:
        if not path:
            _fail("pass path= or id=", 400)
        try:
            full = vault.safe_path(path)
        except vault.VaultError as exc:
            _fail(str(exc), 403)
        if not full.is_file():
            _fail(f"no such file: {path}", 404)
        name = full.name
    media = roles.serve_media(name)
    if not roles.may_preview(media):
        _fail(f"plm will not render {media} in a browser — download it.", 415)
    size = full.stat().st_size
    if size > PREVIEW_MAX:
        _fail(f"{name} is {size:,} bytes — too large to preview "
              f"(limit {PREVIEW_MAX:,}).", 413)
    return Response(full.read_bytes(), media_type=roles.preview_media(media),
                    headers=_blob_headers(name, disposition="inline"))


# -- disposable 3D snippets ---------------------------------------------------
# "Hey, do you like this?" — a temporary STL with a link, no registry
# footprint. See plm/snippets.py and docs/PLM_SNIPPETS.md. These live on the
# machine plane (/api/* needs no session), which is what makes them usable by
# an agent; the private network plus an unguessable token is the access control.

class SnippetCreate(BaseModel):
    name: str = "model.stl"
    content_b64: str = ""
    # Files-by-reference: a staged handle from upload() or a link to the
    # bytes instead of inline bytes — the only sane path
    # for a big mesh. `filedrop_key` is the legacy alias of `handle`.
    url: str = ""
    handle: str = ""
    filedrop_key: str = ""
    sha256: str = ""
    size: int = 0
    note: str = ""
    ttl_days: float = snippets.TTL_DEFAULT_DAYS
    # Internal/testing knob: seconds instead of days, so the janitor can be
    # exercised without waiting a day. Not advertised to callers.
    ttl_seconds: float = 0
    actor: str = ""


def _snip_fail(exc: snippets.SnippetError):
    raise HTTPException(status_code=exc.code, detail=str(exc))


@app.post("/api/snippets")
def api_snippet_create(req: SnippetCreate) -> dict:
    """Upload a temporary STL; returns the viewer URL and when it dies."""
    handle = (req.handle or req.filedrop_key or "").strip()
    link = (req.url or "").strip()
    data = b""
    if not handle and not link:
        if not req.content_b64:
            _fail("give content_b64 (small STL), url (a link to the bytes) or "
                  "handle (a staged upload handle)", 400)
        try:
            data = base64.b64decode(req.content_b64, validate=False)
        except Exception:  # noqa: BLE001
            _fail("content_b64 is not valid base64", 400)
    with db.connect() as conn:
        try:
            if link:
                return snippets.create_from_url(
                    conn, url=link, sha256=req.sha256, size=req.size,
                    name=req.name, note=req.note, ttl_days=req.ttl_days,
                    ttl_seconds=req.ttl_seconds, actor=req.actor or "api")
            if handle:
                return snippets.create_from_handle(
                    conn, handle=handle, sha256=req.sha256, size=req.size,
                    name=req.name, note=req.note, ttl_days=req.ttl_days,
                    ttl_seconds=req.ttl_seconds, actor=req.actor or "api")
            return snippets.create(
                conn, name=req.name, data=data, note=req.note,
                ttl_days=req.ttl_days, ttl_seconds=req.ttl_seconds,
                actor=req.actor or "api")
        except snippets.SnippetError as exc:
            _snip_fail(exc)
        except staging.StagingError as exc:
            _fail(str(exc), exc.code if hasattr(exc, "code") else 400)
        except storage.StorageError as exc:
            _fail(f"could not store the snippet: {exc}", 502)


@app.get("/api/snippets")
def api_snippets(include_expired: bool = False, limit: int = 100) -> list[dict]:
    with db.connect() as conn:
        return snippets.listing(conn, include_expired=include_expired,
                                limit=limit)


@app.get("/api/snippets/{token}")
def api_snippet(token: str) -> dict:
    with db.connect() as conn:
        try:
            return snippets.record(snippets.get(conn, token))
        except snippets.SnippetError as exc:
            _snip_fail(exc)


@app.get("/api/snippets/{token}/blob")
def api_snippet_blob(token: str):
    """The snippet's STL — the viewer's mesh source, served like /api/blob."""
    with db.connect() as conn:
        try:
            path = snippets.path(snippets.get(conn, token))
        except snippets.SnippetError as exc:
            _snip_fail(exc)
    return FileResponse(path, media_type=_blob_media(path.name),
                        headers={**_blob_headers(path.name,
                                                 disposition="inline"),
                                 "Cache-Control": "private, max-age=300"})


@app.delete("/api/snippets/{token}")
def api_snippet_delete(token: str, actor: str = "") -> dict:
    """Revoke early — the same thing the janitor does when the TTL runs out."""
    with db.connect() as conn:
        try:
            return snippets.delete(conn, token, actor=actor or "api")
        except snippets.SnippetError as exc:
            _snip_fail(exc)


# The human-facing snippet link, /snip/<token>[/info], is a page of the UI like
# any other since stage 22 (real paths): the web mount serves the shell for it
# and the router draws the snippet — no redirect into a `#/snip/…` fragment any
# more. Still deliberately NOT under /api: a path survives the OIDC round trip,
# and being outside PUBLIC_PREFIXES means it is gated like every human page.
# The token stays the capability on the machine plane (/api/snippets/<token>).


@app.post("/api/snippets/gc")
def api_snippet_gc() -> dict:
    """Run the expiry sweep now (it also runs at startup and every few hours)."""
    with db.connect() as conn:
        return snippets.purge_expired(conn)


# -- staged uploads: getting big files IN (docs/PLM_UPLOADS.md) ---------------
# plm's own inbox is the main road (a LINK, the `url` wire form, is the other
# one): POST mints a token, the client
# PUTs the raw bytes to /api/uploads/<token> (machine plane, bearer token like
# every /api/ write) and checks in with the handle.

class UploadRequest(BaseModel):
    name: str = Field(description="filename, e.g. bracket.stl — used in the object key")
    size: int = Field(default=0, description="byte count you are about to upload (verified)")
    sha256: str = Field(default="", description="sha256 of the bytes (verified)")
    expires_in: float = Field(default=0, description="seconds; default PLM_STAGING_TTL_SECONDS")
    actor: str = ""


def _staging_fail(exc: staging.StagingError):
    _fail(str(exc), exc.code)


@app.post("/api/uploads")
def api_upload_request(req: UploadRequest) -> dict:
    """Mint an upload and return {put_url, handle, expires}; PUT the bytes to
    put_url (with your bearer token).

    Declare size and sha256 if you know them: they are checked against what
    actually lands, so a truncated upload fails at check-in instead of becoming
    a corrupt revision."""
    with db.connect() as conn:
        try:
            return {"ok": True, **staging.mint(
                conn, name=req.name, size=req.size, sha256=req.sha256,
                actor=req.actor or "api",
                expires_in=req.expires_in or None)}
        except staging.StagingError as exc:
            _staging_fail(exc)


def _in_db(fn, *args, **kw):
    """`fn(conn, *args, **kw)` on a fresh connection — for run_in_threadpool,
    so an async route never touches SQLite on the event loop."""
    with db.connect() as conn:
        return fn(conn, *args, **kw)


def _refuse_declared_oversize(request: Request) -> None:
    """413 on a Content-Length over PLM_RAW_UPLOAD_MAX, before anything is
    opened; a body that lies about its length is caught by the Sink."""
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > config.RAW_UPLOAD_MAX:
        _staging_fail(staging.Sink.too_big(f"{int(declared):,} bytes"))


async def _stream_into(sink: staging.Sink, request: Request) -> None:
    """The request body into `sink`, about a megabyte per threadpool write —
    the event loop only ever receives; the file I/O and hashing run off it.
    A failure discards what was written."""
    buf = bytearray()
    try:
        async for chunk in request.stream():
            buf += chunk
            if len(buf) >= 1 << 20:
                await run_in_threadpool(sink.write, bytes(buf))
                buf.clear()
        if buf:
            await run_in_threadpool(sink.write, bytes(buf))
    except BaseException:
        await run_in_threadpool(sink.close, True)
        raise
    await run_in_threadpool(sink.close)


@app.put("/api/uploads/{token}")
async def api_upload_put(token: str, request: Request) -> dict:
    """The bytes of a minted upload, as the raw request body (`curl -f -T`).

    Streamed to plm's inbox and capped at PLM_RAW_UPLOAD_MAX — the
    Content-Length is refused up front when it says too much, and the count
    is enforced while reading when it lies. Hashed here, once: a sha256/size
    declared at upload() that disagrees is a 400 and nothing is kept. Written
    once: a second PUT is a 409 (drop_upload + upload again). The identity
    middleware leaves this body alone (plm/identity.py)."""
    _refuse_declared_oversize(request)
    try:
        row, sink = await run_in_threadpool(_in_db, staging.open_inbox, token)
        await _stream_into(sink, request)
        row = await run_in_threadpool(_in_db, staging.land, row, sink,
                                      actor=_verified_actor(request) or "")
    except staging.StagingError as exc:
        _staging_fail(exc)
    rec = staging.record(row)
    return {"ok": True, **{k: rec[k] for k in ("handle", "name", "sha256",
                                               "size")}}


@app.get("/api/uploads")
def api_uploads(include_expired: bool = False, limit: int = 100) -> list[dict]:
    """Staged uploads not yet checked in — what is sitting in the coat-check."""
    with db.connect() as conn:
        return staging.listing(conn, include_expired=include_expired, limit=limit)


@app.get("/api/uploads/status")
def api_upload_status(handle: str) -> dict:
    """Did my upload land? The row's arrived sha256/size (hashed once, when
    the PUT finished) and a check that the file is still there — nothing is
    re-read and nothing is checked in."""
    with db.connect() as conn:
        try:
            return {"ok": True, "arrived": True, **staging.claim(conn, handle)}
        except staging.StagingError as exc:
            if exc.code == 409:  # minted, nothing uploaded yet
                return {"ok": True, "arrived": False, "handle": handle,
                        "detail": str(exc)}
            _staging_fail(exc)


@app.delete("/api/uploads")
def api_upload_drop(handle: str, actor: str = "") -> dict:
    """Abandon a staged upload now instead of waiting for its TTL."""
    with db.connect() as conn:
        try:
            return {"ok": True, **staging.drop(conn, handle, actor=actor or "api")}
        except staging.StagingError as exc:
            _staging_fail(exc)


@app.post("/api/uploads/gc")
def api_upload_gc() -> dict:
    """Run the staged-upload sweep now (also at startup and every few hours)."""
    with db.connect() as conn:
        return staging.purge_expired(conn)


# -- check-in (the write path) ------------------------------------------------

class ItemCreate(BaseModel):
    name: str
    kind: str = "part"
    project: str
    number: str | None = Field(default=None, description="explicit part number; omit to auto-assign")
    prefix: str | None = Field(default=None, description="number prefix, e.g. KML; required for auto-assign")
    actor: str = ""
    idempotency_key: str = Field(
        default="",
        description="retry safety on the AUTO-NUMBER path (plm/idem.py). Send "
                    "the same key on a retry and plm replays the first result "
                    "instead of minting a second number. Even without one, an "
                    "identical create from the same actor inside "
                    "PLM_IDEMPOTENCY_AUTO_WINDOW is treated as a retry.")


class CheckinFile(BaseModel):
    """One file, in exactly one of three forms (docs/PLM_UPLOADS.md).

    `text` for small documents, `content_b64` for small binaries, `handle` —
    a handle from POST /api/uploads, plm's own inbox (`filedrop_key` is its
    legacy alias) — and `url`, any https link to the bytes that plm can reach.
    The last two are the same idea (files by reference). Big inline payloads
    are refused; see plm/staging.py."""

    name: str
    content_b64: str | None = None
    text: str | None = None
    role: str = ""
    url: str = Field(
        default="",
        description="https link to the bytes that plm can reach; sha256 is "
                    "required with it and verified while streaming")
    handle: str = Field(
        default="", description="staged-upload handle from POST /api/uploads, "
                                "e.g. staging/<token>/<name>")
    filedrop_key: str = Field(default="", description="legacy alias of `handle`")
    sha256: str = Field(default="", description="claimed sha256 of the staged object")
    size: int = Field(default=0, description="claimed byte count of the staged object")


class BomLine(BaseModel):
    child: str
    version: str = ""
    qty: float = 1
    unit: str = "EA"
    find: int | None = None
    notes: str = ""


class Checkin(BaseModel):
    version: str
    state: str = "PROTOTYPE"
    summary: str
    ecn: str = ""
    author: str = ""
    source: str = ""
    selector: str = ""
    files: list[CheckinFile] = Field(default_factory=list)
    bom: list[BomLine] = Field(default_factory=list)


class StateChange(BaseModel):
    state: str
    actor: str = ""
    note: str = ""


@app.post("/api/items")
def api_create_item(req: ItemCreate, request: Request) -> dict:
    with db.connect() as conn:
        try:
            res = ops.create_item(
                conn, req.name, req.kind, req.project,
                number=req.number or "", prefix=req.prefix or "",
                actor=_verified_actor(request) or req.actor or "api",
                idempotency_key=req.idempotency_key)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        out = db.get_item(conn, res["number"])
        # A replayed create returns the item that already exists, and says so —
        # never silently, because "you got the first one back" is information
        # (plm/idem.py).
        if res.get("replayed"):
            out = {**(out or {}), "replayed": True,
                   "idempotent": res["idempotent"]}
        return out


@app.post("/api/items/{number}/checkin")
def api_checkin(number: str, req: Checkin) -> dict:
    """Check in a new revision. **A compatibility shim over git** (stage 3c §1).

    On an item that lives in a repo this is one commit on `draft/v<version>`
    plus a submission, and `plm/policy.py` decides whether that merges now. The
    response keeps every key it had and gains `git`, `approval_id`, `policy`;
    `state` tells you what actually happened.

    Prefer `git push HEAD:refs/for/main -o maturity=<m>` — it keeps your commit
    byte for byte, which is what makes the approval a signature over the bytes.
    """
    with db.connect() as conn:
        try:
            payload = staging.normalize(
                conn, [f.model_dump() for f in req.files], layer="http",
                actor=req.author)
        except staging.StagingError as exc:
            _fail(str(exc), exc.code)
        try:
            res = ops.checkin(conn, number, req.version, state=req.state,
                              summary=req.summary, ecn=req.ecn, author=req.author,
                              source=req.source, selector=req.selector,
                              files=payload,
                              bom=[l.model_dump() for l in req.bom])
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except vault.VaultError as exc:
            _fail(f"vault write failed: {exc}", 500)
        search.touch(conn, number)
    dispatch_event("checkin", {"number": number, "version": req.version,
                               "actor": req.author, "files": len(res["files"])})
    # …and the submit/decide events the shim's policy step produced, if any.
    return {"ok": True, **_decided(res)}


@app.post("/api/items/{number}/state")
def api_state(number: str, req: StateChange, version: str = "") -> dict:
    if req.state not in config.STATES:
        _fail(f"state must be one of {config.STATES}")
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            _fail(f"no revision for {number} {version or '(latest)'}", 404)
        # A revision that lives in git is written through git or not at all
        # (ops._stranded): refused outside `write`, a `[status]` note in it.
        try:
            git = ops.set_state_git(conn, rev, req.state, number=number,
                                    actor=req.actor, note=req.note) \
                if ops.registry_only_write(conn, rev, number=number) else {}
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        if not git:
            conn.execute("UPDATE revisions SET state=? WHERE id=?", (req.state, rev["id"]))
        db.add_event(conn, "state", item_id=rev["item_id"], revision_id=rev["id"],
                     actor=req.actor, data=ops._with_git(
                         {"from": rev["state"], "to": req.state,
                          "note": req.note}, git))
    dispatch_event("state", {"number": number, "version": rev["version"],
                             "from": rev["state"], "to": req.state, "actor": req.actor})
    return ops._with_git({"ok": True, "number": number, "version": rev["version"],
                          "state": req.state}, git)


# -- checkout / revise / propagate --------------------------------------------

class CheckoutReq(BaseModel):
    actor: str
    note: str = ""


class ReleaseReq(BaseModel):
    actor: str
    note: str = ""
    force: bool = Field(
        default=False,
        description="break ANOTHER actor's live lock. Humans only, and a reason "
                    "is required — a stale lock needs neither (ops."
                    "force_release_checkout)")
    reason: str = Field(default="", description="why the lock was broken; it is "
                                               "recorded in the event")


class ReviseReq(BaseModel):
    bump: str = Field(default="patch", pattern="^(major|minor|patch)$")
    summary: str = ""
    actor: str = ""
    rebind: str = Field(default="keep", pattern="^(keep|latest|imprecise)$",
                        description="how BOM child bindings carry forward")


class PropagateReq(BaseModel):
    version: str = ""
    actor: str = ""


@app.get("/api/items/{number}/checkout")
def api_checkout_get(number: str) -> dict:
    with db.connect() as conn:
        item = db.resolve_item(conn, number)
        if not item:
            _fail(f"unknown item: {number}", 404)
        return {"checkout": db.active_checkout(conn, item["id"])}


@app.post("/api/items/{number}/checkout")
def api_checkout(number: str, req: CheckoutReq) -> dict:
    """Check an item out — **advisory on a git-first item** (§1).

    Kept because scripts and agents still call it. On an item that lives in a
    repo it succeeds, records an event and blocks nothing: branches replaced
    locks, and two people working on one part get two `draft/*` branches and a
    merge rather than one of them waiting. The response says `advisory: true`.
    """
    with db.connect() as conn:
        try:
            return ops.claim_lock(conn, number, req.actor, note=req.note)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)


@app.get("/api/checkouts")
def api_checkouts(stale_only: bool = False) -> dict:
    """Every live checkout, oldest first — the recovery view.

    `released_at IS NULL` is the whole definition of "held", so a row here whose
    holding agent is gone IS a stranded item. Before this list existed the only
    way to find one was to try to write to it and read the 409."""
    with db.connect() as conn:
        rows = db.list_checkouts(conn, stale_only=stale_only)
    return {"stale_after_h": round(config.CHECKOUT_STALE_SECONDS / 3600, 1),
            "count": len(rows),
            "stale": sum(1 for r in rows if r["stale"]),
            "checkouts": rows}


@app.post("/api/items/{number}/checkout/release")
def api_checkout_release(number: str, req: ReleaseReq, request: Request) -> dict:
    """Release an item's checkout — yours, a stale one, or (force + a reason,
    humans only) somebody else's live one. ops.force_release_checkout owns the
    rule; this is one door to it."""
    who = _verified_actor(request) or req.actor
    with db.connect() as conn:
        try:
            res = ops.force_release_checkout(
                conn, number, actor=who, reason=req.reason or req.note,
                force=req.force)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, **res}


@app.post("/api/items/{number}/revise")
def api_revise(number: str, req: ReviseReq) -> dict:
    with db.connect() as conn:
        try:
            rev = ops.revise(conn, number, bump=req.bump, summary=req.summary,
                             actor=req.actor, rebind=req.rebind)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    dispatch_event("revise", {"number": number, "version": rev["version"],
                              "actor": req.actor})
    return {"ok": True, "number": number, "revision": rev}


@app.post("/api/items/{number}/propagate")
def api_propagate(number: str, req: PropagateReq) -> dict:
    with db.connect() as conn:
        try:
            created = ops.propagate(conn, number, req.version, req.actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number, *[c["number"] for c in created])
    for c in created:
        dispatch_event("revise", {"number": c["number"], "version": c["version"],
                                  "actor": req.actor, "via": "propagate"})
    return {"ok": True, "number": number, "created":
            [{"number": c["number"], "version": c["version"]} for c in created]}


class FilesUpload(BaseModel):
    files: list[CheckinFile]
    version: str = Field(default="", description="target DRAFT revision; empty = latest")
    actor: str = ""
    message: str = ""


@app.put("/api/items/{number}/files/{filename}")
async def api_file_upload_raw(number: str, filename: str, request: Request,
                              role: str = "", version: str = "",
                              message: str = "", derived: bool = False,
                              actor: str = "") -> dict:
    """Direct raw-byte upload for scripts: `curl -T part.stl
    'https://plm.../api/items/KML-PRT-1002/files/part.stl?role=artifact'`.

    Transport and storage are independent axes: this is just another way for
    bytes to ARRIVE (alongside inline JSON, links and staged upload handles, which
    exist so MCP agents never push bytes through context). The body streams
    through the same Sink as the inbox (capped, hashed once) into a temp file;
    WHERE the bytes land is plm/storage.py's business — vault or offload per
    the configured backend, same as every other path. Working-upload
    semantics: DRAFT revisions only (or derived=true for viz), git records
    every overwrite underneath."""
    _refuse_declared_oversize(request)
    fname = staging.safe_name(filename)
    try:
        sink = await run_in_threadpool(staging.Sink, staging.cache_path(fname),
                                       f"{fname}: the body")
        await _stream_into(sink, request)
    except staging.StagingError as exc:
        _staging_fail(exc)
    if not sink.size:
        await run_in_threadpool(sink.discard)
        _fail("empty body — PUT the file bytes (curl -T <file>)")
    entry = {"path": str(await run_in_threadpool(sink.keep)), "name": fname,
             "sha256": sink.sha256, "size": sink.size}
    who = _verified_actor(request) or actor or "api"
    files = [{"name": filename, "role": role, "data": None, "local": entry}]
    try:
        res = await run_in_threadpool(_raw_upload, number, files,
                                      version=version, actor=who,
                                      message=message, derived=derived)
    finally:
        await run_in_threadpool(staging.release_local, entry)
    dispatch_event("files", {"number": number, "version": res["version"],
                             "actor": who, "files": 1})
    return {"ok": True, **res}


def _raw_upload(number: str, files: list[dict], **kw) -> dict:
    with db.connect() as conn:
        try:
            res = ops.upload_files(conn, number, files, **kw)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except vault.VaultError as exc:
            _fail(f"vault write failed: {exc}", 500)
        search.touch(conn, number)
    return res


@app.post("/api/items/{number}/files")
def api_files_upload(number: str, req: FilesUpload) -> dict:
    """Working upload: files onto a DRAFT revision, no version bump. The
    'just for changes' path — git records every overwrite underneath."""
    with db.connect() as conn:
        try:
            payload = staging.normalize(
                conn, [f.model_dump() for f in req.files], layer="http",
                actor=req.actor)
        except staging.StagingError as exc:
            _fail(str(exc), exc.code)
        if not payload:
            _fail("no files given")
        try:
            res = ops.upload_files(conn, number, payload, version=req.version,
                                   actor=req.actor, message=req.message)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except vault.VaultError as exc:
            _fail(f"vault write failed: {exc}", 500)
        search.touch(conn, number)
    dispatch_event("files", {"number": number, "version": res["version"],
                             "actor": req.actor, "files": len(res["files"])})
    return {"ok": True, **res}


@app.post("/api/items/{number}/viz")
def api_viz_upload(number: str, req: FilesUpload) -> dict:
    """Attach derived visualization geometry (assembled STL etc.) to ANY
    revision — frozen included. Derived data is not a design change; this is
    how assemblies get a correct assembled render when their per-part STLs
    are print-oriented rather than placed."""
    with db.connect() as conn:
        try:
            payload = staging.normalize(
                conn, [f.model_dump() for f in req.files], layer="http",
                force_role="viz", actor=req.actor)
        except staging.StagingError as exc:
            _fail(str(exc), exc.code)
        if not payload:
            _fail("no files given")
        try:
            res = ops.upload_files(conn, number, payload, version=req.version,
                                   actor=req.actor, message=req.message, derived=True)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except vault.VaultError as exc:
            _fail(f"vault write failed: {exc}", 500)
    return {"ok": True, **res}


class ForkReq(BaseModel):
    new_name: str
    version: str = ""
    new_number: str = ""
    reason: str = ""
    actor: str = ""
    idempotency_key: str = Field(
        default="", description="retry safety on the auto-number path (see "
                                "POST /api/items)")


@app.post("/api/items/{number}/fork")
def api_fork(number: str, req: ForkReq) -> dict:
    with db.connect() as conn:
        try:
            res = ops.fork(conn, number, req.new_name, actor=req.actor,
                           version=req.version, new_number=req.new_number,
                           reason=req.reason,
                           idempotency_key=req.idempotency_key)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number, res["number"])
    dispatch_event("fork", {"number": res["number"], "from": number,
                            "actor": req.actor})
    return {"ok": True, **res}


class ItemPatch(BaseModel):
    name: str = Field(description="new descriptive name; the number never changes")
    actor: str = ""


@app.patch("/api/items/{number}")
def api_item_patch(number: str, req: ItemPatch) -> dict:
    """Rename an item. The name is item-level master data (identity, not
    revision data), so this is not a revision-controlled edit — but it is
    logged, and the response carries the old name for a context entry."""
    with db.connect() as conn:
        try:
            res = ops.rename_item(conn, number, req.name, actor=req.actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        search.touch(conn, number)
    return {"ok": True, **res}


class RenumberReq(BaseModel):
    new_number: str = Field(
        default="",
        description="explicit new number (validated); omit to let plm assign the "
                    "next free number in this item's prefix and kind class")
    note: str = Field(default="", description="extra reasoning for the context entry")
    actor: str = ""


@app.post("/api/items/{number}/renumber")
def api_renumber(number: str, req: RenumberReq) -> dict:
    """Give an item the number it should have had (ops.renumber_item).

    The repair tool for identity, not a routine edit: the OLD number keeps
    resolving (items.former_numbers), the vault folder is renamed with a git
    commit, and the reason is written into the item's context. BOM lines and
    relations reference item IDs, so structure is untouched."""
    with db.connect() as conn:
        try:
            res = ops.renumber_item(conn, number, new_number=req.new_number,
                                   actor=req.actor, note=req.note)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except vault.VaultError as exc:
            _fail(f"vault move failed: {exc}", 500)
        search.touch(conn, res["number"])
    dispatch_event("renumber", {"number": res["number"],
                                "former_number": res["former_number"],
                                "actor": req.actor})
    return {"ok": True, **res}


class BomLineAdd(BaseModel):
    child: str = Field(description="child item number, e.g. KML-ASM-4000")
    version: str = Field(default="", description="parent DRAFT revision; empty = latest")
    child_version: str = Field(default="", description="empty = imprecise (latest)")
    qty: float = 1
    unit: str = "EA"
    find: int | None = Field(default=None, description="find number; default next decade")
    notes: str = ""
    actor: str = ""


@app.post("/api/items/{number}/bom")
def api_bom_add(number: str, req: BomLineAdd) -> dict:
    """Add one occurrence to a DRAFT revision's BOM. Frozen revisions refuse —
    Revise ↑ first, so the structure change lands on a revision of its own."""
    with db.connect() as conn:
        try:
            res = ops.add_bom_line(conn, number, req.child, version=req.version,
                                   child_version=req.child_version, qty=req.qty,
                                   unit=req.unit, find=req.find, notes=req.notes,
                                   actor=req.actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, **res}


@app.delete("/api/bom/{line_id}")
def api_bom_delete(line_id: int, actor: str = "") -> dict:
    """Remove one occurrence from a DRAFT revision's BOM. On released structure
    this refuses: suppress the line instead (PATCH), which keeps the record."""
    with db.connect() as conn:
        try:
            res = ops.remove_bom_line(conn, line_id, actor=actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return {"ok": True, **res}


class BomLinePatch(BaseModel):
    """One BOM occurrence, edited in place.

    Every field is optional and each maps to exactly one ops verb, because the
    three kinds of edit MEAN different things and the event log has to say which
    happened:

      qty / unit / find / notes  -> ops.update_bom_line   (the numbers changed)
      new_child / child_version  -> ops.replace_bom_line  (the binding changed)
      suppressed                 -> ops.suppress_bom_line (absent in THIS
                                    configuration — not removal, and the one
                                    edit allowed on a frozen revision)
    """

    suppressed: bool | None = Field(
        default=None,
        description="150% BOM: the occurrence is part of the product but absent "
                    "in this configuration. NOT deletion — use DELETE for 'this "
                    "was never part of it'.")
    qty: float | None = None
    unit: str | None = None
    find: int | None = Field(default=None, description="move the line's find number")
    notes: str | None = None
    new_child: str = Field(default="", description="replace the child item")
    child_version: str | None = Field(
        default=None,
        description="rebind: a version to pin it precisely, '' to make it "
                    "imprecise (resolve to latest). Omit to leave alone.")
    actor: str = ""


@app.patch("/api/bom/{line_id}")
def api_bom_patch(line_id: int, req: BomLinePatch) -> dict:
    """Edit one BOM occurrence on a DRAFT: quantities, position, binding, or its
    suppression flag. Same ops functions the MCP calls."""
    out: dict = {"ok": True, "line": line_id}
    with db.connect() as conn:
        try:
            if req.suppressed is not None:
                out.update(ops.suppress_bom_line(
                    conn, line_id, suppressed=req.suppressed, actor=req.actor))
            if req.new_child or req.child_version is not None:
                # `out["line"]`, not `line_id`: a git-origin edit re-derives the
                # BOM rows from the commit, so the id moves under us.
                out.update(ops.replace_bom_line(
                    conn, out.get("line") or line_id, new_child=req.new_child,
                    child_version=req.child_version, actor=req.actor))
            if any(v is not None for v in (req.qty, req.unit, req.find, req.notes)):
                out.update(ops.update_bom_line(
                    conn, out.get("line") or line_id, qty=req.qty,
                    unit=req.unit, find=req.find, notes=req.notes,
                    actor=req.actor))
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    if len(out) == 2:
        _fail("nothing to change — pass suppressed, qty, unit, find, notes, "
              "new_child or child_version")
    return out


# -- release workflow: submit -> IN_REVIEW -> approve/reject -----------------

class SubmitReq(BaseModel):
    version: str = ""
    target_state: str = Field(
        default="PROTOTYPE", pattern="^(CONCEPT|DESIGN|PROTOTYPE|RELEASED)$",
        description="the maturity being asked for. `plm/policy.py` decides "
                    "whether it needs a human: prototype and below auto-merge, "
                    "RELEASED never does (GET /api/policy)")
    note: str = ""
    actor: str
    explode_waiver: str = Field(
        default="", description="submit an assembly that does not explode anyway, "
        "and say why (recorded on the approval). See GET /api/items/{n}/explode.")


class DecideReq(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    actor: str
    note: str = ""


def _decided(out: dict) -> dict:
    """Pop the ops layer's `_event`(s) and hand them to the integrations
    framework.

    The release verbs live in `plm.ops` (one code path, shared with the MCP and
    with the git write path); webhook delivery is an HTTP concern and stays
    here. `_events` is the plural case: a submit that POLICY merged on the spot
    (§2) is two things that happened — the request and the decision — and a
    downstream integration is entitled to both rather than to whichever one plm
    decided to keep."""
    for ev in out.pop("_events", None) or ([out.pop("_event")]
                                           if out.get("_event") else []):
        dispatch_event(ev[0], ev[1])
    out.pop("_event", None)
    return out


@app.post("/api/items/{number}/submit")
def api_submit(number: str, req: SubmitReq, request: Request) -> dict:
    """Submit a DRAFT for release: freezes it at IN_REVIEW and opens an
    approval. The version becomes a real record only when an approver says so —
    **unless policy says this maturity is automatic** (plm/policy.py), in which
    case this call IS the merge and the release tag.

    ⚠️ H4. Because of that, a submit at CONCEPT / DESIGN / PROTOTYPE is a
    release decision, and `_actor_for_write` requires a server-established
    identity for it under `PLM_AUTH_MODE=required`. The `actor` in the body is
    not enough to auto-merge with, and it used to be.
    """
    actor = _actor_for_write(request, req.actor)
    with db.connect() as conn:
        try:
            out = ops.submit_for_release(
                conn, number, version=req.version,
                target_state=req.target_state, note=req.note, actor=actor,
                explode_waiver=req.explode_waiver)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


@app.get("/api/policy")
def api_policy(project: str = "") -> dict:
    """The approval policy in force — read-only, deliberately (plm/policy.py).

    Who may say yes, at which maturity, and where that answer comes from. The
    decision bar on the proposal and release pages renders `label` off this, so
    a person about to click Approve can see whether the gate they are standing
    in front of is one plm would have let an agent through.

    There is no write route. Changing who may approve is a change to the gate,
    and a gate that the thing it gates can widen is not a gate — it is
    `python -m plm.policy --project … --target … --bots yes`.
    """
    with db.connect() as conn:
        rows = policy.table(project, conn=conn)
        custom = [dict(r) for r in conn.execute(
            "SELECT * FROM policy ORDER BY project, target_state")]
        from . import gitmode
        gm = gitmode.explain(project, conn)
    return {"project": project or "", "policy": rows, "rows": custom,
            # Stage 9: the policy applies only where git-first is `write`; in
            # `off`/`read` a person decides by the `approver` role, as on master.
            "git_mode": gm, "policy_applies": gm["mode"] == "write",
            "bot_approvals_env": os.environ.get("PLM_POLICY_BOT_APPROVALS",
                                                "none"),
            "note": "read-only: set it with `python -m plm.policy` "
                    "(plm/policy.py explains why it is not a route)"}


@app.get("/api/projects/{project}/readiness")
def api_project_readiness(project: str) -> dict:
    """What flipping this project to `read` / `write` would DO, and what blocks
    it — the report `python -m plm.gitmode --project <p> --readiness` prints
    (plm/readiness.py, stage 10). Read-only; the flip stays the CLI."""
    from . import readiness
    with db.connect() as conn:
        try:
            return readiness.report(conn, project)
        except LookupError as exc:
            _fail(str(exc), 404)


@app.get("/api/gitmode")
def api_gitmode(project: str = "", number: str = "", recheck: bool = False) -> dict:
    """PLM_GIT_MODE — read-only, like the policy (plm/gitmode.py).

    `project=''` answers for the instance; otherwise for that project. Each
    answer names its SOURCE (registry / env / db row), because "why is there no
    repo page?" has to be answerable. Set it with `python -m plm.gitmode`."""
    from . import gitmode
    with db.connect() as conn:
        if number:
            # The git server's pre-receive hook asks this (it never opens the
            # database): may a push land on this item?
            it = db.resolve_item(conn, number)
            if not it:
                _fail(f"unknown item: {number}", 404)
            e = gitmode.explain(it["project"] or "", conn)
            return {"number": it["number"], "project": it["project"] or "",
                    **e, "push": e["mode"] == "write"}
        projects = [r["project"] for r in conn.execute(
            "SELECT DISTINCT project FROM items WHERE project<>'' ORDER BY 1")]
        out = {"instance": gitmode.explain("", conn),
               "project": gitmode.explain(project, conn) if project else None,
               "projects": {p: gitmode.explain(p, conn)["mode"] for p in projects},
               "modes": list(gitmode.MODES),
               "note": "read-only: set it with `python -m plm.gitmode`"}
    # Stage 15: do the clone/web/public bases this instance hands out answer
    # from inside its own network namespace? (plm/addresses.py) `?recheck=1`
    # probes now. Absent where git is off everywhere (master's shape).
    if gitmode.any_serves():
        from . import addresses
        out["addresses"] = addresses.check() if recheck else addresses.state()
    return out


@app.get("/api/approvals")
def api_approvals(pending: bool = True, limit: int = 50) -> list[dict]:
    """The release queue. Each row carries its `context` — comments attached to
    THAT submission (plm/context.py): review remarks, the condition an approver
    wants met. The decision itself stays on the approval row; this is the
    discussion around it, and the queue is short enough to ship it inline.

    `thumb` is the submitted revision's render URL, or null when it has no
    geometry — a document revision in the queue draws its placeholder without a
    request."""
    with db.connect() as conn:
        where = "WHERE a.decision=''" if pending else ""
        rows = [dict(r) for r in conn.execute(
            f"""SELECT a.*, r.version, r.state, r.summary,
                       i.id AS item_id, i.number, i.name, i.kind
                FROM approvals a
                JOIN revisions r ON r.id=a.revision_id
                JOIN items i ON i.id=r.item_id
                {where} ORDER BY a.requested_at DESC LIMIT ?""", (limit,))]
        counts = context.counts(conn, "approval", [r["id"] for r in rows])
        from . import gitmode, proposals
        modes: dict = {}
        for r in rows:
            # Stage 9 (additive): the queue asks the repo for draft stats only
            # where there IS a repo — an `off` project's row must not 404.
            proj = conn.execute("SELECT project FROM items WHERE id=?",
                                (r["item_id"],)).fetchone()["project"] or ""
            if proj not in modes:
                modes[proj] = gitmode.mode_for(proj, conn)
            r["git_mode"] = modes[proj]
            r["comments"] = counts.get(r["id"], 0)
            r["context"] = context.entries(conn, "approval", r["id"])["entries"] \
                if r["comments"] else []
            # STAGE 13 (additive): the proposal this round belongs to, with its
            # open / BLOCKING thread counts — "impossible to approve without
            # seeing them" (PLM_MARKUP §5, PLM_REVIEW_MODEL §2.4).
            prop = proposals.for_approval(conn, r["id"])
            r["proposal"] = proposals.brief(conn, prop) if prop else None
        # `revision_id` on the approval row IS the thing being pictured.
        return thumbs.Presence(conn).rows(rows)


@app.post("/api/approvals/{approval_id}/decide")
def api_decide(approval_id: int, req: DecideReq, request: Request) -> dict:
    """The gate. Approvers only; approval runs the workflow (revision moves to
    its target state — the moment the version becomes real). Rejection sends
    it back to DRAFT for more work.

    The decider's identity is established by the server (OIDC), not taken from
    the request body: "you need to know who is approving things".
    """
    actor = _require_verified_actor(request, req.actor)
    with db.connect() as conn:
        try:
            out = ops.decide_approval(
                conn, approval_id, decision=req.decision, actor=actor,
                note=req.note,
                identity_via=(getattr(request.state, "plm_identity", None)
                              or {}).get("via", "unverified"))
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


class WithdrawReq(BaseModel):
    actor: str
    note: str = ""


@app.post("/api/approvals/{approval_id}/withdraw")
def api_withdraw(approval_id: int, req: WithdrawReq, request: Request) -> dict:
    """Pull a submission back: the requester (or an approver) cancels the
    pending approval and the revision returns to DRAFT for more work — the
    'I looked at it, tell the bot to revise' path.

    Also a decision of record (it un-freezes a revision), so it needs the same
    verified identity as `decide`.
    """
    actor = _require_verified_actor(request, req.actor)
    with db.connect() as conn:
        try:
            out = ops.withdraw_approval(conn, approval_id, actor=actor,
                                        note=req.note)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


# -- RELEASE: promotion of an existing revision (§2b) -------------------------
#
# The second object. `POST /api/items/{n}/submit` asks "is this change right?";
# these ask "is this revision ready to build against?" about a revision that
# already exists, with no file change and no new version. Nothing here moves a
# tag or writes a commit on `main` — the decision is a `[status]` note.

class ReleaseReqBody(BaseModel):
    version: str = ""
    to_maturity: str = Field(default="released",
                             pattern="^(released|obsolete|hold)$")
    notes: str = ""
    actor: str


class ReleaseDecide(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    actor: str
    note: str = ""
    attest_rest: bool = Field(
        default=True,
        description="attest the checklist lines nobody has signed yet, in the "
                    "decider's name. Each becomes its own row — this is the one "
                    "click, not a shortcut past the workflow.")


class SignoffBody(BaseModel):
    kind: str = Field(pattern="^(approval|checklist)$")
    item: str = ""
    verdict: str = ""
    note: str = ""
    actor: str
    # stage 10: attest a checklist line BY REFERENCE — "GL-CPN-0001@v0.1.0", a
    # passing claim on the exact commit being released (release.EVIDENCE)
    evidence: str = ""


class NotesBody(BaseModel):
    notes_md: str
    actor: str = ""


@app.post("/api/items/{number}/releases")
def api_release_request(number: str, req: ReleaseReqBody) -> dict:
    """Ask for an existing tagged revision to be promoted. No file changes.

    A bot may request one — requesting is not deciding — and normally should:
    the agent is what knows the part was printed and fitted."""
    with db.connect() as conn:
        try:
            out = ops.request_release(conn, number, version=req.version,
                                      to_maturity=req.to_maturity,
                                      notes=req.notes, actor=req.actor)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


@app.get("/api/releases")
def api_releases(pending: bool = True, number: str = "",
                 limit: int = 50) -> list[dict]:
    """Pending promotions — the other half of the approvals queue."""
    with db.connect() as conn:
        return ops.list_releases(conn, pending=pending, number=number,
                                 limit=limit)


@app.get("/api/items/{number}/releases/by-tag/{tag:path}")
def api_release_by_tag(number: str, tag: str) -> dict:
    """`/<N>/releases/tag/<vX.Y.Z>` (stage 22): the NEWEST release record of
    that tag on that item — a tag can be promoted more than once, and the page
    a tag link names is the latest promotion; `/<N>/releases/<id>` is exact."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rel.id, rel.tag, rel.version, rel.state FROM releases rel "
            "JOIN items i ON i.id=rel.item_id WHERE i.number=? AND rel.tag=? "
            "ORDER BY rel.requested_at DESC, rel.id DESC LIMIT 1",
            (number, tag)).fetchone()
    if not row:
        _fail(f"{number} has no release record for tag {tag!r}", 404)
    return {**dict(row), "number": number,
            "page": webroutes.release(number, row["id"], row["tag"])}


@app.get("/api/items/{number}/pull/{n}")
def api_resolve_pull(number: str, n: int) -> dict:
    """What `/<N>/pull/<n>` names (stage 22, plm/webroutes.py).

    GitHub numbers a repo's pull requests per repo; plm has TWO global
    sequences that can both appear there — proposals (one item's draft under
    review) and changes (the project-level pull request across items). So:
      * <N> is a member of CHG-<n>  → the change, scoped to <N>;
      * else proposal #<n> is on <N> → that proposal;
      * else 404.
    When both hold and are DIFFERENT subjects (proposal #n on <N> is not in
    CHG-n), the change wins and `also_proposal` says so; the explicit
    `/<N>/pull/p<n>` always means the proposal."""
    from . import changes as chmod, proposals as pmod
    with db.connect() as conn:
        pmod.ensure(conn)
        chmod.ensure(conn)
        prop = conn.execute(
            "SELECT p.id, p.change_id FROM proposals p JOIN items i "
            "ON i.id=p.item_id WHERE p.id=? AND i.number=?",
            (n, number)).fetchone()
        member = conn.execute(
            "SELECT p.id FROM proposals p JOIN items i ON i.id=p.item_id "
            "JOIN changes c ON c.id=p.change_id WHERE c.id=? AND i.number=? "
            "ORDER BY p.id DESC LIMIT 1", (n, number)).fetchone()
    if member:
        out = {"kind": "change", "change": f"CHG-{n}", "member": number,
               "proposal": member["id"], "page": chmod.page(n)}
        if prop and prop["change_id"] != n:
            out["also_proposal"] = prop["id"]
        return out
    if prop:
        return {"kind": "proposal", "proposal": prop["id"],
                "change": f"CHG-{prop['change_id']}" if prop["change_id"] else "",
                "page": pmod.page(number, prop["id"])}
    _fail(f"{number} has no proposal #{n} and is not a member of CHG-{n}", 404)


@app.get("/api/releases/{release_id}")
def api_release(release_id: int) -> dict:
    """One release: the workflow as a signable checklist, who signed what and
    when, the pin closure with maturity marks, and what is still missing."""
    with db.connect() as conn:
        try:
            return ops.get_release(conn, release_id)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)


@app.post("/api/releases/{release_id}/signoffs")
def api_release_signoff(release_id: int, req: SignoffBody,
                        request: Request) -> dict:
    """Sign one line of the workflow.

    A CHECKLIST line is a statement of fact and an agent that can prove one
    should attest it (recorded as `signer_kind = bot`, with its evidence in the
    note). An APPROVAL is a decision, and `plm/policy.py` says who may make one.
    """
    actor = _verified_actor(request) or req.actor
    with db.connect() as conn:
        try:
            out = ops.sign_release(
                conn, release_id, kind=req.kind, item=req.item,
                verdict=req.verdict, note=req.note, actor=actor,
                identity_via=(getattr(request.state, "plm_identity", None)
                              or {}).get("via", "unverified"),
                evidence=req.evidence)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


@app.post("/api/releases/{release_id}/decide")
def api_release_decide(release_id: int, req: ReleaseDecide,
                       request: Request) -> dict:
    """Approve or reject the promotion — the one click for somebody who has in
    fact done all of it. Every checklist line still becomes its own signoff row
    in their name."""
    actor = _require_verified_actor(request, req.actor)
    with db.connect() as conn:
        try:
            out = ops.decide_release(
                conn, release_id, decision=req.decision, actor=actor,
                note=req.note, attest_rest=req.attest_rest,
                identity_via=(getattr(request.state, "plm_identity", None)
                              or {}).get("via", "unverified"))
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


@app.post("/api/releases/{release_id}/withdraw")
def api_release_withdraw(release_id: int, req: WithdrawReq,
                         request: Request) -> dict:
    actor = _require_verified_actor(request, req.actor)
    with db.connect() as conn:
        try:
            out = ops.withdraw_release(conn, release_id, actor=actor,
                                       note=req.note)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


@app.get("/api/releases/{release_id}/notes-draft")
def api_release_notes_draft(release_id: int) -> dict:
    """The generated release notes: what merged since the previous RELEASED
    revision, the pin bumps with the child's own commit subjects, and the
    attribute deltas. Generated, then edited — nobody writes release notes from
    a blank page, and nobody should ship the machine's first draft either."""
    from . import release as release_mod
    with db.connect() as conn:
        try:
            rel = ops._release_row(conn, release_id)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        pins = release_mod.pin_closure(rel["number"], rel["tag_sha"]) \
            if rel["tag_sha"] else []
        draft = release_mod.notes_draft(conn, {**rel, "pins": pins})
    return {"release": release_id, "notes_md": draft,
            "stored_md": rel["notes_md"] or "",
            "note": "PUT it back with POST /api/releases/{id}/notes once you "
                    "have edited it"}


@app.post("/api/releases/{release_id}/notes")
def api_release_notes(release_id: int, req: NotesBody) -> dict:
    with db.connect() as conn:
        try:
            rel = ops._release_row(conn, release_id)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        if rel["state"] == "approved":
            _fail("this release is approved and its notes are inside the frozen "
                  "evidence document — editing them now would make the recorded "
                  "sha256 a claim about something that no longer exists.", 409)
        conn.execute("UPDATE releases SET notes_md=? WHERE id=?",
                     (req.notes_md, release_id))
        db.add_event(conn, "release.notes", item_id=rel["item_id"],
                     revision_id=rel["revision_id"], actor=req.actor,
                     data={"release": release_id, "chars": len(req.notes_md)})
    return {"ok": True, "release": release_id, "chars": len(req.notes_md)}


@app.get("/api/releases/{release_id}/evidence")
def api_release_evidence(release_id: int, download: bool = False):
    """The frozen evidence document, byte for byte as it was hashed.

    Never re-serialised on the way out: serving a freshly rendered document
    would make `evidence_sha256` a claim about something nobody can check.
    """
    from . import release as release_mod
    with db.connect() as conn:
        try:
            rel = ops._release_row(conn, release_id)
            raw, digest = release_mod.read_evidence(rel)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
        except release_mod.ReleaseError as exc:
            _fail(str(exc), exc.code)
    headers = {"X-Plm-Evidence-Sha256": digest,
               "X-Plm-Evidence-Recorded": rel["evidence_sha256"] or ""}
    if download:
        headers["content-disposition"] = (
            f'attachment; filename="{rel["number"]}-v{rel["version"]}-'
            f'release-{release_id}.json"')
    return Response(content=raw, media_type="application/json",
                    headers=headers)


@app.get("/api/releases/workflows")
def api_release_workflows() -> dict:
    """The RELEASE_WORKFLOWS registry — what each maturity transition asks for."""
    from . import release as release_mod
    return {"workflows": release_mod.registry(),
            "generic": release_mod.GENERIC,
            "note": "a transition nobody registered gets the generic workflow "
                    "and is marked `registered: false`"}


# -- the internal index endpoint: ONE writer ---------------------------------
#
# The git container runs `receive-pack`, so it is where a push lands — but it
# must NOT open the registry read-write, because then there would be two
# processes writing the same SQLite file and the "one code path per behaviour"
# rule would be decorative. So the post-receive hook calls this, plm does the
# write, and `plm/gitindex.py` stays the single writer of design facts.
#
# Guarded by a shared secret rather than a user: this is service-to-service,
# both ends are inside the compose network, and the token lives only under
# /srv/plm/ (never in the worktree, never in a log line).

class GitIndexReq(BaseModel):
    number: str
    commit: str = ""
    ref: str = ""
    actor: str = ""
    action: str = Field(default="index", pattern="^(index|withdraw)$")
    origin: str = "push"
    # the ref's previous tip when this push REPLACED one (stage 11b): what it
    # orphaned is kept if something still pins it (plm/gitkeep.guard)
    old: str = Field(default="", pattern="^([0-9a-f]{40})?$")
    # stage 14: `git push -o change=new -o title="…"` / `-o change=CHG-n`
    change: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=200)
    # stage 19: `git push -o base=<ref|sha>` — the REVIEW BASELINE, when the
    # inferred one (the draft/tag the head descends from) is not the right one
    base: str = Field(default="", max_length=200)


@app.post("/api/git/index")
def api_git_index(req: GitIndexReq, request: Request) -> dict:
    """Index a commit as a revision, or retire the DRAFT a deleted branch left.

    The response is what the pusher sees as `remote:` output, so it is shaped to
    be read: what landed, at what version, how many files, and every BOM line
    with the version its gitlink resolved to."""
    want = config.GIT_INDEX_TOKEN
    got = (request.headers.get("authorization", "")
           .removeprefix("Bearer ").removeprefix("bearer ").strip())
    if not want or not hmac.compare_digest(got, want):
        _fail("this endpoint is plm's own index hook and needs its token", 403)
    from . import gitindex, proposals
    with db.connect() as conn:
        try:
            if req.action == "withdraw":
                # the proposal first: withdraw_draft deletes the revision row
                # it is found by (stage 13 — it closes as `withdrawn` and its
                # refs/plm/proposals/<n>/head keeps the commits)
                closed = proposals.on_branch_deleted(conn, req.number, req.ref,
                                                     actor=req.actor)
                out = gitindex.withdraw_draft(conn, req.number, req.ref,
                                              req.commit)
                if closed:
                    out["proposal"] = closed
                return out
            # ops.index_draft = gitindex.index_commit + the proposal hook (a
            # head move: follow it, dismiss stale reviews, re-open a reject)
            out = ops.index_draft(conn, req.number, req.commit, ref=req.ref,
                                  actor=req.actor, origin=req.origin,
                                  state=gitindex.DRAFT_STATE, old=req.old,
                                  base=req.base)
            if req.change and (out.get("proposal") or {}).get("id"):
                # STAGE 14: the agent's native door into a CHANGE. The push
                # landed either way; a bad `-o change=` is said in the
                # remote: output, not by un-indexing a valid commit.
                from . import changes
                try:
                    out["change"] = changes.attach_push(
                        conn, req.number, out["proposal"]["id"], req.change,
                        title=req.title, actor=req.actor)
                except changes.ChangeError as exc:
                    out["change_error"] = str(exc)
            elif (out.get("proposal") or {}).get("change"):
                from . import changes
                out["change"] = changes.brief(
                    conn, changes.row(conn, out["proposal"]["change_id"]))
        except gitindex.IndexError_ as exc:
            _fail(str(exc), exc.code)
        except ops.OpsError as exc:
            _fail(str(exc), exc.code)
    return _decided(out)


@app.get("/api/users")
def api_users() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY username")]


# -- admin: the full search rebuild, as an explicit act --------------

#: L3's in-flight guard. A module-level lock rather than a flag, because the
#: two places that release it are a `finally` and a thread's `finally`.
_reindex_lock = threading.Lock()


@app.post("/api/admin/reindex")
def api_admin_reindex(background: bool = True) -> dict:
    """Rebuild the WHOLE search index (plm/search.py).

    Writes no longer do this — they reindex only the items they touched, because
    the full pass reads every attached markdown file off the synced vault and
    measured 41 s on production, which is what put a check-in past a 30 s client
    timeout. The full rebuild is still the repair for an index that drifted (an
    unreadable file, a schema change, an import), so it stays — as something
    somebody asks for.

    background=true (the default) returns immediately and rebuilds on a thread,
    the same pattern the startup warm uses. Either way the writer lock is only
    taken for the final delete+insert burst."""
    # ⛔ L3 — ONE AT A TIME. This spawned an unbounded NEW THREAD per call, each
    # doing a full vault-wide reindex (41 s measured on production), with no
    # dedupe and no in-flight guard — so N requests were N concurrent full
    # passes over the synced vault. It is authenticated now (C2), which is the
    # bigger half; this is the other one.
    if not _reindex_lock.acquire(blocking=False):
        _fail("a full reindex is already running on this instance. It takes "
              "about a minute and\nreplaces the whole index at the end, so a "
              "second one would do the same work twice.", 409)
    if not background:
        try:
            with db.connect() as conn:
                return {"ok": True, "rows": search.reindex(conn),
                        "background": False}
        finally:
            _reindex_lock.release()

    def run() -> None:
        try:
            with db.connect() as conn:
                search.reindex(conn)
        except Exception as exc:                               # noqa: BLE001
            print(f"plm: admin reindex failed: {exc}", flush=True)
        finally:
            _reindex_lock.release()

    threading.Thread(target=run, daemon=True, name="plm-reindex").start()
    return {"ok": True, "background": True,
            "note": ("full reindex started on a background thread — reads every "
                     "attached markdown file off the vault, so allow ~1 min on "
                     "production. GET /api/search keeps serving the old index "
                     "until the burst at the end swaps it.")}


# -- integrations (PLM stays PLM; downstream registers here) ------------------

class IntegrationCreate(BaseModel):
    name: str
    url: str
    action_label: str = ""
    on_events: str = Field(default="", description="csv of event types: checkin,revise,state")
    enabled: bool = True
    config: dict = Field(default_factory=dict)


@app.get("/api/integrations")
def api_integrations() -> list[dict]:
    """Registered downstream integrations — **redacted** (C1).

    `actions.public_row` keeps scheme+host+path so a person can see where this
    plm talks to, and drops the query string, any userinfo and every
    secret-shaped `config` key. This endpoint served the real the shop-floor service API
    key, in full, unauthenticated, on 0.0.0.0, for the life of the POC.
    """
    from . import actions as actionmod
    with db.connect() as conn:
        return [actionmod.public_row(r) for r in
                conn.execute("SELECT * FROM integrations ORDER BY name")]


@app.post("/api/integrations")
def api_integration_create(req: IntegrationCreate, request: Request) -> dict:
    """Register or update an integration. ⛔ APPROVERS ONLY, AND VALIDATED (C3).

    This was unauthenticated and unvalidated, and its `ON CONFLICT` branch could
    set `on_events` on the EXISTING real printer row — one POST from "nothing
    fires" to "every check-in calls the shop-floor service". It now needs the `approver`
    role (registering a place plm will send your designs is an administrative
    act, not an editing one) and the URL must pass the same
    `actions.check_target` the wire does.
    """
    from . import actions as actionmod
    actor = _require_verified_actor(request, "")
    with db.connect() as conn:
        if not db.user_has_role(conn, actor, "approver"):
            _fail(f"{actor} does not hold the `approver` role. Registering an "
                  "integration says where plm may\nsend your designs — it is "
                  "administration, not editing.", 403)
        try:
            actionmod.check_target(req.url)
        except actionmod.SendError as exc:
            _fail(str(exc), exc.code)
        if "?" in req.url:
            _fail("an integration URL must not carry a query string: that is "
                  "where the credential\nended up last time "
                  "(the security review C1). Put the secret in a "
                  "file\nand name it in config.api_key_file; plm sends it as a "
                  "header.", 400)
        conn.execute(
            """INSERT INTO integrations (name,url,action_label,on_events,enabled,config,created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET url=excluded.url,
                 action_label=excluded.action_label, on_events=excluded.on_events,
                 enabled=excluded.enabled, config=excluded.config""",
            (req.name, req.url, req.action_label, req.on_events,
             int(req.enabled), json.dumps(req.config), db.now()))
        db.add_event(conn, "integration.registered", actor=actor,
                     data={"name": req.name,
                           "host": actionmod.host_of(req.url),
                           "on_events": req.on_events,
                           "enabled": bool(req.enabled)})
        row = conn.execute("SELECT * FROM integrations WHERE name=?", (req.name,)).fetchone()
    return actionmod.public_row(row)


class FileActionReq(BaseModel):
    actor: str = ""


class InvokeReq(BaseModel):
    number: str
    version: str = ""
    actor: str = ""
    #: ⭐ WHICH FILE, when the revision carries more than one printable one.
    #: A revision with two artifact STLs (a jig and its test coupon, say) cannot
    #: be sent without saying which — the downstream refuses with
    #: `ambiguous_artifact` and lists the candidates. This is how the answer gets
    #: back, and it is passed straight through rather than interpreted here: the
    #: PLM does not know what "printable" means, which is the whole point of the
    #: integration boundary.
    path: str = ""


@app.post("/api/files/{file_id}/actions/{action}")
def api_file_action(file_id: int, action: str, req: "FileActionReq") -> dict:
    """⭐ Run a registered action on ONE named file.

    The counterpart to the revision-level button, and the answer to its central
    weakness: that button hands over a revision and lets the integration guess
    which file was meant. Here you clicked a row, so the file is not in question
    — which is why this path can print the second of two meshes, or a `viz`
    mesh, or anything else the integration is willing to accept.

    ⛔ The action must be one the REGISTRY offers for this file. Not a
    convenience check: it is what stops `/actions/print` becoming a way to post
    arbitrary files at arbitrary integrations by guessing at names.
    """
    from . import actions as actionmod

    spec = actionmod.known(action)
    if not spec:
        _fail(f"no such action: {action}", 404)
    # ⭐ Only an INTEGRATION action is performed here. `client` verbs (open,
    # download, copy a link) never reach the server, and `op` verbs are the
    # registry's own write endpoints — `PUT /api/files/{id}/role`,
    # `DELETE /api/files/{id}` — which already exist and already refuse
    # correctly. Routing them through here would be a second door to the same
    # room. (Same rule in plm/gitapi.py's repo-side twin.)
    if spec.get("type", "integration") != "integration":
        _fail(f"{action} is a {spec.get('type')} action — it is performed by "
              "the caller, not by this endpoint", 400)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            _fail(f"no file {file_id}", 404)
        f = db._file_out(dict(row))
        offered = {a["name"]: a for a in f.get("actions") or []}
        if action not in offered:
            _fail(f"{spec['label']} does not apply to this file", 409)
        if not offered[action]["enabled"]:
            _fail(offered[action]["reason"] or "not available for this file", 409)
        rev = conn.execute("SELECT * FROM revisions WHERE id=?",
                           (row["revision_id"],)).fetchone()
        if not rev:
            _fail("this file's revision is missing", 409)
        item_row = conn.execute("SELECT number FROM items WHERE id=?",
                                (rev["item_id"],)).fetchone()
        if not item_row:
            _fail("this file's item is missing", 409)
        item = {"number": item_row["number"]}
    # Reuse the integration path wholesale, naming the file — one transport, one
    # audit trail, one place that knows how to talk to each downstream service.
    return api_integration_invoke(
        spec["integration"],
        InvokeReq(number=item["number"], version=rev["version"],
                  actor=req.actor or "", path=str(row["path"] or "")))


@app.post("/api/integrations/{name}/invoke")
def api_integration_invoke(name: str, req: InvokeReq) -> dict:
    """User-triggered action: POST the revision's context (metadata + artifact
    URLs) to the integration. The integration does its own thing from there —
    the PLM's job ends at handing over controlled data."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM integrations WHERE name=? AND enabled=1",
                           (name,)).fetchone()
        if not row:
            _fail(f"no enabled integration named {name}", 404)
        rev = db.find_revision(conn, req.number, req.version)
        if not rev:
            _fail(f"no revision for {req.number} {req.version or '(latest)'}", 404)
        item = db.get_item(conn, req.number)
        files = [{"path": f["path"], "size": f["size"], "role": f["role"],
                  "url": (f"/api/blob?id={f['id']}"
                          if f.get("backend") not in ("", None, "vault")
                          else f"/api/blob?path={f['path']}")}
                 for f in item["files"].get(rev["id"], [])]
    payload = {"action": name, "number": req.number, "name": item["name"],
               "version": rev["version"], "state": rev["state"],
               "actor": req.actor, "files": files}
    if req.path:
        payload["path"] = req.path
    # ⛔ ONE OUTBOUND CALL (C3). The dry-run guard, the scheme/host allowlist,
    # the timeout and the credential-from-a-file all live in `actions.send()`,
    # which `dispatch_event` and the repo-side twin (plm/gitapi.py) also go
    # through. This handler used to hold its own copy of the guard and
    # `dispatch_event` held none.
    from . import actions as actionmod
    try:
        res = actionmod.send(row, payload, what=f"{req.number} {rev['version']}",
                             actor=req.actor)
    except actionmod.SendError as exc:
        # ⛔⛔ A DOWNSTREAM REFUSAL IS NOT A TRANSPORT FAILURE, and collapsing
        # the two threw away the only useful part of it: the shop-floor service answers a
        # 409 with a precise reason — "this revision's only STL is a `viz`
        # composite, not a printable artifact" — and that arrived at the button
        # as "integration print unreachable: 409 Client Error", which is both
        # wrong and unactionable. 30 items in this vault refuse for reasons
        # exactly that specific. `send()` keeps the status and the body, so the
        # DETAILS travel too and the UI can turn a candidate list into a picker.
        body = exc.body if isinstance(exc.body, dict) else {}
        with db.connect() as conn:
            db.add_event(
                conn,
                "integration.refused" if 400 <= exc.code < 500
                else "integration.failed", actor=name,
                data={"number": req.number, "status": exc.status,
                      "host": actionmod.host_of(row["url"]),
                      "code": str(body.get("code") or "")[:60],
                      "error": str(exc)[:300]})
        if 400 <= exc.code < 500:
            raise HTTPException(status_code=exc.code, detail={
                "error": str(exc), "code": body.get("code") or "",
                "details": body.get("details") or {}}) from None
        _fail(str(exc), exc.code)
    if res.get("dry_run"):
        return {"ok": True, "dry_run": True, "integration": name,
                "to": res["to"], "payload": payload,
                "result": res["result"]}
    result = res["result"]
    with db.connect() as conn:
        db.add_event(conn, "integration.invoked", item_id=item["id"], revision_id=rev["id"],
                     actor=req.actor or name, data={"integration": name})
    # ⭐ Where to go and look. The config already records the integration's own
    # landing page; handing it back means the toast can offer a link instead of
    # leaving "it is on the queue" as a claim you have to go and verify.
    try:
        config = json.loads(row["config"] or "{}")
    except (ValueError, TypeError):
        config = {}
    return {"ok": True, "integration": name, "result": result,
            "link": config.get("queue") or ""}


# -- web UI -------------------------------------------------------------------
# THE SHELL MUST NOT BE HEURISTICALLY CACHED.
#
# Starlette's StaticFiles sends `etag` and `last-modified` and NO
# `Cache-Control`. With no directive, browsers fall back to HEURISTIC caching:
# they may reuse a response with no conditional request at all, commonly for
# about 10% of the age since last-modified. For a single-page app that means the
# SHELL goes stale, and a stale shell is the worst thing to cache, because
# everything else is loaded BY it.
#
# This is the second half of a fix whose first half was not enough. index.html
# loads app.js with a `?v=` cache-buster precisely so code changes land
# immediately — but a buster that lives INSIDE index.html cannot help when
# index.html is itself the thing served from cache. The symptom was a black 3D
# viewer that a hard reload "fixed", which reads as a viewer bug and is not one.
#
# `no-cache` does NOT mean "do not store" — it means REVALIDATE BEFORE USE. The
# etag stays, so the normal case is a 304 with no body: correctness for the price
# of one conditional request. Only the shell and the entry script need it; blobs
# and thumbnails are content-addressed or served by their own routes.
_NO_CACHE = {".html", ".js", ".css"}


# REAL PATHS (stage 22, plm/webroutes.py). The UI's pages are `/CID-ASM-0001`,
# `/CID-ASM-0001/compare/a...b`, `/pulls/380` — no file in web/ has those
# names, so StaticFiles alone would 404 them. The order is:
#   1. a real file in web/ (app.js, vendor/…, favicon.svg) is served as a file;
#   2. an old page path (`/changes/CHG-n`, `/item/N/…`) is a 301 to its new
#      one (webroutes.legacy_redirect — the one table);
#   3. a UI path (first segment an item number or a fixed word) gets the SHELL,
#      index.html, with the same no-cache rule — the router in the page reads
#      location.pathname and draws the page;
#   4. anything else is a small HTML 404 (JSON for a non-HTML client), NOT the
#      shell: a typo must not spin up the app and show an empty page.
# `/api/…`, `/mcp…`, `/auth/…` never get here — their routes are registered
# before this mount — and their unknown paths stay FastAPI's JSON 404.
NOT_FOUND_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not found · plm</title>
<style>body{font:15px system-ui,sans-serif;margin:3rem auto;max-width:32rem;
padding:0 1rem;color:#222;background:#fff}a{color:#0b5fff}code{font-size:13px}</style>
</head><body>
<h1>Not found</h1>
<p>plm has no page at <code>__PATH__</code>.</p>
<p>A page here is <code>/&lt;ITEM-NUMBER&gt;/…</code> (e.g. <code>/CID-ASM-0001</code>),
<code>/pulls/&lt;n&gt;</code>, <code>/release-sets/&lt;n&gt;</code>, <code>/registry</code>,
<code>/approvals</code>, <code>/search?q=…</code> or <code>/projects/&lt;p&gt;</code>.</p>
<p><a href="/">plm home</a></p>
</body></html>
"""


def _not_found_page(path: str, accept: str) -> Response:
    from html import escape
    # the machine plane keeps FastAPI's JSON 404 whatever the browser accepts
    machine = path.startswith(("/api/", "/mcp", "/auth/"))
    if "text/html" in (accept or "") and not machine:
        return HTMLResponse(NOT_FOUND_HTML.replace("__PATH__", escape(path[:300])),
                            status_code=404, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Not Found"}, status_code=404)


class _ShellStatic(StaticFiles):
    async def get_response(self, path: str, scope):
        from starlette.exceptions import HTTPException as _StarletteHTTP
        try:
            resp = await super().get_response(path, scope)
        except _StarletteHTTP as exc:
            if exc.status_code != 404:
                raise
            url_path = scope.get("path") or "/"
            moved = webroutes.legacy_redirect(url_path)
            if moved:               # an old page path (webroutes._LEGACY)
                q = (scope.get("query_string") or b"").decode("latin-1")
                return RedirectResponse(moved + (f"?{q}" if q else ""), 301)
            if not webroutes.is_ui_path(url_path):
                hdrs = dict((k.decode("latin-1").lower(), v.decode("latin-1"))
                            for k, v in scope.get("headers") or [])
                return _not_found_page(url_path, hdrs.get("accept", ""))
            path = "index.html"
            resp = await super().get_response(path, scope)
        if Path(path).suffix.lower() in _NO_CACHE or path in ("", "."):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


if WEB_DIR.is_dir():
    app.mount("/", _ShellStatic(directory=str(WEB_DIR), html=True), name="web")
