"""What you can DO with a file — a registry, not a pile of buttons.

⭐ WHY THIS EXISTS. "Send this STL to the printer" arrived as a single button on
the revision header, wired to one integration, able to act on exactly one file:
whichever one `services/plm.printable_file` picked. That is fine until a revision
carries two printable meshes — a jig and its test coupon — at which point the
button can only refuse, because nothing in the model lets you say *which*.

The fix is not a second button. It is to notice that "send to the build plate" is
one member of a family — send to a printer, hand to a CAM post, publish a
drawing — and that they all have the same shape:

    given a FILE, if it carries the right kind of content,
    offer a named action that hands THAT FILE to a registered integration.

So actions are declared here as data and the UI renders whatever matches. Adding
"send this DXF to the laser" is a dict entry, not a feature.

⭐⭐ STAGE 5 GENERALISED THAT TO EVERY VERB, not just the integrations. Opening a
file, downloading it, copying its link, diffing it, declaring what it is — each
of those was re-decided at five different call sites in `web/app.js`, each with
its own idea of what a file was. They are entries here now, so ONE menu renders
them all and there is one answer to "what can I do with this?" whether the file
came out of the database, out of a git tree, or out of the object store.

⛔ AN ACTION NEVER DECIDES WHICH FILE. That was the original sin: the integration
guessed, and a guess about which of two meshes you meant to print is a guess with
a spool of filament riding on it. Here the file is the SUBJECT of the action —
you clicked the row — so there is nothing to infer.

⚠️ `applies` matches on the file's CLASSIFICATION, never on its filename: its
media KIND (`plm.roles.MEDIA_KINDS`, measured from the bytes), its declared
`role`, and the kind of content that role `carries`. A `name` glob may only
NARROW a match that the classification already made. Matching on names was how
everything ended up in the `artifact` junk drawer in the first place.
"""
from __future__ import annotations

import fnmatch
import time

from . import roles as rolemod
from .roles import ROLES

_INTEG_TTL = 15.0
_integ_cache: tuple[float, frozenset] = (0.0, frozenset())


def enabled_integrations() -> frozenset:
    """Which integrations are registered AND enabled, cached for a few seconds.

    ⭐ An action whose downstream does not exist is not an action, and it must
    say so rather than fail at the click. Asked here, once, so a database row, a
    git tree row and a compare row cannot give three different answers about
    whether anything can receive a file. (Lazy import: `plm.db` imports this
    module.)
    """
    global _integ_cache
    at, names = _integ_cache
    now = time.monotonic()
    if now - at < _INTEG_TTL:
        return names
    try:
        from . import db
        with db.connect() as conn:
            names = frozenset(
                r["name"] for r in conn.execute(
                    "SELECT name FROM integrations WHERE enabled=1"))
    except Exception:                       # a read that fails is "none known"
        names = frozenset()
    _integ_cache = (now, names)
    return names

#: Menu order, and where the separators fall. A group is a sentence about the
#: verb: look at it · take it away · see what changed · send it somewhere ·
#: change the record.
GROUPS: tuple[str, ...] = ("open", "get", "compare", "send", "edit")

#: ⭐ CONTEXT PREDICATES. An action can apply to a file and still not be
#: available HERE — "compare with…" means nothing for a row that has no ref
#: behind it. Each predicate is evaluated server-side from a small context dict
#: (or from the file itself) and carries the sentence it is refused with, so an
#: unmet one renders as a DISABLED item with the reason, never as a hidden one.
PREDICATES: dict[str, str] = {
    "previewable": "plm cannot show this format in the browser — download it",
    "bytes": "this row is a submodule — a pointer at a child repo, pinned at a "
             "commit. There are no bytes of its own to fetch; open the child.",
    "ref": "this row is a database file, not a file at a git ref — browse the "
           "repo at a release or a draft to compare it with another",
    "tracked": "git has no history for this row — it is not a committed path "
               "at a ref (either a database file row, or an external file the "
               "manifest declares but the tree does not hold)",
    "dbfile": "this row came out of the repo, not the registry — what a file "
              "in a git tree is (and whether it is there at all) changes by "
              "pushing a commit, which is the whole point of git-first",
    "draft": "only a DRAFT revision can be edited in place — a released one is "
             "immutable, and revising it is how it changes",
    "editor": "you do not have write access to this item",
    "integration": "no enabled integration named {integration} is registered "
                   "on this instance, so there is nowhere to send it",
}


def _facts(f: dict, ctx: dict) -> dict:
    """The predicate answers, FILE first and caller second.

    ⭐ Most of them are properties of the row itself — a row with a database id
    is a database row, a row with a ref came out of a repo — so a caller that
    passes no context still gets the right answer instead of a degraded one.
    That is what keeps `for_file(f)` (what `plm/db._file_out` has always called)
    correct with no edit at the call site.
    """
    # ⭐ A DATABASE ROW CAN ALSO BE A FILE IN A REPO. Stage 3b made every write a
    # commit: a git-origin revision's files have `backend='git'` and a key of
    # `<NUMBER>@<commit>:<path>`, so they ARE tracked paths and comparing or
    # historying them is meaningful — the row just reached us through the
    # registry rather than through the tree API.
    in_git = str(f.get("backend") or "") == "git"
    return {
        "dbfile": bool(f.get("id")),
        "ref": bool(f.get("ref")) or in_git,
        # An `[[files.external]]` row is declared by the manifest
        # at a ref but is NOT in the tree, so `git log -- <path>` is empty for
        # it. That is a real difference and the menu says which it is.
        "tracked": (bool(f.get("ref")) and f.get("source") != "external") or in_git,
        "draft": bool(f.get("draft")) or f.get("revision_state") == "DRAFT",
        # No sign-in on this instance (pre-OIDC trusted private network, plm/config.py),
        # so everybody who can reach it may write. When OIDC lands, the caller
        # passes the real answer and this default stops applying.
        "editor": True,
        **{k: v for k, v in (ctx or {}).items() if v is not None},
    }

#: The kinds of content each action is willing to act on. An action lists media
#: `kinds`, the `carries` values it accepts, and/or the exact roles — the first
#: two are the broad gate, the third is for narrowing when a kind is too coarse.
ACTIONS: dict[str, dict] = {
    # ⭐ `primary` is what clicking the file's NAME does. Exactly one action may
    # claim it; everything else is in the ⋯ menu.
    "open": {
        "label": {"3d": "View in 3D", "image": "Preview image",
                  "md": "Preview", "pdf": "Open PDF", "*": "Preview"},
        "help": "Opens the file in place — geometry in the 3-D room, "
                "everything else in a preview over the page. Nothing is "
                "downloaded and nothing leaves the page.",
        "type": "client", "group": "open", "primary": True,
        "kinds": "*", "needs": ("previewable",),
    },
    "download": {
        "label": "Download",
        "help": "The bytes, exactly as plm holds them.",
        "type": "client", "group": "get", "kinds": "*", "needs": ("bytes",),
    },
    "copy_link": {
        "label": "Copy link",
        "help": "A URL to these bytes, for a message or a terminal.",
        "type": "client", "group": "get", "kinds": "*", "needs": ("bytes",),
    },
    "compare": {
        "label": "Compare with…",
        "help": "Diff this file against another release or draft — the right "
                "widget for its kind: a redline, a 3-D overlay, an image swipe.",
        "type": "client", "group": "compare", "kinds": "*", "needs": ("ref",),
    },
    "history": {
        "label": "File history",
        "help": "Every commit that touched this path, newest first.",
        "type": "client", "group": "compare", "kinds": "*",
        "needs": ("tracked",),
    },
    "print": {
        "label": "Send to build plate",
        "help": ("Queues this exact file for printing. Nothing is sliced and "
                 "nothing starts; it joins the print queue."),
        # ⭐ Every mesh, including a `viz` one. The integration still has its own
        # opinion about viz geometry and will say so — but that refusal belongs
        # downstream, where the reason lives. Hiding the action here would leave
        # somebody who genuinely wants to print a viz mesh with no route at all
        # and no explanation.
        "carries": ("geometry",),
        "integration": "print",
        "verb": "queued",
        "type": "integration", "group": "send",
        "needs": ("integration",),
        #: ⚠️ Shown but disabled when the media is not something the printer eats.
        #: A 3MF is geometry and is not (yet) sliceable through this path, and an
        #: action that silently vanishes teaches nobody anything.
        "media": ("model/stl",),
    },
    "set_role": {
        "label": "Say what this is…",
        "help": "Declare the file's purpose — mesh, drawing, render, photo. A "
                "guess plm made from the filename is not a declaration, and "
                "several actions only become available once somebody says.",
        "type": "op", "group": "edit", "kinds": "*",
        "needs": ("dbfile", "editor"),
    },
    "remove": {
        "label": "Remove from this revision",
        "help": "Takes the file off this revision's manifest. On a git-origin "
                "revision that is a real commit; the bytes are not destroyed, "
                "the record stops claiming them.",
        "type": "op", "group": "edit", "kinds": "*", "danger": True,
        "needs": ("dbfile", "draft", "editor"),
    },
}


def _role_info(role: str) -> dict:
    return ROLES.get(role) or {}


def _label(spec: dict, block: dict) -> str:
    """A label may vary with the preview room — "View in 3D" for a mesh reads
    better than "Preview", and it is the same action."""
    lab = spec["label"]
    if isinstance(lab, str):
        return lab
    return lab.get(block.get("preview") or "") or lab.get("*") or ""


def _predicate(name: str, facts: dict, block: dict, spec: dict) -> tuple[bool, str]:
    """(met, reason). `previewable` is answered by the file's KIND — does it
    have a room to open in — and everything else by `_facts`."""
    if name == "previewable":
        return bool(block.get("preview")), (block.get("why")
                                            or PREDICATES["previewable"])
    why = PREDICATES.get(name, f"{name} is required")
    if name == "integration" and facts.get("integration") is None:
        return (spec.get("integration") in enabled_integrations(),
                why.format(integration=spec.get("integration", "")))
    return bool(facts.get(name)), why.format(
        integration=spec.get("integration", ""))


#: The fields of an `actions` entry as `master` defined them (2026-09-17). The
#: wire contract agents already read; `for_file` keeps it exactly.
CONTRACT_FIELDS = ("name", "label", "help", "enabled", "reason", "integration")


def for_file(f: dict, ctx: dict | None = None) -> list[dict]:
    """`files[].actions` — what this file can be SENT to. The master contract.

    ⭐ STAGE 9: TWO KEYS, ONE REGISTRY. On `master`, `actions` was the list of
    INTEGRATION actions a file qualifies for (`print`, today), each
    `{name, label, help, enabled, reason, integration}` — and agents read it
    that way through the API and the MCP. Stage 5 grew the same registry into
    the UI's whole ⋯ menu (open, download, compare, history, set_role, remove)
    and served it under the same key, which changed the meaning of a key
    production clients already read. The compatibility harness (the compatibility harness)
    found it on every file row.

    So `actions` keeps master's meaning, projected out of `menu_for()` — the
    same resolver, the same predicates, no second list — and the full menu is
    served beside it as `menu` (additive). The UI reads `menu`.
    """
    out = []
    for a in menu_for(f, ctx):
        if a.get("type") != "integration":
            continue
        c = {k: a.get(k) for k in CONTRACT_FIELDS}
        if c["reason"] == LEGACY_REASON.format(remedy=REMEDY_MENU):
            c["reason"] = LEGACY_REASON.format(remedy=REMEDY_CHIP)
        out.append(c)
    return out


#: The undeclared-file sentence, with its remedy named for where it is read: the
#: menu has a "Say what this is…" item; the API's consumers (master's UI, the
#: agents) were told to click the role chip.
LEGACY_REASON = ("this file has never been classified — say what it is "
                 "({remedy}) and this becomes available")
REMEDY_MENU = "Say what this is…"
REMEDY_CHIP = "click the role chip"


def menu_for(f: dict, ctx: dict | None = None) -> list[dict]:
    """The actions offered for one file record, in menu order — `files[].menu`.

    ⭐ THE ONLY RESOLVER. Database rows (`plm/db._file_out`), git tree entries,
    `[[files.external]]` rows and compare rows all come through here, so the
    answer cannot differ by surface.

    `ctx` is optional and every predicate defaults to false, which keeps the
    signature backward-compatible: a caller that knows nothing about refs and
    drafts gets the file-only actions enabled and the rest shown disabled with
    the reason — which is the correct answer, not a degraded one.

    Returns a list of `{name, label, help, enabled, reason, type, group,
    primary, danger}` — including the ones that do NOT apply to this file,
    marked `enabled: False` with a reason. An action that disappears looks like
    a missing feature; one that is visibly unavailable, with a sentence, is an
    answer.
    """
    facts = _facts(f, dict(ctx or {}))
    name_of = str(f.get("name") or f.get("path") or "")
    block = rolemod.media_block(name_of, str(f.get("media") or ""),
                                kind=str(f.get("kind") or ""),
                                type_=str(f.get("type") or ""))
    # The measured format when the row carries one, the extension's guess when
    # it does not — `media_block` resolved it, so the two cannot disagree. A git
    # tree row has no `media` column; the action ENDPOINT re-sniffs the real
    # bytes before anything is sent anywhere.
    media = block["media"]
    facts["bytes"] = block["kind"] != "submodule"
    role = str(f.get("role") or "")
    info = _role_info(role)
    carries = info.get("carries") or ""
    legacy = bool(info.get("legacy"))
    out = []
    for name, spec in ACTIONS.items():
        wanted = spec.get("media") or ()
        kinds = spec.get("kinds", ())
        wants_carries = spec.get("carries")
        # The broad gate: media KIND and/or the content class the declared role
        # carries. An action that names neither is offered on everything.
        matches_kind = True
        if kinds and kinds != "*":
            matches_kind = block["kind"] in kinds
        if wants_carries is not None:
            matches_kind = matches_kind and carries in wants_carries
        # The NARROWING escape hatch, never the only test: a name glob can only
        # shrink a match the classification already made.
        glob = spec.get("name")
        if glob and not fnmatch.fnmatch(name_of.rsplit("/", 1)[-1].lower(),
                                        glob.lower()):
            continue

        # ⭐⭐ THE LEGACY CASE, AND WHY IT IS NOT SILENCE.
        #
        # `artifact` is the old default and carries NOTHING — it means "nobody
        # said". 228 rows are in it, including every STL that predates the role
        # registry. Matching on `carries` alone therefore offers no action at
        # all on exactly the files somebody is most likely to want to print, and
        # a button that is simply absent reads as "this feature is broken",
        # which is the same failure the whole registry exists to avoid.
        #
        # So an undeclared file whose BYTES are right for the action is shown the
        # action, disabled, with the actual remedy: say what it is. That turns a
        # dead end into the one-click fix (the role chip beside it), and it is
        # the bargain — declare the thing, and the system can act on it.
        undeclared = legacy and bool(wanted) and media in wanted
        if not matches_kind and not undeclared:
            continue

        if undeclared:
            reason = LEGACY_REASON.format(remedy=REMEDY_MENU)
            enabled = False
        elif wanted and media not in wanted:
            reason = (f"{media or 'this file type'} cannot be sent this way; "
                      f"expected {', '.join(wanted)}")
            enabled = False
        else:
            reason, enabled = "", True
            for need in spec.get("needs") or ():
                met, why = _predicate(need, facts, block, spec)
                if not met:
                    enabled = False
                    reason = why
                    break

        out.append({
            "name": name,
            "label": _label(spec, block),
            "help": spec["help"],
            "enabled": enabled,
            "reason": reason,
            "type": spec.get("type", "client"),
            "group": spec.get("group", "get"),
            "primary": bool(spec.get("primary")),
            "danger": bool(spec.get("danger")),
            "integration": spec.get("integration", ""),
        })
    out.sort(key=lambda a: GROUPS.index(a["group"]) if a["group"] in GROUPS
             else len(GROUPS))
    return out


def known(name: str) -> dict:
    """The action's spec, or `{}`. Callers branch on emptiness, not exceptions —
    an unknown action name is a client sending something stale, not a fault."""
    return ACTIONS.get(name) or {}


def describe() -> dict:
    """The whole vocabulary as data — `GET /api/actions`, and the MCP.

    An agent reading this learns what plm can do with a file without guessing
    at verbs, and the docs stop being a second copy of the table.
    """
    return {
        "groups": list(GROUPS),
        "predicates": [{"name": k, "refused_with": v}
                       for k, v in PREDICATES.items()],
        "actions": [{
            "name": k,
            "label": v["label"] if isinstance(v["label"], str)
                     else v["label"].get("*", k),
            "labels": v["label"] if isinstance(v["label"], dict) else {},
            "help": v["help"],
            "type": v.get("type", "client"),
            "group": v.get("group", "get"),
            "primary": bool(v.get("primary")),
            "danger": bool(v.get("danger")),
            "kinds": ("*" if v.get("kinds") == "*"
                      else list(v.get("kinds") or ())),
            "carries": list(v.get("carries") or ()),
            "media": list(v.get("media") or ()),
            "name_glob": v.get("name", ""),
            "needs": list(v.get("needs") or ()),
            "integration": v.get("integration", ""),
        } for k, v in ACTIONS.items()],
        "kinds": rolemod.describe_kinds(),
    }


# =============================================================================
# THE WIRE — C1 / C3 / M4. Every outbound HTTP request plm makes on somebody's
# behalf leaves through `send()`, and through nothing else.
# =============================================================================
#
# ⛔ WHAT WENT WRONG, AND WHY THIS IS ONE FUNCTION NOW.
#
# `PLM_ACTIONS_DRY_RUN` was checked in TWO places — copy-pasted into the two
# user-triggered action handlers (`plm/gitapi.py`, `plm/api.py`) — and NOT in
# `api.dispatch_event`, the automatic webhook fan-out called from eleven sites
# (every check-in, state change, revise and file change). So the POC's central
# safety control did not cover the third outbound path, and one unauthenticated
# `POST /api/integrations` was enough to make every subsequent write POST
# revision metadata to an arbitrary URL (C3).
#
# The same handler then wrote the FULL integration URL — api_key and all — into
# the `events` table (M4), and `GET /api/integrations` served the raw rows to
# anybody on the LAN (C1).
#
# So: one function. It holds the dry-run guard, the host allowlist, the timeout,
# and the logging rule — **log the host, never the URL** — and the three callers
# hold none of them. A fourth caller gets all four for free, which is the only
# way a rule like this survives the next feature.

import json as _json                                        # noqa: E402
import os                                                   # noqa: E402
import re                                                   # noqa: E402
from urllib.parse import urlsplit                           # noqa: E402

#: Hosts an integration may point at. Empty (the default) = any host, which is
#: what a single-operator plm has always been; set `PLM_ACTIONS_ALLOW_HOSTS` to
#: a comma-separated list to make `POST /api/integrations` refuse anything else.
#: The check is on the HOST, not the URL, because that is the part an operator
#: can reason about.
ALLOW_HOSTS = tuple(h.strip().lower() for h in
                    os.environ.get("PLM_ACTIONS_ALLOW_HOSTS", "").split(",")
                    if h.strip())

#: Schemes an integration may use. `file:`, `ftp:` and friends are not things
#: plm posts JSON to, and a scheme allowlist is cheaper than discovering that.
ALLOW_SCHEMES = ("http", "https")

OUTBOUND_TIMEOUT = float(os.environ.get("PLM_ACTIONS_TIMEOUT", "10"))


#: Query-string parameters whose VALUE is a credential wherever it appears —
#: including inside somebody else's exception text.
_SECRET_QS = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|key|secret|password|sig|"
    r"signature)=([^\s&\"'#]+)")
#: `scheme://user:pass@host` — the other place a credential hides in a URL.
_SECRET_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def scrub(text: str) -> str:
    """Take every credential out of a string before it is stored or shown.

    ⛔ THE THIRD COPY THE REVIEW DID NOT FIND. M4 named `dispatch_event`'s
    `data={"url": s["url"]}`, and that was real — but `httpx`'s own
    `raise_for_status()` message embeds the request URL, and `api.py` stored
    `str(exc)` in `events.data["error"]`. 17 rows in this POC's copy of the
    registry carried the the shop-floor service key that way, from a 409 nobody thought
    about. Any text that came from an exception, a downstream body or a log line
    goes through here.
    """
    if not text:
        return text
    out = _SECRET_QS.sub(lambda m: f"{m.group(1)}=<redacted>", str(text))
    return _SECRET_USERINFO.sub(lambda m: f"{m.group(1)}<redacted>@", out)


class SendError(RuntimeError):
    def __init__(self, msg: str, code: int = 502, body=None, status: int = 0):
        super().__init__(scrub(msg))
        self.code, self.status = code, status
        self.body = body


def dry_run() -> bool:
    """⛔ THE ONE READING OF `PLM_ACTIONS_DRY_RUN`. It was read at two call
    sites and missed at a third; there is one now, and `send()` is the only
    caller."""
    return os.environ.get("PLM_ACTIONS_DRY_RUN", "") not in ("", "0", "false",
                                                             "no")


def host_of(url: str) -> str:
    """The host an integration URL points at — what gets LOGGED (M4).

    `.split("?")[0]` is what the two guarded paths did, and it is not enough:
    a credential can be in userinfo (`https://user:pass@host/`) or in the path.
    The host is the useful half of "where did this go" and carries nothing.
    """
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def check_target(url: str) -> str:
    """Validate an integration URL and return its host. Raises `SendError(400)`.

    Called by `send()` AND by `POST /api/integrations` at registration time, so
    a URL plm would refuse to call is a URL plm refuses to store.
    """
    parts = urlsplit(url or "")
    if parts.scheme.lower() not in ALLOW_SCHEMES:
        raise SendError(
            f"an integration URL must be http: or https: (got "
            f"{parts.scheme or 'no scheme'!r}).", 400)
    host = (parts.hostname or "").lower()
    if not host:
        raise SendError("an integration URL must name a host.", 400)
    if parts.username or parts.password:
        raise SendError(
            "an integration URL must not carry credentials in its userinfo "
            "(`https://user:pass@host/`).\n"
            "  Put the secret in a file and name it in `config.api_key_file`; "
            "plm sends it\n  as a header and never stores, logs or serves it.",
            400)
    if ALLOW_HOSTS and host not in ALLOW_HOSTS:
        raise SendError(
            f"{host} is not in this instance's integration host allowlist "
            f"({', '.join(ALLOW_HOSTS)}).\n"
            "  PLM_ACTIONS_ALLOW_HOSTS is the setting; widening it is an "
            "operator's decision,\n  not a caller's.", 400)
    return host


def send(row, payload: dict, *, what: str = "", actor: str = "",
         force_dry_run: bool = False) -> dict:
    """POST `payload` to an integration. **The only outbound call in plm.**

    `row` is an `integrations` row (a mapping). Returns
    `{ok, dry_run, integration, to, result, status}` — `to` is the HOST, never
    the URL, so a caller cannot leak the credential by echoing the response.

    Raises `SendError` with the downstream's status when it answers 4xx, because
    a REFUSAL is an answer and not a transport failure: the shop-floor service says "this
    revision's only STL is a `viz` composite, not a printable artifact", and
    collapsing that to "unreachable: 409 Client Error" threw away the only
    useful part of it. 5xx and transport errors are ours to report as 502.
    """
    import httpx
    name = str(row["name"] if "name" in row.keys() else row.get("name", ""))
    url = str(row["url"] if "url" in row.keys() else row.get("url", ""))
    host = check_target(url)
    headers = auth_headers(row)

    if force_dry_run or dry_run():
        # ⛔ STOP AT THE WIRE. Return exactly what WOULD have been sent, minus
        # the credential — which is what the whole path needs proving anyway.
        return {"ok": True, "dry_run": True, "integration": name, "to": host,
                "status": 0, "payload": payload,
                "sent_headers": sorted(headers),
                "result": {"message":
                           f"dry run — {what or name} was prepared for {name} "
                           f"at {host} and NOT sent (PLM_ACTIONS_DRY_RUN)"}}
    try:
        r = httpx.post(url, json=payload, headers=headers,
                       timeout=OUTBOUND_TIMEOUT)
    except Exception as exc:                                # noqa: BLE001
        raise SendError(f"integration {name} ({host}) unreachable: "
                        f"{str(exc)[:200]}") from None
    body = (r.json() if "json" in r.headers.get("content-type", "")
            else {"error": r.text[:400] or f"HTTP {r.status_code}"})
    if 400 <= r.status_code < 500:
        raise SendError(
            str(body.get("error") or body.get("detail")
                or f"{name} refused this ({r.status_code})"),
            r.status_code, body=body, status=r.status_code)
    if r.status_code >= 500:
        raise SendError(f"integration {name} ({host}) answered "
                        f"{r.status_code}", 502, body=body,
                        status=r.status_code)
    return {"ok": True, "dry_run": False, "integration": name, "to": host,
            "status": r.status_code, "result": body}


def auth_headers(row) -> dict:
    """The credential an integration needs, read from a FILE at call time.

    ⛔ C1 STRUCTURALLY. The the shop-floor service API key lived in `integrations.url` as a
    query parameter — in the database, in `GET /api/integrations`, in the MCP's
    `list_integrations`, and (M4) a second time in the `events` table. A secret
    in a URL is a secret in every log, every referrer and every response that
    echoes the row.

    So a secret is never a column. `config` may carry:

        {"api_key_file": "/var/plm/secrets/print-api-key"}   # → X-API-Key
        {"api_key_file": "…", "api_key_header": "Authorization"}  # → Bearer

    and the bytes are read HERE, at the moment of the call, and go into a header.
    Nothing that serves a row has them, because the row does not have them.

    ⛔ THE DEFAULT HEADER IS `X-API-Key`, because that is PRODUCTION'S ROW. The
    C1 hotfix on `master` (2026-09-18, `abf0384`) put the the shop-floor service key in a
    file named by `config.api_key_file` with no header key, and its own
    `plm/integrations.auth_headers` sent it as `X-API-Key`. This module and that
    one were two implementations of one idea; stage 9 kept this one (it also
    owns the dry-run guard, the host allowlist and `dispatch_event`) and
    deleted that one — so the row prod already has must keep meaning exactly
    what it meant. `api_key_header: "Authorization"` opts into
    `Bearer <key>`; `api_key_prefix` overrides the prefix for either.
    """
    try:
        cfg = _json.loads((row["config"] if "config" in row.keys()
                           else row.get("config")) or "{}")
    except (ValueError, TypeError):
        cfg = {}
    if not isinstance(cfg, dict):
        return {}
    path = str(cfg.get("api_key_file") or "").strip()
    if not path:
        return {}
    try:
        key = open(path).read().strip()
    except OSError as exc:
        raise SendError(
            f"this integration's credential file ({path}) is not readable: "
            f"{exc.strerror}.\n"
            "  A secret belongs in a file mounted into the container, never in "
            "the `url` column\n  (the security review C1).",
            500) from None
    header = str(cfg.get("api_key_header") or "X-API-Key")
    prefix = str(cfg.get("api_key_prefix",
                         "Bearer " if header.lower() == "authorization" else ""))
    return {header: f"{prefix}{key}"}


#: Columns of an `integrations` row that may carry a secret and are therefore
#: never served whole. `url` keeps its scheme+host+path so a human can still see
#: WHERE this plm talks to; the query and any userinfo go.
REDACT_NOTE = "redacted — plm does not serve integration credentials (C1)"


def public_row(row) -> dict:
    """One `integrations` row, safe to serve. ⛔ EVERY read surface uses this.

    `GET /api/integrations`, the MCP's `list_integrations` and `dispatch_event`'s
    event payload all go through here, so there is one answer to "what does an
    integration look like from outside" and it cannot drift into three.
    """
    d = dict(row)
    raw = str(d.get("url") or "")
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        d["url"] = f"{parts.scheme}://{(parts.hostname or '')}" \
                   + (f":{parts.port}" if parts.port else "") + (parts.path or "")
        d["host"] = (parts.hostname or "").lower()
        d["url_redacted"] = bool(parts.query or parts.username or parts.password)
    else:
        d["url"], d["host"], d["url_redacted"] = "", "", bool(raw)
    try:
        cfg = _json.loads(d.get("config") or "{}")
    except (ValueError, TypeError):
        cfg = {}
    if isinstance(cfg, dict):
        safe = {}
        has_cred = False
        for k, v in cfg.items():
            low = str(k).lower()
            if low.endswith("_file"):
                # ⚠️ STAGE 9: DROPPED, as master's C1 hotfix drops it. A path is
                # not itself a secret, and this module used to serve it ("it is
                # the fix") — but it names where the secret lives, prod's own
                # hotfix chose not to, and the compatibility harness found the
                # two answers disagreeing on `GET /api/integrations`. The
                # stricter one wins; `has_credential` says one is configured.
                has_cred = True
                continue
            elif any(w in low for w in ("key", "token", "secret", "password",
                                        "auth", "credential")):
                safe[k] = REDACT_NOTE
                has_cred = True
            else:
                safe[k] = v
        d["config"] = _json.dumps(safe)
        d["has_credential"] = has_cred
    else:
        d["config"] = "{}"
    if d.get("url_redacted"):
        d["note"] = REDACT_NOTE
    return d


# -- event delivery: the integrations subscribed to an event type ---------------

def dispatch(type_: str, payload: dict) -> None:
    """Deliver an event to every enabled integration subscribed to it.
    Fire-and-forget on a thread: a dead endpoint must never slow a check-in.
    Delivery results land in the events table for the dashboard.

    ⛔ C3 / M4. Goes through `send()` — the guard, the scheme/host allowlist,
    the timeout and the credential-from-a-file all apply — and the event row
    records the HOST, never the URL. (Moved here from `plm/api.py`'s
    `dispatch_event` in stage 13, unchanged, so the MCP process can deliver the
    review model's "needs your attention" events through the same wire.)
    """
    import threading
    from . import db
    # `actionmod` is this module, by the name plm/api.py always used for it:
    # `api.dispatch_event` IS this function now, and the security smoke test's
    # C3/M4 assertion reads its source for `actionmod.send(` + a host-only
    # event row. Same wire either way.
    from . import actions as actionmod
    with db.connect() as conn:
        subs = [dict(r) for r in conn.execute(
            "SELECT * FROM integrations WHERE enabled=1 AND on_events!=''")]
    subs = [s for s in subs if type_ in [e.strip() for e in s["on_events"].split(",")]]
    if not subs:
        return

    def deliver():
        body = {"type": type_, "ts": time.time(), **payload}
        for s in subs:
            ok, err, host = True, "", actionmod.host_of(s["url"])
            dry = False
            try:
                res = actionmod.send(s, body, what=f"event {type_}")
                dry = bool(res.get("dry_run"))
            except actionmod.SendError as exc:
                ok, err = False, str(exc)[:300]
            except Exception as exc:                        # noqa: BLE001
                ok, err = False, str(exc)[:300]
            with db.connect() as conn:
                db.add_event(
                    conn,
                    "integration.dry-run" if dry else
                    "integration.delivered" if ok else "integration.failed",
                    actor=s["name"],
                    data={"event": type_, "host": host, "error": err})

    threading.Thread(target=deliver, daemon=True).start()
