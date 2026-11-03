# -*- coding: utf-8 -*-
"""Решения стража: чистая функция `decide` на синтетических состояниях."""
from __future__ import annotations

from pathlib import Path

import config as config_mod
import gate

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}


def ctx(**kw) -> dict:
    base = {"next": dict(STEP), "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "verdict": None, "journal_closed": False, "plan_id": "1"}
    base.update(kw)
    return base


def card(kind: str, status: str = "done", n: str = "", short: str = "1") -> dict:
    key = f"planloop:1:S1:{kind}" + (f":{n}" if n else "")
    return {"id": f"t_{short}", "status": status, "key": key, "result": "", "error": ""}


def test_no_cards_creates_step():
    assert gate.decide(ctx())["action"] == "create_step"


def test_step_running_is_idle():
    d = gate.decide(ctx(cards=[card("step", "running")]))
    assert d["action"] == "idle"
    assert "в работе" in d["reason"]


def test_step_done_creates_review():
    d = gate.decide(ctx(cards=[card("step", "done")]))
    assert d["action"] == "create_review"
    assert d["attempt"] == 1
    assert d["parent"] == "t_1"


def test_failed_step_stops():
    d = gate.decide(ctx(cards=[card("step", "crashed")]))
    assert d["action"] == "stop"


def test_review_approve_closes_step():
    cards = [card("step", "done"), card("review", "done", n="1")]
    d = gate.decide(ctx(cards=cards, verdict={"verdict": "APPROVE", "path": "x", "commit": "a"}))
    assert d["action"] == "close_step"


def test_review_request_changes_creates_fix():
    cards = [card("step", "done"), card("review", "done", n="1")]
    d = gate.decide(ctx(cards=cards, verdict={"verdict": "REQUEST_CHANGES", "path": "x"}))
    assert d["action"] == "create_fix"
    assert d["round"] == 1


def test_second_request_changes_stops_with_findings(tmp_path):
    lines = ["VERDICT: REQUEST_CHANGES", "", "## Замечания", "",
             "- риск шага не проверяется при ручном допуске",
             "- вывод статуса врёт про hold"]
    report = tmp_path / "2-S1.md"
    report.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    cards = [card("step", "done", short="1"),
             card("review", "done", n="1", short="2"),
             card("fix", "done", n="1", short="3"),
             card("review", "done", n="2", short="4")]
    d = gate.decide(ctx(cards=cards, verdict={"verdict": "REQUEST_CHANGES",
                                        "path": str(report)}))
    assert d["action"] == "stop"
    assert "второй REQUEST_CHANGES" in d["reason"]
    details = d["details"]
    assert "риск шага не проверяется" in details
    assert "вывод статуса врёт про hold" in details
    assert "Что делать (выбери одно)" in details
    assert "переписать постановку" in details


def test_details_prefer_reviewer_recommendations(tmp_path):
    lines = ["## ВЕРДИКТ: REQUEST_CHANGES", "", "## Находки", "",
             "- правка файла из списка Запрещено", "",
             "## Рекомендации владельцу", "",
             "- откатить строку ACTION_LABELS в snapshot.py",
             "- поправить адрес в §11.2 на gate.py:1402-1418"]
    report = tmp_path / "2-S1.md"
    report.write_text(chr(10).join(lines), encoding="utf-8")
    cards = [card("step", "done", short="1"),
             card("review", "done", n="1", short="2"),
             card("fix", "done", n="1", short="3"),
             card("review", "done", n="2", short="4")]
    d = gate.decide(ctx(cards=cards, verdict={"verdict": "REQUEST_CHANGES",
                                        "path": str(report)}))
    details = d["details"]
    assert "Рекомендации ревьюера:" in details
    assert details.index("откатить строку") < details.index("Что делать")
    assert "правка файла из списка" not in details.split("Рекомендации")[1]

def test_review_findings_skips_short_and_markup(tmp_path):
    lines = ["# Заголовок", "- мало",
             "- это осмысленное замечание по коду", "| a | b |",
             "1. нумерованное замечание тоже берём"]
    report = tmp_path / "r.md"
    report.write_text(chr(10).join(lines), encoding="utf-8")
    items = gate._review_findings(report, 5)
    assert "это осмысленное замечание по коду" in items
    assert "нумерованное замечание тоже берём" in items
    assert "мало" not in items


def test_fix_in_progress_is_idle():
    cards = [card("step", "done"),
             card("fix", "running", n="1"),
             card("review", "done", n="1")]
    d = gate.decide(ctx(cards=cards, verdict={"verdict": "REQUEST_CHANGES", "path": "x"}))
    assert d["action"] == "idle"
    assert "правки" in d["reason"]


def test_unreadable_verdict_stops():
    cards = [card("step", "done"), card("review", "done", n="1")]
    d = gate.decide(ctx(cards=cards, verdict={"verdict": "AMBIGUOUS", "path": "docs/reviews/1-S1.md"}))
    assert d["action"] == "stop"
    assert "AMBIGUOUS" in d["reason"] or "не распознан" in d["reason"]


def test_missing_report_stops():
    cards = [card("step", "done"), card("review", "done", n="1")]
    d = gate.decide(ctx(cards=cards, verdict=None))
    assert d["action"] == "stop"
    assert "отчёта" in d["reason"]


def test_owner_stop_wins_over_everything():
    d = gate.decide(ctx(stop={"reason": "хватит", "source": "owner", "kind": "owner"}))
    assert d["action"] == "idle"
    assert "владельца" in d["reason"]


def test_incident_stop_is_cleared_when_cause_gone():
    d = gate.decide(ctx(stop={"reason": "было", "source": "gate", "kind": "incident"}))
    assert d["clear_incident"] is True
    assert d["action"] == "create_step"


def test_hold_step_is_idle():
    d = gate.decide(ctx(hold=["S1"]))
    assert d["action"] == "idle"
    assert "допуска" in d["reason"]


def test_plan_done_is_idle():
    d = gate.decide(ctx(next=None))
    assert d["action"] == "idle"
    assert d.get("plan_done") is True


def test_closed_at_request_changes_rerereviews(project: Path):
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    prev_verdict = {"verdict": "REQUEST_CHANGES", "path": str(p.review_dir / "1-S1.md")}
    d = gate.decide(ctx(prev_closed="S1", prev_verdict=prev_verdict,
                        cards=[], prev_cards=[card("step", "done")]))
    assert d["action"] == "create_rereview"
    assert d["step"] == "S1"
