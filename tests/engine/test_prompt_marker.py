# -*- coding: utf-8 -*-
"""D6: версия ревью постановки сверяется по тексту, а не по записи sha.

Страж принимал вердикт только при точном совпадении `recon_commit` отчёта и шапки
постановки. Ревьюер регулярно пишет в отчёт sha HEAD (иногда короткий) вместо sha
постановки — конвейер вставал на формальном расхождении (инцидент 09.11.2026, план 29
шаг R1-C: 8.5 часа простоя).

Правило после правки: расхождение отметок — стоп, НО если текст постановки на коммите
из отчёта совпадает с текущим файлом или обе отметки указывают на один коммит, вердикт
принимается. Подмены нет: вердикт, написанный про переписанную постановку, находит на
своём коммите другой текст и по-прежнему останавливает конвейер.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, write_approval

PROMPT_REL = "docs/prompts/2026-11-04_1-S1_prompt.md"


def plan_body(count: int = 3) -> str:
    """План из `count` шагов с журналом (все строки «открыт»)."""
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить сверку версии ревью.", "",
             "## Шаги", ""]
    for i in range(1, count + 1):
        parts += [f"### S{i}. Шаг номер {i} (1 дн)", "",
                  f"- Файл: `app/s{i}.py`",
                  f"- Тесты/гейты: `python -m pytest tests/test_s{i}.py -q` → 1 passed",
                  f"- Готово = шаг S{i} сделан", ""]
    parts += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|"]
    parts += [f"| — | S{i} | — | — | открыт |" for i in range(1, count + 1)]
    return "\n".join(parts) + "\n"


def write_plan(root: Path, text: str) -> Path:
    """Переписать план проекта и переутвердить его (запись хранит sha файла плана)."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(text, encoding="utf-8")
    write_approval(root, path)
    return path


def write_prompt(root: Path, step: str = "S1", recon: str | None = None,
                 note: str = "первая") -> Path:
    """Постановка шага; `recon` — отметка `recon_commit:` в шапке, `note` — текст редакции."""
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    head = f"recon_commit: {recon}\n" if recon else ""
    path = folder / f"2026-11-04_1-{step}_prompt.md"
    path.write_text(f"# Постановка {step}\n\n{head}\n## Ревью\n\nроль `reviewer`, "
                    f"редакция {note}.\n", encoding="utf-8")
    return path


def write_report(root: Path, verdict: str, recon: str, step: str = "S1") -> Path:
    """Отчёт ревью постановки: отметка версии и одна строка вердикта."""
    folder = Path(root) / "docs" / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"1-{step}-prompt.md"
    path.write_text(f"recon_commit: {recon}\n\n## ВЕРДИКТ: {verdict}\n\nпроверено\n",
                    encoding="utf-8")
    return path


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def init_repo(root: Path) -> str:
    """Настоящий git-репозиторий проекта; возвращает sha первого коммита."""
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    return commit_all(root, "init")


def paths(root: Path):
    """Пути плана 1 после чтения `pipeline.json` проекта."""
    return gate.resolve_paths(config_mod.load(root), "1")


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def history(root: Path) -> dict[str, str]:
    """История постановки из трёх редакций и код после неё.

    Черновик → редакция 2 → редакция 3 (текущий файл) → код после снятия постановки.
    Так воспроизводится инцидент: в шапке постановки стоит sha предыдущей редакции,
    а ревьюер помечает отчёт sha HEAD — вердикт про ту же редакцию, записанный другим
    коммитом. `draft` — коммит, где текст постановки заведомо другой.
    """
    write_plan(root, plan_body(3))
    write_prompt(root, note="черновик")
    draft = init_repo(root)
    write_prompt(root, recon=draft, note="редакция 2")
    old = commit_all(root, "постановка: редакция 2")
    write_prompt(root, recon=old, note="редакция 3")
    current = commit_all(root, "постановка: редакция 3")
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "s1.py").write_text("x = 1\n", encoding="utf-8")
    head = commit_all(root, "код после снятия постановки")
    return {"draft": draft, "old": old, "current": current, "head": head}


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, paths(project), hermes_stub


@pytest.fixture()
def stale(stand):
    """Проект с историей постановки: пути плана и sha коммитов истории."""
    root, p, db = stand
    return root, p, db, history(root)


def test_prompt_text_same_reads_text_not_marker(stale):
    """Сверка идёт по тексту постановки: sha HEAD и sha её редакции дают одно и то же."""
    root, p, db, sha = stale
    assert gate.prompt_text_same(p, PROMPT_REL, sha["head"]) is True
    assert gate.prompt_text_same(p, PROMPT_REL, sha["current"]) is True
    assert gate.prompt_text_same(p, PROMPT_REL, sha["current"][:7]) is True
    assert gate.prompt_text_same(p, PROMPT_REL, sha["old"]) is False      # другая редакция
    assert gate.prompt_text_same(p, PROMPT_REL, sha["draft"]) is False
    assert gate.prompt_text_same(p, PROMPT_REL, "0" * 40) is False        # sha вне репозитория
    assert gate.prompt_text_same(p, PROMPT_REL, "") is False
    assert gate.prompt_text_same(p, None, sha["head"]) is False
    assert gate.prompt_text_same(p, "docs/prompts/нет-такого.md", sha["head"]) is False


def test_prompt_marker_same_commit_accepts_short_record(stale):
    """Короткий sha в отчёте и полный в шапке — тот же коммит, а не расхождение."""
    root, p, db, sha = stale
    full = sha["current"]
    assert gate.prompt_marker_same_commit(p, full, full[:7]) is True
    assert gate.prompt_marker_same_commit(p, full, full) is True
    assert gate.prompt_marker_same_commit(p, full, sha["old"]) is False
    assert gate.prompt_marker_same_commit(p, full, "0" * 40) is False
    assert gate.prompt_marker_same_commit(p, full, "") is False
    assert gate.prompt_marker_same_commit(p, "", full) is False


def test_newlines_are_not_a_revision():
    """CRLF в рабочем дереве против LF в blob — одна редакция, а не правка."""
    assert gate._newlines_unified("a\r\nb") == gate._newlines_unified("a\nb")
    assert gate._newlines_unified("a\r\nb") != gate._newlines_unified("a\n\nb")


def test_verdict_by_head_sha_keeps_the_pipeline_moving(stale):
    """APPROVE помечен sha HEAD вместо sha постановки, текст тот же — шаг стартует."""
    root, p, db, sha = stale
    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_prompt_review", first["decision"]
    complete(db, ":promptreview")
    write_report(root, "APPROVE", sha["head"])

    started = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert started["decision"]["action"] == "create_step", started["decision"]
    assert started["applied"] is True
    assert gate.load_stop(p) is None
    assert "planloop:1:S1:step" in {card["key"] for card in cards(db)}


def test_short_marker_of_prompt_commit_keeps_the_pipeline_moving(stale):
    """Короткая запись коммита постановки в отчёте — тот же коммит, стопа нет."""
    root, p, db, sha = stale
    gate.run_gate(p, execute=True, notify_fn=quiet)
    complete(db, ":promptreview")
    write_report(root, "APPROVE", sha["old"][:7])          # sha из шапки, короткий

    started = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert started["decision"]["action"] == "create_step", started["decision"]
    assert gate.load_stop(p) is None
    assert "planloop:1:S1:step" in {card["key"] for card in cards(db)}


def test_marker_of_revision_with_other_text_stops(stale):
    """Отметка коммита, где постановка была другой, — честный стоп «не про эту версию»."""
    root, p, db, sha = stale
    gate.run_gate(p, execute=True, notify_fn=quiet)
    complete(db, ":promptreview")
    write_report(root, "APPROVE", sha["draft"])

    stopped = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert stopped["decision"]["action"] == "stop", stopped["decision"]
    assert "не про эту версию" in stopped["detail"]
    assert "planloop:1:S1:step" not in {card["key"] for card in cards(db)}
    assert gate.load_stop(p) is not None


def test_ctx_keeps_marker_keys_on_collector_failure(project: Path, monkeypatch):
    """Сбой сборщика шагов не оставляет решение без ключей D6 — иначе `NameError`."""
    def boom(_p):
        raise SystemExit("шагов не видно")

    monkeypatch.setattr(gate, "collector", boom)
    ctx = gate.build_ctx(paths(project))
    assert ctx["prompt_marker_text_same"] is False
    assert ctx["prompt_marker_same_commit"] is False
