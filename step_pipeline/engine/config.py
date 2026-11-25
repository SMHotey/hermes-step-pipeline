# -*- coding: utf-8 -*-
"""Конфигурация конвейера: один файл `pipeline.json` в корне проекта.

Смысл: движок (`gate.py`, `registry.py`, `next_step.py`) не знает ни одного
проектного имени. Борда, профили, скиллы, каталоги планов и ревью, тексты
маркеров журнала — всё берётся отсюда. Другой проект = другой `pipeline.json`,
правки кода не нужны.

Переменные среды (перекрывают файл, нужны тестам и песочнице):
    PIPELINE_CONFIG      — путь к файлу конфигурации;
    PIPELINE_REPO        — корень проекта (иначе — каталог над `.pipeline/`);
    PIPELINE_BOARD       — борда канбана;
    PIPELINE_STATE_DIR   — каталог состояния/стоп-флагов;
    PIPELINE_PLANS       — список ведомых планов через запятую (`*` — все);
    PIPELINE_KANBAN_DB   — путь к базе борды (для проверок без гейтвея);
    STEP_PIPELINE_DEBUG  — `1` включает вывод подавленных исключений в stderr.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "pipeline.json"


class ConfigError(RuntimeError):
    """Конфигурация отсутствует или непригодна — конвейер стартовать не должен."""


DEFAULTS: dict = {
    "project": "project",
    "board": "default",
    "profiles": {"driver": "default", "prompter": "prompter", "reviewer": "reviewer"},
    # Маршрут «предметка → профиль»: `routing` в pipeline.json (план 5, S7). Пустой словарь —
    # маршрута нет: предметка ведёт к драйверу, как до шага. Ключи владельца не трогаем —
    # `profiles` описывает профили по ролям, а здесь предметки шага.
    "routing": {},
    "skills": {
        "step": ["pipeline-next-step"],
        "review": ["pipeline-reviewer"],
        "prompt": ["pipeline-prompter", "agent-prompt-authoring"],
    },
    # Каталог ревью хранит и реестр находок плана (`plan-N-findings.md`), и реестр находок
    # постановки (`prompt-N-ШАГ-findings.md`): путь реестра выводится из отчёта ревью (план)
    # или из префикса и номера шага (постановка), отдельного ключа нет — второй источник
    # пути разошёлся бы.
    "paths": {
        "plans": "docs/plans",
        "reviews": "docs/reviews",
        "prompts": "docs/prompts",
        "state": ".pipeline/state",
    },
    "markers": {"journal": "журнал", "steps": "шаг"},
    # Идентификатор шага: буквы + номер + необязательная буква + необязательный «-номер».
    # Годится для S3a, T10, E15a, S25-8; список идентификаторов не нужен.
    "step_id_regex": r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)",
    "default_plan": None,
    # Какие планы ведёт `--run` без `--plan`. `["*"]` — все с распознанным журналом.
    # Пустой список запрещает автозапуск: черновик плана не начнёт исполняться сам.
    "driven_plans": ["*"],
    # Шаг без промта-постановки не начинается: страж закажет постановку роли prompter.
    "require_prompt": True,
    # Предзаказ постановки N+1, пока шаг N в работе, и гейт закрытия без неё (план 2, S6).
    "prefetch_prompt": True,
    # Ревью постановки против текущего HEAD: auto — только устаревшая, always — каждая,
    # never — поведение как до шага (правило 3 плана 2 S6). Вердикт ревью постановки с шага
    # 12-S2 считается по реестру находок (`prompt-N-ШАГ-findings.md`), а строка отчёта
    # остаётся разбором: отчёт APPROVE при открытой находке шаг не открывает.
    "prompt_review": "auto",
    # Риск-гейт высокорискового шага (8-S2B). `spec` (умолчание) — до кода шаг получает
    # спецификацию в постановке (блок «Спека»): спеки нет — страж заказывает круг правок
    # постановки, а не держит шаг. `hold` — прежнее поведение целиком: шаг ждёт ручного
    # `--unhold` владельца. Неизвестное значение — `ConfigError`, а не молчаливое `spec`.
    "risk_gate": "spec",
    # Сколько автоматических кругов правок делает страж (шаг и план), прежде чем
    # остановиться. 1 — стоп на первом же отказе после круга; 2 — ещё один круг до решения
    # владельца. Останавливает лимит, а не первый отказ: у ревью плана каждый
    # `REQUEST_CHANGES` даёт карточку правок, пока круги не исчерпаны (0.25.2).
    "max_fix_rounds": 2,
    # Предел кругов правок для шага с `Риск: high` (план 12, S3). 6 — цена ошибки в опасном
    # шаге — разбор плана, а не ещё один круг; ступеней выше `high` нет, потому что `_risk_of`
    # знает три значения (`low`/`normal`/`high`) и любую нераспознанную пометку сводит к `high`.
    # `high < база` разрешён и означает «для опасного шага строже» — выключатель без второго
    # ключа, как `max_stale_rebuilds: 0`.
    "max_fix_rounds_high": 6,
    # Лимит автоматических пересборок протухшей постановки (план 12, S1). Считается по
    # карточкам `:promptfix-stale:` и НЕ входит в `max_fix_rounds`: сдвиг HEAD и правка по
    # ревью — разные события, иначе пересбор по свежести съедает бюджет правок (6-S8A
    # израсходовал два круга из шести на сдвиг HEAD). 0 — пересборок нет, расхождение
    # версий снова останавливает конвейер.
    "max_stale_rebuilds": 2,
    # Лимит расширения плана правкой (план 7, S4). Правка по ревью закрывает находки,
    # а не объясняет план ревьюеру: площадь проверки иначе растёт быстрее, чем закрываются
    # находки. Допуск — меньшее из `percent` процентов версии плана (без журнала) и
    # `chars_per_finding` символов на каждую находку, ради которой правку заказывали.
    # Ответственность за рост с 19.11.2026 не блокирующая: замер пишется в состояние плана
    # и в карточку правок, но шаги не останавливает (`enforce: false` — умолчание). Причина
    # решения владельца: ревью существует, чтобы менять план, а рост объёма — следствие
    # правки; стоп срабатывал на первом же круге, где закрытых находок ещё ноль.
    "max_plan_growth": {"percent": 10, "chars_per_finding": 400, "enforce": False},
    # Прогоны шага (план 9, S2): страж исполняет пункты «Прогон» до ревью, и красный прогон
    # отсекает шаг карточкой правок, а не кругом ревью. `timeout` — предел одного прогона,
    # `budget` — суммарный бюджет шага: исчерпанный бюджет — инцидентный стоп, который
    # снимает не `--resume`, а новый коммит (sha меняется) или решение владельца.
    # В `pipeline.json` проекта ключ не пишется вовсе: умолчание живёт в коде.
    "step_runs": {"timeout": 120, "budget": 600},
    # Залипание инструмента (план 17, S2): в журнале метрики отмечается попытка, где один
    # инструмент шёл `stall_seconds` секунд в `stall_repeats` вызовах подряд (или упал по
    # таймауту). Отметка — комментарий в карточку и строка «ПРАВИЛО МЕТОДА» следующей
    # карточке шага; шаг при этом не останавливается. Мусор в полях — `ConfigError`:
    # молчаливая подмена умолчанием скрыла бы, что порог владельца не действует.
    "tools": {"stall_seconds": 300, "stall_repeats": 2},
    # Защита от цикла (план 17, S4): повтор убитой по лимиту попытки разрешён только
    # с эскалацией — лимит новой карточки в `factor` раз больше убитого; второй подряд
    # при тех же условиях — стоп владельцу. `enabled` — выключатель владельца,
    # `escalations` — потолок подъёмов на шаг (0 — не поднимать вовсе, только стоп).
    # Умолчание в коде — «включено»; в `pipeline.json` проекта ключ может быть выключен
    # (замер 17-S4: 6 из 7 убийств по лимиту — первые для своих шагов).
    "runtime_guard": {"enabled": True, "escalations": 2, "factor": 1.5},
    "runtimes": {"step": "3h", "review": "150m", "prompt": "45m"},
    # Смотритель и страж: один порог «висит» на двоих (план 10, S2). Возраст карточки —
    # от последнего события карточки, а не от `waiting_since` стража; `running` со свежим
    # heartbeat висящей не считается. `card_deadline_hours = 0` выключает проверку.
    # Мусор в значении (`abc`, `-1`, `null`) — умолчание и WARNING в stderr, а не стоп
    # конвейера: владелец, поправивший ключ руками, не должен останавливать тик проекта.
    "watchdog": {"heartbeat_fresh_minutes": 30, "card_deadline_hours": 12},
    # Пауза по расписанию: окна локального времени ОС, в которые тик не работает.
    # Выключено — поведение проектов не меняется. Интервалов не больше двух: владелец
    # просил два (ночь и обед), а длинный список настраивать руками никто не будет.
    # Окно может идти через полночь (`23:00`–`07:00`); конец окна — не включается.
    "quiet_hours": {"enabled": False, "windows": []},
    "kanban": {"tenant": "plan"},
    "git": {"commit": True, "push": True},
    # Уведомления владельцу. Выключено — сообщения идут в stdout (видит `hermes cron runs`).
    "notify": {
        "enabled": False,
        "token_env": "PIPELINE_TG_TOKEN",
        "chat_env": "PIPELINE_TG_CHAT",
        "prefix": "",
        # Сторож простоя: дольше этого ожидание — одно напоминание на причину (не чаще раза
        # в час). digest_at — время суточной сводки по локальным часам.
        "watchdog_hours": 3,
        "digest_at": "09:00",
        # Вопрос шага без ответа (план 8, S5): через столько секунд принимается названная
        # автором рекомендация. 0 — автовыбора нет, вопрос ждёт человека.
        "answer_timeout": 3600,
        # Приём команд владельца из чата (getUpdates в тике). Включён там, где включены
        # уведомления; `wait` — секунды длинного опроса в отдельной джобе.
        "commands": {"enabled": True, "wait": 0},
        # Диалог со свободным текстом (S4): потолки держат ответ в 240 секундах и не дают
        # агенту превратить вопрос в прогон тестов. toolsets — список через запятую для
        # `hermes chat -t`; terminal/execute_code/pipeline_run в нём быть не должно.
        "dialog": {
            "enabled": True,
            "max_turns": 8,
            "run_budget": 200,
            "timeout": 240,
            "session_max_messages": 40,
            "session_max_tokens": 120000,
            "tokens_per_char": 0.34,
            "toolsets": "web,file,skills,todo,memory,session_search",
        },
    },
}


#: Файлы движка — единственный источник списка (план 7, S7). По нему мастер установки
#: копирует движок в проект, по нему же считаются хеш, маркер `ENGINE.sha256` и дрейф
#: копии. Список живёт в движке, а не в `setup_flow`: движок копируется в проект и обязан
#: работать без плагина, а вспомогательный `.py` в `.pipeline/` (скрипт проекта) не должен
#: считаться ни файлом движка, ни дрейфом (прецедент раскатки 07.11.2026 — три таких файла
#: в DoorSystem роняли doctor и маркер). Порядок внутри кортежа произвольный: и копирование,
#: и хеш идут по сортированным именам.
ENGINE_FILES = ("__init__.py", "analyze.py", "approval.py", "check_task_prompt.py",
                "config.py", "gate.py", "keeper_heal.py", "keeper_scan.py", "next_step.py",
                "plan_cycle.py",
                "plan_lint.py", "registry.py", "scout_report.py", "state_io.py",
                "step_card.py", "step_metrics.py", "tick.py")

# Режимы ревью постановки: иное значение в pipeline.json — ошибка конфигурации, а не
# молчаливое «auto»: владелец должен узнать, что его настройка не подействовала.
PROMPT_REVIEW_MODES = ("auto", "always", "never")

# Режимы риск-гейта высокорискового шага (8-S2B): `spec` — спецификация в постановке,
# `hold` — прежний ручной допуск. Мусор — `ConfigError`, а не молчаливое умолчание:
# владелец должен узнать, что его настройка не подействовала.
RISK_GATE_MODES = ("spec", "hold")

# Предметки, которые движок знает по имени (план 5, S7). Их не надо объявлять в конфиге:
# маршрут лишь говорит, какому профилю предметка соответствует. Предметки без записи в
# `routing` ведут к драйверу — поведение проектов до шага сохраняется.
SUBJECT_AREAS = ("frontend", "backend", "devops", "designer", "security")

# Ключ маршрута — то же ограничение, что у токена владельца в заголовке шага.
ROUTING_KEY_RE = re.compile(r"[a-z][a-z0-9_-]{1,30}")

# Время окна паузы: 24-часовой формат `ЧЧ:ММ`, только цифры с ведущим нулём — «7:00» и
# «25:00» в pipeline.json означали бы окно не там, где его ждёт владелец.
PAUSE_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Предел интервалов паузы: два. Больше окон — это уже расписание смен, а не «поставить
# паузу на ночь и на обед»; предел держится и в панели, и в движке одним числом.
PAUSE_WINDOWS_MAX = 2


def _deep_merge(base: dict, override: dict) -> dict:
    """Слить конфигурацию: словари рекурсивно, списки и скаляры — заменой."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _env_list(name: str) -> tuple[str, ...] | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or None


def _fix_rounds(value: object) -> int:
    """Лимит автоматических кругов правок: целое ≥ 1 (по умолчанию 2).

    Мусор в `pipeline.json` — ошибка конфигурации, а не молчаливое умолчание: иначе
    владелец будет думать, что правило действует, а движок остановится, как раньше.
    """
    if value is None:
        return 2
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"max_fix_rounds должен быть целым числом, получено: {value!r}")
    try:
        rounds = int(value)
    except ValueError as exc:
        raise ConfigError(f"max_fix_rounds должен быть целым числом, получено: {value!r}") from exc
    if rounds < 1:
        raise ConfigError(f"max_fix_rounds должен быть ≥ 1, получено: {rounds}")
    return rounds


def _fix_rounds_high(value: object) -> int:
    """Предел кругов правок для шага `Риск: high`: целое ≥ 1 (по умолчанию 6).

    Форма — копия `_stale_rebuilds`: мусор в `pipeline.json` — ошибка конфигурации, а не
    молчаливое умолчание, иначе владелец думает, что ступень действует, а страж работает по
    базе. Перекрёстной проверки «не меньше базы» нет: `high < база` — законный выключатель
    «для опасного шага строже».
    """
    if value is None:
        return 6
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"max_fix_rounds_high должен быть целым числом, получено: {value!r}")
    try:
        rounds = int(value)
    except ValueError as exc:
        raise ConfigError(f"max_fix_rounds_high должен быть целым числом, "
                          f"получено: {value!r}") from exc
    if rounds < 1:
        raise ConfigError(f"max_fix_rounds_high должен быть ≥ 1, получено: {rounds}")
    return rounds


def _stale_rebuilds(value: object) -> int:
    """Лимит пересборок протухшей постановки: целое ≥ 0 (по умолчанию 2).

    Форма та же, что у `_fix_rounds`, с одним осознанным отличием: **0 разрешён** и означает
    «пересборок нет, расхождение версий снова останавливает конвейер» — выключатель без
    второго ключа, как `prompt_review: never` у ревью постановок. Мусор в `pipeline.json` —
    ошибка конфигурации, а не молчаливое умолчание: иначе владелец будет думать, что
    лимит действует, а страж остановится на первом же расхождении.
    """
    if value is None:
        return 2
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"max_stale_rebuilds должен быть целым числом, получено: {value!r}")
    try:
        rounds = int(value)
    except ValueError as exc:
        raise ConfigError(f"max_stale_rebuilds должен быть целым числом, "
                          f"получено: {value!r}") from exc
    if rounds < 0:
        raise ConfigError(f"max_stale_rebuilds должен быть ≥ 0, получено: {rounds}")
    return rounds


def _plan_growth(value: object) -> dict:
    """Лимит расширения плана правкой: `percent` и `chars_per_finding` (план 7, S4).

    Умолчание — 10 % объёма версии плана и 400 символов на каждую закрытую находку. Мусор
    в `pipeline.json` — ошибка конфигурации, а не молчаливое умолчание (тот же принцип, что
    у `_fix_rounds`): иначе владелец думает, что правило действует, а страж работает по
    прежнему лимиту. Отсутствующий ключ или отсутствующее поле внутри него — умолчание:
    так проект может задать только процент.
    """
    default = DEFAULTS["max_plan_growth"]
    if value is None:
        return dict(default)
    if not isinstance(value, dict):
        raise ConfigError(f"max_plan_growth должен быть объектом JSON, получено: {value!r}")
    out = dict(default)
    if value.get("enforce") is not None:
        raw = value["enforce"]
        if not isinstance(raw, bool):
            raise ConfigError(f"max_plan_growth.enforce должен быть true или false, "
                              f"получено: {raw!r}")
        out["enforce"] = raw
    for key in ("percent", "chars_per_finding"):
        if value.get(key) is None:
            continue
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ConfigError(f"max_plan_growth.{key} должен быть числом, получено: {raw!r}")
        try:
            number = float(raw)
        except ValueError as exc:
            raise ConfigError(f"max_plan_growth.{key} должен быть числом, "
                              f"получено: {raw!r}") from exc
        if number < 0:
            raise ConfigError(f"max_plan_growth.{key} должен быть ≥ 0, получено: {raw!r}")
        if key == "percent" and number != int(number):
            raise ConfigError(f"max_plan_growth.percent должен быть целым числом процентов, "
                              f"получено: {raw!r}")
        out[key] = int(number)
    return out


def _step_runs(value: object) -> dict:
    """Прогоны шага (план 9, S2): `timeout` одного прогона и `budget` шага в секундах.

    Умолчание — 120 с на прогон и 600 с на шаг: страж исполняет пункты «Прогон» сам,
    до ревью, и это поведение по умолчанию — план обязан чем-то доказываться, а не
    пересказом исполнителя. Мусор в `pipeline.json` — ошибка конфигурации, а не
    молчаливое умолчание (тот же принцип, что у `_fix_rounds`): иначе владелец думает,
    что поднял бюджет, а страж работает по прежнему лимиту. Отсутствующий ключ —
    умолчание целиком; поля целые не меньше 1, неизвестные поля — тоже ошибка.
    """
    default = DEFAULTS["step_runs"]
    if value is None:
        return dict(default)
    if not isinstance(value, dict):
        raise ConfigError(f"step_runs должен быть объектом JSON, получено: {value!r}")
    out = dict(default)
    unknown = sorted(str(key) for key in value if key not in default)
    if unknown:
        raise ConfigError(f"step_runs: неизвестные поля {unknown}, "
                          f"допустимы {sorted(default)}, получено: {value!r}")
    for key in ("timeout", "budget"):
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ConfigError(f"step_runs.{key} должен быть целым числом секунд, "
                              f"получено: {raw!r}")
        if raw < 1:
            raise ConfigError(f"step_runs.{key} должен быть ≥ 1, получено: {raw!r}")
        out[key] = raw
    return out


def _tools(value: object) -> dict:
    """Порог залипания инструмента (план 17, S2): `stall_seconds` и `stall_repeats`.

    Форма — как у `_step_runs`, и это осознанно (Д3 постановки 17-S2): у порога нет
    значения-выключателя (`0` секунд означало бы «залипло всё»), а тихая подмена умолчанием
    скрыла бы от владельца, что его настройка не действует. Мусор — `ConfigError` с именем
    поля, а не `WARNING`: порог читает и карточка, и комментарий стража.
    """
    default = DEFAULTS["tools"]
    if value is None:
        return dict(default)
    if not isinstance(value, dict):
        raise ConfigError(f"tools должен быть объектом JSON, получено: {value!r}")
    out = dict(default)
    unknown = sorted(str(key) for key in value if key not in default)
    if unknown:
        raise ConfigError(f"tools: неизвестные поля {unknown}, "
                          f"допустимы {sorted(default)}, получено: {value!r}")
    for key in ("stall_seconds", "stall_repeats"):
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ConfigError(f"tools.{key} должен быть целым числом, получено: {raw!r}")
        if raw < 1:
            raise ConfigError(f"tools.{key} должен быть ≥ 1, получено: {raw!r}")
        out[key] = raw
    return out


def _runtime_guard(value: object) -> dict:
    """Правило «повтор только с изменением условий» (план 17, S4): выключатель, потолок, множитель.

    Форма — как у `_tools` (Д6 постановки 17-S4): неизвестное поле и мусор в значении дают
    `ConfigError` с именем поля, а не тихое умолчание. Выключатель здесь — решение владельца
    (в этом проекте он выключен по замеру), и подмена его умолчанием скрыла бы, что правило
    снова работает; множитель ≤ 1.0 означал бы «повтор с тем же лимитом», то есть ровно ту
    петлю, ради которой правило и заведено.
    """
    default = DEFAULTS["runtime_guard"]
    if value is None:
        return dict(default)
    if not isinstance(value, dict):
        raise ConfigError(f"runtime_guard должен быть объектом JSON, получено: {value!r}")
    out = dict(default)
    unknown = sorted(str(key) for key in value if key not in default)
    if unknown:
        raise ConfigError(f"runtime_guard: неизвестные поля {unknown}, "
                          f"допустимы {sorted(default)}, получено: {value!r}")
    enabled = value.get("enabled")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ConfigError(f"runtime_guard.enabled должен быть true или false, "
                              f"получено: {enabled!r}")
        out["enabled"] = enabled
    escalations = value.get("escalations")
    if escalations is not None:
        if isinstance(escalations, bool) or not isinstance(escalations, int):
            raise ConfigError(f"runtime_guard.escalations должен быть целым числом, "
                              f"получено: {escalations!r}")
        if escalations < 0:
            raise ConfigError(f"runtime_guard.escalations должен быть ≥ 0, "
                              f"получено: {escalations!r}")
        out["escalations"] = escalations
    factor = value.get("factor")
    if factor is not None:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise ConfigError(f"runtime_guard.factor должен быть числом, получено: {factor!r}")
        if float(factor) <= 1.0:
            raise ConfigError(f"runtime_guard.factor должен быть больше 1.0, "
                              f"получено: {factor!r}")
        out["factor"] = float(factor)
    return out


def _watchdog_number(value: object, default: int) -> int | None:
    """Целое ≥ 0 из настройки: строка из цифр и целое `1.0` годятся, `bool` — нет.

    `None` — значение не принято (вызывающий берёт умолчание и пишет WARNING).
    `0` — законное значение: проверка выключена, а не «мусор».
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    return None


def _watchdog(value: object) -> dict:
    """Пороги смотрителя и стража (план 10, S2): терпимый разбор, а не `ConfigError`.

    Форма осознанно отличается от `_fix_rounds` и `_quiet_hours`: там мусор — ошибка
    конфигурации, потому что значение гейт. Здесь предел не гейт — тик не должен
    вставать из-за опечатки владельца, поэтому мусор даёт умолчание и одну строку
    WARNING в stderr (его видит cron-лог; stdout читают панель и cron — туда нельзя).
    """
    default = DEFAULTS["watchdog"]
    out = dict(default)
    if value is None:
        return out
    if not isinstance(value, dict):
        print(f"WARNING: watchdog={value!r} — ожидается объект, беру умолчания"
              f" {default}", file=sys.stderr)
        return out
    for key, raw in value.items():
        if key not in default:
            print(f"WARNING: watchdog.{key}={raw!r} — неизвестный ключ, беру"
                  f" {sorted(default)}", file=sys.stderr)
            continue
        number = _watchdog_number(raw, default[key])
        if number is None:
            print(f"WARNING: watchdog.{key}={raw!r} — беру {default[key]}", file=sys.stderr)
            continue
        out[key] = number
    return out


def _pause_time(value: object, where: str) -> int:
    """Время окна паузы в минутах суток; всё, кроме `ЧЧ:ММ`, — ошибка конфигурации.

    Молчаливого умолчания здесь нет намеренно (тот же принцип, что у `_fix_rounds`):
    пауза выключает конвейер, и окно, тихо прочитанное не так, означало бы простой
    или работу не в те часы, которых ждёт владелец.
    """
    text = str(value if value is not None else "").strip()
    match = PAUSE_TIME_RE.match(text)
    if not match:
        raise ConfigError(f"{where}: ожидается время ЧЧ:ММ (например, 23:00), "
                          f"получено: {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def _quiet_hours(value: object) -> dict:
    """Окна паузы по расписанию: `enabled` и до двух интервалов `ЧЧ:ММ`–`ЧЧ:ММ`.

    Пустой список окон законен и при `enabled: true`: пауза включена, но расписания нет —
    тики идут круглосуточно (так настройка, начатая в панели, не выключает конвейер, пока
    интервалы не заданы). Пересечение окон не ошибка: два окна — это «когда не работать»,
    и лишнее покрытие ни на что не влияет.
    """
    default = DEFAULTS["quiet_hours"]
    if value is None:
        return {"enabled": bool(default["enabled"]), "windows": []}
    if not isinstance(value, dict):
        raise ConfigError(f"quiet_hours должен быть объектом JSON, получено: {value!r}")
    enabled = value.get("enabled", default["enabled"])
    if not isinstance(enabled, bool):
        raise ConfigError(f"quiet_hours.enabled должен быть true или false, "
                          f"получено: {enabled!r}")
    raw_windows = value.get("windows")
    if raw_windows is None:
        raw_windows = []
    if not isinstance(raw_windows, list):
        raise ConfigError(f"quiet_hours.windows должен быть списком интервалов, "
                          f"получено: {raw_windows!r}")
    if len(raw_windows) > PAUSE_WINDOWS_MAX:
        raise ConfigError(f"quiet_hours.windows: интервалов не больше {PAUSE_WINDOWS_MAX}, "
                          f"получено: {len(raw_windows)}")
    windows: list[dict] = []
    for index, item in enumerate(raw_windows, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"quiet_hours.windows[{index}]: нужен объект "
                              f'{{"from": "23:00", "to": "07:00"}}, получено: {item!r}')
        start = _pause_time(item.get("from"), f"quiet_hours.windows[{index}].from")
        end = _pause_time(item.get("to"), f"quiet_hours.windows[{index}].to")
        if start == end:
            raise ConfigError(f"quiet_hours.windows[{index}]: начало и конец совпадают — "
                              f"интервал не задан")
        window = {"from": _hhmm(start), "to": _hhmm(end)}
        if window in windows:
            raise ConfigError(f"quiet_hours.windows[{index}]: интервал "
                              f"{window['from']}–{window['to']} уже задан")
        windows.append(window)
    return {"enabled": enabled, "windows": windows}


def _hhmm(minutes: int) -> str:
    """Минуты суток обратно в `ЧЧ:ММ` — форма, в которой окно живёт в конфиге."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _in_window(minute: int, start: int, end: int) -> bool:
    """Минута суток внутри интервала `[start, end)`; конец раньше начала — через полночь."""
    if end > start:
        return start <= minute < end
    return minute >= start or minute < end


def pause_state(cfg: Config, now: float | None = None) -> dict:
    """Пауза по расписанию на момент *now* (epoch; локальное время ОС, как у сводки).

    Одна форма ответа и для панели, и для CLI, и для тика: `enabled`, `windows`, `paused`,
    `window` (активное окно или None), `until` (конец окна) и `reason` — готовая строка,
    которую можно показать владельцу. Часы читаются ровно здесь: решение о паузе —
    функция состояния и времени, без модели и без истории.
    """
    data = cfg.quiet_hours if isinstance(cfg.quiet_hours, dict) else {}
    windows = list(data.get("windows") or [])
    enabled = bool(data.get("enabled"))
    moment = time.time() if now is None else float(now)
    local = time.localtime(moment)
    minute = local.tm_hour * 60 + local.tm_min
    state = {"enabled": enabled, "windows": windows, "paused": False, "window": None,
             "until": "", "at": time.strftime("%H:%M", local), "reason": ""}
    if not enabled:
        state["reason"] = "пауза по расписанию выключена"
        return state
    if not windows:
        state["reason"] = "пауза по расписанию включена, интервалы не заданы"
        return state
    for window in windows:
        start = _pause_time(window.get("from"), "quiet_hours.windows.from")
        end = _pause_time(window.get("to"), "quiet_hours.windows.to")
        if _in_window(minute, start, end):
            state.update({
                "paused": True, "window": window, "until": window["to"],
                "reason": (f"пауза по расписанию: окно {window['from']}–{window['to']} "
                           f"действует (сейчас {state['at']}), тики до {window['to']}"),
            })
            return state
    state["reason"] = ("пауза по расписанию: сейчас не окно ("
                       + ", ".join(f"{w['from']}–{w['to']}" for w in windows)
                       + "), тики идут")
    return state


@dataclass(frozen=True)
class Config:
    """Разобранная конфигурация одного проекта."""

    repo: Path
    project: str
    board: str
    driver: str
    prompter: str
    reviewer: str
    step_skills: tuple[str, ...]
    review_skills: tuple[str, ...]
    prompt_skills: tuple[str, ...]
    plans_dir: Path
    reviews_dir: Path
    prompts_dir: Path
    state_dir: Path
    journal_marker: str
    steps_marker: str
    step_regex: re.Pattern
    default_plan: str | None
    driven: tuple[str, ...]
    require_prompt: bool
    prefetch_prompt: bool
    prompt_review: str
    risk_gate: str
    max_fix_rounds: int
    max_fix_rounds_high: int
    max_stale_rebuilds: int
    max_plan_growth: dict
    step_runs: dict
    tools: dict
    runtime_guard: dict
    watchdog: dict
    quiet_hours: dict
    step_runtime: str
    review_runtime: str
    prompt_runtime: str
    tenant: str
    git_commit: bool
    git_push: bool
    notify: dict
    kanban_db: Path

    def rel(self, path: Path) -> str:
        """Путь относительно корня проекта (`docs/plans/01-x.md`); вне репо — имя."""
        try:
            return Path(path).resolve().relative_to(self.repo.resolve()).as_posix()
        except ValueError:
            return Path(path).name

    def abs(self, rel: str) -> Path:
        return (self.repo / rel).resolve()

    @property
    def state(self) -> dict:
        """Сырой словарь конфигурации (для ролей-скиллов и отчётов)."""
        return self._raw

    _raw: dict = None  # type: ignore[assignment]


def routing_map(cfg: Config) -> dict[str, str]:
    """Маршрут «предметка → профиль» из слитого конфига; мусор — `ConfigError`.

    Ключ — предметка шага (ограничение токена владельца), значение — непустая строка
    (имя профиля Hermes). Молчаливого отбрасывания нет: владелец должен узнать, что его
    маршрут не подействовал (тот же принцип, что у `_fix_rounds`).
    """
    raw = (cfg._raw or {}).get("routing")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"routing должен быть объектом JSON, получено: {raw!r}")
    out: dict[str, str] = {}
    for key, target in raw.items():
        name = str(key).strip()
        if not ROUTING_KEY_RE.fullmatch(name):
            raise ConfigError(
                f"routing: ключ {name!r} не подходит под шаблон [a-z][a-z0-9_-]{{1,30}}")
        if not isinstance(target, str) or not target.strip():
            raise ConfigError(
                f"routing: значение ключа {name!r} должно быть непустой строкой, "
                f"получено: {target!r}")
        out[name] = target.strip()
    return out


def known_profiles(cfg: Config) -> set[str]:
    """Допустимые профили: значения `profiles` плюс значения маршрута.

    Идёт в текст ошибки «неизвестный исполнитель»: владельцу нужен список того, что
    действительно можно писать в «Исполнитель:».
    """
    raw = cfg._raw or {}
    names: set[str] = set()
    profiles = raw.get("profiles")
    if isinstance(profiles, dict):
        for value in profiles.values():
            name = str(value).strip()
            if name:
                names.add(name)
    names.update(routing_map(cfg).values())
    return names


def setup_stdio() -> None:
    """UTF-8 в stdout/stderr.

    Без этого на Windows-консолях (cp1251) любой русский вывод движка падает
    с UnicodeEncodeError, и страж выглядит «сломанным». Вызывается из main().
    """
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:  # подменённый поток (pytest, IDE)
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


DEBUG_ENV = "STEP_PIPELINE_DEBUG"


def debug_enabled() -> bool:
    """Отладочный вывод включён: `STEP_PIPELINE_DEBUG=1`.

    Зачем отдельная ручка: часть `except` в движке глотает исключение намеренно
    (необязательный шаг не должен ронять тик), но молчать о нём совсем нельзя —
    иначе «почему не сработало» не выяснить. По умолчанию вывод молчит: stdout
    движка читают cron-лог и панель, лишняя строка там — шум.
    """
    value = (os.environ.get(DEBUG_ENV) or "").strip().lower()
    return value not in ("", "0", "false", "нет", "no")


def debug_note(where: str, exc: object) -> None:
    """Сказать в stderr, что исключение поймано и подавлено (только в отладке)."""
    if debug_enabled():
        print(f"[{where}] подавлено: {exc}", file=sys.stderr)


def _kanban_db(board: str) -> Path:
    override = os.environ.get("PIPELINE_KANBAN_DB")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "hermes" / "kanban" / "boards" / board / "kanban.db"


def load(repo: Path | str | None = None) -> Config:
    """Прочитать `pipeline.json` проекта. Нет файла — ошибка, а не молчаливые умолчания."""
    root = Path(repo or os.environ.get("PIPELINE_REPO")
                or Path(__file__).resolve().parents[1]).resolve()
    path = Path(os.environ.get("PIPELINE_CONFIG") or root / CONFIG_NAME)
    if not path.is_file():
        if os.environ.get("PIPELINE_ASSUME_DEFAULTS") == "1":
            # режим предпросмотра: читаем проект, в котором конвейер ещё не установлен
            data = json.loads(json.dumps(DEFAULTS))
        else:
            raise ConfigError(
                f"нет файла конфигурации конвейера: {path}. "
                f"Скопируй шаблон (templates/pipeline.json.example) или запусти install.py")
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"pipeline.json не разбирается как JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("pipeline.json должен быть объектом JSON")
    raw = _deep_merge(DEFAULTS, data)

    profiles = raw.get("profiles") or {}
    skills = raw.get("skills") or {}
    paths = raw.get("paths") or {}
    runtimes = raw.get("runtimes") or {}
    notify = dict(raw.get("notify") or {})

    board = os.environ.get("PIPELINE_BOARD") or str(raw.get("board") or "").strip()
    if not board:
        raise ConfigError("в pipeline.json не задана борда (`board`)")

    driven = _env_list("PIPELINE_PLANS")
    if driven is None:
        driven = tuple(str(item).strip() for item in raw.get("driven_plans") or [] if str(item).strip())
    if not driven:
        driven = ()

    state_dir = Path(os.environ.get("PIPELINE_STATE_DIR") or paths.get("state") or ".pipeline/state")
    if not state_dir.is_absolute():
        state_dir = root / state_dir

    regex = str(raw.get("step_id_regex") or DEFAULTS["step_id_regex"])
    pattern = regex if regex.startswith("^") else "^" + regex
    try:
        step_regex = re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"step_id_regex не компилируется: {pattern}: {exc}") from exc
    if "id" not in step_regex.groupindex:
        raise ConfigError(f"в step_id_regex нет именованной группы id: {pattern}")

    prompt_review = str(raw.get("prompt_review") or "auto").strip().lower()
    if prompt_review not in PROMPT_REVIEW_MODES:
        raise ConfigError(f"prompt_review: неизвестный режим {prompt_review!r}; допустимо: "
                          f"auto | always | never")

    risk_gate = str(raw.get("risk_gate") or DEFAULTS["risk_gate"]).strip().lower()
    if risk_gate not in RISK_GATE_MODES:
        raise ConfigError(f"risk_gate: неизвестный режим {risk_gate!r}; допустимо: "
                          f"spec | hold")

    cfg = Config(
        repo=root,
        project=str(raw.get("project") or root.name),
        board=board,
        driver=str(profiles.get("driver") or "default"),
        prompter=str(profiles.get("prompter") or "prompter"),
        reviewer=str(profiles.get("reviewer") or "reviewer"),
        step_skills=tuple(skills.get("step") or ()),
        review_skills=tuple(skills.get("review") or ()),
        prompt_skills=tuple(skills.get("prompt") or ()),
        plans_dir=root / str(paths.get("plans") or "docs/plans"),
        reviews_dir=root / str(paths.get("reviews") or "docs/reviews"),
        prompts_dir=root / str(paths.get("prompts") or "docs/prompts"),
        state_dir=state_dir,
        journal_marker=str((raw.get("markers") or {}).get("journal") or "журнал").lower(),
        steps_marker=str((raw.get("markers") or {}).get("steps") or "шаг").lower(),
        step_regex=step_regex,
        default_plan=(str(raw["default_plan"]) if raw.get("default_plan") else None),
        driven=driven,
        require_prompt=bool(raw.get("require_prompt", True)),
        prefetch_prompt=bool(raw.get("prefetch_prompt", True)),
        prompt_review=prompt_review,
        risk_gate=risk_gate,
        max_fix_rounds=_fix_rounds(raw.get("max_fix_rounds")),
        max_fix_rounds_high=_fix_rounds_high(raw.get("max_fix_rounds_high")),
        max_stale_rebuilds=_stale_rebuilds(raw.get("max_stale_rebuilds")),
        max_plan_growth=_plan_growth(raw.get("max_plan_growth")),
        step_runs=_step_runs(raw.get("step_runs")),
        tools=_tools(raw.get("tools")),
        runtime_guard=_runtime_guard(raw.get("runtime_guard")),
        watchdog=_watchdog(raw.get("watchdog")),
        quiet_hours=_quiet_hours(raw.get("quiet_hours")),
        step_runtime=str(runtimes.get("step") or "3h"),
        review_runtime=str(runtimes.get("review") or "150m"),
        prompt_runtime=str(runtimes.get("prompt") or "45m"),
        tenant=str((raw.get("kanban") or {}).get("tenant") or "plan"),
        git_commit=bool((raw.get("git") or {}).get("commit", True)),
        git_push=bool((raw.get("git") or {}).get("push", True)),
        notify=notify,
        kanban_db=_kanban_db(board),
    )
    object.__setattr__(cfg, "_raw", raw)
    # Маршрут предметок проверяется на загрузке: мусор в `routing` роняет конвейер с
    # именем ключа, а не превращается в молчаливое «маршрута нет» (S7).
    routing_map(cfg)
    return cfg
