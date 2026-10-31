"""plm configuration.

Two roots, deliberately separate:

- DATA_ROOT: the CAD/git vault (the 3d-printing workspace repo). Files here are
  the source of truth. It lives on a network-synced volume, which is fine for
  ordinary files but corrupts busy databases.
- DB_PATH: the SQLite index. Rebuildable from the vault at any time, so it goes
  in a Docker named volume (or ~/.cache when run on the host) — never on the
  synced tree.
"""
from __future__ import annotations

import os
from pathlib import Path


def secret(name: str, default: str = "") -> str:
    """A secret from `<NAME>_FILE` first, then from `<NAME>`.

    ⭐ `_FILE` INDIRECTION, EVERYWHERE A SECRET IS READ (H3). `docker inspect`
    prints every env VALUE a container was started with, to anybody in the
    docker group, forever — so a token that is an env value is a token in the
    process list. The POC's compose already used `_FILE` for the two secrets the
    git container reads and claimed in a comment that the same was true of
    `PLM_GIT_INDEX_TOKEN`, which was a plain value on all three containers.
    This is the mechanism that makes the comment true;
    `plm/identity.py::_env_secret` did the same thing for the OIDC client secret
    and now calls this instead, so there is one implementation.

    The env value is still honoured: an operator debugging on a laptop should
    not have to make a file, and a POC that refused would be a POC nobody runs.
    """
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            # A configured-but-unreadable secret file is worth one line on
            # stderr and is NOT worth crashing the service: the endpoint that
            # needs it refuses by itself, with a message about that endpoint.
            print(f"plm: {name}_FILE={path} is not readable; falling back to "
                  f"{name}", flush=True)
    return os.environ.get(name, default)


def _default_db() -> Path:
    for base in (Path("/var/plm"), Path.home() / ".cache" / "plm"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / "plm.sqlite3"
        except OSError:
            continue
    return Path("/tmp/plm.sqlite3")


DATA_ROOT = Path(os.environ.get("PLM_DATA_ROOT", "/data"))
if not DATA_ROOT.is_dir():  # host-side fallback for the importer / dev
    # PLM_DATA_ROOT_FALLBACK: where a host shell finds the vault when /data is
    # a container-only path (one operator's checkout of the legacy CAD tree).
    _alt = Path(os.environ.get("PLM_DATA_ROOT_FALLBACK", "") or "/nonexistent")
    if _alt.is_dir():
        DATA_ROOT = _alt

DB_PATH = Path(os.environ.get("PLM_DB", str(_default_db())))

#: Where the per-item bare repositories live (git-first). ONE home for the
#: default: four modules used to carry the POC's own path
#: (`/srv/plm/repos`) as theirs, so a production deployment
#: that forgot PLM_REPOS would have looked for its repos in the POC's runtime
#: root. Default: beside the database, on the same local disk.
REPOS = Path(os.environ.get("PLM_REPOS") or str(DB_PATH.parent / "repos"))

#: Git LFS objects (stage 17, docs/LARGE-FILES.md): plm's content-addressed
#: store, `objects/<aa>/<bb>/<sha256>` — the same layout git-lfs keeps locally.
#: SERVICE DATA, backed up beside the repos (the sidecar's DATA_TREES); a
#: pointer in a tree is only as good as the object behind it. Default: a
#: sibling of the repos, on the same local disk.
LFS_ROOT = Path(os.environ.get("PLM_LFS_ROOT") or str(REPOS.parent / "lfs"))
#: Above this, a non-text file must be an LFS pointer (push door, split,
#: plm's own commits). Meshes are LFS at ANY size — `.gitattributes` cannot
#: express a size, and a mesh that is a pointer in one commit and a blob in
#: the next is what makes `git status` lie in an LFS clone.
LFS_THRESHOLD = int(os.environ.get("PLM_LFS_THRESHOLD_BYTES")
                    or str(1024 * 1024))
#: The largest single LFS object the git service accepts.
LFS_MAX_OBJECT = int(os.environ.get("PLM_LFS_MAX_OBJECT_BYTES")
                     or str(2 * 1024 * 1024 * 1024))

#: DERIVED data — thumbnails, the object cache, URL arrivals in flight, the
#: upload inbox and 3-D snippets (both expire by themselves),
#: rendered geometry for checks. Rebuildable, never backed up: the service
#: framework's `cache/` (stage 11). ONE home for the default, which is where all
#: four have always lived — beside the database — so an instance that does not
#: set it behaves exactly as before.
CACHE_DIR = Path(os.environ.get("PLM_CACHE_DIR") or str(DB_PATH.parent))

# -- SQLite concurrency (plm/db.py) ---------------------------------
# SQLite has ONE writer. That is fine — plm's transactions are supposed to be
# milliseconds — but it means every write path has to be honest about how long
# it holds the lock, and every waiter has to be bounded rather than told
# "database is locked".
#
# BUSY_TIMEOUT_MS: how long a statement waits for the writer lock inside SQLite
#   itself (sqlite3.connect(timeout=...) is exactly this pragma).
# WRITE_RETRIES: how many extra attempts db.PlmConnection makes after a
#   SQLITE_BUSY, with exponential backoff and jitter. A statement that returns
#   SQLITE_BUSY has NOT run, so retrying the identical statement is safe.
# The worst-case wait a caller can see is roughly
#   (WRITE_RETRIES + 1) * BUSY_TIMEOUT_MS + the backoff sum
# and the refusal that follows says so, with the retry count, instead of a bare
# 'database is locked'.
BUSY_TIMEOUT_MS = int(os.environ.get("PLM_SQLITE_BUSY_TIMEOUT_MS", "10000"))
WRITE_RETRIES = int(os.environ.get("PLM_SQLITE_WRITE_RETRIES", "4"))

# -- checkouts that outlived their agent ---------------------------
# The advisory lock is Teamcenter-style: it protects work in progress, and the
# holder releases it by checking in. An agent that dies mid-work releases
# nothing, so an item can be stranded forever. A checkout older than this is
# STALE and may be taken over by the next writer (evented, never silent).
# Long enough that a real multi-hour job is not interrupted.
CHECKOUT_STALE_SECONDS = float(os.environ.get("PLM_CHECKOUT_STALE_SECONDS",
                                              str(6 * 3600)))

# -- idempotency (plm/idem.py) --------------------------------------
# How long a replay record lives. It only has to outlast a client's retry
# window: the failure it exists for is "the write committed, the response never
# arrived, the agent tried again".
IDEMPOTENCY_TTL_SECONDS = float(os.environ.get("PLM_IDEMPOTENCY_TTL_SECONDS",
                                               str(24 * 3600)))
# Window in which an IDENTICAL identity-minting request from the same actor,
# with no explicit idempotency_key, is treated as a retry rather than a second
# item. This is the guard that makes the duplicate impossible for
# clients that send no key at all.
IDEMPOTENCY_AUTO_WINDOW = float(os.environ.get("PLM_IDEMPOTENCY_AUTO_WINDOW",
                                               "900"))

# -- storage backends (plm/storage.py, plm/storage.py) -----------------
# Where NEW binary files land. 'vault' (the default) = today's behavior, git
# working tree, zero change. 'lfs' (the decision) offloads bulk
# binaries to plm's own LFS store; text/source/viz stay pinned to the vault
# regardless (see storage.py).
STORAGE_DEFAULT = os.environ.get("PLM_BLOBSTORE_DEFAULT", "vault")
STORAGE_MIN_SIZE = int(os.environ.get("PLM_BLOBSTORE_MIN_SIZE", str(256 * 1024)))
STORAGE_PIN_EXT = os.environ.get(
    "PLM_BLOBSTORE_PIN_EXT",
    ".scad .md .txt .csv .json .toml .yaml .yml .ini .py .gitignore").split()
# Direct raw-byte uploads (PUT /api/items/{n}/files/{name}, PUT
# /api/uploads/{token}, and what a `url` arrival may fetch) — transport for
# scripts; nginx's client_max_body_size (200m on the vhost) is the outer wall.
RAW_UPLOAD_MAX = int(os.environ.get("PLM_RAW_UPLOAD_MAX", str(192 * 1024 * 1024)))

# -- large-file intake (docs/PLM_UPLOADS.md, plm/staging.py) -----------------
# Big binaries do NOT travel as base64 through an MCP tool call. The agent
# hands plm a LINK (a file-sharing service), or PUTs the raw bytes to plm's
# own upload inbox and checks in with the HANDLE it got. These are the limits
# on the inline alternative.
#
# The MCP limit is the strict one: a tool call is a context window, and a 30 MB
# STL as base64 is 40 MB of tokens nobody can afford. HTTP is looser because
# real clients (the web UI, existing viz posts) legitimately POST a few MB and
# breaking them to make a point would be rude. Text files get their own,
# generous limit: an inline markdown SOP is exactly what inline is FOR.
INLINE_MAX_MCP = int(os.environ.get("PLM_MAX_INLINE_BYTES", str(1024 * 1024)))
INLINE_MAX_HTTP = int(os.environ.get("PLM_MAX_INLINE_BYTES_HTTP",
                                     str(16 * 1024 * 1024)))
INLINE_MAX_TEXT = int(os.environ.get("PLM_MAX_INLINE_TEXT_BYTES",
                                     str(8 * 1024 * 1024)))
# How long a staged (uploaded-but-not-checked-in) object lives before the
# janitor deletes it. Long enough to upload and check in; short enough that a
# forgotten stage is not a permanent disk leak.
STAGING_TTL_SECONDS = float(os.environ.get("PLM_STAGING_TTL_SECONDS", str(24 * 3600)))
STAGING_MAX_TTL_SECONDS = float(os.environ.get("PLM_STAGING_MAX_TTL_SECONDS",
                                               str(7 * 24 * 3600)))

# Where this instance is reachable, for links we hand to humans (the snippet
# viewer URL, docs/PLM_SNIPPETS.md). identity.py resolves the same value for
# OIDC redirects; kept here too so non-HTTP callers (the MCP) can build a URL
# without importing the identity layer.
PUBLIC_URL = (os.environ.get("PLM_PUBLIC_URL", "").strip()
              or "http://localhost:8097").rstrip("/")

#: The e-mail plm's own commits carry (gitwrite, split, vault). ONE home:
#: PLM_GIT_BOT_EMAIL, else `plm@<the public URL's host>`.
def _bot_email() -> str:
    explicit = os.environ.get("PLM_GIT_BOT_EMAIL", "").strip()
    if explicit:
        return explicit
    from urllib.parse import urlsplit
    host = (urlsplit(PUBLIC_URL).hostname or "localhost").strip(".")
    return f"plm@{host}"


BOT_EMAIL = _bot_email()

# Stage 20: WHERE GIT-FIRST PROJECTS LIVE, as seen from an
# instance that is `off`. Production points this at the git-first instance
# (staging, until rollout step 3), so an agent about to write a NEW project on
# the legacy path is told — once, in the MCP instructions, and again on
# get_item / create_item / whoami — where it should have gone. Empty (the
# default) = no routing line anywhere: `off` stays exactly master's.
GITFIRST_INSTANCE_URL = os.environ.get("PLM_GITFIRST_INSTANCE_URL",
                                       "").strip().rstrip("/")

# Instance label shown in the UI masthead — e.g. "SANDBOX" for a demo copy,
# so nobody mistakes a scratch registry for the system of record.
LABEL = os.environ.get("PLM_LABEL", "")

# The shared secret the git service's post-receive hook uses to reach
# POST /api/git/index. Service-to-service, inside the compose network, and the
# reason it exists at all is that the git container must never be a second
# writer of the registry — it asks plm to index, plm does the write.
# Empty (the default) means the endpoint is closed.
GIT_INDEX_TOKEN = secret("PLM_GIT_INDEX_TOKEN")

# Lifecycle states, in intended order of progression. DRAFT is mutable;
# IN_REVIEW is frozen and awaiting an approval decision; everything after is
# a released record.
#
# ⭐ CONCEPT and DESIGN were REAL AND UNDECLARED (rule 2 of
# plm/migrate_cleanup.py). 26 revisions have been sitting in them for a year and
# the rest of the system has always treated them as released maturities:
# `manifest.MATURITY_BY_STATE` maps both, `split` writes them a tag,
# `plm/policy.py` has an `auto` row for each, and `plm/release.py`'s workflow
# table starts at concept. The registry was right and this list was lying — so
# the fix is one line here, not an UPDATE over 26 rows.
#
# Order matters: it is read as a progression (concept -> design -> prototype ->
# released), and both sit before PROTOTYPE because that is where they are in a
# part's life.
STATES = ["DRAFT", "IN_REVIEW", "CONCEPT", "DESIGN", "PROTOTYPE", "RELEASED",
          "SUPERSEDED", "OBSOLETE"]

# Item kind by part-number class (the middle token of e.g. KML-PRT-1011).
KIND_BY_CLASS = {
    "PRT": "part",
    "ASM": "assembly",
    "PUR": "purchased",
    "HDW": "hardware",
    "FW": "firmware",
    "DOC": "document",
    # Test coupons (docs/PLM_RELATIONS.md): real items — revisions, files,
    # context — that verify ONE revision of a real part through a coupon_for
    # relation. Structurally barred from BOMs: they are V&V artifacts, not
    # occurrences (see ops.COUPON_IN_BOM).
    "CPN": "coupon",
    # ASSETS (docs/PLM_RELATIONS.md): the thing your part attaches to,
    # which you neither build nor buy — a camper, a truck bed, a garage wall, a
    # bike frame. Reference geometry and measured dimensions for FIT; never a
    # BOM line, never a purchase, never a product card. Real items otherwise
    # (revisions, files, attributes, context), because the datum you design
    # against has to be versioned like anything else you measure.
    #
    # AST, not REF: the number class is a projection of `kind`, and `kind` is
    # now `asset`. REF was the token a Codex bot invented for "reference
    # geometry" while there was no kind to carry the fact — the MCP SOP names
    # GL-REF-0001 as a forbidden number for exactly that reason, and reusing it
    # would resurrect the pattern the SOP forbids.
    "AST": "asset",
}
CLASS_BY_KIND = {v: k for k, v in KIND_BY_CLASS.items()}

# Kinds that are NOT product structure. Two consequences, and they are the same
# fact stated twice: such an item can never be a BOM child (ops.NOT_IN_BOM
# guards both write paths) and can never be flagged items.product
# (ops.set_product). Adding a non-structural kind is one entry here.
#
# LOAD-BEARING. Every downstream exclusion — the material rollup, _build_plan's
# buy list, /api/products, _top_items — is derived from the invariant these two
# guards maintain, not from a filter of its own. Take a guard away and coupons
# and assets start appearing in purchasing plans.
NON_STRUCTURAL_KINDS = ("coupon", "asset")
