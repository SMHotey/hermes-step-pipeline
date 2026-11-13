# -*- coding: utf-8 -*-
"""Заказ карточки, которой нет на борде — сбой, а не тихий простой.

Движок брал id из вывода `hermes kanban create` и считал заказ выполненным. Если строки
на борде не появлялось (гонка за базу, чужая борда, сбой записи), план вставал: следующий
тик видел пустоту либо карточку в `ready` и уходил в простой, а в журнале было «заказано».
Отсюда часы простоя на живом проекте и мигающий `test_prompt_fix_rounds_exhausted_stops`
в полном прогоне: помощник `complete()` молча ничего не закрывал, и тест падал на другом
шаге. Теперь `_create_card` сверяет заказ с бордой, `_card_visible` ждёт появления строки
ограниченно, а пустой `complete()` — громкий отказ.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, write_approval
from stub_hermes import SCHEMA


def plan_body(count: int = 3) -> str:
    """План из `count` шагов с журналом (все строки «открыт»)."""
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить заказ карточки.", "",
             "## Шаги", ""]
    for i in range(1, count + 1):
        parts += [f"### S{i}. Шаг номер {i} (1 дн)", "",
                  f"- Файл: `app/s{i}.py`",
                  f"- Тесты/гейты: `python -m pytest tests/test_s{i}.py` → 1 passed",
                  f"- Готово = шаг S{i} сделан", ""]
    parts += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|"]
    parts += [f"| — | S{i} | — | — | открыт |" for i in range(1, count + 1)]
    return "\n".join(parts) + "\n"


def write_plan(root: Path) -> Path:
    """План проекта с записью утверждения (она хранит sha файла плана)."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(plan_body(), encoding="utf-8")
    write_approval(root, path)
    return path


def write_prompt(root: Path, recon: str) -> Path:
    """Постановка шага S1 с отметкой `recon_commit:` на текущий HEAD."""
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "2026-11-13_1-S1_prompt.md"
    path.write_text(f"# Постановка S1\n\nrecon_commit: {recon}\n\n## Ревью\n\nроль `reviewer`.\n",
                    encoding="utf-8")
    return path


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def init_repo(root: Path) -> str:
    """Настоящий git-репозиторий проекта; возвращает sha HEAD."""
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return git(root, "rev-parse", "HEAD")


def paths(root: Path):
    return gate.resolve_paths(config_mod.load(root), "1")


def quiet(_text: str) -> None:
    """Заглушка уведомлений: здесь проверяются не тексты."""


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Проект с готовым к старту шагом S1: свежая постановка, план утверждён."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    write_plan(project)
    head = init_repo(project)
    write_prompt(project, head)
    return project, paths(project), hermes_stub


def test_lost_card_is_loud_error(stand, monkeypatch):
    """CLI напечатал id, строки нет — GateError с ключом, а не простой на следующем тике."""
    root, p, db = stand
    monkeypatch.setenv("PIPELINE_STUB_LOST_CREATE", "1")
    with pytest.raises(gate.GateError) as exc:
        gate.run_gate(p, execute=True, notify_fn=quiet)
    message = str(exc.value)
    assert "борды её не знает" in message, message
    assert "planloop:1:S1:step" in message, message
    assert cards(db) == [], "карточки быть не должно: строка не записалась"


def test_normal_order_is_verified_on_board(stand):
    """Обычный заказ проходит только потому, что карточка видна по своему ключу."""
    root, p, db = stand
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    created = [c for c in cards(db) if (c["key"] or "").endswith(":S1:step")]
    assert len(created) == 1, cards(db)
    card = created[0]
    assert gate._card_visible(p, card["id"], card["key"]) is True
    assert gate._card_visible(p, "t_000000", card["key"]) is False


def test_complete_without_card_raises(tmp_path):
    """Пустой `complete()` — отказ: молчаливый no-op терял круг правок и путал тесты."""
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()
    with pytest.raises(AssertionError):
        complete(db, ":promptfix:1")
