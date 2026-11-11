# -*- coding: utf-8 -*-
"""CLI плагина: `hermes pipeline setup|status|run|ui|doctor|notify-test|config|plan|plan-mode`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from . import doctor
    from . import plan_accept
    from . import plan_mode
    from . import project as proj
    from . import setup_flow, snapshot as snap, telegram
    from . import stats
    from . import update
except ImportError:  # pragma: no cover - отладка файлом
    import doctor  # type: ignore
    import plan_accept  # type: ignore
    import plan_mode  # type: ignore
    import project as proj  # type: ignore
    import setup_flow  # type: ignore
    import snapshot as snap  # type: ignore
    import telegram  # type: ignore
    import stats  # type: ignore
    import update  # type: ignore


def build_parser(sp: argparse.ArgumentParser) -> None:
    """Вызывается загрузчиком плагинов с уже созданным subparser'ом `pipeline`."""
    sub = sp.add_subparsers(dest="cmd")

    setup = sub.add_parser("setup", help="установить конвейер в проект (спросит токен бота)")
    setup.add_argument("--project", default=".", help="каталог проекта")
    setup.add_argument("--board", default="", help="борда канбана")
    setup.add_argument("--driver", default="", help="профиль исполнителя")
    setup.add_argument("--prompter", default="", help="профиль постановщика")
    setup.add_argument("--reviewer", default="", help="профиль ревьюера")
    setup.add_argument("--scout", default="", help="профиль разведки (готовые решения)")
    setup.add_argument("--telegram-token", default="", help="токен бота (без вопроса)")
    setup.add_argument("--telegram-chat", default="", help="chat id (без вопроса)")
    setup.add_argument("--no-telegram", action="store_true", help="не настраивать уведомления")
    setup.add_argument("--no-test", action="store_true", help="не слать тестовое сообщение")
    setup.add_argument("--no-profiles", action="store_true", help="не создавать профили ролей")
    setup.add_argument("--no-cron", action="store_true", help="не заводить cron-джобу")
    setup.add_argument("--no-trust", action="store_true",
                       help="не оформлять доверие проекту у профилей ролей")
    setup.add_argument("--no-smoke", action="store_true",
                       help="не проверять старт ролей пробной карточкой")
    setup.add_argument("--smoke-timeout", type=int, default=0,
                       help="сколько секунд ждать смоук ролей (0 — умолчание)")
    setup.add_argument("--yes", action="store_true", help="без вопросов, значения по умолчанию")
    setup.add_argument("--force", action="store_true", help="перезаписать движок и pipeline.json")
    setup.add_argument("--upgrade", action="store_true",
                       help="обновить только движок в уже поставленном проекте")
    setup.set_defaults(handler=cmd_setup)

    status = sub.add_parser("status", help="состояние конвейера и решение стража")
    status.add_argument("--project", default="")
    status.add_argument("--plan", default="")
    status.add_argument("--preview", action="store_true",
                        help="читать проект без установки (движок плагина, только чтение)")
    status.add_argument("--board", default="", help="борда для предпросмотра")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)

    run = sub.add_parser("run", help="один тик стража (создать карточку/закрыть шаг)")
    run.add_argument("--project", default="")
    run.add_argument("--plan", default="")
    run.add_argument("--max-actions", type=int, default=None,
                     help="сколько действий за тик (по умолчанию — из pipeline.json)")
    run.set_defaults(handler=cmd_run)

    ui = sub.add_parser("ui", help="локальный UI с визуализацией конвейера")
    ui.add_argument("--project", default="")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--no-open", action="store_true", help="не открывать браузер")
    ui.add_argument("--detach", action="store_true",
                    help="запустить в фоне и выйти (живёт после закрытия терминала)")
    ui.add_argument("--stop", action="store_true", help="остановить фоновый UI")
    ui.add_argument("--preview", action="store_true",
                    help="показать проект без установки конвейера (только чтение)")
    ui.add_argument("--board", default="", help="борда для предпросмотра")
    ui.add_argument("--extra-project", action="append", default=[], metavar="PATH",
                    help="разовый проект в реестре панели (можно несколько раз)")
    ui.set_defaults(handler=cmd_ui)

    doctor = sub.add_parser("doctor", help="проверить установку и окружение")
    doctor.add_argument("--project", default="")
    doctor.add_argument("--plan", default="",
                        help="проверить только один план: номер или путь "
                             "(пусто — все планы проекта)")
    doctor.set_defaults(handler=cmd_doctor)

    notify = sub.add_parser("notify-test", help="отправить тестовое сообщение в Telegram")
    notify.add_argument("--project", default="")
    notify.set_defaults(handler=cmd_notify_test)

    config = sub.add_parser("config", help="прочитать/изменить pipeline.json")
    config.add_argument("--project", default="")
    config.add_argument("--get", default="", help="ключ через точку: paths.plans")
    config.add_argument("--set", nargs=2, metavar=("КЛЮЧ", "ЗНАЧЕНИЕ"), default=None)
    config.set_defaults(handler=cmd_config)

    plan = sub.add_parser("plan", help="черновики и утверждение планов: --list | --check | --accept")
    plan.add_argument("--project", default="")
    plan.add_argument("--list", action="store_true", help="черновики и утверждённые планы")
    plan.add_argument("--check", default="", metavar="N|ПУТЬ", help="проверить черновик")
    plan.add_argument("--accept", default="", metavar="N", help="утвердить план по номеру")
    plan.set_defaults(handler=cmd_plan)

    planmode = sub.add_parser("plan-mode", help="режим планировщика: status | on | off")
    planmode.add_argument("action", nargs="?", default="", help="status | on | off")
    planmode.add_argument("--project", default="")
    planmode.add_argument("--json", action="store_true")
    planmode.set_defaults(handler=cmd_plan_mode)

    upd = sub.add_parser("update", help="проверить GitHub-релиз и обновить плагин")
    upd.add_argument("--check", action="store_true",
                     help="только проверить, ничего не устанавливать")
    upd.add_argument("--json", action="store_true", help="ответ одним объектом JSON")
    upd.add_argument("--timeout", type=float, default=0,
                     help="секунды на запрос к GitHub (0 — умолчание)")
    upd.set_defaults(handler=cmd_update)


def _project(raw: str) -> Path:
    """Проект команды: `--project`, затем проект по умолчанию, затем поиск вверх от cwd.

    Проект по умолчанию — ключ `project_root` из схемы плагина (`config.yaml` Hermes)
    или env `PIPELINE_PROJECT_ROOT`: так `hermes pipeline status` работает из любого
    каталога, а не только из корня репозитория.
    """
    if raw:
        return Path(raw).expanduser().resolve()
    configured = proj.configured_project_root()
    if configured is not None:
        return configured
    return proj.find_project()


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# Допустимые действия проверяются в обработчике, а не через `choices`: иначе argparse
# печатает свою ошибку и свой код 2, а голый `plan-mode` (без действия) вообще не дошёл бы
# до нашей подсказки. Один список — одно место, откуда берутся и проверка, и `usage`.
PLAN_MODE_ACTIONS = ("status", "on", "off")
PLAN_MODE_USAGE = ("usage: hermes pipeline plan-mode status|on|off "
                   "[--project PATH] [--json]")
PLAN_USAGE = ("usage: hermes pipeline plan --list | --check N|ПУТЬ | --accept N "
              "[--project PATH]")


def cmd_setup(args) -> int:
    return setup_flow.run_setup(args)


def cmd_status(args) -> int:
    project = _project(args.project)
    data = snap.snapshot(project, args.plan or None,
                         preview=bool(getattr(args, "preview", False)),
                         board=getattr(args, "board", "") or None)
    if args.json:
        print(_dump(data))
        return 0
    print(render_status(data))
    return 0


def render_status(data: dict) -> str:
    """Человеческий отчёт для терминала."""
    lines = [f"Проект: {data.get('project')}  (борда {data.get('board')})"]
    if data.get("preview"):
        lines.append("Режим: ПРЕДПРОСМОТР — конвейер в проект не установлен, только чтение")
    git = data.get("git") or {}
    if git.get("head") and git.get("head") != "?":
        dirty = git.get("dirty_count") or 0
        lines.append(f"Git: {git.get('branch')} @ {git.get('head')} | "
                     f"{'чисто' if not dirty else f'грязно файлов: {dirty}'}")
    else:
        lines.append("Git: не репозиторий (коммиты страж не делает)")
    for plan in data.get("plans") or []:
        stop = plan.get("stop") or {}
        mark = f"  ⛔ {stop.get('kind')}: {stop.get('reason', '')}" if stop else ""
        lines.append(f"План {plan.get('id')}: закрыто {plan.get('closed')}, "
                     f"следующий {plan.get('next')}{mark}")
    if not data.get("plans"):
        lines.append("Планов нет: скопируй docs/templates/plan-template.md → "
                     "docs/plans/1-<слаг>.md и заполни шаги с журналом.")
    plan = data.get("plan") or {}
    if plan.get("steps"):
        closed = set(plan.get("closed") or [])
        lines.append("")
        lines.append(f"Шаги плана {plan.get('id')}:")
        for step in plan["steps"]:
            state = "ЗАКРЫТ" if step["id"] in closed else (
                "→" if (plan.get("next") or {}).get("id") == step["id"] else "открыт")
            lines.append(f"  {step['id']:<6} {state:<7} {step.get('days') or 0:>4} дн  "
                         f"{step.get('owner') or '':<12} {step.get('title')}")
    decision = data.get("decision") or {}
    reason = decision.get("reason") or decision.get("step") or ""
    lines.append("")
    lines.append(f"Решение: {decision.get('action')} — {reason}")
    cycle = data.get("cycle") or {}
    if cycle:
        lines.append("")
        lines.append(stats.render_cycle(cycle))
    if data.get("status"):
        lines.append("")
        lines.append(data["status"])
    cards = data.get("cards") or []
    if cards:
        lines.append("")
        lines.append("Карточки (последние):")
        for card in cards[:8]:
            lines.append(f"  {card.get('id'):<12} {card.get('status'):<10} "
                         f"{(card.get('assignee') or ''):<12} {str(card.get('title'))[:70]}")
    tg = data.get("telegram") or {}
    lines.append("")
    lines.append(f"Telegram: {'ВКЛ' if tg.get('enabled') else 'выкл'}"
                 f"{'' if tg.get('token_set') else ' (токен не задан)'}"
                 f"  чат: {tg.get('chat') or '—'}")
    for error in data.get("errors") or []:
        lines.append(f"! {error}")
    return "\n".join(lines)


def cmd_run(args) -> int:
    project = _project(args.project)
    extra = ["--plan", args.plan] if args.plan else []
    # Флаг не задан — движок берёт предел из pipeline.json проекта (D2).
    if args.max_actions is not None:
        extra += ["--max-actions", str(args.max_actions)]
    res = proj.run_engine(project, "gate.py", "--run", *extra, timeout=600)
    out = (res.stdout or "").strip()
    print(out or "тик выполнен: конвейер ждёт (шаг в работе, ревью идёт или стоп-флаг)")
    if res.returncode != 0:
        print((res.stderr or "").strip(), file=sys.stderr)
    return res.returncode


def cmd_ui(args) -> int:
    from . import ui_control, ui_server
    project = _project(args.project)
    port = args.port or int(proj.read_config(project).get("ui_port") or 8770)
    preview = bool(getattr(args, "preview", False))
    board = getattr(args, "board", "") or None
    extra = list(getattr(args, "extra_project", []) or [])
    if getattr(args, "stop", False):
        result = ui_control.stop(project)
        print(result.get("detail") or result.get("error"), file=sys.stdout if result["ok"] else sys.stderr)
        return 0 if result["ok"] else 1
    if getattr(args, "detach", False):
        result = ui_control.start(project, port, args.host, preview=preview, board=board,
                                  extra_projects=extra)
        if not result.get("ok"):
            print(f"ошибка: {result.get('error')}", file=sys.stderr)
            return 1
        extra_note = " (уже был запущен)" if result.get("already") else ""
        print(f"UI конвейера: {result['url']}{extra_note}")
        print(f"остановить:  hermes pipeline ui --project \"{project}\" --stop")
        return 0
    try:
        url = ui_server.serve(project, host=args.host, port=port,
                              open_browser=not args.no_open, preview=preview,
                              board=board, extra_projects=extra)
    except RuntimeError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"UI конвейера: {url}  (Ctrl+C — остановить)")
    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nUI остановлен")
    return 0


def cmd_doctor(args) -> int:
    project = _project(args.project)
    report = doctor.collect(project, getattr(args, "plan", "") or "")
    print(doctor.format_report(report))
    return 0 if not report.problems else 1


def cmd_notify_test(args) -> int:
    project = _project(args.project)
    state = snap.telegram_state(project)
    # Канал проекта, а не профиля оболочки: `hermes -p pipeline pipeline notify-test` для
    # проекта другого профиля иначе возьмёт чужой токен и чужой прокси (0.20.1).
    env = proj.engine_env(project)
    token = env.get(state["token_env"], "")
    if not token or not state["chat"]:
        print("Telegram не настроен: нет токена или chat id", file=sys.stderr)
        return 2
    try:
        telegram.send_message(token, state["chat"],
                              telegram.test_message(str(state.get("prefix") or "")), env=env)
    except telegram.TelegramError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    print("тестовое сообщение отправлено")
    return 0


def _get_path(data: dict, key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set_path(data: dict, key: str, value: str) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    node[parts[-1]] = parsed


def cmd_config(args) -> int:
    project = _project(args.project)
    cfg = proj.read_config(project)
    if args.set:
        _set_path(cfg, args.set[0], args.set[1])
        proj.write_config(project, cfg)
        print(f"{args.set[0]} = {_get_path(cfg, args.set[0])}")
        return 0
    if args.get:
        print(_dump(_get_path(cfg, args.get)))
        return 0
    print(_dump(cfg))
    return 0


def cmd_plan(args) -> int:
    """Черновики планов: ровно одно действие из `--list`, `--check`, `--accept`.

    Проверка действий — здесь, а не через `choices`: у трёх действий разные значения, и
    argparse с `choices` напечатал бы свою ошибку вместо подсказки с примером (так же
    сделано для `plan-mode`).
    """
    chosen = sum(1 for value in (getattr(args, "list", False), getattr(args, "check", ""),
                                getattr(args, "accept", "")) if value)
    if chosen != 1:
        print(PLAN_USAGE, file=sys.stderr)
        return 2

    project = _project(args.project)
    if getattr(args, "list", False):
        _print_plan_list(project)
        return 0

    target = getattr(args, "check", "")
    if target:
        try:
            draft = plan_accept.find_draft(project, target)
        except ValueError as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            return 1
        errors = plan_accept.check_draft(project, draft)
        for problem in errors:
            print(f"  ✗ {problem}")
        if errors:
            print(f"ИТОГ: ошибок {len(errors)}")
            return 1
        print("ИТОГ: ошибок нет")
        return 0

    return plan_accept.accept(project, getattr(args, "accept", ""))


def _rel(path: Path, project: Path) -> str:
    """Путь относительно проекта в posix-виде (для заголовков каталогов)."""
    try:
        return Path(path).resolve().relative_to(Path(project).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


def _plan_rows(folder: Path) -> list[tuple[str, str]]:
    """(номер, имя файла) по файлам каталога: номер из имени, файлы без номера — в конце."""
    rows = [(plan_accept.plan_number(path.name) or "—", path.name)
            for path in folder.glob("*.md")]
    rows.sort(key=lambda row: (row[0] == "—", int(row[0]) if row[0].isdigit() else 0, row[1]))
    return rows


def _print_plan_list(project: Path) -> None:
    """Два раздела: черновики из `drafts/` и утверждённые планы каталога планов."""
    drafts = plan_accept.drafts_dir(project)
    if not drafts.is_dir():
        print(f"Черновики: нет (нет каталога {_rel(drafts, project)})")
    else:
        rows = _plan_rows(drafts)
        if not rows:
            print("Черновики: нет")
        else:
            print(f"Черновики ({_rel(drafts, project)}):")
            for number, name in rows:
                print(f"  {number}  {name}")

    approved = plan_accept.plans_dir(project)
    rows = _plan_rows(approved) if approved.is_dir() else []
    if not rows:
        print("Утверждённые планы: нет")
    else:
        print(f"Утверждённые планы ({_rel(approved, project)}):")
        for number, name in rows:
            print(f"  {number}  {name}")


def cmd_update(args) -> int:
    """Проверить GitHub-релиз и обновить плагин.

    `--check` — только отчёт (код 0 и когда обновление есть: это не ошибка). Сбой сети
    или архива — код 1 и текст причины: «обновлений нет» при недоступном GitHub было бы
    ложью, из-за которой обновление не приезжало бы никогда.
    """
    timeout = float(getattr(args, "timeout", 0) or 0) or update.DEFAULT_TIMEOUT
    as_json = bool(getattr(args, "json", False))
    info = update.check(timeout=timeout)
    if info["error"]:
        if as_json:
            print(_dump(info))
        print(f"ошибка проверки обновлений: {info['error']}", file=sys.stderr)
        return 1
    current, latest = info["current"], info["latest"]
    if not info["update_available"]:
        if as_json:
            print(_dump(info))
        elif latest:
            print(f"обновлений нет: установлена v{current}, на GitHub v{latest}")
        else:
            # Пустой latest — не «равные версии»: у репозитория нет выпусков, либо он
            # приватный и проверка без токена получила 404. Второй случай молчанием
            # выглядел бы как «всё актуально».
            print(f"обновлений нет: установлена v{current}; выпусков на GitHub не видно "
                  f"(приватный репозиторий? нужен GITHUB_TOKEN)")
        return 0
    if getattr(args, "check", False):
        if as_json:
            print(_dump(info))
        else:
            print(f"доступно обновление: v{current} → v{latest} ({info['source']})")
            print("поставить: hermes pipeline update")
        return 0
    print(f"обновляю плагин: v{current} → v{latest}…")
    try:
        result = update.install(tag=info["tag"], timeout=timeout)
    except update.UpdateError as exc:
        print(f"ошибка обновления: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(_dump(result))
        return 0
    print(result.get("message") or "готово")
    if result.get("backup"):
        print(f"прежняя копия: {result['backup']}")
    if result.get("restart"):
        print(result["restart"])
    return 0


def cmd_plan_mode(args) -> int:
    """Прочитать или переключить режим планировщика проекта.

    Действие приходит позиционным аргументом без `choices`, поэтому проверка и подсказка
    живут здесь, в одном месте: неизвестное действие — код 2, а не исключение. Исключение
    `run()` превратил бы в `ошибка: ...` с кодом 1, смешав опечатку в команде со сбоем
    записи состояния.
    """
    action = (getattr(args, "action", "") or "").strip().lower()
    if action not in PLAN_MODE_ACTIONS:
        print(PLAN_MODE_USAGE, file=sys.stderr)
        return 2

    project = _project(args.project)
    path = plan_mode.state_file(project)
    as_json = bool(getattr(args, "json", False))

    if action == "status":
        label = "on" if plan_mode.read_mode(project) else "off"
        if as_json:
            print(_dump({"mode": label, "file": str(path)}))
        else:
            print(f"Режим планировщика: {label}")
            print(f"Файл состояния: {path}")
        return 0

    on = action == "on"
    label = "on" if on else "off"
    before = plan_mode.read_mode(project)
    if before == on:
        # Файл не трогаем: повторный вызов не должен перезаписывать `changed_at`, иначе
        # дата последнего настоящего переключения теряется, а «изменилось» становится ложью.
        if as_json:
            print(_dump({"mode": label, "file": str(path), "changed": False}))
        else:
            state = "уже включён" if on else "уже выключен"
            print(f"Режим планировщика: {label} (без изменений: {state})")
            print(f"Файл состояния: {path}")
        return 0

    payload = plan_mode.write_mode(project, on)
    if as_json:
        print(_dump({"mode": label, "file": str(path), "changed": True,
                     "changed_at": payload["changed_at"]}))
    else:
        was = "on" if before else "off"
        print(f"Режим планировщика: {label} (было {was})")
        print(f"Файл состояния: {path}")
    return 0


def run(args) -> int:
    """Обработчик, который вызывает `hermes pipeline ...`."""
    proj.setup_stdio()
    handler = getattr(args, "handler", None)
    if handler is None:
        print("команды: setup | status | run | ui | doctor | notify-test | config | plan | "
              "plan-mode | update")
        return 0
    try:
        return handler(args)
    except proj.ProjectNotFound as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:                                     # noqa: BLE001
        print(f"ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
