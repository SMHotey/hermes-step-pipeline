# -*- coding: utf-8 -*-
"""Мастер установки в панели: шаги, идемпотентность, секрет, предпросмотр, проверки.

Тесты держат границу «мастер ничего не делает сам»: каждый шаг — вызов уже
существующих функций `setup_flow`, а состояние считается по фактам на диске.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402  (загружает плагин как пакет и регистрирует алиасы)
import project as proj  # noqa: E402
import setup_flow  # noqa: E402
import ui_server  # noqa: E402

TOKEN = "123456:AAH-demo-secret"

#: Кривой план из тестов линтера: шаг без критерия готовности и шаг только в журнале.
BROKEN = """# 2. Кривой план

## Шаги

### S1. Шаг без критерия готовности (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""


class FakeHermes:
    """Поддельный `hermes`: профили, борды, доверие и cron — как настоящий, но в tmp.

    `kanban --board <борда> init` действительно создаёт файл базы (`board_db`), иначе
    факт готовности борды в тестах проверялся бы на честном слове.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[tuple] = []
        self.boards: list[str] = []
        self.create_ok = True
        self.trusted: dict[str, list[str]] = {}

    @staticmethod
    def result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def __call__(self, *argv, timeout: int = 180):        # noqa: ARG002 - сигнатура proj
        self.calls.append(tuple(argv))
        args = list(argv)
        if args[:2] == ["profile", "list"]:
            return self.result(stdout=" ".join(["default", "shop", "prompter", "reviewer"]))
        if args[:2] == ["profile", "create"]:
            return self.result()
        if args[:3] == ["kanban", "boards", "list"]:
            return self.result(stdout="\n".join(self.boards))
        if args[:3] == ["kanban", "boards", "create"]:
            if not self.create_ok:
                return self.result(1, "", "ошибка создания борды")
            self.boards.append(args[3])
            return self.result()
        if args[:2] == ["kanban", "--board"] and args[3:4] == ["init"]:
            db = Path(os.environ["PIPELINE_KANBAN_DB"])
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_bytes(b"")
            return self.result()
        if args[0] == "-p" and args[2:4] == ["skills", "trust"]:
            self.trust(args[1], args[4])
            return self.result()
        return self.result()

    def trust(self, role: str, project: str) -> None:
        """`skills trust` пишет путь в config.yaml профиля — как настоящий hermes."""
        entries = self.trusted.setdefault(role, [])
        if project not in entries:
            entries.append(project)
        path = self.home / role / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["skills:", "  trusted_project_dirs:"]
        lines += [f"    - '{item}'" for item in entries]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def stub(monkeypatch, hermes_stub, tmp_path: Path) -> FakeHermes:   # noqa: ARG001
    """Поддельный hermes + изолированный каталог профилей: тесты не трогают профили."""
    home = tmp_path / "profiles"
    home.mkdir()
    monkeypatch.setattr(proj, "profile_dir", lambda profile: home / profile)
    fake = FakeHermes(home)
    monkeypatch.setattr(proj, "run_hermes", fake)
    return fake


def telegram_ok(monkeypatch) -> None:
    """Telegram без сети: тот же бот, тот же чат, без запросов наружу."""
    monkeypatch.setattr(setup_flow.telegram, "get_me", lambda token: {"username": "demo_bot"})
    monkeypatch.setattr(setup_flow.telegram, "validate_chat",
                        lambda token, chat: {"id": chat})


@contextmanager
def running(root: Path, preview: bool = False):
    """Сервер панели на свободном порту: адрес и объект (нужен state для лога)."""
    httpd = ui_server.make_server(root, port=0, preview=preview)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def post_setup(url: str, payload: dict) -> tuple[int, dict]:
    """POST шага мастера: JSON и метка панели, как это делает страница."""
    request = urllib.request.Request(
        f"{url}/api/setup", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Pipeline-Panel": "1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def files_state(root: Path) -> dict[str, int]:
    """Слепок проекта: имя файла → mtime. Шаг «проверки» не имеет права его менять."""
    return {path.relative_to(root).as_posix(): path.stat().st_mtime_ns
            for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts}


def full_wizard(root: Path, monkeypatch) -> None:
    """Провести проект через все шаги настройки — как это сделал бы владелец в панели."""
    telegram_ok(monkeypatch)
    values = {"board": "shop", "driver": "shop", "prompter": "prompter",
              "reviewer": "reviewer"}
    for step in ("project", "board", "profiles"):
        assert setup_flow.run_setup_step(root, step, values)["ok"] is True
    assert setup_flow.run_setup_step(root, "telegram",
                                     {**values, "token": TOKEN, "chat": "42"})["ok"] is True
    assert setup_flow.run_setup_step(root, "cron", values)["ok"] is True


def test_setup_status_starts_from_project_on_empty_dir(hermes_stub,   # noqa: ARG001
                                                       tmp_path: Path):
    """Пустой проект: мастер включён, все шаги не выполнены, следующий — «проект»."""
    root = tmp_path / "bare"
    root.mkdir()
    with running(root) as (url, _httpd):
        status, data = get_json(url + "/api/setup")
    assert status == 200
    assert data["ok"] is True
    assert data["enabled"] is True and data["preview"] is False
    assert data["next"] == "project"
    assert [step["id"] for step in data["steps"]] == list(setup_flow.SETUP_STEPS)
    assert all(step["done"] is False for step in data["steps"])
    assert "token" not in data["values"]          # токена в ответе нет вовсе
    assert data["values"]["telegram_token_set"] is False


def test_project_step_installs_engine_roles_and_config(stub, tmp_path: Path):  # noqa: ARG001
    """Шаг «проект» ставит движок, роли, каталоги и конфиг — следующий шаг «борда»."""
    root = tmp_path / "shop"
    root.mkdir()
    result = setup_flow.run_setup_step(root, "project", {"board": "shop", "driver": "shop"})
    assert result["ok"] is True
    assert result["already"] is False
    assert result["next"] == "board"
    assert (root / ".pipeline" / "gate.py").is_file()
    assert (root / ".pipeline" / "VERSION").is_file()
    assert (root / ".pipeline" / "ENGINE.sha256").is_file()
    assert (root / ".hermes" / "skills" / "software-development" /
            "pipeline-reviewer" / "SKILL.md").is_file()
    assert (root / "docs" / "plans").is_dir()
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["board"] == "shop"
    assert cfg["profiles"] == {"driver": "shop", "prompter": "prompter",
                               "reviewer": "reviewer", "scout": "scout"}
    assert cfg["notify"]["enabled"] is False


def test_repeat_project_step_says_already_configured(stub, tmp_path: Path):  # noqa: ARG001
    """Повторный проход: «уже настроено» в логе, правки владельца в конфиге целы."""
    root = tmp_path / "repeat"
    root.mkdir()
    setup_flow.run_setup_step(root, "project", {"board": "shop", "driver": "shop"})
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["driven_plans"] = ["2"]
    cfg["project"] = "Склад"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    again = setup_flow.run_setup_step(root, "project")
    assert again["ok"] is True
    assert again["already"] is True
    assert any("уже настроено" in line for line in again["log"])
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert kept["driven_plans"] == ["2"]
    assert kept["project"] == "Склад"
    assert kept["board"] == "shop"


def test_board_failure_names_resume_step(stub, tmp_path: Path):   # noqa: ARG001
    """Отказ борды: красный шаг с причиной, следующий шаг не съезжает вперёд."""
    root = tmp_path / "board"
    root.mkdir()
    setup_flow.run_setup_step(root, "project", {"board": "shop", "driver": "shop"})
    stub.create_ok = False

    failed = setup_flow.run_setup_step(root, "board", {"board": "shop"})
    assert failed["ok"] is False
    assert "борда" in failed["error"]
    assert failed["next"] == "board"

    stub.create_ok = True
    second = setup_flow.run_setup_step(root, "board", {"board": "shop"})
    assert second["ok"] is True
    assert second["next"] == "profiles"


def test_profiles_and_cron_steps_call_existing_setup(stub, tmp_path: Path):   # noqa: ARG001
    """Шаг профилей доверяет проект трём ролям; cron-шаг помнит того же драйвера."""
    root = tmp_path / "full"
    root.mkdir()
    setup_flow.run_setup_step(root, "project", {"board": "shop", "driver": "shop"})
    setup_flow.run_setup_step(root, "board", {"board": "shop"})
    profiles = setup_flow.run_setup_step(root, "profiles", {"driver": "shop"})
    assert profiles["ok"] is True
    assert profiles["next"] == "telegram"

    trust_calls = [call for call in stub.calls
                   if call[0] == "-p" and call[2:4] == ("skills", "trust")]
    assert {call[1] for call in trust_calls} == {"shop", "prompter", "reviewer", "scout"}
    assert all(call[4] == str(root) for call in trust_calls)

    # Имена ролей — из конфига, а не из формы: иначе зелёный шаг с чужими именами
    # оставлял бы `next` на `profiles` навсегда (факт считается по pipeline.json).
    other = setup_flow.run_setup_step(root, "profiles", {"driver": "shop2"})
    assert other["ok"] is True
    assert other["next"] == "telegram"
    assert "shop2" not in {call[1] for call in stub.calls if call[0] == "-p"}

    cron = setup_flow.run_setup_step(root, "cron")
    assert cron["ok"] is True
    marker = json.loads((root / ".pipeline" / "cron.json").read_text(encoding="utf-8"))
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert marker["driver"] == cfg["profiles"]["driver"]
    assert cron["next"] == "telegram"      # telegram ещё не настроен: шаг честно ждёт


def test_telegram_step_keeps_token_out_of_responses(stub, monkeypatch,   # noqa: ARG001
                                                    tmp_path: Path):
    """Токен из формы уходит в .env профиля и не возвращается ни в ответ, ни в лог."""
    root = tmp_path / "tg"
    root.mkdir()
    setup_flow.run_setup_step(root, "project", {"board": "shop", "driver": "shop"})
    telegram_ok(monkeypatch)

    with running(root) as (url, httpd):
        status, body = post_setup(url, {"step": "telegram",
                                        "values": {"token": TOKEN, "chat": "42"}})
        tail = "\n".join(httpd.RequestHandlerClass.state.tail())

    assert status == 200, body
    assert body["ok"] is True
    assert "@demo_bot" in "\n".join(body["log"])
    assert TOKEN not in json.dumps(body, ensure_ascii=False)
    assert TOKEN not in tail
    env_text = (stub.home / "shop" / ".env").read_text(encoding="utf-8")
    assert TOKEN in env_text
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is True

    # Состояние мастера после шага секрет тоже не отдаёт — только признак.
    with running(root) as (url, _httpd):
        _status, state = get_json(url + "/api/setup")
    assert state["values"]["telegram_token_set"] is True
    assert TOKEN not in json.dumps(state, ensure_ascii=False)

    # Имя переменной панель берёт из конфига (`notify.token_env`), как и факт шага:
    # при своём имени она не должна врать «токена нет» у закрытого шага.
    cfg["notify"]["token_env"] = "MY_BOT_TOKEN"
    (root / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    (stub.home / "shop" / ".env").write_text("MY_BOT_TOKEN=" + TOKEN + "\n", encoding="utf-8")
    with running(root) as (url, _httpd):
        _status, custom = get_json(url + "/api/setup")
    assert custom["values"]["telegram_token_set"] is True

    # Принудительная утечка: Telegram отклоняет токен и повторяет его в тексте ошибки.
    # Редакция обязана ловить то же (срезанное) значение, что исполнил шаг, иначе токен
    # с хвостовым переводом строки уходит в ответ API и в state.log панели.
    def reject(token: str) -> dict:
        raise setup_flow.telegram.TelegramError("Unauthorized: " + token)

    monkeypatch.setattr(setup_flow.telegram, "get_me", reject)
    with running(root) as (url, httpd):
        code, body = post_setup(url, {"step": "telegram",
                                      "values": {"token": TOKEN + "\n", "chat": "42"}})
        tail = "\n".join(httpd.RequestHandlerClass.state.tail())

    assert code == 400, body
    assert body["ok"] is False
    assert TOKEN not in json.dumps(body, ensure_ascii=False)
    assert TOKEN not in tail
    assert "***" in body["error"] and "***" in tail


def test_preview_hides_master_and_rejects_post(hermes_stub, tmp_path: Path):   # noqa: ARG001
    """Предпросмотр: мастер отвечает «выключен», POST отклоняется и ничего не пишет."""
    root = tmp_path / "preview"
    root.mkdir()
    with running(root, preview=True) as (url, _httpd):
        status, data = get_json(url + "/api/setup")
        code, body = post_setup(url, {"step": "project", "values": {}})
    assert status == 200
    assert data["enabled"] is False and data["preview"] is True
    assert code == 403
    assert "предпросмотр" in body["error"]
    assert not (root / "pipeline.json").exists()


def test_checks_step_reports_lint_problems(stub, monkeypatch, tmp_path: Path):  # noqa: ARG001
    """Проверки: чистый проект — «проблем нет» и ни одной записи; кривой — счёт проблем."""
    root = tmp_path / "clean"
    root.mkdir()
    full_wizard(root, monkeypatch)
    before = files_state(root)
    result = setup_flow.run_setup_step(root, "checks")
    assert result["ok"] is True
    assert result["next"] is None          # все шаги настройки пройдены
    assert "проблем нет" in "\n".join(result["log"])
    assert files_state(root) == before      # шаг только читает

    (root / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")
    dirty = setup_flow.run_setup_step(root, "checks")
    found = re.search(r"проблем (\d+)", "\n".join(dirty["log"]))
    assert found is not None and int(found.group(1)) > 0


def test_panel_has_setup_tab_hidden_in_preview():
    """Страница: вкладка мастера, поле токена типа password и скрытие в предпросмотре."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="setup"' in html
    assert "Установка" in html
    assert 'type="password" id="setup-token"' in html
    render = html.split("function render(data)")[1].split("function refresh(")[0]
    assert "setupTab.hidden = !!data.preview" in render


# ───────────────── S11: бот опционален, владелец из /start, три переменные ─────────────────


def _project_step(root: Path) -> None:
    """Проект с конвейером: без `pipeline.json` шаг Telegram красный по §4.8 п.4."""
    assert setup_flow.run_setup_step(root, "project",
                                     {"board": "shop", "driver": "shop"})["ok"] is True


def test_telegram_step_without_bot_finishes_without_warnings(stub, tmp_path: Path):
    """Снятый чекбокс — допустимый выбор: шаг зелёный, ни одной строки `! ` (D3)."""
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)

    result = setup_flow.run_setup_step(root, "telegram", {"bot": "0"})
    assert result["ok"] is True
    assert not [line for line in result["log"] if line.startswith("  ! ")]
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["skipped"] is True
    assert cfg["notify"]["enabled"] is False
    assert not (stub.home / "shop" / ".env").exists()
    assert result["next"] != "telegram"


def test_telegram_detect_returns_chat_and_owner(stub, monkeypatch, tmp_path: Path):
    """detect отдаёт chat и owner, ничего не пишет; запись — вторым нажатием (D7)."""
    monkeypatch.setattr(setup_flow.telegram, "detect_chat_and_owner",
                        lambda token, wait_seconds=15: ("42", "777"))
    telegram_ok(monkeypatch)
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)

    with running(root) as (url, httpd):
        code, body = post_setup(url, {"step": "telegram",
                                      "values": {"token": TOKEN, "detect": "1"}})
        tail = "\n".join(httpd.RequestHandlerClass.state.tail())
        assert code == 200, body
        assert body["found"] == {"chat": "42", "owner": "777"}
        assert not (stub.home / "shop" / ".env").exists()
        assert TOKEN not in json.dumps(body, ensure_ascii=False)
        assert TOKEN not in tail

        code, body = post_setup(url, {"step": "telegram",
                                      "values": {"token": TOKEN, "chat": "42",
                                                 "owner": "777"}})
        assert code == 200, body

    env = (stub.home / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.TOKEN_ENV}={TOKEN}" in env
    assert f"{setup_flow.CHAT_ENV}=42" in env
    assert f"{setup_flow.OWNER_ENV}=777" in env


def test_telegram_step_writes_owner_env(stub, monkeypatch, tmp_path: Path):
    """Шаг пишет три переменные; имя владельца берётся из конфига (D1, D6)."""
    telegram_ok(monkeypatch)
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)

    result = setup_flow.run_setup_step(root, "telegram",
                                       {"token": TOKEN, "chat": "42", "owner": "777"})
    assert result["ok"] is True
    env = (stub.home / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.OWNER_ENV}=777" in env
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["owner_env"] == setup_flow.OWNER_ENV
    assert cfg["notify"]["skipped"] is False


def test_telegram_step_invites_token_replacement(stub, tmp_path: Path):
    """Токен уже в `.env`: приглашение «заменить», `.env` не переписывается (D5)."""
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)
    setup_flow.write_env("shop", {setup_flow.TOKEN_ENV: TOKEN})
    env_path = stub.home / "shop" / ".env"
    before = env_path.stat().st_mtime_ns

    result = setup_flow.run_setup_step(root, "telegram", {})
    assert result["ok"] is True
    assert "заменить" in "\n".join(result["log"])
    assert not [line for line in result["log"] if line.startswith("  ! ")]
    assert env_path.stat().st_mtime_ns == before


def test_notify_dialog_writes_owner_env(stub, monkeypatch, tmp_path: Path):
    """Диалог «Уведомления» пишет те же три переменные и не понижает владельца (D6)."""
    telegram_ok(monkeypatch)
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)

    state = ui_server.State(root)
    first = ui_server.act_save_telegram(state, {"token": TOKEN, "chat": "42", "owner": "777"})
    assert first["ok"] is True
    env = (stub.home / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.TOKEN_ENV}={TOKEN}" in env
    assert f"{setup_flow.CHAT_ENV}=42" in env
    assert f"{setup_flow.OWNER_ENV}=777" in env
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is True
    assert cfg["notify"]["owner_env"] == setup_flow.OWNER_ENV

    again = ui_server.act_save_telegram(state, {"token": TOKEN, "chat": "42", "owner": ""})
    assert again["ok"] is True
    env = (stub.home / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.OWNER_ENV}=777" in env      # прежний владелец, не chat id


def test_panel_has_bot_toggle_owner_and_detect():
    """Форма мастера: чекбокс бота без `checked`, поле owner, кнопка detect (D2, D6, D7)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="setup-bot"' in html
    assert 'id="setup-owner"' in html
    assert 'id="setup-detect"' in html
    assert 'type="password" id="setup-token"' in html
    load = html.split("async function loadSetup()")[1].split("async function runSetupStep")[0]
    assert '$("setup-bot").checked = !!values.telegram_token_set;' in load
    assert '$("setup-bot").checked ? "1" : "0"' in html
    checkbox = [line for line in html.splitlines() if 'id="setup-bot"' in line][0]
    assert "checked" not in checkbox


def test_skipped_bot_after_connection_closes_step(stub, tmp_path: Path):
    """Снятый чекбокс на настроенном боте гасит уведомления и закрывает шаг (D8 п.2)."""
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["notify"].update({"enabled": True, "skipped": False})
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    setup_flow.write_env("shop", {setup_flow.TOKEN_ENV: TOKEN, setup_flow.CHAT_ENV: "42"})

    result = setup_flow.run_setup_step(root, "telegram", {"bot": "0"})
    assert result["ok"] is True
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is False
    assert cfg["notify"]["skipped"] is True
    assert setup_flow._telegram_fact(cfg) is True
    assert setup_flow.setup_status(root)["next"] != "telegram"


def test_checks_step_with_skipped_bot_is_neutral(stub, tmp_path: Path):
    """Проверки не ругают выбор «без бота»: строка нейтральная, `! ` нет (D9)."""
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["notify"].update({"skipped": True, "enabled": False})
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    result = setup_flow.run_setup_step(root, "checks")
    log = "\n".join(result["log"])
    assert result["ok"] is True
    assert "telegram: не настроен — установка без бота, это допустимо" in log
    assert not [line for line in result["log"]
                if line.startswith("  ! ") and "telegram" in line]


def test_panel_setup_form_has_scout_role():
    """Форма мастера: поле профиля разведки, значение из конфига и отправка шагу."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="setup-scout"' in html
    load = html.split("async function loadSetup()")[1].split("async function runSetupStep")[0]
    assert '["setup-scout", "scout"]' in load
    run = html.split("async function runSetupStep")[1]
    assert 'scout: $("setup-scout").value.trim()' in run


def test_panel_keeps_token_after_detect():
    """Поле токена гасится только после записи: detect и ошибка оставляют его (D7)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    run = html.split("async function runSetupStep")[1].split("// D7")[0]
    assert 'if (data.ok && !data.found) $("setup-token").value = "";' in run
    assert run.count('$("setup-token").value = ""') == 1


def test_repeat_click_on_closed_step_keeps_notify_enabled(stub, tmp_path: Path):
    """Повторное «Проверить» с включённым чекбоксом не гасит настроенного бота (D8)."""
    root = tmp_path / "shop"
    root.mkdir()
    _project_step(root)
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["notify"].update({"enabled": True, "skipped": False})
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    setup_flow.write_env("shop", {setup_flow.TOKEN_ENV: TOKEN, setup_flow.CHAT_ENV: "42"})
    env_path = stub.home / "shop" / ".env"
    before = env_path.stat().st_mtime_ns

    result = setup_flow.run_setup_step(root, "telegram", {"bot": "1"})
    assert result["ok"] is True
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is True
    assert cfg["notify"].get("skipped") is False     # отказ не выставлен — бот остался
    assert env_path.stat().st_mtime_ns == before
    assert "заменить" in "\n".join(result["log"])
    assert not [line for line in result["log"] if line.startswith("  ! ")]
