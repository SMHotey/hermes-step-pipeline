# -*- coding: utf-8 -*-
"""Коды находок шага: закрытый словарь, статус из кода и машинные проверки (план 9, S4).

Что держат эти проверки:

1. словарь кодов объявлен одним местом (`registry.py`, якорь `ANCHOR:коды_находок`), названия
   кодов непустые и различимые;
2. код печатается в строке реестра карточки правок и блок машинных проверок в теле есть;
3. блокирующая находка без кода шаг не держит, но текст её строки сохраняется;
4. код вне словаря (`F10`) кодом не считается и называется линтером отчёта;
5. F01 считается по списку файлов коммита, белый список D7 не красит объявленный коммит;
6. F03 подтягивает команду, код возврата и хвост вывода красного прогона;
7. реестр шага с кодом читается парсером, общим с реестром плана 7;
8. файл плана 7 (шесть колонок, `plan_sha256:`) с записями без кода читается без миграции;
9. пустой реестр не роняет ни печать строк, ни проверки.

Фикстуры — по образцу `tests/engine/test_step_findings.py::stand`/`::spy_cards`; путь движка
берётся из `tests/engine/engine_kit.py::ENGINE`, временные каталоги — `tmp_path`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import config as config_mod
import gate
import registry
from engine_kit import ENGINE, make_project

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1.md"

#: Отчёт ревью с кодом: находка про реестр — нарушение инварианта движка (F04).
REPORT_CODED = """## ВЕРДИКТ: REQUEST_CHANGES

## Находки

- [блокирующая] F04 `step_pipeline/engine/gate.py::step_finding_rows` — статус строки
  выводится из класса без кода — ожидается: статус из класса и кода словаря
- [стилистическая] F06 `CHANGELOG.md` — раздел без раздела о кодах — ожидается: запись
"""

#: Отчёт ревью без кода: та же находка, но без ведущего кода словаря (D3).
REPORT_UNCODED = """## ВЕРДИКТ: REQUEST_CHANGES

## Находки

- [блокирующая] `step_pipeline/engine/gate.py::step_finding_rows` — статус без кода
"""

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def paths(project: Path):
    """Пути плана 1 проекта фикстуры: `gate.Paths` для вызовов движка."""
    return gate.resolve_paths(config_mod.load(project), "1")


def write_report(project: Path, text: str, rel: str = REPORT_REL) -> Path:
    """Положить отчёт ревью шага в проект и вернуть его путь."""
    path = Path(project) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def add_registry(p, text: str, kind: str = "блокирующая",
                 status: str | None = None) -> Path:
    """Положить в реестр шага одну строку руками, как её пишет ревьюер."""
    path = gate.step_findings_path(p, "S1")
    gate.add_findings(path,
                      [{"kind": kind, "text": text,
                        "action": "проверить коды",
                        "status": status if status is not None else "открыто"}],
                      gate.STEP_FINDINGS_HEADER, gate.STEP_FINDINGS_KEYS, "шага", "step-",
                      kinds=gate.STEP_FINDING_KINDS)
    return path


# ─────────────────────────── словарь (п.1) ───────────────────────────

def test_dictionary_is_closed_and_anchored():
    """Девять кодов в порядке плана, названия непустые и различимые, якорь на месте."""
    source = (ENGINE / "registry.py").read_text(encoding="utf-8")
    assert "# ANCHOR:коды_находок" in source, "якоря словаря нет в registry.py"
    assert registry.FINDING_CODES == ("F01", "F02", "F03", "F04", "F05", "F06", "F07",
                                      "F08", "F09"), registry.FINDING_CODES
    titles = [registry.finding_code_title(code) for code in registry.FINDING_CODES]
    assert all(titles), f"пустые названия кодов: {titles}"
    assert len(set(titles)) == len(titles), f"названия кодов совпадают: {titles}"
    assert set(registry.FINDING_CODE_TITLES) == set(registry.FINDING_CODES)
    assert registry.finding_code_title("F01") == "правка вне объявленных файлов"


# ────────────────────── печать в карточке правок (п.4, п.6) ──────────────────────

def test_code_printed_in_fix_card(project):
    """Строка реестра несёт код и название, а в теле карточки есть блок с строкой F02."""
    p = paths(project)
    add_registry(p, "F01 `app/models/invoice.py` — правка вне объявленных файлов")
    body = gate.fix_card_body(p, STEP, 1, REPORT_REL)
    assert "- F1 (блокирующая): " in body, "префикс строки реестра сломан"
    assert ("- F1 (блокирующая): F01 «правка вне объявленных файлов» — "
            "`app/models/invoice.py` — правка вне объявленных файлов") in body, body
    assert gate.MACHINE_CHECKS_HEADER in body, "блока машинных проверок нет в карточке"
    assert any(line.startswith("F02 — ") for line in body.splitlines()), body


# ───────────────────── статус строки из класса и кода (п.2) ─────────────────────

def test_text_without_code_does_not_block(project):
    """Блокирующая находка без кода встаёт как `не блокирует`, текст строки сохранён."""
    p = paths(project)
    write_report(project, REPORT_UNCODED)
    rows = gate.ensure_step_findings(p, STEP, REPORT_REL)
    assert [row["status"] for row in rows] == ["не блокирует"], rows
    assert rows[0]["kind"] == "блокирующая", "класс строки потерян"
    assert "статус без кода" in rows[0]["text"], rows[0]["text"]
    assert registry.finding_code(rows[0]["text"]) == ""
    assert gate.open_step_findings({"step_findings": rows}) == [], "строка держит шаг без кода"

    # Тот же отчёт с ведущим кодом строку открывает — правило работает на коде, а не на отчёте.
    other = make_project(Path(project).parent / "coded")
    write_report(other, REPORT_CODED)
    coded = gate.ensure_step_findings(paths(other), STEP, REPORT_REL)
    assert [row["status"] for row in coded] == ["открыто", "не блокирует"], coded


# ──────────────────── код вне словаря (D4) ────────────────────

def test_code_outside_dictionary_rejected():
    """`F10` — не код: ведущий код пуст, линтер отчёта его называет, S3 его не возвращает."""
    assert registry.finding_code("F10 — правка вне файлов") == "", "F10 признан кодом"
    assert registry.finding_code("F00 `x.py`") == ""
    assert registry.finding_code("F0X") == ""
    text = ("## Находки\n\n- [блокирующая] F10 `app/x.py` — код вне словаря\n"
            "- [значимая] F06 `CHANGELOG.md` — документация\n")
    assert gate.report_unknown_codes(text) == ["F10"], gate.report_unknown_codes(text)
    assert gate.report_finding_codes(text) == ["F06"], gate.report_finding_codes(text)


# ─────────────────────── F01 по файлам коммита (п.5) ───────────────────────

def git(root: Path, *args: str) -> str:
    """Выполнить git в каталоге стенда и вернуть stdout (ошибка — падение теста)."""
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, f"git {args}: {res.stderr}"
    return res.stdout


def commit_all(root: Path, files: list[str]) -> str:
    """Закоммитить перечисленные файлы стенда (не `-A`) и вернуть полный sha."""
    git(root, "init", "-q")
    git(root, "add", *files)
    git(root, "-c", "user.name=stand", "-c", "user.email=stand@example.invalid",
        "commit", "-q", "-m", "стенд F01")
    return git(root, "rev-parse", "HEAD").strip()


def test_f01_counts_commit_files(tmp_path):
    """Посторонний файл назван, объявленные и белый список D7 в находку не попадают."""
    root = make_project(tmp_path / "f01")
    declared = ["app/models/invoice.py"]
    stray = "step_pipeline/ui/app.js"
    tracked = declared + [stray, "docs/reports/1-S1_report.md", "tests/test_invoice.py",
                          "CHANGELOG.md", "step_pipeline/plugin.yaml"]
    for rel in tracked:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    sha = commit_all(root, tracked)
    p = paths(root)

    assert gate.commit_files(p, sha) and stray in gate.commit_files(p, sha)
    step = dict(STEP, files=declared)
    assert gate.step_file_offenders(p, step, sha) == [stray], gate.step_file_offenders(
        p, step, sha)

    lines = gate.finding_code_checks(p, step, sha)
    f01 = [line for line in lines if line.startswith("F01")][0]
    assert stray in f01, f01
    assert "tests/test_invoice.py" not in f01 and "CHANGELOG.md" not in f01, f01
    body = gate.fix_card_body(p, step, 1, REPORT_REL)
    assert f"F01 — правка вне объявленных файлов: `{stray}`" in body, body


# ─────────────────────── F03 по доказательству прогонов (п.5) ───────────────────────

def test_f03_pulls_run_output(project):
    """Красный прогон даёт команду, код возврата и хвост вывода; зелёный — счётчик."""
    p = paths(project)
    green = {"command": "python -m pytest tests/test_invoice.py", "expect": "exit",
             "value": "0", "outcome": "green", "code": 0, "seconds": 3, "tail": "",
             "reason": ""}
    red = {"command": "python -m pytest tests/test_api_invoice.py", "expect": "exit",
           "value": "0", "outcome": "red", "code": 1, "seconds": 4,
           "tail": "FAILED tests/test_api_invoice.py::test_list - AssertionError: boom",
           "reason": "выход не совпал"}
    proof = {"plan": "1", "step": "S1", "sha": SHA, "total": 2, "green": 1, "seconds": 7,
             "runs": [green, red]}
    gate.save_runs_proof(p, proof)

    f03 = [line for line in gate.finding_code_checks(p, STEP, SHA)
           if line.startswith("F03")]
    assert len(f03) == 1, f03
    line = f03[0]
    assert "python -m pytest tests/test_api_invoice.py" in line, line
    assert "код 1" in line, "код возврата красного прогона не назван"
    assert "AssertionError: boom" in line, "хвост вывода прогона не подтянут"

    # Все прогоны зелёные — строка со счётчиком, а не с хвостом.
    ok = dict(proof, green=2, runs=[green, dict(green, command="python -m pytest tests/test_x.py")])
    gate.save_runs_proof(p, ok)
    green_line = [line for line in gate.finding_code_checks(p, STEP, SHA)
                  if line.startswith("F03")][0]
    assert "Прогоны: 2/2" in green_line, green_line


# ──────────────── реестр шага читается парсером плана 7 (п.9) ────────────────

def test_registry_read_by_plan7_parser(project):
    """Реестр шага с кодом читается тем же разбором: ID, класс, код в тексте, статус."""
    p = paths(project)
    path = add_registry(p, "F04 `step_pipeline/engine/gate.py` — нарушение инварианта")
    rows = gate.load_findings(path, gate.STEP_FINDINGS_HEADER, gate.STEP_FINDINGS_KEYS,
                              kinds=gate.STEP_FINDING_KINDS)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["id"] == "F1", "ID строки изменился"
    assert row["kind"] == "блокирующая"
    assert row["status"] == "открыто"
    assert registry.finding_code(row["text"]) == "F04", row["text"]
    assert registry.finding_text(row["text"]) == "`step_pipeline/engine/gate.py` — нарушение инварианта"


def test_plan7_registry_without_codes_still_reads(tmp_path):
    """Файл плана 7 (шесть колонок, `plan_sha256:`) с записями без кода читается как раньше."""
    path = tmp_path / "plan-7-findings.md"
    path.write_text(
        "plan_sha256: " + "a" * 64 + "\n\n"
        "# Реестр находок плана 7\n\n"
        "| ID | Шаг | Класс | Находка | Что сделать | Статус |\n"
        "|---|---|---|---|---|---|\n"
        "| F1 | S6, S7 | замысел | Порядок шагов противоречит зависимости | "
        "Переставить шаги | закрыто |\n"
        "| F2 | S7 | стиль | Формулировка пункта «Готово =» | Поправить текст | не блокирует |\n",
        encoding="utf-8")
    rows = gate.load_findings(path)
    assert [row["id"] for row in rows] == ["F1", "F2"], rows
    assert [row["status"] for row in rows] == ["закрыто", "не блокирует"], rows
    assert [row["step"] for row in rows] == ["S6, S7", "S7"], rows
    assert all(registry.finding_code(row["text"]) == "" for row in rows), "код появился из ниоткуда"


def test_empty_registry_does_not_crash(project):
    """Ни файла, ни отчёта: печать строк даёт служебную строку, проверки не падают."""
    p = paths(project)
    assert not gate.step_findings_path(p, "S1").is_file(), "стенд уже с реестром"
    lines = gate.step_registry_lines(p, STEP)
    assert lines == ["- реестра ещё нет: первая находка создаёт файл с шапкой выше."], lines
    assert gate.open_step_findings_lines(p, STEP) == []
    checks = gate.finding_code_checks(p, STEP)
    assert [line.split(" ")[0] for line in checks] == ["F01", "F02", "F03"], checks
    assert all(line.startswith(("F01", "F02", "F03")) for line in checks), checks
