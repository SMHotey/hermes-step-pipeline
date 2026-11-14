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
