"""Thumbnail rendering: a revision's geometry → a small PNG.

Rendered with the OpenSCAD that is already in this image (import the STL, shoot
it headless under xvfb + software GL). Thumbnails are derived data: they live
in a cache next to the SQLite index (off the synced volume), keyed by the
source file's identity, and are never committed to the vault.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from . import config, db, roles, storage, vault

CACHE = config.CACHE_DIR / "thumbs"
MAX_STL_BYTES = 120 << 20     # relief maps get big; beyond this, skip
TIMEOUT_S = 150
SIZE = "560,420"

# A dashboard of fresh items can request many thumbnails at once; renders are
# CPU-bound OpenSCAD runs, so keep only a couple in flight — and the
# physical-diff tiles (plm/checks.py) take the same slots, through render_png.
_RENDER_SLOTS = threading.Semaphore(2)
#: OpenSCAD, headless (software GL under xvfb) — every invocation in plm
OPENSCAD = ("xvfb-run", "-a", "openscad")


def render_png(scad: str, out: Path, *, size: str = SIZE,
               timeout: int = TIMEOUT_S, extra: tuple = (),
               unless_exists: bool = False) -> bool:
    """THE OpenSCAD → PNG call: a preview render (color() and alpha survive —
    a CGAL render would discard colour) of `scad`, throttled by
    `_RENDER_SLOTS`, written to a per-render temp name beside `out` and moved
    into place, so a reader never sees half a PNG. `extra`: camera,
    projection, colour scheme. False when `unless_exists` and another render
    got there first; raises RuntimeError (with OpenSCAD's own words) or
    subprocess.TimeoutExpired."""
    # Temp output beside `out`: os.replace must not cross filesystems (/tmp is
    # tmpfs; the cache is a bind mount). The name is unique PER RENDER, not per
    # key: three revisions can share one STL (hence one key), and with two
    # render slots two of them rendered into the same `<key>.tmp.png` at once —
    # the loser's file was replaced under it, a phantom 404 on a thumbnail that
    # does exist.
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.tmp.png")
    with tempfile.TemporaryDirectory(prefix="plmscad") as td:
        src = Path(td) / "scene.scad"
        src.write_text(scad)
        try:
            with _RENDER_SLOTS:
                if unless_exists and out.is_file():
                    return False
                res = subprocess.run(
                    [*OPENSCAD, "-o", str(tmp), f"--imgsize={size}", "--viewall",
                     "--autocenter", *extra, str(src)],
                    capture_output=True, timeout=timeout, cwd=td,
                    env={**os.environ, "HOME": td})
            if res.returncode != 0 or not tmp.is_file():
                err = (res.stderr or res.stdout).decode(errors="replace").strip()
                raise RuntimeError(err[-600:] or f"openscad exited {res.returncode}")
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
    return True


def _pick_stl(conn, rev_id: int) -> dict | None:
    """The one renderable body that represents this revision, as a whole files row.

    'viz' first: a deliberately-assembled representation beats a
    print-oriented mesh for anything visual. Within a role, prefer a
    vault-backed row — bytes already on disk render without a fetch — but an
    offloaded mesh is still a valid answer (storage.local_copy
    materializes it), so offloaded geometry never silently disappears from the
    thumbnailer or the viewer.

    SELECTED BY MEASURED FORMAT, NOT BY FILENAME. This used to be
    `path LIKE '%.stl'`, which had two faults: it matched nothing for a
    content-addressed object-store key (so flipping STORAGE_DEFAULT would have
    emptied the thumbnailer), and it disagreed with `role` on 60 of the STLs on
    prod. `media` is sniffed from the bytes, so it is right in both cases.

    Role still orders the candidates — that is role's actual job, saying which
    body is FOR looking at — but it no longer decides what counts as geometry.
    `artifact` stays in the ordering because 228 legacy rows are still parked in
    that junk drawer and some of them are real meshes.
    """
    pred, args = roles.media_sql("media", "renderable")
    rows = conn.execute(
        f"SELECT * FROM files WHERE revision_id=? AND {pred} "
        "ORDER BY CASE role WHEN 'viz' THEN 0 WHEN 'mesh' THEN 1 "
        "              WHEN 'artifact' THEN 2 ELSE 3 END, "
        "         CASE WHEN COALESCE(backend,'') IN ('','vault') THEN 0 ELSE 1 END, "
        "         size DESC",
        (rev_id, *args)).fetchall()
    return dict(rows[0]) if rows else None


# -- one palette, two renderers ----------------------------------------------
# Colour by FIRST-LEVEL COMPONENT: every body that descends from the same direct
# BOM child of the viewed revision shares a colour. So a lantern shows its dome,
# cage and internals as three colours even when the walk had to recurse three
# levels to reach actual meshes — which is the question a human asks of an
# assembly picture ("which bit is which sub-assembly?"), not "which of these 13
# meshes is which".
#
# ⭐ DEFINED ONCE, SERVER-SIDE, AND HANDED TO THE CLIENT. The thumbnailer renders
# with OpenSCAD and the viewer renders with three.js; if each kept its own palette
# the same assembly would be orange-then-blue in the thumbnail and blue-then-orange
# in the viewer, and nobody could tell whether they were looking at the same thing.
# geometry_bodies stamps `color` onto every body, the API passes it through, and
# both renderers are told what to draw rather than deciding.
#
# These are the ten the viewer already used, in order, so a single-part render is
# byte-identical to before.
BODY_COLORS = ("#ff7a2f", "#4f9cf0", "#4ade80", "#ffcf3f", "#ec4899",
               "#8b5cf6", "#2dd4bf", "#f0564a", "#a3e635", "#818cf8")


def _scad_str(p) -> str:
    """One path, safe inside an OpenSCAD double-quoted string.

    OpenSCAD's lexer understands `\\` and `\"`; anything else that could end the
    literal early is what L8 was about. Belt and braces with
    `storage.CACHE_EXT`: this is the sink, that is the source.
    """
    return str(p).replace("\\", "\\\\").replace('"', '\\"')


def _rgb(hexcolor: str) -> tuple[float, float, float]:
    """'#ff7a2f' -> (1.0, 0.478, 0.184), the form OpenSCAD's color() takes."""
    h = hexcolor.lstrip("#")
    return tuple(round(int(h[i:i + 2], 16) / 255.0, 4) for i in (0, 2, 4))


def geometry_bodies(conn, rev_id: int, depth: int = 6,
                    max_bodies: int = 60, split: bool = True) -> list[dict]:
    """The renderable bodies of one revision: its own primary STL, else ONE
    primary STL per unsuppressed BOM occurrence — recursing through nested
    sub-assemblies (which usually have no geometry of their own) until real
    parts are found. Shared CAD coordinates throughout. Used by the composite
    thumbnailer and the viewer's geometry endpoint.

    Each body carries enough of its files row (id/backend/key/sha256) that
    callers can fetch the bytes from any storage backend — the viewer builds
    /api/blob?id= from it, the thumbnailer feeds it to storage.local_copy."""
    out: list[dict] = []
    seen: set[tuple] = set()

    def own_stl(rid: int) -> dict | None:
        return _pick_stl(conn, rid)

    # First-level component -> colour index. An ordinary dict, so the index is
    # assignment order, which is BOM order (get_bom sorts by find_number then
    # number) — stable across calls, so a body keeps its colour between the
    # thumbnail and the viewer and across a page reload.
    groups: dict[str, int] = {}

    def child_bodies(rid: int) -> list:
        """Primary bodies on unsuppressed first-level children.

        These are candidates for a separable assembly view only when every body
        is an explicitly posed ``viz``. A print-oriented child mesh has no BOM
        occurrence transform and must never displace the parent's own assembled
        visualization.
        """
        found = []
        for line in db.get_bom(conn, rid):
            if line.get("suppressed"):
                continue
            cr = (db.find_revision(conn, line["child_number"], line["child_version"])
                  or db.find_revision(conn, line["child_number"]))
            body = own_stl(cr["id"]) if cr else None
            if body:
                found.append(body)
        return found

    def _add(f: dict, number: str, name: str, depth_from_top: int,
             group: str, group_name: str, view: str) -> None:
        ident = (f.get("backend") or "vault", f.get("key") or "", f["path"])
        if ident in seen:
            return
        seen.add(ident)
        # A body with no first-level ancestor IS the revision's own mesh, so it
        # groups under itself — one part, one colour, colour 0.
        key = group or number
        gi = groups.setdefault(key, len(groups))
        out.append({"number": number, "name": name, "path": f["path"],
                    "id": f["id"], "backend": f.get("backend") or "",
                    "key": f.get("key") or "",
                    "sha256": f.get("sha256") or "",
                    "size": f.get("size") or 0,
                    "role": f.get("role") or "",
                    # WHICH FIRST-LEVEL COMPONENT this body belongs to, and the
                    # colour both renderers must use for it.
                    "group": key,
                    "group_name": group_name or name,
                    "group_index": gi,
                    "color": BODY_COLORS[gi % len(BODY_COLORS)],
                    # WHERE THIS BODY CAME FROM — a fact, not a guess, and the
                    # input to the placement warning below. 'own': this
                    # revision's own mesh, whole and correctly placed by
                    # definition. Anything else was reached by walking the BOM,
                    # and plm applies an IDENTITY transform to it, because bom
                    # rows carry no occurrence transform at all.
                    "origin": "own" if depth_from_top == 0 else "bom",
                    # 'assembled': the official view (the own mesh);
                    # 'component': one of the separable bodies the explode
                    # slider shows instead. A revision with no own mesh has
                    # components only, and they are then also the picture.
                    "view": view,
                    })

    def walk(rid: int, number: str, name: str, d: int, depth_from_top: int = 0,
             group: str = "", group_name: str = "") -> None:
        if len(out) >= max_bodies:
            return
        f = own_stl(rid)
        # ⭐ SPLIT THE THING YOU ARE LOOKING AT INTO ITS COMPONENTS.
        #
        # An assembly usually has a single baked `_assembled.stl` viz mesh, and
        # taking it (the `return` below) means ONE body — so it cannot be coloured
        # per component, soloed, or hidden a piece at a time. KML-ASM-0000 v0.8.2
        # is exactly that: four sub-assemblies fused into one file.
        #
        # Its children carry their own viz meshes with positions baked in, so
        # rendering THEM gives the same picture in separable pieces. Verified on
        # that revision: the union of the four children's bounding boxes is
        # X[-125.2,125.2] Y[-125.2,125.2] Z[8.0,489.5] — identical to the parent's
        # own mesh, i.e. a faithful decomposition, not an approximation.
        #
        # ONLY AT THE TOP, only when there are at least two children to split
        # into, and only when ALL selected child bodies explicitly declare role
        # ``viz``. A mesh/artifact is print-oriented unless occurrence placement
        # data says otherwise (and plm has none), so splitting onto one would
        # replace a correct parent assembly with a heap at the model origin.
        # Deeper down the own-mesh short-circuit is exactly right: a sub-assembly's
        # own viz is the one body that represents it, which is what makes "a
        # sub-assembly is one colour" true.
        #
        # ⚠️ The honest limit: if a parent's baked mesh contained geometry that no
        # child has, splitting loses it. Nothing in this vault does that (the
        # assembled mesh is generated FROM the children by one OpenSCAD source),
        # and the bbox check above is the evidence — but a future hand-authored
        # assembly mesh would need `split=False`.
        # THE OFFICIAL VIEW IS THE REVISION'S OWN MESH (Geoff, 2026-10-24:
        # "the official assembly should be defined in the manifest"). It is
        # what plm.toml's `files.primary` / `viz` names, and it is the only
        # body that carries geometry modelled in the assembly's own source —
        # CID-ASM-0001's bungee keeper and cable clip live in dock.scad, not
        # in any child, so a children-only picture does not show the change.
        # The split below therefore ADDS the components (view="component")
        # beside the own mesh (view="assembled") instead of replacing it: the
        # thumbnail and the viewer draw the parts (picture_bodies; the
        # viewer's "official mesh" toggle swaps to the own body), and
        # explode_status reasons over the components alone.
        view = "assembled" if depth_from_top == 0 else "component"
        if f and split and depth_from_top == 0 and d > 0:
            kids = child_bodies(rid)
            if len(kids) >= 2 and all(k.get("role") == "viz" for k in kids):
                _add(f, number, name, depth_from_top, group, group_name, "assembled")
                f = None                     # …and walk the components too
        if f:
            _add(f, number, name, depth_from_top, group, group_name, view)
            return
        if d <= 0:
            return
        for line in db.get_bom(conn, rid):
            if line.get("suppressed"):
                continue
            child_rev = db.find_revision(conn, line["child_number"], line["child_version"]) \
                or db.find_revision(conn, line["child_number"])
            if child_rev:
                # The group is fixed at the FIRST level and carried down: a part
                # three levels deep still colours as the top-level component it
                # belongs to. `group or ...` is what pins it — only the outermost
                # call sets it, every deeper call inherits.
                walk(child_rev["id"], line["child_number"], line["child_name"],
                     d - 1, depth_from_top + 1,
                     group or line["child_number"],
                     group_name or line["child_name"])

    row = conn.execute(
        """SELECT i.number, i.name FROM revisions r JOIN items i ON i.id=r.item_id
           WHERE r.id=?""", (rev_id,)).fetchone()
    walk(rev_id, row["number"] if row else "", row["name"] if row else "", depth)
    return out


# ------------------------------------------------------------------ placement
# PLM HAS NO OCCURRENCE TRANSFORMS. A Teamcenter BOM occurrence carries the
# position and orientation of the child within the parent; plm's `bom` table
# carries qty, unit, find number and suppression — and no transform. So when
# geometry_bodies walks the BOM, every child mesh is added to the scene with an
# IDENTITY transform, and the picture is only correct if the CAD author happened
# to model each part in ASSEMBLY coordinates.
#
# For this vault, that splits cleanly and observably:
#
#   sub-assembly `viz` meshes ARE assembled — OpenSCAD wrote them with each part
#   already translated into place (KML-ASM-1000 spans Z 349→489, KML-ASM-2000
#   Z 8→342: they stack into a lantern).
#
#   individual part meshes are NOT — each is modelled at its own origin, so all
#   thirteen of them start at Z=0 and land in a heap.
#
# KML-ASM-0000 v0.6.0 resolves to three sub-assembly viz bodies and looks right;
# EVERY earlier revision of it resolves to individual parts and looks like a
# pile. Same code, same viewer — the difference is entirely which files exist.
#
# plm cannot invent the missing transforms, so it must stop presenting the heap
# as an assembly. This reports what plm actually knows: whether any body was
# placed by assumption rather than by data.

def placement(bodies: list[dict]) -> dict:
    """Can this set of bodies be trusted as an ASSEMBLED view?

    A body's placement can be trusted when EITHER:

      - it is the revision's OWN mesh (origin='own') — one body, whole, and
        placed by definition; or
      - its declared role is `viz`, which in roles.ROLES means precisely "a
        deliberately-assembled representation … an assembly posed in shared
        coordinates". That is the AUTHOR'S DECLARATION that the position is baked
        into the file, and it is exactly what exists to let people say.

    A body reached through the BOM whose role is anything else is an individual
    part drawn at its own modelled origin, and THOSE are the ones that heap up.

    The first version of this warned whenever any body came through the BOM,
    which flagged KML-ASM-0000 v0.6.0 — the one revision that renders correctly.
    A warning that fires on the good case trains people to ignore it, so the
    declared role does the work instead. Still provenance, never geometry: no
    bounding boxes are read to produce a caption.
    """
    n = len(bodies)
    def placed(b: dict) -> bool:
        return b.get("origin") == "own" or (b.get("role") or "") == "viz"
    loose = [b for b in bodies if not placed(b)]
    if n <= 1 or not loose:
        return {"assembled": True, "bodies": n, "unplaced": 0, "note": ""}
    names = ", ".join(sorted({b.get("number") or "?" for b in loose})[:6])
    return {
        "assembled": False, "bodies": n, "unplaced": len(loose),
        "note": (f"{len(loose)} of {n} bodies are positioned by ASSUMPTION, not "
                 "by data — plm's BOM lines carry no occurrence transform, so "
                 f"each is drawn at its own modelled origin ({names}"
                 + (" …" if len(loose) > 6 else "")
                 + "). Parts modelled individually will overlap at the origin "
                 "instead of stacking. A revision whose children are assembled "
                 "`viz` meshes shows correctly, because the position is baked "
                 "into those files."),
    }


def picture_bodies(bodies: list[dict]) -> list[dict]:
    """What a PICTURE of the revision shows — the thumbnail on the dashboard,
    the registry and the item page: the individual parts, coloured per
    component, exactly what the viewer opens on (Geoff, 2026-10-26). The
    revision's own mesh is the picture only when there are no posed parts to
    draw (a single part, or an assembly whose children are not posed)."""
    return component_bodies(bodies) or bodies


def component_bodies(bodies: list[dict]) -> list[dict]:
    """What the explode slider separates: the components the walk found beside
    (or instead of) the assembled body."""
    return [b for b in bodies if b.get("view") != "assembled"]


def geometry_paths(conn, rev_id: int) -> list[str]:
    return [b["path"] for b in geometry_bodies(conn, rev_id)]


# ---------------------------------------------------------------- presence
# PRESENCE IS DATA, NOT A GUESS. A client must never discover that a revision
# has no picture by asking for the picture and eating a 404: one page of a
# 25-line BOM used to fire 25 doomed requests (times the retry helper's
# attempts). The server already knows — it owns the files table — so every read
# that a UI row is built from carries `thumb`: a ready-to-use URL, or None
# meaning "there is nothing to render, do not ask".
#
# has_geometry answers that question the cheap way: DB only (no vault reads, no
# OpenSCAD), short-circuiting at the first body found, and kind-aware so a
# purchased screw or a document never walks a structure.

# Kinds whose revisions can carry — or inherit through a BOM — a mesh. A
# purchased fastener, a document, a firmware image has none and never will, so
# the walk is skipped on the kind alone. (Its OWN files are still checked: if
# somebody does attach an STL to a purchased item, the picture is real and this
# must not hide it.)
# `asset` is here because reference geometry is the POINT of an asset: the
# LiDAR-scanned camper a rack has to fit is a mesh, and "does it fit?" is a
# question you answer by looking. An asset has no BOM to walk (it is barred from
# structure), so the walk costs one query against its own files.
GEOMETRY_KINDS = frozenset({"part", "assembly", "coupon", "asset"})


def has_own_stl(conn, rev_id: int) -> bool:
    """Does this revision carry renderable geometry of its own?

    One indexed lookup (idx_files_media), LIMIT 1 — no row is materialized.
    Asks the MEASURED format, not the filename, so it answers
    correctly for content-addressed blobs.

    The name is kept: it is the honest question ("is there a mesh here"), it is
    what the fabrication surfaces phrase their answer against, and renaming it
    would churn a dozen call sites for nothing.
    """
    pred, args = roles.media_sql("media", "renderable")
    return conn.execute(
        f"SELECT 1 FROM files WHERE revision_id=? AND {pred} LIMIT 1",
        (rev_id, *args)).fetchone() is not None


def has_geometry(conn, rev_id: int, kind: str = "", depth: int = 6,
                 memo: dict | None = None) -> bool:
    """Is there anything to render for this revision? The boolean twin of
    geometry_bodies, answered without building the list: it stops at the FIRST
    body found, so an assembly with its own viz STL costs a single query and a
    25-line hardware BOM costs one query per child.

    `kind` is the owning item's kind when the caller already has it (every row
    that carries a thumbnail does), which saves the lookup and enables the
    no-walk shortcut. `memo` is a dict the caller keeps for the life of one
    payload/request, so a child that appears on three BOM lines — or a revision
    asked about by three different cards — is resolved once.
    """
    if not rev_id:
        return False
    if memo is None:
        memo = {}
    if rev_id in memo:
        return memo[rev_id]
    memo[rev_id] = False        # also the cycle guard: a BOM loop answers 'no'
    if not kind:
        row = conn.execute(
            "SELECT i.kind FROM revisions r JOIN items i ON i.id=r.item_id "
            "WHERE r.id=?", (rev_id,)).fetchone()
        kind = row["kind"] if row else ""
    found = has_own_stl(conn, rev_id)
    if not found and depth > 0 and kind in GEOMETRY_KINDS:
        for line in db.get_bom(conn, rev_id):
            if line.get("suppressed"):
                continue        # 150% BOM: a suppressed occurrence is not there
            child = db.find_revision(conn, line["child_number"], line["child_version"]) \
                or db.find_revision(conn, line["child_number"])
            if child and has_geometry(conn, child["id"], line["child_kind"],
                                      depth - 1, memo):
                found = True
                break           # short circuit: one body is the whole answer
    memo[rev_id] = found
    return found


# Bump when the RULES of the picture change (what picture_bodies draws, the
# palette, the camera): the URL every page hands a browser carries it, so a
# browser that cached the old picture under the old URL (api_thumb says
# max-age=300) asks again instead of showing yesterday's render. The on-disk
# cache keys itself on the inputs and needs no bump.
#   1  the own mesh / children composite        (to 2026-10-25)
#   2  the individual parts, coloured per component (2026-10-26)
THUMB_GEN = 2


def thumb_url(number: str, version: str = "") -> str:
    """THE thumbnail URL shape, in one place (api_thumb serves it; it ignores
    `g`, which exists only to make a changed picture a changed URL)."""
    return (f"/api/items/{quote(str(number))}/thumb"
            + (f"?version={quote(str(version))}&" if version else "?")
            + f"g={THUMB_GEN}")


class Presence:
    """Geometry presence for the span of one payload: the memo lives here.

    Build one per request (`p = thumbs.Presence(conn)`), then decorate every
    list of rows that goes out. Every site emits the SAME field name, `thumb`,
    with the same meaning — a URL, or None for 'nothing to render'.
    """

    def __init__(self, conn):
        self.conn = conn
        self.memo: dict[int, bool] = {}

    def url(self, number, version: str = "", rev_id: int = 0,
            kind: str = "") -> str | None:
        """The thumb field for one row: a URL when a body exists, else None.

        The URL names the revision that was actually resolved, so a BOM line
        pinned to a version that no longer exists (bound to the item's latest,
        as geometry_bodies does) gets a URL that resolves rather than one that
        404s forever."""
        if not number:
            return None
        if not rev_id:
            rev = (db.find_revision(self.conn, number, version)
                   or (db.find_revision(self.conn, number) if version else None))
            if not rev:
                return None
            rev_id, version = rev["id"], rev["version"]
        if not has_geometry(self.conn, rev_id, kind, memo=self.memo):
            return None
        return thumb_url(number, version)

    def rows(self, rows: list[dict], *, number: str = "number",
             version: str = "version", kind: str = "kind",
             rev: str = "revision_id", field: str = "thumb") -> list[dict]:
        """Stamp `thumb` onto every row, reading whichever keys these rows use
        (BOM lines say child_number/child_version/child_kind)."""
        for r in rows:
            r[field] = self.url(r.get(number) or "", r.get(version) or "",
                                int(r.get(rev) or 0), r.get(kind) or "")
        return rows

    def bom(self, lines: list[dict]) -> list[dict]:
        """BOM lines (db.get_bom): the child is the thing with the picture."""
        return self.rows(lines, number="child_number", version="child_version",
                         kind="child_kind", rev="child_rev_id")


def thumb_for(number: str, version: str = "") -> Path | None:
    """Cached PNG for a revision. Parts render their primary STL; assemblies
    render the ASSEMBLED composite — every unsuppressed BOM child's primary
    body imported into one scene (shared coordinates, same as the viewer)."""
    with db.connect() as conn:
        rev = db.find_revision(conn, number, version)
        if not rev:
            return None
        bodies = picture_bodies(geometry_bodies(conn, rev["id"]))
    srcs = []
    total = 0
    for b in bodies:
        # Any backend: vault rows resolve to their path on disk, offloaded rows
        # are materialized into the sha-keyed object cache next to the DB.
        try:
            p = storage.local_copy(b)
        except (storage.StorageError, vault.VaultError, OSError):
            continue
        if p.is_file():
            st = p.stat()
            total += st.st_size
            # Identity: local files can change under us (hand edits in the
            # vault), so key on size+mtime; remote objects are immutable at a
            # key we already have the sha for.
            tag = (b["sha256"] if (b.get("backend") or "vault") != "vault"
                   and b.get("sha256") else f"{st.st_size}:{int(st.st_mtime)}")
            # The COLOUR is part of the tag, so it is part of the cache key below.
            # Without it, every thumbnail rendered before per-component colouring
            # existed would be served forever — the source bytes did not change,
            # only how we draw them.
            srcs.append((f"{b['path']}:{tag}:{b.get('color') or ''}", p, st,
                         b.get('color') or ''))
    if not srcs or total > MAX_STL_BYTES:
        return None

    ident = "|".join(tag for tag, _, _, _ in srcs)
    key = hashlib.sha256(ident.encode()).hexdigest()[:24]
    out = CACHE / f"{key}.png"
    if out.is_file():
        return out
    CACHE.mkdir(parents=True, exist_ok=True)

    # color() per body, so each first-level component reads as its own part
    # of the assembly (render_png's preview keeps it).
    # ⛔ L8 — the path is ESCAPED for OpenSCAD's string literal, not merely
    # quoted by luck. `storage.local_copy` now allowlists the extension it
    # appends, so `p` is a cache path built from a sha256; this is the second
    # wall, and it is one line.
    scad = "".join(
        (f'color([{r},{g},{b}]) ' if col else '')
        + f'import("{_scad_str(p)}", convexity=5);\n'
        for _, p, _, col in srcs
        for r, g, b in [_rgb(col) if col else (0, 0, 0)])
    try:
        render_png(scad, out, unless_exists=True)   # a concurrent request may win
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    return out if out.is_file() else None


# ------------------------------------------------------------------- explode
# "WHY CAN'T I EXPLODE THIS ASSEMBLY?" kept coming back (GSI-ASM-0003 on
# 2026-09-24, then BCC-ASM-0001 the same afternoon, and LMT before them) because
# explodability was EMERGENT: geometry_bodies splits an assembly into separable
# bodies only when every first-level child that has a mesh has a `viz` mesh
# posed in the parent's coordinates, and silently falls back to the parent's one
# fused mesh otherwise. Nothing said so at check-in, the viewer just showed a
# slider that moved nothing, and every agent had to rediscover the rule.
#
# explode_status makes it a FACT computed by the same code the viewer uses (so
# it cannot disagree with what you see), with a per-child diagnosis naming the
# fix. explode_gate turns it into a RULE: an assembly of made things does not
# submit for release until it explodes, unless a human-readable waiver says why.

MADE_KINDS = ("part", "assembly")


def _frozen_child(conn, number: str):
    """The newest revision of `number` that is not a DRAFT / IN_REVIEW."""
    return conn.execute(
        "SELECT r.* FROM revisions r JOIN items i ON i.id=r.item_id WHERE "
        "i.number=? AND r.state NOT IN ('DRAFT','IN_REVIEW') ORDER BY "
        "r.vmaj DESC, r.vmin DESC, r.vpat DESC LIMIT 1", (number,)).fetchone()


def explode_status(conn, rev_id: int, *, frozen_children: bool = False) -> dict:
    """`frozen_children` (stage 15, for a BASE revision): a child the BOM
    line does not pin precisely resolves to its newest FROZEN revision, not
    to its newest revision — which may be the very draft the change under
    review added, and would make the base look as broken as the head."""
    bodies = component_bodies(geometry_bodies(conn, rev_id))
    place = placement(bodies)
    kids = []
    for line in db.get_bom(conn, rev_id):
        if line.get("suppressed"):
            continue
        precise = (db.find_revision(conn, line["child_number"],
                                    line["child_version"])
                   if line["child_version"] else None)
        cr = (precise
              or (_frozen_child(conn, line["child_number"]) if frozen_children
                  else None)
              or db.find_revision(conn, line["child_number"]))
        f = _pick_stl(conn, cr["id"]) if cr else None
        kind = line.get("child_kind") or ""
        if f and (f.get("role") or "") == "viz":
            st, fix = "posed", ""
        elif f:
            st, fix = "unposed", (f"has a {f.get('role') or 'mesh'} mesh but no `viz` "
                                  "posed in this assembly's coordinates — add one")
        elif kind in MADE_KINDS:
            st, fix = "no_mesh", "no mesh at all — add a `viz` posed in this assembly"
        else:
            st, fix = "not_drawn", ""    # bought/hardware with no mesh: fine, just absent
        kids.append({"number": line["child_number"], "name": line.get("child_name") or "",
                     "kind": kind, "qty": line.get("qty"), "status": st, "fix": fix})
    explodable = len(bodies) >= 2 and place["assembled"]
    made = [k for k in kids if k["kind"] in MADE_KINDS]
    blockers = [k for k in kids if k["status"] in ("unposed", "no_mesh")]
    if explodable:
        reason = f"{len(bodies)} separately placed bodies"
    elif not kids:
        reason = "no BOM — nothing to explode"
    elif blockers:
        reason = (f"{len(blockers)} of {len(kids)} children block it: "
                  + ", ".join(f"{k['number']} ({k['status'].replace('_', ' ')})"
                              for k in blockers[:6]))
    else:
        reason = f"only {len(bodies)} body resolves — fewer than two posed children"
    return {"explodable": explodable, "bodies": len(bodies),
            "required": len(made) >= 2, "reason": reason, "children": kids,
            "rule": "every first-level child that has a mesh must carry a `viz` "
                    "posed in the assembly's coordinates (plm/thumbs.py)"}


BLOCKING = ("unposed", "no_mesh")


def explode_base(conn, rev_id: int) -> dict | None:
    """The revision a draft is measured AGAINST: the newest frozen (not DRAFT,
    not IN_REVIEW) revision of the same item below this one's version — what
    the draft was revised from. None for a first revision."""
    rev = conn.execute("SELECT * FROM revisions WHERE id=?", (rev_id,)).fetchone()
    if not rev:
        return None
    row = conn.execute(
        "SELECT * FROM revisions WHERE item_id=? AND id<>? AND state NOT IN "
        "('DRAFT','IN_REVIEW') AND (vmaj, vmin, vpat) < (?, ?, ?) "
        "ORDER BY vmaj DESC, vmin DESC, vpat DESC LIMIT 1",
        (rev["item_id"], rev_id, rev["vmaj"], rev["vmin"], rev["vpat"])).fetchone()
    return dict(row) if row else None


def explode_regression(conn, rev_id: int) -> dict:
    """WRITE MODE'S EXPLODE RULE: did THIS
    change make explodability worse?

    The legacy gate fires on the head's state alone, so a weldment whose
    children were unposed long before anybody touched it refuses every change
    to it — and an agent learns to type a waiver by reflex, which is the end of
    the gate meaning anything. So the head is compared with its BASE
    (`explode_base`): it is a REGRESSION when the head does not explode and
      · there is no base to compare with (a first revision — the legacy rule),
      · or the base exploded,
      · or a child that BLOCKS now did not block in the base: a child this
        change ADDED without a posed viz, or one whose posed viz it lost.
    Anything else is PRE-EXISTING: the same children blocked before. That is a
    `warn` and a sentence, never a waiver."""
    head = explode_status(conn, rev_id)
    base_rev = explode_base(conn, rev_id)
    out = {"head": head, "base_version": base_rev["version"] if base_rev else "",
           "base_explodable": None, "worse": [], "regression": False,
           "pre_existing": False}
    if not head["required"] or head["explodable"]:
        return out
    if not base_rev:
        out["regression"] = True
        return out
    base = explode_status(conn, base_rev["id"], frozen_children=True)
    was = {k["number"]: k["status"] for k in base["children"]}
    worse = [k for k in head["children"] if k["status"] in BLOCKING
             and was.get(k["number"]) not in BLOCKING]
    out.update(base_explodable=base["explodable"], worse=[
        {**k, "was": was.get(k["number"]) or "absent"} for k in worse])
    out["regression"] = bool(base["explodable"] or worse)
    out["pre_existing"] = not out["regression"]
    return out


def explode_sentence(number: str, version: str, reg: dict) -> str:
    """The one line a submit response and the checks board say about it."""
    head = reg["head"]
    if reg["pre_existing"]:
        return (f"{number} v{version} does not explode, and neither did "
                f"v{reg['base_version']} ({head['reason']}) — pre-existing, not "
                "caused by this change, so no waiver is needed. The fix, when "
                "somebody takes it on: give each child a `viz` posed in this "
                "assembly's coordinates.")
    if reg["base_explodable"]:
        return (f"{number} v{version} does not explode, but v"
                f"{reg['base_version']} did — this change made it worse: "
                + head["reason"])
    if reg["worse"]:
        return (f"{number} v{version}: this change made explodability worse — "
                + ", ".join(f"{k['number']} is {k['status'].replace('_', ' ')} "
                            f"(was {k['was'].replace('_', ' ')})"
                            for k in reg["worse"][:6]))
    return f"{number} v{version} does not explode — {head['reason']}"


def explode_gate(conn, rev_id: int, waiver: str = "") -> str:
    """'' when this revision may be submitted, else the refusal message.
    Applies to assemblies of two or more MADE children; a written waiver
    (recorded on the approval) lets a deliberate exception through."""
    st = explode_status(conn, rev_id)
    if not st["required"] or st["explodable"] or waiver.strip():
        return ""
    return ("this assembly does not explode — " + st["reason"] + ". Fix: "
            + "; ".join(f"{k['number']}: {k['fix']}" for k in st["children"] if k["fix"])[:900]
            + ". Or pass explode_waiver='<why>' to submit anyway.")
