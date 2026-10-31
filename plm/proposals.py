"""The PROPOSAL — a pull request that survives review rounds (stage 13).

docs/PLM_REVIEW_MODEL.md §2.1 / §2.2 / §2.8. Before this, a "proposal" was a
DRAFT revision plus whichever `approvals` row was newest: every submit made a
new row and a new page, a rejection ended the object, and nothing recorded
which commit a decision had looked at. Git-first breaks that in three places —
several drafts can be open at once, a draft is re-pushed during review, and what
was approved must provably be what was merged — so the proposal is now a
first-class, long-lived object:

    proposals   one per draft BRANCH, numbered #n registry-wide (= its id).
                Surviving reject -> re-push -> resubmit. `head_sha` follows the
                branch; `refs/plm/proposals/<n>/head` keeps it reachable after
                the branch is merged or deleted (GitHub's refs/pull/N/head).
    approvals   unchanged, plus `proposal_id`: each row is now one ROUND.
    reviews     the signoffs, each BOUND TO THE SHA it looked at. A later push
                DISMISSES the ones it made stale (forge "dismiss stale
                reviews"), so an approval in the record is always an approval
                of the bytes that shipped.

WHAT THIS MODULE IS NOT. It is not a second write path: submit, decide and
withdraw stay `plm.ops` verbs, and this module is told about them by four hooks
(`on_head`, `on_submit`, `on_decide`, `on_withdraw`) plus one for a deleted
branch. Policy (`plm/policy.py`) still decides who may approve and whether a
maturity auto-merges; nothing here asks `kind == "bot"`.

PLM_GIT_MODE != write (stage 9): the hooks still keep the proposal/round/review
bookkeeping — it is additive tables and one additive column — but they write NO
event row, NO ref and change no response but by adding keys, so the compatibility harness
(`off` == `master`) sees additive-only differences. In `off` there is no branch,
so a proposal there is "the DRAFT revision and its rounds" with an empty
`head_ref`.

STAGE 14 (built — plm/changes.py): every hook below tells the CHANGE a
proposal belongs to. What follows was the seam as stage 13 left it.

STAGE 14 SEAM (Geoff, 2026-10-05: "the agent is asking me to approve a pull
request to main for the PROJECT — and then also approve a release"). A CHANGE
is a set of proposals across items reviewed as one unit (§2.5). `change_id` is
here, nullable and unused; every read takes a LIST of subjects (`scope`) rather
than one id; the attention event is `review.requested` with a typed `subject`
so a change can raise the same one; and `ops.decide_approval(head_sha=…)` is
side-effect free beyond its own proposal, so a change can call it N times in
dependency order and stop at the first refusal.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from . import config, db

STATES = ("draft", "open", "approved", "merged", "rejected", "withdrawn")
#: a proposal in one of these is closed: a push to its branch name starts a NEW
#: proposal (a deleted-and-recreated `draft/foo` is new work, not a reopening)
CLOSED = ("approved", "merged", "withdrawn")
REF_PREFIX = "refs/plm/proposals/"
#: stage 19 — the review baseline's columns (see `review_base`)
REVIEW_BASE_COLS = ("review_base_ref", "review_base_sha", "review_base_how")

_SCHEMA = """
-- THE PROPOSAL (plm/proposals.py, PLM_REVIEW_MODEL §2.1). One per draft
-- branch, surviving every review round. `id` IS the registry-wide #number.
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  revision_id INTEGER,                    -- the DRAFT it proposes; NULL once a
                                          -- withdrawn draft's row is deleted
  version TEXT NOT NULL DEFAULT '',
  head_ref TEXT NOT NULL DEFAULT '',      -- refs/heads/draft/<topic>; '' = legacy
  head_sha TEXT NOT NULL DEFAULT '',
  base_ref TEXT NOT NULL DEFAULT '',
  base_sha TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  body_md TEXT NOT NULL DEFAULT '',
  target_maturity TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'draft',    -- draft|open|approved|merged|
                                          -- rejected|withdrawn
  author TEXT NOT NULL DEFAULT '',
  change_id INTEGER,                      -- §2.5, stage 14: the CHANGE (ECO)
                                          -- this proposal belongs to
  origin TEXT NOT NULL DEFAULT '',        -- push|api|submit|backfill
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  closed_at REAL,
  merged_sha TEXT NOT NULL DEFAULT '',
  merged_tag TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proposals_item ON proposals(item_id, state);
CREATE INDEX IF NOT EXISTS idx_proposals_rev ON proposals(revision_id);
CREATE INDEX IF NOT EXISTS idx_proposals_change ON proposals(change_id);

-- REVIEWS (§2.2): the signoffs, BOUND TO THE SHA THEY LOOKED AT. `counts` = 1
-- when the review was the decision of record (it went through
-- ops.decide_approval and policy.may_approve); a bot's review that policy does
-- not admit is recorded with counts = 0 — advisory, and stamped so.
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  proposal_id INTEGER NOT NULL REFERENCES proposals(id),
  approval_id INTEGER,                    -- the round it decided, if it did
  reviewer TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',          -- human | bot | policy
  verdict TEXT NOT NULL,                  -- approve | reject | comment
  head_sha TEXT NOT NULL DEFAULT '',      -- what they actually looked at
  body_md TEXT NOT NULL DEFAULT '',
  counts INTEGER NOT NULL DEFAULT 0,
  identity TEXT NOT NULL DEFAULT '',      -- oidc | token | trusted-network …
  created_at REAL NOT NULL,
  dismissed_at REAL,                      -- a later push made it stale
  dismissed_by_sha TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reviews_proposal ON reviews(proposal_id);
"""

_ready = False
_ready_lock = threading.Lock()


def ensure(conn: sqlite3.Connection) -> None:
    """Tables, the one additive column, and the backfill — once per process.

    Here and not in `plm/db.py`'s schema block, for the reason `checks.py` gave:
    the feature is a module, and it merges (or is rolled back) as one. Runs on
    the CALLER's connection and never commits: it is reached from inside ops
    transactions (a submit, a decide), and a commit here would commit half of
    somebody else's write. Every statement is idempotent and the backfill only
    touches rows whose `proposal_id` is NULL.
    """
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        for stmt in _statements(_SCHEMA):
            conn.execute(stmt)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(approvals)")}
        if "proposal_id" not in cols:
            # ADDITIVE: every existing round gets its proposal by the backfill
            # below; NULL means "not yet backfilled", never "has none".
            conn.execute("ALTER TABLE approvals ADD COLUMN proposal_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_proposal "
                     "ON approvals(proposal_id)")
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(proposals)")}
        for col in REVIEW_BASE_COLS:
            if col not in pcols:
                # ADDITIVE (stage 19): the REVIEW BASELINE, kept apart from the
                # merge target (`base_ref`/`base_sha` = main + merge-base)
                conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} TEXT "
                             "NOT NULL DEFAULT ''")
        from . import threads
        threads.ensure(conn)
        backfill(conn)
        _ready = True


def _statements(script: str) -> list[str]:
    """`executescript` COMMITs first, so the schema is run statement by
    statement on the caller's transaction instead."""
    import re
    body = re.sub(r"--[^\n]*", "", script)     # comments carry `;` and quotes
    return [s.strip() for s in body.split(";") if s.strip()]


# -- the backfill: every approvals row becomes a round of a proposal ----------

_DECISION_STATE = {"": "open", "approved": "approved", "rejected": "rejected",
                   "withdrawn": "draft"}


def backfill(conn: sqlite3.Connection) -> int:
    """Group the existing rounds by revision and give each group a proposal.

    One revision's rounds are one proposal: a reject followed by a resubmit of
    the same DRAFT was always the same line of work, and the old UI simply had
    no object to hang them on. Decided rounds also become `reviews` rows — with
    `head_sha` EMPTY, because which bytes the approver looked at was never
    recorded, and inventing it would be the one lie this table exists to stop.
    """
    revs = [r[0] for r in conn.execute(
        "SELECT revision_id FROM approvals WHERE proposal_id IS NULL "
        "GROUP BY revision_id ORDER BY MIN(id)")]
    n = 0
    for rid in revs:
        rounds = [dict(r) for r in conn.execute(
            "SELECT * FROM approvals WHERE revision_id=? ORDER BY id", (rid,))]
        rev = conn.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone()
        if not rev or not rounds:
            continue
        last = rounds[-1]
        state = _DECISION_STATE.get(last["decision"] or "", "open")
        if state == "approved" and (rev["git_tag"] or ""):
            state = "merged"
        pid = _insert(conn, item_id=rev["item_id"], revision_id=rid,
                      version=rev["version"], head_sha=rev["git_commit"] or "",
                      title=rev["summary"] or "", body_md=rounds[0]["note"] or "",
                      target=last["target_state"], state=state,
                      author=rounds[0]["requested_by"], origin="backfill",
                      at=rounds[0]["requested_at"],
                      closed_at=last["decided_at"] if state in CLOSED else None,
                      merged_tag=(rev["git_tag"] or "") if state == "merged" else "")
        conn.execute("UPDATE approvals SET proposal_id=? WHERE revision_id=? "
                     "AND proposal_id IS NULL", (pid, rid))
        for a in rounds:
            if a["decision"] in ("approved", "rejected"):
                conn.execute(
                    "INSERT INTO reviews (proposal_id, approval_id, reviewer, kind, "
                    "verdict, head_sha, body_md, counts, identity, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, a["id"], a["approver"] or "", a.get("approver_kind") or "",
                     "approve" if a["decision"] == "approved" else "reject", "",
                     a["decision_note"] or "", 1, "backfill",
                     a["decided_at"] or a["requested_at"]))
        n += 1
    return n


def _insert(conn, *, item_id, revision_id, version, head_sha, title, body_md,
            target, state, author, origin, head_ref="", at=None,
            closed_at=None, merged_tag="", base_ref="", base_sha="") -> int:
    t = at or db.now()
    cur = conn.execute(
        "INSERT INTO proposals (item_id, revision_id, version, head_ref, head_sha, "
        "base_ref, base_sha, title, body_md, target_maturity, state, author, "
        "origin, created_at, updated_at, closed_at, merged_tag) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, revision_id, version, head_ref, head_sha, base_ref, base_sha,
         title, body_md, target, state, author, origin, t, t, closed_at,
         merged_tag))
    return cur.lastrowid


# -- helpers ------------------------------------------------------------------

def _writes(conn, item) -> bool:
    from . import ops
    return ops.git_writes(conn, item)


def _row(conn, pid: int) -> dict | None:
    r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def label(pid: int) -> str:
    """A proposal as people read it: `p12`. `#n` is a PULL REQUEST's mark
    (changes.ref); a proposal inside one is a `p<n>` (stage 23)."""
    return f"p{pid}"


def page(number: str, pid: int) -> str:
    """The proposal's page as a PATH: `/<N>/pull/<pid>` (stage 22)."""
    from . import webroutes
    return webroutes.pull(number, pid)


def url(number: str, pid: int) -> str:
    """The proposal's address — what a notification carries and a phone opens."""
    return f"{config.PUBLIC_URL}{page(number, pid)}"


def _change_page(cid: int) -> str:
    from . import webroutes
    return webroutes.change(cid)


def _repo(number: str):
    from . import gitrepo
    try:
        return gitrepo.repo_path(number)
    except gitrepo.RepoError:
        return None


def _base_of(number: str, sha: str) -> tuple[str, str]:
    """`main` and the merge-base the head grew from — '' when there is no main
    (59 items have never released anything)."""
    from . import safegit
    repo = _repo(number)
    if repo is None or not sha:
        return "", ""
    main = safegit.git(repo, "rev-parse", "--verify", "--quiet",
                       "refs/heads/main", check=False).strip()
    if not main:
        return "", ""
    mb = safegit.git(repo, "merge-base", main, sha, check=False).strip()
    return "main", mb


# -- the REVIEW BASELINE (stage 19) -------------------------------------------
#
# `base_ref`/`base_sha` above are the MERGE TARGET: `main` and the merge-base
# the head grew from. A first proposal on an item that has never released has
# no `main`, so both were empty and every reader fell back to "the whole
# history" — CHG-379's cradle (#832) read as its registration, its import and
# four tag commits rather than the one edit Codex made on `draft/v0.1.0`
# (handoff finding 1). The REVIEW BASELINE is a different fact: the commit the
# reviewer should diff against. It is, in order:
#
#   push-option  `git push -o base=<ref|sha>` — the author said so; it must be
#                an ancestor of the head (anything else is refused into
#                `base_error`, and the inference below is used instead)
#   inferred     the newest release TAG or open DRAFT tip the head DESCENDS
#                FROM — `gitvalidate.bump_base_for`, the same answer the push
#                door already gives for "which version is this a bump over"
#   main         no version is an ancestor, but `main` exists: its merge-base
#   none         nothing to compare against: a new item, every file is new
#
# Stored on every head move; for a proposal with no stored value (made before
# stage 19) it is inferred AT READ TIME and not written back, so reading an
# old change never edits it.

_RB_CACHE: dict = {}
_RB_TTL = 20.0


def _rb_label(rb: dict) -> str:
    ref = rb.get("ref") or ""
    short = ref.replace("refs/heads/", "").replace("refs/tags/", "")
    return short or (rb.get("sha") or "")[:10]


def review_base(number: str, sha: str, ref: str = "", *, explicit: str = "",
                keep: dict | None = None) -> dict:
    """{ref, sha, how, version, label[, error]} — see the block above."""
    import time
    from . import gitvalidate, safegit
    repo = _repo(number)
    if repo is None or not sha:
        return {"ref": "", "sha": "", "how": "none", "version": "", "label": ""}
    out: dict = {}
    if explicit:
        got, found = "", ""
        for cand in (f"refs/heads/{explicit}", f"refs/tags/{explicit}", explicit):
            got = safegit.git(repo, "rev-parse", "--verify", "--quiet",
                              "--end-of-options", f"{cand}^{{commit}}",
                              check=False).strip()
            if got:
                found = cand
                break
        if not got:
            out["error"] = (f"-o base={explicit}: no such ref or commit in "
                            f"{number}.git — inferred the baseline instead")
        elif not safegit.ok(repo, "merge-base", "--is-ancestor", got, sha):
            out["error"] = (f"-o base={explicit} ({got[:10]}) is not an ancestor "
                            f"of {sha[:10]} — inferred the baseline instead")
        else:
            rb = {"ref": found if found.startswith("refs/") else "",
                  "sha": got, "how": "push-option", "version": ""}
            rb["label"] = _rb_label(rb)
            return rb
    if keep and keep.get("review_base_how") == "push-option" \
            and keep.get("review_base_sha") and safegit.ok(
                repo, "merge-base", "--is-ancestor", keep["review_base_sha"], sha):
        rb = {"ref": keep.get("review_base_ref") or "",
              "sha": keep["review_base_sha"], "how": "push-option", "version": ""}
        rb["label"] = _rb_label(rb)
        return {**rb, **out}
    key = (str(repo), sha, ref)
    hit = _RB_CACHE.get(key)
    if hit and hit[0] > time.time():
        return {**hit[1], **out}
    try:
        v, kind, where = gitvalidate.bump_base_for(repo, sha, ref, strict=True)
    except Exception:                                          # noqa: BLE001
        v, kind, where = "", "", ""
    rb = {"ref": "", "sha": "", "how": "none", "version": ""}
    if kind == "release" and v:
        tag = gitvalidate.releases(repo).get(v, "")
        rb = {"ref": f"refs/tags/v{v}", "sha": tag, "how": "inferred",
              "version": v}
    elif kind == "draft" and where:
        tip = safegit.git(repo, "rev-parse", "--verify", "--quiet",
                          f"refs/heads/{where}", check=False).strip()
        rb = {"ref": f"refs/heads/{where}", "sha": tip, "how": "inferred",
              "version": v}
    if not rb["sha"]:
        mref, mb = _base_of(number, sha)
        if mb:
            rb = {"ref": "refs/heads/main", "sha": mb, "how": "main",
                  "version": ""}
    rb["label"] = _rb_label(rb)
    if len(_RB_CACHE) > 512:
        _RB_CACHE.clear()
    _RB_CACHE[key] = (time.time() + _RB_TTL, rb)
    return {**rb, **out}


def _store_review_base(conn, pid: int, rb: dict) -> None:
    conn.execute("UPDATE proposals SET review_base_ref=?, review_base_sha=?, "
                 "review_base_how=? WHERE id=?",
                 (rb.get("ref") or "", rb.get("sha") or "",
                  rb.get("how") or "", pid))


def baseline(p: dict, number: str = "") -> dict:
    """The REVIEW BASELINE of a proposal row: stored, else inferred (read-only),
    else the merge-base with main. Always a dict with `ref, sha, how, label`
    and `range` (`<base>...<head>`, or the head alone when there is none)."""
    head = p.get("head_sha") or ""
    # A stored INFERRED baseline equal to the head is the stage-19 bug, not a
    # baseline (a sibling draft branch at the same commit was taken as the
    # version this one grew from): infer again at read time, strictly. An
    # author's explicit `-o base=` is never second-guessed.
    self_base = (head and p.get("review_base_sha") == head
                 and p.get("review_base_how") != "push-option"
                 and p.get("state") not in CLOSED and number)
    if self_base:
        rb = review_base(number, head, p.get("head_ref") or "")
        rb.pop("error", None)
        rb["stored"] = False
    elif p.get("review_base_sha") or p.get("review_base_how") == "none":
        rb = {"ref": p.get("review_base_ref") or "",
              "sha": p.get("review_base_sha") or "",
              "how": p.get("review_base_how") or "", "version": ""}
        rb["label"] = _rb_label(rb)
    elif p.get("state") in CLOSED or not number:
        rb = {"ref": p.get("base_ref") or "", "sha": p.get("base_sha") or "",
              "how": "main" if p.get("base_sha") else "none", "version": ""}
        rb["label"] = _rb_label(rb)
    else:
        rb = review_base(number, head, p.get("head_ref") or "")
        rb.pop("error", None)
        rb["stored"] = False
    rb["head"] = head
    rb["range"] = (f"{rb['sha'][:10]}...{head[:10]}" if rb.get("sha") and head
                   else head[:10])
    rb["say"] = (
        f"review {rb['sha'][:10]} → {head[:10]} ({rb['label']}, {rb['how']})"
        if rb.get("sha") else
        f"no baseline: {head[:10]} is compared with nothing (a new item — "
        "every file is new)")
    return rb


def _set_ref(number: str, pid: int, sha: str) -> None:
    """`refs/plm/proposals/<n>/head` = the head. The review history outlives the
    draft branch: after a merge or a withdrawal this ref still names the commit
    every round was about, and `git fetch origin refs/plm/proposals/<n>/head`
    gets it. `refs/plm/` is refused to pushers (check_ref_namespace), so only
    plm moves it; `--all` reachability (plm/gitkeep.py) counts it as a root."""
    from . import gitwrite, safegit
    repo = _repo(number)
    if repo is None or not sha:
        return
    try:
        with gitwrite.lock(repo):
            safegit.git(repo, "update-ref", "-m", f"plm proposal #{pid}",
                        f"{REF_PREFIX}{pid}/head", sha)
    except Exception as exc:                                   # noqa: BLE001
        # Never fatal: the branch is the live pointer; this ref is the memory.
        # `ensure_ref` heals it on the next read of the proposal.
        print(f"[proposals] could not move {REF_PREFIX}{pid}/head: {exc}",
              flush=True)


def _guard(conn, number: str, old: str, why: str) -> None:
    from . import gitkeep
    try:
        gitkeep.guard(conn, number, [old], why=why)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[proposals] keep guard for {number}@{old[:10]} failed: {exc}",
              flush=True)


def keep_sha(number: str, sha: str, why: str) -> None:
    """A commit a REVIEW or a THREAD is anchored to must outlive a force-push.
    The branch reaches it today; `refs/plm/keep/<sha>` (plm/gitkeep.py, the one
    keep rule) makes sure something still does tomorrow. Best-effort, like
    every keep site."""
    if not (number and sha and len(sha) == 40):
        return
    from . import gitkeep
    try:
        gitkeep.keep(number, sha, why, closure=False)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[proposals] keep {number}@{sha[:10]} failed: {exc}", flush=True)


def event(conn, type_: str, p: dict, *, actor: str = "", data: dict | None = None,
          force: bool = False) -> None:
    """An event row for the TIMELINE — only where git-first writes (stage 9:
    `off` must add no event row; compat.sh diffs the events table)."""
    if not force:
        item = conn.execute("SELECT * FROM items WHERE id=?",
                            (p["item_id"],)).fetchone()
        if not item or not _writes(conn, item):
            return
    db.add_event(conn, type_, item_id=p["item_id"], revision_id=p.get("revision_id"),
                 actor=actor, data={"proposal": p["id"], **(data or {})})


def attention(conn, kind: str, p: dict, *, reason: str, actor: str = "",
              headline: str = "", extra: dict | None = None) -> tuple:
    """ONE "needs your attention" moment, as the `(type, payload)` tuple the
    HTTP layer hands to `actions.dispatch` (→ `actions.send()`, dry-run in the
    POC). Kinds: `review.requested` (submitted, re-pushed after a reject),
    `thread.blocking`, `threads.addressed`.

    The payload names its SUBJECT generically — `{type: "proposal", id}` — so a
    stage-14 CHANGE raises the very same event with `type: "change"`, and a
    subscriber written today keeps working."""
    number = conn.execute("SELECT number FROM items WHERE id=?",
                          (p["item_id"],)).fetchone()[0]
    payload = {"subject": {"type": "proposal", "id": p["id"],
                           "number": f"#{p['id']}"},
               "reason": reason, "item": number, "version": p.get("version") or "",
               "head_sha": p.get("head_sha") or "", "actor": actor,
               "url": url(number, p["id"]),
               "headline": headline or f"#{p['id']} {number} — {reason}",
               **(extra or {})}
    event(conn, kind, p, actor=actor, data={k: v for k, v in payload.items()
                                            if k not in ("subject", "url")})
    return (kind, payload)


# -- lookups ------------------------------------------------------------------

def for_revision(conn, revision_id: int, *, open_only: bool = True) -> dict | None:
    q = "SELECT * FROM proposals WHERE revision_id=?"
    if open_only:
        q += f" AND state NOT IN ({','.join('?' * len(CLOSED))})"
        r = conn.execute(q + " ORDER BY id DESC LIMIT 1", (revision_id, *CLOSED))
    else:
        r = conn.execute(q + " ORDER BY id DESC LIMIT 1", (revision_id,))
    r = r.fetchone()
    return dict(r) if r else None


def for_approval(conn, approval_id: int) -> dict | None:
    ensure(conn)
    a = conn.execute("SELECT proposal_id, revision_id FROM approvals WHERE id=?",
                     (approval_id,)).fetchone()
    if not a:
        return None
    if a["proposal_id"]:
        return _row(conn, a["proposal_id"])
    return for_revision(conn, a["revision_id"], open_only=False)


def _for_branch(conn, item_id: int, ref: str) -> dict | None:
    r = conn.execute(
        f"SELECT * FROM proposals WHERE item_id=? AND head_ref=? AND state NOT IN "
        f"({','.join('?' * len(CLOSED))}) ORDER BY id DESC LIMIT 1",
        (item_id, ref, *CLOSED)).fetchone()
    return dict(r) if r else None


# -- the hooks ----------------------------------------------------------------

def on_head(conn, number: str, ref: str, sha: str, *, actor: str = "",
            origin: str = "push", base: str = "") -> dict:
    """A draft branch's head moved (a push, or a commit plm made on it).

    Find or open the proposal for that branch, follow the head, keep it
    reachable, and — if the head CHANGED — dismiss every review that looked at
    an older sha. A rejected proposal re-opens as a draft: "a reject no longer
    ends the object — the same proposal reopens on the next push" (§2.2).
    Returns `{proposal, dismissed, reopened, _events}` for the caller's response.
    """
    if not (ref or "").startswith("refs/heads/draft/") or not sha:
        return {}
    ensure(conn)
    item = db.resolve_item(conn, number)
    if not item:
        return {}
    rev = conn.execute(
        "SELECT * FROM revisions WHERE item_id=? AND git_commit=? "
        "ORDER BY id DESC LIMIT 1", (item["id"], sha)).fetchone()
    p = _for_branch(conn, item["id"], ref)
    if not p and rev:
        # adopt a proposal that has no branch name yet (backfilled, or opened by
        # a submit of a revision plm made before it knew the branch)
        p = for_revision(conn, rev["id"])
        if p and p["head_ref"] and p["head_ref"] != ref:
            p = None
    base_ref, base_sha = _base_of(number, sha)
    out: dict = {"_events": []}
    rb = review_base(number, sha, ref, explicit=base,
                     keep=p if p else None)
    if rb.get("error"):
        out["base_error"] = rb["error"]
    if not p:
        pid = _insert(conn, item_id=item["id"],
                      revision_id=rev["id"] if rev else None,
                      version=rev["version"] if rev else "", head_ref=ref,
                      head_sha=sha, base_ref=base_ref, base_sha=base_sha,
                      title=(rev["summary"] if rev else "") or ref.split("/", 3)[-1],
                      body_md="", target="", state="draft", author=actor,
                      origin=origin)
        _store_review_base(conn, pid, rb)
        p = _row(conn, pid)
        event(conn, "proposal.opened", p, actor=actor,
              data={"head": sha, "ref": ref, "origin": origin})
        _set_ref(number, pid, sha)
        out["proposal"] = brief(conn, p)
        return out
    prev = p["head_sha"]
    conn.execute(
        "UPDATE proposals SET head_ref=?, head_sha=?, base_ref=?, base_sha=?, "
        "revision_id=COALESCE(?, revision_id), version=COALESCE(?, version), "
        "updated_at=? WHERE id=?",
        (ref, sha, base_ref, base_sha, rev["id"] if rev else None,
         rev["version"] if rev else None, db.now(), p["id"]))
    _store_review_base(conn, p["id"], rb)
    p = _row(conn, p["id"])
    _set_ref(number, p["id"], sha)
    if prev and prev != sha:
        # ⚠️ ORPHAN TIME, AGAIN (plm/gitkeep.py). The index's own guard ran
        # while refs/plm/proposals/<n>/head still reached the old tip, so it
        # saw nothing orphaned; moving that ref is what orphans it. Ask the
        # same question now — keep the old tip iff something still pins it.
        _guard(conn, number, prev, f"proposal #{p['id']} head moved "
                                   f"{prev[:10]} -> {sha[:10]}")
        out["dismissed"] = dismiss_stale(conn, p, sha, actor=actor)
        in_change = False
        if p.get("change_id") and _writes(conn, item):
            # STAGE 14: the change's signoffs are bound to every member's head;
            # this one moved, so those stop speaking for the bytes (visibly),
            # and a rejected change comes back as a draft.
            from . import changes
            got = changes.on_head_moved(conn, p, sha, prev, actor=actor)
            if got.get("dismissed"):
                out["change_dismissed"] = got["dismissed"]
            in_change = changes.member_count(conn, p["change_id"]) > 1
        if p["state"] == "rejected":
            conn.execute("UPDATE proposals SET state='draft', updated_at=? "
                         "WHERE id=?", (db.now(), p["id"]))
            p = _row(conn, p["id"])
            event(conn, "proposal.reopened", p, actor=actor,
                  data={"from": prev, "to": sha})
            out["reopened"] = True
            if in_change:
                # one piece of work, one notification: a multi-member change
                # asks again when it is SUBMITTED again (changes.on_submitted)
                out["proposal"] = brief(conn, p)
                return out
            # NEEDS YOUR ATTENTION: the author answered a rejection. Whoever
            # rejected it is the person to tell.
            rej = conn.execute(
                "SELECT reviewer FROM reviews WHERE proposal_id=? AND "
                "verdict='reject' ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
            out["_events"].append(attention(
                conn, "review.requested", p, reason="re-pushed after reject",
                actor=actor, extra={"to": rej[0] if rej else "",
                                    "from_sha": prev}))
    out["proposal"] = brief(conn, p)
    return out


def dismiss_stale(conn, p: dict, sha: str, *, actor: str = "") -> list[int]:
    """Forge "dismiss stale reviews", keyed PER PROPOSAL: every approve/reject
    that looked at a sha other than the new head stops speaking for the bytes.
    It stays in the record — dismissed, with the sha that dismissed it."""
    rows = [r[0] for r in conn.execute(
        "SELECT id FROM reviews WHERE proposal_id=? AND dismissed_at IS NULL "
        "AND verdict IN ('approve','reject') AND head_sha<>? AND head_sha<>''",
        (p["id"], sha))]
    if rows:
        conn.execute(
            f"UPDATE reviews SET dismissed_at=?, dismissed_by_sha=? WHERE id IN "
            f"({','.join('?' * len(rows))})", (db.now(), sha, *rows))
        event(conn, "review.dismissed", p, actor=actor,
              data={"reviews": rows, "head": sha,
                    "why": "the head moved past the sha they reviewed"})
    return rows


def on_submit(conn, *, approval_id: int, rev, item, actor: str,
              target_state: str, note: str,
              notify: bool = True) -> tuple[dict, list]:
    """A round opened. Attach it to the revision's proposal (opening one for a
    revision that never had a branch — legacy, or a plm-made draft), mark the
    proposal `open`, and raise the attention event. Returns (proposal brief,
    events)."""
    ensure(conn)
    writes = _writes(conn, item)
    p = for_revision(conn, rev["id"])
    if not p:
        head_ref = ""
        sha = rev["git_commit"] or ""
        if writes:
            from . import ops
            g = ops.git_origin(conn, dict(rev) | {"number": item["number"]})
            head_ref = (g or {}).get("branch") or ""
            if head_ref and not head_ref.startswith("refs/heads/"):
                head_ref = "refs/heads/" + head_ref
        pid = _insert(conn, item_id=item["id"], revision_id=rev["id"],
                      version=rev["version"], head_ref=head_ref, head_sha=sha,
                      title=rev["summary"] or "", body_md=note, target=target_state,
                      state="open", author=actor, origin="submit")
        p = _row(conn, pid)
        if writes and head_ref:
            _set_ref(item["number"], pid, sha)
    resubmit = bool(conn.execute(
        "SELECT 1 FROM approvals WHERE proposal_id=? AND id<>? LIMIT 1",
        (p["id"], approval_id)).fetchone())
    conn.execute("UPDATE approvals SET proposal_id=? WHERE id=?",
                 (p["id"], approval_id))
    conn.execute(
        "UPDATE proposals SET state='open', target_maturity=?, "
        "body_md=CASE WHEN ?<>'' THEN ? ELSE body_md END, updated_at=? WHERE id=?",
        (target_state, note, note, db.now(), p["id"]))
    p = _row(conn, p["id"])
    if writes:
        # ⭐ STAGE 14 — EVERY REVIEW IS A CHANGE. The round joins (or opens) a
        # CHANGE, and the human is asked about the change, once — not about
        # this proposal on its own. Outside `write` none of this runs.
        from . import changes
        cbrief, cevents = changes.on_submitted(conn, p, item, actor=actor,
                                               round_id=approval_id,
                                               notify=notify)
        out = brief(conn, _row(conn, p["id"]))
        out["change"] = cbrief
        return out, cevents
    if not notify:
        return brief(conn, p), []
    from . import threads
    tc = threads.counts(conn, [("proposal", p["id"])]).get(("proposal", p["id"]),
                                                           threads.ZERO)
    ev = attention(
        conn, "review.requested", p,
        reason="resubmitted" if resubmit else "submitted", actor=actor,
        headline=f"#{p['id']} needs your approval — {item['number']} "
                 f"v{rev['version']} → {target_state.lower()}"
                 + (f" · {tc['open']} open thread{'s' if tc['open'] != 1 else ''}"
                    if tc["open"] else ""),
        extra={"round": approval_id, "target_state": target_state,
               "threads": tc})
    return brief(conn, p), [ev]


def on_decide(conn, *, approval: dict, decision: str, actor: str,
              approver_kind: str, note: str, head_sha: str, git_result: dict,
              identity: str = "", via_change: int = 0) -> dict:
    """A round was decided. Record the signoff — bound to the sha that was
    reviewed — and move the proposal. Side-effect free beyond this proposal."""
    ensure(conn)
    p = for_approval(conn, approval["id"])
    if not p:
        return {}
    conn.execute(
        "INSERT INTO reviews (proposal_id, approval_id, reviewer, kind, verdict, "
        "head_sha, body_md, counts, identity, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (p["id"], approval["id"], actor, approver_kind or "",
         "approve" if decision == "approved" else "reject", head_sha or "",
         note or "", 1, identity or "", db.now()))
    if decision == "approved":
        state = "merged" if git_result.get("merged") or git_result.get("tag") \
            else "approved"
        conn.execute(
            "UPDATE proposals SET state=?, closed_at=?, updated_at=?, "
            "merged_sha=?, merged_tag=? WHERE id=?",
            (state, db.now(), db.now(), git_result.get("commit") or "",
             git_result.get("tag") or "", p["id"]))
    else:
        conn.execute("UPDATE proposals SET state='rejected', updated_at=? "
                     "WHERE id=?", (db.now(), p["id"]))
    p = _row(conn, p["id"])
    item = conn.execute("SELECT number FROM items WHERE id=?",
                        (p["item_id"],)).fetchone()
    keep_sha(item[0], head_sha, f"review of proposal #{p['id']}")
    if p.get("change_id") and not via_change:
        # the single-member door: the change follows its one proposal
        from . import changes
        changes.follow(conn, p["change_id"])
    return brief(conn, p)


def on_withdraw(conn, *, approval_id: int, actor: str = "",
                why: str = "the round was withdrawn") -> dict:
    ensure(conn)
    p = for_approval(conn, approval_id)
    if not p:
        return {}
    conn.execute("UPDATE proposals SET state='draft', updated_at=? WHERE id=?",
                 (db.now(), p["id"]))
    if p.get("change_id"):
        from . import changes
        changes.on_round_withdrawn(conn, p, actor=actor, why=why)
    return brief(conn, _row(conn, p["id"]))


def on_branch_deleted(conn, number: str, ref: str, *, actor: str = "") -> dict:
    """`git push origin :draft/<topic>` — the draft is withdrawn and its
    revision row goes (gitindex.withdraw_draft). The proposal stays, closed as
    `withdrawn`, and its head ref keeps the commits — so a link to it still
    shows what was proposed and why it stopped. Called BEFORE the revision row
    is deleted."""
    ensure(conn)
    item = db.resolve_item(conn, number)
    if not item or not (ref or "").startswith("refs/heads/draft/"):
        return {}
    p = _for_branch(conn, item["id"], ref)
    if not p:
        return {}
    conn.execute("UPDATE proposals SET state='withdrawn', closed_at=?, "
                 "updated_at=?, revision_id=NULL WHERE id=?",
                 (db.now(), db.now(), p["id"]))
    p = _row(conn, p["id"])
    event(conn, "proposal.withdrawn", p, actor=actor,
          data={"ref": ref, "why": "the draft branch was deleted"})
    if p.get("change_id"):
        from . import changes
        changes.follow(conn, p["change_id"])
    return brief(conn, p)


# -- reviews that are not decisions --------------------------------------------

def add_review(conn, pid: int, *, actor: str, verdict: str, body: str = "",
               head_sha: str = "", counts: bool = False,
               identity: str = "") -> dict:
    """A review that does NOT decide: a comment-review, or an advisory approve
    / reject from somebody policy does not admit (a bot, by default) — recorded,
    stamped, and never counted. Decisions go through `ops.decide_approval`."""
    ensure(conn)
    p = _row(conn, pid)
    if not p:
        raise LookupError(f"no proposal #{pid}")
    user = db.get_user(conn, actor) or {}
    head = head_sha or p["head_sha"]
    cur = conn.execute(
        "INSERT INTO reviews (proposal_id, reviewer, kind, verdict, head_sha, "
        "body_md, counts, identity, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, actor, user.get("kind") or "", verdict, head, body, int(counts),
         identity, db.now()))
    event(conn, "review", p, actor=actor,
          data={"verdict": verdict, "head": head, "counts": bool(counts),
                "kind": user.get("kind") or "", "body": body[:400]})
    item = conn.execute("SELECT number FROM items WHERE id=?",
                        (p["item_id"],)).fetchone()
    keep_sha(item[0], head, f"review of proposal #{pid}")
    return {"id": cur.lastrowid, "verdict": verdict, "head_sha": head,
            "counts": bool(counts), "kind": user.get("kind") or ""}


# -- reads --------------------------------------------------------------------

def brief(conn, p: dict) -> dict:
    """The small form every response carries: enough to link and to badge."""
    from . import threads
    number = conn.execute("SELECT number FROM items WHERE id=?",
                          (p["item_id"],)).fetchone()[0]
    tc = threads.counts(conn, [("proposal", p["id"])]).get(
        ("proposal", p["id"]), threads.ZERO)
    return {"id": p["id"], "number": f"#{p['id']}", "state": p["state"],
            "item": number, "version": p["version"], "head_ref": p["head_ref"],
            "head_sha": p["head_sha"], "change_id": p["change_id"],
            "review_base": baseline(p, number),
            **({"change": f"CHG-{p['change_id']}",
                "change_page": _change_page(p["change_id"])}
               if p["change_id"] else {}),
            "threads": tc, "url": url(number, p["id"]),
            "page": page(number, p["id"])}


def ensure_ref(conn, p: dict, number: str) -> None:
    """Heal `refs/plm/proposals/<n>/head` if a best-effort move failed."""
    from . import safegit
    repo = _repo(number)
    if repo is None or not p.get("head_sha") or not p.get("head_ref"):
        return
    cur = safegit.git(repo, "rev-parse", "--verify", "--quiet",
                      f"{REF_PREFIX}{p['id']}/head", check=False).strip()
    if cur != p["head_sha"] and safegit.ok(repo, "cat-file", "-e",
                                           f"{p['head_sha']}^{{commit}}"):
        _set_ref(number, p["id"], p["head_sha"])


def get(conn, pid: int) -> dict:
    """One proposal and everything about it except the diff (the diff is
    `/api/repo/{n}/compare?base=&head=` with the refs in `git`)."""
    ensure(conn)
    p = _row(conn, pid)
    if not p:
        raise LookupError(f"no proposal #{pid}")
    item = dict(conn.execute("SELECT id, number, name, kind, project FROM items "
                             "WHERE id=?", (p["item_id"],)).fetchone())
    rounds = [dict(r) for r in conn.execute(
        "SELECT * FROM approvals WHERE proposal_id=? ORDER BY id", (pid,))]
    reviews = [dict(r) for r in conn.execute(
        "SELECT * FROM reviews WHERE proposal_id=? ORDER BY created_at, id", (pid,))]
    rev = None
    if p["revision_id"]:
        r = conn.execute("SELECT id, version, state, summary, author, git_commit, "
                         "git_tag, created_at FROM revisions WHERE id=?",
                         (p["revision_id"],)).fetchone()
        rev = dict(r) if r else None
    ensure_ref(conn, p, item["number"])
    return {**p, "number": f"#{pid}", "item": item, "revision": rev,
            "review_base": baseline(p, item["number"]),
            "rounds": rounds, "current_round": next(
                (r for r in reversed(rounds) if not r["decision"]), None),
            "reviews": reviews, "url": url(item["number"], pid),
            "head_keep_ref": f"{REF_PREFIX}{pid}/head",
            "page": page(item["number"], pid)}


def listing(conn, *, item: str = "", states: list[str] | None = None,
            change_id: int | None = None, ids: list[int] | None = None,
            limit: int = 100) -> list[dict]:
    ensure(conn)
    where, args = [], []
    if item:
        it = db.resolve_item(conn, item)
        if not it:
            raise LookupError(f"unknown item: {item}")
        where.append("p.item_id=?"); args.append(it["id"])
    if states:
        where.append(f"p.state IN ({','.join('?' * len(states))})"); args += states
    if change_id is not None:
        where.append("p.change_id=?"); args.append(change_id)
    if ids:
        where.append(f"p.id IN ({','.join('?' * len(ids))})"); args += ids
    rows = [dict(r) for r in conn.execute(
        "SELECT p.*, i.number AS item_number, i.name AS item_name FROM proposals p "
        "JOIN items i ON i.id=p.item_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY p.updated_at DESC LIMIT ?", (*args, limit))]
    from . import threads
    tc = threads.counts(conn, [("proposal", r["id"]) for r in rows])
    for r in rows:
        r["threads"] = tc.get(("proposal", r["id"]), threads.ZERO)
        r["url"] = url(r["item_number"], r["id"])
    return rows


# -- the timeline (§2.8): every source, one ordering ---------------------------

TIMELINE_KIND = {
    "git.index": "pushed", "submit": "submitted", "release": "approved",
    "release.rejected": "rejected", "release.withdrawn": "withdrawn",
    "git.withdraw": "branch-deleted", "proposal.opened": "opened",
    "proposal.reopened": "reopened", "proposal.withdrawn": "branch-deleted",
    "review.dismissed": "dismissed", "review": "reviewed",
    "review.requested": "requested", "thread.opened": "thread",
    "thread.resolved": "resolved", "thread.reopened": "thread-reopened",
    "thread.claimed": "claimed", "thread.applied": "applied",
    "thread.blocking": "blocking", "threads.addressed": "addressed",
    "release.signoff": "signoff", "context": "commented",
    # stage 14: a member's change events carry `proposal` too, so they reach
    # the proposal's timeline — named the way the change page names them
    "change.member": "member-added", "change.submitted": "change-submitted",
    "change.review": "reviewed", "change.review.dismissed": "dismissed",
    "change.reopened": "reopened", "change.merged": "merged",
    "change.repinned": "repinned", "change.disposition": "disposition",
    "change.withdrawn_from_review": "left-review", "change.partial": "partial",
    "change.opened": "opened", "change.rejected": "rejected",
}


def timeline(conn, scope: list[tuple[str, int]]) -> list[dict]:
    """The forge timeline over a SCOPE — a list of subjects (`("proposal", 3)`,
    `("release", 9)`; a stage-14 change page passes its N proposals). Sources,
    interleaved by time: the event log (pushes, submits, decisions, dismissals,
    threads), the commits in base..head, the check board of every pushed sha,
    and thread comments. Each entry says which subject it belongs to."""
    ensure(conn)
    out: list[dict] = []
    seen_ev: set[int] = set()
    for st, sid in scope:
        if st == "proposal":
            out += _timeline_proposal(conn, sid, seen_ev)
        elif st == "release":
            out += _timeline_release(conn, sid, seen_ev)
    out.sort(key=lambda e: (e.get("at") or 0, e.get("order", 5)))
    return out


def _ev(r, subject) -> dict:
    try:
        data = json.loads(r["data"] or "{}")
    except ValueError:
        data = {}
    return {"kind": TIMELINE_KIND.get(r["type"], r["type"]), "type": r["type"],
            "at": r["at"], "actor": r["actor"], "data": data, "subject": subject,
            "id": r["id"]}


def _timeline_proposal(conn, pid: int, seen: set) -> list[dict]:
    from . import gitrepo, threads
    p = _row(conn, pid)
    if not p:
        return []
    subject = {"type": "proposal", "id": pid}
    out = []
    # 1. events that name this proposal
    for r in conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "json_extract(data,'$.proposal')=? AND type<>'thread.reply' "
            "ORDER BY at", (p["item_id"], pid)):
        seen.add(r["id"]); out.append(_ev(r, subject))
    # 2. events of its revision(s), fenced by time (a withdrawn draft's rowid is
    #    reused by the next INSERT — stage 4's finding), and of its rounds
    revs = {p["revision_id"]} if p["revision_id"] else set()
    revs |= {r[0] for r in conn.execute(
        "SELECT DISTINCT revision_id FROM approvals WHERE proposal_id=?", (pid,))}
    for rid in revs:
        for r in conn.execute(
                "SELECT * FROM events WHERE revision_id=? AND item_id=? AND at>=? "
                "AND type IN ('git.index','submit','release','release.rejected',"
                "'release.withdrawn') ORDER BY at",
                (rid, p["item_id"], p["created_at"] - 1)):
            if r["id"] in seen:
                continue
            seen.add(r["id"]); out.append(_ev(r, subject))
    # 3. comments on the rounds (the stage-4 conversation, kept)
    for c in conn.execute(
            "SELECT c.* FROM context c JOIN approvals a ON a.id=c.target_id "
            "WHERE c.target_type='approval' AND a.proposal_id=?", (pid,)):
        out.append({"kind": "commented", "type": "context", "at": c["created_at"],
                    "actor": c["actor"], "data": {"body": c["body"]},
                    "subject": subject})
    # 4. thread comments (not the first: that one IS the "opened" entry)
    for t in conn.execute("SELECT id FROM threads WHERE subject_type='proposal' "
                          "AND subject_id=?", (pid,)):
        rows = list(conn.execute(
            "SELECT * FROM context WHERE target_type='thread' AND target_id=? "
            "ORDER BY created_at, id", (t[0],)))
        for c in rows[1:]:
            if _narration(c["body"]):
                continue              # the event row already says it
            out.append({"kind": "replied", "type": "thread.reply",
                        "at": c["created_at"], "actor": c["actor"],
                        "data": {"thread": t[0], "body": c["body"]},
                        "subject": subject})
    # 5. commits and checks — git's half
    number = conn.execute("SELECT number FROM items WHERE id=?",
                          (p["item_id"],)).fetchone()[0]
    repo = _repo(number)
    if repo is not None and p["head_sha"]:
        base = baseline(p, number).get("sha") or ""
        try:
            rng = f"{base}..{p['head_sha']}" if base else p["head_sha"]
            raw = gitrepo.git(repo, "log", "--format=%H%x1f%s%x1f%an%x1f%ct",
                              "-n", "60", rng, check=False).strip()
        except gitrepo.RepoError:
            raw = ""
        for line in raw.splitlines():
            f = (line.split("\x1f") + [""] * 4)[:4]
            try:
                at = float(f[3])
            except ValueError:
                continue
            out.append({"kind": "commit", "type": "commit", "at": at,
                        "actor": f[2], "order": 1,
                        "data": {"commit": f[0], "subject": f[1]},
                        "subject": subject})
        shas = {e["data"].get("commit") for e in out
                if e["type"] == "git.index"} | {p["head_sha"]}
        shas.discard(None)
        out += _checks_entries(conn, number, shas, subject)
    return out


def _checks_entries(conn, number: str, shas: set, subject: dict) -> list[dict]:
    try:
        rows = list(conn.execute(
            f"SELECT sha, status, MAX(COALESCE(ended_at, created_at)) AS at, "
            f"COUNT(*) AS n FROM checks WHERE number=? AND sha IN "
            f"({','.join('?' * len(shas))}) GROUP BY sha, status",
            (number, *shas)))
    except sqlite3.OperationalError:
        return []                              # no checks table on this instance
    by: dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(r["sha"], {"counts": {}, "at": 0})
        b["counts"][r["status"]] = r["n"]
        b["at"] = max(b["at"], r["at"] or 0)
    return [{"kind": "checks", "type": "checks", "at": b["at"], "actor": "checks",
             "order": 6, "data": {"commit": sha, "counts": b["counts"]},
             "subject": subject} for sha, b in by.items()]


def _timeline_release(conn, rid: int, seen: set) -> list[dict]:
    subject = {"type": "release", "id": rid}
    rel = conn.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()
    if not rel:
        return []
    out = [{"kind": "requested", "type": "release.requested",
            "at": rel["requested_at"], "actor": rel["requested_by"],
            "data": {"to": rel["to_maturity"], "from": rel["from_maturity"]},
            "subject": subject}]
    for s in conn.execute("SELECT * FROM release_signoffs WHERE release_id=? "
                          "ORDER BY signed_at", (rid,)):
        out.append({"kind": "signoff", "type": "release.signoff",
                    "at": s["signed_at"], "actor": s["signer"],
                    "data": {"kind": s["kind"], "item": s["item"],
                             "verdict": s["verdict"], "note": s["note"],
                             "signer_kind": s["signer_kind"]},
                    "subject": subject})
    for r in conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "json_extract(data,'$.release')=? AND type<>'thread.reply' "
            "ORDER BY at", (rel["item_id"], rid)):
        if r["id"] in seen:
            continue
        seen.add(r["id"]); out.append(_ev(r, subject))
    if rel["decided_at"]:
        out.append({"kind": rel["state"], "type": "release.decided",
                    "at": rel["decided_at"], "actor": rel["decided_by"],
                    "data": {"note": rel["decision_note"],
                             "kind": rel["decided_kind"]}, "subject": subject})
    for t in conn.execute("SELECT id FROM threads WHERE subject_type='release' "
                          "AND subject_id=?", (rid,)):
        rows = list(conn.execute(
            "SELECT * FROM context WHERE target_type='thread' AND target_id=? "
            "ORDER BY created_at, id", (t[0],)))
        for c in rows[1:]:
            if _narration(c["body"]):
                continue
            out.append({"kind": "replied", "type": "thread.reply",
                        "at": c["created_at"], "actor": c["actor"],
                        "data": {"thread": t[0], "body": c["body"]},
                        "subject": subject})
    return out


def _narration(body: str) -> bool:
    """The lines plm/threads.py writes into a thread when its STATE moves
    (resolved, addressed, reopened, applied) — kept in the thread, where a
    reader of the conversation needs them, and left out of the timeline, where
    the state event already says the same thing."""
    return (body or "").startswith(("**Resolved**", "**Addressed**",
                                    "**Reopened**", "**Suggestion applied**"))
