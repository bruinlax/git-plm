"""Repos that FOLLOW legacy writes — the `read` mode's other half (stage 9).

In PLM_GIT_MODE=`read` the registry is still the record: writes take the legacy
path (DB + vault), and the repositories are a PROJECTION of it — the migration's
`split --normalize-names`, served read-only. After a legacy write the projection
is stale. This module keeps it current, item by item:

    db.add_event(item_id=N)          every legacy write is evented ("everything
          │                          evented", the charter) — the same seam
          ▼                          the checks hang off for git writes
    gitsync.notice(N)                in memory only: NEVER touches the database
          │                          (it is called inside the writer's
          ▼                          transaction — checks.enqueue's rule)
    debounce (PLM_GITSYNC_DEBOUNCE, 2 s quiet; at most PLM_GITSYNC_MAX_WAIT)
          │
          ▼
    split --item N --prune (the root's recorded layout)   in-process, under one flock
          │
          ▼  did any EXISTING ref of N move? (a tag re-made because a released
          │  revision's facts changed — not merely a new tag appearing)
    parents of N (where-used, ONE level) are re-split in the next pass, and
    cascade only if THEIR refs move in turn.

WHY "one level per pass" AND NOT "mark stale, rebuild lazily". A parent's
gitlinks pin the child's COMMITS. A new child release does not move any
existing pin (imprecise lines pin the newest tag dated ≤ the parent revision,
precise lines name a version), so the common case — a check-in, a revise, a
submit — re-splits exactly one repo and stops. Only a change to an ALREADY
TAGGED revision re-makes that tag, and only then do parents' pins point at a
commit the child no longer has; that is the case the cascade exists for, and it
is bounded by the BOM's depth. Lazy-on-read would put a 1-3 s split inside a
page load and still need this walk. Measured in the design notes.

THE STALENESS STATE IS COMPUTED, NOT REMEMBERED. The API and the MCP are two
processes, each with its own queue; neither can see the other's in-memory
pending set, and neither may write a row from inside a writer's transaction.
So the repo page asks the database a question it can always answer honestly:
"is there an event for this item newer than the last completed sync?" — the
`gitsync` table is written by the worker after a sync, the `events` table by
every write. Newer event ⇒ "syncing… as of <the last sync>".

WHAT THIS NEVER DOES
  * touch a LIVE repo — `split` skips any repo holding pushed or plm-written
    work (`plm-written` marker) and says so; a sync of one is a no-op;
  * run for a project in `off` (no repos);
  * touch a CROSSED-OVER item of a `write` project. There a design write IS a
    commit and the repo is the record. But an item that has NOT crossed yet
    takes legacy writes in `write` too (stage 3b: its first revise is the
    crossing), and its generated repo is exactly a `read` projection — so it is
    synced the same way (stage 9b, open question 3). Otherwise its repo goes
    stale, and the crossing then starts from stale refs. The race stage 9 named
    — a split over a repo `gitwrite` is committing to — is closed by taking
    that repo's own write lock (`gitwrite.lock`) around the split and
    re-checking liveness under it: a crossing either finished first (the repo
    is live; nothing is written) or waits for the split;
  * run at all on an instance with no project in `read`/`write` (`notice`
    checks a cached `gitmode.any_serves()` and returns).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import db

DEBOUNCE_S = float(os.environ.get("PLM_GITSYNC_DEBOUNCE", "2.0"))
MAX_WAIT_S = float(os.environ.get("PLM_GITSYNC_MAX_WAIT", "10.0"))
ENABLED = os.environ.get("PLM_GITSYNC", "1") not in ("0", "", "no", "false")

_lock = threading.Condition()
_pending: dict[int, tuple[float, float]] = {}      # item_id -> (first, last)
_worker: threading.Thread | None = None
_on_cache = (0.0, False)
_ON_TTL = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS gitsync (
  number TEXT PRIMARY KEY,
  synced_at REAL NOT NULL,          -- when the last sync of this item FINISHED
  started_at REAL NOT NULL,
  seconds REAL NOT NULL DEFAULT 0,
  refs_moved INTEGER NOT NULL DEFAULT 0,   -- existing refs whose sha changed
  refs_added INTEGER NOT NULL DEFAULT 0,
  refs_pruned INTEGER NOT NULL DEFAULT 0,
  skipped TEXT NOT NULL DEFAULT '',        -- why nothing was written (live repo…)
  error TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT ''             -- notice | parent:<N> | cli
);
"""


def ensure_schema(conn=None) -> None:
    """Statement by statement — never `executescript`, which COMMITs the
    caller's open transaction first (stage 16b: `status(conn)` runs inside
    other requests' transactions)."""
    from .proposals import _statements
    own = conn is None
    conn = conn or db.connect()
    try:
        for st in _statements(SCHEMA):
            conn.execute(st)
    finally:
        if own:
            conn.commit()
            conn.close()


def _git_on() -> bool:
    """Is any project in read/write? Cached: `notice` runs on every event."""
    global _on_cache
    at, on = _on_cache
    now = time.monotonic()
    if now - at < _ON_TTL:
        return on
    try:
        from . import gitmode
        on = gitmode.any_serves(modes=("read", "write"))
    except Exception:  # noqa: BLE001
        on = False
    _on_cache = (now, on)
    return on


#: >0 while a caller has said "these writes will be ROLLED BACK" (stage 16b).
_suspended = 0


class suspended:
    """`with gitsync.suspended():` — notices are DROPPED for the duration.

    For `gitindex --rebuild --check --dry-run`, whose re-indexing adds events
    it then rolls back. Each event used to `notice()` its item, which started
    a `plm-gitsync` thread IN THE TOOL'S OWN PROCESS; two seconds later that
    thread re-split items under a write transaction, and the next item's wipe
    failed "database is locked … DELETE FROM files" (27 of 387 items on the
    quiet POC, a different 27 each run). A rolled-back write has nothing to
    project, so there is nothing to notice."""
    def __enter__(self):
        global _suspended
        _suspended += 1
        return self

    def __exit__(self, *exc):
        global _suspended
        _suspended -= 1
        return False


def notice(item_id: int | None) -> None:
    """A write touched this item. **Never raises, never blocks, never writes.**"""
    if not item_id or not ENABLED or _suspended:
        return
    try:
        if not _git_on():
            return
        now = time.monotonic()
        with _lock:
            first, _ = _pending.get(item_id, (now, now))
            _pending[item_id] = (first, now)
            _lock.notify()
        _start()
    except Exception:  # noqa: BLE001 — a projection must never break a write
        pass


def _start() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="plm-gitsync", daemon=True)
        _worker.start()


def _due(now: float) -> list[int]:
    return [i for i, (first, last) in _pending.items()
            if now - last >= DEBOUNCE_S or now - first >= MAX_WAIT_S]


def _loop() -> None:
    while True:
        with _lock:
            while True:
                now = time.monotonic()
                due = _due(now)
                if due:
                    for i in due:
                        _pending.pop(i, None)
                    break
                _lock.wait(timeout=0.25 if _pending else 30)
        try:
            sync_items(due, via="notice")
        except Exception as exc:  # noqa: BLE001 — the worker never dies
            print(f"[gitsync] pass failed: {exc}", flush=True)


# -- the sync itself ----------------------------------------------------------

def _refs(repo: Path) -> dict[str, str]:
    from . import split
    if not (repo / "HEAD").exists():
        return {}
    return split.existing_refs(repo)


def sync_items(item_ids: list[int], *, via: str = "notice",
               max_passes: int = 8) -> list[dict]:
    """Re-split these items (and, pass by pass, the parents whose pins moved).
    Returns one record per item synced. Safe to call from any thread or CLI."""
    from . import gitmode, gitwrite, manifest, safegit, split
    out: list[dict] = []
    todo = list(dict.fromkeys(item_ids))
    done: set[int] = set()
    passes = 0
    while todo and passes < max_passes:
        passes += 1
        nxt: list[int] = []
        with db.connect() as conn:
            ensure_schema(conn)
            rows = {r["id"]: dict(r) for r in conn.execute(
                f"SELECT id, number, kind, project FROM items WHERE id IN "
                f"({','.join('?' * len(todo))})", todo)}
        for iid in todo:
            item = rows.get(iid)
            if not item or iid in done:
                continue
            done.add(iid)
            with db.connect() as conn:
                mode = gitmode.mode_for(item["project"] or "", conn)
            if mode not in ("read", "write"):
                continue
            if mode == "write" and gitwrite.is_live(_repo_of(item)):
                continue        # crossed over: git is its record — never touched
            item["mode"] = mode
            rec = _sync_one(item, via=via if passes == 1 else via)
            out.append(rec)
            if rec.get("refs_moved"):
                from . import relations as relmod
                with db.connect() as conn:
                    for r in conn.execute(
                            "SELECT DISTINCT r.item_id FROM bom b "
                            "JOIN revisions r ON r.id=b.parent_rev_id "
                            "WHERE b.child_item_id=?", (iid,)):
                        if r["item_id"] not in done:
                            nxt.append(r["item_id"])
                    # …and the CLAIMANTS (stage 10): a coupon's tree pins this
                    # item's commit exactly as a parent's gitlink does, so a
                    # moved tag strands its claim the same way.
                    for r in conn.execute(
                            "SELECT DISTINCT item_id, relation FROM relations "
                            "WHERE target_item_id=?", (iid,)):
                        if (relmod.home(r["relation"]) != "item"
                                and r["item_id"] not in done):
                            nxt.append(r["item_id"])
        todo = list(dict.fromkeys(nxt))
        via = f"parent-pass-{passes + 1}"
    return out


def _repo_of(item: dict) -> Path:
    from . import manifest, split
    catalog = item["kind"] in manifest.CATALOG_KINDS
    return split.REPOS / ("catalog.git" if catalog else f"{item['number']}.git")


class _Skip(Exception):
    pass


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sync_one(item: dict, *, via: str) -> dict:
    from . import gitwrite, manifest, safegit, split
    number = item["number"]
    catalog = item["kind"] in manifest.CATALOG_KINDS
    repo = _repo_of(item)
    t0 = time.time()
    rec = {"number": number, "started_at": t0, "via": via, "error": "",
           "skipped": "", "refs_moved": 0, "refs_added": 0, "refs_pruned": 0}
    try:
        # ONE splitter at a time per runtime root: the API and the MCP are two
        # processes and may both have noticed the same write.
        # In `write`, also the item repo's OWN write lock — the one a crossing
        # (gitwrite) takes — and liveness re-checked under it.
        in_write = item.get("mode") == "write" and (repo / "HEAD").exists()
        with safegit.flock(split.REPOS / ".gitsync.lock", "the repo sync",
                           wait=120), \
                (gitwrite.lock(repo, wait=30) if in_write else _NoLock()):
            if in_write and gitwrite.is_live(repo):
                rec["skipped"] = ("crossed over while queued — git is its "
                                  "record; not regenerated")
                raise _Skip
            before = _refs(repo)
            with db.connect() as conn:
                # roots=[] builds only the catalog — which is what a purchased
                # or hardware item's write changes (§5.4: one TOML each).
                # The filename layout is the ROOT's (`split.LAYOUT_FILE`,
                # recorded by the step-2 build), never this caller's opinion. A
                # root with no marker and no repos is a first build: it gets
                # the migration's layout. One with repos but no marker refuses
                # (SplitError → this row's `error`) rather than guess.
                normalize = None if (split.layout() or split._has_repos(
                    split.REPOS)) else True
                res = split.run(conn, roots=[] if catalog else [item["id"]],
                                normalize=normalize, prune=True)
            after = _refs(repo)
        b = res["built"].get(item["id"]) if not catalog else None
        if b and b.get("skipped"):
            rec["skipped"] = "live repository — git is its record; not regenerated"
        # Only a MOVED tag or branch can strand a parent's pin; the item ref
        # (refs/plm/item) moving is item master data and pins nothing.
        rec["refs_moved"] = sum(1 for r, s in before.items()
                                if r in after and after[r] != s
                                and r.startswith(("refs/tags/", "refs/heads/")))
        rec["refs_added"] = sum(1 for r in after if r not in before)
        rec["refs_pruned"] = sum(1 for r in before if r not in after)
    except _Skip:
        pass
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"[:500]
    rec["seconds"] = round(time.time() - t0, 3)
    rec["synced_at"] = time.time()
    try:
        with db.connect() as conn:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO gitsync (number, synced_at, started_at, seconds, "
                "refs_moved, refs_added, refs_pruned, skipped, error, via) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(number) DO UPDATE SET "
                "synced_at=excluded.synced_at, started_at=excluded.started_at, "
                "seconds=excluded.seconds, refs_moved=excluded.refs_moved, "
                "refs_added=excluded.refs_added, refs_pruned=excluded.refs_pruned,"
                " skipped=excluded.skipped, error=excluded.error, via=excluded.via",
                (number, rec["synced_at"], t0, rec["seconds"], rec["refs_moved"],
                 rec["refs_added"], rec["refs_pruned"], rec["skipped"],
                 rec["error"], via))
    except Exception as exc:  # noqa: BLE001
        print(f"[gitsync] could not record the sync of {number}: {exc}",
              flush=True)
    return rec


# -- the state the repo page shows --------------------------------------------

def status(conn, number: str) -> dict:
    """`{state, as_of, dirty_since, seconds, error}` for one item's repo.

    state: `synced` · `syncing` (an event is newer than the last sync — the
    worker is on it, or will be within the debounce window) · `error` (the last
    sync failed; the message says why) · `generated` (never synced since the
    migration built it — as_of is when its refs were last written).
    """
    from . import split
    item = db.resolve_item(conn, number)
    if not item:
        return {"state": "unknown"}
    try:
        ensure_schema(conn)
    except Exception:  # noqa: BLE001
        pass
    last = conn.execute("SELECT MAX(at) FROM events WHERE item_id=?",
                        (item["id"],)).fetchone()[0] or 0.0
    try:
        row = conn.execute("SELECT * FROM gitsync WHERE number=?",
                           (item["number"],)).fetchone()
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        repo = split.REPOS / f"{item['number']}.git"
        stamp = 0.0
        for p in (repo / "packed-refs", repo / "refs" / "heads", repo / "refs" / "tags"):
            try:
                stamp = max(stamp, p.stat().st_mtime)
            except OSError:
                pass
        state = "syncing" if last > stamp else "generated"
        return {"state": state, "as_of": stamp or None,
                "dirty_since": last if last > stamp else None,
                "seconds": None, "error": ""}
    if row["error"]:
        state = "error"
    elif last > row["synced_at"]:
        state = "syncing"
    else:
        state = "synced"
    return {"state": state, "as_of": row["synced_at"],
            "dirty_since": last if last > row["synced_at"] else None,
            "seconds": row["seconds"], "error": row["error"],
            "skipped": row["skipped"], "via": row["via"]}


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m plm.gitsync",
        description="Re-split items now (what the debounced worker does).")
    ap.add_argument("--item", action="append", default=[], help="item number")
    ap.add_argument("--status", action="store_true", help="print the state only")
    args = ap.parse_args(argv)
    with db.connect() as conn:
        ids = []
        for n in args.item:
            it = db.resolve_item(conn, n)
            if not it:
                print(f"unknown item {n}")
                return 2
            ids.append(it["id"])
        if args.status:
            for n in args.item:
                print(n, json.dumps(status(conn, n)))
            return 0
    for rec in sync_items(ids, via="cli"):
        print(json.dumps(rec))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
