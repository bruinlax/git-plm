"""The CHANGE — the project-level pull request (stage 14).

docs/PLM_REVIEW_MODEL.md §2.5 is the spec; docs/PLM_CHANGES.md (the August ECO
design: impact analysis, dispositions, agents draft / humans decide) is the
idea, built here on git. Geoff, 2026-10-05:

    "I'd like my workflow to be like the agent is asking me to approve a pull
    request to main for the project — and then also approve a release."

A piece of work rarely lives in one repo: a part changes, its assembly's pin is
bumped, a coupon proves the fit. Stage 13 gave each of those a PROPOSAL (one
per draft branch). This module groups them into ONE reviewable, decidable
thing:

    changes          CHG-n. Title, body, the "home" project, state
                     draft | open | approved | merged | rejected | withdrawn,
                     who decided and why, and — because one decision merges N
                     repos — an explicit, resumable merge log.
    proposals        + `change_id` (stage 13's seam): a proposal belongs to at
                     most one change.
    change_reviews   the change-level signoffs, bound to EVERY member's head
                     sha at once ({proposal: sha}). A push to any member
                     dismisses the ones it made stale — visibly.
    change_impacts   impact analysis (computed from the gitlink graph) and the
                     DISPOSITION a person recorded per affected parent:
                     bump (in this change) | use_as_is | no_impact, + a note.

THE RULES, decided with the brief (do not re-litigate without reading why):

  * EVERY REVIEW IS A CHANGE. A proposal submitted with no change gets a
    single-member change (`auto = 1`) at the moment it reaches a human, so the
    human sees ONE kind of thing in the queue. Draft proposals that nobody has
    asked anybody to look at are not reviews yet and are left alone. Existing
    OPEN proposals are wrapped by `backfill()`.
  * `PLM_GIT_MODE != write`: NOTHING here runs. No change rows, no event rows,
    no response keys — the compatibility harness stays additive-only (the tables exist).
  * ONE DECISION. `ops.decide_change` pre-flights everything BEFORE the first
    merge (`preflight()`), then merges in dependency order (`plan()`):
    children before parents, and a parent whose gitlink points at a member's
    DRAFT head is re-pinned to that member's release commit as part of the
    parent's own release commit (`gitindex.approve_commit(repin=…)`).
  * A MEMBER'S DIRECT DECISION IS REFUSED when the change has more than one
    member ("decide the change"). A single-member change follows its proposal,
    so every stage-4/13 door (`/api/approvals/{id}/decide`, the proposal
    page's Approve, the MCP's decide_approval) keeps working unchanged.
  * IMPACT IS COMPUTED, DISPOSITIONS ARE RECORDED. Agents may DRAFT them; a
    human's disposition is CONFIRMED. Confirmed dispositions are required only
    when a member targets RELEASED; at PROTOTYPE the panel informs, it does not
    gate. A `bump` disposition is a promise — "the bump is in this change" — and
    a change whose promise is unkept is refused at any maturity.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from . import config, db, proposals, webroutes

STATES = ("draft", "open", "approved", "merged", "rejected", "withdrawn")
CLOSED = ("approved", "merged", "withdrawn")
#: ⭐ OPEN — the one definition (the list's Open tab, the banners, the badge,
#: `?state=open`): everything not CLOSED. A refused pull request is open: the
#: next push to its branches re-opens it.
OPEN = tuple(s for s in STATES if s not in CLOSED)
DISPOSITIONS = {
    "bump": "bump in this pull request",
    "use_as_is": "use as is",
    "no_impact": "no impact",
    # stage 19 (handoff finding 4) — for an item that carries a DIFFERENT copy
    # of a source a member carries (`source-drift`, `shared-source`): a
    # different blob is a signal, not proof that the item's part changed
    "unchanged_geometry": "selected geometry unchanged",
    "historical": "historical copy (evidence for an older target)",
}
#: the dispositions that only mean something for a carried SOURCE file
SOURCE_DISPOSITIONS = ("unchanged_geometry", "historical")
#: every spelling a pull request has had — ONE parser, plm/webroutes.py's
LABEL_RE = webroutes.LABEL_RE

_SCHEMA = """
-- THE CHANGE (plm/changes.py, PLM_REVIEW_MODEL §2.5). `id` IS the CHG-n number.
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL DEFAULT '',        -- the HOME project (members may span)
  title TEXT NOT NULL DEFAULT '',
  body_md TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'draft',     -- draft|open|approved|merged|
                                           -- rejected|withdrawn
  author TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT '',         -- push|api|mcp|auto|backfill
  auto INTEGER NOT NULL DEFAULT 0,         -- 1 = wrapped around ONE proposal
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  submitted_at REAL,
  decided_at REAL,
  decided_by TEXT NOT NULL DEFAULT '',
  decided_kind TEXT NOT NULL DEFAULT '',   -- human | bot | policy
  decision_note TEXT NOT NULL DEFAULT '',
  merge_state TEXT NOT NULL DEFAULT '',    -- '' | merging | partial | done
  merge_log TEXT NOT NULL DEFAULT '[]'     -- one entry per member decided
);
CREATE INDEX IF NOT EXISTS idx_changes_state ON changes(state);

-- CHANGE-LEVEL SIGNOFFS, bound to every member's head at once.
CREATE TABLE IF NOT EXISTS change_reviews (
  id INTEGER PRIMARY KEY,
  change_id INTEGER NOT NULL REFERENCES changes(id),
  reviewer TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',           -- human | bot | policy
  verdict TEXT NOT NULL,                   -- approve | reject | comment
  heads TEXT NOT NULL DEFAULT '{}',        -- {"<proposal id>": "<sha>"}
  body_md TEXT NOT NULL DEFAULT '',
  counts INTEGER NOT NULL DEFAULT 0,       -- 1 = the decision of record
  identity TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  dismissed_at REAL,
  dismissed_by TEXT NOT NULL DEFAULT ''    -- "#<proposal>@<sha>" that moved
);
CREATE INDEX IF NOT EXISTS idx_change_reviews ON change_reviews(change_id);

-- IMPACT + DISPOSITION per affected parent (PLM_CHANGES.md, on git).
CREATE TABLE IF NOT EXISTS change_impacts (
  id INTEGER PRIMARY KEY,
  change_id INTEGER NOT NULL REFERENCES changes(id),
  parent_item_id INTEGER NOT NULL REFERENCES items(id),
  disposition TEXT NOT NULL DEFAULT '',    -- bump | use_as_is | no_impact
  note TEXT NOT NULL DEFAULT '',
  drafted_by TEXT NOT NULL DEFAULT '',
  drafted_kind TEXT NOT NULL DEFAULT '',
  confirmed_by TEXT NOT NULL DEFAULT '',   -- a HUMAN; required at RELEASED
  confirmed_at REAL,
  updated_at REAL NOT NULL,
  UNIQUE (change_id, parent_item_id)
);
"""

_ready = False
_ready_lock = threading.Lock()


def ensure(conn: sqlite3.Connection) -> None:
    """Tables + the backfill, once per process, on the CALLER's connection and
    without committing (the stage-13 rule: we are reached from inside ops
    transactions)."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        from . import proposals
        proposals.ensure(conn)
        for stmt in proposals._statements(_SCHEMA):
            conn.execute(stmt)
        _ready = True          # before the backfill (it may ask ensure() again)
        try:
            backfill(conn)
        except Exception as exc:                               # noqa: BLE001
            print(f"[changes] backfill skipped: {exc}", flush=True)


class ChangeError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# -- names and addresses --------------------------------------------------------

def parse_id(ref) -> int:
    m = LABEL_RE.match(str(ref or ""))
    if not m:
        raise ChangeError(f"not a pull request: {ref!r} — a pull request is CHG-<n>", 400)
    return int(m.group(1))


def label(cid: int) -> str:
    """The legacy label — `CHG-380`. Still ACCEPTED everywhere (`parse_id`)
    and still the API's `number`; what a person reads is `ref`."""
    return f"CHG-{cid}"


def ref(cid: int) -> str:
    """The pull request as people say it: `#380` (GitHub's word and mark)."""
    return f"#{cid}"


def title_ref(cid: int) -> str:
    """`Pull request #380` — how a sentence about one starts."""
    return f"Pull request {ref(cid)}"


def page(cid: int) -> str:
    """The pull request's page — a PATH (`/pulls/n`), not a #fragment."""
    return webroutes.change(cid)


def url(cid: int) -> str:
    return f"{config.PUBLIC_URL}{page(cid)}"


def names(cid: int) -> dict:
    """Every name of one pull request, as a payload carries them — spread
    into `brief`, `view`, `listing` and `pending.change_entry` alike."""
    return {"id": cid, "number": label(cid), "ref": ref(cid), "url": url(cid),
            "page": page(cid)}


def _writes(conn, item) -> bool:
    from . import ops
    return ops.git_writes(conn, item)


def row(conn, cid) -> dict:
    ensure(conn)
    cid = parse_id(cid)
    r = conn.execute("SELECT * FROM changes WHERE id=?", (cid,)).fetchone()
    if not r:
        raise ChangeError(f"no pull request {ref(cid)}", 404)
    return dict(r)


def members(conn, cid: int) -> list[dict]:
    """The proposals in a change, with their item and their open round."""
    rows = [dict(r) for r in conn.execute(
        "SELECT p.*, i.number AS item_number, i.name AS item_name, "
        "i.project AS item_project, i.kind AS item_kind FROM proposals p "
        "JOIN items i ON i.id=p.item_id WHERE p.change_id=? ORDER BY p.id",
        (cid,))]
    for m in rows:
        rnd = conn.execute(
            "SELECT id, target_state, requested_by, requested_at FROM approvals "
            "WHERE proposal_id=? AND decision='' ORDER BY id DESC LIMIT 1",
            (m["id"],)).fetchone()
        m["round"] = dict(rnd) if rnd else None
        m["target"] = (rnd["target_state"] if rnd else
                       (m["target_maturity"] or "PROTOTYPE")).upper()
    return rows


def member_count(conn, cid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM proposals WHERE change_id=?",
                        (cid,)).fetchone()[0]


def _home_item(conn, cid: int) -> int | None:
    r = conn.execute("SELECT item_id FROM proposals WHERE change_id=? "
                     "ORDER BY id LIMIT 1", (cid,)).fetchone()
    return r[0] if r else None


def event(conn, type_: str, cid: int, *, actor: str = "", data: dict | None = None,
          item_id: int | None = None) -> None:
    """An event row on the change's HOME item (its first member). Only ever
    written where git-first writes — every caller is behind that test."""
    db.add_event(conn, type_, item_id=item_id or _home_item(conn, cid),
                 actor=actor, data={"change": cid, **(data or {})})


def _touch(conn, cid: int, **fields) -> None:
    """THE write of a pull request's row. Closing it (merged, approved,
    withdrawn) also drops its rendered check images — derived data, re-made
    on demand if somebody opens the closed page."""
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE changes SET {sets} WHERE id=?", (*fields.values(), cid))
    if fields.get("state") in CLOSED:
        from . import checks
        checks.drop_images(label(cid))


# -- creating, joining ----------------------------------------------------------

def create(conn, *, title: str, body: str = "", project: str = "", actor: str,
           origin: str = "api", auto: bool = False) -> dict:
    ensure(conn)
    if not (title or "").strip():
        raise ChangeError("a pull request needs a title — say what the piece of work "
                          "is, in one line")
    if not db.get_user(conn, actor):
        raise ChangeError(f"unknown user: {actor}", 403)
    t = db.now()
    cur = conn.execute(
        "INSERT INTO changes (project, title, body_md, state, author, origin, auto, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (project or "", title.strip()[:200], body or "", "draft", actor, origin,
         int(bool(auto)), t, t))
    return row(conn, cur.lastrowid)


def add_member(conn, cid, pid: int, *, actor: str, origin: str = "api") -> dict:
    """Put proposal #pid into change `cid`.

    A proposal belongs to at most ONE change. The one exception is the
    automatic single-member wrapper: a proposal that was auto-wrapped (it was
    submitted on its own) may be moved into a real change while that wrapper is
    not open — the wrapper is withdrawn, empty, and says where it went."""
    from . import proposals
    c = row(conn, cid)
    cid = c["id"]
    if c["state"] in CLOSED:
        raise ChangeError(f"{title_ref(cid)} is {c['state']} — open a new pull request "
                          "for new work", 409)
    p = proposals._row(conn, int(pid))
    if not p:
        raise ChangeError(f"no proposal {proposals.label(pid)}", 404)
    if p["state"] in proposals.CLOSED:
        raise ChangeError(f"proposal {proposals.label(pid)} is {p['state']} — a closed proposal "
                          "cannot join a pull request", 409)
    item = conn.execute("SELECT * FROM items WHERE id=?", (p["item_id"],)).fetchone()
    if not _writes(conn, item):
        raise ChangeError(f"{item['number']} is not in a git-first `write` "
                          "project, so it has no draft branch to review as part "
                          "of a pull request", 409)
    if p["change_id"] == cid:
        return brief(conn, c)
    if p["change_id"]:
        old = row(conn, p["change_id"])
        if not (old["auto"] and old["state"] in ("draft", "rejected")
                and member_count(conn, old["id"]) == 1):
            raise ChangeError(
                f"proposal {proposals.label(pid)} already belongs to pull request {ref(old['id'])} "
                f"({old['state']}, \"{old['title']}\"). A proposal is in at most "
                "one pull request.", 409)
        _touch(conn, old["id"], state="withdrawn", decision_note=(
            f"its only proposal {proposals.label(pid)} moved to pull request {ref(cid)}"))
        event(conn, "change.withdrawn", old["id"], actor=actor,
              data={"why": f"{proposals.label(pid)} moved to pull request {ref(cid)}"},
              item_id=p["item_id"])
    clash = conn.execute(
        "SELECT id FROM proposals WHERE change_id=? AND item_id=? AND id<>?",
        (cid, p["item_id"], pid)).fetchone()
    if clash:
        raise ChangeError(
            f"{title_ref(cid)} already holds proposal {proposals.label(clash[0])} on "
            f"{item['number']}. One proposal per item per change — two drafts "
            "of the same part in one decision would race each other for "
            "`main`. Push both edits to the same draft branch.", 409)
    conn.execute("UPDATE proposals SET change_id=?, updated_at=? WHERE id=?",
                 (cid, db.now(), pid))
    if not c["project"]:
        _touch(conn, cid, project=item["project"] or "")
    if c["state"] == "rejected":
        _touch(conn, cid, state="draft")
    _touch(conn, cid)
    event(conn, "change.member", cid, actor=actor, item_id=p["item_id"],
          data={"proposal": pid, "item": item["number"], "origin": origin,
                "version": p["version"]})
    if c["state"] == "open" and p["state"] != "open":
        # a change in review grew a member nobody has submitted: it is not one
        # reviewable thing any more until it is submitted again
        _touch(conn, cid, state="draft")
        event(conn, "change.reopened", cid, actor=actor,
              data={"why": f"{proposals.label(pid)} joined while the pull request was in review"})
    recheck(conn, cid, skip=pid)
    return brief(conn, row(conn, cid))


#: How long a submit waits, by default, for the members' checks to finish
#: before it answers (stage 16, finding 5). Bounded: a `minutes` render on a
#: big assembly must not hold a tool call forever — what is still running is
#: REPORTED instead, by name.
SUBMIT_WAIT_S = float(os.environ.get("PLM_SUBMIT_WAIT_S", "90"))


def checks_pending(conn, mem: list[dict]) -> dict[str, list[str]]:
    """{item: [check names still queued or running]} over the members' heads;
    a head nobody has measured yet reads ["(not measured yet)"]."""
    from . import checks
    out: dict[str, list[str]] = {}
    for m in mem:
        if not m.get("head_sha") or m["state"] in ("withdrawn", "merged"):
            continue
        rows = checks.read(conn, m["item_number"], m["head_sha"])
        busy = [r["name"] for r in rows if r["status"] in ("queued", "running")]
        if not rows and (checks.ENABLED or checks.RELAY):
            busy = ["(not measured yet)"]
        if busy:
            out[m["item_number"]] = busy
    return out


def settle(cid, *, wait: float | None = None) -> dict:
    """Before a submit answers: re-measure `shared-source` on every member head
    (the membership is final now) and wait — bounded — until nothing on any
    member head is queued or running. OWN connections, and never inside the
    submit's write transaction: the runner has to be able to write its rows.

    Run 3 tried `sleep 60` and then polled the checks by hand; the submit
    answer it eventually got carried a verdict measured before two of its
    three members existed. Returns {waited_s, pending} — what is still
    running when the bound is hit, for the submit answer to name."""
    from . import checks
    wait = SUBMIT_WAIT_S if wait is None else max(0.0, min(float(wait), 300.0))
    t0 = time.monotonic()
    try:
        with db.connect() as conn:
            c = row(conn, cid)
            mem = members(conn, c["id"])
            for m in mem:
                if not m.get("head_sha") or m["state"] in ("withdrawn", "merged"):
                    continue
                # mark it queued NOW, so the wait below cannot read "nothing
                # pending" in the instant before the runner picks it up
                conn.execute(
                    "UPDATE checks SET status='queued', started_at=NULL "
                    "WHERE number=? AND sha=? AND name='shared-source' "
                    "AND status NOT IN ('queued','running')",
                    (m["item_number"], m["head_sha"]))
                checks.enqueue(m["item_number"], m["head_sha"],
                               names=["shared-source"], item_id=m["item_id"],
                               conn=conn if checks.RELAY else None)
            conn.commit()
            pend = checks_pending(conn, mem)
        while pend and time.monotonic() - t0 < wait:
            time.sleep(0.5)
            with db.connect() as conn:
                pend = checks_pending(conn, mem)
    except Exception as exc:                                   # noqa: BLE001
        return {"waited_s": round(time.monotonic() - t0, 1), "pending": {},
                "error": str(exc)}
    return {"waited_s": round(time.monotonic() - t0, 1), "pending": pend}


def recheck(conn, cid: int, *, skip: int | None = None) -> list[str]:
    """The pull request's head set moved (a member joined or was re-pushed, a
    disposition changed what a check reads): re-measure what depends on the
    OTHER members. `shared-source` on every other member's head (stage 16), and
    the pull request's own `physical-diff` (§2.10) once it has been submitted —
    a key-less request, so an unchanged head set is not rendered again.

    That check compares a member with the change's own heads, so its verdict
    on member A is stale the moment member B joins or is re-pushed: run 3's
    base was measured before the lid and the assembly existed in the change,
    and its "DRIFT — differs from 2 other copies" then rode into the submit
    answer. Never raises; `checks.enqueue` never blocks (and, in the MCP's
    relay process, rides in the caller's transaction)."""
    from . import checks
    done = []
    try:
        for m in members(conn, cid):
            if m["id"] == skip or not m.get("head_sha") \
                    or m["state"] in ("withdrawn", "merged"):
                continue
            checks.enqueue(m["item_number"], m["head_sha"],
                           names=["shared-source"], item_id=m["item_id"],
                           conn=conn if checks.RELAY else None)
            done.append(m["item_number"])
        if row(conn, cid)["submitted_at"]:
            enqueue_change_checks(conn, cid)
    except Exception:                                          # noqa: BLE001
        pass
    return done


def enqueue_change_checks(conn, cid: int) -> None:
    """Ask for the pull request's own checks (`applies: change`), key-less:
    they run only if the current head set has no finished row (the hooks'
    idempotence — `checks._change_job`)."""
    from . import checks
    checks.enqueue(label(cid), "", conn=conn if checks.RELAY else None)


def own_checks(conn, c: dict, *, heads: list[dict] | None = None,
               regenerate: bool = False) -> dict:
    """The pull request's OWN check board (§2.10) — the rows filed under its
    current head-set key, the shape `GET /api/repo/<CHG-n>/checks` answers.
    `heads`: the head set when the caller already has it (`view` does).
    `regenerate`: a submitted, undecided pull request with no row asks for one
    (the cache or the queue lost it)."""
    from . import checks
    subject = label(c["id"])
    key = checks.set_key(head_set(conn, c["id"]) if heads is None else heads)
    rows = checks.read(conn, subject, key)
    if regenerate and not rows and c["submitted_at"] and c["state"] not in CLOSED:
        enqueue_change_checks(conn, c["id"])
    return {"subject": subject, "sha": key, "checks": rows,
            "rollup": checks.rollup(rows, applicable=checks.applies_to(subject, key))}


def head_entry(m: dict, rb: dict) -> dict:
    """One member's entry in the head set: item, head, REVIEW BASELINE (what
    Files changed diffs against — `proposals.baseline`), version."""
    return {"item": m["item_number"], "head": m["head_sha"] or "",
            "base": rb.get("sha") or "", "version": m["version"]}


def head_set(conn, cid, mem: list[dict] | None = None) -> list[dict]:
    """What the pull request's checks are keyed by: `head_entry` per member."""
    mem = members(conn, row(conn, cid)["id"]) if mem is None else mem
    return [head_entry(m, proposals.baseline(m, m["item_number"])) for m in mem]


def attach_push(conn, number: str, pid: int, spec: str, *, title: str = "",
                actor: str) -> dict:
    """`git push -o change=new -o title="…"` / `-o change=CHG-n` — the agent's
    native door. Returns the change brief (what the `remote:` output prints)."""
    from . import proposals
    spec = (spec or "").strip()
    if not spec:
        return {}
    p = proposals._row(conn, pid)
    if spec.lower() == "new":
        if p and p["change_id"]:
            cur = row(conn, p["change_id"])
            if not cur["auto"]:
                return brief(conn, cur)       # re-push of the first push: same
        item = db.resolve_item(conn, number)
        c = create(conn, title=title or (p or {}).get("title") or number,
                   project=(item["project"] if item else "") or "", actor=actor,
                   origin="push")
        return add_member(conn, c["id"], pid, actor=actor, origin="push")
    return add_member(conn, spec, pid, actor=actor, origin="push")


# -- the hooks proposals.py calls ------------------------------------------------

def on_submitted(conn, p: dict, item, *, actor: str, round_id: int,
                 notify: bool = True) -> tuple[dict, list]:
    """A member's round opened. EVERY REVIEW IS A CHANGE: wrap a loose proposal
    in a single-member change. When every member is in review the change OPENS
    and raises ONE `review.requested` (subject: change). Returns (brief,
    events)."""
    ensure(conn)
    if not p.get("change_id"):
        c = create(conn, title=p.get("title") or f"{item['number']} "
                   f"v{p.get('version') or ''}", body=p.get("body_md") or "",
                   project=item["project"] or "", actor=actor, origin="auto",
                   auto=True)
        conn.execute("UPDATE proposals SET change_id=? WHERE id=?",
                     (c["id"], p["id"]))
        event(conn, "change.opened", c["id"], actor=actor, item_id=item["id"],
              data={"auto": True, "proposal": p["id"]})
        p = dict(p, change_id=c["id"])
    cid = p["change_id"]
    c = row(conn, cid)
    mem = members(conn, cid)
    waiting = [m for m in mem if m["state"] != "open" and m["state"] not in
               ("merged", "approved", "withdrawn")]
    if waiting:
        return brief(conn, c, extra={
            "note": f"{title_ref(cid)} has {len(waiting)} member(s) not yet in "
                    "review (" + ", ".join(f"{proposals.label(m['id'])} {m['item_number']}"
                                          for m in waiting)
                    + "); it opens for review when every member is submitted "
                    "— submit_change submits them all"}), []
    resubmit = c["submitted_at"] is not None
    _touch(conn, cid, state="open", submitted_at=db.now(), merge_state=(
        c["merge_state"] if c["merge_state"] == "partial" else ""))
    c = row(conn, cid)
    event(conn, "change.submitted", cid, actor=actor,
          data={"proposals": [m["id"] for m in mem], "round": round_id,
                "resubmit": resubmit})
    enqueue_change_checks(conn, cid)
    if not notify:
        return brief(conn, c), []
    return brief(conn, c), [attention(conn, c, mem, reason="resubmitted"
                                      if resubmit else "submitted",
                                      actor=actor)]


def on_head_moved(conn, p: dict, sha: str, prev: str, *, actor: str) -> dict:
    """A member's head moved: every change-level signoff that looked at the old
    head stops speaking for the bytes (dismissed, visibly), and a rejected
    change comes back as a draft — the same object, carrying on."""
    ensure(conn)
    cid = p.get("change_id")
    if not cid or not prev or prev == sha:
        return {}
    out = {"dismissed": dismiss_stale(conn, cid, p["id"], sha, actor=actor)}
    recheck(conn, cid, skip=p["id"])
    c = row(conn, cid)
    if c["state"] == "rejected":
        _touch(conn, cid, state="draft")
        event(conn, "change.reopened", cid, actor=actor, item_id=p["item_id"],
              data={"proposal": p["id"], "from": prev, "to": sha,
                    "why": "a member was re-pushed after the rejection"})
        out["reopened"] = True
    return out


def on_round_withdrawn(conn, p: dict, *, actor: str, why: str) -> None:
    cid = p.get("change_id")
    if not cid:
        return
    c = row(conn, cid)
    if c["state"] == "open":
        _touch(conn, cid, state="draft")
        event(conn, "change.withdrawn_from_review", cid, actor=actor,
              item_id=p["item_id"], data={"proposal": p["id"], "why": why})


def follow(conn, cid: int) -> dict:
    """Recompute a change's state from its members — the single-member door
    (a proposal decided directly) and the end of every decide_change."""
    c = row(conn, cid)
    st = [m["state"] for m in members(conn, cid)]
    if not st:
        return c
    new = c["state"]
    if all(s in ("merged", "approved") for s in st):
        new = "merged" if any(s == "merged" for s in st) else "approved"
    elif all(s == "withdrawn" for s in st):
        new = "withdrawn"
    elif any(s == "rejected" for s in st) and not any(s == "open" for s in st):
        new = "rejected"
    if new != c["state"]:
        last = conn.execute(
            "SELECT reviewer, kind, body_md FROM reviews WHERE proposal_id IN "
            "(SELECT id FROM proposals WHERE change_id=?) AND counts=1 "
            "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        fields = {"state": new}
        if new in ("merged", "approved", "rejected") and last:
            fields.update(decided_at=db.now(), decided_by=last["reviewer"],
                          decided_kind=last["kind"] or "",
                          decision_note=last["body_md"] or "")
        if new in ("merged", "approved"):
            fields["merge_state"] = "done"
        _touch(conn, cid, **fields)
        event(conn, f"change.{new}", cid, actor=fields.get("decided_by", ""),
              data={"followed": True})
    return row(conn, cid)


# -- change-level reviews -------------------------------------------------------

def heads_of(mem: list[dict]) -> dict:
    return {str(m["id"]): m["head_sha"] for m in mem}


def add_review(conn, cid: int, *, actor: str, verdict: str, body: str = "",
               heads: dict | None = None, counts: bool = False,
               identity: str = "", kind: str = "") -> dict:
    ensure(conn)
    c = row(conn, cid)
    mem = members(conn, c["id"])
    h = {str(k): v for k, v in (heads or heads_of(mem)).items()}
    k = kind or (db.get_user(conn, actor) or {}).get("kind") or ""
    cur = conn.execute(
        "INSERT INTO change_reviews (change_id, reviewer, kind, verdict, heads, "
        "body_md, counts, identity, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (c["id"], actor, k, verdict, json.dumps(h, sort_keys=True), body,
         int(bool(counts)), identity, db.now()))
    event(conn, "change.review", c["id"], actor=actor,
          data={"verdict": verdict, "counts": bool(counts), "kind": k,
                "heads": h, "body": (body or "")[:400]})
    return {"id": cur.lastrowid, "verdict": verdict, "heads": h,
            "counts": bool(counts), "kind": k}


def dismiss_stale(conn, cid: int, pid: int, sha: str, *, actor: str = "") -> list[int]:
    """Forge "dismiss stale reviews", for a change: a signoff bound to a set of
    heads stops speaking for the change the moment ANY of those heads moves."""
    out = []
    for r in conn.execute(
            "SELECT id, heads FROM change_reviews WHERE change_id=? AND "
            "dismissed_at IS NULL AND verdict IN ('approve','reject')", (cid,)):
        h = json.loads(r["heads"] or "{}")
        if str(pid) in h and h[str(pid)] and h[str(pid)] != sha:
            out.append(r["id"])
    if out:
        conn.execute(
            f"UPDATE change_reviews SET dismissed_at=?, dismissed_by=? WHERE id IN "
            f"({','.join('?' * len(out))})", (db.now(), f"{proposals.label(pid)}@{sha}", *out))
        event(conn, "change.review.dismissed", cid, actor=actor,
              data={"reviews": out, "proposal": pid, "head": sha,
                    "why": f"proposal {proposals.label(pid)} moved to {sha[:10]} after the "
                           "signoff — it no longer speaks for these bytes"})
    return out


# -- the attention seam ----------------------------------------------------------

def attention(conn, c: dict, mem: list[dict], *, reason: str, actor: str = "",
              headline: str = "") -> tuple:
    """`review.requested`, subject CHANGE, as the `(type, payload)` tuple the
    HTTP door hands to `actions.dispatch` (dry-run in the POC). Stage 13's
    proposal-level event had the same shape with `subject.type = proposal`, so
    a subscriber written then reads this one. The event row carries
    `proposal` too when the change has a single member (so the stage-13
    readers of `$.proposal` keep finding it)."""
    from . import threads
    sc = [("change", c["id"])] + [("proposal", m["id"]) for m in mem]
    tc = threads.counts(conn, sc)
    open_ = sum(v["open"] for v in tc.values())
    blocking = sum(v["blocking"] for v in tc.values())
    pins = sum(1 for m in mem for _ in (plan_repins(m, mem) or {}))
    n = len(mem)
    head = headline or (
        f"{title_ref(c['id'])} needs your approval — {c['title']} · "
        f"{n} item{'s' if n != 1 else ''}"
        + (f", {pins} pin bump{'s' if pins != 1 else ''}" if pins else "")
        + (f" · {open_} open thread{'s' if open_ != 1 else ''}" if open_ else ""))
    payload = {"subject": {"type": "change", "id": c["id"], "number": label(c["id"])},
               "reason": reason, "title": c["title"], "project": c["project"],
               "items": [f"{m['item_number']} v{m['version']}" for m in mem],
               "proposals": [m["id"] for m in mem],
               "heads": heads_of(mem), "actor": actor, "url": url(c["id"]),
               "threads": {"open": open_, "blocking": blocking},
               "checks": _check_lines(conn, c, mem),
               "headline": head}
    data = {k: v for k, v in payload.items() if k not in ("subject", "url")}
    if n == 1:
        data["proposal"] = mem[0]["id"]
    db.add_event(conn, "review.requested", item_id=_home_item(conn, c["id"]),
                 actor=actor, data={"change": c["id"], **data})
    return ("review.requested", payload)


def _check_lines(conn, c: dict, mem: list[dict]) -> list[dict]:
    """The pull request's own check rows as a notification carries them — the
    first image as an absolute URL (GitHub's `output.images[0].image_url`)."""
    try:
        rows = own_checks(conn, c, heads=head_set(conn, c["id"], mem))["checks"]
    except Exception:                                          # noqa: BLE001
        return []
    return [{"name": r["name"], "status": r["status"], "summary": r["summary"],
             "image_url": (config.PUBLIC_URL + r["images"][0]["image_url"])
             if r["images"] else ""} for r in rows]


def plan_repins(m: dict, mem: list[dict]) -> dict:
    """{path: child member} for a member's gitlinks that point at another
    member's HEAD — the pins plm re-points at the child's release commit."""
    from . import gitvalidate, gitwrite
    from .gitindex import MODE_GITLINK
    if not m.get("head_sha"):
        return {}
    heads = {x["item_number"]: x for x in mem if x["id"] != m["id"]}
    try:
        ents = gitvalidate.tree_entries(gitwrite.repo_path(m["item_number"]),
                                        m["head_sha"])
    except Exception:                                          # noqa: BLE001
        return {}
    out = {}
    for path, e in ents.items():
        # parts/<N> (a BOM child) and lib/<N> (a shared source, stage 15) are
        # both pins plm re-points at the child's release commit at merge
        if e.mode != MODE_GITLINK or not (path.startswith("parts/")
                                          or path.startswith("lib/")):
            continue
        child = heads.get(path.split("/", 1)[1])
        if child and child["head_sha"] == e.sha:
            out[path] = child
    return out


# -- the plan: dependency order + re-pins + stale pins ------------------------------

def plan(conn, cid: int, mem: list[dict] | None = None) -> dict:
    """Topological order over the change's OWN gitlinks (and its coupons'
    commit-pinned claims), children first.

    For each member: `repin` — gitlinks that point at another member's head
    (re-pointed at that member's release commit when the parent merges);
    `after` — the members it must follow; `problems` — a gitlink or claim that
    points at a member's OLDER draft commit (the parent would ship with a child
    nobody reviewed it against)."""
    from . import gitvalidate, gitwrite, safegit
    from .gitindex import MODE_GITLINK
    mem = mem if mem is not None else members(conn, cid)
    by_num = {m["item_number"]: m for m in mem}
    deps: dict[int, set] = {m["id"]: set() for m in mem}
    info: dict[int, dict] = {m["id"]: {"repin": {}, "claims": [], "problems": []}
                             for m in mem}
    for m in mem:
        if not m["head_sha"]:
            continue
        repo = gitwrite.repo_path(m["item_number"])
        try:
            ents = gitvalidate.tree_entries(repo, m["head_sha"])
            doc, _ = gitvalidate._manifest_of(repo, m["head_sha"])
        except Exception:                                      # noqa: BLE001
            continue
        pins = []
        for path, e in ents.items():
            if e.mode == MODE_GITLINK and (path.startswith("parts/")
                                           or path.startswith("lib/")):
                pins.append(("pin", path, path.split("/", 1)[1], e.sha))
        for r in (doc or {}).get("relations") or []:
            if r.get("commit") and r.get("item"):
                pins.append(("claim", f"[[relations]] {r.get('type', '')}",
                             r["item"], r["commit"]))
        for what, path, child, sha in pins:
            cm = by_num.get(child)
            if not cm or cm["id"] == m["id"] or not cm["head_sha"]:
                continue
            if sha == cm["head_sha"]:
                deps[m["id"]].add(cm["id"])
                if what == "pin":
                    info[m["id"]]["repin"][path] = {
                        "child": child, "proposal": cm["id"], "from": sha}
                else:
                    info[m["id"]]["claims"].append(
                        {"child": child, "proposal": cm["id"], "commit": sha,
                         "what": path})
                continue
            crepo = gitwrite.repo_path(child)
            if safegit.ok(crepo, "merge-base", "--is-ancestor", sha,
                          cm["head_sha"]):
                info[m["id"]]["problems"].append({
                    "why": f"{path} pins {child} at {sha[:10]}, an OLDER commit "
                           f"of proposal {proposals.label(cm['id'])}'s draft (its head is "
                           f"{cm['head_sha'][:10]}). Merging would ship "
                           f"{m['item_number']} against a {child} nobody "
                           "reviewed it with.",
                    "fix": (f"bump the pin to the head and re-push "
                            f"{m['item_number']}:\n"
                            f"    git -C {path} fetch origin && git -C {path} "
                            f"checkout {cm['head_sha'][:12]}\n"
                            f"    git add {path} && git commit -m 'pin {child} "
                            f"at its draft head' && git push")
                    if what == "pin" else
                    f"point the claim at {cm['head_sha'][:12]} and re-push"})
    order, seen, temp = [], set(), set()
    ids = {m["id"]: m for m in mem}

    def visit(i):
        if i in seen:
            return
        if i in temp:                    # impossible with commit hashes; be safe
            return
        temp.add(i)
        for d in sorted(deps[i], key=lambda x: ids[x]["item_number"]):
            visit(d)
        temp.discard(i)
        seen.add(i)
        order.append(ids[i])
    for m in sorted(mem, key=lambda x: x["item_number"]):
        visit(m["id"])
    return {"order": order, "info": info,
            "deps": {k: sorted(v) for k, v in deps.items()}}


# -- impact analysis -------------------------------------------------------------

def impact(conn, cid: int, mem: list[dict] | None = None) -> list[dict]:
    """Who pins the changed items, and is that parent in the change?

    From the gitlink graph: candidates come from the registry's where-used
    index (`bom`), and each is confirmed against the parent's `main` in git —
    the pin a parent ships with NOW. A parent that is itself a member is listed
    as "bumped in this change"; any other needs a disposition."""
    from . import gitindex, gitvalidate, gitwrite
    mem = mem if mem is not None else members(conn, cid)
    member_items = {m["item_id"]: m for m in mem}
    disp = {r["parent_item_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM change_impacts WHERE change_id=?", (cid,))}
    parents: dict[int, dict] = {}
    for m in mem:
        child_id, child = m["item_id"], m["item_number"]
        cands = [(dict(p), "parts") for p in conn.execute(
            "SELECT DISTINCT i.id, i.number, i.name, i.project FROM bom b "
            "JOIN revisions r ON r.id=b.parent_rev_id JOIN items i "
            "ON i.id=r.item_id WHERE b.child_item_id=?", (child_id,))]
        # stage 15: consumers of a shared SOURCE pin it at `lib/<N>` — the
        # same impact, through the source-pin index (plm/sources.py)
        if _has_table(conn, "source_pins"):
            from . import sources
            cands += [(p, "lib") for p in sources.consumers(conn, child_id)]
        for p, under in cands:
            if p["id"] == child_id:
                continue
            pin = _main_pin(p["number"], child, under)
            if pin is None:
                continue                      # not pinned by what ships now
            ent = parents.setdefault(p["id"], {
                "parent": p["number"], "parent_item_id": p["id"],
                "name": p["name"], "project": p["project"],
                "parent_version": pin.get("parent_version", ""),
                "in_change": p["id"] in member_items, "pins": []})
            ent["pins"].append({"child": child, "proposal": m["id"],
                                "path": pin["path"], "commit": pin["commit"],
                                "version": pin["version"],
                                "on_a_tag": pin["released"],
                                "child_new_version": m["version"]})
    # stage 15: items that carry a source file this change MODIFIED, at the
    # same path with the pre-change blob — affected exactly like a parent
    # pinning a changed child (plm/sources.py, layer 1)
    from . import sources
    try:
        shared = sources.sharers(conn, mem)
    except Exception:                                          # noqa: BLE001
        shared = {}
    for iid, sh in shared.items():
        it = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        ent = parents.get(iid)
        if ent is None:
            ver = ""
            try:
                from . import gitvalidate, gitwrite
                r_ = gitwrite.repo_path(sh["number"])
                d_, _ = gitvalidate._manifest_of(
                    r_, "refs/heads/" + sh["shares"][0]["ref"]
                    if not sh["shares"][0]["ref"].startswith("refs/")
                    else sh["shares"][0]["ref"])
                ver = str((d_ or {}).get("version") or "")
            except Exception:                                  # noqa: BLE001
                pass
            ent = parents.setdefault(iid, {
                "parent": sh["number"], "parent_item_id": iid,
                "name": sh["name"], "project": it["project"] if it else "",
                "parent_version": ver,
                "in_change": iid in member_items, "pins": []})
        ent["shares"] = sh["shares"]
    # stage 19 (handoff finding 4): items of the project that carry a
    # DIFFERENT copy of a source a member's head carries — the `shared-source`
    # check's DRIFT, as rows a reviewer can disposition (`historical`,
    # `unchanged_geometry`, …). ADVISORY: listed and recorded, never a gate.
    try:
        for iid, d in drift_carriers(conn, mem).items():
            if iid in member_items or iid in parents:
                continue
            parents[iid] = {"parent": d["number"], "parent_item_id": iid,
                            "name": d["name"], "project": d["project"],
                            "parent_version": d.get("version", ""),
                            "in_change": False, "pins": [],
                            "drift": d["drift"], "advisory": True}
    except Exception:                                          # noqa: BLE001
        pass
    out = []
    for pid_, ent in sorted(parents.items(), key=lambda kv: kv[1]["parent"]):
        shares = ent.get("shares") or []
        share_txt = "; ".join(
            f"carries `{x['path']}` at the pre-change blob {x['blob'][:10]} "
            f"(changed by {x['member']})" for x in shares)
        if ent["in_change"]:
            pm = member_items[pid_]
            ent["member"] = {"proposal": pm["id"], "version": pm["version"]}
            ent["disposition"] = "bump"
            ent["disposition_label"] = "bumped in this pull request"
            ent["confirmed"] = True
            ent["sentence"] = (
                (f"{ent['parent']} pins "
                 + ", ".join(x["child"] for x in ent["pins"])
                 + f"; this pull request bumps it ({proposals.label(pm['id'])} v{pm['version']})."
                 if ent["pins"] else f"{ent['parent']} is in this pull request")
                + (f" ⚠ but its head still {share_txt} — re-vendor it"
                   if shares else ""))
        else:
            d = disp.get(pid_) or {}
            ent.update({
                "disposition": d.get("disposition", ""),
                "disposition_label": DISPOSITIONS.get(d.get("disposition", ""),
                                                      "needs a disposition"),
                "note": d.get("note", ""), "drafted_by": d.get("drafted_by", ""),
                "drafted_kind": d.get("drafted_kind", ""),
                "confirmed_by": d.get("confirmed_by", ""),
                "confirmed": bool(d.get("confirmed_by")),
                "track": any(_tracks(ent["parent"], x["path"])
                             for x in ent["pins"])})
            if shares and ent["disposition"] == "bump":
                ent["disposition_label"] = "re-vendor in this pull request"
            drift_txt = "; ".join(
                f"carries a different `{x['path']}` ({x['blob'][:10]} on "
                f"{x['ref']}) than {x['member']}'s head ({x['member_blob'][:10]})"
                for x in ent.get("drift") or [])
            ent["sentence"] = "; ".join(filter(None, [
                (f"{ent['parent']} pins "
                 + ", ".join(f"{x['child']} at v{x['version']}" if x["on_a_tag"]
                             else f"{x['child']} at {x['commit'][:10]}"
                             for x in ent["pins"])
                 + "; this pull request does not bump it") if ent["pins"] else "",
                (f"{ent['parent']} {share_txt} — not in this pull request"
                 if shares else ""),
                (f"{ent['parent']} {drift_txt} — drift, not in this pull request "
                 "(advisory: `historical` or `unchanged_geometry` say why it "
                 "may stay)" if drift_txt else "")])) + "."
        ent["kind"] = ("pin+shared-source" if ent["pins"] and shares else
                       "shared-source" if shares else
                       "source-drift" if ent.get("drift") else "pin")
        out.append(ent)
    return out


def drift_carriers(conn, mem: list[dict]) -> dict[int, dict]:
    """item_id -> {number, name, project, version, drift: [{path, blob, ref,
    member, member_blob}]} — every non-member item of a member's project that
    carries one of the member HEAD's source files at a DIFFERENT blob (what
    the `shared-source` check calls DRIFT), compared on what that item ships
    (`main`, else its newest draft). Members' own copies are compared at
    their change head by the check itself."""
    from . import gitvalidate, gitwrite, sources
    member_ids = {m["item_id"] for m in mem}
    out: dict[int, dict] = {}
    for m in mem:
        if not m.get("head_sha") or m.get("state") in ("withdrawn", "merged"):
            continue
        try:
            repo = gitwrite.repo_path(m["item_number"])
            entries = gitvalidate.tree_entries(repo, m["head_sha"])
            doc, _ = gitvalidate._manifest_of(repo, m["head_sha"])
        except Exception:                                      # noqa: BLE001
            continue
        paths = sources.source_paths(doc or {}, entries)
        copies = sources.project_copies(conn, m["item_project"] or "", paths,
                                        exclude=member_ids)
        for p in paths:
            mine = entries[p].sha
            for r in copies.get(p) or []:
                if r["blob"] == mine:
                    continue
                it = conn.execute("SELECT project FROM items WHERE id=?",
                                  (r["item_id"],)).fetchone()
                ent = out.setdefault(r["item_id"], {
                    "number": r["number"], "name": r["name"],
                    "project": it["project"] if it else "", "drift": []})
                if any(x["path"] == p and x["blob"] == r["blob"]
                       for x in ent["drift"]):
                    continue
                ent["drift"].append({"path": p, "blob": r["blob"],
                                     "ref": r["ref"], "member": m["item_number"],
                                     "member_blob": mine})
    return out


def coverage(conn, cid: int, mem: list[dict] | None = None) -> dict:
    """WHAT THE IMPACT ANALYSIS LOOKED AT (stage 19, handoff finding 1).

    `impact()` lists parents whose `main` pins a changed item. An EMPTY list
    used to be indistinguishable from "nothing was checked": a parent with no
    `main` (never released — every CID-* item) was skipped in silence, so
    Codex could not tell from CHG-379 whether a historical DRAFT parent was
    affected. This says, per member, every where-used candidate and how it was
    covered: `main` (its shipping pin was compared — listed in `impact` when
    it pins the member), `in-change` (it is a member), `draft-only` (no main:
    its open draft branches' pins are listed here, not dispositioned — a draft
    ships nothing until it is itself proposed), or `not-pinned` (where-used
    says so, but no ref of the parent pins it any more)."""
    from . import gitindex, gitvalidate, gitwrite
    mem = mem if mem is not None else members(conn, cid)
    member_items = {m["item_id"]: m for m in mem}
    rows, lines = [], []
    for m in mem:
        child_id, child = m["item_id"], m["item_number"]
        cands = [(dict(p), "parts") for p in conn.execute(
            "SELECT DISTINCT i.id, i.number FROM bom b "
            "JOIN revisions r ON r.id=b.parent_rev_id JOIN items i "
            "ON i.id=r.item_id WHERE b.child_item_id=?", (child_id,))]
        if _has_table(conn, "source_pins"):
            from . import sources
            cands += [(p, "lib") for p in sources.consumers(conn, child_id)]
        seen = set()
        for p, under in cands:
            if p["id"] == child_id or (p["id"], under) in seen:
                continue
            seen.add((p["id"], under))
            ent = {"member": child, "parent": p["number"], "via": under}
            if p["id"] in member_items:
                ent.update(covered="in-change",
                           note=f"a member ({proposals.label(member_items[p['id']]['id'])})")
            elif _main_pin(p["number"], child, under) is not None:
                ent.update(covered="main", note="its main pins this item — "
                           "see impact for the disposition")
            else:
                try:
                    repo = gitwrite.repo_path(p["number"])
                    has_main = bool(gitwrite.tip(repo, "refs/heads/main"))
                    drafts = []
                    for dref, tip in gitvalidate.draft_branches(repo).items():
                        e = gitvalidate.tree_entries(repo, tip).get(
                            f"{under}/{child}")
                        if e and e.mode == gitindex.MODE_GITLINK:
                            drafts.append({"ref": dref[len("refs/heads/"):],
                                           "commit": e.sha})
                except Exception:                              # noqa: BLE001
                    has_main, drafts = False, []
                if not has_main:
                    ent.update(covered="draft-only", drafts=drafts,
                               note="no main (never released): "
                               + (", ".join(f"{d['ref']} pins {d['commit'][:10]}"
                                            for d in drafts) or "no draft pins it")
                               + " — drafts ship nothing; not dispositioned")
                else:
                    ent.update(covered="not-pinned", note="where-used lists it, "
                               "but main no longer pins it")
            rows.append(ent)
        if not cands:
            lines.append(f"{child}: no where-used parents")
    by = {}
    for r in rows:
        by.setdefault(r["covered"], []).append(f"{r['parent']}←{r['member']}")
    say = "; ".join(f"{k}: {', '.join(v)}" for k, v in by.items()) or \
        "no member has a where-used parent"
    return {"checked": rows, "roots": lines, "say": "impact covered — " + say,
            "rule": "a parent is affected when its MAIN pins a changed item "
                    "(or carries a changed source at the pre-change blob); "
                    "draft-only parents are listed, not dispositioned"}


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name=?", (name,)).fetchone())


def _main_pin(parent: str, child: str, under: str = "parts") -> dict | None:
    from . import gitindex, gitvalidate, gitwrite
    try:
        repo = gitwrite.repo_path(parent)
    except Exception:                                          # noqa: BLE001
        return None
    if not (repo / "HEAD").exists():
        return None
    tip = gitwrite.tip(repo, "refs/heads/main")
    if not tip:
        return None
    path = f"{under}/{child}"
    try:
        e = gitvalidate.tree_entries(repo, tip).get(path)
    except Exception:                                          # noqa: BLE001
        return None
    if not e or e.mode != gitindex.MODE_GITLINK:
        return None
    lab = gitindex.pin_label(child, e.sha)
    doc, _ = gitvalidate._manifest_of(repo, tip)
    return {"path": path, "commit": e.sha, "version": lab["version"],
            "released": lab["released"],
            "parent_version": str((doc or {}).get("version") or "")}


def _tracks(parent: str, path: str) -> bool:
    from . import gitvalidate, gitwrite
    try:
        repo = gitwrite.repo_path(parent)
        doc, _ = gitvalidate._manifest_of(repo, gitwrite.tip(repo, "refs/heads/main"))
    except Exception:                                          # noqa: BLE001
        return False
    return any(b.get("path") == path and b.get("track") == "release"
               for b in (doc or {}).get("bom") or [])


def set_disposition(conn, cid, parent: str, disposition: str, *, note: str = "",
                    actor: str) -> dict:
    """Record a disposition on an affected parent. A HUMAN's is confirmed at
    once; a BOT's is a draft until a human confirms it (same call, same value,
    by a human). Not a gate at PROTOTYPE; required for a RELESED target."""
    c = row(conn, cid)
    if disposition not in DISPOSITIONS:
        raise ChangeError("disposition is one of " + ", ".join(
            f"`{k}` ({v})" for k, v in DISPOSITIONS.items()), 400)
    user = db.get_user(conn, actor)
    if not user:
        raise ChangeError(f"unknown user: {actor}", 403)
    item = db.resolve_item(conn, parent)
    if not item:
        raise ChangeError(f"unknown item: {parent}", 404)
    affected = {x["parent_item_id"]: x for x in impact(conn, c["id"])}
    if item["id"] not in affected:
        raise ChangeError(
            f"{item['number']} is not affected by pull request {ref(c['id'])} — impact "
            "analysis finds no parent pinning a changed item there.\nAffected: "
            + (", ".join(x["parent"] for x in affected.values()) or "none"), 409)
    if affected[item["id"]]["in_change"]:
        raise ChangeError(f"{item['number']} is a member of pull request {ref(c['id'])}; "
                          "its disposition is the bump itself", 409)
    if disposition in SOURCE_DISPOSITIONS and not (
            affected[item["id"]].get("shares") or affected[item["id"]].get("drift")):
        raise ChangeError(
            f"`{disposition}` is a disposition for an item that carries a "
            f"different copy of a SOURCE file; {item['number']} is affected "
            "through a pin — use bump / use_as_is / no_impact", 400)
    if not (note or "").strip() and disposition != "bump":
        raise ChangeError("say why — a disposition without a reason is the "
                          "decision nobody can review later", 400)
    human = user["kind"] == "human"
    old = conn.execute("SELECT * FROM change_impacts WHERE change_id=? AND "
                       "parent_item_id=?", (c["id"], item["id"])).fetchone()
    now = db.now()
    if old:
        same = old["disposition"] == disposition
        conn.execute(
            "UPDATE change_impacts SET disposition=?, note=?, updated_at=?, "
            "drafted_by=CASE WHEN ? THEN drafted_by ELSE ? END, "
            "drafted_kind=CASE WHEN ? THEN drafted_kind ELSE ? END, "
            "confirmed_by=?, confirmed_at=? WHERE id=?",
            (disposition, note or old["note"], now, int(same and human),
             actor, int(same and human), user["kind"],
             actor if human else "", now if human else None, old["id"]))
    else:
        conn.execute(
            "INSERT INTO change_impacts (change_id, parent_item_id, disposition, "
            "note, drafted_by, drafted_kind, confirmed_by, confirmed_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (c["id"], item["id"], disposition, note, actor, user["kind"],
             actor if human else "", now if human else None, now))
    event(conn, "change.disposition", c["id"], actor=actor,
          data={"parent": item["number"], "disposition": disposition,
                "note": note, "kind": user["kind"], "confirmed": human})
    if affected[item["id"]].get("drift") or affected[item["id"]].get("shares"):
        # the members' `shared-source` rows read the change's dispositions
        # (stage 19): measure them again so the board says what was decided
        try:
            recheck(conn, c["id"])
        except Exception:                                      # noqa: BLE001
            pass
    return next(x for x in impact(conn, c["id"])
                if x["parent_item_id"] == item["id"])


def draft_dispositions(conn, cid, *, actor: str, render: bool = False
                       ) -> list[dict]:
    """The agent's legwork (PLM_CHANGES.md "AI-friendly part"): for every
    affected parent with no disposition, propose one from the facts and SAY
    WHY. A bot's drafts are drafts — a human confirms them.

    Stage 19: a `source-drift` row (an item carrying a DIFFERENT copy of a
    member's source) is advisory. With `render`, its own selector is rendered
    against its copy and against the member's (`sources.selector_compare`):
    identical geometry drafts `unchanged_geometry` with the measurements as
    the note; anything else is left for a human (`historical`, or re-vendor),
    and says why."""
    c = row(conn, cid)
    out = []
    mem = members(conn, c["id"])
    heads = {m["item_number"]: m["head_sha"] for m in mem}
    for x in impact(conn, c["id"], mem):
        if x["in_change"] or x["disposition"]:
            continue
        if x.get("advisory") and x.get("drift"):
            if not render:
                out.append({"parent": x["parent"], "disposition": "",
                            "note": "source drift — advisory; `historical` or "
                                    "`unchanged_geometry` is a reviewer's call "
                                    "(draft_dispositions(render=true) compares "
                                    "the selected geometry)"})
                continue
            from . import sources
            res = []
            for d in x["drift"]:
                ref = d["ref"] if d["ref"].startswith("refs/") \
                    else f"refs/heads/{d['ref']}"
                res.append(sources.selector_compare(
                    x["parent"], ref, d["path"], d["member"],
                    heads.get(d["member"], "")))
            if res and all(r.get("same") is True for r in res):
                note = "; ".join(r["why"] for r in res)
                set_disposition(conn, c["id"], x["parent"], "unchanged_geometry",
                                note=note, actor=actor)
                out.append({"parent": x["parent"],
                            "disposition": "unchanged_geometry", "note": note})
            else:
                out.append({"parent": x["parent"], "disposition": "",
                            "note": "not drafted — " + "; ".join(
                                r.get("why", "") for r in res)})
            continue
        pins = x["pins"]
        if not pins and x.get("shares"):
            sh = x["shares"]
            disp = "use_as_is"
            note = (f"{x['parent']} carries "
                    + ", ".join(f"`{s_['path']}` at the pre-change blob "
                                f"{s_['blob'][:10]}" for s_ in sh)
                    + " — what it released was generated from that copy and "
                      "stays true; it diverges from "
                    + ", ".join(sorted({s_['member'] for s_ in sh}))
                    + " until somebody re-vendors it (a follow-up change — or "
                      "`bump`: re-vendor it in this one, if it must follow "
                      "now). A human decides which.")
            set_disposition(conn, c["id"], x["parent"], disp, note=note,
                            actor=actor)
            out.append({"parent": x["parent"], "disposition": disp,
                        "note": note})
            continue
        if x.get("track"):
            disp = "bump"
            note = ("tracks releases (`track = \"release\"`): the bump belongs "
                    "in this pull request — add a pin-bump proposal for "
                    f"{x['parent']} to pull request {ref(c['id'])}")
        elif all(p["on_a_tag"] for p in pins):
            disp = "use_as_is"
            note = (f"{x['parent']} v{x['parent_version']} pins "
                    + ", ".join(f"{p['child']} v{p['version']}" for p in pins)
                    + " — a released tag that this pull request does not alter; the "
                      "parent keeps building against it until somebody decides "
                      "to adopt v" + pins[0]["child_new_version"])
        else:
            disp = "no_impact"
            note = (f"{x['parent']} pins an unreleased commit of "
                    + ", ".join(p["child"] for p in pins)
                    + " — it is a work in progress already pinned off-release")
        set_disposition(conn, c["id"], x["parent"], disp, note=note, actor=actor)
        out.append({"parent": x["parent"], "disposition": disp, "note": note})
    return out


# -- pre-flight ---------------------------------------------------------------------

def preflight(conn, cid: int, *, actor: str, by_policy: bool = False,
              resume: bool = False) -> dict:
    """Everything the decision needs, asked BEFORE the first merge. Returns
    `{ok, problems: [{member, why, fix}], plan}`; never writes. The point is
    the brief's: no silent half-merge — so every member is proved mergeable and
    every gate is asked here first."""
    from . import checks, gitindex, gitwrite, policy as pol, release, threads
    c = row(conn, cid)
    mem = members(conn, c["id"])
    pl = plan(conn, c["id"], mem)
    probs: list[dict] = []

    def add(m, why, fix=""):
        probs.append({"member": (f"{proposals.label(m['id'])} {m['item_number']} v{m['version']}"
                                 if m else label(c["id"])),
                      "proposal": m["id"] if m else None, "why": why, "fix": fix})

    if not mem:
        add(None, "the pull request has no proposals in it",
            "push a draft with `-o pr=" + str(c["id"]) + "`, or "
            "add_to_change")
    if c["state"] != "open" and not (resume and c["merge_state"] == "partial"):
        add(None, f"{title_ref(c['id'])} is {c['state']}, not in review",
            "submit it first (submit_change / POST "
            f"/api/pulls/{c['id']}/submit)" if c["state"] in ("draft", "rejected")
            else "")
    done = {e["proposal"] for e in json.loads(c["merge_log"] or "[]")
            if e.get("ok")}
    any_released = False
    for m in mem:
        if m["id"] in done or m["state"] in ("merged", "approved"):
            continue
        if m["state"] == "withdrawn":
            add(m, "this proposal was withdrawn (its draft branch was deleted)",
                f"remove it from the pull request or push the branch again")
            continue
        if not m["round"]:
            add(m, f"it has no open round (proposal state {m['state']})",
                "submit the pull request again — every member must be in review")
            continue
        target = m["target"]
        any_released |= target == "RELEASED"
        item = conn.execute("SELECT * FROM items WHERE id=?",
                            (m["item_id"],)).fetchone()
        if by_policy:
            spec = pol.for_target(item["project"] or "", target, conn=conn)
            if not spec["auto"]:
                add(m, f"policy does not auto-merge {target} in "
                       f"{item['project'] or 'this project'}", "a human decides")
        else:
            v = pol.may_approve(conn, item["project"] or "", target, actor)
            if not v["ok"]:
                add(m, v["why"].split("\n")[0],
                    "\n".join(v["why"].split("\n")[1:]).strip())
        for p in pl["info"][m["id"]]["problems"]:
            add(m, p["why"], p["fix"])
        repin_paths = set(pl["info"][m["id"]]["repin"])
        mg = gitindex.merge_preflight(m["item_number"], m["head_sha"], m["version"])
        if not mg["ok"]:
            add(m, mg["why"], mg.get("fix", ""))
        maturity = release.maturity_of(target)
        try:
            roll = checks.gate_for(conn, m["item_number"], m["head_sha"],
                                   target_maturity=maturity)
        except Exception as exc:                               # noqa: BLE001
            roll = {"gate": {"ok": False, "why": str(exc)}, "blocking": []}
        if not roll["gate"]["ok"]:
            # ONE exception, stated: `pins-released` measured on a parent's
            # draft head warns about a child's DRAFT pin that this very
            # decision re-points at the child's release tag. Every other
            # finding stands.
            only_pins = (roll.get("required_failed") == ["pins-released"]
                         and not roll.get("required_missing")
                         and all(b["name"] == "pins-released"
                                 for b in roll.get("blocking") or []))
            unrel = {u["path"] for u in gitindex.release_gate(
                gitwrite.repo_path(m["item_number"]), m["head_sha"])}
            if not (only_pins and unrel and unrel <= repin_paths):
                why = roll["gate"]["why"] or "checks are not green"
                add(m, why.split("\n")[0], "\n".join(why.split("\n")[1:]).strip())
        if target == "RELEASED":
            unrel = [u for u in gitindex.release_gate(
                gitwrite.repo_path(m["item_number"]), m["head_sha"])
                if u["path"] not in repin_paths]
            if unrel:
                add(m, "a RELEASED revision cannot pin unreleased commits: "
                       + ", ".join(f"{u['path']} @ {u['pin']}" for u in unrel),
                    "release (or add to this pull request) the child first, then "
                    "bump the pin to its tag")
            blockers = threads.blocking_open(conn, [("proposal", m["id"])])
            if blockers:
                add(m, threads.refusal(blockers, proposals.label(m["id"])).split("\n")[0],
                    "resolve them, or ship over them by resolving WITH A REASON")
    if any_released:
        blockers = threads.blocking_open(conn, [("change", c["id"])])
        if blockers:
            add(None, threads.refusal(blockers, label(c["id"])).split("\n")[0],
                "resolve them, or ship over them by resolving WITH A REASON")
    for x in impact(conn, c["id"], mem):
        if x["in_change"]:
            continue
        if x.get("advisory") and x["disposition"] != "bump":
            continue                  # source drift: listed, never a gate
        if x["disposition"] == "bump":
            add(None, f"the disposition for {x['parent']} says `bump in this "
                      "change`, but no proposal for it is in the pull request",
                f"push the pin bump with `-o pr={c['id']}` — or "
                "change the disposition")
        elif any_released and not x["confirmed"]:
            add(None, f"{x['sentence']} A RELEASED target needs a disposition "
                      "confirmed by a human"
                      + (f" (drafted: {x['disposition']} by {x['drafted_by']})"
                         if x["disposition"] else ""),
                f"record it: POST /api/pulls/{c['id']}/dispositions "
                f"{{\"parent\": \"{x['parent']}\", \"disposition\": "
                "\"use_as_is\", \"note\": \"why\"}")
    return {"ok": not probs, "problems": probs, "plan": pl}


def say(cid: int, state: str, held: list[str] | None = None) -> str:
    """The submit answer's FIRST line — the one an agent tells the human."""
    label, link = title_ref(cid), url(cid)
    if held:
        # stage 16b: "just build it" HELD it — say why, so the human (or the
        # agent's next step) knows what policy would not decide by silence
        return (f"{label} is waiting for your approval — \"just build it\" "
                f"would have merged it, but: {'; '.join(held)}: {link}")
    if state == "open":
        return f"{label} is waiting for your approval: {link}"
    if state == "merged":
        return (f"{label} was merged by policy (\"just build it\") — nobody "
                f"reviewed it: {link}")
    return f"{label} is {state}: {link}"


def refusal(c: dict, problems: list[dict]) -> str:
    lines = [f"{title_ref(c['id'])} was NOT approved — nothing was merged. "
             f"Pre-flight found {len(problems)} problem"
             f"{'s' if len(problems) != 1 else ''}:"]
    for p in problems:
        lines.append(f"  · {p['member']}: {p['why']}")
        for f in (p.get("fix") or "").splitlines():
            lines.append(f"      {f}")
    lines += ["", "Every member is checked before the first merge, so a refusal "
                  "leaves every repo exactly as it was.",
              f"  {url(c['id'])}"]
    return "\n".join(lines)


# -- reads ----------------------------------------------------------------------------

def brief(conn, c: dict, extra: dict | None = None) -> dict:
    n = member_count(conn, c["id"])
    return {**names(c["id"]), "title": c["title"],
            "state": c["state"], "project": c["project"], "members": n,
            "auto": bool(c["auto"]), **(extra or {})}


def view(conn, cid) -> dict:
    """The change page's payload: header facts, members (each with what its
    section needs to mount the stage-13 components), impact, reviews, the plan,
    the pre-flight answer for the decision bar, and the unified timeline."""
    from . import checks, gitmode, policy as pol, proposals, release, threads
    c = row(conn, cid)
    mem = members(conn, c["id"])
    pl = plan(conn, c["id"], mem)
    order = {m["id"]: i for i, m in enumerate(pl["order"])}
    tc = threads.counts(conn, [("change", c["id"])] +
                        [("proposal", m["id"]) for m in mem])
    out_m, heads = [], []
    for m in mem:
        rnd = m["round"]
        target = m["target"]
        mat = release.maturity_of(target)
        try:
            roll = checks.rollup_for(conn, m["item_number"], m["head_sha"],
                                     target_maturity=mat) if m["head_sha"] else {}
        except Exception:                                      # noqa: BLE001
            roll = {}
        spec = pol.for_target(m["item_project"] or "", target, conn=conn)
        # stage 15: what a reviewer will see in amber/red, as sentences, so an
        # agent does not have to curl /api/repo/<n>/checks to learn it
        warns = []
        if m["head_sha"]:
            # a pin at ANOTHER MEMBER's draft head is re-pinned to its tag at
            # merge — not a finding about this change (run 1 read exactly that
            # warning as "the upright and sleeve parts were never released")
            repinned = {v["child"] for v in pl["info"][m["id"]]["repin"].values()}
            try:
                for r in checks.read(conn, m["item_number"], m["head_sha"]):
                    if r["status"] not in ("warn", "fail", "error"):
                        continue
                    if r["name"] == "pins-released" and set(
                            (r.get("facts") or {}).get("children") or []) <= repinned:
                        continue
                    warns.append(f"{m['item_number']} · {r['name']}: "
                                 f"{r['summary']}")
            except Exception:                                  # noqa: BLE001
                warns = []
        rb = proposals.baseline(m, m["item_number"])
        heads.append(head_entry(m, rb))
        out_m.append({
            "proposal": m["id"], "number": proposals.label(m["id"]), "item": m["item_number"],
            "name": m["item_name"], "kind": m["item_kind"],
            "project": m["item_project"], "version": m["version"],
            "state": m["state"], "title": m["title"], "author": m["author"],
            "head_ref": m["head_ref"], "head_sha": m["head_sha"],
            "base_ref": m["base_ref"], "base_sha": m["base_sha"],
            # stage 19: what the reviewer diffs against — NOT the merge
            # target above (`main`, absent on a never-released item)
            "review_base": rb,
            "review_base_sha": rb.get("sha") or "",
            "merged_tag": m["merged_tag"], "merged_sha": m["merged_sha"],
            "target": target, "round": rnd, "order": order.get(m["id"]),
            "after": pl["deps"].get(m["id"], []),
            "repin": [{"path": k, **v} for k, v in
                      pl["info"][m["id"]]["repin"].items()],
            "claims": pl["info"][m["id"]]["claims"],
            "problems": pl["info"][m["id"]]["problems"],
            "threads": tc.get(("proposal", m["id"]), threads.ZERO),
            "checks": {k: roll.get(k) for k in ("state", "required",
                                                 "required_missing",
                                                 "required_failed", "counts")},
            "warnings": warns,
            "policy": {k: spec[k] for k in ("auto", "approver_kinds", "label",
                                            "source")},
            "url": proposals.url(m["item_number"], m["id"]),
            "page": proposals.page(m["item_number"], m["id"])})
    reviews = [dict(r) for r in conn.execute(
        "SELECT * FROM change_reviews WHERE change_id=? ORDER BY created_at, id",
        (c["id"],))]
    for r in reviews:
        r["heads"] = json.loads(r["heads"] or "{}")
    pf = preflight(conn, c["id"], actor="", resume=True) if c["state"] in (
        "open", "draft", "rejected") else {"ok": False, "problems": []}
    # who would be asked: the decision bar renders the effective rule
    rules = sorted({f"{m['item_project'] or '(no project)'}/{m['target']}: "
                    + pol.for_target(m["item_project"] or "", m["target"],
                                     conn=conn)["label"] for m in mem})
    own = own_checks(conn, c, regenerate=True, heads=heads)
    return {
        "change": {**c, **names(c["id"]),
                   "merge_log": json.loads(c["merge_log"] or "[]")},
        "checks": {**own, "hero": checks.hero_tiles(
            next((r for r in own["checks"] if r["name"] == "physical-diff"), None),
            out_m)},
        "members": sorted(out_m, key=lambda x: (x["order"] is None,
                                                x["order"] or 0)),
        "impact": impact(conn, c["id"], mem),
        "impact_coverage": coverage(conn, c["id"], mem),
        "reviews": reviews,
        "threads": {"change": tc.get(("change", c["id"]), threads.ZERO),
                    "open": sum(v["open"] for v in tc.values()),
                    "blocking": sum(v["blocking"] for v in tc.values())},
        "heads": heads_of(mem),
        # The page asks with no actor, so "(nobody) may not approve" is
        # filtered — and `ok` must follow the list it shows (stage 15: run 2
        # read `{"ok": false, "problems": []}`, which says nothing).
        "preflight": _preflight_view(pf, c, mem),
        "policy": rules,
        "timeline": timeline(conn, c["id"], mem),
        "git_mode": gitmode.mode_for(c["project"] or "", conn),
    }


def _preflight_view(pf: dict, c: dict, mem: list[dict]) -> dict:
    probs = [p for p in pf["problems"]
             if not p["why"].startswith("(nobody)")
             and "is not a user in plm" not in p["why"]]
    if probs:
        return {"ok": False, "problems": probs}
    if c["state"] not in ("open", "draft", "rejected"):
        return {"ok": False, "problems": [],
                "means": f"{title_ref(c['id'])} is {c['state']} — nothing to decide"}
    if c["state"] == "open":
        return {"ok": True, "problems": [],
                "means": "nothing stops the approval — it is waiting for the "
                         "approver the policy names"}
    return {"ok": not probs, "problems": probs}


def timeline(conn, cid: int, mem: list[dict] | None = None) -> list[dict]:
    """ONE timeline: the change's own events + every member's (stage 13's
    `proposals.timeline` over the members' scope — no second implementation)."""
    from . import proposals
    mem = mem if mem is not None else members(conn, cid)
    out = proposals.timeline(conn, [("proposal", m["id"]) for m in mem])
    seen = {e.get("id") for e in out}
    subject = {"type": "change", "id": cid}
    for r in conn.execute(
            "SELECT * FROM events WHERE json_extract(data,'$.change')=? "
            "ORDER BY at", (cid,)):
        if r["id"] in seen:
            continue
        e = proposals._ev(r, subject)
        e["kind"] = CHANGE_KIND.get(r["type"], e["kind"])
        out.append(e)
    for t in conn.execute("SELECT id FROM threads WHERE subject_type='change' "
                          "AND subject_id=?", (cid,)):
        rows = list(conn.execute(
            "SELECT * FROM context WHERE target_type='thread' AND target_id=? "
            "ORDER BY created_at, id", (t[0],)))
        for cm in rows[1:]:
            if proposals._narration(cm["body"]):
                continue
            out.append({"kind": "replied", "type": "thread.reply",
                        "at": cm["created_at"], "actor": cm["actor"],
                        "data": {"thread": t[0], "body": cm["body"]},
                        "subject": subject})
    out.sort(key=lambda e: (e.get("at") or 0, e.get("order", 5)))
    return out


CHANGE_KIND = {
    "change.opened": "opened", "change.member": "member-added",
    "change.submitted": "change-submitted", "change.review": "reviewed",
    "change.review.dismissed": "dismissed", "change.reopened": "reopened",
    "change.merged": "merged", "change.approved": "approved",
    "change.rejected": "rejected", "change.withdrawn": "withdrawn",
    "change.withdrawn_from_review": "left-review",
    "change.disposition": "disposition", "change.repinned": "repinned",
    "change.partial": "partial", "review.requested": "requested",
    # a pull request's own check finished (§2.10) — the bot's timeline line
    "checks.completed": "checks",
}


def listing(conn, *, states: list[str] | None = None, project: str = "",
            limit: int = 100, viewer: str = "") -> list[dict]:
    """Pull requests, newest first — each the ONE row shape
    (`pending.change_entry`: names, `open`, `members`, the viewer's
    `can_decide`) over the stored columns, plus what the queue rows show
    (threads, pin bumps, targets)."""
    ensure(conn)
    from . import pending, threads
    where, args = [], []
    if states:
        where.append(f"c.state IN ({','.join('?' * len(states))})")
        args += states
    if project:
        where.append("c.project=?")
        args.append(project)
    out = []
    for c in [dict(r) for r in conn.execute(
            "SELECT c.* FROM changes c "
            + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY COALESCE(c.submitted_at, c.updated_at) DESC LIMIT ?",
            (*args, limit))]:
        mem = members(conn, c["id"])
        tc = threads.counts(conn, [("change", c["id"])] +
                            [("proposal", m["id"]) for m in mem])
        c.pop("merge_log", None)
        out.append({**c, **pending.change_entry(conn, c, viewer, mem),
                    "threads": {"open": sum(v["open"] for v in tc.values()),
                                "blocking": sum(v["blocking"] for v in tc.values())},
                    "pin_bumps": sum(len(plan_repins(m, mem)) for m in mem),
                    "targets": sorted({m["target"] for m in mem})})
    return out


# -- the backfill ---------------------------------------------------------------------

def backfill(conn) -> int:
    """Every OPEN proposal in a `write` project gets its single-member change —
    once, idempotently (only proposals with no change). Nothing else is
    touched: decided proposals are history and keep no change."""
    from . import ops
    n = 0
    for p in [dict(r) for r in conn.execute(
            "SELECT p.*, i.project FROM proposals p JOIN items i "
            "ON i.id=p.item_id WHERE p.change_id IS NULL AND p.state='open'")]:
        item = conn.execute("SELECT * FROM items WHERE id=?",
                            (p["item_id"],)).fetchone()
        if not item or not ops.git_writes(conn, item):
            continue
        t = db.now()
        cur = conn.execute(
            "INSERT INTO changes (project, title, body_md, state, author, origin, "
            "auto, created_at, updated_at, submitted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item["project"] or "", (p["title"] or item["number"])[:200],
             p["body_md"] or "", "open", p["author"] or "", "backfill", 1,
             p["created_at"], t, p["updated_at"]))
        conn.execute("UPDATE proposals SET change_id=? WHERE id=?",
                     (cur.lastrowid, p["id"]))
        n += 1
    return n
