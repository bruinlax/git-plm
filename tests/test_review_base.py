"""The review baseline is never the head itself (CHG-380, 2026-10-16).

CHG-380's members were pushed to `draft/usb-correction` while plm's own
`draft/v0.2.1` pointed at the SAME commit. The inference took that sibling as
"the version this head grew from", so every member was diffed against itself
and the change page's Files tab read "no diff" on a real fit correction.
"""
import subprocess

from plm import gitvalidate


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, version, msg):
    (repo / "plm.toml").write_text(
        f'schema = 1\nnumber = "TST-PRT-0001"\nversion = "{version}"\n')
    _git(repo, "add", "plm.toml")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def test_sibling_branch_at_the_head_is_not_its_baseline(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "draft/v0.2.0")
    a = _commit(repo, "0.2.0", "v0.2.0")
    _git(repo, "checkout", "-qb", "draft/usb-correction")
    b = _commit(repo, "0.2.1", "lower the USB opening")
    _git(repo, "branch", "draft/v0.2.1", b)          # plm's sibling, same tip
    ref = "refs/heads/draft/usb-correction"
    # the push door's answer is unchanged (a re-push of v0.2.1's tip)…
    assert gitvalidate.bump_base_for(repo / ".git", b, ref) == ("0.2.1", "draft", "draft/v0.2.1")
    # …but a REVIEW baseline must be something the head descends FROM
    v, kind, where = gitvalidate.bump_base_for(repo / ".git", b, ref, strict=True)
    assert (v, kind, where) == ("0.2.0", "draft", "draft/v0.2.0")
    assert _git(repo, "rev-parse", f"refs/heads/{where}") == a


def test_stored_self_baseline_is_reinferred(monkeypatch):
    from plm import proposals
    seen = {}

    def fake(number, sha, ref="", **k):
        seen["args"] = (number, sha, ref)
        return {"ref": "refs/heads/draft/v0.2.0", "sha": "a" * 40,
                "how": "inferred", "version": "0.2.0", "label": "draft/v0.2.0"}
    monkeypatch.setattr(proposals, "review_base", fake)
    p = {"head_sha": "b" * 40, "head_ref": "refs/heads/draft/usb-correction",
         "review_base_sha": "b" * 40, "review_base_ref": "refs/heads/draft/v0.2.1",
         "review_base_how": "inferred", "state": "open"}
    rb = proposals.baseline(p, "TST-PRT-0001")
    assert rb["sha"] == "a" * 40 and rb["stored"] is False
    assert seen["args"][1] == "b" * 40
    # an author's explicit base is kept, even if odd
    p2 = {**p, "review_base_how": "push-option"}
    assert proposals.baseline(p2, "TST-PRT-0001")["sha"] == "b" * 40
