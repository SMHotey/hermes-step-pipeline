# -*- coding: utf-8 -*-
"""Реестр находок плана (S1 плана 7): схема файла, коды ошибок, блок в карточке ревью."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ENGINE = TESTS.parent / "step_pipeline" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import config as config_mod  # noqa: E402 - движок импортирует соседей плоско
import gate  # noqa: E402


def finding(**kw) -> dict:
    """Находка без ID: ID присваивает `add_findings`."""
    base = {"step": "S1", "kind": "замысел", "text": "шаг без входа", "action": "добавить вход"}
    base.update(kw)
    return base


@pytest.fixture()
def paths_obj(project):
    """Пути плана 1 установленного проекта: относительный путь и каталог ревью."""
    return gate.resolve_paths(config_mod.load(project), "1")


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    """Путь реестра вне проекта: файла ещё нет — его создаёт первый `add_findings`."""
    return tmp_path / "docs" / "reviews" / "plan-7-findings.md"


def _card(paths_obj) -> str:
    """Тело карточки ревью плана 1 так, как его собирает страж."""
    return gate.plan_review_card_body(paths_obj, paths_obj.cfg.rel(Path(paths_obj.plan_file)),
                                      gate.plan_review_rel(paths_obj), "reviewer")


def test_add_creates_registry_and_card_shows_it(paths_obj):
    """Создание реестра: путь рядом с отчётом ревью, карточка печатает реестр и правило."""
    path = gate.plan_findings_path(paths_obj)
    assert path.name == "plan-1-findings.md"
    assert path.parent == paths_obj.review_dir
    assert gate.plan_findings_rel(paths_obj) == "docs/reviews/plan-1-findings.md"
    assert not path.exists()

    empty = _card(paths_obj)
    assert "РЕЕСТР НАХОДОК (обязательно)" in empty
    assert "реестра ещё нет" in empty

    gate.add_findings(path, [finding()])
    assert path.is_file()
    assert "| ID | Шаг | Класс | Находка | Что сделать | Статус |" in path.read_text(encoding="utf-8")
    loaded = gate.load_findings(path)
    assert len(loaded) == 1
    assert loaded[0]["id"] == "F1"
    assert loaded[0]["status"] == "открыто"

    card = _card(paths_obj)
    assert "| F1 |" in card
    assert "закрыть фактом или подтвердить" in card
    assert "Блокирует вердикт только" in card


def test_add_appends_with_next_id(registry):
    """Добавление: ID растут по порядку, порядок файла совпадает с порядком добавления."""
    gate.add_findings(registry, [finding(text="первая"), finding(text="вторая"),
                                 finding(text="третья")])
    gate.add_findings(registry, [finding(text="четвёртая")])
    loaded = gate.load_findings(registry)
    assert [item["id"] for item in loaded] == ["F1", "F2", "F3", "F4"]
    assert [item["text"] for item in loaded] == ["первая", "вторая", "третья", "четвёртая"]
    text = registry.read_text(encoding="utf-8")
    positions = [text.index(item["text"]) for item in loaded]
    assert positions == sorted(positions)


def test_status_change_persists(registry):
    """Закрытие: `write_findings` сохраняет новый статус, текст и порядок целы."""
    gate.add_findings(registry, [finding(text="первая"), finding(text="вторая")])
    loaded = gate.load_findings(registry)
    loaded[0]["status"] = "закрыто"
    gate.write_findings(registry, loaded)

    again = gate.load_findings(registry)
    assert [item["status"] for item in again] == ["закрыто", "открыто"]
    assert [item["text"] for item in again] == ["первая", "вторая"]


def test_duplicate_id_rejected(registry):
    """Дубль ID: ошибка, прежний файл не затёрт половиной новой таблицы."""
    gate.add_findings(registry, [finding(text="первая"), finding(text="вторая")])
    before = registry.read_text(encoding="utf-8")
    broken = gate.load_findings(registry)
    broken.append(dict(broken[1]))                  # F2 второй раз

    with pytest.raises(gate.GateError) as exc:
        gate.write_findings(registry, broken)
    assert "F2" in str(exc.value)
    assert registry.read_text(encoding="utf-8") == before
    assert [item["id"] for item in gate.load_findings(registry)] == ["F1", "F2"]


def test_broken_file_raises(registry):
    """Битый файл — `GateError` с путём: без шапки, чужой статус, пять ячеек."""
    registry.parent.mkdir(parents=True, exist_ok=True)
    header = "| ID | Шаг | Класс | Находка | Что сделать | Статус |\n|---|---|---|---|---|---|\n"

    registry.write_text("# Реестр находок плана 7\n\nтаблицы нет\n", encoding="utf-8")
    with pytest.raises(gate.GateError) as no_header:
        gate.load_findings(registry)
    assert str(registry) in str(no_header.value)

    registry.write_text(header + "| F1 | S1 | замысел | шаг без входа | добавить | в работе |\n",
                        encoding="utf-8")
    with pytest.raises(gate.GateError) as bad_status:
        gate.load_findings(registry)
    assert str(registry) in str(bad_status.value)
    assert "в работе" in str(bad_status.value)

    registry.write_text(header + "| F1 | S1 | замысел | шаг без входа | открыто |\n",
                        encoding="utf-8")
    with pytest.raises(gate.GateError) as bad_cells:
        gate.load_findings(registry)
    assert str(registry) in str(bad_cells.value)


def test_missing_file_loads_empty(registry):
    """Отсутствие файла — норма: пусто, и файл после чтения не создаётся."""
    assert gate.load_findings(registry) == []
    assert not registry.exists()


def test_unicode_roundtrip(registry):
    """Юникод: ё, ёлочки, тире, стрелка и эмодзи возвращаются без искажений."""
    text = "ёлка «в кавычках» — шаг → ревью 🚀"
    gate.add_findings(registry, [finding(text=text, action="добавить «вход» — и всё")])

    loaded = gate.load_findings(registry)
    assert loaded[0]["text"] == text
    assert loaded[0]["action"] == "добавить «вход» — и всё"
    assert text in registry.read_bytes().decode("utf-8")


def test_order_preserved(registry):
    """Порядок: `write_findings` не сортирует — F3, F1, F2 читаются в том же порядке."""
    rows = [
        {"id": "F3", "step": "S1", "kind": "стиль", "text": "третья",
         "action": "поправить", "status": "открыто"},
        {"id": "F1", "step": "S1", "kind": "замысел", "text": "первая",
         "action": "поправить", "status": "закрыто"},
        {"id": "F2", "step": "S2", "kind": "двусмысленность", "text": "вторая",
         "action": "поправить", "status": "не блокирует"},
    ]
    gate.write_findings(registry, rows)
    assert [item["id"] for item in gate.load_findings(registry)] == ["F3", "F1", "F2"]
