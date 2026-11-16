# -*- coding: utf-8 -*-
"""Правило раскатки: обновление плагина — предварительный шаг раскатки движка.

Движок раскатывается только после плагина; порядок и состав шагов — `STEPS`, и второго
описания порядка в пакете нет. Итог каждого шага живёт в реестре профиля (`projects.json`)
ключом верхнего уровня `rollout`, по ключу шага: плагин пишется сюда, движок — в S8A.

Установку не переделываем: `update.install` уже скачивает и распаковывает релиз, а модуль
лишь ставит вокруг неё три вещи, которых в установке нет: блокировку от второго жеста,
запись итога в реестр и ответ, пригодный и браузеру, и панели.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

try:
    from . import panel_pause
    from . import project as proj
    from . import setup_flow
    from . import update
    from .engine import state_io as engine_state_io
except ImportError:  # pragma: no cover
    import panel_pause  # type: ignore
    import project as proj  # type: ignore
    import setup_flow  # type: ignore
    import update  # type: ignore
    import state_io as engine_state_io  # type: ignore

#: Порядок раскатки: плагин ставится до движка, иначе новый движок поедет по проектам
#: старой установкой. Список читает S8A — второго описания порядка не заводится (D1).
STEPS: tuple[str, ...] = ("plugin", "engine")
PLUGIN_STEP = STEPS[0]
#: Ключ записи в реестре профиля: `rollout` → ключ шага (`plugin`, `engine`).
RECORD_KEY = "rollout"
PLUGIN_KEY = "plugin"
#: Свой шаг берётся из общего порядка, а не из второго списка (D1).
ENGINE_STEP = STEPS[-1]
ENGINE_KEY = "engine"

#: Пауза перед повторной проверкой окна — дебаунс как `RestartSec` у systemd.
ROLLOUT_INTERVAL = 5.0
#: Предел ожидания окна на проект: дольше — проект `failed`, раскатка идёт дальше (D5).
ROLLOUT_WAIT_LIMIT = 1800.0

#: Статусы строки проекта в записи `rollout.engine`; их читает S8B.
STATUS_UPDATED = "updated"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_WAITING = "waiting"

#: Подписи статусов — одна таблица на весь пакет: слова плана, других формулировок нет (D5).
STATUS_LABELS: dict[str, str] = {STATUS_UPDATED: "обновлено", STATUS_SKIPPED: "пропущено",
                                 STATUS_FAILED: "ошибка", STATUS_WAITING: "ждёт"}
#: Порядок счёта и подписи — как в плане; второго порядка не заводится.
STATUS_ORDER: tuple[str, ...] = (STATUS_UPDATED, STATUS_SKIPPED, STATUS_FAILED, STATUS_WAITING)
#: Ключи строки статуса проекта — ровно эти и в этом порядке (сверяет тест шага).
PROJECT_ROW_KEYS: tuple[str, ...] = ("id", "name", "root", "status", "status_label", "from", "to",
                                     "reason", "error", "at")

#: Статус «раскатки не было»: отсутствие записи, а не нули, выставленные заранее (D1).
NO_ROLLOUT_TEXT = "раскатка движка не запускалась"
#: Причина строки проекта, до которого раскатка не дошла.
WAITING_QUEUE_REASON = "ждёт очереди"
#: Пояснение к статусу, когда запись говорит «идёт», а живого процесса уже нет (D3).
STALE_NOTE = "раскатка прервана: запись говорит «идёт», живого процесса нет"

#: Блокировка от второго жеста: одна установка за раз на профиль (D5).
_LOCK = threading.Lock()


def _stamp() -> str:
    """Метка времени итога в формате стоп-флага плана и маркера кругов."""
    return time.strftime("%d.%m.%Y %H:%M:%S")


def _read_record(key: str) -> dict:
    """Запись шага раскатки из контейнера `rollout`; нет записи — `{}`.

    Битый реестр (`ValueError` из `proj._read_registry_raw`) пробрасывается наверх:
    что ответить — решает вызывающий, а не читатель.
    """
    container, _entries = proj._read_registry_raw()
    if not isinstance(container, dict):
        return {}
    record = container.get(RECORD_KEY)
    if not isinstance(record, dict):
        return {}
    entry = record.get(key)
    return entry if isinstance(entry, dict) else {}


def read_plugin() -> dict:
    """Последний итог обновления плагина из реестра профиля; нет записи — `{}`."""
    return _read_record(PLUGIN_KEY)


def read_engine() -> dict:
    """Последний итог раскатки движка из реестра профиля; нет записи — `{}`."""
    return _read_record(ENGINE_KEY)


def _projection(entry: dict) -> dict:
    """Плоские ключи контейнера `rollout` из записи шага `engine`.

    Это проекция, а не второе состояние (D1): их пишет тот же `_put_record` и в том же
    атомарном сохранении, поэтому расхождению негде появиться. Владелец читает статус,
    не зная формы записи, а код по-прежнему читает запись шага (`read_engine`).
    """
    return {"running": bool(entry.get("running")),
            "at": str(entry.get("at") or ""),
            "projects": list(entry.get("projects") or [])}


def _put_record(key: str, entry: dict) -> None:
    """Записать итог шага раскатки в реестр профиля, сохранив чужие ключи контейнера.

    Форма-список приводится к словарю `{"projects": [...]}`: в список ключ `rollout` не
    положить, а `read_projects` читает обе формы. Запись атомарна и делает её
    `proj._write_registry` — второго писателя `projects.json` в пакете нет (D3).
    """
    container, entries = proj._read_registry_raw()
    box: dict = container if isinstance(container, dict) else {"projects": entries}
    if RECORD_KEY not in box:
        box[RECORD_KEY] = {}
    # `rollout` в контейнере есть, но не словарь — это `TypeError` на присваивании:
    # портить чужую запись догадкой нельзя, отказ уходит наверх (D3).
    box[RECORD_KEY][key] = entry
    if key == ENGINE_KEY:
        # Плоские ключи контейнера — проекция записи шага `engine` (D1): владелец читает
        # статус, не зная формы записи, а источник истины остаётся один.
        box[RECORD_KEY].update(_projection(entry))
    proj._write_registry(box, entries)


def write_plugin(entry: dict) -> None:
    """Записать итог обновления плагина (правило записи одно — `_put_record`, D14)."""
    _put_record(PLUGIN_KEY, entry)


def write_engine(entry: dict) -> None:
    """Записать итог раскатки движка тем же правилом, что и плагин (D14)."""
    _put_record(ENGINE_KEY, entry)


def busy() -> bool:
    """Идёт ли установка прямо сейчас (для панели и второго жеста)."""
    return _LOCK.locked()


# ───────────────────────── статус раскатки для панели (S8B) ─────────────────────────

def _summary(rows: list[dict]) -> dict:
    """Счёт строк по статусам: ключи и порядок — `STATUS_ORDER`, нули на месте (D6)."""
    counts = {status: 0 for status in STATUS_ORDER}
    for row in rows:
        status = str(row.get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def _summary_text(summary: dict) -> str:
    """«обновлено: 1, пропущено: 0, ошибка: 0, ждёт: 2» — русские подписи из одной таблицы."""
    return ", ".join(f"{STATUS_LABELS[status]}: {int(summary.get(status) or 0)}"
                     for status in STATUS_ORDER)


def _status_text(running: bool, stale: bool, ok: bool | None, at: str, finished: str,
                 summary: dict, note: str) -> str:
    """Готовая строка статуса: страница печатает её как есть и ничего не досчитывает (D6).

    `finished` может быть пустым (запись сделана не `run_engine`) — тогда берётся `at`;
    подставлять пустоту в текст нельзя, поэтому путь «закончена» требует хоть одной метки.
    """
    text = _summary_text(summary)
    if ok is None and not at:
        # Записи нет: «не запускалась» — это факт, а не нули, выставленные заранее (D1).
        return note or NO_ROLLOUT_TEXT
    if running:
        return f"идёт раскатка с {at}: {text}"
    if stale:
        return f"раскатка прервана, запись от {at}: {text}"
    when = finished or at
    if ok is False and summary.get(STATUS_FAILED):
        return f"раскатка закончена {when} с ошибками: {text}"
    return f"раскатка закончена {when}: {text}"


def _status_row(entry: dict | None, registry_row: dict) -> dict:
    """Строка статуса одного проекта: исход из записи шага, иначе — `waiting`.

    Порядок и набор ключей — `PROJECT_ROW_KEYS`; проект реестра без строки в записи
    показан `waiting` с причиной «ждёт очереди»: раскатка до него не дошла, и это видно.
    """
    root = str(registry_row.get("root") or "")
    if not entry:
        return {"id": str(registry_row.get("id") or ""),
                "name": str(registry_row.get("name") or ""),
                "root": root,
                "status": STATUS_WAITING,
                "status_label": STATUS_LABELS[STATUS_WAITING],
                "from": "", "to": "",
                "reason": WAITING_QUEUE_REASON,
                "error": "",
                "at": ""}
    status = str(entry.get("status") or "")
    return {"id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            "root": str(entry.get("root") or root),
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "from": str(entry.get("from") or ""),
            "to": str(entry.get("to") or ""),
            "reason": str(entry.get("reason") or ""),
            "error": str(entry.get("error") or ""),
            "at": str(entry.get("at") or "")}


def status() -> dict:
    """Статус раскатки движка: жив ли процесс, чем закончилось и строка на каждый проект.

    Источник строк — реестр профиля (`proj.read_projects`), исходов — запись
    `rollout.engine`; слияние по `id` (D4). Порядок строк — порядок реестра, а записи,
    чьего проекта в реестре уже нет, идут в хвосте: история не теряется.

    `running` — живой процесс раскатки (`busy()`) при записи «идёт»: запись `running: true`
    без процесса (панель перезапустили) — не раскатка, а `stale` с пояснением (D3).
    """
    registry = proj.read_projects()
    note = ""
    try:
        engine = read_engine()
    except ValueError as exc:
        # Битый реестр статус не отменяет: владелец видит причину, а не отказ панели.
        # `read_projects` такой файл переживает сам и отдаёт пустой список: выдуманных
        # строк в статусе не будет.
        engine = {}
        note = f"реестр профиля не разбирается: {exc}"
    outcomes: dict[str, dict] = {}
    for item in engine.get("projects") or []:
        if isinstance(item, dict):
            key = str(item.get("id") or "")
            if key and key not in outcomes:
                outcomes[key] = item
    rows = [_status_row(outcomes.get(str(row.get("id") or "")), row) for row in registry]
    seen = {str(row.get("id") or "") for row in registry}
    for item in engine.get("projects") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        rows.append(_status_row(item, item))
    summary = _summary(rows)
    process = busy()
    # «Идёт» — это живой процесс, а не само поле записи: панель перезапустили посреди
    # раскатки, запись осталась `running: true`, а раскатки уже нет (D3). Живой процесс
    # без записи «идёт» — не раскатка движка, а другая установка, и кнопку она не держит.
    running = process and bool(engine.get("running"))
    stale = bool(engine.get("running")) and not process
    at = str(engine.get("at") or "")
    finished = str(engine.get("finished") or "")
    if not engine:
        ok = None
    elif stale:
        ok = False
    else:
        # `ok` пишет каждый итог; запись без него (владелец собрал руками) — не провал.
        ok = bool(engine["ok"]) if "ok" in engine else None
    if stale:
        note = STALE_NOTE
    return {"running": running, "stale": stale, "at": at, "finished": finished, "ok": ok,
            "note": note, "summary": summary, "summary_text": _summary_text(summary),
            "status_text": _status_text(running=running, stale=stale, ok=ok, at=at,
                                        finished=finished, summary=summary, note=note),
            "projects": rows}


def _failure(before: str, exc: BaseException) -> dict:
    """Запись-отказ по D4: прежнего «обновлено» в ней не остаётся ни одним ключом."""
    error = str(exc) if isinstance(exc, update.UpdateError) else f"{type(exc).__name__}: {exc}"
    return {"step": PLUGIN_STEP, "at": _stamp(), "ok": False, "updated": False,
            "from": before, "to": "", "latest": "", "error": error, "message": ""}


def _success(answer: dict) -> dict:
    """Запись-успех по D4 — ровно те ключи, что видит владелец в реестре."""
    to = str(answer.get("to") or "")
    return {"step": PLUGIN_STEP, "at": _stamp(), "ok": bool(answer.get("ok", True)),
            "updated": bool(answer.get("updated", False)),
            "from": str(answer.get("from") or ""), "to": to,
            "latest": str(answer.get("latest") or to),
            "error": str(answer.get("error") or ""),
            "message": str(answer.get("message") or "")}


def update_plugin(tag: str = "") -> dict:
    """Поставить релиз *tag* (пусто — последний) и записать итог в реестр.

    Ответ несёт ключи записи плюс `busy` при занятости и `backup`/`restart` из установки,
    если она их вернула: реестр — журнал итогов, а браузеру нужен путь к копии пакета и
    напоминание о перезапуске. Занятость реестр не трогает: иначе второй жест затёр бы
    итог первого (D5).
    """
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "step": PLUGIN_STEP,
                "error": "обновление плагина уже идёт", "at": ""}
    try:
        return _install(tag)
    finally:
        _LOCK.release()


def _install(tag: str) -> dict:
    """Установка под захваченной блокировкой: ответ установки → запись → ответ панели.

    `try/except` покрывает только вызов `update.install` и разбор его ответа. Отказы
    реестра (битый JSON — `ValueError`, отказ записи — `OSError`, `rollout` не словарь —
    `TypeError`) уходят наверх как есть: это не отказ установки, и выдавать их за сетевую
    ошибку нельзя (D3). Блокировку освобождает `finally` вызывающего и на этих путях.
    """
    before = update.local_version()
    try:
        answer = dict(update.install(tag=tag))
    except Exception as exc:                                   # noqa: BLE001
        entry = _failure(before, exc)
        write_plugin(entry)
        return dict(entry)
    entry = _success(answer)
    write_plugin(entry)
    result = dict(entry)
    for key in ("backup", "restart"):
        if answer.get(key):
            result[key] = answer[key]
    return result


# ───────────────────────────── раскатка движка (S8A) ─────────────────────────────

def window(root: Path) -> str:
    """Причина, по которой движок в проекте менять нельзя; окно свободно — `''`.

    Окно — это отсутствие идущего шага, а не время суток: прежняя копия `gate.py` под
    работающим тиком ломает идущий шаг. Поэтому в проверке две вещи: живая блокировка
    тика (`state/tick.lock`, протухшая — не помеха: её снимет сам движок) и карточки
    борды в работе. Текст причины попадает в запись реестра как есть.
    """
    payload = engine_state_io.holder(proj.state_dir(root))
    if payload is not None and not engine_state_io._stale(payload, engine_state_io.STALE_AFTER):
        return (f"идёт тик: блокировка с {payload.get('at') or '?'} "
                f"(pid {payload.get('pid') or '?'})")
    cards = panel_pause.running_cards(root)
    if cards:
        return "идёт шаг: карточки " + ", ".join(
            str(card.get("id") or "?") for card in cards[:3])
    return ""


def _upgrade(project: Path, log: setup_flow.Log) -> int:
    """Один рычаг раскатки: обновление движка и ролей из установленного плагина.

    Шов намеренно тонкий: тесты подменяют его, а не `setup_flow`, и каждый вызов
    `setup_flow.upgrade` в них виден по отдельности.
    """
    return setup_flow.upgrade(project, log, trust=True)


def _start_record(rows: list[dict], started: str) -> dict:
    """Стартовая запись `rollout.engine`: идёт раскатка, итога ещё нет.

    `projects` — `list(rows)`: вызывающий передаёт `[]`, чтобы строки реестра
    (с `Path` внутри) не попали в журнал как есть.
    """
    return {"step": ENGINE_STEP, "running": True, "at": started or _stamp(),
            "finished": "", "ok": False, "error": "", "projects": list(rows)}


def _row(row: dict, status: str, *, at: str = "", was: str = "", now: str = "",
         reason: str = "", error: str = "") -> dict:
    """Строка проекта в записи раскатки — ровно те ключи, что читает S8B."""
    return {"id": str(row.get("id") or ""), "name": str(row.get("name") or ""),
            "root": str(row["root"]), "status": status, "from": str(was),
            "to": str(now), "reason": str(reason), "error": str(error),
            "at": at or _stamp()}


def _record(rows: list[dict], *, running: bool, ok: bool, begin: str,
            error: str = "") -> dict:
    """Собрать запись и записать её; отказ реестра не отменяет сделанную работу (D8).

    Следующая запись несёт полный список строк, поэтому потерянная не рвёт раскатку:
    имя проекта уже сказано и в `setup_flow.upgrade`, и в строке причины.
    """
    entry = {"step": ENGINE_STEP, "running": running, "at": begin,
             "finished": "" if running else _stamp(), "ok": ok, "error": error,
             "projects": list(rows)}
    try:
        write_engine(entry)
    except (OSError, ValueError, TypeError) as exc:
        proj.debug_note("rollout.engine", exc)
    return entry


def run_engine(rows: list[dict], *, interval: float = ROLLOUT_INTERVAL,
               limit: float = ROLLOUT_WAIT_LIMIT, sleep=time.sleep,
               started: str = "") -> dict:
    """Синхронный проход по проектам: окно → `upgrade` → строка → запись реестра.

    Один проект за раз: тяжёлый `upgrade` (копия движка, роли, cron, доверие) не должен
    идти параллельно двум проектам, иначе непонятно, чей отказ в итоге. Ожидание окна
    имеет предел: не дождались — проект `failed` с причиной, раскатка идёт дальше (D5).
    `sleep`, `interval` и `limit` — параметры, чтобы тест не ждал настоящие полчаса.
    """
    begin = started or _stamp()
    # Нулевой (или отрицательный) интервал не должен крутить цикл ожидания вечно:
    # `waited += interval` тогда не дойдёт до предела, окно будет считаться занятым
    # навсегда и блокировка раскатки останется захваченной (ревью 6-S8A, находка 5).
    interval = max(float(interval), 0.01)
    results: list[dict] = []
    ok = True
    for row in rows:
        position: int | None = None
        try:
            root = Path(row["root"])
            if not (root / ".pipeline" / "gate.py").is_file():
                final = _row(row, STATUS_SKIPPED, reason="конвейер не установлен")
            else:
                reason = window(root)
                if reason:
                    # Первое обнаружение пишется сразу: панель должна видеть, чего ждём.
                    position = len(results)
                    results.append(_row(row, STATUS_WAITING, reason=reason))
                    _record(results, running=True, ok=False, begin=begin)
                    waited = 0.0
                    while reason and waited < limit:
                        sleep(interval)
                        waited += interval
                        reason = window(root)
                if reason:
                    final = _row(row, STATUS_FAILED,
                                 reason=f"окно не найдено за {limit:g} с: {reason}")
                else:
                    # Окно открыто: `upgrade` зовётся даже когда движок актуален — он же
                    # чинит дрейф по маркеру, а статус решается сравнением версий (D7).
                    was = setup_flow.engine_version(root)
                    code = _upgrade(root, setup_flow.Log(quiet=True))
                    now = setup_flow.engine_version(root)
                    if code != 0:
                        final = _row(row, STATUS_FAILED, error=f"upgrade вернул {code}")
                    elif was == now:
                        final = _row(row, STATUS_SKIPPED, reason="движок уже актуален")
                    else:
                        final = _row(row, STATUS_UPDATED, was=was, now=now)
        except Exception as exc:                               # noqa: BLE001
            # Карточки, маркер, lock — всё, что может отказать на проекте. Итог проекта
            # записывается, раскатка продолжается: один проект не держит остальные (D5).
            final = _row(row, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
        if position is None:
            results.append(final)
        else:
            results[position] = final
        if final["status"] == STATUS_FAILED:
            ok = False
        _record(results, running=True, ok=False, begin=begin)
    return _record(results, running=False, ok=ok, begin=begin)


def _background(rows: list[dict], started: str) -> None:
    """Фоновый проход: исключение не роняет поток молча и всегда отпускает блокировку.

    Необработанное исключение в потоке не увидит никто: сервер ответил 200 ещё до него,
    а pytest превратил бы это в предупреждение. Поэтому общий отказ — запись реестра.
    """
    try:
        run_engine(rows, started=started)
    except Exception as exc:                                   # noqa: BLE001
        proj.debug_note("rollout.engine", exc)
        _record([], running=False, ok=False, begin=started,
                error=f"{type(exc).__name__}: {exc}")
    finally:
        _LOCK.release()


def start_engine(rows: list[dict]) -> dict:
    """Запустить раскатку движка в фоне; ответ — стартовая запись или `busy`.

    Ответ 200 означает «раскатка пошла», а не «движок везде обновлён»: `upgrade` ждёт
    окна до получаса на проект, держать HTTP-запрос столько нельзя. Итог читается в
    реестре (`rollout.engine`), туда же пишутся строки проектов по ходу.
    """
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "step": ENGINE_STEP,
                "error": "раскатка движка уже идёт", "at": ""}
    started = _stamp()
    try:
        entry = _start_record([], started)
        write_engine(entry)
    except (OSError, ValueError, TypeError):
        # Реестр не пишется — раскатку не начинаем: итог некуда класть, а панель
        # покажет «идёт» навсегда. Блокировку отпускаем, отказ уходит наверх (D8).
        _LOCK.release()
        raise
    thread = threading.Thread(target=_background, args=(rows, started), daemon=True,
                             name="pipeline-rollout-engine")
    thread.start()
    return entry
