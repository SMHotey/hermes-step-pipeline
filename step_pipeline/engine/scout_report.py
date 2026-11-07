# -*- coding: utf-8 -*-
"""Гейт артефакта разведки: разделы, таблица кандидатов, вердикты и решение.

Разбирает `docs/research/<план>-prior-art.md` по шаблону
`docs/research/TEMPLATE.md`: раздел «Кандидаты» с таблицей восьми колонок,
раздел «Итог» со строкой «Решение:». Артефакт принимается, только когда шапка
таблицы совпадает с COLUMNS, обязательные поля заполнены, у каждого кандидата
есть вердикт из словаря и дата коммита в формате ГГГГ-ММ-ДД. Непринятый
артефакт в план не выводится: summary_for_plan на нём падает ValueError.

Запуск:
    python .pipeline/scout_report.py docs/research/5-prior-art.md
    python .pipeline/scout_report.py docs/research/5-prior-art.md --quiet

Выход: 0 — артефакт принят; 1 — есть нарушения; 2 — файл не найден.

Проверки:
    R1  раздел «Кандидаты» и таблица с шапкой из COLUMNS (порядок колонок обязателен)
    R2  обязательные поля кандидата не пусты (пустая ячейка и тире — пусто)
    R3  вердикт кандидата — «берём», «форкаем» или «своё»
    R4  дата коммита — ГГГГ-ММ-ДД
    R5  в разделе «Итог» есть строка «Решение:» с исходом go / needs-clarification / kill
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Контракт шаблона и разбора (D1): состав и порядок колонок обязаны совпадать
#: точно — шаблон, разбор и вывод в план становятся одним контрактом, а
#: расхождение ловится шапкой R1 и тестом шаблона.
COLUMNS = ("Кандидат", "Ссылка", "Звёзды", "Дата коммита", "Лицензия",
           "Что даёт", "Стоимость, дн", "Вердикт")
#: Словарь вердиктов §5a (D2): кандидат, который не берём, получает «своё» —
#: четвёртого значения формат не знает.
VERDICTS = ("берём", "форкаем", "своё")
DECISIONS = ("go", "needs-clarification", "kill")
#: Маркер пустоты — тот же, что в plan_lint._EMPTY_GATES (D3): тире в ячейке
#: означает «поле не заполнено», иначе таблица с прочерками прошла бы гейт.
EMPTY_CELLS = ("", "—", "–", "-", "−")
#: Дата последнего коммита — единственное проверяемое поле живости кандидата (D4).
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CANDIDATES_MARK = "кандидат"
SUMMARY_MARK = "итог"
DECISION_MARK = "Решение:"
#: Ведущие разделители пояснения после «Решение: <исход>» (не регулярка: это
#: набор символов для str.lstrip).
NOTE_LEADERS = "—–-.: \t"


@dataclass(frozen=True)
class Candidate:
    """Одна строка таблицы кандидатов, колонки — в порядке COLUMNS."""

    name: str        # «Кандидат»
    url: str         # «Ссылка»
    stars: str       # «Звёзды»
    last_commit: str  # «Дата коммита», ГГГГ-ММ-ДД
    license: str     # «Лицензия»
    gives: str       # «Что даёт»
    cost: str        # «Стоимость, дн»
    verdict: str     # «Вердикт»


@dataclass(frozen=True)
class Report:
    """Результат разбора: кандидаты, решение и записи всех пяти проверок."""

    candidates: tuple[Candidate, ...]
    decision: str                     # "go" / "needs-clarification" / "kill" / ""
    decision_note: str                # текст после исхода, может быть пустым
    problems: tuple[tuple[str, str, str], ...]  # (уровень, код, сообщение)

    @property
    def errors(self) -> tuple[str, ...]:
        """Сообщения находок (уровень FAIL) — они же текст для ValueError."""
        return tuple(message for level, _code, message in self.problems
                     if level == "FAIL")

    @property
    def accepted(self) -> bool:
        """Артефакт принят: ни одной находки."""
        return not self.errors


def _record(code: str, ok_message: str, violations: list[str]) -> list[tuple[str, str, str]]:
    """Собрать записи одной проверки: PASS, одна находка или сводная + находки."""
    if not violations:
        return [("PASS", code, ok_message)]
    if len(violations) == 1:
        return [("FAIL", code, violations[0])]
    # Несколько нарушений: сводная запись с числом и сразу за ней каждое.
    return ([("FAIL", code, f"нарушений: {len(violations)}")]
            + [("FAIL", code, item) for item in violations])


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
    """Тело первого раздела, заголовок которого содержит маркер (без регистра)."""
    lowered = marker.lower()
    for title, body in sections.items():
        if lowered in title.lower():
            return body
    return None


def cells(line: str) -> list[str]:
    """Ячейки строки таблицы: снять крайние `|`, обрезать каждую ячейку."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [part.strip() for part in body.split("|")]


def _is_separator(row: list[str]) -> bool:
    """Строка-разделитель: непустые ячейки, состоящие из `-` и `:`."""
    return bool(row) and all(row) and all(all(ch in "-:" for ch in cell) for cell in row)


def table_rows(body: str) -> tuple[list[str], list[list[str]]]:
    """Первая подряд идущая таблица раздела: (шапка, строки данных).

    Шапка — первая строка, начинающаяся с `|`; строки-разделители
    отбрасываются; узкие строки (не по числу колонок) возвращаются как есть —
    их отклонит R1.
    """
    header: list[str] = []
    rows: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header:
                break  # таблица кончилась
            continue
        row = cells(stripped)
        if not header:
            header = row
        elif not _is_separator(row):
            rows.append(row)
    return header, rows


def check_text(text: str) -> Report:
    """Прогнать проверки R1-R5. Записи идут в порядке R1, R2, R3, R4, R5."""
    sections = split_sections(text)
    body = find_section(sections, CANDIDATES_MARK)
    header: list[str] = []
    rows: list[list[str]] = []
    if body is not None:
        header, rows = table_rows(body)

    # R1 — раздел, шапка и ширина таблицы.
    r1: list[str] = []
    if not header:
        r1.append("нет раздела «Кандидаты» с таблицей кандидатов")
    else:
        if tuple(header) != COLUMNS:
            expected = "| " + " | ".join(COLUMNS) + " |"
            r1.append(f"шапка таблицы «{' | '.join(header)}» не равна ожидаемой: {expected}")
        for index, row in enumerate(rows, start=1):
            if len(row) != len(COLUMNS):
                r1.append(f"строка {index}: ячеек {len(row)}, ожидалось {len(COLUMNS)}")

    # Позиционный разбор (D1): кандидатом становится только строка из восьми
    # ячеек; узкая строка уже отклонена R1 и в R2-R4 не попадает.
    good = [row for row in rows if len(row) == len(COLUMNS)]
    candidates = tuple(Candidate(*row) for row in good)
    verdict_at = COLUMNS.index("Вердикт")
    date_at = COLUMNS.index("Дата коммита")

    # R2 — обязательные поля: пустая ячейка и тире одинаково «не заполнено».
    r2: list[str] = []
    for index, row in enumerate(good, start=1):
        for column, value in zip(COLUMNS, row):
            if value in EMPTY_CELLS:
                r2.append(f"строка {index}: пустое поле «{column}»")

    # R3 — вердикт из словаря; пустой вердикт даёт и R2, и R3 (две находки).
    r3: list[str] = [
        f"строка {index}: вердикт «{row[verdict_at]}» не из списка: " + ", ".join(VERDICTS)
        for index, row in enumerate(good, start=1)
        if row[verdict_at] not in VERDICTS
    ]

    # R4 — дата коммита строго ГГГГ-ММ-ДД.
    r4: list[str] = [
        f"строка {index}: дата коммита «{row[date_at]}» не в формате ГГГГ-ММ-ДД"
        for index, row in enumerate(good, start=1)
        if not DATE_RE.match(row[date_at])
    ]

    # R5 — строка «Решение:» в разделе «Итог» и исход из списка.
    r5: list[str] = []
    decision = ""
    note = ""
    summary = find_section(sections, SUMMARY_MARK) or ""
    line = next((ln.strip() for ln in summary.splitlines()
                 if ln.strip().startswith(DECISION_MARK)), "")
    if not line:
        r5.append("в разделе «Итог» нет строки «Решение:»")
    else:
        rest = line[len(DECISION_MARK):].strip()
        parts = rest.split()
        token = parts[0] if parts else ""
        decision = token
        # Пояснение — остаток строки после исхода без ведущих разделителей.
        note = rest[len(token):].lstrip(NOTE_LEADERS) if token else ""
        if token not in DECISIONS:
            r5.append(f"исход «{token}» не из списка: " + ", ".join(DECISIONS))

    problems: list[tuple[str, str, str]] = []
    # D5: при пустой таблице R2-R4 проверять нечего — они дают PASS с этой же
    # формулировкой, а не «вердикты в порядке» по нулю кандидатов.
    nothing = "проверять нечего"
    problems += _record("R1", "раздел «Кандидаты», таблица и шапка из COLUMNS на месте", r1)
    problems += _record("R2", "обязательные поля заполнены" if good else nothing, r2)
    problems += _record("R3", "вердикты из словаря формата" if good else nothing, r3)
    problems += _record("R4", "даты коммитов в формате ГГГГ-ММ-ДД" if good else nothing, r4)
    problems += _record("R5", "решение на месте", r5)
    return Report(candidates, decision, note, tuple(problems))


def check_file(path: Path | str) -> Report:
    """Прочитать артефакт как UTF-8 (битые байты — заменой) и прогнать проверки."""
    return check_text(Path(path).read_text(encoding="utf-8", errors="replace"))


def summary_for_plan(report: Report, artifact: str) -> str:
    """Блок `## Разведка` для плана: ссылка, таблица кандидатов, решение.

    На непринятом артефакте падает ValueError: план не должен получить
    разведку, которую гейт отклонил.
    """
    if not report.accepted:
        raise ValueError("артефакт разведки не принят: " + "; ".join(report.errors))
    lines = [
        "## Разведка",
        "",
        f"Артефакт: `{artifact}`",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---" for _ in COLUMNS) + "|",
    ]
    for candidate in report.candidates:
        values = (candidate.name, candidate.url, candidate.stars, candidate.last_commit,
                  candidate.license, candidate.gives, candidate.cost, candidate.verdict)
        lines.append("| " + " | ".join(values) + " |")
    lines.append("")
    decision = f"Решение: {report.decision}"
    if report.decision_note:
        decision += f" — {report.decision_note}"
    lines.append(decision)
    return "\n".join(lines) + "\n"


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
    parser = argparse.ArgumentParser(description="Гейт артефакта разведки")
    parser.add_argument("path", help="путь к файлу артефакта (docs/research/<план>-prior-art.md)")
    parser.add_argument("--quiet", action="store_true", help="печатать только нарушения и итог")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"ОШИБКА: файл артефакта не найден: {path}")
        return 2

    report = check_file(path)
    for level, code, message in report.problems:
        if args.quiet and level == "PASS":
            continue
        print(f"{level} {code} {message}")

    failures = [item for item in report.problems if item[0] == "FAIL"]
    if failures:
        print(f"ИТОГ: FAIL — нарушений {len(failures)}")
        return 1
    print(f"ИТОГ: PASS — кандидатов {len(report.candidates)}, решение {report.decision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
