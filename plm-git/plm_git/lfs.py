"""The Git LFS API, served next to smart HTTP (stage 17, docs/LARGE-FILES.md).

`git-lfs` finds this by itself: the LFS endpoint of `http://host/git/N.git` is
`http://host/git/N.git/info/lfs`, so a plain `git clone` / `git push` with
git-lfs installed needs no configuration at all. Four routes:

  POST info/lfs/objects/batch      the batch API — `basic` transfer only
  GET  info/lfs/objects/<oid>      download (anonymous, like clone)
  PUT  info/lfs/objects/<oid>      upload   (bearer, like push)
  POST info/lfs/verify             "did it land?" — size + presence
  POST info/lfs/locks/verify       {"ours": [], "theirs": []} — see below

Bytes live in plm's content-addressed store (`plm/lfs.py`), shared by every
item: the same mesh in two parts is one object. An upload is hashed while it
streams and only becomes an object if it hashes to the oid in its URL and has
the promised size — so the store cannot hold a lie, and the receive hook only
has to ask "is it there, at that size?".

The SAME door as a push: reads are anonymous on the private network, an `upload`
operation and a PUT need a bearer token that maps to a plm actor
(`push.identify`). The batch answer's upload `href` carries the caller's own
Authorization header back to it — git-lfs sends it on the PUT, and the PUT is
identified again; nothing is minted that could outlive the token.

LOCKS ARE NOT BUILT (§6 names them; Phase 4's "retire `checkouts`" is a later
step). `locks/verify` answers "no locks" so git-lfs's pre-push does not warn on
every push, and creating a lock is refused with a pointer to plm's checkout.
"""
from __future__ import annotations

import json
import re

from plm import config as plmconfig
from plm import lfs as store

MEDIA = "application/vnd.git-lfs+json"
_OID = re.compile(r"^[0-9a-f]{64}$")


def _lfs_json(h, obj, code: int = 200, extra: dict | None = None) -> None:
    body = (json.dumps(obj) + "\n").encode()
    h.send_response(code)
    h.send_header("Content-Type", MEDIA)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def _error(h, code: int, message: str, extra: dict | None = None) -> None:
    _lfs_json(h, {"message": message,
                  "documentation_url": "docs/LARGE-FILES.md"}, code, extra)


def _base(h, repo: str) -> str:
    """The URL the client used to reach us — hrefs must come back to the same
    host it already trusts with its token (no configured hostname to drift)."""
    proto = (h.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
    host = h.headers.get("Host") or f"127.0.0.1:{h.server.server_address[1]}"
    return f"{proto}://{host}/git/{repo}/info/lfs"


def _read_json(h, limit: int = 1 << 20):
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0 or n > limit:
        h.close_connection = n > limit
        return None
    try:
        return json.loads(h.rfile.read(n))
    except ValueError:
        return None


def _drain(h, limit: int = 1 << 20) -> None:
    """Consume a request body nobody reads. HTTP/1.1 keep-alive: an unread
    body is read as the NEXT request line — git-lfs's batch POST arrived as
    method `{"ref":…}POST` after an unread locks/verify body (found running
    demo-lfs). Too big to drain -> close the connection instead."""
    n = int(h.headers.get("Content-Length") or 0)
    if n > limit:
        h.close_connection = True
    elif n > 0:
        h.rfile.read(n)


def handle(h, repo: str, rest: str, actor_for) -> None:
    """`rest` is the path after `/git/<repo>/info/lfs/`. `actor_for()` runs the
    push door and returns the actor or raises the push module's Refused."""
    method = h.command
    if not (rest == "objects/batch" or rest == "verify"
            or (method == "PUT" and rest.startswith("objects/"))):
        _drain(h)
    if rest == "objects/batch" and method == "POST":
        return _batch(h, repo, actor_for)
    m = re.match(r"^objects/([0-9a-f]{64})$", rest)
    if m and method == "GET":
        return _download(h, m.group(1))
    if m and method == "PUT":
        return _upload(h, m.group(1), actor_for)
    if rest == "verify" and method == "POST":
        return _verify(h, actor_for)
    if rest == "locks/verify" and method == "POST":
        return _lfs_json(h, {"ours": [], "theirs": [], "next_cursor": ""})
    if rest == "locks" and method == "GET":
        return _lfs_json(h, {"locks": [], "next_cursor": ""})
    if rest.startswith("locks"):
        return _error(h, 501, "plm does not serve LFS locks yet — an opaque "
                      "file is held with plm's checkout (claim_checkout) "
                      "until Phase 4 retires it (docs/LARGE-FILES.md).")
    return _error(h, 404, f"no LFS route {method} {rest}")


def _authed(h, actor_for):
    try:
        return actor_for(), None
    except Exception as exc:                                  # push.Refused
        needs = getattr(exc, "needs_credential", False)
        extra = {"LFS-Authenticate": 'Basic realm="plm (password = your plm token)"',
                 "WWW-Authenticate": 'Basic realm="plm (password = your plm token)"'} \
            if needs else {}
        _error(h, 401 if needs else 403, str(exc), extra)
        return None, exc


def _batch(h, repo: str, actor_for) -> None:
    req = _read_json(h)
    if not isinstance(req, dict) or not isinstance(req.get("objects"), list):
        return _error(h, 422, "a batch request is {operation, objects: "
                      "[{oid, size}], transfers}")
    op = req.get("operation")
    if op not in ("download", "upload"):
        return _error(h, 422, f"operation {op!r} — plm knows download and upload")
    transfers = req.get("transfers") or ["basic"]
    if "basic" not in transfers:
        return _error(h, 422, "plm speaks the `basic` transfer only")
    auth_header = {}
    if op == "upload":
        actor, refused = _authed(h, actor_for)
        if refused is not None:
            return
        if auth := h.headers.get("Authorization"):
            auth_header = {"Authorization": auth}
    base = _base(h, repo)
    out = []
    for o in req["objects"][:1000]:
        oid = str((o or {}).get("oid") or "")
        try:
            size = int((o or {}).get("size"))
        except (TypeError, ValueError):
            size = -1
        item = {"oid": oid, "size": size}
        if not _OID.match(oid) or size < 0:
            item["error"] = {"code": 422, "message": "oid must be 64 hex "
                             "(sha256) and size a non-negative integer"}
        elif op == "download":
            if store.has(oid, size):
                item.update({"authenticated": True, "actions": {"download": {
                    "href": f"{base}/objects/{oid}", "expires_in": 3600}}})
            else:
                item["error"] = {"code": 404, "message": (
                    "plm's LFS store does not have this object"
                    + ("" if store.size_of(oid) is None else
                       f" at that size (it has {store.size_of(oid):,} bytes)"))}
        else:
            if size > plmconfig.LFS_MAX_OBJECT:
                item["error"] = {"code": 422, "message": (
                    f"{size:,} bytes is over plm's {plmconfig.LFS_MAX_OBJECT:,}"
                    "-byte LFS object limit (PLM_LFS_MAX_OBJECT_BYTES)")}
            elif not store.has(oid, size):
                # already present -> no actions: git-lfs skips it (dedupe)
                item.update({"authenticated": True, "actions": {
                    "upload": {"href": f"{base}/objects/{oid}",
                               "header": auth_header, "expires_in": 3600},
                    "verify": {"href": f"{base}/verify",
                               "header": auth_header, "expires_in": 3600}}})
        out.append(item)
    _lfs_json(h, {"transfer": "basic", "objects": out, "hash_algo": "sha256"})


def _download(h, oid: str) -> None:
    p = store.object_path(oid)
    if not p.is_file():
        return _error(h, 404, f"plm's LFS store does not have {oid[:12]}…")
    size = p.stat().st_size
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", str(size))
    h.end_headers()
    try:
        for chunk in store.stream(oid, 1 << 16):
            h.wfile.write(chunk)
    except BrokenPipeError:
        pass


def _upload(h, oid: str, actor_for) -> None:
    h.close_connection = True          # until the body is fully consumed
    actor, refused = _authed(h, actor_for)
    if refused is not None:
        return
    if (h.headers.get("Transfer-Encoding") or "").lower() == "chunked":
        return _error(h, 411, "send Content-Length (git-lfs does)")
    n = int(h.headers.get("Content-Length") or -1)
    if n < 0:
        return _error(h, 411, "Content-Length is required")
    if n > plmconfig.LFS_MAX_OBJECT:
        return _error(h, 413, f"{n:,} bytes is over plm's "
                      f"{plmconfig.LFS_MAX_OBJECT:,}-byte LFS object limit")

    def body():
        left = n
        while left > 0:
            b = h.rfile.read(min(1 << 16, left))
            if not b:
                return
            left -= len(b)
            yield b
    try:
        got, size = store.put_stream(body(), oid=oid, size=n)
    except store.LfsError as exc:
        h.close_connection = True                 # the body may be half-read
        return _error(h, 422, str(exc))
    h.close_connection = False
    h.log_message("lfs upload %s %d bytes by %s", got[:12], size,
                  getattr(actor, "name", "?"))
    h.send_response(200)
    h.send_header("Content-Length", "0")
    h.end_headers()


def _verify(h, actor_for) -> None:
    req = _read_json(h) or {}
    actor, refused = _authed(h, actor_for)
    if refused is not None:
        return
    oid = str(req.get("oid") or "")
    try:
        size = int(req.get("size"))
    except (TypeError, ValueError):
        size = -1
    if not _OID.match(oid) or size < 0:
        return _error(h, 422, "verify takes {oid, size}")
    if store.has(oid, size):
        return _lfs_json(h, {"oid": oid, "size": size})
    have = store.size_of(oid)
    return _error(h, 404 if have is None else 422,
                  "not in plm's LFS store" if have is None else
                  f"plm has {have:,} bytes for this oid, not {size:,}")
