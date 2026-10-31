"""whoami on an `off` instance (stage 20).

The git-first MCP has had `whoami()` since stage 19 (plm_mcp/tools_changes.py,
registered only where git is on somewhere). An instance where git is off
EVERYWHERE — production today — had nothing to ask before the first write, and
that is where a project's correction went down the legacy path unseen.

Registered only when PLM_GITFIRST_INSTANCE_URL is set, so an unconfigured
`off` instance's tool list stays exactly master's (the compatibility harness).
"""
from __future__ import annotations

from plm import db


def whoami(project: str = "") -> dict:
    """WHO YOU ARE AND WHERE YOU ARE — call it before the first write.
    `actor` is your token's actor (`via: token`), never an argument. With
    `project`: that project's `git_mode` HERE and `gitfirst_instance` — where
    git-first projects live. This instance is the LEGACY path (git off): if
    the project's listed home (the workspace's GIT-FIRST.md table) is the
    git-first instance, or the project is NEW, STOP and tell the human."""
    from plm import config, identity, routing
    from plm_mcp import server as srv
    who = srv._session_actor()
    with db.connect() as conn:
        user = db.get_user(conn, who) if who else None
        route = routing.for_project(conn, project)
    out = {"actor": who or None, "via": "token" if who else "none",
           "auth_mode": identity.AUTH_MODE,
           "ok": bool(who and user),
           "instance": {"label": config.LABEL, "web": config.PUBLIC_URL,
                        "git": "off everywhere"},
           "git_mode": route["git_mode"],
           "gitfirst_instance": route["gitfirst_instance"],
           "routing": route, "say": route["say"]}
    if project:
        out["project"] = project
        out["project_exists"] = route.get("project_exists", False)
        if not out["project_exists"]:
            out["say"] += (f". {project!r} does not exist here: a NEW project "
                           "belongs on the git-first instance")
    if who and not user:
        out["say"] = (f"Your token maps to {who!r}, which is not a user here — "
                      "writes will be refused. " + out["say"])
    return out


def install(mcp) -> None:
    from plm import gitmode, routing
    if gitmode.any_serves() or not routing.gitfirst_url():
        return
    mcp.tool(whoami)
