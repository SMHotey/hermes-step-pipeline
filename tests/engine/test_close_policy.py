# -*- coding: utf-8 -*-
"""Закрытие шага по прогонам (план 9, S3): три условия закрытия в решении стража.

Проверяется чистая функция `decide` на синтетических состояниях: стенд, git и подпроцессы
здесь не нужны — доказательство прогона приходит в `ctx` готовым словарём, отчёт ревью —
файлом в `tmp_path`. Признак нового правила один — пункт «Прогон» у шага; у шага без
прогонов поведение обязано остаться прежним (обратная совместимость legacy-планов).
"""
from __future__ import annotations

from pathlib import Path

import gate

SHA = "a" * 40
RUNS = [{"command": "python -c pass", "expect": "exit", "value": "0", "timeout": 60}]


def step(risk: str = "low", runs: bool = True) -> dict:
    """Шаг-исполнитель с нужным риском и (не)объявленными прогонами."""
    return {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
            "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": "",
            "risk": risk, "runs": list(RUNS) if runs else []}


def proof(green: int = 1, total: int = 1, outcome: str = "green", sha: str = SHA) -> dict:
    """Доказательство прогона в том виде, в каком его пишет `run_step_runs`."""
    return {"plan": "1", "step": "S1", "sha": sha, "total": total, "green": green,
            "seconds": 1.0, "budget": {},
            "runs": [{"command": "python -c pass", "outcome": outcome, "code": 0,
                      "seconds": 1.0, "tail": ""}]}


def card(kind: str, status: str = "done", n: str = "") -> dict:
    key = f"planloop:1:S1:{kind}" + (f":{n}" if n else "")
    return {"id": f"t_{kind}{n or ''}", "status": status, "key": key, "result": "",
            "error": ""}


def ctx(step_dict: dict, verdict=None, runs=None, **kw) -> dict:
    """Состояние «шаг done + ревью done»: ровно та точка, где решается закрытие."""
    base = {"next": step_dict, "cards": [card("step"), card("review", n="1")],
            "prev_cards": [], "closed": [], "prev_closed": None, "prev_verdict": None,
            "stop": None, "hold": [], "verdict": verdict, "journal_closed": False,
            "plan_id": "1", "step_runs": runs, "fixes": [], "step_findings": [],
            "max_fix_rounds": 2, "max_fix_rounds_high": 3, "prefetch": False,
            "require_prompt": True, "next_step": None, "next_prompt": None,
            "next_cards": []}
    base.update(kw)
    return base


def report(tmp_path: Path, lines: list[str], verdict: str | None = "APPROVE") -> Path:
    """Отчёт ревью с разделом находок: коды ищутся только в пунктах области.

    `verdict=None` — отчёт без строки вердикта (состояние «вердикт `NONE`»), раздел
    находок при этом тот же: коды читаются из пунктов, а не из шапки.
    """
    path = tmp_path / "1-S1.md"
    head = [f"## ВЕРДИКТ: {verdict}", ""] if verdict else []
    path.write_text("\n".join(head + ["## Находки", ""] + lines) + "\n", encoding="utf-8")
    return path


def test_low_closes_without_verdict():
    """Т1: шаг low с зелёными прогонами закрывается без вердикта; коммит — sha прогона."""
    d = gate.decide(ctx(step("low"), verdict=None, runs=proof()))
    assert d["action"] == "close_step", d
    assert d.get("runs_only") is True
    assert d["commit"] == SHA
    assert d["verdict"] == {}


def test_normal_without_verdict_stops():
    """Т2: шаг normal без APPROVE стоит; причина называет недостающий допуск."""
    d = gate.decide(ctx(step("normal"), verdict=None, runs=proof()))
    assert d["action"] == "stop", d
    assert "нет APPROVE" in d["reason"]
    assert "normal" in d["reason"]
    assert d.get("kind", "incident") == "incident"


def test_legacy_step_without_runs_keeps_old_rule():
    """Т3: у шага без «Прогона» прежнее правило — отчёта нет, значит стоп."""
    d = gate.decide(ctx(step("low", runs=False), verdict=None, runs=None))
    assert d["action"] == "stop", d
    assert "отчёта" in d["reason"]
    assert not d.get("runs_only")


def test_red_runs_before_verdict_become_fix_card():
    """Т4: красный прогон — карточка правок, а не закрытие и не ревью."""
    d = gate.decide(ctx(step("low"), verdict=None,
                        runs=proof(green=0, total=1, outcome="red")))
    assert d["action"] == "create_fix", d
    assert d.get("runs") is True
    assert d["round"] == 1
    assert d["parent"] == "t_step"


def test_absent_proof_asks_runs_not_stop():
    """Т5: доказательства нет — исполнение прогонов, а не стоп."""
    d = gate.decide(ctx(step("low"), verdict=None, runs=None))
    assert d["action"] == "step_runs", d
    assert d["step"] == "S1"


def test_open_registry_blocks_close():
    """Т6: открытая строка реестра шага не даёт закрыть шаг по прогонам."""
    findings = [{"id": "F1", "kind": "код", "text": "нет индекса", "action": "добавить",
                 "status": "открыто"}]
    d = gate.decide(ctx(step("low"), verdict=None, runs=proof(), step_findings=findings))
    assert d["action"] != "close_step", d


def test_ambiguous_verdict_low_closes(tmp_path):
    """Т7: отчёт с несколькими вердиктами у low ведёт политику, а не прежний стоп."""
    path = tmp_path / "1-S1.md"
    path.write_text("## ВЕРДИКТ: APPROVE\n\n## ВЕРДИКТ: REQUEST_CHANGES\n", encoding="utf-8")
    d = gate.decide(ctx(step("low"), verdict={"verdict": "AMBIGUOUS", "path": str(path),
                                              "commit": ""}, runs=proof()))
    assert d["action"] == "close_step", d
    assert d.get("runs_only") is True


def test_code_finding_blocks_low(tmp_path):
    """Т8: находка с кодом держит шаг low и открывает круг правок."""
    path = report(tmp_path, ["- [значимая] `app/models/invoice.py:41` — F06: тест не "
                             "обновлён"], verdict="REQUEST_CHANGES")
    d = gate.decide(ctx(step("low"), verdict={"verdict": "REQUEST_CHANGES",
                                              "path": str(path), "commit": ""},
                        runs=proof()))
    assert d["action"] == "create_fix", d
    assert d["round"] == 1
    assert "F06" in d["cause"]


def test_prefetch_nplus1_blocks_close():
    """Т9: без заказанной постановки N+1 шаг не закрывается и по прогонам."""
    nxt = {"id": "S2", "title": "Эндпоинт списка"}
    d = gate.decide(ctx(step("low"), verdict=None, runs=proof(), prefetch=True,
                        next_step=nxt, next_prompt=None))
    assert d["action"] == "create_prompt", d
    assert d["step"] == "S2"
    assert d.get("after_close") is True


def test_normal_with_none_verdict_stops(tmp_path):
    """Т10 (§4 п.8.2): вердикт `NONE` при отчёте на месте — тот же стоп, что и без отчёта."""
    path = tmp_path / "1-S1.md"
    path.write_text("## Находки\n", encoding="utf-8")
    d = gate.decide(ctx(step("normal"), verdict={"verdict": "NONE", "path": str(path),
                                                 "commit": ""}, runs=proof()))
    assert d["action"] == "stop", d
    assert "нет APPROVE" in d["reason"]
    assert "normal" in d["reason"]
    assert d.get("kind", "incident") == "incident"


def test_high_ambiguous_stops(tmp_path):
    """Т11 (§4 п.8.3): высокий риск не закрывается и при нераспознанном вердикте."""
    path = tmp_path / "1-S1.md"
    path.write_text("## ВЕРДИКТ: APPROVE\n\n## ВЕРДИКТ: REQUEST_CHANGES\n", encoding="utf-8")
    d = gate.decide(ctx(step("high"), verdict={"verdict": "AMBIGUOUS", "path": str(path),
                                               "commit": ""}, runs=proof()))
    assert d["action"] == "stop", d
    assert "нет APPROVE" in d["reason"]
    assert "high" in d["reason"]


def test_code_finding_blocks_with_none_verdict(tmp_path):
    """Т12 (§4 п.8.5): код держит шаг и без вердикта — круг правок, причина называет код."""
    path = report(tmp_path, ["- [значимая] `app/models/invoice.py:41` — F06: тест не "
                             "обновлён"], verdict=None)
    d = gate.decide(ctx(step("low"), verdict={"verdict": "NONE", "path": str(path),
                                              "commit": ""}, runs=proof()))
    assert d["action"] == "create_fix", d
    assert d["round"] == 1
    assert "F06" in d["cause"]


def test_code_finding_limit_exhausted_stops(tmp_path):
    """Т13 (§4 п.8.5): лимит кругов исчерпан — стоп с числами, а не новый круг."""
    path = report(tmp_path, ["- [значимая] `app/models/invoice.py:41` — F06: тест не "
                             "обновлён"], verdict=None)
    cards = [card("step"), card("fix", n="1"), card("fix", n="2"), card("review", n="3")]
    d = gate.decide(ctx(step("low"), verdict={"verdict": "NONE", "path": str(path),
                                              "commit": ""}, runs=proof(), cards=cards))
    assert d["action"] == "stop", d
    assert "F06" in d["reason"]
    assert "израсходовано кругов правок 2 при пределе 2" in d["reason"]
    assert "риска: low" in d["reason"]


def test_report_without_findings_section_closes(tmp_path):
    """Т14 (§4 п.8.6): раздела находок нет — кодов нет, закрытие по прогонам идёт."""
    path = tmp_path / "1-S1.md"
    path.write_text("## ВЕРДИКТ: APPROVE\n", encoding="utf-8")
    d = gate.decide(ctx(step("low"), verdict={"verdict": "NONE", "path": str(path),
                                              "commit": ""}, runs=proof()))
    assert d["action"] == "close_step", d
    assert d.get("runs_only") is True
    assert gate.report_finding_codes_at(str(path)) == []


def test_legacy_approve_with_commit_closes(tmp_path):
    """Т15 (§4 п.8.7): legacy-шаг с APPROVE и существующим коммитом закрывается как раньше."""
    path = report(tmp_path, [], verdict="APPROVE")
    d = gate.decide(ctx(step("low", runs=False),
                        verdict={"verdict": "APPROVE", "path": str(path), "commit": SHA},
                        runs=None))
    assert d["action"] == "close_step", d
    assert not d.get("runs_only")
    assert d["verdict"]["commit"] == SHA


def test_broken_step_registry_stops(tmp_path):
    """Т16 (§4 п.8.8): битый реестр шага — прежний стоп, новое решение не включается."""
    path = report(tmp_path, [], verdict="APPROVE")
    d = gate.decide(ctx(step("low"),
                        verdict={"verdict": "APPROVE", "path": str(path), "commit": SHA},
                        runs=proof(), step_findings_error="битый JSON"))
    assert d["action"] == "stop", d
    assert not d.get("runs_only")
    assert "реестр находок шага S1 не читается" in d["reason"]


def test_code_finding_lowercase_blocks(tmp_path):
    """Т17 (F2 ревью 9-S3): метка кода в нижнем регистре распознаётся и держит шаг."""
    path = report(tmp_path, ["- [блокирующая] `app/y.py:20` — f03 прогон красный"],
                  verdict=None)
    assert gate.report_finding_codes_at(str(path)) == ["F03"]
    d = gate.decide(ctx(step("low"), verdict={"verdict": "NONE", "path": str(path),
                                              "commit": ""}, runs=proof()))
    assert d["action"] == "create_fix", d
    assert "F03" in d["cause"]

