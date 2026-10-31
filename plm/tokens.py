"""The machine plane's credential — ONE token store, read the same way twice.

⭐ WHAT THIS IS. `plm-git` has always had a token map: a file of
`<token>=<plm username>` lines, mounted `:ro` at `/run/secrets/push-tokens`,
that says which plm actor is holding a bearer token. A `git push` has needed one
since stage 3. The HTTP API and the MCP did not — which is C2 in
`the security review`: "every state-changing API is
unauthenticated, and turning OIDC on does not change that".

Stage 7 makes that one store the credential for **all three doors**. Same file,
same lines, same actors: a token that may push a revision is a token that may
call `POST /api/items/{n}/checkin`, and one that may not, may not. There is no
second list to keep in step, and `plm_git.push.tokens()` now reads through this
module so the git container and the API cannot disagree about who a token is.

THE THREE PLANES, after this (docs/PLM_IDENTITY.md's shape, unchanged in
spirit):

    humans   an OIDC session          -> identity.identity_of(), a `users` row
    machines a bearer token from HERE -> a `users` row, by name
    nobody   no credential            -> reads only, and not the ones that
                                        carry somebody's email or a secret

⚠️ A TOKEN IS AN ACTOR, NOT A PERMISSION. This module answers "whose token is
this?" and stops. Whether that actor may edit, approve or release is
`plm/db.py`'s roles and `plm/policy.py`'s policy, exactly as for a human — which
is the same two-layer split `plm_git.push.identify` has always made, and the
reason a stolen bot token still cannot approve a release.

⚠️ COMPARISON IS CONSTANT-TIME (L6). `push.identify` did `tokens().get(token)`;
a dict lookup is not the byte-by-byte oracle a bare `==` is, but
`POST /api/git/index` next door already used `hmac.compare_digest` and the two
should not differ. Every comparison here is `hmac.compare_digest`, over every
entry, with no early exit.
"""
from __future__ import annotations

import hmac
import os
import re
from pathlib import Path

#: Where the map lives. The default is the path the POC's compose already
#: mounts into the git container; stage 7 mounts it into the API and MCP
#: containers too, `:ro`. `PLM_TOKENS` (a value) is honoured for a laptop.
TOKENS_FILE = os.environ.get("PLM_TOKENS_FILE", "/run/secrets/push-tokens")
TOKENS_ENV = "PLM_TOKENS"

#: Re-read at most this often. A token added to the file should work without a
#: restart (that is half the point of a file), and re-reading it on every
#: request would be a syscall per API call.
TTL_S = float(os.environ.get("PLM_TOKENS_TTL", "30"))

_cache: dict[str, str] = {}
_cache_at = -1.0


def parse(raw: str) -> dict[str, str]:
    """`<token>=<plm username>` lines -> {token: actor}.

    Line-oriented, and comments are stripped PER LINE — a naive whitespace split
    turns `# <token>=<plm username>` in the sample header into a real entry
    called `"<plm"`, which is exactly the kind of quiet nonsense an auth map must
    not contain. (That rule is `plm_git.push.tokens`'s, learned there; this is
    the same code, in the one place both callers now read.)
    """
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.split("#", 1)[0].strip()
        for chunk in re.split(r"[,\s]+", line):
            if not chunk or "=" not in chunk:
                continue
            tok, _, actor = chunk.partition("=")
            tok, actor = tok.strip(), actor.strip()
            if tok and actor:
                out[tok] = actor
    return out


def load(*, path: str = "", env_raw: str = "", force: bool = False
         ) -> dict[str, str]:
    """The map, cached for `TTL_S`. Never raises: an unreadable file is an
    EMPTY map, which fails closed — no token matches, everything that needs one
    answers 401, and the reason is in the refusal text rather than in a 500."""
    global _cache, _cache_at
    import time
    if not force and path == "" and env_raw == "" \
            and (time.monotonic() - _cache_at) < TTL_S:
        return _cache
    raw = env_raw or os.environ.get(TOKENS_ENV, "")
    p = path or TOKENS_FILE
    if p:
        try:
            raw = Path(p).read_text() + "\n" + raw
        except OSError:
            pass
    out = parse(raw)
    if path == "" and env_raw == "":
        _cache, _cache_at = out, time.monotonic()
    return out


def configured() -> bool:
    """Is there any token at all? An instance with none cannot authenticate a
    machine, and `PLM_AUTH_MODE=required` says so in its 401 instead of leaving
    somebody guessing at a password."""
    return bool(load())


def actor_in(table: dict[str, str], token: str) -> str:
    """The plm username a bearer token belongs to in `table`, or ''.

    ⛔ CONSTANT-TIME AND NO EARLY EXIT (L6). Every entry is compared; the loop
    does not stop at the first match, so the time this takes depends on how many
    tokens exist and not on how many leading bytes the caller guessed right.
    """
    token = (token or "").strip()
    if not token:
        return ""
    found = ""
    for candidate, actor in table.items():
        if hmac.compare_digest(candidate, token):
            found = actor
    return found


def actor_for(token: str) -> str:
    """`actor_in` against this process's own store."""
    return actor_in(load(), token)


def bearer_of(headers) -> str:
    """The token out of an `Authorization` header — Bearer or Basic.

    Basic is here because that is what git's credential helper sends: `git push
    https://user:$TOKEN@host/...` and `credential.helper` both arrive as Basic,
    and plm-git has accepted them since stage 3. The API accepts the same shapes
    so ONE token works in curl, in git and in an MCP client.
    """
    auth = ""
    for k, v in (headers.items() if hasattr(headers, "items") else headers):
        if k.lower() == "authorization":
            auth = v or ""
            break
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.lower().startswith("basic "):
        import base64
        try:
            userpass = base64.b64decode(auth[6:].strip()).decode()
        except Exception:  # noqa: BLE001
            return ""
        user, _, pw = userpass.partition(":")
        return pw or user
    return ""


def describe() -> dict:
    """What an operator needs to debug auth, and NOT ONE TOKEN. Counts and
    actor names only — the names are already public (they are `users` rows);
    the tokens are the secret and never leave this module."""
    m = load()
    return {"source": TOKENS_FILE, "configured": bool(m), "tokens": len(m),
            "actors": sorted(set(m.values()))}
