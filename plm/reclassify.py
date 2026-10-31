"""Backfill classification onto pre- files rows, without lying about it.

837 rows existed before `media_source`/`role_source` did. They need a format
recorded — that is what makes the geometry predicate work for content-addressed
blobs — but they must not be handed a fabricated pedigree on the way.

THE ONE RULE: THIS PASS ADDS KNOWLEDGE, IT NEVER REWRITES A CLASSIFICATION.

  media  is FILLED IN, by measuring the bytes where they are cheaply reachable.
         A measurement is safe to write because it is checkable: anybody can
         re-read the file and get the same answer.

  role   IS NOT TOUCHED. Not the value, not the provenance.

That second half is the important one, and it is a deliberate refusal. A legacy
row's `role_source` is '', which reads as "unknown provenance — nobody can prove
this was declared", and that is the exact truth about those rows: 63 of them say
`viz`, which the viz-upload path really did set on purpose, while 228 say
`artifact`, which is just where the old default dumped everything. Nothing in the
data distinguishes the two. So:

  - re-inferring the role would DESTROY the 63 real declarations to tidy up the
    228 guesses;
  - stamping the existing roles as 'declared' would be exactly the laundering
    this whole ticket exists to stop, performed in bulk;
  - stamping them 'extension' would assert they came from ROLE_BY_EXT, which for
    the viz rows is false.

'' is the honest value, `roles.is_guess('')` is True, so every surface already
shows these as unconfirmed and invites a human to say what they are. The
disagreements get REPORTED (see `disagreements`) rather than silently resolved,
because a controlled record is the wrong place to be clever.
"""
from __future__ import annotations

import sqlite3

from . import config, db, roles

HEAD_BYTES = 8192


def _head(row: sqlite3.Row) -> bytes:
    """First bytes of a file, if they are cheap to get.

    VAULT ROWS ONLY, on purpose. An offloaded row's bytes are in the object
    store, and fetching 800 objects to identify files we deliberately
    avoided downloading would be a poor trade for a classification we can get
    from the name — so those fall back to the extension and are recorded as the
    guess they are. (There is no extension in a content-addressed key, so in
    practice those rows keep whatever media the row already had, which for the
    offloaded corpus was set at upload time by a path that DID have the name.)
    """
    if (row["backend"] or "vault") not in ("", "vault"):
        return b""
    try:
        p = config.DATA_ROOT / row["path"]
        if not p.is_file():
            return b""
        with open(p, "rb") as fh:
            return fh.read(HEAD_BYTES)
    except OSError:
        return b""


def backfill(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Fill `media`/`media_source` on rows that never had them.

    Idempotent, and safe to re-run: `roles.better_source` gates every write, so
    a second pass cannot downgrade a sniffed measurement to an extension guess,
    and cannot touch anything a caller declared.
    """
    rows = conn.execute(
        "SELECT id, path, size, media, media_source, role, role_source, backend "
        "FROM files").fetchall()
    out = {"rows": len(rows), "media_filled": 0, "media_upgraded": 0,
           "unchanged": 0, "sniffed": 0, "from_extension": 0,
           "still_unknown": 0, "changes": []}
    for r in rows:
        name = str(r["path"]).rsplit("/", 1)[-1]
        head = _head(r)
        media, src = roles.sniff_media(head, name, int(r["size"] or 0))
        if src == roles.SNIFFED:
            out["sniffed"] += 1
        elif src == roles.EXTENSION:
            out["from_extension"] += 1
        cur_src = r["media_source"] or roles.UNKNOWN
        # Never downgrade, never overwrite something declared.
        if roles.better_source(cur_src, src) != src or media == "":
            if not (r["media"] or "") and not media:
                out["still_unknown"] += 1
            else:
                out["unchanged"] += 1
            continue
        if media == (r["media"] or "") and cur_src == src:
            out["unchanged"] += 1
            continue
        key = "media_upgraded" if (r["media"] or "") else "media_filled"
        out[key] += 1
        # Report only where the ANSWER changed, not merely its provenance — a
        # log of 800 "still a PNG, now we know it properly" lines hides the
        # handful that matter.
        if media != (r["media"] or ""):
            out["changes"].append(
                {"id": r["id"], "file": name, "was": r["media"] or "(none)",
                 "now": media, "how": src, "role": r["role"]})
        if not dry_run:
            conn.execute("UPDATE files SET media=?, media_source=? WHERE id=?",
                         (media, src, r["id"]))
    return out


def _decides(role: str) -> bool:
    """Does this role feed a DECISION? Only those are worth a human's attention.

    The queue has to be short enough to actually work through. Every one of the
    837 rows has unconfirmed provenance, so listing them all would be a list
    nobody reads. A row matters when being wrong about it changes an outcome:
    `approval` roles become release evidence, `primary` roles decide which body
    represents the revision in the viewer and the thumbnail.
    """
    spec = roles.ROLES.get(role) or {}
    return bool(spec.get("approval") or spec.get("primary"))


def disagreements(conn: sqlite3.Connection) -> list[dict]:
    """Rows where the declared purpose and the measured format do not agree —
    the review queue, not an auto-fix list.

    Three kinds, and they are different problems:

      mismatch      a role that expects one format class holding another. 'You
                    called it a drawing and the bytes are a PNG.'
      legacy        a row still parked in the `artifact` junk drawer. Not wrong,
                    just uninformative — and 122 of these are not geometry.
      unconfirmed   geometry whose role nobody has ever confirmed. These are the
                    ones a human can clear fastest and with the most benefit.
    """
    out: list[dict] = []
    for r in conn.execute(
            """SELECT f.id, f.path, f.role, f.role_source, f.media,
                      f.media_source, i.number, r.version
               FROM files f
               JOIN revisions r ON r.id=f.revision_id
               JOIN items i ON i.id=r.item_id
               ORDER BY i.number, r.version"""):
        name = str(r["path"]).rsplit("/", 1)[-1]
        base = {"id": r["id"], "number": r["number"], "version": r["version"],
                "file": name, "role": r["role"], "media": r["media"],
                "role_source": r["role_source"] or "",
                "media_source": r["media_source"] or ""}
        warn = roles.mismatch(r["role"], r["media"])
        if warn:
            out.append({**base, "issue": "mismatch", "detail": warn})
        elif r["role"] in roles.LEGACY_ROLES:
            out.append({**base, "issue": "legacy", "detail":
                        f"role {r['role']!r} is the historical default and says "
                        f"nothing; the bytes are {r['media'] or 'unknown'}. "
                        f"Suggest {roles.infer_role(name, r['media'])!r}."})
        elif roles.is_guess(r["role_source"] or "") and _decides(r["role"]):
            alts = roles.ambiguous(r["media"])
            if not alts:
                continue      # the format admits only one meaning: nothing to ask
            out.append({**base, "issue": "unconfirmed", "candidates": alts,
                        "detail":
                        f"nobody has confirmed this is a {r['role']!r}. These "
                        f"bytes ({r['media']}) could be any of "
                        + "/".join(alts) + ", and the choice changes whether it "
                        "counts as release evidence or which body represents "
                        "the revision."})
    return out


def report(conn: sqlite3.Connection) -> dict:
    """Counts for /api/stats and the ticket evidence."""
    iss = disagreements(conn)
    by = {}
    for d in iss:
        by[d["issue"]] = by.get(d["issue"], 0) + 1
    total = conn.execute("SELECT count(*) FROM files").fetchone()[0]
    return {
        "files": total,
        "no_media": conn.execute(
            "SELECT count(*) FROM files WHERE media=''").fetchone()[0],
        "media_sniffed": conn.execute(
            "SELECT count(*) FROM files WHERE media_source='sniffed'").fetchone()[0],
        "role_declared": conn.execute(
            "SELECT count(*) FROM files WHERE role_source='declared'").fetchone()[0],
        "role_unconfirmed": conn.execute(
            "SELECT count(*) FROM files WHERE role_source IN ('','extension')"
        ).fetchone()[0],
        "review": by,
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m plm.reclassify [--dry-run] [--report]` — run the backfill.

    Deliberately a CLI and not part of db.init(): 837 file reads on a synced
    volume is not something an app start should do silently, and an operator
    should see the change list before it lands.
    """
    import json
    import sys
    argv = list(argv if argv is not None else sys.argv[1:])
    db.init()
    with db.connect() as conn:
        if "--report" in argv:
            print(json.dumps(report(conn), indent=2))
            return 0
        dry = "--dry-run" in argv
        res = backfill(conn, dry_run=dry)
        if not dry:
            conn.commit()
        changes = res.pop("changes")
        print(json.dumps(res, indent=2))
        for c in changes[:40]:
            print(f"  {c['file'][:52]:52} {c['was']:22} -> {c['now']:16} "
                  f"({c['how']})")
        if len(changes) > 40:
            print(f"  … and {len(changes) - 40} more")
        print(json.dumps(report(conn), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
