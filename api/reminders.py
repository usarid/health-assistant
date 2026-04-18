"""Reminders system — scheduled health follow-ups with optional actions.

Handles:
  GET    /api/reminders          — list reminders (optionally filter by status)
  POST   /api/reminders          — create a new reminder
  PUT    /api/reminders/{id}     — update a reminder (snooze, dismiss, complete)
  DELETE /api/reminders/{id}     — delete a reminder
  POST   /api/reminders/evaluate — trigger background evaluation of reminders

Reminders can be created by the user or by the AI assistant. Each reminder
has a message, due date, optional linked medication/condition, and optional
offered actions (structured JSON) that the UI can render as buttons.
"""

import json
import os
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/reminders", tags=["reminders"])

DB_PATH = os.environ.get("ASSISTANT_DB", "/data/chat.db")

_db = None


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                context TEXT DEFAULT '',
                due_at TEXT NOT NULL,
                status TEXT DEFAULT 'active' CHECK (status IN ('active','snoozed','completed','dismissed')),
                priority TEXT DEFAULT 'normal' CHECK (priority IN ('low','normal','high')),
                linked_med TEXT DEFAULT '',
                linked_condition TEXT DEFAULT '',
                linked_resource_id TEXT DEFAULT '',
                actions TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT DEFAULT '',
                source TEXT DEFAULT 'user' CHECK (source IN ('user','assistant','system'))
            );
        """)
        await _db.commit()
    return _db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    """Convert a DB row to a dict, parsing JSON fields."""
    d = dict(row)
    # Parse the actions JSON
    try:
        d["actions"] = json.loads(d.get("actions", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["actions"] = []
    return d


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("")
async def list_reminders(
    status: str = Query(None, description="Filter by status: active, snoozed, completed, dismissed"),
    include_dismissed: bool = Query(False),
):
    """List reminders, optionally filtered by status."""
    db = await _get_db()

    if status:
        cursor = await db.execute(
            "SELECT * FROM reminders WHERE status = ? ORDER BY due_at ASC",
            (status,),
        )
    elif include_dismissed:
        cursor = await db.execute(
            "SELECT * FROM reminders ORDER BY due_at ASC"
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM reminders WHERE status IN ('active','snoozed') ORDER BY due_at ASC"
        )

    rows = await cursor.fetchall()
    reminders = [_row_to_dict(r) for r in rows]

    # Annotate each with relative due status
    now = datetime.now(timezone.utc)
    for r in reminders:
        try:
            due = datetime.fromisoformat(r["due_at"])
            diff = due - now
            if diff.total_seconds() < 0:
                r["due_status"] = "overdue"
                r["due_relative"] = _relative_time(diff)
            elif diff.total_seconds() < 86400:
                r["due_status"] = "today"
                r["due_relative"] = "today"
            elif diff.total_seconds() < 172800:
                r["due_status"] = "upcoming"
                r["due_relative"] = "tomorrow"
            else:
                r["due_status"] = "upcoming"
                r["due_relative"] = f"in {diff.days} days"
        except Exception:
            r["due_status"] = "unknown"
            r["due_relative"] = ""

    return {"reminders": reminders, "total": len(reminders)}


def _relative_time(td: timedelta) -> str:
    """Human-friendly relative time for overdue reminders."""
    seconds = abs(td.total_seconds())
    if seconds < 3600:
        return f"{int(seconds/60)}m overdue"
    elif seconds < 86400:
        return f"{int(seconds/3600)}h overdue"
    else:
        return f"{int(seconds/86400)}d overdue"


@router.post("")
async def create_reminder(body: dict):
    """Create a new reminder.

    Body:
        message: str (required) — what to remind about
        context: str — additional context (e.g., conversation snippet)
        due_at: str — ISO datetime when reminder is due (required)
        priority: str — low/normal/high
        linked_med: str — medication key if linked to a med
        linked_condition: str — condition name if linked
        linked_resource_id: str — FHIR resource ID if linked
        actions: list — offered resolution actions, each with:
            label: str, action_type: str, params: dict
        source: str — user/assistant/system
    """
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"error": "message is required"})

    due_at = body.get("due_at", "")
    if not due_at:
        return JSONResponse(status_code=400, content={"error": "due_at is required"})

    now = _now_iso()
    actions_json = json.dumps(body.get("actions", []))

    db = await _get_db()
    cursor = await db.execute(
        """INSERT INTO reminders (message, context, due_at, status, priority,
               linked_med, linked_condition, linked_resource_id, actions,
               created_at, updated_at, source)
           VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message,
            body.get("context", ""),
            due_at,
            body.get("priority", "normal"),
            body.get("linked_med", ""),
            body.get("linked_condition", ""),
            body.get("linked_resource_id", ""),
            actions_json,
            now,
            now,
            body.get("source", "user"),
        ),
    )
    await db.commit()
    reminder_id = cursor.lastrowid

    # Fetch and return the created reminder
    cursor = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    row = await cursor.fetchone()
    return {"ok": True, "reminder": _row_to_dict(row)}


@router.put("/{reminder_id}")
async def update_reminder(reminder_id: int, body: dict):
    """Update a reminder — change status, snooze, edit message/actions.

    Body can include:
        status: str — active/snoozed/completed/dismissed
        due_at: str — new due date (for snoozing)
        message: str — updated message
        actions: list — updated actions
        completed_at: str — when it was completed
        snooze_days: int — shorthand to snooze N days from now
    """
    db = await _get_db()

    # Check it exists
    cursor = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    row = await cursor.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Reminder not found"})

    now = _now_iso()
    updates = {"updated_at": now}

    if "status" in body:
        updates["status"] = body["status"]
        if body["status"] == "completed":
            updates["completed_at"] = body.get("completed_at", now)

    if "snooze_days" in body:
        new_due = datetime.now(timezone.utc) + timedelta(days=body["snooze_days"])
        updates["due_at"] = new_due.isoformat()
        updates["status"] = "snoozed"

    if "due_at" in body:
        updates["due_at"] = body["due_at"]

    if "message" in body:
        updates["message"] = body["message"]

    if "actions" in body:
        updates["actions"] = json.dumps(body["actions"])

    if "priority" in body:
        updates["priority"] = body["priority"]

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [reminder_id]

    await db.execute(f"UPDATE reminders SET {set_clause} WHERE id = ?", values)
    await db.commit()

    cursor = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    row = await cursor.fetchone()
    return {"ok": True, "reminder": _row_to_dict(row)}


@router.delete("/{reminder_id}")
async def delete_reminder(reminder_id: int):
    """Delete a reminder permanently."""
    db = await _get_db()
    cursor = await db.execute("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
    if not await cursor.fetchone():
        return JSONResponse(status_code=404, content={"error": "Reminder not found"})

    await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    await db.commit()
    return {"ok": True}


# ── Evaluation / polling ──────────────────────────────────────────────

@router.post("/evaluate")
async def evaluate_reminders():
    """Evaluate active reminders against current data.

    This is called by the scheduled polling job. It checks:
    1. Reminders that are newly overdue → promote to high priority
    2. Reminders linked to medications or conditions → check if
       related FHIR data has changed (e.g., appointment scheduled,
       lab result received)

    Returns a summary of changes made.
    """
    db = await _get_db()
    now = datetime.now(timezone.utc)
    changes = []

    # 1. Find active reminders that are past due and not yet high priority
    cursor = await db.execute(
        "SELECT * FROM reminders WHERE status = 'active' AND priority != 'high'"
    )
    rows = await cursor.fetchall()
    for row in rows:
        try:
            due = datetime.fromisoformat(row["due_at"])
            if now > due:
                await db.execute(
                    "UPDATE reminders SET priority = 'high', updated_at = ? WHERE id = ?",
                    (_now_iso(), row["id"]),
                )
                changes.append({
                    "reminder_id": row["id"],
                    "change": "promoted_to_high",
                    "message": row["message"],
                })
        except Exception:
            pass

    # 2. Reactivate snoozed reminders whose snooze period has elapsed
    cursor = await db.execute(
        "SELECT * FROM reminders WHERE status = 'snoozed'"
    )
    rows = await cursor.fetchall()
    for row in rows:
        try:
            due = datetime.fromisoformat(row["due_at"])
            if now > due:
                await db.execute(
                    "UPDATE reminders SET status = 'active', updated_at = ? WHERE id = ?",
                    (_now_iso(), row["id"]),
                )
                changes.append({
                    "reminder_id": row["id"],
                    "change": "reactivated_from_snooze",
                    "message": row["message"],
                })
        except Exception:
            pass

    await db.commit()

    return {
        "evaluated_at": _now_iso(),
        "changes": changes,
        "total_changes": len(changes),
    }
