# -*- coding: utf-8 -*-
"""Раскатка движка сразу после замены плагина (карточка 6-S9).

Симптом был такой: `hermes pipeline update` заменил пакет, копии `.pipeline/` в проектах
реестра остались от ПРЕЖНЕГО плагина, и `doctor` печатал «копия расходится с плагином при
равной версии». Лечение — порядок действий, а не ослабление сверки: `update` (и cron
автообновления) после успешной замены пакета раскатывают движок по реестру тем же проходом,
что панель (`rollout.run_engine`), и печатают итог по проектам.

Проверяется то, что ломается молча: что раскатка вообще заказывается после замены пакета,
что итог виден строками по проектам, что пустой реестр — не ошибка, что занятую раскатку
второй раз не начинают и что отказ одного проекта не отменяет остальные.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)

import cli
from step_pipeline import project as proj
from step_pipeline import rollout, setup_flow, update

VERSION = ".pipeline/VERSION"


# ───────────────────────────── помощники ─────────────────────────────

def write_registry(roots: list[Path]) -> None:
    """Реестр профиля в форме панели: `id` и `name` из имени каталога."""
    entries = [{"id": root.name, "name": root.name, "root": str(root)} for root in roots]
    path = proj.projects_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def registry() -> dict:
    path = proj.projects_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def upgrade_spy(monkeypatch) -> list[Path]:
    """Подмена `rollout._upgrade`: настоящая раскатка здесь не нужна (как в S8A).

    Вызовы копятся списком, а версия в проекте меняется на 9.9.9 — по ней видно, что
    движок проекта после нашего шага уже новый.
    """
    calls: list[Path] = []

    def fake_upgrade(project, log):
        calls.append(Path(project))
        (Path(project) / VERSION).write_text("9.9.9\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(rollout, "_upgrade", fake_upgrade)
    return calls


def fake_hermes(monkeypatch) -> None:
    """Поддельный `hermes`: доверие и cron в живом профиле не трогаются."""

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(proj, "run_hermes", lambda *argv, timeout=180: Result())


def check_available(monkeypatch, current: str = "0.31.0", latest: str = "0.32.0") -> None:
    monkeypatch.setattr(update, "check", lambda **_: {
        "current": current, "latest": latest, "tag": f"v{latest}", "source": "release",
        "update_available": True, "error": "", "checked_at": "2026-11-17T00:00:00+00:00"})


def install_updated(monkeypatch) -> None:
    monkeypatch.setattr(update, "install", lambda **_kwargs: {
        "ok": True, "updated": True, "from": "0.31.0", "to": "0.32.0",
        "message": "плагин обновлён: v0.31.0 → v0.32.0", "backup": "/tmp/backup",
        "restart": "перезапусти сессию Hermes; движок в проектах раскатывается следом"})


# ─────────────────────── раскатка как шаг обновления ───────────────────────

def test_after_update_rolls_engine_over_registry(project: Path, monkeypatch):
    """После замены плагина движок проектов обновляется и итог виден строками по проектам."""
    calls = upgrade_spy(monkeypatch)
    fake_hermes(monkeypatch)
    (project / VERSION).write_text("0.31.0\n", encoding="utf-8")
    write_registry([project])

    report = rollout.after_update(interval=0.01, limit=1.0)

    assert report["ok"] is True and report["running"] is False
    assert [path.name for path in calls] == [project.name]
    assert (project / VERSION).read_text(encoding="utf-8").strip() == "9.9.9"
    # Итог по проектам: заголовок со счётом и строка проекта с «было → стало».
    assert report["lines"][0].startswith(rollout.AFTER_UPDATE_HEADER)
    assert "обновлено: 1" in report["lines"][0]
    assert report["lines"][1].strip() == f"{project.name}: обновлено 0.31.0 → 9.9.9"
    # Итог лёг в реестр профиля тем же путём, что у панели: история раскатки одна.
    entry = registry()["rollout"]["engine"]
    assert entry["ok"] is True and entry["running"] is False
    assert entry["projects"][0]["status"] == rollout.STATUS_UPDATED


def test_after_update_with_empty_registry_is_not_an_error(monkeypatch):
    """Пустой реестр — не сбой: назвать «раскатывать нечего» честнее, чем молчать."""
    calls = upgrade_spy(monkeypatch)
    write_registry([])

    report = rollout.after_update()

    assert report["ok"] is True and report["projects"] == []
    assert rollout.NO_PROJECTS_NOTE in report["lines"][0]
    assert calls == []


def test_after_update_does_not_start_second_pass(project: Path, monkeypatch):
    """Идущую раскатку второй раз не начинают: её запись реестра не затирается (D5)."""
    calls = upgrade_spy(monkeypatch)
    write_registry([project])
    assert rollout._LOCK.acquire(blocking=False)
    try:
        report = rollout.after_update()
    finally:
        rollout._LOCK.release()

    assert report["ok"] is False and report["busy"] is True
    assert rollout.BUSY_NOTE in report["lines"][0]
    assert calls == []
    assert "rollout" not in registry()      # запись не тронута вовсе


def test_after_update_reads_and_writes_the_given_registry(project: Path, tmp_path: Path,
                                                         monkeypatch):
    """Реестр, названный вызывающим, — и источник строк, и место записи итога.

    Так ходит cron-скрипт автообновления: у джобы свой `HERMES_HOME` (профиль), а панель
    может работать с реестром хоста. Итог в чужом файле означал бы «раскатки не было».
    """
    upgrade_spy(monkeypatch)
    fake_hermes(monkeypatch)
    write_registry([])                      # реестр профиля пуст — он тут не при чём
    own = tmp_path / "host" / "projects.json"
    own.parent.mkdir()
    own.write_text(json.dumps({"projects": [{"id": project.name, "name": project.name,
                                             "root": str(project)}]},
                              ensure_ascii=False), encoding="utf-8")

    report = rollout.after_update(own, interval=0.01, limit=1.0)

    assert report["ok"] is True
    assert f"{project.name}: обновлено" in report["lines"][1]
    saved = json.loads(own.read_text(encoding="utf-8"))
    assert saved["rollout"]["engine"]["projects"][0]["status"] == rollout.STATUS_UPDATED
    assert "rollout" not in registry()      # реестр профиля остался чистым


def test_cli_update_rolls_engine_and_prints_it(project: Path, monkeypatch, capsys):
    """`hermes pipeline update`: после установки печатает итог раскатки по проектам."""
    upgrade_spy(monkeypatch)
    fake_hermes(monkeypatch)
    write_registry([project])
    check_available(monkeypatch)
    install_updated(monkeypatch)

    assert cli.cmd_update(Namespace(check=False, json=False, timeout=0)) == 0

    out = capsys.readouterr().out
    assert "плагин обновлён: v0.31.0 → v0.32.0" in out
    assert rollout.AFTER_UPDATE_HEADER in out
    assert f"{project.name}: обновлено" in out


def test_cli_update_fails_when_rollout_failed(project: Path, monkeypatch, capsys):
    """Плагин обновился, а движок в проекте — нет: код 1, причина в строке проекта.

    Молчаливый 0 здесь означал бы «всё хорошо» при красном `doctor` — ровно тот дефект,
    из-за которого карточка и появилась.
    """
    def broken_upgrade(project, log):
        return 2

    monkeypatch.setattr(rollout, "_upgrade", broken_upgrade)
    fake_hermes(monkeypatch)
    write_registry([project])
    check_available(monkeypatch)
    install_updated(monkeypatch)

    assert cli.cmd_update(Namespace(check=False, json=False, timeout=0)) == 1

    out = capsys.readouterr().out
    assert "ошибка: 1" in out                       # счёт строки «ошибка» в заголовке
    assert "upgrade вернул 2" in out


def test_cli_update_json_carries_rollout(project: Path, monkeypatch, capsys):
    """`update --json` остаётся разбираемым: итог раскатки — ключом, а не строками."""
    upgrade_spy(monkeypatch)
    fake_hermes(monkeypatch)
    write_registry([project])
    check_available(monkeypatch)
    install_updated(monkeypatch)

    assert cli.cmd_update(Namespace(check=False, json=True, timeout=0)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["to"] == "0.32.0"
    assert payload["rollout"]["ok"] is True
    assert payload["rollout"]["projects"][0]["status"] == rollout.STATUS_UPDATED


def test_cli_does_not_roll_out_when_plugin_already_current(monkeypatch, capsys):
    """«Уже актуально» ничего не сдвинуло — раскатывать по реестру нечего."""
    check_available(monkeypatch)
    monkeypatch.setattr(update, "install", lambda **_kwargs: {
        "ok": True, "updated": False, "from": "0.32.0", "to": "0.32.0",
        "message": "уже актуально: v0.32.0", "error": ""})

    def forbidden(*_args, **_kwargs):                            # pragma: no cover
        raise AssertionError("без замены пакета раскатка не заказывается")

    monkeypatch.setattr(rollout, "after_update", forbidden)
    assert cli.cmd_update(Namespace(check=False, json=False, timeout=0)) == 0
    assert "уже актуально: v0.32.0" in capsys.readouterr().out


# ───────────────────── cron автообновления: тот же порядок ─────────────────────

def fake_plugin(home: Path, *, rollout_ok: bool = True) -> Path:
    """Плагин-подделка: `update.auto` говорит «обновил», `rollout.after_update` — итог.

    Скрипт грузит раскатку пакетом (`step_pipeline.rollout`), поэтому каталог обязан быть
    пакетом — с `__init__.py`, как настоящий. `project.read_projects` возвращает одну
    строку с путём, который ему передали: так видно, что скрипт читает РЕЕСТР, записанный
    мастером установки, а не реестр профиля из `HERMES_HOME` джобы.
    """
    package = home / "shop" / "plugins" / "step_pipeline"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "update.py").write_text(
        "def auto():\n"
        "    return {'updated': True, 'message': 'плагин обновлён: v0.31.0 → v0.32.0',\n"
        "            'restart': 'перезапусти сессию Hermes'}\n",
        encoding="utf-8")
    (package / "rollout.py").write_text(
        "def after_update(registry=None):\n"
        f"    return {{'ok': {rollout_ok!r},\n"
        "             'lines': ['раскатка: обновлено: 1',\n"
        "                       '  shop: обновлено 0.31.0 → 0.32.0',\n"
        "                       '  реестр: ' + str(registry)]}\n",
        encoding="utf-8")
    return package


def make_script(tmp_path: Path, monkeypatch, *, rollout_ok: bool = True) -> tuple[Path, Path]:
    """Скрипт автообновления, положенный настоящим `install_update_script`.

    Профиль изолирован: `profile_dir` смотрит в tmp, поэтому скрипт и поддельный плагин
    встречаются там, где их ищет `find_plugin`, а живой Hermes не трогается.
    """
    home = tmp_path / "h"
    monkeypatch.setattr(proj, "profile_dir", lambda profile: home / profile)
    fake_plugin(home, rollout_ok=rollout_ok)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"id": "shop", "name": "shop",
                                                  "root": str(tmp_path / "shop")}]},
                                   ensure_ascii=False), encoding="utf-8")
    script = setup_flow.install_update_script("shop", registry)
    return script, registry


def run_script(script: Path) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(script.parent))


def test_cron_script_rolls_engine_after_successful_update(tmp_path: Path, monkeypatch):
    """Cron автообновления: сначала замена пакета, потом раскатка движка по реестру."""
    script, registry = make_script(tmp_path, monkeypatch)

    done = run_script(script)

    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "плагин обновлён: v0.31.0 → v0.32.0" in out
    assert "раскатка: обновлено: 1" in out and "shop: обновлено 0.31.0 → 0.32.0" in out
    # Порядок шагов — тот же, что в `rollout.STEPS`: движок после плагина, не до.
    assert out.index("плагин обновлён") < out.index("раскатка: обновлено")
    # Реестр — тот, что записан при установке: у cron `HERMES_HOME` свой, у панели свой.
    assert f"реестр: {registry}" in out


def test_cron_script_reports_rollout_failure(tmp_path: Path, monkeypatch):
    """Раскатка не удалась — cron-джоба отвечает кодом 1, а не «всё прошло»."""
    script, _registry = make_script(tmp_path, monkeypatch, rollout_ok=False)

    done = run_script(script)

    assert done.returncode == 1
    assert "раскатка: обновлено: 1" in done.stdout
