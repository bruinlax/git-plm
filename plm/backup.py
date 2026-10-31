"""plm backup — RETIRED. NOT SCHEDULED. Do not extend it.

A shared backup image is authoritative for plm's backups (do not write a
backup.py). It is declared as a `backup` sidecar in the deployment's
docker-compose.yml and configured with environment only — DB_PATHS,
CONFIG_DIR, HOST_PATH_MAP, KEEP, INTERVAL_SECONDS.

This module stays on disk because it wrote the older backup artifacts and its
--restore still reads them, but nothing runs it any more: backups, manual and
nightly, both go through the shared image, and its generated RESTORE.md is the
current one.
Two hand-rolled copies of this logic is how the ".env was never backed up" bug
happened twice — that is the whole reason it was replaced.

Original docstring follows.

plm backup: snapshot + logical ledger export, on a schedule, restorable.

Honesty first: the SQLite index is no longer purely derived. Context entries,
approvals, users, events, materials, suppression flags and propagate-created
revisions exist ONLY here — so the DB is primary data and gets backed up like
it. Two artifacts per run, written to PLM_BACKUP_DIR (a NAS-synced folder,
protected by the devops backup plan):

  plm-<ts>.sqlite3         consistent binary snapshot (VACUUM INTO — safe
                            against a live WAL writer)
  plm-ledger-<ts>.jsonl.gz logical dump of every real table — restores the
                            registry even without SQLite file compatibility,
                            and diffs meaningfully

Retention: newest KEEP of each kind. Restore:

  python -m plm.backup --restore latest     # or a specific snapshot filename
  (stop the plm services first; the previous DB is kept as *.pre-restore)

Run once:  python -m plm.backup
The compose `plm-backup` sidecar runs this daily.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import config

BACKUP_DIR = Path(os.environ.get("PLM_BACKUP_DIR", "/backups"))
# The same folder as a HOST shell sees it. RESTORE.md is read by a human on a
# host, not by this container, so any command it hands out must use host paths —
# the 2026-08-11 restore drill found another service's RESTORE.md emitting the
# container's /backups and failing on copy-paste. Overridable to stay
# host-agnostic (skill 002: no machine identity baked into a service folder).
BACKUP_HOST_DIR = os.environ.get("PLM_BACKUP_HOST_DIR", "") or str(BACKUP_DIR)
KEEP = int(os.environ.get("PLM_BACKUP_KEEP", "14"))
TABLES = ["items", "revisions", "files", "bom", "sources", "relations",
          "context", "events", "checkouts", "approvals", "users", "integrations",
          # stage 13 — the review model (plm/proposals.py, plm/threads.py):
          # forge state that exists ONLY here, so it rides the ledger too
          # (PLM_MARKUP §6: a redline left out of a restore is a lost redline)
          "proposals", "reviews", "threads",
          # stage 14 — changes (plm/changes.py) and release sets
          # (plm/releasesets.py): the decisions over N proposals / N releases,
          # and the dispositions people recorded. The release objects they
          # group (stage 3c) were never on the ledger; they are now.
          "changes", "change_reviews", "change_impacts", "release_sets",
          "releases", "release_signoffs"]


def _backup_config(backup_dir) -> None:
    """Copy the live deployment config (.env + compose) beside the data.

    A restored service without its .env boots green but in degraded auth —
    the worst failure shape (found by the 2026-08-11 restore drill). The
    repo is bind-mounted at /app in the sidecar, so the live files are
    right there. Overwritten each run; mode 600 on the .env copy.
    """
    import stat
    cfg = backup_dir / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    for name in (".env", "docker-compose.yml", "sample.env", ".env.example"):
        src = Path("/app") / name
        if not src.is_file():
            src = Path(__file__).resolve().parent.parent / name
        if src.is_file():
            dest = cfg / (name.lstrip(".") if name == ".env" else name)
            dest.write_bytes(src.read_bytes())
            if name.endswith(".env") or name == ".env":
                os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)


def snapshot() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    _backup_config(BACKUP_DIR)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    snap = BACKUP_DIR / f"plm-{ts}.sqlite3"
    conn.execute("VACUUM INTO ?", (str(snap),))
    sha = hashlib.sha256(snap.read_bytes()).hexdigest()
    (BACKUP_DIR / f"plm-{ts}.sqlite3.sha256").write_text(f"{sha}  {snap.name}\n")

    ledger = BACKUP_DIR / f"plm-ledger-{ts}.jsonl.gz"
    with gzip.open(ledger, "wt") as f:
        for table in TABLES:
            try:
                for row in conn.execute(f"SELECT * FROM {table}"):
                    f.write(json.dumps({"table": table, **dict(row)}) + "\n")
            except sqlite3.OperationalError:
                pass  # table not created yet on this instance
    conn.close()

    _write_readme()
    _prune("plm-*.sqlite3")
    _prune("plm-ledger-*.jsonl.gz")
    print(f"backup ok: {snap.name} ({snap.stat().st_size:,} B), {ledger.name}")


def _prune(pattern: str) -> None:
    files = sorted(BACKUP_DIR.glob(pattern), key=lambda p: p.name)
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)
        sha = old.with_suffix(old.suffix + ".sha256")
        sha.unlink(missing_ok=True)


def _write_readme() -> None:
    (BACKUP_DIR / "RESTORE.md").write_text(
        "# Restoring plm\n\n"
        "A restore was drilled for real on 2026-08-11: the latest snapshot\n"
        "restored into an empty data dir and a fresh container reached full\n"
        "parity with the live instance in 70 seconds.\n\n"
        "## What is in this folder\n\n"
        "| Artifact | What it is |\n"
        "|---|---|\n"
        "| `plm-<ts>.sqlite3` (+`.sha256`) | consistent DB snapshot (`VACUUM INTO`) — items, revisions, files, bom, sources, relations, context, events, checkouts, approvals, users, integrations |\n"
        "| `plm-ledger-<ts>.jsonl.gz` | logical dump of the same tables, one JSON object per row |\n"
        "| `plm2-*` | pre-rename snapshots (this service was `plm2` until 2026-08-11). Historical only; `--restore latest` never picks them. |\n"
        "| `archive-print-plm-v1-*.tar.gz` | the retired v1 print-plm repo. Not part of a restore. |\n\n"
        "## What a fresh host needs BEFORE step 1\n\n"
        "The DB restore below is one of four things; the other three are not in\n"
        "this folder:\n\n"
        "1. **The data dir** this instance mounts (`PLM_DB_DIR`, LOCAL disk,\n"
        "   never a network share for SQLite) and, if used, the legacy vault\n"
        "   (`PLM_DATA_ROOT`).\n"
        "2. **The code**: a clone of the plm repository at the commit you ran.\n"
        "3. **`.env` and the secrets dir** (push tokens, index token, the OIDC\n"
        "   client secret if you use OIDC). **Without `.env` the service still\n"
        "   starts — in the default trusted mode, no OIDC, no sign-in.** A green\n"
        "   `/api/stats` does not prove the auth plane came back; check\n"
        "   `/auth/whoami` and the `/auth/login` redirect too.\n"
        "4. **Empty dirs** for everything compose mounts; compose creates\n"
        "   missing bind-mount dirs, the vault must be mounted.\n\n"
        "## Restore\n\n"
        "1. Stop the services: `docker compose stop plm plm-mcp` (in the plm\n"
        "   repo). On a fresh host there is nothing to stop — skip it.\n"
        "2. `docker compose run --rm plm-backup python -m plm.backup --restore latest`\n"
        "   (or pass a specific `plm-<ts>.sqlite3` name). The old DB, if any, is\n"
        "   kept beside the live one as `*.pre-restore`. The restore re-checks the\n"
        "   `.sha256` and refuses on mismatch; to check by hand first:\n"
        f"   `sha256sum -c {BACKUP_HOST_DIR}/plm-<ts>.sqlite3.sha256`\n"
        "   (run in that folder). To restore somewhere else — a drill, or a\n"
        "   side-by-side check — put `PLM_DB_DIR=/some/scratch/dir` in front of\n"
        "   the same command; nothing else changes.\n"
        "3. `docker compose up -d`.\n\n"
        "## Verify green (this IS the conformance test, not a formality)\n\n"
        "    curl -s localhost:8097/api/stats                  # counts\n"
        "    curl -s localhost:8097/api/approvals              # pending rows\n"
        "    curl -s localhost:8097/api/items/<number>         # one item + revisions\n"
        "    curl -s \"localhost:8097/api/search?q=<term>\"      # FTS index rode along\n"
        "    curl -s \"localhost:8097/api/blob?path=<vault path>\" | sha256sum\n\n"
        "The 2026-08-11 drill, against that day's snapshot: 92 items / 213\n"
        "revisions / 590 bom lines / 375 files, 2 pending approvals, 30 hits for\n"
        "\"lantern\", and a 1,318,899-byte STL streaming byte-identical to prod.\n"
        "Compare against the live instance you are replacing where you still can.\n\n"
        "## The rest of the picture\n\n"
        "The vault (CAD files/git), when one is mounted, is protected by its\n"
        "own backup — this folder only\n"
        "carries the registry database. Thumbnails, the object cache and the\n"
        "sandbox are derived and never backed up. The ledger .jsonl.gz is a\n"
        "logical dump usable even without SQLite file compatibility.\n\n"
        "## Bytes the DB only points at (cross-service reference)\n\n"
        "Since swappable storage backends (plm/storage.py), a `files` row\n"
        "may say `backend='lfs'` instead of living in the\n"
        "vault. The row then records only where the bytes are (key + sha256):\n\n"
        "  lfs       plm's own LFS object store (PLM_LFS_ROOT,\n"
        "            objects/<aa>/<bb>/<sha256>) — THE object store; backed up\n"
        "            with plm's data trees, re-hashed by `python -m plm.lfs --fsck`\n\n"
        "So a full restore is: this snapshot (the registry) + the vault repo +\n"
        "the LFS object store, restored to the SAME point in time or\n"
        "later. A DB older than the object store is fine (it references fewer\n"
        "objects); a DB NEWER than the object store will have rows whose bytes\n"
        "do not exist yet — `/api/blob?id=` returns 502 for those. Text files,\n"
        "`source` and `viz` roles are pinned to the vault, so sources and\n"
        "visualization geometry are always covered by the vault backup alone.\n")


def restore(which: str) -> None:
    snaps = sorted(BACKUP_DIR.glob("plm-*.sqlite3"), key=lambda p: p.name)
    if not snaps:
        sys.exit("no snapshots found")
    src = snaps[-1] if which == "latest" else BACKUP_DIR / which
    if not src.is_file():
        sys.exit(f"no such snapshot: {src}")
    sha_file = src.with_suffix(src.suffix + ".sha256")
    if sha_file.is_file():
        want = sha_file.read_text().split()[0]
        have = hashlib.sha256(src.read_bytes()).hexdigest()
        if want != have:
            sys.exit(f"checksum mismatch on {src.name} — refusing to restore")
    live = config.DB_PATH
    if live.exists():
        shutil.copy2(live, live.with_suffix(".sqlite3.pre-restore"))
    for suffix in ("-wal", "-shm"):
        p = Path(str(live) + suffix)
        p.unlink(missing_ok=True)
    shutil.copy2(src, live)
    print(f"restored {src.name} -> {live} (previous kept as .pre-restore)")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--restore":
        restore(sys.argv[2])
    else:
        snapshot()
