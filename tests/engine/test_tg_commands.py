# -*- coding: utf-8 -*-
"""Команды владельца из чата: разбор `/status`, `/stop`, `/resume`, `/hold` и опрос getUpdates.

Telegram не трогаем: `_tg_call` подменяется подделкой, поэтому тесты работают без сети.
"""
from __future__ import annotations

import json
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

import config as config_mod
import gate

CHAT = "555000111"


@pytest.fixture()
def cfg(project: Path, monkeypatch) -> config_mod.Config:
    monkeypatch.setenv("STEP_TEST_TOKEN", "123456:TEST")
    monkeypatch.setenv("STEP_TEST_CHAT", CHAT)
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["notify"] = {"enabled": True, "token_env": "STEP_TEST_TOKEN",
                     "chat_env": "STEP_TEST_CHAT", "prefix": ""}
    raw["default_plan"] = "1"
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return config_mod.load(project)


class FakeTG:
    """Подделка Bot API: отдаёт заранее заданные обновления и запоминает ответы."""

    def __init__(self, updates=None, fail=None):
        self.updates = list(updates or [])
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token, method, http_timeout=20, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            return dict(self.fail) if self.fail else {"ok": True, "result": self.updates}
        return {"ok": True, "result": {}}

    def answers(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]


def update(text: str, chat: str = CHAT, uid: int = 1, user: int | None = None) -> dict:
    msg = {"text": text, "chat": {"id": chat}}
    if user is not None:
        msg["from"] = {"id": user}
    return {"update_id": uid, "message": msg}


def test_help_lists_commands(cfg):
    answer = gate.handle_command(cfg, "/help")
    assert "/status" in answer and "/stop" in answer and "/resume" in answer


def test_bot_suffix_in_command_is_stripped(cfg):
    assert gate.handle_command(cfg, "/help@pipeline_bot") == gate.TG_HELP


def test_unknown_command_says_so(cfg):
    assert "не знаю" in gate.handle_command(cfg, "/deploy")


def test_stop_command_sets_owner_flag(cfg):
    answer = gate.handle_command(cfg, "/stop перерыв на обед")
    p = gate.resolve_paths(cfg, "1")
    stop = gate.load_stop(p)
    assert stop["kind"] == "owner"
    assert stop["reason"] == "перерыв на обед"
    assert "стоп поставлен" in answer


def test_stop_without_reason_has_default(cfg):
    gate.handle_command(cfg, "/stop")
    stop = gate.load_stop(gate.resolve_paths(cfg, "1"))
    assert stop["reason"]


def test_resume_clears_flag(cfg):
    p = gate.resolve_paths(cfg, "1")
    gate.write_stop(p, "было", "owner")
    assert "снят" in gate.handle_command(cfg, "/resume")
    assert gate.load_stop(p) is None
    assert "не было" in gate.handle_command(cfg, "/resume")


def test_hold_and_unhold_change_state(cfg):
    p = gate.resolve_paths(cfg, "1")
    assert "S2" in gate.handle_command(cfg, "/hold s2")
    assert gate.load_state(p)["hold_steps"] == ["S2"]
    assert "—" in gate.handle_command(cfg, "/unhold S2")
    assert gate.load_state(p)["hold_steps"] == []


def test_unhold_from_chat_marks_released(cfg):
    """Снятие допуска из чата — то же действие владельца, что `--unhold` (D4).

    Отметку `hold_released` пишут оба пути: без неё автоматический риск-hold вернулся бы
    на следующем тике и шаг держался бы вечно (план 2 §S4).
    """
    p = gate.resolve_paths(cfg, "1")
    gate.handle_command(cfg, "/hold S1")
    gate.handle_command(cfg, "/unhold S1")
    state = gate.load_state(p)
    assert state["hold_steps"] == []
    assert state["hold_released"] == ["S1"]


def test_status_reports_stop_and_plans(cfg, monkeypatch):
    monkeypatch.setattr(gate, "plans_table", lambda _cfg: "| план | шаг |\n| 1 | S1 |")
    p = gate.resolve_paths(cfg, "1")
    gate.write_stop(p, "инцидент из теста", "gate")

    answer = gate.tg_status(cfg)
    assert "Конвейер: demo" in answer
    assert "инцидент" in answer
    assert "| план | шаг |" in answer


def test_poll_executes_command_and_sends_answer(cfg, monkeypatch):
    fake = FakeTG([update("/status")])
    monkeypatch.setattr(gate, "_tg_call", fake)
    result = gate.poll_commands(cfg)

    assert result["ok"] is True
    assert result["handled"] == 1
    assert fake.answers() and "Конвейер" in fake.answers()[0]
    assert fake.calls[0][0] == "getUpdates"


def test_poll_ignores_foreign_chat(cfg, monkeypatch):
    fake = FakeTG([update("/stop взлом", chat="999999")])
    monkeypatch.setattr(gate, "_tg_call", fake)
    result = gate.poll_commands(cfg)

    assert result["handled"] == 0
    assert result["ignored"] == 1
    assert gate.load_stop(gate.resolve_paths(cfg, "1")) is None
    assert fake.answers() == []


def test_poll_remembers_offset(cfg, monkeypatch):
    fake = FakeTG([update("/help", uid=41)])
    monkeypatch.setattr(gate, "_tg_call", fake)
    gate.poll_commands(cfg)

    stored = json.loads((Path(cfg.state_dir) / "tg_offset.json").read_text(encoding="utf-8"))
    assert stored["offset"] == 42
    assert fake.calls[0][1]["offset"] is None


def test_poll_passes_saved_offset(cfg, monkeypatch):
    fake = FakeTG([])
    monkeypatch.setattr(gate, "_tg_call", fake)
    gate.poll_commands(cfg)
    gate._write_offset(gate._offset_path(cfg), 77)
    gate.poll_commands(cfg)
    assert fake.calls[-1][1]["offset"] == 77


def test_poll_reports_webhook_conflict(cfg, monkeypatch):
    fake = FakeTG(fail={"ok": False, "error_code": 409,
                        "description": "Conflict: can't use getUpdates method"})
    monkeypatch.setattr(gate, "_tg_call", fake)
    # Ветка 409 зовёт notify: без подмены тест делает настоящий sendMessage.
    monkeypatch.setattr(gate, "notify", lambda _cfg, _text: True)
    result = gate.poll_commands(cfg)

    assert result["ok"] is False
    assert "вебхук" in result["reason"]


def test_poll_without_token_is_not_an_error(cfg, monkeypatch):
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    result = gate.poll_commands(cfg)
    assert result["ok"] is False
    assert "токен" in result["reason"]


def test_poll_can_be_disabled(cfg, project: Path, monkeypatch):
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["notify"]["commands"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    cfg2 = config_mod.load(project)
    fake = FakeTG([update("/status")])
    monkeypatch.setattr(gate, "_tg_call", fake)

    result = gate.poll_commands(cfg2)
    assert result["handled"] == 0
    assert "выключен" in result["reason"]
    assert fake.calls == []


def test_free_text_goes_to_agent(cfg, monkeypatch):
    tg = FakeTG([update("что там с планом?", uid=21, user=555000111)])
    monkeypatch.setenv("STEP_PIPELINE_TELEGRAM_OWNER_ID", CHAT)
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "ask_agent",
                        lambda _cfg, text, _chat, timeout=240: "ответ: " + text)
    res = gate.poll_commands(cfg, wait=0)
    assert res["handled"] == 1
    assert tg.answers() == ["ответ: что там с планом?"]


def test_foreign_user_is_ignored(cfg, monkeypatch):
    tg = FakeTG([update("/status", uid=22, user=999)])
    monkeypatch.setenv("STEP_PIPELINE_TELEGRAM_OWNER_ID", CHAT)
    monkeypatch.setattr(gate, "_tg_call", tg)
    res = gate.poll_commands(cfg, wait=0)
    assert res["handled"] == 0 and res["ignored"] == 1
    assert tg.answers() == []


def test_ask_agent_runs_oneshot_session_per_chat(cfg, monkeypatch):
    seen = {}

    class Done:
        stdout = "ответ агента"
        stderr = ""

    def fake_run(cmd, cwd=None, timeout=0, env=None):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate.ask_agent(cfg, "привет", CHAT) == "ответ агента"
    assert "chat" in seen["cmd"] and "--oneshot" in seen["cmd"]
    assert any(str(arg).startswith(f"tg-{CHAT}-") for arg in seen["cmd"])


def test_clean_agent_output_strips_service_noise():
    crlf = chr(13) + chr(10)
    esc = chr(27)
    raw = (crlf + "session_id: 20261103_1" + crlf + esc + "[32mОтвет" + esc + "[0m агента"
           + crlf + "вторая строка" + crlf)
    assert gate.clean_agent_output(raw) == "Ответ агента" + chr(10) + "вторая строка"


def test_clean_agent_output_keeps_plain_text():
    assert gate.clean_agent_output("просто текст") == "просто текст"


def test_clean_agent_output_drops_resume_notice():
    raw = "ответ" + chr(10) + "↻ Resumed session 20261103_1 (1 user message)"
    assert gate.clean_agent_output(raw) == "ответ"


def test_split_message_respects_limit():
    parts = gate.split_message("x" * 8000, limit=3500)
    assert len(parts) == 3 and all(len(x) <= 3500 for x in parts)


def test_split_message_keeps_lines_together():
    text = chr(10).join(["строка"] * 1000)
    parts = gate.split_message(text, limit=3500)
    assert all(len(x) <= 3500 for x in parts) and len(parts) > 1


def test_ask_agent_uses_cli_mode(cfg, monkeypatch):
    seen = {}

    class Done:
        stdout = "ответ"
        stderr = ""

    def fake_run(cmd, cwd=None, timeout=0, env=None):
        seen["cmd"], seen["env"] = cmd, env or {}
        return Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    gate.ask_agent(cfg, "привет", CHAT)
    assert "--cli" in seen["cmd"] and seen["env"].get("NO_COLOR") == "1"


# ───────────────────────── S4: диалог, /status, 409 ─────────────────────────

def test_status_includes_gate_decision(cfg, monkeypatch):
    """`/status` отвечает решением стража и таблицей планов — это и есть «что дальше»."""
    monkeypatch.setattr(gate, "plans_table", lambda _cfg: "| план | шаг |\n| 1 | S1 |")
    monkeypatch.setattr(gate, "run_gate", lambda p, execute=False: {
        "decision": {"action": "create_step", "reason": "карточки шага нет"},
        "applied": False, "detail": ""})

    answer = gate.tg_status(cfg)
    assert "Решение стража (план 1): create_step — карточки шага нет" in answer
    assert "| план | шаг |" in answer


def test_ask_agent_caps_turns_budget_and_toolsets(cfg, monkeypatch):
    """Потолки D3 в командной строке: агент отвечает, а не уходит разбирать проект."""
    seen = {}

    class Done:
        stdout = "ответ агента"
        stderr = ""

    def fake_run(cmd, cwd=None, timeout=0, env=None):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate.ask_agent(cfg, "привет", CHAT) == "ответ агента"

    cmd = seen["cmd"]
    pairs = list(zip(cmd, cmd[1:]))
    assert ("--max-turns", "8") in pairs
    assert ("--run-budget", "200") in pairs
    assert ("-t", "web,file,skills,todo,memory,session_search") in pairs
    for flag in ("--oneshot", "--cli", "--create-if-missing"):
        assert flag in cmd
    assert "-Q" not in cmd
    assert cmd[cmd.index("--continue") + 1].startswith(f"tg-{CHAT}-")

    stored = json.loads((Path(cfg.state_dir) / "tg_dialog.json").read_text(encoding="utf-8"))
    assert stored[CHAT]["messages"] == 2


def test_ask_agent_timeout_is_human_message(cfg, monkeypatch):
    """Срез по времени — человеческая строка с частичным выводом, без repr исключения."""
    def boom(cmd, cwd=None, timeout=0, env=None):
        raise subprocess.TimeoutExpired(cmd=["hermes", "chat"], timeout=240,
                                        output="начало ответа")

    monkeypatch.setattr(gate, "_run", boom)
    answer = gate.ask_agent(cfg, "что случилось?", CHAT)

    assert "/reset" in answer
    assert "начало ответа" in answer
    assert "Command" not in answer
    assert "240" not in answer
    assert "hermes" not in answer


def test_ask_agent_launch_failure_has_no_command_line(cfg, monkeypatch):
    """Сбой запуска CLI — тоже человеческая строка, а не repr трейсбека."""
    def boom(cmd, cwd=None, timeout=0, env=None):
        raise FileNotFoundError("hermes не найден")

    monkeypatch.setattr(gate, "_run", boom)
    answer = gate.ask_agent(cfg, "привет", CHAT)

    assert "сбой запуска" in answer and "/reset" in answer
    assert "hermes" not in answer
    assert "No such file" not in answer
    assert "Traceback" not in answer


def test_clean_agent_output_drops_console_noise():
    """Шапка профиля и строки инструментов из `--cli` ответа выкидываются (D4)."""
    raw = chr(10).join(["┌ Hermes Agent v0.6.0 ┐",
                        "│ Provider: opencode-go │",
                        "⚙ read_file (path=x)",
                        "Ответ по делу"])
    assert gate.clean_agent_output(raw) == "Ответ по делу"


def test_reset_starts_new_dialog(cfg, monkeypatch):
    """`/reset` — новая сессия: следующий вопрос уходит в `-2`, а не в разросшуюся `-1`."""
    seen: list[str] = []

    class Done:
        stdout = "ответ"
        stderr = ""

    def fake_run(cmd, cwd=None, timeout=0, env=None):
        seen.append(cmd[cmd.index("--continue") + 1])
        return Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    gate.ask_agent(cfg, "первый вопрос", CHAT)
    assert seen[0].startswith(f"tg-{CHAT}-") and seen[0].endswith("-1")

    answer = gate.handle_command(cfg, "/reset", chat=CHAT)
    assert "диалог сброшен" in answer

    gate.ask_agent(cfg, "второй вопрос", CHAT)
    assert seen[1].endswith("-2")
    stored = json.loads((Path(cfg.state_dir) / "tg_dialog.json").read_text(encoding="utf-8"))
    assert stored[CHAT]["messages"] == 2


def test_dialog_rotates_by_messages_and_tokens(cfg, monkeypatch):
    """Ротация D6: 40 сообщений или 120k токенов — новая сессия, счётчики с нуля."""
    seen: list[str] = []

    class Done:
        stdout = "ответ"
        stderr = ""

    def fake_run(cmd, cwd=None, timeout=0, env=None):
        seen.append(cmd[cmd.index("--continue") + 1])
        return Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    today = time.strftime("%Y%m%d")
    path = Path(cfg.state_dir) / "tg_dialog.json"

    def store(serial: int, messages: int, tokens: int) -> None:
        path.write_text(json.dumps({CHAT: {"chat": CHAT, "date": today, "serial": serial,
                                           "messages": messages, "tokens": tokens,
                                           "name": f"tg-{CHAT}-{today}-{serial}"}},
                                   ensure_ascii=False), encoding="utf-8")

    store(3, 39, 0)                       # (а) сообщений 39: +2 = 41 > 40
    gate.ask_agent(cfg, "короткий вопрос", CHAT)
    assert seen[-1].endswith("-4")
    assert json.loads(path.read_text(encoding="utf-8"))[CHAT]["messages"] == 2

    store(5, 0, 119000)                   # (б) токенов 119k + вопрос на 9 000 символов
    gate.ask_agent(cfg, "в" * 9000, CHAT)
    assert seen[-1].endswith("-6")        # ротация по токенам, а не по сообщениям
    after = json.loads(path.read_text(encoding="utf-8"))[CHAT]
    assert after["messages"] == 2 and 0 < after["tokens"] < 120000   # счётчики с нуля


def test_poll_conflict_warns_once_and_stops_polling(cfg, monkeypatch):
    """409 в тике: одно предупреждение, маркер, и приём команд не трогает сеть."""
    fake = FakeTG(fail={"ok": False, "error_code": 409,
                        "description": "Conflict: can't use getUpdates method"})
    monkeypatch.setattr(gate, "_tg_call", fake)
    warned: list[str] = []
    monkeypatch.setattr(gate, "notify", lambda _cfg, text: warned.append(text) or True)

    first = gate.poll_commands(cfg)
    assert first["disabled"] is True and first["ok"] is False
    assert len(warned) == 1
    marker = Path(cfg.state_dir) / "tg_commands_off.json"
    assert marker.is_file()

    calls_before = len(fake.calls)
    second = gate.poll_commands(cfg)
    assert second["disabled"] is True
    assert len(fake.calls) == calls_before      # сети не касались
    assert len(warned) == 1                     # и не спамили

    assert gate.clear_commands_off(cfg) is True
    assert not marker.exists()
    gate.poll_commands(cfg)
    assert len(fake.calls) > calls_before        # после сброса опрос снова идёт


def test_poll_logs_chat_actions_to_history(cfg, monkeypatch):
    """Действия из чата попадают в историю тиков (S3): план, вид, длительность."""
    fake = FakeTG([update("/status", uid=31), update("что случилось?", uid=32)])
    monkeypatch.setattr(gate, "_tg_call", fake)
    monkeypatch.setattr(gate, "ask_agent",
                        lambda _cfg, text, _chat, timeout=240: "ответ: " + text)

    assert gate.poll_commands(cfg)["handled"] == 2

    rows = [json.loads(line) for line
            in gate.history_path(cfg).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["action"] for row in rows] == ["tg_command", "tg_dialog"]
    assert all(row["plan"] == "1" and row["step"] == "" for row in rows)
    assert rows[0]["detail"].startswith("/status")
    assert rows[1]["detail"].startswith("что случилось?")


# ───────────────── S12: длинные ответы, «печатает», защита ─────────────────

def _with_rate(project: Path, max_msgs: int, window: int = 60) -> config_mod.Config:
    """Конфиг проекта с явным лимитом частоты: ключ необязательный, поэтому пишем его сами."""
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["notify"]["commands"] = {"rate_limit": {"max": max_msgs, "window": window}}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return config_mod.load(project)


def test_ask_agent_returns_full_answer(cfg, monkeypatch):
    """Длинный ответ агента возвращается целиком: обрезка в ask_agent снята."""

    class Done:
        stdout = "о" * 8000
        stderr = ""

    monkeypatch.setattr(gate, "_run", lambda cmd, cwd=None, timeout=0, env=None: Done())
    assert len(gate.ask_agent(cfg, "привет", CHAT)) == 8000


def test_long_dialog_answer_goes_in_several_messages(cfg, monkeypatch):
    """Ответ в 8000 символов уходит тремя сообщениями, ни один кусок не больше лимита."""
    tg = FakeTG([update("расскажи подробно", uid=51)])
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "x" * 8000)

    result = gate.poll_commands(cfg, wait=0)

    assert len(tg.answers()) == 3
    assert all(len(chunk) <= gate.TG_TEXT_LIMIT for chunk in tg.answers())
    assert "".join(tg.answers()) == "x" * 8000
    assert result["handled"] == 1


def test_long_command_answer_goes_in_several_messages(cfg, monkeypatch):
    """Командная ветка тоже шлёт ответ целиком — терять хвост из-за обрезки она не должна."""
    tg = FakeTG([update("/plans", uid=52)])
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "plans_table", lambda _cfg: "y" * 8000)

    assert gate.poll_commands(cfg, wait=0)["handled"] == 1

    assert len(tg.answers()) == 3
    assert all(len(chunk) <= gate.TG_TEXT_LIMIT for chunk in tg.answers())


def test_typing_sent_before_agent_answer(cfg, monkeypatch):
    """«Печатает» уходит до ответа агента, а не после."""
    tg = FakeTG([update("привет", uid=53)])
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "ответ")

    result = gate.poll_commands(cfg, wait=0)

    typing = [i for i, (method, params) in enumerate(tg.calls)
              if method == "sendChatAction" and params.get("action") == "typing"
              and params.get("chat_id") == CHAT]
    first_answer = [i for i, (method, _params) in enumerate(tg.calls)
                    if method == "sendMessage"]
    assert typing and first_answer and typing[0] < first_answer[0]
    assert result["handled"] == 1


def test_typing_keepalive_repeats_until_stopped(monkeypatch):
    """Поток «печатает» живёт до stop_event и не оставляет работы: три вызова, ни одного ответа."""
    stop_event = threading.Event()
    calls: list[tuple[str, dict]] = []

    def fake(_token, method, http_timeout=20, **params):
        calls.append((method, params))
        if len(calls) == 3:
            stop_event.set()
        return {"ok": True, "result": {}}

    monkeypatch.setattr(gate, "_tg_call", fake)
    gate._typing_keepalive("123456:TEST", CHAT, stop_event, interval=0)

    assert [method for method, _params in calls] == ["sendChatAction"] * 3
    assert all(params.get("action") == "typing" for _method, params in calls)


def test_typing_failure_does_not_block_answer(cfg, monkeypatch):
    """Сбой sendChatAction — косметика: ответ владельцу всё равно доставлен."""

    class FailingTyping(FakeTG):
        def __call__(self, token, method, http_timeout=20, **params):
            if method == "sendChatAction":
                self.calls.append((method, params))
                return {"ok": False, "description": "Bad Request: chat not found"}
            return super().__call__(token, method, http_timeout=http_timeout, **params)

    tg = FailingTyping([update("привет", uid=54)])
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "ответ")

    result = gate.poll_commands(cfg, wait=0)

    assert tg.answers()
    assert result["handled"] == 1


def test_rate_limit_drops_extra_free_text(cfg, project: Path, monkeypatch):
    """Лишний свободный текст не запускает агента; о первом отсеве — одно предупреждение."""
    cfg2 = _with_rate(project, max_msgs=3)
    tg = FakeTG([update(f"вопрос {n}", uid=60 + n) for n in range(1, 7)])
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "ответ")

    result = gate.poll_commands(cfg2, wait=0)

    assert result["handled"] == 3
    assert result["limited"] == 3
    assert len(tg.answers()) == 4
    assert "3" in tg.answers()[-1] and "60" in tg.answers()[-1]


def test_rate_limit_does_not_apply_to_commands(cfg, project: Path, monkeypatch):
    """Команды с «/» квоту не тратят: аварийный /stop проходит всегда."""
    cfg2 = _with_rate(project, max_msgs=1)
    tg = FakeTG([update("/status", uid=71), update("/status", uid=72), update("/status", uid=73)])
    monkeypatch.setattr(gate, "_tg_call", tg)

    result = gate.poll_commands(cfg2, wait=0)

    assert result["handled"] == 3
    assert result["limited"] == 0


def test_rate_limit_window_resets(cfg, project: Path, monkeypatch):
    """Окно считается по _now: после window секунд квота начинается заново."""
    cfg2 = _with_rate(project, max_msgs=3)
    clock = {"now": 1000.0}
    monkeypatch.setattr(gate, "_now", lambda: clock["now"])
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "ответ")
    tg = FakeTG([update(f"вопрос {n}", uid=80 + n) for n in range(1, 5)])
    monkeypatch.setattr(gate, "_tg_call", tg)

    first = gate.poll_commands(cfg2, wait=0)
    assert first["handled"] == 3 and first["limited"] == 1

    clock["now"] += 61
    tg.updates = [update("после окна", uid=90)]
    second = gate.poll_commands(cfg2, wait=0)
    assert second["handled"] == 1 and second["limited"] == 0


def test_rate_limit_quota_survives_new_poll(cfg, project: Path, monkeypatch):
    """Квота живёт в файле: cron запускает новый процесс, память процесса её не помнит."""
    cfg2 = _with_rate(project, max_msgs=5)
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "ответ")
    first = FakeTG([update(f"вопрос {n}", uid=100 + n) for n in range(1, 4)])
    monkeypatch.setattr(gate, "_tg_call", first)
    assert gate.poll_commands(cfg2, wait=0)["handled"] == 3

    second = FakeTG([update(f"ещё {n}", uid=110 + n) for n in range(1, 4)])
    monkeypatch.setattr(gate, "_tg_call", second)
    result = gate.poll_commands(cfg2, wait=0)

    assert result["handled"] == 2
    assert result["limited"] == 1


def test_rate_limit_zero_means_no_limit(cfg, project: Path, monkeypatch):
    """`max = 0` — лимит выключен: сообщения идут все, файла квоты нет."""
    cfg2 = _with_rate(project, max_msgs=0)
    tg = FakeTG([update(f"вопрос {n}", uid=120 + n) for n in range(1, 7)])
    monkeypatch.setattr(gate, "_tg_call", tg)
    monkeypatch.setattr(gate, "ask_agent", lambda _cfg, text, _chat, timeout=240: "ответ")

    result = gate.poll_commands(cfg2, wait=0)

    assert result["handled"] == 6
    assert result["limited"] == 0
    assert not (Path(cfg2.state_dir) / "tg_rate.json").exists()


def test_foreign_user_attempt_is_logged_with_ids(cfg, monkeypatch, capsys):
    """Чужой user id ничего не получает, но в stderr есть и его id, и id чата."""
    tg = FakeTG([update("/status", uid=131, user=999)])
    monkeypatch.setenv("STEP_PIPELINE_TELEGRAM_OWNER_ID", CHAT)
    monkeypatch.setattr(gate, "_tg_call", tg)

    result = gate.poll_commands(cfg, wait=0)

    err = capsys.readouterr().err
    assert tg.answers() == []
    assert result["ignored"] == 1
    assert "999" in err and CHAT in err


def test_foreign_chat_attempt_logged_without_user(cfg, monkeypatch, capsys):
    """Чужой чат без поля `from` логируется адресом чата, а не «?» вместо него."""
    tg = FakeTG([update("/status", uid=141, chat="999999")])
    monkeypatch.setattr(gate, "_tg_call", tg)

    gate.poll_commands(cfg, wait=0)

    err = capsys.readouterr().err
    assert "999999" in err and "?" in err
    assert tg.answers() == []


def test_help_mentions_all_commands_and_flood_guard():
    """Справка перечисляет все семь команд и называет защиту от флуда."""
    for command in ("/status", "/plans", "/stop", "/resume", "/hold", "/unhold", "/reset"):
        assert command in gate.TG_HELP
    assert "защита от флуда" in gate.TG_HELP


def test_ssl_context_prefers_bundle_from_env(monkeypatch, tmp_path):
    """Корни берём из явного набора: просроченные корни хранилища ОС ломают Let's Encrypt."""
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))

    assert gate.ca_bundle() == str(bundle)


def test_ssl_context_builds_without_bundle(monkeypatch):
    """Без переменных окружения контекст всё равно строится (certifi или хранилище ОС)."""
    for name in gate.CA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    gate.ssl_context.cache_clear()
    try:
        assert isinstance(gate.ssl_context(), ssl.SSLContext)
    finally:
        gate.ssl_context.cache_clear()
