"""Pending on a repo / a project / the viewer (the PR page's banners + bell).

GET /api/items/<N>/pending, GET /api/projects/<p>/pending, and the viewer
fields on GET /api/changes/<CHG-n> and GET /api/waiting — read-only, against a
scratch DB built the way test_real_paths builds one.
"""
import pytest
from fastapi.testclient import TestClient

from plm import api, db


@pytest.fixture(scope="module")
def client():
    db.init()
    with db.connect() as conn:
        _seed(conn)
    return TestClient(api.app, follow_redirects=False)


def _seed(conn):
    from plm import changes, proposals
    proposals.ensure(conn)
    changes.ensure(conn)
    now = db.now()
    for u, kind, roles in (("pgeoff", "human", "approver,editor"),
                           ("pviewer", "human", "editor"),
                           ("pbot", "bot", "editor")):
        conn.execute("INSERT OR IGNORE INTO users (username, kind, roles, created_at) "
                     "VALUES (?,?,?,?)", (u, kind, roles, now))
    for num in ("PND-PRT-0001", "PND-PRT-0002", "PND-ASM-0001", "PND-PRT-0009"):
        conn.execute("INSERT OR IGNORE INTO items (number, name, kind, project, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (num, num.lower(), "assembly" if "ASM" in num else "part",
                      "pendtest", now, now))
    iid = {r[0]: r[1] for r in conn.execute(
        "SELECT number, id FROM items WHERE number LIKE 'PND-%'")}
    conn.execute("INSERT OR REPLACE INTO changes (id, project, title, state, author, "
                 "created_at, updated_at, submitted_at) VALUES "
                 "(931, 'pendtest', 'USB correction', 'open', 'pbot', ?, ?, ?)",
                 (now, now, now))
    conn.execute("INSERT OR REPLACE INTO changes (id, project, title, state, author, "
                 "created_at, updated_at) VALUES "
                 "(932, 'pendtest', 'old work', 'merged', 'pbot', ?, ?)", (now, now))
    rows = [  # id, item, change, state, ref, sha
        (931, "PND-PRT-0001", 931, "open", "refs/heads/draft/usb", "a" * 40),
        (932, "PND-ASM-0001", 931, "open", "refs/heads/draft/usb", "b" * 40),
        (933, "PND-PRT-0001", None, "draft", "refs/heads/draft/v0.2.1", "a" * 40),
        (934, "PND-PRT-0002", None, "draft", "refs/heads/draft/rib", "c" * 40),
        (935, "PND-PRT-0002", 932, "merged", "refs/heads/draft/old", "d" * 40),
    ]
    for pid, num, cid, st, ref, sha in rows:
        conn.execute("INSERT OR REPLACE INTO proposals (id, item_id, state, change_id, "
                     "head_ref, head_sha, version, title, review_base_how, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, iid[num], st, cid, ref, sha, "0.2.1", f"t{pid}", "none",
                      now, now))


def test_item_pending_for_an_approver(client):
    r = client.get("/api/items/PND-PRT-0001/pending?actor=pgeoff").json()
    assert r["number"] == "PND-PRT-0001" and r["viewer"] == "pgeoff"
    [c] = r["changes"]
    assert c["number"] == "CHG-931" and c["state"] == "open"
    assert c["can_decide"] is True and c["why_not"] == ""
    assert "pgeoff" in c["waiting_on"] and "pbot" not in c["waiting_on"]
    assert [m["item"] for m in c["members"]] == ["PND-PRT-0001", "PND-ASM-0001"]
    assert c["page"] == "/pulls/931" and c["ref"] == "#931" and "pull" not in c
    assert c["open"] is True and [m["label"] for m in c["members"]] == ["p931", "p932"]
    # draft/v0.2.1 is the SAME head as the member in CHG-931: not a loose draft
    assert r["drafts"] == []


def test_item_pending_for_somebody_else(client):
    c = client.get("/api/items/PND-PRT-0001/pending?actor=pviewer").json()["changes"][0]
    assert c["can_decide"] is False and "approver" in c["why_not"]
    c = client.get("/api/items/PND-PRT-0001/pending?actor=pbot").json()["changes"][0]
    assert c["can_decide"] is False
    c = client.get("/api/items/PND-PRT-0001/pending").json()["changes"][0]
    assert c["can_decide"] is False and c["why_not"] == "sign in to decide"


def test_loose_draft_banner(client):
    r = client.get("/api/items/PND-PRT-0002/pending?actor=pgeoff").json()
    assert r["changes"] == []                       # CHG-932 merged: history
    [d] = r["drafts"]
    assert d["branch"] == "draft/rib" and d["proposal"] == 934
    assert d["page"] == "/PND-PRT-0002/pull/934"
    assert d["push"].startswith('git push -o pr=new -o title="t934"')
    assert client.get("/api/items/PND-PRT-0009/pending").json() == {
        "number": "PND-PRT-0009", "viewer": "", "state": "open", "changes": [],
        "drafts": []}
    assert client.get("/api/items/NOPE-PRT-0001/pending").status_code == 404


def test_project_pending_dedups_by_change(client):
    r = client.get("/api/projects/pendtest/pending?actor=pgeoff").json()
    assert [c["number"] for c in r["changes"]] == ["CHG-931"]   # 2 items, 1 row
    assert [d["proposal"] for d in r["drafts"]] == [934]


def test_change_view_carries_the_viewer(client, monkeypatch):
    from plm import changes
    # the full view reads the members' repos (none here); the viewer block is
    # what this test is about
    monkeypatch.setattr(changes, "view", lambda conn, cid: {"change": {"id": 931}})
    v = client.get("/api/changes/CHG-931?actor=pgeoff").json()
    assert v["viewer"]["can_decide"] is True and v["viewer"]["actor"] == "pgeoff"
    v = client.get("/api/changes/CHG-931?actor=pviewer").json()
    assert v["viewer"]["can_decide"] is False and "pgeoff" in v["viewer"]["waiting_on"]


def test_waiting_says_whose(client):
    w = client.get("/api/waiting?actor=pgeoff").json()
    row = next(r for r in w["waiting"] if r.get("number") == "CHG-931")
    assert row["can_decide"] is True and w["yours"] >= 1
    w = client.get("/api/waiting?actor=pviewer").json()
    row = next(r for r in w["waiting"] if r.get("number") == "CHG-931")
    assert row["can_decide"] is False


def test_item_pull_requests_tab_reads_state_all(client):
    """The item's Pull requests tab: `state=all` adds history (CHG-932 merged)
    to the open rows the banners read; each member row carries its checks
    state and the tag a merge produced, and each row says whether it is open."""
    live = client.get("/api/items/PND-PRT-0002/pending").json()
    assert live["changes"] == [] and live["state"] == "open"
    allp = client.get("/api/items/PND-PRT-0002/pending?state=all").json()
    assert [c["number"] for c in allp["changes"]] == ["CHG-932"]
    assert allp["changes"][0]["open"] is False
    [m] = allp["changes"][0]["members"]
    assert m["state"] == "merged" and "merged_tag" in m and "checks" in m
    closed = client.get("/api/items/PND-PRT-0002/pending?state=closed").json()
    assert [c["state"] for c in closed["changes"]] == ["merged"]
    assert client.get("/api/items/PND-PRT-0002/pending?state=nope").status_code == 404
    proj = client.get("/api/projects/pendtest/pending?state=all").json()
    assert {c["number"] for c in proj["changes"]} == {"CHG-931", "CHG-932"}


def test_open_is_defined_once():
    """ONE definition of open (changes.OPEN): `?state=open` filters on it,
    every row carries it, and a refused pull request is open (the next push
    re-opens it) — the list, the banners and the badge all read the row."""
    from plm import changes, pending
    assert changes.OPEN == ("draft", "open", "rejected")
    assert set(changes.OPEN) | set(changes.CLOSED) == set(changes.STATES)
    assert not set(changes.OPEN) & set(changes.CLOSED)
    assert pending.STATES == {"open": changes.OPEN, "closed": changes.CLOSED,
                              "all": changes.STATES}


def test_api_changes_rows_are_change_entries(client):
    """The global list (/pulls) reads /api/changes: the same row shape as the
    banners (`members`, `open`, the viewer's `can_decide`, merged tags, the
    checks dot), plus the queue's threads / pin bumps — never `items`."""
    rows = client.get("/api/changes?actor=pgeoff&limit=300").json()["changes"]
    row = next(r for r in rows if r["number"] == "CHG-931")
    assert row["open"] is True and row["can_decide"] is True and row["ref"] == "#931"
    assert "items" not in row and "pull" not in row
    assert {"merged_tag", "checks", "label", "kind"} <= set(row["members"][0])
    assert "threads" in row and "pin_bumps" in row
    merged = next(r for r in rows if r["number"] == "CHG-932")
    assert merged["open"] is False and merged["can_decide"] is False


def test_checks_dot_reads_stored_rows_only(client, monkeypatch):
    """The dot on a list row is the rollup of the STORED rows — no Ctx (no git
    ls-tree / cat-file) per member per row on a page view."""
    from plm import checks, db
    monkeypatch.setattr(checks, "Ctx", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a list row opened a repo")))
    with db.connect() as conn:
        checks.ensure_schema(conn)
        sha = conn.execute("SELECT head_sha FROM proposals WHERE id=931").fetchone()[0]
        conn.execute("DELETE FROM checks WHERE number='PND-PRT-0001' AND sha=?", (sha,))
        conn.execute("INSERT INTO checks (item_id, number, sha, name, status, source, "
                     "created_at) VALUES (0,'PND-PRT-0001',?,'manifest','fail','builtin',?)",
                     (sha, db.now()))
        conn.commit()
    c = client.get("/api/items/PND-PRT-0001/pending").json()["changes"][0]
    assert next(m for m in c["members"] if m["item"] == "PND-PRT-0001")["checks"] == "red"
