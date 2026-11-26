# -*- coding: utf-8 -*-
"""Карточка правок требует детерминированный тест, а не повтор прогона (план 13, шаг S3).

Замер 6-S3: круг 2 ввёл тест новых ветвей, круг 3 правил его же — `pytest.fail` внутри
цикла повторов зависел от загрузки машины (`4 passed in 7.82 s` против `24.84 s` до
правки). Один лишний круг, около 70 минут. Правило шага — текстовое: движок pytest за
исполнителя не запускает, он печатает требование в карточке правок и в роли.

Тесты держат две вещи: (1) требование трёх прогонов и запрет цикла повторов в тексте
карточки; (2) разбор классов отчёта печатается в карточке — исполнитель видит класс и
подметку находки, счёт считает разбор S1 (`report_finding_classes_at`), второй не пишем.

Шаг S4 дописывает третье: форма ответа исполнителя по каждому номеру реестра шага —
три метки в карточке и в роли, проверка формы чистой функцией и возврат карточки тем же
кругом на неформальный ответ (второго возврата нет).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config as config_mod
import gate
from engine_kit import cards, complete, make_project
from stub_hermes import SCHEMA

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

# Отчёт стенда возврата: одна блокирующая находка, значимых и стилистических нет.
REPORT_BLOCKING = """## ВЕРДИКТ: REQUEST_CHANGES

## Классы находок

Блокирующих 1, значимых 0, стилистических 0.

## Находки

- [блокирующая] F04 `step_pipeline/engine/gate.py` — форма ответа не проверяется
"""

F1_TEXT = "`step_pipeline/engine/gate.py` — форма ответа не проверяется"

# Ответ исполнителя прозой: форма не выполнена ни по одному номеру.
PROSE = "Пункт 1 закрыт: переписал по существу, см. дифф."

# Формальный ответ по открытой строке стенда — то, что круг обязан был написать.
FORMAL = ("F1: исправлено — step_pipeline/engine/gate.py — "
          "pytest tests/engine/test_fix_card.py → 9 passed")


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


# ─────────── форма ответа исполнителя по номерам реестра (13-S4) ───────────

def open_registry(project: Path):
    """Реестр шага с одной открытой находкой `F1` в проекте фикстуры `project`."""
    p = gate.resolve_paths(config_mod.load(project), "1")
    gate.add_findings(gate.step_findings_path(p, "S1"),
                      [{"kind": "блокирующая", "text": F1_TEXT,
                        "action": "проверять форму ответа", "status": "открыто"}],
                      gate.STEP_FINDINGS_HEADER, gate.STEP_FINDINGS_KEYS, "шага", "step-",
                      kinds=gate.STEP_FINDING_KINDS)
    return p


def test_fix_card_demands_three_answer_forms(project):
    """Карточка при открытом реестре печатает три формы дословно и следствие возврата.

    Без открытых строк блок не печатается: тело карточки обязано остаться прежним
    (на этом стоят `test_comments` и `test_prompt_fix_card`).
    """
    plain = stand(project)
    assert "ФОРМА ОТВЕТА" not in plain, "блок формы напечатан без реестра"

    p = open_registry(project)
    body = gate.fix_card_body(p, STEP, 1, REPORT_REL)
    assert "ФОРМА ОТВЕТА ПО КАЖДОМУ НОМЕРУ РЕЕСТРА" in body, body
    for form in gate.FIX_ANSWER_FORMS:
        assert f"F1: {form}" in body, f"нет метки формы «{form}» дословно"
    assert "коммит/файл" in body, "нет места под коммит/файл"
    assert "почему, с доказательством" in body, "нет требования доказательства"
    assert "в реестр плана" in body, "нет формы «вне объёма шага»"
    assert "kanban_complete" in body, "не сказано, куда писать ответ"
    assert "возвращается тем же кругом один раз" in body, "нет следствия неформального ответа"
    assert "- F1 (блокирующая): " in body, "блок реестра потерян"


def test_answer_form_problems_accepts_three_forms():
    """Таблица §4.2: три формы закрывают номер, всё остальное — незакрытый пункт."""
    accepted = [
        "F1: исправлено — gate.py — pytest tests/engine/test_fix_card.py → 7 passed",
        "- F1: не находка — класса нет в коде — grep -n 'def decide' gate.py",
        "F1: вне объёма шага — в реестр плана F4",
        "F1: вне объёма шага — docs/reviews/plan-13-findings.md F4",
    ]
    for line in accepted:
        assert gate.fix_answer_problems(["F1"], line) == [], line

    rejected = [
        "F1: исправлено",
        "F1: поправил, см. дифф",
        "пункт 1 закрыт, F1 исправлено",
        "F1: исправлено — gate.py — всё хорошо",
        "F1 — исправлено — gate.py — pytest -q",
    ]
    for line in rejected:
        assert gate.fix_answer_problems(["F1"], line) == ["F1"], line

    # Порядок — как у открытых строк реестра; повтор номера не ошибка.
    text = "F1: исправлено — gate.py — pytest -q\nF1: исправлено — gate.py — pytest -q"
    assert gate.fix_answer_problems(["F2", "F1"], text) == ["F2"]


def insert_card(db: Path, card_id: str, key: str, status: str = "done",
                assignee: str = "demo", created_at: int = 0) -> None:
    """Карточка в поддельной борде (колонки — `stub_hermes.SCHEMA`)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
            " completed_at, result, last_failure_error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (card_id, "карточка", status, assignee, key, created_at, "", "", created_at))
        con.commit()
    finally:
        con.close()


def spy_cards():
    """Тела карточек, которые заказывает страж (описания в поддельной борде нет)."""
    seen: list[dict] = []

    def spy(p, title, body, *args, **kwargs):
        seen.append({"title": title, "body": body,
                     "key": args[1] if len(args) > 1 else kwargs.get("key", "")})
        return gate._create_card(p, title, body, *args, **kwargs)  # noqa: SLF001 - стенд

    return seen, spy


def body_of(seen: list[dict], key: str) -> str:
    """Тело заказанной карточки по ключу идемпотентности."""
    for item in seen:
        if item["key"] == key:
            return item["body"]
    raise AssertionError(f"карточки {key} не было: {[c['key'] for c in seen]}")


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def stand_e2e(tmp_path: Path, db: Path, monkeypatch):
    """Проект в состоянии «шаг сделан, ревью вернуло блокирующую находку»."""
    root = make_project(tmp_path / "e2e")
    prompts = root / "docs" / "prompts"
    for sid in ("S1", "S2"):
        (prompts / f"2026-11-16_1-{sid}_prompt.md").write_text(f"# Промт {sid}\n",
                                                               encoding="utf-8")
    insert_card(db, "t_step_s1", "planloop:1:S1:step", created_at=1)
    insert_card(db, "t_rev_s1", "planloop:1:S1:review:1", assignee="reviewer", created_at=2)
    (root / REPORT_REL).write_text(REPORT_BLOCKING, encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    seen, spy = spy_cards()
    return root, gate.resolve_paths(config_mod.load(root), "1"), spy, seen


def to_return(p, db: Path, spy) -> None:
    """Довести стенд до «круг 1 заказан, закрыт прозой, заказан возврат»."""
    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    complete(db, ":fix:1", result=PROSE)
    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_fix_return", res["decision"]


def test_informal_answer_orders_return_card(tmp_path: Path, hermes_stub: Path, monkeypatch):
    """Ответ без формы — карточка возврата того же круга, а не новый круг."""
    _root, p, spy, seen = stand_e2e(tmp_path, hermes_stub, monkeypatch)
    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    complete(hermes_stub, ":fix:1", result=PROSE)

    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    decision = res["decision"]
    assert decision["action"] == "create_fix_return", decision
    assert decision["problems"] == ["F1"], decision
    assert res["detail"].startswith("возврат карточки правок: "), res["detail"]
    assert "без формы: F1" in res["detail"], res["detail"]

    keys = {card["key"]: card for card in cards(hermes_stub)}
    assert "planloop:1:S1:fix:1:return" in keys, sorted(keys)
    assert not [key for key in keys if key.endswith(":fix:2")], "заказан новый круг"
    returned = keys["planloop:1:S1:fix:1:return"]
    assert returned["status"] == "ready", returned
    assert returned["assignee"] == "demo", returned
    body = body_of(seen, "planloop:1:S1:fix:1:return")
    assert "ПУНКТЫ БЕЗ ФОРМЫ" in body and "- F1 (блокирующая): " in body, body
    assert "форма ответа не проверяется" in body, "текст находки из реестра не напечатан"
    assert "ЭТО НЕ НОВЫЙ КРУГ" in body, body
    for form in gate.FIX_ANSWER_FORMS:
        assert f"F1: {form}" in body, f"нет метки формы «{form}»"


def test_second_informal_answer_does_not_return(tmp_path: Path, hermes_stub: Path,
                                                monkeypatch):
    """Второй неформальный ответ уходит в ревью: возврат на круг один."""
    _root, p, spy, _seen = stand_e2e(tmp_path, hermes_stub, monkeypatch)
    to_return(p, hermes_stub, spy)
    complete(hermes_stub, ":fix:1:return", result=PROSE)

    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_review", res["decision"]
    returns = [key for key in {card["key"] for card in cards(hermes_stub)}
               if key.endswith(":return")]
    assert returns == ["planloop:1:S1:fix:1:return"], returns
    assert "planloop:1:S1:review:2" in {card["key"] for card in cards(hermes_stub)}


def test_answer_in_run_summary_counts(tmp_path: Path, hermes_stub: Path, monkeypatch):
    """Ответ только в `summary` (`kanban_complete(summary=…)`) — форма найдена, возврата нет."""
    _root, p, spy, _seen = stand_e2e(tmp_path, hermes_stub, monkeypatch)
    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    complete(hermes_stub, ":fix:1", summary=FORMAL)

    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_review", res["decision"]
    assert not [key for key in {card["key"] for card in cards(hermes_stub)}
                if key.endswith(":return")], "возврат заказан по ответу из summary"
