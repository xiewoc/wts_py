"""Local-only timetable edits. Keep the fetched snapshot intact so edits can be
restored and re-applied after refresh, without making university-side writes.

Mirrors ``src-tauri/src/course_deletions.rs``: rules are scoped to the local
account through :mod:`wts_core.scoped_cache`, stored atomically, and applied
against each freshly fetched schedule rather than mutating the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import scoped_cache
from .academic import effective_schedule, is_exam
from .errors import ServiceError
from .json_model import JsonModel
from .models import Course, ScheduleResponse

FILE_NAME = "course-deletions.json"
MAX_RECORDS = 1000
MAX_BYTES = 1024 * 1024


@dataclass
class CourseDeletion(JsonModel):
    id: str = ""
    term_id: str = ""
    source_course_id: str = ""
    name: str = ""
    teacher: str = ""
    #: ``None`` removes the course for the semester; a date removes one meeting.
    date: str | None = None
    start_slot: int = 0
    end_slot: int = 0

    omit_when_empty = frozenset({"source_course_id"})

    def matches_course(self, course: Course) -> bool:
        if is_exam(course):
            return False
        if self.source_course_id and course.source_course_id.strip():
            return self.source_course_id == course.source_course_id.strip()
        return self.name == course.name.strip() and self.teacher == course.teacher.strip()

    @classmethod
    def create(
        cls,
        schedule: ScheduleResponse,
        course_id: str,
        occurrence: str | None,
    ) -> "CourseDeletion":
        if not schedule.term_id.strip():
            raise ServiceError("请先获取有效学期的课表。")
        course = next(
            (item for item in schedule.courses if item.id == course_id), None
        )
        if course is None:
            raise ServiceError("课程已变化，请重新选择。")
        if is_exam(course):
            raise ServiceError("考试安排不能通过课程删除操作修改。")
        if not course.name.strip():
            raise ServiceError("无法识别此课程。")
        if occurrence is not None:
            day = _contract_date(occurrence)
            if not _occurs_on(schedule, course, day):
                raise ServiceError("所选日期没有这次课程。")
        record = cls(
            id="",
            term_id=schedule.term_id,
            source_course_id=course.source_course_id.strip(),
            name=course.name.strip(),
            teacher=course.teacher.strip(),
            date=occurrence,
            start_slot=course.start_slot if occurrence is not None else 0,
            end_slot=course.end_slot if occurrence is not None else 0,
        )
        # Match serde_json::to_vec: compact separators, raw UTF-8, field order.
        payload = json.dumps(
            record.to_dict(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        record.id = hashlib.sha1(payload).hexdigest()
        return record


def _contract_date(value: str) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ServiceError("课程日期格式不正确。")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ServiceError("课程日期格式不正确。") from error
    if parsed.isoformat() != value:
        raise ServiceError("课程日期格式不正确。")
    return parsed


def _occurs_on(schedule: ScheduleResponse, course: Course, day: date) -> bool:
    try:
        start = _contract_date(schedule.term_start_date)
    except ServiceError:
        return False
    if start.weekday() != 0:  # term must start on a Monday
        return False
    elapsed = (day - start).days
    if elapsed < 0:
        return False
    return course.weekday == day.isoweekday() and (
        elapsed // 7 + 1
    ) in course.week_numbers


def apply(
    schedule: ScheduleResponse, records: list[CourseDeletion]
) -> ScheduleResponse:
    visible = ScheduleResponse.from_dict(schedule.to_dict())
    assert visible is not None
    try:
        start: date | None = _contract_date(schedule.term_start_date)
    except ServiceError:
        start = None
    kept: list[Course] = []
    for course in visible.courses:
        if is_exam(course):
            continue
        rules = [
            record
            for record in records
            if record.term_id == schedule.term_id and record.matches_course(course)
        ]
        if any(record.date is None for record in rules):
            continue
        if start is not None:
            weekday = course.weekday
            remaining: list[int] = []
            for week in course.week_numbers:
                offset: int | None = None
                if week >= 1 and weekday >= 1:
                    offset = (week - 1) * 7 + (weekday - 1)
                day = start + timedelta(days=offset) if offset is not None else None
                removed = any(
                    record.start_slot == course.start_slot
                    and record.end_slot == course.end_slot
                    and day is not None
                    and record.date == day.isoformat()
                    for record in rules
                )
                if not removed:
                    remaining.append(week)
            course.week_numbers = remaining
        if course.week_numbers:
            kept.append(course)
    visible.courses = kept


def _decode_records(raw: Any) -> list[CourseDeletion]:
    if not isinstance(raw, list):
        raise TypeError("需要 JSON 数组。")
    records: list[CourseDeletion] = []
    for entry in raw:
        decoded = CourseDeletion.from_dict(entry)
        if decoded is None:
            raise TypeError("需要课程删除记录对象。")
        if not isinstance(decoded.id, str) or not isinstance(decoded.term_id, str):
            raise TypeError("课程删除记录字段类型不正确。")
        if not isinstance(decoded.date, (str, type(None))):
            raise TypeError("课程删除记录字段类型不正确。")
        if not isinstance(decoded.start_slot, int) or not isinstance(
            decoded.end_slot, int
        ):
            raise TypeError("课程删除记录字段类型不正确。")
        records.append(decoded)
    return records


def load(path: str, scope: str) -> list[CourseDeletion]:
    if not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
    except OSError as error:
        raise ServiceError("无法读取课程删除记录。") from error
    if size > MAX_BYTES:
        raise ServiceError("课程删除记录过大。")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as error:
        raise ServiceError("无法读取课程删除记录。") from error
    records = scoped_cache.decode(raw, scope, "课程删除记录", _RecordList)
    if records is None:
        return []
    if len(records) > MAX_RECORDS:
        raise ServiceError("课程删除记录过多。")
    return records


class _RecordList:
    """Adapter so :func:`wts_core.scoped_cache.decode` builds typed records."""

    @staticmethod
    def from_dict(raw: Any) -> list[CourseDeletion]:
        return _decode_records(raw)


def save(path: str, scope: str, records: list[CourseDeletion]) -> None:
    if len(records) > MAX_RECORDS:
        raise ServiceError("课程删除记录过多，请先恢复部分课程。")
    payload = scoped_cache.encode(
        scope, [record.to_dict() for record in records], "课程删除记录"
    )
    if len(payload) > MAX_BYTES:
        raise ServiceError("课程删除记录过大，请先恢复部分课程。")
    directory = os.path.dirname(path)
    if not directory:
        raise ServiceError("课程删除记录路径无效。")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as error:
        raise ServiceError("无法创建课程删除记录目录。") from error
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".course-deletions-", suffix=".tmp", dir=directory
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    except OSError as error:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise ServiceError("无法保存课程删除记录。") from error

    return effective_schedule(visible)
