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
# D9: три попытки на сетевых отказах и 5xx, паузы между ними — 0.5 и 1.0 с.
RETRIES = 3
RETRY_PAUSES = (0.5, 1.0)


class TelegramError(RuntimeError):
    """Ошибка Bot API с человеческим текстом (не 200, description, сеть)."""


def _pause(attempt: int) -> float:
    """Пауза перед следующей попыткой: последняя пауза повторяется."""
    return RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)]


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    """Описание отказа из тела ответа Telegram (или код, если тела нет)."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        return f"HTTP {exc.code} от Telegram"
    if isinstance(payload, dict) and payload.get("description"):
        return str(payload["description"])
    return f"HTTP {exc.code} от Telegram"


def _call(token: str, method: str, http_timeout: int = DEFAULT_TIMEOUT,
          retries: int | None = None, **params) -> dict:
    """Вызов Bot API с повтором на сетевых отказах и 5xx (D9).

    Всего до `retries` попыток (по умолчанию `RETRIES`), паузы 0.5 и 1.0 секунды.
    Повторяются только сетевые отказы (`URLError`, `TimeoutError`, `OSError`) и ответы
    5xx: 4xx (включая 401 и 429) — окончательный отказ, повтор ничего не изменит.
    """
    token = (token or "").strip()
    if not token:
        raise TelegramError("пустой токен бота")
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    request = urllib.request.Request(API.format(token=token, method=method), data=data)
    attempts = RETRIES if retries is None else max(1, int(retries))
    last: TelegramError | None = None
    for attempt in range(attempts):
        retry_left = attempt + 1 < attempts
        try:
            with urllib.request.urlopen(request, timeout=http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and retry_left:
                last = TelegramError(f"HTTP {exc.code} от Telegram")
                time.sleep(_pause(attempt))
                continue
            raise TelegramError(_http_error_text(exc)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = TelegramError(f"сеть недоступна: {exc}")
            if retry_left:
                time.sleep(_pause(attempt))
                continue
            raise last from exc
        if not payload.get("ok"):
            raise TelegramError(payload.get("description") or "Telegram ответил не ok")
        return payload.get("result") or {}
    raise last or TelegramError("Telegram не ответил")


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


def get_webhook_info(token: str) -> dict:
    """Вебхук бота (`{url, ...}`): при непустом url getUpdates отвечает 409 (D8)."""
    return _call(token, "getWebhookInfo")


def _poll_updates(token: str, offset: int) -> list:
    """Один getUpdates для мастера установки; сбой — TelegramError наружу."""
    updates = _call(token, "getUpdates", http_timeout=15, limit=20, offset=offset or None)
    return updates if isinstance(updates, list) else []


def detect_chat(token: str, wait_seconds: int = 60, poll_every: float = 2.0) -> str:
    """Найти chat id по последнему сообщению боту (getUpdates).

    Сценарий мастера установки: «напиши боту /start» → ждём обновление → берём chat.id.
    Сетевой сбой (`TelegramError`) одного опроса ловлю не обрывает: последняя ошибка
    запоминается, цикл идёт до deadline, и только в конце она отдаётся наружу (D9).
    """
    deadline = time.time() + max(5, wait_seconds)
    seen: set[str] = set()
    offset = 0
    last: TelegramError | None = None
    while time.time() < deadline:
        try:
            updates = _poll_updates(token, offset)
        except TelegramError as exc:
            last = exc
            time.sleep(poll_every)
            continue
        for update in updates:
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
    if last is not None:
        raise TelegramError(str(last))
    raise TelegramError("не увидел ни одного сообщения боту — напиши боту /start и повтори")


def detect_chat_and_owner(token: str, wait_seconds: int = 60,
                          poll_every: float = 2.0) -> tuple[str, str]:
    """Chat id и user id владельца по последнему сообщению боту (getUpdates)."""
    deadline = time.time() + max(5, wait_seconds)
    offset = 0
    last: TelegramError | None = None
    while time.time() < deadline:
        try:
            updates = _poll_updates(token, offset)
        except TelegramError as exc:
            last = exc
            time.sleep(poll_every)
            continue
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            user_id = (message.get("from") or {}).get("id")
            if chat_id is None:
                continue
            return str(chat_id), str(user_id or "")
        time.sleep(poll_every)
    if last is not None:
        raise TelegramError(str(last))
    raise TelegramError("сообщений боту нет: напиши боту /start и повтори")
