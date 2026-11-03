# -*- coding: utf-8 -*-
"""Telegram Bot API: проверка токена, отправка, поиск чата — на локальном фейковом сервере."""
from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402,F401  (загружает плагин как пакет)
import telegram  # noqa: E402


class FakeBot(BaseHTTPRequestHandler):
    sent: list[dict] = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        parts = self.path.strip("/").split("/")
        token = parts[0].removeprefix("bot") if parts else ""
        method = parts[1] if len(parts) > 1 else ""
        params = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        FakeBot.sent.append({"method": method, "params": params})
        if token == "bad":
            return self._json(401, {"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return self._json(200, {"ok": True, "result": {"id": 1, "username": "demo_bot"}})
        if method == "getUpdates":
            return self._json(200, {"ok": True, "result": [
                {"update_id": 7, "message": {"chat": {"id": 4242}, "text": "/start"}}]})
        if method == "getChat":
            return self._json(200, {"ok": True, "result": {"id": params.get("chat_id")}})
        if method == "getWebhookInfo":
            return self._json(200, {"ok": True, "result": {"url": ""}})
        if method == "sendMessage":
            return self._json(200, {"ok": True, "result": {"message_id": 1}})
        return self._json(404, {"ok": False, "description": "not found"})

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def bot(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeBot)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    FakeBot.sent.clear()
    monkeypatch.setattr(telegram, "API",
                        f"http://127.0.0.1:{httpd.server_address[1]}/bot{{token}}/{{method}}")
    try:
        yield FakeBot
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_get_me(bot):
    assert telegram.get_me("good")["username"] == "demo_bot"


def test_bad_token_raises_with_description(bot):
    with pytest.raises(telegram.TelegramError, match="Unauthorized"):
        telegram.get_me("bad")


def test_empty_token_raises():
    with pytest.raises(telegram.TelegramError, match="пустой токен"):
        telegram.get_me("  ")


def test_send_message(bot):
    assert telegram.send_message("good", "42", "привет") is True
    call = [c for c in bot.sent if c["method"] == "sendMessage"][-1]
    assert call["params"]["chat_id"] == "42"
    assert call["params"]["text"] == "привет"


def test_empty_message_not_sent(bot):
    assert telegram.send_message("good", "42", "   ") is False
    assert not [c for c in bot.sent if c["method"] == "sendMessage"]


def test_validate_chat(bot):
    assert telegram.validate_chat("good", "42")["id"] == "42"


def test_detect_chat(bot):
    assert telegram.detect_chat("good", wait_seconds=5) == "4242"


def test_get_webhook_info(bot):
    assert telegram.get_webhook_info("good") == {"url": ""}
    assert bot.sent[-1]["method"] == "getWebhookInfo"


def _response(payload: dict):
    """Подделка ответа urlopen: читается как контекстный менеджер и отдаёт JSON."""

    class Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Response()


def test_call_retries_network_failure(monkeypatch):
    """TLS handshake timeout — не повод терять команду: три попытки с паузами (D9)."""
    attempts: list[object] = []
    pauses: list[float] = []

    def fake_urlopen(request, timeout=20):
        attempts.append(request)
        if len(attempts) < 3:
            raise urllib.error.URLError("TLS handshake timeout")
        return _response({"ok": True, "result": {"id": 1}})

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(telegram.time, "sleep", lambda value: pauses.append(value))

    assert telegram.get_me("good")["id"] == 1
    assert len(attempts) == 3
    assert pauses == [0.5, 1.0]


def test_call_does_not_retry_bad_token(monkeypatch):
    """401 — окончательный отказ: повтор не нужен и не делается."""
    calls: list[object] = []

    def fake_urlopen(request, timeout=20):
        calls.append(request)
        raise urllib.error.HTTPError("https://bot.invalid/botgood/getMe", 401,
                                     "Unauthorized", {},
                                     io.BytesIO(json.dumps({"ok": False,
                                                            "description": "Unauthorized"}).encode()))

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(telegram.time, "sleep", lambda _value: pytest.fail("повтор не нужен"))

    with pytest.raises(telegram.TelegramError, match="Unauthorized"):
        telegram.get_me("good")
    assert len(calls) == 1


def test_detect_chat_survives_transient_error(monkeypatch):
    """Сетевой отказ одного опроса не обрывает ловлю chat id (D9)."""
    calls: list[str] = []

    def fake(token, method, http_timeout=15, **params):
        calls.append(method)
        if len(calls) == 1:
            raise telegram.TelegramError("сеть недоступна: TLS handshake timeout")
        return [{"update_id": 7, "message": {"chat": {"id": 4242}, "text": "/start"}}]

    monkeypatch.setattr(telegram, "_call", fake)
    monkeypatch.setattr(telegram.time, "sleep", lambda _value: None)

    assert telegram.detect_chat("good", wait_seconds=10) == "4242"
    assert calls == ["getUpdates", "getUpdates"]


def test_detect_chat_timeout(monkeypatch, bot):
    bot_updates = bot

    def empty(token):
        return []

    # патчим _call точечно: getUpdates без сообщений
    real = telegram._call

    def fake(token, method, http_timeout=20, **params):
        if method == "getUpdates":
            return []
        return real(token, method, http_timeout=http_timeout, **params)

    monkeypatch.setattr(telegram, "_call", fake)
    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)
    with pytest.raises(telegram.TelegramError, match="не увидел"):
        telegram.detect_chat("good", wait_seconds=5)
