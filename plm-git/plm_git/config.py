"""plm-git configuration — every host-specific value arrives via the environment.

Same host-agnosticism rule plm itself follows: no hostnames, paths or ports are
hardcoded anywhere but here, and every one of them has a working default so the
POC runs with an empty .env.
"""
from __future__ import annotations

import os
from pathlib import Path


def _p(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


# The CAD vault: ONE git repo, the system of record for bytes. plm-git only
# ever READS it, and never through the live working tree — see extract.py.
VAULT = _p("PLMGIT_VAULT", "/vault")

# Local scratch (never on the synced volume — it holds git object stores).
CACHE = _p("PLMGIT_CACHE", "~/.cache/plm-git")

# The safe read-only copy of the vault that all extraction runs against.
MIRROR = CACHE / "vault-mirror.git"

# GIT_PROJECT_ROOT for git-http-backend. One bare repo per part: <NUMBER>.git
REPOS = CACHE / "repos"

# plm proper. Preferred resolver: it owns identity (numbers, former numbers,
# folders), so we ask it rather than re-deriving the folder convention.
PLM_URL = os.environ.get("PLMGIT_PLM_URL", "http://127.0.0.1:8097").rstrip("/")

# Fallback resolver when the API is unreachable: plm's SQLite index, opened
# read-only (mode=ro) so a POC can never mutate the registry.
PLM_DB = _p("PLMGIT_DB", "~/.cache/plm/plm.sqlite3")

BIND = os.environ.get("PLMGIT_BIND", "0.0.0.0")
PORT = int(os.environ.get("PLMGIT_PORT", "8099"))

# --------------------------------------------------------------------------
# DIRECT-SERVE MODE (the git-first POC — the design docs §E)
# --------------------------------------------------------------------------
# Point this at a directory of bare repos and the server stops being a
# projection of the vault: no resolve, no extract, no replay, no freshness
# check — just `git http-backend` over repos somebody else generated
# (plm/split.py). That is the whole difference between "git is a costume plm
# wears" and "the repo is the record", and it is one environment variable
# because the transport was never the part that had to change.
#
# Direct mode is READ-ONLY and not negotiable: receive-pack is refused at the
# door regardless of PLMGIT_ALLOW_PUSH, because in git-first a push has to be
# validated against plm.toml by a pre-receive hook that does not exist yet.
DIRECT_ROOT = os.environ.get("PLMGIT_DIRECT_ROOT", "")

# --------------------------------------------------------------------------
# the write path ("git push" == "propose a revision")
# --------------------------------------------------------------------------
# Hard off switch. Default 0 everywhere: with this at 0 the router refuses every
# receive-pack request AND every generated repo carries http.receivepack=false,
# so there are two independent reasons a push cannot happen. Turning it on is
# step 1 of 3 — see "Enabling push" in README.md.
ALLOW_PUSH = os.environ.get("PLMGIT_ALLOW_PUSH", "0") == "1"

# WHICH plm the write path writes to. Reads (number -> folder) use PLM_URL; the
# write path uses this, defaulting to the SANDBOX, because a mistake here is a
# commit in the operator's system of record. Both are host-agnostic env values.
PUSH_PLM_URL = os.environ.get("PLMGIT_PUSH_PLM_URL",
                              os.environ.get("PLMGIT_SANDBOX_PLM_URL",
                                             "http://plm-sandbox:8097")).rstrip("/")

# Where a HUMAN reaches that plm — the link printed in the push output. The
# internal compose name is right for HTTP calls and useless in a browser.
PUSH_PLM_PUBLIC_URL = os.environ.get(
    "PLMGIT_PUSH_PLM_PUBLIC_URL", "").rstrip("/") or PUSH_PLM_URL

# Step 3 of enabling push against the system of record. A URL is treated as
# "prod" unless it names a sandbox; pointing the write path at prod without
# saying so explicitly is refused at the door, with the reason printed.
PUSH_ALLOW_PROD = os.environ.get("PLMGIT_PUSH_ALLOW_PROD", "0") == "1"

# token -> plm actor, as `token=actor` pairs (comma or whitespace separated), or
# the same content in a file (preferred: keeps secrets out of `docker inspect`).
# A push with no recognised token is a 401 — the read path stays anonymous.
PUSH_TOKENS = os.environ.get("PLMGIT_PUSH_TOKENS", "")
PUSH_TOKENS_FILE = os.environ.get("PLMGIT_PUSH_TOKENS_FILE", "")

# The ref namespace a proposal is pushed to: `git push origin HEAD:refs/for/main`
# (the Gerrit convention, which every model already recognises). Pushes to
# refs/heads/* are always refused: the served branch is plm's, not the pusher's.
PUSH_REF = os.environ.get("PLMGIT_PUSH_REF", "refs/for/main")

# What `submit_for_release` asks for. plm's approver decides; this is only the
# state the proposal is aiming at.
PUSH_TARGET_STATE = os.environ.get("PLMGIT_PUSH_TARGET_STATE", "PROTOTYPE")

# Size sanity, checked in pre-receive where saying no is cheap. Defaults match
# plm's own PLM_RAW_UPLOAD_MAX (192 MiB) per file.
PUSH_MAX_FILE_BYTES = int(os.environ.get("PLMGIT_PUSH_MAX_FILE_BYTES",
                                         str(192 * 1024 * 1024)))
PUSH_MAX_TOTAL_BYTES = int(os.environ.get("PLMGIT_PUSH_MAX_TOTAL_BYTES",
                                          str(256 * 1024 * 1024)))
PUSH_MAX_FILES = int(os.environ.get("PLMGIT_PUSH_MAX_FILES", "200"))

# Extensions that travel INLINE as text in the check-in JSON. Everything else is
# a binary and moves by reference (staged upload handle, or a raw PUT) so bytes
# never ride a JSON body. Mirrors plm's PLM_BLOBSTORE_PIN_EXT.
PUSH_TEXT_EXT = set(os.environ.get(
    "PLMGIT_PUSH_TEXT_EXT",
    ".scad .md .txt .csv .json .toml .yaml .yml .ini .py .cfg .gitignore "
    ".sha256 .gcode .svg").split())

# How binaries reach plm — an explicit choice, no automatic fallback.
# `staged` = plm mints an upload, we stream the bytes to its inbox and hand
# over the handle with the rest of the files in one working upload; `raw` =
# PUT each binary straight onto the DRAFT (/api/items/<n>/files/<name>).
PUSH_BINARY_TRANSPORT = os.environ.get("PLMGIT_PUSH_BINARY_TRANSPORT", "staged")

# Where accepted proposals are recorded, outside the generated repos (which are
# rebuilt from scratch). This ledger is what re-creates refs/plm/drafts/* after
# a rebuild, so the "pull request" ref survives a refresh.
PROPOSALS = CACHE / "proposals"
LOCKS = CACHE / "locks"

# Don't re-check the vault for every object request during one clone.
FRESHNESS_TTL = int(os.environ.get("PLMGIT_FRESHNESS_TTL", "30"))

# Branch name inside each generated per-part repo.
BRANCH = os.environ.get("PLMGIT_BRANCH", "main")

GIT = os.environ.get("PLMGIT_GIT", "git")

# --------------------------------------------------------------------------
# GIT-FIRST WRITE PATH — direct-serve mode only
# --------------------------------------------------------------------------
# Direct mode was read-only in stage 1 because a push had nothing to validate
# against. It has now: plm.toml IS the revision, plm/gitvalidate.py is the
# check, and plm/gitindex.py turns the accepted commit into registry rows.
#
# Reads stay anonymous (private network posture). Writes need a bearer token that maps
# to a plm user holding `editor` — the same door plm_git/push.py has always
# been, reused rather than reinvented.
GITFIRST_ALLOW_PUSH = os.environ.get("PLMGIT_GITFIRST_ALLOW_PUSH", "0") == "1"

# plm, for the two things the hook asks it to do: index a commit, submit a
# proposal. The git container never opens the registry itself.
GITFIRST_PLM_URL = os.environ.get("PLMGIT_GITFIRST_PLM_URL",
                                  "http://plm:8097").rstrip("/")

# The shared secret for POST /api/git/index. From a file when given, so it stays
# out of `docker inspect` — and, like every secret here, out of the worktree.
def _read_secret(value: str, path: str) -> str:
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    return value


GITFIRST_INDEX_TOKEN = _read_secret(
    os.environ.get("PLMGIT_GITFIRST_INDEX_TOKEN", ""),
    os.environ.get("PLMGIT_GITFIRST_INDEX_TOKEN_FILE", ""))

# The one namespace a pusher owns. `refs/heads/draft/<topic>`; everything else
# — main, tags, refs/plm/* — belongs to plm (plm/gitvalidate.check_ref_namespace).
GITFIRST_DRAFT_PREFIX = os.environ.get("PLMGIT_GITFIRST_DRAFT_PREFIX",
                                       "draft")


# The committer identity on commits plm-git itself makes. PLMGIT_COMMITTER_EMAIL,
# else `plm-git@<host of the public URL>` (PLMGIT_PUSH_PLM_PUBLIC_URL, then
# PLM_PUBLIC_URL), else plm-git@localhost.
def _committer_email() -> str:
    explicit = os.environ.get("PLMGIT_COMMITTER_EMAIL", "").strip()
    if explicit:
        return explicit
    from urllib.parse import urlsplit
    url = (os.environ.get("PLMGIT_PUSH_PLM_PUBLIC_URL", "").strip()
           or os.environ.get("PLM_PUBLIC_URL", "").strip())
    host = (urlsplit(url).hostname if url else "") or "localhost"
    return f"plm-git@{host}"


COMMITTER_EMAIL = _committer_email()
