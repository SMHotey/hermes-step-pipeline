# -*- coding: utf-8 -*-
"""Ручной толчок конвейера: dry-run и создание карточки шага.

Причина тестов: `step_card.py` не импортирует ни движок, ни один тест, поэтому его
поведение (заголовок, ключ карточки `planloop`, коды возврата) держалось на глазах, а не
на проверке. Карточки создаёт поддельный `hermes` из `stub_hermes.py` — настоящая борда
не трогается, установленный `hermes` не нужен.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from engine_kit import ENGINE

STEP_CARD = ENGINE / "step_card.py"


def load_step_card():
    """Импортировать `step_pipeline/engine/step_card.py` — файл движка, не копию.

    Тест на копии вне шаблонов `.coveragerc` не попадал бы в замер (см. 1-S12 §2.3),
    поэтому грузим ровно тот файл, который исполняет человек из `.pipeline/`.
    """
    spec = importlib.util.spec_from_file_location("step_card_s12", str(STEP_CARD))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def card_rows(db: Path) -> list[dict]:
    """Карточки из поддельной борды вместе с полями `tenant` и `workspace`."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, title, status, assignee, idempotency_key, max_runtime, tenant,"
            " workspace FROM tasks ORDER BY created_at").fetchall()
    finally:
        con.close()
    keys = ("id", "title", "status", "assignee", "key", "max_runtime", "tenant", "workspace")
    return [dict(zip(keys, row)) for row in rows]


@pytest.fixture()
def manual(project: Path, hermes_stub: Path, monkeypatch) -> tuple[Path, Path, object]:
    """Проект + поддельная борда + модуль `step_card`.

    `PIPELINE_REPO` обязателен: `gate.repo_root()` без него вернёт каталог плагина, а не
    временный проект, и `pipeline.json` искался бы не там.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, hermes_stub, load_step_card()


def test_dry_run_prints_card_without_creating(manual, capsys):
    """`--dry-run`: карточка показана целиком, но не создана."""
    _project, db, module = manual

    code = module.main(["--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "[план 1 S1] Таблица счетов" in out
    assert "-" * 60 in out
    assert "Гейты (запустить и показать фактический результат):" in out
    assert "Готово = таблица `invoice` создана, 3 теста зелёные" in out
    assert "Контракт шага:" in out and "1-demo.md" in out and "§S1" in out
    # карточки нет: `_create_card` не вызывался вовсе, база даже не появилась
    assert not db.exists()


def test_creates_step_card_with_planloop_key(manual, capsys):
    """Ручной путь создаёт ту же карточку, что и страж: ключ, заголовок, лимит."""
    _project, db, module = manual

    code = module.main([])

    out = capsys.readouterr().out
    assert code == 0
    assert "карточка шага S1: t_" in out
    rows = card_rows(db)
    assert len(rows) == 1
    card = rows[0]
    assert card["key"] == "planloop:1:S1:step"
    assert card["title"] == "[план 1 S1] Таблица счетов"
    assert card["assignee"] == "demo"          # драйвер из pipeline.json проекта
    assert card["status"] == "ready"
    assert card["tenant"] == "plan"
    assert card["max_runtime"] == "3h"        # runtimes.step из шаблона конфига


def test_unknown_plan_returns_2(manual, capsys):
    """Неизвестный номер плана — код 2 и понятное сообщение, карточки нет."""
    _project, db, module = manual

    code = module.main(["--plan", "9"])

    err = capsys.readouterr().err
    assert code == 2
    assert "ошибка:" in err
    assert not db.exists()


def test_broken_config_returns_2(manual, capsys):
    """Нет `pipeline.json` — код 2 и текст ошибки конфига, а не трассировка."""
    project, db, module = manual
    (project / "pipeline.json").unlink()

    code = module.main(["--plan", "1"])

    err = capsys.readouterr().err
    assert code == 2
    assert "ошибка:" in err
    assert not db.exists()
