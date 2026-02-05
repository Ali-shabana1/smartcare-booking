from datetime import datetime, date, timedelta
from typing import List, Optional, Dict
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -------------------------
# App config
# -------------------------
app = FastAPI(title="SmartCare Booking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Booking rules
# -------------------------
ALLOWED_MONTHS_AHEAD = 3  # current month + next 3 months

WORK_START_HOUR = 9
WORK_END_HOUR = 17
SLOT_MINUTES = 30

def generate_slots() -> List[str]:
    slots = []
    t = datetime(2000, 1, 1, WORK_START_HOUR, 0)
    end = datetime(2000, 1, 1, WORK_END_HOUR, 0)
    while t < end:
        slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_MINUTES)
    return slots

ALL_SLOTS = generate_slots()
DAILY_CAPACITY = len(ALL_SLOTS)

def first_day_of_month(d: date) -> date:
    return d.replace(day=1)

def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, 1)

def is_month_allowed(ym: str) -> bool:
    today = date.today()
    cur = first_day_of_month(today)
    max_month = add_months(cur, ALLOWED_MONTHS_AHEAD)
    try:
        req = datetime.strptime(ym, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM")
    return cur <= req <= max_month

def is_date_allowed(ds: str) -> bool:
    today = date.today()
    cur = first_day_of_month(today)
    max_month = add_months(cur, ALLOWED_MONTHS_AHEAD)
    try:
        req = datetime.strptime(ds, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    if req < today:
        return False
    if req < cur:
        return False
    if req >= add_months(max_month, 1):
        return False
    return True

# -------------------------
# SQLite setup
# -------------------------
DB_PATH = "smartcare.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        situation_type TEXT NOT NULL,
        service_id INTEGER NOT NULL,
        appt_date TEXT NOT NULL,
        appt_time TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(service_id) REFERENCES services(id)
    )
    """)

    # Seed services if empty
    cur.execute("SELECT COUNT(*) AS c FROM services")
    if cur.fetchone()["c"] == 0:
        cur.executemany(
            "INSERT INTO services (id, name, duration_minutes) VALUES (?, ?, ?)",
            [
                (1, "General Consultation", 30),
                (2, "Lab Services", 30),
                (3, "Follow-up Consultation", 30),
            ],
        )

    conn.commit()
    conn.close()

@app.on_event("startup")
def on_startup():
    init_db()

def get_service(service_id: int) -> sqlite3.Row:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM services WHERE id = ?", (service_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid service_id")
    return row

def count_booked_for_day(service_id: int, appt_date: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) AS c
        FROM appointments
        WHERE status='booked'
          AND service_id=?
          AND appt_date=?
    """, (service_id, appt_date))
    c = cur.fetchone()["c"]
    conn.close()
    return c

def booked_times(appt_date: str, service_id: int) -> List[str]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT appt_time
        FROM appointments
        WHERE status='booked'
          AND service_id=?
          AND appt_date=?
        ORDER BY appt_time
    """, (service_id, appt_date))
    times = [r["appt_time"] for r in cur.fetchall()]
    conn.close()
    return times

# -------------------------
# API models
# -------------------------
class AppointmentCreate(BaseModel):
    patient_name: str = Field(min_length=2, max_length=60)
    phone: str = Field(min_length=6, max_length=20)
    situation_type: str
    service_id: int
    appt_date: str  # YYYY-MM-DD
    appt_time: str  # HH:MM

class AppointmentOut(BaseModel):
    id: int
    patient_name: str
    phone: str
    situation_type: str
    service_id: int
    service_name: str
    appt_date: str
    appt_time: str
    status: str
    created_at: str

class CancelResult(BaseModel):
    ok: bool

# -------------------------
# Routes
# -------------------------
@app.get("/")
def root():
    return {"message": "SmartCare Booking backend is running"}

@app.get("/services")
def get_services():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, duration_minutes FROM services ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@app.get("/time-slots")
def get_time_slots():
    return {"slots": ALL_SLOTS, "daily_capacity": DAILY_CAPACITY}

@app.get("/booked-times")
def get_booked_times(appt_date: str = Query(...), service_id: int = Query(...)):
    if not is_date_allowed(appt_date):
        raise HTTPException(status_code=400, detail="Date not allowed. Use today or within next 3 months.")
    get_service(service_id)
    return {"appt_date": appt_date, "service_id": service_id, "booked_times": booked_times(appt_date, service_id)}

@app.get("/calendar-load")
def calendar_load(month: str = Query(...), service_id: Optional[int] = Query(None)):
    if not is_month_allowed(month):
        raise HTTPException(status_code=400, detail="Month not allowed. Only current to next 3 months.")

    first_day = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if first_day.month == 12:
        next_month = first_day.replace(year=first_day.year + 1, month=1, day=1)
    else:
        next_month = first_day.replace(month=first_day.month + 1, day=1)

    # Count per day for this month (for selected service)
    counts: Dict[str, int] = {}
    if service_id is not None:
        get_service(service_id)
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            SELECT appt_date, COUNT(*) AS c
            FROM appointments
            WHERE status='booked'
              AND service_id=?
              AND appt_date >= ?
              AND appt_date < ?
            GROUP BY appt_date
        """, (service_id, first_day.strftime("%Y-%m-%d"), next_month.strftime("%Y-%m-%d")))
        for r in cur.fetchall():
            counts[r["appt_date"]] = r["c"]
        conn.close()

    def level(c: int) -> str:
        if c <= 5:
            return "Low"
        if c <= 12:
            return "Medium"
        return "High"

    days = []
    d = first_day
    today = date.today()
    while d < next_month:
        ds = d.strftime("%Y-%m-%d")
        c = counts.get(ds, 0)

        is_full = False
        if service_id is not None and c >= DAILY_CAPACITY:
            is_full = True

        is_past = d < today
        disabled = is_past or is_full

        days.append({
            "date": ds,
            "count": c,
            "level": level(c),
            "is_full": is_full,
            "disabled": disabled
        })
        d += timedelta(days=1)

    return {"month": month, "service_id": service_id, "daily_capacity": DAILY_CAPACITY, "days": days}

@app.post("/appointments", response_model=AppointmentOut)
def create_appointment(payload: AppointmentCreate):
    if not is_date_allowed(payload.appt_date):
        raise HTTPException(status_code=400, detail="Date not allowed. Use today or within next 3 months.")

    service = get_service(payload.service_id)

    if payload.appt_time not in ALL_SLOTS:
        raise HTTPException(status_code=400, detail="Invalid time slot")

    # FULL day protection
    if count_booked_for_day(payload.service_id, payload.appt_date) >= DAILY_CAPACITY:
        raise HTTPException(status_code=409, detail="This day is FULL for the selected service")

    conn = db()
    cur = conn.cursor()

    # Prevent double booking (same service + date + time)
    cur.execute("""
        SELECT COUNT(*) AS c FROM appointments
        WHERE status='booked'
          AND service_id=?
          AND appt_date=?
          AND appt_time=?
    """, (payload.service_id, payload.appt_date, payload.appt_time))
    if cur.fetchone()["c"] > 0:
        conn.close()
        raise HTTPException(status_code=409, detail="This time slot is already booked")

    created_at = datetime.now().isoformat(timespec="seconds")
    cur.execute("""
        INSERT INTO appointments (patient_name, phone, situation_type, service_id, appt_date, appt_time, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'booked', ?)
    """, (
        payload.patient_name.strip(),
        payload.phone.strip(),
        payload.situation_type,
        payload.service_id,
        payload.appt_date,
        payload.appt_time,
        created_at
    ))
    appt_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": appt_id,
        "patient_name": payload.patient_name.strip(),
        "phone": payload.phone.strip(),
        "situation_type": payload.situation_type,
        "service_id": payload.service_id,
        "service_name": service["name"],
        "appt_date": payload.appt_date,
        "appt_time": payload.appt_time,
        "status": "booked",
        "created_at": created_at
    }

@app.get("/appointments", response_model=List[AppointmentOut])
def list_appointments(phone: str = Query(...)):
    phone = phone.strip()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.*, s.name AS service_name
        FROM appointments a
        JOIN services s ON s.id = a.service_id
        WHERE a.phone = ?
        ORDER BY a.appt_date DESC, a.appt_time DESC
    """, (phone,))
    rows = cur.fetchall()
    conn.close()

    return [{
        "id": r["id"],
        "patient_name": r["patient_name"],
        "phone": r["phone"],
        "situation_type": r["situation_type"],
        "service_id": r["service_id"],
        "service_name": r["service_name"],
        "appt_date": r["appt_date"],
        "appt_time": r["appt_time"],
        "status": r["status"],
        "created_at": r["created_at"]
    } for r in rows]

@app.patch("/appointments/{appt_id}/cancel", response_model=CancelResult)
def cancel_appointment(appt_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, status FROM appointments WHERE id=?", (appt_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Appointment not found")

    if row["status"] == "cancelled":
        conn.close()
        return {"ok": True}

    cur.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appt_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


