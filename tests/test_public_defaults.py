"""plm is deployable by a stranger: no operator-specific address is a DEFAULT.

An operator's values (hostnames, LAN, OIDC provider) reach a deployment
through its own .env. In code they may appear only in
COMMENTS (history: where a rule came from). A string, a docstring or a default
in these files is what a stranger's instance would say or try to reach, so it
must be neutral.
"""
from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "plm/config.py",
    "plm/identity.py",
    "plm/gitlinks.py",
    "plm/gitrepo.py",
    "plm-git/plm_git/config.py",
    "plm_mcp/instructions.py",
]
FORBIDDEN = ("doempke", "jaw" + "breaker", "192." + "168.")


def _code_without_comments(path: Path) -> list[tuple[int, str]]:
    out = []
    src = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append((tok.start[0], tok.string))
    return out


@pytest.mark.parametrize("rel", FILES)
def test_no_operator_strings_outside_comments(rel):
    hits = [(line, text.strip()[:80])
            for line, text in _code_without_comments(ROOT / rel)
            for bad in FORBIDDEN if bad in text.lower()]
    assert not hits, f"{rel}: operator-specific value outside a comment: {hits}"


def test_neutral_runtime_defaults(tmp_path):
    """The values a stranger's instance actually computes with no env — in a
    fresh interpreter, so this process's configured modules stay untouched."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PLM_", "OIDC_", "PLMGIT_"))}
    env.update(PLM_DB=str(tmp_path / "plm.sqlite3"), HOME=str(tmp_path),
               PYTHONPATH=f"{ROOT}{os.pathsep}{ROOT / 'plm-git'}")
    code = ("from plm import config, identity, gitlinks, gitrepo\n"
            "from plm_git import config as gc\n"
            "print(config.PUBLIC_URL, config.BOT_EMAIL, identity.ISSUER or '-',"
            " gitlinks.WEB_BASE, gitrepo.CLONE_BASE, gc.VAULT, gc.COMMITTER_EMAIL)")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["http://localhost:8097", "plm@localhost", "-",
                   "http://localhost:8097", "http://localhost:8097/git",
                   "/vault", "plm-git@localhost"]
