"""사건 단계와 티켓·알림함을 같은 로컬 SQLite 파일에 보관한다."""
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

from . import audit
from .first_response import now


@contextmanager
def database():
    db = audit._conn()
    try:
        db.row_factory = sqlite3.Row
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS incident_events
                (seq INTEGER PRIMARY KEY, incident TEXT, event_key TEXT, at TEXT, actor TEXT,
                 action TEXT, detail TEXT, UNIQUE(incident,event_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS incident_tickets
                (id INTEGER PRIMARY KEY, incident TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL,
                 reason TEXT, context TEXT, status TEXT, delivery TEXT, UNIQUE(incident,kind))""")
            yield db
    finally:
        db.close()


def record(incident, key, actor, action, detail, db=None):
    if db is None:
        with database() as conn:
            record(incident, key, actor, action, detail, conn)
        return
    db.execute("""INSERT OR IGNORE INTO incident_events(incident,event_key,at,actor,action,detail)
                  VALUES(?,?,?,?,?,?)""", (incident, key, now(), actor, action, detail))


def record_echo(incident, cycle, receipt):
    with database() as db:
        for step in receipt["attempts"]:
            record(incident, f"echo-{cycle}-{step['attempt']}", "앱",
                   "요청 실행" if step["attempt"] == 1 else "같은 요청 재시도",
                   f"{step['at']} · {step['detail']} · {step['ms']}ms", db)
        outcome = receipt["outcome"]
        label = {"healthy": "첫 요청 정상", "recovered": "재시도 후 응답 본문 확인", "escalated": "앱 복구 실패 · 진단 요청"}
        record(incident, f"echo-result-{cycle}", "앱", label[outcome], "demo/web → demo/api echo", db)


def timeline(incident):
    with database() as db:
        return [dict(row) for row in db.execute(
            "SELECT at,actor,action,detail FROM incident_events WHERE incident=? ORDER BY seq", (incident,))]


def tickets(incident=None):
    with database() as db:
        rows = db.execute("SELECT * FROM incident_tickets ORDER BY id DESC") if incident is None else db.execute(
            "SELECT * FROM incident_tickets WHERE incident=? ORDER BY id", (incident,))
        return [{**dict(row), "context": json.loads(row["context"])} for row in rows]


def notify(ticket_id):
    if sys.platform == "darwin" and os.getenv("KUBEOPS_DESKTOP_NOTIFICATIONS", "1") == "1":
        script = f'display notification "티켓 #{ticket_id} · KubeOps 티켓함을 확인하세요." with title "KubeOps Copilot"'
        try:
            subprocess.run(["/usr/bin/osascript", "-e", script], check=True, timeout=5, capture_output=True)
            return "알림함 저장 · macOS 알림 요청"
        except (OSError, subprocess.SubprocessError):
            return "알림함 저장 · macOS 알림 요청 실패"
    return "알림함 저장"


def issue(incident, kind, reason, context):
    with database() as db:
        created = db.execute("""INSERT OR IGNORE INTO incident_tickets
            (incident,kind,created,reason,context,status,delivery) VALUES(?,?,?,?,?,'접수 대기','알림함 저장')""",
            (incident, kind, now(), reason, json.dumps(context, ensure_ascii=False))).rowcount
        ticket_id = db.execute("SELECT id FROM incident_tickets WHERE incident=? AND kind=?",
                               (incident, kind)).fetchone()[0]
        if created:
            record(incident, f"ticket-{ticket_id}", "시스템", "티켓 발급", f"#{ticket_id} {kind} · {reason}", db)
    if created:
        delivery = notify(ticket_id)
        with database() as db:
            db.execute("UPDATE incident_tickets SET delivery=? WHERE id=?", (delivery, ticket_id))
            record(incident, f"notify-{ticket_id}", "시스템", "담당자 알림", delivery, db)
    return ticket_id


def transition(ticket_id, status, actor, note):
    allowed = {"처리 중": ("접수 대기",), "처리 완료": ("처리 중",), "승인됨": ("접수 대기", "처리 중"),
               "거절됨": ("접수 대기", "처리 중"), "기한 종료": ("접수 대기", "처리 중")}
    if status not in allowed or not actor.strip() or not note.strip():
        return False
    with database() as db:
        row = db.execute("SELECT incident,kind,status FROM incident_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row or row["status"] not in allowed[status] or (row["kind"] == "승인 요청" and status == "처리 완료"):
            return False
        changed = db.execute("UPDATE incident_tickets SET status=? WHERE id=? AND status=?",
                             (status, ticket_id, row["status"])).rowcount
        if changed:
            record(row["incident"], f"{ticket_id}-{status}", actor, "티켓 " + status, note, db)
        return bool(changed)


def approval_result(state):
    decision = state.get("approval") or {}
    if not decision:
        return
    for t in tickets(state["thread_id"]):
        if t["kind"] == "승인 요청" and decision:
            expired = decision.get("comment") in ("승인 시간 초과", "승인 미제공")
            status = "승인됨" if decision["approved"] else "기한 종료" if expired else "거절됨"
            transition(t["id"], status, decision.get("approver") or "운영자", decision.get("comment") or status)
