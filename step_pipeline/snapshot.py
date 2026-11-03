# -*- coding: utf-8 -*-
"""Снимок состояния конвейера для UI и инструментов агента.

Всё читается из проекта и борды, ничего не меняется. Источники:
    gate.py --plans --json     планы, следующий шаг, стоп-флаги
    gate.py --status --json    решение стража по текущему плану
    next_step.py --json        шаги плана, контракт следующего, git
    kanban db (sqlite, ro)     карточки ролей
    файл плана                 хвост журнала

Снимок кэшируется в памяти процесса на CACHE_TTL секунд и инвалидируется отпечатком —
только `stat` (mtime_ns + size) файлов плана, каталога состояния и базы борды, плюс сам
`pipeline.json`. Содержимое файлов отпечаток не читает: это дешёвая проверка «менялось ли
что-то», а не второй проход по данным.

Ключ `overview` — вычисленное следствие кнопки «Тик» до нажатия: подпись действия, причина,
доступность (стоп-флаг выбранного плана и предпросмотр), простой из состояния плана и
прогресс закрыто/всего по файлам планов. Ради этого движок НЕ запускается: число шагов
разбирается по заголовкам файла плана, иначе сборка снимка подорожала бы на запуск на план
(критерий шага 3-S1 — не больше 20 запусков движка в минуту).
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import project as proj
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore[no-redef]

CARD_COLUMNS = ("id", "title", "status", "assignee", "idempotency_key", "created_at",
                "completed_at", "result", "last_failure_error", "tenant", "max_runtime")
STAGE_LABELS = {"create_step": "заказ постановки", "create_review": "ревью",
                "create_fix": "правки после ревью", "close_step": "журнал и коммит",
                "create_rereview": "повторное ревью", "idle": "ждёт",
                "stop": "остановлен", "create_rereview_after_fix": "повторное ревью"}
ACTION_LABELS = {"create_step": "шаг", "create_prompt": "постановка", "create_review": "ревью",
                 "create_fix": "правки", "create_rereview": "ревью", "close_step": "журнал",
                 "idle": "ждёт", "stop": "стоп", "retry_pending": "довыкладка",
                 "hold_risk": "ручной допуск"}

CACHE_TTL = 2.5          # секунд: снимок не старше этого отдаётся из памяти
CACHE_LIMIT = 8          # ключей: проект × план × предпросмотр × борда

# Причины действий, у которых страж причину не пишет: `create_step` разбирается отдельно —
# там причина зависит от наличия постановки.
ACTION_REASONS = {
    "create_review": "шаг закрыт исполнителем — страж закажет ревью",
    "create_fix": "ревью вернуло правки — страж закажет круг правок",
    "create_rereview": "вердикт REQUEST_CHANGES при закрытии — страж закажет повторное ревью",
    "close_step": "вердикт APPROVE — страж запишет строку журнала и закоммитит шаг",
    "retry_pending": "есть отложенный коммит — страж повторит выкладку",
    "hold_risk": "шаг помечен риском — страж поставит ручной допуск владельца",
}

# Эффект действия для подписи кнопки «Тик»: подпись читает владелец, поэтому у каждого
# действия стража русское существительное, а не ключ решения.
EFFECT_LABELS = {"create_step": "шаг", "create_prompt": "постановка", "create_review": "ревью",
                 "create_rereview": "повторное ревью", "create_fix": "правки после ревью",
                 "create_rereview_after_fix": "повторное ревью", "close_step": "журнал и коммит",
                 "hold_risk": "допуск по риску", "retry_pending": "довыкладка",
                 "idle": "ждёт", "stop": "стоп"}
PROMPT_EFFECT = "заказ постановки"
NO_STEP_ACTIONS = ("idle", "stop")      # шаг в подпись не попадает

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*?)\s*$")
_DEFAULT_STEP_ID = r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)"

# Кэш процесса: ключ → (время сборки по monotonic, отпечаток источников, снимок).
_CACHE: dict[tuple, tuple[float, tuple, dict]] = {}
_CACHE_LOCK = threading.RLock()


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def cards(db: Path, limit: int = 30, key_prefix: str | None = None) -> list[dict]:
    """Последние карточки борды или карточки по префиксу ключа идемпотентности.

    Схема читается по факту: колонки менялись между версиями. `key_prefix` — фильтр в
    SQL, а не окно «последние N задач»: иначе карточки старого шага выпадали из выборки
    (находка ревью 3-S3).
    """
    con = _ro(db)
    if con is None:
        return []
    try:
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        cols = [c for c in CARD_COLUMNS if c in present]
        if not cols:
            return []
        order = "created_at" if "created_at" in present else cols[0]
        where, args = "", ()
        if key_prefix:
            if "idempotency_key" not in present:
                return []
            escaped = (key_prefix.replace("\\", "\\\\").replace("%", "\\%")
                       .replace("_", "\\_"))
            where, args = " WHERE idempotency_key LIKE ? ESCAPE '\\'", (escaped + "%",)
        rows = con.execute(f"SELECT {', '.join(cols)} FROM tasks{where} ORDER BY {order} DESC"
                           f" LIMIT {int(limit)}", args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(zip(cols, row)) for row in rows]


def journal_tail(project: Path, plan_file: str | None, limit: int = 8) -> list[list[str]]:
    """Последние строки таблицы журнала (без шапки и разделителя)."""
    if not plan_file:
        return []
    path = Path(project) / plan_file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    marker = (proj.read_config(project).get("markers") or {}).get("journal") or "журнал"
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("#") and marker.lower() in line.strip().lower():
            start = index
            break
    if start is None:
        return []
    rows: list[list[str]] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0].lower() in ("дата", "date"):
            continue
        rows.append(cells)
    return rows[-int(limit):]


def telegram_state(project: Path) -> dict:
    """Настроены ли уведомления (значения секретов наружу не отдаются)."""
    import os
    cfg = proj.read_config(project)
    notify = cfg.get("notify") or {}
    token_env = notify.get("token_env") or "STEP_PIPELINE_TELEGRAM_BOT_TOKEN"
    chat_env = notify.get("chat_env") or "STEP_PIPELINE_TELEGRAM_CHAT_ID"
    return {
        "enabled": bool(notify.get("enabled")),
        "token_set": bool(os.environ.get(token_env)),
        "chat": os.environ.get(chat_env) or "",
        "token_env": token_env,
        "chat_env": chat_env,
    }


def _stat_entry(label: str, path: Path) -> tuple:
    """Тройка отпечатка для пути: (метка, mtime_ns, size); нет пути — нули."""
    try:
        st = path.stat()
        return (label, st.st_mtime_ns, st.st_size)
    except OSError:
        return (label, 0, 0)


def fingerprint(root: Path, plan: str | None = None, preview: bool = False,
                board: str | None = None) -> tuple:
    """Отпечаток источников снимка: только `stat`, без чтения содержимого.

    Входят `pipeline.json`, все `*.md` каталога планов, файлы каталога состояния, файл
    борды и сам ключ кэша (проект, план, предпросмотр, борда) — чтобы отпечатки разных
    планов одного проекта не совпали. Результат отсортирован: порядок обхода не влияет.
    """
    root = Path(root)
    cfg = proj.read_config(root)
    entries: list[tuple] = [_stat_entry("config", root / "pipeline.json")]
    plans_dir = root / ((cfg.get("paths") or {}).get("plans") or "docs/plans")
    try:
        plan_files = sorted(plans_dir.glob("*.md"))
    except OSError:
        plan_files = []
    entries.extend(_stat_entry("plan:" + path.name, path) for path in plan_files)
    try:
        state_files = sorted(path for path in proj.state_dir(root).glob("*") if path.is_file())
    except OSError:
        state_files = []
    entries.extend(_stat_entry("state:" + path.name, path) for path in state_files)
    entries.append(_stat_entry("board", proj.board_db(root, board)))
    entries.extend([
        ("key:root", str(root), 0),
        ("key:plan", str(plan or ""), 0),
        ("key:preview", str(bool(preview)), 0),
        ("key:board", str(board or ""), 0),
    ])
    return tuple(sorted(entries))


def clear_cache() -> None:
    """Очистить кэш снимков: тестам для изоляции, действиям — как страховка после записи."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _prompt_file(root: Path, cfg: dict, plan_id, step_id) -> Path | None:
    """Постановка шага: `<prompts_dir>/<дата>_<план>-<шаг>_prompt.md` (дата в имени любая).

    Тот же поиск, что у стража (`gate.py:prompt_rel`): панель показывает состояние
    постановки, но своего представления о её имени не заводит.
    """
    plan_id, step_id = str(plan_id or ""), str(step_id or "")
    if not plan_id or not step_id:
        return None
    folder = Path(root) / str((cfg.get("paths") or {}).get("prompts") or "docs/prompts")
    if not folder.is_dir():
        return None
    matches = sorted(folder.glob(f"*_{plan_id}-{step_id}_prompt.md"))
    return matches[-1] if matches else None


def _relative(root: Path, path: Path | None) -> str:
    """Путь для показа: относительно проекта, если он внутри проекта."""
    if path is None:
        return ""
    try:
        return Path(path).relative_to(Path(root)).as_posix()
    except ValueError:
        return str(path)


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(индекс строки, уровень, заголовок) по всем заголовкам файла плана."""
    found: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            found.append((index, len(match.group("hashes")), match.group("title")))
    return found


def _section_end(found: list[tuple[int, int, str]], pos: int, limit: int) -> int:
    """Первый заголовок того же или более высокого уровня — граница раздела."""
    level = found[pos][1]
    for index in range(pos + 1, len(found)):
        if found[index][1] <= level:
            return found[index][0]
    return limit


def _is_step_title(step_re: re.Pattern, title: str) -> bool:
    """Заголовок шага: идентификатор по `step_id_regex`, затем точка и название."""
    match = step_re.match(title)
    if not match:
        return False
    rest = title[match.end():].strip()
    return rest.startswith(".") and bool(rest[1:].strip())


def _steps_total(root: Path, cfg: dict, plan_rel: str) -> int:
    """Число шагов плана по файлу: раздел с маркером шагов и заголовки `### <ШАГ>. название`.

    Повторяет разбор движка (`next_step.plan_steps`), но без запуска движка: сборка снимка
    не имеет права добавлять запуски (критерий шага 3-S1). Нет файла, раздела или
    совпадений — 0.
    """
    if not plan_rel:
        return 0
    try:
        lines = (Path(root) / plan_rel).read_text(encoding="utf-8",
                                                 errors="replace").splitlines()
    except OSError:
        return 0
    marker = str((cfg.get("markers") or {}).get("steps") or "шаг").lower()
    try:
        step_re = re.compile(str(cfg.get("step_id_regex") or _DEFAULT_STEP_ID))
    except re.error:
        return 0
    found = _headings(lines)
    span = None
    for pos, (index, level, title) in enumerate(found):
        if level >= 2 and marker in title.lower():
            span = (index, _section_end(found, pos, len(lines)))
            break
    if span is None:
        return 0
    return sum(1 for index, _level, title in found
               if span[0] < index < span[1] and _is_step_title(step_re, title))


def _duration(seconds) -> str:
    """Длительность по-русски: «4 ч 10 мин», «4 ч», «45 мин», «меньше минуты».

    Формат — как у стража (`gate.py:_ago`); копия нужна потому, что движок вендорится в
    проект и импортировать его из плагина нельзя (D5).
    """
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "меньше минуты"
    hours, minutes = total // 3600, total % 3600 // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _plan_state(root: Path, plan_id) -> dict:
    """Состояние плана из `state_dir/plan_N_state.json`; нет файла или битый — пустой словарь."""
    if str(plan_id or "") == "":
        return {}
    path = proj.state_dir(Path(root)) / f"plan_{plan_id}_state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _plan_stop(root: Path, plan_id) -> dict:
    """Стоп-флаг плана из `state_dir/plan_N_stop.json`; его наличие — не повод гасить допуск."""
    if str(plan_id or "") == "":
        return {}
    path = proj.state_dir(Path(root)) / f"plan_{plan_id}_stop.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fix_rounds(card_list: list[dict], plan_id, step_id) -> int:
    """Кругов правок по шагу: карточки с ключом `planloop:<план>:<шаг>:fix:` (D7)."""
    if not plan_id or not step_id:
        return 0
    prefix = f"planloop:{plan_id}:{str(step_id).upper()}:fix:"
    return sum(1 for card in card_list or []
               if str(card.get("idempotency_key") or "").startswith(prefix))


def _overview(root: Path, cfg: dict, out: dict) -> dict:
    """Экран «Обзор»: что произойдёт при Тик — подпись, причина, доступность, простой, прогресс.

    Всё считается здесь, чтобы подпись и запрет нажатия проверялись питоном, а страница
    оставалась рисовальщиком (D1). Имена полей — ровно те, что читает страница (D4); движок
    ради обзора не запускается: число шагов берётся из файлов планов.
    """
    decision = out.get("decision") or {}
    package = out.get("package") or {}
    plan_row = out.get("plan") or {}
    plan_id = str(plan_row.get("id") or "")
    action = str(decision.get("action") or "")
    raw_data = decision.get("data")
    data: dict = raw_data if isinstance(raw_data, dict) else {}
    raw_next = package.get("next")
    next_step: dict = raw_next if isinstance(raw_next, dict) else {}
    step = str(decision.get("step") or data.get("id") or next_step.get("id") or "").upper()

    prompt = _prompt_file(root, cfg, plan_id, step)
    prompt_rows = {"exists": prompt is not None, "path": _relative(root, prompt)}

    # «Заказ постановки» и «шаг» приходят одним действием `create_step` — различает их
    # только факт наличия постановки (D2).
    effect = EFFECT_LABELS.get(action, action)
    if action == "create_step" and out.get("require_prompt", True) and not prompt_rows["exists"]:
        effect = PROMPT_EFFECT
    if not action:
        tick_label = "Тик: —"
    elif step and action not in NO_STEP_ACTIONS:
        tick_label = f"Тик: {effect} {step}"
    else:
        tick_label = f"Тик: {effect}"

    reason = str(decision.get("reason") or "")
    if not reason:
        if action == "create_step" and step:
            reason = (f"постановка шага {step} на месте — страж закажет карточку шага"
                      if prompt_rows["exists"] else
                      f"нет постановки шага {step} — страж закажет её у роли промптера "
                      f"и остановится до появления файла")
        elif action == "create_prompt" and step:
            reason = f"нет постановки шага {step} — страж закажет её у роли промптера"
        else:
            reason = ACTION_REASONS.get(action, "")

    # Стоп — только у ВЫБРАННОГО плана: действия панели адресованы ему, чужой стоп не
    # должен глушить кнопку (D3).
    chosen = next((row for row in out.get("plans") or []
                   if str(row.get("id")) == plan_id), None)
    stop_row = (chosen or {}).get("stop") or None
    stop = None
    if stop_row:
        stop = {"kind": str(stop_row.get("kind") or "owner"),
                "reason": str(stop_row.get("reason") or ""),
                "at": str(stop_row.get("at") or "")}

    tick_available, disabled_reason = True, ""
    if out.get("preview"):
        tick_available = False
        disabled_reason = ("предпросмотр — только чтение: поставь конвейер в проект "
                           "(`hermes pipeline setup`)")
    elif stop:
        tick_available = False
        where = f" (с {stop['at']})" if stop["at"] else ""
        disabled_reason = (f"стоп-флаг ({stop['kind']}): "
                           f"{stop['reason'] or 'причина не записана'}{where}; снять: "
                           f"кнопка «Продолжить» или `python .pipeline/gate.py --resume`")

    # Простой читается из состояния плана (`waiting_since` пишет сторож), а не из карточки:
    # карточка живёт минуты, простой — часы.
    state = _plan_state(root, plan_id)
    since = state.get("waiting_since")
    waiting = None
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        seconds = max(0, int(time.time() - float(since)))
        waiting = {"seconds": seconds, "text": _duration(seconds),
                   "reason": str(state.get("waiting_reason") or "")}

    risk = str(next_step.get("risk") or data.get("risk") or "")

    progress: list[dict] = []
    for row in out.get("plans") or []:
        progress.append({
            "id": str(row.get("id")),
            "closed": int(row.get("closed") or 0),
            "total": _steps_total(root, cfg, str(row.get("file") or "")),
            "driven": bool(row.get("driven")),
            "next": str(row.get("next") or ""),
            "stop": bool(row.get("stop")),
        })

    return {"action": action, "step": step, "prompt": prompt_rows, "tick_label": tick_label,
            "reason": reason, "stop": stop, "tick_available": tick_available,
            "tick_disabled_reason": disabled_reason, "waiting": waiting,
            "fix_rounds": _fix_rounds(out.get("cards") or [], plan_id, step), "risk": risk,
            "progress": progress,
            # Задел под бейдж критичных проблем шага S5 (D8): проверки здесь не запускаются.
            "checks": None}


# ───────────────────────────── экран «Шаг» (план 3 S3) ─────────────────────────────

STEP_LINES = 200                        # строк на блок экрана «Шаг»
CARD_TEXT_LIMIT = 2000                  # символов результата карточки: с пометкой обрезки
TEXT_SUFFIXES = (".md", ".txt", ".json", ".py", ".html", ".css", ".js", ".yaml", ".yml",
                 ".toml", ".ini", ".cfg")
# Вердикт и коммит разбираются теми же выражениями, что у стража (`gate.py`): копия нужна
# потому, что движок вендорится в проект и импортировать его из плагина нельзя (D5).
_VERDICT_RE = re.compile(r"^#{0,4}\s*ВЕРДИКТ\s*:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                         re.IGNORECASE | re.MULTILINE)
_COMMIT_RE = re.compile(r"коммит\S*\s*`([0-9a-f]{7,40})`", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")
_LABEL_RE = re.compile(r"^(?P<label>[^:=]{1,24}?)\s*[:=]\s*(?P<value>.*)$")
_TICK_RE = re.compile(r"`([^`]+)`")


class UnsafePath(ValueError):
    """Путь за пределы проекта или не показываемый файл: панель такое не читает."""


def safe_path(root: Path | str, rel: str | None) -> Path:
    """Файл строго внутри корня проекта.

    Отклоняются абсолютные пути, `..`, точки (`.env`, `.git/…`) и симлинки, ведущие
    наружу: экран «Шаг» показывает только файлы проекта — это контракт шага 3-S3.
    """
    base = Path(root).resolve()
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw:
        raise UnsafePath("пустой путь")
    if raw.startswith(("/", "~")) or _WIN_ABS_RE.match(raw):
        raise UnsafePath(f"путь вне корня проекта: {raw}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise UnsafePath(f"путь вне корня проекта: {raw}")
    # Схема (`file:///C:/…`, `file:/…`) — не путь проекта: без этой проверки она
    # разрешалась в несуществующее имя внутри корня и давала 404 вместо отказа.
    if parts and ":" in parts[0]:
        raise UnsafePath(f"путь вне корня проекта: {raw}")
    if any(part.startswith(".") for part in parts):
        raise UnsafePath(f"служебный файл не показывается: {raw}")
    try:
        real = base.joinpath(*parts).resolve()
    except OSError as exc:
        raise UnsafePath(f"путь не разрешается: {raw} ({exc})") from exc
    if real != base and base not in real.parents:
        raise UnsafePath(f"путь вне корня проекта: {raw}")
    return real


def _lines_word(count: int) -> str:
    """Форма слова «строка»: 1 строка, 3 строки, 5 строк — пометка читается человеком."""
    if count % 10 == 1 and count % 100 != 11:
        return "строка"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "строки"
    return "строк"


def _clip(text: str, limit: int = STEP_LINES) -> dict:
    """Обрезать текст по строкам с явной пометкой: тихого усечения быть не должно."""
    lines = str(text or "").splitlines()
    total = len(lines)
    cap = max(1, int(limit or STEP_LINES))
    shown = lines[:cap]
    cut = total > len(shown)
    return {"text": "\n".join(shown), "shown": len(shown), "total": total, "truncated": cut,
            "note": (f"показаны первые {len(shown)} {_lines_word(len(shown))}, всего {total}"
                     if cut else "")}


def read_text_safe(root: Path | str, rel: str | None,
                   limit: int = STEP_LINES) -> dict:
    """Текст файла проекта с обрезкой. Бросает `UnsafePath` на всё, что вне корня."""
    target = safe_path(root, rel)
    # Порядок проверок важен для сообщения: каталог — не «не текстовый файл».
    if not target.exists():
        raise UnsafePath(f"файла нет: {rel}")
    if target.is_dir():
        raise UnsafePath(f"это каталог, а не файл: {rel}")
    if target.suffix.lower() not in TEXT_SUFFIXES:
        raise UnsafePath(f"показываем только текстовые файлы: {rel}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise UnsafePath(f"не прочитать {rel}: {exc}") from exc
    return {"path": str(rel or ""), "exists": True, **_clip(text, limit)}


def _clip_chars(text: str, limit: int) -> dict:
    """Обрезка по символам для текста без строк: та же явная пометка, что у `_clip`."""
    raw = str(text or "")
    cap = max(1, int(limit))
    cut = len(raw) > cap
    return {"text": raw[:cap], "shown": min(len(raw), cap), "total": len(raw),
            "truncated": cut,
            "note": f"показаны первые {cap} символов, всего {len(raw)}" if cut else ""}


def comments(db: Path, task_id: str, limit: int = 20) -> list[dict]:
    """Комментарии карточки: на них ведёт ссылка «комментарии» с экрана шага."""
    if not task_id:
        return []
    con = _ro(db)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT author, body, created_at FROM task_comments"
                           " WHERE task_id = ? ORDER BY created_at LIMIT ?",
                           (str(task_id), int(limit))).fetchall()
    except sqlite3.Error:                    # борда старой версии: таблицы может не быть
        return []
    finally:
        con.close()
    return [{"author": str(row[0] or ""), "body": str(row[1] or "")[:2000],
             "created_at": row[2]} for row in rows]


def comments_total(db: Path, task_id: str) -> int:
    """Сколько комментариев у карточки всего: нужно для пометки обрезки."""
    if not task_id:
        return 0
    con = _ro(db)
    if con is None:
        return 0
    try:
        row = con.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                          (str(task_id),)).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        con.close()
    return int(row[0] or 0) if row else 0


def _plan_file_of(root: Path, cfg: dict, plan_id: str) -> Path | None:
    """Файл плана `<номер>-*.md` — то же имя, что распознаёт реестр движка."""
    folder = Path(root) / str((cfg.get("paths") or {}).get("plans") or "docs/plans")
    if not folder.is_dir():
        return None
    prefix = f"{plan_id}-"
    matches = sorted(path for path in folder.glob("*.md")
                     if re.match(rf"^{re.escape(prefix)}\d*", path.name))
    return matches[0] if matches else None


def _bullets(lines: list[str]) -> list[str]:
    """Пункты `- …` с приклеенными строками переноса (в плане они переносятся по ширине)."""
    out: list[str] = []
    current: str | None = None
    for line in lines:
        match = _BULLET_RE.match(line)
        if match:
            if current:
                out.append(current)
            current = match.group("text")
        elif current is not None and line.strip() and not line.lstrip().startswith(("#", "|")):
            current += " " + line.strip()
        elif current is not None and not line.strip():
            out.append(current)
            current = None
    if current:
        out.append(current)
    return out


def _contract(root: Path, cfg: dict, plan_id: str, step_id: str) -> dict:
    """Контракт шага из плана: цель, файлы, гейты, «что сделать» и «Готово =»."""
    empty = {"found": False, "title": "", "goal": "", "files": [], "gates": [], "what": "",
             "done": ""}
    path = _plan_file_of(root, cfg, plan_id)
    if path is None:
        return {**empty, "error": f"файл плана {plan_id} не найден"}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {**empty, "error": f"план не прочитан: {exc}"}
    try:
        step_re = re.compile(str(cfg.get("step_id_regex") or _DEFAULT_STEP_ID))
    except re.error:
        step_re = re.compile(_DEFAULT_STEP_ID)
    found = _headings(lines)
    for pos, (index, _level, title) in enumerate(found):
        match = step_re.match(title)
        if not match or match.group("id").upper() != step_id:
            continue
        title_text = title[match.end():].strip().lstrip(".").strip()
        body = lines[index + 1:_section_end(found, pos, len(lines))]
        rows = {"found": True, "title": title_text, "goal": "", "files": [], "gates": [],
                "what": "", "done": ""}
        for bullet in _bullets(body):
            label = _LABEL_RE.match(bullet)
            if label is None:
                continue
            key = label.group("label").strip().lower()
            value = label.group("value").strip()
            if key.startswith("цель"):
                rows["goal"] = value
            elif key.startswith("файл"):
                rows["files"] = [item.strip() for item in _TICK_RE.findall(value)] or [value]
            elif key.startswith(("тест", "гейт")):
                rows["gates"] = [item.strip() for item in _TICK_RE.findall(value)] or [value]
            elif key.startswith("что сделать"):
                rows["what"] = value
            elif key.startswith("готово"):
                rows["done"] = value
        return {**rows, "path": _relative(root, path)}
    return {**empty, "error": f"шаг {step_id} в плане {plan_id} не найден",
            "path": _relative(root, path)}


_FINDING_NUMBER_RE = re.compile(r"^\s{0,8}(?P<num>\d{1,3})[.)]\s+(?P<text>\S.*)$")
_FINDING_ROLE_RE = re.compile(r"^\s{0,8}(?:\*\*)?\[(?P<cls>[^\]]{1,60})\](?:\*\*)?\s*"
                              r"(?P<text>\S.*)$")
_FINDING_BOLD_RE = re.compile(r"^\s{0,8}\*\*(?P<head>[^*]{1,160})\*\*\s*(?P<text>.*)$")
_FINDING_CLASS_RE = re.compile(r"блокирующ\w*|значим\w*|стилистическ\w*|критичн\w*|"
                               r"незначительн\w*", re.IGNORECASE)
# Строки, которые означают «находок нет», а не находку: их разбор не считает пунктами.
_NO_FINDINGS_RE = re.compile(r"^(?:—|-|\*)?\s*(?:нет\b|не\s+найден\w*|отсутству\w*|"
                             r"блокирующих\s+находок\s+нет)", re.IGNORECASE)


def _finding_class(text: str) -> str:
    """Явный класс находки из строки (`[блокирующая]`, `**[значимая]**`, `З1 (значимая…)`)."""
    match = _FINDING_CLASS_RE.search(str(text or ""))
    return match.group(0).lower() if match else ""


def _findings_section(text: str, limit: int = 40) -> tuple[list[dict], str]:
    """Находки ревью и пометка, если раздел непустой, но не разобран.

    Разбираются все форматы, которыми пишут отчёты: пункты `- …`/`* …`, нумерованные
    `1. …`, строки роли `[класс] file:line — …` и жирные `**[класс] …**`/`**З1 (класс).**`.
    Пустой блок находок читается как «их нет», поэтому неразобранный раздел помечается.
    """
    lines = text.splitlines()
    found = _headings(lines)
    start = next((pos for pos, (_i, level, title) in enumerate(found)
                  if level in (2, 3) and "наход" in title.lower()), None)
    if start is None:
        return [], ""
    end = _section_end(found, start, len(lines))
    body = lines[found[start][0] + 1:end]
    rows: list[dict] = []
    group = ""
    last_kind = ""
    substantive = False
    for line in body:
        head = _HEADING_RE.match(line)
        if head:
            group = head.group("title").strip()
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _NO_FINDINGS_RE.match(stripped):
            continue
        match = _BULLET_RE.match(line)
        if match:
            item = _finding_item(match.group("text"), group)
            rows.append(item)
            last_kind = "bullet"
        else:
            numbered = _FINDING_NUMBER_RE.match(line)
            role = _FINDING_ROLE_RE.match(line)
            bold = _FINDING_BOLD_RE.match(line)
            if numbered:
                rows.append(_finding_item(numbered.group("text"), group))
                last_kind = "number"
            elif role:
                rows.append(_finding_item(role.group("text"), group, role.group("cls")))
                last_kind = "role"
            elif bold and bold.group("text").strip():
                rows.append(_finding_item((bold.group("head") + " " +
                                           bold.group("text")).strip(), group))
                last_kind = "bold"
            elif bold:
                rows.append(_finding_item(bold.group("head"), group))
                last_kind = "bold"
            elif rows and (line.startswith((" ", "\t")) or last_kind == "bold"):
                # Перенос строки внутри пункта: у плана и отчётов он есть и в отступе,
                # и без него (жирный пункт продолжается обычным абзацем).
                rows[-1]["text"] = (rows[-1]["text"] + " " + stripped)[:1200]
            else:
                substantive = True
                continue
        if len(rows) >= limit:
            break
    note = ""
    if not rows and substantive:
        note = ("раздел «Находки» есть, но разобрать пункты не удалось — "
                "смотри текст отчёта")
    return rows, note


def _finding_item(text: str, group: str, explicit: str = "") -> dict:
    """Один пункт находки: класс из явной метки, иначе — из подзаголовка раздела."""
    # `**` в тексте — разметка отчёта, а не содержимое: панель показывает пункт одной строкой.
    body = str(text or "").replace("**", "").strip()
    cls = _finding_class(explicit) or _finding_class(body)
    return {"level": cls or group or "находка", "text": body, "class": cls}


def _findings(text: str, limit: int = 40) -> list[dict]:
    """Находки ревью: пункты раздела «Находки» с подзаголовками-уровнями."""
    return _findings_section(text, limit)[0]


def _review(root: Path, cfg: dict, plan_id: str, step_id: str,
            limit: int = STEP_LINES) -> dict:
    """Отчёт ревью: путь, текст, вердикт (ровно как у стража), коммит шага, находки."""
    rel = (f"{((cfg.get('paths') or {}).get('reviews') or 'docs/reviews').rstrip('/')}"
           f"/{plan_id}-{step_id}.md")
    path = Path(root) / rel
    rows = {"exists": False, "path": rel, "verdict": "NONE", "occurrences": 0,
            "commit": "", "findings": [], "findings_note": "", "text": "", "shown": 0,
            "total": 0, "truncated": False, "note": ""}
    if not path.is_file():
        rows["note"] = "отчёта ревью нет"
        return rows
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        rows["note"] = f"отчёт не прочитан: {exc}"
        return rows
    found = [match.group(1).upper() for match in _VERDICT_RE.finditer(text)]
    unique = sorted(set(found))
    verdict = "NONE" if not found else (unique[0] if len(unique) == 1 else "AMBIGUOUS")
    commit = _COMMIT_RE.search(text)
    findings, findings_note = _findings_section(text)
    return {"exists": True, "path": rel, "verdict": verdict, "occurrences": len(found),
            "commit": commit.group(1) if commit else "",
            "findings": findings, "findings_note": findings_note, **_clip(text, limit)}


def _git_show(root: Path, sha: str, limit: int = STEP_LINES, timeout: int = 30) -> dict:
    """`git show` коммита шага: сводка целиком, дифф — с обрезкой и явной пометкой."""
    sha = str(sha or "").strip().lower()
    rows = {"sha": sha, "short": sha[:7], "available": False, "error": "",
            "stat": "", "diff": {"text": "", "shown": 0, "total": 0, "truncated": False,
                                 "note": ""}}
    if not _SHA_RE.match(sha):
        rows["error"] = "в отчёте ревью нет коммита шага"
        return rows
    base = ["git", "-C", str(root)]
    try:
        stat = proj.run([*base, "show", "--stat", "--no-color", "--format=%h %s", sha],
                        timeout=timeout)
        patch = proj.run([*base, "show", "--patch", "--no-color", "--format=", sha],
                         timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        rows["error"] = f"git недоступен: {exc}"
        return rows
    if stat.returncode != 0:
        rows["error"] = ((stat.stderr or stat.stdout or "").strip() or
                         f"git show {sha}: код {stat.returncode}")[:400]
        return rows
    rows["available"] = True
    rows["stat"] = (stat.stdout or "").strip()
    if patch.returncode != 0:
        rows["error"] = ((patch.stderr or patch.stdout or "").strip() or
                         f"git show --patch {sha}: код {patch.returncode}")[:400]
        return rows
    rows["diff"] = _clip((patch.stdout or "").strip(), limit)
    return rows


def step_detail(project: Path | str | None = None, plan_id: str | int = "",
                step_id: str = "", limit: int = STEP_LINES,
                board: str | None = None) -> dict:
    """Экран «Шаг»: контракт из плана, постановка, отчёт ревью, коммит и карточки шага.

    Ничего не пишет и движок не запускает: только файлы проекта, `git show` и борда.
    Контракт ищется по файлу плана `<номер>-*.md` — того же вида, что ведёт страж.
    """
    root = Path(project).resolve() if project else proj.find_project()
    cfg = proj.read_config(root)
    plan = str(plan_id).strip()
    if plan.isdigit():
        plan = str(int(plan))
    step = str(step_id or "").strip().upper()
    if not plan or not step:
        raise ValueError("нужны параметр plan и id шага")
    cap = max(1, int(limit or STEP_LINES))
    contract = _contract(root, cfg, plan, step)
    prompt_path = _prompt_file(root, cfg, plan, step)
    prompt: dict = {"exists": prompt_path is not None,
                    "path": _relative(root, prompt_path),
                    "text": "", "shown": 0, "total": 0, "truncated": False,
                    "note": "" if prompt_path else "постановки нет"}
    if prompt_path is not None:
        try:
            prompt.update(_clip(prompt_path.read_text(encoding="utf-8", errors="replace"), cap))
        except OSError as exc:
            prompt["note"] = f"постановка не прочитана: {exc}"
            prompt["exists"] = False
    review = _review(root, cfg, plan, step, cap)
    held = [str(item) for item in (_plan_state(root, plan).get("hold_steps") or [])]
    stop = _plan_stop(root, plan)
    db = proj.board_db(root, board)
    prefix = f"planloop:{plan}:{step}:"
    step_cards: list[dict] = []
    # Карточки — запросом по префиксу ключа: окно «последние N задач» теряло старый шаг.
    for card in cards(db, limit=100, key_prefix=prefix):
        card_id = str(card.get("id") or "")
        result = _clip_chars(str(card.get("result") or ""), CARD_TEXT_LIMIT)
        shown_comments = comments(db, card_id)
        total_comments = comments_total(db, card_id)
        step_cards.append({"id": card_id, "title": str(card.get("title") or ""),
                           "status": str(card.get("status") or ""),
                           "assignee": str(card.get("assignee") or ""),
                           "key": str(card.get("idempotency_key") or ""),
                           "result": result["text"], "result_note": result["note"],
                           "result_truncated": result["truncated"],
                           "completed_at": card.get("completed_at"),
                           "comments": shown_comments,
                           "comments_note": (f"показаны первые {len(shown_comments)} "
                                             f"комментариев, всего {total_comments}"
                                             if total_comments > len(shown_comments) else "")})
    return {"ok": True, "plan": plan, "step": step, "title": contract.get("title") or "",
            "contract": contract, "prompt": prompt, "review": review,
            "commit": _git_show(root, review.get("commit") or "", cap),
            "cards": step_cards,
            # `allowed` — правило «стоп-флаг допуск не гасит» в снимке, а не в разметке:
            # так его проверяет тест, а страница только рисует.
            "hold": {"steps": held, "held": step in held, "allowed": step not in held,
                     "stop": str(stop.get("kind") or ""),
                     "stop_reason": str(stop.get("reason") or "")},
            "limit": cap}


def _build_snapshot(root: Path, plan: str | None, preview: bool, board: str | None) -> dict:
    """Собрать снимок с нуля: три запуска движка, борда и хвост журнала."""
    cfg = proj.read_config(root)
    engine_dir = proj.ENGINE_SRC if preview else None
    extra_env = {"PIPELINE_ASSUME_DEFAULTS": "1"} if preview else {}
    if preview and board:
        extra_env["PIPELINE_BOARD"] = board
    out: dict[str, Any] = {
        "project": cfg.get("project") or root.name,
        "root": str(root),
        "board": board or cfg.get("board") or "default",
        "profiles": cfg.get("profiles") or {},
        "runtimes": cfg.get("runtimes") or {},
        "driven": cfg.get("driven_plans") or [],
        "require_prompt": cfg.get("require_prompt", True),
        "telegram": telegram_state(root),
        "preview": preview,
        "plans": [],
        "plan": None,
        "package": None,
        "decision": None,
        "status": "",
        "cards": cards(proj.board_db(root, board)),
        "journal": [],
        "errors": [],
    }
    try:
        out["plans"] = proj.engine_json(root, "gate.py", "--plans", engine_dir=engine_dir,
                                        extra_env=extra_env) or []
    except Exception as exc:                                    # noqa: BLE001 - показываем в UI
        out["errors"].append(f"планы: {exc}")
    chosen = plan
    if chosen is None and out["plans"]:
        # по умолчанию — план, у которого есть открытые шаги (иначе первый по номеру)
        pending = [p["id"] for p in out["plans"]
                   if str(p.get("next") or "") not in ("", "—", "None")]
        chosen = str(pending[0]) if pending else str(out["plans"][0]["id"])
    args = ["--plan", str(chosen)] if chosen else []
    try:
        decision = proj.engine_json(root, "gate.py", "--status", *args,
                                    engine_dir=engine_dir, extra_env=extra_env) or {}
        out["decision"] = decision.get("decision") or {}
        out["status"] = decision.get("status") or ""
    except Exception as exc:                                    # noqa: BLE001
        out["errors"].append(f"решение: {exc}")
    try:
        pkg = proj.engine_json(root, "next_step.py", *args,
                               engine_dir=engine_dir, extra_env=extra_env) or {}
        out["package"] = pkg
        out["plan"] = {
            "id": pkg.get("plan_id"),
            "file": pkg.get("plan_file"),
            "steps_total": pkg.get("steps_total", 0),
            "closed": pkg.get("closed") or [],
            "steps": pkg.get("steps") or [],
            "next": pkg.get("next"),
            "note": pkg.get("note") or "",
        }
        out["journal"] = journal_tail(root, pkg.get("plan_file"))
        out["git"] = pkg.get("git") or {}
    except Exception as exc:                                    # noqa: BLE001
        out["errors"].append(f"шаги: {exc}")
    decision = out.get("decision") or {}
    out["stage"] = ACTION_LABELS.get(decision.get("action") or "", decision.get("action") or "")
    out["stop"] = any((p.get("stop") for p in out["plans"]))
    out["overview"] = _overview(root, cfg, out)
    return out


def snapshot(project: Path | str | None = None, plan: str | None = None,
             preview: bool = False, board: str | None = None) -> dict:
    """Полный снимок: конфиг, планы, шаги, карточки, журнал, решение стража.

    *preview* — читать проект, в котором конвейер не установлен: движок берётся из
    плагина, конфиг — умолчания (борду можно задать явно). Ничего не пишется.

    Ответ кэшируется по ключу (проект, план, предпросмотр, борда) на CACHE_TTL секунд и
    сбрасывается, как только изменился отпечаток источников. Сборка идёт под замком с
    двойной проверкой: два одновременных запроса не запускают движок дважды. Наружу
    отдаётся мелкая копия — вызывающий дописывает `log`/`plan_mode`/`ok`, кэш не мутирует.
    """
    root = Path(project).resolve() if project else proj.find_project()
    key = (str(root), str(plan or ""), bool(preview), str(board or ""))
    stamp = fingerprint(root, plan, preview, board)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            built, cached_stamp, value = cached
            if cached_stamp == stamp and time.monotonic() - built <= CACHE_TTL:
                return dict(value)
        result = _build_snapshot(root, plan, preview, board)
        _CACHE[key] = (time.monotonic(), stamp, result)
        if len(_CACHE) > CACHE_LIMIT:
            extra = len(_CACHE) - CACHE_LIMIT
            for stale in sorted(_CACHE, key=lambda item: _CACHE[item][0])[:extra]:
                _CACHE.pop(stale, None)
        return dict(result)


if __name__ == "__main__":  # ручная проверка: python -m plugin.snapshot <проект>
    import sys
    print(json.dumps(snapshot(sys.argv[1] if len(sys.argv) > 1 else None),
                     ensure_ascii=False, indent=2, default=str))
