"""`main` is a fact the server reports (GitHub lists it first under Branches):
the default branch, where it points and which tag sits on it. The client never
invents it, so the three repo shapes below have to come back right."""
import subprocess
from pathlib import Path

import pytest

from plm import gitrepo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _bare(tmp_path: Path, number: str) -> Path:
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "plm.toml").write_text(f'number = "{number}"\nversion = "0.1.0"\n')
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "first")
    bare = tmp_path / f"{number}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(gitrepo, "REPOS", tmp_path)
    return tmp_path


def test_main_reported_with_its_tag_and_counted(repos):
    bare = _bare(repos, "T-ASM-0001")
    _git(bare, "tag", "-a", "v0.1.0", "-m", "release", "main")
    _git(bare, "branch", "draft/topic", "main")
    r = gitrepo.refs("T-ASM-0001")
    assert r["main"]["ref"] == "main" and r["main"]["default"] is True
    assert r["main"]["at"] == "v0.1.0"
    assert [d["ref"] for d in r["drafts"]] == ["draft/topic"]
    assert r["counts"] == {"releases": 1, "drafts": 1, "branches": 2,
                           "commits": r["counts"]["commits"]}


def test_main_without_a_tag_is_still_a_branch(repos):
    _bare(repos, "T-CAT-0001")                    # catalog.git: commits on main, no tag
    r = gitrepo.refs("T-CAT-0001")
    assert r["main"]["ref"] == "main" and r["main"]["at"] == ""
    assert r["counts"]["branches"] == 1 and r["drafts"] == []


def test_no_main_means_none(repos):
    bare = _bare(repos, "T-PRT-0001")
    _git(bare, "branch", "draft/v0.1.0", "main")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/draft/v0.1.0")
    _git(bare, "branch", "-D", "main")
    r = gitrepo.refs("T-PRT-0001")
    assert r["main"] is None
    assert r["counts"]["branches"] == 1 and r["default_ref"] == "draft/v0.1.0"
