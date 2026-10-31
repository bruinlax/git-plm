"""Idempotency for the writes that MINT IDENTITY.

== WHY THIS IS SO SMALL ==

Almost nothing in plm needs a replay cache, because almost everything is already
idempotent by construction — and uniqueness you can state in the schema beats
bookkeeping you have to remember:

  checkin            versions are IMMUTABLE, so attempt two is a 409
                     ("{n} v{v} already exists"). The retry is refused, loudly,
                     and no second revision appears.
  create_item with
  an explicit number numbers are UNIQUE, so attempt two is a 409.
  add_bom_line       an occurrence is (child, find_number), and a duplicate is
                     a 409 (see ops.add_bom_line — auto find numbers get an
                     extra guard, because "next free decade" is the one thing
                     about that call that is NOT deterministic).
  set_state,
  set_attributes,
  suppress_bom_line  last-writer-wins on a VALUE. Running them twice with the
                     same argument is the same as running them once.
  relations           unique per (item, relation, target, versions).

That leaves exactly one gap, and it is the one that bit: the paths where plm
CHOOSES the identity for you. `create_item(prefix='CCR', kind='asset')` and
`fork()` allocate the next free number, so attempt two allocates a DIFFERENT
number and succeeds — which is how a 30 s client timeout on a write that had
already committed produced three empty CCR-AST-000{2,3,4} shells that had to be
deleted by hand.

== THE MECHANISM ==

A replay record keyed on either:

  the caller's `idempotency_key`   explicit, the right answer, and what an agent
                                   retrying a request should send;
  or an AUTO key                   sha256 of (op, actor, the request's
                                   identity-bearing fields), honoured only
                                   inside config.IDEMPOTENCY_AUTO_WINDOW.

The auto key is what makes existing clients safe with no change at all: an
identical create_item from the same actor within fifteen minutes is a retry, not
a second bracket. It is deliberately narrow — same actor, same op, byte-identical
arguments, short window — and it is visible: a replayed response carries
`replayed: true` and the key that matched, so nothing silently disappears. If you
genuinely want two identically-named items in one window, pass distinct
idempotency_key values (or different names, which you wanted anyway).

Rows expire (config.IDEMPOTENCY_TTL_SECONDS) and are swept by the same janitor
pattern snippets and staged uploads use: this is a retry window, not history.
The audit trail is the events table, which is never pruned.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import config, db


def fingerprint(op: str, actor: str, fields: dict) -> str:
    """A stable digest of the identity-bearing arguments of one write."""
    payload = json.dumps({"op": op, "actor": actor or "",
                          "fields": {k: fields[k] for k in sorted(fields)}},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def lookup(conn: sqlite3.Connection, *, op: str, actor: str, fields: dict,
           key: str = "") -> dict | None:
    """The stored response for a request plm has already carried out, or None.

    An explicit key matches regardless of age (within its TTL) — the caller
    asserted "this is the same request". An auto key additionally has to be
    recent, because a fingerprint match a week later is far more likely to be a
    second real item than a retry."""
    now = db.now()
    fp = fingerprint(op, actor, fields)
    if key:
        row = conn.execute(
            "SELECT * FROM idempotency WHERE key=? AND expires_at>?",
            (f"key:{key}", now)).fetchone()
        if row:
            return _replay(row, why=f"idempotency_key {key!r}")
        return None
    row = conn.execute(
        "SELECT * FROM idempotency WHERE key=? AND expires_at>? AND created_at>?",
        (f"auto:{fp}", now, now - config.IDEMPOTENCY_AUTO_WINDOW)).fetchone()
    if row:
        return _replay(
            row, why=(f"an identical {op} from {actor or 'this actor'} "
                      f"{now - row['created_at']:.0f}s ago"))
    return None


def _replay(row: sqlite3.Row, *, why: str) -> dict:
    out = json.loads(row["response"] or "{}")
    out["replayed"] = True
    out["idempotent"] = (
        f"Replayed the first attempt's result — {why} already created this. "
        "Nothing new was written. plm mints identity, so a retried "
        "auto-numbered create would otherwise have allocated a SECOND number "
        "and left a duplicate shell behind.")
    return out


def record(conn: sqlite3.Connection, *, op: str, actor: str, fields: dict,
           key: str = "", response: dict, item_id: int | None = None) -> None:
    """Remember what this request returned, so a retry can be answered.

    INSERT OR REPLACE: two racing attempts both write, and they wrote the same
    thing by definition of them being the same request."""
    now = db.now()
    stored = {k: v for k, v in (response or {}).items()
              if k not in ("replayed", "idempotent")}
    conn.execute(
        """INSERT OR REPLACE INTO idempotency
             (key,op,actor,fingerprint,response,item_id,created_at,expires_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (f"key:{key}" if key else f"auto:{fingerprint(op, actor, fields)}",
         op, actor or "", fingerprint(op, actor, fields),
         json.dumps(stored), item_id, now,
         now + config.IDEMPOTENCY_TTL_SECONDS))


def sweep(conn: sqlite3.Connection) -> int:
    """Drop expired replay records. A retry window, not history."""
    cur = conn.execute("DELETE FROM idempotency WHERE expires_at<=?", (db.now(),))
    return cur.rowcount or 0
