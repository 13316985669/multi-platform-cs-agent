"""调度对账与进程生命周期。不打开 AdsPower，也不启动 run_assigned.py。"""
import os

import pytest
import subprocess
import sys
import time
from pathlib import Path

from run_fleet import (BACKOFF_SECONDS, START_GAP_SECONDS, FleetSupervisor, TrackedChild,
                       acquire_fleet_lock, foreign_worker_ids, reconcile)

SLEEPER = (
    "import os, pathlib, time\n"
    "root = pathlib.Path(os.environ['FLEET_TEST_DIR'])\n"
    "name = os.environ['WORKER_ID']\n"
    "(root / (name + '.txt')).write_text(name + '\\n' + os.environ['STATE_DB'], encoding='utf-8')\n"
    "time.sleep(120)\n"
)


def _wait_text(path: Path) -> str:
    deadline = time.time() + 5
    while time.time() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    raise AssertionError(f"子进程没有写到 {path}")


def _binding(worker_id, profile_id="p"):
    return {
        "worker_id": worker_id, "binding_id": 1, "driver": "douyin_feige",
        "adspower_profile_id": profile_id,
    }


def test_reconcile_starts_keeps_stops_and_skips():
    actions, _ = reconcile(
        [_binding("keep-me", "p1"), _binding("start-me", "p2"), _binding("foreign-me", "p3")],
        {
            "keep-me": TrackedChild("keep-me", "p1", running=True),
            "gone": TrackedChild("gone", "p-old", running=True),
        },
        now=100,
        foreign_worker_ids={"foreign-me"},
    )
    by_worker = {item["worker_id"]: item["op"] for item in actions}
    assert by_worker["keep-me"] == "keep"
    assert by_worker["start-me"] == "start"
    assert by_worker["gone"] == "stop"
    assert by_worker["foreign-me"] == "skip"
    stopped = next(item for item in actions if item["op"] == "stop")
    assert stopped["profile_id"] == "p-old"


def test_reconcile_exit_zero_is_not_a_crash():
    actions, backoff = reconcile(
        [_binding("changed")],
        {"changed": TrackedChild("changed", running=False, exit_code=0)},
        now=20,
    )
    assert actions[0]["op"] == "start"
    assert backoff == {}


def test_reconcile_crash_enters_backoff():
    child = {"crashed": TrackedChild("crashed", running=False, exit_code=1)}
    actions, backoff = reconcile([_binding("crashed")], child, now=10)
    assert actions[0]["op"] == "backoff"
    assert backoff["crashed"] == 10 + BACKOFF_SECONDS

    again, still = reconcile([_binding("crashed")], child, now=12, backoff_until=backoff)
    assert again[0]["op"] == "backoff"
    assert still["crashed"] == 10 + BACKOFF_SECONDS

    ready, cleared = reconcile(
        [_binding("crashed")], child, now=10 + BACKOFF_SECONDS, backoff_until=backoff,
    )
    assert ready[0]["op"] == "start"
    assert "crashed" not in cleared


def test_reconcile_fleet_max_queues_extra():
    actions, _ = reconcile(
        [_binding("running"), _binding("waiting")],
        {"running": TrackedChild("running", running=True)},
        now=50,
        fleet_max=1,
        last_start_at=None,
    )
    by_worker = {item["worker_id"]: item for item in actions}
    assert by_worker["running"]["op"] == "keep"
    assert by_worker["waiting"]["op"] == "queue"
    assert by_worker["waiting"]["reason"] == "fleet_max"


def test_reconcile_starts_are_at_least_three_seconds_apart():
    bindings = [_binding("a"), _binding("b")]
    first, _ = reconcile(bindings, {}, now=0)
    assert [item["op"] for item in first] == ["start", "queue"]
    assert first[1]["reason"] == "stagger"

    too_soon, _ = reconcile(bindings, {}, now=START_GAP_SECONDS - 0.1, last_start_at=0.0)
    assert [item["op"] for item in too_soon] == ["queue", "queue"]

    later, _ = reconcile(bindings, {}, now=START_GAP_SECONDS, last_start_at=0)
    assert later[0]["op"] == "start"


def _binding_row(worker_id, profile_id, binding_id):
    return {
        "worker_id": worker_id, "binding_id": binding_id, "driver": "douyin_feige",
        "adspower_profile_id": profile_id,
    }


def test_process_starts_two_children_and_stops_one(tmp_path):
    stopped = []
    supervisor = FleetSupervisor(
        tmp_path, [sys.executable, "-c", SLEEPER],
        profile_stopper=stopped.append,
        extra_env={"FLEET_TEST_DIR": str(tmp_path)},
    )
    try:
        bindings = [_binding_row("w-a", "pa", 1), _binding_row("w-b", "pb", 2)]
        supervisor.tick(bindings, now=0)
        supervisor.tick(bindings, now=START_GAP_SECONDS)
        for name in ("w-a", "w-b"):
            worker_id, state_db = _wait_text(tmp_path / f"{name}.txt").splitlines()
            assert worker_id == name
            assert state_db.replace("\\", "/").endswith(f"worker_state/{name}.db")
        assert set(supervisor.slots) == {"w-a", "w-b"}
        supervisor.tick([bindings[0]], now=START_GAP_SECONDS + 1)
        assert set(supervisor.slots) == {"w-a"}
        assert supervisor.slots["w-a"].proc.poll() is None
        assert "pb" in stopped
    finally:
        supervisor.close()


def test_process_job_close_kills_children(tmp_path):
    supervisor = FleetSupervisor(tmp_path, [sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        supervisor.tick([_binding_row("job-child", "pj", 1)], now=0)
        proc = supervisor.slots["job-child"].proc
        assert proc.poll() is None
        supervisor.release_job()
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        supervisor.close()


def test_process_lock_blocks_a_second_supervisor(tmp_path, monkeypatch):
    dead = tmp_path / "dead"
    dead.mkdir()
    (dead / ".fleet.lock").write_text("424242", encoding="utf-8")
    monkeypatch.setattr("run_fleet._pid_alive", lambda _pid: False)
    acquire_fleet_lock(dead)
    assert (dead / ".fleet.lock").read_text(encoding="utf-8").strip() == str(os.getpid())

    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.undo()
    acquire_fleet_lock(live)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from run_fleet import acquire_fleet_lock\n"
        "try:\n"
        "    acquire_fleet_lock(Path(sys.argv[2]))\n"
        "except SystemExit:\n"
        "    raise SystemExit(2)\n"
        "raise SystemExit(0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(live)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True,
    )
    assert proc.returncode == 2, proc.stderr


@pytest.mark.asyncio
async def test_login_recheck_reads_after_sign_in(tmp_path):
    import asyncio

    from base_worker import BaseWorker, WorkerConfig

    class Driver:
        name = "douyin_feige"

        def __init__(self):
            self.statuses = ["login_expired", "online"]
            self.reads = 0

        async def check_status(self, page):
            return self.statuses.pop(0)

        async def dismiss_blockers(self, page):
            return None

        async def check_platform_bot_disabled(self, page):
            return True

        async def read_self_name(self, page):
            return ""

        async def read_incoming(self, page):
            self.reads += 1
            raise asyncio.CancelledError

    driver = Driver()
    cfg = WorkerConfig(
        rpa_key="k", account="shop", platform="douyin", poll_interval=0,
        state_db=str(tmp_path / "state.db"),
    )
    worker = BaseWorker(cfg, driver, backend=object())
    worker._paused = True
    worker.status = "login_expired"
    with pytest.raises(asyncio.CancelledError):
        await worker._inbound_loop(None)
    assert driver.reads == 1
    assert driver.statuses == []


def test_process_keeps_child_when_bindings_fetch_fails(tmp_path):
    stopped = []
    supervisor = FleetSupervisor(
        tmp_path, [sys.executable, "-c", "import time; time.sleep(120)"],
        profile_stopper=stopped.append,
    )
    try:
        supervisor.tick([_binding_row("keep", "p-keep", 1)], now=0)
        proc = supervisor.slots["keep"].proc
        assert proc.poll() is None
        stopped.clear()
        assert supervisor.apply_bindings(None) == []
        assert proc.poll() is None
        assert "keep" in supervisor.slots
        assert stopped == []
    finally:
        supervisor.close()


def test_process_skips_foreign_live_worker(tmp_path):
    binding = {**_binding_row("busy", "px", 1), "worker_live": True}
    supervisor = FleetSupervisor(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        actions = supervisor.tick([binding], now=0, foreign=foreign_worker_ids([binding], set()))
        assert actions[0]["op"] == "skip"
        assert supervisor.slots == {}
    finally:
        supervisor.close()
