"""Retiring filedrop, step 1: object-store rows become `lfs` rows.

A filedrop row's bytes are already in the revision's tree (stage 17 put them
there as a pointer or a plain blob) and already in plm's LFS store.
`migrate_cleanup --only filedrop-repoint` finds the tree
path by the bytes and re-points the row; `split` then reads that `lfs` row from
the store and must build the very same tree entry it built from the external
row. New bytes go straight to the LFS store with PLM_BLOBSTORE_DEFAULT=lfs.
"""
import hashlib
import subprocess
import time

import pytest

from plm import config, db, gitrepo, lfs, migrate_cleanup, split, storage

NUM = "TST-PRT-0001"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """One item, one released revision, three filedrop rows: a mesh and a
    small plain file whose objects are in the store, and one that is not."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "plm.sqlite3")
    monkeypatch.setenv("PLM_LFS_ROOT", str(tmp_path / "lfs"))
    repos = tmp_path / "repos"
    monkeypatch.setattr(gitrepo, "REPOS", repos)
    monkeypatch.setattr(split, "REPOS", repos)
    db.init()
    mesh = b"solid floor_plate\nendsolid floor_plate\n" * 40
    small = b"pin 1 -> GND\n"
    ghost = b"never imported\n"
    lfs.put_bytes(mesh)
    lfs.put_bytes(small)
    now = time.time()
    with db.connect() as conn:
        iid = conn.execute(
            "INSERT INTO items (number, name, kind, project, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            (NUM, "floor plate", "part", "tst", now, now)).lastrowid
        rid = conn.execute(
            "INSERT INTO revisions (item_id, version, vmaj, vmin, vpat, state, "
            "summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "0.1.0", 0, 1, 0, "RELEASED", "first", now)).lastrowid
        for name, data in ((f"{NUM}_v0.1.0_floor_plate.stl", mesh),
                           ("wiring.dat", small), ("ghost.stl", ghost)):
            conn.execute(
                "INSERT INTO files (revision_id, role, path, sha256, size, "
                "backend, key) VALUES (?,?,?,?,?,?,?)",
                (rid, "artifact", name, _sha(data), len(data), "lfs",
                 _sha(data)))
        split.run(conn, repos=repos, normalize=True, lfs_layout=True)
        # Prod's state when the rule runs: the bytes are in the tree, and the
        # rows still name the retired filedrop store. The fixture builds the
        # tree from `lfs` rows and then gives them the spelling prod had.
        conn.execute("UPDATE files SET backend='filedrop', "
                     "path=?||'/v0.1.0/'||path, key='plm/'||?||'/v0.1.0/'||path "
                     "WHERE revision_id=?", (NUM, NUM, rid))
    return {"repos": repos, "rid": rid, "mesh": mesh, "small": small,
            "ghost": ghost}


def _rows(rid):
    with db.connect() as conn:
        return {r["sha256"]: dict(r) for r in conn.execute(
            "SELECT * FROM files WHERE revision_id=?", (rid,))}


def _tree(repos):
    out = subprocess.run(
        ["git", "--git-dir", str(repos / f"{NUM}.git"), "ls-tree", "v0.1.0"],
        check=True, capture_output=True, text=True).stdout
    return {line.split("\t")[1]: line.split()[2] for line in out.splitlines()}


def test_stable_name_is_idempotent_on_a_normalised_name():
    once = gitrepo.stable_name(f"{NUM}_v0.3.0_floor_plate.stl", NUM)
    assert once == "floor_plate.stl"
    assert gitrepo.stable_name(once, NUM) == once
    assert gitrepo.stable_name("floor_plate~2.stl", NUM) == "floor_plate~2.stl"


def test_repoint_dry_run_apply_idempotent_and_split_agrees(reg):
    before = _tree(reg["repos"])
    assert "floor_plate.stl" in before and "wiring.dat" in before
    with db.connect() as conn:
        dry = migrate_cleanup.rule_filedrop_repoint(conn, apply=False,
                                                    actor="t")
    assert dry["found"] == 3 and dry["changed"] == 2
    text = "\n".join(dry["lines"])
    assert f"{NUM}_v0.1.0_floor_plate.stl -> floor_plate.stl" in text
    assert "wiring.dat -> wiring.dat" in text
    assert "ghost.stl: SKIP" in text and "not in the LFS store" in text
    assert all(r["backend"] == "filedrop" for r in _rows(reg["rid"]).values())

    with db.connect() as conn:
        res = migrate_cleanup.rule_filedrop_repoint(conn, apply=True, actor="t")
    assert res["changed"] == 2
    rows = _rows(reg["rid"])
    m, s, g = (rows[_sha(reg[k])] for k in ("mesh", "small", "ghost"))
    assert (m["backend"], m["key"], m["path"]) == (
        "lfs", _sha(reg["mesh"]), "floor_plate.stl")
    assert (s["backend"], s["key"], s["path"]) == (
        "lfs", _sha(reg["small"]), "wiring.dat")
    assert (g["backend"], g["path"]) == ("filedrop", f"{NUM}/v0.1.0/ghost.stl")
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM context WHERE body LIKE ?",
            (f"%{migrate_cleanup._REPOINT_NOTE}%",)).fetchone()[0] == 1
        again = migrate_cleanup.rule_filedrop_repoint(conn, apply=True,
                                                      actor="t")
        assert again["changed"] == 0 and again["found"] == 1
        # the re-pointed rows are not "unrecoverable": their bytes are in LFS
        unrec = migrate_cleanup.rule_unrecoverable(conn, apply=False, actor="t")
        assert not any("floor_plate" in ln or "wiring" in ln
                       for ln in unrec["lines"])

    kind, p = storage.resolve(m)
    assert kind == "file" and p == lfs.object_path(_sha(reg["mesh"]))
    assert p.read_bytes() == reg["mesh"]

    # split reads the `lfs` rows from the store: the same tree entries, byte
    # for byte, as stage 17 built from the external rows
    with db.connect() as conn:
        out = split.run(conn, repos=reg["repos"], force=True)
    after = _tree(reg["repos"])
    assert after["floor_plate.stl"] == before["floor_plate.stl"]
    assert after["wiring.dat"] == before["wiring.dat"]
    assert not out["problems"].get("files_not_recoverable")


def test_split_reports_an_lfs_row_whose_object_is_missing(reg):
    with db.connect() as conn:
        migrate_cleanup.rule_filedrop_repoint(conn, apply=True, actor="t")
        conn.execute("UPDATE files SET key=? WHERE path='wiring.dat'",
                     ("f" * 64,))
        out = split.run(conn, repos=reg["repos"], force=True)
    assert any("wiring.dat" in x and "not in plm's LFS store" in x
               for x in out["problems"]["files_not_recoverable"])


def test_cli_dry_run(reg, capsys):
    rc = migrate_cleanup.main(["--dry-run", "--only", "filedrop-repoint",
                               "--project", "tst", "--db",
                               str(config.DB_PATH)])
    assert rc == 0
    assert "filedrop-repoint" in capsys.readouterr().out


def test_storage_put_with_lfs_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PLM_LFS_ROOT", str(tmp_path / "lfs"))
    monkeypatch.setattr(config, "STORAGE_DEFAULT", "lfs")
    assert storage.choose_backend("big.stl", "artifact",
                                  config.STORAGE_MIN_SIZE + 1) == "lfs"
    data = b"\x00binary mesh\x01" * 100
    row = storage.put(data, number=NUM, version="0.1.0",
                      name="sub/dir/big.stl", role="artifact")
    assert row == {"backend": "lfs", "key": _sha(data), "path": "big.stl",
                   "sha256": _sha(data), "size": len(data), "role": "artifact"}
    kind, p = storage.resolve(row)
    assert kind == "file" and p.read_bytes() == data
    src = tmp_path / "in.stl"
    src.write_bytes(data + b"!")
    row2 = storage.put_file(src, number=NUM, version="0.1.0", name="in.stl")
    assert row2["backend"] == "lfs" and row2["key"] == _sha(data + b"!")
    assert storage.local_copy(row2).read_bytes() == data + b"!"


def test_repoint_catalog_item_rows_by_sha_alone(reg):
    """A purchased item has no tree (one TOML in catalog.git, no files), so its
    filedrop row is re-pointed on the store alone and keeps its display name."""
    data = b"%PDF-1.4 datasheet\n"
    lfs.put_bytes(data)
    now = time.time()
    with db.connect() as conn:
        iid = conn.execute(
            "INSERT INTO items (number, name, kind, project, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            ("TST-PUR-0001", "switch", "purchased", "tst", now, now)).lastrowid
        rid = conn.execute(
            "INSERT INTO revisions (item_id, version, vmaj, vmin, vpat, state, "
            "summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "0.1.0", 0, 1, 0, "RELEASED", "bought", now)).lastrowid
        conn.execute(
            "INSERT INTO files (revision_id, role, path, sha256, size, "
            "backend, key) VALUES (?,?,?,?,?,?,?)",
            (rid, "doc", "TST-PUR-0001/v0.1.0/KS-33_datasheet.pdf", _sha(data),
             len(data), "filedrop", "plm/TST-PUR-0001/v0.1.0/KS-33_datasheet.pdf"))
        res = migrate_cleanup.rule_filedrop_repoint(
            conn, apply=True, actor="t", project="tst")
        row = dict(conn.execute("SELECT * FROM files WHERE revision_id=?",
                                (rid,)).fetchone())
    assert "catalog item, no tree" in "\n".join(res["lines"])
    assert (row["backend"], row["key"], row["path"]) == (
        "lfs", _sha(data), "KS-33_datasheet.pdf")
    kind, p = storage.resolve(row)
    assert kind == "file" and p.read_bytes() == data
