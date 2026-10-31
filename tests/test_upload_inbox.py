"""plm's upload inbox and snippets live on plm's own disk.

upload() mints a token and `PUT /api/uploads/<token>`; the bytes land in
CACHE_DIR/inbox/<token>/<name>, hashed ONCE as they stream, and the row records
the arrived sha256/size; claim compares against the row; checkin adopts the
file into the LFS store and consumes the stage. share_3d's bytes live in
CACHE_DIR/snippets/<token>/. A row on a retired backend (filedrop, local) is
refused with the command that re-points it.
"""
import base64
import errno
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plm import (api, config, db, identity, lfs, readiness, snippets, staging,
                 storage)

NUM = "INB-PRT-0001"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _stl(tris: int = 2) -> bytes:
    """A valid binary STL: 80-byte header, count, 50 bytes per triangle."""
    return b"\0" * 80 + tris.to_bytes(4, "little") + b"\1" * (50 * tris)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "plm.sqlite3")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("PLM_LFS_ROOT", str(tmp_path / "lfs"))
    # meshes go to plm's LFS store, so a check-in needs no vault commit
    monkeypatch.setattr(config, "STORAGE_DEFAULT", "lfs")
    monkeypatch.setattr(config, "STORAGE_MIN_SIZE", 0)
    db.init()
    return TestClient(api.app, follow_redirects=False)


def _mint(client, name="bracket.stl", **kw) -> dict:
    r = client.post("/api/uploads", json={"name": name, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _inbox(token: str):
    return config.CACHE_DIR / "inbox" / token


def test_put_claim_checkin_round_trip(client):
    body = _stl(3)
    up = _mint(client, size=len(body), sha256=_sha(body))
    tok = up["token"]
    assert up["handle"] == f"staging/{tok}/bracket.stl"
    assert up["put_path"] == f"/api/uploads/{tok}"
    assert up["put_url"] == f"{config.PUBLIC_URL}{up['put_path']}"
    assert 'curl -f -T bracket.stl -H "Authorization: Bearer $PLM_TOKEN"' \
        in up["instructions"]
    assert f"handle='{up['handle']}'" in up["instructions"]
    assert "filedrop" not in up["instructions"]

    # nothing landed yet: status says so, without failing
    st = client.get("/api/uploads/status", params={"handle": up["handle"]})
    assert st.json()["arrived"] is False

    r = client.put(f"/api/uploads/{tok}", content=body)
    assert r.status_code == 200, r.text
    assert (r.json()["sha256"], r.json()["size"]) == (_sha(body), len(body))
    assert (_inbox(tok) / "bracket.stl").read_bytes() == body
    # written once
    again = client.put(f"/api/uploads/{tok}", content=body)
    assert again.status_code == 409 and "drop_upload" in again.json()["detail"]

    st = client.get("/api/uploads/status", params={"handle": up["handle"]})
    assert st.json()["arrived"] is True and st.json()["sha256"] == _sha(body)

    assert client.post("/api/items", json={
        "name": "bracket", "project": "inb", "number": NUM}).status_code == 200
    r = client.post(f"/api/items/{NUM}/checkin", json={
        "version": "0.1.0", "summary": "first", "files": [
            {"name": "bracket.stl", "role": "artifact",
             "handle": up["handle"], "sha256": _sha(body),
             "size": len(body)}]})
    assert r.status_code == 200, r.text
    [f] = r.json()["files"]
    assert (f["backend"], f["key"], f["sha256"]) == ("lfs", _sha(body),
                                                     _sha(body))
    # consumed: the directory and the ticket are gone
    assert not _inbox(tok).exists()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0] == 0
    # and the bytes are served back from the store
    got = client.get(f"/api/blob?id={f['id']}")
    assert got.status_code == 200 and got.content == body

    # file_link: same shape, no presign, opens only where plm is reachable
    link = client.get(f"/api/files/{f['id']}/link").json()
    assert link["url"] == f"/api/blob?id={f['id']}"
    assert link["expiring_url"] is None and "serves this link itself" in link["note"]


def test_a_declared_sha256_mismatch_is_refused_at_the_put(client):
    """The declared numbers are checked when the bytes land: a 400 printing
    both hashes, nothing kept, and the handle still takes the right bytes."""
    body = _stl(1)
    up = _mint(client, sha256="0" * 64)
    r = client.put(f"/api/uploads/{up['token']}", content=body)
    assert r.status_code == 400
    assert "sha256 mismatch" in r.json()["detail"]
    assert _sha(body) in r.json()["detail"] and "0" * 64 in r.json()["detail"]
    d = _inbox(up["token"])
    assert not d.exists() or not any(d.iterdir())
    st = client.get("/api/uploads/status", params={"handle": up["handle"]})
    assert st.json()["arrived"] is False
    # a declared SIZE that disagrees is refused the same way
    up2 = _mint(client, size=len(body) + 1)
    r = client.put(f"/api/uploads/{up2['token']}", content=body)
    assert r.status_code == 400 and "size mismatch" in r.json()["detail"]
    # ...and a check-in claiming other numbers than what landed is a 400
    up3 = _mint(client)
    assert client.put(f"/api/uploads/{up3['token']}",
                      content=body).status_code == 200
    client.post("/api/items", json={"name": "x", "project": "inb",
                                    "number": NUM})
    r = client.post(f"/api/items/{NUM}/checkin", json={
        "version": "0.1.0", "summary": "x", "files": [
            {"name": "bracket.stl", "handle": up3["handle"],
             "sha256": "1" * 64, "size": len(body)}]})
    assert r.status_code == 400 and "mismatch" in r.json()["detail"]


def test_bytes_are_hashed_once_on_arrival(client, monkeypatch):
    """After the PUT, status, claim and the check-in never re-read the file:
    every re-hash path is booby-trapped, and the revision still lands —
    adopted into the store by the numbers the PUT measured (a hard link)."""
    body = _stl(5)
    up = _mint(client, size=len(body), sha256=_sha(body))
    assert client.put(f"/api/uploads/{up['token']}",
                      content=body).status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM staging WHERE token=?",
                           (up["token"],)).fetchone()
    assert (row["sha256"], row["size"]) == (_sha(body), len(body))
    assert row["received_at"]
    src = _inbox(up["token"]) / "bracket.stl"
    ino = src.stat().st_ino

    def boom(*a, **k):
        raise AssertionError("re-hashed bytes that were already measured")
    monkeypatch.setattr(storage, "sha256_file", boom)
    monkeypatch.setattr(lfs, "put_stream", boom)
    monkeypatch.setattr(lfs, "put_file", boom)

    st = client.get("/api/uploads/status", params={"handle": up["handle"]})
    assert st.json()["arrived"] is True and st.json()["size"] == len(body)
    client.post("/api/items", json={"name": "b", "project": "inb",
                                    "number": NUM})
    r = client.post(f"/api/items/{NUM}/checkin", json={
        "version": "0.1.0", "summary": "x", "files": [
            {"name": "bracket.stl", "filedrop_key": up["handle"],   # alias
             "sha256": _sha(body), "size": len(body)}]})
    assert r.status_code == 200, r.text
    obj = lfs.object_path(_sha(body))
    assert obj.read_bytes() == body and obj.stat().st_ino == ino


def test_oversize_body_is_a_413_and_leaves_no_file(client, monkeypatch):
    monkeypatch.setattr(config, "RAW_UPLOAD_MAX", 100)
    up = _mint(client)
    r = client.put(f"/api/uploads/{up['token']}", content=b"x" * 101)
    assert r.status_code == 413 and "PLM_RAW_UPLOAD_MAX" in r.json()["detail"]
    d = _inbox(up["token"])
    assert not (d / "bracket.stl").exists()
    assert not any(d.iterdir()) if d.exists() else True
    # a body that LIES about its length is caught while streaming
    with db.connect() as conn:
        row, sink = staging.open_inbox(conn, up["token"])
    with pytest.raises(staging.StagingError) as exc:
        with sink:
            sink.write(b"y" * 60)
            sink.write(b"y" * 60)
    assert exc.value.code == 413 and not (d / "bracket.stl").exists()


def test_put_to_an_unknown_or_expired_token(client):
    assert client.put("/api/uploads/nosuchtoken0", content=b"x").status_code == 404
    up = _mint(client)
    with db.connect() as conn:
        conn.execute("UPDATE staging SET expires_at=1 WHERE token=?",
                     (up["token"],))
    assert client.put(f"/api/uploads/{up['token']}",
                      content=b"x").status_code == 410


def test_drop_and_purge_delete_the_directory(client):
    a, b = _mint(client, name="a.stl"), _mint(client, name="b.stl")
    for up in (a, b):
        client.put(f"/api/uploads/{up['token']}", content=_stl())
        assert _inbox(up["token"]).is_dir()
    r = client.delete("/api/uploads", params={"handle": a["handle"]})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert not _inbox(a["token"]).exists()

    with db.connect() as conn:
        conn.execute("UPDATE staging SET expires_at=1 WHERE token=?",
                     (b["token"],))
    res = client.post("/api/uploads/gc").json()
    assert res["deleted"] == 1 and res["handles"] == [b["handle"]]
    assert not _inbox(b["token"]).exists()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0] == 0


def test_an_old_bucket_handle_is_not_an_upload_handle(client):
    """`plm/staging/<token>/<name>` named bytes in a bucket plm no longer
    reads: the honest answer is the 404 that says upload() again."""
    body = _stl()
    up = _mint(client)
    client.put(f"/api/uploads/{up['token']}", content=body)
    with db.connect() as conn:
        for bad in (f"plm/{up['handle']}", f"other/{up['handle']}"):
            with pytest.raises(staging.StagingError) as exc:
                staging.claim(conn, bad)
            assert exc.value.code == 404
            assert "not an upload handle" in str(exc.value)
        assert staging.claim(conn, up["handle"])["sha256"] == _sha(body)


def test_snippet_create_stream_delete_on_local_disk(client):
    body = _stl(4)
    r = client.post("/api/snippets", json={
        "name": "look.stl", "content_b64": base64.b64encode(body).decode(),
        "note": "like it?"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    d = config.CACHE_DIR / "snippets" / tok
    assert (d / "look.stl").read_bytes() == body
    got = client.get(f"/api/snippets/{tok}/blob")
    assert got.status_code == 200 and got.content == body
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("inline")

    # from a staged upload: the inbox file MOVES under the snippet token
    up = _mint(client, name="big.stl")
    client.put(f"/api/uploads/{up['token']}", content=body)
    s2 = client.post("/api/snippets", json={
        "name": "big.stl", "handle": up["handle"],
        "sha256": _sha(body), "size": len(body)}).json()
    assert not _inbox(up["token"]).exists()
    assert client.get(f"/api/snippets/{s2['token']}/blob").content == body

    assert client.delete(f"/api/snippets/{tok}").json()["deleted"] is True
    assert not d.exists()
    assert client.get(f"/api/snippets/{tok}/blob").status_code == 404

    # the janitor removes an expired one's directory too
    with db.connect() as conn:
        conn.execute("UPDATE snippets SET expires_at=1 WHERE token=?",
                     (s2["token"],))
        assert snippets.purge_expired(conn)["deleted"] == 1
    assert not (config.CACHE_DIR / "snippets" / s2["token"]).exists()


def test_a_snippet_that_is_not_an_stl_is_refused(client):
    up = _mint(client, name="x.stl")
    client.put(f"/api/uploads/{up['token']}", content=b"not a mesh at all")
    r = client.post("/api/snippets", json={"handle": up["handle"]})
    assert r.status_code == 400 and "not an STL" in r.json()["detail"]


@pytest.mark.parametrize("backend", ["filedrop", "local"])
def test_resolve_refuses_a_retired_backend_with_the_fix(backend):
    with pytest.raises(storage.StorageError) as exc:
        storage.resolve({"backend": backend, "key": f"{NUM}/v0.1.0/a.stl"})
    msg = str(exc.value)
    assert f"backend '{backend}' is retired" in msg
    assert "--only filedrop-repoint" in msg and "" in msg
    assert storage.RETIRED_BACKENDS[backend] in msg


def test_a_manifest_may_declare_only_vault():
    for backend in ("filedrop", "local"):
        with pytest.raises(storage.StorageError) as exc:
            storage.external_key(NUM, "0.1.0", "a.stl", backend=backend)
        assert "retired" in str(exc.value) and "into the tree" in str(exc.value)
    with pytest.raises(storage.StorageError):
        storage.external_key(NUM, "0.1.0", "a.stl", backend="git")
    assert storage.external_key(NUM, "0.1.0", "a.stl") == ("vault", "")


def test_lfs_adopt_links_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("PLM_LFS_ROOT", str(tmp_path / "lfs"))
    data = b"adopt me\n" * 50
    src = tmp_path / "in.stl"
    src.write_bytes(data)
    assert lfs.adopt(src, _sha(data), len(data)) == (_sha(data), len(data))
    obj = lfs.object_path(_sha(data))
    assert obj.read_bytes() == data
    assert obj.stat().st_ino == src.stat().st_ino and src.exists()  # a link
    assert not any((tmp_path / "lfs" / "tmp").iterdir())
    # a size that disagrees with the file is refused, nothing stored
    other = tmp_path / "other.bin"
    other.write_bytes(b"x" * 10)
    with pytest.raises(lfs.LfsError):
        lfs.adopt(other, _sha(b"x" * 10), 11)
    assert not lfs.has(_sha(b"x" * 10))

    def no_link(err):
        def link(a, b):
            raise OSError(err, os.strerror(err))
        return link
    # across filesystems: copied, the source stays
    monkeypatch.setattr(lfs.os, "link", no_link(errno.EXDEV))
    assert lfs.adopt(other, _sha(b"x" * 10), 10)
    assert lfs.object_path(_sha(b"x" * 10)).read_bytes() == b"x" * 10
    assert other.exists()
    assert lfs.object_path(_sha(b"x" * 10)).stat().st_ino != other.stat().st_ino
    # no hard links at all: renamed into place
    third = tmp_path / "third.bin"
    third.write_bytes(b"y" * 7)
    monkeypatch.setattr(lfs.os, "link", no_link(errno.EPERM))
    lfs.adopt(third, _sha(b"y" * 7), 7)
    assert lfs.object_path(_sha(b"y" * 7)).read_bytes() == b"y" * 7
    assert not third.exists()


def test_readiness_splits_retired_from_external_in_one_query(client):
    now = time.time()
    with db.connect() as conn:
        iid = conn.execute(
            "INSERT INTO items (number, name, kind, project, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            (NUM, "x", "part", "inb", now, now)).lastrowid
        rid = conn.execute(
            "INSERT INTO revisions (item_id, version, vmaj, vmin, vpat, state, "
            "summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "0.1.0", 0, 1, 0, "RELEASED", "x", now)).lastrowid
        for name, backend in (("a.stl", "filedrop"), ("b.stl", "local"),
                              ("c.stl", "elsewhere"), ("d.stl", "lfs")):
            conn.execute(
                "INSERT INTO files (revision_id, role, path, sha256, size, "
                "backend, key) VALUES (?,?,?,?,?,?,?)",
                (rid, "artifact", name, "0" * 64, 5, backend, name))
        seen = []
        conn.set_trace_callback(seen.append)
        rep_ = readiness.report(conn, "inb")
        conn.set_trace_callback(None)
    scans = [q for q in seen if "FROM files f" in q and "backend" in q
             and "NOT IN" in q]
    assert len(scans) == 1
    assert rep_["external"]["count"] == 1          # c.stl only
    [blk] = [f for f in rep_["verdicts"]["read"]["findings"]
             if f["key"] == "retired-backend"]
    assert blk["level"] == "block" and len(blk["items"]) == 2
    assert "filedrop-repoint" in blk["fix"]
    [info] = [f for f in rep_["verdicts"]["read"]["findings"]
              if f["key"] == "external"]
    assert info["count"] == 1


def test_raw_put_streams_through_the_sink(client, monkeypatch):
    client.post("/api/items", json={"name": "r", "project": "inb",
                                    "number": NUM})
    assert client.post(f"/api/items/{NUM}/checkin", json={
        "version": "0.1.0", "state": "DRAFT", "summary": "draft",
        "files": [{"name": "seed.stl", "content_b64": base64.b64encode(
            _stl(1)).decode()}]}).status_code == 200
    body = _stl(3)
    r = client.put(f"/api/items/{NUM}/files/raw.stl?role=artifact",
                   content=body)
    assert r.status_code == 200, r.text
    [f] = [x for x in r.json()["files"] if x["path"] == "raw.stl"]
    assert (f["backend"], f["sha256"]) == ("lfs", _sha(body))
    assert lfs.object_path(_sha(body)).read_bytes() == body
    cache = config.CACHE_DIR / "urlcache"
    assert not cache.exists() or not any(cache.iterdir())   # temp released

    monkeypatch.setattr(config, "RAW_UPLOAD_MAX", 100)
    r = client.put(f"/api/items/{NUM}/files/big.stl", content=b"z" * 101)
    assert r.status_code == 413 and "PLM_RAW_UPLOAD_MAX" in r.json()["detail"]
    assert not cache.exists() or not any(cache.iterdir())
    # a body over the cap without a usable Content-Length is cut off while
    # streaming, and nothing is left behind
    def chunks():
        yield b"z" * 60
        yield b"z" * 60
    r = client.put(f"/api/items/{NUM}/files/big.stl", content=chunks())
    assert r.status_code == 413
    assert not cache.exists() or not any(cache.iterdir())
    r = client.put(f"/api/items/{NUM}/files/empty.stl", content=b"")
    assert r.status_code == 400 and "empty body" in r.json()["detail"]


def test_a_non_json_put_body_passes_through_untouched(client, monkeypatch):
    """Under a verified identity the middleware rewrites `actor` in a JSON
    write — but a raw PUT to the inbox is never read by it (it would pull a
    whole mesh into memory to learn it is not JSON), so the bytes land exactly
    as sent even when they happen to be a JSON object with an `actor`."""
    monkeypatch.setattr(identity, "identity_of",
                        lambda request, conn: {"sub": "geoff-sub"})
    monkeypatch.setattr(identity, "resolve_user",
                        lambda conn, ident: {"username": "geoff"})
    seen = []
    real = identity.IdentityMiddleware._rewrite_actor

    async def spy(request, actor):
        seen.append((request.method, request.url.path))
        return await real(request, actor)

    monkeypatch.setattr(identity.IdentityMiddleware, "_rewrite_actor",
                        staticmethod(spy))

    # control: a JSON write IS rewritten — the minted row records geoff
    up = _mint(client, name="doc.json", actor="somebody")
    assert up["actor"] == "geoff"

    body = json.dumps({"actor": "somebody", "pad": "x" * 100}).encode()
    r = client.put(f"/api/uploads/{up['token']}", content=body)
    assert r.status_code == 200, r.text
    assert (_inbox(up["token"]) / "doc.json").read_bytes() == body
    assert ("PUT", f"/api/uploads/{up['token']}") not in seen
    assert ("POST", "/api/uploads") in seen


class _FakeStream:
    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status_code = body, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body

    def iter_bytes(self, chunk_size=1 << 20):
        for i in range(0, len(self.body), 7):
            yield self.body[i:i + 7]


def test_upload_by_url_lands_in_the_inbox_and_a_bad_hash_leaves_nothing(
        client, monkeypatch):
    body = _stl(2)
    monkeypatch.setattr(staging.httpx, "stream",
                        lambda *a, **k: _FakeStream(body))
    with db.connect() as conn:
        res = staging.stage_from_url(conn, name="bus.stl",
                                     url="https://drop.example/x",
                                     sha256=_sha(body), actor="t")
        tok = res["handle"].split("/")[1]
        assert res["handle"] == f"staging/{tok}/bus.stl"
        assert (_inbox(tok) / "bus.stl").read_bytes() == body
        assert staging.claim(conn, res["handle"])["size"] == len(body)
        assert [p.name for p in _inbox(tok).iterdir()] == ["bus.stl"]

        before = set((config.CACHE_DIR / "inbox").iterdir())
        with pytest.raises(staging.StagingError) as exc:
            staging.stage_from_url(conn, name="bus.stl",
                                   url="https://drop.example/x",
                                   sha256="0" * 64)
        assert exc.value.code == 400 and "mismatch" in str(exc.value)
        assert set((config.CACHE_DIR / "inbox").iterdir()) == before


def test_plm_git_puts_the_bytes_through_its_one_request_path(tmp_path,
                                                             monkeypatch):
    """plm-git's put_upload joins the minted put_path with ITS base URL and
    goes through Plm._request: bearer token, streamed file body, plm's own
    `detail` on a refusal."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]
                                    / "plm-git"))
    from plm_git import plmapi
    sent = []

    class _Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def urlopen(req, timeout=0):
        sent.append((req.get_method(), req.full_url, dict(req.header_items()),
                     req.data.read()))
        return _Resp(b'{"ok": true, "handle": "staging/abc/x.stl"}')

    monkeypatch.setattr(plmapi.urllib.request, "urlopen", urlopen)
    f = tmp_path / "x.stl"
    f.write_bytes(b"mesh bytes")
    plm = plmapi.Plm("http://plm:8090/", token="tok")
    assert plm.put_upload("/api/uploads/abc", f)["handle"] == "staging/abc/x.stl"
    method, url, head, data = sent[0]
    assert (method, url, data) == ("PUT", "http://plm:8090/api/uploads/abc",
                                   b"mesh bytes")
    assert head["Authorization"] == "Bearer tok"
    assert head["Content-length"] == str(len(b"mesh bytes"))
    plm.put_raw(NUM, path=f, name="x.stl", version="0.1.0", role="artifact",
                actor="a", message="m")
    assert sent[1][1].startswith(f"http://plm:8090/api/items/{NUM}/files/x.stl?")
