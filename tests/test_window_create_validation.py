"""建窗写路径红测：倒挂/重名整笔拒绝（甲路径）+ 同名争用 + 校验册同态。

纪律：
- 甲路径：时间倒挂或重名时整笔拒绝，windows 零增，必写审计。
- 同名建窗须占建窗锁，后到只记争用（contention_log + 审计），不写业务。
- 建窗校验册偏离即拒写并记「建窗偏了」。
- 五处同态：窗口列表、详情卡、审计流水、种子夹具、争用。
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
import db
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _window_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"]


def _audit_actions() -> list[str]:
    with connect() as conn:
        return [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_trail").fetchall()
        ]


# ---------- 红测 1：非法建窗（倒挂/重名）整笔拒绝 ----------

def test_inverted_time_window_rejected_zero_growth():
    """open_at 晚于 close_at：整笔拒绝，windows 零增，审计可查。"""
    c = TestClient(app)
    h = _auth(c)
    before = _window_count()
    r = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-BAD-INVERT",
            "open_at": "2026-11-10T23:59:59",
            "close_at": "2026-11-10T00:00:00",
        },
    )
    assert r.status_code in (400, 409, 422)
    assert _window_count() == before  # windows 零增
    names = [
        w["name"]
        for w in c.get("/api/windows", headers=h).json()["items"]
    ]
    assert "W-BAD-INVERT" not in names  # 列表同态：非法窗不出现
    assert "window_create_reject" in _audit_actions()  # 审计流水可查


def test_equal_open_close_rejected():
    """open_at == close_at 亦属倒挂（必须严格 open < close）。"""
    c = TestClient(app)
    h = _auth(c)
    before = _window_count()
    r = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-BAD-EQUAL",
            "open_at": "2026-11-10T12:00:00",
            "close_at": "2026-11-10T12:00:00",
        },
    )
    assert r.status_code in (400, 409, 422)
    assert _window_count() == before
    assert "window_create_reject" in _audit_actions()


def test_duplicate_name_rejected_old_generation_intact():
    """重名窗不得覆盖旧世代：整笔拒绝，旧窗世代/字段不变，零增。"""
    c = TestClient(app)
    h = _auth(c)
    existing = c.get("/api/windows", headers=h).json()["items"][0]
    before = _window_count()
    r = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": existing["name"],
            "open_at": "2026-12-01T00:00:00",
            "close_at": "2026-12-01T23:59:59",
        },
    )
    assert r.status_code in (400, 409)
    assert _window_count() == before  # 零增，无覆盖
    again = c.get(f"/api/windows/{existing['id']}", headers=h).json()["window"]
    # 详情卡同态：旧窗世代与时间未被撕裂/覆盖
    assert again["generation"] == existing["generation"]
    assert again["open_at"] == existing["open_at"]
    assert again["close_at"] == existing["close_at"]
    assert "window_create_reject" in _audit_actions()


def test_valid_window_still_creates_and_audited():
    """合法窗正常落库，且审计为 window_create（拒绝与成功口径不混用）。"""
    c = TestClient(app)
    h = _auth(c)
    before = _window_count()
    r = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-GOOD-1",
            "open_at": "2026-11-20T00:00:00",
            "close_at": "2026-11-20T23:59:59",
        },
    )
    assert r.status_code == 200, r.text
    assert _window_count() == before + 1
    body = r.json()
    assert body["status"] == "开启"
    assert body["generation"] == 1
    detail = c.get(f"/api/windows/{body['id']}", headers=h).json()
    # 列表/详情同态：同一 id 字段一致
    assert detail["window"]["name"] == "W-GOOD-1"
    assert "window_create" in _audit_actions()


# ---------- 红测 2：双人同时建同名窗，建窗锁争用 ----------

def test_concurrent_same_name_only_one_wins_lock_contention():
    """两人同时创建同名窗：占建窗锁，后到 409 只记争用，windows 仅 +1。"""
    c = TestClient(app)
    h1 = _auth(c)
    tok2 = c.post(
        "/api/login", json={"username": "closer", "password": "close123"}
    ).json()["token"]
    h2 = {"Authorization": "Bearer " + tok2}
    before = _window_count()

    # 先占建窗锁（与运行时同锁键），模拟先到者持锁
    lock_key = "window_create:W-RACE"
    assert c.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": lock_key}
    ).status_code == 200

    # 后到者建同名窗：须 409 争用，不写业务
    r = c.post(
        "/api/windows",
        headers=h2,
        json={
            "name": "W-RACE",
            "open_at": "2026-11-21T00:00:00",
            "close_at": "2026-11-21T23:59:59",
        },
    )
    assert r.status_code == 409
    assert _window_count() == before  # 争用不写业务
    contention = c.get("/api/contention", headers=h1).json()["items"]
    assert any(x["lock_key"] == lock_key for x in contention)  # 争用同态
    assert "window_create_contention" in _audit_actions()


def test_concurrent_same_name_two_threads_single_row():
    """真实双线程并发同名建窗：恰有一窗落库，后到者失败且零业务污染、留痕。

    SQLite 写串行化：后到者要么撞建窗锁争用(409)，要么持锁后撞重名整笔拒绝(400)，
    两种都属「后到不写业务」；纯争用语义由上一条显式持锁测试钉死。
    """
    c = TestClient(app)
    h_admin = _auth(c)
    tok2 = c.post(
        "/api/login", json={"username": "closer", "password": "close123"}
    ).json()["token"]
    payload = {
        "name": "W-THREAD-RACE",
        "open_at": "2026-11-22T00:00:00",
        "close_at": "2026-11-22T23:59:59",
    }
    results: list[int] = []
    errors: list[object] = []
    barrier = threading.Barrier(2)

    def worker(token: str) -> None:
        local = TestClient(app)
        headers = {"Authorization": "Bearer " + token}
        barrier.wait()
        try:
            r = local.post("/api/windows", headers=headers, json=payload)
            results.append(r.status_code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    admin_token = h_admin["Authorization"].removeprefix("Bearer ")
    t1 = threading.Thread(target=worker, args=(admin_token,))
    t2 = threading.Thread(target=worker, args=(tok2,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not errors, errors
    assert 200 in results and all(s in (400, 409) for s in results if s != 200), results
    assert results.count(200) == 1, results
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE name=?", ("W-THREAD-RACE",)
        ).fetchone()["c"]
        contention_n = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            ("window_create:W-THREAD-RACE",),
        ).fetchone()["c"]
        reject_n = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='window_create_reject'"
        ).fetchone()["c"]
    assert n == 1  # 仅一窗，零业务污染
    assert contention_n >= 1 or reject_n >= 1  # 后到留痕（争用或整笔拒绝）


# ---------- 建窗校验册：甲路径钉死，偏离拒写 ----------

def test_registry_pins_path_a():
    """校验册存在且钉死甲路径；不允许混用乙路径/draft 表。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM window_create_registry"
        ).fetchall()
    assert len(row) == 1
    reg = dict(row[0])
    assert reg["chosen_path"] == "甲"
    assert reg["active"] == 1
    # 乙路径的草稿表在甲路径下不得存在（两边不得混用）
    with connect() as conn:
        draft = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='window_draft'"
        ).fetchone()
    assert draft is None


def test_registry_deviation_rejects_and_audits():
    """校验册被改偏（active=0/口径不符）：建窗拒写并记「建窗偏了」。"""
    c = TestClient(app)
    h = _auth(c)
    with connect() as conn:
        conn.execute("UPDATE window_create_registry SET active=0")
        conn.commit()
    before = _window_count()
    r = c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-DEVIATE",
            "open_at": "2026-11-23T00:00:00",
            "close_at": "2026-11-23T23:59:59",
        },
    )
    assert r.status_code == 409
    assert _window_count() == before
    targets = [
        (r["action"], r["target"])
        for r in c.get("/api/audit?limit=50", headers=h).json()["items"]
    ]
    assert any(a == "建窗偏了" for a, _ in targets)


def test_registry_cannot_be_changed_same_day():
    """当天选定甲路径后禁止改口：同日改册被拒，册仍为甲。"""
    with connect() as conn:
        # 尝试把甲改成乙（同日）
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE window_create_registry SET chosen_path='乙' WHERE chosen_date=date('now')"
            )
            conn.commit()
        row = conn.execute("SELECT chosen_path FROM window_create_registry").fetchone()
    assert row["chosen_path"] == "甲"


# ---------- 种子夹具同态：装载路径复用同一校验 ----------

def test_seed_loader_rejects_invalid_fixture(tmp_path, monkeypatch):
    """种子夹具含倒挂/重名：装载路径拒绝非法行，合法行仍入，审计留痕。"""
    from pathlib import Path

    monkeypatch.setattr("db.DB_PATH", tmp_path / "seed.db")
    monkeypatch.setattr("db.FIXTURES", Path("/workspace/tests/fixtures_bad"))
    init_db()
    with connect() as conn:
        names = [r["name"] for r in conn.execute("SELECT name FROM windows").fetchall()]
        assert "W-OK-1" in names
        assert "W-INVERTED" not in names  # 倒挂夹具被拒
        assert names.count("W-DUP-A") == 1  # 重名夹具只保留一条
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_trail").fetchall()]
    assert "window_create_reject" in actions


def test_seed_fixture_file_itself_valid():
    """仓内正式夹具 windows.json 必须通过同一校验（五处同态的夹具面）。"""
    data = json.loads(
        (db.FIXTURES / "windows.json").read_text(encoding="utf-8")
    )
    seen: set[str] = set()
    for w in data:
        assert w["open_at"] < w["close_at"], f"夹具倒挂: {w['name']}"
        assert w["name"] not in seen, f"夹具重名: {w['name']}"
        seen.add(w["name"])
