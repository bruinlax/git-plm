# plm — the part registry, as ONE self-contained image.
#
#   docker build -t plm-selfhost:latest --build-arg PLM_COMMIT=$(git rev-parse --short HEAD) .
#   (or just `docker compose up -d --build` — see DEPLOY.md § "Run it anywhere")
#
# One image runs all three services (the API/UI, the MCP server and the git
# door): they share the code and differ only in their command.
#
# What is in it, and why:
#   - Ubuntu 24.04: the distro OpenSCAD packages cleanly on (render checks,
#     thumbnails). Its system Python is 3.12, which plm targets.
#   - openscad + xvfb + xauth: OpenSCAD wants a display even headless.
#   - git (+ a pinned git-lfs): every item IS a git repo; LFS carries meshes.
#   - tini as PID 1: `xvfb-run` and git leave grandchildren that only an init
#     reaps (35 zombies after one afternoon without it).
#   - No slicer. Slicing left plm on 2026-08-11; nothing in plm/ calls one.
#
# deploy/image/Dockerfile can be layered on top of this image
# (`--build-arg BASE=...`); most people need only this file.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# `apt-get upgrade` at build, so `docker build --pull` carries Ubuntu's security fixes.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git tini \
      python3 python3-venv python3.12-venv \
      openscad xvfb xauth \
      locales fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe

# GIT LFS, THE CLIENT (docs/LARGE-FILES.md § Install). The server side needs
# none of it — plm-git speaks the batch API itself — but every place a part is
# CLONED does. The static release binary, pinned by version AND sha256. Not
# `git lfs install --system`: a system-wide smudge filter would make plm's own
# `git archive` (the render check) try to download from a remote a bare repo
# does not have.
ARG GIT_LFS_VERSION=3.8.0
ARG GIT_LFS_SHA256=e455e00f15d9b95661b8d53498ffb0c3367962cf1ec73c31ab7369516cd6ab8d
RUN curl -fsSL -o /tmp/git-lfs.tgz \
      "https://github.com/git-lfs/git-lfs/releases/download/v${GIT_LFS_VERSION}/git-lfs-linux-amd64-v${GIT_LFS_VERSION}.tar.gz" \
 && echo "${GIT_LFS_SHA256}  /tmp/git-lfs.tgz" | sha256sum -c - \
 && tar -xzf /tmp/git-lfs.tgz -C /tmp "git-lfs-${GIT_LFS_VERSION}/git-lfs" \
 && install -m 755 "/tmp/git-lfs-${GIT_LFS_VERSION}/git-lfs" /usr/local/bin/git-lfs \
 && rm -rf /tmp/git-lfs.tgz "/tmp/git-lfs-${GIT_LFS_VERSION}" \
 && git lfs version

# git refuses to operate on a repo owned by another uid; the repos are on a
# bind mount owned by whoever runs the stack.
RUN git config --system --add safe.directory '*'

# Ubuntu 24.04 marks the system Python as externally managed (PEP 668), so the
# app gets its own venv.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
# Dependencies first (a code change does not reinstall them). constraints.txt
# pins every runtime dependency, so a rebuild never silently upgrades
# fastapi/fastmcp/starlette underneath the code.
COPY pyproject.toml README.md constraints.txt ./
RUN python3 -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -c constraints.txt -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY plm /app/plm
COPY plm_mcp /app/plm_mcp
COPY plm-git /app/plm-git
COPY web /app/web
# Editable on purpose: plm serves web/ from beside its package (/app/web), so
# the package must be imported from /app, not from site-packages.
RUN pip install --no-cache-dir --no-deps -e . && pip check \
 && python3 -m compileall -q /app/plm /app/plm_mcp /app/plm-git

# Runs as a non-root user. Compose overrides the uid with PLM_UID/PLM_GID so
# files in ./data belong to whoever owns the checkout.
RUN useradd --uid 10001 --gid 0 --home-dir /tmp --no-create-home plm
USER 10001

ARG PLM_COMMIT=unknown
LABEL org.opencontainers.image.title="plm" \
      org.opencontainers.image.revision="${PLM_COMMIT}" \
      plm.commit="${PLM_COMMIT}"
ENV PLM_CODE_COMMIT="${PLM_COMMIT}" \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PLM_DB=/var/plm/plm.sqlite3 \
    PLM_REPOS=/var/plm/repos \
    PLM_LFS_ROOT=/var/plm/lfs \
    PLM_EVIDENCE_DIR=/var/plm/evidence \
    PLM_CACHE_DIR=/var/cache/plm \
    PLM_DATA_ROOT=/data

EXPOSE 8097 8098 8092
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "plm.api:app", "--host", "0.0.0.0", "--port", "8097"]
