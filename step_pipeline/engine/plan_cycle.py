# -*- coding: utf-8 -*-
"""Корректировка плана по разведке: якорь раздела, вердикт, шаг интеграции, отметка, дифф.

Разведка не «прикладывается» к плану, а меняет его, и это должно быть проверяемо:
план ссылается на раздел-якорь `## Разведка`, вердикт «берём» подтверждён шагом
интеграции зависимости, правка оформлена отметкой `Правки по разведке: N`, а дифф
«до/после» считается вызывающим кодом через `edit_diff`.

Модуль самодостаточен: чистые функции над текстом плана, только stdlib
(`re`, `difflib`), импортов других модулей движка нет — файл копируется в проект
списком `ENGINE_FILES` и должен работать без плагина.

Вход для ревью плана (S5):
    `can_review(text)` — функция, которую ревью плана вызывает перед переходом
    к ревью: при `(False, причина)` ревью не назначается, причина — сообщение
    первой блокирующей находки, её и читает ревьюер.
    Правило D6: первая FAIL обязана называть причину сама — иначе ревьюер читал
    бы причину из сообщения, а не из факта, на чём список остановился первым.

Проверки:
    P1  в плане есть раздел-якорь `## Разведка` (заголовок уровня 2-6 с маркером
        «разведка», не заголовок шага и не внутри раздела шагов)
    P2  при вердикте «берём» в разделе разведки в плане есть шаг интеграции
    P3  правка оформлена отметкой `Правки по разведке: N`
"""
from __future__ import annotations

import difflib
import re

#: Маркер раздела-якоря в заголовке (без учёта регистра).
RESEARCH_MARKER = "разведка"
#: Ячейка строки таблицы раздела — вердикт «берём готовое» (без учёта регистра).
TAKE_VERDICT = "берём"
#: Подстрока заголовка шага интеграции зависимости (без учёта регистра).
INTEGRATION_MARK = "интеграц"
#: Отметка оформленной правки плана: `Правки по разведке: 2`, `правок по разведке: 12`.
#: Позиция в формате не закреплена (D5), поэтому ищется по всему тексту плана;
#: `0` тоже отметка — важен сам факт оформления, а не размер правки.
EDIT_MARK_RE = re.compile(r"прав\w*\s+по\s+разведке\s*:\s*(\d+)", re.IGNORECASE)
#: Идентификатор шага по образцу `plan_lint._STEP_ID_REGEX` (`S1`, `S2A`, `S10`):
#: `### S1. Старт`, `### S2A. Обзор`, `### S10. Уточнения`.
STEP_HEADING_RE = re.compile(r"^#{3,}\s+[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?\b")

#: Уровень заголовка-маркера: `#`…`######` плюс пробел, тело — до конца строки.
_MAX_HASHES = 6


def _headings(text: str) -> list[tuple[int, int, str]]:
    """Заголовки текста: `(уровень, номер строки, текст без решёток)`.

    Номер строки — с единицы. Разбор общий для `research_section` (ищет
    раздел-якорь и помнит последний заголовок уровня 2) и `integration_step`
    (ищет шаг по заголовку); публичного API у помощника нет.
    """
    found: list[tuple[int, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        cut = line.find(" ")
        if cut < 1:
            continue  # не «решётки и пробел»: `####без-пробела` — не заголовок
        head = line[:cut]
        if head.strip("#") or len(head) > _MAX_HASHES:
            continue  # внутри только решётки, от одной до шести
        found.append((len(head), lineno, line[cut:].strip()))
    return found


def _step_line(level: int, title: str) -> str:
    """Восстановить форму строки заголовка — `STEP_HEADING_RE` матчит её с `#`."""
    return "#" * level + " " + title


def research_section(text: str) -> str | None:
    """Тело раздела разведки по якорю, иначе `None` (D2).

    Подходит заголовок уровня 2-6 с маркером «разведка» без регистра, который не
    является заголовком шага и не лежит внутри раздела шагов. Заголовок шага
    разделом не считается: `### S3. Разведка рынка` — шаг, и находка P1 про него
    была бы ложью (та же причина, что в `plan_lint._research_section`).
    """
    lines = text.splitlines()
    found = _headings(text)
    last_h2 = ""  # последний заголовок уровня 2 — граница раздела шагов
    for idx, (level, lineno, title) in enumerate(found):
        candidate = (
            2 <= level <= _MAX_HASHES
            and RESEARCH_MARKER in title.lower()
            # заголовок шага — не раздел плана (D2)
            and not STEP_HEADING_RE.match(_step_line(level, title))
            # уровень выше 2 — только вне раздела шагов: `### Разведка в проекте`
            # внутри `## Шаги` — это шаг, а не якорь плана (D2)
            and (level == 2 or "шаг" not in last_h2.lower())
        )
        if candidate:
            # тело — до следующего заголовка того же или более высокого уровня
            end = len(lines)
            for next_level, next_lineno, _ in found[idx + 1:]:
                if next_level <= level:
                    end = next_lineno - 1
                    break
            return "\n".join(lines[lineno:end]).strip()
        if level == 2:
            last_h2 = title
    return None


def _cells(line: str) -> list[str]:
    """Ячейки строки таблицы: снять крайние `|`, обрезать каждую ячейку.

    Свой разбор вместо импорта `scout_report.cells` (D1): правка формата таблицы
    артефакта разведки не должна молча менять поведение план-коррекции.
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [part.strip() for part in body.split("|")]


def take_candidates(text: str) -> tuple[str, ...]:
    """Имена кандидатов, у которых в строке таблицы раздела разведки есть «берём» (D3).

    Вердикт живёт в ячейке колонки «Вердикт» таблицы, которую в план выводит
    `scout_report.summary_for_plan`. Прозаическая строка «Практики берём у
    spec-kit» вердиктом не считается — иначе план немедленно получил бы P2 и
    требование шага интеграции, которого формат §5a не имел в виду.
    Раздела в плане нет — пустой кортеж.
    """
    body = research_section(text)
    if body is None:
        return ()
    names: list[str] = []
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue  # проза раздела — не таблица, ячейки там нет
        cells = _cells(line)
        if any(cell.lower() == TAKE_VERDICT for cell in cells):
            names.append(cells[0])
    return tuple(names)


def integration_step(text: str) -> str | None:
    """Заголовок первого шага интеграции (без решёток), иначе `None` (D4).

    Тело шага не разбирается: критерий обязан быть виден ревьюеру одним взглядом
    на список шагов (`### S2. Интеграция demo/spec-kit`), а не парсингом целей.
    """
    for level, _lineno, title in _headings(text):
        if STEP_HEADING_RE.match(_step_line(level, title)) and INTEGRATION_MARK in title.lower():
            return title
    return None


def edits_count(text: str) -> int | None:
    """Число из отметки `Правки по разведке: N`, иначе `None` (D5)."""
    match = EDIT_MARK_RE.search(text)
    return int(match.group(1)) if match else None


def check_plan(text: str) -> list[tuple[str, str, str]]:
    """Прогнать проверки. Возвращает список `(уровень, код, сообщение)` в порядке P1-P3.

    По одной записи на код, уровни `PASS`/`FAIL` — та же структура, что у
    `scout_report.check_text`, ревьюер S5 читает обе одинаково (D6).
    """
    results: list[tuple[str, str, str]] = []

    if research_section(text) is not None:
        results.append(("PASS", "P1", "раздел «Разведка» на месте по якорю ## Разведка"))
    else:
        results.append(("FAIL", "P1",
                        "нет раздела «Разведка» — план без ссылки на разведку не идёт на ревью"))

    candidates = take_candidates(text)
    if candidates and integration_step(text) is None:
        results.append(("FAIL", "P2",
                        f"вердикт «{TAKE_VERDICT}» у кандидата {candidates[0]}, "
                        "шага интеграции в плане нет"))
    else:
        results.append(("PASS", "P2", f"кандидатов «{TAKE_VERDICT}» без шага интеграции нет"))

    count = edits_count(text)
    if count is None:
        # P3 блокирует (D7): без отметки правка не оформлена, «видно диффом»
        # из цели шага превратилось бы в необязательное.
        results.append(("FAIL", "P3",
                        "нет отметки «правок по разведке: N» — правка по разведке не оформлена"))
    else:
        results.append(("PASS", "P3", f"отметка правок по разведке: {count}"))

    return results


def can_review(text: str) -> tuple[bool, str]:
    """Дверь перед ревью плана (S5): `(True, "")`, пока нет ни одной FAIL.

    При блокирующей находке — `(False, сообщение первой FAIL)`: это сообщение
    увидит ревьюер, поэтому первая FAIL должна называть причину сама (D6).
    """
    for level, _code, message in check_plan(text):
        if level == "FAIL":
            return False, message
    return True, ""


def edit_diff(before: str, after: str) -> str:
    """Unified-дифф правки плана с метками «до/после», различий нет — пустая строка (D8).

    Дифф кому-то показывает вызывающий код (S5 или отчёт правки): модуль ничего
    не читает и не хранит сам.
    """
    diff = difflib.unified_diff(
        before.splitlines(True), after.splitlines(True),
        fromfile="план (до правки)", tofile="план (после правки)",
    )
    return "".join(diff)
