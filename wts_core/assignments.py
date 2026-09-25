"""UCloud teaching-cloud assignment queries (credential-scoped).

Mirrors ``src-tauri/src/assignments.rs``: one CAS + OAuth login per credential
digest, every response is size-limited and redirect-refused, the in-memory
cache is rejected the moment the credential revision changes, and nothing
(tokens, cookies, items) is ever written to disk. Unlike Rust's ``Zeroizing``
types, Python strings cannot be wiped after drop; references are dropped
instead and no copy leaves process memory.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from .errors import ServiceError
from .http import JSON_ACCEPT, public_client, read_limited
from .models import (
    AssignmentCalendarResponse,
    AssignmentDeadlineItem,
    AssignmentsRequest,
    AssignmentsResponse,
    CalendarRangeRequest,
)
from .session_cache import SessionCache, check_auth_payload, token_ttl

SOURCE_URL = "https://ucloud.bupt.edu.cn/uclass/"
UCLOUD_ORIGIN = "https://apiucloud.bupt.edu.cn"
UCLOUD_HOST = "apiucloud.bupt.edu.cn"
CAS_LOGIN_URL = (
    "https://auth.bupt.edu.cn/authserver/login"
    "?service=https%3A%2F%2Fucloud.bupt.edu.cn"
)
CAS_SERVICE_ORIGIN = "https://ucloud.bupt.edu.cn"
CAS_SERVICE_HOST = "ucloud.bupt.edu.cn"
# Public OAuth client id shipped by the official UCloud web app; not a credential.
PORTAL_AUTHORIZATION = "Basic  cG9ydGFsOnBvcnRhbF9zZWNyZXQ="
TENANT_ID = "000000"
USER_AGENT_VALUE = (
    "WhereToStudy/0.3.1 (+https://github.com/Nemoyu/where_to_study)"
)
MAX_LOGIN_HTML_BYTES = 1024 * 1024
MAX_TOKEN_BYTES = 512 * 1024
MAX_API_BYTES = 8 * 1024 * 1024
MAX_COOKIE_BYTES = 16 * 1024
MAX_COURSES = 100
MAX_ASSIGNMENTS = 5_000
COURSE_PAGE_SIZE = 9_999
ASSIGNMENT_PAGE_SIZE = 9_999
CACHE_TTL = 10 * 60
MAX_CALENDAR_RANGE_DAYS = 370

RECORD_POINTERS = (
    "/data/records",
    "/data/data/records",
    "/records",
    "/data/undoneList",
    "/data/data/undoneList",
    "/undoneList",
)


@dataclass
class CourseRef:
    id: str
    name: str | None = None


class AssignmentCache:
    """Single-slot, credential-revision-guarded assignment cache."""

    def __init__(self) -> None:
        self._revision = 0
        self._snapshot: tuple[str, int, float, list[AssignmentDeadlineItem]] | None = (
            None
        )

    def clear(self) -> None:
        self._revision += 1
        self._snapshot = None

    def credential_revision(self) -> int:
        return self._revision

    def ensure_revision(self, revision: int) -> None:
        if self._revision != revision:
            raise ServiceError("教学云平台凭据已更改，请重新获取作业。")

    def items(
        self, account_scope: str, request_revision: int
    ) -> list[AssignmentDeadlineItem] | None:
        self.ensure_revision(request_revision)
        snapshot = self._snapshot
        if snapshot is None:
            return None
        scope, revision, fetched_at, items = snapshot
        if (
            scope == account_scope
            and revision == request_revision
            and time.monotonic() - fetched_at < CACHE_TTL
        ):
            return list(items)
        return None

    def save(
        self,
        account_scope: str,
        items: list[AssignmentDeadlineItem],
        request_revision: int,
    ) -> None:
        self.ensure_revision(request_revision)
        self._snapshot = (
            account_scope,
            request_revision,
            time.monotonic(),
            list(items),
        )


ASSIGNMENT_CACHE = AssignmentCache()
ASSIGNMENT_SESSION: SessionCache = SessionCache()
ASSIGNMENT_FETCH = asyncio.Lock()


def clear_cache() -> None:
    ASSIGNMENT_CACHE.clear()
    ASSIGNMENT_SESSION.clear()


def credential_revision() -> int:
    return ASSIGNMENT_CACHE.credential_revision()


def ensure_credential_revision(revision: int) -> None:
    ASSIGNMENT_CACHE.ensure_revision(revision)


def base_headers() -> dict[str, str]:
    return {
        "Accept": JSON_ACCEPT,
        "Authorization": PORTAL_AUTHORIZATION,
        "Tenant-Id": TENANT_ID,
        "User-Agent": USER_AGENT_VALUE,
        "Referer": "https://ucloud.bupt.edu.cn/",
    }



def text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    return ""


_RFC3339_PATTERN = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


def normalized_deadline(value: str) -> tuple[date, str] | None:
    trimmed = value.strip()
    if _RFC3339_PATTERN.match(trimmed) is not None:
        try:
            parsed = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        # Match chrono: keep the offset, drop the fraction only when absent.
        normalized = parsed.isoformat()
        return parsed.date(), normalized
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed_naive = datetime.strptime(trimmed, fmt)
        except ValueError:
            continue
        return parsed_naive.date(), parsed_naive.strftime("%Y-%m-%d %H:%M:%S")
    return None


def assignment_status(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return {
            99: "未提交",
            0: "已提交",
            1: "已批改",
            2: "已驳回",
        }.get(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        return stripped or None
    return None


_MISSING = object()


def _pointer(payload: Any, pointer: str) -> Any:
    """JSON-pointer lookup that distinguishes JSON null from a missing path."""
    node = payload
    for part in pointer.split("/")[1:]:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def collect_records(payload: Any) -> list[Any]:
    for pointer in RECORD_POINTERS:
        value = _pointer(payload, pointer)
        if isinstance(value, list):
            return value
    return []


def validate_record_collection(payload: Any) -> None:
    # Rust finds the first pointer that *exists* (even null or non-array) and
    # fails on that one instead of searching on for a later usable array.
    for pointer in RECORD_POINTERS:
        value = _pointer(payload, pointer)
        if value is not _MISSING:
            if isinstance(value, list):
                return
            break
    raise ServiceError("教学云列表格式不正确，保留上次结果。")


def is_assignment_record(raw: Any) -> bool:
    if not isinstance(raw, dict) or "type" not in raw:
        return True
    kind = raw.get("type")
    numeric: int | None
    if isinstance(kind, bool):
        numeric = None
    elif isinstance(kind, int):
        numeric = kind
    elif isinstance(kind, str):
        try:
            numeric = int(kind.strip())
        except ValueError:
            numeric = None
    else:
        numeric = None
    if numeric is None:
        return True
    return numeric in (3, 5)


def parse_assignment_record(
    raw: Any, course_name_override: str | None
) -> AssignmentDeadlineItem | None:
    if not isinstance(raw, dict):
        return None
    if not is_assignment_record(raw):
        return None
    deadline_source = _first_present(raw, ("assignmentEndTime", "endTime"))
    normalized = normalized_deadline(text(deadline_source))
    if normalized is None:
        return None
    _, deadline = normalized
    item_id = text(_first_present(raw, ("id", "assignmentId", "activityId")))
    title = text(_first_present(raw, ("assignmentTitle", "activityName", "title")))
    if not item_id.strip() or not title.strip():
        return None
    embedded = text(_first_present(raw, ("siteName", "courseName", "siteTitle")))
    if embedded.strip():
        course_name: str | None = embedded
    elif course_name_override is not None and course_name_override.strip():
        course_name = course_name_override.strip()
    else:
        course_name = None
    status_raw = raw.get("assignmentStatus")
    return AssignmentDeadlineItem(
        id=item_id,
        title=title,
        course_name=course_name,
        deadline=deadline,
        status=assignment_status(status_raw),
    )


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Rust ``Option::or_else``: a present-but-null key does not fall through."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def parse_all_assignment_deadlines(
    payload: Any, course_name_override: str | None
) -> list[AssignmentDeadlineItem]:
    items: list[AssignmentDeadlineItem] = []
    for raw in collect_records(payload):
        item = parse_assignment_record(raw, course_name_override)
        if item is not None:
            items.append(item)
    return items


def parse_assignment_deadlines(
    payload: Any, requested_date: date
) -> list[AssignmentDeadlineItem]:
    requested = requested_date.isoformat()
    items = [
        item
        for item in parse_all_assignment_deadlines(payload, None)
        if item.deadline[:10] == requested
    ]
    sort_items(items)
    return items


def sort_items(items: list[AssignmentDeadlineItem]) -> None:
    # ``course_name is None`` sorts before every string, matching Option order.
    items.sort(
        key=lambda item: (
            item.deadline,
            0 if item.course_name is None else 1,
            item.course_name or "",
            item.title,
            item.id,
        )
    )


def assignment_items_in_range(
    all_items: list[AssignmentDeadlineItem], start: date, end: date
) -> list[AssignmentDeadlineItem]:
    items: list[AssignmentDeadlineItem] = []
    for item in all_items:
        head = item.deadline[:10]
        try:
            day = date.fromisoformat(head) if len(head) == 10 else None
        except ValueError:
            day = None
        if day is not None and start <= day <= end:
            items.append(item)
    sort_items(items)
    return items


def decode_html_attribute(value: str) -> str:
    return (
        value.replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


_INPUT_PATTERN = re.compile(r"<input\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTRIBUTE_PATTERN = re.compile(
    r"\b(name|value)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE | re.DOTALL,
)


def parse_execution(html: str) -> str | None:
    for input_match in _INPUT_PATTERN.finditer(html):
        name: str | None = None
        value: str | None = None
        for captures in _ATTRIBUTE_PATTERN.finditer(input_match.group(0)):
            attribute_value = (
                captures.group(2)
                if captures.group(2) is not None
                else captures.group(3)
                if captures.group(3) is not None
                else captures.group(4)
            )
            if attribute_value is None:
                return None
            attribute_value = decode_html_attribute(attribute_value)
            key = captures.group(1).lower()
            if key == "name":
                name = attribute_value
            elif key == "value":
                value = attribute_value
        if name == "execution":
            if value is None or not value.strip():
                return None
            return value
    return None


def cookies_from(response: httpx.Response) -> str:
    cookies: list[str] = []
    for value in response.headers.get_list("set-cookie"):
        cookie = value.split(";")[0].strip()
        if cookie:
            cookies.append(cookie)
    joined = "; ".join(cookies)
    if not joined or len(joined.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise ServiceError("统一认证未返回有效会话 Cookie。")
    return joined


def parse_json(body: bytes, label: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ServiceError(f"{label}没有返回有效 JSON：{error}") from error


def business_success(payload: Any) -> bool:
    """A missing ``code`` is a plain envelope (success); anything else needs 200."""
    if not isinstance(payload, dict) or "code" not in payload:
        return True
    code = payload.get("code")
    if isinstance(code, bool):
        return False
    if isinstance(code, int):
        return code == 200
    if isinstance(code, str):
        return code == "200"
    return False


def validate_api_url(url: httpx.URL) -> None:
    if (
        url.scheme != "https"
        or url.host != UCLOUD_HOST
        or url.username
        or url.password
    ):
        raise ServiceError("教学云接口地址不受信任。")


def validate_ticket_location(location: str) -> str:
    try:
        base = httpx.URL(CAS_SERVICE_ORIGIN)
        url = base.join(location)
    except (httpx.InvalidURL, ValueError) as error:
        raise ServiceError("统一认证返回了无效跳转地址。") from error
    if (
        url.scheme != "https"
        or url.host != CAS_SERVICE_HOST
        or url.username
        or url.password
    ):
        raise ServiceError("统一认证返回了不受信任的跳转地址。")
    from urllib.parse import parse_qsl

    # httpx keeps ``query`` as raw bytes; decode first so names compare as str
    # and percent-encoded values unquote the way Rust's ``query_pairs`` does.
    query = url.query.decode("utf-8", "replace") if url.query else ""
    ticket = next(
        (
            value
            for name, value in parse_qsl(query, keep_blank_values=True)
            if name == "ticket" and value.strip()
        ),
        None,
    )
    if ticket is None:
        raise ServiceError.with_status(
            "统一认证未返回有效票据；请检查账号密码，若官方页面要求验证码请先完成验证。",
            401,
        )
    return ticket


def parse_date(value: str) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ServiceError.with_status("作业日期格式不正确。", 400)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ServiceError.with_status("作业日期格式不正确。", 400) from error
    if parsed.isoformat() != value:
        raise ServiceError.with_status("作业日期格式不正确。", 400)
    return parsed


async def parse_api_response(response: httpx.Response) -> Any:
    if response.is_redirect:
        raise ServiceError("教学云接口返回了不受信任的重定向。")
    status = response.status_code
    body = await read_limited(response, MAX_API_BYTES, "教学云数据接口")
    if status >= 400:
        raise ServiceError.with_status(
            f"教学云数据接口返回 HTTP {status}。", status
        )
    payload = parse_json(body, "教学云数据接口")
    check_auth_payload(payload)
    if not business_success(payload):
        raise ServiceError(
            f"教学云数据接口返回业务状态 {text(payload.get('code'))}。"
        )
    return payload


def _api_url(path: str, query: list[tuple[str, str]] | None = None) -> httpx.URL:
    try:
        url = httpx.URL(UCLOUD_ORIGIN).join(path)
    except (httpx.InvalidURL, ValueError) as error:
        raise ServiceError(f"教学云接口地址无效：{error}") from error
    validate_api_url(url)
    if query:
        url = url.copy_set_params(*[list(pair) for pair in query])
    return url


@dataclass
class AuthenticatedClient:
    client: httpx.AsyncClient
    access_token: str
    user_id: str
    ttl: float

    def api_headers(self) -> dict[str, str]:
        if any(character in self.access_token for character in "\r\n"):
            raise ServiceError("教学云访问令牌格式不正确。")
        headers = base_headers()
        headers["Blade-Auth"] = self.access_token
        return headers

    async def get(self, path: str, query: list[tuple[str, str]]) -> Any:
        url = _api_url(path, query)
        try:
            response = await self.client.get(url, headers=self.api_headers())
        except httpx.HTTPError as error:
            raise ServiceError(f"无法连接教学云数据接口：{error}") from error
        return await parse_api_response(response)

    async def post_json(self, path: str, body: dict[str, Any]) -> Any:
        url = _api_url(path)
        try:
            response = await self.client.post(
                url, headers=self.api_headers(), json=body
            )
        except httpx.HTTPError as error:
            raise ServiceError(f"无法连接教学云作业接口：{error}") from error
        return await parse_api_response(response)


async def authenticate(account: str, password: str) -> AuthenticatedClient:
    if not account.strip() or not password:
        raise ServiceError.with_status(
            "请先在设置中保存教务账号和密码。", 401
        )
    client = public_client(20, connect_timeout_seconds=8)

    def fail(message: str) -> ServiceError:
        return ServiceError(message)

    try:
        try:
            login_page = await client.get(
                CAS_LOGIN_URL,
                headers={"Accept": "text/html", "User-Agent": USER_AGENT_VALUE},
            )
        except httpx.HTTPError as error:
            raise fail(f"无法连接统一认证登录页：{error}")
        if login_page.status_code >= 400:
            raise fail(
                f"统一认证登录页返回 HTTP {login_page.status_code}。"
            )
        cookies = cookies_from(login_page)
        try:
            login_html_bytes = await read_limited(
                login_page, MAX_LOGIN_HTML_BYTES, "统一认证登录页"
            )
            login_html = login_html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise fail("统一认证登录页编码不正确。")
        execution = parse_execution(login_html)
        if not execution:
            raise fail("统一认证登录页缺少 execution 参数。")

        try:
            login_response = await client.post(
                CAS_LOGIN_URL,
                headers={
                    "Cookie": cookies,
                    "Referer": CAS_LOGIN_URL,
                    "User-Agent": USER_AGENT_VALUE,
                },
                data={
                    "username": account.strip(),
                    "password": password,
                    "type": "username_password",
                    "execution": execution,
                    "_eventId": "submit",
                },
            )
        except httpx.HTTPError as error:
            raise fail(f"无法提交统一认证登录：{error}")
        location = login_response.headers.get("location", "")
        ticket = validate_ticket_location(location)
        token_url = _api_url("/ykt-basics/oauth/token")
        try:
            token_response = await client.post(
                token_url,
                headers=base_headers(),
                data={"ticket": ticket, "grant_type": "third"},
            )
        except httpx.HTTPError as error:
            raise fail(f"无法换取教学云访问令牌：{error}")
        if token_response.is_redirect:
            raise fail("教学云令牌接口返回了不受信任的重定向。")
        token_status = token_response.status_code
        token_bytes = await read_limited(
            token_response, MAX_TOKEN_BYTES, "教学云令牌接口"
        )
        if token_status >= 400:
            raise fail(f"教学云令牌接口返回 HTTP {token_status}。")
        try:
            token_payload = json.loads(token_bytes)
        except (ValueError, UnicodeDecodeError) as error:
            raise fail(f"教学云令牌接口数据格式不正确：{error}")
        if not isinstance(token_payload, dict):
            raise fail("教学云令牌接口数据格式不正确。")
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise fail("教学云令牌接口未返回访问令牌。")
        user_id = text(token_payload.get("user_id") or token_payload.get("userId"))
        if not user_id.strip():
            raise fail("教学云令牌接口未返回用户标识。")
        raw_expiry = token_payload.get("expires_in")
        if isinstance(raw_expiry, bool) or not isinstance(raw_expiry, (int, str)):
            expiry: int | None = None
        elif isinstance(raw_expiry, int):
            expiry = raw_expiry
        else:
            try:
                expiry = int(raw_expiry)
            except ValueError:
                expiry = None
        ttl = token_ttl(access_token, expiry)
        return AuthenticatedClient(
            client=client,
            access_token=access_token,
            user_id=user_id,
            ttl=ttl,
        )
    except BaseException:
        await client.aclose()
        raise


def parse_courses(payload: Any) -> list[CourseRef]:
    """Courses for the assignment sweep, capped like Rust's ``take``."""
    courses: list[CourseRef] = []
    for record in collect_records(payload):
        if not isinstance(record, dict):
            continue
        course_id = text(_first_present(record, ("id", "siteId", "courseId")))
        if not course_id.strip():
            continue
        name = text(
            _first_present(record, ("siteName", "courseName", "siteTitle", "name"))
        )
        courses.append(CourseRef(id=course_id, name=name or None))
        if len(courses) >= MAX_COURSES:
            break
    return courses


def merge_items(
    items: list[AssignmentDeadlineItem],
) -> list[AssignmentDeadlineItem]:
    """Rust's ``BTreeMap`` merge keyed by ``id\\u{1f}deadline``.

    A later copy replaces an earlier one only when the earlier entry has no
    course name and the later one does; the merged list is then sorted.
    """
    merged: dict[str, AssignmentDeadlineItem] = {}
    for item in items[:MAX_ASSIGNMENTS]:
        key = f"{item.id}\x1f{item.deadline}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
        elif existing.course_name is None and item.course_name is not None:
            merged[key] = item
    result = [merged[key] for key in sorted(merged)]
    sort_items(result)
    return result


async def fetch_all_assignments(
    account: str, password: str
) -> list[AssignmentDeadlineItem]:
    """Full catalogue for the current credential revision."""
    return await fetch_all_assignments_at(account, password, credential_revision())


async def fetch_all_assignments_at(
    account: str, password: str, revision: int
) -> list[AssignmentDeadlineItem]:
    """Full catalogue pinned to ``revision``, reusing the cached CAS session."""
    ASSIGNMENT_CACHE.ensure_revision(revision)

    async def login() -> tuple[AuthenticatedClient, float]:
        ASSIGNMENT_CACHE.ensure_revision(revision)
        authenticated = await authenticate(account, password)
        ASSIGNMENT_CACHE.ensure_revision(revision)
        return authenticated, authenticated.ttl

    async def request(
        authenticated: AuthenticatedClient,
    ) -> list[AssignmentDeadlineItem]:
        ASSIGNMENT_CACHE.ensure_revision(revision)
        items = await fetch_all_with_session(authenticated)
        ASSIGNMENT_CACHE.ensure_revision(revision)
        return items

    return await ASSIGNMENT_SESSION.run(account, password, login, request)


async def fetch_all_with_session(
    authenticated: AuthenticatedClient,
) -> list[AssignmentDeadlineItem]:
    courses_payload = await authenticated.get(
        "/ykt-site/site/list/student/current",
        [
            ("size", str(COURSE_PAGE_SIZE)),
            ("current", "1"),
            ("userId", authenticated.user_id),
            ("siteRoleCode", "2"),
        ],
    )
    courses = parse_courses(courses_payload)
    validate_record_collection(courses_payload)
    # A failed course page aborts the sweep: never publish or cache an
    # incomplete catalogue as an empty (or complete) success, so the caller
    # keeps its previous snapshot.
    all_items: list[AssignmentDeadlineItem] = []
    for course in courses:
        payload = await authenticated.post_json(
            "/ykt-site/work/student/list",
            {
                "siteId": course.id,
                "userId": authenticated.user_id,
                "keyword": "",
                "chapterId": "",
                "nodeId": "",
                "current": 1,
                "size": ASSIGNMENT_PAGE_SIZE,
                "studentAssignmentStatus": "",
                "status": "",
                "sortColumn": "",
                "sortType": "",
            },
        )
        validate_record_collection(payload)
        all_items.extend(parse_all_assignment_deadlines(payload, course.name))
        if len(all_items) >= MAX_ASSIGNMENTS:
            break

    # The homepage list is merged after course lists: it covers pending
    # assignments that UCloud occasionally omits from a course page.
    try:
        undone_payload = await authenticated.get(
            "/ykt-site/site/student/undone", [("userId", authenticated.user_id)]
        )
    except ServiceError as error:
        if error.authentication_expired:
            raise
    else:
        all_items.extend(parse_all_assignment_deadlines(undone_payload, None))
    return merge_items(all_items)


async def fetch_assignment_list(
    account: str,
    password: str,
    account_scope: str,
    request_revision: int,
    force: bool,
) -> list[AssignmentDeadlineItem]:
    """Full catalogue shared by the day view and the calendar, cached by scope.

    An explicit refresh invalidates results, not the authentication session.
    """
    async with ASSIGNMENT_FETCH:
        ASSIGNMENT_CACHE.ensure_revision(request_revision)
        if not force:
            cached = ASSIGNMENT_CACHE.items(account_scope, request_revision)
            if cached is not None:
                return cached
        items = await fetch_all_assignments_at(account, password, request_revision)
        ASSIGNMENT_CACHE.save(account_scope, items, request_revision)
        return items


async def fetch_assignments(
    payload: AssignmentsRequest,
    account: str,
    password: str,
    account_scope: str,
    request_revision: int,
) -> AssignmentsResponse:
    """Capture ``request_revision`` with the credentials and never refresh it."""
    requested = parse_date(payload.date.strip()).isoformat()
    all_items = await fetch_assignment_list(
        account, password, account_scope, request_revision, False
    )
    items = [item for item in all_items if item.deadline[:10] == requested]
    ASSIGNMENT_CACHE.ensure_revision(request_revision)
    return AssignmentsResponse(
        date=requested,
        source=SOURCE_URL,
        items=items,
        unavailable_reason=None,
    )


async def fetch_assignment_calendar(
    payload: CalendarRangeRequest,
    account: str,
    password: str,
    account_scope: str,
    request_revision: int,
) -> AssignmentCalendarResponse:
    """Calendar slice, using the same credential snapshot contract."""
    start = parse_date(payload.start_date.strip())
    end = parse_date(payload.end_date.strip())
    day_count = (end - start).days + 1
    if not 1 <= day_count <= MAX_CALENDAR_RANGE_DAYS:
        raise ServiceError.with_status("作业日历查询范围必须在 1 至 370 天内。", 400)

    all_items = await fetch_assignment_list(
        account, password, account_scope, request_revision, False
    )
    items = assignment_items_in_range(all_items, start, end)
    ASSIGNMENT_CACHE.ensure_revision(request_revision)
    return AssignmentCalendarResponse(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        source=SOURCE_URL,
        items=items,
    )


