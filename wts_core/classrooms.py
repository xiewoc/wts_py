"""Realtime empty-classroom queries against the mobile academic system (SJD).

Mirrors ``src-tauri/src/classrooms.rs``. The response shape follows the verified
SJD contract: period names map to zero-based period indices, building names are
normalized to the canonical set shared with the native clients, and a room is
only kept when its canonical building is one the clients know about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

import httpx

from .auth import resolve_credentials
from .config import (
    CAMPUSES,
    EMPTY_CLASSROOM_LOGIN_URL,
    EMPTY_CLASSROOM_TODAY_URL,
    SJD_LOGIN_PAGE_URL,
    SJD_ORIGIN,
    SJD_REST_CLASSROOM_PAGE_URL,
    SLOT_TIMES,
    campus_name,
    now_in_app_tz,
    today_in_app_tz,
)
from .errors import ServiceError
from .http import (
    parse_json,
    read_limited,
    send_with_redirects,
)
from .models import (
    CLASSROOMS_CACHE_VERSION,
    ClassroomStatus,
    ClassroomsCacheResponse,
    ClassroomsRequest,
    ClassroomsResponse,
)
from .session_cache import SessionCache, SessionEpoch, token_ttl

MAX_SJD_REDIRECTS = 10
MAX_SJD_LOGIN_RESPONSE_BYTES = 64 * 1024
MAX_SJD_DATA_RESPONSE_BYTES = 4 * 1024 * 1024

SJD_SESSION: SessionCache = SessionCache()

UNKNOWN_BUILDING = "未知教学楼"
_BUILDING_PREFIXES = ("校本部-", "西土城-", "沙河-")
_SIZE_PATTERN = re.compile(r"[\(（]\s*([0-9]+)\s*[\)）]")
_ROOM_PATTERN = re.compile(r"[0-9]{3}(?:-[0-9]{3})?")
_NODE_PATTERN = re.compile(r"[0-9]+")
_CHAR_DIGIT = re.compile(r"[0-9]")
_DASHES = str.maketrans({c: "-" for c in "－—–"})
_IDEOGRAPHIC_SPACE = "\u3000"

# The two sides of the teaching-experiment building share one upstream name.
_TEACHING_EXPERIMENT_BUILDING = "教学实验综合楼"

_BUILDING_ALIASES: dict[str, str] = {}
for _alias in ("1", "教一楼"):
    _BUILDING_ALIASES[_alias] = "教1"
for _alias in ("2", "教二楼"):
    _BUILDING_ALIASES[_alias] = "教2"
for _alias in ("3", "教三楼"):
    _BUILDING_ALIASES[_alias] = "教3"
for _alias in ("4", "教四楼"):
    _BUILDING_ALIASES[_alias] = "教4"
_BUILDING_ALIASES["未来学习大楼"] = "主楼"
for _alias in (
    "N",
    "N楼",
    "N座",
    "北楼",
    "综合教学楼N",
    "综合教学楼N楼",
    "综合教学楼N座",
    "综合楼N",
    "综合楼N楼",
    "综合N",
):
    _BUILDING_ALIASES[_alias] = "综合教学楼N"
for _alias in (
    "S",
    "S楼",
    "S座",
    "南楼",
    "综合教学楼S",
    "综合教学楼S楼",
    "综合教学楼S座",
    "综合楼S",
    "综合楼S楼",
    "综合S",
):
    _BUILDING_ALIASES[_alias] = "综合教学楼S"
for _alias in (
    "教学实验综合楼N",
    "教学实验综合楼N楼",
    "教学实验综合楼N座",
    "教学实验综合楼北",
    "教学实验综合楼北楼",
    "教学实验综合楼-N",
    "教学实验综合楼-N楼",
    "教学实验综合楼(综教)N",
    "教学实验综合楼（综教）N",
    "教学实验综合楼N(综教)",
    "教学实验综合楼N（综教）",
    "综教N",
    "综教N楼",
    "综教N座",
    "综教北",
    "综教北楼",
    "综教-N",
    "综教-N楼",
):
    _BUILDING_ALIASES[_alias] = "教学实验综合楼N"
for _alias in (
    "教学实验综合楼S",
    "教学实验综合楼S楼",
    "教学实验综合楼S座",
    "教学实验综合楼南",
    "教学实验综合楼南楼",
    "教学实验综合楼-S",
    "教学实验综合楼-S楼",
    "教学实验综合楼(综教)S",
    "教学实验综合楼（综教）S",
    "教学实验综合楼S(综教)",
    "教学实验综合楼S（综教）",
    "综教S",
    "综教S楼",
    "综教S座",
    "综教南",
    "综教南楼",
    "综教-S",
    "综教-S楼",
):
    _BUILDING_ALIASES[_alias] = "教学实验综合楼S"
for _alias in ("智慧楼", "智慧教室楼", "智慧教室"):
    _BUILDING_ALIASES[_alias] = "智慧教学楼"

ORIGINAL_BUILDING_NAMES = frozenset(
    {
        "教1",
        "教2",
        "教3",
        "教4",
        "主楼",
        "综合教学楼N",
        "综合教学楼S",
        "教学实验综合楼N",
        "教学实验综合楼S",
        "智慧教学楼",
    }
)


@dataclass
class RoomAccumulator:
    id: str
    building: str
    room: str
    name: str
    size: int | None
    available_slots: list[int] = field(default_factory=list)


def clear_session() -> None:
    SJD_SESSION.clear()


def session_epoch() -> SessionEpoch:
    return SJD_SESSION.epoch()


async def with_sjd_session(
    account: str,
    password: str,
    request: Callable[[str], Awaitable[Any]],
) -> Any:
    return await with_sjd_session_at(session_epoch(), account, password, request)


async def with_sjd_session_at(
    epoch: SessionEpoch,
    account: str,
    password: str,
    request: Callable[[str], Awaitable[Any]],
) -> Any:
    async def login() -> tuple[str, float]:
        token, ttl = await login_uncached(account, password)
        return token, ttl

    return await SJD_SESSION.run_at(epoch, account, password, login, request)


def sjd_headers(token: str | None, referer: str) -> dict[str, str]:
    headers = {
        "Origin": SJD_ORIGIN,
        "Referer": referer or SJD_ORIGIN,
        "User-Agent": "Mozilla/5.0",
    }
    if token is not None and token.strip():
        if not any(character in token for character in "\r\n\0"):
            headers["token"] = token
    return headers


def sjd_client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
    )


def validate_sjd_redirect_target(target: httpx.URL, previous_request_count: int) -> None:
    """Allow only same-origin HTTPS hops without embedded user information."""
    if previous_request_count > MAX_SJD_REDIRECTS:
        raise ServiceError("移动教务重定向次数过多。")
    origin = httpx.URL(SJD_ORIGIN)
    if target.scheme != "https":
        raise ServiceError("移动教务重定向不得降级为明文传输。")
    if target.username or target.password:
        raise ServiceError("移动教务重定向不得携带用户信息。")
    if target.host != origin.host or target.port != origin.port:
        raise ServiceError("移动教务重定向目标不受信任。")


async def sjd_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: Any = None,
    data: Any = None,
    json: Any = None,
) -> httpx.Response:
    """Send one SJD request, validating every redirect hop before following it."""
    return await send_with_redirects(
        client,
        method,
        url,
        max_redirects=MAX_SJD_REDIRECTS,
        validate=validate_sjd_redirect_target,
        headers=headers,
        params=params,
        data=data,
        json=json,
    )

async def read_sjd_json_response(
    response: httpx.Response, max_bytes: int, response_name: str
) -> Any:
    """Read an SJD response body, then validate its envelope."""
    status = response.status_code
    if status == 401:
        raise ServiceError.expired()
    try:
        body = await read_limited(response, max_bytes, response_name)
    except ServiceError:
        raise
    except httpx.HTTPError as error:
        raise ServiceError(f"无法读取{response_name}响应：{error}") from error
    return parse_sjd_response_bytes(status, body, max_bytes, response_name)


def parse_sjd_response_bytes(
    status: int, body: bytes, max_bytes: int, response_name: str
) -> Any:
    if status == 401:
        raise ServiceError.expired()
    payload = parse_limited_json_bytes(body, max_bytes, response_name)
    code = _numeric_code(payload)
    # Verified SJD contract: an invalid token can return HTTP 500 with JSON
    # {"code":"401","message":"非法访问：/currentTerm"}. This exception is
    # specific to SJD; other HTTP failures and message-only hints never relogin.
    if ((200 <= status < 300) or status == 500) and code == 401:
        raise ServiceError.expired()
    if not 200 <= status < 300:
        raise ServiceError(f"{response_name}获取失败，HTTP {status}。")
    return payload


def parse_limited_json_bytes(body: bytes, max_bytes: int, response_name: str) -> Any:
    if len(body) > max_bytes:
        raise ServiceError(f"{response_name}响应过大。")
    return parse_json(body, response_name)


def _numeric_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def login_empty_classroom(account: str, password: str) -> str:
    """Log in and return the current token (cached per credential digest)."""

    async def request(token: str) -> str:
        return token

    return await with_sjd_session(account, password, request)


async def login_uncached(account: str, password: str) -> tuple[str, float]:
    async with sjd_client(20) as client:
        try:
            response = await sjd_request(
                client,
                "POST",
                EMPTY_CLASSROOM_LOGIN_URL,
                headers=sjd_headers(None, SJD_LOGIN_PAGE_URL),
                data={"userNo": account, "pwd": password},
            )
        except httpx.HTTPError as error:
            raise ServiceError(
                "无法连接空教室服务，请确认网络能访问 jwglweixin.bupt.edu.cn。"
            ) from error

        if response.status_code >= 400:
            raise ServiceError(
                f"空教室服务登录失败，HTTP {response.status_code}。"
            )

        payload = await read_sjd_json_response(
            response, MAX_SJD_LOGIN_RESPONSE_BYTES, "空教室服务"
        )
        if not code_is_success(payload):
            message = first_text(payload, ("Msg", "msg")) or "空教室服务登录失败。"
            raise ServiceError.with_status(message, 401)

        data = payload.get("data") if isinstance(payload, dict) else None
        token = ""
        if isinstance(data, dict):
            raw_token = data.get("token")
            if isinstance(raw_token, str):
                token = raw_token.strip()
        if not token:
            raise ServiceError("空教室服务登录成功但没有返回 token。")

        scope = data if isinstance(data, dict) else payload
        return token, token_ttl(token, _declare_expiry(scope))


def parse_classroom(raw: str) -> tuple[str, str, int | None] | None:
    """Split ``校本部-教一楼-1-101(80)`` into building, room and seat count."""
    clean = raw.strip()
    if not clean:
        return None

    size: int | None = None
    match = _SIZE_PATTERN.search(clean)
    if match is not None:
        digits = match.group(1)
        if len(digits) <= 18:
            size = int(digits)
        clean = clean[: match.start()].strip()

    clean = clean.translate(_DASHES)
    parts = [part.strip() for part in clean.split("-") if part.strip()]
    if len(parts) >= 3 and parts[0] in ("校本部", "西土城", "沙河"):
        index = clean.find(parts[2])
        if index < 0:
            index = len(clean)
        building = clean[:index].rstrip("-").strip()
        room = clean[index:].strip()
    elif "-" in clean:
        building, _, room = clean.partition("-")
        building = building.strip()
        room = room.strip()
    else:
        building = UNKNOWN_BUILDING
        room = clean

    if not building:
        building = UNKNOWN_BUILDING
    if not room:
        room = clean
    return building, room, size


def normalize_building_name(name: str) -> str:
    normalized = name.strip().translate(_DASHES)
    clean = normalized
    for prefix in _BUILDING_PREFIXES:
        if normalized.startswith(prefix):
            clean = normalized[len(prefix) :]
            break
    clean = clean.strip()
    compact = clean.replace(" ", "").replace(_IDEOGRAPHIC_SPACE, "")
    alias = _BUILDING_ALIASES.get(compact)
    if alias is not None:
        return alias
    if not clean:
        return UNKNOWN_BUILDING
    return clean


def is_original_building_name(name: str) -> bool:
    return name in ORIGINAL_BUILDING_NAMES


def infer_teaching_experiment_side(building: str, room_name: str) -> tuple[str, str]:
    """Resolve the shared 教学实验综合楼 name into its N/S side when possible."""
    if building != _TEACHING_EXPERIMENT_BUILDING:
        return building, room_name

    clean_room = (
        room_name.strip()
        .translate(_DASHES)
        .replace(" ", "")
        .replace(_IDEOGRAPHIC_SPACE, "")
    )
    if not clean_room:
        return building, room_name
    side = clean_room[0]
    rest = clean_room[1:].lstrip("-")
    if not rest or not _is_ascii_digit(rest[0]):
        return building, room_name
    if side in ("N", "n", "北"):
        return "教学实验综合楼N", rest
    if side in ("S", "s", "南"):
        return "教学实验综合楼S", rest
    return building, room_name


def extract_room_name(value: str, building: str) -> str | None:
    clean = value.strip().translate(_DASHES)
    if not clean:
        return None
    if building.startswith("教"):
        building_number = building[1:]
        for prefix in (f"{building_number}-", f"教{building_number}-"):
            if clean.startswith(prefix):
                clean = clean[len(prefix) :].strip()
                break
    match = _ROOM_PATTERN.search(clean)
    return match.group(0) if match is not None else None


def node_name_to_slot(value: str) -> int | None:
    match = _NODE_PATTERN.search(value.strip())
    if match is None:
        return None
    node = int(match.group(0))
    if 1 <= node <= len(SLOT_TIMES):
        return node - 1
    return None


def parse_available_classrooms(
    items: Any, room_map: dict[str, RoomAccumulator]
) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        slot = node_name_to_slot(
            value_string(_first_present(item, ("NODENAME", "nodeName", "nodename")))
        )
        if slot is None:
            continue
        classrooms = value_string(
            _first_present(item, ("CLASSROOMS", "classrooms", "Classrooms"))
        )
        for classroom in (part.strip() for part in classrooms.split(",")):
            if not classroom:
                continue
            parsed = parse_classroom(classroom)
            if parsed is None:
                continue
            building_name, room_name, size = parsed
            building, room_name = infer_teaching_experiment_side(
                normalize_building_name(building_name), room_name
            )
            if not is_original_building_name(building):
                continue
            room = extract_room_name(room_name, building)
            if room is None:
                continue
            key = f"{building}-{room}"
            entry = room_map.get(key)
            if entry is None:
                entry = RoomAccumulator(
                    id=key, building=building, room=room, name=key, size=size
                )
                room_map[key] = entry
            elif entry.size is None and size is not None:
                entry.size = size
            if slot not in entry.available_slots:
                entry.available_slots.append(slot)


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _is_ascii_digit(character: str) -> bool:
    return len(character) == 1 and "0" <= character <= "9"


async def fetch_realtime_classrooms(
    client: httpx.AsyncClient, token: str, campus_id: str
) -> list[Any]:
    try:
        response = await sjd_request(
            client,
            "GET",
            EMPTY_CLASSROOM_TODAY_URL,
            params={"campusId": campus_id},
            headers=sjd_headers(token, SJD_REST_CLASSROOM_PAGE_URL),
        )
    except httpx.HTTPError as error:
        raise ServiceError("实时教室数据获取失败，请稍后重试。") from error

    payload = await read_sjd_json_response(
        response, MAX_SJD_DATA_RESPONSE_BYTES, "实时教室服务"
    )
    if not code_is_success(payload):
        message = first_text(payload, ("Msg", "msg")) or "实时教室数据获取失败。"
        raise ServiceError(message)

    data = payload.get("data") if isinstance(payload, dict) else None
    return list(data) if isinstance(data, list) else []


def service_date_from_payload(payload: ClassroomsRequest) -> date:
    target = (payload.target_date or "").strip()
    if target:
        try:
            service_date = date.fromisoformat(target)
        except ValueError as error:
            raise ServiceError.with_status("查询日期格式不正确。", 400) from error
    else:
        service_date = today_in_app_tz()

    if service_date != today_in_app_tz():
        raise ServiceError.with_status("空教室实时接口仅支持当天查询。", 400)
    return service_date


def classrooms_response_from_items(
    campus_id: str,
    service_date: date,
    fetched_at: str,
    available_classrooms: Any,
) -> ClassroomsResponse:
    room_map: dict[str, RoomAccumulator] = {}
    parse_available_classrooms(available_classrooms, room_map)

    rooms = [
        ClassroomStatus(
            id=item.id,
            building=item.building,
            room=item.room,
            name=item.name,
            size=item.size,
            type="",
            available_slots=sorted(set(item.available_slots)),
            source="sjd",
        )
        for item in room_map.values()
    ]
    rooms.sort(key=lambda room: (room.building, room.room))

    return ClassroomsResponse(
        campus_id=campus_id,
        campus_name=campus_name(campus_id),
        target_date=service_date.isoformat(),
        fetched_at=fetched_at,
        realtime=True,
        provider="sjd",
        rooms=rooms,
    )


def code_is_success(payload: Any) -> bool:
    code = _numeric_code(payload)
    if code is not None:
        return code == 1
    if isinstance(payload, dict):
        return payload.get("code") == "1"
    return False


def _declare_expiry(scope: Any) -> int | None:
    if not isinstance(scope, dict):
        return None
    for key in ("expires_in", "expiresIn"):
        value = scope.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return None


def first_text(payload: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def value_string(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return ""


async def fetch_all_classrooms(payload: ClassroomsRequest) -> ClassroomsCacheResponse:
    return await fetch_all_classrooms_at(session_epoch(), payload)


async def fetch_all_classrooms_at(
    epoch: SessionEpoch, payload: ClassroomsRequest
) -> ClassroomsCacheResponse:
    service_date = service_date_from_payload(payload)
    user, secret = resolve_credentials(payload.account, payload.password)

    async def request(token: str) -> ClassroomsCacheResponse:
        return await fetch_all_classrooms_with_token(service_date, token)

    return await with_sjd_session_at(epoch, user, secret, request)


async def fetch_all_classrooms_with_token(
    service_date: date, token: str
) -> ClassroomsCacheResponse:
    campus_items: list[tuple[str, list[Any]]] = []
    async with sjd_client(30) as client:
        for campus in CAMPUSES:
            items = await fetch_realtime_classrooms(client, token, campus.id)
            campus_items.append((campus.id, items))

    fetched_at = now_in_app_tz()
    campuses = [
        classrooms_response_from_items(campus_id, service_date, fetched_at, items)
        for campus_id, items in campus_items
    ]

    return ClassroomsCacheResponse(
        cache_version=CLASSROOMS_CACHE_VERSION,
        target_date=service_date.isoformat(),
        fetched_at=fetched_at,
        realtime=True,
        provider="sjd",
        campuses=campuses,
    )

