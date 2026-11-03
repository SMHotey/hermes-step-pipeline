# -*- coding: utf-8 -*-
"""Мастер установки конвейера в проект: один вопрос за раз, всё остальное само.

Что делает: копирует движок и роли, пишет `pipeline.json`, создаёт профили-роли,
заводит cron-джобу стража, спрашивает токен Telegram-бота и проверяет его живым
запросом, ставит проект в доверенные у всех профилей-ролей (драйвер, промптер,
ревьюер). Повторный запуск идемпотентен.
"""
from __future__ import annotations

import getpass
import json
import hashlib
import os
import re
import shutil
import subprocess
import sqlite3
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from . import plan_lint
    from . import project as proj
    from . import telegram
except ImportError:  # pragma: no cover - запуск файлом при отладке
    import plan_lint  # type: ignore
    import project as proj  # type: ignore
    import telegram  # type: ignore

ENGINE_FILES = ("__init__.py", "config.py", "registry.py", "next_step.py", "gate.py",
                "step_card.py", "check_task_prompt.py", "tick.py", "state_io.py")
#: Маркер целостности: хеш содержимого движка, а не только номер версии.
#: Без него копия в проекте может молча разойтись с плагином при равном VERSION
#: (прецедент 02.11.2026: 527 строк разницы в gate.py при 0.2.0 у обоих).
ENGINE_MARKER = "ENGINE.sha256"
#: Куда `setup --upgrade` складывает прежнюю копию роли перед перезаписью.
ROLES_BACKUP = ".pipeline/roles-backup"


def engine_digest(base) -> str:
    """sha256 по всем .py движка: имя + байты, стабильный порядок."""
    import hashlib as _hashlib
    from pathlib import Path as _Path

    digest = _hashlib.sha256()
    for path in sorted(_Path(base).glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_engine_marker(project) -> str:
    """Записать маркер целостности движка в `<проект>/.pipeline/ENGINE.sha256`."""
    from pathlib import Path as _Path

    base = _Path(project) / ".pipeline"
    digest = engine_digest(base)
    (base / ENGINE_MARKER).write_text("sha256:" + digest, encoding="utf-8")
    return digest
SKILL_CATEGORIES = {
    "pipeline-next-step": "software-development",
    "pipeline-prompter": "software-development",
    "pipeline-reviewer": "software-development",
    "pipeline-orchestrator": "software-development",
    "pipeline-plan-authoring": "software-development",
    "agent-prompt-authoring": "autonomous-ai-agents",
}
TOKEN_ENV = "STEP_PIPELINE_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "STEP_PIPELINE_TELEGRAM_CHAT_ID"
OWNER_ENV = "STEP_PIPELINE_TELEGRAM_OWNER_ID"
#: Источник текста тик-скрипта — файл движка, а не рукописная копия. Копия разошлась
#: с движком 03.11.2026 (sha256 c11840c3… против b7d98376…) и при отсутствии `gate.py`
#: возвращала 0 молча: под cron джоба показывала `ok` при мёртвом конвейере. Прецедент
#: двух разных копий движка при равном VERSION — 02.11.2026 (1-S17).
TICK_SOURCE = proj.ENGINE_SRC / "tick.py"
TICK_SCRIPT = TICK_SOURCE.read_text(encoding="utf-8")


def install_tick_script(driver: str) -> Path:
    """Положить тик-скрипт движка в `profiles/<профиль>/scripts/` — байтами.

    Байты, а не текст: `write_text` на Windows переводит строки в CRLF, и установленный
    скрипт перестаёт быть побайтово равным файлу движка (инвариант шага — sha256).
    """
    scripts = proj.profile_dir(driver) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    target = scripts / "pipeline_tick.py"
    target.write_bytes(TICK_SOURCE.read_bytes())
    return target

COMMANDS_SCRIPT = '''# -*- coding: utf-8 -*-
"""Приём команд конвейера из Telegram: один getUpdates, ответ в тот же чат.

Заводится отдельной cron-джобой раз в минуту: страж идёт раз в 10 минут, а команде
владельца ждать десять минут незачем. Секунды длинного опроса — PIPELINE_COMMANDS_WAIT.
"""
import os
import runpy
import sys
from pathlib import Path


def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def load_profile_env():
    paths = []
    explicit = os.environ.get("PIPELINE_ENV")
    if explicit:
        paths.append(Path(explicit))
    paths.append(Path(__file__).resolve().parents[1] / ".env")
    for path in paths:
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
            value = value.strip().strip('"').strip("'")
            if key.strip() and value:
                os.environ.setdefault(key.strip(), value)


def find_gate():
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


def main():
    _setup_stdio()
    load_profile_env()
    gate = find_gate()
    if gate is None:
        return 0
    wait = os.environ.get("PIPELINE_COMMANDS_WAIT", "0")
    sys.argv = [str(gate), "--commands", "--commands-wait", wait]
    runpy.run_path(str(gate), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


class Log:
    """Печать шагов мастера: ровно одна строка на действие."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def step(self, text: str) -> None:
        self.lines.append(text)
        print(text, flush=True)

    def ok(self, text: str) -> None:
        self.step(f"  ✓ {text}")

    def warn(self, text: str) -> None:
        self.step(f"  ! {text}")


def _ask(prompt: str, default: str = "", *, yes: bool, secret: bool = False) -> str:
    """Спросить значение. В режиме `--yes` вопрос не задаётся."""
    if yes or not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return raw.strip() or default


def _ask_bool(prompt: str, default: bool, *, yes: bool) -> bool:
    if yes or not sys.stdin.isatty():
        return default
    try:
        raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    return raw in ("y", "yes", "д", "да", "1")


def env_path(profile: str) -> Path:
    return proj.profile_dir(profile) / ".env"


def write_env(profile: str, values: dict[str, str]) -> Path:
    """Записать/обновить переменные в .env профиля (права 0600)."""
    path = env_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for key, value in values.items():
        replaced = False
        for index, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[index] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def copy_engine(project: Path, force: bool) -> list[str]:
    """Движок в `<проект>/.pipeline/` (существующие файлы обновляются)."""
    dest_dir = project / ".pipeline"
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ENGINE_FILES:
        src = proj.ENGINE_SRC / name
        if not src.is_file():
            raise FileNotFoundError(f"в плагине нет движка: {src}")
        dest = dest_dir / name
        if dest.is_file() and not force and dest.read_text(encoding="utf-8", errors="replace") == \
                src.read_text(encoding="utf-8", errors="replace"):
            continue
        shutil.copy2(src, dest)
        copied.append(f".pipeline/{name}")
    return copied


def plugin_version() -> str:
    """Версия плагина из plugin.yaml (без зависимости от PyYAML)."""
    path = proj.PLUGIN_DIR / "plugin.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "0"
    match = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", text)
    return match.group(1) if match else "0"


def write_engine_version(project: Path) -> str:
    """Маркер `.pipeline/VERSION`: какую версию движка несёт проект."""
    version = plugin_version()
    dest = project / ".pipeline" / "VERSION"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(version + "\n", encoding="utf-8")
    return version


def engine_version(project: Path) -> str:
    """Версия движка, записанная в проекте ('' если маркера нет)."""
    path = project / ".pipeline" / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def hermes_home() -> Path:
    """Корень Hermes: HERMES_HOME, иначе LOCALAPPDATA/hermes, иначе ~/.hermes."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "hermes").is_dir():
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _plugin_roots() -> list[Path]:
    home = hermes_home()
    roots = [home / "plugins"]
    roots += sorted((home / "profiles").glob("*/plugins"))
    return [root for root in roots if root.is_dir()]


def quarantine_stale_plugins(log: Log) -> list[str]:
    """Убрать из plugins/ копии нашего плагина с прежним именем.

    Hermes грузит плагин по имени из манифеста, поэтому лежащая рядом старая копия
    (`step-pipeline`, `step_pipeline.bak-*`) перекрывает свежую: панель отдаёт старую
    страницу, а `hermes pipeline` может не найтись вовсе.
    """
    home = hermes_home()
    moved: list[str] = []
    for root in _plugin_roots():
        scope = "global" if root.parent == home else root.parent.name
        for sub in sorted(root.iterdir()):
            manifest = sub / "plugin.yaml"
            if not sub.is_dir() or not manifest.is_file():
                continue
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = re.search(r"(?m)^name:\s*([^\s]+)", text)
            name = found.group(1) if found else ""
            if name.replace("-", "_") != "step_pipeline":
                continue
            if sub.name == "step_pipeline" and name == "step_pipeline":
                continue
            dest = home / "plugin-backups" / scope / sub.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(dest.name + "-2")
            try:
                shutil.move(str(sub), str(dest))
            except OSError as exc:
                log.warn(f"старую копию плагина не удалось убрать: {sub.name} ({exc})")
                continue
            moved.append(f"{scope}/{sub.name}")
            log.warn(f"старая копия плагина убрана из plugins/: {sub.name} → plugin-backups/{scope}/")
    return moved


def ensure_plugin_enabled(profile: str, log: Log) -> str:
    """Включить текущий id плагина в профиле и проверить, что команда `pipeline` есть."""
    hermes = shutil.which("hermes") or "hermes"
    try:
        res = subprocess.run([hermes, "-p", profile, "plugins", "enable", "step_pipeline"],
                             capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warn(f"плагин не включён автоматически: {exc}")
        return f"включение плагина не удалось ({exc})"
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    if res.returncode != 0:
        log.warn(f"`plugins enable step_pipeline` вернул {res.returncode}: {out}")
        return "плагин не включён"
    check = subprocess.run([hermes, "-p", profile, "pipeline", "--help"],
                           capture_output=True, text=True, timeout=120)
    if check.returncode != 0:
        log.warn("команда `hermes pipeline` не отвечает после включения плагина — "
                 "перезапусти сессию профиля")
        return "плагин включён, команда проверится в новой сессии"
    log.ok(f"плагин step_pipeline включён в профиле {profile}")
    return "плагин включён и команда отвечает"


def refresh_tick_script(project: Path, log: Log) -> str:
    """Обновить установленный тик-скрипт профиля драйвера, если cron заводили.

    Признак заведённой джобы — метка `.pipeline/cron.json` (её пишет мастер). У проекта
    с `--no-cron` файл в профиле не создаётся: писать владельцу в профиль там, где джобы
    нет, — сюрприз, а не обновление.
    """
    if not (project / ".pipeline" / "cron.json").is_file():
        log.ok("тик-скрипт профиля не обновлялся: проект без cron")
        return "тик-скрипт профиля не обновлялся (проект без cron)"
    driver = roles_from_config(proj.read_config(project))[0]
    install_tick_script(driver)
    log.ok(f"тик-скрипт профиля {driver}: обновлён")
    return f"тик-скрипт профиля {driver} обновлён"


def upgrade(project: Path, log: Log, *, trust: bool = True) -> int:
    """Обновить движок и роли в уже поставленном проекте, не трогая конфиг.

    Роли синхронизируются с плагином (прежняя версия уходит в `.pipeline/roles-backup/`):
    иначе правки `step_pipeline/roles/` до поставленного проекта не доезжают вовсе.
    Доверие проекту оформляется тем же ролям, что и при установке; `trust=False`
    (флаг `--no-trust`) пропускает его и пишет предупреждение.
    """
    if not (project / ".pipeline").is_dir():
        print(f"ошибка: конвейер не установлен в {project} — нужен `hermes pipeline setup`",
              file=sys.stderr)
        return 2
    was = engine_version(project) or "?"
    changed = copy_engine(project, force=False)
    roles = copy_roles(project, refresh=True)
    now = write_engine_version(project)
    write_engine_marker(project)
    for item in changed:
        log.ok(f"обновлён {item}")
    for item in roles.added:
        log.ok(f"добавлена роль {item}")
    for item in roles.refreshed:
        log.ok(f"обновлена роль {item} (прежняя версия: {roles.backups[item]})")
    if not changed and not roles.added and not roles.refreshed:
        log.ok(f"движок и роли уже актуальны ({now})")
    quarantine_stale_plugins(log)
    driver = roles_from_config(proj.read_config(project))[0]
    plugin_state = ensure_plugin_enabled(driver, log)
    tick = refresh_tick_script(project, log)
    if trust:
        failed = trust_roles(roles_from_config(proj.read_config(project)), project, log)
        tail = ("доверие ролям оформлено" if not failed
                else f"доверие не оформлено у: {', '.join(failed)}")
    else:
        log.warn("доверие проекту не оформлялось (--no-trust)")
        tail = "доверие ролям пропущено (--no-trust)"
    log.step(f"── Движок: {was} → {now}, роли: {len(roles.refreshed)} обновлено. "
             f"Конфиг, telegram и cron-джоба не тронуты; {tick}; {tail} ──")
    return 0


@dataclass
class RolesSync:
    """Итог синхронизации ролей проекта: добавленные, перезаписанные и где их бэкап."""

    added: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    backups: dict[str, str] = field(default_factory=dict)


def _role_files(root: Path) -> dict[str, bytes]:
    """Файлы роли без байткода: относительный путь → содержимое."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def copy_roles(project: Path, *, refresh: bool = False) -> RolesSync:
    """Роли в `.hermes/skills/<категория>/<имя>`.

    Без `refresh` копируются только недостающие: повторный `setup` не переписывает
    поставленное. С `refresh` (так делает `setup --upgrade`) роль приводится к версии
    плагина, а прежняя копия уезжает в `.pipeline/roles-backup/<роль>-<время>/` —
    правка проекта не пропадает молча. Причина: улучшения ролей живут в
    `step_pipeline/roles/`, `.hermes/` в проекте — вендоренная копия; до 0.6.0 она
    не обновлялась никогда, и правки роли не доезжали до уже поставленных проектов.
    """
    result = RolesSync()
    for name, category in SKILL_CATEGORIES.items():
        src = proj.ROLES_SRC / name
        if not src.is_dir():
            continue
        rel = f".hermes/skills/{category}/{name}"
        dest = project / ".hermes" / "skills" / category / name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
            result.added.append(rel)
            continue
        if not refresh or _role_files(dest) == _role_files(src):
            continue
        backup = project / ROLES_BACKUP / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dest, backup, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        result.refreshed.append(rel)
        result.backups[rel] = backup.relative_to(project).as_posix()
    return result


def ensure_dirs(project: Path) -> list[str]:
    made: list[str] = []
    for rel in ("docs/plans", "docs/reviews", "docs/prompts", "docs/templates"):
        path = project / rel
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            made.append(rel + "/")
    template = project / "docs" / "templates" / "plan-template.md"
    if not template.is_file() and (proj.TEMPLATES_SRC / "plan-template.md").is_file():
        shutil.copy2(proj.TEMPLATES_SRC / "plan-template.md", template)
        made.append("docs/templates/plan-template.md")
    return made


def ensure_gitignore(project: Path) -> bool:
    path = project / ".gitignore"
    entries = (".pipeline/state/", ".pipeline/.env", ROLES_BACKUP + "/")
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    missing = [e for e in entries if e not in text.splitlines()]
    if not missing:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    text += "# состояние и секреты конвейера\n" + "\n".join(missing) + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def build_config(project: Path, board: str, driver: str, prompter: str, reviewer: str,
                 existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """`pipeline.json`: шаблон плагина + выбранные значения, чужие правки сохраняются."""
    base: dict[str, Any] = {}
    try:
        base = json.loads(proj.DEFAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        base = {}
    cfg = {**base, **(existing or {})}
    cfg["project"] = (existing or {}).get("project") or project.name
    cfg["board"] = board
    cfg["profiles"] = {"driver": driver, "prompter": prompter, "reviewer": reviewer}
    notify = dict(cfg.get("notify") or {})
    notify.update({"token_env": TOKEN_ENV, "chat_env": CHAT_ENV,
               "owner_env": OWNER_ENV, "dialog": {"enabled": True}})
    cfg["notify"] = notify
    return cfg


def ensure_profiles(driver: str, prompter: str, reviewer: str, log: Log) -> None:
    """Профили ролей: клон драйвера, максимум рассуждений, один воркер за раз."""
    listing = proj.run_hermes("profile", "list")
    have = listing.stdout or ""
    for role, description, effort in (
        (prompter, "Постановщик: контекст шага плана → промт-постановка", "max"),
        (reviewer, "Ревьюер: независимая проверка шага по плану и коду", "max"),
    ):
        if f" {role} " in f" {have} ":
            log.ok(f"профиль {role} уже есть")
        else:
            res = proj.run_hermes("profile", "create", role, "--clone-from", driver,
                                  "--description", description)
            if res.returncode == 0:
                log.ok(f"профиль {role} создан")
            else:
                log.warn(f"профиль {role}: {(res.stderr or res.stdout or '').strip()[:200]}")
        for key, value in (("agent.max_turns", "300"),
                           ("agent.reasoning_effort", effort),
                           ("kanban.max_in_progress", "1"),
                           ("kanban.max_in_progress_per_profile", "1"),
                           ("kanban.failure_limit", "2")):
            proj.run_hermes("-p", role, "config", "set", key, value)
    for key, value in (("agent.max_turns", "300"),
                       ("kanban.max_in_progress_per_profile", "1"),
                       ("kanban.failure_limit", "2"),
                       ("kanban.default_assignee", driver)):
        proj.run_hermes("-p", driver, "config", "set", key, value)
    log.ok(f"настройки конвейера применены к {driver}/{prompter}/{reviewer}")


#: Роли конвейера по умолчанию — те же, что у мастера установки.
DEFAULT_ROLES = ("default", "prompter", "reviewer")

#: Смоук установки: одна эхо-карточка на роль, один дедлайн на все три (S16).
SMOKE_TIMEOUT = 300
SMOKE_INTERVAL = 5.0
SMOKE_RUNTIME = "10m"
SMOKE_TITLE = "Смоук установки: роль {role} стартует"
SMOKE_SKILL_KEYS = {"driver": "step", "prompter": "prompt", "reviewer": "review"}
#: Навыки ролей по умолчанию — копия `DEFAULTS["skills"]` движка
#: (`step_pipeline/engine/config.py:37-41`): импортировать движок из плагина нельзя.
SMOKE_DEFAULT_SKILLS = {
    "step": ("pipeline-next-step",),
    "prompt": ("pipeline-prompter", "agent-prompt-authoring"),
    "review": ("pipeline-reviewer",),
}
#: Терминальные статусы карточки — как у стража (`engine/gate.py:TERMINAL`).
TERMINAL = {"done", "blocked", "crashed", "timed_out", "gave_up"}


def roles_from_config(cfg: dict[str, Any] | None) -> list[str]:
    """Три роли конвейера из `pipeline.json`: драйвер → промптер → ревьюер.

    Пустой или битый `profiles` даёт умолчания, дубликаты схлопываются.
    """
    profiles = (cfg or {}).get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    roles = [profiles.get(key) or fallback
             for key, fallback in zip(("driver", "prompter", "reviewer"), DEFAULT_ROLES)]
    return list(dict.fromkeys(str(role) for role in roles if role))


def trust_roles(roles, project: Path, log: Log) -> list[str]:
    """Оформить доверие проекту у профилей-ролей; вернуть те, где не вышло.

    Ошибка одной роли не ломает мастер: `skills trust` пишет в профиль, а не в проект.
    """
    failed: list[str] = []
    for role in dict.fromkeys(roles):
        trust = proj.run_hermes("-p", role, "skills", "trust", str(project))
        if trust.returncode == 0:
            log.ok(f"проект в доверенных у {role}")
        else:
            failed.append(role)
            log.warn(f"skills trust ({role}): "
                     f"{(trust.stderr or trust.stdout or '').strip()[:200]}")
    return failed


def _trust_value(raw: str) -> str:
    """Значение из YAML-списка: без обрамляющих кавычек и хвостового комментария."""
    value = raw.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def _parse_trusted_dirs(text: str) -> list[str]:
    """Разбор `trusted_project_dirs` из config.yaml без PyYAML.

    Поддерживает обе живые формы: блок-список (`- путь` с отступом больше, чем у
    ключа) и строчный список (`[a, b]`, `[]`). Чужие ключи не читаются, битый
    файл даёт пустой список, а не исключение.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)trusted_project_dirs\s*:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        rest = match.group(2).strip()
        if rest.startswith("["):
            inner = rest[1:rest.rfind("]")] if "]" in rest else rest[1:]
            return [value for value in (_trust_value(part) for part in inner.split(","))
                    if value]
        entries: list[str] = []
        for follow in lines[index + 1:]:
            if not follow.strip():
                continue
            item = re.match(r"^(\s*)-\s*(.*)$", follow)
            if not item or len(item.group(1)) <= indent:
                break
            value = _trust_value(item.group(2))
            if value:
                entries.append(value)
        return entries
    return []


def trusted_project_dirs(profile: str) -> list[str]:
    """Пути, которым доверяет профиль (`skills.trusted_project_dirs` профиля)."""
    path = proj.profile_dir(profile) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_trusted_dirs(text)


def is_project_trusted(profile: str, project: Path) -> bool:
    """Доверяет ли профиль проекту — сравнением путей, как у Hermes."""
    try:
        target = Path(project).expanduser().resolve()
    except OSError:
        return False
    for entry in trusted_project_dirs(profile):
        try:
            if Path(entry).expanduser().resolve() == target:
                return True
        except OSError:
            continue
    return False


def profile_provider(profile: str) -> str:
    """Провайдер модели профиля: `model.provider` из его `config.yaml`.

    Разбор без PyYAML — как у `_parse_trusted_dirs`: блок `model:` и в нём `provider:`
    с отступом больше, чем у ключа верхнего уровня. Нет файла или ключа — пустая строка.
    """
    path = proj.profile_dir(profile) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)model\s*:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        rest = match.group(2).strip()
        if rest.startswith("{"):
            inline = re.search(r"provider\s*:\s*([^,}]+)", rest)
            return _trust_value(inline.group(1)) if inline else ""
        for follow in lines[index + 1:]:
            if not follow.strip() or follow.lstrip().startswith("#"):
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            item = re.match(r"^\s*provider\s*:\s*(.*)$", follow)
            if item:
                return _trust_value(item.group(1))
        return ""
    return ""


def auth_entries(profile: str) -> dict[str, list[dict]]:
    """`credential_pool` профиля: словарь «провайдер → записи» из его `auth.json`.

    Нет файла, битый JSON, не тот тип — пустой словарь, без исключения.
    """
    path = proj.profile_dir(profile) / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    pool = data.get("credential_pool") if isinstance(data, dict) else None
    if not isinstance(pool, dict):
        return {}
    entries: dict[str, list[dict]] = {}
    for name, items in pool.items():
        if isinstance(items, list):
            entries[str(name)] = [item for item in items if isinstance(item, dict)]
    return entries


def _is_saved_entry(entry: dict) -> bool:
    """Запись сохранена в профиле, а не подтягивается из переменной окружения.

    Правило Hermes (`agent/credential_pool.py`): источники `env:*` «пере-гидратируются
    из окружения на каждой загрузке» и под демоном шлюза выпадают из ротации — именно
    это дало инцидент 01.11.2026 (`No usable credentials found for provider`).
    """
    source = str(entry.get("source") or "")
    if source.lower().startswith("env:"):
        return False
    return bool(entry.get("access_token") or entry.get("api_key"))


def has_saved_credential(profile: str) -> tuple[bool, str]:
    """Есть ли у профиля сохранённый ключ; второй элемент — провайдер из конфига."""
    provider = profile_provider(profile)
    pool = auth_entries(profile)
    candidates = {provider: pool.get(provider, [])} if provider else pool
    for items in candidates.values():
        if any(_is_saved_entry(item) for item in items):
            return True, provider
    return False, provider


def ensure_board(board: str, log: Log) -> bool:
    """Борда канбана существует: создать (`boards create`) и инициализировать БД."""
    listing = proj.run_hermes("kanban", "boards", "list")
    if not re.search(rf"(?<![\w-]){re.escape(board)}(?![\w-])", listing.stdout or ""):
        res = proj.run_hermes("kanban", "boards", "create", board)
        if res.returncode != 0:
            log.warn(f"борда {board}: {(res.stderr or res.stdout or '').strip()[:200]}")
            return False
        log.ok(f"борда {board} создана")
    res = proj.run_hermes("kanban", "--board", board, "init")
    if res.returncode == 0:
        log.ok(f"борда {board} готова (kanban init)")
        return True
    log.warn(f"борда {board}: {(res.stderr or res.stdout or '').strip()[:200]}")
    return False


def _write_cron_marker(project: Path, driver: str, name: str) -> None:
    """Метка для `doctor`: какую джобу завёл мастер (cron list не показывает workdir)."""
    marker = project / ".pipeline" / "cron.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"driver": driver, "name": name, "workdir": str(project)},
                                 ensure_ascii=False), encoding="utf-8")


def ensure_cron(driver: str, project: Path, log: Log, *, skip: bool) -> None:
    """Cron-джоба стража: no-agent, каждые 10 минут, доставка local.

    Тик-скрипт профиля пишется при каждом запуске мастера, в том числе когда джоба уже
    есть: иначе джоба, заведённая со старой копией, остаётся с ней навсегда (1-S17).
    """
    if skip:
        log.warn("cron не заводил (--no-cron)")
        return
    install_tick_script(driver)
    name = "План: страж шага (конвейер)"
    listing = proj.run_hermes("-p", driver, "cron", "list")
    if name in (listing.stdout or ""):
        log.ok("cron-джоба стража уже есть (тик-скрипт обновлён)")
        _write_cron_marker(project, driver, name)
        return
    res = proj.run_hermes("-p", driver, "cron", "create", "*/10 * * * *",
                          "--name", name, "--no-agent",
                          "--script", "pipeline_tick.py",
                          "--deliver", "local",
                          "--workdir", str(project))
    if res.returncode == 0:
        log.ok("cron-джоба стража создана (каждые 10 минут)")
        _write_cron_marker(project, driver, name)
    else:
        log.warn(f"cron: {(res.stderr or res.stdout or '').strip()[:300]}")
        log.warn("завести вручную: " + cron_command(driver, project))


def cron_command(driver: str, project: Path) -> str:
    return (f'hermes -p {driver} cron create "*/10 * * * *" --name "План: страж шага (конвейер)" '
            f'--no-agent --script pipeline_tick.py --deliver local --workdir "{project}"')


COMMANDS_JOB = "План: бот команды (конвейер)"


def ensure_commands_cron(driver: str, project: Path, log: Log, *, skip: bool,
                         enabled: bool) -> None:
    """Cron-джоба приёма команд бота: no-agent, раз в минуту.

    Отдельно от стража: страж идёт раз в 10 минут, а `/status` и `/stop` столько ждать
    нельзя. Модель не нужна — это один `getUpdates`.
    """
    if skip:
        return
    if not enabled:
        log.warn("приём команд не заводил: уведомления выключены (--no-telegram)")
        return
    if COMMANDS_JOB in (proj.run_hermes("-p", driver, "cron", "list").stdout or ""):
        log.ok("cron-джоба команд бота уже есть")
        return
    scripts = proj.profile_dir(driver) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "pipeline_commands.py").write_text(COMMANDS_SCRIPT, encoding="utf-8")
    res = proj.run_hermes("-p", driver, "cron", "create", "* * * * *",
                          "--name", COMMANDS_JOB, "--no-agent",
                          "--script", "pipeline_commands.py",
                          "--deliver", "local",
                          "--workdir", str(project))
    if res.returncode == 0:
        log.ok("cron-джоба команд бота создана (каждую минуту)")
    else:
        log.warn(f"cron команд: {(res.stderr or res.stdout or '').strip()[:300]}")
        log.warn("завести вручную: " + commands_cron_command(driver, project))


def commands_cron_command(driver: str, project: Path) -> str:
    return (f'hermes -p {driver} cron create "* * * * *" --name "{COMMANDS_JOB}" '
            f'--no-agent --script pipeline_commands.py --deliver local --workdir "{project}"')


def smoke_targets(cfg: dict[str, Any] | None,
                  roles: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Пары «роль → навыки» для смоука в порядке ролей (драйвер, промптер, ревьюер).

    Навыки берутся из `skills` конфига проекта — ровно те, с которыми страж заводит
    карточку шага. Нет всей секции `skills` — умолчания движка; ключа нет или он пуст —
    карточка без `--skill` (это не ошибка).
    """
    skills_cfg = (cfg or {}).get("skills")
    has_section = isinstance(skills_cfg, dict)
    if not has_section:
        skills_cfg = {}
    targets: list[tuple[str, tuple[str, ...]]] = []
    for kind, role in zip(("driver", "prompter", "reviewer"), roles):
        key = SMOKE_SKILL_KEYS[kind]
        raw = (skills_cfg or {}).get(key)
        values = tuple(str(item) for item in raw if item) if isinstance(raw, (list, tuple)) else ()
        if not values and not has_section:
            values = tuple(SMOKE_DEFAULT_SKILLS.get(key, ()))
        targets.append((str(role), values))
    return targets


def smoke_key(role: str, stamp: str) -> str:
    """Ключ идемпотентности смоук-карточки: свой на роль и запуск мастера."""
    return f"smoke:{role}:{stamp}"


def smoke_body(role: str) -> str:
    """Тело карточки-эха: проверка старта воркера роли, больше ничего."""
    return (f"Это проверка установки конвейера для роли `{role}`: воркер роли должен\n"
            "запуститься сам и закрыть карточку.\n\n"
            "Ничего не читать и не править. Единственное действие — закрыть карточку\n"
            "через `kanban_complete` со сводкой `смоук OK`. Если закрыть нельзя —\n"
            "`kanban_block` с причиной в одной строке.\n")


def create_smoke_card(project, cfg: dict[str, Any] | None, role: str,
                      skills: tuple[str, ...], stamp: str, log: Log | None = None) -> str | None:
    """Завести смоук-карточку роли — тем же вызовом, что и карточку шага у стража.

    `--workspace dir:ПРОЕКТ` обязателен: навыки ролей лежат в проекте, на `scratch`
    воркер упадёт `Unknown skill(s)` и смоук даст ложный провал.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(smoke_body(role))
        body_path = fh.name
    board = str((cfg or {}).get("board") or "default")
    tenant = str(((cfg or {}).get("kanban") or {}).get("tenant") or "plan")
    cmd = ("kanban", "--board", board, "create", SMOKE_TITLE.format(role=role),
           "--body-file", body_path, "--assignee", role, "--tenant", tenant,
           "--workspace", f"dir:{Path(project).as_posix()}",
           "--idempotency-key", smoke_key(role, stamp), "--max-runtime", SMOKE_RUNTIME)
    for skill in skills:
        cmd += ("--skill", skill)
    try:
        res = proj.run_hermes(*cmd)
    finally:
        try:
            Path(body_path).unlink()
        except OSError:
            pass
    out = (res.stdout or "") + (res.stderr or "")
    found = re.search(r"t_[0-9a-f]{6,}", out)
    if res.returncode != 0 and not found:
        message = f"смоук ({role}): карточка не создана — {out.strip()[:200]}"
        if log is not None:
            log.warn(message)
        else:  # pragma: no cover - вызов без лога только из отладки
            print(message, file=sys.stderr)
        return None
    return found.group(0) if found else None


def smoke_card_state(project, key: str) -> dict:
    """Состояние карточки по ключу идемпотентности — только чтение борды.

    Причина провала: `last_failure_error`, затем `result`, затем payload последнего
    события. Таблицы `task_events` может не быть (урезанные схемы) — тогда пусто.
    """
    db = proj.board_db(project)
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        try:
            row = con.execute(
                "SELECT id, status, COALESCE(result,''), COALESCE(last_failure_error,'')"
                " FROM tasks WHERE idempotency_key = ? AND status <> 'archived'"
                " ORDER BY created_at DESC", (key,)).fetchone()
        except sqlite3.Error:
            return {}
        if not row:
            return {}
        state = {"id": row[0], "status": row[1] or "", "result": row[2], "error": row[3]}
        payload = ""
        try:
            event = con.execute("SELECT payload FROM task_events WHERE task_id = ?"
                                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                (row[0],)).fetchone()
            payload = str(event[0] or "") if event else ""
        except sqlite3.Error:
            payload = ""
        state["event"] = payload
        return state
    finally:
        con.close()


def wait_smoke_card(project, key: str, timeout: float = SMOKE_TIMEOUT,
                    interval: float = SMOKE_INTERVAL, sleep=time.sleep,
                    clock=time.monotonic) -> dict:
    """Дождаться терминального статуса карточки; флаг `timed_out` в ответе.

    `sleep` и `clock` — параметры, чтобы тест проверял дедлайн без настоящего ожидания.
    """
    deadline = clock() + max(0.0, float(timeout))
    while True:
        state = smoke_card_state(project, key)
        if str(state.get("status") or "") in TERMINAL:
            state["timed_out"] = False
            return state
        now = clock()
        if now >= deadline:
            state["timed_out"] = True
            return state
        sleep(min(float(interval) or SMOKE_INTERVAL, deadline - now))


def smoke_cause(state: dict) -> str:
    """Первая непустая причина провала из полей карточки (без `worker_output`)."""
    for field in ("error", "result", "event"):
        value = str(state.get(field) or "").strip()
        if value:
            return value[:300]
    return ""


def smoke_repair(cause: str, role: str, project, card_id: str) -> str:
    """Готовая команда починки по тексту причины (таблица D8 постановки)."""
    text = (cause or "").lower()
    if "unknown skill" in text:
        return f'hermes -p {role} skills trust "{project}" && hermes pipeline setup --upgrade'
    if "no usable credentials found" in text or "missing_api_key" in text:
        provider = profile_provider(role)
        if provider:
            return f"hermes -p {role} auth add {provider} --type api-key"
        return f"hermes -p {role} auth list"
    if "gateway" in text or not text:
        return f"hermes gateway status, затем hermes kanban show {card_id}"
    return f"hermes kanban show {card_id}"


def run_smoke(project, cfg: dict[str, Any] | None, roles: list[str], log: Log,
              timeout: float = SMOKE_TIMEOUT) -> tuple[int, int]:
    """Пробный запуск ролей: карточка-эхо на роль и ожидание закрытия.

    Один дедлайн на все три карточки: они заводятся подряд, ожидание общее.
    Возвращает (сколько закрылось, сколько всего).
    """
    log.step("── Смоук ролей ──")
    stamp = time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"
    cards: list[tuple[str, str, str]] = []
    for role, skills in smoke_targets(cfg, roles):
        key = smoke_key(role, stamp)
        cards.append((role, create_smoke_card(project, cfg, role, skills, stamp, log) or "?", key))
    if not cards:
        log.warn("смоук: ролей нет, проверять нечего")
        return 0, 0
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout))
    ok_count = 0
    for role, card_id, key in cards:
        if card_id == "?":
            # Карточку не завели — ждать нечего, иначе одна ошибка съест весь дедлайн.
            state = {"status": "не создана", "result": "", "error": "",
                     "event": "", "timed_out": False}
        else:
            state = wait_smoke_card(project, key, deadline - time.monotonic())
        elapsed = int(round(time.monotonic() - started))
        status = str(state.get("status") or "") or "нет карточки"
        if status == "done":
            ok_count += 1
            log.ok(f"{role}: карточка {card_id} — done")
            continue
        cause = smoke_cause(state) or ("дедлайн истёк" if state.get("timed_out") else "причина не названа")
        log.warn(f"{role}: карточка {card_id} — {status} за {elapsed} с: {cause}")
        log.step(f"  починка: {smoke_repair(cause, role, project, card_id)}")
    return ok_count, len(cards)


def _mask(token: str) -> str:
    return token[:8] + "…" + token[-4:] if len(token) > 14 else "***"


def setup_telegram(project: Path, driver: str, args, log: Log) -> str | None:
    """Токен бота, чат и запись в .env профиля. Возвращает chat id, если настроено."""
    if getattr(args, "no_telegram", False):
        log.warn("уведомления в Telegram отключены (--no-telegram)")
        return None
    if not getattr(args, "telegram_token", "") and not os.environ.get(TOKEN_ENV):
        log.step("── Telegram (необязательно) ──")
        if not _ask_bool("  Подключить бота для уведомлений и диалога?", True,
                         yes=getattr(args, "yes", False)):
            log.warn("работаем без бота: уведомлений и команд не будет")
            return None
    token = (getattr(args, "telegram_token", "") or os.environ.get(TOKEN_ENV)
             or os.environ.get("TELEGRAM_BOT_TOKEN") or "")
    if not token:
        log.step("── Telegram ──")
        log.step("  Бот нужен для уведомлений: старт шага, вердикт ревью, инциденты.")
        token = _ask("  Токен бота от @BotFather (Enter — пропустить)",
                     yes=getattr(args, "yes", False), secret=True)
    if not token:
        log.warn("уведомления в Telegram не настроены")
        return None
    try:
        me = telegram.get_me(token)
    except telegram.TelegramError as exc:
        log.warn(f"токен не принят: {exc}")
        return None
    log.ok(f"бот @{me.get('username', '?')} на связи")
    # Вебхук на боте занимает getUpdates: команды из чата получали бы 409. Предупреждаем
    # заранее, в мастере, а не молчаливым отказом стража (D8).
    try:
        hook = telegram.get_webhook_info(token)
        hook_url = str((hook or {}).get("url") or "").strip()
        if hook_url:
            log.warn(f"на боте стоит вебхук {hook_url}: команды из чата работать не будут"
                     " (getUpdates → 409)")
            log.warn("снять вебхук: https://api.telegram.org/botTOKEN/deleteWebhook, "
                     "затем python .pipeline/gate.py --commands-reset")
    except telegram.TelegramError as exc:
        log.warn(f"вебхук проверить не удалось: {exc}")

    chat = (getattr(args, "telegram_chat", "") or os.environ.get(CHAT_ENV) or "")
    if not chat:
        chat = _ask("  Куда слать (chat id, @канал или Enter — найти по последнему сообщению)",
                    yes=getattr(args, "yes", False))
    if not chat and not getattr(args, "yes", False) and sys.stdin.isatty():
        if _ask_bool("  Напиши боту /start и нажми Enter, чтобы поймать chat id?", True,
                     yes=getattr(args, "yes", False)):
            try:
                chat, owner = telegram.detect_chat_and_owner(token, wait_seconds=60)
                log.ok(f"чат найден: {chat}")
            except telegram.TelegramError as exc:
                log.warn(str(exc))
    if not chat:
        log.warn("chat id не задан — уведомления останутся выключены")
        return None
    try:
        telegram.validate_chat(token, chat)
    except telegram.TelegramError as exc:
        log.warn(f"чат не подтверждён: {exc} (проверь chat id и что бот добавлен в чат)")
        return None

    owner = locals().get("owner") or ""
    if not owner:
        owner = _ask("  Твой Telegram user id (Enter — только ты из чата)",
                     default=str(chat), yes=getattr(args, "yes", False))
    path = write_env(driver, {TOKEN_ENV: token, CHAT_ENV: chat, OWNER_ENV: owner})
    log.ok(f"токен и чат записаны в {path} ({_mask(token)})")
    if not getattr(args, "no_test", False):
        try:
            telegram.send_message(token, chat, "🔧 Конвейер шагов: уведомления подключены.")
            log.ok("тестовое сообщение отправлено")
        except telegram.TelegramError as exc:
            log.warn(f"тестовое сообщение не ушло: {exc}")
    return chat


def run_setup(args) -> int:
    """Полная установка. Возвращает код выхода."""
    log = Log()
    project = Path(getattr(args, "project", "") or Path.cwd()).expanduser().resolve()
    if not project.is_dir():
        print(f"ошибка: нет каталога {project}", file=sys.stderr)
        return 2
    yes = bool(getattr(args, "yes", False))
    force = bool(getattr(args, "force", False))
    if getattr(args, "upgrade", False):
        log.step(f"═══ Обновление движка → {project} ═══")
        return upgrade(project, log, trust=not bool(getattr(args, "no_trust", False)))
    log.step(f"═══ Конвейер шагов → {project} ═══")

    existing = proj.read_config(project) if (project / proj.MARKER).is_file() else {}
    board = (getattr(args, "board", "") or existing.get("board")
             or _ask("Борда канбана", proj.slug(project.name), yes=yes))
    profiles = existing.get("profiles") or {}
    driver = (getattr(args, "driver", "") or profiles.get("driver")
              or os.environ.get("HERMES_PROFILE") or "default")
    prompter = getattr(args, "prompter", "") or profiles.get("prompter") or "prompter"
    reviewer = getattr(args, "reviewer", "") or profiles.get("reviewer") or "reviewer"
    log.step(f"── Проект ──")
    log.ok(f"борда {board}, роли: {driver} (исполнитель), {prompter}, {reviewer}")

    log.step("── Движок ──")
    for item in copy_engine(project, force):
        log.ok(f"{item}")
    log.ok(f"версия движка: {write_engine_version(project)}")
    log.ok(f"маркер целостности: {write_engine_marker(project)[:12]}")
    roles_sync = copy_roles(project, refresh=force)
    for item in roles_sync.added:
        log.ok(f"добавлена роль {item}")
    for item in roles_sync.refreshed:
        log.ok(f"обновлена роль {item} (прежняя версия: {roles_sync.backups[item]})")
    for item in ensure_dirs(project):
        log.ok(f"{item}")
    log.ok(".gitignore: " + ("дополнен" if ensure_gitignore(project) else "уже содержит записи"))

    cfg = build_config(project, board, driver, prompter, reviewer, existing)
    chat = setup_telegram(project, driver, args, log)
    notify = dict(cfg.get("notify") or {})
    notify["enabled"] = bool(chat)
    cfg["notify"] = notify
    proj.write_config(project, cfg)
    log.ok(f"{proj.MARKER}: записан (уведомления {'вкл' if chat else 'выкл'})")

    if not getattr(args, "no_profiles", False):
        log.step("── Профили ролей ──")
        ensure_profiles(driver, prompter, reviewer, log)
    ensure_board(board, log)
    ensure_cron(driver, project, log, skip=bool(getattr(args, "no_cron", False)))
    ensure_commands_cron(driver, project, log, skip=bool(getattr(args, "no_cron", False)),
                         enabled=bool(chat))

    # Доверие проекту нужно каждой роли: без него воркер падает `Unknown skill(s)`,
    # и карточка уходит в blocked по failure_limit. Ошибка одной роли не ломает мастер.
    roles = roles_from_config(proj.read_config(project))
    if getattr(args, "no_trust", False):
        log.warn("доверие проекту не оформлялось (--no-trust)")
    else:
        trust_roles(roles, project, log)

    # Линтер только предупреждает: кривой план не должен ронять уже сделанную установку.
    log.step("── Проверка планов ──")
    lint = plan_lint.lint_project(project)
    if lint.problems:
        log.warn(f"линтер планов: проблем {len(lint.problems)} — подробности: hermes pipeline doctor")
        for problem in lint.problems[:10]:
            log.warn(str(problem))
    else:
        log.ok(f"линтер планов: файлов {len(lint.files)}, проблем нет")

    # Смоук: установка не «зелёная» на веру — пробная карточка на каждую роль.
    # Пропуск только там, где проверять нечего: профили и cron в этом запуске не
    # настраивались, либо владелец выключил проверку флагом.
    smoke: tuple[int, int] | None = None
    if getattr(args, "no_smoke", False):
        log.warn("смоук ролей пропущен (--no-smoke)")
    elif getattr(args, "no_profiles", False) or getattr(args, "no_cron", False):
        log.warn("смоук ролей пропущен: профили и cron в этом запуске не настраивались")
    else:
        timeout = int(getattr(args, "smoke_timeout", 0) or SMOKE_TIMEOUT)
        smoke = run_smoke(project, proj.read_config(project), roles, log, timeout)

    log.step("── Готово ──")
    if smoke is None:
        log.step("  смоук: не выполнялся — старт ролей не проверен")
    elif smoke[0] == smoke[1]:
        log.step(f"  смоук: роли стартуют ({smoke[0]} из {smoke[1]})")
    else:
        log.step(f"  смоук: {smoke[0]} из {smoke[1]} — "
                 f"конвейер не заработает, пока не починено")
    log.step(f"  статус:  hermes pipeline status --project \"{project}\"")
    log.step(f"  UI:      hermes pipeline ui --project \"{project}\"")
    log.step(f"  план:    скопировать templates/plan-template.md → docs/plans/1-<слаг>.md")
    log.step(f"  тик:     python .pipeline/gate.py --status   (в каталоге проекта)")
    if smoke is not None and smoke[0] < smoke[1]:
        return 3
    return 0
