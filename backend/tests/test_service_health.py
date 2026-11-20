import asyncio
import importlib
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
from threading import Event, Thread
import time
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest
import websockets


BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]
LAUNCHER = BACKEND.parent / "desktop" / "launcher.pyw"


@pytest.fixture
def launcher():
    spec = spec_from_loader("desktop_launcher", SourceFileLoader("desktop_launcher", str(LAUNCHER)))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def health_api(monkeypatch):
    monkeypatch.setenv("DB_PATH", ":memory:")
    server = importlib.import_module("reply_server")
    database_module = importlib.import_module("db_manager")
    database = database_module.DBManager(":memory:")
    monkeypatch.setattr(server, "db_manager", database)
    monkeypatch.setattr(database_module, "db_manager", database)
    monkeypatch.setattr(server.cookie_manager, "manager", object())
    try:
        yield TestClient(server.app), server, database
    finally:
        database.conn.close()


def test_health_reports_ready_with_an_empty_account_database(health_api):
    client, _, _ = health_api

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_health_returns_503_when_database_query_fails(health_api):
    client, _, database = health_api
    with database.lock, database.conn:
        database.conn.execute("DROP TABLE cookies")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["services"]["database"] == "error"


def test_health_returns_503_when_account_manager_is_missing(health_api, monkeypatch):
    client, server, _ = health_api
    monkeypatch.setattr(server.cookie_manager, "manager", None)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["services"]["cookie_manager"] == "error"


def test_scheduled_token_refresh_keeps_the_same_heartbeat_task(monkeypatch):
    from XianyuAutoAsync import XianyuLive
    import cookie_manager

    async def scenario():
        monkeypatch.setattr(cookie_manager, "manager", None)
        live = XianyuLive("unb=1", cookie_id="token-refresh-test")
        live.heartbeat_interval = 0.01
        live.current_token = "old-token"
        socket = AsyncMock(closed=False)
        live.ws = socket
        refresh_started = asyncio.Event()
        allow_refresh = asyncio.Event()
        refresh_finished = asyncio.Event()
        sleep = live._interruptible_sleep

        async def refresh_token():
            refresh_started.set()
            await allow_refresh.wait()
            live.current_token = "new-token"
            live.last_token_refresh_time = time.time()
            return live.current_token

        async def wait_for_next_refresh(duration):
            if duration == 60:
                refresh_finished.set()
            await sleep(duration)

        async def wait_for_heartbeat_count(count):
            while socket.send.await_count < count:
                await asyncio.sleep(0.01)

        monkeypatch.setattr(live, "refresh_token", refresh_token)
        monkeypatch.setattr(live, "_interruptible_sleep", wait_for_next_refresh)
        heartbeat_task = asyncio.create_task(live.heartbeat_loop(socket))
        live.heartbeat_task = heartbeat_task
        refresh_task = asyncio.create_task(live.token_refresh_loop())
        refresh_waiter = asyncio.create_task(refresh_finished.wait())
        try:
            await refresh_started.wait()
            await wait_for_heartbeat_count(2)
            allow_refresh.set()
            await asyncio.wait({refresh_task, refresh_waiter}, return_when=asyncio.FIRST_COMPLETED)
            socket.close.assert_not_awaited()
            await wait_for_heartbeat_count(3)
            assert live.current_token == "new-token"
            assert live.heartbeat_task is heartbeat_task
            assert not heartbeat_task.done()
            assert not refresh_task.done()
        finally:
            heartbeat_task.cancel()
            refresh_task.cancel()
            refresh_waiter.cancel()
            await asyncio.gather(heartbeat_task, refresh_task, refresh_waiter, return_exceptions=True)
            live._unregister_instance()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_websocket_reconnect_restarts_heartbeat_without_cancelling_the_old_task(monkeypatch):
    from XianyuAutoAsync import XianyuLive
    import cookie_manager

    async def scenario():
        monkeypatch.setattr(cookie_manager, "manager", None)
        live = XianyuLive("unb=1", cookie_id="heartbeat-reconnect-test")
        live.heartbeat_interval = 60
        live.current_token = "refreshed-token"
        live.last_token_refresh_time = time.time()
        live.item_sync_enabled = False
        live.cookie_refresh_enabled = False
        first_heartbeat_task = None
        reconnected = asyncio.Event()

        async def server(socket):
            nonlocal first_heartbeat_task
            registration = json.loads(await socket.recv())
            assert registration["headers"]["token"] == "refreshed-token"
            assert json.loads(await socket.recv())["lwp"] == "/r/SyncStatus/ackDiff"
            assert json.loads(await socket.recv())["lwp"] == "/!"
            if first_heartbeat_task is None:
                first_heartbeat_task = live.heartbeat_task
                await socket.close()
            else:
                reconnected.set()
                await socket.wait_closed()

        async with websockets.serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            live.base_url = f"ws://127.0.0.1:{port}"
            main_task = asyncio.create_task(live.main())
            try:
                await reconnected.wait()
                assert first_heartbeat_task.done()
                assert not first_heartbeat_task.cancelled()
                assert live.heartbeat_task is not first_heartbeat_task
                assert not live.heartbeat_task.done()
            finally:
                main_task.cancel()
                await asyncio.gather(main_task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(scenario(), timeout=8))


def test_wait_ready_requires_the_health_endpoint(launcher):
    paths = []

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if len(paths) == 1:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "healthy"}')

        def log_message(self, format, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), HealthHandler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            assert launcher.wait_ready("127.0.0.1", server.server_port, None)
            assert paths == ["/health", "/health"]
        finally:
            server.shutdown()
            worker.join()


def test_watchdog_retries_a_failed_restart_and_only_notifies_after_ready(launcher, monkeypatch):
    quit_signal = Event()
    checks = 0

    def wait(seconds):
        nonlocal checks
        if seconds == launcher.WATCH_INTERVAL:
            checks += 1
            if checks == 4:
                quit_signal.set()
        return quit_signal.is_set()

    attempts = []

    def boot(host, port, quiet):
        attempts.append((host, port, quiet))
        if len(attempts) == 1:
            launcher._SERVER["proc"] = None
            return False
        launcher._SERVER["proc"] = Mock(poll=Mock(return_value=None))
        return True

    launcher._SERVER["proc"] = Mock(poll=Mock(return_value=1), returncode=1)
    monkeypatch.setattr(launcher, "_QUIT", quit_signal)
    monkeypatch.setattr(quit_signal, "wait", wait)
    monkeypatch.setattr(launcher.time, "sleep", wait)
    monkeypatch.setattr(launcher, "boot_server", boot)
    monkeypatch.setattr(launcher, "health_ready", Mock(return_value=False))
    monkeypatch.setattr(launcher, "log", Mock())
    monkeypatch.setattr(launcher, "notify", Mock())

    launcher.watchdog("127.0.0.1", 8080)

    assert attempts == [("127.0.0.1", 8080, True), ("127.0.0.1", 8080, True)]
    launcher.notify.assert_called_once_with("后台服务第 2 次自动重启")
