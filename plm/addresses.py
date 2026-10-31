"""Do the addresses plm HANDS OUT actually answer? (stage 15, finding 1)

Every link plm gives an agent or a human — `get_item`'s `git.clone_url`, the
push output's change URL, the browse/compare links — is built from three
configured bases:

  public   config.PUBLIC_URL   change / proposal / release-set pages
  web      gitlinks.WEB_BASE   browse_url, compare_url
  clone    gitrepo.CLONE_BASE  clone_url (git smart HTTP)

Two cold agents (2026-10-05) were each handed `http://localhost:8194/…`. On
the host itself `the host` resolves to 127.0.1.1 (Debian's /etc/hosts
line) and nothing listens there — the POC binds 127.0.0.1, the LAN address and
the private-network address — so both burned steps discovering the IP before they could
clone. The address worked from Geoff's browser and from nowhere an agent runs.

So a base is CHECKED, from inside this process's own network namespace, at
startup and on demand: a base that does not answer is shouted in the log and
reported in `GET /api/gitmode` (`addresses`), and `the security smoke test`
asks the same question from the HOST shell, where agents run. Warn, not refuse
to start: the git service starts after this one (compose `depends_on`), so a
refusal would be a boot-order deadlock, not a safety property. The check
retries for a while before it decides.

Only where git-first serves somewhere (`gitmode.any_serves()`): an `off`
instance hands out no clone URL, and production's step-1 boot must stay
byte-for-byte master's.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

#: How long the startup check keeps retrying a base that does not answer yet
#: (the git container starts after this one).
SETTLE_S = float(os.environ.get("PLM_ADDRESS_CHECK_SETTLE_S", "90"))
TIMEOUT_S = 4.0

_STATE: dict = {"checked_at": None, "ok": None, "bases": [], "running": False}
_LOCK = threading.Lock()


def bases() -> list[tuple[str, str, str]]:
    """(name, base URL, what answers there) — the three a link is built from."""
    from . import config, gitlinks, gitrepo
    return [("public", config.PUBLIC_URL, "/api/stats"),
            ("web", gitlinks.WEB_BASE, "/api/stats"),
            ("clone", gitrepo.CLONE_BASE, "/healthz")]


def _resolve(host: str) -> list[str]:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
    except OSError:
        return []


def _loopback(addrs: list[str]) -> bool:
    try:
        return bool(addrs) and all(ipaddress.ip_address(a).is_loopback
                                   for a in addrs)
    except ValueError:
        return False


def probe(name: str, base: str, path: str) -> dict:
    """One base: does it answer, and is it an address other machines can use?

    The CLONE base is probed at `<base>/healthz` — UNDER the clone root, the
    git service's liveness route, which it answers at `/git/healthz` as well
    as `/healthz` — because `<base>/<NUMBER>.git` needs a number and, behind
    an edge, only the clone root is forwarded to the git service (the origin's
    `/healthz` is the web app's, a 404). The web bases are probed at
    `/api/stats`, the same route the healthcheck uses."""
    u = urlsplit(base)
    host = u.hostname or ""
    addrs = _resolve(host)
    origin = f"{u.scheme}://{u.netloc}"
    url = (base.rstrip("/") if name == "clone" else origin) + path
    out = {"name": name, "base": base, "probe": url, "host": host,
           "resolves_to": addrs, "ok": False, "why": "", "warning": ""}
    if not host:
        out["why"] = "not a URL"
        return out
    if not addrs:
        out["why"] = f"`{host}` does not resolve here"
        return out
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            out["ok"] = 200 <= r.status < 400
            out["why"] = f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        out["why"] = f"HTTP {exc.code}"
    except Exception as exc:                               # noqa: BLE001
        out["why"] = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
    if _loopback(addrs):
        literal = host in ("localhost", "127.0.0.1", "::1")
        out["warning"] = (
            f"`{host}` is a loopback address ({', '.join(addrs)}): a link built "
            "on it opens on this host only — not from the LAN, the private network or "
            "another machine"
            if literal else
            f"`{host}` resolves to {', '.join(addrs)} HERE (an /etc/hosts "
            "loopback line), so on this host the link does not reach the "
            "service even though other machines' DNS may say otherwise")
    return out


def check() -> dict:
    """Probe every base once. Never raises."""
    rows = [probe(n, b, p) for n, b, p in bases()]
    res = {"checked_at": time.time(), "ok": all(r["ok"] for r in rows),
           "bases": rows}
    with _LOCK:
        _STATE.update(res)
    return res


def state() -> dict:
    """The last answer (for `/api/gitmode`), without probing."""
    with _LOCK:
        return {k: _STATE[k] for k in ("checked_at", "ok", "bases")}


def banner(res: dict) -> str:
    bad = [r for r in res["bases"] if not r["ok"]]
    lines = ["", "⛔" * 3 + " PLM ADDRESS CHECK FAILED " + "⛔" * 3,
             "plm hands these addresses to agents and humans, and they do not "
             "answer from here:"]
    for r in bad:
        lines.append(f"  {r['name']:<7} {r['base']}  -> {r['probe']}: {r['why']}"
                     + (f" ({r['warning']})" if r["warning"] else ""))
    lines += ["Every clone_url / change URL built on them is a dead link. Set "
              "PLM_PUBLIC_URL / PLM_WEB_BASE /",
              "PLM_GIT_CLONE_BASE to an address that answers from the host, the "
              "LAN and the private network",
              "(a LAN address rather than the host's own name, which Debian's "
              "/etc/hosts maps to 127.0.1.1 on the host itself).", ""]
    return "\n".join(lines)


def start() -> None:
    """Startup: probe in the background until every base answers or SETTLE_S
    passes, then say so — loudly if not. Only where git-first serves."""
    try:
        from . import gitmode
        if not gitmode.any_serves():
            return
    except Exception:                                      # noqa: BLE001
        return
    with _LOCK:
        if _STATE["running"]:
            return
        _STATE["running"] = True

    def run() -> None:
        deadline = time.time() + SETTLE_S
        res = check()
        while not res["ok"] and time.time() < deadline:
            time.sleep(5)
            res = check()
        if res["ok"]:
            warn = [r for r in res["bases"] if r["warning"]]
            print("plm: address check OK — "
                  + ", ".join(f"{r['name']} {r['base']}" for r in res["bases"])
                  + "".join(f"\nplm: ⚠ {r['name']}: {r['warning']}"
                            for r in warn), file=sys.stderr, flush=True)
        else:
            print(banner(res), file=sys.stderr, flush=True)
        with _LOCK:
            _STATE["running"] = False

    threading.Thread(target=run, name="plm-address-check", daemon=True).start()
