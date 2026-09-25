"""Personal timetable parsing and retrieval from the mobile academic system.

Mirrors ``src-tauri/src/schedule.rs``. Week text, period codes and room names are
normalized exactly like the Rust core so every client derives the same courses,
week numbers and time ranges from the same upstream payload.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any

import httpx

from . import academic
from .auth import resolve_credentials
from .classrooms import (
    MAX_SJD_DATA_RESPONSE_BYTES,
    code_is_success,
    first_text,
    read_sjd_json_response,
    session_epoch,
    sjd_client,
    sjd_headers,
    sjd_request,
    with_sjd_session_at,
)
from .config import (
    SJD_REST_CLASSROOM_PAGE_URL,
    SJD_STUDENT_CURRICULUM_URL,
    SLOT_TIMES,
    is_valid_term_id,
    now_in_app_tz,
    suggested_term_for_date,
    today_in_app_tz,
)
from .errors import ServiceError
from .models import Course, ExamSchedule, ScheduleRequest, ScheduleResponse
from .session_cache import SessionEpoch

MAX_SJD_COURSE_NESTING_DEPTH = 64

_BRACKET_PATTERN = re.compile(r"\[.*?\]")
_PAREN_PATTERN = re.compile(r"\(.*?\)")
_TWO_DIGIT_PATTERN = re.compile(r"[0-9]{2}")
_NUMBER_PATTERN = re.compile(r"[0-9]+")
_DASHES = str.maketrans({c: "-" for c in "－—–"})


def _rust_remainder(value: int, divisor: int) -> int:
    """``%`` with Rust semantics (the sign follows the dividend)."""
    return value % divisor if value >= 0 else -((-value) % divisor)


def expand_week_numbers(week_text: str) -> list[int]:
    """Expand ``1-5(单),7`` style week text into ascending week numbers.

    A global odd/even marker applies to items without their own suffix; when both
    markers appear (``1-17单,2-18双``) each item keeps its own parity.
    """
    raw = week_text.replace("，", ",").replace(" ", "")
    global_odd = "单" in raw
    global_even = "双" in raw
    raw = raw.replace("周", "")
    raw = _BRACKET_PATTERN.sub("", raw)
    raw = _PAREN_PATTERN.sub("", raw)

    weeks: list[int] = []
    for item in raw.split(","):
        if not item:
            continue
        item_odd = "单" in item
        item_even = "双" in item
        clean = item.replace("单", "").replace("双", "")
        expanded: list[int] = []
        if "-" in clean:
            left, _, right = clean.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                pass
            else:
                expanded.extend(range(start, end + 1))
        else:
            try:
                expanded.append(int(clean))
            except ValueError:
                pass
        if item_odd or (not item_even and global_odd):
            expanded = [week for week in expanded if _rust_remainder(week, 2) == 1]
        elif item_even or (not item_odd and global_even):
            expanded = [week for week in expanded if _rust_remainder(week, 2) == 0]
        weeks.extend(expanded)

    return sorted(set(weeks))


def _json_text(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return ""


def _dict_get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _digits_only(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit()


def normalize_course_room(value: str) -> str:
    """Normalize a classroom string: ``3-335`` becomes ``335``, ``202-203`` stays."""
    normalized = value.strip().translate(_DASHES)
    if "-" not in normalized:
        return normalized
    prefix, _, room = normalized.partition("-")
    building_prefix = prefix[1:] if prefix.startswith("教") else prefix
    if (
        len(building_prefix) == 1
        and _digits_only(building_prefix)
        and len(room) == 3
        and _digits_only(room)
    ):
        return room
    return normalized


def parse_sjd_week_numbers(course: Any) -> list[int]:
    weeks = expand_week_numbers(_json_text(_dict_get(course, "classWeek")))
    if not weeks:
        weeks = expand_week_numbers(_json_text(_dict_get(course, "classWeekDetails")))
    if not weeks:
        weeks = [
            int(match)
            for match in _NUMBER_PATTERN.findall(
                _json_text(_dict_get(course, "classWeekDetails"))
            )
        ]
    return sorted(set(weeks))


def parse_sjd_slots(course: Any) -> tuple[int, int] | None:
    """Decode ``classTime`` (``1030405`` = one course using periods 03, 04, 05)."""
    class_time = _json_text(_dict_get(course, "classTime"))
    nodes = [int(match) for match in _TWO_DIGIT_PATTERN.findall(class_time[1:])]
    if not nodes:
        nodes = [
            int(match)
            for match in _NUMBER_PATTERN.findall(
                _json_text(_dict_get(course, "weekNoteDetail"))
            )
        ]
    if not nodes:
        return None
    start_slot = min(nodes) - 1
    end_slot = max(nodes) - 1
    if start_slot < 0 or end_slot < 0:
        return None
    if start_slot >= len(SLOT_TIMES) or end_slot >= len(SLOT_TIMES):
        return None
    if start_slot > end_slot:
        return None
    return start_slot, end_slot


def _first_key(mapping: Any, keys: tuple[str, ...]) -> Any:
    """``Option::or_else`` semantics: a present-but-null key does not fall through."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _contract_date(text: str) -> date | None:
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return None
    if not all(character == "-" or character.isdigit() for character in text):
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def parse_sjd_courses(
    payload: Any, term_id: str, term_start_date: date
) -> ScheduleResponse:
    data = _dict_get(payload, "data")
    if not isinstance(data, list) or not data:
        raise ServiceError("移动教务课表返回为空。")
    root = data[0]
    raw_items = _first_key(root, ("item", "courses"))

    raw_courses: list[Any] = []
    _collect_sjd_course_items(raw_items, raw_courses)

    courses: list[Course] = []
    seen_ids: set[str] = set()
    for raw in raw_courses:
        slots = parse_sjd_slots(raw)
        if slots is None:
            continue
        start_slot, end_slot = slots

        weekday_text = _json_text(_first_key(raw, ("weekDay", "classTime")))
        weekday: int | None = None
        if weekday_text and weekday_text[0].isascii() and weekday_text[0].isdigit():
            weekday = int(weekday_text[0])
        if weekday is None or not 1 <= weekday <= 7:
            continue

        name = _json_text(_dict_get(raw, "courseName")).strip() or "未命名课程"
        teacher = _json_text(_dict_get(raw, "teacherName")).strip()
        building = _json_text(_dict_get(raw, "buildingName")).strip()
        room = normalize_course_room(
            _json_text(_first_key(raw, ("classroomName", "location")))
        )
        if building and room and building not in room:
            location = f"{building}-{room}"
        elif room:
            location = room
        else:
            location = building

        week_text = _json_text(_first_key(raw, ("classWeek", "classWeekDetails"))).strip()
        week_numbers = parse_sjd_week_numbers(raw)
        stable = "|".join(
            (
                _json_text(_dict_get(raw, "jx0408id")),
                name,
                teacher,
                location,
                week_text,
                str(weekday),
                str(start_slot),
                str(end_slot),
            )
        )
        course_id = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12]
        if course_id in seen_ids:
            continue
        seen_ids.add(course_id)

        start_time = _json_text(_dict_get(raw, "startTime"))
        end_time = _json_text(_first_key(raw, ("endTIme", "endTime")))
        courses.append(
            Course(
                id=course_id,
                source_course_id=_json_text(_dict_get(raw, "jx0408id")).strip(),
                name=name,
                teacher=teacher,
                room=location,
                week_text=week_text,
                week_numbers=week_numbers,
                exam_week_numbers=[],
                weekday=weekday,
                start_slot=start_slot,
                end_slot=end_slot,
                section_text=(
                    f"{start_slot + 1}节"
                    if start_slot == end_slot
                    else f"{start_slot + 1}-{end_slot + 1}节"
                ),
                time_range=(
                    f"{start_time or SLOT_TIMES[start_slot][0]}-"
                    f"{end_time or SLOT_TIMES[end_slot][1]}"
                ),
            )
        )

    courses.sort(key=lambda course: (course.weekday, course.start_slot, course.name))
    return ScheduleResponse(
        term_id=term_id,
        term_start_date=term_start_date.isoformat(),
        fetched_at=now_in_app_tz(),
        courses=courses,
        exam_schedule=None,
    )


def _collect_sjd_course_items(value: Any, output: list[Any], depth: int = 0) -> None:
    if depth > MAX_SJD_COURSE_NESTING_DEPTH:
        return
    if isinstance(value, dict):
        if "courseName" in value or "jx0408id" in value:
            output.append(value)
            return
        for child in value.values():
            _collect_sjd_course_items(child, output, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _collect_sjd_course_items(child, output, depth + 1)

def infer_term_start_date(payload: Any) -> date | None:
    """Read the first week's Monday out of a curriculum payload."""
    data = _dict_get(payload, "data")
    if not isinstance(data, list) or not data:
        return None
    root = data[0]
    dates = _dict_get(root, "date")
    if not isinstance(dates, list):
        return None
    dated = None
    for item in dates:
        if not isinstance(item, dict):
            continue
        if item.get("mxrq") is not None and _json_text(item.get("zc")) != "all":
            dated = item
            break
    if dated is None:
        return None

    top_info = _dict_get(root, "topInfo")
    top_week = _dict_get(top_info[0], "week") if isinstance(top_info, list) and top_info else None
    week: int | None = None
    for candidate in (dated.get("zc"), _dict_get(root, "week"), top_week):
        try:
            week = int(_json_text(candidate))
        except ValueError:
            continue
        break
    if week is None or week < 0:
        return None

    day = _contract_date(_json_text(dated.get("mxrq")))
    if day is None:
        return None
    weekday: int | None = None
    try:
        parsed_weekday = int(_json_text(dated.get("xqid")))
    except ValueError:
        parsed_weekday = None
    if parsed_weekday == 0:
        weekday = 7
    elif parsed_weekday is not None and 1 <= parsed_weekday <= 7:
        weekday = parsed_weekday
    if weekday is None:
        weekday = day.isoweekday()
    monday = day - timedelta(days=weekday - 1)
    return monday - timedelta(weeks=week - 1)


def infer_term_id(payload: Any) -> str | None:
    data = _dict_get(payload, "data")
    if not isinstance(data, list) or not data:
        return None
    root = data[0]
    top_info = _dict_get(root, "topInfo")
    top = top_info[0] if isinstance(top_info, list) and top_info else None
    for candidate in (
        _dict_get(root, "semesterId"),
        _dict_get(root, "xnxq01id"),
        _dict_get(top, "semesterId"),
        _dict_get(top, "xnxq01id"),
    ):
        value = _json_text(candidate).strip()
        if value:
            return value
    return None


def resolve_schedule_term(
    payload: ScheduleRequest, current_term: tuple[str, str]
) -> tuple[str, date]:
    automatic = payload.automatic_term_detection_enabled
    if automatic is None:
        automatic = True
    user_term_id = (payload.term_id or "").strip()
    current_term_id, current_term_start = current_term

    if automatic:
        term_id = current_term_id
    elif not user_term_id:
        raise ServiceError.with_status("请填写学期编号。", 400)
    elif not is_valid_term_id(user_term_id):
        raise ServiceError.with_status(
            "学期编号格式不正确，请使用 YYYY-YYYY-1 或 YYYY-YYYY-2。", 400
        )
    else:
        term_id = user_term_id

    user_term_start = (payload.term_start_date or "").strip() or None
    if automatic:
        if (
            user_term_start is not None
            and user_term_id == term_id
            and _contract_date(user_term_start) is not None
        ):
            term_start_source = user_term_start
        else:
            term_start_source = current_term_start
    else:
        if user_term_start is None:
            raise ServiceError.with_status("请填写第一周周一日期。", 400)
        term_start_source = user_term_start

    term_start_date = _contract_date(term_start_source)
    if term_start_date is None:
        raise ServiceError.with_status("第一周周一日期格式不正确。", 400)
    return term_id, term_start_date


async def fetch_schedule(payload: ScheduleRequest) -> ScheduleResponse:
    return await fetch_schedule_at(session_epoch(), payload)


async def fetch_schedule_at(
    epoch: SessionEpoch, payload: ScheduleRequest
) -> ScheduleResponse:
    current_term = suggested_term_for_date(today_in_app_tz())
    term_id, term_start_date = resolve_schedule_term(payload, current_term)
    return await fetch_sjd_schedule(
        epoch, payload.account, payload.password, term_id, term_start_date
    )


async def fetch_sjd_schedule(
    epoch: SessionEpoch,
    account: str | None,
    password: str | None,
    term_id: str,
    fallback_term_start_date: date,
) -> ScheduleResponse:
    user, secret = resolve_credentials(account, password)
    key = academic.account_key(user)
    partial: list[ScheduleResponse] = []

    # Curriculum and exam sync share one epoch and one authentication retry
    # budget. A cleared request cannot capture a new epoch for its second stage.
    async def request(token: str) -> ScheduleResponse:
        schedule = await fetch_sjd_schedule_with_token(
            token, term_id, fallback_term_start_date
        )
        try:
            exams = await academic.fetch_exams_using_token(token, schedule.term_id, key)
        except ServiceError as error:
            schedule.exam_schedule = ExamSchedule(
                term_id=schedule.term_id,
                account_key=key,
                fetched_at=now_in_app_tz(),
                status="failed",
                message="考试安排同步失败，普通课程仍可查看。",
                items=[],
            )
            if error.authentication_expired:
                # Retain the successfully fetched lessons if exams are still
                # unauthorized after the single session retry.
                partial.append(schedule)
                raise
        else:
            schedule.exam_schedule = exams
        return schedule

    try:
        return await with_sjd_session_at(epoch, user, secret, request)
    except ServiceError as error:
        # Revocation / epoch changes must propagate even if lessons completed.
        if error.authentication_expired and partial:
            return partial[0]
        raise


async def fetch_sjd_schedule_with_token(
    token: str, term_id: str, fallback_term_start_date: date
) -> ScheduleResponse:
    async with sjd_client(30) as client:
        try:
            current_response = await sjd_request(
                client,
                "POST",
                SJD_STUDENT_CURRICULUM_URL,
                params={"week": ""},
                headers=sjd_headers(token, SJD_REST_CLASSROOM_PAGE_URL),
            )
            all_response = await sjd_request(
                client,
                "POST",
                SJD_STUDENT_CURRICULUM_URL,
                params={"week": "all"},
                headers=sjd_headers(token, SJD_REST_CLASSROOM_PAGE_URL),
            )
        except httpx.HTTPError as error:
            raise ServiceError(f"无法连接移动教务课表服务：{error}") from error

        current_payload = await read_sjd_json_response(
            current_response, MAX_SJD_DATA_RESPONSE_BYTES, "移动教务课表"
        )
        all_payload = await read_sjd_json_response(
            all_response, MAX_SJD_DATA_RESPONSE_BYTES, "移动教务课表"
        )

    for payload in (current_payload, all_payload):
        if not code_is_success(payload):
            message = first_text(payload, ("Msg", "msg")) or "移动教务课表获取失败。"
            raise ServiceError(message)

    inferred_start = (
        infer_term_start_date(current_payload) or fallback_term_start_date
    )
    inferred_term_id = infer_term_id(current_payload) or ""
    return parse_sjd_courses(
        all_payload, inferred_term_id or term_id, inferred_start
    )

