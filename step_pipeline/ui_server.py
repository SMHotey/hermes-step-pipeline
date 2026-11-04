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
    GET  /api/checks       проверки: doctor и линтер планов (?refresh=1 — перепроверить,
                           ?plan=<N> — только один план); действий не выполняют
    GET  /api/projects     сводка реестра: проект, план, следующий шаг, закрыто, стоп, простой
    GET  /api/setup        мастер установки: шаги, следующий шаг, значения формы (без токена)
    POST /api/setup        один шаг мастера: {"step": ..., "values": {...}},
                           заголовок X-Pipeline-Panel; токен уходит в .env профиля
                           и обратно в браузер не возвращается
    POST /api/action       действия: tick, stop, resume, hold, unhold,
                           save_config, save_telegram, test_telegram
    POST /api/plan-mode    режим планировщика: {"on": true|false}, заголовок X-Pipeline-Panel
Слушает только localhost: это операторский инструмент владельца, без авторизации.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import urllib.request

try:
    from . import doctor
    from . import plan_lint
    from . import plan_mode
    from . import project as proj
    from . import setup_flow, snapshot as snap, telegram
except ImportError:  # pragma: no cover
    import doctor  # type: ignore
    import plan_lint  # type: ignore
    import plan_mode  # type: ignore
    import project as proj  # type: ignore
    import setup_flow  # type: ignore
    import snapshot as snap  # type: ignore
    import telegram  # type: ignore

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"

def plugin_version() -> str:
    """Версия плагина из plugin.yaml — панель показывает, что именно отдаёт сервер."""
    try:
        text = (Path(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "0"
    match = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", text)
    return match.group(1) if match else "0"


def ui_sha() -> str:
    """Отпечаток отдаваемой страницы: отличает свежий UI от старой копии плагина."""
    try:
        return hashlib.sha256(UI_FILE.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def busy_version(host: str, port: int, timeout: float = 0.5) -> str | None:
    """Кто уже отвечает на порту: версия UI или None, если порт свободен."""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8", "replace"))
    except Exception:                                        # noqa: BLE001
        return None
    return str(data.get("version") or "?")

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


def _redact(text: str, secret: str) -> str:
    """Убрать секрет из текста: токен не попадает ни в ответ API, ни в лог панели (D6).

    Заменяется полное вхождение; строк с частью токена мастер не печатает вовсе —
    поэтому маски в его логе нет (в отличие от `setup_flow._mask`).
    """
    if not secret:
        return text
    return text.replace(secret, "***")


class State:
    """Разделяемое состояние сервера: проект, журнал действий, режим чтения.

    Один `State` на проект реестра (D6): журнал действий, кэш проверок и настройки
    панели не смешиваются у соседних проектов. `id` и `name` — из реестра (D5).
    """

    def __init__(self, project: Path, preview: bool = False, board: str | None = None,
                 project_id: str = "", name: str = "") -> None:
        self.project = project
        self.preview = preview
        self.board = board
        self.id = project_id or project.name
        self.name = name or project.name
        self.log: deque[str] = deque(maxlen=200)
        self.cache: dict[str, Any] = {}
        self.lock = threading.Lock()
        # Проверки (план 3 S5): последний результат и признак «проверка идёт».
        self.checks: dict[str, Any] | None = None
        self.checks_lock = threading.Lock()
        # Сводка реестра (план 3 S6): кэш на CACHE_TTL, чтобы переключение вкладок
        # не запускало `gate.py --plans` по каждому проекту (D8).
        self.projects_cache: tuple[float, dict] | None = None

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


# ───────────────────────────── проверки в панели (план 3 S5) ─────────────────────────────

def _blank_checks() -> dict:
    """Пустая форма проверок: «не проверяли» отличается от «проверено, чисто»."""
    return {"ok": True, "checked_at": "", "busy": False, "critical": None,
            "notes": [], "problems": [], "lint": {"files": 0, "problems": []}}


def plan_id_of(file: str) -> str:
    """Номер плана из имени файла находки (`docs/plans/3-panel.md` → `3`).

    Регулярка берётся у линтера, чтобы имя считалось ровно так же, как при поиске плана.
    """
    match = plan_lint._PLAN_NUM_RE.match(Path(str(file)).name)
    return str(int(match.group(1))) if match else ""


def collect_checks(state: State, plan: str = "") -> dict:
    """Проверки панели: doctor без своего линтера плюс структурный линтер планов.

    Действий конвейера здесь нет и файлов проект не получает. Движок запускается только
    на чтение — снимок для строки «решение стража» (как в `hermes pipeline doctor`).
    """
    report = doctor.collect(state.project, plan, with_lint=False,
                            preview=state.preview, board=state.board or "")
    lint = plan_lint.lint_project(state.project, plan)
    findings = [{"plan": plan_id_of(problem.file), "step": problem.step,
                 "file": problem.file, "line": problem.line, "text": problem.text}
                for problem in lint.problems]
    return {"ok": True, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "busy": False,
            "critical": len(report.problems) + len(findings),
            "notes": list(report.notes), "problems": list(report.problems),
            "lint": {"files": len(lint.files), "problems": findings}}


def checks(state: State, plan: str = "", refresh: bool = False) -> dict:
    """Результат проверок; *refresh* запускает их заново.

    Пока проверка идёт, вторая не начинается: ответ помечается `busy: true` и несёт
    прежние данные — панель не плодит параллельные прогоны на каждый кадр.
    """
    if not refresh:
        payload = dict(state.checks) if state.checks else _blank_checks()
        payload["busy"] = state.checks_lock.locked()
        return payload
    if not state.checks_lock.acquire(blocking=False):
        payload = dict(state.checks) if state.checks else _blank_checks()
        payload["busy"] = True
        return payload
    try:
        try:
            payload = collect_checks(state, plan)
        except Exception as exc:                                # noqa: BLE001
            # Панель показывает отказ, а не пустоту: иначе «проверено, чисто» соврёт.
            previous = dict(state.checks) if state.checks else _blank_checks()
            text = f"проверка не удалась: {type(exc).__name__}: {exc}"
            state.note(text)
            previous["problems"] = [text]
            previous["busy"] = False
            payload = previous
        state.checks = payload
    finally:
        state.checks_lock.release()
    return dict(payload)


def checks_summary(state: State) -> dict | None:
    """Короткая форма проверок для снимка (S5, §3.6): ``None``, пока проверки не было,
    иначе четыре поля — ``checked_at``, ``critical``, ``problems``, ``notes``. Без
    блокировки, напрямую из ``state.checks``: бейдж читает ``critical`` из снимка.
    """
    payload = state.checks
    if not payload:
        return None
    return {"checked_at": payload.get("checked_at") or "",
            "critical": payload.get("critical"),
            "problems": list(payload.get("problems") or []),
            "notes": list(payload.get("notes") or [])}


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


# ───────────────────────── реестр проектов (план 3 S6) ─────────────────────────

def _installed(project: Path) -> bool:
    """Конвейер установлен: в проекте есть `.pipeline/gate.py` (§4.1)."""
    return (Path(project) / ".pipeline" / "gate.py").is_file()


def _display_name(root: Path) -> str:
    """Имя проекта для панели: `project` из `pipeline.json`, иначе имя каталога."""
    name = str(proj.read_config(root).get("project") or "").strip()
    return name or Path(root).name


def _registry_rows(project: Path, extra_projects: list[str | Path] | None) -> list[dict]:
    """Реестр: проект сервера, потом `projects.json`, потом разовые пути (D4, D5).

    Один каталог не попадает в реестр дважды (§4.3): проект по умолчанию, который
    владелец вписал ещё и в файл, не раздваивается в таблице.
    """
    rows: list[dict] = []

    def add(root: Path | str, name: str = "", wanted_id: str = "",
            source: str = "registry") -> None:
        path = Path(root).expanduser().resolve()
        if any(row["root"] == path for row in rows):
            return
        base = wanted_id or proj.slug(name or path.name)
        project_id, number = base, 2
        while any(row["id"] == project_id for row in rows):
            project_id, number = f"{base}-{number}", number + 1
        rows.append({"id": project_id, "name": name or _display_name(path),
                     "root": path, "source": source})

    add(project, source="default")
    for entry in proj.read_projects():
        add(entry["root"], entry["name"], entry["id"])
    for extra in extra_projects or []:
        add(extra, source="command line")
    return rows


def _missing_snapshot(state: State) -> dict:
    """Снимок проекта без конвейера: 200 и объяснение в `errors`, а не 500.

    Форма — как у обычного снимка: страница не должна знать, что проекта нет,
    кроме `installed: false` и строки ошибки (D9).
    """
    root = Path(state.project)
    return {"project": state.name, "root": str(root),
            "board": state.board or str(proj.read_config(root).get("board") or "default"),
            "profiles": {}, "runtimes": {}, "driven": [], "require_prompt": True,
            "telegram": {"enabled": False, "chat": ""},
            "preview": state.preview, "plans": [], "plan": None, "package": None,
            "decision": None, "status": "", "metrics": {}, "cards": [], "journal": [],
            "errors": [f"конвейер не установлен в проект: нет {root / '.pipeline' / 'gate.py'}"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "StepPipelineUI/1.0"
    state: State  # задаётся в serve()
    # Реестр проектов (S6) задаётся в make_server: записи реестра и их состояния.
    registry: list[dict] = []
    states: dict[str, State] = {}

    protocol_version = "HTTP/1.1"

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
            self.send_header("Connection", "close")
            self.send_header("Content-Security-Policy", CSP)
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
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

    # ── реестр проектов: выбор адресата (план 3 S6) ──
    def _state_of(self, row: dict) -> State:
        """Состояние проекта реестра: создаётся при первом обращении (D6)."""
        state = self.states.get(row["id"])
        if state is None:
            state = State(row["root"], preview=self.state.preview, board=self.state.board,
                          project_id=row["id"], name=row["name"])
            self.states[row["id"]] = state
            row["state"] = state
        return state

    def _choose_project(self, raw: Any) -> State | None:
        """Проект запроса: без параметра — серверный, неизвестный `id` — ответ 400.

        Молча работать с чужим проектом нельзя: страница подписана именем проекта,
        значит и ошибка обязана называть, какой `id` не найден (D7).
        """
        wanted = str(raw or "").strip()
        if not wanted:
            return self.state
        for row in self.registry:
            if str(row["id"]) == wanted:
                return self._state_of(row)
        ids = ", ".join(str(row["id"]) for row in self.registry) or "—"
        self._json(400, {"ok": False, "error": f"неизвестный проект: {wanted}; доступные: {ids}",
                         "projects": [str(row["id"]) for row in self.registry]})
        return None

    def _summary_plans(self, state: State) -> list[dict]:
        """`gate.py --plans` проекта; в предпросмотре — движком плагина, как снимок."""
        if state.preview:
            extra = {"PIPELINE_ASSUME_DEFAULTS": "1"}
            if state.board:
                extra["PIPELINE_BOARD"] = state.board
            data = proj.engine_json(state.project, "gate.py", "--plans", timeout=90,
                                    engine_dir=proj.ENGINE_SRC, extra_env=extra)
        else:
            data = proj.engine_json(state.project, "gate.py", "--plans", timeout=90)
        return data if isinstance(data, list) else []

    def _project_summary(self, row: dict) -> dict:
        """Строка сводки: план, следующий шаг, закрыто, стоп, простой (D8).

        Полный снимок проекта здесь не собирается: на каждый проект хватает одного
        `gate.py --plans` и файла состояния плана — иначе таблица тянула бы движок
        трижды на репозиторий.
        """
        state = self._state_of(row)
        root = Path(row["root"])
        summary = {"id": row["id"], "name": row["name"], "root": str(root),
                   "source": row["source"], "installed": _installed(root),
                   "plan": "", "next": "—", "closed": 0, "total": 0,
                   "stop": None, "idle": None, "error": ""}
        if not summary["installed"] and not state.preview:
            return summary
        try:
            cfg = proj.read_config(root)
            plans = self._summary_plans(state)
        except Exception as exc:                                # noqa: BLE001
            summary["error"] = f"{type(exc).__name__}: {exc}"
            return summary
        if not plans:
            return summary
        # План, у которого есть открытые шаги, иначе первый — как в снимке.
        chosen = next((p for p in plans if str(p.get("next") or "") not in ("", "—", "None")),
                      plans[0])
        plan_id = str(chosen.get("id") or "")
        summary["plan"] = plan_id
        summary["next"] = str(chosen.get("next") or "—")
        summary["closed"] = int(chosen.get("closed") or 0)
        summary["total"] = snap._steps_total(root, cfg, str(chosen.get("file") or ""))
        stop_row = chosen.get("stop")
        if isinstance(stop_row, dict) and stop_row:
            summary["stop"] = {"kind": str(stop_row.get("kind") or "owner"),
                               "reason": str(stop_row.get("reason") or "")}
        plan_state = snap._plan_state(root, plan_id)
        since = plan_state.get("waiting_since")
        if isinstance(since, (int, float)) and not isinstance(since, bool):
            summary["idle"] = {"text": snap._duration(max(0, int(time.time() - float(since))))}
        return summary

    def _projects_payload(self) -> dict:
        """Сводка реестра с кэшем на CACHE_TTL: один `State` сервера держит его (D8)."""
        cached = self.state.projects_cache
        if cached is not None and time.monotonic() - cached[0] <= snap.CACHE_TTL:
            return cached[1]
        payload = {"ok": True, "default": self.state.id,
                   "preview": bool(self.state.preview),
                   "projects": [self._project_summary(row) for row in self.registry]}
        self.state.projects_cache = (time.monotonic(), payload)
        return payload

    def _get_projects(self) -> None:
        """Сводная таблица репозиториев: только чтение, действий не выполняет."""
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        try:
            self._json(200, self._projects_payload())
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    @staticmethod
    def _line_limit(query: dict[str, str], default: int = snap.STEP_LINES) -> int:
        """Сколько строк блока отдавать: параметр `lines`, иначе умолчание, с потолком."""
        try:
            value = int(str(query.get("lines") or default))
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, 5000))

    def _snapshot_with_version(self, plan: str | None, state: State | None = None) -> bytes:
        """Снимок + версия UI: панель показывает, что именно ей отдал сервер."""
        body = self._snapshot_body(plan, state)
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            return body
        data["version"] = plugin_version()
        data["ui_sha"] = ui_sha()
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _snapshot_body(self, plan: str | None, state: State | None = None) -> bytes:
        """Тело ответа `/api/snapshot`: снимок + журнал сервера + режим планировщика + ok.

        Проект реестра без `.pipeline` получает 200 и пустую форму с объяснением (D9):
        ошибка на этом экране означала бы, что сводка по соседям недоступна.
        """
        state = state or self.state
        if state.preview or _installed(state.project):
            data = snap.snapshot(state.project, plan, preview=state.preview,
                                 board=state.board)
        else:
            data = _missing_snapshot(state)
        # Короткая форма проверок для бейджа: прогон запускает вкладка «Проверки», здесь
        # только то, что уже посчитано (или None, пока проверки не было). overview
        # копируется, иначе правка ушла бы в кэш снимка.
        data["overview"] = dict(data.get("overview") or {})
        data["overview"]["checks"] = checks_summary(state)
        data["log"] = state.tail()
        data["plan_mode"] = plan_mode.read_mode(state.project)
        data["installed"] = _installed(state.project)
        data["project_id"] = state.id
        data["ok"] = True
        return _json_bytes(data)

    def _event_frame(self, body: bytes) -> None:
        """Один кадр SSE: имя события `snapshot` и JSON в строке `data:`."""
        self.wfile.write(b"event: snapshot\ndata: " + body + b"\n\n")
        self.wfile.flush()

    def _events(self, plan: str | None, state: State | None = None) -> None:
        """Поток событий: первый кадр сразу, дальше — по изменению отпечатка.

        Живёт не дольше EVENTS_MAX_SECONDS и закрывается: переподключение — забота
        EventSource. Любая сетевая ошибка (вкладку закрыли) — норма, а не сбой сервера.
        """
        state = state or self.state
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
            stamp = snap.fingerprint(state.project, plan, state.preview,
                                     state.board)
            self._event_frame(self._snapshot_with_version(plan, state))
            started = last_ping = time.monotonic()
            while time.monotonic() - started < EVENTS_MAX_SECONDS:
                time.sleep(EVENTS_POLL)
                now = time.monotonic()
                current = snap.fingerprint(state.project, plan, state.preview,
                                           state.board)
                if current != stamp:
                    stamp = current
                    self._event_frame(self._snapshot_with_version(plan, state))
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
            state = self._choose_project(self._query().get("project"))
            if state is None:
                return
            try:
                body = self._snapshot_with_version(self._plan_param(), state)
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
            state = self._choose_project(self._query().get("project"))
            if state is None:
                return
            self._events(self._plan_param(), state)
            return
        if path == "/api/projects":
            self._get_projects()
            return
        if path == "/api/step":
            self._get_step()
            return
        if path == "/api/file":
            self._get_file()
            return
        if path == "/api/checks":
            self._get_checks()
            return
        if path == "/api/setup":
            self._get_setup()
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "root": str(self.state.project),
                                   "version": plugin_version(), "ui_sha": ui_sha()})
            return
        self._json(404, {"ok": False, "error": "нет такого маршрута"})

    # ── экран «Шаг» и файлы проекта (план 3 S3) ──
    def _get_step(self) -> None:
        """Экран шага: контракт, постановка, отчёт ревью, коммит и карточки шага."""
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        query = self._query()
        state = self._choose_project(query.get("project"))
        if state is None:
            return
        plan_id = str(query.get("plan") or "").strip()
        step_id = str(query.get("id") or query.get("step") or "").strip()
        if not plan_id or not step_id:
            self._json(400, {"ok": False, "error": "нужны параметры plan и id (например, ?plan=3&id=S3)"})
            return
        try:
            data = snap.step_detail(state.project, plan_id, step_id,
                                    limit=self._line_limit(query), board=state.board)
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
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        query = self._query()
        state = self._choose_project(query.get("project"))
        if state is None:
            return
        rel = str(query.get("path") or "")
        try:
            data = snap.read_text_safe(state.project, rel, limit=self._line_limit(query))
        except snap.UnsafePath as exc:
            code = 404 if str(exc).startswith("файла нет") else 400
            self._json(code, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, {"ok": True, **data})

    # ── проверки (план 3 S5) ──
    def _get_checks(self) -> None:
        """Проверки: doctor и линтер планов. Действий не выполняют, файлов не пишут.

        `?refresh=1` — перепроверить; без него отдаётся последний результат или пустой
        вид «не проверяли» (`checked_at: ""`).
        """
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        query = self._query()
        state = self._choose_project(query.get("project"))
        if state is None:
            return
        plan = str(query.get("plan") or "").strip()
        refresh = str(query.get("refresh") or "") not in ("", "0", "false", "no")
        try:
            data = checks(state, plan, refresh=refresh)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, data)

    # ── мастер установки (план 3 S4) ──
    def _get_setup(self) -> None:
        """Состояние мастера: шаги, следующий и значения формы без секретов (D6)."""
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        state = self._choose_project(self._query().get("project"))
        if state is None:
            return
        try:
            data = setup_flow.setup_status(state.project)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        preview = bool(getattr(state, "preview", False))
        self._json(200, {"ok": True, "preview": preview, "enabled": not preview,
                         "project": str(state.project),
                         "steps": data["steps"], "next": data["next"],
                         "values": data["values"]})

    def _drain_body(self) -> None:
        """Дочитать тело POST перед отказом: иначе клиент видит RST вместо ответа."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= 200_000:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _post_setup(self) -> None:
        """Один шаг мастера. Токен из формы уходит в `.env` и обратно не возвращается."""
        if getattr(self.state, "preview", False):
            self._drain_body()
            self._json(403, {"ok": False,
                             "error": "предпросмотр только читает: мастер установки недоступен"})
            return
        if not self._guard_post(panel_header=True):
            return
        payload = self._read_json_body()
        if payload is None:
            return
        state = self._choose_project(payload.get("project"))
        if state is None:
            return
        step = str(payload.get("step") or "").strip()
        if step not in setup_flow.SETUP_STEPS:
            allowed = ", ".join(setup_flow.SETUP_STEPS)
            self._json(400, {"ok": False, "step": step,
                             "error": f"неизвестный шаг: {step or '—'}; "
                                      f"допустимые: {allowed}"})
            return
        raw_values = payload.get("values")
        values = raw_values if isinstance(raw_values, dict) else {}
        # Редакция тем же значением, которое исполнит шаг: `run_setup_step` срезает
        # значения формы (`.strip()`), поэтому токен с хвостовым переводом строки
        # иначе не совпадает с текстом ошибки и уходит в ответ и в `state.log`.
        token = str(values.get("token") or "").strip()
        try:
            result = setup_flow.run_setup_step(state.project, step, values)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "step": step,
                             "error": _redact(f"{type(exc).__name__}: {exc}", token)})
            return
        # Секрет вычищается из всего, что уходит наружу: и в ответ, и в лог панели.
        log = [_redact(str(line), token) for line in result.get("log") or []]
        error = _redact(str(result.get("error") or ""), token)
        state.note(_redact(f"мастер: шаг {step}\n" + "\n".join(log), token))
        if result.get("ok"):
            snap.clear_cache()      # шаг пишет на диск: прежний снимок больше не верен
            self.state.projects_cache = None   # и сводка реестра — тоже
        body = {"ok": bool(result.get("ok")), "step": step,
                "already": bool(result.get("already")), "next": result.get("next"),
                "log": log}
        if error:
            body["error"] = error
        self._json(200 if body["ok"] else 400, body)

    # ── POST ──
    def do_POST(self) -> None:                                  # noqa: N802 - stdlib API
        if not self._local_only():
            self._json(403, {"ok": False, "error": "только localhost"})
            return
        path = self.path.split("?")[0]
        if path == "/api/plan-mode":
            self._post_plan_mode()
            return
        if path == "/api/setup":
            self._post_setup()
            return
        if path != "/api/action":
            self._json(404, {"ok": False, "error": "нет такого маршрута"})
            return
        if not self._guard_post():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        # Отказ предпросмотра не зависит от проекта (D10): сначала «только чтение»,
        # потом выбор проекта и проверка установки.
        if getattr(self.state, "preview", False):
            self._json(400, {"ok": False, "error": "режим предпросмотра: действия выключены — "
                                                   "поставь конвейер (hermes pipeline setup)"})
            return
        state = self._choose_project(payload.get("project"))
        if state is None:
            return
        action = str(payload.get("action") or "").strip()
        handler = ACTIONS.get(action)
        if handler is None:
            self._json(400, {"ok": False, "error": f"неизвестное действие: {action}"})
            return
        stamp = {"id": state.id, "name": state.name}
        if not _installed(state.project):
            self._json(400, {"ok": False, "project": stamp,
                             "error": f"конвейер не установлен в проект {state.project}: "
                                      f"нет .pipeline/gate.py"})
            return
        try:
            result = handler(state, payload)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "project": stamp,
                             "error": f"{type(exc).__name__}: {exc}"})
            return
        result.setdefault("action", action)
        # Действие подписано проектом: иначе по ответу не видно, кого оно коснулось (D7).
        result["project"] = stamp
        # Действие могло записать на диск: кэш снимка сбрасываем, чтобы ответ и следующий
        # запрос не отдавали состояние «до».
        snap.clear_cache()
        self.state.projects_cache = None      # сводка живёт на состоянии сервера (D8)
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
        state = self._choose_project(payload.get("project"))
        if state is None:
            return
        try:
            result = set_plan_mode(state, on)
        except Exception as exc:                                # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        result["project"] = {"id": state.id, "name": state.name}
        self._json(200, result)


def make_server(project: Path, host: str = "127.0.0.1", port: int = 8770,
                preview: bool = False, board: str | None = None,
                extra_projects: list[str | Path] | None = None) -> ThreadingHTTPServer:
    """Собрать сервер, не запуская его (нужно тестам).

    Реестр строится здесь: проект сервера первым, дальше `projects.json` профиля и
    разовые пути из командной строки. `state` сервера — состояние первого проекта,
    остальные состояния создаются при первом обращении (`states`).
    """
    rows = _registry_rows(project, extra_projects)
    state = State(rows[0]["root"], preview=preview, board=board,
                  project_id=rows[0]["id"], name=rows[0]["name"])
    rows[0]["state"] = state
    handler = type("BoundHandler", (Handler,),
                   {"state": state, "registry": rows, "states": {state.id: state}})
    return ThreadingHTTPServer((host, port), handler)


def serve(project: Path, host: str = "127.0.0.1", port: int = 8770,
          open_browser: bool = True, preview: bool = False,
          board: str | None = None,
          extra_projects: list[str | Path] | None = None) -> str:
    """Запустить UI в фоновом потоке и вернуть URL."""
    busy = busy_version(host, port)
    if busy:
        # Windows разрешает двум процессам слушать один порт (SO_REUSEADDR), поэтому
        # старый сервер продолжает отвечать, а новый молча простаивает.
        raise RuntimeError(f"порт {port} занят другим UI конвейера (версия {busy}); "
                           f"останови его: hermes pipeline ui --stop, или задай --port")
    server = make_server(project, host, port, preview=preview, board=board,
                         extra_projects=extra_projects)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="pipeline-ui")
    thread.start()
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return url
