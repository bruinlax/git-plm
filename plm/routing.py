"""Routing — is this the instance this project's writes belong on? (stage 20)

A Codex agent corrected a project through the legacy
tools on production (`PLM_GIT_MODE=off`) and produced three silent DRAFTS where
the operator expected branch / commit / push / change / approval. The agent followed
its instructions correctly: new projects defaulted to the instance where git is
off, and nothing before the first write said so. This module is the one place
that says it.

    PLM_GITFIRST_INSTANCE_URL   on an `off` instance: where git-first projects
                                live (production -> staging until step 3)

`line()` is THE sentence (MCP instructions, get_item, create_item, whoami all
print the same one); `for_project()` is the structured answer whoami returns.
An instance that serves git for a project returns no line for it: the routing
question only exists where the legacy path is about to be taken.
"""
from __future__ import annotations

from . import config


def gitfirst_url() -> str:
    """The configured git-first instance, '' when none is configured."""
    return config.GITFIRST_INSTANCE_URL


def gitfirst_instance(conn=None) -> str:
    """Where git-first projects live, as THIS instance knows it: the configured
    URL, else this instance itself when it serves git anywhere."""
    if config.GITFIRST_INSTANCE_URL:
        return config.GITFIRST_INSTANCE_URL
    from . import gitmode
    try:
        return config.PUBLIC_URL if gitmode.any_serves(conn) else ""
    except Exception:  # noqa: BLE001 — a routing hint never fails a call
        return ""


def line() -> str:
    """The ONE line, or '' when no git-first instance is configured."""
    url = gitfirst_url()
    if not url:
        return ""
    return (f"git_mode: off — this project is on the legacy path; git-first "
            f"projects live on {url}; a new project should be created THERE "
            "unless the human says otherwise")


def _mode(conn, project: str) -> str:
    from . import gitmode
    return gitmode.mode_for(project or "", conn)


def project_exists(conn, project: str) -> bool:
    return bool(project) and bool(conn.execute(
        "SELECT 1 FROM items WHERE project=? LIMIT 1", (project,)).fetchone())


def for_item(conn, item: dict) -> dict | None:
    """The `routing` block for get_item: only for an item on the legacy path
    of an instance that knows where git-first lives."""
    if not gitfirst_url() or _mode(conn, item.get("project") or "") != "off":
        return None
    return {"git_mode": "off", "instance": config.PUBLIC_URL,
            "gitfirst_instance": gitfirst_url(), "say": line()}


def for_create(conn, project: str) -> dict | None:
    """The `routing` block for create_item, computed BEFORE the item exists:
    a NEW project created on the legacy path gets the warning (no refusal —
    a bot that was told to work here must still be able to)."""
    if not gitfirst_url() or _mode(conn, project) != "off":
        return None
    new = not project_exists(conn, project)
    out = {"git_mode": "off", "instance": config.PUBLIC_URL,
           "gitfirst_instance": gitfirst_url(), "new_project": new,
           "say": line()}
    if new:
        out["warning"] = (
            f"NEW PROJECT {project!r} was just created on {config.PUBLIC_URL}, "
            f"the legacy path. Git-first projects live on {gitfirst_url()}. "
            "Unless the human told you to use this instance, STOP, tell them, "
            "and create it there instead (its items here can be carried with "
            "`python -m plm.carry`).")
    return out


def for_project(conn, project: str = "") -> dict:
    """whoami's answer: this instance, the project's mode HERE, and where
    git-first projects live — plus the one-line verdict."""
    mode = _mode(conn, project)
    gf = gitfirst_instance(conn)
    out = {"instance": config.PUBLIC_URL, "label": config.LABEL,
           "git_mode": mode, "gitfirst_instance": gf or None}
    if project:
        out["project"] = project
        out["project_exists"] = project_exists(conn, project)
    if mode == "off" and gitfirst_url():
        out["say"] = line()
    elif mode == "off":
        out["say"] = ("git_mode: off — the legacy path; this instance names no "
                      "git-first instance (PLM_GITFIRST_INSTANCE_URL)")
    else:
        out["say"] = (f"git_mode: {mode} — "
                      + ("push drafts and open a change here"
                         if mode == "write" else
                         "repos follow the registry; write through the tools")
                      + (f" ({project})" if project else ""))
    return out
