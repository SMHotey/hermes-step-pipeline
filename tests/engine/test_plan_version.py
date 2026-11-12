# -*- coding: utf-8 -*-
"""Версия плана без журнала (0.22.1): закрытие шага не сбрасывает утверждение и ревью.

Зачем: журнал выполнения живёт внутри файла плана, поэтому каждое закрытие шага
(`открыт` → `ЗАКРЫТ`) меняло sha256 файла. Страж считал, что план правился после
APPROVE, и заказывал новый круг ревью плана — у плана 1 проекта gapaudit таких кругов
набралось одиннадцать, часть из них только из-за строки журнала. Теперь sha считается по
версии плана: файл с нормализованным журналом (`approval.plan_version_text`).

Проверяется: закрытие шага версию не меняет и вердикт остаётся свежим; правка шага
версию меняет и вердикт становится `stale`; запись утверждения с `version_sha256`
переживает закрытие шага; легаси-запись (только сырой `sha256`) работает по-старому;
команда `gate.py --plan-version-sha` печатает ту же метку, что ждёт страж.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import approval
import config as config_mod
import gate
import registry
from engine_kit import PLAN, make_project, plan_sha256, write_approval

# План действующего формата: строка `Конституция:` в шапке включает ревью плана.
PLAN_TEXT = PLAN.replace("# 1. Демо-план конвейера\n",
                         "# 1. Демо-план конвейера\n\nКонституция: docs/CONSTITUTION.md\n", 1)


def plan_path(root: Path) -> Path:
    return Path(root) / "docs" / "plans" / "1-demo.md"


def write_report(root: Path, label: str) -> Path:
    """Отчёт ревью плана с меткой версии и ссылкой на конституцию."""
    path = Path(root) / "docs" / "reviews" / "plan-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Ревью плана 1\n\n"
        f"Конституция: п.1 — нарушений нет\n"
        f"plan_sha256: {label}\n\n"
        "## ВЕРДИКТ: APPROVE\n",
        encoding="utf-8")
    return path


@pytest.fixture()
def stand(project: Path, monkeypatch):
    """Проект-приёмник: план с конституцией, утверждение владельца, живой конфиг."""
    root = make_project(project)
    plan_path(root).write_text(PLAN_TEXT, encoding="utf-8")
    write_approval(root, plan_path(root))
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    return root, plan_path(root), config_mod.load(root)


def close_step(root: Path, cfg, step: str = "S1") -> bool:
    """Закрыть шаг как это делает страж: строка журнала `открыт` → `ЗАКРЫТ`."""
    p = gate.resolve_paths(cfg, "1")
    return gate.append_journal_row(p, step, "Таблица счетов", "abc1234", "ревью APPROVE")


def test_journal_row_does_not_change_plan_version(stand):
    """Строка журнала — не версия плана: sha версии до и после закрытия шага совпадает."""
    root, plan, cfg = stand
    version_before = approval.plan_version_sha(plan)
    raw_before = plan_sha256(plan)

    assert close_step(root, cfg) is True

    assert approval.plan_version_sha(plan) == version_before
    assert plan_sha256(plan) != raw_before, "сырой хэш файла закрытие шага меняет"
    assert "ЗАКРЫТ" in plan.read_text(encoding="utf-8")


def test_review_stays_fresh_after_step_close(stand):
    """Вердикт, привязанный к версии плана, не устаревает от закрытия шага."""
    root, plan, cfg = stand
    write_report(root, approval.plan_version_sha(plan))
    spec = registry.get(cfg, "1")

    assert approval.review_status(cfg, spec)["state"] == approval.REVIEW_FRESH

    close_step(root, cfg)

    review = approval.review_status(cfg, spec)
    assert review["state"] == approval.REVIEW_FRESH, review["reason"]
    assert review["label_sha"] == review["version_sha256"]


def test_approval_survives_step_close(stand):
    """Запись утверждения с `version_sha256` остаётся `approved` после закрытия шага."""
    root, plan, cfg = stand
    spec = registry.get(cfg, "1")
    write_report(root, approval.plan_version_sha(plan))

    close_step(root, cfg)

    decision = approval.evaluate(cfg, spec)
    assert decision["state"] == "approved", decision["reason"]
    assert decision["ok"] is True, decision["reason"]


def test_step_edit_still_stales_review(stand):
    """Содержательная правка шага версию меняет: вердикт устаревает, как раньше."""
    root, plan, cfg = stand
    write_report(root, approval.plan_version_sha(plan))
    spec = registry.get(cfg, "1")

    text = plan.read_text(encoding="utf-8").replace(
        "- Готово = таблица `invoice` создана, 3 теста зелёные",
        "- Готово = таблица `invoice` создана, 4 теста зелёные")
    plan.write_text(text, encoding="utf-8")

    review = approval.review_status(cfg, spec)
    assert review["state"] == approval.REVIEW_STALE
    assert "правился после APPROVE" in review["reason"]


def test_legacy_record_without_version_still_approved(stand):
    """Запись без `version_sha256` (до 0.22.1) сверяется по сырому хэшу — как раньше."""
    root, plan, cfg = stand
    write_approval(root, plan, version_sha="")
    spec = registry.get(cfg, "1")

    assert approval.evaluate(cfg, spec)["state"] == "approved"

    plan.write_text(plan.read_text(encoding="utf-8") + "\nПравка.\n", encoding="utf-8")
    assert approval.evaluate(cfg, spec)["state"] == "changed"


def test_legacy_report_label_with_raw_sha_still_fresh(stand):
    """Отчёт с меткой по сырому sha принимается, пока файл не менялся."""
    root, plan, cfg = stand
    write_report(root, plan_sha256(plan))
    spec = registry.get(cfg, "1")

    assert approval.review_status(cfg, spec)["state"] == approval.REVIEW_FRESH


def test_plan_version_text_keeps_other_tables(stand):
    """Нормализуется только журнал: таблица разведки и шапка журнала остаются в версии."""
    root, plan, cfg = stand
    text = plan.read_text(encoding="utf-8").replace(
        "## Журнал выполнения\n\n",
        "## Разведка\n\n| Кандидат | Вердикт |\n|---|---|\n| demo/spec-kit | берём |\n\n"
        "## Журнал выполнения\n\n")
    plan.write_text(text, encoding="utf-8")
    before = approval.plan_version_sha(plan)

    plan.write_text(text.replace("| demo/spec-kit | берём |", "| demo/spec-kit | своё |"),
                    encoding="utf-8")

    assert approval.plan_version_sha(plan) != before


def test_cli_prints_the_same_version_sha(stand, project: Path):
    """`gate.py --plan-version-sha` печатает метку, которую ждёт страж в отчёте."""
    root, plan, _cfg = stand
    engine = Path(__file__).resolve().parents[2] / "step_pipeline" / "engine" / "gate.py"
    env = dict(os.environ, PIPELINE_REPO=str(root), PYTHONIOENCODING="utf-8")

    out = subprocess.run([sys.executable, str(engine), "--plan-version-sha",
                          "docs/plans/1-demo.md"],
                         cwd=str(root), env=env, capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, out.stderr
    printed = out.stdout.strip().splitlines()[-1]
    assert printed == approval.plan_version_sha(plan)
    assert len(printed) == 64


def test_plan_review_card_forbids_address_nitpicks(stand):
    """Карточка ревью плана запрещает придирки об адресах и арифметике тестов.

    Круг правок плана стоит часов работы и заказа, поэтому в контракт внесены «не находки»:
    номера строк в коде, сумма тестов, имена внутренних функций, формулировки. Блокирует
    только то, из-за чего шаг нельзя выполнить; закрытые находки повторно не поднимаются.
    """
    root, plan, cfg = stand
    p = gate.resolve_paths(cfg, "1")

    body = gate.plan_review_card_body(p, p.cfg.rel(plan), "docs/reviews/plan-1.md", "reviewer")

    assert "НЕ НАХОДКИ" in body
    assert "номера строк" in body
    assert "арифметика числа тестов" in body
    assert "Блокирует только то" in body
    assert "повторно не поднимаются" in body


def test_plan_review_card_points_to_version_command(stand):
    """Карточка ревью плана называет команду метки, а не `sha256sum` по файлу."""
    root, plan, cfg = stand
    p = gate.resolve_paths(cfg, "1")

    body = gate.plan_review_card_body(p, p.cfg.rel(plan), "docs/reviews/plan-1.md", "reviewer")

    assert "--plan-version-sha docs/plans/1-demo.md" in body
    assert "sha256sum" not in body
