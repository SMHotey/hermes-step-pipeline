# -*- coding: utf-8 -*-
"""Защита от цикла (план 17, S4): повтор убитой по лимиту попытки требует изменения условий.

Зачем файл: 20.11.2026 шаг 2-S6 перезапускался дважды подряд без изменения модели, лимита и
постановки — каждая попытка шла с нуля и упиралась в тот же предел. Тесты держат то, что
ломается молча: формулу планового лимита (одна на заказ и на проверку условий), счёт
условий (тело карточки и «правило метода» его не сбрасывают), три исхода правила
(эскалация, стоп владельцу, `idle` «повтор уже заказан»), место правила относительно
инцидентного стопа и `--max-retries 0` у карточек шага и постановки.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
import step_metrics

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}


def ctx(**kw) -> dict:
    base = {"next": dict(STEP), "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "verdict": None, "journal_closed": False, "plan_id": "1",
            "attempts": [], "runtime_guard": {}}
    base.update(kw)
    return base


def card(kind: str, status: str = "blocked", n: str = "", short: str = "1") -> dict:
    key = f"planloop:1:S1:{kind}" + (f":{n}" if n else "")
    return {"id": f"t_{short}", "status": status, "key": key, "result": "", "error": ""}


def row(short: str = "1", model: str = "m-1", limit_s: int = 10800,
        escalated: bool = False, body_sha: str = "a" * 64, method: bool = False,
        killed: str = "runtime", kind: str = "step", wall_ms: int = 4_100_000,
        stall_tool: str = "", stall_s: int = 0) -> dict:
    """Строка метрики в форме `attempt_row`: только то, что читает правило."""
    return {"plan": "1", "step": "S1", "card": f"t_{short}", "card_kind": kind,
            "run_id": f"{short}", "model": model, "limit_s": limit_s,
            "killed_by": killed, "escalated": escalated, "body_sha": body_sha,
            "method": method, "wall_ms": wall_ms, "attempt": 1,
            "stall": bool(stall_tool), "stall_tool": stall_tool, "stall_s": stall_s}


# ───────────────────────── П1: плановый лимит и условия ─────────────────────────

def test_planned_limit_rounds_up_to_minute():
    """1.5 × 4080 с = 6120 с; 1.1 × 4080 с = 4488 с → 4500 с (вверх до минуты, Д3)."""
    assert step_metrics.planned_limit(4080, 1.5) == 6120
    assert step_metrics.planned_limit(4080, 1.1) == 4500
    assert step_metrics.planned_limit(3600, 2) == 7200


def test_planned_limit_without_base_is_zero():
    """Нет лимита или множитель неположительный — 0: из нуля лимит не выводится (Д5)."""
    assert step_metrics.planned_limit(0, 1.5) == 0
    assert step_metrics.planned_limit(None, 1.5) == 0
    assert step_metrics.planned_limit(4080, 0) == 0


def test_same_conditions_equal_model_and_planned_limit():
    """Модель та же, лимит ровно плановый от предыдущего — условия не менялись."""
    prev = row("1", limit_s=4080)
    cur = row("2", limit_s=6120, escalated=True)
    same, diffs = step_metrics.same_conditions(prev, cur)
    assert same is True
    assert diffs == []


def test_same_conditions_changed_limit_resets():
    """Лимит не тот, что запланировала эскалация, — счёт условий сброшен (только «лимит»)."""
    prev = row("1", limit_s=4080)
    cur = row("2", limit_s=9000, escalated=True)
    same, diffs = step_metrics.same_conditions(prev, cur)
    assert same is False
    assert diffs == ["лимит"]


def test_same_conditions_changed_model_resets():
    """Сменили модель — условия другие, эскалация разрешена снова."""
    same, diffs = step_metrics.same_conditions(row("1", limit_s=4080),
                                               row("2", limit_s=6120, model="m-2"))
    assert same is False
    assert diffs == ["модель"]


def test_body_and_method_are_named_but_do_not_reset():
    """Тело карточки и «правило метода» в списке различий есть, ключ не сбрасывают (Д5)."""
    prev = row("1", limit_s=4080, body_sha="a" * 64, method=False)
    cur = row("2", limit_s=6120, escalated=True, body_sha="b" * 64, method=True)
    same, diffs = step_metrics.same_conditions(prev, cur)
    assert same is True
    assert diffs == ["тело карточки", "правило метода"]


# ───────────────────────── П2: поля строки метрики ─────────────────────────

def test_row_carries_escalation_fields():
    """Строка несёт `body_sha`, `method` и `escalated`; тела нет — литерал `absent`."""
    body = "шаг\n\nПРАВИЛО МЕТОДА\n- patch не отвечал\n"
    row_one = step_metrics.attempt_row(
        "17", "S2", {"id": "t_2", "assignee": "demo", "body": body,
                     "key": "planloop:17:S2:step"}, None,
        escalated_cards={"t_2"})
    assert row_one["body_sha"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert row_one["method"] is True
    assert row_one["escalated"] is True

    row_two = step_metrics.attempt_row(
        "17", "S2", {"id": "t_3", "assignee": "demo", "key": "planloop:17:S2:step"}, None)
    assert row_two["body_sha"] == "absent"
    assert row_two["method"] is False
    assert row_two["escalated"] is False


def test_escalated_flag_comes_from_state_file(project: Path):
    """`escalated` читается из `runtime_guard.json` и только по id карточки (Д7)."""
    cfg = config_mod.load(project)
    path = step_metrics.runtime_guard_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"1:S1": {"step": "S1", "card": "t_9", "limit_s": 6120}},
                               ensure_ascii=False), encoding="utf-8")
    assert step_metrics.escalated_cards(cfg) == {"t_9"}
    assert step_metrics.read_runtime_guard(cfg)["1:S1"]["limit_s"] == 6120

    path.write_text("{битый", encoding="utf-8")
    assert step_metrics.escalated_cards(cfg) == set()


# ───────────────────────── П3: ключ конфигурации ─────────────────────────

def test_config_defaults_enable_guard(project: Path):
    """Без ключа в файле — умолчания кода: включено, 2 подъёма, ×1.5."""
    assert config_mod.load(project).runtime_guard == {
        "enabled": True, "escalations": 2, "factor": 1.5}


def test_config_reads_guard_values(project: Path):
    """Значения из `pipeline.json` читаются как есть, включая выключенный ключ (Д6)."""
    path = project / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["runtime_guard"] = {"enabled": False, "escalations": 0, "factor": 2}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    guard = config_mod.load(project).runtime_guard
    assert guard == {"enabled": False, "escalations": 0, "factor": 2.0}


def test_config_rejects_bad_guard(project: Path):
    """Мусор в правиле — `ConfigError` с именем поля, а не тихое умолчание."""
    path = project / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for bad, field in (({"factor": 1.0}, "runtime_guard.factor"),
                       ({"factor": "1.5x"}, "runtime_guard.factor"),
                       ({"escalations": -1}, "runtime_guard.escalations"),
                       ({"escalations": True}, "runtime_guard.escalations"),
                       ({"enabled": "да"}, "runtime_guard.enabled"),
                       ({"limit": 3}, "runtime_guard")):
        raw["runtime_guard"] = bad
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load(project)
        assert field in str(exc.value)


# ───────────────────────── П4: три исхода правила ─────────────────────────

def test_first_kill_escalates_limit():
    """Первое убийство — повтор с лимитом ×1.5 и ключом `<старый>:2`, без снятия стопа (Д10)."""
    cards = [card("step", "blocked", short="1")]
    d = gate.decide(ctx(cards=cards, attempts=[row("1", limit_s=10800)]))
    assert d["action"] == "escalate_attempt"
    assert d["kind"] == "step"
    assert d["attempt"] == 2
    assert d["limit_s"] == 16200
    assert d["card"] == "t_1"
    assert "clear_incident" not in d
    assert d["numbers"] and "10800" in d["numbers"][0]


def test_first_kill_with_incident_stop_still_escalates():
    """Инцидентный стоп от тика убийства не превращает правило в `idle` (место в `decide`)."""
    cards = [card("step", "blocked", short="1")]
    stop = {"reason": "карточка шага S1 в статусе blocked", "kind": "incident"}
    d = gate.decide(ctx(cards=cards, attempts=[row("1")], stop=stop))
    assert d["action"] == "escalate_attempt"


def test_second_kill_same_conditions_stops_owner():
    """Второй подряд при тех же модели и лимите — стоп владельцу с числами по попыткам."""
    cards = [card("step", "blocked", short="1"), card("step", "blocked", n="2", short="2")]
    attempts = [row("1", limit_s=10800), row("2", limit_s=16200, escalated=True)]
    d = gate.decide(ctx(cards=cards, attempts=attempts))
    assert d["action"] == "stop"
    assert d["kind"] == "owner"
    assert "условия не менялись" in d["reason"]
    assert len([line for line in d["details"].splitlines() if line.startswith("попытка")]) == 2
    assert "16200" in d["details"]


def test_second_kill_with_changed_model_escalates_again():
    """Модель сменилась — эскалация разрешена снова, стопа нет."""
    cards = [card("step", "blocked", short="1"), card("step", "blocked", n="2", short="2")]
    attempts = [row("1", limit_s=10800), row("2", limit_s=16200, escalated=True, model="m-2")]
    d = gate.decide(ctx(cards=cards, attempts=attempts))
    assert d["action"] == "escalate_attempt"
    assert d["attempt"] == 3
    assert d["limit_s"] == 24300


def test_cap_exhausted_stops_owner():
    """Потолок `escalations` исчерпан — стоп владельцу, а не четвёртый подъём."""
    cards = [card("step", "blocked", short="1"), card("step", "blocked", n="2", short="2"),
             card("step", "blocked", n="3", short="3")]
    attempts = [row("1", limit_s=10800),
                row("2", limit_s=16200, escalated=True),
                row("3", limit_s=24300, escalated=True, model="m-2")]
    d = gate.decide(ctx(cards=cards, attempts=attempts,
                        runtime_guard={"enabled": True, "escalations": 2, "factor": 1.5}))
    assert d["action"] == "stop"
    assert d["kind"] == "owner"
    assert "потолок" in d["reason"]
    assert "модель" in d["details"]


def test_disabled_guard_keeps_base_decision():
    """Выключенный ключ возвращает базовое решение целиком (Д6)."""
    cards = [card("step", "blocked", short="1")]
    d = gate.decide(ctx(cards=cards, attempts=[row("1")],
                        runtime_guard={"enabled": False}))
    assert d["action"] == "stop"
    assert "условия" not in d["reason"]


def test_repeat_in_progress_is_idle_and_clears_incident():
    """Повтор уже заказан и идёт — `idle` без второго заказа, инцидент снимается (Д10)."""
    cards = [card("step", "blocked", short="1"), card("step", "ready", n="2", short="2")]
    d = gate.decide(ctx(cards=cards, attempts=[row("1")]))
    assert d["action"] == "idle"
    assert d["clear_incident"] is True
    assert "повтор уже заказан" in d["reason"]
    assert "t_2" in d["reason"]


def test_owner_stop_wins_over_guard():
    """Стоп владельца сильнее правила: повтор не заказывается."""
    cards = [card("step", "blocked", short="1")]
    stop = {"reason": "остановка после выполнения шага", "kind": "owner"}
    d = gate.decide(ctx(cards=cards, attempts=[row("1")], stop=stop))
    assert d["action"] == "idle"
    assert "стоп-флаг владельца" in d["reason"]


def test_manual_hold_wins_over_guard():
    """Ручной допуск владельца тоже сильнее правила: шаг ждёт его решения."""
    cards = [card("step", "blocked", short="1")]
    d = gate.decide(ctx(cards=cards, attempts=[row("1")], hold=["S1"]))
    assert d["action"] != "escalate_attempt"


def test_kill_of_prompt_card_escalates_prompt():
    """Убитая постановка повторяется карточкой постановки: вид и лимит из строки метрики."""
    cards = [card("prompt", "blocked", short="1")]
    d = gate.decide(ctx(cards=cards, attempts=[row("1", kind="prompt", limit_s=2700)]))
    assert d["action"] == "escalate_attempt"
    assert d["kind"] == "prompt"
    assert d["limit_s"] == 4080


def test_kill_of_review_card_is_not_guard_business():
    """Карточка ревью (номер-круг) правилом не повторяется: у неё номер в ключе — круг."""
    cards = [card("review", "blocked", n="1", short="1")]
    d = gate.decide(ctx(cards=cards, attempts=[row("1", kind="review")]))
    assert d["action"] != "escalate_attempt"


# ───────────────────────── П5: `--max-retries 0` ─────────────────────────

class _Done:
    returncode = 0
    stdout = "t_abc123\n"
    stderr = ""


def create_card_cmd(project: Path, monkeypatch, key: str,
                    guard: dict | None = None) -> list[str]:
    """Собрать argv заказа карточки: `_run` подменён, борда не трогается."""
    if guard is not None:
        path = project / "pipeline.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["runtime_guard"] = guard
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    seen: dict = {}

    def fake_run(cmd, cwd=None, timeout=120, env=None):
        seen["cmd"] = list(cmd)
        return _Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    monkeypatch.setattr(gate, "_card_visible", lambda *a, **k: True)
    gate._create_card(p, "заголовок", "тело", "demo", key, ("s",), max_runtime="3h")
    return seen["cmd"]


def _no_retries(argv: list[str]) -> bool:
    return "--max-retries" in argv and argv[argv.index("--max-retries") + 1] == "0"


def test_step_card_gets_zero_retries(project: Path, monkeypatch):
    """Карточка шага при включённом ключе заказывается с `--max-retries 0` (Д2)."""
    assert _no_retries(create_card_cmd(project, monkeypatch, "planloop:1:S1:step"))


def test_prompt_card_gets_zero_retries(project: Path, monkeypatch):
    """Карточка постановки тоже: её повтор так же принадлежит стражу."""
    assert _no_retries(create_card_cmd(project, monkeypatch, "planloop:1:S1:prompt"))


def test_review_card_keeps_board_retries(project: Path, monkeypatch):
    """У вида с номером-кругом (`review`) флага нет: номер в ключе — круг, а не попытка."""
    argv = create_card_cmd(project, monkeypatch, "planloop:1:S1:review:1")
    assert not _no_retries(argv)


def test_disabled_guard_keeps_board_retries(project: Path, monkeypatch):
    """Выключенный ключ возвращает прежнее поведение целиком: флага нет и у шага."""
    argv = create_card_cmd(project, monkeypatch, "planloop:1:S1:step",
                           guard={"enabled": False, "escalations": 2, "factor": 1.5})
    assert not _no_retries(argv)


# ───────────────────────── П6: заказ повтора в тике ─────────────────────────

TASKS = ("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
         " status TEXT DEFAULT 'ready', assignee TEXT, idempotency_key TEXT,"
         " completed_at INTEGER, result TEXT, last_failure_error TEXT, created_at INTEGER,"
         " tenant TEXT, workspace TEXT, max_runtime TEXT)")
TASK_RUNS = ("CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " task_id TEXT NOT NULL, profile TEXT, status TEXT, outcome TEXT,"
             " summary TEXT, error TEXT, started_at INTEGER, ended_at INTEGER,"
             " max_runtime_seconds INTEGER)")
COMMENTS = ("CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT, body TEXT, author TEXT, created_at INTEGER)")


def seed_killed(db: Path, card_id: str = "t_step01",
                key: str = "planloop:1:S1:step") -> None:
    """Убитая по лимиту карточка шага: статус `blocked`, текст лимита, прогон 3 ч."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASKS)
        con.executescript(TASK_RUNS)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, completed_at, result, last_failure_error,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (card_id, "шаг", "blocked", "demo", key, None, "",
                     "elapsed 10802s > limit 10800s", int(time.time()) - 4000))
        con.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at,"
                    " ended_at, max_runtime_seconds) VALUES (?,?,?,?,?,?,?)",
                    (card_id, "demo", "failed", "timed_out", int(time.time()) - 11000,
                     int(time.time()) - 100, 10800))
        con.commit()
    finally:
        con.close()


def comments(db: Path, card_id: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        con.executescript(COMMENTS)
        rows = con.execute("SELECT body FROM comments WHERE task_id = ? ORDER BY id",
                           (card_id,)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def board(db: Path) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT id, status, idempotency_key, max_runtime FROM tasks"
                           " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"id": r[0], "status": r[1], "key": r[2], "max_runtime": r[3]} for r in rows]


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch, tmp_path: Path) -> Path:
    """Стенд тика: проект, поддельная борда, убитая карточка и её строка метрики."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    home = tmp_path / "hermes-home"
    (home / "profiles" / "demo" / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    raw["notify"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                           encoding="utf-8")
    return project


def write_attempt(cfg, **fields) -> None:
    """Строка метрики убитой попытки — то, что записал бы тик убийства."""
    path = step_metrics.metrics_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"plan": "1", "step": "S1", "card": "t_step01", "card_kind": "step",
            "run_id": "1", "killed_by": "runtime", "limit_s": 10800,
            "model": "m-1", "body_sha": "a" * 64, "method": False,
            "wall_ms": 10_802_000, "attempt": 1, "stall": False, "stall_tool": "",
            "stall_s": 0, "ts": int(time.time()) - 200}
    line.update(fields)
    path.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")


def test_tick_orders_escalated_card(stand: Path, hermes_stub: Path):
    """Тик заказывает повтор с лимитом 16200 с, пишет метку и числа комментарием (П6)."""
    seed_killed(hermes_stub)
    cfg = config_mod.load(stand)
    write_attempt(cfg, stall=True, stall_tool="patch", stall_s=691)
    p = gate.resolve_paths(cfg, "1")

    gate.run_gate(p, notify_fn=lambda text: None)

    rows = board(hermes_stub)
    made = [item for item in rows if item["key"] == "planloop:1:S1:step:2"]
    assert len(made) == 1
    assert made[0]["max_runtime"] == "270m"
    assert made[0]["status"] == "ready"
    # Числа попытки — комментарием в убитую карточку, с указанием лимита повтора.
    notes = comments(hermes_stub, "t_step01")
    assert any("повышенным лимитом 16200 с" in body for body in notes)
    assert any("patch (691 с)" in body for body in notes)
    # Метка эскалации: id новой карточки и её лимит — по ней `record` ставит `escalated`.
    guard = step_metrics.read_runtime_guard(cfg)
    assert guard["1:S1"]["card"] == made[0]["id"]
    assert guard["1:S1"]["limit_s"] == 16200
    assert guard["1:S1"]["prev_limit_s"] == 10800
    assert not gate.load_stop(p)


def test_tick_does_not_order_second_repeat(stand: Path, hermes_stub: Path):
    """Повтор уже заказан — второй карточки нет, а решение говорит «ждём исхода»."""
    seed_killed(hermes_stub)
    cfg = config_mod.load(stand)
    write_attempt(cfg)
    p = gate.resolve_paths(cfg, "1")
    gate.run_gate(p, notify_fn=lambda text: None)
    first = [item for item in board(hermes_stub) if item["key"] == "planloop:1:S1:step:2"]
    assert len(first) == 1

    result = gate.run_gate(p, notify_fn=lambda text: None)
    assert result["decision"]["action"] == "idle"
    assert "повтор уже заказан" in result["decision"]["reason"]
    assert len([item for item in board(hermes_stub)
                if item["key"] == "planloop:1:S1:step:2"]) == 1
