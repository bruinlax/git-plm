"""Import a lantern-style file vault into the plm index.

Reads the hand-maintained ledgers the lantern project established:

  parts/index.csv        part_number,name,latest_part_version,…,item_folder
  assemblies/index.csv   assembly_number,name,latest_version,status,item_folder
  plm/versions.csv       part_number,version,date,lifecycle_state,ecn,summary,…
  plm/bom.csv            parent+version, find, child+version, qty, unit, notes
  plm/sources.csv        purchased-part sourcing
  plm/releases/*.csv     version -> source .scad + selector + artifact binding

plus a filesystem scan of each item's versions/ folders to register files.
Idempotent: re-running updates in place.

Usage: python -m plm.importer projects/lantern-gobo-bearing
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from . import config, db, roles, search, vault

# ROLE_BY_EXT IS GONE. It lived here:
#
#   ROLE_BY_EXT = {".scad": "source", ".stl": "artifact", ".3mf": "artifact",
#                  ".step": "artifact", ".gcode": "artifact", ".bin": "artifact",
#                  ".md": "doc", ".csv": "doc", ".pdf": "doc",
#                  ".png": "drawing", ".jpg": "drawing", ".svg": "drawing"}
#
# and its output went straight into `files.role`, which is how a filename guess
# became a field that looks like a declaration. Note what it did: every STL,
# gcode, .bin and .step became `artifact` (one junk drawer, 228 rows on prod,
# 122 of them not geometry at all), and every image became `drawing` (120 PNG
# renders promoted to candidate approval evidence).
#
# The replacement is plm/roles.py: format MEASURED from the bytes, purpose
# guessed only when nobody stated it and stamped `role_source='extension'` so it
# stays visibly a guess. Kept as a comment because deleting it would lose the
# evidence of what the old data means.


def _read_csv(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def import_project(rel: str) -> dict:
    proj_dir = config.DATA_ROOT / rel
    if not proj_dir.is_dir():
        raise SystemExit(f"no such project folder: {proj_dir}")
    project = proj_dir.name
    stats = {"items": 0, "revisions": 0, "bom": 0, "files": 0, "sources": 0}

    db.init()
    with db.connect() as conn:
        # 1) item indexes (parts + assemblies vaults)
        for row in _read_csv(proj_dir / "parts" / "index.csv"):
            db.upsert_item(conn, row["part_number"], name=row.get("name", ""),
                           project=project, status=row.get("status", ""),
                           folder=f"{rel}/{row['item_folder']}" if row.get("item_folder") else None)
            stats["items"] += 1
        for row in _read_csv(proj_dir / "assemblies" / "index.csv"):
            db.upsert_item(conn, row["assembly_number"], name=row.get("name", ""),
                           project=project, status=row.get("status", ""),
                           folder=f"{rel}/{row['item_folder']}" if row.get("item_folder") else None)
            stats["items"] += 1

        # 2) revision ledger. Items only mentioned here (PUR/HDW/FW/DOC) get
        #    created on the fly with kind derived from their number class.
        for row in _read_csv(proj_dir / "plm" / "versions.csv"):
            item_id = db.upsert_item(conn, row["part_number"], project=project)
            db.upsert_revision(conn, item_id, row["version"],
                               state=row.get("lifecycle_state", ""),
                               date=row.get("date", ""),
                               ecn=row.get("ecn", ""),
                               summary=row.get("change_summary", ""),
                               verification=row.get("verification_required", ""))
            stats["revisions"] += 1

        # 3) release bindings: how each revision regenerates + its git tag
        for rel_csv in sorted((proj_dir / "plm" / "releases").glob("*.csv")):
            for row in _read_csv(rel_csv):
                num = row.get("part_number")
                ver = row.get("version")
                if not num or not ver:
                    continue
                item_id = db.upsert_item(conn, num, project=project)
                db.upsert_revision(conn, item_id, ver,
                                   source=row.get("source_file") or row.get("source") or "",
                                   selector=row.get("selector", ""),
                                   git_tag=row.get("git_tag") or row.get("release_tag") or "")

        # 4) BOM lines, bound to the parent (item, version) revision
        for row in _read_csv(proj_dir / "plm" / "bom.csv"):
            pid = db.upsert_item(conn, row["parent_part_number"], project=project)
            prev = db.upsert_revision(conn, pid, row["parent_version"])
            cid = db.upsert_item(conn, row["child_part_number"], project=project)
            # Dedupe key includes find_number: the same hardware legitimately
            # appears at two find numbers under one parent (two stations).
            find = int(row["find_number"] or 0) or None
            conn.execute(
                """INSERT INTO bom (parent_rev_id, find_number, child_item_id,
                                    child_version, qty, unit, alt_group, notes)
                   SELECT ?,?,?,?,?,?,?,?
                   WHERE NOT EXISTS (SELECT 1 FROM bom WHERE parent_rev_id=?
                                     AND child_item_id=? AND child_version=?
                                     AND find_number IS ?)""",
                (prev, find, cid,
                 row.get("child_version", ""), float(row.get("quantity") or 1),
                 row.get("unit", "EA"), row.get("alternate_group", ""),
                 row.get("notes", ""), prev, cid, row.get("child_version", ""), find))
            stats["bom"] += 1

        # 5) sourcing for purchased items
        for row in _read_csv(proj_dir / "plm" / "sources.csv"):
            item_id = db.upsert_item(conn, row["part_number"], project=project)
            conn.execute(
                """INSERT INTO sources (item_id,supplier,manufacturer,mpn,url,status,notes)
                   SELECT ?,?,?,?,?,?,?
                   WHERE NOT EXISTS (SELECT 1 FROM sources WHERE item_id=? AND mpn=? AND supplier=?)""",
                (item_id, row.get("supplier", ""), row.get("manufacturer", ""),
                 row.get("manufacturer_part_number", ""), row.get("url", ""),
                 row.get("source_status", ""), row.get("notes", ""),
                 item_id, row.get("manufacturer_part_number", ""), row.get("supplier", "")))
            stats["sources"] += 1

        # 6) register files found in each item's versions/ folders. No hashing
        #    here: reading 300+ MB through the synced volume takes minutes, and
        #    each version folder already carries a MANIFEST.sha256 written at
        #    check-in time. The index only needs to know the files exist.
        for item in conn.execute("SELECT id, folder FROM items WHERE folder IS NOT NULL").fetchall():
            vroot = config.DATA_ROOT / item["folder"] / "versions"
            if not vroot.is_dir():
                continue
            for vdir in sorted(p for p in vroot.iterdir() if p.is_dir()):
                rev_id = db.upsert_revision(conn, item["id"], vdir.name.lstrip("v"))
                for f in sorted(p for p in vdir.rglob("*") if p.is_file()):
                    rp = str(f.relative_to(config.DATA_ROOT))
                    if conn.execute("SELECT 1 FROM files WHERE revision_id=? AND path=?",
                                    (rev_id, rp)).fetchone():
                        continue
                    # classify by MEASURING the bytes, and record that
                    # the role is a guess. This line is the origin of the bug the
                    # ticket is about — it used to write ROLE_BY_EXT's filename
                    # guess into `role` with no provenance, so a guess became
                    # indistinguishable from a declaration and no consumer could
                    # trust the field. The importer cannot know PURPOSE (nobody
                    # is here to state it), so it still guesses; the difference
                    # is that it now says so.
                    #
                    # The header read is cheap and worth it: the file is already
                    # being stat()ed, and 8 KB tells us the real format even when
                    # the name lies or has no extension at all.
                    st = f.stat()
                    try:
                        with open(f, "rb") as fh:
                            head = fh.read(8192)
                    except OSError:
                        head = b""
                    c = roles.classify(f.name, "", head, st.st_size)
                    conn.execute(
                        "INSERT INTO files (revision_id,role,path,sha256,size,"
                        "media,role_source,media_source) VALUES (?,?,?,?,?,?,?,?)",
                        (rev_id, c["role"], rp, "", st.st_size, c["media"],
                         c["role_source"], c["media_source"]))
                    stats["files"] += 1

        # 7) context ingestion: the vault's ITEM.md is item-level context, each
        #    version folder's VERSION.md is revision-level context. Idempotent
        #    (skips when an identical body is already recorded).
        def add_ctx(item_id: int, rev_id: int | None, body: str) -> None:
            body = body.strip()
            if not body:
                return
            # target_type='' scopes the dedupe (and the write) to the CLASSIC
            # item/revision form — a comment that happens to share a body with an
            # ITEM.md but hangs off a relation is a different row (plm/context.py).
            dupe = conn.execute(
                "SELECT 1 FROM context WHERE item_id=? AND revision_id IS ? AND "
                "body=? AND target_type=''", (item_id, rev_id, body)).fetchone()
            if dupe:
                return
            conn.execute(
                "INSERT INTO context (item_id, revision_id, body, actor, created_at) "
                "VALUES (?,?,?,?,?)", (item_id, rev_id, body, "import", db.now()))
            stats["context"] += 1

        stats["context"] = 0
        for item in conn.execute("SELECT id, folder FROM items WHERE folder IS NOT NULL").fetchall():
            folder = config.DATA_ROOT / item["folder"]
            item_md = folder / "ITEM.md"
            if item_md.is_file():
                add_ctx(item["id"], None, item_md.read_text(errors="replace"))
            vroot = folder / "versions"
            if vroot.is_dir():
                for vdir in (p for p in vroot.iterdir() if p.is_dir()):
                    vmd = vdir / "VERSION.md"
                    if vmd.is_file():
                        rev_id = db.upsert_revision(conn, item["id"], vdir.name.lstrip("v"))
                        add_ctx(item["id"], rev_id, vmd.read_text(errors="replace"))

        for item in conn.execute("SELECT id FROM items").fetchall():
            db.refresh_latest(conn, item["id"])
        db.add_event(conn, "import", actor="importer", data={"project": rel, **stats})
        stats["search_rows"] = search.reindex(conn)

    return stats


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(import_project(sys.argv[1].strip("/")))


if __name__ == "__main__":
    main()
