"""Verified identity for plm — "you need to know who is approving things".

Until now the release gate trusted a client-supplied `actor` string: any curl
could claim to be Geoff and approve its own submission. That is fine for a
trusted-network toy and not fine for a release process, which is a record of
*who decided what*.

This module supplies the OIDC identity (any provider: OIDC_ISSUER) and one
rule: **when a verified identity is present, it wins over anything the client
said.** The override is applied in a single middleware (`install`), not at each
call site, because a security invariant that has to be remembered twelve times
is an invariant that will eventually be forgotten.

Integration follows the pattern the other consolidated services use — the same
one a file-drop service was built on, and *not* the
`X-Auth-Request-User` proxy plan sketched in PLM_IDENTITY.md, because no such proxy exists in this deployment yet
(oauth2-proxy is Phase 2 of the auth-consolidation project):

* authorization-code flow with PKCE, implemented in-app;
* identity resolved by calling **`/oauth/userinfo`** with the access token —
  access tokens here are opaque, so userinfo is the only way to resolve them;
* an **in-app group check**, because the authorization server does not enforce a
  client's `required_group` (it only filters portal tiles);
* server-side sessions, so a logout is real.

Trusted identity headers are also honored *if explicitly enabled*, so that when
the Phase 2 proxy does land this needs configuration rather than code.

The three planes, unchanged in spirit from PLM_IDENTITY.md:

  humans  OIDC session  -> users row matched on `sub`, auto-provisioned, roles
                           granted deliberately in the users table
  bots    as today      -> body/Bearer actor on the private network; may do all the
                           legwork, may never decide an approval
  nobody  no identity   -> reads
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse

from . import config, db, tokens as tokenstore


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def _env_secret(*names: str) -> str:
    """`<NAME>_FILE` then `<NAME>`, for each name in turn — `config.secret`'s
    job, which is now the one implementation of it (H3)."""
    for name in names:
        got = config.secret(name)
        if got:
            return got.strip()
    return ""


# No default issuer: empty = OIDC unconfigured (configured() is then False and
# the login route refuses by name).
ISSUER = _env("OIDC_ISSUER", "PLM_OIDC_ISSUER")
CLIENT_ID = _env("OIDC_CLIENT_ID", "PLM_OIDC_CLIENT_ID")
CLIENT_SECRET = _env_secret("OIDC_CLIENT_SECRET", "PLM_OIDC_CLIENT_SECRET")
PUBLIC_URL = config.PUBLIC_URL
REDIRECT_URI = _env("OIDC_REDIRECT_URI", "PLM_OIDC_REDIRECT_URI",
                    default=f"{PUBLIC_URL}/auth/callback")
# `groups` must stay in scope: the claim is scope-gated at the provider.
SCOPES = _env("OIDC_SCOPES", default="openid profile email groups")
ACCESS_GROUPS = [g.strip().lower() for g in
                 _env("OIDC_ACCESS_GROUP", default="plm-users,ldap-admins,admins")
                 .replace(" ", ",").split(",") if g.strip()]

TRUSTED_HEADERS = _bool("PLM_OIDC_TRUSTED_HEADERS", False)
HEADER_USER = os.environ.get("PLM_OIDC_HEADER_USER", "x-auth-request-user")
HEADER_EMAIL = os.environ.get("PLM_OIDC_HEADER_EMAIL", "x-auth-request-email")
HEADER_GROUPS = os.environ.get("PLM_OIDC_HEADER_GROUPS", "x-auth-request-groups")

SESSION_COOKIE = "plm_session"
SESSION_TTL = int(os.environ.get("PLM_SESSION_TTL", str(12 * 3600)))

# The switchover switch. Default: ON exactly when OIDC is configured, so wiring the
# client id/secret is the single act that turns enforcement on, and an
# unconfigured instance (tests, the sandbox, local dev) keeps working as before.
_REQUIRE_RAW = os.environ.get("PLM_REQUIRE_OIDC_APPROVALS")

# =============================================================================
# C2 — THE AUTH MODE. The one switch that says whether this instance believes
# its own network.
# =============================================================================
#
# What the review found: `/api/` was in PUBLIC_PREFIXES, the middleware's ONLY
# enforcement was a 302 to /auth/login for HTML GETs, and POSTs were excluded
# outright — so every write was anonymous, and turning OIDC on did not change
# that. All three ports were on 0.0.0.0, LAN-wide, while the design docs justified
# running without OIDC because it was "private-network-only on a high port".
#
#   required          (the DEFAULT) a write needs a credential. An OIDC session
#                     or a machine token from plm/tokens.py; anything else is
#                     401 with a WWW-Authenticate, so a client knows what to
#                     present rather than discovering it wrote nothing.
#   trusted-network   the pre-stage-7 behaviour, kept because it is what the
#                     August instance and the sandbox actually run, and because
#                     a mode that only exists in a comment is one nobody can
#                     turn on deliberately. ⚠️ It means "every client that can
#                     reach this port may approve a release", and the /healthz
#                     and /auth/whoami payloads say so in those words.
#
# It is ONE variable whose name says what it is — not a
# combination of PLM_REQUIRE_OIDC_APPROVALS, an unset OIDC_CLIENT_ID and a bind
# address, which is what it used to be.
AUTH_MODE = (os.environ.get("PLM_AUTH_MODE") or "required").strip().lower()
AUTH_MODES = ("required", "trusted-network")
if AUTH_MODE not in AUTH_MODES:
    print(f"plm: PLM_AUTH_MODE={AUTH_MODE!r} is not one of "
          f"{', '.join(AUTH_MODES)} — falling back to 'required'", flush=True)
    AUTH_MODE = "required"


def auth_required() -> bool:
    return AUTH_MODE == "required"


# Paths that never require a session. ⛔ `/api/` LEFT THIS LIST in stage 7 (C2)
# and `/mcp` with it: they are the machine plane, and "the machine plane
# authenticates itself" is a statement about HOW, not about WHETHER.
PUBLIC_PREFIXES = ("/auth/", "/blob", "/thumb", "/static/",
                   "/assets/", "/favicon")
# The machine plane: never bounced to a login PAGE (that is for humans); under
# `required` an anonymous write or protected GET there is a 401 instead.
MACHINE_PREFIXES = ("/api/", "/mcp")
#: PUT routes whose body is RAW FILE BYTES, never JSON — the upload inbox
#: (`curl -f -T`, plm/staging.py) and the raw file upload. The actor rewrite
#: must not read them: it would pull a 190 MB mesh into memory to learn it is
#: not a JSON object. Both routes take their actor from the verified identity.
RAW_BODY_PUTS = re.compile(r"^/api/(uploads/[^/]+|items/[^/]+/files/[^/]+)$")

# GETs that need a credential even though they only read, because of WHAT they
# read. `/api/integrations` is C1's endpoint (its rows are redacted now, and it
# still lists which hosts this plm talks to); `/api/users` is L3's (every user
# row raw — sub, email, roles); `/api/events` and `/api/activity` carry event
# payloads, which is where M4's second copy of the printer key lived. Everything
# else — items, repos, blobs, compare, checks — stays anonymous on purpose: the
# POC's whole point is a registry you can browse, and a read-only surface that
# needed a token would not be one.
PROTECTED_GETS = ("/api/integrations", "/api/users", "/api/events",
                  "/api/activity", "/api/git/index")

_DISCOVERY: dict | None = None
_DISCOVERY_AT = 0.0
_FALLBACK = {
    "authorization_endpoint": "/oauth/authorize",
    "token_endpoint": "/oauth/token",
    "userinfo_endpoint": "/oauth/userinfo",
}
# Bearer -> (claims, expiry). Keeps the machine plane from making a userinfo
# round trip on every request.
_TOKEN_CACHE: dict[str, tuple[dict, float]] = {}
_TOKEN_TTL = 300.0


class IdentityError(Exception):
    pass


def configured() -> bool:
    return bool(ISSUER and CLIENT_ID and CLIENT_SECRET) or TRUSTED_HEADERS


def require_oidc_approvals() -> bool:
    if _REQUIRE_RAW is not None:
        return _REQUIRE_RAW.strip().lower() in ("1", "true", "yes", "on")
    return configured()


def discovery() -> dict:
    global _DISCOVERY, _DISCOVERY_AT
    if _DISCOVERY and (time.time() - _DISCOVERY_AT) < 3600:
        return _DISCOVERY
    issuer = ISSUER.rstrip("/")
    doc: dict = {"issuer": issuer}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(issuer + "/.well-known/openid-configuration")
            resp.raise_for_status()
            doc = resp.json()
    except Exception:  # noqa: BLE001 — fall back to the documented layout
        doc["_from_fallback"] = True
    for name, path in _FALLBACK.items():
        doc.setdefault(name, issuer + path)
    _DISCOVERY, _DISCOVERY_AT = doc, time.time()
    return doc


# -- where a sign-in lands: `next` --------------------------------------------
#
# THE BUG (2026-10-16): a user clicked https://plm.example.com/#/change/CHG-380
# with no session. The middleware answered `GET /` with a 302 to
# /auth/login?next=/ — the server never sees a `#fragment`, so the one thing that
# said WHERE he was going was gone before plm ever ran, and the round trip ended
# on the dashboard. The fix has two halves:
#
#   * the browser half: an anonymous HTML GET gets SIGNIN_SHELL (200) instead of
#     a 302. Its script reads `location.pathname + search + hash` — the fragment
#     included, which only the browser has — and goes to /auth/login with that
#     as `next`. The UI's own "Sign in" link does the same.
#   * this half: `next` may carry a fragment (`/#/change/CHG-380`), is stored in
#     the OIDC state row as-is, and comes back VERBATIM on the callback's
#     redirect — as long as it is a same-origin relative location.
#
# Path-agnostic on purpose: a real path (`/CID-ASM-0001/pull/380`) is just a
# `next` with no fragment and takes exactly the same road. Since stage 22 the UI
# IS real paths (plm/webroutes.py), so the server sees the whole location and
# the fragment half only matters for an old `/#/…` link — which the shell still
# carries, and the UI then maps to its path.
#
# ⛔ THE OPEN-REDIRECT GUARD. Before this it was `next.startswith("/")`, which
# let `//evil.example` through — a scheme-relative URL, i.e. a redirect off-site
# after a real sign-in. A `next` is accepted only if it is ONE leading `/` and
# nothing a browser could read as another origin:
#   - no `//` prefix (scheme-relative), no `\` anywhere (browsers treat `/\x`
#     as `//x`), no scheme, no netloc;
#   - no control characters at all (CR/LF would split headers; a browser
#     silently DELETES tab/CR/LF from a URL, so `/\t/evil` would become
#     `//evil`), no DEL;
#   - bounded length.
# Anything else is `/`. It is applied when the login starts AND when the state
# row is read back, so a row written by an older code line is held to it too.
NEXT_MAX = 2048


def safe_next(raw) -> str:
    """`raw` if it is a same-origin relative location, else "/"."""
    if not isinstance(raw, str) or not raw or len(raw) > NEXT_MAX:
        return "/"
    if raw[0] != "/" or raw.startswith("//") or "\\" in raw:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return "/"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return raw


# The page an anonymous browser gets instead of a 302 (see above). Tiny,
# self-contained, never cached (`no-store`: the same URL is the app once the
# cookie exists). `noscript` falls back to what the 302 used to do — the path
# and query the server can see, fragment lost. A loop guard: if the browser was
# sent to sign in from here less than 20 s ago and is back without a session
# (cookie refused, clock skew), it stops and shows a link instead of bouncing
# between plm and the identity provider forever.
SIGNIN_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>plm — signing in</title>
<noscript><meta http-equiv="refresh" content="0;url=__FALLBACK__"></noscript>
<style>body{font:15px system-ui,sans-serif;margin:3rem auto;max-width:32rem;
padding:0 1rem;color:#222;background:#fff}a{color:#0b5fff}</style>
</head><body>
<p id="msg">Signing in&hellip;</p>
<p><a id="go" href="__FALLBACK__">Sign in to plm</a></p>
<script>
(function () {
  var here = location.pathname + location.search + location.hash;
  var url = '/auth/login?next=' + encodeURIComponent(here);
  document.getElementById('go').href = url;
  var key = 'plm.signin_bounce', now = Date.now(), last = 0;
  try { last = +sessionStorage.getItem(key) || 0; } catch (e) {}
  if (now - last < 20000) {
    document.getElementById('msg').textContent =
      'Sign-in did not stick (is this browser refusing the session cookie?).';
    try { sessionStorage.removeItem(key); } catch (e) {}
    return;
  }
  try { sessionStorage.setItem(key, String(now)); } catch (e) {}
  location.replace(url);
})();
</script>
</body></html>
"""


def signin_shell(path_and_query: str) -> HTMLResponse:
    """The 200 sign-in shell for an anonymous HTML GET of `path_and_query`."""
    from html import escape
    from urllib.parse import quote
    fallback = "/auth/login?next=" + quote(safe_next(path_and_query), safe="")
    return HTMLResponse(
        SIGNIN_SHELL.replace("__FALLBACK__", escape(fallback, quote=True)),
        status_code=200,
        headers={"Cache-Control": "no-store", "Vary": "Cookie",
                 "X-PLM-Signin": "required"})


# -- login flow ---------------------------------------------------------------

def begin_login(conn: sqlite3.Connection, redirect_to: str = "/") -> str:
    if not CLIENT_ID or not ISSUER:
        raise IdentityError("OIDC_ISSUER / OIDC_CLIENT_ID are not set; register "
                            "plm with your OIDC provider first (DEPLOY.md § "
                            "Auth modes)")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_to = safe_next(redirect_to)
    conn.execute(
        "INSERT INTO oidc_flows (state,nonce,verifier,redirect_to,created_at) "
        "VALUES (?,?,?,?,?)", (state, nonce, verifier, redirect_to, db.now()))
    conn.execute("DELETE FROM oidc_flows WHERE created_at < ?", (db.now() - 900,))
    params = {
        "response_type": "code", "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state,
        "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{discovery()['authorization_endpoint']}?{urlencode(params)}"


def complete_login(conn: sqlite3.Connection, code: str, state: str) -> tuple[str, str]:
    row = conn.execute("SELECT * FROM oidc_flows WHERE state=?", (state,)).fetchone()
    if not row:
        raise IdentityError("unknown or expired login state — try again")
    conn.execute("DELETE FROM oidc_flows WHERE state=?", (state,))
    if db.now() - row["created_at"] > 900:
        raise IdentityError("login state expired — try again")

    data = {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID,
        "code_verifier": row["verifier"],
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET  # client_secret_post
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(discovery()["token_endpoint"], data=data,
                               headers={"Accept": "application/json"})
            if resp.status_code >= 400:
                raise IdentityError(f"token endpoint returned {resp.status_code}: "
                                    f"{resp.text[:300]}")
            access_token = (resp.json() or {}).get("access_token")
        if not access_token:
            raise IdentityError("the token response carried no access_token")
        claims = userinfo(access_token)
    except IdentityError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IdentityError(f"token exchange failed: {exc}") from exc

    allowed, reason = user_allowed(claims)
    if not allowed:
        raise IdentityError(reason)

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise IdentityError("the provider returned no subject")
    session_id = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (id,sub,email,name,groups,created_at,expires_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (session_id, sub, str(claims.get("email") or ""),
         str(claims.get("name") or claims.get("preferred_username") or sub),
         ",".join(normalize_groups(claims.get("groups"))),
         db.now(), db.now() + SESSION_TTL))
    # Bind the identity to a users row now, so the first login is also the
    # provisioning event and the approvals UI has somebody to name.
    resolve_user(conn, {"sub": sub, "email": claims.get("email") or "",
                        "name": claims.get("name") or sub,
                        "groups": ",".join(normalize_groups(claims.get("groups")))})
    return session_id, safe_next(row["redirect_to"])


def userinfo(access_token: str) -> dict:
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(discovery()["userinfo_endpoint"],
                          headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code >= 400:
            raise IdentityError(f"userinfo returned {resp.status_code}")
        return resp.json() or {}


def normalize_groups(raw) -> list[str]:
    if raw is None:
        return []
    items = ([g.strip() for g in raw.replace(";", ",").split(",") if g.strip()]
             if isinstance(raw, str)
             else [str(g).strip() for g in raw if str(g).strip()])
    out: list[str] = []
    for g in items:
        out.append(g)
        if g.lower().startswith("cn="):
            out.append(g[3:].split(",", 1)[0])
    return list(dict.fromkeys(out))


def user_allowed(claims: dict) -> tuple[bool, str]:
    """The in-app group gate the authorization server does not perform."""
    if not ACCESS_GROUPS:
        return True, ""
    have = {g.lower() for g in normalize_groups(claims.get("groups"))}
    if any(g in have for g in ACCESS_GROUPS):
        return True, ""
    who = claims.get("email") or claims.get("sub") or "you"
    return False, (f"{who} is not in plm's access group "
                   f"({', '.join(ACCESS_GROUPS)}). Ask an admin to add you.")


# -- sessions + resolution ----------------------------------------------------

def session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < db.now():
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return None
    return dict(row)


def end_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def identity_of(request, conn: sqlite3.Connection) -> dict | None:
    """The verified identity behind this request, or None.

    Order: an explicitly-enabled trusted header, then a session cookie, then a
    provider access token presented as a Bearer (the resource-server
    pattern, so an agent holding a real token is a real human/bot identity).
    """
    if TRUSTED_HEADERS:
        user = request.headers.get(HEADER_USER)
        if user:
            return {"sub": user, "email": request.headers.get(HEADER_EMAIL) or "",
                    "name": user,
                    "groups": request.headers.get(HEADER_GROUPS) or "",
                    "via": "oidc-header"}

    row = session(conn, request.cookies.get(SESSION_COOKIE, ""))
    if row:
        return {"sub": row["sub"], "email": row["email"], "name": row["name"],
                "groups": row["groups"], "via": "oidc-session"}

    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer ") and configured():
        token = auth_header[7:].strip()
        if tokenstore.actor_for(token) or (
                config.GIT_INDEX_TOKEN and hmac.compare_digest(token, config.GIT_INDEX_TOKEN)):
            # One of OUR tokens — a machine token (plm/tokens.py, machine_identity())
            # or the git container's index token (service_identity()). They own it.
            # Never forward it to the provider — that leaked plm's secret to
            # userinfo and logged an auth.fail there per request (2026-10-16:
            # agent git pushes paged Geoff as privileged-auth-fail /
            # success-after-failures,/422).
            return None
        cached = _TOKEN_CACHE.get(token)
        if cached and cached[1] > time.time():
            claims = cached[0]
        else:
            try:
                claims = userinfo(token)
            except Exception:  # noqa: BLE001 — not a provider token
                return None
            allowed, _ = user_allowed(claims)
            if not allowed:
                return None
            _TOKEN_CACHE[token] = (claims, time.time() + _TOKEN_TTL)
        return {"sub": str(claims.get("sub") or ""),
                "email": str(claims.get("email") or ""),
                "name": str(claims.get("name") or claims.get("sub") or ""),
                "groups": ",".join(normalize_groups(claims.get("groups"))),
                "via": "oidc-bearer"}
    return None


def resolve_user(conn: sqlite3.Connection, identity: dict) -> dict:
    """Map a verified identity to a `users` row, provisioning on first sight.

    Matched on `sub` first (the durable key), then on username — so the seeded
    `geoff` row adopts the OIDC subject the first time he signs in and keeps his
    approver role, rather than a second `geoff` appearing with no roles.
    A brand-new person is provisioned with **no roles**: being able to log in is
    not the same as being able to approve.
    """
    sub = str(identity.get("sub") or "").strip()
    email = str(identity.get("email") or "")
    display = str(identity.get("name") or sub)

    row = conn.execute("SELECT * FROM users WHERE sub=? AND sub!=''", (sub,)).fetchone()
    if row:
        user = dict(row)
        if email and not user["email"]:
            conn.execute("UPDATE users SET email=? WHERE id=?", (email, user["id"]))
        return user

    row = conn.execute("SELECT * FROM users WHERE username=?", (sub,)).fetchone()
    if row:
        conn.execute("UPDATE users SET sub=?, email=COALESCE(NULLIF(email,''),?), "
                     "display=COALESCE(NULLIF(display,''),?) WHERE id=?",
                     (sub, email, display, row["id"]))
        db.add_event(conn, "identity.bound", actor=sub,
                     data={"username": row["username"], "sub": sub})
        return dict(conn.execute("SELECT * FROM users WHERE id=?",
                                 (row["id"],)).fetchone())

    conn.execute(
        "INSERT INTO users (username, sub, display, kind, email, roles, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (sub, sub, display, "human", email, "", db.now()))
    db.add_event(conn, "identity.provisioned", actor=sub,
                 data={"username": sub, "email": email, "roles": ""})
    return dict(conn.execute("SELECT * FROM users WHERE username=?", (sub,)).fetchone())


#: The name the git container's service credential answers to. It is the
#: `PLM_GIT_INDEX_TOKEN` — the shared secret `POST /api/git/index` has always
#: required — and it is a SERVICE credential, not an actor's.
SERVICE_NAME = "plm-git"


def service_identity(request) -> dict | None:
    """Is this the git container, holding the index token?

    ⭐ WHY THIS IS A THIRD KIND OF PRINCIPAL, and why it may still name an actor
    in a request body when nobody else may (C2's carve-out, stated).

    The git transport authenticates a push ITSELF: `plm_git.push.identify` maps
    the pusher's bearer token to a plm actor and refuses anything plm does not
    vouch for. The hook then has an identity it established but not the token
    that proved it — deliberately, so a receive-pack child never holds a
    credential. When it calls `POST /api/items/{n}/submit` on that pusher's
    behalf it therefore has to SAY who, and be believed.

    That is not a new grant. The holder of this token could already index any
    commit as any actor (`POST /api/git/index`), which the review noted is
    "inside the trust boundary the token defines". It lives in a file mounted
    `:ro` into one container, it is not in the actor token map, and it cannot be
    used as an actor — `plm_actor` stays None, so `_rewrite_actor` does not fire
    and the body's `actor` stands.
    """
    want = config.GIT_INDEX_TOKEN
    got = tokenstore.bearer_of(request.headers)
    if not (want and got) or not hmac.compare_digest(got, want):
        return None
    return {"sub": "", "email": "", "name": SERVICE_NAME, "groups": "",
            "via": "service-token", "service": SERVICE_NAME}


def machine_identity(request) -> dict | None:
    """The identity behind a machine token, or None.

    ⭐ THE SECOND PLANE (C2). A bearer token from `plm/tokens.py` names a plm
    actor exactly as an OIDC session does, and is treated as verified for the
    same reason: the SERVER established it. What that actor may then do is
    `users.roles` and `plm/policy.py`, unchanged — a bot token still cannot
    satisfy a human-only approval, it just can no longer be an anonymous string
    in a JSON body either.
    """
    actor = tokenstore.actor_for(tokenstore.bearer_of(request.headers))
    if not actor:
        return None
    return {"sub": actor, "email": "", "name": actor, "groups": "",
            "via": "machine-token"}


def verified_actor(request, conn: sqlite3.Connection) -> str | None:
    """The username a verified identity maps to, or None if unverified."""
    identity = getattr(request.state, "plm_identity", None)
    if identity is None:
        identity = identity_of(request, conn) or machine_identity(request)
    if not identity:
        return None
    if identity.get("via") == "machine-token":
        # A machine token names an EXISTING plm user; it does not provision one.
        # Auto-provisioning from a token would let whoever writes the token file
        # mint actors, and the token file is operations, not user administration.
        row = db.get_user(conn, identity["sub"])
        return row["username"] if row else None
    return resolve_user(conn, identity)["username"]


def describe() -> dict:
    out = {
        "auth_mode": AUTH_MODE,
        "machine_tokens": tokenstore.describe(),
        "configured": configured(),
        "issuer": ISSUER,
        "client_id": CLIENT_ID or None,
        "client_secret_set": bool(CLIENT_SECRET),
        "redirect_uri": REDIRECT_URI,
        "scopes": SCOPES,
        "access_groups": ACCESS_GROUPS,
        "trusted_headers": TRUSTED_HEADERS,
        "require_oidc_approvals": require_oidc_approvals(),
    }
    # ⛔ NO LIVE CALL FROM AN UNAUTHENTICATED PATH (L10). `describe()` is
    # reached from `GET /healthz`, which anybody may call, and it used to run
    # `discovery()` — a real outbound GET to the operator's IdP
    # (auth.example.com), triggerable by anyone, from a POC that is not supposed
    # to talk to the IdP at all. When OIDC is not configured there is nothing to
    # discover, so it does not ask; when it IS configured this reports the
    # CACHED answer and never warms it.
    if configured():
        out["provider_reachable"] = (
            None if _DISCOVERY is None
            else not _DISCOVERY.get("_from_fallback", False))
    else:
        out["provider_reachable"] = None
        out["why"] = ("OIDC is not configured on this instance, so no request "
                      "is made to the identity provider.")
    return out


# -- the middleware: one enforcement point ------------------------------------

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _unauthorized(request, why: str) -> JSONResponse:
    """401, in the tone the rest of this codebase refuses in: what happened,
    what to present, and where the credential comes from.

    `WWW-Authenticate` is not decoration — without it a client cannot tell which
    scheme to use, and `git`'s credential helper never runs (the same lesson
    `plm-git/plm_git/server.py` learned on the push door). ASCII only: header
    values are latin-1 on the wire.
    """
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="plm", charset="UTF-8"'},
        content={"detail":
                 f"{why}\n"
                 "\n"
                 "  plm's machine plane authenticates with a bearer token — the "
                 "SAME token that\n  pushes a revision "
                 "(plm/tokens.py, PLM_TOKENS_FILE):\n"
                 "\n"
                 "    curl -H \"Authorization: Bearer $PLM_TOKEN\" ...\n"
                 "    git -c http.extraHeader=\"Authorization: Bearer "
                 "$PLM_TOKEN\" push ...\n"
                 "\n"
                 "  A human signs in instead: GET /auth/login.\n"
                 "  The token names a plm ACTOR; what that actor may do is its "
                 "roles and\n  the approval policy (GET /api/policy), "
                 "unchanged.\n"
                 f"\n  This instance is PLM_AUTH_MODE={AUTH_MODE}. "
                 "GET /auth/whoami says what it sees."})


class IdentityMiddleware(BaseHTTPMiddleware):
    """Resolve identity once, then make it authoritative. **And refuse.**

    Three jobs:

    1. **Establish who is calling.** An OIDC session, an OIDC bearer, or a
       machine token from `plm/tokens.py`. One resolution, on every request,
       whether or not OIDC is configured — it used to run only when OIDC was,
       which is why the machine plane had no identity at all.
    2. **Refuse an unauthenticated write** (C2). Under
       `PLM_AUTH_MODE=required`, any POST/PUT/PATCH/DELETE without an identity
       is 401, and so is a GET of a path that carries somebody's email or an
       integration's configuration. This is the whole fix: the middleware now
       HAS a reject path. Before, its sole enforcement was a 302 for HTML GETs.
    3. **Override the client-supplied actor.** For a JSON write carrying an
       `actor` field, the verified identity replaces whatever was sent — in one
       place, so no future endpoint can forget it.
    """

    async def dispatch(self, request, call_next):
        request.state.plm_identity = None
        request.state.plm_actor = None
        request.state.plm_service = ""

        svc = service_identity(request)
        if svc:
            request.state.plm_service = svc["service"]
            request.state.plm_identity = svc

        # ⚠️ NOT GATED ON `configured()` ANY MORE. Identity resolution used to
        # run only when OIDC was wired up, so on this POC — which deliberately
        # has no OIDC — nothing ever established anybody, and every write was
        # whatever string the body said.
        try:
            with db.connect() as conn:
                identity = identity_of(request, conn)
                if identity:
                    request.state.plm_identity = identity
                    request.state.plm_actor = resolve_user(
                        conn, identity)["username"]
                else:
                    identity = machine_identity(request)
                    if identity:
                        row = db.get_user(conn, identity["sub"])
                        if row:
                            request.state.plm_identity = identity
                            request.state.plm_actor = row["username"]
                        else:
                            request.state.plm_token_unknown = identity["sub"]
        except Exception:  # noqa: BLE001 — identity must never 500 a read
            pass

        path = request.url.path
        actor = request.state.plm_actor
        method = request.method

        bad_tok = getattr(request.state, "plm_token_unknown", "")
        if bad_tok and not request.state.plm_service \
                and method in WRITE_METHODS and not path.startswith(("/auth/",)):
            # ⛔ STAGE 19 — IN EVERY AUTH MODE. Under `trusted-network` an
            # anonymous write is allowed and the body's `actor` stands — but a
            # request that PRESENTS a credential is not anonymous. Its token
            # names `codex`, there is no `codex` user, and letting the body's
            # `actor: "agent"` stand would attribute Codex's write to `agent`:
            # the caller-supplied actor winning over the token.
            return _unauthorized(
                request,
                f"that token maps to the plm actor {bad_tok!r}, and this "
                "instance has no such user.\nA token names somebody plm "
                "already knows; it does not create them, and it is not "
                "ignored in favour of the body's `actor`.")
        if not actor and not request.state.plm_service and auth_required():
            bad = getattr(request.state, "plm_token_unknown", "")
            if bad:
                return _unauthorized(
                    request,
                    f"that token maps to the plm actor {bad!r}, and this "
                    "instance has no such user.\nA token names somebody plm "
                    "already knows; it does not create them.")
            if method in WRITE_METHODS and not path.startswith(("/auth/",)):
                return _unauthorized(
                    request,
                    f"{method} {path} changes something, and this instance "
                    "does not accept anonymous writes.")
            if method in ("GET", "HEAD") and path.startswith(PROTECTED_GETS):
                return _unauthorized(
                    request,
                    f"{path} carries configuration or personal data "
                    "(integration endpoints, user\nemails, event payloads), so "
                    "it is not part of the anonymous read surface.")

        if actor and method in ("POST", "PUT", "PATCH") \
                and not (method == "PUT" and RAW_BODY_PUTS.match(path)):
            request = await self._rewrite_actor(request, actor)

        # Sign-in is for the HUMAN plane only. `/api/` and `/mcp` left
        # PUBLIC_PREFIXES for C2, but C2's enforcement is the 401 above; a
        # browser opening an API link (a file download, the /api masthead link)
        # is answered as master answered it, not bounced to a login page
        # (stage 9b, the OIDC compat test).
        #
        # ⚠️ 200 + SIGNIN_SHELL, NOT A 302 (2026-10-16). A 302 is decided
        # before the browser runs anything, and the browser never sends the
        # `#/change/CHG-380` part — so every deep link landed on the dashboard
        # after sign-in. The shell's script reads the fragment and carries it
        # as `next`. See `safe_next` above.
        if configured() and not actor and method == "GET" \
                and not path.startswith(PUBLIC_PREFIXES + MACHINE_PREFIXES) \
                and "text/html" in (request.headers.get("accept") or ""):
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return signin_shell(nxt)

        return await call_next(request)

    @staticmethod
    async def _rewrite_actor(request, actor: str):
        """Replace `actor` in a JSON body with the verified username.

        ⚠️ NO LONGER CONTENT-TYPE GATED (L1). It used to return early unless the
        header said `application/json` — and FastAPI parses a body as JSON when
        the header is ABSENT, or when the subtype ends `+json`, so omitting
        Content-Type kept the client's `actor` through a verified session. The
        test is now "does this body parse as a JSON object", which is the same
        question the endpoint's own parser asks.
        """
        content_type = (request.headers.get("content-type") or "").lower()
        if content_type and "json" not in content_type \
                and content_type.split(";")[0].strip() not in ("", "text/plain"):
            return request
        body = await request.body()
        if not body:
            return request
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return request
        if not isinstance(payload, dict) or payload.get("actor") == actor:
            return request
        payload["actor"] = actor
        new_body = json.dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": new_body, "more_body": False}

        request._receive = receive  # noqa: SLF001 — the documented way to do this
        request._body = new_body    # noqa: SLF001
        headers = dict(request.scope["headers"])
        headers[b"content-length"] = str(len(new_body)).encode()
        request.scope["headers"] = list(headers.items())
        return request


def install(app) -> None:
    app.add_middleware(IdentityMiddleware)
