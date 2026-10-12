import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="git-хуки ставимо лише на Mac розробника, не на Windows"
)

HOOKS = Path(__file__).resolve().parent.parent / ".githooks"
ZERO = "0" * 40
TICKET = "chore/INF-002-zakhyst"
# Без GIT_* з оточення: інакше git у тестах може писати в робочий репозиторій.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=ENV,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "--allow-empty", "--no-verify", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run_hook(repo: Path, hook: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(HOOKS / hook), "origin", "https://example.invalid/repo.git"],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=ENV,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


@pytest.fixture
def history(repo: Path) -> dict[str, str]:
    """base ← ahead (fast-forward) і base ← other (розходиться з ahead)."""
    base = commit(repo, "base")
    ahead = commit(repo, "ahead")
    git(repo, "switch", "-q", "-c", "other", base)
    other = commit(repo, "other")
    return {"base": base, "ahead": ahead, "other": other}


def push_line(local_sha: str, remote_ref: str, remote_sha: str, local_ref: str = "") -> str:
    local_ref = local_ref or f"refs/heads/{TICKET}"
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def test_push_new_ticket_branch_allowed(repo, history):
    result = run_hook(repo, "pre-push", push_line(history["ahead"], f"refs/heads/{TICKET}", ZERO))
    assert result.returncode == 0, result.stderr


def test_push_fast_forward_allowed(repo, history):
    line = push_line(history["ahead"], f"refs/heads/{TICKET}", history["base"])
    result = run_hook(repo, "pre-push", line)
    assert result.returncode == 0, result.stderr


def test_push_to_main_rejected(repo, history):
    result = run_hook(repo, "pre-push", push_line(history["ahead"], "refs/heads/main", ZERO))
    assert result.returncode != 0
    assert "пуш у main заборонено" in result.stderr


def test_delete_rejected(repo, history):
    line = push_line(ZERO, f"refs/heads/{TICKET}", history["base"], local_ref="(delete)")
    result = run_hook(repo, "pre-push", line)
    assert result.returncode != 0
    assert "видалення заборонено" in result.stderr


def test_tag_rejected(repo, history):
    line = push_line(history["base"], "refs/tags/v1", ZERO, local_ref="refs/tags/v1")
    result = run_hook(repo, "pre-push", line)
    assert result.returncode != 0
    assert "лише гілки" in result.stderr


def test_force_push_rejected(repo, history):
    line = push_line(history["other"], f"refs/heads/{TICKET}", history["ahead"])
    result = run_hook(repo, "pre-push", line)
    assert result.returncode != 0
    assert "не fast-forward" in result.stderr


def test_unknown_remote_commit_rejected(repo, history):
    line = push_line(history["ahead"], f"refs/heads/{TICKET}", "1" * 40)
    result = run_hook(repo, "pre-push", line)
    assert result.returncode != 0
    assert "git fetch" in result.stderr


@pytest.mark.parametrize(
    "name", ["my-branch", "feature/inf-2-x", "chore/INF-002_x", "hotfix/INF-2", "chore/INF-2-X"]
)
def test_wrong_branch_name_rejected(repo, history, name):
    result = run_hook(repo, "pre-push", push_line(history["ahead"], f"refs/heads/{name}", ZERO))
    assert result.returncode != 0
    assert "не схожа на гілку тікета" in result.stderr


@pytest.mark.parametrize("name", ["feature/CORE-1", "fix/FIX-12-a-b", "docs/DOC-3-x1"])
def test_good_branch_names_allowed(repo, history, name):
    result = run_hook(repo, "pre-push", push_line(history["ahead"], f"refs/heads/{name}", ZERO))
    assert result.returncode == 0, result.stderr


def test_commit_on_main_rejected(repo):
    result = run_hook(repo, "pre-commit")
    assert result.returncode != 0
    assert "коміт у main заборонено" in result.stderr


def test_commit_on_detached_head_rejected(repo, history):
    git(repo, "switch", "-q", "--detach", history["base"])
    result = run_hook(repo, "pre-commit")
    assert result.returncode != 0
    assert "від'єднаний" in result.stderr


def test_commit_on_ticket_branch_allowed(repo, history):
    git(repo, "switch", "-q", "-c", TICKET)
    result = run_hook(repo, "pre-commit")
    assert result.returncode == 0, result.stderr
