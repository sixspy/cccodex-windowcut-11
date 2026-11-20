"""SQLite 连接、建表、种子。业务窗关账母仓公共库层。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FIXTURES = ROOT / "fixtures"
FRONTEND = ROOT / "frontend"

PRODUCT_META = {
    "modules": [
        "windows",
        "ingest",
        "late_bucket",
        "materialize",
        "reconcile",
        "locks",
        "audit",
        "hold_staging",
        "reopen",
    ],
    "stubs": ["hold_release", "late_promote", "partition_mat", "reopen"],
}


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def sha_password(password: str) -> str:
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  password_sha TEXT NOT NULL,
  role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS windows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  seal_token TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  disposition TEXT NOT NULL,
  hold_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS late_bucket(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materializations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  total_amount REAL NOT NULL,
  partition_key TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hold_staging(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS window_locks(
  lock_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contention_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lock_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_trail(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reopen_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  from_generation INTEGER NOT NULL,
  to_generation INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reject_slips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- 建窗校验册：钉死甲路径（整笔拒绝），当日禁止改口（见触发器）。
-- 不设 window_draft 表：乙路径已废弃，甲乙不得混用。
CREATE TABLE IF NOT EXISTS window_create_registry(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  chosen_path TEXT NOT NULL CHECK (chosen_path IN ('甲','乙')),
  active INTEGER NOT NULL CHECK (active IN (0,1)),
  chosen_date TEXT NOT NULL,
  note TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_registry_no_flip_today
BEFORE UPDATE OF chosen_path ON window_create_registry
WHEN OLD.chosen_date = date('now') AND NEW.chosen_path <> OLD.chosen_path
BEGIN
  SELECT RAISE(ABORT, '建窗校验册当日禁止改口');
END;
"""

# 校验册固定单行 id；甲路径为唯一在用口径。
REGISTRY_ROW_ID = 1
CHOSEN_PATH = "甲"


class WindowReject(Exception):
    """建窗整笔拒绝（甲路径）：携带原因，调用方写 window_create_reject 审计。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RegistryDeviation(Exception):
    """校验册偏离：拒写并记「建窗偏了」。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_window_payload(
    conn: sqlite3.Connection, name: str, open_at: str, close_at: str
) -> None:
    """甲路径创建前整笔校验。不过则抛 WindowReject，调用方保证 windows 零增。

    与种子装载路径共用，保证「夹具同态」。
    """
    if not name or not str(name).strip():
        raise WindowReject("窗口名不能为空")
    if not open_at or not close_at:
        raise WindowReject("开启/关闭时间不能为空")
    # 严格 open < close：相等或倒挂（open 晚于 close）一律拒绝。
    if not (open_at < close_at):
        raise WindowReject(f"时间倒挂：open_at({open_at}) 必须早于 close_at({close_at})")
    dup = conn.execute(
        "SELECT id, generation FROM windows WHERE name=?", (name,)
    ).fetchone()
    if dup is not None:
        # 重名不得覆盖旧世代。
        raise WindowReject(
            f"窗口重名：{name} 已存在(id={dup['id']}, generation={dup['generation']})，禁止覆盖旧世代"
        )


def ensure_registry(conn: sqlite3.Connection) -> None:
    """初始化并钉死校验册为甲路径；已存在则不覆盖当日选择。"""
    from datetime import date

    row = conn.execute(
        "SELECT id FROM window_create_registry WHERE id=?", (REGISTRY_ROW_ID,)
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO window_create_registry(id,chosen_path,active,chosen_date,note)
               VALUES (?,?,?,?,?)""",
            (
                REGISTRY_ROW_ID,
                CHOSEN_PATH,
                1,
                date.today().isoformat(),
                "甲路径：时间倒挂/重名整笔拒绝，windows 零增并写审计；不引入 window_draft",
            ),
        )


def assert_registry_pins_path_a(conn: sqlite3.Connection) -> None:
    """写路径闸门：校验册偏离即抛 RegistryDeviation（调用方记「建窗偏了」并拒写）。"""
    row = conn.execute(
        "SELECT chosen_path, active FROM window_create_registry WHERE id=?",
        (REGISTRY_ROW_ID,),
    ).fetchone()
    if row is None:
        raise RegistryDeviation("建窗校验册缺失")
    if row["active"] != 1 or row["chosen_path"] != CHOSEN_PATH:
        raise RegistryDeviation(
            f"校验册偏离：active={row['active']}, chosen_path={row['chosen_path']}"
        )


def ensure_users(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("admin", sha_password("123456"), "管理员"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("viewer", sha_password("view123"), "只读"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("closer", sha_password("close123"), "关账员"),
        )


def _load_fixture(name: str) -> list | dict | None:
    path = FIXTURES / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def seed_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] > 0:
        return
    windows = _load_fixture("windows.json") or [
        {
            "name": "W-1101",
            "open_at": "2026-11-01T00:00:00",
            "close_at": "2026-11-01T23:59:59",
            "status": "已关闭",
            "generation": 1,
        },
        {
            "name": "W-1102",
            "open_at": "2026-11-02T00:00:00",
            "close_at": "2026-11-02T23:59:59",
            "status": "开启",
            "generation": 1,
        },
        {
            "name": "W-1103",
            "open_at": "2026-11-03T00:00:00",
            "close_at": "2026-11-03T23:59:59",
            "status": "开启",
            "generation": 1,
        },
    ]
    for w in windows:
        # 种子夹具与 API 建窗走同一校验（五处同态之「种子夹具」）：
        # 倒挂/重名整笔拒绝，跳过该行并写 window_create_reject，不污染正式表。
        try:
            validate_window_payload(
                conn, w["name"], w["open_at"], w["close_at"]
            )
        except WindowReject as exc:
            write_audit(
                conn,
                "system",
                "window_create_reject",
                w.get("name"),
                {"source": "seed", "reason": exc.reason},
            )
            continue
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,?)",
            (
                w["name"],
                w["open_at"],
                w["close_at"],
                w["status"],
                int(w.get("generation", 1)),
            ),
        )
    events = _load_fixture("events.json") or [
        {
            "window_id": 1,
            "biz_key": "ORD-1",
            "event_ts": "2026-11-01T10:00:00",
            "amount": 12.5,
            "disposition": "窗内",
            "created_at": "2026-11-01T10:00:01",
        },
        {
            "window_id": None,
            "biz_key": "ORD-late",
            "event_ts": "2026-11-01T23:50:00",
            "amount": 3.0,
            "disposition": "迟到桶",
            "created_at": "2026-11-02T00:01:00",
        },
    ]
    for e in events:
        conn.execute(
            "INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at) VALUES (?,?,?,?,?,?)",
            (
                e.get("window_id"),
                e["biz_key"],
                e["event_ts"],
                e["amount"],
                e["disposition"],
                e.get("created_at", e["event_ts"]),
            ),
        )
        if e["disposition"] == "迟到桶":
            conn.execute(
                """INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                    "W-1101",
                    e["biz_key"],
                    e["event_ts"],
                    e["amount"],
                    "关窗后到达",
                    e.get("created_at", e["event_ts"]),
                ),
            )
    conn.execute(
        "INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,created_at) VALUES (?,?,?,?,?,?)",
        (1, "成功", 1, 12.5, "W-1101-g1", "2026-11-02T00:10:00"),
    )
    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        ("system", "seed", "windows", json.dumps({"count": len(windows)}), "2026-11-01T00:00:00"),
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        ensure_users(conn)
        ensure_registry(conn)
        seed_if_empty(conn)
        conn.commit()


def write_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    target: str | None = None,
    payload: dict | None = None,
    created_at: str | None = None,
) -> None:
    from datetime import datetime

    now = created_at or datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        (actor, action, target, json.dumps(payload or {}, ensure_ascii=False), now),
    )
