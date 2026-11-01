# -*- coding: utf-8 -*-
"""Гейт формы промта-постановки для агента-исполнителя.

Проверяет, что постановка шага плана действительно готова к выдаче: все обязательные
блоки на месте, они не пустые, в блоке гейтов есть команда и ожидаемый результат,
заглушек вида `TBD` и `<...>` не осталось, а блок «Ревью» называет роль и вердикт.

Запуск:
    python .pipeline/check_task_prompt.py docs/prompts/<файл>.md
    python .pipeline/check_task_prompt.py <файл> --quiet
    python .pipeline/check_task_prompt.py <файл> --role reviewer   # своё имя роли

Выход: 0 — промт годен; 1 — есть нарушения; 2 — файл не найден.

Проверки:
    C1  все 9 обязательных блоков присутствуют (включая «Ревью»)
    C2  ни один блок не пустой
    C3  нет заглушек (TBD/TODO/FIXME/???/заполнить/<...>)
    C4  блок «Как проверить» содержит хотя бы одну команду
    C5  в блоке «Как проверить» есть ожидаемый результат (passed/EXIT/ожидается)
    C6  объём промта не меньше 800 символов
    C7  блок «Ревью» называет роль ревьюера и вердикт APPROVE/REQUEST_CHANGES
    W1  в блоке «Вопросы» не больше 3 вопросительных знаков (предупреждение)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = (
    ("role", "Роль и цель"),
    ("context", "Контекст"),
    ("bounds", "Границы"),
    ("tasks", "Что сделать"),
    ("gates", "Как проверить"),
    ("stops", "Стоп-сигналы"),
    ("report", "Формат отчёта"),
    ("questions", "Вопросы"),
    ("review", "Ревью"),
)

MIN_PROMPT_CHARS = 800
MAX_QUESTIONS = 3
PLACEHOLDER_TOKENS = ("TBD", "TODO", "FIXME", "???", "заполнить")
PLACEHOLDER_ANGLE = re.compile(r"<[^<>\n]{0,40}>")
VERDICT_HINT = re.compile(r"APPROVE|REQUEST_CHANGES", re.IGNORECASE)
COMMAND_HINT = re.compile(
    r"(?:python|pytest|grep|rg\b|git\s|npm|pnpm|yarn|cargo|go\s+test|make\b|dotnet|"
    r"scripts/|\.venv|alembic|docker|bash|powershell)",
    re.IGNORECASE,
)
EXPECTED_HINT = re.compile(r"(passed|failed|EXIT|ожида|вернёт|вернет|PASS|FAIL)")


def split_sections(text: str) -> dict[str, str]:
    """Разложить markdown на блоки `## <заголовок> -> тело`."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def find_section(sections: dict[str, str], marker: str) -> str | None:
    lowered = marker.lower()
    for title, body in sections.items():
        if lowered in title.lower():
            return body
    return None


def find_placeholders(text: str) -> list[str]:
    found: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for token in PLACEHOLDER_TOKENS:
            if token in line:
                found.append(f"{lineno}: {token}")
        for match in PLACEHOLDER_ANGLE.finditer(line):
            inner = match.group(0)[1:-1]
            # `<...>` как плейсхолдер отличается от кода с операторами внутри.
            if any(ch in inner for ch in "=([\"]|"):
                continue
            found.append(f"{lineno}: {match.group(0)}")
    return found


def check_prompt(text: str, reviewer_role: str = "reviewer",
                 reviewer_skill: str = "") -> list[tuple[str, str, str]]:
    """Прогнать все проверки. Возвращает список (уровень, код, сообщение)."""
    results: list[tuple[str, str, str]] = []
    sections = split_sections(text)

    missing = [marker for _, marker in REQUIRED_SECTIONS if find_section(sections, marker) is None]
    if missing:
        results.append(("FAIL", "C1", "нет обязательных блоков: " + ", ".join(missing)))
    else:
        results.append(("PASS", "C1", f"все {len(REQUIRED_SECTIONS)} обязательных блока на месте"))

    empty = [marker for _, marker in REQUIRED_SECTIONS
             if find_section(sections, marker) is not None and not find_section(sections, marker)]
    if empty:
        results.append(("FAIL", "C2", "пустые блоки: " + ", ".join(empty)))
    else:
        results.append(("PASS", "C2", "пустых блоков нет"))

    placeholders = find_placeholders(text)
    if placeholders:
        results.append(("FAIL", "C3", "остались заглушки: " + ", ".join(placeholders[:8])))
    else:
        results.append(("PASS", "C3", "заглушек нет"))

    gates = find_section(sections, "Как проверить") or ""
    if COMMAND_HINT.search(gates):
        results.append(("PASS", "C4", "в блоке гейтов есть команда"))
    else:
        results.append(("FAIL", "C4", "в блоке «Как проверить» нет ни одной команды"))

    if EXPECTED_HINT.search(gates):
        results.append(("PASS", "C5", "в блоке гейтов есть ожидаемый результат"))
    else:
        results.append(("FAIL", "C5", "в блоке «Как проверить» нет ожидаемого результата"))

    size = len(text)
    if size >= MIN_PROMPT_CHARS:
        results.append(("PASS", "C6", f"объём {size} символов"))
    else:
        results.append(("FAIL", "C6", f"объём {size} символов < {MIN_PROMPT_CHARS}: это набросок"))

    review = find_section(sections, "Ревью") or ""
    role_ok = re.search(re.escape(reviewer_role), review, re.IGNORECASE) is not None
    skill_ok = bool(reviewer_skill) and reviewer_skill.lower() in review.lower()
    if (role_ok or skill_ok) and VERDICT_HINT.search(review):
        results.append(("PASS", "C7", "блок «Ревью» называет роль и вердикт"))
    else:
        results.append((
            "FAIL", "C7",
            f"в блоке «Ревью» нет роли `{reviewer_role}` и вердикта APPROVE/REQUEST_CHANGES"))

    questions = find_section(sections, "Вопросы") or ""
    marks = questions.count("?")
    if marks > MAX_QUESTIONS:
        results.append(("WARN", "W1", f"в блоке «Вопросы» {marks} вопросительных знаков "
                                      f"(> {MAX_QUESTIONS})"))
    else:
        results.append(("PASS", "W1", f"вопросов в пределах нормы ({marks})"))

    return results


def _setup_stdio() -> None:
    """UTF-8 в stdout/stderr: на Windows-консолях cp1251 русский текст падает."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    parser = argparse.ArgumentParser(description="Гейт формы промта-постановки для агента")
    parser.add_argument("path", help="путь к файлу промта (docs/prompts/<файл>.md)")
    parser.add_argument("--quiet", action="store_true", help="печатать только нарушения и итог")
    parser.add_argument("--role", default="reviewer",
                        help="имя профиля ревьюера (по умолчанию `reviewer`)")
    parser.add_argument("--skill", default="", help="имя скилла ревьюера (необязательно)")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"ОШИБКА: файл промта не найден: {path}")
        return 2

    text = path.read_text(encoding="utf-8")
    results = check_prompt(text, args.role, args.skill)

    for level, code, message in results:
        if args.quiet and level == "PASS":
            continue
        print(f"{level} {code} {message}")

    failures = [item for item in results if item[0] == "FAIL"]
    if failures:
        print(f"ИТОГ: FAIL — нарушений {len(failures)}")
        return 1
    print(f"ИТОГ: PASS — проверок {len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
