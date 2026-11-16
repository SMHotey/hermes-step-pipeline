# -*- coding: utf-8 -*-
"""Вердикт ревью, сверенный с классами находок (план 13, шаг S1).

Правило шага: круг правок открывает только блокирующая находка. `REQUEST_CHANGES` без
единой блокирующей шаг не держит — шаг принимается, замечания уходят строками реестра
находок плана со статусом `не блокирует`. Отчёт без раздела «Находки» и отчёт без меток
классов — ошибки формата: круг не заказывается, шаг остаётся открытым, причина видна
в решении стража.

Тесты делятся на три слоя: разбор отчёта (`report_finding_classes`), решение
(`gate.decide` на синтетическом состоянии) и сквозной тик (`gate.run_gate` на временном
проекте) — последнее ловит то, чего не видно в чистой функции: журнал, реестр, тему
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

# Отчёт с замечаниями вне блокирующих: значимая и стилистическая находки, ни одной
# блокирующей. Такой отчёт и есть предмет шага: правок он больше не заказывает.
REPORT_NOTES = """## ВЕРДИКТ: REQUEST_CHANGES

Объект ревью: коммит `{{HEAD}}`

## Классы находок

Блокирующих 0, значимых 1, стилистических 1.

## Находки

- [значимая, вне объёма шага] `app/models/invoice.py:41` — нет теста на дубль номера
- [стилистическая] `app/models/invoice.py:12` — имя переменной не по смыслу
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


def git(root: Path, *args: str) -> str:
    """Команда git в проекте; отказ — падение теста, а не тихий пропуск шага."""
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr or res.stdout
    return res.stdout.strip()


def journal_rows(root: Path) -> list[list[str]]:
    """Строки журнала плана по шагу S1 (ячейки без внешних `|`)."""
    text = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in text.splitlines() if line.strip().startswith("|")]
    return [row for row in rows if row and row[1].startswith("S1")]


def test_classes_counted_by_label():
    """Счёт идёт по метке в начале пункта; запятая и статус меткой класса не считаются."""
    text = "\n".join([
        "## ВЕРДИКТ: REQUEST_CHANGES", "",
        "## Находки", "",
        "- [блокирующая] `gate.py:10` — вердикт не сверяется",
        "- [блокирующая/тест-недетерминирован] `tests/x.py:3` — тест мигает",
        "- [значимая, вне объёма шага] `gate.py:99` — лог не пишется",
        "- [стилистическая] `gate.py:120` — имя не по смыслу",
        "- [закрыта] `gate.py:140` — находка статуса, а не класса",
        "- пункт вовсе без метки, но осмысленный",
    ])
    classes = gate.report_finding_classes(text)
    assert (classes["blocking"], classes["significant"], classes["style"]) == (2, 1, 1)
    assert classes["labeled"] == 4
    assert classes["unlabeled"] == 2          # `[закрыта]` и пункт без метки
    assert classes["sublabels"] == {"тест-недетерминирован": 1}
    assert classes["section"] is True
    assert classes["read"] is True


def test_classes_counted_by_section_without_label():
    """Подраздел класса внутри области даёт класс пунктам без своей метки (и без «Находки»)."""
    text = "\n".join([
        "## ВЕРДИКТ: REQUEST_CHANGES", "",
        "### Блокирующие", "",
        "1. `app/step.py:12` — проверка не покрыта тестом",
        "2. **[блокирующая]** `app/step.py:30` — своя метка сильнее подраздела", "",
        "### Значимые", "",
        "- `app/step.py:44` — нет лога ошибки",
    ])
    classes = gate.report_finding_classes(text)
    assert (classes["blocking"], classes["significant"], classes["style"]) == (2, 1, 0)
    assert classes["unlabeled"] == 0
    assert classes["section"] is True


def test_report_without_findings_section_is_format_stop(tmp_path):
    """Нет раздела находок — стоп формата: круг не заказывается, шаг открыт."""
    report = tmp_path / "1-S1.md"
    report.write_text("## ВЕРДИКТ: REQUEST_CHANGES\n\nБлокирующих нет, править нечего.\n",
                      encoding="utf-8")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(report)}))
    assert d["action"] == "stop"
    assert "формат" in d["reason"]
    assert "Находки" in d["reason"]
    assert "Находки" in d["details"] or "раздела" in d["details"]


def test_request_changes_without_labels_is_format_stop(tmp_path):
    """Раздел есть, меток класса нет — тоже стоп формата, а не круг правок."""
    report = tmp_path / "1-S1.md"
    report.write_text("## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n"
                      "- тест не утверждает значение\n- лог не пишется\n", encoding="utf-8")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(report)}))
    assert d["action"] == "stop"
    assert "метки" in d["reason"]
    assert "формат" in d["reason"]


def test_request_changes_with_one_blocking_creates_fix(tmp_path):
    """Блокирующая находка по-прежнему заказывает карточку правок."""
    report = tmp_path / "1-S1.md"
    report.write_text("## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n"
                      "- [блокирующая] `app/sql.py:14` — индекс не создан\n",
                      encoding="utf-8")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(report)}))
    assert d["action"] == "create_fix"
    assert d["round"] == 1


def test_request_changes_without_blocking_closes_step(tmp_path):
    """Блокирующих нет — шаг закрывается, замечания едут в реестр счётом."""
    report = tmp_path / "1-S1.md"
    report.write_text(REPORT_NOTES, encoding="utf-8")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "REQUEST_CHANGES", "path": str(report)}))
    assert d["action"] == "close_step"
    assert d["remarks"] == 2
    assert d["unlabeled"] == 0


def test_approve_with_blocking_stops(tmp_path):
    """Обратное расхождение тоже видно: APPROVE при блокирующей находке — стоп."""
    report = tmp_path / "1-S1.md"
    report.write_text("## ВЕРДИКТ: APPROVE\n\n## Находки\n\n"
                      "- [блокирующая] `app/sql.py:14` — индекс не создан\n", encoding="utf-8")
    d = gate.decide(ctx(cards=review_cards(),
                        verdict={"verdict": "APPROVE", "path": str(report)}))
    assert d["action"] == "stop"
    assert "APPROVE расходится" in d["reason"]
    assert "1" in d["reason"]


def test_remarks_written_to_plan_registry(tmp_path, hermes_stub, monkeypatch):
    """Замечания закрытого шага — строками реестра плана: статус `не блокирует`, преамбула цела."""
    root = setup_project(tmp_path, hermes_stub)
    registry = root / "docs" / "reviews" / "plan-1-findings.md"
    registry.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")

    res = gate.run_gate(p, notify_fn=lambda text: None)
    assert res["decision"]["action"] == "close_step"

    text = registry.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "plan_sha256: 0f0e0d0c0b0a"
    rows = gate.load_findings(registry)
    added = [row for row in rows if row["step"] == "S1"]
    assert len(added) == 2
    assert [row["status"] for row in added] == ["не блокирует", "не блокирует"]
    assert [row["id"] for row in added] == ["F2", "F3"]
    assert "[значимая]" in added[0]["text"]
    assert added[0]["text"].startswith("[значимая] app/models/invoice.py:41")
    assert "нет теста на дубль номера" in added[0]["text"]


def test_journal_row_says_accepted_with_remarks(tmp_path, hermes_stub, monkeypatch):
    """Журнал, тема коммита и сообщение тика несут одну фразу: «принято с замечаниями…»."""
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
    assert any(phrase in s for s in says), says
    assert phrase in res["detail"]
    assert subjects and phrase in subjects[0]
    row = journal_rows(root)
    assert len(row) == 1, row
    assert "ЗАКРЫТ" in row[0][-1].upper()
    assert "принято с замечаниями" in row[0][-2]


def test_pending_commit_keeps_remarks_subject(tmp_path, hermes_stub, monkeypatch):
    """Отложенный коммит не теряет тему: запись состояния помнит «принято с замечаниями»."""
    root = setup_project(tmp_path, hermes_stub)
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")

    gate.run_gate(p, notify_fn=lambda text: None,
                  commit_fn=lambda paths, files, message: (False, "дерево занято"))
    pending = gate.load_state(p).get("pending_commit") or {}
    assert "принято с замечаниями" in str(pending.get("message"))

    seen: list[str] = []

    def fake_commit(paths, files, message):
        seen.append(message)
        return True, "abc1234"

    monkeypatch.setattr(gate, "commit_and_push", fake_commit)
    assert gate.retry_pending(p)["applied"] is True
    assert seen and "принято с замечаниями" in seen[0]
    assert not (gate.load_state(p).get("pending_commit"))


def test_budget_unchanged_for_blocking(tmp_path):
    """Бюджет кругов не тронут: блокирующая находка едет по прежней лестнице `max_fix_rounds`."""
    report = tmp_path / "1-S1.md"
    report.write_text("## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n"
                      "- [блокирующая] `app/sql.py:14` — индекс не создан\n",
                      encoding="utf-8")
    verdict = {"verdict": "REQUEST_CHANGES", "path": str(report)}
    fresh = gate.decide(ctx(cards=review_cards(), verdict=verdict))
    assert (fresh["action"], fresh["round"]) == ("create_fix", 1)

    spent = [card("step", "done", short="1"),
             card("review", "done", n="1", short="2"),
             card("fix", "done", n="1", short="3"),
             card("review", "done", n="2", short="4")]
    d = gate.decide(ctx(cards=spent, verdict=verdict, max_fix_rounds=1))
    assert d["action"] == "stop"
    assert "лимит" in d["reason"]
