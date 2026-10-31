"""Large-file intake: handles instead of base64 (docs/PLM_UPLOADS.md).

The problem this exists to kill: an agent with a 40 MB STL had exactly one way
to get it into plm — base64 it into a tool call. That is 54 MB of context for
one file, it fails at scale, and it makes the tool call a byte-proxy.

**plm's own upload inbox is the main road.** `upload()` mints a
token and a PUT address on plm itself; the agent PUTs the raw bytes there (on
the machine plane, with its bearer token like every /api/ write) and checks in
with the HANDLE:

    upload(name, size, sha256)  ->  {put_url, put_path, handle, expires}
    curl -f -T model.stl -H "Authorization: Bearer $PLM_TOKEN" "<put_url>"
    checkin(files=[{name, role, handle, sha256, size}])

(`filedrop_key` is the legacy name of the `handle` field and stays accepted.)

**A link is the other road.** An agent that already has the bytes somewhere
reachable (an object store, a file bus of its own) hands plm the LINK; plm's end
of that is `fetch_url` below and the `url` wire form in `normalize`:

    checkin(files=[{name, role, url, sha256, size}])

Both forms land in the same place by the same code. The difference is OWNERSHIP:
plm minted the handle, so plm deletes it; the link's object belongs to whoever
staged it, so plm never touches it (see fetch_url and ops._consume_inputs).

A **handle** names the inbox file:

    staging/<token>/<name>          (bytes at CACHE_DIR/inbox/<token>/<name>)

and it is only honored while a `staging` row vouches for it. The token is
unguessable and the row is the authority; anything else is a 404.

Verification, because a claim is not a fact
------------------------------------------
The bytes are hashed ONCE, by the Sink that writes them (a PUT to the inbox, or
a `url` fetch). The caller may declare sha256 and size at mint time; when the
bytes land, the arrived numbers must equal the declared ones (else a 400 and
nothing is kept), and they are then stored on the row with `received_at`. At
claim time the caller's numbers are compared against the ROW — the file is
checked to exist, never re-read. A mismatch is a 400 that prints both numbers:
a truncated upload, a stale handle, or the wrong file, and the caller needs to
know which.

Lifecycle
---------
mint()          insert the row, return the handle and the PUT address.
open_inbox()    PUT /api/uploads/<token>: a live row, nothing landed yet ->
                a Sink the route streams the body into (size-capped).
land()          the PUT finished: check the declared numbers, keep the file,
                record the arrived sha256/size on the row.
claim()         compare the caller's numbers with the row; hand ops the local
                path. Raises with a *useful* error when nothing was ever
                uploaded (the common mistake: forgot the curl).
consume()       delete the directory then the row, after the revision owns a
                copy.
purge_expired() the janitor, same shape and ordering rules as snippets.

Nothing here is ever a `files` row. A stage is a coat-check ticket; check-in
copies the coat into the revision and throws the ticket away.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import time
import urllib.parse
from pathlib import Path

import httpx

from . import config, db

KEY_PREFIX = "staging"   # a handle is `staging/<token>/<name>`
INBOX = "inbox"          # ... and its bytes are CACHE_DIR/inbox/<token>/<name>
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Where a URL arrival lands while plm decides what to do with it. Local disk
# (beside the database), never the synced vault — and never a permanent home:
# ops deletes each file as soon as the revision owns its copy, and the janitor
# sweeps whatever a failed write left behind.
URL_CACHE = config.CACHE_DIR / "urlcache"
URL_CACHE_TTL = float(2 * 3600)


class StagingError(Exception):
    """Same shape as ops.OpsError — carries the HTTP status it deserves."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# -- the inline guard (requirement 5) -----------------------------------------

def inline_limit(name: str, *, layer: str = "mcp") -> int:
    base = config.INLINE_MAX_MCP if layer == "mcp" else config.INLINE_MAX_HTTP
    if Path(name or "").suffix.lower() in set(config.STORAGE_PIN_EXT):
        return max(base, config.INLINE_MAX_TEXT)  # inline is FOR text
    return base


def guard_inline(name: str, nbytes: int, *, layer: str = "mcp") -> None:
    """Refuse an inline payload that should have been a handle.

    Deliberately phrased as instructions rather than a limit: an agent that hits
    this needs to know the next three commands, not the value of an env var."""
    limit = inline_limit(name, layer=layer)
    if nbytes <= limit:
        return
    raise StagingError(
        f"{name}: {nbytes:,} bytes inline is over the {limit:,} byte limit for "
        f"{'a tool call' if layer == 'mcp' else 'an inline HTTP body'}. Inline "
        "content is for small TEXT documents; binaries (STL/3MF/STEP/PNG) go "
        "through the upload flow instead:\n"
        "  1. upload(name, size, sha256) -> {put_url, handle}\n"
        "  2. curl -f -T <file> -H \"Authorization: Bearer $PLM_TOKEN\" "
        "'<put_url>'\n"
        "  3. checkin/put_file with handle=<handle>, sha256, size\n"
        "or hand plm a link instead (url=, sha256=). See docs/PLM_UPLOADS.md.",
        413)


# -- the wire -> ops translation, once, for both HTTP and MCP -----------------

def normalize(conn: sqlite3.Connection, files: list[dict], *,
              layer: str = "mcp", force_role: str = "", actor: str = "") -> list[dict]:
    """Turn client file entries into the ops entry shape.

    FOUR accepted wire forms — four TRANSPORTS, one destination. Which one a
    client uses says nothing about where the bytes end up; that is
    storage.choose_backend's decision alone (SERVICE-FRAMEWORK.md §3):

      {name, text}                              small text — inline is correct
      {name, content_b64}                       small binary — inline, guarded
      {name, handle, sha256, size}             plm's own staged upload
                                                (`filedrop_key`: legacy alias)
      {name, url, sha256, size?}                ANY https URL plm can reach
                                                (a presigned GET, say)

    The url form is for bytes an agent already has somewhere reachable: it
    hands plm the LINK. plm streams it down server-side, verifies the sha256 it was promised, and
    feeds the same ops path as every other arrival. It does NOT delete the source
    object: plm may not own it (see fetch_url).

    Every path through here either produces bytes plm was always going to hold
    anyway, or a verified reference it will copy without reading. Shared by the
    API and the MCP so the guard and the verification cannot drift apart."""
    import base64
    out: list[dict] = []
    for f in files or []:
        name = (f.get("name") or "").strip()
        if not name:
            raise StagingError("every file needs a name")
        role = force_role or f.get("role") or ""
        handle = (f.get("handle") or f.get("filedrop_key") or "").strip()
        if handle:
            claimed = claim(conn, handle, sha256=f.get("sha256") or "",
                            size=int(f.get("size") or 0))
            out.append({"name": name, "role": role, "data": None,
                        "staged": claimed})
            continue
        url = (f.get("url") or "").strip()
        if url:
            fetched = fetch_url(conn, name=name, url=url,
                                sha256=f.get("sha256") or "",
                                size=int(f.get("size") or 0), actor=actor)
            out.append({"name": name, "role": role, "data": None,
                        "local": fetched})
            continue
        if f.get("content_b64") is not None:
            try:
                data = base64.b64decode(f["content_b64"], validate=False)
            except Exception as exc:  # noqa: BLE001
                raise StagingError(f"{name}: content_b64 is not valid base64: {exc}")
            guard_inline(name, len(data), layer=layer)
        elif f.get("text") is not None:
            data = f["text"].encode()
            guard_inline(name, len(data), layer=layer)
        else:
            raise StagingError(
                f"{name}: give text (small documents), content_b64 (small "
                "binaries), handle (plm's own upload handle) or url (an "
                "https link to the bytes)")
        out.append({"name": name, "role": role, "data": data})
    return out


# -- arrival by URL: the universal transport (SERVICE-FRAMEWORK.md §3) --------

def fetch_url(conn: sqlite3.Connection, *, name: str, url: str, sha256: str,
              size: int = 0, actor: str = "", dest: Path | None = None) -> dict:
    """Stream an https URL down to local disk, verifying sha256 as it goes.

    This is plm's end of files-by-link: an agent stages a file somewhere plm
    can reach, hands plm the (presigned) GET link, and plm downloads it
    server-side. Nothing about the bytes passes through the tool call or the
    agent's context — the link does.

    Three rules, and they are the reason this is safe to expose:

    1. **sha256 is REQUIRED.** plm holds these bytes only for a moment, and
       whatever it writes into a revision is immutable afterwards; a link is a
       promise from somewhere else, so it has to come with the hash it is
       promising. Verified while streaming, and a mismatch is a 400 printing both
       values (never a quietly-wrong revision).
    2. **Size-capped** at PLM_RAW_UPLOAD_MAX, enforced during the download, so a
       hostile or mistaken link cannot fill the disk. https only, and redirects
       are not followed — a redirect is how a validated https URL becomes a
       plaintext or internal one.
    3. **plm never deletes the source object.** It may not own it — a filedrop
       inbox object belongs to the agent that staged it, and other services may
       have been handed the same link. Ownership stays with whoever created it;
       the inbox's own TTL/consume-on-download machinery cleans up. plm deletes
       only what plm minted (`consume` below, for upload handles).

    `dest` is where the verified file lands: a temp file in URL_CACHE by
    default, or an inbox file when `stage_from_url` turns the link into a
    handle.
    """
    fname = safe_name(name)
    sha = (sha256 or "").strip().lower()
    if not sha:
        raise StagingError(
            f"{fname}: the url form requires sha256 — plm cannot vouch for bytes "
            "it fetched from a link without the hash they were promised to have. "
            "`sha256sum <file>` before you upload.")
    if not _SHA_RE.match(sha):
        raise StagingError(f"{fname}: sha256 must be 64 hex characters")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise StagingError(
            f"{fname}: url must be an https URL (got {url[:60]!r})")

    if dest is None:
        dest = cache_path(fname)
    try:
        # No redirect following: the scheme/host were validated above, and a
        # redirect is how an https URL turns into a plaintext or internal one.
        with httpx.stream("GET", url, timeout=120.0, follow_redirects=False) as r:
            if r.status_code != 200:
                r.read()
                raise StagingError(
                    f"{fname}: GET {parts.netloc} -> {r.status_code}. The link "
                    "may have expired, been consumed (single-use links die on "
                    "first download), or never had bytes PUT to it.",
                    502 if r.status_code >= 500 else 400)
            with Sink(dest, f"{fname}: the link") as sink:
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    sink.write(chunk)
    except httpx.HTTPError as exc:
        raise StagingError(f"{fname}: could not fetch the url: {exc}", 502) from exc

    actual, total = sink.sha256, sink.size
    if actual != sha:
        sink.discard()
        raise StagingError(
            f"{fname}: sha256 mismatch — you said {sha}, the {total:,} bytes at "
            f"that link hash to {actual}. Wrong link, wrong file, or a truncated "
            "upload. Nothing was written.", 400)
    if size and int(size) != total:
        sink.discard()
        raise StagingError(
            f"{fname}: size mismatch — you said {int(size):,}, the link "
            f"delivered {total:,} bytes", 400)
    sink.keep()

    # OUT OF BAND, and this is load-bearing. normalize() runs BEFORE
    # ops.checkin, so writing this event on the caller's connection opened the
    # write transaction here — and then held SQLite's single writer lock through
    # vault.write_checkin and vault.commit (git over the synced volume, tens of
    # seconds under load), which is what made unrelated writers wait out their
    # busy_timeout and report `database is locked`. The event is worth keeping;
    # it is not worth a transaction that spans a git commit.
    db.event_out_of_band("upload.fetched", actor=actor,
                         data={"name": fname, "host": parts.netloc,
                               "size": total, "sha256": actual})
    return {"path": str(dest), "name": fname, "sha256": actual, "size": total,
            "url": url}


def stage_from_url(conn: sqlite3.Connection, *, name: str, url: str,
                   sha256: str, size: int = 0, actor: str = "") -> dict:
    """upload(url=...): fetch a link server-side NOW and turn it into a staged
    handle — same handle currency as a PUT to the inbox, so everything
    downstream (checkin/put_file/put_viz/share_3d) treats both uploads
    identically. plm owns this staged copy (it minted it); the SOURCE link's
    object is untouched — ownership stays with whoever created it (see
    fetch_url). fetch_url already hashed the bytes, so the row records them as
    arrived."""
    fname = safe_name(name)
    token = secrets.token_urlsafe(12)
    try:
        entry = fetch_url(conn, name=fname, url=url, sha256=sha256, size=size,
                          actor=actor, dest=inbox_dir(token) / fname)
    except BaseException:
        remove_dir(INBOX, token)
        raise
    row = _insert_row(conn, token=token, fname=fname, sha=entry["sha256"],
                      size=entry["size"], actor=actor, ttl=_ttl(None),
                      received_at=db.now())
    handle = handle_of(row)
    db.add_event(conn, "upload.staged", actor=actor,
                 data={"handle": handle, "from": "url",
                       "sha256": row["sha256"], "size": row["size"]})
    return {**record(row), "instructions": (
        f"staged and verified — pass handle='{handle}' with sha256 and size "
        "to checkin/put_file/put_viz/share_3d")}


def cache_path(fname: str) -> Path:
    """A fresh URL_CACHE path for one arrival plm holds only until the
    revision owns a copy (a `url` fetch, a raw PUT to an item)."""
    return URL_CACHE / f"{secrets.token_hex(8)}-{fname}"


def release_local(entry: dict | None) -> None:
    """Delete a fetched temp copy. Called once the revision owns its own bytes."""
    if not entry:
        return
    try:
        Path(entry["path"]).unlink(missing_ok=True)
    except OSError:
        pass  # the janitor sweeps the cache anyway


def purge_url_cache(*, now: float | None = None) -> dict:
    """Sweep temp copies a failed write left behind (nothing owns them)."""
    now = time.time() if now is None else now
    deleted = []
    if URL_CACHE.is_dir():
        for path in URL_CACHE.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > URL_CACHE_TTL:
                    path.unlink()
                    deleted.append(path.name)
            except OSError:
                continue
    return {"deleted": len(deleted), "files": deleted}


# -- helpers ------------------------------------------------------------------

def safe_name(name: str) -> str:
    base = _NAME_RE.sub("_", Path((name or "").strip()).name).strip("._")
    return base[:120] or "upload.bin"


def token_dir(area: str, token: str) -> Path:
    """`CACHE_DIR/<area>/<token>` — THE map from a token to the directory that
    holds its bytes, for the upload inbox (`INBOX`) and for 3-D snippets
    (plm/snippets.py). Local disk beside the database, never the synced vault;
    everything under it expires by itself. The token is validated here, so no
    caller can turn a client string into a path outside its area."""
    if not _TOKEN_RE.match(token or ""):
        raise StagingError(f"malformed token: {token!r}", 404)
    return config.CACHE_DIR / area / token


def inbox_dir(token: str) -> Path:
    return token_dir(INBOX, token)


def remove_dir(area: str, token: str) -> None:
    """Delete a token's directory and everything in it. Idempotent — a missing
    directory is a success, which is what a janitor wants. A real failure
    (permissions, a busy disk) raises OSError for the caller to keep the row."""
    d = token_dir(area, token)
    if d.exists():
        shutil.rmtree(d)


class Sink:
    """Write arriving bytes to a file while hashing and counting them, and
    refuse past PLM_RAW_UPLOAD_MAX. The ONE copy of that loop: a `url` fetch,
    a PUT to the inbox and a raw PUT to an item all feed it, so the cap and
    the hash cannot drift — and it is the only place plm hashes arriving
    bytes.

        with Sink(dest, what) as sink:         # an exception discards it
            for chunk in body: sink.write(chunk)
        sink.sha256, sink.size                 # verify, then:
        sink.keep()  (or sink.discard())

    Bytes go to a private temp file beside `dest`; `keep()` links it into
    place EXCLUSIVELY, so a second writer to the same `dest` gets a 409 instead
    of interleaving with the first. `what` names the payload in the refusals
    ("bracket.stl: the body").
    """

    def __init__(self, dest: Path, what: str):
        self.dest, self.what = dest, what
        self.cap = config.RAW_UPLOAD_MAX
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.tmp = dest.with_name(f".{secrets.token_hex(6)}.part")
        self._fh = self.tmp.open("wb")
        self._digest = hashlib.sha256()
        self.size = 0
        self.sha256 = ""

    @staticmethod
    def too_big(what: str) -> StagingError:
        """The 413 — for a body that grew past the cap, and for a
        Content-Length that announces it before anything is opened."""
        return StagingError(
            f"{what} is over the {config.RAW_UPLOAD_MAX:,} byte upload limit "
            "(PLM_RAW_UPLOAD_MAX)", 413)

    @staticmethod
    def landed(name: str) -> StagingError:
        """The 409 — an upload is written once."""
        return StagingError(
            f"{name}: bytes already landed for this upload. An upload is "
            "written once — drop_upload(handle) and upload() again to "
            "replace it.", 409)

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.cap:
            raise self.too_big(self.what)
        self._digest.update(chunk)
        self._fh.write(chunk)

    def close(self, failed: bool = False) -> None:
        """Done writing: a failure discards the bytes, success fixes sha256."""
        self._fh.close()
        if failed:
            self.discard()
        else:
            self.sha256 = self._digest.hexdigest()

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(failed=exc_type is not None)

    def keep(self) -> Path:
        try:
            os.link(self.tmp, self.dest)
        except FileExistsError:
            raise self.landed(self.dest.name) from None
        finally:
            self.tmp.unlink(missing_ok=True)
        return self.dest

    def discard(self) -> None:
        self.tmp.unlink(missing_ok=True)


def _ttl(ttl_seconds: float | None) -> float:
    secs = float(ttl_seconds or config.STAGING_TTL_SECONDS)
    if secs <= 0:
        raise StagingError("ttl must be positive")
    return min(secs, config.STAGING_MAX_TTL_SECONDS)


def handle_of(row: dict | sqlite3.Row) -> str:
    """The handle a row IS: `staging/<token>/<name>`."""
    return f"{KEY_PREFIX}/{row['token']}/{row['name']}"


def put_command(fname: str, put_url: str) -> str:
    """The one spelling of "PUT these bytes to plm's inbox" — the mint's
    instructions and the MCP tool text say exactly this."""
    return (f"curl -f -T {fname} -H \"Authorization: Bearer $PLM_TOKEN\" "
            f"'{put_url}'")


def record(row: dict | sqlite3.Row, now: float | None = None) -> dict:
    row = dict(row)
    now = db.now() if now is None else now
    return {
        "handle": handle_of(row),
        "token": row["token"],
        "name": row["name"],
        "sha256": row["sha256"],
        "size": row["size"],
        "arrived": bool(row.get("received_at")),
        "received_at": row.get("received_at"),
        "actor": row["actor"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "expires_in_seconds": round(row["expires_at"] - now, 1),
        "expired": row["expires_at"] <= now,
    }


def _insert_row(conn: sqlite3.Connection, *, token: str, fname: str, sha: str,
                size: int, actor: str, ttl: float,
                received_at: float | None = None) -> dict:
    """The one INSERT for a stage — minted (declared numbers, nothing landed)
    or fetched from a link (arrived numbers, received now)."""
    now = db.now()
    row = {"token": token, "name": fname, "sha256": sha, "size": int(size or 0),
           "actor": actor, "created_at": now, "expires_at": now + ttl,
           "received_at": received_at}
    conn.execute(
        "INSERT INTO staging (token,name,key,sha256,size,actor,created_at,"
        "expires_at,received_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (token, fname, handle_of(row), row["sha256"], row["size"], actor,
         row["created_at"], row["expires_at"], received_at))
    return row


def _live(row: sqlite3.Row | None, what: str) -> dict:
    """A row that exists and has not expired, or the 404/410 that says so."""
    if not row:
        raise StagingError(
            f"unknown upload {what} — it was never minted here, or it expired "
            "and was cleaned up. Call upload() again.", 404)
    if row["expires_at"] <= db.now():
        raise StagingError(
            f"this upload expired at {row['expires_at']:.0f} — staged objects "
            "are temporary. upload() again and re-upload.", 410)
    return dict(row)


# -- mint / receive / claim / consume ------------------------------------------

def mint(conn: sqlite3.Connection, *, name: str, size: int = 0,
         sha256: str = "", actor: str = "", expires_in: float | None = None
         ) -> dict:
    """Mint an upload: a token, its handle and the inbox address to PUT to.
    `put_path` is the same address without the public origin, for a client
    that reaches plm at a different base URL."""
    fname = safe_name(name)
    sha = (sha256 or "").strip().lower()
    if sha and not _SHA_RE.match(sha):
        raise StagingError("sha256 must be 64 hex characters (or omitted)")
    if size and size < 0:
        raise StagingError("size must not be negative")
    ttl = _ttl(expires_in)
    row = _insert_row(conn, token=secrets.token_urlsafe(12), fname=fname,
                      sha=sha, size=size, actor=actor, ttl=ttl)
    handle = handle_of(row)
    put_path = f"/api/uploads/{row['token']}"
    put_url = f"{config.PUBLIC_URL}{put_path}"
    db.add_event(conn, "upload.minted", actor=actor,
                 data={"handle": handle, "declared_size": row["size"]})
    return {**record(row), "put_url": put_url, "put_path": put_path,
            "put_expires_in_seconds": int(ttl),
            "instructions": (
                f"PUT the raw bytes of {fname} to put_url before it expires: "
                f"{put_command(fname, put_url)} — then pass "
                f"handle='{handle}' (with sha256 and size) to "
                "checkin/put_file/put_viz/share_3d. The bytes go straight to "
                "plm's upload inbox; do NOT base64 them into a tool call.")}


def open_inbox(conn: sqlite3.Connection, token: str) -> tuple[dict, Sink]:
    """PUT /api/uploads/<token>: the live row and a Sink for its bytes.

    Refused when the token is unknown (404) or expired (410), and when bytes
    already landed (409): an upload is written once, so a handle names one
    set of bytes for its whole life. `Sink.keep()` enforces the same rule
    against two PUTs racing each other."""
    d = inbox_dir(token)
    row = _live(conn.execute("SELECT * FROM staging WHERE token=?",
                             (token,)).fetchone(), "token")
    if row["received_at"]:
        raise Sink.landed(row["name"])
    return row, Sink(d / row["name"], f"{row['name']}: the body")


def land(conn: sqlite3.Connection, row: dict, sink: Sink, *,
         actor: str = "") -> dict:
    """The PUT's bytes are all in the Sink: hold them to what the minter
    declared, keep the file, and record the ARRIVED numbers on the row — the
    one hash every later claim and status read trusts."""
    for label, declared, real in (("sha256", row["sha256"], sink.sha256),
                                  ("size", row["size"], sink.size)):
        if declared and declared != real:
            sink.discard()
            raise StagingError(
                f"{row['name']}: {label} mismatch — you declared {declared} at "
                f"upload(), the {sink.size:,} bytes that arrived are {real}. "
                "Truncated upload, or not the file you hashed; nothing was "
                "kept. PUT the right bytes again.", 400)
    sink.keep()
    now = db.now()
    conn.execute("UPDATE staging SET sha256=?, size=?, received_at=? "
                 "WHERE token=?", (sink.sha256, sink.size, now, row["token"]))
    row = {**row, "sha256": sink.sha256, "size": sink.size, "received_at": now}
    db.add_event(conn, "upload.received", actor=actor or row["actor"] or "api",
                 data={"handle": handle_of(row), "sha256": sink.sha256,
                       "size": sink.size})
    return row


def by_handle(conn: sqlite3.Connection, handle: str) -> dict:
    """The staging row for a handle, or a 404. A handle plm never minted is not
    a handle — otherwise this would read any file in plm's inbox."""
    handle = (handle or "").strip()
    prefix = f"{KEY_PREFIX}/"
    if not handle.startswith(prefix):
        raise StagingError(
            f"not an upload handle: {handle!r} — handles look like "
            f"'{prefix}<token>/<name>' and come from upload()", 404)
    token = handle[len(prefix):].split("/", 1)[0]
    if not _TOKEN_RE.match(token):
        raise StagingError(f"malformed upload handle: {handle!r}", 404)
    row = conn.execute("SELECT * FROM staging WHERE token=?", (token,)).fetchone()
    if row and handle_of(row) != handle:
        row = None
    return _live(row, f"handle: {handle!r}")


def claim(conn: sqlite3.Connection, handle: str, *, sha256: str = "",
          size: int = 0) -> dict:
    """Return what ops needs to copy a staged file, after holding the caller's
    numbers to the ones recorded when the bytes landed.

    No hashing here: the Sink that wrote the file hashed it and the row holds
    that result. This is a row read and one stat."""
    row = by_handle(conn, handle)
    path = inbox_dir(row["token"]) / row["name"]
    if not row["received_at"] or not path.is_file():
        raise StagingError(
            f"nothing was uploaded to {handle_of(row)} yet — PUT the bytes "
            "to the put_url from upload() first ("
            + put_command("<file>", "<put_url>") + "), then check in.", 409)
    for label, claimed, real in (("sha256", (sha256 or "").strip().lower(),
                                  row["sha256"]),
                                 ("size", int(size or 0), row["size"])):
        if claimed and claimed != real:
            raise StagingError(
                f"{row['name']}: {label} mismatch — you said {claimed}, the "
                f"stored upload is {real}. This is not the file you hashed, "
                "or the handle is stale. Re-upload and try again.", 400)
    return {"token": row["token"], "handle": handle_of(row),
            "name": row["name"], "sha256": row["sha256"], "size": row["size"],
            "path": str(path)}


def consume(conn: sqlite3.Connection, token: str, *, actor: str = "") -> None:
    """The revision owns a copy now: directory first, then row (snippets' rule
    — an orphaned directory costs disk, an orphaned row tells a lie).

    MUST be called only after the revision that references the bytes is
    COMMITTED (ops.checkin does this deliberately): if the
    transaction rolled back after the bytes were gone, the revision that was
    supposed to own them would never exist — an unretryable check-in."""
    row = conn.execute("SELECT * FROM staging WHERE token=?", (token,)).fetchone()
    if not row:
        return
    try:
        remove_dir(INBOX, token)
    except OSError:
        return  # leave the row; the janitor retries at expiry
    conn.execute("DELETE FROM staging WHERE token=?", (token,))
    db.add_event(conn, "upload.consumed", actor=actor,
                 data={"handle": handle_of(row)})


def drop(conn: sqlite3.Connection, handle_or_token: str, *,
         actor: str = "") -> dict:
    """Abandon a stage early (the agent changed its mind)."""
    token = handle_or_token
    if "/" in handle_or_token:
        token = by_handle(conn, handle_or_token)["token"]
    row = conn.execute("SELECT * FROM staging WHERE token=?", (token,)).fetchone()
    if not row:
        raise StagingError(f"unknown upload: {handle_or_token}", 404)
    try:
        remove_dir(INBOX, token)
    except OSError as exc:
        raise StagingError(f"could not delete the staged upload: {exc}",
                           500) from exc
    conn.execute("DELETE FROM staging WHERE token=?", (token,))
    db.add_event(conn, "upload.dropped", actor=actor,
                 data={"handle": handle_of(row)})
    return {"handle": handle_of(row), "deleted": True}


def listing(conn: sqlite3.Connection, *, include_expired: bool = False,
            limit: int = 100) -> list[dict]:
    now = db.now()
    sql = "SELECT * FROM staging"
    args: tuple = (min(limit, 500),)
    if not include_expired:
        sql += " WHERE expires_at > ?"
        args = (now, min(limit, 500))
    sql += " ORDER BY created_at DESC LIMIT ?"
    return [record(r, now) for r in conn.execute(sql, args)]


# -- garbage collection -------------------------------------------------------

def purge_expired(conn: sqlite3.Connection, *, now: float | None = None) -> dict:
    now = db.now() if now is None else now
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM staging WHERE expires_at <= ? ORDER BY expires_at", (now,))]
    deleted, errors = [], []
    for row in rows:
        try:
            remove_dir(INBOX, row["token"])
        except (OSError, StagingError) as exc:
            errors.append({"handle": handle_of(row), "error": str(exc)[:200]})
            continue
        conn.execute("DELETE FROM staging WHERE token=?", (row["token"],))
        deleted.append(handle_of(row))
    if deleted or errors:
        db.add_event(conn, "upload.gc", actor="janitor",
                     data={"deleted": len(deleted), "failed": len(errors)})
    return {"expired": len(rows), "deleted": len(deleted),
            "handles": deleted, "errors": errors}


def janitor_once() -> dict:
    with db.connect() as conn:
        result = purge_expired(conn)
    stale = purge_url_cache()
    if stale["deleted"]:
        result["url_cache_deleted"] = stale["deleted"]
        result["deleted"] += stale["deleted"]
    return result
