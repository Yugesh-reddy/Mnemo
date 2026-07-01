"""Project identity for shared memory: a repository's normalized remote, else its path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mnemo.project import detect_project, normalize_remote


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Yugesh-reddy/Mnemo.git",
        "https://github.com/Yugesh-reddy/Mnemo.git",
        "https://user:secret-token@github.com/Yugesh-reddy/Mnemo",
        "ssh://git@github.com/Yugesh-reddy/Mnemo.git",
        "ssh://git@github.com:22/yugesh-reddy/mnemo/",
    ],
)
def test_remote_forms_normalize_to_one_project(url):
    assert normalize_remote(url) == "github.com/yugesh-reddy/mnemo"


def test_normalized_remote_never_keeps_credentials():
    project = normalize_remote("https://user:secret@example.com/team/app.git")
    assert project == "example.com/team/app"


def test_local_path_remotes_are_not_projects():
    assert normalize_remote("/srv/git/app.git") is None
    assert normalize_remote("") is None


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_repository_with_a_remote_is_that_remote_from_any_subdirectory(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "origin", "git@github.com:Acme/Widget.git")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    assert detect_project(nested) == "github.com/acme/widget"


def test_repository_without_a_remote_is_its_top_level_path(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "sub").mkdir()
    assert detect_project(tmp_path / "sub") == f"path:{tmp_path.resolve()}"


def test_outside_a_repository_there_is_no_project(tmp_path):
    assert detect_project(tmp_path) is None
