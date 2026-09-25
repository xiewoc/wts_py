"""Contract v1 payload models (snake_case JSON).

Mirrors ``src-tauri/src/models.rs`` plus the academic models that Rust keeps in
``academic.rs``. They live here so the query modules never import each other in
a cycle; only the parsing and transport logic stays in the per-feature module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .json_model import JsonModel

DEFAULT_DAILY_COURSE_NOTIFICATION_MINUTES = 450
CLASSROOMS_CACHE_VERSION = 2


# --- metadata -----------------------------------------------------------------


@dataclass
class SlotMetadata(JsonModel):
    index: int = 0
    label: str = ""
    start: str = ""
    end: str = ""


@dataclass
class CampusMetadata(JsonModel):
    id: str = ""
    name: str = ""


@dataclass
class MetadataResponse(JsonModel):
    campuses: list[CampusMetadata] = field(default_factory=list)
    slots: list[SlotMetadata] = field(default_factory=list)
    default_term_id: str = ""
    default_term_start_date: str = ""
    supports_calendar_import: bool = False


# --- classrooms ---------------------------------------------------------------


@dataclass
class ClassroomsRequest(JsonModel):
    account: str | None = None
    password: str | None = None
    campus_id: str | None = None
    target_date: str | None = None


@dataclass
class ClassroomStatus(JsonModel):
    id: str = ""
    building: str = ""
    room: str = ""
    name: str = ""
    size: int | None = None
    type: str = ""  # noqa: A003 - the contract key is exactly "type"
    available_slots: list[int] = field(default_factory=list)
    source: str = "sjd"


@dataclass
class ClassroomsResponse(JsonModel):
    campus_id: str = ""
    campus_name: str = ""
    target_date: str = ""
    fetched_at: str = ""
    realtime: bool = True
    provider: str = "sjd"
    rooms: list[ClassroomStatus] = field(default_factory=list)


@dataclass
class ClassroomsCacheResponse(JsonModel):
    cache_version: int = 0
    target_date: str = ""
    fetched_at: str = ""
    realtime: bool = True
    provider: str = "sjd"
    campuses: list[ClassroomsResponse] = field(default_factory=list)


# --- schedule -----------------------------------------------------------------


@dataclass
class Course(JsonModel):
    id: str = ""
    source_course_id: str = ""
    name: str = ""
    teacher: str = ""
    room: str = ""
    week_text: str = ""
    week_numbers: list[int] = field(default_factory=list)
    exam_week_numbers: list[int] = field(default_factory=list)
    weekday: int = 0
    start_slot: int = 0
    end_slot: int = 0
    section_text: str = ""
    time_range: str = ""
    event_kind: str | None = None
    event_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None

    omit_when_empty = frozenset({"source_course_id"})
    omit_when_none = frozenset({"event_kind", "event_date", "start_time", "end_time"})


@dataclass
class ScheduleRequest(JsonModel):
    account: str | None = None
    password: str | None = None
    term_id: str | None = None
    term_start_date: str | None = None
    automatic_term_detection_enabled: bool | None = None


@dataclass
class ScheduleResponse(JsonModel):
    term_id: str = ""
    term_start_date: str = ""
    fetched_at: str = ""
    courses: list[Course] = field(default_factory=list)
    exam_schedule: "ExamSchedule | None" = None

    omit_when_none = frozenset({"exam_schedule"})


# --- holidays -----------------------------------------------------------------


@dataclass
class HolidaysRequest(JsonModel):
    year: int = 0


@dataclass
class HolidayItem(JsonModel):
    date: str = ""
    name: str = ""
    kind: str = ""

    renames = {"kind": "type"}


@dataclass
class HolidaysResponse(JsonModel):
    year: int = 0
    source: str = ""
    fetched_at: str = ""
    items: list[HolidayItem] = field(default_factory=list)


# --- important events ---------------------------------------------------------


@dataclass
class ImportantEventItem(JsonModel):
    id: str = ""
    name: str = ""
    event_type: str = ""
    source_type: str = ""
    primary_deadline: str = ""
    deadline_label: str | None = None
    organizer: str | None = None
    official_url: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    level: str | None = None
    location: str | None = None
    status: str | None = None
    description: str | None = None
    eligibility: str | None = None
    notes: str | None = None
    region: str | None = None
    mode: str | None = None
    published_at: str | None = None
    stale: bool = False
    archived: bool = False


@dataclass
class ImportantEventsResponse(JsonModel):
    fetched_at: str = ""
    source: str = ""
    used_backup: bool = False
    items: list[ImportantEventItem] = field(default_factory=list)


@dataclass
class FavoritesFile(JsonModel):
    version: int = 0
    updated_at: str = ""
    items: list[ImportantEventItem] = field(default_factory=list)


# --- shuttle bus --------------------------------------------------------------


@dataclass
class ShuttleBusSource(JsonModel):
    name: str = ""
    page_url: str = ""


@dataclass
class ShuttleBusStats(JsonModel):
    notices: int = 0
    images: int = 0
    parsed_schedules: int = 0
    needs_review: int = 0


@dataclass
class ShuttleBusStop(JsonModel):
    campus: str = ""
    location: str = ""


@dataclass
class ShuttleBusPeriod(JsonModel):
    label: str = ""
    start_date: str | None = None
    end_date: str | None = None


@dataclass
class ShuttleBusService(JsonModel):
    vehicle: str = ""
    count: int = 0


@dataclass
class ShuttleBusRow(JsonModel):
    departure_time: str = ""
    services: dict[str, ShuttleBusService | None] = field(default_factory=dict)


@dataclass
class ShuttleBusSchedule(JsonModel):
    period: ShuttleBusPeriod = field(default_factory=ShuttleBusPeriod)
    from_: str | None = None
    to: str | None = None
    parse_status: str = ""
    parse_confidence: float = 0.0
    parse_engine: str = ""
    rows: list[ShuttleBusRow] = field(default_factory=list)

    renames = {"from_": "from"}


@dataclass
class ShuttleBusNotice(JsonModel):
    id: str = ""
    title: str = ""
    published_at: str = ""
    source_url: str = ""
    parse_status: str = ""
    stops: list[ShuttleBusStop] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    schedules: list[ShuttleBusSchedule] = field(default_factory=list)


@dataclass
class ShuttleBusResponse(JsonModel):
    schema_version: str = ""
    generated_at: str = ""
    status: str = ""
    source: ShuttleBusSource = field(default_factory=ShuttleBusSource)
    stats: ShuttleBusStats = field(default_factory=ShuttleBusStats)
    last_parsed_notice_id: str | None = None
    items: list[ShuttleBusNotice] = field(default_factory=list)


# --- assignments --------------------------------------------------------------


@dataclass
class AssignmentsRequest(JsonModel):
    date: str = ""


@dataclass
class AssignmentDeadlineItem(JsonModel):
    id: str = ""
    title: str = ""
    course_name: str | None = None
    deadline: str = ""
    status: str | None = None


@dataclass
class AssignmentsResponse(JsonModel):
    date: str = ""
    source: str = ""
    items: list[AssignmentDeadlineItem] = field(default_factory=list)
    unavailable_reason: str | None = None


@dataclass
class AssignmentCalendarResponse(JsonModel):
    start_date: str = ""
    end_date: str = ""
    source: str = ""
    items: list[AssignmentDeadlineItem] = field(default_factory=list)


@dataclass
class CalendarRangeRequest(JsonModel):
    start_date: str = ""
    end_date: str = ""


# --- academic (grades and exams) ----------------------------------------------


@dataclass
class AcademicTerm(JsonModel):
    id: str = ""
    name: str = ""


@dataclass
class GradeTerms(JsonModel):
    current_term_id: str = ""
    terms: list[AcademicTerm] = field(default_factory=list)


@dataclass
class GradeItem(JsonModel):
    id: str = ""
    name: str = ""
    score: str = ""
    credits: str = ""
    course_code: str = ""
    course_attribute: str = ""
    course_nature: str = ""
    exam_nature: str = ""
    semester_name: str = ""
    grade_status: str = ""


@dataclass
class GradeReport(JsonModel):
    term_id: str = ""
    record_type: str = ""
    fetched_at: str = ""
    average_grade_point: str = ""
    items: list[GradeItem] = field(default_factory=list)


@dataclass
class GradeRequest(JsonModel):
    #: ``None`` means the school's current term; ``""`` means all terms.
    term_id: str | None = None
    record_type: str | None = None


@dataclass
class ExamArrangement(JsonModel):
    id: str = ""
    name: str = ""
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    room: str = ""
    seat: str = ""
    time_text: str = ""


@dataclass
class ExamSchedule(JsonModel):
    term_id: str = ""
    account_key: str = ""
    fetched_at: str = ""
    status: str = ""
    message: str = ""
    items: list[ExamArrangement] = field(default_factory=list)
