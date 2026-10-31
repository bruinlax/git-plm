"""After sign-in you land on exactly the page you clicked (2026-10-16).

Geoff opened https://plm.example.com/#/change/CHG-380 with no session and
ended up on the dashboard: the middleware 302'd `/` to /auth/login?next=/ and
the fragment — which never reaches a server — was gone. These pin both halves:
the sign-in shell an anonymous browser now gets (200, carries the fragment) and
the `next` round trip through the OIDC state row, with its open-redirect guard.
"""
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from plm import api, db, identity

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
    identity._DISCOVERY_AT = 1e18  # never refetch
    # No `with`: the startup hook warms search and starts background sweeps,
    # none of which these tests need.
    return TestClient(api.app, follow_redirects=False)


@pytest.fixture
def fake_idp(monkeypatch):
    """The token endpoint and userinfo, answered locally."""
    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "fake-access-token"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(identity.httpx, "Client", _Client)
    monkeypatch.setattr(identity, "userinfo", lambda tok: {
        "sub": "sub-geoff", "email": "geoff@example.invalid",
        "name": "Geoff", "groups": ["plm-users"]})


def _login(client, nxt_encoded: str):
    """GET /auth/login?next=<encoded> -> the state the IdP would echo back."""
    r = client.get("/auth/login?next=" + nxt_encoded, headers=HTML)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith("http://idp.invalid/authorize?")
    return parse_qs(urlsplit(loc).query)["state"][0]


def _stored(state: str) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT redirect_to FROM oidc_flows WHERE state=?",
                            (state,)).fetchone()[0]


# -- the guard ----------------------------------------------------------------

@pytest.mark.parametrize("good", [
    "/",
    "/#/change/CHG-380",
    "/#/item/CID-ASM-0001/compare/v0.1.0...v0.2.0",
    "/#/release-set/RS-12",
    "/?x=1#/change/CHG-380",
    "/CID-ASM-0001/pull/380",            # a real path, later
    "/CID-ASM-0001/pull/380?tab=files",
    "/#//not-a-host",                    # `//` inside a fragment is harmless
])
def test_safe_next_keeps_same_origin_locations(good):
    assert identity.safe_next(good) == good


@pytest.mark.parametrize("bad", [
    "//evil.example", "//evil.example/#/change/CHG-380", "///evil.example",
    "https://evil.example", "http://evil.example/", "javascript:alert(1)",
    "evil.example", "#/change/CHG-380", "",
    "/\\evil.example", "/\\/evil.example", "/x\\y",
    "/\r\nSet-Cookie: x=1", "/ok\nLocation: //evil", "/ok\r",
    "/\t/evil.example", "/\x00", "/\x7f",
    "/" + "a" * identity.NEXT_MAX,
    None, 42,
])
def test_safe_next_rejects_everything_else(bad):
    assert identity.safe_next(bad) == "/"


# -- the round trip: login stores, callback restores, verbatim ------------------

@pytest.mark.parametrize("target", [
    "/#/change/CHG-380",
    "/#/item/CID-ASM-0001/compare/v0.1.0...v0.2.0",
    "/#/release-set/RS-12",
    "/?x=1#/change/CHG-380",
    "/CID-ASM-0001/pull/380",            # no fragment: identical road
    "/CID-ASM-0001/pull/380?tab=files&y=2",
])
def test_next_round_trips_to_the_exact_redirect(client, fake_idp, target):
    state = _login(client, quote(target, safe=""))
    assert _stored(state) == target
    r = client.get(f"/auth/callback?code=c&state={state}", headers=HTML)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == target
    assert "plm_session=" in r.headers.get("set-cookie", "")


def test_the_exact_url_geoff_clicked(client, fake_idp):
    # what the shell's encodeURIComponent('/' + '' + '#/change/CHG-380') sends
    state = _login(client, "%2F%23%2Fchange%2FCHG-380")
    r = client.get(f"/auth/callback?code=c&state={state}")
    assert r.headers["location"] == "/#/change/CHG-380"


@pytest.mark.parametrize("bad_encoded", [
    quote("//evil.example", safe=""), quote("https://x", safe=""),
    quote("/\\evil", safe=""), quote("/a\r\nX: y", safe=""),
    quote("/\nb", safe=""),
])
def test_login_refuses_off_site_next(client, fake_idp, bad_encoded):
    state = _login(client, bad_encoded)
    assert _stored(state) == "/"
    r = client.get(f"/auth/callback?code=c&state={state}")
    assert r.headers["location"] == "/"


def test_callback_rechecks_a_row_it_did_not_write(client, fake_idp):
    """A state row from an older code line (whose guard was startswith('/'))
    is held to the new guard on the way out."""
    state = _login(client, "%2F")
    with db.connect() as conn:
        conn.execute("UPDATE oidc_flows SET redirect_to=? WHERE state=?",
                     ("//evil.example", state))
    r = client.get(f"/auth/callback?code=c&state={state}")
    assert r.headers["location"] == "/"


# -- the anonymous HTML GET: a 200 shell, not a 302 ------------------------------

@pytest.mark.parametrize("path", ["/", "/?x=1", "/CID-ASM-0001/pull/380"])
def test_anonymous_html_gets_the_signin_shell(client, path):
    r = client.get(path, headers=HTML)
    assert r.status_code == 200
    assert r.headers.get("x-plm-signin") == "required"
    assert "no-store" in r.headers.get("cache-control", "")
    body = r.text
    # the script carries path + query + FRAGMENT, encoded, to /auth/login
    assert "location.pathname + location.search + location.hash" in body
    assert "'/auth/login?next=' + encodeURIComponent(here)" in body
    assert "location.replace(url)" in body
    # no-JS fallback: what the server can see, encoded and guarded
    assert f'href="/auth/login?next={quote(path, safe="")}"' in body


def test_shell_fallback_is_escaped_and_guarded(client):
    r = client.get('/x"><script>alert(1)</script>', headers=HTML)
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text


def test_api_link_and_assets_are_not_shelled(client):
    r = client.get("/api/stats", headers=HTML)
    assert r.status_code == 200
    assert "x-plm-signin" not in r.headers
    assert "application/json" in r.headers["content-type"]
    r = client.get("/auth/whoami", headers=HTML)
    assert r.status_code == 200 and r.json()["verified"] is False


def test_json_get_of_root_is_not_shelled(client):
    r = client.get("/", headers={"Accept": "application/json"})
    assert "x-plm-signin" not in r.headers


def test_signed_in_html_gets_the_app(client, fake_idp):
    state = _login(client, "%2F")
    r = client.get(f"/auth/callback?code=c&state={state}")
    cookie = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    c2 = TestClient(api.app, follow_redirects=False)
    c2.cookies.set(identity.SESSION_COOKIE, cookie)
    r = c2.get("/", headers=HTML)
    assert r.status_code == 200
    assert "x-plm-signin" not in r.headers
    assert "app.js" in r.text


def test_whoami_names_the_instance_admin_as_the_ui_fallback(client, monkeypatch):
    """With nobody signed in, the UI acts as the instance's admin (PLM_ADMIN),
    never as a name baked into the client."""
    monkeypatch.setenv("PLM_ADMIN", "alice")
    body = client.get("/auth/whoami").json()
    assert body["verified"] is False and body["fallback_actor"] == "alice"
    monkeypatch.delenv("PLM_ADMIN")
    assert client.get("/auth/whoami").json()["fallback_actor"] == "admin"
