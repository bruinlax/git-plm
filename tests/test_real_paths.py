"""Stage 22 — real paths (`/CID-ASM-0001`, not `/#/item/CID-ASM-0001`).

Pins the server's half: which GETs are the app (the shell), which are a small
HTML 404, that static files stay files, that the sign-in round trip carries a
real path, the ref longest-match rule, the `/<N>/pull/<n>` resolver, and every
link plm hands out (one assertion per emitter: the exact string it writes).
"""
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from plm import api, db, identity, webroutes

HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture(scope="module")
def client():
    db.init()
    identity._DISCOVERY = {
        "issuer": "http://idp.invalid",
        "authorization_endpoint": "http://idp.invalid/authorize",
        "token_endpoint": "http://idp.invalid/token",
        "userinfo_endpoint": "http://idp.invalid/userinfo",
    }
    identity._DISCOVERY_AT = 1e18
    return TestClient(api.app, follow_redirects=False)


@pytest.fixture
def signed_in(client, monkeypatch):
    """A client holding a real session cookie (the IdP faked locally)."""
    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "fake-access-token"}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(identity.httpx, "Client", _C)
    monkeypatch.setattr(identity, "userinfo", lambda tok: {
        "sub": "sub-geoff", "email": "geoff@example.invalid",
        "name": "Geoff", "groups": ["plm-users"]})
    return monkeypatch


def _session(client) -> TestClient:
    from urllib.parse import parse_qs, urlsplit
    r = client.get("/auth/login?next=%2F", headers=HTML)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = client.get(f"/auth/callback?code=c&state={state}")
    cookie = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    c2 = TestClient(api.app, follow_redirects=False)
    c2.cookies.set(identity.SESSION_COOKIE, cookie)
    return c2


# -- the first-segment rule ----------------------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/CID-ASM-0001", "/CID-ASM-0001/bom", "/CID-ASM-0001/tree/draft/v0.4.1/cad",
    "/CID-ASM-0001/blob/v0.1.0/cad/x.scad", "/CID-ASM-0001/compare/v0.1.0...v0.2.0",
    "/CID-ASM-0001/pull/380", "/CID-ASM-0001/releases/tag/v0.2.0",
    "/LFD-PRT-PRT-0001", "/KML-PRT-1012/history",
    "/pulls/380", "/pulls/380/files", "/pulls/380/checks",
    "/pulls/380/commits", "/CID-ASM-0001/pull/380/files", "/pulls",
    "/CID-ASM-0001/pulls",
    "/release-sets/12", "/registry", "/approvals",
    "/search", "/projects/inky-frame", "/snip/abc", "/snip/abc/info",
])
def test_ui_paths(path):
    assert webroutes.is_ui_path(path)


@pytest.mark.parametrize("path", [
    "/registr", "/cid-asm-0001", "/CID-ASM", "/api", "/mcp", "/auth/x",
    "/git/CID-ASM-0001.git", "/healthz", "/metrics", "/favicon.ico", "/x",
    "/CID-ASM-0001.git",
    # old page paths are the server's 301 (webroutes._LEGACY), not pages
    "/item/CID-ASM-0001", "/changes/CHG-380", "/change-requests/CHG-1",
])
def test_not_ui_paths(path):
    assert not webroutes.is_ui_path(path)


# -- the shell, the 404, the files (signed in) -----------------------------------

@pytest.mark.parametrize("path", [
    "/", "/CID-ASM-0001", "/CID-ASM-0001/compare/a...b",
    "/CID-ASM-0001/tree/draft/v0.4.1/cad", "/pulls/380", "/pulls/380/files", "/pulls",
    "/release-sets/3", "/registry", "/search?q=bracket", "/snip/tok123",
])
def test_ui_path_gets_the_shell(client, signed_in, path):
    c = _session(client)
    r = c.get(path, headers=HTML)
    assert r.status_code == 200, (path, r.status_code)
    assert "app.js" in r.text and "/routes.js" in r.text
    assert "no-cache" in r.headers.get("cache-control", "")
    assert "x-plm-signin" not in r.headers


def test_shell_assets_are_absolute(client, signed_in):
    """At /N/compare/a...b a relative `app.js` would resolve under /N/compare/."""
    body = _session(client).get("/CID-ASM-0001/compare/a...b", headers=HTML).text
    for rel in ("'app.js", "'changes.js", "'review.js", "'checks.js", "'flat.js", "'diffwidgets.js",
                '"vendor/', "'routes.js"):
        assert rel not in body, rel


def test_typo_is_a_small_404_not_the_shell(client, signed_in):
    c = _session(client)
    r = c.get("/registr", headers=HTML)
    assert r.status_code == 404
    assert "app.js" not in r.text and "Not found" in r.text
    r = c.get("/nope/x", headers={"Accept": "application/json"})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_typo_404_escapes_the_path(client, signed_in):
    r = _session(client).get('/x"><script>alert(1)</script>', headers=HTML)
    assert r.status_code == 404 and "<script>alert(1)" not in r.text


@pytest.mark.parametrize("path,ctype", [
    ("/app.js", "javascript"), ("/routes.js", "javascript"),
    ("/favicon.svg", "svg"), ("/vendor/three.min.js", "javascript"),
])
def test_static_files_stay_files(client, signed_in, path, ctype):
    r = _session(client).get(path)
    assert r.status_code == 200 and ctype in r.headers["content-type"]
    if path.endswith(".js"):
        assert "no-cache" in r.headers.get("cache-control", "")


def test_api_404_is_still_json(client, signed_in):
    r = _session(client).get("/api/no-such-thing", headers=HTML)
    assert r.status_code == 404 and "application/json" in r.headers["content-type"]


def test_snip_link_is_the_shell_not_a_hash_redirect(client, signed_in):
    r = _session(client).get("/snip/tok123", headers=HTML)
    assert r.status_code == 200 and "location" not in r.headers


# -- sign-in carries the real path -------------------------------------------------

@pytest.mark.parametrize("target", [
    "/CID-ASM-0001/compare/a...b",
    "/CID-ASM-0001/compare/v0.1.0...draft/v0.2.0?file=cad%2Fx.scad",
    "/pulls/380",
])
def test_real_path_round_trips_through_sign_in(client, signed_in, target):
    from urllib.parse import parse_qs, urlsplit
    # 1. anonymous: the sign-in shell, whose no-JS fallback already names it
    r = client.get(target, headers=HTML)
    assert r.status_code == 200 and r.headers.get("x-plm-signin") == "required"
    assert f'next={quote(target, safe="")}' in r.text
    # 2. what its script sends
    r = client.get("/auth/login?next=" + quote(target, safe=""), headers=HTML)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    # 3. back from the IdP: exactly there
    r = client.get(f"/auth/callback?code=c&state={state}")
    assert r.headers["location"] == target
    # 4. and there is the app
    cookie = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    c2 = TestClient(api.app, follow_redirects=False)
    c2.cookies.set(identity.SESSION_COOKIE, cookie)
    r = c2.get(target, headers=HTML)
    assert r.status_code == 200 and "app.js" in r.text


# -- refs: longest match ------------------------------------------------------------

REFS = ["main", "v0.1.0", "v0.4.1", "draft/v0.4.1", "draft/v0.4.1-b", "feature/x/y"]


@pytest.mark.parametrize("rest,want", [
    ("v0.1.0", ("v0.1.0", "")),
    ("v0.1.0/cad/x.scad", ("v0.1.0", "cad/x.scad")),
    ("draft/v0.4.1", ("draft/v0.4.1", "")),
    ("draft/v0.4.1/cad", ("draft/v0.4.1", "cad")),
    ("draft/v0.4.1-b/README.md", ("draft/v0.4.1-b", "README.md")),
    ("feature/x/y/z", ("feature/x/y", "z")),
    ("draft%2Fv0.4.1/cad/a%20b.scad", ("draft/v0.4.1", "cad/a b.scad")),
    ("0123abcd/cad", ("0123abcd", "cad")),              # a sha: first segment
    ("", ("", "")),
])
def test_split_ref_longest_first(rest, want):
    assert webroutes.split_ref(rest, REFS) == want


def test_split_ref_without_a_ref_list():
    assert webroutes.split_ref("draft/v0.4.1/cad", []) == ("draft/v0.4.1", "cad")
    assert webroutes.split_ref("v0.1.0/cad", []) == ("v0.1.0", "cad")


# -- every link plm hands out -------------------------------------------------------

def test_emitter_gitlinks(monkeypatch):
    from plm import gitlinks
    monkeypatch.setattr(gitlinks, "WEB_BASE", "https://plm.example")
    assert gitlinks.browse_url("CID-ASM-0001") == "https://plm.example/CID-ASM-0001"
    assert gitlinks.browse_url("CID-ASM-0001", "draft/v0.4.1") == \
        "https://plm.example/CID-ASM-0001/tree/draft/v0.4.1"
    assert gitlinks.compare_url("CID-ASM-0001", "v0.1.0", "draft/v0.2.0") == \
        "https://plm.example/CID-ASM-0001/compare/v0.1.0...draft/v0.2.0"


def test_change_tab_links():
    assert webroutes.change(380, "files") == "/pulls/380/files"
    assert webroutes.change("CHG-380", "checks") == "/pulls/380/checks"
    assert webroutes.change("#380") == "/pulls/380"
    assert webroutes.change(380, "conversation") == "/pulls/380"
    assert webroutes.change(380, "../x") == "/pulls/380"
    assert webroutes.PULLS == "/pulls"
    assert webroutes.change("CHG380") == webroutes.change(" 380 ") == "/pulls/380"
    assert webroutes.pull("CID-ASM-0001", 380, "commits") == "/CID-ASM-0001/pull/380/commits"
    assert webroutes.CHANGE_TABS == ("commits", "checks", "files")


def test_emitter_changes(monkeypatch):
    from plm import changes, config
    monkeypatch.setattr(config, "PUBLIC_URL", "https://plm.example")
    assert changes.page(380) == "/pulls/380"
    assert changes.url(380) == "https://plm.example/pulls/380"
    assert changes.ref(380) == "#380" and changes.label(380) == "CHG-380"
    for spelling in ("CHG-380", "chg380", "#380", "380", " 380 "):
        assert changes.parse_id(spelling) == 380


def test_emitter_proposals(monkeypatch):
    from plm import config, proposals
    monkeypatch.setattr(config, "PUBLIC_URL", "https://plm.example")
    assert proposals.page("CID-ASM-0001", 17) == "/CID-ASM-0001/pull/17"
    assert proposals.url("CID-ASM-0001", 17) == "https://plm.example/CID-ASM-0001/pull/17"
    assert proposals._change_page(380) == "/pulls/380"


def test_emitter_releasesets(monkeypatch):
    from plm import config, releasesets
    monkeypatch.setattr(config, "PUBLIC_URL", "https://plm.example")
    assert releasesets.page(12) == "/release-sets/12"
    assert releasesets.url(12) == "https://plm.example/release-sets/12"
    # a member's page: the release by its tag (GitHub's shape), id as fallback
    assert webroutes.release("CID-ASM-0001", 44, "v0.2.0") == \
        "/CID-ASM-0001/releases/tag/v0.2.0"
    assert webroutes.release("CID-ASM-0001", 44, "") == "/CID-ASM-0001/releases/44"


def test_emitter_revreport():
    from plm import revreport
    rep = {"change": "CHG-380", "items": [{
        "item": "CID-ASM-0001",
        "before": {"sha": "a" * 40, "ref": "v0.1.0", "version": "0.1.0"},
        "after": {"sha": "b" * 40, "ref": "v0.2.0", "version": "0.2.0"},
        "changed": True, "files": [], "manifest": {"attributes": []},
        "disposition": {}, "adoption": [],
        "bom": {"lines_after": 1, "requirements_after": 0, "pins": [], "lines": []}}]}
    md = revreport.markdown(rep, web="https://plm.example")
    assert "(https://plm.example/pulls/380)" in md
    assert "(https://plm.example/CID-ASM-0001/bom)" in md
    assert f"(https://plm.example/CID-ASM-0001/compare/{'a' * 12}...{'b' * 12})" in md
    assert "#/" not in md


def test_emitter_push_door_lines():
    """plm-git's `remote:` lines — built from webroutes, so assert the shapes."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "plm-git", "plm_git", "gitfirst.py").read_text()
    assert "#/" not in src
    assert "webroutes.item(number)" in src
    assert "webroutes.compare(number, rb['sha'][:12], new[:12])" in src
    assert webroutes.compare("CID-ASM-0001", "a" * 12, "b" * 12) == \
        f"/CID-ASM-0001/compare/{'a' * 12}...{'b' * 12}"


def test_emitter_snippets(monkeypatch):
    from plm import config, snippets
    monkeypatch.setattr(config, "PUBLIC_URL", "https://plm.example")
    assert snippets.viewer_url("tok_123") == "https://plm.example/snip/tok_123"


def test_no_server_code_writes_a_hash_route():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for d in ("plm", "plm_mcp", "plm-git"):
        for f in root.joinpath(d).rglob("*.py"):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                # an f-string / string that BUILDS a hash link
                if re.search(r"""(\}/#/|\bf["']#/|["']#/(item|change|release-set|snip|project|registry|search|approvals))""", line):
                    offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# -- /<N>/pull/<n> ------------------------------------------------------------------

def _seed_pull(conn):
    from plm import changes, proposals
    proposals.ensure(conn)
    changes.ensure(conn)
    now = db.now()
    for num in ("PULL-ASM-0001", "PULL-ASM-0002"):
        conn.execute("INSERT OR IGNORE INTO items (number, name, kind, project, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?)",
                     (num, num, "assembly", "pulltest", now, now))
    iid = {r[0]: r[1] for r in conn.execute(
        "SELECT number, id FROM items WHERE number LIKE 'PULL-%'")}
    conn.execute("INSERT OR REPLACE INTO changes (id, project, title, state, created_at, updated_at) "
                 "VALUES (901, 'pulltest', 't', 'open', ?, ?)", (now, now))
    conn.execute("INSERT OR REPLACE INTO changes (id, project, title, state, created_at, updated_at) "
                 "VALUES (902, 'pulltest', 't', 'open', ?, ?)", (now, now))
    rows = [  # id, item, change
        (901, "PULL-ASM-0001", 902),   # proposal 901 on 0001, in CHG-902
        (950, "PULL-ASM-0001", 901),   # 0001 is a member of CHG-901 too  -> collision on 901
        (902, "PULL-ASM-0002", 902),   # proposal 902 is in CHG-902: same subject
        (903, "PULL-ASM-0002", None),  # a lone proposal
    ]
    for pid, num, cid in rows:
        conn.execute("INSERT OR REPLACE INTO proposals (id, item_id, state, change_id, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (pid, iid[num], "open", cid, now, now))


def test_pull_resolver(client):
    with db.connect() as conn:
        _seed_pull(conn)
    r = client.get("/api/items/PULL-ASM-0001/pull/901").json()
    assert r["kind"] == "change" and r["change"] == "CHG-901" and r["also_proposal"] == 901
    assert r["page"] == "/pulls/901"
    r = client.get("/api/items/PULL-ASM-0002/pull/902").json()
    assert r["kind"] == "change" and "also_proposal" not in r
    r = client.get("/api/items/PULL-ASM-0002/pull/903").json()
    assert r["kind"] == "proposal" and r["proposal"] == 903
    assert r["page"] == "/PULL-ASM-0002/pull/903"
    assert client.get("/api/items/PULL-ASM-0002/pull/904").status_code == 404


# -- the old names of a pull request: permanent redirects ------------------------

@pytest.mark.parametrize("old,new", [
    ("/changes/CHG-380", "/pulls/380"),
    ("/changes/CHG-380/files", "/pulls/380/files"),
    ("/changes/380/checks?x=1", "/pulls/380/checks?x=1"),
    ("/changes/CHG380", "/pulls/380"),
    ("/changes", "/pulls"),
    ("/changes/nonsense", "/pulls"),
    ("/item/CID-ASM-0001", "/CID-ASM-0001"),
    ("/item/CID-ASM-0001/bom?ref=v1", "/CID-ASM-0001/bom?ref=v1"),
])
def test_old_pull_request_paths_redirect(client, signed_in, old, new):
    # where a page would get the shell: after sign-in (an anonymous visitor
    # signs in at the old path and lands on this 301 after the round trip)
    r = _session(client).get(old, headers=HTML)
    assert r.status_code == 301 and r.headers["location"] == new
    assert webroutes.legacy_redirect(old.split("?")[0]) == new.split("?")[0]


def test_legacy_redirect_leaves_everything_else():
    for p in ("/pulls/380", "/CID-ASM-0001/pull/3", "/approvals", "/", "/changesx",
              "/item", "/change-requests/CHG-1"):
        assert webroutes.legacy_redirect(p) is None


def test_change_requests_never_shipped_is_a_404(client, signed_in):
    """`/change-requests/…` was a name for a day and never shipped: no alias."""
    r = _session(client).get("/change-requests/CHG-380", headers=HTML)
    assert r.status_code == 404


def test_api_pulls_is_the_same_router_not_a_rewrite(client):
    """`/api/pulls/…` is the changes router mounted a second time — no
    middleware rewrites paths (it used to wrap every request, streamed meshes
    included)."""
    from plm import api
    for verb, path in (("get", "/CHG-999999/preflight"),
                       ("post", "/CHG-999999/dispositions/draft")):
        a = getattr(client, verb)("/api/pulls" + path, **({"json": {}} if verb == "post" else {}))
        b = getattr(client, verb)("/api/changes" + path, **({"json": {}} if verb == "post" else {}))
        assert a.status_code == b.status_code != 405 and a.json() == b.json(), path
    assert not any(getattr(m, "kwargs", {}).get("dispatch") and
                   "pulls" in getattr(m.kwargs["dispatch"], "__name__", "")
                   for m in api.app.user_middleware)


def test_api_pulls_is_an_alias_of_api_changes(client):
    a = client.get("/api/pulls?state=merged")
    b = client.get("/api/changes?state=merged")
    assert a.status_code == 200 and a.json() == b.json()
    assert client.get("/api/pulls/CHG-999999").status_code == \
        client.get("/api/changes/CHG-999999").status_code == 404
