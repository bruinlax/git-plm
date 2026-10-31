"""The only way plm-git writes anything: plm's own HTTP API.

There is exactly one rule in this file and it is the whole reason the write path
is safe: **plm-git never writes the vault.** It does not `git commit`, it does not
touch a version folder, it does not compute a MANIFEST. It hands plm bytes and a
version number and lets plm's check-in pipeline (`plm/ops.py`, `plm/staging.py`,
`plm/vault.py`) do what it already does for the UI, the MCP and curl. Anything
else would be a second implementation of check-in, and the second one is always
the one that is wrong.

stdlib only, like the rest of the service — urllib is enough for JSON in, JSON
out, and a streamed PUT of a file's bytes.

Error handling is the interesting part. plm answers a refusal with
`{"detail": "..."}` and a status, and those messages are written to be read by
whoever caused them ("… is frozen — Revise ↑ first"). So `PlmError` carries
plm's own sentence, unmodified, and the hook prints it to git's stderr. The
pusher sees the real reason from the real authority, not a translation.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO


class PlmError(Exception):
    """A refusal from plm, carrying plm's own message and status."""

    def __init__(self, msg: str, code: int = 0, where: str = ""):
        super().__init__(msg)
        self.code = code
        self.where = where

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} (plm {self.code} on {self.where})" if self.where else base


class Plm:
    """A thin client for one plm instance.

    ⭐ IT AUTHENTICATES (stage 7, C2). plm's API plane refuses an anonymous
    write — and refuses an anonymous `GET /api/users`, which is how
    `push.identify` asks "does plm know this actor, and may they edit?" — so
    this client presents the service credential the git container already holds:
    `PLMGIT_GITFIRST_INDEX_TOKEN(_FILE)`, the shared secret
    `POST /api/git/index` has always required.
    """

    def __init__(self, base_url: str, timeout: float = 180.0,
                 token: str = ""):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        if token:
            self.token = token
        else:
            from . import config as _config
            self.token = _config.GITFIRST_INDEX_TOKEN or ""

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, *,
                 data: bytes | BinaryIO | None = None,
                 headers: dict | None = None) -> object:
        url = path if path.startswith("http") else f"{self.base}{path}"
        head = dict(headers or {})
        if self.token and "Authorization" not in head:
            head["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=head)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            detail = ""
            try:
                payload = json.loads(raw)
                detail = payload.get("detail") if isinstance(payload, dict) else ""
                if isinstance(detail, list):  # pydantic validation errors
                    detail = "; ".join(
                        f"{'.'.join(str(x) for x in d.get('loc', []))}: {d.get('msg')}"
                        for d in detail)
            except Exception:  # noqa: BLE001
                pass
            raise PlmError(str(detail or raw.decode(errors="replace")[:600] or exc.reason),
                           exc.code, f"{method} {path}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlmError(f"could not reach plm at {self.base}: {exc}", 0,
                           f"{method} {path}") from None
        if not body:
            return {}
        try:
            return json.loads(body)
        except ValueError:
            return body.decode(errors="replace")

    def get(self, path: str) -> object:
        return self._request("GET", path)

    def post(self, path: str, payload: dict) -> object:
        return self._request("POST", path, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})

    # -- the reads the write path needs -----------------------------------
    def item(self, number: str) -> dict:
        return self.get(f"/api/items/{urllib.parse.quote(number)}")  # type: ignore[return-value]

    def users(self) -> list[dict]:
        return self.get("/api/users")  # type: ignore[return-value]

    def user(self, username: str) -> dict | None:
        for u in self.users():
            if u.get("username") == username:
                return u
        return None

    def checkout(self, number: str) -> dict | None:
        res = self.get(f"/api/items/{urllib.parse.quote(number)}/checkout")
        return (res or {}).get("checkout")  # type: ignore[union-attr]

    def approvals(self, pending: bool = True) -> list[dict]:
        return self.get(f"/api/approvals?pending={'true' if pending else 'false'}")  # type: ignore[return-value]

    # -- the writes -------------------------------------------------------
    def revise(self, number: str, *, bump: str, summary: str, actor: str,
               rebind: str = "keep") -> dict:
        """New DRAFT seeded from the latest — files, BOM and attributes carried
        forward. This is why a push does NOT call checkin() directly: checkin
        mints a bare version, and an assembly pushed that way would silently lose
        its BOM. revise() is plm's own carry-forward."""
        return self.post(f"/api/items/{urllib.parse.quote(number)}/revise",
                         {"bump": bump, "summary": summary, "actor": actor,
                          "rebind": rebind})  # type: ignore[return-value]

    def put_files(self, number: str, files: list[dict], *, version: str,
                  actor: str, message: str) -> dict:
        """Working upload onto the DRAFT (ops.upload_files). `files` entries are
        already in plm's wire shape: {name, text} for text, or
        {name, handle, sha256, size} for a staged binary."""
        return self.post(f"/api/items/{urllib.parse.quote(number)}/files",
                         {"files": files, "version": version, "actor": actor,
                          "message": message})  # type: ignore[return-value]

    def submit(self, number: str, *, version: str, actor: str,
               target_state: str, note: str) -> dict:
        return self.post(f"/api/items/{urllib.parse.quote(number)}/submit",
                         {"version": version, "actor": actor,
                          "target_state": target_state, "note": note})  # type: ignore[return-value]

    def add_context(self, number: str, body: str, *, version: str,
                    actor: str) -> dict:
        return self.post(f"/api/items/{urllib.parse.quote(number)}/context",
                         {"body": body, "version": version, "actor": actor})  # type: ignore[return-value]

    # -- getting binary bytes in, without a JSON body ---------------------
    def mint_upload(self, *, name: str, size: int, sha256: str,
                    actor: str) -> dict:
        """plm mints an upload in its own inbox: a handle and the address to
        PUT the bytes to — `put_path`, joined with this client's base URL by
        put_upload (plm/staging.py)."""
        return self.post("/api/uploads", {"name": name, "size": size,
                                          "sha256": sha256, "actor": actor})  # type: ignore[return-value]

    def drop_upload(self, handle: str, actor: str) -> None:
        try:
            self._request("DELETE", "/api/uploads?"
                          + urllib.parse.urlencode({"handle": handle, "actor": actor}))
        except PlmError:
            pass  # the staging janitor sweeps it; a failed cleanup is not a failure

    def _put_file(self, path_: str, path: Path) -> object:
        """A file as the raw request body, streamed from disk — never a JSON
        body, so a 40 MB STL never becomes 54 MB of JSON."""
        with path.open("rb") as fh:
            return self._request(
                "PUT", path_, data=fh,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": str(path.stat().st_size)})

    def put_upload(self, put_path: str, path: Path) -> dict:
        """Stream one file to the inbox address plm minted (mint_upload's
        `put_path`), at this client's own base URL: plm mints `put_url` on
        its public address, which a container beside plm may not route to."""
        return self._put_file(put_path, path)  # type: ignore[return-value]

    def put_raw(self, number: str, *, path: Path, name: str, version: str,
                role: str, actor: str, message: str) -> dict:
        """The `raw` transport (PLMGIT_PUSH_BINARY_TRANSPORT=raw): PUT the
        bytes straight onto the DRAFT, `PUT /api/items/<n>/files/<name>`."""
        qs = urllib.parse.urlencode({"role": role, "version": version,
                                     "actor": actor, "message": message})
        return self._put_file(  # type: ignore[return-value]
            f"/api/items/{urllib.parse.quote(number)}"
            f"/files/{urllib.parse.quote(name)}?{qs}", path)
