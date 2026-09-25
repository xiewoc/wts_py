"""Public shuttle-bus and important-event queries plus local favorites.

Mirrors ``where-to-study-core/src/public_queries.rs``: the two fixed HTTPS
endpoints never receive credentials, redirects are refused outright, responses
are size-limited, and favorites are stored only on this device.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

import httpx

from .config import APP_TZ, now_in_app_tz
from .errors import ServiceError
from .http import JSON_ACCEPT, public_client
from .models import (
    FavoritesFile,
    ImportantEventItem,
    ImportantEventsResponse,
    ShuttleBusNotice,
    ShuttleBusResponse,
    ShuttleBusSchedule,
    ShuttleBusService,
)

SHUTTLE_URL = "https://where-to-study.cn/api/shuttle-bus"
CONTEST_PRIMARY_URL = "https://nemoyuzx.github.io/contest-ddl/data/competitions.json"
CONTEST_BACKUP_URL = "https://where-to-study.cn/api/contest-events"
SCHOOL_NOTICES_URL = "https://where-to-study.cn/api/contest-notices"
SHUTTLE_HOST = "where-to-study.cn"
CONTEST_PRIMARY_HOST = "nemoyuzx.github.io"
SCHOOL_SOURCE_HOST = "ucloud.bupt.edu.cn"
SHUTTLE_SOURCE_HOST = "hq.bupt.edu.cn"
MAX_SHUTTLE_BYTES = 512 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENT_ITEMS = 5_000
CACHE_TTL = 5 * 60
USER_AGENT = "WhereToStudyTerminal/0.3.1"
FAVORITES_VERSION = 1
FAVORITES_FILE = "favorite-events.json"
MAX_FAVORITES_BYTES = 2 * 1024 * 1024

IMPORTANT_EVENT_TYPES = (
    "competition",
    "conference",
    "journal_special_issue",
    "hackathon",
    "summer_camp",
    "pre_admission",
)

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class TtlCache:
    """A tiny single-slot cache with a monotonic lifetime (Rust ``Instant``)."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entry: tuple[float, Any] | None = None

    def get(self) -> Any | None:
        if self._entry is None:
            return None
        stored_at, value = self._entry
        if time.monotonic() - stored_at >= self._ttl:
            return None
        return value

    def save(self, value: Any) -> None:
        self._entry = (time.monotonic(), value)


SHUTTLE_CACHE = TtlCache(CACHE_TTL)
EVENT_CACHE = TtlCache(CACHE_TTL)


def fixed_url(value: str, host: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError) as error:
        raise ServiceError(f"公共查询接口地址无效：{error}") from error
    if (
        url.scheme != "https"
        or url.host != host
        or url.username
        or url.password
        or url.fragment
    ):
        raise ServiceError("公共查询接口地址不受信任。")
    return url


def trusted_https_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        url = httpx.URL(candidate)
    except (httpx.InvalidURL, ValueError):
        return None
    if url.scheme != "https" or not url.host:
        return None
    if url.username or url.password:
        return None
    return candidate


def trusted_source_url(value: Any, host: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        return False
    if url.scheme != "https" or url.host != host:
        return False
    return not url.username and not url.password


def valid_text(value: Any, maximum: int) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and len(text) <= maximum


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"{label}解析失败：{error}") from error


async def _read_capped(
    response: httpx.Response, maximum: int, label: str
) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum:
                raise ServiceError(f"{label}响应超过安全上限。")
        except ValueError:
            pass
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > maximum:
            raise ServiceError(f"{label}响应超过安全上限。")
        body.extend(chunk)
    return bytes(body)


async def fetch_limited(url: httpx.URL, maximum: int, label: str) -> bytes:
    async with public_client(18, connect_timeout_seconds=8) as client:
        try:
            response = await client.get(
                url, headers={"Accept": JSON_ACCEPT, "User-Agent": USER_AGENT}
            )
        except httpx.HTTPError as error:
            raise ServiceError(f"无法获取{label}：{error}") from error
        if response.is_redirect:
            raise ServiceError(f"{label}接口返回了不受信任的重定向。")
        if response.status_code >= 400:
            raise ServiceError(f"{label}接口返回错误：HTTP {response.status_code}。")
        return await _read_capped(response, maximum, label)

CLOCK_PATTERN = re.compile(r"([0-9]{2}):([0-9]{2})\Z")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _exact_date(value: Any) -> date | None:
    if not isinstance(value, str) or DATE_PATTERN.match(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 19 or text[4] != "-" or text[10] != "T":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _clock_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = CLOCK_PATTERN.match(value)
    if match is None:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def _required_date(value: Any, label: str) -> date | None:
    if value is None:
        return None
    parsed = _exact_date(value)
    if parsed is None:
        raise ServiceError(label)
    return parsed


def validate_shuttle_schedule(schedule: ShuttleBusSchedule) -> None:
    confidence = schedule.parse_confidence
    confidence_ok = (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and 0.0 <= confidence <= 1.0
    )
    if (
        not valid_text(schedule.period.label, 160)
        or (schedule.from_ is not None and not valid_text(schedule.from_, 80))
        or (schedule.to is not None and not valid_text(schedule.to, 80))
        or schedule.parse_status not in ("parsed", "needs_review", "image_only")
        or not confidence_ok
        or not valid_text(schedule.parse_engine, 80)
        or len(schedule.rows) > 100
    ):
        raise ServiceError("班车时刻表字段不符合 Schema 1.0。")

    start = _required_date(schedule.period.start_date, "班车运行开始日期无效。")
    end = _required_date(schedule.period.end_date, "班车运行结束日期无效。")
    if start is not None and end is not None and start > end:
        raise ServiceError("班车运行日期范围无效。")

    for row in schedule.rows:
        if _clock_minutes(row.departure_time) is None:
            raise ServiceError("班车发车时间或星期字段无效。")
        if any(weekday not in WEEKDAYS for weekday in row.services):
            raise ServiceError("班车发车时间或星期字段无效。")
    for row in schedule.rows:
        for service in row.services.values():
            if service is None:
                continue
            count = service.count
            count_ok = (
                isinstance(count, int)
                and not isinstance(count, bool)
                and 1 <= count <= 20
            )
            if not valid_text(service.vehicle, 40) or not count_ok:
                raise ServiceError("班车车型或车辆数量无效。")

def parse_shuttle(raw: bytes) -> ShuttleBusResponse:
    payload = _decode_json(raw, "班车数据")
    try:
        response = ShuttleBusResponse.from_dict(payload)
    except (TypeError, ValueError) as error:
        raise ServiceError(f"班车数据解析失败：{error}") from error
    if response is None:
        raise ServiceError("班车数据解析失败：响应为空。")

    if (
        response.schema_version != "1.0"
        or response.status not in ("healthy", "stale")
        or _rfc3339(response.generated_at) is None
        or not valid_text(response.source.name, 100)
        or not trusted_source_url(response.source.page_url, SHUTTLE_SOURCE_HOST)
        or len(response.items) > 100
    ):
        raise ServiceError("班车数据不符合 Schema 1.0。")

    for notice in response.items:
        if (
            not valid_text(notice.id, 160)
            or not valid_text(notice.title, 300)
            or _exact_date(notice.published_at) is None
            or not trusted_source_url(notice.source_url, SHUTTLE_SOURCE_HOST)
            or notice.parse_status
            not in ("parsed", "partial", "needs_review", "text_only")
            or len(notice.stops) > 20
            or len(notice.notes) > 40
            or len(notice.schedules) > 32
        ):
            raise ServiceError("班车通知字段不符合 Schema 1.0。")
        if any(
            not valid_text(stop.campus, 80) or not valid_text(stop.location, 160)
            for stop in notice.stops
        ) or any(not valid_text(note, 600) for note in notice.notes):
            raise ServiceError("班车站点或提示字段无效。")
        for schedule in notice.schedules:
            validate_shuttle_schedule(schedule)
    return response


async def fetch_shuttle_uncached() -> ShuttleBusResponse:
    raw = await fetch_limited(
        fixed_url(SHUTTLE_URL, SHUTTLE_HOST), MAX_SHUTTLE_BYTES, "班车数据"
    )
    response = parse_shuttle(raw)
    SHUTTLE_CACHE.save(response)
    return response


async def fetch_shuttle_bus() -> ShuttleBusResponse:
    cached = SHUTTLE_CACHE.get()
    if cached is not None:
        return cached
    return await fetch_shuttle_uncached()


async def refresh_shuttle_bus() -> ShuttleBusResponse:
    return await fetch_shuttle_uncached()


@dataclass
class TodayShuttleDeparture:
    time: str
    vehicle: str
    count: int
    departed: bool
    next: bool


@dataclass
class TodayShuttleRoute:
    from_: str
    to: str
    period_label: str
    departures: list[TodayShuttleDeparture] = field(default_factory=list)


@dataclass
class TodayShuttlePresentation:
    date: str
    status: str
    next_departure: str | None = None
    notice_id: str | None = None
    notice_title: str | None = None
    notice_url: str | None = None
    stops: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    routes: list[TodayShuttleRoute] = field(default_factory=list)
    stale: bool = False


def active_shuttle_notice(
    response: ShuttleBusResponse, target: date
) -> tuple[ShuttleBusNotice, list[ShuttleBusSchedule]] | None:
    """Newest notice that still has an authoritative timetable for ``target``."""
    notices = [
        notice
        for notice in response.items
        if _exact_date(notice.published_at) is not None
        and _exact_date(notice.published_at) <= target
    ]
    notices.sort(key=lambda notice: (notice.published_at, notice.id), reverse=True)

    for notice in notices:
        parsed = [
            schedule
            for schedule in notice.schedules
            if schedule.parse_status == "parsed" and schedule.rows
        ]
        if not parsed:
            continue
        active = []
        for schedule in parsed:
            start = _exact_date(schedule.period.start_date)
            if start is None:
                continue
            end = _exact_date(schedule.period.end_date)
            if start <= target and (end is None or target <= end):
                active.append(schedule)
        if not active:
            # A newer authoritative timetable must not be replaced by an older
            # notice during a gap between periods.
            return None
        starts = [
            schedule.period.start_date
            for schedule in active
            if schedule.period.start_date is not None
        ]
        if not starts:
            return None
        latest_start = max(starts)
        return notice, [
            schedule
            for schedule in active
            if schedule.period.start_date == latest_start
        ]
    return None



def shuttle_today(response: ShuttleBusResponse) -> TodayShuttlePresentation:
    return shuttle_at(response, datetime.now(timezone.utc))


def shuttle_at(
    response: ShuttleBusResponse, now: datetime
) -> TodayShuttlePresentation:
    """Render the shuttle timetable that is in effect right now."""
    local = now.astimezone(APP_TZ)
    target = local.date()
    weekday = WEEKDAYS[local.weekday()]
    now_minutes = local.hour * 60 + local.minute
    selection = active_shuttle_notice(response, target)

    routes: list[TodayShuttleRoute] = []
    if selection is not None:
        _, schedules = selection
        for schedule in schedules:
            if schedule.from_ is None or schedule.to is None:
                continue
            departures: list[TodayShuttleDeparture] = []
            for row in schedule.rows:
                service = row.services.get(weekday)
                if not isinstance(service, ShuttleBusService):
                    continue
                departure_minutes = _clock_minutes(row.departure_time)
                if departure_minutes is None:
                    continue
                departures.append(
                    TodayShuttleDeparture(
                        time=row.departure_time,
                        vehicle=service.vehicle,
                        count=service.count,
                        departed=departure_minutes <= now_minutes,
                        next=False,
                    )
                )
            departures.sort(key=lambda departure: departure.time)
            routes.append(
                TodayShuttleRoute(
                    from_=schedule.from_,
                    to=schedule.to,
                    period_label=schedule.period.label,
                    departures=departures,
                )
            )
    routes.sort(key=lambda route: (route.from_, route.to, route.period_label))

    upcoming: tuple[int, int, str] | None = None
    for route_index, route in enumerate(routes):
        for departure_index, departure in enumerate(route.departures):
            if departure.departed:
                continue
            if upcoming is None or departure.time < upcoming[2]:
                upcoming = (route_index, departure_index, departure.time)
    if upcoming is not None:
        routes[upcoming[0]].departures[upcoming[1]].next = True

    departure_count = sum(len(route.departures) for route in routes)
    vehicle_count = sum(
        departure.count for route in routes for departure in route.departures
    )
    if selection is None:
        status = "未找到当前生效的班车时刻表"
    elif departure_count == 0:
        status = "今日暂无已安排班车"
    else:
        status = f"今日安排 {departure_count} 个发车时刻 · {vehicle_count} 辆车"

    next_departure: str | None = None
    if upcoming is not None:
        route = routes[upcoming[0]]
        next_departure = f"下一班 {upcoming[2]} · {route.from_} → {route.to}"
    elif departure_count > 0:
        next_departure = "今日班车已结束"

    notice_id: str | None = None
    notice_title: str | None = None
    notice_url: str | None = None
    stops: list[Any] = []
    notes: list[str] = []
    if selection is not None:
        notice, _ = selection
        notice_id = notice.id
        notice_title = notice.title
        notice_url = notice.source_url
        stops = list(notice.stops)
        notes = list(notice.notes)

    return TodayShuttlePresentation(
        date=target.isoformat(),
        status=status,
        next_departure=next_departure,
        notice_id=notice_id,
        notice_title=notice_title,
        notice_url=notice_url,
        stops=stops,
        notes=notes,
        routes=routes,
        stale=response.status == "stale",
    )



def normalized_optional(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maximum:
        return None
    return text


def normalized_labels(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return result
    for value in values:
        label = normalized_optional(value, 80)
        if label is None or label in seen:
            continue
        seen.add(label)
        result.append(label)
        if len(result) >= 32:
            break
    return result


def _format_rfc3339(value: datetime) -> str:
    """Match ``DateTime::to_rfc3339`` (fractional seconds only when present)."""
    if value.microsecond:
        return value.isoformat()
    return value.isoformat(timespec="seconds")


def _contest_deadline_label(item: dict[str, Any]) -> str | None:
    deadline = item.get("primary_deadline")
    if not isinstance(deadline, str):
        return None
    for key, label in (
        ("registration_deadline", "报名截止"),
        ("abstract_deadline", "摘要截止"),
        ("submission_deadline", "提交截止"),
        ("competition_start", "活动开始"),
        ("competition_end", "活动结束"),
    ):
        if item.get(key) == deadline:
            return label
    return None


def parse_contest_events(raw: bytes) -> tuple[list[ImportantEventItem], str]:
    envelope = _decode_json(raw, "公开重要事件")
    if not isinstance(envelope, dict):
        raise ServiceError("公开重要事件不符合 Schema 1.4。")
    raw_items = envelope.get("items")
    generated_at = envelope.get("generated_at")
    if (
        envelope.get("schema_version") != "1.4"
        or envelope.get("timezone") != "Asia/Shanghai"
        or _rfc3339(generated_at) is None
        or not isinstance(raw_items, list)
        or len(raw_items) > MAX_EVENT_ITEMS
    ):
        raise ServiceError("公开重要事件不符合 Schema 1.4。")

    items: list[ImportantEventItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("archived") is True:
            continue
        if item.get("event_type") not in IMPORTANT_EVENT_TYPES:
            continue
        raw_id = item.get("id")
        raw_name = item.get("name")
        item_id = raw_id.strip() if isinstance(raw_id, str) else ""
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        deadline_text = item.get("primary_deadline")
        if not isinstance(deadline_text, str):
            continue
        deadline = _rfc3339(deadline_text)
        if deadline is None:
            continue
        if not valid_text(item_id, 128) or not valid_text(name, 240):
            continue

        source = item.get("source")
        source_name = (
            normalized_optional(source.get("name"), 120)
            if isinstance(source, dict)
            else None
        )
        source_url = (
            trusted_https_url(source.get("url")) if isinstance(source, dict) else None
        )
        items.append(
            ImportantEventItem(
                id=item_id,
                name=name,
                event_type=item["event_type"],
                source_type="contest_ddl",
                primary_deadline=_format_rfc3339(deadline),
                deadline_label=_contest_deadline_label(item),
                organizer=normalized_optional(item.get("organizer"), 200),
                official_url=trusted_https_url(item.get("official_url")),
                source_name=source_name,
                source_url=source_url,
                categories=normalized_labels(item.get("categories")),
                tags=normalized_labels(item.get("tags")),
                level=normalized_optional(item.get("level"), 120),
                location=normalized_optional(item.get("location"), 200),
                status=normalized_optional(item.get("status"), 64),
                description=normalized_optional(item.get("description"), 2_000),
                eligibility=normalized_optional(item.get("eligibility"), 500),
                notes=normalized_optional(item.get("notes"), 4_000),
                region=normalized_optional(item.get("region"), 80),
                mode=normalized_optional(item.get("mode"), 80),
                published_at=None,
                stale=item.get("stale") is True,
                archived=False,
            )
        )
    items.sort(key=lambda item: (item.primary_deadline, item.name))
    return items, str(generated_at)



def parse_school_events(raw: bytes) -> tuple[list[ImportantEventItem], str]:
    envelope = _decode_json(raw, "校内重要事件")
    if not isinstance(envelope, dict):
        raise ServiceError("校内重要事件不符合 Schema 1.0。")
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    notices = envelope.get("items")
    generated_at = envelope.get("generated_at")
    if (
        envelope.get("schema_version") != "1.0"
        or envelope.get("timezone") != "Asia/Shanghai"
        or envelope.get("status") not in ("healthy", "stale")
        or _rfc3339(generated_at) is None
        or not valid_text(source.get("name"), 120)
        or not trusted_source_url(source.get("url"), SCHOOL_SOURCE_HOST)
        or not isinstance(notices, list)
        or len(notices) > MAX_EVENT_ITEMS
    ):
        raise ServiceError("校内重要事件不符合 Schema 1.0。")
    envelope_source_name = source.get("name")

    items: list[ImportantEventItem] = []
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        raw_id = notice.get("id")
        notice_id = raw_id.strip() if isinstance(raw_id, str) else ""
        name_value = notice.get("name")
        if name_value is None:
            name_value = notice.get("title")
        name: str | None = None
        if isinstance(name_value, str):
            candidate = name_value.strip()
            name = candidate if valid_text(candidate, 240) else None
        if not valid_text(notice_id, 128) or name is None:
            continue

        source_name = normalized_optional(notice.get("source"), 120)
        if source_name is None:
            source_name = envelope_source_name
        official_url = trusted_https_url(notice.get("source_url"))
        published_at: str | None = None
        raw_published = notice.get("published_at")
        if isinstance(raw_published, str):
            trimmed = raw_published.strip()
            if _exact_date(trimmed) is not None:
                published_at = trimmed

        deadlines = notice.get("deadlines")
        if not isinstance(deadlines, list) or not deadlines:
            primary = notice.get("primary_deadline")
            deadlines = (
                [{"date": primary, "label": notice.get("primary_deadline_label")}]
                if primary is not None
                else []
            )

        for index, deadline in enumerate(deadlines):
            if not isinstance(deadline, dict):
                continue
            raw_date = deadline.get("date")
            if not isinstance(raw_date, str):
                continue
            parsed_deadline = _rfc3339(raw_date.strip())
            if parsed_deadline is None:
                continue
            items.append(
                ImportantEventItem(
                    id=f"school:{notice_id}:{index}",
                    name=name,
                    event_type="competition",
                    source_type="school_notice",
                    primary_deadline=_format_rfc3339(parsed_deadline),
                    deadline_label=normalized_optional(deadline.get("label"), 80)
                    or "截止时间",
                    organizer=source_name,
                    official_url=official_url,
                    source_name=source_name,
                    source_url=official_url,
                    categories=["校内竞赛通知"],
                    tags=[],
                    status=(
                        "upcoming"
                        if parsed_deadline > datetime.now(timezone.utc)
                        else "ended"
                    ),
                    published_at=published_at,
                    stale=envelope.get("status") == "stale",
                    archived=False,
                )
            )
    items.sort(key=lambda item: (item.primary_deadline, item.name))
    return items, str(generated_at)



async def fetch_contest_source() -> tuple[list[ImportantEventItem], str, bool]:
    """Public contest DDL feed, with the fixed HTTPS backup on failure."""
    try:
        raw = await fetch_limited(
            fixed_url(CONTEST_PRIMARY_URL, CONTEST_PRIMARY_HOST),
            MAX_EVENT_BYTES,
            "公开重要事件",
        )
        items, _ = parse_contest_events(raw)
    except ServiceError as primary_error:
        try:
            raw = await fetch_limited(
                fixed_url(CONTEST_BACKUP_URL, SHUTTLE_HOST),
                MAX_EVENT_BYTES,
                "备用公开重要事件",
            )
            items, _ = parse_contest_events(raw)
        except ServiceError as backup_error:
            raise ServiceError(
                f"主重要事件源不可用（{primary_error.message}）；"
                f"备用源也不可用（{backup_error.message}）。"
            ) from backup_error
        return items, CONTEST_BACKUP_URL, True
    return items, CONTEST_PRIMARY_URL, False


async def fetch_events_uncached() -> ImportantEventsResponse:
    public: tuple[list[ImportantEventItem], str, bool] | None = None
    public_error: ServiceError | None = None
    try:
        public = await fetch_contest_source()
    except ServiceError as error:
        public_error = error

    school_items: list[ImportantEventItem] | None = None
    school_error: ServiceError | None = None
    try:
        raw = await fetch_limited(
            fixed_url(SCHOOL_NOTICES_URL, SHUTTLE_HOST),
            MAX_EVENT_BYTES,
            "校内竞赛通知",
        )
        school_items, _ = parse_school_events(raw)
    except ServiceError as error:
        school_error = error

    if public is not None and school_items is not None:
        public_items, source, used_backup = public
        combined = list(public_items) + school_items
        source_label = f"{source} + {SCHOOL_NOTICES_URL}"
    elif public is not None:
        public_items, source, used_backup = public
        combined = list(public_items)
        source_label = source
    elif school_items is not None:
        combined = list(school_items)
        source_label = SCHOOL_NOTICES_URL
        used_backup = False
    else:
        raise ServiceError(
            f"公开重要事件不可用（{public_error.message if public_error else ''}）；"
            f"校内竞赛通知也不可用（{school_error.message if school_error else ''}）。"
        )

    seen: set[str] = set()
    deduped: list[ImportantEventItem] = []
    for item in combined:
        if item.source_type in ("assignment", "custom"):
            continue
        key = favorite_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped.sort(key=lambda item: (item.primary_deadline, item.name))
    response = ImportantEventsResponse(
        fetched_at=now_in_app_tz(),
        source=source_label,
        used_backup=used_backup,
        items=deduped[:MAX_EVENT_ITEMS],
    )
    EVENT_CACHE.save(response)
    return response


async def fetch_important_events() -> ImportantEventsResponse:
    cached = EVENT_CACHE.get()
    if cached is not None:
        return cached
    return await fetch_events_uncached()


async def refresh_important_events() -> ImportantEventsResponse:
    return await fetch_events_uncached()


class ImportantEventSourceFilter(Enum):
    """Source selector used by the CLI/TUI/JSON frontends."""

    ALL = "all"
    PUBLIC = "public"
    SCHOOL = "school"


@dataclass
class ImportantEventFilter:
    query: str = ""
    event_type: str | None = None
    category: str | None = None
    source: ImportantEventSourceFilter = ImportantEventSourceFilter.ALL
    include_ended: bool = False
    favorites_only: bool = False


def favorite_key(item: ImportantEventItem) -> str:
    return f"{item.source_type}:{item.id}"


def available_event_types(items: list[ImportantEventItem]) -> list[str]:
    return [
        event_type
        for event_type in IMPORTANT_EVENT_TYPES
        if any(
            item.event_type == event_type
            and item.source_type in ("contest_ddl", "school_notice")
            for item in items
        )
    ]


def available_event_categories(items: list[ImportantEventItem]) -> list[str]:
    categories = [category for item in items for category in item.categories]
    return sorted(set(categories))


def merge_live_and_favorite_events(
    live: list[ImportantEventItem], favorites: list[ImportantEventItem]
) -> list[ImportantEventItem]:
    items = list(live)
    seen = {favorite_key(item) for item in items}
    for item in favorites:
        key = favorite_key(item)
        if key not in seen:
            seen.add(key)
            items.append(item)
    items.sort(key=lambda item: (item.primary_deadline, item.name))
    return items


def filter_important_events(
    items: list[ImportantEventItem],
    favorites: list[ImportantEventItem],
    filter: ImportantEventFilter,
    now: datetime,
) -> list[ImportantEventItem]:
    """Visible events for the query UI; ``now`` must be timezone-aware (UTC)."""
    favorite_ids = {favorite_key(item) for item in favorites}
    query = filter.query.strip().lower()
    filtered: list[ImportantEventItem] = []
    for item in items:
        if item.archived or item.event_type not in IMPORTANT_EVENT_TYPES:
            continue
        if item.source_type not in ("contest_ddl", "school_notice"):
            continue
        deadline = _rfc3339(item.primary_deadline)
        if deadline is None:
            continue
        parts = [
            item.name,
            item.organizer,
            item.level,
            item.location,
            item.description,
            item.eligibility,
            item.notes,
            item.region,
            item.mode,
            item.status,
            item.deadline_label,
            item.source_name,
            *item.categories,
            *item.tags,
        ]
        haystack = " ".join(part for part in parts if part is not None).lower()
        if query and query not in haystack:
            continue
        if filter.event_type is not None and item.event_type != filter.event_type:
            continue
        if filter.category is not None and filter.category not in item.categories:
            continue
        if (
            filter.source == ImportantEventSourceFilter.PUBLIC
            and item.source_type != "contest_ddl"
        ):
            continue
        if (
            filter.source == ImportantEventSourceFilter.SCHOOL
            and item.source_type != "school_notice"
        ):
            continue
        if not filter.include_ended and deadline < now:
            continue
        if filter.favorites_only and favorite_key(item) not in favorite_ids:
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: (item.primary_deadline, item.name))
    return filtered



# --- local favorites (device-only, never uploaded) ------------------------------


def config_root() -> str | None:
    """Mirror the Rust ``config_root``: LOCALAPPDATA/APPDATA, XDG, or macOS."""
    if sys.platform == "win32":
        return os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or None
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        if not home:
            return None
        return os.path.join(home, "Library", "Application Support")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return xdg
    home = os.environ.get("HOME")
    if not home:
        return None
    return os.path.join(home, ".config")


def favorite_events_path() -> str:
    root = config_root()
    if root is None:
        raise ServiceError("无法确定重要事件收藏目录。")
    return os.path.join(root, "where-to-study", FAVORITES_FILE)


def _validate_stored_item(item: ImportantEventItem) -> None:
    """Reject wrong-typed stored fields the way serde does on load."""
    required_strings = (
        "id",
        "name",
        "event_type",
        "source_type",
        "primary_deadline",
    )
    optional_strings = (
        "deadline_label",
        "organizer",
        "official_url",
        "source_name",
        "source_url",
        "level",
        "location",
        "status",
        "description",
        "eligibility",
        "notes",
        "region",
        "mode",
        "published_at",
    )
    for name in required_strings:
        if not isinstance(getattr(item, name), str):
            raise ServiceError("重要事件收藏格式不正确。")
    for name in optional_strings:
        value = getattr(item, name)
        if value is not None and not isinstance(value, str):
            raise ServiceError("重要事件收藏格式不正确。")
    for name in ("categories", "tags"):
        value = getattr(item, name)
        if not isinstance(value, list) or not all(
            isinstance(entry, str) for entry in value
        ):
            raise ServiceError("重要事件收藏格式不正确。")
    for name in ("stale", "archived"):
        if not isinstance(getattr(item, name), bool):
            raise ServiceError("重要事件收藏格式不正确。")


def load_favorites_from(path: str) -> list[ImportantEventItem]:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ServiceError(
            f"无法检查重要事件收藏文件（{path}）：{error}"
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > MAX_FAVORITES_BYTES
    ):
        raise ServiceError(f"重要事件收藏必须是大小合理的普通文件：{path}")
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_FAVORITES_BYTES + 1)
    except OSError as error:
        raise ServiceError(f"无法读取重要事件收藏（{path}）：{error}") from error
    try:
        stored = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"重要事件收藏格式不正确：{error}") from error
    if not isinstance(stored, dict):
        raise ServiceError("重要事件收藏格式不正确。")
    version = stored.get("version")
    updated_at = stored.get("updated_at")
    raw_items = stored.get("items")
    if (
        "version" not in stored
        or "updated_at" not in stored
        or "items" not in stored
        or not isinstance(version, int)
        or isinstance(version, bool)
        or not isinstance(updated_at, str)
        or not isinstance(raw_items, list)
    ):
        raise ServiceError("重要事件收藏格式不正确。")
    if version != FAVORITES_VERSION or len(raw_items) > MAX_EVENT_ITEMS:
        raise ServiceError("重要事件收藏版本或数量不受支持。")
    items: list[ImportantEventItem] = []
    for entry in raw_items:
        try:
            item = ImportantEventItem.from_dict(entry)
        except TypeError as error:
            raise ServiceError(f"重要事件收藏格式不正确：{error}") from error
        if item is None:
            raise ServiceError("重要事件收藏格式不正确。")
        _validate_stored_item(item)
        items.append(item)
    result: list[ImportantEventItem] = []
    seen: set[str] = set()
    for item in items:
        if (
            not item.archived
            and item.event_type in IMPORTANT_EVENT_TYPES
            and item.source_type in ("contest_ddl", "school_notice")
            and _rfc3339(item.primary_deadline) is not None
        ):
            key = favorite_key(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result



def save_favorites_to(path: str, items: list[ImportantEventItem]) -> None:
    parent = os.path.dirname(path)
    if not parent:
        raise ServiceError("重要事件收藏路径缺少父目录。")
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as error:
        raise ServiceError(f"无法创建重要事件收藏目录：{error}") from error
    try:
        parent_meta = os.lstat(parent)
    except OSError as error:
        raise ServiceError(
            f"无法创建重要事件收藏目录：{error}"
        ) from error
    if stat.S_ISLNK(parent_meta.st_mode) or not stat.S_ISDIR(parent_meta.st_mode):
        raise ServiceError("重要事件收藏目录不是普通目录。")
    if os.path.lexists(path):
        try:
            path_meta = os.lstat(path)
        except OSError as error:
            raise ServiceError(
                f"重要事件收藏路径不是普通文件：{error}"
            ) from error
        if stat.S_ISLNK(path_meta.st_mode) or not stat.S_ISREG(path_meta.st_mode):
            raise ServiceError("重要事件收藏路径不是普通文件。")
    if os.name == "posix":
        try:
            os.chmod(parent, 0o700)
        except OSError as error:
            raise ServiceError(
                f"无法限制重要事件收藏目录权限：{error}"
            ) from error
    stored = FavoritesFile(
        version=FAVORITES_VERSION,
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=list(items),
    )
    try:
        payload = json.dumps(
            stored.to_dict(), ensure_ascii=False, indent=2
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ServiceError(f"无法序列化重要事件收藏：{error}") from error
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".favorite-events-", suffix=".tmp", dir=parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # os.replace overwrites atomically on every platform, matching
        # NamedTempFile::persist after the Rust Windows pre-delete.
        os.replace(temporary_name, path)
        temporary_name = ""
    except OSError as error:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise ServiceError(f"无法提交重要事件收藏：{error}") from error
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as error:
            raise ServiceError(
                f"无法限制重要事件收藏文件权限：{error}"
            ) from error



def load_favorite_events() -> list[ImportantEventItem]:
    return load_favorites_from(favorite_events_path())


def save_favorite_events(items: list[ImportantEventItem]) -> None:
    unique: list[ImportantEventItem] = []
    seen: set[str] = set()
    for item in items:
        key = favorite_key(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    save_favorites_to(favorite_events_path(), unique)


def toggle_favorite_event(
    favorites: list[ImportantEventItem], item: ImportantEventItem
) -> bool:
    """Add or remove ``item`` in place; roll the list back if saving fails."""
    key = favorite_key(item)
    for index, favorite in enumerate(favorites):
        if favorite_key(favorite) == key:
            removed = favorites.pop(index)
            try:
                save_favorite_events(favorites)
            except ServiceError:
                favorites.insert(index, removed)
                raise
            return False
    favorites.append(deepcopy(item))
    try:
        save_favorite_events(favorites)
    except ServiceError:
        favorites.pop()
        raise
    return True


