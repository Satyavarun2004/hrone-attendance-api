"""
Employee Attendance & Analytics API - Complete Implementation
Conforms strictly to openapi.yaml (v2.0.0) and DATA_MODEL.md
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: Idempotent Index Creation
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    # 1. Employees unique index on emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # Supporting indexes for department and join date filtering
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # 2. Attendance logs unique compound index on (emp_code, date)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # For list_attendance: sorted by date desc, then emp_code asc
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # For punch-out lookup
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    # For analytics date-range and status lookups
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])


# --------------------------------------------------------------------------- #
# Math & Business Logic Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def truncate_to_seconds(ms: int) -> int:
    return (ms // 1000) * 1000


def ms_to_utc_datetime(ms: int) -> datetime:
    truncated_ms = truncate_to_seconds(ms)
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """R1: IST date of punch_in. Overnight shift: if punch-in IST < shift_end, belongs to previous day."""
    punch_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end_str <= shift_start_str

    if is_overnight:
        end_h, end_m = map(int, shift_end_str.split(":"))
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (end_h, end_m, 0):
            prev_date = (punch_ist.date() - timedelta(days=1)).isoformat()
            return prev_date

    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start_str: str, attendance_date_str: str) -> int:
    """R2: Grace period 10:00. Strictly > 10 min late counts elapsed minutes from shift_start."""
    punch_ist = punch_in_dt.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start_str.split(":"))
    shift_start_ist = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, 0, tzinfo=IST)

    diff_sec = (punch_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in sec / 3600, rounded 2 decimals half-up."""
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    raw_hours = diff_sec / 3600.0
    return round_half_up(raw_hours, 2)


def compute_overtime_minutes(punch_out_dt: datetime, shift_end_str: str, shift_start_str: str, attendance_date_str: str) -> int:
    """R3: whole minutes (floor) between shift_end and punch_out, counted only when >= 30."""
    punch_out_ist = punch_out_dt.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    end_h, end_m = map(int, shift_end_str.split(":"))

    is_overnight = shift_end_str <= shift_start_str
    if is_overnight:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_ist = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, end_h, end_m, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()

    if diff_sec >= 1800:  # >= 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def is_weekday_mon_fri(date_str: str) -> bool:
    d = date.fromisoformat(date_str)
    return d.weekday() < 5


def get_month_range(month_str: str):
    """Returns (first_date_str, last_date_str) for YYYY-MM."""
    year, month = map(int, month_str.split("-"))
    first_date = date(year, month, 1)
    if month == 12:
        last_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_date = date(year, month + 1, 1) - timedelta(days=1)
    return first_date.isoformat(), last_date.isoformat()


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_serialized = []
    for h in doc.get("history", []):
        entry_changes = {}
        for k, v in h.get("changes", {}).items():
            fr = v.get("from")
            to = v.get("to")
            if isinstance(fr, datetime):
                fr = datetime_to_ms(fr)
            if isinstance(to, datetime):
                to = datetime_to_ms(to)
            entry_changes[k] = {"from": fr, "to": to}

        h_at = h.get("at")
        if isinstance(h_at, datetime):
            h_at = datetime_to_ms(h_at)

        history_serialized.append({
            "at": h_at,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": entry_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_serialized,
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = datetime_to_ms(created_at)

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at,
    }


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
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise HTTPException(422, "shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


# --------------------------------------------------------------------------- #
# Employees Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_in_dt = ms_to_utc_datetime(punched_ms)

    att_date = get_attendance_date(punch_in_dt, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late_min = compute_late_minutes(punch_in_dt, emp.get("shift_start", "09:30"), att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_dt = ms_to_utc_datetime(punched_ms)

    # Find the most recent record whose punch_in is <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found for this punch-out")

    punch_in_dt = record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Atomic punch-out update: only update if punch_out is currently null
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime_minutes(
        punch_out_dt, emp.get("shift_end", "18:30"), emp.get("shift_start", "09:30"), record["date"]
    )
    half_day = compute_half_day(work_hrs)

    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
    )

    if res.modified_count == 0:
        raise HTTPException(409, "Record is already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

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
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}", tags=["attendance"])
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(...),
   date_str: str = Path(..., alias="date_str", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found for this date")

    # Validation: setting ABSENT or LEAVE clears both punch times
    new_status = body.status if body.status is not None else record["status"]
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = ms_to_utc_datetime(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(final_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        if calc_date != date_str:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            final_punch_out = ms_to_utc_datetime(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Compute new derived values
    if new_status in ["ABSENT", "LEAVE"]:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(final_punch_in, emp.get("shift_start", "09:30"), date_str)
        if final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime_minutes(
                final_punch_out, emp.get("shift_end", "18:30"), emp.get("shift_start", "09:30"), date_str
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect actual changes
    changes = {}
    if new_status != record.get("status"):
        changes["status"] = {"from": record.get("status"), "to": new_status}
    if final_punch_in != record.get("punch_in"):
        changes["punch_in"] = {"from": record.get("punch_in"), "to": final_punch_in}
    if final_punch_out != record.get("punch_out"):
        changes["punch_out"] = {"from": record.get("punch_out"), "to": final_punch_out}
    if new_work_hours != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, "No fields changed")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(record.get("history", []))
    res = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if res.modified_count == 0:
        raise HTTPException(409, "Concurrent modification detected")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def count_working_days(start_date: date, end_date: date) -> int:
    cur = start_date
    count = 0
    while cur <= end_date:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    m_start_str, m_end_str = get_month_range(month)
    m_start_date = date.fromisoformat(m_start_str)
    m_end_date = date.fromisoformat(m_end_str)
    joined_date = date.fromisoformat(emp["joined_on"])

    # Working days per R7: Mon-Fri from joined_on
    if joined_date > m_end_date:
        working_days = 0
    else:
        effective_start = max(m_start_date, joined_date)
        working_days = count_working_days(effective_start, m_end_date)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start_str, "$lte": m_end_str}}},
        {
            "$group": {
                "_id": None,
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "records": {"$push": {"date": "$date", "status": "$status", "half_day": "$half_day"}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))

    present_days = 0.0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    leave_days = 0

    if res:
        data = res[0]
        late_count = data.get("late_count", 0)
        total_late_minutes = data.get("total_late_minutes", 0)
        total_overtime_minutes = data.get("total_overtime_minutes", 0)
        leave_days = data.get("leave_days", 0)

        for r in data.get("records", []):
            if is_weekday_mon_fri(r["date"]) and r["status"] in ["PRESENT", "WFH", "ON_DUTY"]:
                present_days += 0.5 if r.get("half_day") is True else 1.0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    m_start_str, m_end_str = get_month_range(month)

    emp_match = {"joined_on": {"$lte": m_end_str}}
    if department:
        emp_match["department"] = department

    # 1. Group employees by department to count headcount
    dept_headcounts = {}
    for d in db.employees.aggregate([
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$sort": {"_id": ASCENDING}},
    ]):
        dept_headcounts[d["_id"]] = d["headcount"]

    if not dept_headcounts:
        return {"month": month, "items": []}

    # 2. Aggregate logs joined with employees
    log_match = {
        "date": {"$gte": m_start_str, "$lte": m_end_str},
    }

    pipeline = [
        {"$match": log_match},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": m_end_str}}},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.append({
        "$group": {
            "_id": "$emp.department",
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_list": {
                "$push": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$work_hours", None]},
                            ]
                        },
                        "$work_hours",
                        "$$REMOVE",
                    ]
                }
            },
            "records": {"$push": {"date": "$date", "status": "$status", "half_day": "$half_day"}},
        }
    })

    log_stats = {d["_id"]: d for d in db.attendance_logs.aggregate(pipeline)}

    items = []
    for dept_name in sorted(dept_headcounts.keys()):
        hc = dept_headcounts[dept_name]
        stats = log_stats.get(dept_name, {})

        present_days = 0.0
        for r in stats.get("records", []):
            if is_weekday_mon_fri(r["date"]) and r["status"] in ["PRESENT", "WFH", "ON_DUTY"]:
                present_days += 0.5 if r.get("half_day") is True else 1.0

        wh_list = stats.get("work_hours_list", [])
        avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2) if wh_list else None

        items.append({
            "department": dept_name,
            "headcount": hc,
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0),
            "leave_count": stats.get("leave_count", 0),
            "on_duty_count": stats.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    m_start_str, m_end_str = get_month_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": m_start_str, "$lte": m_end_str}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})

    results = list(db.attendance_logs.aggregate(pipeline))

    # Standard Competition Ranking (1, 2, 2, 4)
    ranked_items = []
    current_rank = 1
    for i, row in enumerate(results):
        if i > 0:
            prev = results[i - 1]
            if row["total_late_minutes"] < prev["total_late_minutes"]:
                current_rank = i + 1

        if current_rank > limit:
            break

        ranked_items.append({
            "rank": current_rank,
            "emp_code": row["_id"],
            "name": row["emp"]["name"],
            "department": row["emp"]["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })

    return {"month": month, "items": ranked_items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_start = date.fromisoformat(from_date)
    d_end = date.fromisoformat(to_date)
    if d_end < d_start:
        raise HTTPException(422, "to must not be earlier than from")
    if (d_end - d_start).days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")

    # 404 if department has no employees
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    # Fetch department employees joined_on
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))

    # Aggregate attendance for the department in the date range
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
    ]

    log_map = {d["_id"]: d for d in db.attendance_logs.aggregate(pipeline)}

    items = []
    rates = []
    cur = d_start
    while cur <= d_end:
        cur_str = cur.isoformat()
        is_working = cur.weekday() < 5
        hc = sum(1 for e in dept_employees if e["joined_on"] <= cur_str)

        log_data = log_map.get(cur_str, {})
        present_count = float(log_data.get("present_count", 0.0))
        late_count = int(log_data.get("late_count", 0))

        if is_working and hc > 0:
            rate = round_half_up(present_count / hc, 4)
        else:
            rate = None

        rates.append(rate)

        # 7-day moving average over available non-null rates in [max(0, idx-6), idx]
        window = [r for r in rates[-7:] if r is not None]
        moving_avg = round_half_up(sum(window) / len(window), 4) if window else None

        items.append({
            "date": cur_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be greater than date_to")
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
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        explain_res = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        m_start_str, m_end_str = get_month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start_str, "$lte": m_end_str}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        m_start_str, m_end_str = get_month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": m_start_str, "$lte": m_end_str}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.append({"$group": {"_id": "$emp.department", "count": {"$sum": 1}}})
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        m_start_str, m_end_str = get_month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": m_start_str, "$lte": m_end_str}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
        ]
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    raise HTTPException(422, "Unknown endpoint")