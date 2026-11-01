# -*- coding: utf-8 -*-
"""Мастер установки конвейера в проект: один вопрос за раз, всё остальное само.

Что делает: копирует движок и роли, пишет `pipeline.json`, создаёт профили-роли,
заводит cron-джобу стража, спрашивает токен Telegram-бота и проверяет его живым
запросом, ставит проект в доверенные. Повторный запуск идемпотентен.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

try:
    from . import project as proj
    from . import telegram
except ImportError:  # pragma: no cover - запуск файлом при отладке
    import project as proj  # type: ignore
    import telegram  # type: ignore

ENGINE_FILES = ("__init__.py", "config.py", "registry.py", "next_step.py", "gate.py",
                "step_card.py", "check_task_prompt.py", "tick.py", "state_io.py")
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
TICK_SCRIPT = '''# -*- coding: utf-8 -*-
"""Тик конвейера для cron: один шаг стража. Пустой вывод = конвейер ждёт.

Свой разбор .env профиля: cron шлюза отдаёт HERMES_HOME, но не переменные из
profiles/<профиль>/.env — без этого уведомления движка молчат.
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
    sys.argv = [str(gate), "--run"]
    runpy.run_path(str(gate), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

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


def upgrade(project: Path, log: Log) -> int:
    """Обновить движок в уже поставленном проекте, не трогая конфиг и роли."""
    if not (project / ".pipeline").is_dir():
        print(f"ошибка: конвейер не установлен в {project} — нужен `hermes pipeline setup`",
              file=sys.stderr)
        return 2
    was = engine_version(project) or "?"
    changed = copy_engine(project, force=False)
    roles = copy_roles(project)
    now = write_engine_version(project)
    for item in changed:
        log.ok(f"обновлён {item}")
    for item in roles:
        log.ok(f"добавлена роль {item}")
    if not changed and not roles:
        log.ok(f"движок уже актуален ({now})")
    log.step(f"── Движок: {was} → {now}. Конфиг, telegram и cron не тронуты ──")
    return 0


def copy_roles(project: Path) -> list[str]:
    """Роли в `.hermes/skills/<категория>/<имя>` — только если ещё нет."""
    copied: list[str] = []
    for name, category in SKILL_CATEGORIES.items():
        src = proj.ROLES_SRC / name
        if not src.is_dir():
            continue
        dest = project / ".hermes" / "skills" / category / name
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        copied.append(f".hermes/skills/{category}/{name}")
    return copied


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
    entries = (".pipeline/state/", ".pipeline/.env")
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
    notify.update({"token_env": TOKEN_ENV, "chat_env": CHAT_ENV})
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
    """Cron-джоба стража: no-agent, каждые 10 минут, доставка local."""
    if skip:
        log.warn("cron не заводил (--no-cron)")
        return
    name = "План: страж шага (конвейер)"
    listing = proj.run_hermes("-p", driver, "cron", "list")
    if name in (listing.stdout or ""):
        log.ok("cron-джоба стража уже есть")
        _write_cron_marker(project, driver, name)
        return
    scripts = proj.profile_dir(driver) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "pipeline_tick.py").write_text(TICK_SCRIPT, encoding="utf-8")
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


def _mask(token: str) -> str:
    return token[:8] + "…" + token[-4:] if len(token) > 14 else "***"


def setup_telegram(project: Path, driver: str, args, log: Log) -> str | None:
    """Токен бота, чат и запись в .env профиля. Возвращает chat id, если настроено."""
    if getattr(args, "no_telegram", False):
        log.warn("уведомления в Telegram отключены (--no-telegram)")
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

    chat = (getattr(args, "telegram_chat", "") or os.environ.get(CHAT_ENV) or "")
    if not chat:
        chat = _ask("  Куда слать (chat id, @канал или Enter — найти по последнему сообщению)",
                    yes=getattr(args, "yes", False))
    if not chat and not getattr(args, "yes", False) and sys.stdin.isatty():
        if _ask_bool("  Напиши боту /start и нажми Enter, чтобы поймать chat id?", True,
                     yes=getattr(args, "yes", False)):
            try:
                chat = telegram.detect_chat(token, wait_seconds=60)
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

    path = write_env(driver, {TOKEN_ENV: token, CHAT_ENV: chat})
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
        return upgrade(project, log)
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
    for item in copy_roles(project):
        log.ok(f"{item}")
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
    for role in dict.fromkeys((driver, prompter, reviewer)):
        trust = proj.run_hermes("-p", role, "skills", "trust", str(project))
        if trust.returncode == 0:
            log.ok(f"проект в доверенных у {role}")
        else:
            log.warn(f"skills trust ({role}): {(trust.stderr or trust.stdout or '').strip()[:200]}")

    log.step("── Готово ──")
    log.step(f"  статус:  hermes pipeline status --project \"{project}\"")
    log.step(f"  UI:      hermes pipeline ui --project \"{project}\"")
    log.step(f"  план:    скопировать templates/plan-template.md → docs/plans/1-<слаг>.md")
    log.step(f"  тик:     python .pipeline/gate.py --status   (в каталоге проекта)")
    return 0
