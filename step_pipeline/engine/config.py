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
    PIPELINE_KANBAN_DB   — путь к базе борды (для проверок без гейтвея).
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
    "skills": {
        "step": ["pipeline-next-step"],
        "review": ["pipeline-reviewer"],
        "prompt": ["pipeline-prompter", "agent-prompt-authoring"],
    },
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
    return cfg
