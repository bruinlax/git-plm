# plm — THE CODE AS AN IMAGE LAYER (stage 11, the service framework's shape).
#
# The base image (the repo-root Dockerfile, tagged plm-base:latest) carries the
# heavy, slow-moving part: Ubuntu, OpenSCAD, the venv. (Until 2026-10-21 it also
# carried OrcaSlicer; nothing in plm/ has called it since 2026-08-11.) This layer carries
# the code, so a container runs a FIXED, labelled commit with a read-only root
# filesystem instead of whatever the worktree holds when it restarts.
#
#   docker build -f deploy/image/Dockerfile \
#     --build-arg PLM_COMMIT=$(git rev-parse --short HEAD) -t plm:latest .
#
# Context = the repo root, so the root .dockerignore applies (house baseline:
# .git, .venv, __pycache__, *.sqlite3*, .env* never ship). Build seconds, not
# minutes: only plm/, plm_mcp/, plm-git/ and web/ are copied.
ARG BASE=plm:latest
FROM ${BASE}

# The base may be the old Orca-era image (root by default) or today's
# repo-root Dockerfile (non-root user 10001, already carrying tini, git-lfs
# and the code). Either way this layer installs as root; your compose file
# sets the runtime `user:` itself.
USER root

ARG PLM_COMMIT=unknown
ARG BASE=plm:latest

# ⭐ AN INIT AS PID 1 (stage 16b). Without one, uvicorn is PID 1 and never
# reaps: every `xvfb-run -a openscad` (render checks, thumbnails) leaves its
# Xvfb, and git leaves its helpers, reparented to PID 1 as zombies — 35 in the
# POC API container after one afternoon. `subprocess` waits for its own
# children; only an init reaps the grandchildren. tini also forwards SIGTERM,
# so `docker stop` stops the container cleanly. Same effect as compose `init: true`,
# carried by the image so every service built from it (and prod at step 2)
# gets it without a deploy-definition change.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/usr/bin/tini", "--"]

# ⭐ GIT LFS, THE CLIENT (stage 17, docs/LARGE-FILES.md § Install). The server
# side needs none of it — plm-git speaks the batch API itself and plm reads
# pointers itself — but every place a part is CLONED does: the demo/drill
# clones in these containers, agents' containers. The static release binary,
# pinned by version AND sha256 (the value in the release's sha256sums.asc), so
# a rebuild can never pick up a different binary. Not `git lfs install
# --system`: a system-wide smudge filter would make plm's own `git archive`
# (the render check) try to download from a remote a bare repo does not have.
ARG GIT_LFS_VERSION=3.8.0
ARG GIT_LFS_SHA256=e455e00f15d9b95661b8d53498ffb0c3367962cf1ec73c31ab7369516cd6ab8d
RUN curl -fsSL -o /tmp/git-lfs.tgz \
      "https://github.com/git-lfs/git-lfs/releases/download/v${GIT_LFS_VERSION}/git-lfs-linux-amd64-v${GIT_LFS_VERSION}.tar.gz" \
 && echo "${GIT_LFS_SHA256}  /tmp/git-lfs.tgz" | sha256sum -c - \
 && tar -xzf /tmp/git-lfs.tgz -C /tmp "git-lfs-${GIT_LFS_VERSION}/git-lfs" \
 && install -m 755 "/tmp/git-lfs-${GIT_LFS_VERSION}/git-lfs" /usr/local/bin/git-lfs \
 && rm -rf /tmp/git-lfs.tgz "/tmp/git-lfs-${GIT_LFS_VERSION}" \
 && git lfs version

# ⭐ A DEPENDENCY NEWER THAN THE BASE (2026-10-16): Pillow, for the
# physical-diff check's composite (plm/checks.py, PLM_REVIEW_MODEL §2.10). The
# base installs pyproject's dependencies when it is rebuilt; until then this
# layer adds what pyproject gained since, pinned by constraints.txt — so a
# code deploy never waits on a base rebuild. `pip check` keeps it honest.
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt pillow && pip check \
 && rm /tmp/constraints.txt

# `pip install -e .` in the base registered /app as the package root, which is
# where the POC used to bind-mount the worktree; the same paths, baked in.
COPY pyproject.toml README.md /app/
COPY plm /app/plm
COPY plm_mcp /app/plm_mcp
COPY plm-git /app/plm-git
COPY web /app/web

# Byte-compile now: with read_only: true nothing can write __pycache__ later.
RUN python3 -m compileall -q /app/plm /app/plm_mcp /app/plm-git

LABEL org.doempke.plm.commit="${PLM_COMMIT}" \
      org.doempke.plm.base="${BASE}"
ENV PLM_CODE_COMMIT="${PLM_COMMIT}"
