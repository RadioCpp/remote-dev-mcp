from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    action_name: str
    host_name: str
    tmux_session_name: str
    remote_log_path: str
    remote_status_path: str
    cwd: str
    command_preview: str
    env: dict[str, str]
    args: list[str]
    allowed_exit_codes: list[int]
    created_at: str
    status: str
    params: dict[str, Any]
    ready: bool = False
    ready_at: str | None = None
    exit_code: int | None = None
    finished_at: str | None = None
    note: str | None = None


class SessionStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def put(self, record: SessionRecord) -> None:
        payload = asdict(record)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                  session_id, action_name, host_name, tmux_session_name, remote_log_path,
                  remote_status_path, cwd, command_preview, env_json, args_json,
                  allowed_exit_codes_json, created_at, status, params_json,
                  ready, ready_at, exit_code, finished_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  action_name=excluded.action_name,
                  host_name=excluded.host_name,
                  tmux_session_name=excluded.tmux_session_name,
                  remote_log_path=excluded.remote_log_path,
                  remote_status_path=excluded.remote_status_path,
                  cwd=excluded.cwd,
                  command_preview=excluded.command_preview,
                  env_json=excluded.env_json,
                  args_json=excluded.args_json,
                  allowed_exit_codes_json=excluded.allowed_exit_codes_json,
                  created_at=excluded.created_at,
                  status=excluded.status,
                  params_json=excluded.params_json,
                  ready=excluded.ready,
                  ready_at=excluded.ready_at,
                  exit_code=excluded.exit_code,
                  finished_at=excluded.finished_at,
                  note=excluded.note
                """,
                (
                    payload["session_id"],
                    payload["action_name"],
                    payload["host_name"],
                    payload["tmux_session_name"],
                    payload["remote_log_path"],
                    payload["remote_status_path"],
                    payload["cwd"],
                    payload["command_preview"],
                    json.dumps(payload["env"], sort_keys=True),
                    json.dumps(payload["args"]),
                    json.dumps(payload["allowed_exit_codes"]),
                    payload["created_at"],
                    payload["status"],
                    json.dumps(payload["params"], sort_keys=True),
                    int(bool(payload["ready"])),
                    payload["ready_at"],
                    payload["exit_code"],
                    payload["finished_at"],
                    payload["note"],
                ),
            )
            self._conn.commit()

    def get(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list(
        self,
        limit: int = 50,
        statuses: list[str] | None = None,
        action_name: str | None = None,
        host_name: str | None = None,
        newest_first: bool = True,
    ) -> list[SessionRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if action_name:
            clauses.append("action_name = ?")
            params.append(action_name)
        if host_name:
            clauses.append("host_name = ?")
            params.append(host_name)
        where_sql = ""
        if clauses:
            where_sql = "WHERE " + " AND ".join(clauses)
        order = "DESC" if newest_first else "ASC"
        query = (
            "SELECT * FROM sessions "
            f"{where_sql} "
            f"ORDER BY created_at {order}, session_id {order} "
            "LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                  session_id TEXT PRIMARY KEY,
                  action_name TEXT NOT NULL,
                  host_name TEXT NOT NULL,
                  tmux_session_name TEXT NOT NULL,
                  remote_log_path TEXT NOT NULL,
                  remote_status_path TEXT NOT NULL,
                  cwd TEXT NOT NULL,
                  command_preview TEXT NOT NULL,
                  env_json TEXT NOT NULL,
                  args_json TEXT NOT NULL,
                  allowed_exit_codes_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  ready INTEGER NOT NULL DEFAULT 0,
                  ready_at TEXT,
                  exit_code INTEGER,
                  finished_at TEXT,
                  note TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "ready" not in columns:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
            if "ready_at" not in columns:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN ready_at TEXT")
            self._conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            action_name=row["action_name"],
            host_name=row["host_name"],
            tmux_session_name=row["tmux_session_name"],
            remote_log_path=row["remote_log_path"],
            remote_status_path=row["remote_status_path"],
            cwd=row["cwd"],
            command_preview=row["command_preview"],
            env=json.loads(row["env_json"]),
            args=json.loads(row["args_json"]),
            allowed_exit_codes=json.loads(row["allowed_exit_codes_json"]),
            created_at=row["created_at"],
            status=row["status"],
            params=json.loads(row["params_json"]),
            ready=bool(row["ready"]) if "ready" in row.keys() else False,
            ready_at=row["ready_at"] if "ready_at" in row.keys() else None,
            exit_code=row["exit_code"],
            finished_at=row["finished_at"],
            note=row["note"],
        )
