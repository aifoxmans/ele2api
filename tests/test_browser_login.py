"""Manual login finalization shares the setup gate and keeps its viewer on failure."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.browser import BrowserRejected
from app.browser_login import BrowserLoginManager, LoginError
from app.store import Store


@pytest.fixture
def manager(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "manual.db"))
    account = store.upsert_account({"name": "one", "proxy_url": "socks5://proxy.test:1080",
                                    "login_email": "person@example.test"})
    store.update_account(account["id"], {"status": "logging_in", "enabled": False})
    service = SimpleNamespace(_lock=threading.RLock(), settings=lambda: {"low_balance_disable_threshold": 0})
    manager = BrowserLoginManager(store, service)
    manager.active = {"id": "private-session", "account_id": account["id"],
                      "desktop": SimpleNamespace(cdp_port=20001, alive=lambda: True), "enable_after": True,
                      "previous": {"enabled": True, "status": "active"},
                      "deadline": time.monotonic() + 100, "phase": "login"}
    events = []

    class Browser:
        error = None

        def __init__(self, port):
            assert port == 20001

        def complete_onboarding(self, *, expected_email):
            assert expected_email == "person@example.test"
            assert not store.account(account["id"])["enabled"]
            events.append("onboarding-ui")
            if self.error:
                raise self.error
            return {"access_token": "verified-private-access"}

    class Client:
        ready = True

        def __init__(self, value, **kwargs):
            assert value["access_token"] == "verified-private-access"
            assert value["cdp_port"] == 20001

        def onboarding_complete(self):
            events.append("readiness")
            return self.ready

        def account_state(self):
            assert not store.account(account["id"])["enabled"]
            events.append("balance")
            return {"available_balance": 100, "buckets": {"used": 0, "limit": 100}}

        def close(self):
            events.append("close")

    monkeypatch.setattr("app.browser_login.CDPBrowser", Browser)
    monkeypatch.setattr("app.browser_login.UpstreamClient", Client)
    return manager, account["id"], events, Browser, Client


def test_manual_finish_requires_ui_then_server_readiness_before_activating(manager):
    manager, account_id, events, _, _ = manager
    result = asyncio.run(manager.finish("private-session"))
    assert result["enabled"] and result["status"] == "active"
    assert result["cdp_port"] == 20001 and manager.active is None
    assert events == ["onboarding-ui", "readiness", "balance", "close"]
    assert manager.store.account(account_id, private=True)["access_token"] == "verified-private-access"


def test_server_not_ready_keeps_manual_viewer_and_account_disabled(manager):
    manager, account_id, events, _, client = manager
    client.ready = False
    with pytest.raises(LoginError, match="偏好设置"):
        asyncio.run(manager.finish("private-session"))
    assert manager.active["phase"] == "login"
    assert manager.store.account(account_id)["status"] == "logging_in"
    assert not manager.store.account(account_id)["enabled"]
    assert events == ["onboarding-ui", "readiness", "close"]


@pytest.mark.parametrize("code", ["ONBOARDING_REQUIRED", "LOGIN_REQUIRED", "CAPTCHA_REQUIRED"])
def test_manual_completion_failure_does_not_expose_private_diagnostics(manager, code):
    manager, account_id, events, browser, _ = manager
    browser.error = BrowserRejected(code + " private-password private-cookie")
    with pytest.raises(LoginError) as failure:
        asyncio.run(manager.finish("private-session"))
    assert "private-" not in str(failure.value)
    assert not manager.store.account(account_id)["enabled"]
    assert manager.active["phase"] == "login" and events == ["onboarding-ui"]


def test_explicitly_disabled_manual_account_stays_disabled_after_setup(manager):
    manager, _, _, _, _ = manager
    manager.active["enable_after"] = False
    result = asyncio.run(manager.finish("private-session"))
    assert not result["enabled"] and result["status"] == "disabled"


def test_cancel_after_failed_setup_does_not_restore_previous_active_state(manager):
    manager, account_id, _, _, client = manager
    client.ready = False
    with pytest.raises(LoginError):
        asyncio.run(manager.finish("private-session"))
    asyncio.run(manager.cancel("private-session"))
    account = manager.store.account(account_id)
    assert not account["enabled"] and account["status"] == "onboarding_required"
