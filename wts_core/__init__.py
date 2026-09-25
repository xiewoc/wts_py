"""Where To Study headless core, ported to async Python.

Mirrors ``where-to-study-core`` (Rust): fixed HTTPS endpoints, manual
redirect validation, size-limited bodies, credential-scoped sessions, and
device-local favorites. Nothing here persists credentials beyond the
credential store's own scope rules.
"""

from __future__ import annotations

from .config import (
    CAMPUSES,
    SLOT_TIMES,
    campuses_payload,
    default_term,
    default_term_id,
    default_term_start_date,
    is_valid_term_id,
    normalize_campus_id,
    now_in_app_tz,
    slot_payload,
    suggested_term_for_date,
    today_in_app_tz,
)
from .errors import ServiceError
from .models import (
    Course,
    FavoritesFile,
    GradeItem,
    GradeReport,
    HolidaysResponse,
    ImportantEventItem,
    ImportantEventsResponse,
    ScheduleResponse,
    ShuttleBusResponse,
)

__all__ = [
    "CAMPUSES",
    "SLOT_TIMES",
    "Course",
    "FavoritesFile",
    "GradeItem",
    "GradeReport",
    "HolidaysResponse",
    "ImportantEventItem",
    "ImportantEventsResponse",
    "ScheduleResponse",
    "ServiceError",
    "ShuttleBusResponse",
    "campuses_payload",
    "default_term",
    "default_term_id",
    "default_term_start_date",
    "is_valid_term_id",
    "normalize_campus_id",
    "now_in_app_tz",
    "slot_payload",
    "suggested_term_for_date",
    "today_in_app_tz",
]
