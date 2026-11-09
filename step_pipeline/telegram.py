# -*- coding: utf-8 -*-
"""Telegram Bot API для уведомлений конвейера: проверка токена, поиск чата, отправка.

Никакого Hermes-гейтвея: конвейер шлёт сообщения напрямую, поэтому уведомления
работают и из cron-джобы `--no-agent`, где агента нет вовсе.
"""
from __future__ import annotations

import functools
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
# Прокси Bot API (Cloudflare-воркер) отдаёт тот же путь `/bot<токен>/<метод>`, поэтому
# подменяется только хост. Свой ключ конвейера сильнее общего ключа Hermes: там прокси уже
# может быть настроен для шлюза, и конвейер обязан ходить тем же путём (см.
# `docs/TELEGRAM-PROXY.md`).
API_ENV = "STEP_PIPELINE_TELEGRAM_API_BASE"
API_ENV_SHARED = "TELEGRAM_API_BASE_URL"
# Cloudflare-воркер за Cloudflare-защитой режет `Python-urllib/x.y` (ошибка 1010),
# поэтому клиент представляется собой, а не стандартной библиотекой.
USER_AGENT = "step-pipeline (+https://github.com/SMHotey/hermes-step-pipeline)"
DEFAULT_TIMEOUT = 20


def api_template() -> str:
    """Шаблон адреса Bot API с учётом прокси из окружения.

    Пусто — прямой `api.telegram.org`. Значение — база вида `https://<воркер>` (хвостовой
    слэш не важен): путь `/bot<токен>/<метод>` дописывается как обычно. База, уже
    оканчивающаяся на `/bot` (так её пишет шлюз Hermes), не дублирует префикс.
    """
    base = (os.environ.get(API_ENV) or os.environ.get(API_ENV_SHARED) or "").strip()
    if not base:
        return API
    base = base.rstrip("/")
    if base.endswith("/bot"):
        return base + "{token}/{method}"
    return base + "/bot{token}/{method}"


# Python без явного набора корней доверяет хранилищу ОС, а в нём бывают просроченные
# сертификаты (в Windows — DST Root CA X3 и другие старые корни). OpenSSL тогда строит
# через просроченный корень путь для Let's Encrypt, и запрос падает с
# `CERTIFICATE_VERIFY_FAILED: certificate has expired`, хотя сертификат сервера жив.
# Поэтому корни берём из явного набора: переменная окружения, иначе certifi из Hermes.
CA_ENV_VARS = ("STEP_PIPELINE_CA_BUNDLE", "HERMES_CA_BUNDLE", "SSL_CERT_FILE",
               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def ca_bundle() -> str:
    """Путь к набору корневых сертификатов; пусто — доверять хранилищу ОС."""
    for name in CA_ENV_VARS:
        path = (os.environ.get(name) or "").strip()
        if path and os.path.isfile(path):
            return path
    try:
        import certifi
    except ImportError:
        return ""
    path = certifi.where()
    return path if os.path.isfile(path) else ""


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Контекст TLS с найденным набором корней (строится один раз на процесс)."""
    bundle = ca_bundle()
    return ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()


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
    request = urllib.request.Request(api_template().format(token=token, method=method),
                                     data=data, headers={"User-Agent": USER_AGENT})
    attempts = RETRIES if retries is None else max(1, int(retries))
    last: TelegramError | None = None
    for attempt in range(attempts):
        retry_left = attempt + 1 < attempts
        try:
            with urllib.request.urlopen(request, timeout=http_timeout,
                                        context=ssl_context()) as response:
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


def test_message(prefix: str = "") -> str:
    """Текст тестового сообщения: префикс проекта, если задан в pipeline.json."""
    base = "🧪 Тест уведомлений конвейера."
    return f"{prefix}{base}" if prefix else base
