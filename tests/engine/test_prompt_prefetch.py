# -*- coding: utf-8 -*-
"""Предзаказ постановки N+1 и гейт закрытия шага без неё (план 2, §S6).

Три состояния стража: постановка N+1 заказывается, пока шаг N в работе (и не заказывается
повторно); у последнего шага плана она не заказывается; шаг N не закрывается в журнале,
пока постановки N+1 нет, и закрывается сразу, как только она появилась. Тесты идут через
`gate.run_gate` на временном проекте с поддельной бордой (`hermes_stub`), карточки
читаются из sqlite.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, write_approval

APPROVE = "## ВЕРДИКТ: APPROVE\n\nГейты: 1 passed\n"


def plan_body(count: int = 3, closed: dict | None = None) -> str:
    """План из `count` шагов с журналом; `closed` задаёт статус строки (`S1` → «ЗАКРЫТ»)."""
    statuses = closed or {}
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить предзаказ постановки.", "",
             "## Шаги", ""]
    for i in range(1, count + 1):
        parts += [f"### S{i}. Шаг номер {i} (1 дн)", "",
                  f"- Файл: `app/s{i}.py`",
                  f"- Тесты/гейты: `python -m pytest tests/test_s{i}.py -q` → 1 passed",
                  f"- Готово = шаг S{i} сделан", ""]
    parts += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|"]
    for i in range(1, count + 1):
        parts.append(f"| — | S{i} | — | — | {statuses.get(f'S{i}', 'открыт')} |")
    return "\n".join(parts) + "\n"


def write_plan(root: Path, text: str) -> Path:
    """Переписать план проекта и переутвердить его (запись хранит sha файла плана)."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(text, encoding="utf-8")
    write_approval(root, path)
    return path


def write_prompt(root: Path, step: str, recon: str | None = None) -> Path:
    """Постановка шага; `recon` — отметка `recon_commit:` в шапке."""
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    head = f"recon_commit: {recon}\n" if recon else ""
    path = folder / f"2026-11-04_1-{step}_prompt.md"
    path.write_text(f"# Постановка {step}\n\n{head}\n## Ревью\n\nроль `reviewer`.\n",
                    encoding="utf-8")
    return path


def keys_of(db: Path) -> set[str]:
    """Ключи идемпотентности всех карточек поддельной борды."""
    return {c["key"] for c in cards(db)}


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def step_to_review(p, db: Path, root: Path) -> None:
    """Довести шаг S1 до состояния «ревью закрыто, отчёт APPROVE лежит»."""
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_step"
    complete(db, ":step")
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_review"
    (root / "docs" / "reviews" / "1-S1.md").write_text(APPROVE, encoding="utf-8")
    complete(db, ":review:1")


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1"), hermes_stub


def test_prefetch_orders_prompt_for_next_step(stand):
    """Пока шаг S1 в работе, страж сам заказывает постановку S2 (правило 2)."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:S1:step" in keys_of(db)

    messages: list[str] = []
    second = gate.run_gate(p, execute=True, notify_fn=messages.append)
    assert second["decision"]["action"] == "create_prompt", second["decision"]
    assert second["decision"]["step"] == "S2"
    assert second["applied"] is True
    order = next(c for c in cards(db) if c["key"] == "planloop:1:S2:prompt")
    assert order["assignee"] == "prompter"
    assert order["max_runtime"] == "45m"
    assert "planloop:1:S2:step" not in keys_of(db)      # шаг N+1 не начат
    assert messages == []                               # предзаказ владельца не будит

    body = gate.prompt_order_body(p, {"id": "S2", "title": "Шаг номер 2", "goal": "g",
                                      "files": [], "gates": [], "dod": "d"})
    assert "recon_commit" in body                       # отметку требует любой заказ


def test_prefetch_is_idempotent(stand):
    """Повторный тик видит карточку заказа и ждёт: второй постановки нет."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")
    gate.run_gate(p, execute=True, notify_fn=quiet)
    gate.run_gate(p, execute=True, notify_fn=quiet)

    third = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert third["decision"]["action"] == "idle", third["decision"]
    assert third["applied"] is False
    assert "заказана заранее" in third["decision"]["reason"]
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S2:prompt") == 1


def test_prefetch_skips_last_step(stand):
    """S1 закрыт, в работе S2, его N+1 — последний шаг плана: заказа нет (D2)."""
    root, p, db = stand
    write_plan(root, plan_body(3, {"S1": "ЗАКРЫТ"}))
    write_prompt(root, "S2")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:S2:step" in keys_of(db)

    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert "в работе" in second["decision"]["reason"]
    assert "planloop:1:S3:prompt" not in keys_of(db)


def test_prefetch_disabled_keeps_old_behaviour(stand):
    """`prefetch_prompt: false` — сценарий первого теста даёт ожидание, а не заказ."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")
    raw = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    raw["prefetch_prompt"] = False
    (root / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    off = gate.resolve_paths(config_mod.load(root), "1")

    first = gate.run_gate(off, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    second = gate.run_gate(off, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert "S2" not in second["decision"]["reason"]
    assert "planloop:1:S2:prompt" not in keys_of(db)


def test_close_blocked_without_next_prompt(stand):
    """APPROVE есть, постановки S2 нет: журнал не пишется, владельцу уходит фраза D9."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")                      # постановки S2 нет
    step_to_review(p, db, root)

    phrase = "шаг S1 принят, но закрыть не могу: нет постановки S2"
    messages: list[str] = []
    res = gate.run_gate(p, execute=True, notify_fn=messages.append)
    decision = res["decision"]
    # Гейт живёт в решении (D10): страж заказывает постановку N+1, а не «висит» отказом.
    assert decision["action"] == "create_prompt", decision
    assert decision["step"] == "S2"
    assert decision.get("after_close") is True
    assert res["applied"] is True
    assert phrase in res["detail"]
    assert gate.journal_closed(p, "S1") is False        # шаг остался открытым
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S2:prompt") == 1
    assert len(messages) == 1 and phrase in messages[0], messages

    again = gate.run_gate(p, execute=True, notify_fn=messages.append)
    assert again["decision"]["action"] == "idle", again["decision"]
    assert phrase in again["decision"]["reason"]
    assert again["applied"] is False
    assert len(messages) == 1                            # второго сообщения нет
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S2:prompt") == 1


def test_close_proceeds_when_next_prompt_exists(stand):
    """Постановка S2 на месте — шаг закрывается, коммитятся план и отчёт ревью."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")
    write_prompt(root, "S2")
    step_to_review(p, db, root)

    calls: list[list[str]] = []

    def commit(p_, files, message):
        calls.append(list(files))
        return True, "abc1234 (тест)"

    res = gate.run_gate(p, execute=True, notify_fn=quiet, commit_fn=commit)
    assert res["decision"]["action"] == "close_step", res["decision"]
    assert res["applied"] is True
    assert gate.journal_closed(p, "S1") is True
    assert len(calls) == 1
    assert set(calls[0]) == {"docs/plans/1-demo.md", "docs/reviews/1-S1.md"}
