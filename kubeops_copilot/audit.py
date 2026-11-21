"""질문·근거·제안·승인·실행·검증 이력을 SQLite에 남긴다 (감사 로그 겸 운영 사례 축적)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    thread_id TEXT,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    category TEXT,
    root_cause TEXT,
    action TEXT,
    target TEXT,
    approver TEXT,
    verdict TEXT,
    analysis_seconds REAL,
    payload TEXT NOT NULL
)
"""


def _conn() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.execute(_SCHEMA)
    return conn


def save_case(state: dict) -> int:
    analysis = state.get("analysis") or {}
    proposal = state.get("proposal") or {}
    approval = state.get("approval") or {}
    verification = state.get("verification") or {}
    payload = {k: state.get(k) for k in (
        "intent", "evidence", "signals", "docs", "analysis", "proposal", "guard", "triage", "recheck",
        "approval", "execution", "verification", "timings", "answer")}
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO cases (created_at, thread_id, question, status, category, root_cause, action, target, "
            "approver, verdict, analysis_seconds, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dt.datetime.now().isoformat(timespec="seconds"),
                state.get("thread_id"),
                state.get("question", ""),
                state.get("status", ""),
                analysis.get("root_cause_category"),
                analysis.get("root_cause"),
                proposal.get("action"),
                f"{proposal.get('namespace', '')}/{proposal.get('deployment', '')}".strip("/"),
                approval.get("approver"),
                verification.get("verdict"),
                (state.get("timings") or {}).get("analysis_total"),
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)


def list_cases(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, created_at, question, status, category, action, target, approver, verdict, analysis_seconds "
            "FROM cases ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_case(case_id: int) -> dict | None:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["payload"] = json.loads(out["payload"])
    return out
