"""Website setup is a separate gate from authentication and credit balance."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from app.browser import BrowserError, BrowserRejected
from app.service import Service, account_login_failure
from app.store import Store


@pytest.fixture
def account_service(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "accounts.db"))
    account = store.upsert_account({"name": "one", "proxy_url": "socks5://proxy.test:1080",
                                    "login_email": "person@example.test", "login_password": "private-password"})
    store.update_account(account["id"], {"status": "active", "balance": 100})
    service = Service(store)
    events = []

    class Client:
        ready = True
        error = None

        def __init__(self, account, **kwargs):
            self.account = account

        def onboarding_complete(self):
            events.append("readiness")
            if self.error:
                raise self.error
            return self.ready

        def account_state(self):
            events.append("balance")
            return {"available_balance": 100, "buckets": {"used": 0, "limit": 100}}

        def close(self):
            events.append("close")

    monkeypatch.setattr("app.service.EleClient", Client)
    monkeypatch.setattr(service.browser_runtime, "acquire",
                        lambda value, **kwargs: ({**value, "cdp_port": 19300 + value["id"]}, True))
    monkeypatch.setattr(service.browser_runtime, "release", lambda *args: events.append("release"))
    yield service, account["id"], events, Client
    service.stop()


def test_normal_account_check_reads_setup_before_balance_and_never_clicks(account_service, monkeypatch):
    service, account_id, events, _ = account_service
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: pytest.fail("ordinary check touched website UI"))
    assert service.check_account(account_id)["status"] == "active"
    assert events == ["readiness", "balance", "close", "release"]


def test_unfinished_setup_disables_account_before_balance_is_requested(account_service):
    service, account_id, events, client = account_service
    client.ready = False
    result = service.check_account(account_id)
    assert result["status"] == "onboarding_required" and result["enabled"] is False
    assert "readiness" in events and "balance" not in events
    assert service._reserve_account() is None


def test_maintenance_does_not_enable_or_navigate_unfinished_accounts(account_service, monkeypatch):
    service, account_id, events, client = account_service
    client.ready = False

    class OneIteration:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.stopped = True

        def set(self):
            self.stopped = True

    service._stop = OneIteration()
    service.store.update_account(account_id, {"cdp_port": 19300 + account_id})
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: pytest.fail("maintenance touched website UI"))
    service._maintain_accounts()
    result = service.store.account(account_id)
    assert result["status"] == "onboarding_required" and not result["enabled"]
    assert "balance" not in events
    service._stop.stopped = False
    events.clear()
    service._maintain_accounts()
    assert not events


def test_explicit_browser_completion_keeps_disabled_until_ui_and_server_confirm(account_service, monkeypatch):
    service, account_id, events, _ = account_service
    service.store.update_account(account_id, {"enabled": False, "status": "onboarding_required"})

    def complete(*, expected_email):
        current = service.store.account(account_id)
        assert current["status"] == "logging_in" and not current["enabled"]
        assert expected_email == "person@example.test"
        events.append("onboarding-ui")
        return {"access_token": "verified-private-access"}

    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(complete_onboarding=complete))
    result = service.check_account(account_id, complete_onboarding=True)
    assert result["status"] == "active" and result["enabled"]
    assert events == ["onboarding-ui", "readiness", "balance", "close", "release"]
    assert service.store.account(account_id, private=True)["access_token"] == "verified-private-access"


@pytest.mark.parametrize("message,status", [
    ("ONBOARDING_REQUIRED private-password", "onboarding_required"),
    ("CAPTCHA_REQUIRED private-password", "verification_required"),
    ("LOGIN_REQUIRED private-password", "login_required"),
])
def test_completion_errors_are_clear_private_and_leave_account_disabled(account_service, monkeypatch, message, status):
    service, account_id, events, _ = account_service

    def complete(**kwargs):
        raise BrowserRejected(message)

    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(complete_onboarding=complete))
    result = service.check_account(account_id, complete_onboarding=True)
    assert result["status"] == status and not result["enabled"]
    assert "private-password" not in result["last_error"]
    assert "readiness" not in events and events[-1] == "release"


@pytest.mark.parametrize("busy", ["task", "unresolved", "login"])
def test_browser_completion_rejects_busy_account_before_ui(account_service, monkeypatch, busy):
    service, account_id, events, _ = account_service
    if busy == "task":
        service._active[account_id] = 1
    elif busy == "unresolved":
        monkeypatch.setattr(service.store, "unresolved_account_count", lambda value: 1)
    else:
        service.store.update_account(account_id, {"status": "logging_in"})
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: pytest.fail("busy account touched website UI"))
    with pytest.raises(BrowserError, match="active login or unresolved"):
        service.check_account(account_id, complete_onboarding=True)
    assert not events


def test_transient_maintenance_error_remains_eligible_for_later_read_only_recheck(account_service, monkeypatch):
    service, account_id, events, client = account_service
    client.error = BrowserError("temporary transport unavailable private-detail")
    monkeypatch.setattr("app.service.time.sleep", lambda *args: None)
    result = service.check_account(account_id)
    assert result["status"] == "login_required" and result["enabled"]
    assert events.count("readiness") == 3 and "balance" not in events
    assert "private-detail" not in result["last_error"]


def test_automatic_login_preserves_server_onboarding_required(account_service, monkeypatch):
    service, account_id, _, client = account_service
    client.ready = False
    service.store.update_account(account_id, {"status": "login_pending", "enabled": False})
    service._login_pending.add(account_id)
    service._login_results[account_id] = {"account_id": account_id}
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=lambda *args: {"access_token": "verified-private-access"}))
    service._run_account_login(account_id)
    result = service.store.account(account_id)
    assert result["status"] == "onboarding_required" and not result["enabled"]
    assert service.login_results()[0]["status"] == "onboarding_required"
    assert not service.login_in_progress(account_id)


def test_low_balance_is_applied_only_after_server_readiness(account_service):
    service, account_id, events, _ = account_service
    service.update_settings({"low_balance_disable_threshold": 101})
    result = service.check_account(account_id)
    assert result["status"] == "disabled_low_balance" and not result["enabled"]
    assert events.index("readiness") < events.index("balance")


def test_background_readiness_cannot_overwrite_new_login_reservation(account_service, monkeypatch):
    service, account_id, _, client = account_service
    entered, release = threading.Event(), threading.Event()

    def readiness(self):
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(client, "onboarding_complete", readiness)
    service._login_pool.shutdown(wait=True)
    service._login_pool = SimpleNamespace(submit=lambda *args: None, shutdown=lambda **kwargs: None)
    with ThreadPoolExecutor(1) as pool:
        checked = pool.submit(service.check_account, account_id)
        assert entered.wait(2)
        service.queue_account_login(account_id)
        release.set()
        result = checked.result(timeout=3)
    assert result["status"] == "login_pending" and not result["enabled"]
    assert service.store.account(account_id)["status"] == "login_pending"


@pytest.mark.parametrize("http_status", [408, 425, 429, 500, 502, 503, 599])
def test_temporary_readiness_http_retries_are_bounded_and_preserve_enabled(account_service, monkeypatch, http_status):
    service, account_id, events, client = account_service
    client.error = BrowserRejected(f"ElevenLabs HTTP {http_status}: private-provider-body")
    sleeps = []
    monkeypatch.setattr("app.service.time.sleep", sleeps.append)
    result = service.check_account(account_id)
    assert result["status"] == "network_error" and result["enabled"]
    assert "上游暂时不可用" in result["last_error"]
    assert "private-provider-body" not in result["last_error"]
    assert events.count("readiness") == events.count("close") == 3
    assert "balance" not in events and sleeps == [1, 1]


def test_maintenance_rechecks_temporary_http_failure_and_recovers_without_ui(account_service, monkeypatch):
    service, account_id, events, client = account_service
    service.store.update_account(account_id, {"cdp_port": 19300 + account_id})
    calls = []
    waits = []

    def readiness(self):
        calls.append("GET")
        if len(calls) <= 3:
            raise BrowserRejected("ElevenLabs HTTP 503: private-provider-body")
        return True

    class TwoIterations:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            current = service.store.account(account_id)
            waits.append((timeout, current["status"], current["enabled"]))
            if len(waits) == 2:
                self.stopped = True

        def set(self):
            self.stopped = True

    service._stop = TwoIterations()
    monkeypatch.setattr(client, "onboarding_complete", readiness)
    monkeypatch.setattr("app.service.time.sleep", lambda *args: None)
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: pytest.fail("maintenance touched website UI"))
    service._maintain_accounts()
    assert calls == ["GET"] * 4
    assert waits == [(15, "network_error", True), (300, "active", True)]
    assert service.store.account(account_id)["status"] == "active" and events.count("balance") == 1


def test_automatic_login_temporary_http_failure_keeps_original_disabled_state(account_service, monkeypatch):
    service, account_id, events, client = account_service
    client.error = BrowserRejected("ElevenLabs HTTP 429: private-provider-body")
    service.store.update_account(account_id, {"status": "login_pending", "enabled": False})
    service._login_pending.add(account_id)
    service._login_results[account_id] = {"account_id": account_id}
    monkeypatch.setattr("app.service.time.sleep", lambda *args: None)
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=lambda *args: {"access_token": "verified-private-access"}))
    service._run_account_login(account_id)
    result = service.store.account(account_id)
    assert result["status"] == "network_error" and not result["enabled"]
    assert service.login_results()[0]["status"] == "network_error"
    assert "private-provider-body" not in service.login_results()[0]["message"]
    assert events.count("readiness") == 3 and "balance" not in events
    assert not service.login_in_progress(account_id)


@pytest.mark.parametrize("message,status", [
    ("ElevenLabs HTTP 401: session invalid", "login_required"),
    ("ElevenLabs HTTP 401: prior ElevenLabs HTTP 503: invalid session", "login_required"),
    ("LOGIN_REQUIRED: expired", "login_required"),
    ("CAPTCHA_REQUIRED: private-provider-body", "verification_required"),
])
def test_definitive_account_rejections_disable_without_temporary_http_retry(account_service, monkeypatch, message, status):
    service, account_id, events, client = account_service
    client.error = BrowserRejected(message)
    monkeypatch.setattr("app.service.time.sleep", lambda *args: pytest.fail("definitive rejection retried"))
    result = service.check_account(account_id)
    assert result["status"] == status and not result["enabled"]
    assert events.count("readiness") == 1 and "balance" not in events


def test_automatic_login_result_explains_completed_age_and_preferences(account_service, monkeypatch):
    service, account_id, _, _ = account_service
    service.store.update_account(account_id, {"status": "login_pending", "enabled": False})
    service._login_pending.add(account_id)
    service._login_results[account_id] = {"account_id": account_id}
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=lambda *args: {"access_token": "verified-private-access"}))
    service._run_account_login(account_id)
    assert service.login_results()[0]["status"] == "active"
    assert "年龄确认" in service.login_results()[0]["message"]
    assert "偏好设置" in service.login_results()[0]["message"]


@pytest.fixture
def automatic_account(account_service, monkeypatch):
    service, account_id, events, client = account_service
    service.store.update_account(account_id, {"status": "login_pending", "enabled": False})
    service._login_pending.add(account_id)
    service._login_results[account_id] = {"account_id": account_id}
    monkeypatch.setattr(service._stop, "wait", lambda timeout: False)
    return service, account_id, events, client


@pytest.mark.parametrize("marker", ["ONBOARDING_LOADING_TIMEOUT", "ONBOARDING_NETWORK_ERROR"])
def test_transient_setup_resume_never_replays_password_login(automatic_account, monkeypatch, marker):
    service, account_id, _, _ = automatic_account
    calls = []

    def login(*args):
        calls.append("login")
        raise BrowserRejected(marker + ": private-detail")

    def complete(**kwargs):
        calls.append("complete")
        assert kwargs == {"expected_email": "person@example.test"}
        return {"access_token": "verified-private-access"}

    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=login, complete_onboarding=complete))
    service._run_account_login(account_id)
    assert calls == ["login", "complete"]
    assert service.store.account(account_id)["status"] == "active"
    result = service.login_results()[0]
    assert result["onboarding_resume_attempts"] == 1 and result["phase"] == "complete"


def test_setup_resume_stops_after_one_extra_attempt(automatic_account, monkeypatch):
    service, account_id, _, _ = automatic_account
    calls = []

    def fail(stage):
        calls.append(stage)
        raise BrowserError("ONBOARDING_NETWORK_ERROR: private-cookie private-password")

    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=lambda *args: fail("login"), complete_onboarding=lambda **kwargs: fail("complete")))
    service._run_account_login(account_id)
    result = service.store.account(account_id)
    assert calls == ["login", "complete"]
    assert result["status"] == "network_error" and not result["enabled"]
    assert "已登录" in result["last_error"] and "private-" not in result["last_error"]
    assert service.login_results()[0]["phase"] == "onboarding"


@pytest.mark.parametrize("marker", [
    "ONBOARDING_REQUIRED", "CAPTCHA_REQUIRED", "LOGIN_INVALID_CREDENTIALS",
    "LOGIN_FAILED", "LOGIN_LOADING_TIMEOUT", "LOGIN_NETWORK_ERROR",
])
def test_unconfirmed_or_definitive_login_failures_never_replay_credentials(automatic_account, monkeypatch, marker):
    service, account_id, _, _ = automatic_account
    calls = []

    def login(*args):
        calls.append("login")
        raise BrowserRejected(marker + ": private-cookie private-password")

    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=login, complete_onboarding=lambda **kwargs: pytest.fail("must not resume this failure")))
    service._run_account_login(account_id)
    result = service.store.account(account_id)
    assert calls == ["login"] and not result["enabled"]
    assert "private-" not in result["last_error"]
    assert service.login_results()[0]["onboarding_resume_attempts"] == 0


def test_browser_initialization_retries_before_password_submission_only(automatic_account, monkeypatch):
    service, account_id, _, _ = automatic_account
    calls = []

    def acquire(account, **kwargs):
        calls.append("acquire")
        if calls == ["acquire"]:
            raise BrowserError("ElevenLabs CDP port 19301 is unavailable: TimeoutError")
        return {**account, "cdp_port": 19301}, True

    monkeypatch.setattr(service.browser_runtime, "acquire", acquire)
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: SimpleNamespace(
        login_with_password=lambda *args: calls.append("login") or {"access_token": "verified-private-access"}))
    service._run_account_login(account_id)
    assert calls == ["acquire", "acquire", "login"]
    assert service.login_results()[0]["browser_attempts"] == 2
    assert service.store.account(account_id)["status"] == "active"


@pytest.mark.parametrize("failure,attempts,status", [
    (BrowserError("managed ElevenLabs Chrome did not start"), 2, "network_error"),
    (BrowserRejected("LOGIN_REQUIRED: private-token"), 1, "login_required"),
    (BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port"), 1, "network_error"),
])
def test_startup_failure_is_bounded_and_never_enters_login(automatic_account, monkeypatch, failure, attempts, status):
    service, account_id, _, _ = automatic_account
    calls = []

    def acquire(*args, **kwargs):
        calls.append("acquire")
        raise failure

    monkeypatch.setattr(service.browser_runtime, "acquire", acquire)
    monkeypatch.setattr("app.service.CDPBrowser", lambda *args: pytest.fail("startup failed before login"))
    service._run_account_login(account_id)
    result = service.store.account(account_id)
    assert calls == ["acquire"] * attempts and not result["enabled"]
    assert result["status"] == status and "private-" not in result["last_error"]
    assert service.login_results()[0]["phase"] == "browser_start"
    assert service.login_results()[0]["browser_attempts"] == attempts


@pytest.mark.parametrize("marker,phrase", [
    ("ONBOARDING_LOADING_TIMEOUT", "页面加载超时"),
    ("ONBOARDING_NETWORK_ERROR", "连接上游失败"),
    ("ONBOARDING_REQUIRED", "当前步骤"),
    ("LOGIN_LOADING_TIMEOUT", "登录页面加载超时"),
    ("LOGIN_NETWORK_ERROR", "登录页面连接上游失败"),
    ("LOGIN_INVALID_CREDENTIALS", "邮箱或密码"),
])
def test_login_diagnostics_preserve_failure_stage_without_private_prose(marker, phrase):
    _, message = account_login_failure(BrowserRejected(marker + ": private-password private-token"))
    assert phrase in message and "private-" not in message
