# Where To Study 无头核心（Python 版）`where-to-study-core`

By DeepSeek

Original Auther: *Nemoyuzx*

`where-to-study-core` 的异步 Python 实现：北邮空教室、个人课表（含考试）、成绩与考试、
教学云课程作业、班车与重要事件查询，以及节假日与设备本地收藏。所有 HTTP 请求使用
[`httpx.AsyncClient`](https://www.python-httpx.org/async/)，与 Rust/Tauri、CLI、TUI
及 iOS/macOS/Android/HarmonyOS 客户端共用同一份 [v1 数据契约](./contracts/v1/README.md)
与同一套安全语义（固定端点、手动校验重定向、限长读取、凭据不落盘）。

本包只提供服务层：没有界面、命令层、系统通知或日历导出。

## 环境要求

- Python 3.11 或更高版本
- `httpx[brotli] >= 0.27`（异步客户端；brotli 用于解压上游压缩响应）
- 测试仅用标准库 `unittest`，无需额外依赖

## 安装与引入

在仓库内直接使用（推荐，无需安装）：

```python
import sys

sys.path.insert(0, "path/to/where_to_study/wts-py")

import wts_core
from wts_core import classrooms  # 子模块可直接导入
```

也可以作为可编辑包安装：

```bash
cd wts-py
python -m pip install -e .
```

## 快速开始

### 1. 公开查询（不需要账号）

班车、重要事件和节假日都是公开数据，只发不含账号、课表或定位的 HTTPS GET：

```python
import asyncio

from wts_core import holidays, public_queries as pq


async def main() -> None:
    shuttle = await pq.fetch_shuttle_bus()
    today = pq.shuttle_today(shuttle)
    print(today.status, today.next_departure)  # 例：今日安排 6 个发车时刻 · 12 辆车

    events = await pq.fetch_important_events()
    print(len(events.items), events.source, events.used_backup)

    year = await holidays.fetch_holidays(2026)
    print(year.year, len(year.items), year.source)


asyncio.run(main())
```

`fetch_shuttle_bus()` 与 `fetch_important_events()` 命中 5 分钟进程内缓存，需要绕过
缓存时使用 `refresh_shuttle_bus()` / `refresh_important_events()`；
`fetch_holidays()` 的顺序是远端 → 本地缓存 → 内置离线数据。

### 2. 空教室（仅当天）

```python
import asyncio

from wts_core import classrooms
from wts_core.models import ClassroomsRequest


async def main() -> None:
    request = ClassroomsRequest(account="2021xxxxxx", password="教务密码", campus_id="01")
    result = await classrooms.fetch_all_classrooms(request)

    for campus in result.campuses:
        print(campus.campus_name, campus.provider, len(campus.rooms))

    west = result.campuses[0]
    for room in west.rooms:
        # available_slots 是 0 起的节次索引，展示时加 1
        print(room.id, room.size, room.available_slots)


asyncio.run(main())
```

`account` / `password` 可以留空，此时读取环境变量 `BUPT_USERNAME` / `BUPT_PASSWORD`。
实时接口只支持当天：`target_date` 不是上海时区的今天会返回 400。

### 3. 个人课表与考试

```python
import asyncio

from wts_core import academic, schedule
from wts_core.models import ScheduleRequest


async def main() -> None:
    payload = ScheduleRequest(account="2021xxxxxx", password="教务密码")
    snapshot = await schedule.fetch_schedule(payload)

    print(snapshot.term_id, snapshot.term_start_date, len(snapshot.courses))
    for course in snapshot.courses:
        print(course.weekday, course.section_text, course.name, course.room, course.week_text)

    if snapshot.exam_schedule is not None:
        print(snapshot.exam_schedule.status, len(snapshot.exam_schedule.items))

    projected = academic.effective_schedule(snapshot)  # 把有日期的考试投影进课表
    print(len(projected.courses), "含考试的可见课程数")


asyncio.run(main())
```

缺省时按上海当天自动推断学期（`automatic_term_detection_enabled` 默认开启）；
手动模式必须同时给出 `term_id` 与 `term_start_date`（第一周周一）。
课表与考试共用一次登录和一次鉴权重试额度，考试同步失败不会让普通课程不可用。

### 4. 成绩与考试

```python
import asyncio

from wts_core import academic
from wts_core.models import GradeRequest


async def main() -> None:
    terms = await academic.fetch_terms("2021xxxxxx", "教务密码")
    print(terms.current_term_id, [term.id for term in terms.terms])

    # record_type："1" 最好成绩（默认）、"0" 首次成绩、"" 全部记录
    report = await academic.fetch_grades("2021xxxxxx", "教务密码", GradeRequest(record_type="0"))
    print(report.term_id, report.average_grade_point, len(report.items))
    for item in report.items:
        print(item.name, item.score, item.credits, item.semester_name)

    exams = await academic.fetch_exams("2021xxxxxx", "教务密码")  # term=None 表示学校当前学期
    print(exams.status, len(exams.items))


asyncio.run(main())
```

`GradeRequest.term_id=None` 取学校当前学期，显式空字符串 `""` 表示全部学期；
无法确定时间的考试只给出 `date` / `time_text`，不会编造节次或倒计时。

### 5. 教学云课程作业

作业查询需要“本地账号作用域”与“凭据版本号”两个参数（见下文
[凭据、会话与本地数据](#凭据会话与本地数据)）：

```python
import asyncio

from wts_core import assignments, scoped_cache
from wts_core.models import AssignmentsRequest, CalendarRangeRequest


async def main() -> None:
    assignments.clear_cache()  # 切换账号或修改密码后必须调用
    scope = scoped_cache.new_account_scope()
    revision = assignments.credential_revision()

    day = await assignments.fetch_assignments(
        AssignmentsRequest(date="2026-08-22"), "2021xxxxxx", "教学云密码", scope, revision
    )
    print(day.date, day.source, len(day.items))

    month = await assignments.fetch_assignment_calendar(
        CalendarRangeRequest(start_date="2026-08-17", end_date="2026-09-15"),
        "2021xxxxxx",
        "教学云密码",
        scope,
        revision,
    )
    print(month.start_date, month.end_date, len(month.items))


asyncio.run(main())
```

旧版本号的请求在任何读取或写入之前就会被拒绝
（`教学云平台凭据已更改，请重新获取作业。`），因此迟到的旧凭据响应无法覆盖新数据。
日历范围必须在 1 至 370 天内。

### 6. 重要事件收藏与本地课程删除

```python
import asyncio
from datetime import datetime, timezone

from wts_core import course_deletions, public_queries as pq, scoped_cache


async def main() -> None:
    favorites = pq.load_favorite_events()
    live = (await pq.fetch_important_events()).items
    merged = pq.merge_live_and_favorite_events(live, favorites)  # 收藏即使已下线也仍在列表中

    filtered = pq.filter_important_events(
        merged,
        favorites,
        pq.ImportantEventFilter(query="人工智能", source=pq.ImportantEventSourceFilter.PUBLIC),
        datetime.now(timezone.utc),
    )
    print([pq.favorite_key(item) for item in filtered[:5]])

    toggle_result = pq.toggle_favorite_event(favorites, merged[0])  # True=已收藏，False=已取消
    print(toggle_result, len(favorites))


asyncio.run(main())
```

课程删除只改本地显示，不会向学校提交退课；规则每次查询后重新应用：

```python
from wts_core import course_deletions, scoped_cache

scope = scoped_cache.new_account_scope()
rule = course_deletions.CourseDeletion.create(snapshot, "0cb282cebe22", None)  # None=整门课
visible = course_deletions.apply(snapshot, [rule])  # snapshot 本体不被修改
course_deletions.save("course-deletions.json", scope, [rule])

# 只删除某一次排课：给出上海日期
one_meeting = course_deletions.CourseDeletion.create(snapshot, "0cb282cebe22", "2026-09-07")
course_deletions.save("course-deletions.json", scope, [rule, one_meeting])
```


## 模块一览

模块与 Rust `where-to-study-core` 一一对应，`wts_core.` 前缀均可直接 `import`。

| 模块 | 对应 Rust 源 | 职责 | 主要入口 |
| --- | --- | --- | --- |
| `wts_core.config` | `config.rs` | 固定端点、校区、14 节课表、学期推断、上海时间 | `suggested_term_for_date`、`slot_payload`、`campuses_payload`、`is_valid_term_id`、`today_in_app_tz`、`now_in_app_tz` |
| `wts_core.errors` | `error.rs` | 统一错误类型 | `ServiceError`（`.message`、`.authentication_expired`） |
| `wts_core.models` | `models.rs` + `academic.rs` 模型 | v1 snake_case JSON 模型 | `Course`、`ScheduleResponse`、`ClassroomsCacheResponse`、`GradeReport`、`ExamSchedule`、`AssignmentDeadlineItem`、`ShuttleBusResponse`、`ImportantEventItem`、`HolidaysResponse` 等 |
| `wts_core.json_model` | serde derive | 与 serde 对齐的序列化基类 | `JsonModel.to_dict()` / `from_dict()`、`omit_when_none`、`omit_when_empty`、`renames` |
| `wts_core.auth` | `auth.rs` | 凭据解析与兜底 | `resolve_credentials(account, password)` |
| `wts_core.session_cache` | `session_cache.rs` | 进程内凭据作用域会话 | `SessionCache`、`SessionEpoch`、`check_auth_payload`、`session_ttl`、`token_ttl` |
| `wts_core.scoped_cache` | `scoped_cache.rs` | 本地账号作用域与信封 | `new_account_scope`、`encode`、`decode`、`is_valid_account_scope` |
| `wts_core.http` | reqwest 客户端 | 共享传输层 | `public_client`、`send_with_redirects`、`read_limited`、`parse_json`、`reject_redirect` |
| `wts_core.credential_store` | `credential_store.rs` | 终端客户端的凭据载荷 | `Credentials`、`assignment_password()` |
| `wts_core.classrooms` | `classrooms.rs` | 移动教务登录与当天空教室 | `fetch_all_classrooms`、`fetch_all_classrooms_at`、`login_empty_classroom`、`parse_available_classrooms`、`normalize_building_name`、`clear_session`、`session_epoch` |
| `wts_core.schedule` | `schedule.rs` | 个人课表解析与获取 | `fetch_schedule`、`fetch_schedule_at`、`parse_sjd_courses`、`expand_week_numbers`、`infer_term_start_date`、`resolve_schedule_term` |
| `wts_core.academic` | `academic.rs` | 学期、成绩、考试、日历投影 | `fetch_terms`、`fetch_grades`、`fetch_exams`、`parse_grades`、`parse_exams`、`effective_schedule`、`merge_exam_fallback`、`busy_slots`、`occurs_on`、`course_minutes` |
| `wts_core.assignments` | `assignments.rs` | 教学云 CAS 登录与作业 | `fetch_assignments`、`fetch_assignment_list`、`fetch_assignment_calendar`、`credential_revision`、`ensure_credential_revision`、`clear_cache`、`parse_assignment_deadlines` |
| `wts_core.holidays` | `holidays.rs` | 法定节假日与调休 | `fetch_holidays`、`fetch_remote`、`decode_source`、`load_cache_from_path`、`save_cache_to_path`、`offline_response`、`cache_path_for` |
| `wts_core.public_queries` | `public_queries.rs` | 班车、重要事件、本地收藏 | `fetch_shuttle_bus`、`shuttle_today`/`shuttle_at`、`fetch_important_events`、`filter_important_events`、`merge_live_and_favorite_events`、`load_favorite_events`、`toggle_favorite_event` |
| `wts_core.course_deletions` | `course_deletions.rs` | 本地课程删除规则 | `CourseDeletion.create`、`apply`、`load`、`save` |

`wts_core.__init__` 只导出最常用的常量、配置助手、`ServiceError` 与核心模型；
其余按需从子模块导入。

## 数据契约

- 字段名就是 v1 的 snake_case JSON 键；`JsonModel` 用两个类属性对齐 serde：
  `omit_when_none`（对应 `skip_serializing_if = "Option::is_none"`）与
  `omit_when_empty`（对应 `skip_serializing_if = "String::is_empty"`），
  `renames` 处理 `type` / `from` 这类关键字字段。
- 约定与 [`contracts/v1`](./contracts/v1/README.md) 一致：节次索引 0 起、`weekday`
  1–7、日期 `YYYY-MM-DD`、`fetched_at` 为不带小数秒的 RFC 3339、
  `exam_week_numbers` 恒为空数组、未知座位数用 `null` 而非 `0`。
- 解析函数只做上游 → 契约的规范化，可以直接用 `contracts/v1/fixtures/` 交叉验证，
  例如 `schedule.parse_sjd_courses` 输入 `sjd-curriculum.json` 得到 `schedule.json`，
  `classrooms.parse_available_classrooms` 输入两个 `sjd-classrooms-*.json` 得到
  `classrooms.json`，`holidays.decode_source` 输入 `holiday-source.json` 得到
  `holidays.json`。

## 凭据、会话与本地数据

- **凭据来源**：显式参数优先，其次环境变量 `BUPT_USERNAME` / `BUPT_PASSWORD`；
  两者都缺时抛 400 `ServiceError`（`wts_core.auth.resolve_credentials`）。
- **教务会话**（`classrooms.SJD_SESSION`）：进程内、以凭据 SHA-256 摘要为键的单槽缓存；
  只对登录路径加锁；识别到鉴权过期只重登一次；`clear_session()` 之后，带着旧 epoch 的
  请求会立即失败（`账户已更改，请重新获取。`）。
- **教学云会话与缓存**（`assignments`）：同样进程内，缓存键包含账号作用域与凭据版本，
  结果复用上限 10 分钟；令牌、票据与 Cookie 只存在于内存。
- **账号作用域**（`scoped_cache`）：32 字节随机十六进制加 `opaque-v1:` 前缀，
  不含账号名；本地文件用它做信封，旧的无作用域文件会被视为“无数据”。
- **设备本地文件**：
  - 收藏：`public_queries.favorite_events_path()` —— Windows
    `%LOCALAPPDATA%\where-to-study\favorite-events.json`（无 `LOCALAPPDATA` 时退回
    `%APPDATA%`）、macOS `~/Library/Application Support/where-to-study/`、
    其他平台 `${XDG_CONFIG_HOME:-~/.config}/where-to-study/`；写入时目录 `0700`、
    文件 `0600`（POSIX），拒绝符号链接。
  - 节假日缓存：`holidays.cache_path_for(year)`，可用 `fetch_holidays(2026, directory=...)`
    指定目录（默认同上，文件名为 `holidays_<year>.json`）。
  - 课程删除记录：路径由调用方提供（终端客户端放在各自凭据目录）。
- 上述文件都不含账号或密码；成绩、作业、令牌、票据与 Cookie 一律不落盘。


## 网络与安全语义

- **端点全部内置**：不接受调用方传入 URL；公开查询的地址还要通过
  `public_queries.fixed_url(value, host)` 校验（HTTPS、主机匹配、无用户信息、无片段）。
- **重定向**：所有客户端都以 `follow_redirects=False` 创建，由代码按跳校验。
  移动教务只允许同源 HTTPS 且最多 10 跳；节假日数据源只允许 `unpkg.com`；
  公开查询与教学云接口直接拒绝任何重定向。
- **响应限长**（按块读取，超出即报错）：统一认证登录页 1 MiB、教学云令牌 512 KiB、
  教学云接口 8 MiB、班车 512 KiB、重要事件 2 MiB、节假日 256 KiB、收藏文件 2 MiB、
  课程删除记录 1 MiB。
- **教学云登录**：只提交一次凭据，`execution` 取自登录页表单；票据必须落在
  `ucloud.bupt.edu.cn` 且非空，否则返回 401；接口域名必须是
  `apiucloud.bupt.edu.cn`。
- **公开查询**：请求只带 `Accept` / `User-Agent`，不含账号、课表、教室或定位。

## 公开数据源

| 数据 | 地址 |
| --- | --- |
| 校区班车 | `https://where-to-study.cn/api/shuttle-bus` |
| 学科竞赛等 DDL（主源） | `https://nemoyuzx.github.io/contest-ddl/data/competitions.json` |
| 公开活动（主源不可用时的固定备用） | `https://where-to-study.cn/api/contest-events` |
| 校内竞赛通知 | `https://where-to-study.cn/api/contest-notices` |
| 法定节假日与调休 | `https://unpkg.com/holiday-calendar@1.3.3/data/CN`（离线兜底为国家法定安排） |
| 移动教务 / 统一认证 / 教学云 | `jwglweixin.bupt.edu.cn`、`auth.bupt.edu.cn`、`ucloud.bupt.edu.cn`、`apiucloud.bupt.edu.cn` |

页面与接口数据仅供参考，请以学校和活动主办方通知为准。详细的隐私说明见
[隐私政策](./PRIVACY.md)。

## 范围之外

以下能力在 Rust 侧属于 `src-tauri` 应用层（`daily_info.rs`、`deadlines.rs`、
`calendar_export.rs`、`course_reminders.rs`、`desktop_notifications*.rs`、
`settings_store.rs`、`classrooms_store.rs`、`schedule_store.rs`、`lib.rs` 命令），
不在 `where-to-study-core` 内，因此本包也没有包含：

- 天气、黄历、自定义日程接口（custom deadline feed）与截止日期聚合
- 桌面通知、课前提醒、系统日历导出
- 各类本地 store（课表/空教室/设置）、Tauri 命令与前端
- `holidays` 的“远端 → 本地缓存 → 内置离线数据”编排在 Rust 由 Tauri 命令完成，
  本包把它放进了 `holidays.fetch_holidays`

## 测试

```bash
cd wts-py
python -m unittest discover -s tests -v
```

25 个用例全部离线运行（网络层用 stub 客户端与 `mock.patch` 替换），覆盖：

- 契约对齐：`sjd-curriculum.json` → `schedule.json`、两个
  `sjd-classrooms-*.json` → `classrooms.json`、`holiday-source.json` → `holidays.json`
- 解析器：周次展开、节次解码、考试成绩时间解析、作业截止时间与状态、`execution` 解析
- 纯逻辑：合并去重、排序、日历区间、课程数量上限、教学楼与教室号规范化
- 安全语义：畸形目录不被当成“成功空数据”、凭据版本在读写前校验、
  会话复用与“仅一次”有界重登、课程页失败不缓存残缺目录、日历 1–370 天守卫

## 已知差异

- `assignments.text()` 会对字符串做 `strip()`；Rust 的 `assignments.rs::text` 保留原样
  并额外支持 `as_u64`。仅影响上游字段带首尾空白时的字面值，不影响日期过滤等逻辑。
- 带小数秒的时间戳：Rust `to_rfc3339()` 输出 `.123`，Python `isoformat()` 输出
  `.123000`。
- Python 字符串不能像 Rust 的 `Zeroizing` 那样在丢弃时清零；本包改为立即丢弃引用，
  并且不把凭据写入磁盘。
- `assignments.fetch_all_assignments` 在 Rust 中是 `#[cfg(test)]` 辅助函数，
  本包保留同名薄封装以便对齐。

## 许可证

本项目按 [GNU General Public License v3.0 only](../LICENSE)（SPDX：
`GPL-3.0-only`）开源发布。第三方材料的条款见
[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) 与
[`THIRD_PARTY_LICENSES.html`](./THIRD_PARTY_LICENSES.html)。

