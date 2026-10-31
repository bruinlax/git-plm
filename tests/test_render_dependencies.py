"""Render sandboxes must contain the exact transitive, local pinned tree."""
import io
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from plm import checks, gitrepo


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args],
                                   stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    store = tmp_path / 'repos'
    store.mkdir()
    monkeypatch.setattr(gitrepo, 'REPOS', store)
    def local_repo(number):
        gitrepo.safe_number(number)
        path = store / f'{number}.git' / '.git'
        if not (path / 'HEAD').exists():
            raise gitrepo.RepoError(f'no repo for {number}', 404)
        return path
    monkeypatch.setattr(gitrepo, 'repo_path', local_repo)

    def make(number, files=None, links=None):
        repo = store / f'{number}.git'
        repo.mkdir()
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        for name, content in (files or {'plm.toml': ''}).items():
            dest = repo / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        git(repo, 'add', '.')
        for name, sha in (links or {}).items():
            git(repo, 'update-index', '--add', '--cacheinfo', f'160000,{sha},{name}')
        git(repo, 'commit', '-qm', 'fixture')
        return repo / '.git', git(repo, 'rev-parse', 'HEAD')
    return make


def test_nested_parts_libs_use_pins_not_heads(tmp_path, repos):
    lib, libsha = repos('TST-PRT-0003', {'helper.scad': 'original'})
    part, partsha = repos('TST-PRT-0002', {'posed.stl': 'posed'},
                         {'lib/TST-PRT-0003': libsha})
    root, sha = repos('TST-ASM-0001', {'assembly.scad': 'model'},
                      {'parts/TST-PRT-0002': partsha})
    (lib.parent / 'helper.scad').write_text('new HEAD must not appear')
    git(lib.parent, 'commit', '-qam', 'unrelated newer head')
    work = tmp_path / 'render'
    work.mkdir()
    checks._materialize_render_tree(root, sha, work)
    assert (work / 'parts/TST-PRT-0002/posed.stl').read_text() == 'posed'
    assert (work / 'parts/TST-PRT-0002/lib/TST-PRT-0003/helper.scad').read_text() == 'original'
    assert not (work / '.git').exists()


def test_render_smudges_transitive_inputs_before_openscad(tmp_path, repos, monkeypatch):
    _, pin = repos('TST-PRT-0002', {'posed.stl': 'pointer'})
    root, sha = repos('TST-ASM-0001', {'assembly.scad': 'model'},
                     {'parts/TST-PRT-0002': pin})
    seen = []
    def smudge(work):
        mesh = work / 'parts/TST-PRT-0002/posed.stl'
        assert mesh.read_text() == 'pointer'
        mesh.write_text('resolved mesh')
        seen.append('smudged')
    real_run = subprocess.run
    def render(cmd, **kwargs):
        if "-o" not in cmd:
            return real_run(cmd, **kwargs)
        assert seen == ['smudged']
        assert (Path(kwargs['cwd']) / 'parts/TST-PRT-0002/posed.stl').read_text() == 'resolved mesh'
        Path(cmd[cmd.index('-o') + 1]).write_bytes(b'rendered geometry')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(checks.lfs, 'smudge_dir', smudge)
    monkeypatch.setattr(checks.subprocess, 'run', render)
    monkeypatch.setattr(checks, 'mesh_facts', lambda data: data)
    assert checks._render(SimpleNamespace(repo=root, sha=sha), 'assembly.scad', []) == b'rendered geometry'


@pytest.mark.parametrize('path', ['other/TST-PRT-0002', 'parts/nested/TST-PRT-0002'])
def test_unknown_gitlink_paths_refused(tmp_path, repos, path):
    _, pin = repos('TST-PRT-0002')
    root, sha = repos('TST-ASM-0001', links={path: pin})
    with pytest.raises(RuntimeError, match='unsupported render dependency'):
        checks._materialize_render_tree(root, sha, tmp_path / 'render')


def test_missing_local_pin_fails_without_fetch(tmp_path, repos):
    root, sha = repos('TST-ASM-0001', links={'parts/TST-PRT-0002': 'a' * 40})
    with pytest.raises(Exception, match='no repo'):
        checks._materialize_render_tree(root, sha, tmp_path / 'render')


@pytest.mark.parametrize('name,link', [('../escape', ''), ('parts', '/tmp')])
def test_archive_escape_refused(tmp_path, monkeypatch, name, link):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        entry = tarfile.TarInfo(name)
        if link:
            entry.type = tarfile.SYMTYPE
            entry.linkname = link
        archive.addfile(entry)
    monkeypatch.setattr(checks.gitvalidate, 'git', lambda *a, **k: stream.getvalue())
    with pytest.raises(tarfile.FilterError):
        checks._materialize_render_tree(tmp_path, 'a' * 40, tmp_path / 'render')


def test_expansion_bound(tmp_path):
    with pytest.raises(RuntimeError, match='128 repositories'):
        checks._materialize_render_tree(tmp_path, 'a' * 40, tmp_path / 'render', budget=[0])
    identity = (str(tmp_path.resolve()), 'a' * 40)
    with pytest.raises(RuntimeError, match='cycle or depth'):
        checks._materialize_render_tree(tmp_path, 'a' * 40, tmp_path / 'render', active=frozenset({identity}))
