# -*- coding: utf-8 -*-
"""Диагностика конвейера без печати: CLI `doctor` и панель «Проверки» — один код.

`collect()` собирает замечания и проблемы, `format_report()` делает из них текст
команды `hermes pipeline doctor`. Печатает вызывающий: панель берёт те же данные
JSON'ом, поэтому проверки в браузере не могут разойтись с терминалом.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    from . import plan_lint
    from . import project as proj
    from . import setup_flow, snapshot as snap, telegram
except ImportError:  # pragma: no cover - отладка файлом
    import plan_lint  # type: ignore
    import project as proj  # type: ignore
    import setup_flow  # type: ignore
    import snapshot as snap  # type: ignore
    import telegram  # type: ignore

try:  # требования стража: движок на stdlib, состояние проекта не читает
    from . import plan_accept
except ImportError:  # pragma: no cover - плагина рядом нет: P4 молча пропускается
    plan_accept = None  # type: ignore

try:  # P1-P4 отдельным блоком: падение одного импорта не уносит остальные проверки
    from .engine import plan_cycle
except ImportError:  # pragma: no cover - движка рядом нет: строки P1-P4 не печатаются
    plan_cycle = None  # type: ignore

try:  # A6-A8 (план 7, S5) — свой блок по той же причине
    from .engine import analyze
except ImportError:  # pragma: no cover - движка рядом нет: A6-A8 не проверяются
    analyze = None  # type: ignore


@dataclass
class Report:
    """Итог диагностики: что в порядке (notes), что сломано (problems) и что заметить (warnings).

    `warnings` — не блокирующие находки линтера (Л20): на код возврата `doctor` и на
    `ИТОГ` они не влияют, но человеку видны — иначе предупреждение не прочитает никто.
    """

    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lint: plan_lint.LintResult | None = None   # None — линтер не запускали


CYCLE_RESEARCH_DEFAULT = "docs/research"

ANALYZE_UNAVAILABLE = ("A6–A8 не проверены: анализатор недоступен "
                       "(нет модуля analyze или конфиг проекта)")


def _research_dir(project: Path) -> str:
    """Каталог разведки путём от проекта — как `gate.scout_research_dir` (gate.py:617-627)."""
    cfg = proj.read_config(Path(project))
    raw = str((cfg.get("paths") or {}).get("research") or CYCLE_RESEARCH_DEFAULT).strip()
    path = Path(raw or CYCLE_RESEARCH_DEFAULT)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(Path(project).resolve()).as_posix()
        except (ValueError, OSError):
            return CYCLE_RESEARCH_DEFAULT
    return path.as_posix().strip("/") or CYCLE_RESEARCH_DEFAULT


def plan_cycle_rows(project: Path, path: Path) -> list[tuple[str, str, str]] | None:
    """Требования стража P1-P4 для файла плана; `None` — проверять нечем.

    Номер плана берётся из имени файла (`plan_accept.plan_number`): заголовок `# План 7. …`
    шаблон `PLAN_ID_RE` не распознаёт, и P4 молча пропустил бы проверку ссылки на артефакт.
    Функция только читает файл: утверждение, состояние и каталоги проекта не читаются.
    """
    if plan_cycle is None or plan_accept is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return plan_cycle.check_plan(text, plan_accept.plan_number(Path(path).name),
                                 _research_dir(project))


def analyze_rows(project: Path, path: Path) -> list[str] | None:
    """Находки A6-A8 анализатора для файла плана; `None` — проверять нечем."""
    if analyze is None:
        return None
    try:
        findings = analyze.analyze(Path(path), Path(project))
    except (OSError, RuntimeError):      # RuntimeError — ConfigError движка (config.py:30)
        return None
    try:
        plan_rel = Path(path).resolve().relative_to(Path(project).resolve()).as_posix()
    except (OSError, ValueError):
        plan_rel = Path(path).as_posix()
    return [finding.render(plan_rel) for finding in findings
            if finding.code in ("A6", "A7", "A8")]


def collect(project: Path, plan: str = "", *, with_lint: bool = True,
            preview: bool = False, board: str = "") -> Report:
    """Проверить установку конвейера: движок, борда, доверие, ключи, cron, Telegram.

    *with_lint=False* — структурный линтер планов не запускать: панель зовёт его
    отдельно и показывает находки ссылками на файл и строку.
    *preview* — проект без установленного конвейера: файлы только читаются.
    """
    project = Path(project)
    report = Report()
    problems = report.problems
    notes = report.notes
    cfg = proj.read_config(project)
    notes.append(f"проект {project}")
    board_name = cfg.get("board") or board
    if board_name:
        notes.append(f"борда {board_name}")
    else:
        # Явный вид вместо пустой строки «борда » (стилистическая находка ревью 3-S5).
        notes.append("борда не задана")
    try:
        proj.engine(project, "gate.py")
        notes.append("движок .pipeline/gate.py на месте")
    except proj.ProjectNotFound as exc:
        problems.append(str(exc))
    plans = sorted((project / "docs" / "plans").glob("*.md"))
    notes.append(f"файлов планов: {len(plans)}")
    if with_lint:
        # Линтер формата: неверный план должен быть виден до запуска, а не через сутки простоя.
        lint = plan_lint.lint_project(project, plan)
        report.lint = lint
        if lint.problems:
            for problem in lint.problems:
                problems.append(f"план {problem}")
        elif plan:
            notes.append(f"планы: линтер проверил только план «{plan}», проблем нет")
        else:
            notes.append(f"планы: линтер — файлов {len(lint.files)}, проблем нет")
        # Предупреждения — свой канал: план без строки `Конституция:` (Л20) утверждение
        # не блокирует, но знать о нём надо (шаг 5-S9).
        for problem in lint.warnings:
            report.warnings.append(f"план {problem}")
        # Требования стража P1-P4: ровно те, по которым gate.plan_checks заворачивает план
        # до ревью (gate.py:2764-2771). Печатаются только для адресного плана: панель считает
        # свой «критично» и зовёт collect с with_lint=False, а legacy-планы каталога — не повод.
        if plan and lint.files:
            rows = plan_cycle_rows(project, Path(lint.files[0]))
            if rows is None:
                report.warnings.append("P1–P4 не проверены: модуль plan_cycle не найден")
            else:
                for level, code, message in rows:
                    (notes if level == "PASS" else problems).append(f"план {code}: {message}")
            findings = analyze_rows(project, Path(lint.files[0]))
            if findings is None:
                report.warnings.append(ANALYZE_UNAVAILABLE)
            else:
                for finding in findings:
                    report.warnings.append(f"план {finding}")
    engine_ver = setup_flow.engine_version(project)
    plugin_ver = setup_flow.plugin_version()
    if engine_ver and engine_ver != plugin_ver:
        problems.append(f"движок {engine_ver} старее плагина {plugin_ver} — "
                        f"hermes pipeline setup --upgrade")
    elif engine_ver:
        notes.append(f"версия движка: {engine_ver}")
    elif (project / ".pipeline").is_dir():
        notes.append("у движка нет маркера версии — поставь через setup")
    # Дрейф копии движка (S3 0.23.0): версия могла совпасть, а содержимое — нет.
    # Проверяем после версии, чтобы не писать два раза про одно и то же.
    if (project / ".pipeline" / "gate.py").is_file():
        broken = setup_flow.engine_marker_problem(project)
        if broken:
            problems.append(f"движок: {broken} — hermes pipeline setup --upgrade")
        elif engine_ver and engine_ver == plugin_ver:
            drift = setup_flow.engine_drift(project)
            if drift:
                shown = ", ".join(drift[:5]) + (f" и ещё {len(drift) - 5}" if len(drift) > 5 else "")
                problems.append(f"движок: копия расходится с плагином при равной версии "
                                f"({shown}) — hermes pipeline setup --upgrade")
            else:
                notes.append("движок: копия совпадает с плагином")
        # Лишние `.py` в каталоге движка — не дрейф и не в хеше (S7 0.27.0): вспомогательные
        # скрипты проекта законны. Строка остаётся предупреждением, чтобы мусор был виден,
        # но не поднимала код возврата и не запрещала обновление.
        for name in setup_flow.engine_extra(project):
            report.warnings.append(f"в каталоге движка лежит не-движковый файл: {name}")
    if not plans:
        notes.append("! ни одного плана — конвейеру нечего вести")
    db = proj.board_db(project, board or None)
    notes.append(f"борда БД: {db} ({'есть' if db.is_file() else 'НЕТ'})")
    if not db.is_file():
        problems.append("нет sqlite борды — карточки не создадутся (проверь --board)")
    roles = setup_flow.roles_from_config(cfg)
    driver = roles[0]
    # Доверие проверяем чтением конфига профиля: `skills trust` с путём ПИШЕТ в него.
    untrusted = [role for role in roles if not setup_flow.is_project_trusted(role, project)]
    for role in untrusted:
        problems.append(f'доверие ({role}): проект не доверен этому профилю — '
                        f'hermes -p {role} skills trust "{project}"')
    if not untrusted:
        notes.append(f"доверие: проект доверен всем ролям ({', '.join(roles)})")
    # Ключ должен быть сохранён в профиле: под демоном шлюза запись `env:*` выпадает
    # из ротации (`No usable credentials found for provider`), а в интерактивной
    # сессии та же запись работает — зелёная установка с мёртвым конвейером.
    no_key: list[tuple[str, str]] = []
    for role in roles:
        saved, provider = setup_flow.has_saved_credential(role)
        if not saved:
            no_key.append((role, provider))
    for role, provider in no_key:
        if provider:
            problems.append(f"ключ ({role}): в профиле нет сохранённого ключа провайдера "
                            f"{provider} — hermes -p {role} auth add {provider} --type api-key")
        else:
            problems.append(f"ключ ({role}): в профиле нет сохранённого ключа — "
                            f"провайдер не определён, hermes -p {role} auth list")
    if not no_key:
        notes.append(f"ключи: сохранённый ключ провайдера есть у всех ролей "
                     f"({', '.join(roles)})")
    # Канал проекта — из `.env` его профиля и с его прокси: чужой `STEP_PIPELINE_TELEGRAM_*`
    # из окружения оболочки (например, профиля Hermes) проверку бы испортил (0.20.1).
    values = proj.read_env_file(proj.profile_dir(driver) / ".env")
    notify = proj.read_config(project).get("notify") or {}
    token = values.get(str(notify.get("token_env") or setup_flow.TOKEN_ENV), "")
    chat = values.get(str(notify.get("chat_env") or setup_flow.CHAT_ENV), "")
    if token and chat:
        try:
            me = telegram.get_me(token, env=proj.engine_env(project))
            notes.append(f"Telegram: @{me.get('username')} → {chat}")
        except telegram.TelegramError as exc:
            # Текст отказа идёт в панель: токен в него не попадает (telegram.py:22-77).
            problems.append(f"Telegram: {exc}")
    else:
        notes.append("Telegram: не настроен")
    marker = project / ".pipeline" / "cron.json"
    job_name = "План: страж шага (конвейер)"
    if marker.is_file():
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            job_name = info.get("name") or job_name
            driver = info.get("driver") or driver
        except (OSError, json.JSONDecodeError):
            pass
        # Список джоб спрашиваем только когда маркер есть: у проекта без конвейера
        # cron-джобы быть не может, а вызов тянет `hermes` целиком (секунды в панели).
        cron = proj.run_hermes("-p", driver, "cron", "list")
        if job_name in (cron.stdout or ""):
            notes.append(f"cron-джоба стража: есть ({job_name}, профиль {driver})")
        else:
            problems.append(f"cron-джоба «{job_name}» не найдена у профиля {driver} — "
                            f"конвейер не будет двигаться сам")
    else:
        # Отсутствие cron — не поломка: `setup --no-cron` его и не заводит, конвейер
        # тогда идёт по кнопке «Тик». В проблемы это писать нельзя, иначе владелец,
        # осознанно выбравший ручной режим, видит красное «критично» на пустом месте.
        report.warnings.append("cron для этого проекта не заводился — "
                               "конвейер работает только по кнопке «Тик»")
    status = snap.snapshot(project, preview=preview)
    decision = status.get("decision") or {}
    notes.append(f"решение стража: {decision.get('action')} — {decision.get('reason')}")
    return report


def format_report(report: Report) -> str:
    """Текст команды `doctor`: замечания «·», проблемы «✗», предупреждения «!», итог.

    Итог и код возврата смотрят только на `problems`: предупреждения (Л20) не превращают
    установку в «ЕСТЬ ПРОБЛЕМЫ» — legacy-план не поломка (решение D1 шага S1).
    """
    lines = ["Диагностика конвейера:"]
    lines.extend(f"  · {note}" for note in report.notes)
    lines.extend(f"  ✗ {problem}" for problem in report.problems)
    if report.warnings:
        lines.append("  предупреждения:")
        lines.extend(f"  ! {warning}" for warning in report.warnings)
    lines.append("")
    lines.append("ИТОГ: " + ("OK" if not report.problems else "ЕСТЬ ПРОБЛЕМЫ"))
    return "\n".join(lines)
