"""AdsPower 客户端只解析本地接口，不真的打开浏览器。"""
import pytest

from base_worker import WorkerConfig
from browser_session import AdsPowerClient, AdsPowerError


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    sent: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, path, **kwargs):
        self.last = (method, path, kwargs)
        _Client.sent.append(kwargs.get("json"))
        return _Resp({
            "code": 0,
            "data": {"ws": {"puppeteer": "ws://127.0.0.1:9/devtools/browser/abc"}},
        })


@pytest.mark.asyncio
async def test_start_returns_cdp(monkeypatch):
    _Client.sent = []
    monkeypatch.setattr("browser_session.httpx.AsyncClient", _Client)
    ws = await AdsPowerClient("http://127.0.0.1:50325", "secret").start("profile-1", headless=False)
    assert ws.endswith("/abc")
    assert _Client.sent[-1]["cdp_mask"] == "0"
    assert _Client.sent[-1]["headless"] == "0"


@pytest.mark.asyncio
async def test_connect_retries_until_debug_port_listens(monkeypatch):
    from browser_session import _connect_when_ready

    async def fast_sleep(_delay):
        return None

    monkeypatch.setattr("browser_session.asyncio.sleep", fast_sleep)

    class PW:
        def __init__(self):
            self.calls = 0
            self.chromium = self

        async def connect_over_cdp(self, ws):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("connect ECONNREFUSED 127.0.0.1:60461")
            return ws

    pw = PW()
    assert await _connect_when_ready(pw, "ws://127.0.0.1:60461/devtools") == "ws://127.0.0.1:60461/devtools"
    assert pw.calls == 3


@pytest.mark.asyncio
async def test_start_rejects_error_code(monkeypatch):
    class Bad(_Client):
        async def request(self, method, path, **kwargs):
            return _Resp({"code": -1, "msg": "环境不存在"})

    monkeypatch.setattr("browser_session.httpx.AsyncClient", Bad)
    with pytest.raises(AdsPowerError):
        await AdsPowerClient("http://127.0.0.1:50325").start("missing")


def test_worker_id_is_stable_and_does_not_collide(tmp_path, monkeypatch):
    from base_worker import allocate_worker_id

    monkeypatch.delenv("WORKER_ID", raising=False)
    monkeypatch.setattr("base_worker._pid_alive", lambda pid: pid == 4242)
    (tmp_path / ".worker_id").write_text("worker-keep", encoding="utf-8")
    (tmp_path / ".worker_id.lock").write_text("4242", encoding="utf-8")
    fresh = allocate_worker_id(tmp_path)
    assert fresh != "worker-keep"
    assert fresh.startswith("worker-")
    assert (tmp_path / ".worker_id.2").read_text(encoding="utf-8").strip() == fresh

    monkeypatch.setattr("base_worker._pid_alive", lambda _pid: False)
    again = allocate_worker_id(tmp_path)
    assert again == "worker-keep"


@pytest.mark.asyncio
async def test_list_profiles_merges_pages_and_stops_on_short_page(monkeypatch):
    pages = {
        1: [{"user_id": f"p{i}", "name": f"环境{i}"} for i in range(100)],
        2: [{"user_id": "tail", "name": "最后"}],
    }
    seen = []

    class Paged(_Client):
        async def request(self, method, path, **kwargs):
            params = kwargs.get("params") or {}
            seen.append(params)
            page = int(params.get("page") or 1)
            return _Resp({"code": 0, "data": {"list": pages.get(page, [])}})

    monkeypatch.setattr("browser_session.httpx.AsyncClient", Paged)
    profiles = await AdsPowerClient("http://127.0.0.1:50325").list_profiles()
    assert [item["id"] for item in profiles[:1]] == ["p0"]
    assert profiles[-1] == {"id": "tail", "name": "最后"}
    assert len(profiles) == 101
    assert [item["page"] for item in seen] == [1, 2]


@pytest.mark.asyncio
async def test_child_worker_does_not_list_profiles(tmp_path, monkeypatch):
    from base_worker import BaseWorker

    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        async def list_profiles(self):
            calls.append("list")
            return [{"id": "p", "name": "环境"}]

    monkeypatch.setattr("browser_session.AdsPowerClient", Client)
    cfg = WorkerConfig(
        rpa_key="k", account="shop", platform="douyin",
        browser_provider="adspower", report_profiles=False,
        state_db=str(tmp_path / "state.db"),
    )
    worker = BaseWorker(cfg, type("Driver", (), {"name": "douyin_feige"})(), backend=object())
    meta = await worker._heartbeat_meta()
    assert "browser_profiles" not in meta
    assert calls == []


def test_comment_platform_maps_to_api_name():
    cfg = WorkerConfig(rpa_key="x", account="shop", platform="xiaohongshu_comment")
    assert cfg.backend_platform == "xiaohongshu"
    cfg.platform = "douyin_enterprise_comment"
    assert cfg.backend_platform == "douyin"
