# -*- coding: utf-8 -*-
"""Бюджет шага и потолки попытки (план 17, S5): конфиг, живой счёт, стоп и `plan --check`.

Зачем файл: лимиты времени и вызовов модели задаются в `pipeline.json`, а не берутся из
кода. Замер основания — 23.11.2026: бюджет прогонов 600 с при объявленном таймауте 1800 с
остановил план на инциденте, а потолка вызовов не было нигде (63 и 77 вызовов на попытку
никто не ограничивал). Тесты держат четыре вещи, на которых бюджет ломается молча: разбор
`45m`/`3h`, живой счёт идущей попытки (окно кончается на «сейчас», а не на бесконечности),
порядок решения (бюджет сильнее правила S4, иначе стоп проглатывается) и авто-бюджет
прогонов как сумма объявленных таймаутов.

Имена тестов — из постановки 17-S5 (§4.1–4.11), по одному тесту на утверждение: двадцать
два имени, двадцать два теста.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
import step_metrics
from step_pipeline import plan_accept

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "agent-log-sample.txt"

TASKS = ("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
         " status TEXT, assignee TEXT, idempotency_key TEXT, completed_at INTEGER,"
         " result TEXT, last_failure_error TEXT, created_at INTEGER)")
TASK_RUNS = ("CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " task_id TEXT NOT NULL, profile TEXT, status TEXT, outcome TEXT,"
             " summary TEXT, error TEXT, started_at INTEGER, ended_at INTEGER,"
             " max_runtime_seconds INTEGER)")


def at(stamp: str) -> int:
    """Момент фикстуры (`10:00:20`) в epoch — та же локальная зона, что у борды."""
    return int(time.mktime(time.strptime("2026-11-20 " + stamp, "%Y-%m-%d %H:%M:%S")))


@pytest.fixture()
def clean_env(monkeypatch) -> None:
    """Никаких `PIPELINE_*` из чужой сессии: конфиг читается из проекта теста."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)


def write_cfg(root: Path, **changes) -> None:
    """Правка `pipeline.json` проекта; `None` в значении — ключ удаляется."""
    path = Path(root) / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_card(db: Path, card_id: str = "t_step01", key: str = "planloop:1:S1:step",
              status: str = "running", assignee: str = "demo") -> None:
    con = sqlite3.connect(db)
    try:
        con.executescript(TASKS)
        con.executescript(TASK_RUNS)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
                    (card_id, "шаг", status, assignee, key, at("09:00:00")))
        con.commit()
    finally:
        con.close()


def add_run(db: Path, card_id: str = "t_step01", profile: str = "demo",
            started: int | None = None, ended: int | None = None) -> int:
    """Прогон карточки в борде; `ended=None` — прогон идёт (живой счёт)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASK_RUNS)
        cur = con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error, started_at,"
            " ended_at, max_runtime_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (card_id, profile, "running", "", "", at("10:00:19") if started is None else started,
             ended, None))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def project_paths(root: Path, plan_id: str = "1") -> gate.Paths:
    """Пути стража по конфигурации проекта теста."""
    return gate.resolve_paths(config_mod.load(root), plan_id)


def ctx(**kw) -> dict:
    """Синтетическое состояние тика — как в `test_gate_decide.py`."""
    step = {"id": "S5", "title": "Бюджет", "epoch": "", "days": 1.0, "owner": "", "goal": "g",
            "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}
    base = {"next": dict(step), "cards": [], "prev_cards": [], "closed": [], "prev_closed": None,
            "prev_verdict": None, "stop": None, "hold": [], "verdict": None,
            "journal_closed": False, "plan_id": "17"}
    base.update(kw)
    return base


def over_budget(over: str = "calls", calls: int = 420, out: int = 210000) -> dict:
    """Готовая проба «попытка за потолком» — то, что собрал бы `budget_probe`."""
    return {"card": "t_5", "kind": "step", "role": "step", "run_id": "77", "attempt": 1,
            "calls": calls, "out": out,
            "max_calls": 400 if over == "calls" else 500,
            "max_output_tokens": 200000 if over == "tokens" else 300000,
            "over": over}


# ─────────────────── §4.1: парсер длительности ───────────────────

def test_parse_duration():
    """`45m`, `270m`, `3h` и `3 h` разбираются; мусор и число без единицы — `None`."""
    assert config_mod.parse_duration("45m") == 45 * 60
    assert config_mod.parse_duration("270m") == 270 * 60
    assert config_mod.parse_duration("3h") == 3 * 3600
    assert config_mod.parse_duration("3H") == 3 * 3600
    assert config_mod.parse_duration("3 h") == 3 * 3600
    assert config_mod.parse_duration("0m") == 0
    for bad in ("", "45", "три часа", "1d", "3h30m", "m45", None, 45, True):
        assert config_mod.parse_duration(bad) is None


# ─────────────────── §4.2: `runtimes` проверяется при загрузке ───────────────────

def test_config_runtime_seconds(project: Path, clean_env):
    """Разделы `runtimes` дают секунды по роли; строки для борды остаются как записаны."""
    write_cfg(project, runtimes={"step": "270m", "review": "3h", "prompt": "45m",
                                 "scout": "30m"})
    cfg = config_mod.load(project)
    assert cfg.runtime_seconds == {"step": 16200, "review": 10800, "prompt": 2700,
                                   "scout": 1800}
    assert (cfg.step_runtime, cfg.review_runtime, cfg.prompt_runtime) == ("270m", "3h", "45m")


def test_config_bad_runtime_is_config_error(project: Path, clean_env):
    """Мусор, число без единицы и `0m` — `ConfigError` с полным именем ключа."""
    for bad in ("три часа", "45", "0m", "", 45, True):
        write_cfg(project, runtimes={"step": bad, "review": "150m", "prompt": "45m"})
        with pytest.raises(config_mod.ConfigError) as err:
            config_mod.load(project)
        assert "runtimes.step" in str(err.value), bad


# ─────────────────── §4.3: раздел `budgets` ───────────────────

def test_config_budgets_shape_ok(project: Path, clean_env):
    """Потолки по роли читаются как заданы; раздел без правок — пустой."""
    assert config_mod.load(project).budgets == {}
    write_cfg(project, budgets={"step": {"max_calls": 400, "max_output_tokens": 200000},
                                "review": {"max_calls": 150}})
    cfg = config_mod.load(project)
    assert cfg.budgets["step"] == {"max_calls": 400, "max_output_tokens": 200000}
    assert cfg.budgets["review"] == {"max_calls": 150}


def test_config_bad_budget_is_config_error(project: Path, clean_env):
    """Неизвестная роль, неизвестное поле, `0`, строка и `true` — `ConfigError` с путём."""
    cases = [
        ({"step": {"max_calls": 0}}, "budgets.step.max_calls"),
        ({"step": {"max_calls": "400"}}, "budgets.step.max_calls"),
        ({"step": {"max_calls": True}}, "budgets.step.max_calls"),
        ({"review": {"max_tokens": 10}}, "budgets.review"),
        ({"keeper": {"max_calls": 3}}, "budgets"),
        ("400", "budgets"),
    ]
    for value, expected in cases:
        write_cfg(project, budgets=value)
        with pytest.raises(config_mod.ConfigError) as err:
            config_mod.load(project)
        assert expected in str(err.value), value


def test_config_without_budgets_keeps_previous_behaviour(project: Path, clean_env):
    """Ключа нет — потолков нет, а бюджет прогонов не число: авто (прежнее поведение)."""
    write_cfg(project, budgets=None)
    cfg = config_mod.load(project)
    assert cfg.budgets == {}
    assert cfg.step_runs["budget"] is None
    assert gate.budget_probe(project_paths(project), ctx(cards=[])) == {}


# ─────────────────── §4.4: живой счёт идущей попытки ───────────────────

def test_live_row_open_window_ends_at_now(tmp_path: Path):
    """Счёт открытой попытки кончается на «сейчас»: строки после этого часа не в счёте."""
    db = tmp_path / "board.db"
    seed_card(db)
    run_id = add_run(db, started=at("10:00:19"))
    card = {"id": "t_step01", "assignee": "demo", "key": "planloop:1:S1:step"}

    row = step_metrics.live_row(db, card, now=at("10:00:25"), log_path=SAMPLE)
    assert (row["calls"], row["out"], row["model"]) == (1, 200, "deepseek-v4.1-flash")
    assert row["run_id"] == str(run_id) and row["attempt"] == 1 and row["role"] == "demo"
    assert row["started_at"] == at("10:00:19")

    # «Сейчас» раньше второго вызова: окно закрыто на моменте замера, а не на бесконечности.
    early = step_metrics.live_row(db, card, now=at("10:00:05"), log_path=SAMPLE)
    assert early["calls"] == 0 and early["out"] == 0
    # Закрытый прогон живым счётом не является.
    closed_db = tmp_path / "closed.db"
    seed_card(closed_db)
    add_run(closed_db, started=at("10:00:00"), ended=at("10:00:30"))
    assert step_metrics.live_row(closed_db, card, now=at("11:00:00"), log_path=SAMPLE) == {}


# ─────────────────── §4.5: проба бюджета ───────────────────

def test_budget_probe_without_budgets_is_empty(project: Path, hermes_stub: Path, clean_env,
                                               monkeypatch):
    """Пустой `budgets` — пустая проба, и база с логом не читаются вовсе."""
    def boom(*args, **kwargs):  # noqa: ANN002,ANN003 - подмена «читать нельзя»
        raise AssertionError("без budgets база читаться не должна")

    monkeypatch.setattr(step_metrics, "live_row", boom)
    p = project_paths(project)
    assert p.cfg.budgets == {}
    assert gate.budget_probe(p, ctx(cards=[{"id": "t_5", "status": "running",
                                           "key": "planloop:17:S5:step"}])) == {}


def test_budget_probe_role_from_card_kind(project: Path, hermes_stub: Path, clean_env,
                                          monkeypatch):
    """Вид карточки задаёт роль: `fix` — это `step`, `promptreview` — это `prompt`."""
    write_cfg(project, budgets={"step": {"max_calls": 400}, "prompt": {"max_calls": 200}})
    monkeypatch.setattr(step_metrics, "live_row",
                        lambda db, card, **kw: {"run_id": "9", "attempt": 2, "calls": 10,
                                                "out": 100})
    p = project_paths(project)

    fix = gate.budget_probe(p, ctx(cards=[{"id": "t_5", "status": "running",
                                          "key": "planloop:17:S5:fix:2"}]))
    assert (fix["role"], fix["kind"], fix["max_calls"], fix["over"]) == ("step", "fix", 400, "")
    prompt = gate.budget_probe(p, ctx(prev_cards=[{"id": "t_6", "status": "running",
                                                 "key": "planloop:17:S4:promptreview:1"}]))
    assert (prompt["role"], prompt["max_calls"], prompt["over"]) == ("prompt", 200, "")
    # Роль, которой в `budgets` нет, потолка не имеет — проба пуста.
    review = gate.budget_probe(p, ctx(cards=[{"id": "t_7", "status": "running",
                                             "key": "planloop:17:S5:review"}]))
    assert review == {}


# ─────────────────── §4.6: решение о стопе ───────────────────

def test_calls_over_budget_stops():
    """Вызовы сверх потолка — инцидентный стоп с числами, шагом, ролью и карточкой."""
    decision = gate.decide(ctx(cards=[{"id": "t_5", "status": "running",
                                      "key": "planloop:17:S5:step"}],
                               budget=over_budget("calls")))
    assert decision["action"] == "stop" and decision["kind"] == "incident"
    assert "бюджет исчерпан: вызовов 420 из 400" in decision["reason"]
    assert "S5" in decision["reason"] and "t_5" in decision["reason"]
    assert "budgets.step.max_calls" in decision["details"]


def test_output_tokens_over_budget_stops():
    """Токены вывода сверх потолка — тот же стоп, но с ключом `max_output_tokens`."""
    decision = gate.decide(ctx(cards=[{"id": "t_5", "status": "running",
                                      "key": "planloop:17:S5:step"}],
                               budget=over_budget("tokens")))
    assert decision["action"] == "stop" and decision["kind"] == "incident"
    assert "бюджет исчерпан: токенов вывода 210000 из 200000" in decision["reason"]
    assert "budgets.step.max_output_tokens" in decision["details"]
    assert "`--resume` этот стоп не снимет" in decision["details"]


def test_under_budget_keeps_idle():
    """Пустая проба или `over: ""` — прежнее поведение: идущий шаг остаётся `idle`."""
    cards = [{"id": "t_5", "status": "running", "key": "planloop:17:S5:step"}]
    assert gate.decide(ctx(cards=cards, budget={}))["action"] == "idle"
    under = dict(over_budget(), over="", calls=1, out=10)
    assert gate.decide(ctx(cards=cards, budget=under))["action"] == "idle"


def test_budget_stop_survives_runtime_guard():
    """Бюджет сильнее правила S4: его `idle` с `clear_incident` стоп бюджета не глотает."""
    cards = [{"id": "t_2", "status": "running", "key": "planloop:17:S5:step:2"}]
    attempts = [{"card": "t_2", "plan": "17", "step": "S5", "card_kind": "step",
                 "killed_by": "runtime", "limit_s": 3600, "escalated": True,
                 "model": "demo", "attempt": 2}]
    state = ctx(next={"id": "S5"}, cards=cards, attempts=attempts,
                runtime_guard={"enabled": True, "escalations": 2, "factor": 1.5})
    # Само правило S4 на этом состоянии молчит ожиданием и гасит инцидент.
    assert gate.runtime_guard_decision(state) == {
        "action": "idle", "step": "S5", "clear_incident": True,
        "reason": "повтор уже заказан: карточка t_2 (running), попытка убита по лимиту — "
                  "ждём её исхода"}
    decision = gate.decide(dict(state, budget=over_budget("calls")))
    assert decision["action"] == "stop" and decision["kind"] == "incident"


# ─────────────────── §4.7–4.8: место пробы на тике ───────────────────

@pytest.fixture()
def tick_stand(project: Path, hermes_stub: Path, monkeypatch, clean_env) -> Path:
    """Проект для полного тика: шаг без постановки, уведомления выключены."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    data = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    data["require_prompt"] = False
    data["notify"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(data, ensure_ascii=False),
                                          encoding="utf-8")
    return project


def _log_stamp(moment: float) -> str:
    """Метка строки лога в формате живого `agent.log`."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment)) + ",000"


def write_agent_log(calls: int = 2, out: int = 500) -> Path:
    """Лог профиля `demo` в подменённом `HERMES_HOME`: ровно `calls` вызовов модели.

    Метки — «сейчас минус N минут», а не дата фикстуры: проба живого счёта кончается на
    «сейчас», и лог недельной давности в окно идущей попытки не попал бы вовсе.
    """
    home = Path(os.environ["HERMES_HOME"])
    folder = home / "profiles" / "demo" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lines = [f"{_log_stamp(now - (calls - index + 1) * 60)} INFO [s] agent.conversation_loop: "
             f"API call #{index}: model=demo provider=demo in=100 out={out} "
             f"total={100 + out} latency=1.0s cache=0/100 (0%) id=g-{index}"
             for index in range(1, calls + 1)]
    path = folder / "agent.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def started_card(db: Path) -> int:
    """Карточка шага в работе (создана сейчас) и её открытый прогон; возвращает id прогона."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASKS)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
                    ("t_step01", "шаг", "running", "demo", "planloop:1:S1:step",
                     int(time.time())))
        con.commit()
    finally:
        con.close()
    return add_run(db, started=int(time.time()) - 660)


def test_budget_comment_once_per_run(tick_stand: Path, hermes_stub: Path, monkeypatch):
    """Переполнение даёт стоп и ОДИН комментарий с числами на прогон (не на тик)."""
    write_cfg(tick_stand, budgets={"step": {"max_calls": 1}})
    write_agent_log(calls=2)
    run_id = started_card(hermes_stub)
    p = project_paths(tick_stand)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(gate, "_comment", lambda paths, card_id, text: sent.append(
        (card_id, text)) or True)

    first = gate.run_gate(p, notify_fn=lambda text: None)
    assert first["decision"]["action"] == "stop"
    assert "бюджет исчерпан: вызовов 2 из 1" in first["decision"]["reason"]
    assert len(sent) == 1 and sent[0][0] == "t_step01"
    assert "бюджет исчерпан: вызовов 2 из 1" in sent[0][1]
    assert gate.load_state(p)["budget_reported"] == [str(run_id)]

    second = gate.run_gate(p, notify_fn=lambda text: None)
    assert second["decision"]["action"] == "stop"
    assert len(sent) == 1, "второй тик по тому же прогону молчит"


def test_status_does_not_probe_budget(tick_stand: Path, hermes_stub: Path, monkeypatch):
    """`--status` (`execute=False`) базу и логи не читает: проба не зовётся вовсе."""
    write_cfg(tick_stand, budgets={"step": {"max_calls": 1}})
    write_agent_log(calls=2)
    started_card(hermes_stub)
    p = project_paths(tick_stand)
    seen: list[str] = []

    def spy(paths, state):
        seen.append("probe")
        raise AssertionError("`--status` не имеет права мерить бюджет")

    monkeypatch.setattr(gate, "budget_probe", spy)
    result = gate.run_gate(p, execute=False, notify_fn=lambda text: None)
    assert seen == []
    assert result["decision"]["action"] != "stop"


def test_probe_failure_does_not_stop_tick(tick_stand: Path, hermes_stub: Path, monkeypatch):
    """Сбой пробы (нет базы, битый лог) — предупреждение, а не стоп: тик живёт."""
    write_cfg(tick_stand, budgets={"step": {"max_calls": 1}})
    started_card(hermes_stub)
    p = project_paths(tick_stand)
    sent: list[str] = []

    def boom(*args, **kwargs):  # noqa: ANN002,ANN003 - подмена сбоя замера
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(step_metrics, "live_row", boom)
    monkeypatch.setattr(gate, "_comment", lambda paths, card_id, text: sent.append(text) or True)
    result = gate.run_gate(p, notify_fn=lambda text: None)
    assert result["decision"]["action"] != "stop"
    assert sent == []


def test_under_budget_comments_nothing(tick_stand: Path, hermes_stub: Path, monkeypatch):
    """Попытка в пределах потолка не даёт ни комментария, ни отметки, ни стопа."""
    write_cfg(tick_stand, budgets={"step": {"max_calls": 400, "max_output_tokens": 200000}})
    write_agent_log(calls=2, out=500)
    started_card(hermes_stub)
    p = project_paths(tick_stand)
    sent: list[str] = []
    monkeypatch.setattr(gate, "_comment", lambda paths, card_id, text: sent.append(text) or True)

    result = gate.run_gate(p, notify_fn=lambda text: None)
    assert result["decision"]["action"] == "idle"
    assert sent == []
    assert gate.load_state(p).get("budget_reported") is None


# ─────────────────── §4.9–4.10: бюджет прогонов шага ───────────────────

def test_default_run_budget_is_sum_with_margin():
    """Ключа нет — бюджет равен полутора суммам объявленных таймаутов, не 600 с."""
    declared = [{"command": "pytest", "timeout": 900}, {"command": "ruff check ."}]
    settings = {"timeout": 60, "budget": None}
    assert gate.runs_budget_limit(settings, declared) == int(1.5 * (900 + 60))
    # Строка без команды не исполняется и в бюджет не входит.
    assert gate.runs_budget_limit(settings, [{"command": "", "timeout": 999}]) == 1


def test_explicit_budget_wins():
    """Заданное число главнее авто — и меньше него, и больше."""
    declared = [{"command": "pytest", "timeout": 900}]
    assert gate.runs_budget_limit({"timeout": 120, "budget": 600}, declared) == 600
    assert gate.runs_budget_limit({"timeout": 120, "budget": 7200}, declared) == 7200


# ─────────────────── §4.11: `plan --check` против противоречивого бюджета ───────────────────

DRAFT = """# 9. Черновик с долгими прогонами

## Шаги

### S1. Шаг с двумя прогонами (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py`
- Прогон: `python -m pytest -m "not slow"` → exit 0; таймаут 900 сек
- Прогон: `ruff check .` → exit 0; таймаут 900 сек
- Готово = оба прогона зелёные

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


def draft_file(project: Path) -> Path:
    """Черновик плана в каталоге черновиков проекта теста."""
    folder = Path(project) / "docs" / "plans" / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "9-draft.md"
    path.write_text(DRAFT, encoding="utf-8")
    return path


def test_check_budget_vs_timeout(project: Path, clean_env):
    """Таймаут прогона больше бюджета — ошибка черновика с именем ключа и шага."""
    write_cfg(project, step_runs={"timeout": 60, "budget": 600})
    errors = plan_accept.check_draft(project, draft_file(project))
    assert any("шаг S1" in text and "таймаут прогона 900 с больше step_runs.budget=600 с" in text
               for text in errors), errors


def test_check_budget_vs_sum_of_timeouts(project: Path, clean_env):
    """Сумма таймаутов по шагу больше бюджета — тоже ошибка: второй прогон уйдёт в скип."""
    write_cfg(project, step_runs={"timeout": 60, "budget": 1200})
    errors = plan_accept.check_draft(project, draft_file(project))
    assert not any("таймаут прогона 900 с больше" in text for text in errors), errors
    assert any("сумма объявленных таймаутов прогонов 1800 с больше step_runs.budget=1200 с"
               in text for text in errors), errors


def test_check_without_budget_key_passes(project: Path, clean_env):
    """Ключа бюджета нет — авто, и спорить ему не с чем: ошибок бюджета нет."""
    write_cfg(project, step_runs={"timeout": 60})
    errors = plan_accept.check_draft(project, draft_file(project))
    assert not any("step_runs.budget" in text for text in errors), errors
