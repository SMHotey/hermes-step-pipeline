# -*- coding: utf-8 -*-
"""Тик конвейера для cron (`--no-agent`): один шаг стража `gate.py --run`.

Молчание в выводе = конвейер ждёт (шаг в работе, ревью идёт, стоп-флаг). Поэтому
джобу заводят с `--deliver local`: stdout попадёт в историю запусков, а не в чат.

Куда кладут этот файл: `profiles/<профиль>/scripts/pipeline_tick.py` — cron принимает
`--script` только оттуда. Корень проекта берётся из `PIPELINE_REPO`, иначе из `--workdir`
джобы (текущий каталог), иначе из расположения файла (когда скрипт лежит в репо).
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

#: Префикс переменных канала уведомлений конвейера (как `NOTIFY_ENV_PREFIX` в `project.py`).
NOTIFY_ENV_PREFIX = "STEP_PIPELINE_TELEGRAM_"
#: Общие переменные Bot API: прокси/база тоже принадлежат каналу, а не процессу-родителю.
NOTIFY_SHARED_ENVS = ("TELEGRAM_API_BASE_URL", "TELEGRAM_API_BASE_FILE_URL")


def _setup_stdio() -> None:
    """UTF-8 в stdout/stderr: cron-лог на Windows пишется в cp1251 и падает."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def drop_inherited_notify_env() -> None:
    """Убрать чужой канал уведомлений, унаследованный от процесса-родителя.

    cron-джоба живёт в процессе шлюза, а шлюз держит в своём окружении `.env` того профиля,
    чей Telegram-адаптер поднялся первым. Дальше `STEP_PIPELINE_TELEGRAM_BOT_TOKEN` в джобе
    оказывается токеном чужого бота, и уведомления проекта уходят от чужого имени (проверено
    16.11.2026: отчёт проекта `pipeline` приходил ботом профиля `writer`). Свой канал
    берётся только из `.env` своего профиля — `load_profile_env`.
    """
    for key in [k for k in os.environ
                if k.startswith(NOTIFY_ENV_PREFIX) or k in NOTIFY_SHARED_ENVS]:
        del os.environ[key]


def load_profile_env() -> None:
    """Дочитать `.env` профиля Hermes и `PIPELINE_ENV`, если задан.

    Зачем: cron шлюза запускает скрипт с `HERMES_HOME` профиля, но переменные из
    `profiles/<профиль>/.env` в окружение не попадают (проверено 01.11.2026: переменные
    уведомлений есть в CLI и отсутствуют в джобе). Без этого уведомления движка молчат.
    Унаследованный чужой канал уведомлений снимается до чтения файлов, поэтому токен бота
    берётся из `.env` своего профиля, а не из окружения шлюза. Прочие существующие значения
    не перетираются.
    """
    drop_inherited_notify_env()
    candidates = []
    explicit = os.environ.get("PIPELINE_ENV")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path(__file__).resolve().parents[1] / ".env")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)


def find_gate() -> Path | None:
    candidates = []
    repo = os.environ.get("PIPELINE_REPO")
    if repo:
        candidates.append(Path(repo) / ".pipeline" / "gate.py")
    candidates.append(Path.cwd() / ".pipeline" / "gate.py")
    candidates.append(Path(__file__).resolve().parent / "gate.py")
    candidates.append(Path(__file__).resolve().parents[1] / ".pipeline" / "gate.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def main() -> int:
    _setup_stdio()
    load_profile_env()
    gate = find_gate()
    if gate is None:
        print("pipeline_tick: gate.py не найден (задай PIPELINE_REPO или --workdir джобы)",
              file=sys.stderr)
        return 1
    sys.argv = [str(gate), "--run"]
    runpy.run_path(str(gate), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
