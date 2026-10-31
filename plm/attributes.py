"""Generic revision attributes: one table, one registry, no per-attribute code.

The facts people build into CAD — mass, material, bounding box, colour, print
time — belong to a REVISION, not an item: change the geometry and every one of
them changes. So `attributes` rows hang off `revision_id`, they carry forward on
revise exactly like files, and nothing here is item-level.

THE REGISTRY IS A GLOSS, NOT A GATE (the deliberate difference from
plm/relations.py, where the registry validates). Any name may be written: a
slicer, a CAD exporter or an agent that learns a new property must not need a
code change to record it. WELL_KNOWN improves DISPLAY — a label, a default unit,
a value kind — and gives the common names one spelling so `mass_g` does not
arrive as `massG`, `weight` and `Mass (g)` from three different tools.

== ONE HOME PER FACT ==

`material` and `mass_g` already live in COLUMNS on `revisions`, because the
build-plan rollup reads them in SQL. They stay there. This module does NOT
duplicate them into rows:

- reads MERGE the columns into the same dict as the rows, flagged `column`, so a
  caller sees one uniform attribute surface;
- writes to those two names are ROUTED to the columns (set_many), so there is
  never a row and a column disagreeing about the same fact.

Everything else is a row. If a row-backed attribute later earns a column (a
rollup needs it in SQL), add it to COLUMN_BACKED and the surface does not
change — that is the seam.

A third case exists and is named: ELSEWHERE. `process` (plm/fab.py) is
a revisions column that is MERGED INTO THIS READ but REFUSED on write, because
it is design intent rather than a measurement and therefore DRAFT-only. The read
surface stays uniform; the write goes through the verb that owns the guard.

== THE INTEGRATION SEAM ==

A slicer or CAD post-processor is the instrument that MEASURES these: after a
slice, POST /api/revisions/{id}/attributes (or MCP set_attributes) with
`source='orca 2.4.2, 0.20mm'`. plm never computes them itself — it records who
measured what, and when.
"""
from __future__ import annotations

import sqlite3

from . import db

# name -> {label, unit, kind}
#   label  how a human surface titles it
#   unit   the default unit, when the caller does not send one
#   kind   number | text | dimension — how to render/parse, not what to enforce
WELL_KNOWN: dict[str, dict] = {
    "mass_g": {"label": "Mass", "unit": "g", "kind": "number"},
    "material": {"label": "Material", "unit": "", "kind": "text"},
    "bbox_x_mm": {"label": "Bounding box X", "unit": "mm", "kind": "dimension"},
    "bbox_y_mm": {"label": "Bounding box Y", "unit": "mm", "kind": "dimension"},
    "bbox_z_mm": {"label": "Bounding box Z", "unit": "mm", "kind": "dimension"},
    "color": {"label": "Colour", "unit": "", "kind": "text"},
    "print_time_min": {"label": "Print time", "unit": "min", "kind": "number"},
    # -- physical envelopes and mounting interfaces -----------------
    # What an ASSET's revision has to be able to say: how big the thing you must
    # fit around is, and what its mounting interface actually looks like. These
    # are measured with a tape or off a LiDAR scan, not computed by a slicer, so
    # the CALLER is the instrument and `source` should say so ('Scaniverse LiDAR
    # scan 2026-08-10, PCA-aligned' beats 'measured').
    #
    # NO UNIT IN THE NAME, and no default unit either — deliberately unlike
    # bbox_x_mm. A slicer's output is always millimetres, so encoding mm in the
    # name is safe there; a camper in a Californian driveway is measured in
    # INCHES, and a default of 'mm' would silently relabel 81 in as 81 mm. Pass
    # the unit with the value ({"value": 81, "unit": "in"}) — the unit column
    # exists precisely so this is data.
    "overall_length": {"label": "Overall length", "unit": "", "kind": "dimension"},
    "overall_width": {"label": "Overall width", "unit": "", "kind": "dimension"},
    "overall_height": {"label": "Overall height", "unit": "", "kind": "dimension"},
    # A mounting interface is a spacing, a pitch, a hole and a limit. Naming
    # them gives the four numbers a fit calculation needs one spelling each,
    # instead of rail_spacing / railSpacing / "rail spacing (c-c)".
    "mount_spacing": {"label": "Mount spacing (c-c)", "unit": "",
                      "kind": "dimension"},
    "mount_hole_pitch": {"label": "Mount hole pitch", "unit": "",
                         "kind": "dimension"},
    "mount_hole_dia": {"label": "Mount hole diameter", "unit": "",
                       "kind": "dimension"},
    "mount_hole_count": {"label": "Mount holes per point", "unit": "",
                         "kind": "number"},
    "load_rating": {"label": "Load rating", "unit": "", "kind": "number"},
    # The one identity fact that belongs on a revision rather than in context:
    # it identifies the physical unit that was MEASURED, so a re-scan of a
    # different unit is visibly a different revision's subject. Owner, location
    # and "do we still have it" are item-level and change without the geometry
    # changing — those go in context, not here.
    "serial_number": {"label": "Serial number", "unit": "", "kind": "text"},
    # -- RAW STOCK (plm/fab.py) --------------------------------------
    # A length of 1x2x0.120 tube is a `purchased` item — it is bought, it has a
    # supplier and a purchase length, and it belongs in the BUY list. What makes
    # it STOCK is these attributes on its revision. They stay a GLOSS and not a
    # gate on purpose: the write path is permissive, and fab.cut_list refuses a
    # bad value BY NAME at the point where it matters (a nest cannot proceed on
    # stock_form='lineal'). Permissive store, strict computation.
    #
    # NO UNIT IN ANY NAME AND NO DEFAULT UNIT — the rule above, applied. His
    # steel is quoted in inches and feet; the plasma table talks millimetres.
    "stock_form": {"label": "Stock form", "unit": "", "kind": "text"},
    "stock_profile": {"label": "Stock profile (nesting key)", "unit": "",
                      "kind": "text"},
    "stock_section": {"label": "Stock section", "unit": "", "kind": "text"},
    "stock_length": {"label": "Purchasable length", "unit": "",
                     "kind": "dimension"},
    "kerf": {"label": "Kerf", "unit": "", "kind": "dimension"},
    "remnant_min": {"label": "Usable remnant threshold", "unit": "",
                    "kind": "dimension"},
    # Read-merged from the revisions column; written through fab.set_process.
    "process": {"label": "Process", "unit": "", "kind": "text"},
}

# Names that are MERGED INTO THIS READ SURFACE but whose write path lives
# elsewhere, with the verb that owns them. The read surface stays uniform — a
# caller sees `process` beside `material` and `mass_g` — while the write goes
# through the guard that fact needs.
#
# WHY `process` IS NOT COLUMN_BACKED. Everything in COLUMN_BACKED is a
# MEASUREMENT: you can legitimately weigh a RELEASED part and record the grams,
# so those writes have no lifecycle guard. `process` is DESIGN INTENT — changing
# "we print this" to "we cut this from tube" on a released revision changes what
# somebody approved — so it is DRAFT-only, like the five BOM verbs. Routing that
# through set_many() would either smuggle lifecycle logic into this module or
# quietly lose the guard. One fact, one home, one guard.
ELSEWHERE: dict[str, tuple[str, str]] = {
    "process": (
        "process",
        "set_process(number, 'make'|'print') / PATCH /api/items/{n}/process — "
        "it is manufacturing DEFINITION, not a measurement, so it is DRAFT-only "
        "(plm/fab.py)"),
}

# The two facts that live in revisions COLUMNS: name -> (column, kind).
# Reads merge them in; writes are routed to the column instead of a row.
COLUMN_BACKED: dict[str, tuple[str, str]] = {
    "material": ("material", "text"),
    "mass_g": ("mass_g", "number"),
}


class AttrError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def spec(name: str) -> dict:
    """The registry entry for a name. Unknown names degrade to a readable label
    rather than disappearing — the table is the record, the dict is the gloss."""
    if name in WELL_KNOWN:
        return {**WELL_KNOWN[name], "name": name, "well_known": True}
    return {"name": name, "well_known": False, "unit": "", "kind": "text",
            "label": name.replace("_", " ").strip().capitalize()}


def registry() -> list[dict]:
    """The well-known names, for callers that build a UI or a prompt from it."""
    return [{**spec(n), "column_backed": n in COLUMN_BACKED or n in ELSEWHERE,
             "written_by": ELSEWHERE[n][1] if n in ELSEWHERE else ""}
            for n in sorted(WELL_KNOWN)]


def get(conn: sqlite3.Connection, revision_id: int) -> dict:
    """The merged attribute surface of one revision: rows plus the
    column-backed facts, keyed by name. Every entry carries the registry gloss
    (label/unit/kind) so no caller re-implements the registry."""
    out: dict[str, dict] = {}
    cols = list(dict.fromkeys([c for c, _ in COLUMN_BACKED.values()]
                              + [c for c, _ in ELSEWHERE.values()]))
    rev = conn.execute(
        f"SELECT {', '.join(cols)} FROM revisions WHERE id=?",
        (revision_id,)).fetchone()
    if rev:
        for name, (col, _kind) in COLUMN_BACKED.items():
            val = rev[col]
            if val in (None, ""):
                continue
            sp = spec(name)
            out[name] = {**sp, "value": _fmt(val), "unit": sp["unit"],
                         "source": "", "updated_at": None, "column": True}
        for name, (col, verb) in ELSEWHERE.items():
            val = rev[col]
            if val in (None, ""):
                continue
            sp = spec(name)
            out[name] = {**sp, "value": _fmt(val), "unit": sp["unit"],
                         "source": "", "updated_at": None, "column": True,
                         "written_by": verb}
    for row in conn.execute(
            "SELECT * FROM attributes WHERE revision_id=? ORDER BY name",
            (revision_id,)):
        name = row["name"]
        if name in COLUMN_BACKED or name in ELSEWHERE:
            continue  # the column is the home; a legacy row never shadows it
        sp = spec(name)
        out[name] = {**sp, "value": row["value"], "unit": row["unit"] or sp["unit"],
                     "source": row["source"], "updated_at": row["updated_at"],
                     "column": False}
    return dict(sorted(out.items()))


def _fmt(val) -> str:
    """Values are TEXT on the wire — one type for every attribute — so a float
    column renders without a trailing .0 surprising anybody."""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _entry(value) -> tuple[str, str, str]:
    """Normalize one incoming attribute to (value, unit, source).
    Accepts a bare scalar or {value, unit?, source?}."""
    if isinstance(value, dict):
        return (_fmt(value.get("value", "")), str(value.get("unit", "") or ""),
                str(value.get("source", "") or ""))
    return (_fmt(value), "", "")


def set_many(conn: sqlite3.Connection, revision_id: int, values: dict, *,
             source: str = "", actor: str = "") -> dict:
    """Write attributes onto a revision. Values may be bare scalars or
    {value, unit?, source?}; an empty value DELETES the attribute (clearing a
    fact is a legitimate edit, and a blank row is not a fact).

    material/mass_g are routed to their columns — see the module docstring."""
    if not isinstance(values, dict) or not values:
        raise AttrError("give at least one attribute: {name: value}")
    written, cleared, routed = [], [], []
    for raw_name, raw in values.items():
        name = str(raw_name).strip()
        if not name:
            raise AttrError("attribute names cannot be empty")
        if len(name) > 64 or any(c.isspace() for c in name):
            raise AttrError(
                f"{name!r} is not a usable attribute name — use a short "
                "snake_case key (e.g. mass_g, bbox_x_mm); the human label "
                "comes from the registry")
        if name in ELSEWHERE:
            raise AttrError(
                f"{name} is not written here — use {ELSEWHERE[name][1]}. It is "
                f"merged into this read surface so there is one uniform "
                f"attribute view, but it has one home and one guard.")
        value, unit, src = _entry(raw)
        src = src or source
        if name in COLUMN_BACKED:
            col, kind = COLUMN_BACKED[name]
            if value == "":
                conn.execute(f"UPDATE revisions SET {col}=? WHERE id=?",
                             (None if kind == "number" else "", revision_id))
                cleared.append(name)
                continue
            if kind == "number":
                try:
                    stored: object = float(value)
                except ValueError as exc:
                    raise AttrError(
                        f"{name} is numeric ({spec(name)['unit']}) — "
                        f"{value!r} is not a number") from exc
            else:
                stored = value
            conn.execute(f"UPDATE revisions SET {col}=? WHERE id=?",
                         (stored, revision_id))
            routed.append(name)
            written.append(name)
            continue
        if value == "":
            conn.execute("DELETE FROM attributes WHERE revision_id=? AND name=?",
                         (revision_id, name))
            cleared.append(name)
            continue
        conn.execute(
            """INSERT INTO attributes (revision_id,name,value,unit,source,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(revision_id,name) DO UPDATE SET
                 value=excluded.value, unit=excluded.unit,
                 source=excluded.source, updated_at=excluded.updated_at""",
            (revision_id, name, value, unit, src, db.now()))
        written.append(name)
    rev = conn.execute("SELECT item_id FROM revisions WHERE id=?",
                       (revision_id,)).fetchone()
    db.add_event(conn, "attributes", item_id=rev["item_id"] if rev else None,
                 revision_id=revision_id, actor=actor,
                 data={"set": written, "cleared": cleared, "columns": routed,
                       "source": source})
    return {"written": written, "cleared": cleared, "column_backed": routed,
            "attributes": get(conn, revision_id)}


def copy(conn: sqlite3.Connection, from_rev: int, to_rev: int) -> int:
    """Carry attributes forward onto a new revision — the same dataset
    semantics files have (ops.revise, ops.fork). The column-backed facts are
    copied by the caller's own revision INSERT/UPDATE, not here."""
    rows = conn.execute("SELECT name,value,unit,source FROM attributes "
                        "WHERE revision_id=?", (from_rev,)).fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO attributes (revision_id,name,value,unit,source,updated_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(revision_id,name) DO NOTHING""",
            (to_rev, r["name"], r["value"], r["unit"], r["source"], db.now()))
    return len(rows)


def search_text(conn: sqlite3.Connection, revision_id: int) -> str:
    """Attribute names, labels, values and units as one searchable string, so
    'PETG' or 'bbox_z_mm' finds the revisions that carry it (plm/search.py)."""
    parts = []
    for name, a in get(conn, revision_id).items():
        parts.append(f"{name} {a['label']} {a['value']} {a['unit']}".strip())
    return "\n".join(parts)
