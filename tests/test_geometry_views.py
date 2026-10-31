"""The official view of an assembly is its OWN mesh (the manifest's primary /
viz file); its posed children are the explode set beside it, never a
replacement for it (Geoff, 2026-10-24: CID-ASM-0001's bungee keeper lives in
the assembly's own source and in no child)."""
import pytest

from plm import db, thumbs


def _seed(conn, tag: str, own: bool, posed_children: int, unposed_children: int = 0):
    now = db.now()
    def item(num, kind):
        conn.execute("INSERT OR IGNORE INTO items (number, name, kind, project, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?)", (num, num, kind, "geomtest", now, now))
        return conn.execute("SELECT id FROM items WHERE number=?", (num,)).fetchone()[0]
    def rev(iid):
        cur = conn.execute("INSERT INTO revisions (item_id, version, state, created_at) "
                           "VALUES (?,?,?,?)", (iid, "0.1.0", "PROTOTYPE", now))
        return cur.lastrowid
    def stl(rid, path, role):
        conn.execute("INSERT INTO files (revision_id, role, path, sha256, size, media) "
                     "VALUES (?,?,?,?,?,?)", (rid, role, path, "0" * 64, 100, "model/stl"))
    asm = item(f"{tag}-ASM-0001", "assembly"); arev = rev(asm)
    if own:
        stl(arev, "meshes/asm.stl", "viz")
    n = 0
    for role, count in (("viz", posed_children), ("mesh", unposed_children)):
        for _ in range(count):
            n += 1
            cid = item(f"{tag}-PRT-{n:04d}", "part"); crev = rev(cid)
            stl(crev, f"p{n}.stl", role)
            conn.execute("INSERT INTO bom (parent_rev_id, find_number, child_item_id, "
                         "child_version) VALUES (?,?,?,?)", (arev, n * 10, cid, "0.1.0"))
    return arev


@pytest.fixture
def conn():
    db.init()
    with db.connect() as c:
        yield c


def test_own_mesh_is_the_view_and_children_the_explode_set(conn):
    arev = _seed(conn, "GVA", own=True, posed_children=2)
    bodies = thumbs.geometry_bodies(conn, arev)
    assert [(b["view"], b["path"]) for b in bodies] == [
        ("assembled", "meshes/asm.stl"), ("component", "p1.stl"), ("component", "p2.stl")]
    # the picture is the parts, the own mesh is there for the viewer's toggle
    assert [b["path"] for b in thumbs.picture_bodies(bodies)] == ["p1.stl", "p2.stl"]
    assert len(thumbs.component_bodies(bodies)) == 2
    st = thumbs.explode_status(conn, arev)
    assert st["explodable"] is True and st["bodies"] == 2


def test_unposed_child_keeps_only_the_own_mesh(conn):
    arev = _seed(conn, "GVB", own=True, posed_children=1, unposed_children=1)
    bodies = thumbs.geometry_bodies(conn, arev)
    assert [(b["view"], b["path"]) for b in bodies] == [("assembled", "meshes/asm.stl")]
    assert thumbs.picture_bodies(bodies) == bodies       # nothing posed: the own mesh
    st = thumbs.explode_status(conn, arev)
    assert st["explodable"] is False and st["bodies"] == 0
    assert "unposed" in st["reason"]


def test_no_own_mesh_means_components_are_the_picture(conn):
    arev = _seed(conn, "GVC", own=False, posed_children=2)
    bodies = thumbs.geometry_bodies(conn, arev)
    assert [b["view"] for b in bodies] == ["component", "component"]
    assert thumbs.picture_bodies(bodies) == bodies
    assert thumbs.explode_status(conn, arev)["explodable"] is True
