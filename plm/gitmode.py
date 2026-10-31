"""PLM_GIT_MODE — how far git-first is switched on, per project (stage 9).

WHY THIS EXISTS
---------------
`master` (the DB-era code in production) and `git-first` drifted for two weeks
while the switchover waited for a free day. The risk to "git-based" was never the
migration (rehearsed: 24 s, deterministic, rollback proven); it was two lines of
development. So the merged code ships to production FIRST, with git-first
switched OFF, and is turned on one step at a time:

  off    reads and writes exactly as `master` does. No repo is required — a
         deployment with an empty `repos/` serves everything master serves.
         This is the mode the merged code first ships in.
  read   + the git surface (repo page, ref dropdown, tree, compare, checks,
         `git clone`) over repos that FOLLOW legacy writes (`plm/gitsync.py`).
         Writes still take the legacy path; a `git push` is refused, teachably.
  write  the full product: a design write is a commit, the indexer is the only
         writer of design rows, policy decides merges. Items cross over at
         their first revise (stage 3b's rule).

ONE QUESTION, ONE HOME. Every fork in behaviour asks `mode_for()` (or one of
the three predicates below) — there is no `os.environ["PLM_GIT_MODE"]` read
anywhere else, and if you are about to write one, call this instead.

The mode is the per-PROJECT permission; `ops.git_origin` stays the per-ITEM
truth. `write` lets an item cross over at its next revise; it does not move
anything by itself. And going back from `write` to `read`/`off` strands nothing
silently: a write to a revision that already lives in git is REFUSED with the
reason (`ops._stranded`) rather than forked into the registry behind git's back.

WHERE A SETTING LIVES (most specific wins — the same layering plm/policy.py
uses, in the same table, because that table is already the one home of
per-project behaviour):

  1. `DEFAULT` below — the registry default, `off`.
  2. `PLM_GIT_MODE` — the instance (env).
  3. the `policy` table, row (project='', target_state='') — every project.
  4. the `policy` table, row (project=<p>, target_state='') — one project.

Written with `python -m plm.gitmode --project <p> --mode read`, read at
`GET /api/gitmode`. Like the approval policy there is no HTTP write route:
switching a project's storage model is an operator's decision.

DECOUPLED FROM AUTH, ON PURPOSE. `PLM_AUTH_MODE` (plm/identity.py) says who may
write; this says where a write goes. Production today is OIDC for the UI with
the API open on the private network; that must stay expressible in every git mode, so
neither module reads the other's variable.
"""
from __future__ import annotations

import argparse
import os

MODES = ("off", "read", "write")

#: The registry default. `off` is what makes "deploy the merged code" a step
#: with no behaviour change.
DEFAULT = "off"

#: What a person reads next to the mode, in the API and on the repo page.
LABEL = {
    "off": "git-first is off for this project: the registry reads and writes "
           "as it always has, and no repository is served.",
    "read": "git is READ-ONLY for this project: the repositories follow the "
            "registry's writes (they are regenerated after each one); writes "
            "go through plm's API, MCP and UI.",
    "write": "git-first: every design write is a commit, and a `git push` is a "
             "proposal.",
}


class ModeError(ValueError):
    pass


def _norm(v: str) -> str:
    v = (v or "").strip().lower()
    if v and v not in MODES:
        raise ModeError(f"git mode must be one of {', '.join(MODES)} (got {v!r})")
    return v


def _env() -> str:
    try:
        return _norm(os.environ.get("PLM_GIT_MODE", ""))
    except ModeError:
        # A typo in the env must not silently turn git ON. The strict answer is
        # the default — and `explain()` shows the bad value, so it is visible.
        return ""


def _rows(conn, project: str) -> dict:
    """{'': mode, '<project>': mode} from the policy table, '' when unset."""
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT project, git_mode FROM policy WHERE target_state='' "
            "AND (project=? OR project='')", (project or "",)).fetchall()
    except Exception:  # noqa: BLE001 — a DB that predates the column: inherit
        return {}
    out = {}
    for r in rows:
        try:
            m = _norm(r["git_mode"])
        except ModeError:
            m = ""
        if m:
            out[r["project"] or ""] = m
    return out


def explain(project: str = "", conn=None) -> dict:
    """`{mode, source, project, label}` — the answer AND where it came from,
    because "why is the repo page missing?" has to be answerable."""
    mode, source = DEFAULT, "registry"
    env = _env()
    if env:
        mode, source = env, f"env:PLM_GIT_MODE={env}"
    elif os.environ.get("PLM_GIT_MODE", "").strip():
        source = (f"registry (PLM_GIT_MODE={os.environ['PLM_GIT_MODE']!r} is "
                  "not a mode and was ignored)")
    own = conn is None
    if own:
        conn = _connect()
    try:
        rows = _rows(conn, project)
    finally:
        if own and conn is not None:
            conn.close()
    if rows.get(""):
        mode, source = rows[""], "db:*"
    if project and rows.get(project):
        mode, source = rows[project], f"db:{project}"
    return {"mode": mode, "source": source, "project": project or "",
            "label": LABEL[mode]}


def mode_for(project: str = "", conn=None) -> str:
    """THE resolver: `off` | `read` | `write` for one project."""
    return explain(project, conn)["mode"]


def _project_of(conn, item) -> str:
    """An item row/dict, a revision dict, or a number → its project."""
    if item is None:
        return ""
    if isinstance(item, str):
        r = conn.execute("SELECT project FROM items WHERE number=?",
                         (item,)).fetchone() if conn is not None else None
        return (r["project"] if r else "") or ""
    keys = item.keys() if hasattr(item, "keys") else ()
    if "project" in keys:
        return item["project"] or ""
    if "item_id" in keys and conn is not None:
        r = conn.execute("SELECT project FROM items WHERE id=?",
                         (item["item_id"],)).fetchone()
        return (r["project"] if r else "") or ""
    if "number" in keys and conn is not None:
        return _project_of(conn, str(item["number"]))
    return ""


def for_item(conn, item) -> str:
    return mode_for(_project_of(conn, item), conn)


def writes_git(conn, item) -> bool:
    """May a write to this item go to git (commit, crossover, policy merge)?"""
    return for_item(conn, item) == "write"


def serves_git(conn, item) -> bool:
    """Is the git surface (repo routes, clone, checks board) on for this item?"""
    return for_item(conn, item) in ("read", "write")


def any_serves(conn=None, modes: tuple = ("read", "write")) -> bool:
    """Is git on ANYWHERE on this instance? (startup: the checks runner, the
    MCP's git paragraph; with `modes=("read",)`, the sync worker.)"""
    if _env() in modes:
        return True
    own = conn is None
    if own:
        conn = _connect()
    try:
        if conn is None:
            return False
        try:
            return conn.execute(
                "SELECT 1 FROM policy WHERE target_state='' AND git_mode IN "
                f"({','.join('?' * len(modes))}) LIMIT 1",
                tuple(modes)).fetchone() is not None
        except Exception:  # noqa: BLE001
            return False
    finally:
        if own and conn is not None:
            conn.close()


def set_mode(conn, *, project: str = "", mode: str, actor: str = "",
             note: str = "") -> dict:
    """Write one project's mode (`mode=''` = inherit). The ONLY write path."""
    from . import db
    m = _norm(mode)
    old = conn.execute("SELECT id, git_mode FROM policy WHERE project=? AND "
                       "target_state=''", (project or "",)).fetchone()
    before = (old["git_mode"] if old else "") or ""
    if old:
        conn.execute("UPDATE policy SET git_mode=?, actor=?, updated_at=? "
                     "WHERE id=?", (m, actor, db.now(), old["id"]))
    else:
        # `note` stays in the EVENT, not the row: a row's `note` is shown as the
        # effective APPROVAL rule by plm/policy.py, and a sentence about storage
        # must not become the text on the Approve bar.
        conn.execute(
            "INSERT INTO policy (project, target_state, git_mode, actor, "
            "updated_at) VALUES (?, '', ?, ?, ?)",
            (project or "", m, actor, db.now()))
    db.add_event(conn, "gitmode.set", actor=actor,
                 data={"project": project or "", "from": before, "to": m,
                       "note": note})
    return explain(project, conn)


def _connect():
    try:
        from . import db
        return db.connect()
    except Exception:  # noqa: BLE001
        return None


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.gitmode",
        description="Read or set PLM_GIT_MODE per project. Deliberately not an "
                    "HTTP route: where a project's writes go is an operator's "
                    "decision.")
    ap.add_argument("--project", default="", help="'' = every project")
    ap.add_argument("--mode", choices=MODES + ("",), default=None,
                    help="off | read | write; '' = inherit")
    ap.add_argument("--actor", default="", help="who is deciding this")
    ap.add_argument("--note", default="", help="why")
    ap.add_argument("--readiness", action="store_true",
                    help="with --project: what flipping it to read/write would "
                         "DO, and what blocks it (plm/readiness.py). Writes "
                         "nothing")
    ap.add_argument("--json", action="store_true",
                    help="with --readiness: the report as JSON")
    args = ap.parse_args(argv)
    if args.readiness:
        if not args.project:
            ap.error("--readiness needs --project")
        if args.mode is not None:
            ap.error("--readiness reports; it does not also set a mode")
        from . import readiness
        return readiness.main(args.project, as_json=args.json)
    from . import db
    with db.connect() as conn:
        if args.mode is not None:
            set_mode(conn, project=args.project, mode=args.mode,
                     actor=args.actor, note=args.note)
        projects = [""] + [r["project"] for r in conn.execute(
            "SELECT DISTINCT project FROM items WHERE project<>'' ORDER BY 1")]
        print(f"PLM_GIT_MODE (env) = {os.environ.get('PLM_GIT_MODE', '') or '(unset)'}")
        for p in ([args.project] if args.project else projects):
            e = explain(p, conn)
            print(f"  {p or '(every project)':<28} {e['mode']:<6} [{e['source']}]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
