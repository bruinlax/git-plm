"""What is PENDING on a repo, a project, or the viewer — GitHub's banners.

Geoff, 2026-10-16: "on the front end for the site we should have a
notification / banner similar to github when i come to the core repo right?"

Two kinds of pending thing, the two GitHub shows on a repo home:

  change   a draft branch of this item is a member of an OPEN (or draft, or
           refused) change — "CHG-380 is waiting for your approval — Review",
           or, for somebody the policy does not let decide, "CHG-380 is in
           review (waiting on geoff)".
  draft    a `draft/*` branch was pushed and is in NO change — GitHub's yellow
           "had recent pushes — Compare & pull request". Every push of a draft
           branch opens a proposal (`proposals.on_head`), so "a branch with no
           proposal" does not arise; a proposal with `change_id IS NULL` is it.

Read-only, and the ONE place the "may this viewer decide?" answer for a change
is computed outside the decision itself: it asks `policy.may_approve` for every
member's (project, target) — the same function `ops.decide_change` asks — so
the banner can never offer a button the decision would refuse on policy.
"""
from __future__ import annotations

from . import changes, checks, db, policy, proposals, release

#: `?state=` on the item/project endpoints, over `changes.OPEN` — the one
#: definition. The banners and the tab's count read `open`; the Pull requests
#: tab reads `all` when it is opened, and splits it on each row's `open`.
STATES = {"open": changes.OPEN, "closed": changes.CLOSED, "all": changes.STATES}
#: the newest pull requests one item/project page lists
TOUCHING_LIMIT = 200


def approvers(conn, spec: dict) -> list[str]:
    """Who the policy for one (project, target) waits on, by name."""
    if spec.get("auto"):
        return ["policy"]
    kinds = tuple(spec.get("approver_kinds") or ("human",))
    out = []
    for u in conn.execute("SELECT username, kind, roles FROM users ORDER BY username"):
        if u["kind"] not in kinds:
            continue
        roles = [r.strip() for r in (u["roles"] or "").split(",") if r.strip()]
        if u["kind"] == "human" and "approver" not in roles:
            continue
        out.append(u["username"])
    return out


def _member_checks(conn, m: dict) -> str:
    """The member head's checks rollup state — the dot on a list row. From
    the STORED rows only: the runner writes every applicable row (queued)
    before the first one runs, so the rows ARE the applicable set, and a list
    page never opens a repo to draw a dot."""
    if not m["head_sha"]:
        return ""
    return checks.rollup(checks.read(conn, m["item_number"], m["head_sha"]),
                         target_maturity=release.maturity_of(m["target"]))["state"]


def push_line(title: str, branch: str) -> str:
    """The agent's way to open a pull request for a draft branch — what the
    UI's "Open a pull request" does by API, as the push it would type."""
    t = (title or branch)[:80].replace('"', "'")
    return f'git push -o pr=new -o title="{t}" origin {branch}'


def change_entry(conn, c: dict, viewer: str = "",
                 mem: list[dict] | None = None) -> dict:
    """One pull request as a banner / bell / list row, with the viewer's
    answer — THE row shape (`changes.listing` is built from it too)."""
    mem = changes.members(conn, c["id"]) if mem is None else mem
    waiting, why = set(), ""
    can = bool(viewer) and c["state"] == "open" and bool(mem)
    for m in mem:
        spec = policy.for_target(m["item_project"] or "", m["target"], conn=conn)
        waiting.update(approvers(conn, spec))
        if can:
            ans = policy.may_approve(conn, m["item_project"] or "", m["target"], viewer)
            if not ans["ok"]:
                can, why = False, ans["why"]
    if c["state"] != "open":
        why = (f"{changes.title_ref(c['id'])} is a draft — not submitted for review yet"
               if c["state"] == "draft" else
               f"{changes.title_ref(c['id'])} was refused — the next push to its "
               "branches re-opens it" if c["state"] == "rejected" else
               f"{changes.title_ref(c['id'])} is {c['state']}")
    elif not viewer:
        why = "sign in to decide"
    return {
        "kind": "change", **changes.names(c["id"]),
        "open": c["state"] in changes.OPEN,
        "created_at": c.get("created_at"), "decided_at": c.get("decided_at"),
        "title": c["title"], "state": c["state"], "project": c["project"],
        "author": c["author"], "auto": bool(c["auto"]),
        "at": c.get("submitted_at") or c.get("updated_at"),
        "submitted_at": c.get("submitted_at"), "updated_at": c.get("updated_at"),
        "can_decide": can, "why_not": "" if can else why,
        "waiting_on": sorted(waiting),
        "members": [{"item": m["item_number"], "name": m["item_name"],
                     "kind": m.get("item_kind") or "",
                     "version": m["version"], "proposal": m["id"],
                     "label": proposals.label(m["id"]),
                     "state": m["state"], "target": m["target"],
                     "head_ref": (m["head_ref"] or "").replace("refs/heads/", ""),
                     "merged_tag": m["merged_tag"] or "",
                     "checks": _member_checks(conn, m),
                     "page": proposals.page(m["item_number"], m["id"])}
                    for m in mem],
    }


def _changes_touching(conn, item_ids: list[int], states) -> list[dict]:
    if not item_ids:
        return []
    q = ",".join("?" * len(item_ids))
    return [dict(r) for r in conn.execute(
        f"SELECT DISTINCT c.* FROM changes c JOIN proposals p ON p.change_id=c.id "
        f"WHERE p.item_id IN ({q}) AND c.state IN ({','.join('?' * len(states))}) "
        "ORDER BY COALESCE(c.submitted_at, c.updated_at) DESC LIMIT ?",
        (*item_ids, *states, TOUCHING_LIMIT))]


def _loose_drafts(conn, item_ids: list[int]) -> list[dict]:
    """Draft proposals in no change. A draft whose head is ALSO the head of a
    proposal that is in a change, or that merged, is the same work under a
    second branch name (`draft/v0.2.1` beside `draft/usb-correction`) — not
    something to open a change for — and is left out."""
    if not item_ids:
        return []
    q = ",".join("?" * len(item_ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT p.*, i.number AS item_number, i.name AS item_name, "
        f"i.project AS item_project FROM proposals p JOIN items i ON i.id=p.item_id "
        f"WHERE p.item_id IN ({q}) AND p.state='draft' AND p.change_id IS NULL "
        "ORDER BY p.updated_at DESC", tuple(item_ids))]
    covered = {r[0] for r in conn.execute(
        f"SELECT head_sha FROM proposals WHERE item_id IN ({q}) AND head_sha!='' "
        "AND (change_id IS NOT NULL OR state IN ('merged','approved'))",
        tuple(item_ids))}
    covered |= {r[0] for r in conn.execute(
        f"SELECT merged_sha FROM proposals WHERE item_id IN ({q}) AND merged_sha!=''",
        tuple(item_ids))}
    out = []
    for p in rows:
        if not p["head_ref"] or p["head_sha"] in covered:
            continue
        branch = p["head_ref"].replace("refs/heads/", "")
        rb = proposals.baseline(p, p["item_number"])
        out.append({
            "kind": "draft", "proposal": p["id"], "item": p["item_number"],
            "name": p["item_name"], "project": p["item_project"],
            "version": p["version"], "branch": branch, "head_sha": p["head_sha"],
            "title": p["title"], "author": p["author"],
            "pushed_at": p["updated_at"], "at": p["updated_at"],
            "page": proposals.page(p["item_number"], p["id"]),
            "base": rb.get("label") or "", "base_sha": rb.get("sha") or "",
            "push": push_line(p["title"], branch),
        })
    return out


def for_items(conn, item_ids: list[int], viewer: str = "",
              state: str = "open") -> dict:
    proposals.ensure(conn)
    changes.ensure(conn)
    if state not in STATES:
        raise LookupError(f"state is one of {', '.join(STATES)}, not {state!r}")
    return {"viewer": viewer or "", "state": state,
            "changes": [change_entry(conn, c, viewer)
                        for c in _changes_touching(conn, item_ids, STATES[state])],
            "drafts": _loose_drafts(conn, item_ids)}


def for_item(conn, number: str, viewer: str = "", state: str = "open") -> dict:
    item = db.resolve_item(conn, number)
    if not item:
        raise LookupError(f"unknown item: {number}")
    return {"number": item["number"],
            **for_items(conn, [item["id"]], viewer, state)}


def for_project(conn, project: str, viewer: str = "", state: str = "open") -> dict:
    ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE project=?",
                                      (project,))]
    return {"project": project, **for_items(conn, ids, viewer, state)}


def release_set_can_decide(conn, viewer: str) -> bool:
    """A release is always a human's (PLM_REVIEW_MODEL §2.1b)."""
    u = db.get_user(conn, viewer) if viewer else None
    return bool(u and u["kind"] == "human" and db.user_has_role(conn, viewer, "approver"))
