"""`plm.toml` — the design facts of one revision, as a file that travels with a
clone (docs/PLM_GIT_FIRST.md §3, revised by §5.1).

The rule the schema is built on: *does it travel with a clone?* Identity, file
roles, attributes, relations, BOM lines, routing, tags and supplier sources are
the part, so they are a file in the repo. Approvals, events, context and users
are the conversation *about* the part, so they stay in the database.

Three things live here and nowhere else:

  build()    DB -> an ordered manifest document (plain dicts/lists)
  dumps()    that document -> deterministic TOML text
  facts()    a parsed manifest -> the canonical design-fact projection

`facts()` is the point of the module. It is computed identically from the DB
(`db_facts`) and from a parsed `plm.toml` (`toml_facts`), so `--roundtrip` is a
real equality test and not a re-serialisation of the same objects.

WRITING TOML BY HAND. Python 3.12 has `tomllib` (read) and no writer, and the
POC image adds no dependencies. The emitter below covers exactly the value types
this schema uses — string, int, float, bool, array of strings, inline table,
table, array of tables — and nothing else, because a general TOML writer is a
library and this is a projection of a schema we control.

ONE FACT DOES NOT TRAVEL, ON PURPOSE: `bom.child_version`. §5.1 says the
gitlink IS the pin, so a version on a BOM line would be a second home for it.
The manifest keeps only whether the line was *imprecise* (`track = "release"`,
i.e. "follow the child's releases"); the resolved version is written by
plm/split.py as the submodule commit. `--roundtrip` reports this explicitly
rather than quietly normalising it away.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tomllib
from pathlib import Path

from . import attributes, config, db

SCHEMA_VERSION = 1

# Kinds that get a repo of their own (§5.4). Purchased + hardware do not: they
# are one TOML each inside catalog.git, because repo-per-screw is the pattern
# the plan refuses.
CATALOG_KINDS = ("purchased", "hardware")

# state -> maturity. Prod has no `maturity` column (the plan proposes one), so
# in the POC maturity is a PROJECTION of state and nothing more — it is emitted
# because the schema declares it and because a tag annotation wants to carry it.
# DRAFT/IN_REVIEW have no maturity: they are not asking to be anything yet.
MATURITY_BY_STATE = {
    "DRAFT": "", "IN_REVIEW": "",
    "CONCEPT": "concept", "DESIGN": "design", "PROTOTYPE": "prototype",
    "RELEASED": "released", "SUPERSEDED": "superseded", "OBSOLETE": "obsolete",
}

# States that are work in progress: a branch, never a tag (BRIEF "Repo shape").
UNRELEASED_STATES = ("DRAFT", "IN_REVIEW")

# Files the vault keeps for its own bookkeeping and the repo does not want:
# MANIFEST.sha256 is git's job now, VERSION.md becomes the tag annotation.
DROP_FROM_TREE = ("MANIFEST.sha256", "VERSION.md")

# Extensions that make a file 3-D geometry for the purposes of picking a
# primary. Deliberately extension-based and not roles.GEOMETRY_MEDIA: the
# manifest names a file inside a git tree, and a clone has the name, not plm's
# sniffed `media` column.
MESH_EXT = (".stl", ".3mf", ".obj", ".step", ".stp", ".ply")


# -- the TOML emitter ---------------------------------------------------------

_BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _key(k: str) -> str:
    k = str(k)
    return k if k and set(k) <= _BARE else _str(k)


def _str(s: str) -> str:
    out = ['"']
    for ch in str(s):
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # repr() round-trips a float exactly and never emits an exponent for the
        # magnitudes a part carries; the ".0" keeps the TOML type stable.
        return repr(v) if v != int(v) else f"{int(v)}.0"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    if isinstance(v, dict):  # inline table
        return "{ " + ", ".join(f"{_key(k)} = {_scalar(x)}"
                                for k, x in v.items()) + " }"
    return _str(v)


def dumps(doc: list[tuple]) -> str:
    """Render an ordered document.

    `doc` is a list of (kind, name, payload):
      ("kv",    "",      {k: v})     top-level key/values, in order
      ("table", "files", {k: v})     [files]
      ("aot",   "bom",   [ {...} ])  a [[bom]] per row
      ("blank", "", None)            a separator line
    """
    lines: list[str] = []
    for kind, name, payload in doc:
        if kind == "blank":
            lines.append("")
        elif kind == "kv":
            for k, v in payload.items():
                lines.append(f"{_key(k)} = {_scalar(v)}")
        elif kind == "table":
            lines.append(f"[{name}]")
            for k, v in payload.items():
                lines.append(f"{_key(k)} = {_scalar(v)}")
        elif kind == "aot":
            for row in payload:
                lines.append(f"[[{name}]]")
                for k, v in row.items():
                    lines.append(f"{_key(k)} = {_scalar(v)}")
                lines.append("")
            if lines and lines[-1] == "":
                lines.pop()
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown document node: {kind}")
    text = "\n".join(lines).rstrip("\n") + "\n"
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


# -- vault path -> repo path --------------------------------------------------

def tree_path(vault_path: str) -> str:
    """Where a vault file lands in the per-item repo.

    `.../<folder>/versions/v0.4.1/stl/foo.stl` -> `stl/foo.stl`. The version
    folder is what git-first deletes (§4): the tree IS the revision, so the
    copy-detection hack (`-M -C --find-copies-harder`) that today's layout
    forces on every diff stops being necessary.

    Deliberately keyed on the LAST `/versions/vX/` segment rather than on this
    revision's own version, because 302 prod file rows point at the version
    folder they were carried forward FROM (see `--roundtrip --oddities`). Using
    the segment that is actually in the path puts a carried-forward file at the
    same tree path in both revisions, which is what makes the diff honest.
    """
    parts = Path(vault_path).parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == "versions" and parts[i + 1].startswith("v"):
            return "/".join(parts[i + 2:])
    return Path(vault_path).name


# Backends whose bytes are IN the git tree, so the manifest names them under
# `[files]` rather than `[[files.external]]`:
#
#   ''/'vault'  the vault working tree — split.py copies those bytes in,
#   'git'       already a blob in this repo, because the revision ARRIVED as a
#               commit (plm/gitindex.py). Its `path` is the tree path.
#
#   'lfs'       a Git LFS POINTER in the tree (stage 17); the bytes are in
#               plm's LFS store, and a clone with git-lfs gets them. In the
#               tree, so `[files]` names it by its tree path like `git`.
#
# Anything else is outside the tree, and that is what `[[files.external]]`
# exists to carry.
IN_TREE_BACKENDS = ("", "vault", "git", "lfs")


def is_external(row) -> bool:
    return (row["backend"] or "vault") not in IN_TREE_BACKENDS


# -- the document -------------------------------------------------------------

def _rev_row(conn: sqlite3.Connection, rev_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT r.*, i.number, i.name AS item_name, i.kind, i.project, "
        "i.folder, i.product FROM revisions r JOIN items i ON i.id=r.item_id "
        "WHERE r.id=?", (rev_id,)).fetchone()
    if not row:
        raise KeyError(f"no revision {rev_id}")
    return row


def _files(conn: sqlite3.Connection, rev_id: int) -> tuple[dict, list[dict]]:
    """(role -> sorted tree paths, external rows) for one revision."""
    by_role: dict[str, list[str]] = {}
    external: list[dict] = []
    for f in conn.execute(
            "SELECT * FROM files WHERE revision_id=? ORDER BY path, id",
            (rev_id,)):
        role = f["role"] or "artifact"
        if is_external(f):
            external.append({
                "name": Path(f["path"]).name,
                "role": role,
                "backend": f["backend"],
                "key": f["key"],
                "sha256": f["sha256"] or "",
                "size": int(f["size"] or 0),
            })
            continue
        # A git-backed row's `path` IS the tree path — it was read out of the
        # tree, not out of a `versions/vX/` folder, so there is nothing to strip
        # and stripping would flatten `stl/foo.stl` to `foo.stl`.
        p = (f["path"] if (f["backend"] or "") in ("git", "lfs")
             else tree_path(f["path"]))
        if Path(p).name in DROP_FROM_TREE:
            continue
        by_role.setdefault(role, []).append(p)
    for role in by_role:
        by_role[role] = sorted(dict.fromkeys(by_role[role]))
    external.sort(key=lambda e: (e["role"], e["name"], e["key"]))
    return dict(sorted(by_role.items())), external


def primary_file(by_role: dict, external: list[dict]) -> str:
    """The one geometry file the viewer, thumbnail and 3-D diff use.

    Convention first (§3): `viz` beats `mesh` beats `artifact`, which is the
    order plm/thumbs.py already picks in, and the name is only written down
    because a clone cannot re-derive plm's preference. An external mesh can be
    primary too — it is still the geometry of record, it just is not
    in the tree, and `[[files.external]]` says where it is.
    """
    for role in ("viz", "mesh", "artifact"):
        for p in by_role.get(role, []):
            if Path(p).suffix.lower() in MESH_EXT:
                return p
        for e in external:
            if e["role"] == role and Path(e["name"]).suffix.lower() in MESH_EXT:
                return e["name"]
    return ""


def _attributes(conn: sqlite3.Connection, rev_id: int) -> dict:
    """The merged attribute surface, minus `process` (which is a top-level key
    in the manifest — ONE HOME PER FACT, and `process` dispatches fabrication)."""
    out = {}
    for name, a in attributes.get(conn, rev_id).items():
        if name == "process":
            continue
        entry = {"value": a["value"]}
        if a.get("unit"):
            entry["unit"] = a["unit"]
        if a.get("source"):
            entry["source"] = a["source"]
        out[name] = entry
    return dict(sorted(out.items()))


def _relations(conn: sqlite3.Connection, row: sqlite3.Row, *,
               pin=None) -> list[dict]:
    """The claims THIS REVISION makes — `[[relations]]` (stage 10).

    Only the registry's `home = "revision"` types, and only the rows whose
    CLAIMANT is this revision (`relations.claimant` — the one answer, inference
    included). An item-home claim lives in `item.toml` on `refs/plm/item`
    (`plm/itemref.py`) and a note-home fact in `refs/notes/plm`
    (`plm/gitnotes.py`): one home each. The snapshot copy of item-level relations
    that used to ride in every `plm.toml` under `scope = "item"` is gone — that
    field was doing the job the file's identity should do.

    `commit` pins the target beside its `version`: the recorded one, else what
    `pin(number, version)` resolves (split passes its build-time resolver). The
    registry-only build leaves it out when nothing was recorded, which is why
    the round-trip compares relations without it — exactly as it compares BOM
    lines without the gitlink (`split --verify-pins` and the push lint are the
    checks that the commit and the version agree).
    """
    from . import relations as relmod
    out = []
    for r in conn.execute(
            "SELECT rel.*, t.number AS target_number FROM relations rel "
            "JOIN items t ON t.id=rel.target_item_id WHERE rel.item_id=? "
            "ORDER BY rel.relation, t.number, rel.target_version, rel.id",
            (row["item_id"],)):
        sp = relmod.spec(r["relation"])
        if sp["home"] != "revision":
            continue
        if relmod.claimant(conn, r)["revision_id"] != row["id"]:
            continue
        e = {"type": r["relation"], "item": r["target_number"]}
        if r["target_version"]:
            e["version"] = r["target_version"]
            commit = r["target_commit"] or (
                pin(r["target_number"], r["target_version"]) if pin else "")
            if commit:
                e["commit"] = commit
        if r["note"]:
            e["note"] = r["note"]
        if r["result"]:
            e["result"] = r["result"]
        if r["result_note"]:
            e["result_note"] = r["result_note"]
        m = relmod._measured(r["measured"])
        if m:
            e["measured"] = dict(sorted(m.items()))
        out.append(e)
    return out


def bom_path(child_number: str, child_kind: str) -> str:
    """Where a BOM line points. A design child is a submodule directory; a
    purchased/hardware child is a file inside the single `catalog` submodule."""
    if child_kind in CATALOG_KINDS:
        return f"catalog/{child_number}.toml"
    return f"parts/{child_number}"


def _bom(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    out = []
    for b in conn.execute(
            "SELECT b.*, c.number AS child_number, c.kind AS child_kind "
            "FROM bom b JOIN items c ON c.id=b.child_item_id "
            "WHERE b.parent_rev_id=? ORDER BY "
            "  CASE WHEN b.find_number IS NULL THEN 1 ELSE 0 END, "
            "  b.find_number, c.number, b.id", (rev_id,)):
        e: dict = {}
        # find_number is declared INTEGER, but SQLite is dynamically typed and
        # 75 prod rows hold PROSE there ('enclosure shell', 'AKASO WT50
        # projector') — somebody used the find column as a description. The
        # manifest carries what is actually in the registry rather than
        # discarding it or crashing: an integer stays an integer, anything else
        # travels as a string. Reported by `--roundtrip` under oddities.
        if b["find_number"] is not None:
            try:
                e["find"] = int(b["find_number"])
            except (TypeError, ValueError):
                e["find"] = str(b["find_number"])
        e["path"] = bom_path(b["child_number"], b["child_kind"])
        e["qty"] = float(b["qty"])
        e["unit"] = b["unit"] or "EA"
        if b["suppressed"]:
            e["suppressed"] = True
        if b["alt_group"]:
            e["alt_group"] = b["alt_group"]
        if b["notes"]:
            e["notes"] = b["notes"]
        # §5.1: git is always precise, so "imprecise" stops being a data state
        # and becomes automation. An empty child_version meant "whatever is
        # current"; that is now a tracking policy the bump bot reads.
        if not b["child_version"]:
            e["track"] = "release"
        out.append(e)
    return out


def _requirements(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    """Purchase REQUIREMENT lines (stage 19, plm/requirements.py) — only where
    the table exists, so an `off` instance's manifests are master's."""
    from . import requirements
    return requirements.manifest_lines(conn, rev_id)


def _routing(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    out = []
    for o in conn.execute(
            "SELECT * FROM operations WHERE revision_id=? ORDER BY seq, id",
            (rev_id,)):
        e = {"seq": int(o["seq"]), "op": o["op"]}
        params = json.loads(o["params"] or "{}")
        if params:
            e["params"] = {k: params[k] for k in sorted(params)}
        if o["work_center"]:
            e["work_center"] = o["work_center"]
        if o["notes"]:
            e["notes"] = o["notes"]
        out.append(e)
    return out


def _tags(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """Item-level tags plus this revision's own. A revision-scoped caveat never
    leaks forward silently (db.py), so the manifest carries exactly the two."""
    rows = conn.execute(
        "SELECT tag FROM tags WHERE item_id=? AND (revision_id IS NULL OR "
        "revision_id=?) ORDER BY tag", (row["item_id"], row["id"]))
    return sorted(dict.fromkeys(r["tag"] for r in rows))


def _suppliers(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    out = []
    for s in conn.execute(
            "SELECT * FROM sources WHERE item_id=? ORDER BY "
            "status DESC, supplier, mpn, id", (item_id,)):
        e = {}
        for k in ("supplier", "manufacturer", "mpn", "url", "status", "notes"):
            if s[k]:
                e[k] = s[k]
        if e:
            out.append(e)
    return out


def build(conn: sqlite3.Connection, rev_id: int, *, pin=None) -> dict:
    """Every manifest fact of one revision, as plain data.

    Returned as a dict (not TOML) so split.py, the API and the round-trip all
    read the same object; `dumps(document(m))` is the only place it becomes text.
    """
    row = _rev_row(conn, rev_id)
    by_role, external = _files(conn, rev_id)
    return {
        "schema": SCHEMA_VERSION,
        "number": row["number"],
        "name": row["item_name"],
        "kind": row["kind"],
        "project": row["project"] or "",
        "version": row["version"],
        "state": row["state"],
        "maturity": MATURITY_BY_STATE.get(row["state"], row["state"].lower()),
        "process": row["process"] or "",
        "date": row["date"] or "",
        "ecn": row["ecn"] or "",
        "author": row["author"] or "",
        "summary": row["summary"] or "",
        "verification": row["verification"] or "",
        "product": bool(row["product"]),
        "tags": _tags(conn, row),
        "source": {"file": row["source"] or "", "selector": row["selector"] or ""},
        "files": by_role,
        "files_external": external,
        "primary": primary_file(by_role, external),
        "attributes": _attributes(conn, rev_id),
        "relations": _relations(conn, row, pin=pin),
        "bom": _bom(conn, rev_id) + _requirements(conn, rev_id),
        "routing": _routing(conn, rev_id),
        "suppliers": _suppliers(conn, row["item_id"]),
        # not manifest facts; carried for split.py and the API
        "_rev_id": rev_id,
        "_item_id": row["item_id"],
        "_folder": row["folder"] or "",
        "_git_commit": row["git_commit"] or "",
    }


def document(m: dict, *, catalog: bool = False) -> list[tuple]:
    """The ordered TOML document for a manifest dict.

    `catalog=True` renders a catalog entry (`catalog/<NUMBER>.toml`): same
    schema, except that `[[source]]` carries the SUPPLIER rows (§6e: IPN != MPN,
    suppliers hang off the internal number with a priority) and the rare
    generating-source file moves to `[generator]`. The two vocabularies collide
    on the word "source" and TOML will not let one document hold both `[source]`
    and `[[source]]`, so the collision is resolved by file type, once, here.
    """
    head = {"schema": m["schema"], "number": m["number"], "name": m["name"],
            "kind": m["kind"]}
    for k in ("project", "version", "state", "maturity", "process", "date",
              "ecn", "author", "summary", "verification"):
        if m[k]:
            head[k] = m[k]
    if m["product"]:
        head["product"] = True
    if m["tags"]:
        head["tags"] = m["tags"]

    doc: list[tuple] = [("kv", "", head)]
    src = {k: v for k, v in m["source"].items() if v}

    if catalog:
        if src:
            doc += [("blank", "", None), ("table", "generator", src)]
        if m["suppliers"]:
            doc += [("blank", "", None), ("aot", "source", m["suppliers"])]
    elif src:
        doc += [("blank", "", None), ("table", "source", src)]

    files = {}
    if m["primary"]:
        files["primary"] = m["primary"]
    files.update(m["files"])
    if files:
        doc += [("blank", "", None), ("table", "files", files)]
    if m["files_external"]:
        doc += [("blank", "", None), ("aot", "files.external", m["files_external"])]
    if m["attributes"]:
        doc += [("blank", "", None), ("table", "attributes", m["attributes"])]
    if m["relations"]:
        doc += [("blank", "", None), ("aot", "relations", m["relations"])]
    if m["bom"]:
        doc += [("blank", "", None), ("aot", "bom", m["bom"])]
    if m["routing"]:
        doc += [("blank", "", None), ("aot", "routing", m["routing"])]
    if m.get("release"):
        doc += [("blank", "", None), ("table", "release", m["release"])]
    return doc


def render(conn: sqlite3.Connection, rev_id: int, *,
           catalog: bool = False) -> str:
    return dumps(document(build(conn, rev_id), catalog=catalog))


# -- the design-fact projection (what --roundtrip compares) -------------------
#
# Computed the same way from both sides. Everything is normalised to strings,
# sorted tuples and sorted lists so the comparison is a plain `==` and a
# mismatch prints as a readable diff rather than as two object reprs.

def _facts_common(m: dict) -> dict:
    return {
        "schema": m["schema"],
        "number": m["number"],
        "name": m["name"],
        "kind": m["kind"],
        "project": m["project"],
        "version": m["version"],
        "state": m["state"],
        "maturity": m["maturity"],
        "process": m["process"],
        "date": m["date"],
        "ecn": m["ecn"],
        "author": m["author"],
        "summary": m["summary"],
        "verification": m["verification"],
        "product": bool(m["product"]),
        "tags": list(m["tags"]),
        "source": dict(m["source"]),
        "files": {r: list(v) for r, v in m["files"].items()},
        "files_external": sorted(
            (e["name"], e["role"], e["backend"], e["key"], e["sha256"],
             int(e["size"])) for e in m["files_external"]),
        "primary": m["primary"],
        "attributes": {n: (a["value"], a.get("unit", ""), a.get("source", ""))
                       for n, a in m["attributes"].items()},
        # `commit` is deliberately not compared (see `_relations`); `scope`
        # is gone — a pre-stage-10 file's `scope` is read tolerantly by the
        # indexer and never written again.
        "relations": sorted(
            (r["type"], r["item"], r.get("version", ""), r.get("note", ""),
             r.get("result", ""), r.get("result_note", ""),
             json.dumps(r.get("measured") or {}, sort_keys=True))
            for r in m["relations"]),
        "bom": sorted(
            (str(r.get("find", "")), r.get("path", ""), float(r.get("qty") or 1),
             r.get("unit", "EA"),
             bool(r.get("suppressed", False)), r.get("alt_group", ""),
             r.get("notes", ""), r.get("track", ""),
             # stage 19: a purchase REQUIREMENT line (no path)
             r.get("requirement", ""),
             json.dumps(r.get("catalog") or [], sort_keys=True))
            for r in m["bom"]),
        "routing": sorted(
            (int(r["seq"]), r["op"],
             json.dumps(r.get("params", {}), sort_keys=True),
             r.get("work_center", ""), r.get("notes", ""))
            for r in m["routing"]),
        "suppliers": sorted(
            tuple(r.get(k, "") for k in
                  ("supplier", "manufacturer", "mpn", "url", "status", "notes"))
            for r in m["suppliers"]),
    }


def db_facts(conn: sqlite3.Connection, rev_id: int, *,
             catalog: bool = False) -> dict:
    m = build(conn, rev_id)
    if not catalog:
        m = {**m, "suppliers": []}  # suppliers only travel in catalog TOMLs
    return _facts_common(m)


def parse(text: str, *, catalog: bool = False) -> dict:
    """`plm.toml` text -> the same dict shape `build()` returns.

    The exact inverse of `build` + `document`, and the reason it exists twice
    over: the round-trip test needs it to compare facts, and the WRITE path
    (`plm/gitwrite.py`) needs it to *mutate* a manifest — parse, change one
    attribute, re-emit. `dumps(document(parse(text))) == text` for anything this
    module wrote, which is what makes a one-attribute edit a ONE-LINE diff in
    `plm.toml` instead of a reflow of the whole file.

    Key order inside `[[bom]]` / `[[relations]]` / `[[routing]]` rows is
    whatever the file had, because `document()` emits `row.items()` in order;
    that is deliberate, so re-emitting somebody else's hand-written manifest
    does not reshuffle their lines either.
    """
    t = tomllib.loads(text)
    files = dict(t.get("files") or {})
    external = files.pop("external", [])
    primary = files.pop("primary", "")
    src = t.get("generator" if catalog else "source") or {}
    attrs = {}
    for n, a in (t.get("attributes") or {}).items():
        # Only the keys that are actually there: document() writes `unit` and
        # `source` only when non-empty, so carrying an empty one back would add
        # a key on re-emission and turn a one-line edit into a file-wide diff.
        entry = {"value": a.get("value", "")}
        if a.get("unit"):
            entry["unit"] = a["unit"]
        if a.get("source"):
            entry["source"] = a["source"]
        attrs[n] = entry
    return {
        "schema": t.get("schema", 0),
        "number": t.get("number", ""), "name": t.get("name", ""),
        "kind": t.get("kind", ""), "project": t.get("project", ""),
        "version": t.get("version", ""), "state": t.get("state", ""),
        "maturity": t.get("maturity", ""), "process": t.get("process", ""),
        "date": t.get("date", ""), "ecn": t.get("ecn", ""),
        "author": t.get("author", ""), "summary": t.get("summary", ""),
        "verification": t.get("verification", ""),
        "product": bool(t.get("product", False)),
        "tags": list(t.get("tags") or []),
        "source": {"file": src.get("file", ""),
                   "selector": src.get("selector", "")},
        "files": {r: ([v] if isinstance(v, str) else list(v))
                  for r, v in files.items()},
        "files_external": [dict(e) for e in external],
        "primary": primary,
        "attributes": attrs,
        "relations": [dict(r) for r in (t.get("relations") or [])],
        "bom": [dict(r) for r in (t.get("bom") or [])],
        "routing": [dict(r) for r in (t.get("routing") or [])],
        "suppliers": [dict(r) for r in (t.get("source") or [])] if catalog else [],
        # stage 19: `[release] required` (§3) — kept so plm's own commits
        # (parse -> edit -> re-emit) never drop it
        "release": dict(t.get("release") or {}),
    }


def toml_facts(text: str, *, catalog: bool = False) -> dict:
    """Parse a rendered manifest back into the same projection."""
    # An empty [source] table is not written, so a parsed manifest has '' where
    # build() has ''. Same for the file-role lists: absent == empty.
    return _facts_common(parse(text, catalog=catalog))


def _diff(a: dict, b: dict, path: str = "") -> list[str]:
    """Readable field-level differences between two fact dicts."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
        return out
    if a != b:
        out.append(f"{path}: db={a!r} toml={b!r}")
    return out


# -- CLI ----------------------------------------------------------------------

def _oddities(conn: sqlite3.Connection) -> dict:
    """Registry facts worth knowing before anybody trusts a generated repo."""
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    odd_prefix = 0
    for f in conn.execute(
            "SELECT f.path, r.version, i.folder FROM files f "
            "JOIN revisions r ON r.id=f.revision_id JOIN items i ON i.id=r.item_id "
            "WHERE COALESCE(f.backend,'')=''"):
        want = f"{f['folder']}/versions/v{f['version']}/" if f["folder"] else ""
        if not want or not f["path"].startswith(want):
            odd_prefix += 1
    return {
        "revisions_with_no_files": q(
            "SELECT COUNT(*) FROM revisions r WHERE NOT EXISTS "
            "(SELECT 1 FROM files f WHERE f.revision_id=r.id)"),
        "items_with_no_vault_folder": q(
            "SELECT COUNT(*) FROM items WHERE folder IS NULL OR folder=''"),
        "file_rows_pointing_at_another_version_folder": odd_prefix,
        "bom_lines_with_PROSE_in_find_number": q(
            "SELECT COUNT(*) FROM bom WHERE find_number IS NOT NULL AND "
            "typeof(find_number)!='integer'"),
        "bom_lines_with_no_find_number": q(
            "SELECT COUNT(*) FROM bom WHERE find_number IS NULL"),
        "bom_lines_imprecise_child_version": q(
            "SELECT COUNT(*) FROM bom WHERE child_version=''"),
        # ⭐ ASK config.STATES, do not restate it. This query held its own copy
        # of the list, so when rule 2 declared CONCEPT and DESIGN in
        # plm/config.py the census went on reporting them as undeclared — the
        # same rule in two homes, disagreeing, which is the whole failure mode
        # the security review's Criticals turned out to be.
        "states_outside_config_STATES": [
            dict(r) for r in conn.execute(
                "SELECT state, COUNT(*) n FROM revisions WHERE state NOT IN "
                f"({','.join('?' * len(config.STATES))}) GROUP BY state",
                tuple(config.STATES))],
    }


def relation_landings(conn: sqlite3.Connection) -> dict:
    """Where every relations row lands in git, and that it lands EXACTLY ONCE
    (stage 10). Each home is rendered through its own emitter and parsed back —
    plm.toml (`build` → `document` → `parse`), item.toml (`itemref`), the note
    (`gitnotes`) — and the parsed lines are counted against the rows.

    Returns `{by_home, by_type, homeless, twice, missing, diff}`: `homeless`
    are rows with no claimant revision (relations.claimant `none`), which no
    tree can declare; `twice`/`missing` must be empty and `diff` lists any
    field that did not survive the item.toml / note round-trip."""
    from collections import Counter
    from . import gitnotes, itemref, relations as relmod
    landed: Counter = Counter()
    diff: list[str] = []
    rows = [dict(r) for r in conn.execute(
        "SELECT rel.*, f.number AS from_number, t.number AS to_number "
        "FROM relations rel JOIN items f ON f.id=rel.item_id "
        "JOIN items t ON t.id=rel.target_item_id ORDER BY rel.id")]
    # revision home: every revision's manifest, parsed back
    for r in conn.execute("SELECT r.id, r.version, i.number FROM revisions r "
                          "JOIN items i ON i.id=r.item_id"):
        m = build(conn, r["id"])
        if not m["relations"]:
            continue
        for e in parse(dumps(document(m)))["relations"]:
            landed[("revision", r["number"], r["version"], e["type"], e["item"],
                    str(e.get("version") or ""))] += 1
    # item home: item.toml, parsed back
    by_item: dict[int, list] = {}
    for row in rows:
        by_item.setdefault(row["item_id"], []).append(row)
    for item_id, rs in by_item.items():
        if not any(relmod.home(x["relation"]) == "item" for x in rs):
            continue
        d = itemref.build(conn, item_id)
        back = itemref.parse(itemref.document(d))
        want = sorted((x["type"], x["item"], x.get("version", ""),
                       x.get("note", "")) for x in d["relations"])
        got = sorted((x["type"], x["item"], str(x.get("version") or ""),
                      str(x.get("note") or "")) for x in back["relations"])
        if want != got:
            diff.append(f"{d['number']} item.toml: {want} != {got}")
        for x in back["relations"]:
            landed[("item", d["number"], "", x["type"], x["item"],
                    str(x.get("version") or ""))] += 1
    # note home: one note per claimant revision, parsed back
    notes: dict[tuple, dict] = {}
    for row in rows:
        sp = relmod.spec(row["relation"])
        if sp["home"] != "note":
            continue
        c = relmod.claimant(conn, row)
        if not c["revision_id"]:
            continue
        n = notes.setdefault((row["from_number"], c["version"]),
                             gitnotes.empty())
        e = {"item": row["to_number"]}
        if row["target_version"]:
            e["version"] = row["target_version"]
        if row["note"]:
            e["note"] = row["note"]
        n[sp["note_key"]].append(e)
    for (num, ver), n in notes.items():
        back = gitnotes.parse(gitnotes.document(n))
        for key in gitnotes.note_keys():
            if sorted(map(str, back.get(key) or [])) != sorted(
                    map(str, n.get(key) or [])):
                diff.append(f"{num} v{ver} note [[{key}]] did not round-trip")
            for x in back.get(key) or []:
                landed[("note", num, ver, gitnotes.type_for_key(key),
                        x["item"], str(x.get("version") or ""))] += 1
    by_home: Counter = Counter()
    by_type: dict = {}
    homeless, twice, missing = [], [], []
    for row in rows:
        h = relmod.home(row["relation"])
        c = relmod.claimant(conn, row)
        t = by_type.setdefault(row["relation"], Counter())
        if h != "item" and not c["revision_id"]:
            homeless.append(f"{row['from_number']} {row['relation']} "
                            f"{row['to_number']} v{row['target_version']} — "
                            "the claimant has no revision")
            t["homeless"] += 1
            by_home["homeless"] += 1
            continue
        key = (h, row["from_number"], c["version"] if h != "item" else "",
               row["relation"], row["to_number"], row["target_version"] or "")
        n = landed.get(key, 0)
        if n == 0:
            missing.append(str(key))
        elif n > 1:
            twice.append(f"{key} x{n}")
        t[h] += 1
        if h != "item":
            t[c["how"]] += 1        # declared | inferred
        by_home[h] += 1
    # a parsed line no row accounts for is the other half of "exactly once"
    want_keys = Counter()
    for row in rows:
        h = relmod.home(row["relation"])
        c = relmod.claimant(conn, row)
        if h != "item" and not c["revision_id"]:
            continue
        want_keys[(h, row["from_number"], c["version"] if h != "item" else "",
                   row["relation"], row["to_number"],
                   row["target_version"] or "")] += 1
    for k, v in landed.items():
        if v > want_keys.get(k, 0):
            twice.append(f"{k} landed {v}x for {want_keys.get(k, 0)} row(s)")
    return {"rows": len(rows), "by_home": dict(by_home),
            "by_type": {k: dict(v) for k, v in sorted(by_type.items())},
            "homeless": homeless, "twice": twice, "missing": missing,
            "diff": diff}


def roundtrip(conn: sqlite3.Connection, *, show: int = 12) -> int:
    """Export every revision, parse it back, diff the facts. Returns exit code."""
    revs = [dict(r) for r in conn.execute(
        "SELECT r.id, i.number, r.version, i.kind FROM revisions r "
        "JOIN items i ON i.id=r.item_id ORDER BY i.number, r.vmaj, r.vmin, "
        "r.vpat")]
    counts = {"revisions": 0, "catalog_revisions": 0, "bytes": 0,
              "attributes": 0, "bom_lines": 0, "relations": 0, "routing_ops": 0,
              "tags": 0, "file_roles": 0, "tree_files": 0, "external_files": 0,
              "suppliers": 0}
    fails: list[str] = []
    dropped_child_version = 0
    for r in revs:
        catalog = r["kind"] in CATALOG_KINDS
        m = build(conn, r["id"])
        text = dumps(document(m, catalog=catalog))
        counts["revisions"] += 1
        counts["catalog_revisions"] += 1 if catalog else 0
        counts["bytes"] += len(text)
        counts["attributes"] += len(m["attributes"])
        counts["bom_lines"] += len(m["bom"])
        counts["relations"] += len(m["relations"])
        counts["routing_ops"] += len(m["routing"])
        counts["tags"] += len(m["tags"])
        counts["file_roles"] += len(m["files"])
        counts["tree_files"] += sum(len(v) for v in m["files"].values())
        counts["external_files"] += len(m["files_external"])
        counts["suppliers"] += len(m["suppliers"]) if catalog else 0
        want = db_facts(conn, r["id"], catalog=catalog)
        try:
            got = toml_facts(text, catalog=catalog)
        except Exception as exc:  # noqa: BLE001
            fails.append(f"{r['number']} v{r['version']}: PARSE FAILED: {exc}")
            continue
        for line in _diff(want, got):
            fails.append(f"{r['number']} v{r['version']}: {line}")
    dropped_child_version = conn.execute(
        "SELECT COUNT(*) FROM bom WHERE child_version!=''").fetchone()[0]

    print("plm.toml round-trip — export every revision, parse it back, diff "
          "the design facts")
    print("-" * 72)
    for k, v in counts.items():
        print(f"  {k:<28} {v:>8,}")
    print("-" * 72)
    if fails:
        print(f"DIFF NOT EMPTY — {len(fails)} field mismatches:")
        for line in fails[:show]:
            print("   ", line)
        if len(fails) > show:
            print(f"    … and {len(fails) - show} more")
    else:
        print("DIFF EMPTY — every exported fact parsed back identical.")
    lands = relation_landings(conn)
    print()
    print("RELATIONS BY HOME (stage 10) — each row rendered into its home and "
          "parsed back:")
    print(f"  {lands['rows']} rows: "
          + ", ".join(f"{h} {n}" for h, n in sorted(lands["by_home"].items())))
    for t, c in lands["by_type"].items():
        print(f"    {t:<26} " + ", ".join(f"{k} {v}" for k, v in
                                          sorted(c.items())))
    for line in lands["homeless"][:show]:
        print(f"    homeless: {line}")
    bad = lands["twice"] + lands["missing"] + lands["diff"]
    if bad:
        print(f"  NOT EXACTLY ONCE — {len(bad)} problem(s):")
        for line in bad[:show]:
            print("    " + line)
        fails += bad
    else:
        print("  EXACTLY ONCE — every row with a home lands in it once, and "
              "nowhere else.")
    print()
    print("NOT CARRIED BY plm.toml, BY DESIGN (docs/PLM_GIT_FIRST.md §5.1):")
    print(f"  bom.child_version on {dropped_child_version:,} precise BOM lines — "
          "the submodule")
    print("  gitlink IS the pin, so a version on the line would be a second "
          "home for it.")
    print("  The manifest keeps only whether the line was imprecise "
          "(track = \"release\").")
    print("  plm/split.py writes the resolved version as the child's commit; "
          "run it with")
    print("  --verify-pins to confirm every gitlink matches bom.child_version.")
    print("  [[relations]] commit — the same rule for a claim's target: the "
          "version is the\n  registry fact, the commit is the pin resolved "
          "from it (split writes it; a push\n  that pins a commit the target "
          "repo lacks is refused, a disagreement is a lint).")
    print("  LIFECYCLE FIELDS — state, maturity, summary, author, date — "
          "are emitted (plm wrote\n  this export) and round-trip above, but "
          "they are PLM'S, not the tree's: on a draft\n  branch the indexer "
          "ignores a pushed plm.toml's copy (the push is a DRAFT, the "
          "commit\n  subject is the summary, the pusher is the author) and "
          "the release commit writes\n  them back. The invariant is: design "
          "facts round-trip; lifecycle fields are plm's.")
    print()
    print("REGISTRY ODDITIES (data, not bugs in this code):")
    for k, v in _oddities(conn).items():
        print(f"  {k:<46} {v}")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.manifest",
        description="Generate plm.toml for a revision, or prove the round-trip.")
    ap.add_argument("--roundtrip", action="store_true",
                    help="export every revision, parse it back, diff vs the DB")
    ap.add_argument("--item", help="print the manifest for an item number")
    ap.add_argument("--version", dest="ver", help="which revision (default: newest)")
    ap.add_argument("--show", type=int, default=12, help="mismatches to print")
    args = ap.parse_args(argv)

    with db.connect() as conn:
        if args.item:
            it = db.resolve_item(conn, args.item)
            if not it:
                print(f"unknown item: {args.item}", file=sys.stderr)
                return 2
            if args.ver:
                row = conn.execute(
                    "SELECT id FROM revisions WHERE item_id=? AND version=?",
                    (it["id"], args.ver.lstrip("v"))).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM revisions WHERE item_id=? ORDER BY vmaj "
                    "DESC, vmin DESC, vpat DESC LIMIT 1", (it["id"],)).fetchone()
            if not row:
                print("no such revision", file=sys.stderr)
                return 2
            print(render(conn, row["id"],
                         catalog=it["kind"] in CATALOG_KINDS), end="")
            return 0
        if args.roundtrip:
            return roundtrip(conn, show=args.show)
    ap.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
