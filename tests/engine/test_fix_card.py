# -*- coding: utf-8 -*-
"""Карточка правок требует детерминированный тест, а не повтор прогона (план 13, шаг S3).

Замер 6-S3: круг 2 ввёл тест новых ветвей, круг 3 правил его же — `pytest.fail` внутри
цикла повторов зависел от загрузки машины (`4 passed in 7.82 s` против `24.84 s` до
правки). Один лишний круг, около 70 минут. Правило шага — текстовое: движок pytest за
исполнителя не запускает, он печатает требование в карточке правок и в роли.

Тесты держат две вещи: (1) требование трёх прогонов и запрет цикла повторов в тексте
карточки; (2) разбор классов отчёта печатается в карточке — исполнитель видит класс и
подметку находки, счёт считает разбор S1 (`report_finding_classes_at`), второй не пишем.
"""
from __future__ import annotations

from pathlib import Path

import config as config_mod
import gate

STEP = {"id": "S1", "epoch": "", "title": "Мастер первого запуска", "days": 1.0,
        "owner": "", "goal": "g", "files": [], "gates": ["python -m pytest tests/test_x.py"],
        "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1.md"

# Отчёт с подметкой класса: мигающий тест помечен ровно так, как требует грамматика S1.
REPORT = """## ВЕРДИКТ: REQUEST_CHANGES

## Находки

- [блокирующая/тест-недетерминирован] `tests/engine/test_x.py:12` — результат зависит от загрузки
- [стилистическая] `step_pipeline/engine/gate.py:40` — имя не по смыслу
"""


def stand(project: Path, report: str | None = REPORT) -> str:
    """Тело карточки правок шага для проекта с отчётом ревью (или без него)."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    path = Path(p.repo) / REPORT_REL
    if report is None:
        path.unlink(missing_ok=True)   # отчёта нет: разбор начинается с нулей
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    return gate.fix_card_body(p, STEP, 1, REPORT_REL)


def test_fix_card_demands_three_runs(project):
    """Карточка требует три прогона затронутых тестов и запрещает цикл повторов до успеха."""
    body = stand(project)
    assert "ДЕТЕРМИНИРОВАННОСТЬ" in body, body
    assert "ТРИ раза" in body, "нет требования прогнать затронутые тесты три раза"
    assert "три итога" in body, "не сказано, что три итога идут в отчёт"
    assert "`monkeypatch`" in body, "нет способа фиксировать окружение"
    assert "`os.replace`" in body, "нет способа фиксировать порядок/атомарность"
    assert "range(N)" in body, "не назван цикл повторов"
    deny = [line for line in body.splitlines() if "--reruns" in line]
    assert deny, "не назван `--reruns`"
    assert any("запрещён" in line for line in deny), \
        "запрет цикла повторов не утверждён текстом"
    assert "тест-недетерминирован" in body, "нет подметки находки о мигающем тесте"


def test_fix_card_prints_report_classes(project):
    """Карточка печатает разбор классов отчёта: счётчики и подметку находки."""
    assert "Классы находок отчёта: блокирующих 1, значимых 0, стилистических 1" in \
        stand(project), "разбор классов отчёта не напечатан"
    body = stand(project)
    assert "блокирующих 1" in body, body
    assert "стилистических 1" in body, "счёт стилистических классов потерян"
    assert "подметки: тест-недетерминирован ×1" in body, "подметка находки не напечатана"
    # Нечитаемый отчёт: строка выходит с нулями и тело не падает (D1 постановки).
    empty = stand(project, report=None)
    assert "Классы находок отчёта: блокирующих 0, значимых 0, стилистических 0" in empty, empty
