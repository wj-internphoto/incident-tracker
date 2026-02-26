#!/usr/bin/env python3
"""Incident Tracker — 알람 인시던트 타임라인 관리 서비스."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

KST = timezone(timedelta(hours=9))
DB_PATH = os.environ.get("DB_PATH", "/data/incidents.db")
PORT = int(os.environ.get("PORT", "8001"))
BASE_PATH = os.environ.get("BASE_PATH", "")  # e.g. "/tracker"

app = FastAPI(title="Incident Tracker")
templates = Jinja2Templates(directory="templates")
templates.env.globals["B"] = BASE_PATH  # 템플릿에서 {{ B }} 로 사용


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                alert_name TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                status TEXT NOT NULL DEFAULT 'firing',
                fired_at TEXT NOT NULL,
                acknowledged_at TEXT,
                resolved_at TEXT,
                resolution_type TEXT,
                labels TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
                ON incidents(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_incidents_status
                ON incidents(status);

            CREATE TABLE IF NOT EXISTS timeline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL REFERENCES incidents(id),
                event_type TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_timeline_incident
                ON timeline_events(incident_id);
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# ── Webhook 수신 ──────────────────────────────────────────────────────────────

@app.post("/webhook/alertmanager")
async def webhook_alertmanager(request: Request):
    """Alertmanager webhook 수신 (firing / resolved)."""
    data = await request.json()
    alerts = data.get("alerts", [])
    for alert in alerts:
        _process_alert(
            fingerprint=alert.get("fingerprint", ""),
            status=alert.get("status", "firing"),
            labels=alert.get("labels", {}),
            annotations=alert.get("annotations", {}),
            starts_at=alert.get("startsAt", ""),
            ends_at=alert.get("endsAt", ""),
        )
    return {"status": "ok", "processed": len(alerts)}


@app.post("/webhook/grafana")
async def webhook_grafana(request: Request):
    """Grafana unified alerting webhook 수신."""
    data = await request.json()
    alerts = data.get("alerts", [])
    for alert in alerts:
        _process_alert(
            fingerprint=alert.get("fingerprint", ""),
            status=alert.get("status", "firing"),
            labels=alert.get("labels", {}),
            annotations=alert.get("annotations", {}),
            starts_at=alert.get("startsAt", ""),
            ends_at=alert.get("endsAt", ""),
        )
    return {"status": "ok", "processed": len(alerts)}


def _process_alert(fingerprint, status, labels, annotations, starts_at, ends_at):
    """알람 → 인시던트 생성 또는 해소 처리."""
    alert_name = labels.get("alertname", "Unknown")
    severity = labels.get("severity", "info")
    ts = now_kst()

    with get_db() as db:
        if status == "firing":
            # 이미 active 인시던트가 있으면 스킵
            existing = db.execute(
                "SELECT id FROM incidents WHERE fingerprint = ? AND status != 'resolved'",
                (fingerprint,)
            ).fetchone()
            if existing:
                return

            cur = db.execute(
                """INSERT INTO incidents
                   (fingerprint, alert_name, severity, status, fired_at, labels, created_at)
                   VALUES (?, ?, ?, 'firing', ?, ?, ?)""",
                (fingerprint, alert_name, severity, ts, json.dumps(labels, ensure_ascii=False), ts)
            )
            incident_id = cur.lastrowid
            desc = annotations.get("description", annotations.get("summary", ""))
            note = f"알람 수신: {desc}" if desc else "알람 수신"
            db.execute(
                """INSERT INTO timeline_events
                   (incident_id, event_type, new_status, note, created_at)
                   VALUES (?, 'alert_fired', 'firing', ?, ?)""",
                (incident_id, note, ts)
            )

        elif status == "resolved":
            row = db.execute(
                "SELECT id, status FROM incidents WHERE fingerprint = ? AND status != 'resolved' ORDER BY fired_at DESC LIMIT 1",
                (fingerprint,)
            ).fetchone()
            if not row:
                return

            incident_id = row["id"]
            old_status = row["status"]
            db.execute(
                "UPDATE incidents SET status = 'resolved', resolved_at = ?, resolution_type = 'auto' WHERE id = ?",
                (ts, incident_id)
            )
            db.execute(
                """INSERT INTO timeline_events
                   (incident_id, event_type, old_status, new_status, note, created_at)
                   VALUES (?, 'alert_resolved', ?, 'resolved', '자동 해소', ?)""",
                (incident_id, old_status, ts)
            )


# ── API ───────────────────────────────────────────────────────────────────────

@app.post("/api/incidents/{incident_id}/ack")
async def ack_incident(incident_id: int, request: Request):
    """원클릭 확인 (acknowledged)."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    note = body.get("note", "")
    ts = now_kst()

    with get_db() as db:
        row = db.execute("SELECT id, status FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "인시던트를 찾을 수 없습니다")
        if row["status"] == "resolved":
            raise HTTPException(400, "이미 해결된 인시던트입니다")

        old_status = row["status"]
        db.execute(
            "UPDATE incidents SET status = 'acknowledged', acknowledged_at = ? WHERE id = ?",
            (ts, incident_id)
        )
        db.execute(
            """INSERT INTO timeline_events
               (incident_id, event_type, old_status, new_status, note, created_at)
               VALUES (?, 'acknowledged', ?, 'acknowledged', ?, ?)""",
            (incident_id, old_status, note or "확인", ts)
        )
    return {"status": "ok"}


@app.patch("/api/incidents/{incident_id}")
async def update_incident(incident_id: int, request: Request):
    """상태 변경 + 메모."""
    body = await request.json()
    new_status = body.get("status")
    note = body.get("note", "")
    ts = now_kst()

    valid_statuses = ("acknowledged", "investigating", "resolved")
    if new_status not in valid_statuses:
        raise HTTPException(400, f"유효한 상태: {valid_statuses}")
    if new_status == "resolved" and not note.strip():
        raise HTTPException(400, "해결 시 원인/조치사항 메모는 필수입니다")

    with get_db() as db:
        row = db.execute("SELECT id, status FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "인시던트를 찾을 수 없습니다")

        old_status = row["status"]
        updates = {"status": new_status}
        if new_status == "acknowledged" and not row.get("acknowledged_at"):
            updates["acknowledged_at"] = ts
        if new_status == "resolved":
            updates["resolved_at"] = ts
            updates["resolution_type"] = "manual"

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE incidents SET {set_clause} WHERE id = ?", (*updates.values(), incident_id))
        db.execute(
            """INSERT INTO timeline_events
               (incident_id, event_type, old_status, new_status, note, created_at)
               VALUES (?, 'status_change', ?, ?, ?, ?)""",
            (incident_id, old_status, new_status, note, ts)
        )
    return {"status": "ok"}


@app.post("/api/incidents/{incident_id}/memo")
async def add_memo(incident_id: int, request: Request):
    """메모만 추가 (상태 변경 없이)."""
    body = await request.json()
    note = body.get("note", "")
    if not note.strip():
        raise HTTPException(400, "메모 내용이 비어 있습니다")
    ts = now_kst()

    with get_db() as db:
        row = db.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "인시던트를 찾을 수 없습니다")
        db.execute(
            """INSERT INTO timeline_events
               (incident_id, event_type, note, created_at)
               VALUES (?, 'memo', ?, ?)""",
            (incident_id, note, ts)
        )
    return {"status": "ok"}


@app.delete("/api/timeline/{event_id}")
async def delete_timeline_event(event_id: int):
    """타임라인 이벤트 삭제 (메모만 삭제 가능)."""
    with get_db() as db:
        row = db.execute(
            "SELECT id, event_type FROM timeline_events WHERE id = ?", (event_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "이벤트를 찾을 수 없습니다")
        if row["event_type"] not in ("memo",):
            raise HTTPException(400, "메모만 삭제할 수 있습니다")
        db.execute("DELETE FROM timeline_events WHERE id = ?", (event_id,))
    return {"status": "ok"}


@app.get("/api/incidents")
async def list_incidents(status: str = None, severity: str = None):
    """인시던트 목록."""
    with get_db() as db:
        query = "SELECT * FROM incidents"
        params = []
        conditions = []
        if status:
            if status == "active":
                conditions.append("status != 'resolved'")
            else:
                conditions.append("status = ?")
                params.append(status)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY CASE WHEN status != 'resolved' THEN 0 ELSE 1 END, fired_at DESC"
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: int):
    """인시던트 상세 + 타임라인."""
    with get_db() as db:
        incident = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404, "인시던트를 찾을 수 없습니다")
        events = db.execute(
            "SELECT * FROM timeline_events WHERE incident_id = ? ORDER BY created_at ASC",
            (incident_id,)
        ).fetchall()
        return {
            "incident": dict(incident),
            "timeline": [dict(e) for e in events],
        }


# ── 문서 내보내기 ─────────────────────────────────────────────────────────────

EVENT_ICONS = {
    "alert_fired": "🔴 알람 발생",
    "acknowledged": "👀 확인",
    "status_change": "🔄 상태 변경",
    "memo": "📝 메모",
    "alert_resolved": "🟢 해소",
}


@app.get("/api/incidents/{incident_id}/export", response_class=PlainTextResponse)
async def export_incident(incident_id: int):
    """단일 인시던트 Markdown 내보내기."""
    with get_db() as db:
        incident = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404)
        events = db.execute(
            "SELECT * FROM timeline_events WHERE incident_id = ? ORDER BY created_at ASC",
            (incident_id,)
        ).fetchall()

    inc = dict(incident)
    duration = ""
    if inc["resolved_at"] and inc["fired_at"]:
        try:
            t1 = datetime.strptime(inc["fired_at"], "%Y-%m-%d %H:%M:%S")
            t2 = datetime.strptime(inc["resolved_at"], "%Y-%m-%d %H:%M:%S")
            mins = int((t2 - t1).total_seconds() / 60)
            duration = f"{mins}분"
        except Exception:
            duration = "-"

    ack_duration = ""
    if inc["acknowledged_at"] and inc["fired_at"]:
        try:
            t1 = datetime.strptime(inc["fired_at"], "%Y-%m-%d %H:%M:%S")
            t2 = datetime.strptime(inc["acknowledged_at"], "%Y-%m-%d %H:%M:%S")
            mins = int((t2 - t1).total_seconds() / 60)
            ack_duration = f"{mins}분"
        except Exception:
            ack_duration = "-"

    lines = [
        f"# 인시던트 리포트: {inc['alert_name']}",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
        f"| 발생 | {inc['fired_at']} |",
    ]
    if inc["acknowledged_at"]:
        lines.append(f"| 인지 | {inc['acknowledged_at']} ({ack_duration}) |")
    if inc["resolved_at"]:
        lines.append(f"| 해소 | {inc['resolved_at']} ({duration}) |")
    lines.extend([
        f"| 심각도 | {inc['severity']} |",
        f"| 해결 방법 | {inc.get('resolution_type') or '미해결'} |",
        f"| 상태 | {inc['status']} |",
        "",
        "## 타임라인",
        "",
        "| 시각 | 이벤트 | 내용 |",
        "|------|--------|------|",
    ])

    for ev in events:
        e = dict(ev)
        time_str = e["created_at"][11:16] if len(e["created_at"]) >= 16 else e["created_at"]
        icon = EVENT_ICONS.get(e["event_type"], e["event_type"])
        note = e["note"] or ""
        if e["event_type"] == "status_change" and e["new_status"]:
            note = f"{e['new_status']}" + (f" - {note}" if note else "")
        lines.append(f"| {time_str} | {icon} | {note} |")

    return "\n".join(lines) + "\n"


@app.get("/api/export", response_class=PlainTextResponse)
async def export_all(start: str = None, end: str = None):
    """기간별 전체 리포트."""
    with get_db() as db:
        query = "SELECT * FROM incidents"
        params = []
        conditions = []
        if start:
            conditions.append("fired_at >= ?")
            params.append(start)
        if end:
            conditions.append("fired_at <= ?")
            params.append(end)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY fired_at DESC"
        incidents = db.execute(query, params).fetchall()

    total = len(incidents)
    resolved = [i for i in incidents if i["status"] == "resolved"]
    active = [i for i in incidents if i["status"] != "resolved"]

    # 평균 인지시간, 해결시간 계산
    ack_times = []
    resolve_times = []
    for inc in resolved:
        try:
            t_fire = datetime.strptime(inc["fired_at"], "%Y-%m-%d %H:%M:%S")
            if inc["acknowledged_at"]:
                t_ack = datetime.strptime(inc["acknowledged_at"], "%Y-%m-%d %H:%M:%S")
                ack_times.append((t_ack - t_fire).total_seconds() / 60)
            if inc["resolved_at"]:
                t_res = datetime.strptime(inc["resolved_at"], "%Y-%m-%d %H:%M:%S")
                resolve_times.append((t_res - t_fire).total_seconds() / 60)
        except Exception:
            pass

    avg_ack = f"{sum(ack_times)/len(ack_times):.0f}분" if ack_times else "-"
    avg_resolve = f"{sum(resolve_times)/len(resolve_times):.0f}분" if resolve_times else "-"

    period = ""
    if start or end:
        period = f" ({start or '~'} ~ {end or '~'})"

    lines = [
        f"# 인시던트 요약 리포트{period}",
        "",
        "## 통계",
        "",
        "| 항목 | 값 |",
        "|------|------|",
        f"| 총 인시던트 | {total}건 |",
        f"| 해결 | {len(resolved)}건 |",
        f"| 미해결 | {len(active)}건 |",
        f"| 평균 인지 시간 | {avg_ack} |",
        f"| 평균 해결 시간 | {avg_resolve} |",
        "",
    ]

    if active:
        lines.extend([
            "## 현재 미해결 인시던트",
            "",
            "| 발생 시각 | 알람 | 심각도 | 상태 |",
            "|-----------|------|--------|------|",
        ])
        for inc in active:
            lines.append(f"| {inc['fired_at']} | {inc['alert_name']} | {inc['severity']} | {inc['status']} |")
        lines.append("")

    lines.extend([
        "## 전체 인시던트 목록",
        "",
        "| 발생 | 해소 | 알람 | 심각도 | 해결방법 |",
        "|------|------|------|--------|----------|",
    ])
    for inc in incidents:
        resolved_at = inc["resolved_at"] or "-"
        res_type = inc["resolution_type"] or "-"
        lines.append(f"| {inc['fired_at']} | {resolved_at} | {inc['alert_name']} | {inc['severity']} | {res_type} |")

    return "\n".join(lines) + "\n"


# ── 웹 UI ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def ui_index(
    request: Request, page: int = 1,
    date_from: str = "", date_to: str = "",
    alert_name: str = "", severity: str = "",
):
    """메인 뷰 — 인시던트 목록 (페이지네이션 + 날짜/알람/심각도 필터)."""
    per_page = 20
    offset = (max(1, page) - 1) * per_page

    with get_db() as db:
        # 활성 인시던트 필터
        active_where = ["status != 'resolved'"]
        active_params: list = []
        if alert_name:
            active_where.append("alert_name = ?")
            active_params.append(alert_name)
        if severity:
            active_where.append("severity = ?")
            active_params.append(severity)
        active = db.execute(
            f"SELECT * FROM incidents WHERE {' AND '.join(active_where)} ORDER BY fired_at DESC",
            active_params
        ).fetchall()

        # 해결된 인시던트 (날짜 필터 + 페이지네이션)
        where = ["status = 'resolved'"]
        params: list = []
        if date_from:
            where.append("fired_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("fired_at <= ?")
            params.append(date_to + " 23:59:59")
        if alert_name:
            where.append("alert_name = ?")
            params.append(alert_name)
        if severity:
            where.append("severity = ?")
            params.append(severity)

        where_sql = " AND ".join(where)
        total = db.execute(f"SELECT COUNT(*) c FROM incidents WHERE {where_sql}", params).fetchone()["c"]
        resolved = db.execute(
            f"SELECT * FROM incidents WHERE {where_sql} ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()

        # 필터 선택지: 전체 알람명/심각도 목록
        alert_names = [r[0] for r in db.execute(
            "SELECT DISTINCT alert_name FROM incidents ORDER BY alert_name"
        ).fetchall()]
        severities = [r[0] for r in db.execute(
            "SELECT DISTINCT severity FROM incidents ORDER BY severity"
        ).fetchall()]

    total_pages = max(1, (total + per_page - 1) // per_page)

    def _parse_labels(row):
        d = dict(row)
        try:
            d["_labels"] = json.loads(d.get("labels") or "{}")
        except Exception:
            d["_labels"] = {}
        return d

    return templates.TemplateResponse("index.html", {
        "request": request,
        "active": [_parse_labels(r) for r in active],
        "resolved": [_parse_labels(r) for r in resolved],
        "now": now_kst(),
        "page": page,
        "total_pages": total_pages,
        "total_resolved": total,
        "date_from": date_from,
        "date_to": date_to,
        "alert_name": alert_name,
        "severity": severity,
        "alert_names": alert_names,
        "severities": severities,
    })


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
async def ui_detail(request: Request, incident_id: int):
    """상세 뷰 — 타임라인."""
    with get_db() as db:
        incident = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404)
        events = db.execute(
            "SELECT * FROM timeline_events WHERE incident_id = ? ORDER BY created_at ASC",
            (incident_id,)
        ).fetchall()
    inc_dict = dict(incident)
    try:
        inc_dict["_labels"] = json.loads(inc_dict.get("labels") or "{}")
    except Exception:
        inc_dict["_labels"] = {}
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "incident": inc_dict,
        "events": [dict(e) for e in events],
        "event_icons": EVENT_ICONS,
    })


@app.get("/incidents/{incident_id}/report", response_class=HTMLResponse)
async def ui_report(request: Request, incident_id: int):
    """장애 보고서 작성 뷰."""
    with get_db() as db:
        incident = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404)
        events = db.execute(
            "SELECT * FROM timeline_events WHERE incident_id = ? ORDER BY created_at ASC",
            (incident_id,)
        ).fetchall()

    inc_dict = dict(incident)
    events_list = [dict(e) for e in events]

    # 장애 시간 계산
    duration = ""
    if inc_dict["resolved_at"] and inc_dict["fired_at"]:
        try:
            t1 = datetime.strptime(inc_dict["fired_at"], "%Y-%m-%d %H:%M:%S")
            t2 = datetime.strptime(inc_dict["resolved_at"], "%Y-%m-%d %H:%M:%S")
            total_min = int((t2 - t1).total_seconds() / 60)
            hours, mins = divmod(total_min, 60)
            duration = f"{hours}시간 {mins}분" if hours else f"{mins}분"
        except Exception:
            pass

    return templates.TemplateResponse("report.html", {
        "request": request,
        "incident": inc_dict,
        "events_json": json.dumps(events_list, ensure_ascii=False),
        "duration": duration,
    })


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()


if __name__ == "__main__":
    import uvicorn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
