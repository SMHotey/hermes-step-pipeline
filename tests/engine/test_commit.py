# -*- coding: utf-8 -*-
"""Безопасный коммит: страж коммитит ровно свои файлы, чужие правки остаются в индексе.

Проверяется поведение `gate.commit_and_push` в настоящем git-репозитории: посторонний
файл, добавленный в индекс до тика, в коммит стража не попадает; отсутствие своих
изменений — успех с текстом «нечего коммитить», а не падение; повторный тик после
успешного коммита второго коммита не создаёт; репозиторий без upstream не роняет
закрытие шага; при настроенном upstream пуш выполняется и origin догоняет HEAD.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import config as config_mod
import gate
from engine_kit import make_project

PLAN_REL = "docs/plans/1-demo.md"
REPORT_REL = "docs/reviews/1-S1.md"
MSG = "шаг S2: коммит только своих файлов"


def sh(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def git(cwd: Path, *args: str) -> str:
    res = sh(["git", *args], cwd)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def make_repo(tmp_path: Path, push: bool = False) -> Path:
    """Проект-приёмник, превращённый в git-репозиторий с одним начальным коммитом."""
    root = make_project(tmp_path / "proj", git={"commit": True, "push": push})
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def with_upstream(root: Path, tmp_path: Path) -> Path:
    """Голый репозиторий рядом с проектом плюс настроенный upstream ветки main."""
    bare = tmp_path / "origin.git"
    git(root, "init", "--bare", str(bare))
    git(root, "remote", "add", "origin", bare.as_posix())
    git(root, "push", "-u", "origin", "main")
    return bare


def paths(root: Path) -> gate.Paths:
    return gate.resolve_paths(config_mod.load(root), "1")


def head_files(root: Path) -> list[str]:
    return sorted(ln.strip() for ln in git(root, "show", "--name-only",
                                           "--pretty=format:", "HEAD").splitlines() if ln.strip())


def staged(root: Path) -> list[str]:
    return sorted(ln.strip() for ln in git(root, "diff", "--cached",
                                           "--name-only").splitlines() if ln.strip())


def commit_count(root: Path) -> int:
    return int(git(root, "rev-list", "--count", "HEAD"))


def touch_plan(root: Path) -> None:
    plan = root / PLAN_REL
    plan.write_text(plan.read_text(encoding="utf-8") + "\nправка шага\n", encoding="utf-8")


def test_stranger_staged_before_tick_stays_out_of_commit(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "stranger.txt").write_text("чужая правка\n", encoding="utf-8")
    git(root, "add", "stranger.txt")
    touch_plan(root)

    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL], MSG)

    assert ok is True, detail
    assert head_files(root) == [PLAN_REL]
    assert staged(root) == ["stranger.txt"]


def test_nothing_to_commit_is_success(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "stranger.txt").write_text("чужая правка\n", encoding="utf-8")
    git(root, "add", "stranger.txt")
    head_before = git(root, "rev-parse", "HEAD")
    count_before = commit_count(root)

    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL], MSG)

    assert ok is True, detail
    assert "нечего коммитить" in detail
    assert git(root, "rev-parse", "HEAD") == head_before
    assert commit_count(root) == count_before
    assert staged(root) == ["stranger.txt"]


def test_second_call_after_commit_makes_no_new_commit(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    touch_plan(root)

    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL], MSG)
    assert ok is True, detail
    assert detail == git(root, "rev-parse", "--short", "HEAD")
    head_after_first = git(root, "rev-parse", "HEAD")
    count_after_first = commit_count(root)

    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL], MSG)

    assert ok is True, detail
    assert "нечего коммитить" in detail
    assert detail == f"нечего коммитить: своих изменений нет (HEAD {git(root, 'rev-parse', '--short', 'HEAD')})"
    assert git(root, "rev-parse", "HEAD") == head_after_first
    assert commit_count(root) == count_after_first
    assert head_files(root) == [PLAN_REL]


def test_repo_without_upstream_does_not_fail(tmp_path: Path) -> None:
    root = make_repo(tmp_path, push=True)
    touch_plan(root)

    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL], MSG)

    assert ok is True, detail
    assert "upstream" in detail.lower()
    assert head_files(root) == [PLAN_REL]


def test_push_runs_when_upstream_configured(tmp_path: Path) -> None:
    root = make_repo(tmp_path, push=True)
    with_upstream(root, tmp_path)
    touch_plan(root)
    (root / REPORT_REL).write_text("## ВЕРДИКТ: APPROVE\n", encoding="utf-8")

    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL, REPORT_REL], MSG)

    assert ok is True, detail
    assert head_files(root) == sorted([PLAN_REL, REPORT_REL])
    assert git(root, "rev-parse", "origin/main") == git(root, "rev-parse", "HEAD")
    assert detail.split(" · ")[0] == git(root, "rev-parse", "--short", "HEAD")

    # D3: повтор после успешного коммита и пуша — «нечего коммитить», но пуш всё равно
    # доходит до origin (иначе отложенный коммит из pending_commit не дотолкает изменения).
    ok, detail = gate.commit_and_push(paths(root), [PLAN_REL, REPORT_REL], MSG)

    assert ok is True, detail
    assert "нечего коммитить" in detail
    assert "пуш выполнен" in detail
    assert git(root, "rev-parse", "origin/main") == git(root, "rev-parse", "HEAD")
