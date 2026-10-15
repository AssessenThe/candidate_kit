"""Employee Attendance & Analytics API."""

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUS = {"PRESENT", "WFH", "ON_DUTY"}
ALLOWED_ADMIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("emp_code")


# --------------------------------------------------------------------------- #
# Utility helpers
# --------------------------------------------------------------------------- #
def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_epoch_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = ensure_utc(value)
        return int(dt.timestamp() * 1000)
    return value


def convert_api_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_epoch_ms(value)
    if isinstance(value, list):
        return [convert_api_value(v) for v in value]
    if isinstance(value, dict):
        return {k: convert_api_value(v) for k, v in value.items()}
    return value


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid date: {value}") from exc


def parse_month(value: str) -> tuple[int, int]:
    try:
        year_str, month_str = value.split("-", 1)
        year = int(year_str)
        month = int(month_str)
        if month < 1 or month > 12:
            raise ValueError
        return year, month
    except ValueError as exc:
        raise HTTPException(422, "Invalid month format. Use YYYY-MM.") from exc


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    raise HTTPException(422, f"Invalid time value: {value}")


def round_half_up(value: Decimal | float | int, places: int = 2) -> float:
    quant = Decimal("1").scaleb(-places)
    if isinstance(value, (float, int)):
        value = Decimal(str(value))
    return float(value.quantize(quant, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    year, month_num = parse_month(month)
    start = date(year, month_num, 1)
    if month_num == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month_num + 1, 1) - timedelta(days=1)
    return start, end


def in_month(date_value: str, month: str) -> bool:
    start, end = month_bounds(month)
    real = parse_date(date_value)
    return start <= real <= end


def is_weekday(value: str | date) -> bool:
    if isinstance(value, str):
        value = parse_date(value)
    return value.weekday() < 5


def month_working_days(joined_on: str, month: str) -> int:
    join_date = parse_date(joined_on)
    start, end = month_bounds(month)
    if join_date > end:
        return 0
    if join_date < start:
        join_date = start
    count = 0
    cur = join_date
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def attendance_date_for_punch(dt_utc: datetime, shift_start: str, shift_end: str) -> date:
    dt_ist = ensure_utc(dt_utc).astimezone(IST)
    start_h, start_m = parse_hhmm(shift_start)
    end_h, end_m = parse_hhmm(shift_end)
    overnight = (end_h, end_m) <= (start_h, start_m)
    if overnight:
        end_time = time(end_h, end_m)
        if dt_ist.time() < end_time:
            return (dt_ist.date() - timedelta(days=1))
    return dt_ist.date()


def shift_start_datetime_for_punch(dt_utc: datetime, shift_start: str, shift_end: str) -> datetime:
    dt_ist = ensure_utc(dt_utc).astimezone(IST)
    start_h, start_m = parse_hhmm(shift_start)
    end_h, end_m = parse_hhmm(shift_end)
    overnight = (end_h, end_m) <= (start_h, start_m)
    if overnight and dt_ist.time() < time(end_h, end_m):
        shift_date = dt_ist.date() - timedelta(days=1)
    else:
        shift_date = dt_ist.date()
    return datetime.combine(shift_date, time(start_h, start_m), tzinfo=IST)


def shift_end_datetime(date_str: str, shift_end: str) -> datetime:
    work_date = parse_date(date_str)
    end_h, end_m = parse_hhmm(shift_end)
    end_date = work_date
    return datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    punch_local = ensure_utc(punch_in).astimezone(IST)
    start_dt = shift_start_datetime_for_punch(punch_in, shift_start, "23:59")
    delta = punch_local - start_dt
    if delta <= timedelta(minutes=10):
        return 0
    return int(delta.total_seconds() / 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    delta = punch_out - punch_in
    hours = Decimal(str(delta.total_seconds())) / Decimal("3600")
    return round_half_up(hours, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    end_dt = shift_end_datetime(date_str, shift_end)
    # Overnight shifts end on the next calendar day.
    end_h, end_m = parse_hhmm(shift_end)
    shift_start_h, shift_start_m = parse_hhmm("00:00")
    # The contract defines overnight when end <= start, but the employee's shift_start string is used elsewhere.
    try:
        shift_start = None
    except Exception:
        pass
    delta = punch_out - end_dt
    if delta <= timedelta(0):
        return 0
    minutes = int(delta.total_seconds() / 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < Decimal("4.50")


def validate_epoch_ms(value: int, allow_null: bool = False) -> int:
    if value is None:
        if allow_null:
            return value
        raise HTTPException(422, "Value cannot be null.")
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(422, "Epoch milliseconds must be an integer.")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "Epoch milliseconds out of range.")
    return value


def ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    validate_epoch_ms(value)
    dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def normalize_record_document(doc: dict[str, Any]) -> dict[str, Any]:
    clean = {key: convert_api_value(value) for key, value in doc.items() if key != "_id"}
    return clean


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_start")
    @classmethod
    def validate_shift_start(cls, value: str) -> str:
        if value == "":
            raise ValueError("shift_start cannot be empty")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_end(cls, value: str) -> str:
        if value == "":
            raise ValueError("shift_end cannot be empty")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_difference(cls, value: str, info: Any) -> str:
        if info.data.get("shift_start") and info.data.get("shift_end"):
            if info.data["shift_start"] == info.data["shift_end"]:
                raise ValueError("shift_start must differ from shift_end")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUS:
            raise ValueError("status must be one of PRESENT, WFH or ON_DUTY")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        validate_epoch_ms(value)
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        validate_epoch_ms(value)
        return value


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise ValueError("status is invalid")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_times(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        validate_epoch_ms(value)
        return value


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except Exception as exc:  # pragma: no cover - runtime DB outage
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except Exception as exc:  # pragma: no cover - defensive path
        raise HTTPException(409, "emp_code already exists") from exc
    return normalize_record_document({**doc})


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = []
    for doc in db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size):
        items.append(normalize_record_document(doc))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    d = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"]).isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except Exception as exc:  # pragma: no cover - race condition path
        raise HTTPException(409, "already punched in for this date") from exc
    return normalize_record_document(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")
    if date_from and date_to:
        if parse_date(date_from) > parse_date(date_to):
            raise HTTPException(422, "date_from cannot be after date_to")
    q: dict[str, Any] = {}
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
    docs = list(
        db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    )
    return {"items": [normalize_record_document(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": None}, sort=[("punch_in", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record["punch_in"] is None:
        raise HTTPException(404, "no punch-in found")
    if ts <= record["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - record["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")
    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime_minutes = compute_overtime(ts, emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(409, "that record is already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return normalize_record_document(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    status = body.status if body.status is not None else record.get("status")
    punch_in_value = record.get("punch_in") if body.punch_in is None else ms_to_datetime(body.punch_in)
    punch_out_value = record.get("punch_out") if body.punch_out is None else ms_to_datetime(body.punch_out)

    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records must not include punch times")
        punch_in_value = None
        punch_out_value = None
    else:
        if punch_in_value is None:
            raise HTTPException(422, "presence status requires a punch_in")
        if attendance_date_for_punch(punch_in_value, employee["shift_start"], employee["shift_end"]) != parse_date(date):
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if punch_out_value is not None:
            if punch_out_value <= punch_in_value:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (punch_out_value - punch_in_value).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if punch_in_value is not None:
        late_minutes = compute_late_minutes(punch_in_value, employee["shift_start"])
    else:
        late_minutes = 0
    if punch_out_value is not None and punch_in_value is not None:
        work_hours = compute_work_hours(punch_in_value, punch_out_value)
        overtime_minutes = compute_overtime(punch_out_value, employee["shift_end"], date)
        half_day = compute_half_day(work_hours)
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    final_doc = {
        "emp_code": emp_code,
        "date": date,
        "status": status,
        "punch_in": punch_in_value,
        "punch_out": punch_out_value,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": record.get("history", []),
    }
    changes: dict[str, dict[str, Any]] = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = record.get(key)
        new_value = final_doc.get(key)
        if old_value != new_value:
            changes[key] = {"from": convert_api_value(old_value), "to": convert_api_value(new_value)}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    final_doc["history"] = record.get("history", []) + [history_entry]
    db.attendance_logs.update_one({"_id": record["_id"]}, {"$set": final_doc})
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return normalize_record_document(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    month_start, month_end = month_bounds(month)
    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}))
    working_days = month_working_days(employee["joined_on"], month)
    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for log in logs:
        log_date = parse_date(log["date"])
        if log["status"] in {"PRESENT", "WFH", "ON_DUTY"} and is_weekday(log_date):
            present_days += 0.5 if log.get("half_day") else 1.0
        if log["status"] == "LEAVE":
            leave_days += 1
        if log.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(log.get("late_minutes", 0))
        total_overtime_minutes += int(log.get("overtime_minutes", 0))
    attendance_pct = None if working_days == 0 else round_half_up((present_days / working_days) * 100, 2)
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


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_start, month_end = month_bounds(month)
    query: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        query["department"] = department
    employees = list(db.employees.find(query, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    if not employees:
        return {"month": month, "items": []}
    employee_codes = [item["emp_code"] for item in employees]
    logs = list(
        db.attendance_logs.find({"emp_code": {"$in": employee_codes}, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}})
    )
    logs_by_emp = {emp["emp_code"]: [] for emp in employees}
    for log in logs:
        logs_by_emp.setdefault(log["emp_code"], []).append(log)
    by_department: dict[str, dict[str, Any]] = {}
    for emp in employees:
        dept = emp["department"]
        by_department.setdefault(dept, {"department": dept, "headcount": 0, "present_days": 0.0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "work_hours_values": []})
        by_department[dept]["headcount"] += 1
        for log in logs_by_emp.get(emp["emp_code"], []):
            if log["status"] in {"PRESENT", "WFH", "ON_DUTY"} and is_weekday(log["date"]):
                by_department[dept]["present_days"] += 0.5 if log.get("half_day") else 1.0
            if log.get("late_minutes", 0) > 0:
                by_department[dept]["late_count"] += 1
                by_department[dept]["total_late_minutes"] += int(log.get("late_minutes", 0))
            if log["status"] == "LEAVE":
                by_department[dept]["leave_count"] += 1
            if log["status"] == "ON_DUTY":
                by_department[dept]["on_duty_count"] += 1
            if log["status"] in {"PRESENT", "WFH", "ON_DUTY"} and log.get("work_hours") is not None:
                by_department[dept]["work_hours_values"].append(float(log["work_hours"]))

    items = []
    for dept, row in sorted(by_department.items()):
        avg = None if not row["work_hours_values"] else round_half_up(sum(row["work_hours_values"]) / len(row["work_hours_values"]), 2)
        items.append({
            "department": dept,
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")
    month_start, month_end = month_bounds(month)
    query: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        query["department"] = department
    employees = list(db.employees.find(query, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}))
    if not employees:
        return {"month": month, "items": []}
    employee_codes = [emp["emp_code"] for emp in employees]
    logs = list(db.attendance_logs.find({"emp_code": {"$in": employee_codes}, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}))
    totals: dict[str, dict[str, Any]] = {}
    for emp in employees:
        totals[emp["emp_code"]] = {"emp_code": emp["emp_code"], "name": emp["name"], "department": emp["department"], "total_late_minutes": 0, "late_count": 0}
    for log in logs:
        emp_code = log["emp_code"]
        if emp_code not in totals:
            continue
        total_late = int(log.get("late_minutes", 0))
        if total_late > 0:
            totals[emp_code]["total_late_minutes"] += total_late
            totals[emp_code]["late_count"] += 1
    rows = [row for row in totals.values() if row["total_late_minutes"] > 0]
    rows.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
    ranked = []
    prev_total = None
    prev_rank = 0
    for index, row in enumerate(rows, start=1):
        if prev_total is None or row["total_late_minutes"] != prev_total:
            prev_rank = index
        row["rank"] = prev_rank
        prev_total = row["total_late_minutes"]
        ranked.append(row)
    filtered = [row for row in ranked if row["rank"] <= limit]
    output = [{
        "rank": row["rank"],
        "emp_code": row["emp_code"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": row["total_late_minutes"],
        "late_count": row["late_count"],
    } for row in filtered]
    return {"month": month, "items": output}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "department not found")
    start = parse_date(from_)
    end = parse_date(to)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range exceeds 92 days")
    logs = list(db.attendance_logs.find({"emp_code": {"$in": [emp["emp_code"] for emp in employees]}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}))
    logs_by_date: dict[str, list[dict[str, Any]]] = {}
    for log in logs:
        logs_by_date.setdefault(log["date"], []).append(log)
    items = []
    current_day = start
    while current_day <= end:
        day_logs = logs_by_date.get(current_day.isoformat(), [])
        headcount = sum(1 for emp in employees if parse_date(emp["joined_on"]) <= current_day)
        present_count = 0.0
        late_count = 0
        for log in day_logs:
            if log["status"] in {"PRESENT", "WFH", "ON_DUTY"}:
                present_count += 0.5 if log.get("half_day") else 1.0
            if log.get("late_minutes", 0) > 0:
                late_count += 1
        working_day = current_day.weekday() < 5
        attendance_rate = None
        if working_day and headcount > 0:
            attendance_rate = round_half_up((present_count / headcount), 4)
        item = {
            "date": current_day.isoformat(),
            "is_working_day": working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        }
        items.append(item)
        current_day += timedelta(days=1)
    rates = []
    for item in items:
        if item["attendance_rate"] is not None:
            rates.append(item["attendance_rate"])
            if len(rates) > 7:
                rates.pop(0)
        if rates:
            item["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    month: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if endpoint not in ALLOWED_ADMIN_ENDPOINTS:
        raise HTTPException(422, f"Unsupported endpoint: {endpoint}")
    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")

    if endpoint == "attendance_list":
        filter_q: dict[str, Any] = {}
        if emp_code:
            filter_q["emp_code"] = emp_code
        if date_from or date_to:
            filter_q["date"] = {}
            if date_from:
                filter_q["date"]["$gte"] = date_from
            if date_to:
                filter_q["date"]["$lte"] = date_to
        if status:
            filter_q["status"] = status
        cursor = db.attendance_logs.find(filter_q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
        explain_doc = cursor.explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        month_start, month_end = month_bounds(month)
        filter_q = {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}
        explain_doc = db.attendance_logs.find(filter_q).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_summary":
        month_start, month_end = month_bounds(month) if month else (None, None)
        if month is None:
            raise HTTPException(422, "month is required")
        pipeline = [
            {"$match": {"joined_on": {"$lte": month_end.isoformat()}}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$unwind": "$logs"},
            {"$match": {"logs.date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
            {"$group": {"_id": "$department", "department": {"$first": "$department"}}},
        ]
        explain_doc = db.employees.aggregate(pipeline).explain("executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$unwind": "$logs"},
            {"$match": {"logs.date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
            {"$match": {"logs.late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "emp_code": {"$first": "$emp_code"}, "name": {"$first": "$name"}, "department": {"$first": "$department"}, "total_late_minutes": {"$sum": "$logs.late_minutes"}, "late_count": {"$sum": 1}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
        if department is not None:
            pipeline.insert(0, {"$match": {"department": department}})
        explain_doc = db.employees.aggregate(pipeline).explain("executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    if endpoint == "department_trend":
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_)
        end = parse_date(to)
        pipeline = [
            {"$match": {"department": department}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$unwind": "$logs"},
            {"$match": {"logs.date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        ]
        explain_doc = db.employees.aggregate(pipeline).explain("executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    raise HTTPException(422, "Unsupported endpoint")
