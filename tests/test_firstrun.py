"""`docker compose up` with nothing before it: plm.firstrun seeds ./data."""
from __future__ import annotations

import os
import re
import stat

from plm import firstrun


def test_firstrun_seeds_once_and_never_prints_a_token(tmp_path):
    uid, gid = os.getuid(), os.getgid()
    lines: list[str] = []
    out = firstrun.run(tmp_path, uid, gid, "alice", log=lines.append)
    assert set(firstrun.DIRS) <= {p.name for p in tmp_path.iterdir()}
    assert "secrets/push-tokens" in out["created"]
    tokens = tmp_path / "secrets" / "push-tokens"
    index = tmp_path / "secrets" / "index-token"
    body = tokens.read_text()
    m = re.search(r"^(plm_[0-9a-f]{48})=alice$", body, re.M)
    assert m, body
    assert stat.S_IMODE(tokens.stat().st_mode) == 0o600
    assert stat.S_IMODE(index.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
    assert len(index.read_text().strip()) == 48
    # it says where the token is, never what it is
    assert not any(m.group(1) in line for line in lines)
    assert any("push-tokens" in line for line in lines)

    # a second run keeps everything (the token file is the credential store)
    again = firstrun.run(tmp_path, uid, gid, "bob", log=lines.append)
    assert again["created"] == []
    assert tokens.read_text() == body
    assert any(line.startswith("kept") for line in lines)


def test_firstrun_cli_defaults_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLM_FIRSTRUN_DATA", str(tmp_path))
    monkeypatch.setenv("PLM_ADMIN", "  geoff ")
    monkeypatch.setenv("PLM_UID", str(os.getuid()))
    monkeypatch.setenv("PLM_GID", str(os.getgid()))
    assert firstrun.main([]) == 0
    assert "=geoff\n" in (tmp_path / "secrets" / "push-tokens").read_text()
