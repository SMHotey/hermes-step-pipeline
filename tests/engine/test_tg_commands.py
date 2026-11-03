# -*- coding: utf-8 -*-
"""Команды владельца из чата: разбор `/status`, `/stop`, `/resume`, `/hold` и опрос getUpdates.

Telegram не трогаем: `_tg_call` подменяется подделкой, поэтому тесты работают без сети.
"""
from __future__ import annotations

import json
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

    def fake_run(cmd, cwd=None, timeout=0):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate.ask_agent(cfg, "привет", CHAT) == "ответ агента"
    assert "chat" in seen["cmd"] and "--oneshot" in seen["cmd"]
    assert f"tg-{CHAT}" in seen["cmd"]
