"""Registry-wide search: "find me the part we modified because <reason>".

An SQLite FTS5 index (porter stemming) over everything with words in it:
item numbers and names, revision summaries / verification notes / ECNs,
context entries (the "why" markdown — on items, revisions, and on any other
context target: a comment on a relation or an approval is indexed through that
target's item/revision anchor), and the markdown documents attached to
revisions (work instructions, version notes).

== WHY THIS IS INCREMENTAL NOW ==

It used to be rebuilt wholesale after every write, on the theory that "the
corpus is hundreds of rows, so a full reindex is milliseconds plus file reads".
The first half was true and the second half was the whole cost. Measured on
production, 2026-08-13: 505 index rows, 189 attached markdown files, 283 KB of
text — and **41 s warm, 59-120 s cold**, essentially all of it the per-file
`stat` + `read` latency of the network-synced vault (~0.22 s per file).

That number sat in the request path of every write endpoint, which is how a
30 s client timeout fired AFTER a check-in had committed, and how the retry
minted duplicate items. So:

  touch()/index_items()  the write path. Reindexes only the items a write
                         touched — one item is one or two markdown reads, not
                         189 — and only AFTER that write has committed.
  reindex()              the full rebuild, still available and still correct,
                         now an explicit admin action (POST /api/admin/reindex,
                         plm.importer) rather than a tax on every write.

Rows are addressed by item NUMBER, which the FTS table already carries, so this
needed no schema change and no trigger bookkeeping: incremental deletion is
`DELETE FROM search WHERE number IN (…)` over a few hundred rows.

== THE TRANSACTION RULE ==

Indexing reads files off a slow network volume, so it must NEVER run inside an
open write transaction — it would hold SQLite's single writer lock for the
duration of those reads. Both entry points enforce that themselves rather than
trusting the caller: they commit whatever is pending, gather in autocommit, and
then write the index in one short burst. `ensure()` deliberately does NOT use
executescript(), because executescript() issues an implicit COMMIT — that quirk
used to be the only thing keeping the old code from holding the lock for a
minute, and an accident is not an invariant.

Semantic upgrade path (deliberate seam): searchable_rows() yields
(key, text) pairs; an embedding backend (e.g. Ollama nomic-embed-text) can
consume the same rows into a vector table and merge ranks with FTS. Not built
until keyword search proves insufficient.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time

from . import attributes, config, db

MAX_DOC_BYTES = 200_000   # per markdown file pulled into the index

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  number, name, project, kind, version, text,
  tokenize='porter unicode61')
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create the FTS table if it is missing — as ONE statement, not a script.

    conn.executescript() would COMMIT any transaction the caller has open. That
    is how the old write path silently committed a check-in half-way through the
    endpoint, which made the `with db.connect()` block look atomic while it was
    not. One statement, no hidden commit."""
    conn.execute(FTS_SCHEMA)


def _doc_text(rel: str) -> str:
    try:
        p = (config.DATA_ROOT / rel)
        if p.stat().st_size > MAX_DOC_BYTES:
            return ""
        return p.read_text(errors="replace")
    except OSError:
        return ""


# -- gathering: the slow half, always outside a transaction -------------------

def _context_for(conn: sqlite3.Connection, item_ids: list[int] | None
                 ) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Context of EVERY target type (plm/context.py), bucketed by item and by
    revision. Generic comments — on a relation, on an approval — are indexed
    through the (item, revision) anchor their target hangs off, which is the
    whole reason that anchor exists: a comment explaining a bench-test link is
    found by searching the item it concerns, with no per-target-type code here.
    The label is prefixed so a search snippet says what the comment was about."""
    sql = "SELECT item_id, revision_id, body, target_type, target_id FROM context"
    args: tuple = ()
    if item_ids:
        sql += f" WHERE item_id IN ({','.join('?' * len(item_ids))})"
        args = tuple(item_ids)
    by_item: dict[int, list[str]] = {}
    by_rev: dict[int, list[str]] = {}
    for c in conn.execute(sql, args).fetchall():
        body = c["body"]
        if c["target_type"]:
            body = f"[comment on {c['target_type']} {c['target_id']}]\n{body}"
        if c["revision_id"]:
            by_rev.setdefault(c["revision_id"], []).append(body)
        else:
            by_item.setdefault(c["item_id"], []).append(body)
    return by_item, by_rev


def _item_rows(conn: sqlite3.Connection, item: sqlite3.Row,
               ctx_by_item: dict[int, list[str]],
               ctx_by_rev: dict[int, list[str]]) -> list[tuple]:
    """Every index row for ONE item: the item itself, then one per revision.

    Shared by the incremental and the full path so the two can never disagree
    about what a row contains — the reason a partial reindex is trustworthy."""
    # Former numbers are searchable too: someone who remembers the old
    # number should find the item, not nothing (ops.renumber_item).
    former = (item["former_numbers"] or "").replace(",", " ")
    rows: list[tuple] = [
        (item["number"], item["name"], item["project"], item["kind"], "",
         "\n".join([*ctx_by_item.get(item["id"], []),
                    f"former numbers: {former}" if former else ""]))]
    for rev in conn.execute("SELECT * FROM revisions WHERE item_id=?",
                            (item["id"],)).fetchall():
        parts = [rev["summary"], rev["verification"], rev["ecn"],
                 rev["source"], rev["selector"]]
        # Attributes are searchable text: "PETG", "bbox_z_mm", a colour.
        # Read through attributes.get so the column-backed facts
        # (material/mass_g) are indexed from their ONE home, not twice.
        parts.append(attributes.search_text(conn, rev["id"]))
        # The ROUTING is searchable text too (plm/fab.py): 'notch',
        # 'cope', '25/64' and 'saddle for 1.660 OD' are exactly how somebody
        # finds the part they already worked out once.
        from . import fab
        parts.append(fab.search_text(conn, rev["id"]))
        parts += ctx_by_rev.get(rev["id"], [])
        for f in conn.execute(
                "SELECT path FROM files WHERE revision_id=? AND path LIKE '%.md'",
                (rev["id"],)).fetchall():
            parts.append(_doc_text(f["path"]))
        rows.append((item["number"], item["name"], item["project"], item["kind"],
                     rev["version"], "\n".join(x for x in parts if x)))
    return rows


def _numbers_of(item: sqlite3.Row) -> list[str]:
    """The numbers rows for this item may be filed under: its current one and
    every number it used to have. Renumbering rewrites the `number` column, so
    without the former ones an incremental reindex would leave the pre-renumber
    rows behind as ghosts."""
    return [item["number"]] + [n for n in (item["former_numbers"] or "").split(",") if n]


def _write(conn: sqlite3.Connection, numbers: list[str],
           rows: list[tuple]) -> int:
    """The fast half: replace the rows for `numbers` in one short burst.

    Milliseconds, and the only part that touches the writer lock. `number` is an
    ordinary FTS5 column, so the delete is a scan-and-filter over a few hundred
    rows — cheaper than the trigger bookkeeping it replaces."""
    with conn:
        if numbers:
            conn.execute(
                f"DELETE FROM search WHERE number IN ({','.join('?' * len(numbers))})",
                tuple(numbers))
        if rows:
            conn.executemany(
                "INSERT INTO search (number,name,project,kind,version,text) "
                "VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def index_items(conn: sqlite3.Connection, refs: list[str | int]) -> int:
    """Reindex exactly the items named — the write path's reindex.

    Costs the markdown reads of those items alone. Refs may be ids, current
    numbers or former numbers (db.resolve_item), because callers hold whatever
    they hold."""
    ensure(conn)
    items, numbers = [], []
    for ref in refs:
        row = db.resolve_item(conn, ref)
        if not row or row["id"] in [i["id"] for i in items]:
            continue
        items.append(row)
        numbers += _numbers_of(row)
    if not items:
        return 0
    ctx_item, ctx_rev = _context_for(conn, [i["id"] for i in items])
    rows: list[tuple] = []
    for item in items:
        rows += _item_rows(conn, item, ctx_item, ctx_rev)
    return _write(conn, numbers, rows)


def touch(conn: sqlite3.Connection, *refs: str | int) -> int:
    """THE write path's search call: commit, then reindex the touched items.

    Replaces `search.reindex(conn)` at every write site, and the two differences
    are the whole of - it COMMITS FIRST. Indexing reads files off the synced vault; doing that
      inside the caller's open transaction would hold SQLite's single writer
      lock for the duration. Committing also states the ordering plainly: the
      write is durable before the index is touched, so an index failure can
      never un-write a check-in.
    - it indexes the items the write touched, not the corpus.

    Never raises. The write is already committed by the time this runs; turning
    a successful write into a 500 because a markdown file was unreadable would
    be strictly worse than a slightly stale index (the admin full reindex is
    the repair, and it is one call)."""
    try:
        if conn.in_transaction:
            conn.commit()
        return index_items(conn, [r for r in refs if r not in (None, "", 0)])
    except Exception as exc:                                   # noqa: BLE001
        print(f"plm: search index update failed for {refs}: {exc} — the write "
              "is committed; POST /api/admin/reindex rebuilds",
              file=sys.stderr, flush=True)
        return -1


def reindex(conn: sqlite3.Connection) -> int:
    """THE FULL REBUILD — every item, every revision. An explicit admin action.

    Correct but expensive: 189 markdown files off the synced vault measured 41 s
    on production. It stays because a rebuildable index has to actually be
    rebuildable — after an import, a schema change, or an incremental update
    that was skipped because a file was unreadable.

    Two phases, and unlike the old version this one really does keep the writer
    lock short: any pending transaction is committed FIRST, the gather runs in
    autocommit, and only the delete+insert burst takes the lock."""
    ensure(conn)
    if conn.in_transaction:
        # Never inherit somebody's write lock and hold it through the reads.
        conn.commit()
    started = time.monotonic()
    ctx_item, ctx_rev = _context_for(conn, None)
    rows: list[tuple] = []
    for item in conn.execute("SELECT * FROM items").fetchall():
        rows += _item_rows(conn, item, ctx_item, ctx_rev)
    gathered = time.monotonic() - started
    with conn:
        conn.execute("DELETE FROM search")
        conn.executemany(
            "INSERT INTO search (number,name,project,kind,version,text) "
            "VALUES (?,?,?,?,?,?)", rows)
    print(f"plm: full search reindex — {len(rows)} rows, {gathered:.1f}s "
          f"gathering (vault reads), {time.monotonic() - started - gathered:.2f}s "
          "writing", file=sys.stderr, flush=True)
    return len(rows)


def query(conn: sqlite3.Connection, q: str, limit: int = 30) -> list[dict]:
    """Natural-ish query: all terms first (best precision); if nothing
    matches, any term (recall), still bm25-ranked."""
    ensure(conn)
    terms = re.findall(r"[\w][\w.-]*", q)
    if not terms:
        return []
    quoted = [f'"{t}"' for t in terms]

    def run(match: str) -> list[dict]:
        return [dict(r) for r in conn.execute(
            """SELECT number, name, kind, project, version,
                      snippet(search, 5, '«', '»', ' … ', 20) AS snippet,
                      bm25(search) AS rank
               FROM search WHERE search MATCH ?
               ORDER BY rank LIMIT ?""", (match, limit))]

    hits = run(" AND ".join(quoted))
    mode = "all terms"
    if not hits and len(quoted) > 1:
        hits = run(" OR ".join(quoted))
        mode = "any term"
    return [{**h, "mode": mode} for h in hits]
