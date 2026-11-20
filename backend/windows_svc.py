"""业务窗开/关/列表。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import (
    RegistryDeviation,
    WindowReject,
    assert_registry_pins_path_a,
    connect,
    validate_window_payload,
    write_audit,
)
from locks import release, require_lock, try_acquire
from models import WindowCreateIn

router = APIRouter(tags=["windows"])


@router.get("/api/windows")
def list_windows(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/windows")
def create_window(body: WindowCreateIn, authorization: AuthHeader = None):
    """甲路径建窗（见 docs/建窗校验册.md）。

    闸门顺序：校验册 → 建窗锁 → 整笔校验 → 同事务插入+审计。
    任一步失败：windows 零增，按口径写审计/争用后回滚。
    """
    user = require_user(authorization)
    lock_key = f"window_create:{body.name}"
    with connect() as conn:
        # 1) 校验册闸门：偏离即拒写并记「建窗偏了」。
        try:
            assert_registry_pins_path_a(conn)
        except RegistryDeviation as dev:
            write_audit(
                conn,
                user,
                "建窗偏了",
                body.name,
                {"reason": dev.reason, "path_guard": "registry"},
            )
            conn.commit()
            raise HTTPException(409, f"建窗校验册偏离，拒写：{dev.reason}") from dev

        # 2) 占建窗锁：后到只记争用，不写业务。
        #    try_acquire 内部已写 contention_log；并发 INSERT 竞态兜底同样按争用处理。
        try:
            acquired = try_acquire(conn, lock_key, user)
        except sqlite3.IntegrityError:
            acquired = False
        if not acquired:
            write_audit(
                conn,
                user,
                "window_create_contention",
                body.name,
                {"lock_key": lock_key, "note": "后到只记争用，不写业务"},
            )
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用，不写业务")

        try:
            # 3) 创建前整笔校验（与种子装载同一函数）：不过即整笔拒绝，零增。
            try:
                validate_window_payload(conn, body.name, body.open_at, body.close_at)
            except WindowReject as rej:
                write_audit(
                    conn,
                    user,
                    "window_create_reject",
                    body.name,
                    {"reason": rej.reason, "open_at": body.open_at, "close_at": body.close_at},
                )
                conn.commit()
                raise HTTPException(400, f"建窗整笔拒绝：{rej.reason}") from rej

            # 4) 校验通过：同事务原子插入正式窗 + 成功审计。
            cur = conn.execute(
                "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,1)",
                (body.name, body.open_at, body.close_at, "开启"),
            )
            wid = cur.lastrowid
            write_audit(conn, user, "window_create", body.name, {"id": wid})
            conn.commit()
            row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/windows/{wid}/close")
def close_window(wid: int, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] == "已关闭":
                return {"ok": True, "already": True}
            now = datetime.now().isoformat(timespec="seconds")
            token = f"seal-{wid}-{w['generation']}-{now}"
            conn.execute(
                "UPDATE windows SET status='已关闭', seal_token=? WHERE id=?",
                (token, wid),
            )
            write_audit(conn, user, "window_close", str(wid), {"seal_token": token})
            conn.commit()
            return {"ok": True, "seal_token": token}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/windows/{wid}")
def get_window(wid: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "窗口不存在")
        ev = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM events WHERE window_id=? AND disposition='窗内'",
            (wid,),
        ).fetchone()
        return {
            "window": dict(w),
            "in_window_count": ev["c"],
            "in_window_amount": ev["s"],
        }
