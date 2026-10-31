"""Context: the "why" as a first-class object — attachable to ANYTHING.

Context started as markdown on an item or a revision. Geoff's reading of it is
the right one: **"it's basically comments"** — and comments are not a property
of parts, they are a property of *things people discuss*. A link someone
questions, an approval someone explains, later a change order: each of those is
a thing, and each deserves the same append-only, searchable log.

So this module does to `context` what `plm/relations.py` did to `relations`:
one table, one REGISTRY, no per-target code.

    TARGETS['relation'] = {label, classic, resolve, hint}

`resolve` answers two questions at once — *does this target exist* and *which
item and revision does it hang off* — and that second answer is the whole trick
(see below). Extending the surface later is one dict entry: no new table, no new
endpoint, no new UI card.

## Two storage forms, one meaning

- **Classic** (`target_type = ''`): the pre-existing shape. `item_id` is the
  item, `revision_id` is NULL for item-level or a revision id for
  revision-level. EVERY row written before this module is in this form, and
  every row written for target types `item`/`revision` still is: there is one
  representation of "a comment on this item", not two.
- **Generic** (`target_type = 'relation' | 'approval' | …`): `target_id` names
  the row being discussed. `item_id`/`revision_id` are still populated, as the
  ANCHOR of the target — the item the relation was made from, the item whose
  revision is under review.

Keeping the anchor is why `context.item_id` could stay `NOT NULL` and why this
migration is two `ALTER TABLE ADD COLUMN`s instead of a table rebuild
(docs/PLM_DATA_MODEL.md). It also pays for itself twice: the event log and the
FTS index keep working with no special cases, so a comment on a relation is
findable by searching the item it concerns.

Every target in the registry today belongs to an item. The day one does not
(a comment on a project, on a user), `item_id` has to become nullable — and
THAT is the day for the new-table-copy migration, not before.

Classic reads filter `target_type = ''`, so generic comments never leak into the
item-page context card; they show up where they were attached.
"""
from __future__ import annotations

import sqlite3

from . import db


class ContextError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# -- resolvers: does the target exist, and what does it hang off? --------------
# Each returns {target_id, item_id, revision_id, ref} or None. `ref` is the
# human name of the target, for the response and for error messages.

def _r_item(conn: sqlite3.Connection, target: str | int) -> dict | None:
    row = db.resolve_item(conn, target)
    if not row:
        return None
    return {"target_id": row["id"], "item_id": row["id"], "revision_id": None,
            "ref": row["number"]}


def _r_revision(conn: sqlite3.Connection, target: str | int) -> dict | None:
    if not str(target).isdigit():
        return None
    row = conn.execute(
        """SELECT r.id, r.version, r.item_id, i.number FROM revisions r
           JOIN items i ON i.id=r.item_id WHERE r.id=?""", (int(target),)).fetchone()
    if not row:
        return None
    return {"target_id": row["id"], "item_id": row["item_id"],
            "revision_id": row["id"], "ref": f"{row['number']} v{row['version']}"}


def _r_relation(conn: sqlite3.Connection, target: str | int) -> dict | None:
    if not str(target).isdigit():
        return None
    from . import relations as rel
    row = conn.execute(
        """SELECT r.id, r.item_id, r.relation, f.number AS from_number,
                  t.number AS to_number, r.target_version
           FROM relations r JOIN items f ON f.id=r.item_id
                            JOIN items t ON t.id=r.target_item_id
           WHERE r.id=?""", (int(target),)).fetchone()
    if not row:
        return None
    label = rel.spec(row["relation"])["label"]
    ver = f" v{row['target_version']}" if row["target_version"] else ""
    return {"target_id": row["id"], "item_id": row["item_id"], "revision_id": None,
            "ref": f"{row['from_number']} {label} {row['to_number']}{ver}"}


def _r_approval(conn: sqlite3.Connection, target: str | int) -> dict | None:
    if not str(target).isdigit():
        return None
    row = conn.execute(
        """SELECT a.id, a.target_state, r.id AS revision_id, r.version,
                  r.item_id, i.number FROM approvals a
           JOIN revisions r ON r.id=a.revision_id
           JOIN items i ON i.id=r.item_id WHERE a.id=?""", (int(target),)).fetchone()
    if not row:
        return None
    return {"target_id": row["id"], "item_id": row["item_id"],
            "revision_id": row["revision_id"],
            "ref": f"{row['number']} v{row['version']} → {row['target_state']}"}


def _r_proposal(conn: sqlite3.Connection, target: str | int) -> dict | None:
    t = str(target).lstrip("#p")
    if not t.isdigit():
        return None
    from . import proposals
    proposals.ensure(conn)
    row = conn.execute(
        "SELECT p.id, p.item_id, p.revision_id, i.number FROM proposals p "
        "JOIN items i ON i.id=p.item_id WHERE p.id=?", (int(t),)).fetchone()
    if not row:
        return None
    return {"target_id": row["id"], "item_id": row["item_id"],
            "revision_id": row["revision_id"], "ref": f"#{row['id']} {row['number']}"}


def _r_release(conn: sqlite3.Connection, target: str | int) -> dict | None:
    if not str(target).isdigit():
        return None
    row = conn.execute(
        "SELECT r.id, r.item_id, r.revision_id, r.version, r.to_maturity, i.number "
        "FROM releases r JOIN items i ON i.id=r.item_id WHERE r.id=?",
        (int(target),)).fetchone()
    if not row:
        return None
    return {"target_id": row["id"], "item_id": row["item_id"],
            "revision_id": row["revision_id"],
            "ref": f"release {row['id']} {row['number']} v{row['version']} → "
                   f"{row['to_maturity']}"}


def _r_thread(conn: sqlite3.Connection, target: str | int) -> dict | None:
    if not str(target).isdigit():
        return None
    from . import proposals
    proposals.ensure(conn)
    row = conn.execute(
        "SELECT t.id, t.item_id, t.revision_id, t.subject_type, t.subject_id, "
        "t.title FROM threads t WHERE t.id=?", (int(target),)).fetchone()
    if not row:
        return None
    return {"target_id": row["id"], "item_id": row["item_id"],
            "revision_id": row["revision_id"],
            "ref": f"thread {row['id']} on {row['subject_type']} "
                   f"#{row['subject_id']}"}


def _r_reserved(conn: sqlite3.Connection, target: str | int) -> dict | None:
    return None


# type -> {label, classic, resolve, hint}
#
#   label     what the target is called to a human
#   classic   stored in the pre-existing item_id/revision_id form (target_type
#             ''), because that IS the one home for a comment on a part
#   resolve   existence check + the (item, revision) anchor
TARGETS: dict[str, dict] = {
    "item": {
        "label": "item",
        "classic": True,
        "resolve": _r_item,
        "hint": "the part/assembly itself — standing reasoning that outlives "
                "any one revision. `target` is a number or an item id.",
    },
    "revision": {
        "label": "revision",
        "classic": True,
        "resolve": _r_revision,
        "hint": "one revision: what was tried, measured, decided at that "
                "version. `target` is a revision id (or use target_type=item "
                "with a version).",
    },
    "relation": {
        "label": "relation",
        "classic": False,
        "resolve": _r_relation,
        "hint": "a typed link (plm/relations.py) — why this coupon verifies "
                "that revision, why this bench test exists. `target` is the "
                "relation id from get_item()'s `related` block.",
    },
    "approval": {
        "label": "approval",
        "classic": False,
        "resolve": _r_approval,
        "hint": "a release submission — review remarks, the condition an "
                "approver wants met. `target` is the approval id. The "
                "DECISION itself stays on the approval row; this is the "
                "discussion around it.",
    },
    # -- stage 13: the review model (plm/proposals.py, plm/threads.py) -------
    # `review: True` marks the entries that exist only where git-first is on
    # somewhere: `targets()` leaves them out when it is not, so an `off`
    # instance answers exactly what master did (the compatibility harness).
    "proposal": {
        "label": "proposal",
        "classic": False,
        "review": True,
        "resolve": _r_proposal,
        "hint": "a proposal (#n — the pull request that survives review "
                "rounds): the general conversation. To point at a line, a "
                "plm.toml key or a spot on the mesh, open a THREAD instead "
                "(open_thread / POST /api/threads).",
    },
    "release": {
        "label": "release",
        "classic": False,
        "review": True,
        "resolve": _r_release,
        "hint": "a promotion (release id) — where \"printed 12, all fit\" is "
                "recorded against the revision.",
    },
    "thread": {
        "label": "review thread",
        "classic": False,
        "review": True,
        "resolve": _r_thread,
        "hint": "a reply on an anchored review thread (plm/threads.py). The "
                "thread's lifecycle — resolve, addressed_by — is its own "
                "verb; this is the conversation.",
    },
    "change": {
        "label": "change (reserved)",
        "classic": False,
        "review": True,
        "reserved": True,
        "resolve": _r_reserved,
        "hint": "RESERVED for stage 14: a CHANGE — a set of proposals across "
                "items reviewed and approved as one unit (PLM_REVIEW_MODEL "
                "§2.5). Not built; nothing resolves to it yet.",
    },
}

_ALIASES = {"": "item"}     # '' is the classic default, i.e. an item comment


def spec(target_type: str) -> dict:
    """The registry entry for a target type, or a readable default so a row
    written by newer code never disappears from an older reader's UI."""
    t = _ALIASES.get(target_type, target_type)
    if t in TARGETS:
        return {"type": t, **TARGETS[t]}
    return {"type": t, "label": t.replace("_", " "), "classic": False,
            "resolve": lambda conn, ref: None, "hint": ""}


def targets() -> list[dict]:
    """The registry, for callers that build their own UI or prompt from it."""
    from . import gitmode
    review = gitmode.any_serves()
    return [{"type": t, "label": v["label"], "classic": v["classic"],
             "hint": v["hint"]} for t, v in sorted(TARGETS.items())
            if review or not v.get("review")]


def resolve(conn: sqlite3.Connection, target_type: str, target: str | int,
            version: str = "") -> dict:
    """Validate a target and return how a row for it must be written:
    {target_type, target_id, item_id, revision_id, ref}.

    `version` is only meaningful for the item form and is how the classic
    "attach to this revision of this item" call arrives (POST
    /api/items/{n}/context?version=…) — it is resolved to the revision anchor
    here so there is ONE normalization, not one per surface."""
    sp = spec(target_type)
    t = sp["type"]
    if t not in TARGETS:
        raise ContextError(
            f"unknown context target type: {target_type!r} — known types: "
            f"{', '.join(sorted(TARGETS))}")
    if target in (None, "", 0):
        raise ContextError(f"which {sp['label']}? pass target")
    got = sp["resolve"](conn, target)
    if not got:
        raise ContextError(f"unknown {sp['label']}: {target}", 404)
    if version:
        if t != "item":
            raise ContextError(
                f"a version only applies to an item target, not to a {sp['label']}")
        rev = db.find_revision(conn, str(target), version)
        if not rev:
            raise ContextError(f"no revision {version} on {got['ref']}", 404)
        got = {**got, "revision_id": rev["id"], "target_id": rev["id"],
               "ref": f"{got['ref']} v{version}"}
        t = "revision"
    # Classic targets keep the pre-existing storage shape: target_type '' with
    # item_id/revision_id carrying the meaning. One home per fact.
    return {"target_type": "" if TARGETS[t]["classic"] else t,
            "type": t, "target_id": got["target_id"], "item_id": got["item_id"],
            "revision_id": got["revision_id"], "ref": got["ref"]}


# -- the one write path -------------------------------------------------------

def add(conn: sqlite3.Connection, body: str, *, target_type: str = "",
        target: str | int = "", version: str = "", actor: str = "",
        event: bool = True) -> dict:
    """Append one context entry to whatever the registry says exists.

    Append-only by design: context is a log of reasoning, not a mutable field.
    Every caller (HTTP, MCP, ops.fork, ops.renumber_item) comes through here, so
    target validation happens once and cannot be forgotten by a new surface.

    `event=False` is for writes that are the narration of a bigger operation
    which already emits its own event (fork, renumber) — it keeps those paths
    byte-identical to what they logged before this module existed.
    """
    if not (body or "").strip():
        raise ContextError("context body is empty")
    at = resolve(conn, target_type, target, version)
    cur = conn.execute(
        """INSERT INTO context (item_id, revision_id, body, actor, created_at,
                                target_type, target_id)
           VALUES (?,?,?,?,?,?,?)""",
        (at["item_id"], at["revision_id"], body, actor, db.now(),
         at["target_type"], at["target_id"]))
    if event:
        data: dict = {"chars": len(body)}
        if at["target_type"]:
            data.update(target_type=at["target_type"], target_id=at["target_id"],
                        target=at["ref"])
        db.add_event(conn, "context", item_id=at["item_id"],
                     revision_id=at["revision_id"], actor=actor, data=data)
    # `target_type` in the RESPONSE is the semantic type the caller aimed at
    # ('revision' when a version resolved), not the storage form — the storage
    # form is this module's business.
    return {"id": cur.lastrowid, "target_type": at["type"],
            "target_id": at["target_id"], "item_id": at["item_id"],
            "revision_id": at["revision_id"], "target": at["ref"]}


# -- reads --------------------------------------------------------------------

_SELECT = ("SELECT c.*, r.version FROM context c "
           "LEFT JOIN revisions r ON r.id=c.revision_id ")


def for_item(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """The CLASSIC surface, unchanged: every item-level and revision-level entry
    of one item, newest first. Generic comments are excluded — they belong to
    the relation or approval they were attached to, not to the item page."""
    return [dict(r) for r in conn.execute(
        _SELECT + "WHERE c.item_id=? AND c.target_type='' "
                  "ORDER BY c.created_at DESC", (item_id,))]


def entries(conn: sqlite3.Connection, target_type: str, target: str | int,
            version: str = "") -> dict:
    """Every entry on one target, plus what that target is — the generic read
    behind GET /api/context. The item form returns the item's whole log (item-
    level plus every revision's), which is what the item page has always shown."""
    at = resolve(conn, target_type, target, version)
    if not at["target_type"]:
        if at["revision_id"] and (version or at["type"] == "revision"):
            rows = conn.execute(
                _SELECT + "WHERE c.revision_id=? AND c.target_type='' "
                          "ORDER BY c.created_at DESC", (at["revision_id"],))
        else:
            rows = conn.execute(
                _SELECT + "WHERE c.item_id=? AND c.target_type='' "
                          "ORDER BY c.created_at DESC", (at["item_id"],))
    else:
        rows = conn.execute(
            _SELECT + "WHERE c.target_type=? AND c.target_id=? "
                      "ORDER BY c.created_at DESC",
            (at["target_type"], at["target_id"]))
    return {"target_type": at["type"], "target_id": at["target_id"],
            "target": at["ref"], "label": spec(at["type"])["label"],
            "entries": [dict(r) for r in rows]}


def counts(conn: sqlite3.Connection, target_type: str,
           ids: list[int]) -> dict[int, int]:
    """How many comments each of these targets carries — the badge on a
    relation row or an approval, without shipping the bodies."""
    if not ids or not target_type:
        return {}
    marks = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in conn.execute(
        f"""SELECT target_id, COUNT(*) FROM context
            WHERE target_type=? AND target_id IN ({marks})
            GROUP BY target_id""", (target_type, *ids))}
