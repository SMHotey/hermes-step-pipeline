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

try:
    from . import project as proj
    from . import update
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore
    import update  # type: ignore

#: Порядок раскатки: плагин ставится до движка, иначе новый движок поедет по проектам
#: старой установкой. Список читает S8A — второго описания порядка не заводится (D1).
STEPS: tuple[str, ...] = ("plugin", "engine")
PLUGIN_STEP = STEPS[0]
#: Ключ записи в реестре профиля: `rollout` → ключ шага (`plugin`, позже `engine`).
RECORD_KEY = "rollout"
PLUGIN_KEY = "plugin"

#: Блокировка от второго жеста: одна установка за раз на профиль (D5).
_LOCK = threading.Lock()


def _stamp() -> str:
    """Метка времени итога в формате стоп-флага плана и маркера кругов."""
    return time.strftime("%d.%m.%Y %H:%M:%S")


def read_plugin() -> dict:
    """Последний итог обновления плагина из реестра профиля; нет записи — `{}`.

    Битый реестр (`ValueError` из `proj._read_registry_raw`) пробрасывается наверх:
    что ответить — решает вызывающий, а не читатель.
    """
    container, _entries = proj._read_registry_raw()
    if not isinstance(container, dict):
        return {}
    record = container.get(RECORD_KEY)
    if not isinstance(record, dict):
        return {}
    entry = record.get(PLUGIN_KEY)
    return entry if isinstance(entry, dict) else {}


def write_plugin(entry: dict) -> None:
    """Записать итог в реестр профиля, сохранив чужие ключи контейнера.

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
    box[RECORD_KEY][PLUGIN_KEY] = entry
    proj._write_registry(box, entries)


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
