# -*- coding: utf-8 -*-
"""Статус раскатки движка: read-model реестра и его строка на странице панели (S8B).

Проверяется то, чего нет у S8A: по строке на каждый проект реестра, различимость
`skipped` и `failed`, блокировка кнопки, пока раскатка идёт, и отличие «не запускалась»
от «закончилась ничем». Ни установка, ни раскатка в тестах не запускается: реестр
пишется литеральным JSON, а страница проверяется своим текстом и живым ответом сервера.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helpers import install_project
from step_pipeline import project as proj
from step_pipeline import rollout, snapshot, ui_server

PAGE = Path(__file__).resolve().parents[1] / "step_pipeline" / "ui" / "index.html"
STAMP = "14.11.2026 12:00:00"
FINISH = "14.11.2026 12:05:00"


# ───────────────────────────── помощники ─────────────────────────────

@pytest.fixture(autouse=True)
def clean_cache():
    """Кэш снимка — состояние процесса: между тестами его обнуляем."""
    snapshot.clear_cache()
    yield
    snapshot.clear_cache()


def write_registry(roots: list[Path], rollout_record: dict | None = None) -> None:
    """Реестр профиля в форме панели: `id` и `name` из имени каталога."""
    path = proj.projects_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": [{"id": root.name, "name": root.name, "root": str(root)}
                            for root in roots],
               "rollout": rollout_record or {}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def registry() -> dict:
    """Файл реестра как он лежит на диске; файла нет — пустой словарь."""
    try:
        return json.loads(proj.projects_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def engine_record(rows: list[dict], **kwargs) -> dict:
    """Запись шага `rollout.engine` в форме `run_engine` (S8A)."""
    record = {"step": rollout.ENGINE_STEP, "running": False, "at": STAMP,
              "finished": FINISH, "ok": True, "error": "", "projects": rows}
    record.update(kwargs)
    return record


def row(root: Path, status: str, **kwargs) -> dict:
    """Строка проекта в записи раскатки — ровно ключи `_row` из S8A."""
    item = {"id": root.name, "name": root.name, "root": str(root), "status": status,
            "from": "", "to": "", "reason": "", "error": "", "at": STAMP}
    item.update(kwargs)
    return item


def page() -> str:
    """Текст страницы панели: единственный источник разметки блока."""
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    """Кусок текста от `start` до ближайшего `end` после него."""
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


class running:
    """Сервер панели в потоке: нужен там, где проверяется живой ответ снимка."""

    def __init__(self, root: Path) -> None:
        self.httpd = ui_server.make_server(root, port=0)

    def __enter__(self):
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}", self.httpd

    def __exit__(self, *exc_info) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ───────────────────────────── тесты ─────────────────────────────

def test_status_has_row_for_every_registry_project(tmp_path: Path, hermes_stub: Path):
    """Три проекта — три строки: исход из записи, проект без исхода `waiting`."""
    alpha = install_project(tmp_path / "alpha", board="statusboard")
    beta = install_project(tmp_path / "beta", board="statusboard")
    bare = tmp_path / "bare"
    bare.mkdir()
    record = engine_record([
        row(alpha, rollout.STATUS_UPDATED, **{"from": "0.26.0", "to": "0.27.1"}),
        row(beta, rollout.STATUS_SKIPPED, reason="движок и роли уже актуальны"),
    ])
    write_registry([alpha, beta, bare], {"engine": record})
    before = registry()["projects"]

    status = rollout.status()

    assert [item["id"] for item in status["projects"]] == ["alpha", "beta", "bare"]
    assert tuple(sorted(status["projects"][0])) == tuple(sorted(rollout.PROJECT_ROW_KEYS))
    rows = {item["id"]: item for item in status["projects"]}
    assert rows["alpha"]["status_label"] == "обновлено"
    assert (rows["alpha"]["from"], rows["alpha"]["to"]) == ("0.26.0", "0.27.1")
    assert rows["beta"]["status"] == "skipped"
    assert rows["beta"]["status_label"] == "пропущено"
    assert rows["bare"]["status"] == "waiting"
    assert rows["bare"]["status_label"] == "ждёт"
    assert rows["bare"]["reason"] == "ждёт очереди"
    assert status["summary"] == {"updated": 1, "skipped": 1, "failed": 0, "waiting": 1}
    assert status["summary_text"] == "обновлено: 1, пропущено: 1, ошибка: 0, ждёт: 1"

    # Тот же итог через единственный писатель: плоские ключи — проекция записи шага,
    # а список проектов реестра остаётся тем, что положил владелец.
    rollout.write_engine(record)
    flat = registry()["rollout"]
    assert flat["running"] is False
    assert flat["at"] == record["at"]
    assert flat["projects"] == record["projects"]
    assert registry()["projects"] == before


def test_skipped_and_failed_are_distinguishable(tmp_path: Path, hermes_stub: Path):
    """`skipped` несёт причину, `failed` — ошибку: по ним владелец видит разницу."""
    alpha = install_project(tmp_path / "alpha", board="statusboard")
    beta = install_project(tmp_path / "beta", board="statusboard")
    record = engine_record([
        row(alpha, rollout.STATUS_SKIPPED, reason="конвейер в проекте не установлен"),
        row(beta, rollout.STATUS_FAILED, error="upgrade вернул код 2"),
    ], ok=False)
    write_registry([alpha, beta], {"engine": record})

    status = rollout.status()

    rows = {item["id"]: item for item in status["projects"]}
    assert [rows["alpha"]["status_label"], rows["beta"]["status_label"]] == ["пропущено",
                                                                           "ошибка"]
    assert rows["alpha"]["reason"] == "конвейер в проекте не установлен"
    assert rows["alpha"]["error"] == ""
    assert rows["beta"]["error"] == "upgrade вернул код 2"
    assert rows["beta"]["reason"] == ""
    assert status["ok"] is False
    assert status["summary"]["skipped"] == 1
    assert status["summary"]["failed"] == 1
    assert "с ошибками" in status["status_text"]
    assert "ошибка: 1" in status["status_text"]


def test_button_disabled_while_rollout_runs(tmp_path: Path, hermes_stub: Path, monkeypatch):
    """Живой процесс и запись «идёт» — кнопка заблокирована; без процесса — «прервана»."""
    alpha = install_project(tmp_path / "alpha", board="statusboard")
    record = engine_record([row(alpha, rollout.STATUS_WAITING,
                                reason="идёт шаг 1-S1: окно закрыто")],
                           running=True, finished="", at=STAMP, ok=False)
    write_registry([alpha], {"engine": record})
    monkeypatch.setattr(rollout, "busy", lambda: True)

    status = rollout.status()

    assert status["running"] is True
    assert status["stale"] is False
    assert status["status_text"] == ("идёт раскатка с 14.11.2026 12:00:00: "
                                     "обновлено: 0, пропущено: 0, ошибка: 0, ждёт: 1")

    body = block(page(), "function renderRollout", "function renderTimes")
    assert "disabled" in body
    assert "running" in body
    assert "esc(" in body

    with running(alpha) as (url, _httpd):
        code, data = get(url + "/api/snapshot")
    assert code == 200
    assert data["rollout"]["running"] is True

    # Панель перезапустили посреди раскатки: запись врёт, процесса нет (D3).
    monkeypatch.setattr(rollout, "busy", lambda: False)
    dead = rollout.status()
    assert dead["running"] is False
    assert dead["stale"] is True
    assert dead["note"] == rollout.STALE_NOTE
    assert dead["status_text"].startswith("раскатка прервана")


def test_finished_rollout_shows_time_and_result(tmp_path: Path, hermes_stub: Path):
    """Законченная раскатка: время, итог и отпечаток, который видит новый статус (D8)."""
    alpha = install_project(tmp_path / "alpha", board="statusboard")
    beta = install_project(tmp_path / "beta", board="statusboard")
    empty = tmp_path / "empty"
    empty.mkdir()
    record = engine_record([
        row(alpha, rollout.STATUS_UPDATED, **{"from": "0.26.0", "to": "0.27.1"}),
        row(beta, rollout.STATUS_UPDATED, **{"from": "0.27.1", "to": "0.27.3"}),
        row(empty, rollout.STATUS_SKIPPED, reason="движок и роли уже актуальны"),
    ])
    write_registry([alpha, beta, empty], {"engine": record})

    status = rollout.status()

    assert status["at"] == STAMP
    assert status["finished"] == FINISH
    assert status["ok"] is True
    assert status["status_text"] == ("раскатка закончена 14.11.2026 12:05:00: "
                                     "обновлено: 2, пропущено: 1, ошибка: 0, ждёт: 0")

    # Реестр без записи: «не запускалась» — отсутствие итога, а не нули (D1).
    write_registry([alpha, beta, empty], {})
    fresh = rollout.status()
    assert fresh["status_text"] == rollout.NO_ROLLOUT_TEXT
    assert fresh["ok"] is None
    assert [item["status"] for item in fresh["projects"]] == ["waiting"] * 3

    snapshot.clear_cache()
    first = snapshot.snapshot(alpha, plan="1")
    assert first["rollout"]["status_text"] == rollout.NO_ROLLOUT_TEXT
    # Запись итога меняет реестр, а он входит в отпечаток: снимок обязан пересобраться.
    rollout.write_engine(record)
    second = snapshot.snapshot(alpha, plan="1")
    assert second["rollout"]["finished"] == FINISH
