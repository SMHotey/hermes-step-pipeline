# -*- coding: utf-8 -*-
"""Вердикт ревью, сверенный с классами находок (план 13, шаг S1).

Правило шага: круг правок открывает только блокирующая находка. `REQUEST_CHANGES` без
единой блокирующей шаг не держит — шаг принимается с замечаниями, а замечания уходят
строками реестра находок плана со статусом `не блокирует`. Отчёт без раздела «Находки» и
отчёт без меток классов — ошибки формата: круг не заказывается, шаг остаётся открытым,
причина видна в решении стража.

Тесты делятся на три слоя: разбор отчёта (`report_finding_classes`), решение
(`gate.decide` на синтетическом состоянии) и сквозной тик (`gate.run_gate` на временном
проекте) — последний ловит то, чего не видно в чистой функции: журнал, реестр, тему
коммита и отложенный коммит.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import config as config_mod
import gate
from engine_kit import make_project
from stub_hermes import SCHEMA

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1.md"

# Отчёт с замечаниями вне блокирующих: значимая и стилистическая находки плюс один пункт
# без метки. Такой отчёт и есть предмет шага: правок он больше не заказывает.
REPORT_NOTES = """## ВЕРДИКТ: REQUEST_CHANGES

Объект ревью: коммит `{{HEAD}}`

## Классы находок

Блокирующих 0, значимых 1, стилистических 1.

## Находки

- [значимая, вне объёма шага] `app/models/invoice.py:41` — нет теста на дубль номера
- [стилистическая] `app/models/invoice.py:12` — имя переменной не по смыслу
- пункт совсем без метки, но осмысленный
"""

# Реестр плана в том виде, в каком его кладёт владелец: привязка к версии плана стоит
# ДО таблицы, и запись стражем не имеет права её стирать.
REGISTRY = """plan_sha256: 0f0e0d0c0b0a

| ID | Шаг | Класс | Находка | Что сделать | Статус |
|---|---|---|---|---|---|
| F1 | S0 | замысел | прежнее замечание плана | переписать §2 | закрыто |
"""


def ctx(**kw) -> dict:
    """Синтетическое состояние стража — то же, что отдаёт `build_ctx`."""
    base = {"next": dict(STEP), "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "verdict": None, "journal_closed": False, "plan_id": "1"}
    base.update(kw)
    return base


def card(kind: str, status: str = "done", n: str = "", short: str = "1") -> dict:
    key = f"planloop:1:S1:{kind}" + (f":{n}" if n else "")
    return {"id": f"t_{short}", "status": status, "key": key, "result": "", "error": ""}


def review_cards() -> list[dict]:
    """Карточки состояния «шаг сделан, ревью вернуло отчёт»."""
    return [card("step", "done", short="1"), card("review", "done", n="1", short="2")]


def write_report(tmp_path: Path, body: str, name: str = "1-S1.md") -> Path:
    """Отчёт ревью по указанному тексту: путь отдаётся решению как есть."""
    report = tmp_path / name
    report.write_text(body, encoding="utf-8")
    return report


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


def git(root: Path, *args: str) -> str:
    """Команда git в проекте; отказ — падение теста, а не тихий пропуск шага."""
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr or res.stdout
    return res.stdout.strip()


def setup_project(tmp_path: Path, db: Path, report: str = REPORT_NOTES) -> Path:
    """Проект в состоянии «ревью вернуло отчёт»: постановки шага и следующего, две карточки.

    Постановка N+1 нужна, чтобы шаг мог закрыться: без неё гейт закрытия держит шаг
    (`close_block_reason`), и сквозной тик до журнала не доходит. Проект под git: отчёт
    ссылается на настоящий коммит (`{{HEAD}}`), иначе гейт существования коммита
    остановит закрытие — проверка ссылки на коммит к этому шагу отношения не имеет.
    """
    root = make_project(tmp_path / "e2e")
    (root / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    prompts = root / "docs" / "prompts"
    for sid in ("S1", "S2"):
        (prompts / f"2026-11-03_1-{sid}_prompt.md").write_text(f"# Промт {sid}\n",
                                                              encoding="utf-8")
    insert_card(db, "t_step_s1", "planloop:1:S1:step", created_at=1)
    insert_card(db, "t_rev_s1", "planloop:1:S1:review:1", created_at=2)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    (root / REPORT_REL).write_text(report.replace("{{HEAD}}",
                                                 git(root, "rev-parse", "--short", "HEAD")),
                                  encoding="utf-8")
    return root


def journal_rows(root: Path) -> list[list[str]]:
    """Строки журнала плана по шагу S1 (ячейки без внешних `|`)."""
    text = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in text.splitlines() if line.strip().startswith("|")]
    return [row for row in rows if row and row[1].startswith("S1")]


def test_classes_counted_by_label():
    """Счёт идёт по метке в начале пункта, а не по слову «блокирующая» в тексте."""
    text = "\n".join([
        "## ВЕРДИКТ: REQUEST_CHANGES", "",
        "## Находки", "",
        "- [блокирующая] `gate.py:10` — вердикт не сверяется с классами",
        "- [блокирующая] `gate.py:20` — вторая блокирующая находка",
        "- [значимая] `gate.py:30` — лог не пишется там, где обязателен",
        "- [стилистическая] `gate.py:40` — имя переменной не по смыслу",
        "- пункт вовсе без метки, но осмысленный",
    ])
    classes = gate.report_finding_classes(text)
    assert (classes["blocking"], classes["significant"], classes["style"]) == (2, 1, 1)
    assert classes["labeled"] == 4
    assert classes["unlabeled"] == 1
    assert classes["section"] is True
    assert classes["read"] is True

    # Живая форма без заголовка «Находки»: подразделы-классы сами задают класс пунктам.
    nested = gate.report_finding_classes("\n".join([
        "## ВЕРДИКТ: REQUEST_CHANGES", "",
        "### Блокирующие", "",
        "1. `app/step.py:12` — проверка не покрыта тестом", "",
        "### Значимые", "",
        "- `app/step.py:44` — нет лога ошибки"]))
    assert (nested["blocking"], nested["significant"]) == (1, 1)
    assert nested["unlabeled"] == 0
    assert nested["section"] is True


def test_sublabel_after_slash_is_accepted():
    """Подметка после дроби едет в `sublabels`; незнакомая подметка — не ошибка."""
    text = "\n".join([
        "## Находки", "",
        "- [блокирующая/тест-недетерминирован] `tests/x.py:3` — тест мигает на слоте",
        "- [значимая/вне-объёма-шага] `gate.py:99` — лог не пишется",
    ])
    classes = gate.report_finding_classes(text)
    assert classes["blocking"] == 1
    assert classes["significant"] == 1
    assert classes["sublabels"] == {"тест-недетерминирован": 1, "вне-объёма-шага": 1}
    assert classes["unlabeled"] == 0


def test_status_label_and_comma_form_are_not_classes():
    """`[закрыта]` — статус, `[значимая, вне объёма шага]` — значимая: словарь узкий."""
    text = "\n".join([
        "## Находки", "",
        "- [закрыта] `gate.py:140` — находка статуса, а не класса",
        "- [вне объёма] `gate.py:150` — метка вне словаря классов",
        "- [значимая, вне объёма шага] `gate.py:99` — собственно значимая",
    ])
    classes = gate.report_finding_classes(text)
    assert classes["significant"] == 1
    assert classes["labeled"] == 1
    assert classes["unlabeled"] == 2
    assert classes["sublabels"] == {}


def test_blocking_finding_orders_fix_card(tmp_path):
    """Блокирующая находка по-прежнему заказывает карточку правок — первый круг."""
    report = write_report(tmp_path, "## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n"
                                    "- [блокирующая] `app/sql.py:14` — индекс не создан\n")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(report)}))
    assert d["action"] == "create_fix"
    assert d["round"] == 1
    assert d["parent"] == "t_2"


def test_request_changes_without_blocking_closes_step_with_remarks(tmp_path):
    """Блокирующих нет — шаг закрывается; `remarks` = значимые + стилистические."""
    report = write_report(tmp_path, REPORT_NOTES.replace("{{HEAD}}", "a1b2c3d"))
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(report)}))
    assert d["action"] == "close_step"
    assert d["remarks"] == 2
    assert d["unlabeled"] == 1
    assert "reason" not in d             # у закрытия причины нет, как и было


def test_remarks_go_to_plan_findings_registry(tmp_path, hermes_stub, monkeypatch):
    """Замечания закрытого шага — строками реестра плана: статус, класс, текст, ID."""
    root = setup_project(tmp_path, hermes_stub)
    registry = root / "docs" / "reviews" / "plan-1-findings.md"
    registry.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")

    res = gate.run_gate(p, notify_fn=lambda text: None)
    assert res["decision"]["action"] == "close_step"

    rows = gate.load_findings(registry)
    added = [row for row in rows if row["step"] == "S1"]
    assert [row["id"] for row in added] == ["F2", "F3"]
    assert [row["status"] for row in added] == ["не блокирует", "не блокирует"]
    assert {row["kind"] for row in added} == {"стиль"}
    assert added[0]["text"].startswith("[значимая] app/models/invoice.py:41")
    assert "нет теста на дубль номера" in added[0]["text"]
    assert added[1]["text"].startswith("[стилистическая] app/models/invoice.py:12")
    assert all("принято при закрытии шага" in row["action"] for row in added)
    # Пункт без метки строкой реестра не становится: класса у него нет.
    assert len(rows) == 3


def test_registry_preamble_is_preserved(tmp_path, hermes_stub, monkeypatch):
    """Привязка реестра к версии плана (`plan_sha256:`) переживает запись стражем."""
    root = setup_project(tmp_path, hermes_stub)
    registry = root / "docs" / "reviews" / "plan-1-findings.md"
    registry.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))

    gate.run_gate(gate.resolve_paths(config_mod.load(root), "1"), notify_fn=lambda text: None)

    lines = registry.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "plan_sha256: 0f0e0d0c0b0a"
    header = next(index for index, line in enumerate(lines) if line.startswith("| ID |"))
    assert header > 0
    assert gate.load_findings(registry)[0]["id"] == "F1"      # прежняя строка цела


def test_report_without_labels_stops_as_format_error(tmp_path):
    """Нет раздела «Находки» или нет меток класса — стоп формата, а не круг правок."""
    no_section = write_report(tmp_path, "## ВЕРДИКТ: REQUEST_CHANGES\n\n"
                                        "Блокирующих нет, править нечего.\n", "no-section.md")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(no_section)}))
    assert d["action"] == "stop"
    assert "нет раздела «Находки»" in d["reason"]
    assert "формат" in d["reason"]
    assert "метками" in d["details"]
    assert d.get("remarks") in (None, 0)

    no_labels = write_report(tmp_path, "## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n"
                                       "- тест не утверждает значение\n- лог не пишется\n",
                             "no-labels.md")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(no_labels)}))
    assert d["action"] == "stop"
    assert "ни одной метки класса" in d["reason"]
    assert "поправь отчёт" in d["reason"]


def test_approve_with_blocking_finding_stops(tmp_path):
    """Обратное расхождение тоже видно: APPROVE при блокирующей находке — стоп."""
    report = write_report(tmp_path, "## ВЕРДИКТ: APPROVE\n\n## Находки\n\n"
                                    "- [блокирующая] `app/sql.py:14` — индекс не создан\n")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "APPROVE", "path": str(report)}))
    assert d["action"] == "stop"
    assert "APPROVE расходится с классами" in d["reason"]
    assert "блокирующих находок 1" in d["reason"]
    assert "есть блокирующая — REQUEST_CHANGES" in d["reason"]


def test_journal_and_tick_message_name_remarks(tmp_path, hermes_stub, monkeypatch):
    """Журнал, тема коммита, сообщение тика и отложенный коммит несут одну фразу."""
    root = setup_project(tmp_path, hermes_stub)
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")
    subjects: list[str] = []
    says: list[str] = []

    def fake_commit(paths, files, message):
        subjects.append(message)
        return True, "abc1234"

    res = gate.run_gate(p, notify_fn=says.append, commit_fn=fake_commit)

    phrase = "принято с замечаниями, находок вне блокирующих: 2"
    assert res["decision"]["action"] == "close_step"
    assert phrase in res["detail"]
    assert any(phrase in s for s in says), says
    assert subjects and phrase in subjects[0]
    row = journal_rows(root)
    assert len(row) == 1, row
    assert row[0][-1].upper().startswith("ЗАКРЫТ")
    assert phrase in row[0][-2]
    assert "APPROVE" not in row[0][-2]

    # Отложенный коммит помнит ту же тему: в состоянии лежит `message`, а не APPROVE.
    other = setup_project(tmp_path / "pending", hermes_stub)
    monkeypatch.setenv("PIPELINE_REPO", str(other))
    held = gate.resolve_paths(config_mod.load(other), "1")
    gate.run_gate(held, notify_fn=lambda text: None,
                  commit_fn=lambda paths, files, message: (False, "дерево занято"))
    pending = gate.load_state(held).get("pending_commit") or {}
    assert phrase in str(pending.get("message"))
    retried: list[str] = []

    def fake_retry(paths, files, message):
        retried.append(message)
        return True, "abc1234"

    monkeypatch.setattr(gate, "commit_and_push", fake_retry)
    assert gate.retry_pending(held)["applied"] is True
    assert retried and phrase in retried[0]


def test_review_card_requires_classes_block(project: Path):
    """Карточка ревью требует счёта классов и называет последствия для вердикта."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    body = gate.review_card_body(p, STEP, 1, REPORT_REL, None)
    assert "Классы находок" in body
    assert "[блокирующая]" in body and "[стилистическая]" in body
    assert "вердикт обязан совпадать с классами" in body.lower()
    assert "ошибка формата" in body
    # Страховка `test_review_card.py`: строка с «коммит» и «сверять» ровно одна.
    both = [line for line in body.splitlines() if "коммит" in line and "сверять" in line]
    assert len(both) == 1, both
