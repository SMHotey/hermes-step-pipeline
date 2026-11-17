# -*- coding: utf-8 -*-
"""Реестр находок плана в панели: строки разбираются, длинные тексты режутся, счёт сходится.

Замечание владельца 0.34.0: вкладка «Шаг» показывала «5 находок» и ни одной находки. Тесты
держат три свойства: строки таблицы читаются (ID, шаг, класс, текст, что сделать, статус),
открытые идут первыми и считаются отдельно от неблокирующих, а нечитаемый реестр даёт
подпись вместо выдуманного «находок нет».
"""
from __future__ import annotations

from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
import findings_view

REGISTRY = """plan_sha256: abc

# Реестр находок плана 1

| ID | Шаг | Класс | Находка | Что сделать | Статус |
|---|---|---|---|---|---|
| F1 | S1 | замысел | В шаге нет стоп-сигнала | Добавить `Риск: high` и стоп | открыто |
| F2 | S2 | двусмысленность | Файл-источник не назван | Назвать `bot/config.py` | открыто |
| F3 | S3 | стиль | Лишняя строка в «Готово» | Убрать | не блокирует |
| F4 | S4 | замысел | Закрыто и не важно | — | закрыто |
"""


def test_rows_are_parsed_with_every_column(tmp_path: Path):
    """Строка реестра отдаётся целиком: без текста и «что сделать» чинить нечего."""
    path = tmp_path / "plan-1-findings.md"
    path.write_text(REGISTRY, encoding="utf-8")
    block = findings_view.load(path)
    assert block["exists"] is True
    assert [row["id"] for row in block["rows"]] == ["F1", "F2", "F3", "F4"]
    first = block["rows"][0]
    assert first["step"] == "S1"
    assert first["cls"] == "замысел"
    assert first["text"] == "В шаге нет стоп-сигнала"
    assert first["action"] == "Добавить `Риск: high` и стоп"
    assert first["status"] == "открыто"
    assert first["open"] is True


def test_open_rows_lead_and_counts_split(tmp_path: Path):
    """Открытые — первыми, счёт открытых отделён от прочих: вердикт держат только они."""
    path = tmp_path / "plan-1-findings.md"
    path.write_text(REGISTRY, encoding="utf-8")
    block = findings_view.load(path)
    assert block["open"] == 2
    assert block["blocking"] == 2
    assert block["closed"] == 2
    assert block["total"] == 4
    assert [row["id"] for row in block["rows"]][:2] == ["F1", "F2"]
    assert block["rows"][2]["open"] is False


def test_missing_registry_is_a_note_not_an_empty_list(tmp_path: Path):
    """Файла нет — подпись, а не пустой список: пустой блок читался бы как «находок нет»."""
    block = findings_view.load(tmp_path / "plan-9-findings.md")
    assert block["exists"] is False
    assert block["rows"] == []
    assert "реестра находок плана нет" in block["note"]


def test_foreign_table_is_reported_as_unparsed(tmp_path: Path):
    """Таблица без шапки реестра — «не разобрано», а не молчаливые нули."""
    path = tmp_path / "plan-1-findings.md"
    path.write_text("# что-то своё\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    block = findings_view.load(path)
    assert block["exists"] is True
    assert block["open"] == 0
    assert "не разобраны" in block["note"]


def test_long_texts_are_cut_with_marker(tmp_path: Path):
    """Длинная находка режется с многоточием: обрезка видна, а не выдаётся за весь текст."""
    long_text = "слово " * 400
    path = tmp_path / "plan-1-findings.md"
    path.write_text(
        "| ID | Шаг | Класс | Находка | Что сделать | Статус |\n|---|---|---|---|---|---|\n"
        f"| F1 | S1 | замысел | {long_text} | — | открыто |\n", encoding="utf-8")
    row = findings_view.load(path)["rows"][0]
    assert len(row["text"]) <= findings_view.TEXT_CAP
    assert row["text"].endswith("…")


@pytest.mark.parametrize("cap", [1, 40])
def test_cap_keeps_the_rest_counted(tmp_path: Path, cap: int):
    """Предел строк не прячет остаток: подпись говорит, сколько находок не показано."""
    path = tmp_path / "plan-1-findings.md"
    path.write_text(REGISTRY, encoding="utf-8")
    block = findings_view.load(path, cap=cap)
    assert block["shown"] == min(cap, block["total"])
    assert block["total"] == 4
    if cap < 4:
        assert "из 4" in block["note"]


def test_snapshot_reads_the_registry_from_the_review_directory(tmp_path: Path):
    """Снимок ищет реестр там же, где отчёт ревью: иначе панель показывает пустоту."""
    path = tmp_path / "docs" / "reviews" / "plan-1-findings.md"
    path.parent.mkdir(parents=True)
    path.write_text(REGISTRY, encoding="utf-8")
    import snapshot

    block = snapshot._plan_findings(tmp_path, {"paths": {"reviews": "docs/reviews"}}, "1")
    assert block["exists"] is True
    assert block["open"] == 2
    assert block["rows"][0]["id"] == "F1"
