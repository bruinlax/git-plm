"""Purchase REQUIREMENTS in a BOM — what must be bought, before anything is
chosen (stage 19, handoff finding 6).

CHG-379's dock retains the iPad with a strap, a buckle, a charging cable and
liners. The reference scenes showed them; the structured BOM listed only the
four printed parts, and the mesh and explode checks went green. Codex wrote
the purchases into DESIGN.md as prose, because the manifest had no way to say
"a 25 mm non-stretch strap is needed and nobody has picked one" without
inventing a supplier selection (a catalog item) that did not exist. A green
board then read as order-ready.

A requirement is a `[[bom]]` line with `requirement` and NO `path`:

    [[bom]]
    find = 50
    requirement = "Non-stretch webbing strap, 25 mm, >= 600 mm, sewn loop"
    qty = 1
    unit = "EA"
    catalog = ["HDW-0031", "HDW-0032"]   # optional: candidates, NOT a selection
    notes = "length set by the over-top anchors; verify against the closed iPad"

* no `path` -> no gitlink, no catalog pin, no child item: nothing is
  SELECTED. The push door accepts it (gitvalidate._check_bom); the explode,
  mesh and pin checks never see it (they read gitlinks / `bom` rows).
* `catalog` is a list of candidate catalog numbers for the reviewer — a
  placeholder, never a pin. Selecting one is replacing the line with an
  ordinary `path = "catalog/<N>.toml"` line (and the `catalog` submodule).
* indexed into `bom_requirements` (this module's own table, `ensure()` only
  where git is in use — an `off` instance's schema is master's); never into
  `bom`, so rolled BOMs and where-used are unchanged.
* the build plan and the BOM page say "requirement — not selected"; the
  `purchase-requirements` check WARNS ("3 purchase requirements unresolved");
  the release gate lists them and BLOCKS only when the manifest says
  `[release] required = ["purchase-requirements"]` (docs/PLM_GIT_FIRST.md §5.6).
"""
from __future__ import annotations

import json
import sqlite3

#: the reserved `[release] required` entry that makes the check required at
#: RELEASED (the rest of that list names release artifacts — §3)
REQUIRED_KEY = "purchase-requirements"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bom_requirements (
    revision_id  INTEGER NOT NULL,
    ord          INTEGER NOT NULL,     -- position among the manifest's lines
    find         TEXT NOT NULL DEFAULT '',
    requirement  TEXT NOT NULL,
    qty          REAL NOT NULL DEFAULT 1,
    unit         TEXT NOT NULL DEFAULT 'EA',
    notes        TEXT NOT NULL DEFAULT '',
    candidates   TEXT NOT NULL DEFAULT '[]',   -- JSON list of catalog numbers
    suppressed   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (revision_id, ord)
)
"""
_ready = False


def ensure(conn: sqlite3.Connection) -> None:
    global _ready
    if _ready:
        return
    conn.execute(_SCHEMA)                     # one statement: no executescript
    _ready = True


def is_requirement(line: dict) -> bool:
    return bool(str(line.get("requirement") or "").strip()) \
        and not str(line.get("path") or "").strip()


def candidates(line: dict) -> list[str]:
    c = line.get("catalog")
    if isinstance(c, str):
        c = [c]
    return [str(x) for x in (c or []) if str(x).strip()]


def lines(doc: dict) -> list[dict]:
    """The requirement lines of a parsed manifest, in file order."""
    return [b for b in (doc.get("bom") or []) if is_requirement(b)]


def validate(doc: dict) -> list[str]:
    """Refusal sentences for malformed requirement-ish lines (empty = fine)."""
    bad = []
    for b in doc.get("bom") or []:
        has_req = "requirement" in b
        if has_req and not str(b.get("requirement") or "").strip():
            bad.append("a `[[bom]]` line has an empty `requirement` — say what "
                       "must be bought")
        if "catalog" in b and not has_req:
            bad.append("`catalog = …` (candidate catalog items) belongs on a "
                       "REQUIREMENT line; a selected line is "
                       "`path = \"catalog/<N>.toml\"`")
        if has_req and not isinstance(b.get("catalog", []), (list, str)):
            bad.append("`catalog` on a requirement is a list of candidate "
                       "catalog numbers")
    return bad


def index(conn: sqlite3.Connection, rev_id: int, doc: dict) -> list[dict]:
    """Re-derive one revision's requirement rows. Idempotent."""
    ensure(conn)
    conn.execute("DELETE FROM bom_requirements WHERE revision_id=?", (rev_id,))
    out = []
    for i, b in enumerate(doc.get("bom") or []):
        if not is_requirement(b):
            continue
        row = {"find": str(b.get("find", "") if b.get("find") is not None else ""),
               "requirement": str(b["requirement"]).strip(),
               "qty": float(b.get("qty") or 1), "unit": b.get("unit") or "EA",
               "notes": b.get("notes") or "", "candidates": candidates(b),
               "suppressed": bool(b.get("suppressed"))}
        conn.execute(
            "INSERT INTO bom_requirements (revision_id, ord, find, requirement,"
            " qty, unit, notes, candidates, suppressed) VALUES (?,?,?,?,?,?,?,?,?)",
            (rev_id, i, row["find"], row["requirement"], row["qty"], row["unit"],
             row["notes"], json.dumps(row["candidates"]), int(row["suppressed"])))
        out.append(row)
    return out


def _has_table(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='bom_requirements'").fetchone())


def had(conn: sqlite3.Connection, rev_id: int) -> bool:
    return _has_table(conn) and bool(conn.execute(
        "SELECT 1 FROM bom_requirements WHERE revision_id=? LIMIT 1",
        (rev_id,)).fetchone())


def for_revision(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    """[{find, requirement, qty, unit, notes, candidates, suppressed, status,
    say}] — `status` is always "not selected": a selected purchase is an
    ordinary catalog line, not a requirement."""
    if not rev_id or not _has_table(conn):
        return []
    out = []
    for r in conn.execute("SELECT * FROM bom_requirements WHERE revision_id=? "
                          "ORDER BY ord", (rev_id,)):
        d = dict(r)
        d["candidates"] = json.loads(d["candidates"] or "[]")
        d["suppressed"] = bool(d["suppressed"])
        d["status"] = "not selected"
        d["say"] = (f"requirement — not selected: {d['qty']:g} {d['unit']} "
                    f"{d['requirement']}"
                    + (f" (candidates: {', '.join(d['candidates'])})"
                       if d["candidates"] else ""))
        out.append(d)
    return out


def manifest_lines(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    """The rows back as `[[bom]]` lines — `manifest.build()` emits them, so a
    split or an export carries them (and `--roundtrip` compares them)."""
    out = []
    for r in for_revision(conn, rev_id):
        e: dict = {}
        if r["find"] != "":
            try:
                e["find"] = int(r["find"])
            except ValueError:
                e["find"] = r["find"]
        e["requirement"] = r["requirement"]
        e["qty"] = float(r["qty"])
        e["unit"] = r["unit"]
        if r["suppressed"]:
            e["suppressed"] = True
        if r["candidates"]:
            e["catalog"] = r["candidates"]
        if r["notes"]:
            e["notes"] = r["notes"]
        out.append(e)
    return out


def release_required(doc: dict) -> bool:
    req = (doc.get("release") or {}).get("required") or []
    return REQUIRED_KEY in [str(x) for x in req]
