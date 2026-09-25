from wts_core import (
    holidays, public_queries, classrooms, academic, schedule,
    assignments, scoped_cache,
)
from wts_core.models import (
    ClassroomsRequest, ScheduleRequest, GradeRequest,
    AssignmentsRequest, CalendarRangeRequest,
)
from collections import defaultdict


class WTSBridge():

    def __init__(self) -> None:
        self.ACCOUNT: str = ""
        self.PSWD_JW: str = ""
        self.PSWD_JXY: str = ""
        self.CAMPUS_ID: str = ""        # 01=校本部, 04=沙河, 02=宏福, 03=校外

        self.ClassroomReq = None
        self.ScheduleReq = None

        # 用于判断作业模块是否需要清缓存
        self._assignment_account: str | None = None

        self.schedule: dict = {
            "0": {"section": 1, "start_time": "8:00", "end_time": "8:45"},
            "1": {"section": 2, "start_time": "8:50", "end_time": "9:35"},
            "2": {"section": 3, "start_time": "9:50", "end_time": "10:35"},
            "3": {"section": 4, "start_time": "10:40", "end_time": "11:25"},
            "4": {"section": 5, "start_time": "11:30", "end_time": "12:15"},
            "5": {"section": 6, "start_time": "13:00", "end_time": "13:45"},
            "6": {"section": 7, "start_time": "13:50", "end_time": "14:35"},
            "7": {"section": 8, "start_time": "14:45", "end_time": "15:30"},
            "8": {"section": 9, "start_time": "15:40", "end_time": "16:25"},
            "9": {"section": 10, "start_time": "16:35", "end_time": "17:20"},
            "10": {"section": 11, "start_time": "17:25", "end_time": "18:10"},
            "11": {"section": 12, "start_time": "18:30", "end_time": "19:15"},
            "12": {"section": 13, "start_time": "19:20", "end_time": "20:05"},
            "13": {"section": 14, "start_time": "20:10", "end_time": "20:55"},
        }
        self.week: dict = {
            "1": "Mon",
            "2": "Tue",
            "3": "Wen",
            "4": "Thr",
            "5": "Fri",
            "6": "Sat",
            "7": "Sun"
        }

    def _slot2time(self, input: list) -> list:
        opt = []
        for item in input:
            opt.append(self.schedule.get(str(item)))
        return opt

    def _num2week(self, num) -> str:
        return str(self.week.get(str(num)))

    @staticmethod
    def _err(e: Exception) -> dict:
        return {
            "error": type(e).__name__,
            "message": str(e),
        }

    def _assignment_scope_and_revision(self):
        """
        作业模块需要 scope + revision。
        仅在账号发生变化时清除缓存，避免每次都清空导致缓存失效。
        """
        if self._assignment_account != self.ACCOUNT:
            assignments.clear_cache()
            self._assignment_account = self.ACCOUNT
        scope = scoped_cache.new_account_scope()
        revision = assignments.credential_revision()
        return scope, revision

    # ---------------- 公共信息 ----------------

    async def get_shuttle_bus(self):
        try:
            shuttle = await public_queries.fetch_shuttle_bus()
            today = public_queries.shuttle_today(shuttle)
            return {
                "status_today": today.status,
                "next_departure": today.next_departure,
            }
        except Exception as e:
            return self._err(e)

    async def get_important_events(self):
        try:
            events = await public_queries.fetch_important_events()
            return {
                "events_count": len(events.items),
                "events_source": events.source,
                "used_backup": events.used_backup,
            }
        except Exception as e:
            return self._err(e)

    async def get_holidays(self, YYYY: int):          # format: YYYY
        try:
            year = await holidays.fetch_holidays(YYYY)
            return {
                "year": year.year,
                "count": len(year.items),
                "source": year.source,
            }
        except Exception as e:
            return self._err(e)

    # ---------------- 教室 ----------------

    async def get_empty_classroom(self):
        try:
            request = ClassroomsRequest(account=self.ACCOUNT, password=self.PSWD_JW)
            result = await classrooms.fetch_all_classrooms(request)

            opt = {}

            for campus in result.campuses:
                room_dict = {}
                opt[f"{campus.campus_name}"] = {
                    "all_classroom_count": len(campus.rooms),
                }
                for room in campus.rooms:
                    room_dict[f"{room.id}"] = {
                        "seats": room.size,
                        "available_time": self._slot2time(room.available_slots),
                    }

                opt[f"{campus.campus_name}"]["rooms"] = room_dict
            return opt
        except Exception as e:
            return self._err(e)

    # ---------------- 课表 ----------------

    async def get_personal_schedule(self):
        try:
            payload = ScheduleRequest(account=self.ACCOUNT, password=self.PSWD_JW)
            snapshot = await schedule.fetch_schedule(payload)

            opt = {
                "term_id": snapshot.term_id,
                "term_start_date": snapshot.term_start_date,
                "course_type_count": len(snapshot.courses),
            }
            course_dict = defaultdict(lambda: defaultdict(list))
            for course in snapshot.courses:
                day = self._num2week(course.weekday)
                course_dict[day][course.section_text].append({
                    "course_name": course.name,
                    "room": course.room,
                    "duration": f"week: {course.week_text}"
                })
            opt["courses"] = {
                day: dict(sections) for day, sections in course_dict.items()
            }
            if snapshot.exam_schedule is not None:
                opt["exams"] = {
                    "exam_schedule_status": snapshot.exam_schedule.status,
                    "total_exam_coumt": len(snapshot.exam_schedule.items),
                    "items": []
                }
                for exam in snapshot.exam_schedule.items:
                    opt["exams"]["items"].append(exam)
            return opt
        except Exception as e:
            return self._err(e)

    # ---------------- 学期 / 成绩 / 考试 ----------------

    async def get_terms(self):
        try:
            terms = await academic.fetch_terms(self.ACCOUNT, self.PSWD_JW)
            return {
                "curent_term_id": terms.current_term_id,
                "all_terms": [term.id for term in terms.terms],
            }
        except Exception as e:
            return self._err(e)

    async def get_grads(self):
        try:
            # record_type："1" 最好成绩（默认）、"0" 首次成绩、"" 全部记录
            report = await academic.fetch_grades(
                self.ACCOUNT, self.PSWD_JW, GradeRequest(record_type="")
            )
            opt = {
                "term": report.term_id,
                "GPA": report.average_grade_point,
                "exams": []
            }
            for item in report.items:
                opt["exams"].append({
                    "name": item.name,
                    "score": item.score,
                    "credits": item.credits,
                    "semester_name": item.semester_name
                })
            return opt
        except Exception as e:
            return self._err(e)

    async def get_exams(self):
        try:
            exams = await academic.fetch_exams(self.ACCOUNT, self.PSWD_JW)
            return {
                "exam_status": exams.status,
                "exams": [item for item in exams.items],
            }
        except Exception as e:
            return self._err(e)

    # ---------------- 作业 ----------------

    async def get_assignments(self, date: str):
        """
        获取某一天的作业。
        date 格式: "YYYY-MM-DD"
        """
        try:
            scope, revision = self._assignment_scope_and_revision()
            day = await assignments.fetch_assignments(
                AssignmentsRequest(date=date),
                self.ACCOUNT,
                self.PSWD_JXY,
                scope,
                revision,
            )
            return {
                "date": day.date,
                "source": day.source,
                "count": len(day.items),
                "items": list(day.items),
            }
        except Exception as e:
            return self._err(e)

    async def get_assignment_calendar(self, start_date: str, end_date: str):
        """
        获取一段日期范围内的作业日历。
        start_date / end_date 格式: "YYYY-MM-DD"
        """
        try:
            scope, revision = self._assignment_scope_and_revision()
            month = await assignments.fetch_assignment_calendar(
                CalendarRangeRequest(start_date=start_date, end_date=end_date),
                self.ACCOUNT,
                self.PSWD_JXY,
                scope,
                revision,
            )
            return {
                "start_date": month.start_date,
                "end_date": month.end_date,
                "count": len(month.items),
                "items": list(month.items),
            }
        except Exception as e:
            return self._err(e)
