"""Unit tests for the Python port of ``src-tauri/src/assignments.rs``.

Everything runs offline: the pure parsers are exercised directly and the
asynchronous fetch layer is driven through stub clients, so no request ever
leaves the process.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wts_core import assignments  # noqa: E402
from wts_core.assignments import (  # noqa: E402
    AssignmentCache,
    AssignmentDeadlineItem,
    AssignmentsRequest,
    CalendarRangeRequest,
    CourseRef,
    assignment_items_in_range,
    assignment_status,
    is_assignment_record,
    merge_items,
    normalized_deadline,
    parse_all_assignment_deadlines,
    parse_assignment_deadlines,
    parse_courses,
    parse_date,
    parse_execution,
    sort_items,
    validate_record_collection,
)
from wts_core.errors import ServiceError  # noqa: E402

COURSE_PAYLOAD = {"data": {"records": [{"siteId": "1", "siteName": "示例课程"}]}}


def item(
    identifier: str, deadline: str, course: str | None = None
) -> AssignmentDeadlineItem:
    return AssignmentDeadlineItem(
        id=identifier,
        title=f"作业{identifier}",
        course_name=course,
        deadline=deadline,
        status=None,
    )


def record(identifier: str, deadline: str, *, kind: int = 3) -> dict:
    return {
        "activityId": identifier,
        "activityName": f"作业{identifier}",
        "type": kind,
        "endTime": deadline,
    }


def course_page(*records: dict) -> dict:
    return {"data": {"records": list(records)}}


class StubClient:
    """Stand-in for ``AuthenticatedClient`` with a scripted response queue."""

    def __init__(
        self, courses_payload, course_pages, undone_payload, *, user_id: str = "u1"
    ) -> None:
        self.user_id = user_id
        self.ttl = 600.0
        self.courses_payload = courses_payload
        self.course_pages = list(course_pages)
        self.undone_payload = undone_payload
        self.gets: list[tuple[str, list[tuple[str, str]]]] = []
        self.posts: list[tuple[str, dict]] = []

    async def get(self, path, query):
        self.gets.append((path, list(query)))
        if path == "/ykt-site/site/list/student/current":
            return self.courses_payload
        if path == "/ykt-site/site/student/undone":
            if isinstance(self.undone_payload, Exception):
                raise self.undone_payload
            return self.undone_payload
        raise AssertionError(f"unexpected path {path}")

    async def post_json(self, path, body):
        self.posts.append((path, body))
        page = self.course_pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


class PureParserTests(unittest.TestCase):
    def test_normalized_deadline_matches_chrono_branches(self) -> None:
        self.assertEqual(
            normalized_deadline("2026-08-22T23:59:00+08:00"),
            (date(2026, 8, 22), "2026-08-22T23:59:00+08:00"),
        )
        self.assertEqual(
            normalized_deadline("2026-08-22T23:59:00Z"),
            (date(2026, 8, 22), "2026-08-22T23:59:00+00:00"),
        )
        self.assertEqual(
            normalized_deadline("2026-08-22 23:59:00"),
            (date(2026, 8, 22), "2026-08-22 23:59:00"),
        )
        self.assertEqual(
            normalized_deadline("2026-08-22 23:59"),
            (date(2026, 8, 22), "2026-08-22 23:59:00"),
        )
        self.assertIsNone(normalized_deadline("2026-08-22T23:59:00"))
        self.assertIsNone(normalized_deadline("not a date"))

    def test_assignment_status_maps_numeric_codes_only(self) -> None:
        self.assertEqual(assignment_status(99), "未提交")
        self.assertEqual(assignment_status(0), "已提交")
        self.assertEqual(assignment_status(1), "已批改")
        self.assertEqual(assignment_status(2), "已驳回")
        self.assertIsNone(assignment_status(7))
        self.assertIsNone(assignment_status(True))
        self.assertEqual(assignment_status(" 未提交 "), "未提交")
        self.assertIsNone(assignment_status("   "))
        self.assertIsNone(assignment_status(None))

    def test_parse_date_requires_an_exact_iso_day(self) -> None:
        self.assertEqual(parse_date("2026-08-22"), date(2026, 8, 22))
        for broken in ("2026-8-22", "2026-08-22x", "", "20260822"):
            with self.assertRaises(ServiceError):
                parse_date(broken)

    def test_validate_record_collection_rejects_malformed_catalogues(self) -> None:
        self.assertIsNone(validate_record_collection({"data": {"records": []}}))
        self.assertIsNone(
            validate_record_collection({"data": {"undoneList": [{"id": "a"}]}})
        )
        for payload in (
            {"code": 200, "data": {}},
            {"data": {"records": None}},
            {"data": {"records": ""}},
        ):
            with self.assertRaises(ServiceError):
                validate_record_collection(payload)

    def test_is_assignment_record_accepts_missing_and_activity_types(self) -> None:
        self.assertTrue(is_assignment_record({"id": "a"}))
        self.assertTrue(is_assignment_record({"type": 3}))
        self.assertTrue(is_assignment_record({"type": "5"}))
        # Rust uses ``is_none_or``: an unparseable type is treated like a
        # missing one instead of being rejected.
        self.assertTrue(is_assignment_record({"type": "exam"}))
        self.assertTrue(is_assignment_record({"type": True}))
        self.assertFalse(is_assignment_record({"type": 1}))

    def test_parse_execution_accepts_attribute_order_and_escapes(self) -> None:
        html = (
            '<div><input value="tok&amp;en" name="execution" /></div>'
            '<input name="username" value="student" />'
        )
        self.assertEqual(parse_execution(html), "tok&en")
        self.assertIsNone(parse_execution("<input name='username' value='x'/>"))


class MergeAndRangeTests(unittest.TestCase):
    def test_merge_items_prefers_the_entry_with_a_course_name(self) -> None:
        merged = merge_items(
            [
                item("a1", "2026-08-22 23:59:00"),
                item("a1", "2026-08-22 23:59:00", "示例课程"),
                item("a1", "2026-08-23 23:59:00", "其他"),
            ]
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].course_name, "示例课程")
        self.assertEqual(merged[1].course_name, "其他")

    def test_merge_items_keeps_the_first_course_name(self) -> None:
        merged = merge_items(
            [
                item("a1", "2026-08-22 23:59:00", "第一"),
                item("a1", "2026-08-22 23:59:00", "第二"),
            ]
        )
        self.assertEqual([entry.course_name for entry in merged], ["第一"])

    def test_merge_items_merges_course_and_homepage_records(self) -> None:
        items = parse_all_assignment_deadlines(
            {"data": {"records": [record("a1", "2026-08-22 23:59:00")]}},
            "示例课程",
        )
        items.extend(
            parse_all_assignment_deadlines(
                {"data": {"undoneList": [record("a1", "2026-08-22 23:59:00", kind=5)]}},
                None,
            )
        )
        merged = merge_items(items)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].course_name, "示例课程")

    def test_sort_items_orders_by_deadline_then_optional_course_name(self) -> None:
        items = [
            item("c", "2026-08-18T23:59:00+08:00"),
            item("a", "2026-08-17T23:59:00+08:00", "乙"),
            item("b", "2026-08-17T23:59:00+08:00"),
        ]
        sort_items(items)
        self.assertEqual([entry.id for entry in items], ["b", "a", "c"])

    def test_calendar_range_keeps_only_assignments_inside_the_visible_dates(
        self,
    ) -> None:
        items = [
            item("before", "2026-08-16T23:59:00+08:00"),
            item("inside", "2026-08-18T23:59:00+08:00"),
            item("after", "2026-08-24T23:59:00+08:00"),
        ]
        filtered = assignment_items_in_range(
            items, date(2026, 8, 17), date(2026, 8, 23)
        )
        self.assertEqual([entry.id for entry in filtered], ["inside"])

    def test_parse_assignment_deadlines_filters_one_day(self) -> None:
        payload = {
            "data": {
                "records": [
                    record("a1", "2026-08-22 23:59:00"),
                    record("a2", "2026-08-23 23:59:00"),
                ]
            }
        }
        parsed = parse_assignment_deadlines(payload, date(2026, 8, 22))
        self.assertEqual([entry.id for entry in parsed], ["a1"])


class ParseCoursesTests(unittest.TestCase):
    def test_parse_courses_prefers_site_fields_and_drops_empty_ids(self) -> None:
        courses = parse_courses(
            {
                "data": {
                    "records": [
                        {"siteId": "1", "siteName": "高等数学"},
                        {"id": "2", "courseName": "大学英语"},
                        {"courseId": "3", "name": "物理"},
                        {"siteName": "缺少编号"},
                    ]
                }
            }
        )
        self.assertEqual(
            courses,
            [
                CourseRef(id="1", name="高等数学"),
                CourseRef(id="2", name="大学英语"),
                CourseRef(id="3", name="物理"),
            ],
        )

    def test_parse_courses_caps_the_sweep(self) -> None:
        records = [
            {"siteId": str(index)} for index in range(assignments.MAX_COURSES + 5)
        ]
        courses = parse_courses({"data": {"records": records}})
        self.assertEqual(len(courses), assignments.MAX_COURSES)


class CacheGuardTests(unittest.TestCase):
    def test_credential_changes_reject_old_reads_and_writes(self) -> None:
        cache = AssignmentCache()
        original = cache.credential_revision()
        cached_items = [item("old", "2026-09-11 12:00:00")]
        cache.save("account-a", cached_items, original)
        self.assertIsNotNone(cache.items("account-a", original))

        cache.clear()
        self.assertGreater(cache.credential_revision(), original)
        with self.assertRaises(ServiceError):
            cache.items("account-a", original)
        with self.assertRaises(ServiceError):
            cache.save("account-a", cached_items, original)

    def test_cache_is_scoped_to_the_account_and_starts_empty(self) -> None:
        cache = AssignmentCache()
        revision = cache.credential_revision()
        self.assertIsNone(cache.items("account-a", revision))
        cache.save("account-a", [item("a", "2026-09-11 12:00:00")], revision)
        self.assertEqual(len(cache.items("account-a", revision) or []), 1)
        self.assertIsNone(cache.items("account-b", revision))


class FetchLayerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        assignments.clear_cache()

    def tearDown(self) -> None:
        assignments.clear_cache()

    async def test_fetch_assignments_reuses_one_session_and_caches_results(
        self,
    ) -> None:
        client = StubClient(
            COURSE_PAYLOAD,
            [course_page(record("a1", "2026-08-22 23:59:00"))],
            {"data": {"undoneList": []}},
        )
        calls: list[tuple[str, str]] = []

        async def authenticate(account, password):
            calls.append((account, password))
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            first = await assignments.fetch_assignments(
                AssignmentsRequest(date="2026-08-22"),
                "user",
                "secret",
                "scope",
                revision,
            )
            second = await assignments.fetch_assignments(
                AssignmentsRequest(date="2026-08-22"),
                "user",
                "secret",
                "scope",
                revision,
            )

        self.assertEqual(first.date, "2026-08-22")
        self.assertEqual(first.source, assignments.SOURCE_URL)
        self.assertIsNone(first.unavailable_reason)
        self.assertEqual([entry.id for entry in first.items], ["a1"])
        self.assertEqual([entry.id for entry in second.items], ["a1"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(
            [path for path, _ in client.gets],
            ["/ykt-site/site/list/student/current", "/ykt-site/site/student/undone"],
        )
        self.assertEqual(
            client.gets[0][1],
            [
                ("size", str(assignments.COURSE_PAGE_SIZE)),
                ("current", "1"),
                ("userId", "u1"),
                ("siteRoleCode", "2"),
            ],
        )
        self.assertEqual(client.posts[0][0], "/ykt-site/work/student/list")
        self.assertEqual(client.posts[0][1]["siteId"], "1")
        self.assertEqual(client.posts[0][1]["size"], assignments.ASSIGNMENT_PAGE_SIZE)

    async def test_force_refresh_reruns_the_sweep_without_logging_in_again(
        self,
    ) -> None:
        client = StubClient(
            COURSE_PAYLOAD,
            [
                course_page(record("a1", "2026-08-22 23:59:00")),
                course_page(record("a2", "2026-08-22 23:59:00")),
            ],
            {"data": {"undoneList": []}},
        )
        calls: list[tuple[str, str]] = []

        async def authenticate(account, password):
            calls.append((account, password))
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            first = await assignments.fetch_assignment_list(
                "user", "secret", "scope", revision, False
            )
            second = await assignments.fetch_assignment_list(
                "user", "secret", "scope", revision, True
            )

        self.assertEqual([entry.id for entry in first], ["a1"])
        self.assertEqual([entry.id for entry in second], ["a2"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(client.posts), 2)

    async def test_stale_revision_is_rejected_before_any_request(self) -> None:
        stale = assignments.credential_revision()
        assignments.clear_cache()
        with self.assertRaises(ServiceError):
            await assignments.fetch_assignment_list(
                "user", "secret", "scope", stale, False
            )

    async def test_failed_course_page_never_caches_a_partial_catalogue(self) -> None:
        client = StubClient(
            COURSE_PAYLOAD,
            [
                course_page(record("a1", "2026-08-22 23:59:00")),
                ServiceError("无法连接教学云数据接口。"),
            ],
            {"data": {"undoneList": []}},
        )
        calls: list[tuple[str, str]] = []

        async def authenticate(account, password):
            calls.append((account, password))
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            await assignments.fetch_assignment_list(
                "user", "secret", "scope", revision, False
            )
            with self.assertRaises(ServiceError):
                await assignments.fetch_assignment_list(
                    "user", "secret", "scope", revision, True
                )
            snapshot = assignments.ASSIGNMENT_CACHE.items("scope", revision)

        self.assertEqual([entry.id for entry in snapshot or []], ["a1"])
        self.assertEqual(len(calls), 1)

    async def test_homepage_failure_is_swallowed_unless_the_session_expired(
        self,
    ) -> None:
        client = StubClient(
            COURSE_PAYLOAD,
            [course_page(record("a1", "2026-08-22 23:59:00"))],
            ServiceError("教学云数据接口返回 HTTP 502。"),
        )
        calls: list[tuple[str, str]] = []

        async def authenticate(account, password):
            calls.append((account, password))
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            response = await assignments.fetch_assignments(
                AssignmentsRequest(date="2026-08-22"),
                "user",
                "secret",
                "scope",
                revision,
            )

        self.assertEqual([entry.id for entry in response.items], ["a1"])
        self.assertEqual(len(calls), 1)

    async def test_expired_session_is_relogged_once_then_reported(self) -> None:
        client = StubClient(
            COURSE_PAYLOAD,
            [
                course_page(record("a1", "2026-08-22 23:59:00")),
                course_page(record("a1", "2026-08-22 23:59:00")),
            ],
            ServiceError.expired(),
        )
        calls: list[tuple[str, str]] = []

        async def authenticate(account, password):
            calls.append((account, password))
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            with self.assertRaises(ServiceError) as raised:
                await assignments.fetch_assignments(
                    AssignmentsRequest(date="2026-08-22"),
                    "user",
                    "secret",
                    "scope",
                    revision,
                )

        self.assertTrue(raised.exception.authentication_expired)
        self.assertEqual(len(calls), 2)

    async def test_calendar_rejects_ranges_outside_one_to_370_days(self) -> None:
        revision = assignments.credential_revision()
        for start, end in (("2026-08-23", "2026-08-17"), ("2026-01-01", "2027-06-01")):
            with self.assertRaises(ServiceError) as raised:
                await assignments.fetch_assignment_calendar(
                    CalendarRangeRequest(start_date=start, end_date=end),
                    "user",
                    "secret",
                    "scope",
                    revision,
                )
            self.assertFalse(raised.exception.authentication_expired)

    async def test_calendar_returns_only_items_inside_the_requested_days(self) -> None:
        client = StubClient(
            COURSE_PAYLOAD,
            [
                course_page(
                    record("before", "2026-08-16 23:59:00"),
                    record("inside", "2026-08-18 23:59:00"),
                    record("after", "2026-08-24 23:59:00"),
                )
            ],
            {"data": {"undoneList": []}},
        )
        calls: list[tuple[str, str]] = []

        async def authenticate(account, password):
            calls.append((account, password))
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            response = await assignments.fetch_assignment_calendar(
                CalendarRangeRequest(start_date="2026-08-17", end_date="2026-08-23"),
                "user",
                "secret",
                "scope",
                revision,
            )

        self.assertEqual(response.start_date, "2026-08-17")
        self.assertEqual(response.end_date, "2026-08-23")
        self.assertEqual(response.source, assignments.SOURCE_URL)
        self.assertEqual([entry.id for entry in response.items], ["inside"])
        self.assertEqual(len(calls), 1)

    async def test_calendar_accepts_the_370_day_boundary(self) -> None:
        client = StubClient(
            COURSE_PAYLOAD, [course_page()], {"data": {"undoneList": []}}
        )

        async def authenticate(account, password):
            return client

        with mock.patch.object(assignments, "authenticate", authenticate):
            revision = assignments.credential_revision()
            response = await assignments.fetch_assignment_calendar(
                CalendarRangeRequest(start_date="2026-01-01", end_date="2027-01-05"),
                "user",
                "secret",
                "scope",
                revision,
            )

        self.assertEqual(response.start_date, "2026-01-01")
        self.assertEqual(response.items, [])


if __name__ == "__main__":
    unittest.main()
