"""First run of the stock compose stack: make ./data usable, seed the secrets.

`docker compose up` runs this once, before the three services, as the `init`
service (root, no network, a one-shot). It exists so that a clone needs NOTHING
before `docker compose up -d --build`: no script, no .env, no account.

What it does, idempotently:

  <data>/db <data>/repos <data>/lfs <data>/cache <data>/evidence <data>/secrets
      created if missing, owned by PLM_UID:PLM_GID (the uid the services run
      as, which compose takes from .env or defaults to 1000). A directory
      Docker pre-created for a bind mount is root-owned and empty: that one is
      re-owned too. A directory with content is never touched.
  <data>/secrets/push-tokens
      `<token>=<plm username>` lines: the ONE credential store for the API,
      the MCP and `git push`. Seeded with one line for PLM_ADMIN (default
      `admin`), whom plm makes the first approver on first start.
  <data>/secrets/index-token
      a service-to-service secret (the git door asks plm to index a push).

It never overwrites a secret that exists, and it never prints one: it prints
WHERE the token is. Safe to run on every `up`.

    python -m plm.firstrun [--data /data] [--uid N] [--gid N] [--admin NAME]
"""
from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

DIRS = ("db", "repos", "lfs", "cache", "evidence", "secrets")


def _rand() -> str:
    return secrets.token_hex(24)


def _own(path: Path, uid: int, gid: int, mode: int, *, log) -> None:
    """chown + chmod when running as root; otherwise only say what differs."""
    st = path.stat()
    me = os.geteuid()
    if me == 0 and (st.st_uid, st.st_gid) != (uid, gid):
        os.chown(path, uid, gid)
        st = path.stat()
    if (me == 0 or st.st_uid == me) and stat.S_IMODE(st.st_mode) != mode:
        os.chmod(path, mode)
    if (st.st_uid, st.st_gid) != (uid, gid):
        log(f"note    {path} is owned by {st.st_uid}:{st.st_gid}, not "
            f"{uid}:{gid} (not root here, cannot change it)")


def run(data: Path, uid: int, gid: int, admin: str, *, log=print) -> dict:
    """Make `data` ready. Returns what it created, for the caller and tests."""
    created: list[str] = []
    data.mkdir(parents=True, exist_ok=True)
    for name in DIRS:
        d = data / name
        fresh = not d.exists()
        d.mkdir(exist_ok=True)
        mode = 0o700 if name == "secrets" else 0o755
        # A dir Docker made for a bind mount is root's and empty: ours now.
        if fresh or not any(d.iterdir()):
            _own(d, uid, gid, mode, log=log)
        elif name == "secrets":
            _own(d, uid, gid, mode, log=log)
        if fresh:
            created.append(name)
    sec = data / "secrets"
    tokens = sec / "push-tokens"
    if not tokens.exists() or tokens.stat().st_size == 0:
        tokens.write_text("# <token>=<plm username>, one per line. "
                          "Add a line per person or agent.\n"
                          f"plm_{_rand()}={admin}\n", encoding="utf-8")
        created.append("secrets/push-tokens")
        log(f"created secrets/push-tokens (first token = '{admin}')")
    else:
        log("kept    secrets/push-tokens")
    index = sec / "index-token"
    if not index.exists() or index.stat().st_size == 0:
        index.write_text(_rand() + "\n", encoding="utf-8")
        created.append("secrets/index-token")
        log("created secrets/index-token")
    else:
        log("kept    secrets/index-token")
    for f in (tokens, index):
        _own(f, uid, gid, 0o600, log=log)
    log(f"your token: the line ending in '={admin}' in "
        f"<your clone>/data/secrets/push-tokens")
    return {"created": created, "admin": admin}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=os.environ.get("PLM_FIRSTRUN_DATA", "/data"))
    ap.add_argument("--uid", type=int, default=int(os.environ.get("PLM_UID", "1000")))
    ap.add_argument("--gid", type=int, default=int(os.environ.get("PLM_GID", "1000")))
    ap.add_argument("--admin", default=(os.environ.get("PLM_ADMIN") or "admin").strip())
    a = ap.parse_args(argv)
    try:
        run(Path(a.data), a.uid, a.gid, a.admin or "admin")
    except OSError as e:
        print(f"firstrun: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
