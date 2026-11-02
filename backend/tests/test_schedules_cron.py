"""Cron expressions of schedules (docs/api/schedules.md, "cron 的写法") and the
scheduler's loop on the memory store: due schedules start runs and move on, an
unfinished last run skips this one, a failure is recorded."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aep.schedules import Cron, CronError, MemoryScheduleStore, Schedule, Scheduler, zone

SHANGHAI = zone("Asia/Shanghai")
# Monday 2 November 2026, 20:00 in Shanghai.
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def local(cron: str, tz: str = "Asia/Shanghai", count: int = 3, after: datetime = NOW):
    z = zone(tz)
    return [
        d.astimezone(z).strftime("%a %Y-%m-%d %H:%M")
        for d in Cron.parse(cron).upcoming(after, z, count)
    ]


def test_the_fields_and_what_comes_next():
    assert local("0 8 * * 1-5") == [
        "Tue 2026-11-03 08:00",
        "Wed 2026-11-04 08:00",
        "Thu 2026-11-05 08:00",
    ]
    assert local("*/20 * * * *") == [
        "Mon 2026-11-02 20:20",
        "Mon 2026-11-02 20:40",
        "Mon 2026-11-02 21:00",
    ]
    assert local("0 20 * * *", count=1) == ["Tue 2026-11-03 20:00"]  # not this minute
    assert local("15,45 9-10 * * *", count=4) == [
        "Tue 2026-11-03 09:15",
        "Tue 2026-11-03 09:45",
        "Tue 2026-11-03 10:15",
        "Tue 2026-11-03 10:45",
    ]
    assert local("0 9 * * 0") == local("0 9 * * 7")  # both are Sunday
    assert local("30 6 1-7/3 * *") == [
        "Wed 2026-11-04 06:30",
        "Sat 2026-11-07 06:30",
        "Tue 2026-12-01 06:30",
    ]
    assert local("0 3 5/10 * *", count=2) == ["Thu 2026-11-05 03:00", "Sun 2026-11-15 03:00"]


def test_both_days_restricted_means_either_one_and_months_without_the_day_are_skipped():
    assert local("0 9 1 * 1") == [
        "Mon 2026-11-09 09:00",
        "Mon 2026-11-16 09:00",
        "Mon 2026-11-23 09:00",
    ]
    assert local("0 0 31 * *") == [
        "Thu 2026-12-31 00:00",
        "Sun 2027-01-31 00:00",
        "Wed 2027-03-31 00:00",
    ]
    assert local("0 0 29 2 *", count=2) == ["Tue 2028-02-29 00:00", "Sun 2032-02-29 00:00"]
    assert local("0 0 1 1 *", count=1) == ["Fri 2027-01-01 00:00"]


def test_clock_changes():
    # 14 March 2027 02:30 never happens in New York; 1 November 2026 01:30 happens twice.
    assert local("30 2 * * *", "America/New_York", 2, datetime(2027, 3, 13, 12, tzinfo=UTC)) == [
        "Mon 2027-03-15 02:30",
        "Tue 2027-03-16 02:30",
    ]
    once = Cron.parse("30 1 * * *").upcoming(
        datetime(2026, 10, 31, 12, tzinfo=UTC), zone("America/New_York"), 2
    )
    assert [d.isoformat() for d in once] == [
        "2026-11-01T05:30:00+00:00",
        "2026-11-02T06:30:00+00:00",
    ]


@pytest.mark.parametrize(
    ("cron", "said"),
    [
        ("0 8 * *", "要写 5 段（分 时 日 月 周），用空格分开"),
        ("0 24 * * *", "第 2 段（时）的 24 超出 0–23"),
        ("60 * * * *", "第 1 段（分）的 60 超出 0–59"),
        ("0 0 0 * *", "第 3 段（日）的 0 超出 1–31"),
        ("0 0 * 13 *", "第 4 段（月）的 13 超出 1–12"),
        ("0 0 * * 8", "第 5 段（周）的 8 超出 0–7"),
        ("0 0 * * MON", "第 5 段（周）的「MON」看不懂，只能写数字、*、-、/ 和逗号"),
        ("0 0 ? * *", "第 3 段（日）的「?」看不懂，只能写数字、*、-、/ 和逗号"),
        ("5-1 * * * *", "第 1 段（分）的范围 5-1 从大到小"),
        ("*/0 * * * *", "第 1 段（分）的步长「0」要是正整数"),
    ],
)
def test_what_cannot_be_read_says_where(cron, said):
    with pytest.raises(CronError) as failed:
        Cron.parse(cron)
    assert str(failed.value) == f"定时写法不对：{said}"


def test_time_zones():
    assert zone("UTC").key == "UTC"
    with pytest.raises(CronError) as failed:
        zone("Mars/Olympus")
    assert str(failed.value) == "时区 Mars/Olympus 不认识"


def schedule(**changes) -> Schedule:
    values = dict(
        schedule_id="sch_1",
        app="report",
        title="每天早上的日报",
        cron="0 8 * * *",
        timezone="Asia/Shanghai",
        input={"question": "日报"},
        project=None,
        enabled=True,
        created_by="张三",
        next_run_at=NOW - timedelta(minutes=1),
    )
    return Schedule(**(values | changes))


class Runs:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.status: dict[str, str] = {}
        self.fail: str | None = None

    async def start(self, s: Schedule):
        if self.fail:
            raise ValueError(self.fail)
        run_id = f"run-{len(self.started) + 1}"
        self.started.append(s.schedule_id)
        self.status[run_id] = "running"
        return SimpleNamespace(run_id=run_id, status="running")

    async def status_of(self, run_id: str) -> str | None:
        return self.status.get(run_id)


async def test_due_schedules_start_runs_and_move_on():
    store, runs = MemoryScheduleStore(), Runs()
    await store.put(schedule())
    await store.put(schedule(schedule_id="sch_off", enabled=False))
    await store.put(schedule(schedule_id="sch_later", next_run_at=NOW + timedelta(days=30)))
    clock = [NOW]
    scheduler = Scheduler(store, runs.start, runs.status_of, clock=lambda: clock[0])

    assert await scheduler.tick() == 1
    assert runs.started == ["sch_1"]
    done = await store.get("sch_1")
    assert done is not None
    assert (done.last_run_id, done.last_run_at, done.last_error) == ("run-1", NOW, None)
    # 08:00 tomorrow in Shanghai.
    assert done.next_run_at == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert await scheduler.tick() == 0  # not due again yet

    # Due again while the last run still waits for a person: skipped, and said so.
    clock[0] = datetime(2026, 11, 3, 0, 0, 20, tzinfo=UTC)
    runs.status["run-1"] = "waiting"
    assert await scheduler.tick() == 0
    skipped = await store.get("sch_1")
    assert skipped is not None and skipped.last_run_id == "run-1"
    assert skipped.last_error == "上一次运行还没结束，这次跳过（2026-11-03 08:00）"
    assert skipped.next_run_at == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)

    # Once it is done the next one starts; a failure to start is recorded, not raised.
    runs.status["run-1"] = "done"
    clock[0] = datetime(2026, 11, 4, 0, 0, 5, tzinfo=UTC)
    assert await scheduler.tick() == 1
    assert (await store.get("sch_1")).last_error is None  # type: ignore[union-attr]
    runs.status["run-2"] = "error"
    runs.fail = "应用 report 没有版本 3"
    clock[0] = datetime(2026, 11, 5, 0, 0, 5, tzinfo=UTC)
    assert await scheduler.tick() == 0
    failed = await store.get("sch_1")
    assert failed is not None and failed.last_run_id == "run-2"
    assert failed.last_error == "2026-11-05 08:00 没有发起运行：应用 report 没有版本 3"
    assert failed.enabled and failed.next_run_at == datetime(2026, 11, 6, 0, 0, tzinfo=UTC)


async def test_run_now_ignores_the_last_run_and_keeps_the_next_time():
    store, runs = MemoryScheduleStore(), Runs()
    later = NOW + timedelta(hours=3)
    await store.put(schedule(next_run_at=later, last_run_id="run-0"))
    runs.status["run-0"] = "waiting"
    scheduler = Scheduler(store, runs.start, runs.status_of, clock=lambda: NOW)
    record = await scheduler.start_now(await store.get("sch_1"))  # type: ignore[arg-type]
    assert record.run_id == "run-1"
    found = await store.get("sch_1")
    assert found is not None
    assert (found.last_run_id, found.next_run_at) == ("run-1", later)
