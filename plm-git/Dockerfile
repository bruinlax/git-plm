# plm-git — git + python, nothing else.
#
# The service is stdlib-only by design: `git http-backend` is the git server and
# the Python is a CGI bridge, so there is no pip step, no wheel to build, and
# no dependency drift between the image and the host. `git` here must be the
# real thing (http-backend is part of core git, not git-core-extras).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# git refuses to operate on repos owned by another uid unless told they're safe.
# The vault is mounted read-only and owned by the host CAD user, and the
# container runs as that uid anyway (compose `user:`), but the mirror clone
# reads the vault's .git directly so make the exception explicit.
RUN git config --system --add safe.directory '*'

WORKDIR /app
COPY plm_git /app/plm_git

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

EXPOSE 8099
CMD ["python", "-m", "plm_git.server"]
