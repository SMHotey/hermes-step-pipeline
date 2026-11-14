# -*- coding: utf-8 -*-
"""Юнит-тесты блока «Ревью плана»: счёт кругов, бюджет, эпоха, время и маркер.

Уровень — модули напрямую, без HTTP и без борды: `plan_review_progress.build` получает
карточки аргументами, а `plan_rounds` читает и пишет маркер в каталог состояния проекта.
Правило счёта — то же, что у движка (карточки ПРАВОК, а не ревью), поэтому здесь же
сторож на две копии чтения маркера: плагин и вендоренный `gate.load_extra_fix_rounds`
обязаны отвечать на одном файле одинаково.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from step_pipeline import plan_review_progress as prp
from step_pipeline import plan_rounds

NOW = 1800000000.0
VERDICT_WAIT = {"verdict": "REQUEST_CHANGES", "findings": []}


def card(card_id: str, key: str, created: int = 0, status: str = "done",
         started: int | None = None, completed: int | None = None) -> dict:
    """Строка борды в той форме, в какой её видит снимок (поля `CARD_COLUMNS`)."""
    return {"id": card_id, "idempotency_key": key, "created_at": created, "status": status,
            "started_at": started, "completed_at": completed}


def review(report: dict | None = None, findings: list | None = None,
           verdict: str = "REQUEST_CHANGES") -> dict:
    """Разбор отчёта ревью плана — те же ключи, что отдаёт `snapshot._review`."""
    return {"exists": True, "verdict": verdict, "findings": findings or [],
            "findings_note": "", **(report or {})}


def approval(root: Path, plan_id: str = "1", **extra) -> Path:
    """Запись утверждения в каталоге состояния проекта (то же, что пишет `plan_accept`)."""
    path = root / ".pipeline" / "state" / f"plan_{plan_id}_approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": plan_id, "file": f"docs/plans/{plan_id}-demo.md", "sha256": "x",
               "approved_at": "2026-01-01T00:00:00", "profile": "test"}
    payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def marker(root: Path, payload, plan_id: str = "1") -> Path:
    """Маркер ручных кругов как есть: тесту нужны и битые значения."""
    path = plan_rounds.marker_path(root, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def engine_extra(project: Path, state: Path, plan_id: str = "1") -> int:
    """Сколько ручных кругов видит движок в том же маркере (сторож на две копии правила).

    Движок берётся вендоренный — из `<проект>/.pipeline/gate.py`, как его читает страж:
    импортировать его в процесс теста нельзя (общие имена `config`, `registry`), поэтому
    проба идёт отдельным интерпретатором.
    """
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import gate\n"
        "paths = gate.Paths(cfg=SimpleNamespace(state_dir=sys.argv[2]), repo=Path(sys.argv[1]),\n"
        "                   plan_file=None, review_dir=None, kanban_db=None, state_file=None,\n"
        "                   stop_file=None, plan_id=sys.argv[3], journal_heading='',\n"
        "                   review_prefix='', spec=None)\n"
        "print(gate.load_extra_fix_rounds(paths))\n"
    )
    done = subprocess.run([sys.executable, "-c", probe, str(project / ".pipeline"), str(state),
                           plan_id], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-500:]
    return int(done.stdout.strip() or 0)


def test_round_counts_fix_cards_not_reviews():
    """Круг — число карточек правок эпохи; ревью без ответной правки круг не двигает."""
    cards = [card("t_r1", "planloop:1:planreview", 100, "done", started=100, completed=400),
             card("t_f1", "planloop:1:planfix:1", 200, "done", started=200, completed=500),
             card("t_r2", "planloop:1:planreview:2", 300, "done", started=300, completed=600)]

    block = prp.build("1", cards, {}, 4, 0, 0, VERDICT_WAIT, NOW)

    assert block["round"] == 1
    assert block["budget"] == 4
    assert block["label"] == "круг 1 из 4"
    assert block["exhausted"] is False
    assert block["can_fix"] is False
    pairs = block["rounds"]
    assert len(pairs) == 2
    assert pairs[0]["review"]["id"] == "t_r1" and pairs[0]["fix"]["id"] == "t_f1"
    assert pairs[1]["review"]["id"] == "t_r2"
    assert pairs[1]["fix"] is None
    assert pairs[1]["state_key"] == "wait"
    assert prp.build("1", cards, {}, 4, 0, 0, VERDICT_WAIT, NOW) == block   # форма стабильна

    many = ([card(f"t_r{i}", "planloop:1:planreview" if i == 1 else f"planloop:1:planreview:{i}",
                  i * 10, "done") for i in range(1, 5)]
            + [card(f"t_f{i}", f"planloop:1:planfix:{i}", i * 10 + 5, "done")
               for i in range(1, 5)])
    full = prp.build("1", many, {}, 4, 0, 0, VERDICT_WAIT, NOW)
    assert full["label"] == "круг 4 из 4"
    assert full["exhausted"] is True
    assert full["can_fix"] is True
    assert full["note"] == ""


def test_budget_sums_marker_and_config(project: Path):
    """Бюджет = `max_fix_rounds` + `extra_fix_rounds` маркера, с умолчаниями движка."""
    assert plan_rounds.budget({"max_fix_rounds": 4}, 0) == 4
    assert plan_rounds.budget({}, 0) == 2                 # умолчание `DEFAULT_FIX_ROUNDS`
    assert plan_rounds.budget({"max_fix_rounds": 0}, 0) == 1
    assert plan_rounds.budget({"max_fix_rounds": "мусор"}, 0) == 2
    assert plan_rounds.budget({"max_fix_rounds": 4}, 1) == 5

    marker(project, {"extra_fix_rounds": 1, "at": "01.01.2026 00:00:00", "reason": "руками"})
    extra = plan_rounds.extra_fix_rounds(project, "1")
    assert extra == 1
    cards = [card(f"t_f{i}", f"planloop:1:planfix:{i}", i * 10, "done") for i in range(1, 5)]

    block = prp.build("1", cards, {}, plan_rounds.budget({"max_fix_rounds": 4}, extra), extra,
                      0, VERDICT_WAIT, NOW)

    assert block["label"] == "круг 4 из 5"
    assert block["exhausted"] is False
    assert block["can_fix"] is False
    assert block["note"] == "ручной круг уже израсходован"


def test_reset_epoch_starts_from_reapproval(project: Path):
    """Эпоха начинается с `review_reset_at`, иначе с `approved_at`, без записи — с нуля."""
    cards = [card("t_f1", "planloop:1:planfix:1", 100, "done"),
             card("t_f2", "planloop:1:planfix:2", 300, "done")]
    approval(project, review_reset_at=200)

    assert plan_rounds.reset_at(project, "1") == 200
    block = prp.build("1", cards, {}, 4, 0, plan_rounds.reset_at(project, "1"), VERDICT_WAIT, NOW)
    assert block["round"] == 1
    assert block["label"] == "круг 1 из 4"
    assert [pair["fix"]["id"] for pair in block["rounds"]] == ["t_f2"]

    # Запись старой версии плагина: поля `review_reset_at` нет — граница по `approved_at`
    # (локальное время того же формата, что пишет `plan_accept.now_iso`).
    approval(project)
    boundary = plan_rounds.reset_at(project, "1")
    assert boundary == int(time.mktime(time.strptime("2026-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")))
    assert boundary > 300
    stale = prp.build("1", cards, {}, 4, 0, boundary, VERDICT_WAIT, NOW)
    assert stale["round"] == 0
    assert stale["exists"] is False

    # Записи нет вовсе: эпоха начинается с нуля, в счёт идут все карточки, а ревью круг
    # не двигает — даже когда оно в наборе.
    (project / ".pipeline" / "state" / "plan_1_approval.json").unlink()
    assert plan_rounds.reset_at(project, "1") == 0
    fresh = prp.build("1", [cards[0], card("t_r2", "planloop:1:planreview:2", 300, "done")],
                      {}, 4, 0, 0, VERDICT_WAIT, NOW)
    assert fresh["round"] == 1
    assert fresh["exists"] is True


def test_round_times_stage_and_findings():
    """Время считают закрытые карточки; этап и находки берутся из состояния и отчёта."""
    cards = [card("t_r1", "planloop:1:planreview", 100, "done", started=100, completed=700),
             card("t_f1", "planloop:1:planfix:1", 800, "done", started=800, completed=1100)]
    report = review(findings=[{"level": "блокирующая", "text": "нет гейта"},
                              {"level": "стиль", "text": "длинная строка"}])

    block = prp.build("1", cards, {}, 4, 0, 0, report, NOW)

    assert block["review_duration"] == "10 мин"
    assert block["fix_duration"] == "5 мин"
    assert block["review_seconds"] == 600
    assert block["fix_seconds"] == 300
    assert block["rounds"][0]["review"]["seconds"] == 600
    assert block["findings"]["verdict"] == "REQUEST_CHANGES"
    assert block["findings"]["total"] == 2
    assert block["findings"]["blocking"] == 1
    assert block["verdict"] == "REQUEST_CHANGES"

    # Открытое ревью вторым кругом: этап «ревью плана идёт», у его стороны времени нет.
    running = cards + [card("t_r2", "planloop:1:planreview:2", 1200, "running", started=1200)]
    live = prp.build("1", running, {}, 4, 0, 0, report, NOW)
    assert live["stage_key"] == "review"
    assert live["stage"] == "ревью плана идёт"
    assert live["rounds"][1]["review"]["duration"] == "идёт"
    assert live["rounds"][1]["review"]["seconds"] is None

    # Та же эпоха с закрытым ревью: APPROVE — «ревью плана пройдено», иной вердикт — «ждёт».
    closed = [row if row["id"] != "t_r2" else {**row, "status": "done", "completed_at": 1500}
              for row in running]
    done = prp.build("1", closed, {}, 4, 0, 0, review(verdict="APPROVE"), NOW)
    assert done["stage_key"] == "done"
    assert done["stage"] == "ревью плана пройдено"
    waiting = prp.build("1", closed, {}, 4, 0, 0, report, NOW)
    assert waiting["stage_key"] == "wait"
    assert waiting["stage"] == "ждёт следующего круга ревью"


def test_marker_read_write_and_state_dir(project: Path, monkeypatch):
    """Маркер читается толерантно, пишется в каталог из `PIPELINE_STATE_DIR`, `+1` за жест."""
    path = plan_rounds.marker_path(project, "1")
    assert plan_rounds.read_marker(project, "1") == {}
    marker(project, "{битый json")
    assert plan_rounds.read_marker(project, "1") == {}
    marker(project, [1, 2, 3])
    assert plan_rounds.read_marker(project, "1") == {}
    assert plan_rounds.extra_fix_rounds(project, "1") == 0

    # Те же правила, что `gate.load_extra_fix_rounds`: bool и мусор — не круги, 1.0 и «3» — круги.
    for value, expected in ((True, 0), (-1, 0), (1.5, 0), ("мусор", 0), (1.0, 1), ("3", 3)):
        marker(project, {"extra_fix_rounds": value})
        assert plan_rounds.extra_fix_rounds(project, "1") == expected, value
    marker(project, {"extra_fix_rounds": 0})

    state = project.parent / "state-env"
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(state))
    first = plan_rounds.grant(project, "01", "первый ручной круг")
    written = state / "plan_1_rounds.json"
    assert written.is_file(), "маркер обязан лечь в каталог из PIPELINE_STATE_DIR"
    assert first["extra_fix_rounds"] == 1
    assert first["reason"] == "первый ручной круг"
    time.strptime(first["at"], "%d.%m.%Y %H:%M:%S")      # формат `at` — как у стоп-флага
    assert json.loads(written.read_text(encoding="utf-8"))["extra_fix_rounds"] == 1
    assert plan_rounds.grant(project, "1")["extra_fix_rounds"] == 2
    assert plan_rounds.extra_fix_rounds(project, "1") == 2
    # Сторож на две копии правила: движок обязан прочитать тот же файл тем же числом.
    assert engine_extra(project, state) == 2
    # Маркер в каталоге проекта (`paths.state`) не изменился: жест пишет туда, куда зовёт env.
    assert json.loads(path.read_text(encoding="utf-8"))["extra_fix_rounds"] == 0
