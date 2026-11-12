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
                "step_card.py", "check_task_prompt.py", "scout_report.py", "plan_cycle.py",
                "approval.py", "tick.py", "analyze.py",
                "state_io.py")
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
    "pipeline-scout": "software-development",
    "pipeline-plan-reviewer": "software-development",
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
    """Печать шагов мастера: ровно одна строка на действие.

    `quiet=True` — строки копятся в `lines`, но не печатаются: мастер в панели отдаёт
    их в ответ API, а не в stdout сервера. Умолчание прежнее — `run_setup` печатает.
    """

    def __init__(self, quiet: bool = False) -> None:
        self.lines: list[str] = []
        self.quiet = quiet

    def step(self, text: str) -> None:
        self.lines.append(text)
        if not self.quiet:
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
        # Сверка по байтам, а не по тексту: `read_text` съедает CRLF, и копия с виндовыми
        # переводами строк считалась идентичной — файл не перезаписывался, а `doctor`
        # (сверка по байтам) потом вечно показывал «копия расходится при равной версии».
        if dest.is_file() and not force and dest.read_bytes() == src.read_bytes():
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


def engine_marker_problem(project: Path) -> str:
    """Почему копия движка в проекте не сходится со своим маркером ('' — сходится).

    Семантика та же, что у `gate.verify_engine_integrity`: маркера нет — легаси-установка,
    это не поломка (тик на ней работает); маркер есть и не сходится — поломка, при которой
    тик отказывается работать (код 3), поэтому в отчёте это проблема, а не заметка.
    """
    base = Path(project) / ".pipeline"
    marker = base / ENGINE_MARKER
    if not marker.is_file():
        return ""
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"маркер целостности не читается: {exc}"
    expected = text.split()[0].removeprefix("sha256:") if text else ""
    if not expected:
        return "маркер целостности пуст"
    actual = engine_digest(base)
    if actual != expected:
        return f"движок изменён после установки: {actual[:12]} != {expected[:12]}"
    return ""


def engine_drift(project: Path) -> list[str]:
    """Файлы, которыми копия движка в проекте расходится с движком плагина.

    Нужен doctor'у: прецедент 02.11.2026 — копия и плагин с одинаковым VERSION несли
    разную логику. Сверка по байтам ловит это без запуска тика, ещё до стопа стража.
    """
    base = Path(project) / ".pipeline"
    if not base.is_dir():
        return []
    names = {path.name for path in base.glob("*.py")} | set(ENGINE_FILES)
    drift = []
    for name in sorted(names):
        mine = proj.ENGINE_SRC / name
        theirs = base / name
        if not mine.is_file() or not theirs.is_file():
            drift.append(name)          # файл есть только с одной стороны
        elif mine.read_bytes() != theirs.read_bytes():
            drift.append(name)
    return drift


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


def link_plugin_profile(profile: str, log: Log, package: Path | None = None) -> bool:
    """Связать профиль с установленным пакетом плагина (ссылка, а не вторая копия).

    `plugins enable` не знает плагина, которого нет в `<профиль>/plugins/`: у профиля
    свой каталог плагинов. Junction на Windows и символьная ссылка на POSIX дают профилю
    тот же пакет, что и остальным, — обновляется он один, отдельная копия не нужна.
    """
    package = (package or Path(__file__).resolve().parent).resolve()
    target = proj.profile_dir(profile) / "plugins" / package.name
    if target.exists() or target.is_symlink():
        return True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            res = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(package)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=60)
            if res.returncode != 0:
                log.warn(f"ссылку на плагин в профиле {profile} создать не удалось "
                         f"(mklink вернул {res.returncode})")
                return False
        else:
            os.symlink(package, target, target_is_directory=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warn(f"ссылку на плагин в профиле {profile} создать не удалось: {exc}")
        return False
    return True


def _enable_plugin(profile: str) -> subprocess.CompletedProcess:
    """`hermes -p <профиль> plugins enable step_pipeline`."""
    hermes = shutil.which("hermes") or "hermes"
    return subprocess.run([hermes, "-p", profile, "plugins", "enable", "step_pipeline"],
                          capture_output=True, text=True, timeout=180)


def _plugin_cli_answers(profile: str) -> bool:
    """Отвечает ли `hermes -p <профиль> pipeline --help` (то есть зарегистрировался CLI)."""
    try:
        res = subprocess.run([shutil.which("hermes") or "hermes", "-p", profile,
                              "pipeline", "--help"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def ensure_plugin_enabled(profile: str, log: Log) -> str:
    """Включить текущий id плагина в профиле и проверить, что команда `pipeline` есть."""
    try:
        res = _enable_plugin(profile)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warn(f"плагин не включён автоматически: {exc}")
        return f"включение плагина не удалось ({exc})"
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    if res.returncode != 0 and "No plugin named" in out:
        # В профиле нет своего каталога плагина — команда его не знает. Связываем профиль
        # с установленным пакетом и повторяем: иначе в этом профиле `hermes pipeline` нет.
        log.warn(f"в профиле {profile} плагина нет — связываю с установленным пакетом")
        if link_plugin_profile(profile, log):
            res = _enable_plugin(profile)
            out = ((res.stdout or "") + (res.stderr or "")).strip()
    if res.returncode != 0:
        log.warn(f"`plugins enable step_pipeline` вернул {res.returncode}: {out}")
        return "плагин не включён"
    check = _plugin_cli_answers(profile)
    if not check:
        log.warn("команда `hermes pipeline` не отвечает после включения плагина — "
                 "перезапусти сессию профиля")
        return "плагин включён, команда проверится в новой сессии"
    log.ok(f"плагин step_pipeline включён в профиле {profile}")
    return "плагин включён и команда отвечает"




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
    for item, files in roles.kept.items():
        log.ok(f"роль {item}: сохранены файлы проекта — {', '.join(files)}")
    if not changed and not roles.added and not roles.refreshed:
        log.ok(f"движок и роли уже актуальны ({now})")
    quarantine_stale_plugins(log)
    cfg = proj.read_config(project)
    driver = roles_from_config(cfg)[0]
    plugin_state = ensure_plugin_enabled(driver, log)  # noqa: F841 - важен сам вызов, а не итог
    # Разведка — четвёртая роль: движок берёт `profiles.scout`, а без ключа — профиль
    # `scout`. Заводим его, если нет (настройки существующего не переписываем), иначе
    # `create_scout` уходит в несуществующий профиль и разведка молча не идёт.
    scout = scout_role(cfg)
    if scout != driver:
        ensure_scout_profile(scout, driver, log)
    # Джобы проверяем при каждом обновлении: проект, поставленный без них (старый мастер
    # или `--no-cron` без записи отказа), иначе остаётся без исполнения карточек — страж
    # заказывает, брать некому. Явный отказ владельца уважаем.
    if cron_opted_out(project):
        log.ok("cron не трогаю: проект поставлен с --no-cron")
        tick = "cron не тронут (--no-cron)"
    else:
        ensure_cron_all(driver, project, log, skip=False,
                        enabled=bool((cfg.get("notify") or {}).get("enabled")))
        tick = "cron-джобы проверены (страж, команды бота, автообновление)"
    if trust:
        failed = trust_roles(list(dict.fromkeys(roles_from_config(cfg) + [scout])), project, log)
        tail = ("доверие ролям оформлено" if not failed
                else f"доверие не оформлено у: {', '.join(failed)}")
    else:
        log.warn("доверие проекту не оформлялось (--no-trust)")
        tail = "доверие ролям пропущено (--no-trust)"
    log.step(f"── Движок: {was} → {now}, роли: {len(roles.refreshed)} обновлено. "
             f"Конфиг и telegram не тронуты; {tick}; {tail} ──")
    return 0


@dataclass
class RolesSync:
    """Итог синхронизации ролей проекта: добавленные, перезаписанные и где их бэкап."""

    added: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    backups: dict[str, str] = field(default_factory=dict)
    kept: dict[str, list[str]] = field(default_factory=dict)


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

    Вендоринг касается только файлов, которые плагин поставляет: свои файлы внутри
    роли (шаблоны промтов, фрагменты `AGENTS.md`) проект дописывает сам, ссылки на них
    стоят в `SKILL.md` роли — такие файлы обновление оставляет на месте (0.25.1).
    До этого `rmtree` сносил их, и шаблон приходилось восстанавливать из бэкапа руками
    (так было на 0.13.0 и повторилось на 0.25.0).
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
        if not refresh:
            continue
        wanted, have = _role_files(src), _role_files(dest)
        # Сравниваем только поставленные плагином файлы: дописанное проектом не повод
        # переписывать роль и плодить бэкап на каждом `--upgrade`.
        if all(have.get(path) == data for path, data in wanted.items()):
            continue
        local_only = sorted(set(have) - set(wanted))
        backup = project / ROLES_BACKUP / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dest, backup, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        for extra in local_only:
            target = dest / extra
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / extra, target)
        result.refreshed.append(rel)
        result.backups[rel] = backup.relative_to(project).as_posix()
        if local_only:
            result.kept[rel] = local_only
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
                 existing: dict[str, Any] | None = None, scout: str = "") -> dict[str, Any]:
    """`pipeline.json`: шаблон плагина + выбранные значения, чужие правки сохраняются."""
    base: dict[str, Any] = {}
    try:
        base = json.loads(proj.DEFAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        base = {}
    cfg = {**base, **(existing or {})}
    cfg["project"] = (existing or {}).get("project") or project.name
    cfg["board"] = board
    profiles = {"driver": driver, "prompter": prompter, "reviewer": reviewer}
    # Разведка — четвёртая роль конвейера: имя пишет мастер (иначе движок берёт профиль
    # `scout`, которого на чистой машине нет и в который карточка уходит вхолостую).
    if scout:
        profiles["scout"] = scout
    # `profiles` принадлежит владельцу (`AGENTS.md`): мастер меняет роли мастера,
    # остальные ключи (например, `scout` из прошлой установки) обязаны пережить
    # повторный `setup` и шаг мастера «Проект» (D9).
    previous = cfg.get("profiles")
    for key, value in (previous.items() if isinstance(previous, dict) else ()):
        profiles.setdefault(key, value)
    cfg["profiles"] = profiles
    notify = dict(cfg.get("notify") or {})
    notify.update({"token_env": TOKEN_ENV, "chat_env": CHAT_ENV,
               "owner_env": OWNER_ENV, "dialog": {"enabled": True}})
    cfg["notify"] = notify
    return cfg


def ensure_scout_profile(scout: str, driver: str, log: Log) -> None:
    """Завести профиль разведки клоном драйвера, если его ещё нет.

    Настройки существующего профиля не переписываются: `setup --upgrade` не должен
    менять то, что владелец уже настроил руками.
    """
    listing = proj.run_hermes("profile", "list")
    have = listing.stdout or ""
    if f" {scout} " in f" {have} ":
        log.ok(f"профиль разведки {scout} уже есть")
        return
    res = proj.run_hermes("profile", "create", scout, "--clone-from", driver,
                          "--description", "Разведка: готовые решения и лицензии → prior art")
    if res.returncode == 0:
        log.ok(f"профиль разведки {scout} создан (клон {driver})")
    else:
        log.warn(f"профиль разведки {scout}: {(res.stderr or res.stdout or '').strip()[:200]}")


def ensure_role_profile(role: str, driver: str, description: str, effort: str, log: Log) -> None:
    """Один профиль роли: клон драйвера, максимум рассуждений, один воркер за раз."""
    listing = proj.run_hermes("profile", "list")
    have = listing.stdout or ""
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


def ensure_profiles(driver: str, prompter: str, reviewer: str, log: Log,
                    scout: str = "") -> None:
    """Профили ролей конвейера: постановщик, ревьюер и разведка.

    Разведка — четвёртая роль: её профиль тоже клон драйвера. Без него `create_scout`
    уходит в профиль, которого нет (или в одноимённый, но без доверия и ключа), и
    разведка не идёт, хотя установка зелёная.
    """
    roles = [
        (prompter, "Постановщик: контекст шага плана → промт-постановка", "max"),
        (reviewer, "Ревьюер: независимая проверка шага по плану и коду", "max"),
    ]
    if scout and scout not in (driver, prompter, reviewer):
        roles.append((scout, "Разведка: готовые решения и лицензии → prior art", "max"))
    for role, description, effort in roles:
        ensure_role_profile(role, driver, description, effort, log)
    for key, value in (("agent.max_turns", "300"),
                       ("kanban.max_in_progress_per_profile", "1"),
                       ("kanban.failure_limit", "2"),
                       ("kanban.default_assignee", driver)):
        proj.run_hermes("-p", driver, "config", "set", key, value)
    applied = f"{driver}/{prompter}/{reviewer}"
    if any(role == scout for role, _, _ in roles) and scout not in (driver, prompter, reviewer):
        applied += f"/{scout}"
    log.ok(f"настройки конвейера применены к {applied}")


#: Роли конвейера по умолчанию — те же, что у мастера установки.
DEFAULT_ROLES = ("default", "prompter", "reviewer")
#: Профиль разведки по умолчанию — как у движка (`gate.SCOUT_PROFILE_DEFAULT`).
SCOUT_ROLE_DEFAULT = "scout"


def scout_role(cfg: dict[str, Any] | None) -> str:
    """Профиль разведки из конфига: `profiles.scout`, иначе умолчание движка."""
    profiles = (cfg or {}).get("profiles")
    name = profiles.get("scout") if isinstance(profiles, dict) else ""
    return str(name or SCOUT_ROLE_DEFAULT)

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
    """Роли конвейера из `pipeline.json`: драйвер → промптер → ревьюер → разведка.

    Разведка попадает в список, только когда задана в конфиге: у проектов, поставленных
    мастером без неё, ключа нет, и проверять доверие/ключ профиля, которым не
    пользуются, значило бы красить зелёную установку. Пустой или битый `profiles` даёт
    умолчания, дубликаты схлопываются.
    """
    profiles = (cfg or {}).get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    roles = [profiles.get(key) or fallback
             for key, fallback in zip(("driver", "prompter", "reviewer"), DEFAULT_ROLES)]
    if profiles.get("scout"):
        roles.append(profiles["scout"])
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


def _write_cron_marker(project: Path, driver: str, name: str, *, skipped: bool = False) -> None:
    """Метка для `doctor`: какую джобу завёл мастер (cron list не показывает workdir).

    `skipped` — владелец поставил `--no-cron`. Отказ надо запомнить: иначе обновление
    движка не отличит «джобу не заводили намеренно» от «её нет, потому что мастер не
    дошёл» и либо заведёт джобу против воли владельца, либо не заведёт её никогда.
    """
    marker = project / ".pipeline" / "cron.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"driver": driver, "name": name, "workdir": str(project),
                                  "skipped": bool(skipped)},
                                 ensure_ascii=False), encoding="utf-8")


def cron_opted_out(project: Path) -> bool:
    """Проект поставлен с `--no-cron`: джобы заводить нельзя, даже при обновлении."""
    marker = project / ".pipeline" / "cron.json"
    if not marker.is_file():
        return False
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool((info or {}).get("skipped"))


def ensure_cron_all(driver: str, project: Path, log: Log, *, skip: bool,
                    enabled: bool = True, commands_skipped: bool = False) -> None:
    """Все джобы конвейера разом: страж, приём команд бота, автообновление.

    Один вход и для установки, и для обновления движка: проект, поставленный до появления
    джоб (или с `--no-cron` в старой версии, где отказ не записывался), иначе остаётся
    без исполнения карточек — страж заказывает карточку, а брать её некому.
    """
    if skip:
        _write_cron_marker(project, driver, "План: страж шага (конвейер)", skipped=True)
        log.warn("cron не заводил (--no-cron)")
        return
    ensure_cron(driver, project, log, skip=False)
    ensure_commands_cron(driver, project, log, skip=False, enabled=enabled,
                         skipped=commands_skipped)
    ensure_update_cron(driver, project, log, skip=False)


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


#: Ежедневное автообновление: релиз выходит не каждый день, а проверка дешёвая (один
#: запрос к GitHub) — раз в сутки достаточно и не мешает тику стража.
UPDATE_CRON = "0 5 * * *"
UPDATE_JOB = "План: обновление плагина (конвейер)"
UPDATE_SCRIPT = '''
def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def find_plugin():
    """Каталог установленного плагина: рядом с профилем, иначе в ~/.hermes."""
    here = Path(__file__).resolve()
    candidates = [here.parents[1] / "plugins" / "step_pipeline",
                  Path.home() / ".hermes" / "plugins" / "step_pipeline"]
    for cand in candidates:
        if (cand / "update.py").is_file():
            return cand
    return None


def main():
    _setup_stdio()
    plugin = find_plugin()
    if plugin is None:
        print("pipeline_update: плагин step_pipeline не найден", file=sys.stderr)
        return 1
    sys.path.insert(0, str(plugin))
    from update import auto  # noqa: E402
    result = auto()
    if result.get("error"):
        print("pipeline_update: " + str(result["error"]), file=sys.stderr)
        return 1
    if result.get("updated"):
        print(result.get("message") or "плагин обновлён")
        if result.get("restart"):
            print(result["restart"])
    return 0
'''


def install_update_script(driver: str) -> Path:
    """Положить скрипт автообновления в `profiles/<профиль>/scripts/` (cron берёт только оттуда)."""
    scripts = proj.profile_dir(driver) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    header = ('# -*- coding: utf-8 -*-\n'
              '"""Автообновление плагина конвейера из GitHub (`--no-agent`, раз в сутки).\n\n'
              'Плагин ставится копией, поэтому обновление — скачать архив тега и переписать\n'
              'пакет. Движок в проектах обновляет владелец: `hermes pipeline setup --upgrade`\n'
              '(плагин не переписывает `.pipeline/` в проектах молча).\n'
              '"""\n'
              'import sys\n'
              'from pathlib import Path\n')
    target = scripts / "pipeline_update.py"
    target.write_text(header + UPDATE_SCRIPT, encoding="utf-8")
    return target


def ensure_update_cron(driver: str, project: Path, log: Log, *, skip: bool) -> None:
    """Cron-джоба автообновления плагина: no-agent, раз в сутки, доставка local.

    Заводится вместе со стражем: иначе «автоматическое обновление» означало бы «пока
    открыта панель». Скрипт переписывается при каждом прогоне мастера — джоба,
    заведённая со старой копией, иначе осталась бы с ней навсегда.
    """
    if skip:
        log.warn("автообновление не заведено (--no-cron)")
        return
    install_update_script(driver)
    listing = proj.run_hermes("-p", driver, "cron", "list")
    if UPDATE_JOB in (listing.stdout or ""):
        log.ok("cron-джоба автообновления уже есть (скрипт обновлён)")
        return
    res = proj.run_hermes("-p", driver, "cron", "create", UPDATE_CRON,
                          "--name", UPDATE_JOB, "--no-agent",
                          "--script", "pipeline_update.py",
                          "--deliver", "local",
                          "--workdir", str(project))
    if res.returncode == 0:
        log.ok("cron-джоба автообновления создана (ежедневно)")
    else:
        log.warn(f"cron автообновления: {(res.stderr or res.stdout or '').strip()[:300]}")
        log.warn("завести вручную: " + update_cron_command(driver, project))


def update_cron_command(driver: str, project: Path) -> str:
    return (f'hermes -p {driver} cron create "{UPDATE_CRON}" --name "{UPDATE_JOB}" '
            f'--no-agent --script pipeline_update.py --deliver local --workdir "{project}"')


COMMANDS_JOB = "План: бот команды (конвейер)"


def ensure_commands_cron(driver: str, project: Path, log: Log, *, skip: bool,
                         enabled: bool, skipped: bool = False) -> None:
    """Cron-джоба приёма команд бота: no-agent, раз в минуту.

    Отдельно от стража: страж идёт раз в 10 минут, а `/status` и `/stop` столько ждать
    нельзя. Модель не нужна — это один `getUpdates`.

    `skipped` — владелец осознанно выбрал «без бота» (D1, D3): это допустимый исход, а не
    предупреждение. `warn` остаётся только для «бот запрошен, но не сконфигурирован».
    """
    if skip:
        return
    if not enabled:
        if skipped:
            log.ok("приём команд бота не заведён: Telegram выключен выбором владельца — допустимо")
        else:
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
    for key in ("error", "result", "event"):
        value = str(state.get(key) or "").strip()
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


def setup_telegram(project: Path, driver: str, args, log: Log) -> tuple[str | None, bool]:
    """Токен бота, чат и владелец в `.env` профиля. Возвращает `(chat, skipped)`.

    `skipped=True` — владелец осознанно выбрал «без бота» (D1): такой путь не печатает ни
    одной строки `! `, а исход сохраняется в `pipeline.json` ключом `notify.skipped`.
    Плохой токен или чат — `(None, False)`: это отказ Telegram, а не выбор владельца.
    """
    if getattr(args, "no_telegram", False):
        log.step("── Telegram (необязательно) ──")
        log.ok("Telegram не подключён: уведомления и команды выключены — допустимый выбор")
        return None, True
    token = getattr(args, "telegram_token", "") or ""
    env_token = os.environ.get(TOKEN_ENV) or ""
    if not token and not env_token:
        log.step("── Telegram (необязательно) ──")
        if not _ask_bool("  Подключить бота для уведомлений и диалога?"
                         " (без бота не будет ни уведомлений о старте шага и вердиктах,"
                         " ни команд из чата)", False,
                         yes=getattr(args, "yes", False)):
            log.ok("Telegram не подключён: уведомления и команды выключены — допустимый выбор")
            return None, True
    if not token and env_token:
        # D5: повторный запуск не молчит про уже настроенный токен, а предлагает замену.
        log.step("── Telegram ──")
        log.ok("токен бота уже настроен")
        if _ask_bool("  Заменить токен бота?", False, yes=getattr(args, "yes", False)):
            replacement = _ask("  Новый токен бота от @BotFather (Enter — оставить прежний)",
                               yes=getattr(args, "yes", False), secret=True)
            if replacement:
                token = replacement
    token = token or env_token or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        log.step("── Telegram ──")
        log.step("  Бот нужен для уведомлений: старт шага, вердикт ревью, инциденты.")
        token = _ask("  Токен бота от @BotFather (Enter — пропустить)",
                     yes=getattr(args, "yes", False), secret=True)
    if not token:
        log.ok("Telegram не подключён: уведомления и команды выключены — допустимый выбор")
        return None, True
    try:
        me = telegram.get_me(token)
    except telegram.TelegramError as exc:
        log.warn(f"токен не принят: {exc}")
        return None, False
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
    # D4 и §4.4: правила BotFather называются словами, которые ищет владелец, а в логе нет
    # ни токена, ни его частей — только путь к `.env`.
    log.step("  BotFather: вебхук снимается методом Bot API deleteWebhook —"
             " https://api.telegram.org/botTOKEN/deleteWebhook; пока он стоит,"
             " getUpdates получает 409 и команды из чата не работают")
    log.step("  BotFather: privacy mode (/setprivacy) — в группе бот должен видеть сообщения")
    log.step("  BotFather: кто может писать боту, галочкой не настраивается — это owner id"
             " (allowlist) в конфиге, API у BotFather для этого нет")

    owner = ""
    chat = (getattr(args, "telegram_chat", "") or os.environ.get(CHAT_ENV) or "")
    if not chat:
        chat = _ask("  Куда слать (chat id, @канал или Enter — найти по последнему сообщению)",
                    yes=getattr(args, "yes", False))
    if not chat and not getattr(args, "yes", False) and sys.stdin.isatty():
        if _ask_bool("  Напиши боту /start и нажми Enter, чтобы поймать chat id?", True,
                     yes=getattr(args, "yes", False)):
            try:
                chat, owner = telegram.detect_chat_and_owner(token, wait_seconds=60)
                # D6: owner из /start — достоверный источник; пустой owner заменяется chat id.
                log.ok(f"чат найден: {chat}"
                       + (f", владелец {owner}" if owner else ", владелец не виден — chat id"))
            except telegram.TelegramError as exc:
                log.warn(str(exc))
    if not chat:
        log.warn("chat id не задан — уведомления останутся выключены")
        return None, False
    try:
        telegram.validate_chat(token, chat)
    except telegram.TelegramError as exc:
        log.warn(f"чат не подтверждён: {exc} (проверь chat id и что бот добавлен в чат)")
        return None, False

    if not owner:
        owner = _ask("  Твой Telegram user id (Enter — только ты из чата)",
                     default=str(chat), yes=getattr(args, "yes", False))
    path = write_env(driver, {TOKEN_ENV: token, CHAT_ENV: chat, OWNER_ENV: owner})
    log.ok(f"токен и чат записаны в {path}")
    if not getattr(args, "no_test", False):
        try:
            telegram.send_message(token, chat, "🔧 Конвейер шагов: уведомления подключены.")
            log.ok("тестовое сообщение отправлено")
        except telegram.TelegramError as exc:
            log.warn(f"тестовое сообщение не ушло: {exc}")
    return chat, False


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
    scout = getattr(args, "scout", "") or profiles.get("scout") or SCOUT_ROLE_DEFAULT
    log.step("── Проект ──")
    log.ok(f"борда {board}, роли: {driver} (исполнитель), {prompter}, {reviewer}, "
           f"разведка {scout}")

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
    for item, files in roles_sync.kept.items():
        log.ok(f"роль {item}: сохранены файлы проекта — {', '.join(files)}")
    for item in ensure_dirs(project):
        log.ok(f"{item}")
    log.ok(".gitignore: " + ("дополнен" if ensure_gitignore(project) else "уже содержит записи"))

    cfg = build_config(project, board, driver, prompter, reviewer, existing, scout=scout)
    chat, skipped = setup_telegram(project, driver, args, log)
    notify = dict(cfg.get("notify") or {})
    # D1: «без бота» — выбор владельца, а не недоделанный шаг. Отказ хранится в конфиге,
    # иначе мастер в панели вечно показывает «следующий шаг: telegram».
    notify["enabled"] = bool(chat)
    notify["skipped"] = bool(skipped and not chat)
    cfg["notify"] = notify
    proj.write_config(project, cfg)
    log.ok(f"{proj.MARKER}: записан (уведомления {'вкл' if chat else 'выкл'})")

    if not getattr(args, "no_profiles", False):
        log.step("── Профили ролей ──")
        ensure_profiles(driver, prompter, reviewer, log, scout=scout)
    ensure_board(board, log)
    ensure_cron_all(driver, project, log, skip=bool(getattr(args, "no_cron", False)),
                    enabled=bool(chat), commands_skipped=bool(notify.get("skipped")))

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
        # Предупреждения видны в итоге установки: иначе «проблем нет» соврёт о состоянии.
        log.ok(f"линтер планов: файлов {len(lint.files)}, проблем нет"
               + (f", предупреждений {len(lint.warnings)}" if lint.warnings else ""))
    # Л20 — предупреждения, а не проблемы: план без строки `Конституция:` в шапке.
    for warning in lint.warnings[:10]:
        log.warn(str(warning))

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
    log.step("  план:    скопировать templates/plan-template.md → docs/plans/1-<слаг>.md")
    log.step("  тик:     python .pipeline/gate.py --status   (в каталоге проекта)")
    if smoke is not None and smoke[0] < smoke[1]:
        return 3
    return 0


# ── Мастер установки в панели (план 3 S4) ────────────────────────────────────
# D2: состояние мастера считается по фактам на диске, курсора нет. Поэтому повторный
# проход идемпотентен, а отказ на середине не оставляет «полушагов»: шаг либо выполнен,
# либо его факт не выполнен, и владелец видит, с какого шага продолжить (`next`).
# D5: код шагов один на CLI и панель — здесь вызываются те же функции, что у run_setup.
# D8: шаг делает только свой предмет. В частности шаг «проект» не выключает уже
# включённые уведомления: Telegram — отдельный шаг, и молча гасить его нельзя.

#: Порядок шагов мастера. `next` — первый шаг, чей факт не выполнен.
SETUP_STEPS = ("project", "board", "profiles", "telegram", "cron", "checks")

SETUP_TITLES = {
    "project": "Проект",
    "board": "Борда",
    "profiles": "Профили ролей",
    "telegram": "Telegram",
    "cron": "Cron",
    "checks": "Проверки",
}

SETUP_HINTS = {
    "project": "нужны pipeline.json, .pipeline/gate.py и роли в .hermes/skills",
    "board": "нужна база борды канбана",
    "profiles": "проект должен быть доверенным у всех трёх профилей-ролей",
    "telegram": "необязательно: токен и chat id в .env драйвера; без бота шаг закрывается выбором",
    "cron": "нужна cron-джоба стража (метка .pipeline/cron.json)",
    "checks": "отчёт: линтер планов, версии, доверие, борда, cron, Telegram",
}


def env_values(profile: str) -> dict[str, str]:
    """Переменные из `.env` профиля: без комментариев, кавычек и пустых значений.

    Разбор один на весь пакет — `project.read_env_file`: мастеру нужны значения профиля,
    а не подмена окружения сервера панели, поэтому `os.environ` здесь не трогаем.
    """
    return proj.read_env_file(env_path(profile))


def setup_defaults(project: Path) -> dict[str, str]:
    """Значения формы мастера по умолчанию: конфиг проекта важнее свежих догадок."""
    project = Path(project).expanduser().resolve()
    existing = proj.read_config(project) if (project / proj.MARKER).is_file() else {}
    profiles = existing.get("profiles") or {}
    return {
        "board": existing.get("board") or proj.slug(project.name),
        "driver": profiles.get("driver") or os.environ.get("HERMES_PROFILE") or "default",
        "prompter": profiles.get("prompter") or "prompter",
        "reviewer": profiles.get("reviewer") or "reviewer",
        "scout": profiles.get("scout") or SCOUT_ROLE_DEFAULT,
    }


def _project_fact(project: Path) -> bool:
    """Конвейер в проекте есть: конфиг и движок на месте."""
    return (project / proj.MARKER).is_file() and (project / ".pipeline" / "gate.py").is_file()


def _board_fact(project: Path) -> bool:
    """База борды существует — её же ищет движок."""
    return proj.board_db(project).is_file()


def _profiles_fact(project: Path, cfg: dict[str, Any]) -> bool:
    """Проект доверен всем ролям конфига: без этого воркер падает `Unknown skill(s)`."""
    roles = roles_from_config(cfg)
    return bool(roles) and all(is_project_trusted(role, project) for role in roles)


def _telegram_fact(cfg: dict[str, Any]) -> bool:
    """Уведомления настроены или владелец осознанно выбрал «без бота» (D1).

    Выключенные уведомления без отметки `skipped` — недоделанный шаг, а не выполненный:
    иначе `next` не позвал бы владельца на шаг Telegram.
    """
    notify = cfg.get("notify") or {}
    if not notify.get("enabled"):
        return bool(notify.get("skipped"))
    env = env_values(roles_from_config(cfg)[0])
    token_env = str(notify.get("token_env") or TOKEN_ENV)
    chat_env = str(notify.get("chat_env") or CHAT_ENV)
    return bool(env.get(token_env)) and bool(env.get(chat_env))


def _cron_fact(project: Path, cfg: dict[str, Any]) -> bool:
    """Cron-джоба стража заведена мастером: метка помнит драйвера и workdir."""
    try:
        marker = json.loads((project / ".pipeline" / "cron.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(marker, dict) and marker.get("driver") == roles_from_config(cfg)[0]


def _step_done(project: Path, step: str, cfg: dict[str, Any]) -> bool:
    """Выполнен ли шаг — по факту на диске. `checks` — отчёт, его факта нет (D4)."""
    if step == "project":
        return _project_fact(project)
    if step == "board":
        return _board_fact(project)
    if step == "profiles":
        return _profiles_fact(project, cfg)
    if step == "telegram":
        return _telegram_fact(cfg)
    if step == "cron":
        return _cron_fact(project, cfg)
    return False


def setup_status(project: Path) -> dict[str, Any]:
    """Состояние мастера: шаги с фактами, `next` и значения формы без секретов (D6).

    `next` считается по `SETUP_STEPS` без терминального `checks`: проверки ничего не
    настраивают, иначе следующий шаг никогда не стал бы пустым (D2).
    """
    project = Path(project).expanduser().resolve()
    cfg = proj.read_config(project) if (project / proj.MARKER).is_file() else {}
    steps = [{"id": step, "title": SETUP_TITLES[step], "hint": SETUP_HINTS[step],
              "done": _step_done(project, step, cfg)} for step in SETUP_STEPS]
    next_step = next((item["id"] for item in steps
                      if item["id"] != "checks" and not item["done"]), None)
    driver = roles_from_config(cfg)[0]
    env = env_values(driver)
    # Имена переменных — из конфига, как в `_telegram_fact`: при своём имени
    # переменной панель не должна показывать «токена нет» у закрытого шага.
    notify = cfg.get("notify") or {}
    token_env = str(notify.get("token_env") or TOKEN_ENV)
    chat_env = str(notify.get("chat_env") or CHAT_ENV)
    values = {
        **setup_defaults(project),
        "engine_version": engine_version(project),
        "plugin_version": plugin_version(),
        "telegram_token_set": bool(env.get(token_env)),
        "telegram_chat": str(env.get(chat_env) or ""),
    }
    return {"steps": steps, "next": next_step, "values": values}


def _setup_error(log: Log, fallback: str) -> str:
    """Текст отказа шага: последнее предупреждение лога, иначе общая фраза."""
    for line in reversed(log.lines):
        if line.strip().startswith("!"):
            return line.strip()[1:].strip()
    return fallback


def run_setup_step(project: Path, step: str, values: dict[str, Any] | None = None,
                   log: Log | None = None) -> dict[str, Any]:
    """Выполнить один шаг мастера: `ok`, `already`, `next`, `log`, `error`.

    Шаг идемпотентен: повторный проход по выполненному шагу говорит «уже настроено»,
    но остаётся безвредным (D5). `next` пересчитывается по фактам после шага, а не по
    порядку, — поэтому отказ на середине честно указывает, с какого шага продолжать.
    Исключений наружу не бросает: падение подшага — красный шаг с текстом ошибки в логе
    и в `error`, проект остаётся в состоянии после уже выполненных подшагов.
    """
    project = Path(project).expanduser().resolve()
    step = str(step or "").strip()
    given = {key: str(value).strip() for key, value in (values or {}).items()
             if value is not None}
    # Печатать в stdout сервера нечего: лог шага уходит в ответ API, поэтому Log тихий.
    log = log if log is not None else Log(quiet=True)

    if step not in SETUP_STEPS:
        return {"ok": False, "step": step, "already": False, "log": log.lines,
                "next": setup_status(project)["next"],
                "error": f"неизвестный шаг мастера: {step}"}

    cfg = proj.read_config(project) if (project / proj.MARKER).is_file() else {}
    already = _step_done(project, step, cfg)
    log.step(f"{SETUP_TITLES[step]}: "
             + ("уже настроено — проверяю заново" if already else "выполняется"))
    # D7: out-словарь — единственный канал для `found` из шага Telegram; глобального
    # состояния нет, форма `(ok, error)` у веток шага остаётся прежней.
    out: dict[str, Any] = {}
    try:
        ok, error = _perform_step(project, step, cfg, given, log, out)
    except Exception as exc:                                # noqa: BLE001 - наружу не бросаем
        log.warn(f"шаг упал: {type(exc).__name__}: {exc}")
        ok, error = False, f"{type(exc).__name__}: {exc}"
    result = {"ok": ok, "step": step, "already": already, "log": log.lines,
              "next": setup_status(project)["next"], "error": error}
    if out.get("found"):
        result["found"] = out["found"]
    return result


def _perform_step(project: Path, step: str, cfg: dict[str, Any], given: dict[str, str],
                  log: Log, out: dict[str, Any]) -> tuple[bool, str]:
    """Тело шага: `(ok, error)`.

    Красный шаг — только три случая (`D` постановки §4.1): `ensure_board` вернул False,
    отказ Telegram на токене или чате, исключение. Предупреждения профилей, доверия и
    cron шаг красным не делают — ровно как в CLI.
    """
    defaults = setup_defaults(project)
    profiles = cfg.get("profiles") or {}
    board = given.get("board") or cfg.get("board") or defaults["board"]
    driver = given.get("driver") or profiles.get("driver") or defaults["driver"]
    prompter = given.get("prompter") or profiles.get("prompter") or defaults["prompter"]
    reviewer = given.get("reviewer") or profiles.get("reviewer") or defaults["reviewer"]
    scout = given.get("scout") or profiles.get("scout") or defaults["scout"]
    if step != "project":
        # Конфиг важнее формы у всех шагов, кроме «Проекта»: имена ролей и борду пишет
        # в `pipeline.json` он же, а факты шагов считаются по конфигу (`_board_fact`,
        # `_profiles_fact`, `_telegram_fact`, `_cron_fact`). Шаг по форме с другими
        # именами давал зелёный шаг и вечный `next` на нём самом.
        board = str(cfg.get("board") or board)
        driver = str(profiles.get("driver") or driver)
        prompter = str(profiles.get("prompter") or prompter)
        reviewer = str(profiles.get("reviewer") or reviewer)
        scout = str(profiles.get("scout") or scout)

    if step == "project":
        # D8: только предмет шага. Уведомления не выключаем, борду не создаём,
        # профили не трогаем — у них свои шаги и свои отказы.
        copied = copy_engine(project, False)
        log.ok(f"движок: файлов {len(ENGINE_FILES)}, версия {write_engine_version(project)}"
               + (f" (обновлено {len(copied)})" if copied else " — уже на месте"))
        log.ok(f"маркер целостности: {write_engine_marker(project)[:12]}")
        sync = copy_roles(project)
        for item in sync.added:
            log.ok(f"добавлена роль {item}")
        for item in ensure_dirs(project):
            log.ok(f"{item}")
        log.ok(".gitignore: "
               + ("дополнен" if ensure_gitignore(project) else "уже содержит записи"))
        fresh = build_config(project, board, driver, prompter, reviewer, cfg, scout=scout)
        notify = dict(fresh.get("notify") or {})
        notify.setdefault("enabled", bool((cfg.get("notify") or {}).get("enabled")))
        fresh["notify"] = notify
        proj.write_config(project, fresh)
        log.ok(f"{proj.MARKER}: записан (борда {board}, роли: {driver}, {prompter}, "
               f"{reviewer}, разведка {scout}; уведомления "
               f"{'вкл' if notify.get('enabled') else 'выкл'})")
        return True, ""

    if step == "board":
        if not ensure_board(board, log):
            return False, _setup_error(log, f"борда {board} не создана")
        log.ok(f"борда {board} готова")
        return True, ""

    if step == "profiles":
        ensure_profiles(driver, prompter, reviewer, log, scout=scout)
        # Неудача доверия одной роли не красит шаг: факт `profiles` останется невыполнен,
        # `next` укажет на этот же шаг, и владелец повторит его после починки профиля.
        trust_roles(list(dict.fromkeys([driver, prompter, reviewer, scout])), project, log)
        return True, ""

    if step == "telegram":
        return _setup_step_telegram(project, cfg, driver, given, log, out)

    if step == "cron":
        # Один разбор конфига на оба факта: `enabled` и осознанный выбор «без бота».
        stored = proj.read_config(project).get("notify") or {}
        ensure_cron(driver, project, log, skip=False)
        ensure_commands_cron(driver, project, log, skip=False,
                             enabled=bool(stored.get("enabled")),
                             skipped=bool(stored.get("skipped")))
        ensure_update_cron(driver, project, log, skip=False)
        return True, ""

    # checks: ничего не меняет — только считает и рассказывает (D10).
    _setup_checks(project, proj.read_config(project), log)
    return True, ""


def _setup_step_telegram(project: Path, cfg: dict[str, Any], driver: str,
                         given: dict[str, str], log: Log,
                         out: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Шаг Telegram: три переменные в `.env`, поиск по `/start`, исход «без бота».

    D6: токен не попадает ни в ответ API, ни в лог — в логе нет ни токена, ни его частей,
    только путь к `.env`. D8: порядок разбора входов — detect, токен формы, снятый
    чекбокс, приглашение заменить токен из `.env`, отказ. Красный шаг — ровно четыре
    случая: нет `pipeline.json`, detect без токена, нет токена, отказ Telegram.
    """
    out = out if out is not None else {}
    # §4.8 п.4: без `pipeline.json` не известен драйвер проекта (`setup_defaults` подставил бы
    # `HERMES_PROFILE` панели), и detect читал бы чужой `.env`. Первая проверка шага.
    if not (project / proj.MARKER).is_file():
        return False, f"сначала шаг Проект: в проекте нет {proj.MARKER}"
    notify = dict(cfg.get("notify") or {})
    token_env = str(notify.get("token_env") or TOKEN_ENV)
    chat_env = str(notify.get("chat_env") or CHAT_ENV)
    owner_env = str(notify.get("owner_env") or OWNER_ENV)
    env = env_values(driver)

    # D7: отдельное действие — только ищет chat id и owner id, на диск ничего не пишет.
    if given.get("detect"):
        token = given.get("token") or env.get(token_env)
        if not token:
            return False, "сначала токен бота"
        try:
            chat, owner = telegram.detect_chat_and_owner(token, wait_seconds=15)
        except telegram.TelegramError as exc:
            log.warn(f"{exc} — напиши боту /start и повтори")
            return False, str(exc) or "Telegram не увидел сообщений боту"
        log.ok(f"нашёл чат {chat}"
               + (f", владелец {owner}" if owner else ", владелец не виден — подставлю chat id"))
        out["found"] = {"chat": chat, "owner": owner}
        return True, ""

    token = given.get("token") or ""
    if not token:
        # D8 п.2 сильнее прежнего токена в `.env`: снятый чекбокс — осознанный отказ, и
        # повторное нажатие «Проверить» на настроенном боте не должно гасить уведомления.
        if given.get("bot") == "0":
            notify["enabled"] = False
            notify["skipped"] = True
            cfg = dict(cfg)
            cfg["notify"] = notify
            proj.write_config(project, cfg)
            log.ok("Telegram не подключён: уведомления и команды выключены — допустимый выбор")
            return True, ""
        if env.get(token_env):
            log.ok(f"токен уже лежит в .env профиля {driver} — чтобы заменить, вставь новый токен")
            return True, ""
        return False, "нужен токен бота"
    chat = given.get("chat") or ""
    if not chat:
        log.warn("токен принят, но не указан chat id — уведомления некуда слать")
        return False, "нужен chat id (chat id или @канал)"
    try:
        me = telegram.get_me(token)
        telegram.validate_chat(token, chat)
    except telegram.TelegramError as exc:
        log.warn(f"токен или чат не приняты: {exc}")
        return False, str(exc) or "Telegram не принял токен или чат"
    log.ok(f"бот @{me.get('username', '?')} на связи")
    # D6: owner из поля формы, иначе chat id — тот же фолбэк, что у `gate.owner_ids`;
    # для группы достоверен только user id, найденный по /start.
    owner = given.get("owner") or chat
    path = write_env(driver, {token_env: token, chat_env: chat, owner_env: owner})
    log.ok(f"токен и чат записаны в {path}")
    notify["enabled"] = True
    notify["skipped"] = False
    notify["token_env"] = token_env
    notify["chat_env"] = chat_env
    notify.setdefault("owner_env", OWNER_ENV)
    cfg = dict(cfg)
    cfg["notify"] = notify
    proj.write_config(project, cfg)
    log.ok("уведомления включены")
    return True, ""


def _setup_checks(project: Path, cfg: dict[str, Any], log: Log) -> None:
    """Отчёт проверок в конце мастера: версия движка, доверие, борда, cron, линтер."""
    version = plugin_version()
    have = engine_version(project)
    if not have:
        log.warn("движок: маркера .pipeline/VERSION нет — установка не завершена")
    elif have != version:
        log.warn(f"движок проекта {have}, плагина {version} — "
                 "обнови: hermes pipeline setup --upgrade")
    else:
        log.ok(f"движок {have}: версия совпадает с плагином")
    roles = roles_from_config(cfg)
    untrusted = [role for role in roles if not is_project_trusted(role, project)]
    if untrusted:
        log.warn(f"проект не доверен профилям: {', '.join(untrusted)}")
    else:
        log.ok(f"проект доверен всем ролям: {', '.join(roles)}")
    if _board_fact(project):
        log.ok(f"борда: база {proj.board_db(project).name} на месте")
    else:
        log.warn(f"борда: базы нет ({proj.board_db(project)})")
    if _cron_fact(project, cfg):
        log.ok("cron: джоба стража заведена")
    else:
        log.warn("cron: метки .pipeline/cron.json нет — страж сам не запустится")
    # D9: выбор «без бота» нейтрален. `warn` — только когда уведомления включены, а
    # переменных в `.env` не хватает: это настоящая поломка, а не выбор владельца.
    notify = cfg.get("notify") or {}
    env = env_values(roles_from_config(cfg)[0])
    token_env = str(notify.get("token_env") or TOKEN_ENV)
    chat_env = str(notify.get("chat_env") or CHAT_ENV)
    if notify.get("skipped"):
        log.ok("telegram: не настроен — установка без бота, это допустимо")
    elif not notify.get("enabled"):
        log.step("telegram: не настроен — шаг Telegram не пройден")
    elif not (env.get(token_env) and env.get(chat_env)):
        log.warn("telegram: выключен — токен и chat id не настроены")
    else:
        log.ok("telegram: включён")
    lint = plan_lint.lint_project(project)
    if lint.problems:
        log.warn(f"линтер планов: проблем {len(lint.problems)} — "
                 "подробности: hermes pipeline doctor")
        for problem in lint.problems[:10]:
            log.warn(str(problem))
    else:
        # Предупреждения видны в итоге: «проблем нет» без их числа соврало бы о состоянии.
        log.ok(f"линтер планов: файлов {len(lint.files)}, проблем нет"
               + (f", предупреждений {len(lint.warnings)}" if lint.warnings else ""))
    # Л20 — предупреждения, а не проблемы: план без строки `Конституция:` в шапке.
    for warning in lint.warnings[:10]:
        log.warn(str(warning))
