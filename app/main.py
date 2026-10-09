"""
Employee Attendance & Analytics API - HROne Engineering Assignment Solution

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment or local .env)
"""
import math
import os
import re
from datetime import datetime, date as date_cls, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Engineering Assignment Solution"
)


# --------------------------------------------------------------------------- #
# Startup Event: Idempotent Index Creation
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    """Create all required indexes idempotently on startup."""
    try:
        # Unique index on employee code
        db.employees.create_index([("emp_code", 1)], unique=True)
        # Index on department for filtering
        db.employees.create_index([("department", 1)])
        # Index on joined_on for headcount filters
        db.employees.create_index([("joined_on", 1)])
        # Compound index for department and joined_on
        db.employees.create_index([("department", 1), ("joined_on", 1)])

        # Unique compound index on attendance_logs (emp_code, date)
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        # Index for list attendance queries (sorted date desc, emp_code asc)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        # Index for list attendance with emp_code filter
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
        # Index for status and date range filtering
        db.attendance_logs.create_index([("status", 1), ("date", 1)])
        # Index for punch-out lookup
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        # Index for late leaderboard aggregation
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    except Exception as e:
        print(f"Error creating indexes: {e}")


# --------------------------------------------------------------------------- #
# Helpers & Business Rule Calculations
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def validate_epoch_ms(v: Optional[int]) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Timestamp must be an integer count of epoch milliseconds")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Timestamp out of range (epoch milliseconds)")
    return v


def parse_epoch_ms(ms: int) -> datetime:
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def get_attendance_date(ts_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Computes IST attendance date YYYY-MM-DD from UTC instant ts_utc according to R1."""
    ts_ist = ts_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(end_h, end_m)
        if ts_ist.time() < shift_end_time:
            return (ts_ist.date() - timedelta(days=1)).isoformat()
    return ts_ist.date().isoformat()


def compute_late_minutes(ts_utc: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """Computes late_minutes according to R2."""
    start_h, start_m = map(int, shift_start.split(":"))
    y, m, d = map(int, attendance_date.split("-"))
    shift_start_dt = datetime(y, m, d, start_h, start_m, tzinfo=IST)
    ts_ist = ts_utc.astimezone(IST)

    delta_sec = (ts_ist - shift_start_dt).total_seconds()
    if delta_sec > 600:
        return int(delta_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """Computes overtime_minutes according to R3."""
    end_h, end_m = map(int, shift_end.split(":"))
    y, m, d = map(int, attendance_date.split("-"))
    shift_end_dt = datetime(y, m, d, end_h, end_m, tzinfo=IST)
    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)

    ts_ist = punch_out_utc.astimezone(IST)
    delta_sec = (ts_ist - shift_end_dt).total_seconds()
    if delta_sec >= 1800:
        return int(delta_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Computes work_hours rounded half-up to 2 decimals (R4)."""
    seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    d_hours = Decimal(seconds) / Decimal(3600)
    return float(d_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Computes half_day boolean (R5)."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats attendance log document for API response according to OpenAPI schema."""
    res = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": []
    }

    raw_history = doc.get("history", [])
    formatted_history = []
    for h in raw_history:
        at_ms = datetime_to_ms(h["at"])
        changes = {}
        for field, c in h.get("changes", {}).items():
            f_val = c.get("from")
            t_val = c.get("to")
            if field in ("punch_in", "punch_out"):
                f_val = datetime_to_ms(f_val) if isinstance(f_val, datetime) else f_val
                t_val = datetime_to_ms(t_val) if isinstance(t_val, datetime) else t_val
            changes[field] = {"from": f_val, "to": t_val}
        formatted_history.append({
            "at": at_ms,
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes
        })
    res["history"] = formatted_history
    return res


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc["created_at"])
    }


def get_month_end_date(month_str: str) -> str:
    y, m = map(int, month_str.split("-"))
    if m == 12:
        d = date_cls(y, 12, 31)
    else:
        d = date_cls(y, m + 1, 1) - timedelta(days=1)
    return d.isoformat()


def compute_working_days(joined_on_str: str, year: int, month: int) -> int:
    m_start = date_cls(year, month, 1)
    if month == 12:
        m_end = date_cls(year, 12, 31)
    else:
        m_end = date_cls(year, month + 1, 1) - timedelta(days=1)

    j_date = date_cls.fromisoformat(joined_on_str)
    start_date = max(m_start, j_date)

    if start_date > m_end:
        return 0

    working_days = 0
    cur = start_date
    while cur <= m_end:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)

    return working_days


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    @classmethod
    def check_shifts(cls, v: str, info: Any) -> str:
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must be different")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def val_punched_at(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError(f"Invalid status '{v}' for punch-in")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def val_punched_at(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def val_times(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError(f"Invalid status '{v}'")
        return v


# --------------------------------------------------------------------------- #
# API Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"emp_code '{body.emp_code}' already exists")
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items_raw = list(
        db.employees.find(q)
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    items = [format_employee_doc(doc) for doc in items_raw]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    if body.punched_at is not None:
        ts_utc = parse_epoch_ms(body.punched_at)
    else:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        ts_utc = parse_epoch_ms(now_ms)

    att_date = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts_utc, att_date, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee '{body.emp_code}' already punched in for date '{att_date}'")

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    if body.punched_at is not None:
        ts_utc = parse_epoch_ms(body.punched_at)
    else:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        ts_utc = parse_epoch_ms(now_ms)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}},
        sort=[("punch_in", -1)]
    )

    if not record:
        raise HTTPException(404, f"No punch-in record found for employee '{body.emp_code}'")

    if record.get("punch_out") is not None:
        raise HTTPException(409, f"Record for employee '{body.emp_code}' on date '{record['date']}' is already punched out")

    punch_in_utc = record["punch_in"]
    if ts_utc <= punch_in_utc:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    if (ts_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(punch_in_utc, ts_utc)
    ot_mins = compute_overtime(ts_utc, record["date"], emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_hrs)

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": is_half
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "Record already punched out by a concurrent request")

    return format_attendance_doc(updated_doc)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, f"Attendance record for employee '{emp_code}' on date '{date}' not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in and punch_out must not be supplied when setting ABSENT or LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        if body.punch_in is not None:
            new_punch_in = parse_epoch_ms(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if not new_punch_in:
            raise HTTPException(422, f"Presence status '{new_status}' requires a punch_in time")

        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(422, f"punch_in instant belongs to attendance date '{calc_date}', not record date '{date}'")

        if body.punch_out is not None:
            new_punch_out = parse_epoch_ms(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, date, emp["shift_start"], emp["shift_end"])
        if new_punch_out is None:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
        else:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, date, emp["shift_start"], emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)

    changes = {}
    target_state = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day
    }

    for k, new_v in target_state.items():
        old_v = record.get(k)
        if k == "half_day" and old_v is None:
            old_v = False
        if k in ("late_minutes", "overtime_minutes") and old_v is None:
            old_v = 0

        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(422, "Regularization request does not change any fields")

    now_utc = datetime.now(UTC)

    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    existing_history = record.get("history", [])

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": existing_history
        },
        {
            "$set": target_state,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "Concurrent regularization modification detected")

    return format_attendance_doc(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    y, m = map(int, month.split("-"))
    working_days = compute_working_days(emp["joined_on"], y, m)

    start_date = f"{month}-01"
    end_date = f"{month}-31"

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        tot_late = int(data.get("total_late_minutes", 0))
        tot_ot = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        tot_late = 0
        tot_ot = 0

    if working_days > 0:
        att_pct = round_half_up(present_days / working_days * 100.0, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": tot_late,
        "total_overtime_minutes": tot_ot,
        "attendance_pct": att_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    month_start = f"{month}-01"
    month_end = get_month_end_date(month)

    emp_match = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        hc = r["headcount"]
        if hc == 0:
            continue
        wh_count = r["work_hours_count"]
        wh_sum = r["work_hours_sum"]
        avg_wh = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        items.append({
            "department": r["department"],
            "headcount": hc,
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"])
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    month_start = f"{month}-01"
    month_end = get_month_end_date(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(404, f"Department '{department}' not found")

    d_from = date_cls.fromisoformat(from_date)
    d_to = date_cls.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(422, "to date must be >= from date")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "day_offset": {"$range": [0, num_days]}
            }
        },
        {"$unwind": "$day_offset"},
        {
            "$project": {
                "date_iso": {
                    "$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": from_date}},
                        "unit": "day",
                        "amount": "$day_offset"
                    }
                }
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_iso", "timezone": "Asia/Kolkata"}
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc_docs"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$d"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {"emp.department": department}
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "date": 1,
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]
                },
                "dow": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]
                            }
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$dow", 2]},
                        {"$lte": ["$dow", 6]}
                    ]
                },
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$gte": ["$dow", 2]},
                                {"$lte": ["$dow", 6]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        }
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        rate = round_half_up(r.get("attendance_rate"), 4)
        m_avg = round_half_up(r.get("moving_avg_7d"), 4)

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })

    return {
        "department": department,
        "items": items
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_data = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_data}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly explain")
        start_date = f"{month}-01"
        end_date = f"{month}-31"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0
                            ]
                        }
                    }
                }
            }
        ]
        explain_data = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_data}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary explain")
        month_start = f"{month}-01"
        month_end = get_month_end_date(month)
        emp_match = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            }
        ]
        explain_data = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_data}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard explain")
        month_start = f"{month}-01"
        month_end = get_month_end_date(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0}
                }
            }
        ]
        explain_data = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_data}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend explain")
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1}
        ]
        explain_data = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_data}
