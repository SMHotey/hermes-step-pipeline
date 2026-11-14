# -*- coding: utf-8 -*-
"""Анализатор согласованности и исход идеи: A1-A5, kill, уточнения, ретроспектива.

Восемь тестов шага S10 плана 5. Первые пять — по классу расхождений на синтетических
планах: шаг без гейтов, файл вне списка шага, раздел «Разведка» без исхода, план без
раздела, ссылка на несуществующий файл. Шестой — чистый план (ноль находок, код 0 у
копии движка в `.pipeline/`, шапка шаблона уточнений равна `QUESTIONS_COLUMNS`).
Седьмой — исход `kill` и `needs-clarification`: плана не касаются ни карточки шагов, ни
стоп-ошибка. Восьмой — черновик ретроспективы после закрытия плана, ровно один раз.

Тесты идут через `gate.run_gate` на временном проекте с поддельной бордой
(`hermes_stub`), карточки читаются из sqlite, стоп-флаг — `gate.load_stop`.
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import analyze
import config as config_mod
import gate
from engine_kit import write_approval

CONSTITUTION = "Конституция: docs/CONSTITUTION.md"

STEP1 = "\n".join([
    "- Файл: `app/models/invoice.py`, `tests/test_invoice.py`",
    "- Тесты/гейты: `python -m pytest tests/test_invoice.py -q` → 3 passed",
    "- Готово = таблица `invoice` создана, 3 теста зелёные",
])

STEP2 = "\n".join([
    "- Файл: `app/api/invoices.py`, `tests/test_api_invoice.py`",
    "- Тесты/гейты: `python -m pytest tests/test_api_invoice.py -q` → 5 passed",
    "- Готово = `GET /invoices` отдаёт 200 и список",
])

STEP1_NO_GATES = "- Файл: `app/models/invoice.py`\n- Готово = таблица `invoice` создана"

STEP2_WISH_GATES = ("- Файл: `app/api/invoices.py`\n"
                    "- Тесты/гейты: прогнать тесты\n"
                    "- Готово = `GET /invoices` отдаёт 200 и список")


def format_plan(*, decision: str | None = "go", research: bool = True,
                constitution: bool = True, s1: str = STEP1, s2: str = STEP2) -> str:
    """Синтетический план формата §5 с двумя шагами и журналом.

    `research=False` убирает раздел «Разведка», `decision=None` оставляет раздел без
    строки «Решение:», `constitution=False` делает план legacy.
    """
    lines = ["# 1. Демо-план конвейера", ""]
    if constitution:
        lines += [CONSTITUTION, ""]
    lines += ["Цель: проверить анализатор согласованности артефактов.", ""]
    if research:
        lines += ["## Разведка", "", "Артефакт: `docs/research/1-prior-art.md`", ""]
        if decision is not None:
            lines += [f"Решение: {decision} — готового нет, идём своим решением", ""]
    lines += ["## Шаги", "", "### S1. Таблица счетов (1 дн)", "", *s1.splitlines(), ""]
    lines += ["### S2. Эндпоинт списка (2 дн)", "", *s2.splitlines(), ""]
    lines += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|",
              "| — | S1 | — | — | открыт |",
              "| — | S2 | — | — | открыт |", ""]
    return "\n".join(lines) + "\n"


def write_plan(root: Path, text: str) -> Path:
    """Переписать план 1 проекта и заново утвердить его: sha файла меняется."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(text, encoding="utf-8")
    write_approval(root, path)
    return path


def accepted_artifact(root: Path, decision: str = "go") -> Path:
    """Артефакт разведки, который принимает `scout_report.check_file` (R1-R5)."""
    path = Path(root) / "docs" / "research" / "1-prior-art.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "# Разведка: готовые решения",
        "",
        "Дата: 2026-11-08. Объект: что искали. Метод: GitHub API.",
        "",
        "## Кандидаты",
        "",
        "| Кандидат | Ссылка | Звёзды | Дата коммита | Лицензия | Что даёт "
        "| Стоимость, дн | Вердикт |",
        "|---|---|---|---|---|---|---|---|",
        "| owner/repo | https://example.org/repo | 10 | 2026-11-01 | MIT | таблица | 2 | своё |",
        "",
        "## Итог",
        "",
        f"Решение: {decision} — готового под задачу нет",
        "",
    ]), encoding="utf-8")
    return path


def questions_file(root: Path, answer: str = "—") -> Path:
    """Артефакт уточнений с одним вопросом и данным ответом."""
    path = Path(root) / "docs" / "research" / "1-questions.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "# Уточнения: план 1",
        "",
        "Дата: 2026-11-08. План: `docs/plans/1-demo.md`. Статус: открыт.",
        "",
        "## Вопросы",
        "",
        "| № | Вопрос | Варианты | Рекомендация | Ответ |",
        "|---|---|---|---|---|",
        f"| 1 | что именно неясно | A — цена; B — цена | A, потому что дешевле | {answer} |",
        "",
    ]), encoding="utf-8")
    return path


def codes(findings) -> list[str]:
    """Коды находок в порядке выдачи."""
    return [f.code for f in findings]


def board_empty(db: Path) -> bool:
    """Карточек нет: базы или таблицы `tasks` может ещё не быть — она создаётся первой записью."""
    if not Path(db).is_file():
        return True
    con = sqlite3.connect(str(db))
    try:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                             " AND name='tasks'").fetchone()
        if not exists:
            return True
        return con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        con.close()


def line_of(text: str, needle: str) -> int:
    """Номер строки (1-based) строки, содержащей `needle`."""
    return next(i for i, line in enumerate(text.splitlines(), start=1) if needle in line)


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в `PIPELINE_REPO`."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1"), hermes_stub


# ─────────────────────────── A1-A5 на синтетике ───────────────────────────

def test_step_without_gates_is_found(project: Path):
    """Без пункта гейтов и с гейтами-пожеланием без команды — A1 на обоих шагах."""
    text = format_plan(s1=STEP1_NO_GATES, s2=STEP2_WISH_GATES)
    plan = write_plan(project, text)

    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A1", "A1"], findings
    assert {f.step for f in findings} == {"S1", "S2"}
    assert findings[0].line == line_of(text, "### S1. Таблица счетов")
    assert "гейт" in findings[0].message

    # Команда в гейтах появилась — находка исчезла там, где доказательство есть.
    text = format_plan(s1=STEP1_NO_GATES, s2=STEP2)
    plan = write_plan(project, text)
    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A1"], findings
    assert findings[0].step == "S1"


def test_file_outside_step_list_is_found(project: Path):
    """Файл есть в дереве, но шаг трогает его вне своего пункта «Файл:» — A2."""
    (project / "app").mkdir(parents=True, exist_ok=True)
    (project / "app" / "extra.py").write_text("", encoding="utf-8")
    note = "- Важно: не ломать контракт `app/extra.py`"
    text = format_plan(s1=STEP1 + "\n" + note)
    plan = write_plan(project, text)

    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A2"], findings
    assert "app/extra.py" in findings[0].message
    assert findings[0].line == line_of(text, "не ломать контракт")

    # Тот же файл объявлен в пункте «Файл:» — находки нет.
    declared = STEP1.replace("`app/models/invoice.py`",
                             "`app/models/invoice.py`, `app/extra.py`") + "\n" + note
    plan = write_plan(project, format_plan(s1=declared))
    assert analyze.analyze(plan, project) == []


def test_research_section_without_decision_is_found(project: Path):
    """Раздел без «Решение:», расхождение с артефактом и согласие — три случая A3."""
    plan = write_plan(project, format_plan(decision=None))
    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A3"], findings
    assert "Решение" in findings[0].message
    assert findings[0].step == ""

    # Артефакт говорит kill, план — go: расхождение.
    plan = write_plan(project, format_plan(decision="go"))
    accepted_artifact(project, "kill")
    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A3"], findings
    assert "не совпадает" in findings[0].message and "kill" in findings[0].message

    # Исходы совпали — находки нет.
    accepted_artifact(project, "go")
    assert analyze.analyze(plan, project) == []


def test_plan_without_research_section_is_found(project: Path):
    """План действующего формата без раздела «Разведка» — A4; legacy не трогаем."""
    plan = write_plan(project, format_plan(research=False))
    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A4"], findings
    assert findings[0].line == 1 and findings[0].step == ""

    plan = write_plan(project, format_plan(research=False, constitution=False))
    assert analyze.analyze(plan, project) == []


def test_nonexistent_file_reference_is_found(project: Path):
    """Ссылка на файл, которого нет и никто не объявлял, — A5; объявленный файл молчит."""
    step1 = STEP1.replace("`app/models/invoice.py`", "`app/models/invoice.py`, `app/later.py`")
    step2 = STEP2 + "\n- Важно: отчёт в `tests/test_gone.py`, рядом `app/later.py`"
    text = format_plan(s1=step1, s2=step2)
    plan = write_plan(project, text)

    findings = analyze.analyze(plan, project)
    assert codes(findings) == ["A5"], findings
    assert "tests/test_gone.py" in findings[0].message
    assert "app/later.py" not in findings[0].message
    assert findings[0].line == line_of(text, "tests/test_gone.py")


# ──────────────────── чистый план, CLI и шаблон уточнений ────────────────────

def test_clean_plan_passes_and_questions_template_matches(project: Path):
    """Чистый план — ноль находок и код 0 у копии движка в `.pipeline/`."""
    plan = write_plan(project, format_plan())
    assert analyze.analyze(plan, project) == []

    cli = [sys.executable, str(project / ".pipeline" / "analyze.py"), "docs/plans/1-demo.md"]
    clean = subprocess.run(cli, cwd=str(project), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "ИТОГ: PASS" in clean.stdout

    # Тот же прогон на плане с шагом без гейтов: находка есть, код 1.
    write_plan(project, format_plan(s1=STEP1_NO_GATES))
    dirty = subprocess.run(cli, cwd=str(project), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    assert dirty.returncode == 1, dirty.stdout + dirty.stderr
    assert "ИТОГ: FAIL" in dirty.stdout and "A1" in dirty.stdout

    template = (Path(__file__).resolve().parents[2] / "docs" / "research"
                / "TEMPLATE-questions.md")
    header = "| " + " | ".join(analyze.QUESTIONS_COLUMNS) + " |"
    assert header in template.read_text(encoding="utf-8").splitlines()


# ─────────────────────── исход идеи: kill и уточнения ───────────────────────

def test_kill_and_needs_clarification_stop_without_stop_flag(stand):
    """kill закрывает план без карточек и стопа; needs-clarification ждёт ответов."""
    root, p, db = stand

    accepted_artifact(root, "kill")
    write_plan(root, format_plan(decision="kill"))
    result = gate.run_gate(p, execute=True, notify_fn=quiet)
    decision = result["decision"]
    assert decision["action"] == "idle", decision
    assert decision.get("plan_killed") is True and decision.get("plan_done") is True
    assert "kill" in decision["reason"]
    assert "Исход разведки: kill" in result["status"]
    assert board_empty(db)
    assert gate.load_stop(p) is None

    accepted_artifact(root, "needs-clarification")
    write_plan(root, format_plan(decision="needs-clarification"))
    questions_file(root)
    result = gate.run_gate(p, execute=True, notify_fn=quiet)
    decision = result["decision"]
    assert decision["action"] == "idle", decision
    assert decision.get("plan_clarify") is True
    assert "1-questions.md" in decision["reason"] and "открыто 1 из 1" in decision["reason"]
    assert board_empty(db)
    assert gate.load_stop(p) is None

    # Предзаказ постановки следующего шага при ожидании ответов не заказывается (D5):
    # шаг S1 в работе, постановки S2 нет — без оговорки тик выдал бы `create_prompt`.
    ctx = {"plan_id": "1", "plan_decision": "needs-clarification", "questions_open": (1, 1),
           "next": {"id": "S1"}, "next_step": {"id": "S2"}, "prefetch": True,
           "cards": [{"id": "t_1", "status": "running", "key": "planloop:1:S1:step"}],
           "prev_cards": [], "closed": [], "stop": None, "hold": [], "verdict": None}
    blocked = gate.decide(ctx)
    assert blocked["action"] == "idle", blocked
    assert blocked.get("plan_clarify") is True
    assert "постановка S2" not in blocked.get("reason", "")


# ───────────────────────── ретроспектива после плана ─────────────────────────

def test_retro_draft_is_written_once_after_plan_closed(stand):
    """Все шаги закрыты — черновик `docs/reviews/retro-1.md` ровно один раз (D8)."""
    root, p, db = stand
    text = format_plan()
    text = text.replace("| — | S1 | — | — | открыт |",
                        "| 01.11.2026 | S1 (Таблица счетов) | abc1234 | 3 passed | ЗАКРЫТ |")
    text = text.replace("| — | S2 | — | — | открыт |",
                        "| 02.11.2026 | S2 (Эндпоинт списка) | def5678 | 5 passed | ЗАКРЫТ |")
    write_plan(root, text)

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "write_retro", first["decision"]
    assert first["applied"] is True
    retro = root / "docs" / "reviews" / "retro-1.md"
    body = retro.read_text(encoding="utf-8")
    assert "Ретроспектива плана 1" in body
    assert "| S1 (Таблица счетов) | 01.11.2026 | abc1234 | ЗАКРЫТ |" in body
    assert "| S2 (Эндпоинт списка) | 02.11.2026 | def5678 | ЗАКРЫТ |" in body
    assert "## Что мешало" in body
    digest = hashlib.sha256(retro.read_bytes()).hexdigest()

    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert hashlib.sha256(retro.read_bytes()).hexdigest() == digest
    assert second["decision"]["action"] == "idle", second["decision"]
    assert second["decision"].get("plan_done") is True
    assert board_empty(db)
    assert gate.load_stop(p) is None
