"""一条命令按账号页的启用绑定拉起、重启和停掉 Worker。

对账是纯函数，进程管理在 FleetSupervisor。测试用假子进程，不打开 AdsPower。
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from base_worker import WorkerConfig, _pid_alive

logger = logging.getLogger("rpa_fleet")

START_GAP_SECONDS = 3.0
BACKOFF_SECONDS = 5.0


@dataclass
class TrackedChild:
    worker_id: str
    profile_id: str = ""
    running: bool = True
    exit_code: int | None = None


def reconcile(
    bindings: list[dict],
    children: dict[str, TrackedChild],
    *,
    now: float,
    fleet_max: int = 0,
    foreign_worker_ids: set[str] | None = None,
    last_start_at: float | None = None,
    backoff_until: dict[str, float] | None = None,
) -> tuple[list[dict], dict[str, float]]:
    """决定每个绑定是拉起、保持、停止、跳过、排队还是退避。

    退出码 0 表示绑定变更后的正常退出，不进入退避。非 0 才退避。
    同一次对账最多安排一个启动，保证两次启动至少间隔 START_GAP_SECONDS。
    fleet_max <= 0 表示不限制同时运行的数量。
    """
    foreign = foreign_worker_ids or set()
    backoff = dict(backoff_until or {})
    wanted = [row for row in bindings if (row.get("worker_id") or "").strip()]
    wanted_ids = {row["worker_id"] for row in wanted}
    actions: list[dict] = []

    for worker_id, child in children.items():
        if worker_id not in wanted_ids and child.running:
            actions.append({
                "op": "stop", "worker_id": worker_id, "profile_id": child.profile_id,
            })

    running_count = sum(
        1 for worker_id, child in children.items()
        if child.running and worker_id in wanted_ids
    )
    can_start_now = last_start_at is None or (now - last_start_at) >= START_GAP_SECONDS
    started_this_pass = False

    for row in wanted:
        worker_id = row["worker_id"]
        child = children.get(worker_id)
        profile_id = row.get("adspower_profile_id") or ""
        if child is not None and child.running:
            backoff.pop(worker_id, None)
            actions.append({"op": "keep", "worker_id": worker_id})
            continue
        if child is None and worker_id in foreign:
            actions.append({"op": "skip", "worker_id": worker_id})
            continue
        if child is not None and child.exit_code not in (None, 0):
            if worker_id not in backoff:
                backoff[worker_id] = now + BACKOFF_SECONDS
            if now < backoff[worker_id]:
                actions.append({
                    "op": "backoff", "worker_id": worker_id, "until": backoff[worker_id],
                })
                continue
        else:
            backoff.pop(worker_id, None)

        occupied = running_count + (1 if started_this_pass else 0)
        if fleet_max > 0 and occupied >= fleet_max:
            actions.append({"op": "queue", "worker_id": worker_id, "reason": "fleet_max"})
            continue
        if started_this_pass or not can_start_now:
            actions.append({"op": "queue", "worker_id": worker_id, "reason": "stagger"})
            continue
        backoff.pop(worker_id, None)
        actions.append({
            "op": "start",
            "worker_id": worker_id,
            "binding_id": row.get("binding_id") or 0,
            "profile_id": profile_id,
            "driver": row.get("driver") or "",
        })
        started_this_pass = True

    return actions, backoff


def foreign_worker_ids(bindings: list[dict], spawned: set[str]) -> set[str]:
    """别的进程已经在心跳的绑定，不要再开一个浏览器。"""
    return {
        row["worker_id"]
        for row in bindings
        if row.get("worker_live") and row.get("worker_id") and row["worker_id"] not in spawned
    }


def fleet_worker_id() -> str:
    host = (socket.gethostname() or "local").split(".")[0]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in host)[:40] or "local"
    return f"fleet-{safe}"


def acquire_fleet_lock(directory: Path) -> Path:
    path = directory / ".fleet.lock"
    if path.exists():
        holder = path.read_text(encoding="utf-8").strip()
        if holder.isdigit() and _pid_alive(int(holder)):
            raise SystemExit("已有调度器在运行")
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def _win_job():
    """子进程放进 Job，关闭句柄时一起结束，避免调度器退出后留下孤儿进程。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW 失败")
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(err, "SetInformationJobObject 失败")
    return job


def _assign_job(job, proc: subprocess.Popen) -> None:
    if not job:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = getattr(proc, "_handle", None)
    if not handle or not kernel32.AssignProcessToJobObject(job, handle):
        err = ctypes.get_last_error()
        raise OSError(err, f"AssignProcessToJobObject 失败 pid={proc.pid} err={err}")


@dataclass
class _Slot:
    proc: subprocess.Popen
    profile_id: str


class FleetSupervisor:
    """按对账结果拉起和停掉子进程。command 在测试里换成假命令。"""

    def __init__(self, work_dir: Path, command: list[str], *, fleet_max: int = 0,
                 profile_stopper=None, extra_env: dict | None = None):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.command = list(command)
        self.fleet_max = fleet_max
        self.profile_stopper = profile_stopper
        self.extra_env = dict(extra_env or {})
        self.slots: dict[str, _Slot] = {}
        self.backoff: dict[str, float] = {}
        self.last_start_at: float | None = None
        self._pending_stops: list[str] = []
        self.lock_path = acquire_fleet_lock(self.work_dir)
        self._job = _win_job()

    def tick(self, bindings: list[dict], *, now: float | None = None,
             foreign: set[str] | None = None) -> list[dict]:
        moment = time.time() if now is None else now
        actions, self.backoff = reconcile(
            bindings,
            self._children(),
            now=moment,
            fleet_max=self.fleet_max,
            foreign_worker_ids=foreign if foreign is not None else set(),
            last_start_at=self.last_start_at,
            backoff_until=self.backoff,
        )
        for action in actions:
            if action["op"] == "start":
                self._start(action)
                self.last_start_at = moment
            elif action["op"] == "stop":
                self._stop(action["worker_id"], action.get("profile_id") or "")
        return actions

    def apply_bindings(self, bindings: list[dict] | None, *, now: float | None = None) -> list[str]:
        """bindings 为 None 表示这次没拉到名单，已打开的环境保持不动。"""
        if bindings is None:
            return []
        self.tick(
            bindings,
            now=now,
            foreign=foreign_worker_ids(bindings, set(self.slots)),
        )
        return self.drain_profile_stops()

    def drain_profile_stops(self) -> list[str]:
        pending = self._pending_stops
        self._pending_stops = []
        return pending

    def release_job(self) -> None:
        job, self._job = self._job, None
        if not job:
            return
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)

    def close(self) -> None:
        profiles = [slot.profile_id for slot in self.slots.values() if slot.profile_id]
        self.release_job()
        for slot in self.slots.values():
            try:
                slot.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                slot.proc.kill()
        self.slots.clear()
        for profile_id in profiles:
            self._note_stop(profile_id)
        if self.lock_path.exists():
            try:
                if self.lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    self.lock_path.unlink()
            except OSError:
                logger.warning("释放调度锁失败", exc_info=True)

    def _children(self) -> dict[str, TrackedChild]:
        found = {}
        for worker_id, slot in self.slots.items():
            code = slot.proc.poll()
            found[worker_id] = TrackedChild(
                worker_id, slot.profile_id, running=code is None, exit_code=code,
            )
        return found

    def _start(self, action: dict) -> None:
        worker_id = action["worker_id"]
        env = os.environ.copy()
        env.update(self.extra_env)
        state_dir = self.work_dir / "worker_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        env["WORKER_ID"] = worker_id
        env["STATE_DB"] = str(state_dir / f"{worker_id}.db")
        env["REPORT_PROFILES"] = "0"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            self.command, cwd=str(self.work_dir), env=env, creationflags=flags,
        )
        _assign_job(self._job, proc)
        self.slots[worker_id] = _Slot(proc, action.get("profile_id") or "")
        logger.info("已拉起 %s pid=%s", worker_id, proc.pid)

    def _stop(self, worker_id: str, profile_id: str) -> None:
        slot = self.slots.pop(worker_id, None)
        if slot is not None and slot.proc.poll() is None:
            slot.proc.terminate()
            try:
                slot.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                slot.proc.kill()
        self._note_stop(profile_id or (slot.profile_id if slot else ""))

    def _note_stop(self, profile_id: str) -> None:
        if not profile_id:
            return
        self._pending_stops.append(profile_id)
        if self.profile_stopper is not None:
            self.profile_stopper(profile_id)


def _fleet_max() -> int:
    raw = os.environ.get("FLEET_MAX", "0").strip() or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


async def _supervise(cfg: WorkerConfig, supervisor: FleetSupervisor) -> None:
    import httpx
    from browser_session import AdsPowerClient

    worker_id = cfg.worker_id or fleet_worker_id()
    headers = {"X-Rpa-Key": cfg.rpa_key}
    async with httpx.AsyncClient(
        base_url=cfg.backend_url, headers=headers, timeout=30, trust_env=False,
    ) as client:
        while True:
            profiles: list[dict] = []
            ads_error = ""
            try:
                profiles = await AdsPowerClient(
                    cfg.adspower_api_base, cfg.adspower_api_key,
                ).list_profiles()
            except Exception as exc:  # noqa: BLE001
                ads_error = str(exc)[:200]
                logger.warning("读取 AdsPower 环境列表失败: %s", ads_error)
            try:
                await client.post("/api/rpa/heartbeat", json={
                    "worker_id": worker_id, "account": "", "platform": "fleet",
                    "status": "fleet",
                    "meta": {"browser_profiles": profiles, "ads_error": ads_error},
                })
            except Exception:  # noqa: BLE001
                logger.exception("调度器心跳失败")
            bindings: list[dict] | None = None
            try:
                resp = await client.get("/api/rpa/fleet")
                resp.raise_for_status()
                bindings = list((resp.json() or {}).get("bindings") or [])
            except Exception:  # noqa: BLE001
                logger.exception("拉取绑定失败，保留已打开的环境")
            ads = AdsPowerClient(cfg.adspower_api_base, cfg.adspower_api_key)
            for profile_id in supervisor.apply_bindings(bindings):
                await ads.stop(profile_id)
            await asyncio.sleep(5)


async def _stop_profiles(cfg: WorkerConfig, profile_ids: list[str]) -> None:
    from browser_session import AdsPowerClient

    ads = AdsPowerClient(cfg.adspower_api_base, cfg.adspower_api_key)
    for profile_id in profile_ids:
        await ads.stop(profile_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ["WORKER_ID"] = fleet_worker_id()
    cfg = WorkerConfig.from_env(require_account=False)
    root = Path(__file__).resolve().parent
    supervisor = FleetSupervisor(
        root,
        [sys.executable, str(root / "run_assigned.py")],
        fleet_max=_fleet_max(),
    )
    try:
        asyncio.run(_supervise(cfg, supervisor))
    except KeyboardInterrupt:
        logger.info("调度器已停止")
    finally:
        supervisor.close()
        pending = supervisor.drain_profile_stops()
        if pending:
            asyncio.run(_stop_profiles(cfg, pending))


if __name__ == "__main__":
    main()
