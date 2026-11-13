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
    # Каталог ревью хранит и реестр находок плана (`plan-N-findings.md`): путь реестра
    # выводится из отчёта ревью, отдельного ключа нет — второй источник пути разошёлся бы.
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
    # never — поведение как до шага (правило 3 плана 2 S6).
    "prompt_review": "auto",
    # Сколько автоматических кругов правок делает страж (шаг и план), прежде чем
    # остановиться. 1 — стоп на первом же отказе после круга; 2 — ещё один круг до решения
    # владельца. Останавливает лимит, а не первый отказ: у ревью плана каждый
    # `REQUEST_CHANGES` даёт карточку правок, пока круги не исчерпаны (0.25.2).
    "max_fix_rounds": 2,
    # Лимит расширения плана правкой (план 7, S4). Правка по ревью закрывает находки,
    # а не объясняет план ревьюеру: площадь проверки иначе растёт быстрее, чем закрываются
    # находки. Допуск — большее из `percent` процентов версии плана (без журнала) и
    # `chars_per_finding` символов на каждую закрытую находку. Превышение не режется
    # молча: карточка правок получает owner-стоп с цифрой, решает владелец.
    "max_plan_growth": {"percent": 10, "chars_per_finding": 400},
    "runtimes": {"step": "3h", "review": "150m", "prompt": "45m"},
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


# Режимы ревью постановки: иное значение в pipeline.json — ошибка конфигурации, а не
# молчаливое «auto»: владелец должен узнать, что его настройка не подействовала.
PROMPT_REVIEW_MODES = ("auto", "always", "never")

# Предметки, которые движок знает по имени (план 5, S7). Их не надо объявлять в конфиге:
# маршрут лишь говорит, какому профилю предметка соответствует. Предметки без записи в
# `routing` ведут к драйверу — поведение проектов до шага сохраняется.
SUBJECT_AREAS = ("frontend", "backend", "devops", "designer", "security")

# Ключ маршрута — то же ограничение, что у токена владельца в заголовке шага.
ROUTING_KEY_RE = re.compile(r"[a-z][a-z0-9_-]{1,30}")


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
    max_fix_rounds: int
    max_plan_growth: dict
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
        max_fix_rounds=_fix_rounds(raw.get("max_fix_rounds")),
        max_plan_growth=_plan_growth(raw.get("max_plan_growth")),
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
