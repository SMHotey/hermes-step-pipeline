# -*- coding: utf-8 -*-
"""Зачем: §S7 плана 5 — шаг сам выбирает исполнителя по предметке.

Проверяется ключ `routing` из `pipeline.json`: предметка (`backend`, `frontend`, ...)
из хвоста заголовка или пункта «Исполнитель:» уходит профилю маршрута, явное имя
профиля сильнее маршрута, шаг без владельца остаётся у драйвера, а неизвестный токен
даёт инцидентный стоп и НЕ создаёт карточку — раньше она молча уезжала драйверу.
Отдельно сверяется README ролей: число ролей то же, и по каждой роли сказано, что она
решает, какой артефакт закрывает и чего она не делает.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import config as config_mod
import gate
from engine_kit import ENGINE, cards, make_project, write_approval

STEP_CARD = ENGINE / "step_card.py"
ROLES = ENGINE.parent / "roles"

# План: S2 с предметкой `backend` в хвосте заголовка, S1 подменяется в тестах.
PLAN_TEMPLATE = """# 1. Демо-план

## Шаги

{step1}

### S2. Эндпоинт списка (2 дн) `backend`

- Файл: `app/api/invoices.py`
- Тесты/гейты: `python -m pytest tests/test_api_invoice.py -q` → 5 passed
- Готово = `GET /invoices` отдаёт 200 и список

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""

S1_DEFAULT = """### S1. Таблица счетов (1 дн)

- Файл: `app/models/invoice.py`
- Тесты/гейты: `python -m pytest tests/test_invoice.py -q` → 3 passed
- Готово = таблица `invoice` создана, 3 теста зелёные"""


def quiet(text: str) -> None:
    """Уведомления в тестах не нужны: тик проверяется по карточкам и стоп-флагу."""


def load_step_card():
    """Импортировать `step_pipeline/engine/step_card.py` — файл движка, не копию."""
    spec = importlib.util.spec_from_file_location("step_card_s7", str(STEP_CARD))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def step_cards(db: Path, key: str) -> list[dict]:
    """Карточки шага по ключу идемпотентности; пустая база — это тоже «карточек нет»."""
    try:
        rows = cards(db)
    except sqlite3.OperationalError:      # поддельный hermes ни одной карточки не создал
        return []
    return [row for row in rows if row["key"] == key]


def init_board(p) -> None:
    """Схема борды: поддельный `hermes` создаёт таблицы только со своей первой карточкой.

    Карточка-заглушка (ключ `init:board`) на проверки не влияет: тест смотрит карточки
    шага, а не устройство борды.
    """
    gate._create_card(p, "[инициализация борды]", "", "demo", "init:board", ())


def rewrite_plan(root: Path, plan_text: str) -> Path:
    """Переписать план проекта и переутвердить: запись утверждения хранит sha файла."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(plan_text, encoding="utf-8")
    write_approval(root, path)
    return path


def test_subject_routes_to_mapped_profile(tmp_path, hermes_stub, monkeypatch):
    """Предметка `backend` в хвосте заголовка → профиль из `routing`, а не драйвер."""
    root = make_project(tmp_path / "route", routing={"backend": "reviewer",
                                                    "frontend": "demo", "devops": "demo",
                                                    "designer": "demo", "security": "demo"})
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    module = load_step_card()

    code = module.main(["--step", "S2"])

    assert code == 0
    rows = cards(hermes_stub)
    assert [row["key"] for row in rows] == ["planloop:1:S2:step"]
    assert rows[0]["assignee"] == "reviewer"


def test_subject_without_route_goes_to_driver(tmp_path, hermes_stub, monkeypatch):
    """Маршрута нет — предметка ведёт к драйверу, как было до шага (поведение не ломаем)."""
    root = make_project(tmp_path / "plain", routing={})
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    module = load_step_card()

    assert module.main(["--step", "S2"]) == 0
    rows = cards(hermes_stub)
    assert [row["key"] for row in rows] == ["planloop:1:S2:step"]
    assert rows[0]["assignee"] == "demo"


def test_owner_profile_name_is_used_as_is(tmp_path, hermes_stub, monkeypatch):
    """Явное имя профиля сильнее маршрута: владелец назвал роль буквально."""
    root = make_project(tmp_path / "point", routing={"backend": "demo"})
    # Токен пункта читается из обратных кавычек (`next_step._OWNER_RE`): запись
    # «Исполнитель: reviewer» без них разбор не видит — S7 это поведение не меняет.
    rewrite_plan(root, PLAN_TEMPLATE.format(
        step1=S1_DEFAULT + "\n- Исполнитель: `reviewer`"))
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    module = load_step_card()

    assert module.main(["--step", "S1"]) == 0
    rows = cards(hermes_stub)
    assert [row["key"] for row in rows] == ["planloop:1:S1:step"]
    assert rows[0]["assignee"] == "reviewer"


def test_step_without_owner_goes_to_driver(tmp_path, hermes_stub, monkeypatch):
    """Ни предметки, ни профиля — карточка драйверу (прежнее поведение)."""
    root = make_project(tmp_path / "no-owner")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    module = load_step_card()

    assert module.main(["--step", "S1"]) == 0
    rows = cards(hermes_stub)
    assert [row["key"] for row in rows] == ["planloop:1:S1:step"]
    assert rows[0]["assignee"] == "demo"


def test_unknown_owner_stops_and_no_card(tmp_path, hermes_stub, monkeypatch):
    """Опечатка в профиле (`bckend`) — инцидентный стоп, карточки нет и во втором тике."""
    root = make_project(tmp_path / "typo", routing={"backend": "reviewer"})
    rewrite_plan(root, PLAN_TEMPLATE.format(
        step1="### S1. Таблица счетов (1 дн) `bckend`\n\n- Готово = таблица создана"))
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# постановка шага S1\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    cfg = config_mod.load(root)
    p = gate.resolve_paths(cfg, "1")
    init_board(p)

    first = gate.run_gate(p, notify_fn=quiet)

    assert first["decision"]["action"] == "stop"
    assert first["applied"] is True
    stop = gate.load_stop(p)
    assert stop and stop["kind"] == "incident"
    assert "bckend" in stop["reason"]
    # Сообщение называет, что писать можно: предметки и известные профили.
    assert "frontend" in stop["reason"] and "routing" in stop["reason"]
    assert "reviewer" in stop["reason"]
    assert step_cards(hermes_stub, "planloop:1:S1:step") == []

    again = gate.run_gate(p, notify_fn=quiet)

    # Второй тик причину не переписывает и второй раз не уведомляет: стоп уже стоит.
    assert again["decision"]["action"] == "idle"
    assert again["decision"].get("incident") is True
    assert again["applied"] is False
    assert step_cards(hermes_stub, "planloop:1:S1:step") == []
    assert gate.load_stop(p)["reason"] == stop["reason"]


def test_owner_outside_template_stops_but_file_name_does_not(tmp_path, hermes_stub, monkeypatch):
    """Токен вне шаблона — стоп, а имя файла в кавычках — нет (спека §4, S7).

    Разбор заголовка берёт первый токен в обратных кавычках, поэтому граница важна:
    заглавная опечатка профиля (`Backend`) останавливает конвейер, а `pipeline.json`
    в том же месте — имя файла, шаг остаётся у драйвера.
    """
    root = make_project(tmp_path / "outside", routing={"backend": "reviewer"})
    rewrite_plan(root, PLAN_TEMPLATE.format(
        step1="### S1. Таблица счетов (1 дн) `Backend`\n\n- Готово = таблица создана"))
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# постановка шага S1\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")
    init_board(p)

    first = gate.run_gate(p, notify_fn=quiet)

    assert first["decision"]["action"] == "stop"
    stop = gate.load_stop(p)
    assert stop and stop["kind"] == "incident"
    assert "Backend" in stop["reason"]
    assert step_cards(hermes_stub, "planloop:1:S1:step") == []

    plain = make_project(tmp_path / "artifact", routing={"backend": "reviewer"})
    rewrite_plan(plain, PLAN_TEMPLATE.format(
        step1="### S1. Таблица счетов (1 дн) `pipeline.json`\n\n- Готово = таблица создана"))
    (plain / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# постановка шага S1\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(plain))
    p2 = gate.resolve_paths(config_mod.load(plain), "1")

    second = gate.run_gate(p2, notify_fn=quiet)

    assert second["decision"]["action"] == "create_step"
    rows = step_cards(hermes_stub, "planloop:1:S1:step")
    assert [row["assignee"] for row in rows] == ["demo"]


def test_roles_readme_lists_every_role():
    """README ролей: семь ролей, у каждой — что решает, артефакт и чего не делает."""
    readme = ROLES / "README.md"
    assert readme.is_file(), "нет README ролей — не сказано, что роль решает и чего нет"
    text = readme.read_text(encoding="utf-8")
    roles = sorted(path.name for path in ROLES.iterdir() if path.is_dir())

    assert len(roles) == 8                       # семь ролей + утилитарный скилл
    assert len([name for name in roles if name.startswith("pipeline-")]) == 7
    for name in roles:
        assert name in text, f"роль {name} не описана в README"
    header = next(line for line in text.splitlines() if line.startswith("|"))
    assert "решает" in header.lower()
    assert "артефакт" in header.lower()
    assert "не делает" in header.lower()
