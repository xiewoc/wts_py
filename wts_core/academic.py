"""Read-only student academic queries: terms, grades and exam arrangements.

Mirrors ``src-tauri/src/academic.rs``. Credentials and private responses never
go through a third-party proxy and grades are never persisted by this service.
Only the four fixed endpoints below are reachable; no caller-provided URL is
ever used.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import date, timedelta
from typing import Any

import httpx

from .classrooms import (
    code_is_success,
    read_sjd_json_response,
    session_epoch,
    sjd_client,
    sjd_headers,
    sjd_request,
    with_sjd_session_at,
)
from .config import SJD_REST_CLASSROOM_PAGE_URL, SLOT_TIMES, now_in_app_tz
from .errors import ServiceError
from .models import (
    AcademicTerm,
    Course,
    ExamArrangement,
    ExamSchedule,
    GradeItem,
    GradeReport,
    GradeRequest,
    GradeTerms,
    ScheduleResponse,
)
from .session_cache import SessionEpoch

BASE = "https://jwglweixin.bupt.edu.cn/bjyddx"
MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 5_000
MAX_FIELD = 2_048

ALLOWED_PATHS = frozenset(
    {
        "/currentTerm",
        "/semesterList",
        "/student/termGPA",
        "/student/examinationArrangement",
    }
)

_DATE_PATTERN = re.compile(r"([12][0-9]{3})[-/年.]([0-9]{1,2})[-/月.]([0-9]{1,2})(?:日)?")
_CLOCK_PATTERN = re.compile(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")
_SEPARATOR_CHARACTERS = frozenset("-–—~～至到")


def account_key(account: str) -> str:
    digest = hashlib.sha256(account.strip().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _stable_id(parts: list[str]) -> str:
    """Length-prefix every field so separators inside source text cannot collide."""
    hasher = hashlib.sha256()
    for part in parts:
        payload = part.encode("utf-8")
        hasher.update(len(payload).to_bytes(8, "big"))
        hasher.update(payload)
    return hasher.hexdigest()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        result = value.strip()
    elif isinstance(value, bool):
        raise ServiceError("教务数据字段格式不正确。")
    elif isinstance(value, (int, float)):
        result = str(value)
    else:
        raise ServiceError("教务数据字段格式不正确。")
    if len(result.encode("utf-8")) > MAX_FIELD:
        raise ServiceError("教务数据字段过长。")
    return result


def _dict_get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _is_ascii_digit(character: str) -> bool:
    return len(character) == 1 and "0" <= character <= "9"
def _rows(payload: Any) -> list[Any]:
    if not code_is_success(payload):
        raise ServiceError("教务查询失败，请检查登录状态后重试。")
    values = _dict_get(payload, "data")
    if not isinstance(values, list):
        raise ServiceError("教务查询返回了无法识别的数据结构。")
    if len(values) > MAX_ITEMS:
        raise ServiceError("教务查询条目过多。")
    return values


def parse_current_term(payload: Any) -> str:
    rows = _rows(payload)
    term = _text(_dict_get(rows[0], "semesterId")) if rows else ""
    validate_term(term, allow_empty=False)
    return term


def validate_term(term: str, *, allow_empty: bool) -> None:
    if (allow_empty and not term) or (
        term
        and len(term) <= 64
        and all(
            (character.isascii() and character.isalnum()) or character in "-_"
            for character in term
        )
    ):
        return
    raise ServiceError("请选择学校返回的有效学期。")


def parse_terms(payload: Any) -> list[AcademicTerm]:
    seen: set[str] = set()
    result: list[AcademicTerm] = []
    for row in _rows(payload):
        term_id = _text(_dict_get(row, "semesterId"))
        validate_term(term_id, allow_empty=False)
        if term_id in seen:
            continue
        seen.add(term_id)
        name = _text(_dict_get(row, "semesterName"))
        result.append(AcademicTerm(name=name or term_id, id=term_id))
    return result


def parse_grades(payload: Any, term: str, record_type: str) -> GradeReport:
    validate_term(term, allow_empty=True)
    if record_type not in ("", "0", "1"):
        raise ServiceError("成绩记录类型不正确。")
    data = _rows(payload)
    if len(data) > 1:
        raise ServiceError("成绩汇总格式不正确。")

    result = GradeReport(
        term_id=term, record_type=record_type, fetched_at=now_in_app_tz()
    )
    if not data:
        return result
    summary = data[0]
    result.average_grade_point = _text(_dict_get(summary, "pjxfjd"))
    achievements = _dict_get(summary, "achievement")
    if not isinstance(achievements, list):
        raise ServiceError("成绩列表格式不正确。")
    if len(achievements) > MAX_ITEMS:
        raise ServiceError("成绩条目过多。")

    seen: set[str] = set()
    for row in achievements:
        item = GradeItem(
            name=_text(_dict_get(row, "courseName")),
            score=_text(_dict_get(row, "fraction")),
            credits=_text(_dict_get(row, "credit")),
            course_code=_text(_dict_get(row, "kcbh")),
            course_attribute=_text(_dict_get(row, "curriculumAttributes")),
            course_nature=_text(_dict_get(row, "courseNature")),
            exam_nature=_text(_dict_get(row, "examinationNature")),
            semester_name=_text(_dict_get(row, "curSemesterName")),
            grade_status=_text(_dict_get(row, "cjbs")),
        )
        if not item.name:
            raise ServiceError("成绩课程名称缺失。")
        record_id = _text(_dict_get(row, "cj0708id"))
        digest = (
            _stable_id([record_id])
            if record_id
            else _stable_id(
                [
                    item.name,
                    item.course_code,
                    item.score,
                    item.credits,
                    item.course_attribute,
                    item.course_nature,
                    item.exam_nature,
                    item.semester_name,
                    item.grade_status,
                ]
            )
        )
        item.id = f"grade:{digest}"
        if item.id in seen:
            continue
        seen.add(item.id)
        result.items.append(item)
    return result

def minutes(value: str) -> int | None:
    """Minutes after midnight, or ``None`` for anything that is not a clock."""
    if ":" not in value:
        return None
    hour, _, minute = value.partition(":")
    if not hour or len(hour) > 2 or len(minute) != 2:
        return None
    if not all(_is_ascii_digit(character) for character in hour + minute):
        return None
    hours = int(hour)
    minutes_part = int(minute)
    if (hours < 24 and minutes_part < 60) or (hours == 24 and minutes_part == 0):
        return hours * 60 + minutes_part
    return None


def _exact_date(value: str) -> date | None:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    if not all(character == "-" or character.isdigit() for character in value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def iso_date(value: str) -> date | None:
    parsed = _exact_date(value)
    if parsed is None or not 1900 <= parsed.year <= 2200:
        return None
    return parsed


def parse_exam_time(value: str) -> tuple[str, str, str]:
    """Extract ``(date, start, end)`` only when one date and one clock range exist."""
    normalized = value.replace("：", ":")
    dates: set[str] = set()
    for match in _DATE_PATTERN.finditer(normalized):
        start, end = match.start(), match.end()
        if start > 0 and _is_ascii_digit(normalized[start - 1]):
            return "", "", ""
        if end < len(normalized) and _is_ascii_digit(normalized[end]):
            return "", "", ""
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return "", "", ""
        text = parsed.isoformat()
        if iso_date(text) is None:
            return "", "", ""
        dates.add(text)

    if len(dates) != 1:
        return "", "", ""
    date_text = next(iter(dates))

    # Look-ahead is unavailable in the Rust regex crate; the clock candidates
    # are found independently and the separator is inspected between them.
    candidates = list(_CLOCK_PATTERN.finditer(normalized))
    if len(candidates) != 2:
        return date_text, "", ""

    values: list[str] = []
    for candidate in candidates:
        seconds = candidate.group(3)
        if seconds is not None and seconds != "00":
            return date_text, "", ""
        start, end = candidate.start(), candidate.end()
        if start > 0 and _is_ascii_digit(normalized[start - 1]):
            return date_text, "", ""
        if end < len(normalized) and _is_ascii_digit(normalized[end]):
            return date_text, "", ""
        try:
            hours = int(candidate.group(1))
        except ValueError:  # pragma: no cover - the regex guarantees digits
            hours = 99
        values.append(f"{hours:02d}:{candidate.group(2)}")

    separator = normalized[candidates[0].end() : candidates[1].start()]
    separator = _DATE_PATTERN.sub("", separator)
    if not separator.strip() or not all(
        character.isspace() or character in _SEPARATOR_CHARACTERS
        for character in separator
    ):
        return date_text, "", ""

    start_minutes = minutes(values[0])
    end_minutes = minutes(values[1])
    if start_minutes is not None and end_minutes is not None:
        if start_minutes < end_minutes and start_minutes < 1440:
            return date_text, values[0], values[1]
    return date_text, "", ""


def parse_exams(payload: Any, term: str, key: str) -> ExamSchedule:
    items: list[ExamArrangement] = []
    seen: set[str] = set()
    for row in _rows(payload):
        name = _text(_dict_get(row, "courseName"))
        if not name:
            raise ServiceError("考试课程名称缺失。")
        room = _text(_dict_get(row, "examinationPlace"))
        # The verified school response has no seat field. Keep the normalized
        # field empty until an actual upstream seat contract is established.
        seat = ""
        time_text = _text(_dict_get(row, "time"))
        auxiliary: tuple[str, str, str] | None = None
        if not time_text:
            fields = [
                _text(_dict_get(row, "ksqssj")),
                _text(_dict_get(row, "zssj1")),
                _text(_dict_get(row, "zssj2")),
            ]
            if (
                iso_date(fields[0]) is not None
                and len(fields[1]) == 5
                and len(fields[2]) == 5
            ):
                start_minutes = minutes(fields[1])
                end_minutes = minutes(fields[2])
                if (
                    start_minutes is not None
                    and end_minutes is not None
                    and start_minutes < end_minutes
                    and start_minutes < 1440
                ):
                    auxiliary = (fields[0], fields[1], fields[2])
            # Preserve the auxiliary text while using the separate fields as the
            # clock range, without inventing a display delimiter.
            time_text = " ".join(fields).strip()

        if auxiliary is not None:
            exam_date, start_time, end_time = auxiliary
        else:
            exam_date, start_time, end_time = parse_exam_time(time_text)

        exam_id = "exam:" + _stable_id(
            [name, exam_date, start_time, end_time, room, seat, time_text]
        )
        if exam_id in seen:
            continue
        seen.add(exam_id)
        items.append(
            ExamArrangement(
                id=exam_id,
                name=name,
                date=exam_date,
                start_time=start_time,
                end_time=end_time,
                room=room,
                seat=seat,
                time_text=time_text,
            )
        )

    unknown = sum(1 for item in items if not item.date or not item.start_time)
    return ExamSchedule(
        term_id=term,
        account_key=key,
        fetched_at=now_in_app_tz(),
        status="fresh",
        message=(f"{unknown} 项考试日期或时间待定，请查看原始安排。" if unknown else ""),
        items=items,
    )

async def _request(path: str, token: str, query: dict[str, str]) -> Any:
    """Call one fixed academic endpoint; the path is never caller-provided."""
    if path not in ALLOWED_PATHS:
        raise ServiceError("不支持的教务查询。")
    async with sjd_client(25) as client:
        try:
            response = await sjd_request(
                client,
                "POST",
                f"{BASE}{path}",
                headers=sjd_headers(token, SJD_REST_CLASSROOM_PAGE_URL),
                params=query,
            )
        except httpx.HTTPError as error:
            raise ServiceError("无法连接学校教务查询服务。") from error
        return await read_sjd_json_response(response, MAX_BYTES, "教务查询")


async def fetch_terms(account: str, password: str) -> GradeTerms:
    return await fetch_terms_at(session_epoch(), account, password)


async def fetch_terms_at(
    epoch: SessionEpoch, account: str, password: str
) -> GradeTerms:
    async def request(token: str) -> GradeTerms:
        return await fetch_terms_with_token(token)

    return await with_sjd_session_at(epoch, account, password, request)


async def fetch_terms_with_token(token: str) -> GradeTerms:
    current_term_id = parse_current_term(await _request("/currentTerm", token, {}))
    terms = parse_terms(await _request("/semesterList", token, {}))
    if not any(term.id == current_term_id for term in terms):
        terms.insert(0, AcademicTerm(id=current_term_id, name=current_term_id))
    return GradeTerms(current_term_id=current_term_id, terms=terms)


async def fetch_grades(
    account: str, password: str, query: GradeRequest
) -> GradeReport:
    return await fetch_grades_at(session_epoch(), account, password, query)


async def fetch_grades_at(
    epoch: SessionEpoch, account: str, password: str, query: GradeRequest
) -> GradeReport:
    record_type = query.record_type if query.record_type is not None else "1"
    if record_type not in ("", "0", "1"):
        raise ServiceError("成绩记录类型不正确。")

    async def request(token: str) -> GradeReport:
        return await fetch_grades_with_token(token, query, record_type)

    return await with_sjd_session_at(epoch, account, password, request)


async def fetch_grades_with_token(
    token: str, query: GradeRequest, record_type: str
) -> GradeReport:
    if query.term_id is not None:
        term = query.term_id.strip()
    else:
        term = parse_current_term(await _request("/currentTerm", token, {}))
    validate_term(term, allow_empty=True)
    payload = await _request(
        "/student/termGPA", token, {"semester": term, "type": record_type}
    )
    return parse_grades(payload, term, record_type)


async def fetch_exams(
    account: str, password: str, term: str | None = None
) -> ExamSchedule:
    return await fetch_exams_at(session_epoch(), account, password, term)


async def fetch_exams_at(
    epoch: SessionEpoch, account: str, password: str, term: str | None
) -> ExamSchedule:
    async def request(token: str) -> ExamSchedule:
        resolved = (
            term
            if term is not None
            else parse_current_term(await _request("/currentTerm", token, {}))
        )
        validate_term(resolved, allow_empty=False)
        return await fetch_exams_using_token(token, resolved, account_key(account))

    return await with_sjd_session_at(epoch, account, password, request)


async def fetch_exams_using_token(token: str, term: str, key: str) -> ExamSchedule:
    payload = await _request(
        "/student/examinationArrangement", token, {"semester": term}
    )
    return parse_exams(payload, term, key)


async def fetch_exams_with_token(token: str, term: str, key: str) -> ExamSchedule:
    """Best-effort variant that reports a failed exam sync instead of raising."""
    try:
        payload = await _request(
            "/student/examinationArrangement", token, {"semester": term}
        )
    except ServiceError:
        return ExamSchedule(
            term_id=term,
            account_key=key,
            fetched_at=now_in_app_tz(),
            status="failed",
            message="考试安排同步失败，普通课程仍可查看。",
            items=[],
        )
    try:
        return parse_exams(payload, term, key)
    except ServiceError:
        return ExamSchedule(
            term_id=term,
            account_key=key,
            fetched_at=now_in_app_tz(),
            status="failed",
            message="考试安排同步失败，普通课程仍可查看。",
            items=[],
        )


def is_exam(course: Course) -> bool:
    return course.event_kind == "exam"


def course_minutes(course: Course) -> tuple[int, int] | None:
    """Busy interval of one course; only examinations use exact clock times."""
    if is_exam(course):
        if course.start_time is None or course.end_time is None:
            return None
        start = minutes(course.start_time)
        end = minutes(course.end_time)
        if start is None or end is None:
            return None
        return (start, end) if start < end and start < 1440 else None
    if not 0 <= course.start_slot < len(SLOT_TIMES):
        return None
    if not 0 <= course.end_slot < len(SLOT_TIMES):
        return None
    start = minutes(SLOT_TIMES[course.start_slot][0])
    end = minutes(SLOT_TIMES[course.end_slot][1])
    if start is None or end is None:
        return None
    return (start, end) if start < end and start < 1440 else None


def occurs_on(course: Course, day: date, term_start: date) -> bool:
    if is_exam(course):
        return iso_date(course.event_date or "") == day
    if day < term_start or course.weekday != day.isoweekday():
        return False
    return ((day - term_start).days // 7 + 1) in course.week_numbers


def busy_slots(course: Course) -> list[int]:
    span = course_minutes(course)
    if span is None:
        return []
    start, end = span
    slots: list[int] = []
    for index, (slot_start, slot_end) in enumerate(SLOT_TIMES):
        first = minutes(slot_start)
        last = minutes(slot_end)
        if first is None or last is None:
            continue
        if start < last and first < end:
            slots.append(index)
    return slots


def effective_schedule(schedule: ScheduleResponse) -> ScheduleResponse:
    """Project dated exams into a timetable without mutating the snapshot."""
    output = copy.deepcopy(schedule)
    output.courses = [
        course for course in output.courses if not is_exam(course)
    ]
    exams = output.exam_schedule
    if exams is None:
        return output
    if (
        exams.status not in ("fresh", "stale")
        or not exams.account_key.strip()
        or exams.term_id != output.term_id
    ):
        return output

    term_start = iso_date(output.term_start_date or "")
    projected: list[Course] = []
    for exam in exams.items:
        exam_date = iso_date(exam.date or "")
        if exam_date is None:
            continue
        course = Course(
            id=exam.id,
            name=exam.name,
            room=exam.room,
            weekday=exam_date.isoweekday(),
            event_kind="exam",
            event_date=exam.date,
            start_time=exam.start_time or None,
            end_time=exam.end_time or None,
            time_range=(
                "时间待定"
                if not exam.start_time
                else f"{exam.start_time}-{exam.end_time}"
            ),
            section_text="考试" if not exam.seat else f"考试 · 座位 {exam.seat}",
        )
        slots = busy_slots(course)
        course.start_slot = slots[0] if slots else 0
        course.end_slot = slots[-1] if slots else 0
        # Dated exams deliberately have no teaching weeks: every consumer must
        # use event_date, including dates beyond the normal teaching calendar.
        projected.append(course)

    exam_spans: list[tuple[date, tuple[int, int] | None]] = []
    for exam in projected:
        exam_day = iso_date(exam.event_date or "")
        if exam_day is not None:
            exam_spans.append((exam_day, course_minutes(exam)))

    if term_start is not None:
        for course in output.courses:
            span = course_minutes(course)
            weekday = course.weekday
            kept: list[int] = []
            for week in course.week_numbers:
                day = term_start + timedelta(days=(week - 1) * 7 + (weekday - 1))
                hidden = False
                for exam_day, exam_span in exam_spans:
                    if exam_day != day or span is None or exam_span is None:
                        continue
                    if span[0] < exam_span[1] and exam_span[0] < span[1]:
                        hidden = True
                        break
                if not hidden:
                    kept.append(week)
            course.week_numbers = kept
        output.courses = [course for course in output.courses if course.week_numbers]

    output.courses.extend(projected)
    return output


def merge_exam_fallback(
    current: ScheduleResponse, previous: ScheduleResponse | None
) -> None:
    """Reuse a verified exam snapshot for the same account and term after a failure."""
    failed = current.exam_schedule
    if failed is None or failed.status != "failed":
        return
    if failed.term_id != current.term_id:
        return
    if previous is None or previous.term_id != current.term_id:
        return
    old = previous.exam_schedule
    if old is None or old.status not in ("fresh", "stale"):
        return
    if not old.account_key or old.account_key != failed.account_key:
        return
    if old.term_id != failed.term_id:
        return
    failed.items = list(old.items)
    failed.fetched_at = old.fetched_at
    failed.status = "stale"
    failed.message = "考试安排同步失败，正在显示此前缓存，请以学校最新安排为准。"

