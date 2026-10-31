"""The `physical-diff` check (PLM_REVIEW_MODEL §2.10): a check on the PULL
REQUEST whose output carries GitHub's `images`. The pure parts — the head-set
key, the layout, the generated .scad — are string/number tests; the serving is
an API test against a scratch DB. Rendering itself needs OpenSCAD and is
verified in the image ."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from plm import api, checks, db


def H(item, head, base):
    return {"item": item, "head": head, "base": base}


def test_set_key_is_order_free_and_moves_with_a_head():
    a = [H("X-PRT-0001", "a" * 40, "b" * 40), H("X-ASM-0001", "c" * 40, "")]
    k = checks.set_key(a)
    assert len(k) == 40 and k == checks.set_key(list(reversed(a)))
    assert checks.set_key([H("X-PRT-0001", "e" * 40, "b" * 40), a[1]]) != k
    assert checks.set_key([H("X-PRT-0001", "a" * 40, "f" * 40), a[1]]) != k


@pytest.mark.parametrize("n,cols,rows,more", [
    (1, 1, 1, 0), (2, 2, 1, 0), (3, 3, 1, 0), (4, 2, 2, 0), (6, 3, 2, 0),
    (9, 3, 3, 0), (12, 4, 3, 0), (15, 4, 3, 3)])
def test_layout(n, cols, rows, more):
    L = checks.picture_layout(n)
    assert (L["cols"], L["rows"], L["more"]) == (cols, rows, more)
    assert len(L["tiles"]) == min(n, checks.MAX_TILES)
    assert L["width"] == 1200 and L["height"] >= 630
    for x, y, w, h in L["tiles"]:
        assert x >= 0 and x + w <= L["width"]
        assert y >= L["header"] and y + h + L["label"] <= L["height"]
    if cols > 1:                                    # ~400 px tiles at 3 a row
        assert L["tiles"][1][0] > L["tiles"][0][0] + L["tiles"][0][2] - 1


def test_scad_diff_form():
    s = checks.physdiff_scad("/w/old.stl", "/w/new.stl", eps=0.1)
    assert 'module O() { import("/w/old.stl", convexity=10); }' in s
    assert 'color("#CF222E") difference() { O(); grow(0.1) N(); }' in s
    assert 'color("#1A7F37") difference() { N(); grow(0.1) O(); }' in s
    assert 'color("#CC7A12", 0.45) N();' in s
    assert "translate(v) children()" in s


def test_scad_fallback_and_one_sided_forms():
    g = checks.physdiff_scad("/w/o.stl", "/w/n.stl", mode="ghost")
    assert 'color("#3E6FB0", 0.35) O();' in g and 'color("#CC7A12") N();' in g
    assert "difference" not in g
    assert 'color("#1A7F37") N();' in checks.physdiff_scad(None, "/w/n.stl")
    assert 'color("#CF222E", 0.6) O();' in checks.physdiff_scad("/w/o.stl", None)
    q = checks.physdiff_scad(None, '/w/a"b.stl')
    assert 'import("/w/a\\"b.stl"' in q              # escaped for OpenSCAD
    gh = checks.physdiff_scad(None, None, ghosts=["/w/g.stl"])
    assert 'color("#8C959F", 0.25) import("/w/g.stl"' in gh
    # an assembly: lists of bodies, the cheap form — what the posed tile draws
    a = checks.physdiff_scad(["/w/o1.stl"], ["/w/n1.stl", "/w/n2.stl"], mode="ghost",
                             ghosts=["/w/g.stl"])
    assert 'module N() { import("/w/n1.stl", convexity=10); import("/w/n2.stl", convexity=10); }' in a
    assert 'color("#3E6FB0", 0.35) O();' in a and 'color("#CC7A12") N();' in a
    # a re-pinned child with no old body is amber in the ghost form, not green
    assert 'color("#CC7A12") N();' in checks.physdiff_scad([], ["/w/n.stl"], mode="ghost")


def test_registry_entry_is_a_change_check():
    assert checks.applies_to("CHG-12", "0" * 40) == ["physical-diff"]
    assert checks.CHECKS["physical-diff"]["advisory"] is True
    for m in ("concept", "prototype", "released"):
        assert "physical-diff" not in checks.required_for(m)
    assert checks.RUNNERS["physical-diff"] is checks.check_physical_diff
    for spelling in ("CHG-7", "#7", "7", "chg7"):
        s = checks.subject(spelling)
        assert s.is_change and s.number == "CHG-7" and s.cid == 7
    s = checks.subject("X-PRT-0001")
    assert not s.is_change and s.number == "X-PRT-0001"


def test_images_column_is_added_to_an_old_table(tmp_path):
    c = sqlite3.connect(tmp_path / "old.sqlite3")
    c.execute("CREATE TABLE checks (id INTEGER PRIMARY KEY, number TEXT, "
              "sha TEXT, name TEXT, points_json TEXT NOT NULL DEFAULT '')")
    assert checks._add_columns(c) is True
    assert "images_json" in {r[1] for r in c.execute("PRAGMA table_info(checks)")}
    assert checks._add_columns(c) is False


# -- serving -----------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    db.init()
    with db.connect() as conn:
        from plm import changes, proposals
        proposals.ensure(conn)
        changes.ensure(conn)
        checks.ensure_schema(conn)
        from plm import gitmode
        gitmode.set_mode(conn, project="pdtest", mode="write", actor="test")
        now = db.now()
        conn.execute("INSERT OR IGNORE INTO items (number, name, kind, project, "
                     "created_at, updated_at) VALUES ('PDF-PRT-0001','bezel','part',"
                     "'pdtest',?,?)", (now, now))
        iid = conn.execute("SELECT id FROM items WHERE number='PDF-PRT-0001'").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO changes (id, project, title, state, author, "
                     "created_at, updated_at, submitted_at) VALUES (941,'pdtest',"
                     "'USB notch','open','pbot',?,?,?)", (now, now, now))
        conn.execute("INSERT OR REPLACE INTO proposals (id, item_id, state, change_id, "
                     "head_ref, head_sha, version, title, review_base_how, created_at, "
                     "updated_at) VALUES (941,?, 'open', 941, 'refs/heads/draft/x', ?, "
                     "'0.2.1', 't', 'none', ?, ?)", (iid, "a" * 40, now, now))
    return TestClient(api.app, follow_redirects=False)


def _key():
    from plm import changes
    with db.connect() as conn:
        return checks.set_key(changes.head_set(conn, 941))


def _row(key, status, images=()):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO checks (item_id, number, sha, name, status, summary, "
            "images_json, source, created_at) VALUES (0,'CHG-941',?,'physical-diff',"
            "?,?,?,'builtin',?) ON CONFLICT(number, sha, name) DO UPDATE SET "
            "status=excluded.status, summary=excluded.summary, "
            "images_json=excluded.images_json",
            (key, status, "1 mesh changed", json.dumps(list(images)), db.now()))
        conn.commit()


def test_image_404_retry_after_while_pending_then_200(client, monkeypatch):
    asked = []
    monkeypatch.setattr(checks, "enqueue", lambda *a, **k: asked.append((a, k)))
    key = _key()
    _row(key, "running")
    url = "/api/repo/CHG-941/checks/physical-diff/images/0.png"
    r = client.get(url)
    assert r.status_code == 404 and r.headers["retry-after"] == "10"
    assert r.json()["generating"] is True
    img = {"alt": "#941", "caption": "#941", "image_url":
           checks.image_url("CHG-941", "physical-diff", key, 0)}
    _row(key, "pass", [img])
    # finished but the cache was cleared: re-rendered on demand, still 404 —
    # and asked for ONCE however many polls (the row is claimed `queued`)
    r = client.get(url)
    assert r.status_code == 404 and r.headers["retry-after"] == "10"
    assert asked and asked[-1][0] == ("CHG-941", key)
    for _ in range(3):
        assert client.get(url + f"?sha={key}").status_code == 404
    assert len(asked) == 1
    # a superseded key (a member moved): 410, nothing rendered
    assert client.get(url + "?sha=" + "f" * 40).status_code == 410
    assert len(asked) == 1
    _row(key, "pass", [img])
    d = checks.image_dir("CHG-941", key, "physical-diff")
    d.mkdir(parents=True, exist_ok=True)
    (d / "0.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    r = client.get(url + f"?sha={key}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/api/repo/CHG-941/checks/physical-diff/images/0.png"
                      "?sha=../../etc").status_code == 404


def test_the_board_and_the_pull_request_carry_images(client, monkeypatch):
    monkeypatch.setattr(checks, "enqueue", lambda *a, **k: None)
    key = _key()
    img = {"alt": "x", "caption": "x", "image_url":
           checks.image_url("CHG-941", "physical-diff", key, 0)}
    _row(key, "pass", [img])
    b = client.get("/api/repo/CHG-941/checks").json()
    assert b["sha"] == key and b["applies"] == ["physical-diff"]
    [row] = b["checks"]
    assert row["images"] == [img] and row["advisory"] is True
    from plm import changes
    with db.connect() as conn:
        own = changes.own_checks(conn, changes.row(conn, 941))
        assert own["sha"] == key and own["checks"][0]["images"] == [img]
        assert own["rollup"]["state"] == "green"
        c = changes.row(conn, 941)
        [line] = changes._check_lines(conn, c, changes.members(conn, 941))   # review.requested carries it
        assert line["image_url"] == "http://testserver" + img["image_url"]
        b = changes.brief(conn, changes.row(conn, 941))
        assert b["ref"] == "#941" and b["number"] == "CHG-941" and "pull" not in b
        assert {k: b[k] for k in changes.names(941)} == changes.names(941)


# -- the store, the renderer, the picture ---------------------------------------

def test_images_are_keyed_by_check_name_and_pruned_per_name(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "IMAGES", tmp_path)
    with pytest.raises(ValueError):
        checks.image_dir("CHG-1", "a" * 40, "../x")
    d = checks.image_dir("#1", "a" * 40, "physical-diff")
    assert d == tmp_path / "CHG-1" / ("a" * 40) / "physical-diff"
    import os
    for i, k in enumerate("abcde"):
        x = checks.image_dir("CHG-1", k * 40, "physical-diff")
        x.mkdir(parents=True)
        (x / "0.png").write_bytes(b"x" * 10)
        os.utime(x, (1000 + i, 1000 + i))
    checks._prune_images("CHG-1", "a" * 40, "physical-diff")
    kept = sorted(p.name[0] for p in (tmp_path / "CHG-1").iterdir())
    assert kept == ["a", "d", "e"]                    # `keep` + the newest two
    monkeypatch.setattr(checks, "IMAGES_MAX_BYTES", 15)
    checks._prune_images("CHG-1", "e" * 40, "physical-diff")
    assert sorted(p.name[0] for p in (tmp_path / "CHG-1").iterdir()) == ["e"]
    checks.drop_images("CHG-1")
    assert not (tmp_path / "CHG-1").exists()


def test_closing_a_pull_request_drops_its_images(client, tmp_path, monkeypatch):
    from plm import changes
    monkeypatch.setattr(checks, "IMAGES", tmp_path)
    d = checks.image_dir("CHG-941", "b" * 40, "physical-diff")
    d.mkdir(parents=True)
    with db.connect() as conn:
        changes._touch(conn, 941, title="USB notch")          # not a close
        assert d.is_dir()
        changes._touch(conn, 941, state="withdrawn")
        assert not (tmp_path / "CHG-941").exists()
        conn.rollback()


def test_render_png_is_the_one_openscad_call(tmp_path, monkeypatch):
    from plm import thumbs
    # a stand-in OpenSCAD: `-o <out>` → writes the out file
    monkeypatch.setattr(thumbs, "OPENSCAD", ("sh", "-c", 'printf png > "$2"', "sh"))
    out = tmp_path / "x.png"
    assert thumbs.render_png("cube(1);", out, extra=("--camera=0,0,0",)) is True
    assert out.read_bytes() == b"png" and [p.name for p in tmp_path.iterdir()] == ["x.png"]
    assert thumbs.render_png("cube(1);", out, unless_exists=True) is False
    monkeypatch.setattr(thumbs, "OPENSCAD", ("sh", "-c", "echo boom >&2; exit 1", "sh"))
    with pytest.raises(RuntimeError, match="boom"):
        thumbs.render_png("cube(1);", tmp_path / "y.png")
    assert not (tmp_path / "y.png").exists()
    assert checks.OPENSCAD is thumbs.OPENSCAD or checks.OPENSCAD == ("xvfb-run", "-a", "openscad")


def test_mesh_facts_bbox_volume_only():
    import struct
    # one closed tetrahedron, binary STL
    v = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
    faces = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    body = b"".join(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0) for a, b, c in faces)
    stl = b"\0" * 80 + struct.pack("<I", len(faces)) + body
    full, light = checks.mesh_facts(stl), checks.mesh_facts(stl, edges=False)
    assert light["bbox_mm"] == full["bbox_mm"] == [10, 10, 10]
    assert light["volume_mm3"] == full["volume_mm3"] == pytest.approx(166.6667, abs=1e-3)
    assert "open_edges" in full and "open_edges" not in light


def _cmp(files, meshes, subs=()):
    return {"base": {"version": "0.1.0"}, "head": {"version": "0.2.0"},
            "files": files, "meshes": meshes, "submodules": list(subs)}


def test_tiles_come_from_the_files_tabs_mesh_chooser(monkeypatch):
    """`compare()`'s `meshes` (renames paired, primary first) decide the tiles;
    unchanged bytes, a same-bytes rename and the print/viz twin draw nothing
    extra."""
    blob = lambda b: {"blob": b}                              # noqa: E731
    files = [
        {"path": "a.stl", "kind": "mesh", "status": "modified", "same_content": False,
         "ident_base": blob("1"), "ident_head": blob("2")},
        {"path": "viz/a.stl", "kind": "mesh", "status": "modified", "same_content": False,
         "ident_base": blob("1"), "ident_head": blob("2")},   # the same bytes: a twin
        {"path": "lfs.stl", "kind": "mesh", "status": "modified", "same_content": True,
         "ident_base": blob("3"), "ident_head": blob("4")},
        {"path": "old.stl", "kind": "mesh", "status": "deleted", "ident_base": blob("5")},
        {"path": "new.stl", "kind": "mesh", "status": "added", "ident_head": blob("5")},
        {"path": "b.3mf", "kind": "mesh", "status": "added", "ident_head": blob("6")},
    ]
    side = lambda p, src="tree": {"path": p, "source": src}  # noqa: E731
    meshes = [
        {"base": side("prim.stl", "external"), "head": side("prim.stl", "external")},
        {"base": side("a.stl"), "head": side("a.stl")},
        {"base": side("viz/a.stl"), "head": side("viz/a.stl")},
        {"base": side("lfs.stl"), "head": side("lfs.stl")},
        {"base": side("old.stl"), "head": side("new.stl")},   # a rename, same bytes
        {"base": None, "head": side("b.3mf")},
        {"base": side("same.stl"), "head": side("same.stl")},  # not in files: unchanged
    ]
    monkeypatch.setattr(checks.gitrepo, "compare", lambda *a, **k: _cmp(files, meshes))
    t = checks._tiles_for({"item": "X-PRT-0001", "base": "b" * 40, "head": "h" * 40})
    assert [x["path"] for x in t] == ["a.stl, viz/a.stl", "b.3mf"]
    assert t[0]["old"] == [("X-PRT-0001", "b" * 40, "a.stl")]
    assert t[1]["old"] == [] and t[1]["new"] == [("X-PRT-0001", "h" * 40, "b.3mf")]
    from plm import roles
    assert roles.is_scad_importable("b.3mf") and roles.is_scad_importable("x.STL")
    assert not roles.is_scad_importable("x.step") and not roles.is_renderable_media("model/3mf")


def test_the_picture_renders_tiles_in_parallel_and_serves_them_small(tmp_path, monkeypatch):
    PIL = pytest.importorskip("PIL.Image")
    import threading
    monkeypatch.setattr(checks, "IMAGES", tmp_path / "img")
    monkeypatch.setattr(checks, "MESH_CACHE", tmp_path / "mesh")
    tiles = [{"item": "X-PRT-0001", "path": f"m{i}.stl", "versions": ("0.1.0", "0.2.0"),
              "old": [("X-PRT-0001", "b" * 40, f"m{i}.stl")],
              "new": [("X-PRT-0001", "h" * 40, f"m{i}.stl")], "ghosts": []}
             for i in range(4)]
    monkeypatch.setattr(checks, "_tiles_for", lambda h: tiles)
    monkeypatch.setattr(checks.gitrepo, "blob", lambda *a, **k: (b"solid x\nendsolid x\n", {}))
    monkeypatch.setattr(checks, "_measure", lambda *a: {"bbox_mm": [1, 2, 3], "volume_mm3": 6.0})
    threads = set()

    def fake_render(scad, out, **kw):
        threads.add(threading.get_ident())
        PIL.new("RGB", checks.TILE_PX, "#888").save(out)
        return True
    from plm import thumbs
    monkeypatch.setattr(thumbs, "render_png", fake_render)

    class C:
        number, cid, title, sha = "CHG-941", 941, "USB notch", "c" * 40
        heads = [{"item": "X-PRT-0001", "head": "h" * 40, "base": "b" * 40}]
    res = checks.check_physical_diff(C)
    assert res["status"] == "pass" and res["summary"] == "4 meshes changed", res["details_md"]
    d = checks.image_dir("CHG-941", "c" * 40, "physical-diff")
    assert sorted(p.name for p in d.iterdir()) == ["0.png", "1.png", "2.png", "3.png", "4.png"]
    assert max(PIL.open(d / "1.png").size) <= checks.TILE_SERVE_PX[0]
    assert PIL.open(d / "0.png").size[0] == 1200
    assert [im["image_url"].split("/images/")[1].split("?")[0] for im in res["images"]] == \
        ["0.png", "1.png", "2.png", "3.png", "4.png"]
    assert res["images"][0]["caption"].startswith("#941 · USB notch")
    assert len(threads) > 1                                   # a pool, not a loop
    # nothing rendered → no directory is made
    monkeypatch.setattr(thumbs, "render_png", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    C.sha = "d" * 40
    res = checks.check_physical_diff(C)
    assert res["status"] == "warn" and not (tmp_path / "img" / "CHG-941" / ("d" * 40)).exists()


def test_the_palette_is_one_palette():
    """The four diff tokens: web/index.html's CSS is the source;
    diffwidgets' fallback and the picture's COLOURS hold to it."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    css = dict(re.findall(r"--plm-(old|new|removed|added):\s*(#[0-9A-Fa-f]{6})",
                          (root / "web" / "index.html").read_text()))
    js = dict(re.findall(r"'--plm-(old|new|removed|added)':\s*'(#[0-9A-Fa-f]{6})'",
                         (root / "web" / "diffwidgets.js").read_text()))
    py = {k: v for k, v in checks.COLOURS.items() if k != "other"}
    assert len(css) == 4 and {k: v.upper() for k, v in css.items()} == \
        {k: v.upper() for k, v in js.items()} == {k: v.upper() for k, v in py.items()}


# -- the hero card: the completed assembly's picture, first (§2.10) -----------

def _htile(item, path, mode, image=1, **kw):
    return {"item": item, "path": path, "mode": mode, "label": f"{item} · {path}",
            "image": image, **kw}


def _hmem(item, pid, after=(), order=0):
    return {"item": item, "proposal": pid, "after": list(after), "order": order}


def _hrow(*tiles):
    return {"number": "CHG-9", "sha": "k" * 40, "facts": {"tiles": list(tiles)}}


MEMS = [_hmem("X-PRT-0001", 1, order=0), _hmem("X-PRT-0002", 2, order=1),
        _hmem("X-ASM-0001", 3, after=[1, 2], order=2)]


def test_hero_picks_the_root_not_its_children():
    row = _hrow(_htile("X-PRT-0001", "a.stl", "diff", 1),
               _htile("X-ASM-0001", "dock.stl", "diff", 2, primary=True))
    h = checks.hero_tiles(row, MEMS)
    assert [x["item"] for x in h] == ["X-ASM-0001"]
    assert h[0]["image_url"].endswith("/images/2.png?sha=" + "k" * 40)
    assert h[0]["compare_mesh"] == "dock.stl"


def test_hero_prefers_primary_over_an_earlier_diff_htile():
    row = _hrow(_htile("X-ASM-0001", "other.stl", "diff", 1),
               _htile("X-ASM-0001", "main.stl, main_viz.stl", "diff", 2, primary=True))
    h = checks.hero_tiles(row, MEMS)[0]
    assert h["path"].startswith("main.stl") and h["compare_mesh"] == "main.stl"


def test_hero_old_rows_fall_back_to_the_first_diff_htile():
    row = _hrow(_htile("X-ASM-0001", "assembly (posed)", "posed", 1),
               _htile("X-ASM-0001", "first.stl", "diff", 2),
               _htile("X-ASM-0001", "second.stl", "diff", 3))
    assert checks.hero_tiles(row, MEMS)[0]["path"] == "first.stl"


def test_hero_posed_is_the_last_resort_and_has_no_3d_link():
    row = _hrow(_htile("X-ASM-0001", "assembly (posed)", "posed", 1))
    h = checks.hero_tiles(row, MEMS)[0]
    assert h["mode"] == "posed" and h["compare_mesh"] == ""


def test_hero_skips_unrendered_tiles_and_empty_rows():
    row = _hrow(_htile("X-ASM-0001", "a.stl", "", image=None, primary=True, error="timeout"))
    assert checks.hero_tiles(row, MEMS) == []
    assert checks.hero_tiles(None, MEMS) == []
    assert checks.hero_tiles(_hrow(), MEMS) == []


def test_hero_part_only_pr_and_cycle_fallback():
    one = [_hmem("X-PRT-0001", 1)]
    row = _hrow(_htile("X-PRT-0001", "a.stl", "diff", 1, primary=True))
    assert [x["item"] for x in checks.hero_tiles(row, one)] == ["X-PRT-0001"]
    cyc = [_hmem("X-PRT-0001", 1, after=[2], order=0), _hmem("X-PRT-0002", 2, after=[1], order=1)]
    row = _hrow(_htile("X-PRT-0002", "b.stl", "diff", 1))
    assert [x["item"] for x in checks.hero_tiles(row, cyc)] == ["X-PRT-0002"]


def test_hero_delta_is_the_checks_own_wording():
    f = {"old": {"bbox_mm": [240, 216, 32], "volume_mm3": 221598.9},
         "new": {"bbox_mm": [240, 200, 32], "volume_mm3": 209225.2}}
    row = _hrow(_htile("X-ASM-0001", "dock.stl", "diff", 1, primary=True, facts=f))
    assert checks.hero_tiles(row, MEMS)[0]["delta"] == (
        "volume -12,373.7 mm³ (-5.6 %) · bbox 240.0×216.0×32.0 → 240.0×200.0×32.0 mm")
    # both files there but one would not measure: still a change, not "added"
    assert checks._what(None, f["new"], (True, True)).startswith("volume — · bbox")
    assert checks._what(None, f["new"]) == "added"
