"""Tags: the cross-cutting labels that were being written as prose.

WHY THIS EXISTS
---------------
These facts were already in plm before this module was. They were just in
sentences, where no query could reach them. Counted on prod:

  operations.notes        MEASURE 2, PROVISIONAL 2, UNPROVEN 1, UNVERIFIED 1
  revisions.summary       MEASURE 7, PROVISIONAL 4, OPEN 28
  revisions.verification  MEASURE 1, OPEN 2

Real examples, verbatim from the camper rack:

  "LOAD PATH UNPROVEN — the unguyed lateral case is not acceptable; hammock use
   requires both crossed guy straps."
  "PROVISIONAL jig datums — prove on a coupon before drilling the production
   batch."
  "MEASURE — jack-point bolt pattern unverified on the truck."

Every one of those is a gate-worthy engineering fact stated in shouty capitals
because the author knew it mattered and had nowhere structured to put it.
"Which parts carry an unverified datum?" was a grep over prose — which means it
was never asked, and a provisional datum could reach a production batch with
nothing in the system objecting.

A tag makes the fact QUERYABLE. The prose stays: the sentence explains, the tag
finds. Deleting the sentence in favour of the tag would trade a reason for a
label, which is a bad trade — so `note` on a tag holds the why, and the summary
keeps its paragraph.

THREE DIFFERENT JOBS, KEPT SEPARATE
-----------------------------------
This is the distinction the whole ticket turns on, and merging any two of these
is how a data model rots:

  kind   (part | assembly | purchased | asset | coupon …) — STRUCTURAL identity.
         Closed, gated, drives BOM legality. config.KIND_BY_CLASS.
  role   (mesh | drawing | source …) — what a FILE is for. Closed and gated:
         plm/roles.py REFUSES an unregistered role, because a purpose no surface
         acts on is a typo.
  tag    — an open cross-cutting label on an ITEM or a REVISION. Not gated.

REGISTRY IS A GLOSS, NOT A GATE — the plm/attributes.py rule, for the same
reason. Any tag may be written: the moment a new word needs a code change, the
person with the fact writes a sentence instead and we are back to grepping
prose. WELL_KNOWN gives the common ones one spelling, a label, and — the part
that will matter — a `caveat` flag saying "this is an unresolved risk", so a
release gate can later ask one question instead of parsing English.

Geoff's ask was to "encourage / enforce". The enforcement lives on `role`, where
the vocabulary is genuinely closed. Tags are the encouraged half: a known tag
renders with its meaning and shows up in the caveat rollup, an unknown one is
accepted and quietly listed as unregistered so the vocabulary converges by being
visible rather than by being refused.

SCOPE: ITEM OR REVISION
-----------------------
Some tags describe the thing forever ('playa-exposed' — it lives in alkali dust
whatever revision it is at). Some describe ONE revision's state of knowledge
('provisional-datum' — that is exactly the sort of thing a revision fixes). So a
tag carries an item_id always, and a revision_id when it is revision-scoped.
Item-level tags apply to every revision; revision-level ones do not leak
forward, because a caveat that silently survived the revision that resolved it
would be worse than no caveat at all.
"""
from __future__ import annotations

import re
import sqlite3
import time

from . import db

# tag -> {label, help, caveat, scope}
#   label   how a human surface titles it
#   help    what it MEANS — so two people tag the same fact the same way
#   caveat  an UNRESOLVED RISK: something is not yet known or proven. These are
#           the ones a release gate will ask about, and the ones the UI surfaces
#           without being asked.
#   scope   'item' | 'revision' | 'any' — advisory, to steer the author; never
#           enforced, because somebody will legitimately need the other one.
WELL_KNOWN: dict[str, dict] = {
    # -- caveats: the prose-shouted facts this module exists to capture -------
    "provisional-datum": {
        "label": "Provisional datum",
        "help": "A hole position, jig reference or measurement is a working "
                "assumption, not a verified fact. Prove it on a coupon before "
                "committing a batch. (Found in prose on the CCR receivers: "
                "'PROVISIONAL jig datums — prove on a coupon before drilling "
                "the production batch.')",
        "caveat": True, "scope": "revision",
    },
    "measure-required": {
        "label": "Measure required",
        "help": "A dimension here was taken from a scan, a drawing or a guess "
                "and has not been measured on the real thing. The MEASURE "
                "shout in a summary, made queryable.",
        "caveat": True, "scope": "revision",
    },
    "load-path-unproven": {
        "label": "Load path unproven",
        "help": "The structure carries load in a way nobody has verified by "
                "test or calculation. Distinct from measure-required: the "
                "numbers may be right and the load path still wrong.",
        "caveat": True, "scope": "revision",
    },
    "fit-unverified": {
        "label": "Fit unverified",
        "help": "Has not been trial-fitted to the asset it installs on. Pairs "
                "with the installed_on relation and the asset envelope "
                "attributes.",
        "caveat": True, "scope": "revision",
    },
    "as-built-undocumented": {
        "label": "As-built undocumented",
        "help": "The physical thing was modified during build and the record "
                "has not caught up. The most dangerous state a part can be in, "
                "because the drawing looks authoritative and is wrong.",
        "caveat": True, "scope": "revision",
    },
    "coating-open": {
        "label": "Coating not specified",
        "help": "A corrosion or finish process is still an open decision. "
                "('Playa corrosion — a released coating process is still open "
                "work.')",
        "caveat": True, "scope": "revision",
    },
    # -- durable properties of the thing itself -------------------------------
    "playa-exposed": {
        "label": "Playa exposed",
        "help": "Lives in alkali dust, UV and wind. Drives material, coating "
                "and fastener choices for the whole life of the part.",
        "caveat": False, "scope": "item",
    },
    "load-bearing": {
        "label": "Load bearing",
        "help": "Failure drops something or somebody. Raises the bar for "
                "verification evidence and for who may approve a change.",
        "caveat": False, "scope": "item",
    },
    "wear-item": {
        "label": "Wear item",
        "help": "Expected to be replaced periodically — plan spares, and keep "
                "it easy to remove.",
        "caveat": False, "scope": "item",
    },
    "fire-exposed": {
        "label": "Fire / heat exposed",
        "help": "Sees flame or sustained heat. Rules out PLA and most "
                "printable polymers outright.",
        "caveat": False, "scope": "item",
    },
    "field-serviceable": {
        "label": "Field serviceable",
        "help": "Must be removable and repairable with hand tools, away from "
                "the shop and possibly in the dark.",
        "caveat": False, "scope": "item",
    },
    "prints-unattended": {
        "label": "Prints unattended",
        "help": "Cleared to run without somebody watching — a real decision "
                "given the monitoring work, and one worth recording.",
        "caveat": False, "scope": "revision",
    },
}

CAVEATS = frozenset(k for k, v in WELL_KNOWN.items() if v.get("caveat"))

# One spelling per fact. Lowercase, hyphenated, no spaces — so 'Load Bearing',
# 'load_bearing' and 'loadBearing' converge instead of becoming three tags.
_CLEAN = re.compile(r"[^a-z0-9]+")


class TagError(ValueError):
    pass


def normalize(tag: str) -> str:
    """Canonical spelling. The ONLY normalization, so every write path agrees.

    Not a gate — anything nonempty survives — but it means the vocabulary
    converges without anybody policing it, which is the cheap 90% of
    'encourage'."""
    t = _CLEAN.sub("-", (tag or "").strip().lower()).strip("-")
    if not t:
        raise TagError("a tag needs at least one letter or digit")
    if len(t) > 48:
        raise TagError(f"tag too long ({len(t)} chars, max 48): {t[:60]!r}. A "
                       "tag is a label; the explanation goes in its note.")
    return t


def known(tag: str) -> bool:
    return tag in WELL_KNOWN


def is_caveat(tag: str) -> bool:
    """An unresolved risk. Unknown tags are NOT caveats — we cannot invent
    severity for a word nobody registered."""
    return tag in CAVEATS


def describe() -> list[dict]:
    """The registry as data, for the API/MCP."""
    return [{"tag": k, "label": v["label"], "help": v["help"],
             "caveat": bool(v.get("caveat")), "scope": v.get("scope") or "any"}
            for k, v in WELL_KNOWN.items()]


def _decorate(row: dict) -> dict:
    """One tag row, with its gloss merged in. `registered` is explicit so a
    surface can show an unregistered tag as exactly that — visible, accepted,
    and a candidate for the registry — rather than silently identical to a
    known one."""
    spec = WELL_KNOWN.get(row["tag"]) or {}
    row["label"] = spec.get("label") or row["tag"].replace("-", " ").capitalize()
    row["help"] = spec.get("help") or ""
    row["caveat"] = bool(spec.get("caveat"))
    row["registered"] = bool(spec)
    row["scope"] = "revision" if row.get("revision_id") else "item"
    return row


def set_tag(conn: sqlite3.Connection, item_id: int, tag: str,
            revision_id: int | None = None, note: str = "",
            actor: str = "") -> dict:
    """Add or update one tag. Idempotent on (item, revision, tag): re-tagging
    updates the note instead of making a duplicate."""
    t = normalize(tag)
    now = time.time()
    conn.execute(
        """INSERT INTO tags (item_id, revision_id, tag, note, actor, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(item_id, COALESCE(revision_id,0), tag)
           DO UPDATE SET note=excluded.note, actor=excluded.actor""",
        (item_id, revision_id, t, note, actor, now))
    row = conn.execute(
        "SELECT * FROM tags WHERE item_id=? AND COALESCE(revision_id,0)=? "
        "AND tag=?", (item_id, revision_id or 0, t)).fetchone()
    return _decorate(dict(row))


def clear_tag(conn: sqlite3.Connection, item_id: int, tag: str,
              revision_id: int | None = None) -> bool:
    """Remove a tag. Removing a caveat is how a caveat is RESOLVED, which is why
    it is a normal write and not a special verb — the event log records who
    cleared it and when, and that is the audit trail that matters."""
    cur = conn.execute(
        "DELETE FROM tags WHERE item_id=? AND COALESCE(revision_id,0)=? "
        "AND tag=?", (item_id, revision_id or 0, normalize(tag)))
    return cur.rowcount > 0


def for_item(conn: sqlite3.Connection, item_id: int,
             revision_id: int | None = None) -> list[dict]:
    """Every tag in force for this item — its item-level tags always, plus the
    given revision's own. Item tags first, then revision tags, caveats first
    within each, so the thing you most need to know is at the top."""
    rows = conn.execute(
        """SELECT * FROM tags WHERE item_id=?
             AND (revision_id IS NULL OR revision_id=?)
           ORDER BY revision_id IS NOT NULL, tag""",
        (item_id, revision_id or 0)).fetchall()
    out = [_decorate(dict(r)) for r in rows]
    out.sort(key=lambda r: (r["scope"] == "revision", not r["caveat"], r["tag"]))
    return out


def caveats_for(conn: sqlite3.Connection, item_id: int,
                revision_id: int | None = None) -> list[dict]:
    """Just the unresolved risks. The question a release decision asks."""
    return [t for t in for_item(conn, item_id, revision_id) if t["caveat"]]


def find(conn: sqlite3.Connection, tag: str = "", caveats_only: bool = False,
         project: str = "") -> list[dict]:
    """Every item/revision carrying a tag — the report that used to be a grep.

    `caveats_only` answers "what in this registry is not yet proven?" across the
    whole estate in one query, which is the report nobody could run before.
    """
    sql = ["""SELECT t.*, i.number, i.name, i.kind, i.project, r.version
              FROM tags t JOIN items i ON i.id=t.item_id
              LEFT JOIN revisions r ON r.id=t.revision_id"""]
    where, args = [], []
    if tag:
        where.append("t.tag=?")
        args.append(normalize(tag))
    if caveats_only:
        where.append("t.tag IN (%s)" % ",".join("?" * len(CAVEATS)))
        args.extend(sorted(CAVEATS))
    if project:
        where.append("i.project=?")
        args.append(project)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY i.project, i.number, t.tag")
    return [_decorate(dict(r)) for r in conn.execute(" ".join(sql), args)]


def copy(conn: sqlite3.Connection, src_rev_id: int, dst_rev_id: int) -> int:
    """Carry REVISION-level tags forward on revise()/fork(), the way files and
    attributes are carried (ops.revise -> attributes.copy, fab.copy).

    A caveat SHOULD survive a revision that did not address it: bumping the
    version does not measure the truck. Clearing it is a deliberate act, which
    is exactly the property that makes the tag worth having — silence has to be
    chosen, not obtained by incrementing a number.
    """
    src = conn.execute("SELECT item_id FROM revisions WHERE id=?",
                       (src_rev_id,)).fetchone()
    dst = conn.execute("SELECT item_id FROM revisions WHERE id=?",
                       (dst_rev_id,)).fetchone()
    if not src or not dst:
        return 0
    rows = conn.execute(
        "SELECT tag, note, actor FROM tags WHERE revision_id=?",
        (src_rev_id,)).fetchall()
    n = 0
    now = time.time()
    for r in rows:
        conn.execute(
            """INSERT INTO tags (item_id, revision_id, tag, note, actor,
                                 created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(item_id, COALESCE(revision_id,0), tag) DO NOTHING""",
            (dst["item_id"], dst_rev_id, r["tag"], r["note"], r["actor"], now))
        n += 1
    return n


def stats(conn: sqlite3.Connection) -> dict:
    """Counts for /api/stats: how much of the estate carries an open caveat, and
    which unregistered tags are accumulating (the signal that WELL_KNOWN needs a
    new entry)."""
    by_tag = dict(conn.execute(
        "SELECT tag, count(*) FROM tags GROUP BY tag ORDER BY 2 DESC"))
    return {
        "tags": sum(by_tag.values()),
        "by_tag": by_tag,
        "caveat_items": conn.execute(
            "SELECT count(DISTINCT item_id) FROM tags WHERE tag IN (%s)"
            % ",".join("?" * len(CAVEATS)), sorted(CAVEATS)).fetchone()[0],
        "unregistered": sorted(t for t in by_tag if t not in WELL_KNOWN),
    }
