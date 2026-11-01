# -*- coding: utf-8 -*-
"""Telegram Bot API для уведомлений конвейера: проверка токена, поиск чата, отправка.

Никакого Hermes-гейтвея: конвейер шлёт сообщения напрямую, поэтому уведомления
работают и из cron-джобы `--no-agent`, где агента нет вовсе.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_TIMEOUT = 20


class TelegramError(RuntimeError):
    """Ошибка Bot API с человеческим текстом (не 200, description, сеть)."""


def _call(token: str, method: str, http_timeout: int = DEFAULT_TIMEOUT, **params) -> dict:
    token = (token or "").strip()
    if not token:
        raise TelegramError("пустой токен бота")
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    request = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            raise TelegramError(payload.get("description") or f"HTTP {exc.code}") from exc
        except (ValueError, json.JSONDecodeError):
            raise TelegramError(f"HTTP {exc.code} от Telegram") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"сеть недоступна: {exc}") from exc
    if not payload.get("ok"):
        raise TelegramError(payload.get("description") or "Telegram ответил не ok")
    return payload.get("result") or {}


def get_me(token: str) -> dict:
    """Проверка токена: возвращает {id, username, ...}."""
    return _call(token, "getMe")


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Отправить сообщение. Кириллица режется по лимиту 4096 символов."""
    body = (text or "").strip()
    if not body:
        return False
    _call(token, "sendMessage", chat_id=chat_id, text=body[:4000],
          disable_web_page_preview="true")
    return True


def validate_chat(token: str, chat_id: str) -> dict:
    """Проверить, что бот видит этот чат (getChat)."""
    return _call(token, "getChat", chat_id=chat_id)


def detect_chat(token: str, wait_seconds: int = 60, poll_every: float = 2.0) -> str:
    """Найти chat id по последнему сообщению боту (getUpdates).

    Сценарий мастера установки: «напиши боту /start» → ждём обновление → берём chat.id.
    """
    deadline = time.time() + max(5, wait_seconds)
    seen: set[str] = set()
    offset = 0
    while time.time() < deadline:
        updates = _call(token, "getUpdates", http_timeout=15, limit=20, offset=offset or None)
        for update in updates if isinstance(updates, list) else []:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            key = str(chat_id)
            if key in seen:
                continue
            seen.add(key)
            return key
        time.sleep(poll_every)
    raise TelegramError("не увидел ни одного сообщения боту — напиши боту /start и повтори")
