# -*- coding: utf-8 -*-
"""Паритет пред-гейта и приёма: одна форма плана — одни строки находок (шаг 15-S3).

Анализ `analyze.py` и утверждение `plan_accept.check_draft` судят форму одним набором
правил (Л1-Л22), но живут в разных слоях: приём — в плагине, пред-гейт — в копии движка
в проекте. Тесты проверяют дословное равенство: строка, которую вернул приём, обязана
стоять в stdout пред-гейта целиком, а не «примерно так же».

Четыре случая: текст совпадает и код 1; ИТОГ считает сумму каналов; чистый план — код 0;
копия без линтера (проект «до `setup --upgrade`») даёт прежний stdout и одну строку в stderr.

Планы строит локальный билдер: раздел «Разведка» без кандидатов и лицензий — ровно тот
дефект, из-за которого шаг и делался.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT))

from engine_kit import make_project  # noqa: E402 - помощник слоя движка

#: Шапка плана действующего формата: без строки «Конституция:» линтер считает план legacy
#: и молчит про раздел «Разведка» — тогда паритет проверять не на чем.
CONSTITUTION = "Конституция: docs/CONSTITUTION.md"

STEP1 = "\n".join([
    "- Файл: `app/models/invoice.py`, `tests/test_invoice.py`",
    "- Тесты/гейты: `python -m pytest tests/test_invoice.py -q` → 3 passed",
    "- Прогон: `python -m pytest tests/test_invoice.py -q` → exit 0",
    "- Готово = таблица `invoice` создана, 3 теста зелёные",
])

STEP2 = "\n".join([
    "- Файл: `app/api/invoices.py`, `tests/test_api_invoice.py`",
    "- Тесты/гейты: `python -m pytest tests/test_api_invoice.py -q` → 5 passed",
    "- Прогон: `python -m pytest tests/test_api_invoice.py -q` → exit 0",
    "- Готово = `GET /invoices` отдаёт 200 и список",
])

#: Строки разведки, которых требует §5a: ссылка на артефакт, кандидаты, лицензии, решение.
RESEARCH_LINK = "Артефакт: `docs/research/1-prior-art.md`"
RESEARCH_DECISION = "Решение: go — готового под нет, идём своим решением"
RESEARCH_CANDIDATES = "Кандидаты: `owner/repo` — лицензия MIT, `other/repo` — Apache-2.0"


def build_plan(*, full: bool = False) -> str:
    """Синтетический план: `full=True` — раздел «Разведка» полный, иначе без кандидатов."""
    lines = ["# 1. Демо-план пред-гейта", "", CONSTITUTION, "",
             "Цель: пред-гейт и приём судят форму по одним правилам.", "",
             "## Разведка", "", RESEARCH_LINK, RESEARCH_DECISION, ""]
    if full:
        lines += [RESEARCH_CANDIDATES, ""]
    lines += ["## Шаги", "", "### S1. Таблица счетов (1 дн)", "", *STEP1.splitlines(), ""]
    lines += ["### S2. Эндпоинт списка (2 дн)", "", *STEP2.splitlines(), ""]
    lines += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|",
              "| — | S1 | — | — | открыт |",
              "| — | S2 | — | — | открыт |", ""]
    return "\n".join(lines) + "\n"


def write_plan(project: Path, text: str) -> Path:
    """План проекта под именем `1-demo.md` (запись утверждения уже написана фикстурой)."""
    path = Path(project) / "docs" / "plans" / "1-demo.md"
    path.write_text(text, encoding="utf-8")
    return path


def run_pregate(project: Path) -> subprocess.CompletedProcess:
    """Пред-гейт из копии движка проекта — тем же способом, что зовёт страж."""
    cli = [sys.executable, str(Path(project) / ".pipeline" / "analyze.py"), "docs/plans/1-demo.md"]
    return subprocess.run(cli, cwd=str(project), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Проект-приёмник: копия движка (с линтером) и `pipeline.json`."""
    return make_project(tmp_path / "proj")


def acceptance(project: Path, plan: Path) -> list[str]:
    """Строки приёма плана — честный `plan_accept.check_draft`, а не пересказ линтера."""
    from step_pipeline import plan_accept

    return plan_accept.check_draft(project, plan)


def test_pregate_repeats_acceptance_text(project: Path) -> None:
    """Каждая строка приёма стоит в stdout пред-гейта дословно; код возврата 1."""
    plan = write_plan(project, build_plan())
    accepted = acceptance(project, plan)
    assert accepted, "приём обязан забраковать план без кандидатов и лицензий"

    result = run_pregate(project)
    assert result.returncode == 1, result.stdout + result.stderr
    printed = set(result.stdout.splitlines())
    assert set(accepted) <= printed, sorted(set(accepted) - printed)


def test_pregate_counts_both_channels(project: Path) -> None:
    """ИТОГ пред-гейта — сумма A-канала и приёма; на этом плане A-канал пуст."""
    plan = write_plan(project, build_plan())
    accepted = acceptance(project, plan)

    result = run_pregate(project)
    findings = len(accepted)          # A-канал на согласованном плане молчит
    assert f"ИТОГ: FAIL — находок {findings}" in result.stdout.splitlines()
    assert len(accepted) == 2, accepted   # «нет кандидатов» + «нет лицензий»


def test_clean_plan_passes(project: Path) -> None:
    """Полный раздел «Разведка» — ни одной строки линтера, код 0 и `ИТОГ: PASS`."""
    plan = write_plan(project, build_plan(full=True))
    assert acceptance(project, plan) == []

    result = run_pregate(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ИТОГ: PASS" in result.stdout
    assert "Разведка" not in result.stdout


def test_pregate_without_linter_keeps_stdout(project: Path) -> None:
    """Копия без линтера: stdout прежний, в stderr одна строка с рецептом (Р5)."""
    clean = build_plan(full=True)
    write_plan(project, clean)
    before = run_pregate(project)
    assert before.returncode == 0, before.stdout + before.stderr

    (Path(project) / ".pipeline" / "plan_lint.py").unlink()
    after = run_pregate(project)
    assert after.returncode == 0, after.stdout + after.stderr
    assert after.stdout == before.stdout
    stderr = [line for line in after.stderr.splitlines() if line.strip()]
    assert len(stderr) == 1, stderr
    assert "setup --upgrade" in stderr[0]

    # Тот же прогон на плане без кандидатов: до шага пред-гейт пропускал его зелёным.
    write_plan(project, build_plan())
    blind = run_pregate(project)
    assert blind.returncode == 0, blind.stdout + blind.stderr
    assert "ИТОГ: PASS" in blind.stdout
    assert "Разведка" not in blind.stdout
