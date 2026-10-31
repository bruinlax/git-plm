"""MCP tools for PULL REQUESTS (CHG-n, the legacy label) and RELEASE SETS (stage 14) — the agent's side of
"the agent is asking me to approve a pull request to main for the project".

Imported by ONE hook line in `plm_mcp/server.py` (`tools_changes.install(mcp)`).
Thin callers of `plm.ops` / `plm/changes.py` / `plm/releasesets.py` — the same
functions the HTTP routes call. Registered only where git-first is on somewhere
(`gitmode.any_serves()`), so an `off` instance's tool list is master's.

THE LOOP an agent runs (also in the instructions):
  push the first repo with `-o pr=new` (or open_change(title)) -> push
  every other repo with `-o pr=<n>` (or add_to_change) -> get_change
  (impact, warnings) -> draft_dispositions -> submit_change -> tell the human
  its `say` line -> WAIT for a human.
  `actor` on every tool here is the MCP session's token actor when the request
  carries one (plm_mcp/server.py, stage 15); pass it only without a token.
  Then request_release(number) on the assembly: one RELEASE SET for its
  unreleased closure, attested where you can prove it, decided by a human.
"""
from __future__ import annotations

from plm import actions, changes, db, ops, releasesets


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _deliver(out: dict) -> dict:
    """"Needs your attention" events go out through `actions.dispatch` (→
    `actions.send()`, the one wire; dry-run in the POC) from this door too."""
    for ev in out.pop("_events", None) or []:
        actions.dispatch(ev[0], ev[1])
    out.pop("_event", None)
    return out


def _run(fn):
    """One transaction per tool call — and a REFUSAL ROLLS IT BACK. (Caught
    inside the `with`, an error would otherwise let the connection commit
    whatever the verb did before it refused: a pull request half-submitted.)"""
    with db.connect() as conn:
        try:
            out = fn(conn)
        except (changes.ChangeError, releasesets.SetError, ops.OpsError) as exc:
            conn.rollback()
            return _err(str(exc))
    return _ok(**_deliver(out))


def whoami(project: str = "") -> dict:
    """WHO YOU ARE TO PLM — call it first, before any write. `actor` is the
    token's actor (`via: token`), never an argument; `say` is one line.
    STOP if `actor` is not you or `ok` is false. Also: instance, git modes,
    LFS, and the workspace git wrapper (`tools/git/git.sh --check`).

    `project`: the project you are about to write to — `git_mode` is then
    ITS mode on this instance and `routing` says where git-first projects
    live (`gitfirst_instance`). STOP if the project's listed home is a
    different instance."""
    from plm import config, gitmode, gitrepo, identity, lfs, routing, split
    from plm import tokens as tokenstore
    from plm_mcp import server as srv
    who = srv._session_actor()
    out: dict = {"actor": who or None, "via": "token" if who else "none",
                 "auth_mode": identity.AUTH_MODE}
    with db.connect() as conn:
        user = db.get_user(conn, who) if who else None
        modes = {}
        for r in conn.execute("SELECT DISTINCT project FROM items "
                              "WHERE project<>'' ORDER BY project"):
            modes.setdefault(gitmode.mode_for(r[0], conn), []).append(r[0])
        route = routing.for_project(conn, project)
    if who and user:
        out["user"] = {"kind": user["kind"], "roles": user["roles"],
                       "display": user["display"]}
        out["can_approve"] = "approver" in [
            x.strip() for x in (user["roles"] or "").split(",")]
        out["ok"] = True
        out["say"] = (f"You are {who} ({user['kind']}, {user['roles'] or 'no roles'}) "
                      f"via your token on {config.LABEL or 'plm'} — every "
                      "write and push is attributed to {who}. If that is not "
                      "you, STOP and say so.").replace("{who}", who)
    elif who:
        out["ok"] = False
        out["say"] = (f"Your token maps to {who!r}, but this instance has NO "
                      "such user: every write will be refused. STOP — the "
                      "operator must add the user (GET /auth/whoami says how).")
    else:
        out["ok"] = False
        out["say"] = ("No token on this MCP session: writes are attributed to "
                      "whatever `actor` a tool is given, or refused. STOP and "
                      "set the `Authorization: Bearer ${PLM_TOKEN}` header in "
                      "the workspace .mcp.json.")
    try:
        lay = split.layout()
    except Exception:                                          # noqa: BLE001
        lay = {}
    out["instance"] = {"label": config.LABEL, "web": config.PUBLIC_URL,
                       "clone_base": gitrepo.CLONE_BASE,
                       "tokens": tokenstore.describe().get("actors", [])}
    # stage 20: `git_mode` is ONE mode — the project's when one is named, else
    # this instance's default; the per-project map is `git_modes`
    out["git_mode"] = route["git_mode"]
    out["git_modes"] = modes
    out["gitfirst_instance"] = route["gitfirst_instance"]
    out["routing"] = route
    out["lfs"] = {"on": bool(lay.get("lfs")),
                  "threshold_bytes": lfs.threshold(),
                  "say": "meshes are Git LFS objects: clone through the "
                         "workspace wrapper (or GIT_LFS_SKIP_SMUDGE=1 for "
                         "text only)" if lay.get("lfs") else
                         "new meshes become LFS objects when pushed"}
    out["wrapper"] = {"path": "tools/git/git.sh", "image": "ws-git",
                      "check": "tools/git/git.sh --check",
                      "say": "run the check once per session (from the "
                             "workspace): it reports the wrapper, the ws-git "
                             "image, git-lfs and this token's actor, and "
                             "builds nothing"}
    return out


def open_change(title: str, body: str = "", project: str = "",
                actor: str = "mcp") -> dict:
    """Open a PULL REQUEST (#n; CHG-n is the legacy label) — ONE piece of
    work, however many repos it touches. Then push each repo's draft with
    `git push -o pr=<n> …`
    (or add_to_change), then submit_change, then WAIT: a human approves.

    Shortcut: the first push may open it — `git push -o pr=new -o
    title="…" origin HEAD:draft/<topic>` prints `plm: pull request #n (CHG-n, …)`."""
    def go(conn):
        c = changes.create(conn, title=title, body=body, project=project,
                           actor=actor, origin="mcp")
        return {"change": changes.brief(conn, c),
                "next": f"git push -o pr={c['id']} origin "
                        "HEAD:draft/<topic>   (each repo), then "
                        f"submit_change('{changes.label(c['id'])}')"}
    return _run(go)


def add_to_change(change: str, number: str = "", ref: str = "",
                  proposal: int = 0, actor: str = "mcp") -> dict:
    """Put a proposal into a pull request: by `proposal` (#n), or by item `number`
    and its draft branch `ref` (e.g. 'draft/v0.9.0'). One proposal per item per
    change; a proposal is in at most one pull request."""
    def go(conn):
        pid = proposal
        if not pid:
            from plm.changesapi import MemberReq, _member_pid
            pid = _member_pid(conn, MemberReq(number=number, ref=ref))
        return {"change": changes.add_member(conn, change, pid, actor=actor,
                                             origin="mcp")}
    return _run(go)


def submit_change(change: str, note: str = "", target_state: str = "",
                  actor: str = "mcp", explode_waiver: str = "",
                  wait: float = 90) -> dict:
    """Submit every member not yet in review and ask the human ONCE
    (`review.requested`, subject: the pull request, with the page link).

    The answer LEADS with `say` — "Pull request #n is waiting for your approval: <url>"
    — the one line to tell the human; then `url`, `warnings` (what a reviewer
    will see in amber: a pre-existing explode failure, checks that warn) and
    `next` (draft_dispositions when affected items need one). Then STOP and
    WAIT — do not decide it yourself. `change` holds the members, the impact
    panel (parents that pin what you changed, and items that still carry a
    source file you changed — both NOT in the pull request) and `preflight`.

    explode_waiver: only for a REGRESSION you mean — in a `write` project an
    assembly that did not explode before your pull request is a warning, not a
    refusal. Note: say what you did and what you did NOT do (e.g. "needs
    re-slice").

    It WAITS for the members' checks first (up to `wait` seconds, default
    90): no need to sleep or poll before calling it. Anything still running
    then is named in `warnings` / `checks_pending`."""
    changes.settle(change, wait=wait)

    def go(conn):
        out = ops.submit_change(conn, change, actor=actor, note=note,
                                target_state=target_state,
                                explode_waiver=explode_waiver)
        v = out.pop("change")
        out["change"] = {k: v[k] for k in ("change", "members", "impact",
                                           "threads", "preflight", "policy",
                                           "checks")}
        return out
    return _run(go)


def get_change(change: str, compact: bool = True,
               timeline: bool = False) -> dict:
    """The pull request: members in merge order with heads, review baselines,
    re-pins, checks rollup and `warnings`; `impact` (affected items NOT in
    the pull request, with dispositions) and `impact_coverage`; `preflight`.
    compact=true (default): no timeline, no per-member threads/policy, no
    signoff bodies. compact=false or timeline=true: everything the page has."""
    def go(conn):
        v = changes.view(conn, change)
        if not compact:
            return v
        mkeys = ("proposal", "item", "version", "state", "title", "head_ref",
                 "head_sha", "review_base", "target", "order", "after",
                 "repin", "problems", "checks", "warnings", "url")
        out = {"change": {k: v["change"].get(k) for k in (
                   "number", "title", "state", "author", "project",
                   "merge_state", "url")},
               "members": [{k: m.get(k) for k in mkeys} for m in v["members"]],
               "heads": v["heads"], "impact": v["impact"],
               "impact_coverage": v["impact_coverage"],
               "preflight": v["preflight"], "policy": v["policy"],
               "reviews": [{k: r.get(k) for k in ("reviewer", "verdict",
                                                  "dismissed_at")}
                           for r in v["reviews"]],
               "threads": v["threads"],
               # the pull request's own checks (physical-diff): one line each;
               # `images` in the full form, or get_checks('CHG-n')
               "checks": [{k: r.get(k) for k in ("name", "status", "summary")}
                          for r in v["checks"]["checks"]],
               "omitted": "timeline, per-member threads/policy, review bodies,"
                          " check images — get_change(change, compact=false)"}
        for m in out["members"]:
            rb = m.get("review_base") or {}
            m["review_base"] = {k: rb.get(k) for k in ("sha", "label", "how",
                                                      "range")}
        if timeline:
            out["timeline"] = v["timeline"]
        return out
    return _run(go)


def list_changes(state: str = ",".join(changes.OPEN), project: str = "") -> dict:
    """Changes, newest first. `state` csv: draft, open, merged, rejected,
    withdrawn."""
    return _run(lambda conn: {"changes": changes.listing(
        conn, states=[s for s in state.split(",") if s], project=project)})


def draft_dispositions(change: str, actor: str = "mcp",
                       render: bool = False) -> dict:
    """Draft a disposition, with the facts as the note, for every affected
    item NOT in the pull request (a parent pinning a changed item; an item sharing
    or drifting from a changed source). A human confirms. `render=true` also
    renders each SOURCE-DRIFT item's own selector against both copies and
    drafts `unchanged_geometry` when identical (slow: two renders each)."""
    return _run(lambda conn: {"drafted": changes.draft_dispositions(
        conn, change, actor=actor, render=render), "impact": changes.impact(
        conn, changes.parse_id(change))})


def set_disposition(change: str, parent: str, disposition: str, note: str,
                    actor: str = "mcp") -> dict:
    """Record one disposition, with WHY: bump | use_as_is | no_impact — and,
    for an item carrying a different copy of a source: unchanged_geometry |
    historical."""
    return _run(lambda conn: {"impact": changes.set_disposition(
        conn, change, parent, disposition, note=note, actor=actor)})


def get_release_set(release_set: int) -> dict:
    """A release set: the closure tree (members bottom-up, already-released
    pins, BLOCKERS with the pull request that must merge first), each member's
    checklist / checks / evidence, the per-line view for bulk attestation, and
    the pre-flight answer."""
    return _run(lambda conn: releasesets.view(conn, release_set))


def tool_help(name: str) -> dict:
    """The FULL description of one tool (tools/list carries a concise one
    on this instance) — its argument notes, examples and caveats."""
    import inspect
    from plm_mcp import server as srv
    fn = getattr(srv, name, None) or globals().get(name)
    if fn is None:
        try:
            from plm_mcp import tools_review
            fn = getattr(tools_review, name, None)
        except Exception:                                      # noqa: BLE001
            fn = None
    fn = getattr(fn, "fn", fn)
    doc = inspect.getdoc(fn) if fn is not None else None
    if not doc:
        return {"ok": False, "error": f"no tool named {name!r}"}
    return {"ok": True, "name": name, "description": doc}


TOOLS = (whoami, tool_help, open_change, add_to_change, submit_change, get_change, list_changes,
         draft_dispositions, set_disposition, get_release_set)


def install(mcp) -> None:
    from plm import gitmode
    if not gitmode.any_serves():
        return
    for fn in TOOLS:
        mcp.tool(fn)
