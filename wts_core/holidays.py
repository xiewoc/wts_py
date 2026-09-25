"""Calendar holidays (public holidays and transfer workdays).

Mirrors ``src-tauri/src/holidays.rs`` for the headless part: the fixed source
URL is validated before use, the schema is validated before publishing, and an
offline response is produced from the pinned authoritative fallback data.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from .config import APP_TZ, now_in_app_tz
from .errors import ServiceError
from .http import public_client, read_limited, send_with_redirects
from .models import HolidayItem, HolidaysResponse

DATA_SOURCE = "https://unpkg.com/holiday-calendar@1.3.3/data/CN"
FALLBACK_SOURCE = "https://www.gov.cn/yaowen/liebiao/202511/content_7047099.htm"
USER_AGENT = "WhereToStudyNative/0.3.1"
MAX_RESPONSE_BYTES = 256 * 1024
MIN_YEAR = 1900
MAX_YEAR = 2100
MAX_SOURCE_LENGTH = 512
MAX_FETCHED_AT_LENGTH = 64
MAX_RECORDS = 128
MAX_NAME_LENGTH = 80
MAX_EXPANDED_ITEMS = 512
MAX_REDIRECTS = 10
SOURCE_HOST = "unpkg.com"

KIND_ALIASES = {"public_holiday": "holiday", "transfer_workday": "workday"}
HOLIDAY_CACHE_PREFIX = "holidays"

_DATE_WITH_DASH_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


def _contract_date(value: str) -> date:
    if not _DATE_WITH_DASH_PATTERN.match(value):
        raise ServiceError("节假日数据日期格式不正确。")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ServiceError("节假日数据日期格式不正确。") from error
    return parsed


def validate_requested_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ServiceError("节假日年份不在支持范围内。")


def validate_fetch_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ServiceError.with_status("节假日年份不在支持范围内。", 400)


def _contract_timestamp(value: str) -> datetime:
    if len(value) > MAX_FETCHED_AT_LENGTH or not _TIMESTAMP_PATTERN.match(value):
        raise ServiceError("本地节假日缓存的获取时间不正确。")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ServiceError("本地节假日缓存的获取时间不正确。") from error


def now_contract_timestamp() -> str:
    current = now_in_app_tz()
    timestamp = _contract_timestamp(current)
    return timestamp.astimezone(timezone.utc).astimezone(APP_TZ).isoformat(
        timespec="seconds"
    )


def validate_holidays_response(
    response: HolidaysResponse, expected_year: int | None
) -> None:
    validate_requested_year(response.year)
    if expected_year is not None and expected_year != response.year:
        raise ServiceError("本地节假日缓存年份与请求不一致。")
    if not response.source.strip() or len(response.source) > MAX_SOURCE_LENGTH:
        raise ServiceError("本地节假日缓存的数据源不正确。")
    _contract_timestamp(response.fetched_at)
    if len(response.items) > MAX_EXPANDED_ITEMS:
        raise ServiceError("本地节假日缓存的条目数量超过限制。")
    for item in response.items:
        item_date = _contract_date(item.date)
        if item_date.year != response.year:
            raise ServiceError("本地节假日缓存包含其他年份的日期。")
        if not item.name.strip() or len(item.name) > MAX_NAME_LENGTH:
            raise ServiceError("本地节假日缓存的名称不正确。")
        if item.kind not in ("holiday", "workday"):
            raise ServiceError("本地节假日缓存的类型不正确。")

def parse_source_item(item: Any, year: int) -> HolidayItem | None:
    if not isinstance(item, dict):
        raise ServiceError("节假日数据解析失败：记录不是对象。")
    name = ""
    for key in ("name_cn", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if not name:
        raise ServiceError("节假日名称不能为空。")
    if len(name) > MAX_NAME_LENGTH:
        raise ServiceError("节假日名称过长。")
    item_date = _contract_date(str(item.get("date", "")))
    if item_date.year != year:
        raise ServiceError("节假日数据包含其他年份的日期。")
    raw_kind = item.get("type")
    if not isinstance(raw_kind, str):
        raise ServiceError("节假日数据解析失败：缺少 type 字段。")
    kind = KIND_ALIASES.get(raw_kind.strip())
    if kind is None:
        return None
    return HolidayItem(date=item_date.isoformat(), name=name, kind=kind)


def parse_source_payload(payload: Any, year: int) -> list[HolidayItem]:
    validate_requested_year(year)
    if not isinstance(payload, dict):
        raise ServiceError("节假日数据解析失败：响应不是对象。")
    if payload.get("year") != year:
        raise ServiceError("节假日数据年份与请求不一致。")
    if payload.get("region") != "CN":
        raise ServiceError("节假日数据区域不正确。")
    dates = payload.get("dates")
    if not isinstance(dates, list):
        raise ServiceError("节假日数据解析失败：dates 不是数组。")
    if len(dates) > MAX_RECORDS:
        raise ServiceError("节假日数据记录过多。")

    days: list[HolidayItem] = []
    for item in dates:
        parsed = parse_source_item(item, year)
        if parsed is not None:
            if len(days) >= MAX_EXPANDED_ITEMS:
                raise ServiceError("节假日展开记录过多。")
            days.append(parsed)
    days.sort(key=lambda day: (day.date, day.kind, day.name))
    if not days:
        raise ServiceError("节假日数据没有可识别的法定节假日或调休记录。")
    return days


def decode_source(raw: bytes, year: int) -> list[HolidayItem]:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"节假日数据解析失败：{error}") from error
    return parse_source_payload(payload, year)


def decode_cache(raw: bytes, expected_year: int) -> HolidaysResponse | None:
    validate_requested_year(expected_year)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ServiceError("本地节假日缓存过大。")
    if not raw:
        return None
    try:
        cached = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"本地节假日缓存格式不正确：{error}") from error
    if not isinstance(cached, dict):
        raise ServiceError("本地节假日缓存格式不正确。")
    if set(cached) != {"year", "source", "fetched_at", "items"}:
        raise ServiceError("本地节假日缓存格式不正确。")
    raw_items = cached.get("items")
    if not isinstance(raw_items, list):
        raise ServiceError("本地节假日缓存格式不正确。")
    if not isinstance(cached.get("year"), int) or isinstance(
        cached.get("year"), bool
    ):
        raise ServiceError("本地节假日缓存格式不正确。")
    if not isinstance(cached.get("source"), str) or not isinstance(
        cached.get("fetched_at"), str
    ):
        raise ServiceError("本地节假日缓存格式不正确。")
    items: list[HolidayItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict) or set(entry) != {"date", "name", "type"}:
            raise ServiceError("本地节假日缓存格式不正确。")
        items.append(
            HolidayItem(
                date=str(entry.get("date", "")),
                name=str(entry.get("name", "")),
                kind=str(entry.get("type", "")),
            )
        )
    response = HolidaysResponse(
        year=cached["year"],
        source=cached["source"],
        fetched_at=cached["fetched_at"],
        items=items,
    )
    validate_holidays_response(response, expected_year)
    return response


def load_cache_from_path(path: str, year: int) -> HolidaysResponse | None:
    validate_requested_year(year)
    if not os.path.exists(path):
        return None
    if os.path.getsize(path) > MAX_RESPONSE_BYTES:
        raise ServiceError("本地节假日缓存过大。")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise ServiceError(f"无法读取本地节假日缓存：{error}") from error
    return decode_cache(raw, year)


def save_cache_to_path(path: str, response: HolidaysResponse) -> None:
    raw = encode_cache(response)
    directory = os.path.dirname(path)
    if not directory:
        raise ServiceError("无法定位本地节假日缓存目录。")
    temporary_name = ""
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            temporary_name = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as error:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise ServiceError(f"无法替换本地节假日缓存：{error}") from error


def fallback_2026_items() -> list[HolidayItem]:
    ranges = (
        ("元旦", "2026-01-01", "2026-01-03", "holiday"),
        ("元旦补班", "2026-01-04", "2026-01-04", "workday"),
        ("春节补班", "2026-02-14", "2026-02-14", "workday"),
        ("春节", "2026-02-15", "2026-02-23", "holiday"),
        ("春节补班", "2026-02-28", "2026-02-28", "workday"),
        ("清明节", "2026-04-04", "2026-04-06", "holiday"),
        ("劳动节", "2026-05-01", "2026-05-05", "holiday"),
        ("劳动节补班", "2026-05-09", "2026-05-09", "workday"),
        ("端午节", "2026-06-19", "2026-06-21", "holiday"),
        ("中秋节", "2026-09-25", "2026-09-27", "holiday"),
        ("国庆节补班", "2026-09-20", "2026-09-20", "workday"),
        ("国庆节", "2026-10-01", "2026-10-07", "holiday"),
        ("国庆节补班", "2026-10-10", "2026-10-10", "workday"),
    )
    items: list[HolidayItem] = []
    for name, start, end, kind in ranges:
        current = _contract_date(start)
        last = _contract_date(end)
        while current <= last:
            items.append(HolidayItem(date=current.isoformat(), name=name, kind=kind))
            current += timedelta(days=1)
    return items


def offline_response(year: int) -> HolidaysResponse:
    if year == 2026:
        response = HolidaysResponse(
            year=year,
            source=FALLBACK_SOURCE,
            fetched_at=now_contract_timestamp(),
            items=fallback_2026_items(),
        )
    else:
        response = HolidaysResponse(
            year=year,
            source=f"unavailable: {DATA_SOURCE}",
            fetched_at=now_contract_timestamp(),
            items=[],
        )
    validate_holidays_response(response, year)
    return response


def validate_holiday_redirect_target(
    target: httpx.URL, previous_request_count: int
) -> None:
    if previous_request_count > MAX_REDIRECTS:
        raise ServiceError("节假日数据源重定向次数过多。")
    if target.scheme != "https":
        raise ServiceError("节假日数据源不得降级为明文传输。")
    if target.host != SOURCE_HOST:
        raise ServiceError("节假日数据源重定向目标不受信任。")
    if target.username or target.password:
        raise ServiceError("节假日数据源重定向不得携带用户信息。")


async def fetch_remote(year: int) -> HolidaysResponse:
    url = f"{DATA_SOURCE}/{year}.json"
    async with public_client(20, connect_timeout_seconds=15) as client:
        try:
            response = await send_with_redirects(
                client,
                "GET",
                url,
                max_redirects=MAX_REDIRECTS,
                validate=validate_holiday_redirect_target,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
        except httpx.HTTPError as error:
            raise ServiceError(f"无法获取节假日数据：{error}") from error
        if response.status_code >= 400:
            raise ServiceError(f"节假日数据源返回错误：HTTP {response.status_code}。")
        body = await read_limited(response, MAX_RESPONSE_BYTES, "节假日数据")
    return HolidaysResponse(
        year=year,
        source=DATA_SOURCE,
        fetched_at=now_contract_timestamp(),
        items=decode_source(body, year),
    )


def cache_path_for(year: int, directory: str | None = None) -> str:
    """``<config-root>/where-to-study/holidays_<year>.json`` unless overridden."""
    validate_requested_year(year)
    root = directory if directory is not None else default_cache_dir()
    return os.path.join(root, f"{HOLIDAY_CACHE_PREFIX}_{year}.json")


def default_cache_dir() -> str:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    elif sys.platform == "darwin":
        home = os.environ.get("HOME")
        root = os.path.join(home, "Library", "Application Support") if home else None
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or (
            os.path.join(os.environ["HOME"], ".config")
            if os.environ.get("HOME")
            else None
        )
    if not root:
        raise ServiceError("无法定位本地节假日目录。")
    return os.path.join(root, "where-to-study")


async def fetch_holidays(year: int, directory: str | None = None) -> HolidaysResponse:
    """Remote first, then the on-disk cache, then the pinned offline dataset.

    Mirrors the Tauri ``fetch_holidays`` command: a failed cache write never
    loses a good remote response, and a failed cache read degrades to the
    offline fallback instead of surfacing an I/O error.
    """
    validate_fetch_year(year)
    try:
        response = await fetch_remote(year)
    except ServiceError:
        try:
            cached = load_cache_from_path(cache_path_for(year, directory), year)
        except ServiceError:
            cached = None
        if cached is not None:
            return cached
        return offline_response(year)
    try:
        save_cache_to_path(cache_path_for(year, directory), response)
    except ServiceError:
        pass  # Best effort: the freshly fetched response is still published.
    return response


def encode_cache(response: HolidaysResponse) -> bytes:
    validate_holidays_response(response, None)
    raw = json.dumps(response.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ServiceError("本地节假日缓存过大。")
    return raw
