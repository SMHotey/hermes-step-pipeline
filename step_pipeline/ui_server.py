# -*- coding: utf-8 -*-
"""Локальный UI конвейера: stdlib-сервер, без сборки и зависимостей.

Маршруты:
    GET  /                 одностраничный интерфейс (ui/index.html)
    GET  /api/snapshot     JSON состояния: планы, шаги, карточки, журнал, решение
                           (отдаёт ETag и 304 при неизменённом снимке)
    GET  /api/events       поток событий (Server-Sent Events): снимок сразу и на изменение
    GET  /api/step         экран шага: контракт из плана, постановка, отчёт ревью,
                           `git show` коммита шага, карточки шага (?plan=<N>&id=<S>)
    GET  /api/file         текст файла проекта, только внутри корня (?path=<...>)
    POST /api/action       действия: tick, stop, resume, hold, unhold,
                           save_config, save_telegram, test_telegram
    POST /api/plan-mode    режим планировщика: {"on": true|false}, заголовок X-Pipeline-Panel
Слушает только localhost: это операторский инструмент владельца, без авторизации.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

try:
    from . import plan_mode
    from . import project as proj
    from . import setup_flow, snapshot as snap, telegram
except ImportError:  # pragma: no cover
    import plan_mode  # type: ignore
    import project as proj  # type: ignore
    import setup_flow  # type: ignore
    import snapshot as snap  # type: ignore
    import telegram  # type: ignore

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
CONFIG_KEYS = ("project", "board", "driven_plans", "require_prompt")
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
# Метка панели вместо токена: кастомный заголовок требует preflight, которого этот
# stdlib-сервер не отвечает, поэтому кросс-доменный POST его не пришлёт (чужой локальный
# процесс метку подделает — от него защищает шаг 1-S7, здесь такой цели нет).
PANEL_HEADER = "X-Pipeline-Panel"
PANEL_HEADER_VALUE = "1"
# Поток событий: опрос отпечатка, пинг и потолок жизни соединения. Короткая жизнь — это
# норма: клиент (EventSource) переподключается сам, вечных держателей не заводим.
EVENTS_POLL = 0.5
EVENTS_PING_SECONDS = 10
EVENTS_MAX_SECONDS = 25


def _allowed_origins(port: int) -> set[str]:
    """Источники, с которых панель вправе звать действия: только петля и свой порт."""
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}


def _json_bytes(payload: dict) -> bytes:
    """Байтовое тело JSON: по нему же считается ETag, поэтому сериализация одна."""
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _etag(body: bytes) -> str:
    """ETag тела: sha256, первые 32 hex в кавычках (браузеру нужны кавычки)."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _if_none_match(header: str | None) -> str:
    """Нормализовать `If-None-Match`: без пробелов, `W/` и кавычек."""
    value = (header or "").strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"').strip()


class State:
    """Разделяемое состояние сервера: проект, журнал действий, режим чтения."""

    def __init__(self, project: Path, preview: bool = False, board: str | None = None) -> None:
        self.project = project
        self.preview = preview
        self.board = board
        self.log: deque[str] = deque(maxlen=200)
        self.cache: dict[str, Any] = {}
        self.lock = threading.Lock()

    def note(self, text: str) -> None:
        with self.lock:
            self.log.append(text.strip()[:2000])

    def tail(self, limit: int = 40) -> list[str]:
        with self.lock:
            return list(self.log)[-limit:]


def _run_engine(state: State, *args: str, timeout: int = 900) -> dict:
    res = proj.run_engine(state.project, "gate.py", *args, timeout=timeout)
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    state.note(f"$ gate.py {' '.join(args)}\n{out or err}".strip())
    return {"ok": res.returncode == 0, "stdout": out, "stderr": err}


def act_tick(state: State, payload: dict) -> dict:
    plan = str(payload.get("plan") or "").strip()
    extra = ["--plan", plan] if plan else []
    result = _run_engine(state, "--run", *extra)
    result["snapshot"] = snap.snapshot(state.project, plan or None)
    return result


def act_stop(state: State, payload: dict) -> dict:
    reason = str(payload.get("reason") or "остановка владельца").strip()
    return _run_engine(state, "--stop", reason)


def act_resume(state: State, payload: dict) -> dict:
    return _run_engine(state, "--resume")


def act_hold(state: State, payload: dict) -> dict:
    step = str(payload.get("step") or "").strip()
    if not step:
        return {"ok": False, "error": "нужен шаг"}
    plan = str(payload.get("plan") or "").strip()
    # Без `--plan` ручной допуск записался бы плану по умолчанию: на вкладке «Шаг» выбран
    # может быть соседний план, поэтому его номер идёт в команду.
    extra = ["--plan", plan] if plan else []
    return _run_engine(state, "--hold", step, *extra)


def act_unhold(state: State, payload: dict) -> dict:
    step = str(payload.get("step") or "").strip()
    if not step:
        return {"ok": False, "error": "нужен шаг"}
    plan = str(payload.get("plan") or "").strip()
    extra = ["--plan", plan] if plan else []
    return _run_engine(state, "--unhold", step, *extra)


def act_save_config(state: State, payload: dict) -> dict:
    """Правка pipeline.json из формы: только известные ключи, значения валидируются."""
    patch = payload.get("config") or {}
    if not isinstance(patch, dict):
        return {"ok": False, "error": "config должен быть объектом"}
    cfg = proj.read_config(state.project)
    changed: list[str] = []
    for key in CONFIG_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "board":
            value = str(value).strip()
            if not value:
                return {"ok": False, "error": "борда не может быть пустой"}
        if key == "driven_plans":
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            if not isinstance(value, list):
                return {"ok": False, "error": "driven_plans — список или строка через запятую"}
        if key == "require_prompt":
            value = bool(value)
        cfg[key] = value
        changed.append(key)
    runtimes = patch.get("runtimes")
    if isinstance(runtimes, dict):
        cfg["runtimes"] = {**((cfg.get("runtimes") or {})), **{k: str(v) for k, v in runtimes.items()}}
        changed.append("runtimes")
    if not changed:
        return {"ok": False, "error": "нечего менять"}
    proj.write_config(state.project, cfg)
    state.note("pipeline.json обновлён: " + ", ".join(changed))
    return {"ok": True, "changed": changed, "config": cfg}


def act_save_telegram(state: State, payload: dict) -> dict:
    token = str(payload.get("token") or "").strip()
    chat = str(payload.get("chat") or "").strip()
    driver = (proj.read_config(state.project).get("profiles") or {}).get("driver") or "default"
    if not token or not chat:
        return {"ok": False, "error": "нужны и токен, и chat id"}
    try:
        me = telegram.get_me(token)
        telegram.validate_chat(token, chat)
    except telegram.TelegramError as exc:
        return {"ok": False, "error": str(exc)}
    setup_flow.write_env(driver, {setup_flow.TOKEN_ENV: token, setup_flow.CHAT_ENV: chat})
    cfg = proj.read_config(state.project)
    notify = dict(cfg.get("notify") or {})
    notify.update({"enabled": True, "token_env": setup_flow.TOKEN_ENV,
                   "chat_env": setup_flow.CHAT_ENV})
    cfg["notify"] = notify
    proj.write_config(state.project, cfg)
    os.environ[setup_flow.TOKEN_ENV] = token
    os.environ[setup_flow.CHAT_ENV] = chat
    state.note(f"Telegram подключён: @{me.get('username', '?')} → {chat}")
    return {"ok": True, "bot": me.get("username")}


def act_test_telegram(state: State, payload: dict) -> dict:
    cfg = proj.read_config(state.project)
    notify = cfg.get("notify") or {}
    token = os.environ.get(notify.get("token_env") or setup_flow.TOKEN_ENV, "")
    chat = os.environ.get(notify.get("chat_env") or setup_flow.CHAT_ENV, "")
    if not token or not chat:
        return {"ok": False, "error": "Telegram не настроен"}
    try:
        telegram.send_message(token, chat, "🧪 Тест из UI конвейера.")
    except telegram.TelegramError as exc:
        return {"ok": False, "error": str(exc)}
    state.note("тестовое сообщение отправлено")
    return {"ok": True}


def set_plan_mode(state: State, on: bool) -> dict:
    """Переключить режим планировщика проекта и вернуть ту же форму, что JSON CLI S2.

    Повтор с тем же значением файл не пишет: иначе `changed_at` переписывался бы на каждом
    клике тумблера, и «когда режим менялся» перестало бы быть правдой.
    """
    path = plan_mode.state_file(state.project)
    label = "on" if on else "off"
    before = plan_mode.read_mode(state.project)
    if before == on:
        return {"ok": True, "mode": label, "file": str(path), "changed": False}
    payload = plan_mode.write_mode(state.project, on)
    state.note(f"режим планировщика: {label} (было {'on' if before else 'off'}) — "
               f"со следующей сессии")
    return {"ok": True, "mode": label, "file": str(path), "changed": True,
            "changed_at": payload["changed_at"]}


ACTIONS = {
    "tick": act_tick,
    "stop": act_stop,
    "resume": act_resume,
    "hold": act_hold,
    "unhold": act_unhold,
    "save_config": act_save_config,
    "save_telegram": act_save_telegram,
    "test_telegram": act_test_telegram,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "StepPipelineUI/1.0"
    state: State  # задаётся в serve()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - имя из stdlib
        return

    # ── ответы ──
    def _send(self, code: int, body: bytes, kind: str = "application/json; charset=utf-8",
              extra: dict[str, str] | None = None,
              cache_control: str = "no-store") -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            # браузер закрыл соединение на автообновлении — это норма, не ошибка сервера
            return

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, _json_bytes(payload))

    def _local_only(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _guard_post(self, panel_header: bool = False) -> bool:
        """Пропустить только JSON-запрос из панели. False — ответ уже отправлен.

        `panel_header=True` дополнительно требует метку панели: её ставит только сама
        страница, поэтому запрос чужой страницы сюда не пройдёт.
        """
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind != "application/json":
            self._json(415, {"ok": False, "error": "нужен Content-Type: application/json"})
            return False
        port = self.server.server_address[1]
        origin = (self.headers.get("Origin") or "").strip().rstrip("/").lower()
        if origin and origin not in _allowed_origins(port):
            self._json(403, {"ok": False, "error": "запрос не из панели: чужой Origin"})
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            self._json(403, {"ok": False, "error": "запрос не из панели: Sec-Fetch-Site"})
            return False
        if panel_header and (self.headers.get(PANEL_HEADER) or "").strip() != PANEL_HEADER_VALUE:
            self._json(403, {"ok": False,
                             "error": "запрос не из панели: нет заголовка X-Pipeline-Panel"})
            return False
        return True

    def _read_json_body(self) -> dict | None:
        """Прочитать тело POST. None — ответ с ошибкой уже отправлен."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 200_000:
            self._json(413, {"ok": False, "error": "слишком большое тело"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"кривой JSON: {exc}"})
            return None
        return payload if isinstance(payload, dict) else {}

    # ── снимок и поток событий ──
    def _query(self) -> dict[str, str]:
        """Параметры строки запроса: значения декодируются (путь приходит с `%20` и `/`)."""
        out: dict[str, str] = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if not part:
                    continue
                key, _, value = part.partition("=")
                out[unquote(key)] = unquote(value)
        return out

    def _plan_param(self) -> str | None:
        """Параметр `plan=` из строки запроса (нет параметра — None)."""
        return self._query().get("plan") or None

    @staticmethod
    def _line_limit(query: dict[str, str], default: int = snap.STEP_LINES) -> int:
        """Сколько строк блока отдавать: параметр `lines`, иначе умолчание, с потолком."""
        try:
            value = int(str(query.get("lines") or default))
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, 5000))

    def _snapshot_body(self, plan: str | None) -> bytes:
        """Тело ответа `/api/snapshot`: снимок + журнал сервера + режим планировщика + ok."""
        data = snap.snapshot(self.state.project, plan, preview=self.state.preview,
                             board=self.state.board)
        data["log"] = self.state.tail()
        data["plan_mode"] = plan_mode.read_mode(self.state.project)
        data["ok"] = True
        return _json_bytes(data)

    def _event_frame(self, body: bytes) -> None:
        """Один кадр SSE: имя события `snapshot` и JSON в строке `data:`."""
        self.wfile.write(b"event: snapshot\ndata: " + body + b"\n\n")
        self.wfile.flush()

    def _events(self, plan: str | None) -> None:
        """Поток событий: первый кадр сразу, дальше — по изменению отпечатка.

        Живёт не дольше EVENTS_MAX_SECONDS и закрывается: переподключение — забота
        EventSource. Любая сетевая ошибка (вкладку закрыли) — норма, а не сбой сервера.
        """
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        try:
            stamp = snap.fingerprint(self.state.project, plan, self.state.preview,
                                     self.state.board)
            self._event_frame(self._snapshot_body(plan))
            started = last_ping = time.monotonic()
            while time.monotonic() - started < EVENTS_MAX_SECONDS:
                time.sleep(EVENTS_POLL)
                now = time.monotonic()
                current = snap.fingerprint(self.state.project, plan, self.state.preview,
                                           self.state.board)
                if current != stamp:
                    stamp = current
                    self._event_frame(self._snapshot_body(plan))
                    last_ping = now
                elif now - last_ping >= EVENTS_PING_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception:                                       # noqa: BLE001
            # pytest.ini считает необработанное исключение в потоке ошибкой теста: ошибку
            # сборки тоже глушим — поток просто закрывается, клиент переподключится.
            return
        finally:
            self.close_connection = True

    # ── GET ──
    def do_GET(self) -> None:                                   # noqa: N802 - stdlib API
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                body = UI_FILE.read_bytes()
            except OSError:
                body = "<h1>ui/index.html не найден</h1>".encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/snapshot":
            try:
                body = self._snapshot_body(self._plan_param())
            except Exception as exc:                            # noqa: BLE001
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            tag = _etag(body)
            # no-cache вместо no-store: иначе браузер не перепроверяет и ETag мёртв.
            if _if_none_match(self.headers.get("If-None-Match")) == tag.strip('"'):
                self._send(304, b"", extra={"ETag": tag}, cache_control="no-cache")
                return
            self._send(200, body, extra={"ETag": tag}, cache_control="no-cache")
            return
        if path == "/api/events":
            self._events(self._plan_param())
            return
        if path == "/api/step":
            self._get_step()
            return
        if path == "/api/file":
            self._get_file()
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "root": str(self.state.project)})
            return
        self._json(404, {"ok": False, "error": "нет такого маршрута"})

    # ── экран «Шаг» и файлы проекта (план 3 S3) ──
    def _get_step(self) -> None:
        """Экран шага: контракт, постановка, отчёт ревью, коммит и карточки шага."""
        query = self._query()
        plan_id = str(query.get("plan") or "").strip()
        step_id = str(query.get("id") or query.get("step") or "").strip()
        if not plan_id or not step_id:
            self._json(400, {"ok": False, "error": "нужны параметры plan и id (например, ?plan=3&id=S3)"})
            return
        try:
            data = snap.step_detail(self.state.project, plan_id, step_id,
                                    limit=self._line_limit(query), board=self.state.board)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if not (data.get("contract") or {}).get("found"):
            self._json(404, {"ok": False,
                             "error": (data.get("contract") or {}).get("error")
                             or f"шаг {step_id} не найден в плане {plan_id}"})
            return
        self._json(200, data)

    def _get_file(self) -> None:
        """Текст файла проекта: только внутри корня, обрезка с явной пометкой."""
        query = self._query()
        rel = str(query.get("path") or "")
        try:
            data = snap.read_text_safe(self.state.project, rel, limit=self._line_limit(query))
        except snap.UnsafePath as exc:
            code = 404 if str(exc).startswith("файла нет") else 400
            self._json(code, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, {"ok": True, **data})

    # ── POST ──
    def do_POST(self) -> None:                                  # noqa: N802 - stdlib API
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        path = self.path.split("?")[0]
        if path == "/api/plan-mode":
            self._post_plan_mode()
            return
        if path != "/api/action":
            self._json(404, {"ok": False, "error": "нет такого маршрута"})
            return
        if not self._guard_post():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        action = str(payload.get("action") or "").strip()
        handler = ACTIONS.get(action)
        if handler is None:
            self._json(400, {"ok": False, "error": f"неизвестное действие: {action}"})
            return
        if getattr(self.state, "preview", False):
            self._json(400, {"ok": False, "error": "режим предпросмотра: действия выключены — "
                                                   "поставь конвейер (hermes pipeline setup)"})
            return
        try:
            result = handler(self.state, payload)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        result.setdefault("action", action)
        # Действие могло записать на диск: кэш снимка сбрасываем, чтобы ответ и следующий
        # запрос не отдавали состояние «до».
        snap.clear_cache()
        self._json(200 if result.get("ok") else 400, result)

    def _post_plan_mode(self) -> None:
        """Переключить режим планировщика: одно идемпотентное действие, без второго «применить»."""
        # Отказ предпросмотра не зависит от заголовков и тела: сначала «только чтение».
        if getattr(self.state, "preview", False):
            self._json(403, {"ok": False,
                             "error": "предпросмотр только читает: переключение режима недоступно"})
            return
        if not self._guard_post(panel_header=True):
            return
        payload = self._read_json_body()
        if payload is None:
            return
        on = payload.get("on")
        if not isinstance(on, bool):
            self._json(400, {"ok": False, "error": "нужно поле on: true или false"})
            return
        try:
            result = set_plan_mode(self.state, on)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, result)


def make_server(project: Path, host: str = "127.0.0.1", port: int = 8770,
                preview: bool = False, board: str | None = None) -> ThreadingHTTPServer:
    """Собрать сервер, не запуская его (нужно тестам)."""
    handler = type("BoundHandler", (Handler,),
                   {"state": State(project, preview=preview, board=board)})
    return ThreadingHTTPServer((host, port), handler)


def serve(project: Path, host: str = "127.0.0.1", port: int = 8770,
          open_browser: bool = True, preview: bool = False,
          board: str | None = None) -> str:
    """Запустить UI в фоновом потоке и вернуть URL."""
    server = make_server(project, host, port, preview=preview, board=board)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="pipeline-ui")
    thread.start()
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return url
