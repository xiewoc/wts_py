"""Fixed endpoints, campus metadata, period table and app-time helpers.

Mirrors ``src-tauri/src/config.rs``. Nothing here performs network I/O; the
constants are the only endpoints the core is ever allowed to reach.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .models import CampusMetadata, SlotMetadata

SJD_ORIGIN = "https://jwglweixin.bupt.edu.cn"
SJD_LOGIN_PAGE_URL = "https://jwglweixin.bupt.edu.cn/sjd/#/login"
SJD_REST_CLASSROOM_PAGE_URL = "https://jwglweixin.bupt.edu.cn/sjd/#/restClassroom"
SJD_STUDENT_CURRICULUM_URL = "https://jwglweixin.bupt.edu.cn/bjyddx/student/curriculum"
EMPTY_CLASSROOM_LOGIN_URL = "https://jwglweixin.bupt.edu.cn/bjyddx/login"
EMPTY_CLASSROOM_TODAY_URL = "https://jwglweixin.bupt.edu.cn/bjyddx/todayClassrooms"

# (start, end) for period 1..14; indices are zero-based in the contract.
SLOT_TIMES: tuple[tuple[str, str], ...] = (
    ("08:00", "08:45"),
    ("08:50", "09:35"),
    ("09:50", "10:35"),
    ("10:40", "11:25"),
    ("11:30", "12:15"),
    ("13:00", "13:45"),
    ("13:50", "14:35"),
    ("14:45", "15:30"),
    ("15:40", "16:25"),
    ("16:35", "17:20"),
    ("17:25", "18:10"),
    ("18:30", "19:15"),
    ("19:20", "20:05"),
    ("20:10", "20:55"),
)


@dataclass(frozen=True)
class Campus:
    id: str
    name: str


CAMPUSES: tuple[Campus, ...] = (
    Campus(id="01", name="西土城"),
    Campus(id="04", name="沙河"),
)

# Asia/Shanghai has had a fixed UTC+8 offset since 1991 and never observes DST,
# so a fixed offset keeps the port free of any tzdata dependency.
APP_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

_TERM_ID_PATTERN = re.compile(r"[0-9]{4}-[0-9]{4}-[12]\Z")


def suggested_term_for_date(value: date) -> tuple[str, str]:
    """Return ``(term_id, first_monday)`` for the term containing ``value``."""
    if 2 <= value.month <= 7:
        term_id = f"{value.year - 1}-{value.year}-2"
        anchor = date(value.year, 3, 2)
    else:
        fall_start_year = value.year - 1 if value.month == 1 else value.year
        term_id = f"{fall_start_year}-{fall_start_year + 1}-1"
        anchor = date(fall_start_year, 9, 1)
    monday = anchor - timedelta(days=anchor.weekday())
    return term_id, monday.isoformat()


def is_valid_term_id(value: str) -> bool:
    return _TERM_ID_PATTERN.fullmatch(value.strip()) is not None


def default_term() -> tuple[str, str]:
    return "", ""


def default_term_id() -> str:
    return default_term()[0]


def default_term_start_date() -> str:
    return default_term()[1]


def campuses_payload() -> list[CampusMetadata]:
    return [CampusMetadata(id=campus.id, name=campus.name) for campus in CAMPUSES]


def slot_payload() -> list[SlotMetadata]:
    return [
        SlotMetadata(index=index, label=str(index + 1), start=start, end=end)
        for index, (start, end) in enumerate(SLOT_TIMES)
    ]


def normalize_campus_id(campus_id: str | None) -> str:
    value = (campus_id if campus_id is not None else CAMPUSES[0].id).strip()
    if not value:
        return CAMPUSES[0].id
    if value.isdigit() and value.isascii():
        return value.zfill(2)
    return value


def campus_name(campus_id: str) -> str:
    normalized = normalize_campus_id(campus_id)
    for campus in CAMPUSES:
        if campus.id == normalized:
            return campus.name
    return f"校区 {normalized}"


def now_in_app_tz() -> str:
    """Second-precision RFC 3339 timestamp in the app timezone (no leap seconds)."""
    return datetime.now(timezone.utc).astimezone(APP_TZ).isoformat(timespec="seconds")


def today_in_app_tz() -> date:
    return datetime.now(timezone.utc).astimezone(APP_TZ).date()
