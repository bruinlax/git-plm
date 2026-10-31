"""MCP tools for the review model (stage 13) — agents are first-class reviewers.

Imported by ONE hook line in `plm_mcp/server.py` (`tools_review.install(mcp)`).
Thin callers of `plm/proposals.py` and `plm/threads.py` — the same functions
the HTTP routes call — so an agent and the browser cannot disagree about what a
thread is anchored to or whether it is outdated.

Registered only where git-first is on somewhere (`gitmode.any_serves()`): an
`off` instance's tool list is master's (the compatibility harness).
"""
from __future__ import annotations

from plm import actions, db, proposals, threads

#: Folded into `plm_mcp/instructions.py` (GIT_FIRST) by stage 14 — one home.
#: Kept as a name so anything that imported it still reads the same text.
from plm_mcp.instructions import REVIEW_LOOP as LOOP  # noqa: E402


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _deliver(out: dict) -> dict:
    """The MCP door used to DROP the integrations event of a write; the review
    model's "needs your attention" moments go through `actions.dispatch` (→
    `actions.send()`, the one wire, dry-run in the POC) from here too."""
    for ev in out.pop("_events", None) or []:
        actions.dispatch(ev[0], ev[1])
    out.pop("_event", None)
    return out


def _scope(conn, number: str, proposal: int, release: int) -> list:
    if proposal:
        return [("proposal", int(proposal))]
    if release:
        return [("release", int(release))]
    if number:
        return [("proposal", r["id"]) for r in proposals.listing(
            conn, item=number, states=["draft", "open", "rejected"])]
    raise threads.ThreadError("name a proposal, a release, or an item number")


def list_threads(number: str = "", proposal: int = 0, release: int = 0,
                 state: str = "open") -> dict:
    """Review threads on a proposal (#n), a release, or every open proposal of
    an item. Each row: id, title, author (+`advisory` when a bot raised it),
    `blocking`, state, `outdated` (computed from git against the head: the
    anchored line / key / mesh changed since it was written), the anchor (kind,
    path, line, key, or the mesh point `p`/`n` in model space), any suggested
    change, and `addressed_by`. `state` = open | resolved | outdated | ''."""
    with db.connect() as conn:
        try:
            rows = threads.listing(conn, _scope(conn, number, proposal, release),
                                   state=state, comments=False)
        except (threads.ThreadError, LookupError) as exc:
            return _err(str(exc))
    return _ok(threads=[{k: t[k] for k in (
        "id", "title", "author", "author_kind", "advisory", "blocking", "state",
        "outdated", "status", "anchor", "suggestion", "addressed_by",
        "resolved_by", "resolution", "subject")} for t in rows],
        count=len(rows))


def get_thread(thread_id: int) -> dict:
    """One thread in full: every comment, and the anchor RESOLVED — a file
    line (and where that line is at the head now), a plm.toml key (its value
    then and now), or a mesh point: `p` and `n` in MODEL space (mm, CAD Z-up —
    the STL's own coordinates, never the viewer's display frame), `tri` as a
    witness valid only while the mesh blob is unchanged, and the camera pose
    the reviewer was looking through. `status.outdated` says whether the head
    still shows what the comment was about."""
    with db.connect() as conn:
        try:
            return _ok(thread=threads.get(conn, thread_id))
        except threads.ThreadError as exc:
            return _err(str(exc))


def open_thread(body: str, proposal: int = 0, release: int = 0,
                anchor_kind: str = "subject", path: str = "", line: int = 0,
                key: str = "", p: list[float] | None = None,
                n: list[float] | None = None, sha: str = "",
                suggest_value: str | None = None, suggest_text: str | None = None,
                blocking: bool = False, title: str = "",
                actor: str = "mcp") -> dict:
    """Open a review thread. Anchors: `subject` (the proposal/release), `line`
    (path + line at the head, or `sha=` of the base for a deleted line), `key`
    (a plm.toml key — attributes.<n>, bom[<path>].qty, files.primary,
    pin:<path>), `mesh` (path + `p` + `n`, MODEL space mm Z-up). A suggested
    change rides on a key (`suggest_value`) or a line (`suggest_text`); the
    author applies it with one call. A bot's thread is advisory: `blocking` is
    refused for agents — put the concern in front of a human instead."""
    anchor: dict = {"kind": anchor_kind}
    if sha:
        anchor["sha"] = sha
    if anchor_kind in ("line", "mesh", "image"):
        anchor["path"] = path
    if anchor_kind == "line":
        anchor["line"] = line
    if anchor_kind == "key":
        anchor["key"] = key
    if anchor_kind == "mesh":
        anchor.update(p=p, n=n)
    sug = None
    if suggest_value is not None:
        sug = {"value": suggest_value}
    elif suggest_text is not None:
        sug = {"text": suggest_text}
    st, sid = ("release", release) if release else ("proposal", proposal)
    with db.connect() as conn:
        try:
            out = threads.open_thread(conn, subject_type=st, subject_id=sid,
                                      body=body, actor=actor, anchor=anchor,
                                      title=title, blocking=blocking,
                                      suggestion=sug)
        except threads.ThreadError as exc:
            return _err(str(exc))
    return _ok(thread=_deliver(out))


def reply_thread(thread_id: int, body: str, actor: str = "mcp") -> dict:
    """Answer a reviewer on their thread — what you changed, or why not."""
    with db.connect() as conn:
        try:
            return _ok(thread=threads.reply(conn, thread_id, body=body,
                                            actor=actor))
        except threads.ThreadError as exc:
            return _err(str(exc))


def resolve_thread(thread_id: int, addressed_by: str = "", reason: str = "",
                   actor: str = "mcp") -> dict:
    """Resolve a thread — with `addressed_by=<sha>`, the commit that fixed it
    (it must be in the proposal's head). On a thread somebody else raised this
    records your CLAIM (state stays open, `addressed_by` set) and the raiser or
    an approver decides: bots propose, humans decide. When the last open
    thread is addressed, the reviewer is notified."""
    with db.connect() as conn:
        try:
            out = threads.resolve(conn, thread_id, actor=actor, reason=reason,
                                  addressed_by=addressed_by)
        except threads.ThreadError as exc:
            return _err(str(exc))
    out = _deliver(out)
    claimed = out["state"] == "open"
    return _ok(thread=out, resolved=not claimed, claimed=claimed,
               note=("recorded as ADDRESSED — "
                     f"{out['author']} (or an approver) resolves it")
               if claimed else "resolved")


def apply_suggestion(thread_id: int, actor: str = "mcp") -> dict:
    """Apply a reviewer's suggested change: ONE commit on the draft branch,
    through the same write path a push meets, and the thread resolves with
    that commit. Pull before you push again — the branch moved."""
    with db.connect() as conn:
        try:
            out = threads.apply_suggestion(conn, thread_id, actor=actor)
        except threads.ThreadError as exc:
            return _err(str(exc))
    return _ok(**_deliver(out))


def list_proposals(number: str = "", state: str = "draft,open,rejected") -> dict:
    """Proposals (#n — pull requests that survive review rounds), newest
    activity first, each with its open / blocking thread counts and its page
    URL. `state` csv: draft, open, approved, merged, rejected, withdrawn."""
    with db.connect() as conn:
        try:
            rows = proposals.listing(conn, item=number,
                                     states=[s for s in state.split(",") if s])
        except LookupError as exc:
            return _err(str(exc))
    return _ok(proposals=[{k: r.get(k) for k in (
        "id", "item_number", "version", "state", "head_ref", "head_sha",
        "target_maturity", "author", "title", "threads", "url", "change_id")}
        for r in rows])


def get_proposal(proposal: int) -> dict:
    """One proposal: its rounds (approvals), its reviews — each bound to the
    sha it looked at, with `dismissed_at` when a later push made it stale —
    the head, `refs/plm/proposals/<n>/head`, and thread counts."""
    with db.connect() as conn:
        try:
            P = proposals.get(conn, proposal)
        except LookupError as exc:
            return _err(str(exc))
        P["threads"] = threads.counts(conn, [("proposal", proposal)]).get(
            ("proposal", proposal), threads.ZERO)
    return _ok(proposal=P)


TOOLS = (list_threads, get_thread, open_thread, reply_thread, resolve_thread,
         apply_suggestion, list_proposals, get_proposal)


def install(mcp) -> None:
    from plm import gitmode
    if not gitmode.any_serves():
        return
    for fn in TOOLS:
        mcp.tool(fn)
    # the loop is part of the git-first instructions now (stage 14 folded it
    # into plm_mcp/instructions.py); nothing is appended at runtime
