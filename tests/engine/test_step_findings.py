# -*- coding: utf-8 -*-
"""Реестр находок шага (план 13, S2): память круга правок.

До шага ревью кода помнило находки только в отчёте, который каждый круг перезаписывался:
исполнитель закрывал F1, ревьюер повторял ту же F1 цитатой, и конвейер шёл на второй круг
по уже сделанному. Здесь проверяется реестр `step-<N>-<шаг>-findings.md`: его заводит
страж из отчёта, ведёт ревьюер фактом, и решение по кругу принимается по открытым строкам,
а не по вердикту отчёта.

Слои тестов те же, что у соседей: путь и формат файла (`add_findings`/`load_findings`),
разбор отчёта в строки реестра, сквозной тик (`gate.run_gate` на временном проекте) и
живой прогон стража отдельным процессом для битого реестра.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, make_project
from stub_hermes import SCHEMA

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1.md"
REGISTRY_REL = "docs/reviews/step-1-S1-findings.md"
HEADER = "| ID | Класс | Находка | Что сделать | Статус |"

# Находки отчёта первого круга: одна блокирующая (круг держит) и одна значимая (нет).
F1_TEXT = "`step_pipeline/engine/gate.py` — реестр шага не заводится на первом круге"
F2_TEXT = "`docs/PLAN-FORMAT.md` — формат реестра шага не описан"

REPORT_BLOCKING = f"""## ВЕРДИКТ: REQUEST_CHANGES

## Классы находок

Блокирующих 1, значимых 1, стилистических 0.

## Находки

- [блокирующая] {F1_TEXT}
- [значимая] {F2_TEXT}
"""

# Тот же отчёт другими чернилами: та же находка в жирном шрифте и без бэктиков. Сверка
# идёт по нормализованному тексту, поэтому новых строк он не даёт.
REPORT_QUOTES = """## ВЕРДИКТ: REQUEST_CHANGES

## Находки

- [блокирующая] step_pipeline/engine/gate.py — **реестр шага не заводится на первом круге**
- [значимая] docs/PLAN-FORMAT.md — **формат реестра шага не описан**
"""

# Отчёт без единой метки класса: файл реестра он не создаёт (и это не ошибка формата —
# ошибку формата ловит решение, а `ensure_step_findings` просто не заводит строк).
REPORT_UNLABELED = """## ВЕРДИКТ: REQUEST_CHANGES

## Находки

- реестр шага не заводится на первом круге правок
- формат реестра шага не описан в документе
"""

# Отчёт круга 2: закрытая находка повторена, новая тема — вне контракта шага.
REPORT_REPEAT_WITH_NEW = """## ВЕРДИКТ: REQUEST_CHANGES

## Классы находок

Блокирующих 0, значимых 1, стилистических 0.

## Находки

- [значимая, вне объёма шага] `app/api/invoices.py:31` — нет теста на пустой список
"""

REPORT_APPROVE = """## ВЕРДИКТ: APPROVE

## Классы находок

Блокирующих 0, значимых 0, стилистических 0.

## Находки

нет
"""


def ctx(**kw) -> dict:
    """Синтетическое состояние стража — то же, что отдаёт `build_ctx`."""
    base = {"next": dict(STEP), "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "verdict": None, "journal_closed": False, "plan_id": "1",
            "step_findings": [], "step_findings_exists": False, "step_findings_error": ""}
    base.update(kw)
    return base


def card(kind: str, status: str = "done", n: str = "", short: str = "1") -> dict:
    key = f"planloop:1:S1:{kind}" + (f":{n}" if n else "")
    return {"id": f"t_{short}", "status": status, "key": key, "result": "", "error": ""}


def review_cards() -> list[dict]:
    """Карточки состояния «шаг сделан, ревью вернуло отчёт»."""
    return [card("step", "done", short="1"), card("review", "done", n="1", short="2")]


def row(kind: str, text: str, status: str | None = None) -> dict:
    """Строка реестра шага: класс, текст, действие и статус по классу."""
    return {"kind": kind, "text": text, "action": gate.STEP_FINDING_ACTION,
            "status": status if status is not None else gate.STEP_FINDING_STATUS[kind]}


def registry_text(rows: list[tuple[str, str, str]], step: str = "S1") -> str:
    """Реестр готовой таблицей: (ID, класс, статус) — текст находки берётся по классу."""
    text_of = {"блокирующая": F1_TEXT, "значимая": F2_TEXT}
    lines = [f"# Реестр находок шага 1-{step}", "", HEADER, "|---|---|---|---|---|"]
    for fid, kind, status in rows:
        lines.append(f"| {fid} | {kind} | {text_of.get(kind, 'находка')} | закрыть правкой "
                     f"| {status} |")
    return "\n".join(lines) + "\n"


def registry_rows(path: Path) -> list[dict]:
    """Строки реестра шага так, как их читает страж."""
    return gate.load_findings(path, gate.STEP_FINDINGS_HEADER, gate.STEP_FINDINGS_KEYS,
                              kinds=gate.STEP_FINDING_KINDS)


def insert_card(db: Path, card_id: str, key: str, status: str = "done",
                created_at: int = 0) -> None:
    """Карточка в поддельной борде (колонки — `stub_hermes.SCHEMA`)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
            " completed_at, result, last_failure_error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (card_id, "карточка", status, "demo", key, created_at, "", "", created_at))
        con.commit()
    finally:
        con.close()


def keys_of(db: Path) -> set[str]:
    """Ключи идемпотентности всех карточек поддельной борды."""
    return {c["key"] for c in cards(db)}


def stand(tmp_path: Path, db: Path, monkeypatch, *, report: str = REPORT_BLOCKING,
          registry: str | None = None) -> tuple[Path, Any]:
    """Проект в состоянии «шаг сделан, ревью вернуло отчёт».

    Постановки шага и следующего на месте: без постановки N+1 гейт закрытия держит шаг
    (`close_block_reason`), и сквозной тик до журнала не доходит.
    """
    root = make_project(tmp_path / "e2e")
    prompts = root / "docs" / "prompts"
    for sid in ("S1", "S2"):
        (prompts / f"2026-11-16_1-{sid}_prompt.md").write_text(f"# Промт {sid}\n",
                                                               encoding="utf-8")
    insert_card(db, "t_step_s1", "planloop:1:S1:step", created_at=1)
    insert_card(db, "t_rev_s1", "planloop:1:S1:review:1", created_at=2)
    (root / REPORT_REL).write_text(report, encoding="utf-8")
    if registry is not None:
        (root / REGISTRY_REL).write_text(registry, encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    return root, gate.resolve_paths(config_mod.load(root), "1")


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def journal_rows(root: Path) -> list[list[str]]:
    """Строки журнала плана по шагу S1 (ячейки без внешних `|`)."""
    text = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in text.splitlines() if line.strip().startswith("|")]
    return [r for r in rows if r and r[1].startswith("S1")]


def plan_registry_rows(root: Path) -> list[dict]:
    """Строки реестра находок плана (пусто — файла нет)."""
    return gate.load_findings(root / "docs" / "reviews" / "plan-1-findings.md")


def spy_cards() -> tuple[list[dict], Any]:
    """Тела карточек, которые заказывает страж.

    Тело в поддельной борде не хранится (её колонки — колонки настоящего Hermes, описания
    у них нет), а проверять надо именно текст, который увидит исполнитель. Спай передаётся
    в `run_gate(create_card=…)` явно: умолчание аргумента привязано при импорте, и
    подмена `gate._create_card` его не меняет.
    """
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


def first_fix(root: Path, p, db: Path, spy) -> None:
    """Довести стенд до состояния «круг правок 1 заказан и закрыт, заказано ревью 2».

    Через настоящий тик, а не руками: реестр шага заводит страж в ветке `create_fix`, и
    стенд обязан проверять именно этот путь.
    """
    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    complete(db, ":fix:1", result="F1: исправлено — step_pipeline/engine/gate.py — "
                                  "pytest tests/engine/test_step_findings.py → 12 passed")
    res = gate.run_gate(p, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_review", res["decision"]
    complete(db, ":review:2")


def test_registry_path_and_title(project: Path):
    """Путь `docs/reviews/step-1-S1-findings.md`, заголовок и шапка — как требует план."""
    p = gate.resolve_paths(config_mod.load(project), "1")
    path = gate.step_findings_path(p, "S1")
    assert gate.step_findings_rel(p, "S1") == REGISTRY_REL
    assert path == Path(p.review_dir) / "step-1-S1-findings.md"

    gate.add_findings(path, [row("блокирующая", F1_TEXT)], gate.STEP_FINDINGS_HEADER,
                      gate.STEP_FINDINGS_KEYS, "шага", "step-",
                      kinds=gate.STEP_FINDING_KINDS)
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# Реестр находок шага 1-S1"
    assert HEADER in text
    assert [(r["id"], r["kind"], r["status"]) for r in registry_rows(path)] == [
        ("F1", "блокирующая", "открыто")]


def test_step_registry_class_dictionary_is_own(project: Path):
    """Словарь классов реестра шага свой: `замысел` и `блокирующая` не взаимозаменяемы."""
    p = gate.resolve_paths(config_mod.load(project), "1")
    path = gate.step_findings_path(p, "S1")
    gate.add_findings(path, [row("блокирующая", F1_TEXT)], gate.STEP_FINDINGS_HEADER,
                      gate.STEP_FINDINGS_KEYS, "шага", "step-",
                      kinds=gate.STEP_FINDING_KINDS)
    rows = registry_rows(path)
    assert [r["kind"] for r in rows] == ["блокирующая"]
    # Статус по умолчанию — из класса: держит шаг только блокирующая.
    assert rows[0]["status"] == "открыто"

    # Чужой класс в реестре шага — GateError с путём: читать такую строку нечем.
    foreign = {"kind": "замысел", "text": "строка чужого словаря", "action": "переписать §2",
               "status": "открыто"}
    with pytest.raises(gate.GateError) as exc:
        gate.add_findings(path, [foreign], gate.STEP_FINDINGS_HEADER, gate.STEP_FINDINGS_KEYS,
                          "шага", "step-", kinds=gate.STEP_FINDING_KINDS)
    assert str(path) in str(exc.value)

    # Обратная сторона: реестр плана по-прежнему берёт `замысел` и не берёт класс находки.
    plan_path = gate.plan_findings_path(p)
    gate.add_findings(plan_path, [{"step": "S1", "kind": "замысел", "text": "тема плана",
                                   "action": "переписать §2"}])
    assert [r["kind"] for r in gate.load_findings(plan_path)] == ["замысел"]
    with pytest.raises(gate.GateError):
        gate.add_findings(plan_path, [{"step": "S1", "kind": "блокирующая",
                                       "text": "класс находки в реестре плана",
                                       "action": "не должен пройти"}])


def test_registry_created_once_from_report(tmp_path: Path, hermes_stub: Path, monkeypatch):
    """Первый `REQUEST_CHANGES` заводит файл из отчёта; повторы строк не добавляют."""
    root, p = stand(tmp_path, hermes_stub, monkeypatch)
    seen, spy = spy_cards()
    notes: list[str] = []

    res = gate.run_gate(p, notify_fn=notes.append, create_card=spy)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    assert "planloop:1:S1:fix:1" in {c["key"] for c in seen}
    path = gate.step_findings_path(p, "S1")
    assert path.is_file()
    rows = registry_rows(path)
    assert [(r["id"], r["kind"], r["status"]) for r in rows] == [
        ("F1", "блокирующая", "открыто"), ("F2", "значимая", "не блокирует")]
    assert "реестр шага: открытых 1, всего 2" in "\n".join(notes)

    # Повторный вызов на том же отчёте: ни второй строки, ни новых ID.
    again = gate.ensure_step_findings(p, STEP, REPORT_REL)
    assert [r["id"] for r in again] == ["F1", "F2"]
    # Второй отчёт цитирует те же находки другими чернилами — строк по-прежнему две.
    (root / REPORT_REL).write_text(REPORT_QUOTES, encoding="utf-8")
    same = gate.ensure_step_findings(p, STEP, REPORT_REL)
    assert [r["id"] for r in same] == ["F1", "F2"]
    assert [r["status"] for r in same] == ["открыто", "не блокирует"]

    # Отчёт без меток классов файла не заводит: пустой реестр читался бы как «находок нет».
    path.unlink()
    (root / REPORT_REL).write_text(REPORT_UNLABELED, encoding="utf-8")
    assert gate.ensure_step_findings(p, STEP, REPORT_REL) == []
    assert not path.is_file()
    # Карточка правок первого круга всё равно есть: круг открыла блокирующая отчёта.
    assert "planloop:1:S1:fix:1" in {c["key"] for c in seen}


def test_second_round_sees_only_open_findings(tmp_path: Path, hermes_stub: Path,
                                              monkeypatch):
    """Круг 2 видит только открытые строки: F1 на месте, закрытая F2 — нет."""
    root, p = stand(tmp_path, hermes_stub, monkeypatch)
    seen, spy = spy_cards()

    first_fix(root, p, hermes_stub, spy)
    # Ревьюер закрыл значимую фактом и повторил блокирующую в отчёте круга 2.
    (root / REGISTRY_REL).write_text(registry_text([("F1", "блокирующая", "открыто"),
                                                    ("F2", "значимая", "закрыто")]),
                                     encoding="utf-8")
    (root / REPORT_REL).write_text(REPORT_QUOTES, encoding="utf-8")

    d = gate.run_gate(p, notify_fn=quiet, create_card=spy)["decision"]
    assert d["action"] == "create_fix" and d["round"] == 2, d
    body = body_of(seen, "planloop:1:S1:fix:2")
    assert "- F1 (блокирующая): " in body
    assert "F2" not in body
    assert REGISTRY_REL in body
    assert "ОТКРЫТЫЕ НАХОДКИ РЕЕСТРА ШАГА" in body
    assert "Реестр находок шага держит 1 открытых находок" in body


def test_closed_finding_does_not_reopen_round(tmp_path: Path, hermes_stub: Path,
                                             monkeypatch):
    """Закрытая строка реестра круга не даёт: шаг закрывается, `:fix:2` не появляется."""
    root, p = stand(tmp_path, hermes_stub, monkeypatch, report=REPORT_BLOCKING)
    seen, spy = spy_cards()

    first_fix(root, p, hermes_stub, spy)
    (root / REGISTRY_REL).write_text(registry_text([("F1", "блокирующая", "закрыто"),
                                                    ("F2", "значимая", "закрыто")]),
                                     encoding="utf-8")
    (root / REPORT_REL).write_text(REPORT_APPROVE, encoding="utf-8")

    d = gate.run_gate(p, notify_fn=quiet, create_card=spy)["decision"]
    assert d["action"] == "close_step", d
    assert "planloop:1:S1:fix:2" not in keys_of(hermes_stub)
    rows = journal_rows(root)
    assert len(rows) == 1 and rows[0][4] == "ЗАКРЫТ", rows


def test_new_topic_goes_to_plan_registry_without_round(tmp_path: Path, hermes_stub: Path,
                                                       monkeypatch):
    """Новая тема вне контракта шага — строка реестра плана, а не круг правок."""
    root, p = stand(tmp_path, hermes_stub, monkeypatch, report=REPORT_BLOCKING)
    seen, spy = spy_cards()

    first_fix(root, p, hermes_stub, spy)
    (root / REGISTRY_REL).write_text(registry_text([("F1", "блокирующая", "закрыто")]),
                                     encoding="utf-8")
    (root / REPORT_REL).write_text(REPORT_REPEAT_WITH_NEW, encoding="utf-8")

    d = gate.run_gate(p, notify_fn=quiet, create_card=spy)["decision"]
    assert d["action"] == "close_step" and d["remarks"] == 1, d
    assert "planloop:1:S1:fix:2" not in keys_of(hermes_stub)
    added = [r for r in plan_registry_rows(root) if r["step"] == "S1"]
    assert [(r["kind"], r["status"]) for r in added] == [("стиль", "не блокирует")], added
    assert "нет теста на пустой список" in added[0]["text"]


def test_approve_with_open_blocking_does_not_close(tmp_path: Path, hermes_stub: Path,
                                                   monkeypatch):
    """APPROVE в отчёте при открытой F1: круг правок, а не закрытие шага."""
    report = tmp_path / "approve.md"
    report.write_text(REPORT_APPROVE, encoding="utf-8")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "APPROVE", "path": str(report),
                                 "commit": "a1b2c3d"},
                        step_findings=[dict(row("блокирующая", F1_TEXT), id="F1")]))
    assert d["action"] == "create_fix", d
    assert d["round"] == 1
    assert d["registry_open"] == ["F1"]
    assert d["parent"] == "t_2"

    # Живой тик: тот же реестр и APPROVE на диске — карточка правок, журнал не тронут.
    root, p = stand(tmp_path, hermes_stub, monkeypatch, report=REPORT_APPROVE,
                    registry=registry_text([("F1", "блокирующая", "открыто")]))
    res = gate.run_gate(p, notify_fn=quiet)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    assert "planloop:1:S1:fix:1" in keys_of(hermes_stub)
    assert journal_rows(root)[0][4] == "открыт", journal_rows(root)


def test_approve_with_significant_finding_closes(tmp_path: Path, hermes_stub: Path,
                                                 monkeypatch):
    """`не блокирует` шаг не держит: шаг закрывается, строка журнала на месте."""
    root, p = stand(tmp_path, hermes_stub, monkeypatch, report=REPORT_APPROVE,
                    registry=registry_text([("F1", "значимая", "не блокирует")]))

    res = gate.run_gate(p, notify_fn=quiet)
    assert res["decision"]["action"] == "close_step", res["decision"]
    rows = journal_rows(root)
    assert len(rows) == 1 and rows[0][4] == "ЗАКРЫТ", rows
    assert "planloop:1:S1:fix:1" not in keys_of(hermes_stub)
    assert [r for r in plan_registry_rows(root) if r["step"] == "S1"] == []


def test_broken_registry_error_line_reaches_card_and_comment(tmp_path: Path, hermes_stub: Path,
                                                            monkeypatch):
    """Битый реестр — в карточке правок и в комментарии строка с причиной (§4.7).

    До решения такой тик не доходит: инцидентный стоп ставится раньше
    (`test_broken_registry_stops_and_names_path`), поэтому проверяется сам построитель.
    Молча пропущенный блок исполнитель прочёл бы как «открытых находок нет» и закрыл круг
    без правки.
    """
    _root, p = stand(tmp_path, hermes_stub, monkeypatch, report=REPORT_BLOCKING,
                     registry="мусор без шапки таблицы\n")
    body = gate.fix_card_body(p, STEP, 1, REPORT_REL, 6, report_verdict="REQUEST_CHANGES")
    assert gate.STEP_FINDINGS_UNREADABLE in body, body
    assert Path(REGISTRY_REL).name in body, body
    # Строка ошибки не выдаёт себя за открытые находки: заголовка блока нет.
    assert "ОТКРЫТЫЕ НАХОДКИ РЕЕСТРА ШАГА" not in body

    comment = gate.fix_comment_body(p, STEP, 1, REPORT_REL, "t_rev_s1")
    assert gate.STEP_FINDINGS_UNREADABLE in comment, comment
    # Тот же блок первым — до выжимки находок отчёта (как и открытые строки).
    assert comment.index(gate.STEP_FINDINGS_UNREADABLE) < comment.index("Блокирующие находки")


def test_broken_registry_stops_and_names_path(tmp_path: Path, hermes_stub: Path,
                                              monkeypatch):
    """Битый реестр — инцидентный стоп с путём в причине: шаг не закрыть вслепую."""
    root, p = stand(tmp_path, hermes_stub, monkeypatch,
                    report=REPORT_APPROVE, registry="мусор без шапки таблицы\n")

    res = gate.run_gate(p, notify_fn=quiet)
    decision = res["decision"]
    assert decision["action"] == "stop", decision
    assert Path(REGISTRY_REL).name in decision["reason"], decision["reason"]
    assert journal_rows(root)[0][4] == "открыт", journal_rows(root)

    # Живой прогон стража отдельным процессом — так его зовёт cron.
    env = {**os.environ, "PIPELINE_REPO": str(root)}
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        env.pop(name, None)
    out = subprocess.run([sys.executable, str(root / ".pipeline" / "gate.py"),
                          "--status", "--plan", "1"], cwd=str(root), capture_output=True,
                         text=True, encoding="utf-8", errors="replace", env=env)
    assert out.returncode == 0, out.stderr[-600:]
    assert REGISTRY_REL.split("/")[-1] in out.stdout, out.stdout[-600:]
