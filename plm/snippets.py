"""Disposable 3D snippets — "hey, do you like this?"

A snippet is a temporary STL that an agent can hand a human as a *link*: the
3D equivalent of pasting a screenshot into chat. It is explicitly NOT a part.
No item, no revision, no `files` row, no git commit, no approval — just bytes
on plm's own disk under an unguessable token (CACHE_DIR/snippets/<token>/), a
row saying when to throw them away, and a viewer page (`/snip/<token>`) that reuses the registry's three.js
overlay.

Why keep this separate from the registry at all? Because a tracked part is a
promise to keep the bytes and their history forever, and most show-and-tell
geometry does not deserve that promise. Forcing "do you like this shape?"
through create-item / checkin / revise pollutes the registry with numbers
nobody will ever order, and the cost of that noise is paid by every future
search. So the rule is a hard one:

    quick look → share_3d()          (this module, expires by itself)
    real part  → checkin()           (numbered, versioned, kept)

Lifecycle
---------
create()        write CACHE_DIR/snippets/<token>/<name>, insert the row,
                return the viewer URL + expiry.
get()           404 for an unknown token, 410 once expires_at has passed — the
                bytes may still be there for a few hours, but an expired
                snippet is dead the moment it says it is.
purge_expired() delete the directory, then the row. Bytes first: a failed
                delete leaves the row so the next pass retries, which loses
                nothing but a little disk. Dropping the row first would orphan
                the bytes forever.
start_janitor() runs purge_expired() at API startup and every 6 hours after,
                in a daemon thread — the same idiom as the search warmer in
                plm/api.py. Every instance collects only the snippets in its
                OWN database and its own CACHE_DIR.

There is no "extend" operation on purpose. If a snippet mattered enough to
keep, it mattered enough to check in as a part.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

from . import config, db, staging

TTL_DEFAULT_DAYS = 7.0
TTL_MAX_DAYS = 30.0
JANITOR_EVERY = float(os.environ.get("PLM_SNIPPET_GC_SECONDS", 6 * 3600))
MAX_BYTES = int(os.environ.get("PLM_SNIPPET_MAX_BYTES", 64 * 1024 * 1024))
KEY_PREFIX = "snippets"   # CACHE_DIR/snippets/<token>/<name>; the row's `key`
ROUTE = "snip"            # the UI page: /snip/<token> (web/routes.js)

# The token is the whole access control story on the machine plane: it must be
# unguessable, and it must be safe to paste into a URL, a directory name and a
# SQL parameter without further thought — staging.token_dir validates it.


class SnippetError(Exception):
    """Same shape as ops.OpsError — carries the HTTP status it deserves."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# -- helpers ------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """staging.safe_name, plus the snippet rule: STL only. The viewer is an
    STLLoader, so accepting .3mf/.step here would produce a link that renders
    nothing — better to say so at upload time."""
    base = staging.safe_name(name or "model.stl")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        return f"{base}.stl"
    if ext.lower() != "stl":
        raise SnippetError(
            f"snippets are STL only (got '.{ext}') — the viewer is an STL "
            "renderer. Export an STL, or check the geometry in as a real part "
            "if it needs to be kept.")
    return f"{stem}.stl"


def _looks_like_stl(head: bytes, size: int) -> bool:
    """Cheap sanity gate, not a parser: catch the 'agent base64'd the wrong
    thing' case before it becomes a link that silently shows nothing. `head`
    is the first bytes (84 is enough), `size` the whole length."""
    if head[:5].lower() == b"solid":
        return True  # ascii STL (or a binary one with a chatty header)
    if len(head) >= 84:
        tris = int.from_bytes(head[80:84], "little")
        return size == 84 + 50 * tris
    return False


def _ttl_seconds(ttl_days: float | None = None,
                 ttl_seconds: float | None = None) -> float:
    """ttl_seconds is the internal/testing knob (it makes the janitor testable
    in one second instead of one day); ttl_days is what callers pass."""
    if ttl_seconds:
        secs = float(ttl_seconds)
    else:
        days = TTL_DEFAULT_DAYS if not ttl_days else float(ttl_days)
        secs = days * 86400.0
    if secs <= 0:
        raise SnippetError("ttl must be positive")
    return min(secs, TTL_MAX_DAYS * 86400.0)  # capped, never rejected


def viewer_url(token: str) -> str:
    """The link we hand a human. A real PATH, not a fragment, on purpose: a
    fragment never reaches the server, so a `#/snip/<token>` would be lost
    across the OIDC login round-trip the first time Geoff clicks a link on a
    new device. /snip/<token> survives it, and since stage 22 (real paths) it
    IS the page — the server serves the UI shell there, no redirect."""
    return f"{config.PUBLIC_URL}/{ROUTE}/{token}"


def record(row: dict | sqlite3.Row, now: float | None = None) -> dict:
    """The wire shape: everything a caller needs to hand the link to a human."""
    row = dict(row)
    now = db.now() if now is None else now
    left = row["expires_at"] - now
    return {
        "token": row["token"],
        "name": row["name"],
        "note": row["note"],
        "size": row["size"],
        "sha256": row["sha256"],
        "actor": row["actor"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "expires_in_seconds": round(left, 1),
        "expires_in_days": round(left / 86400.0, 2),
        "expired": left <= 0,
        "url": viewer_url(row["token"]),
        "blob_url": f"/api/snippets/{row['token']}/blob",
    }


# -- create / read / delete ---------------------------------------------------

def _too_big(size: int) -> None:
    if size > MAX_BYTES:
        raise SnippetError(
            f"{size:,} bytes is over the {MAX_BYTES:,} byte snippet limit — "
            "decimate the mesh, or check it in as a real part", 413)


def _check_stl_file(path: Path, size: int, what: str) -> None:
    with path.open("rb") as fh:
        head = fh.read(84)
    if not _looks_like_stl(head, size):
        raise SnippetError(
            f"{what} not an STL (no ASCII 'solid' header, and the binary "
            "triangle count does not match the size) — check what you "
            "uploaded")


def _insert(conn: sqlite3.Connection, *, token: str, fname: str, note: str,
            sha: str, size: int, ttl: float, actor: str, via: str) -> dict:
    """The row for bytes already in the token's directory, its event, its
    record — the one tail all three ways in share."""
    now = db.now()
    row = {"token": token, "name": fname, "note": (note or "").strip(),
           "key": f"{KEY_PREFIX}/{token}/{fname}", "sha256": sha,
           "size": size, "actor": actor,
           "created_at": now, "expires_at": now + ttl}
    conn.execute(
        "INSERT INTO snippets (token,name,note,key,sha256,size,actor,"
        "created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (row["token"], row["name"], row["note"], row["key"], row["sha256"],
         row["size"], row["actor"], row["created_at"], row["expires_at"]))
    db.add_event(conn, "snippet.created", actor=actor,
                 data={"token": token, "name": fname, "size": size,
                       "via": via, "expires_at": row["expires_at"]})
    return record(row, now)


def create(conn: sqlite3.Connection, *, name: str, data: bytes, note: str = "",
           ttl_days: float | None = None, ttl_seconds: float | None = None,
           actor: str = "") -> dict:
    """Store a temporary STL and return its record (including the URL)."""
    if not data:
        raise SnippetError("no bytes — content_b64 decoded to nothing")
    _too_big(len(data))
    fname = _safe_name(name)
    if not _looks_like_stl(data[:84], len(data)):
        raise SnippetError(
            "these bytes are not an STL (neither an ASCII 'solid' header nor a "
            "valid binary triangle count) — check what you base64'd")
    ttl = _ttl_seconds(ttl_days, ttl_seconds)

    token = secrets.token_urlsafe(12)
    dest = staging.token_dir(KEY_PREFIX, token) / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return _insert(conn, token=token, fname=fname, note=note,
                   sha=hashlib.sha256(data).hexdigest(), size=len(data),
                   ttl=ttl, actor=actor, via="inline")


def create_from_handle(conn: sqlite3.Connection, *, handle: str,
                       sha256: str = "", size: int = 0, name: str = "",
                       note: str = "", ttl_days: float | None = None,
                       ttl_seconds: float | None = None,
                       actor: str = "") -> dict:
    """Snippet from a staged upload (upload() → PUT → here) — the
    files-by-reference path, so big meshes never transit base64/context.
    Claims the stage (sha256+size verified against the bytes), MOVES the
    inbox file under the snippet's token (same disk, no copy), and consumes
    the stage."""
    st = staging.claim(conn, handle, sha256=sha256, size=size)
    _too_big(st["size"])
    fname = _safe_name(name or st["name"])
    src = Path(st["path"])
    _check_stl_file(src, st["size"], "the staged upload is")
    ttl = _ttl_seconds(ttl_days, ttl_seconds)

    token = secrets.token_urlsafe(12)
    dest = staging.token_dir(KEY_PREFIX, token) / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    staging.consume(conn, st["token"], actor=actor)
    return _insert(conn, token=token, fname=fname, note=note,
                   sha=st["sha256"], size=st["size"], ttl=ttl, actor=actor,
                   via="handle")


def create_from_url(conn: sqlite3.Connection, *, url: str, sha256: str = "",
                    size: int = 0, name: str = "", note: str = "",
                    ttl_days: float | None = None,
                    ttl_seconds: float | None = None,
                    actor: str = "") -> dict:
    """Snippet from ANY https link plm can reach (a presigned GET, say).

    plm streams the link straight into the snippet's directory (sha256
    verified against what was promised) and checks it really is an STL. The
    object behind the link belongs to whoever staged it and is never touched
    (SERVICE-FRAMEWORK.md §3 — the service consumes the reference, not the
    other service's storage)."""
    fname = _safe_name(name or "model.stl")
    ttl = _ttl_seconds(ttl_days, ttl_seconds)
    token = secrets.token_urlsafe(12)
    try:
        fetched = staging.fetch_url(conn, name=fname, url=url, sha256=sha256,
                                    size=size, actor=actor,
                                    dest=staging.token_dir(KEY_PREFIX, token)
                                    / fname)
        _too_big(fetched["size"])
        _check_stl_file(Path(fetched["path"]), fetched["size"],
                        "the bytes at that link are")
    except BaseException:
        staging.remove_dir(KEY_PREFIX, token)
        raise
    return _insert(conn, token=token, fname=fname, note=note,
                   sha=fetched["sha256"], size=fetched["size"], ttl=ttl,
                   actor=actor, via="url")


def get(conn: sqlite3.Connection, token: str, *,
        allow_expired: bool = False) -> dict:
    try:
        staging.token_dir(KEY_PREFIX, token)
    except staging.StagingError:
        raise SnippetError("not a snippet token", 404) from None
    row = conn.execute("SELECT * FROM snippets WHERE token=?",
                       (token,)).fetchone()
    if not row:
        raise SnippetError(
            "no such snippet — the link is wrong, or it expired and was "
            "cleaned up. Snippets are disposable by design; ask for a fresh "
            "one.", 404)
    row = dict(row)
    if not allow_expired and row["expires_at"] <= db.now():
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(row["expires_at"]))
        raise SnippetError(
            f"this snippet expired at {when} — snippets are temporary "
            f"(default {TTL_DEFAULT_DAYS:g} days). Ask for a fresh one; if the "
            "geometry matters, it should be checked in as a real part.", 410)
    return row


def path(row: dict) -> Path:
    """The snippet's bytes on disk, or a 404 when they are gone."""
    path = staging.token_dir(KEY_PREFIX, row["token"]) / row["name"]
    if not path.is_file():
        raise SnippetError(
            "this snippet's bytes are gone (the cache was cleared?) — ask for "
            "a fresh one", 404)
    return path


def delete(conn: sqlite3.Connection, token: str, *, actor: str = "") -> dict:
    """Revoke now: bytes then row (see the module docstring on ordering)."""
    row = get(conn, token, allow_expired=True)
    try:
        staging.remove_dir(KEY_PREFIX, token)
    except OSError as exc:
        raise SnippetError(f"could not delete the snippet: {exc}", 500) from exc
    conn.execute("DELETE FROM snippets WHERE token=?", (token,))
    db.add_event(conn, "snippet.deleted", actor=actor,
                 data={"token": token, "name": row["name"]})
    return {"token": token, "deleted": True}


def listing(conn: sqlite3.Connection, *, include_expired: bool = False,
            limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM snippets"
    if not include_expired:
        sql += " WHERE expires_at > ?"
    sql += " ORDER BY created_at DESC LIMIT ?"
    args = (db.now(), min(limit, 500)) if not include_expired \
        else (min(limit, 500),)
    now = db.now()
    return [record(r, now) for r in conn.execute(sql, args)]


# -- garbage collection -------------------------------------------------------

def purge_expired(conn: sqlite3.Connection, *,
                  now: float | None = None) -> dict:
    """Delete every expired snippet's bytes and row. Safe to call at any
    time from anywhere; that is the point of a janitor."""
    now = db.now() if now is None else now
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM snippets WHERE expires_at <= ? ORDER BY expires_at",
        (now,))]
    deleted, errors = [], []
    for row in rows:
        try:
            staging.remove_dir(KEY_PREFIX, row["token"])
        except Exception as exc:  # noqa: BLE001 — OSError, or a bad token
            # Keep the row: the next pass retries. Orphaned bytes cost
            # disk; an orphaned row costs a lie about what still exists.
            errors.append({"token": row["token"], "error": str(exc)[:200]})
            continue
        conn.execute("DELETE FROM snippets WHERE token=?", (row["token"],))
        deleted.append(row["token"])
    if deleted or errors:
        db.add_event(conn, "snippet.gc", actor="janitor",
                     data={"deleted": len(deleted), "failed": len(errors)})
    return {"expired": len(rows), "deleted": len(deleted),
            "tokens": deleted, "errors": errors}


def janitor_once() -> dict:
    with db.connect() as conn:
        return purge_expired(conn)


def start_janitor() -> None:
    """Daemon thread: collect at startup, then every JANITOR_EVERY seconds.
    Collecting on boot means a restart is also a sweep, so nothing lingers
    just because the process was down when it expired.

    The loop itself now lives in plm/gc.py — staged uploads expire the same way,
    and one janitor idiom is enough for the whole service."""
    from . import gc
    gc.start("snippets", janitor_once, JANITOR_EVERY)
