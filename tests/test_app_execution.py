import importlib
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.browser import BrowserError, BrowserRejected
from app.client import AccountModelUnavailable, AccountQuotaRejected
from app.service import Service
from app.store import Store


class Queue:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))

    def shutdown(self, **kwargs):
        pass


class Client:
    submissions = 0
    error = None

    def __init__(self, account, *, store=None):
        self.account = account

    def account_state(self):
        return {"available_balance": 100, "buckets": {"remainPoints": 100}}

    def onboarding_complete(self):
        return True

    def close(self):
        pass

    def upload_inputs(self, normalized):
        return {"images": [], "videos": [], "audios": []}

    def submit(self, normalized, assets):
        type(self).submissions += 1
        if self.error:
            raise self.error
        return {"run_id": "upstream-task", "thread_id": "account-thread"}

    def poll(self, run_id, thread_id):
        assert (run_id, thread_id) == ("upstream-task", "account-thread")
        return {"status": "succeeded", "result_urls": ["https://cdn.test/result.mp4"]}

    def daily_checkin(self):
        return {"status": "manual_required", "manual_required": True}


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "nad.db"))
    account = store.upsert_account({"name": "one", "access_token": "private-access", "refresh_token": "private-refresh",
                                    "proxy_url": "socks5://proxy.test:1080"})
    store.update_account(account["id"], {"status": "active", "balance": 100})
    value = Service(store)
    value._pool = Queue()
    value._login_pool = Queue()
    monkeypatch.setattr(value.browser_runtime, "acquire", lambda account, **_: ({**account, "cdp_port": 19300 + account["id"]}, False))
    Client.submissions, Client.error = 0, None
    monkeypatch.setattr("app.service.EleClient", Client)
    yield value
    value.stop()


def test_prompt_reference_validation_defaults_to_permissive_snapshot(service):
    assert service.settings()["strict_prompt_media_references"] is False
    request = {"model": "sd-2-0-mini", "prompt": "Use image2 in the next scene",
               "image_urls": ["https://example.test/image.png"]}
    task = service.create(request)
    stored = service.store.task(task["id"])
    assert stored["status"] == "queued"
    assert stored["normalized"]["strict_prompt_media_references"] is False
    assert stored["normalized"]["prompt"] == request["prompt"]
    assert stored["request"] == request
    assert len(service._pool.calls) == 1
    assert Client.submissions == 0


def test_default_video_setting_only_adds_one_clip_when_seedance_25_has_no_video(service, monkeypatch):
    prepared = []
    monkeypatch.setattr("app.service.ensure_reference_video", lambda resolution, ratio: prepared.append((resolution, ratio)))
    body = {"model": "sd-2-5", "prompt": "A landscape", "duration": 4, "resolution": "480p",
            "image_urls": ["https://example.test/image.png"]}
    service.update_settings({"allow_video_reference_inputs": False})
    task = service.store.task(service.create(body, idempotency_key="default-clip")["id"])
    assert task["request"] == body
    assert task["normalized"]["fixed_reference_video"] is True
    assert len(task["normalized"]["videos"]) == 1
    assert task["normalized"]["default_reference_video"]["duration"] == 2
    assert task["media_inputs"][-1]["default_reference"] is True
    assert prepared == [("480p", "16:9")]
    service.update_settings({"use_default_reference_video": False, "default_reference_video_resolution": "1080p"})
    assert service.create(body, idempotency_key="default-clip")["id"] == task["id"]
    assert service.store.task(task["id"])["normalized"]["default_reference_video"]["resolution"] == "480p"
    without = service.store.task(service.create(body)["id"])
    assert without["normalized"]["videos"] == []
    service.update_settings({"use_default_reference_video": True, "allow_video_reference_inputs": True})
    sources = [f"https://example.test/video-{index}.mp4" for index in range(3)]
    supplied = service.store.task(service.create({**body, "video_urls": sources})["id"])
    assert supplied["normalized"]["videos"] == sources
    assert supplied["normalized"]["fixed_reference_video"] is False
    assert prepared == [("480p", "16:9")]


def test_batch_login_preserves_explicit_proxy_and_hides_credentials(service):
    report = service.batch_import("one@example.test|private-one\ntwo@example.test\tprivate-two\t"
                                  "socks5://explicit.test:29999\nthree@example.test----private-three")
    assert report["input_count"] == report["count"] == report["login_started_count"] == 3
    accounts = service.store.accounts(private=True)[1:]
    assert [account["proxy_url"] for account in accounts] == ["socks5://host.docker.internal:20001",
            "socks5://explicit.test:29999", "socks5://host.docker.internal:20002"]
    assert all(account["status"] == "login_pending" for account in accounts)
    public = json.dumps({"report": report, "accounts": service.store.accounts(), "results": service.login_results()})
    assert all(password not in public for password in ("private-one", "private-two", "private-three"))
    assert len(service._login_pool.calls) == 3 and service._pool.calls == []
    duplicate = service.batch_import("one@example.test|private-one")
    assert duplicate["duplicate_count"] == 1 and duplicate["login_started_count"] == 0
    assert len(service._login_pool.calls) == 3
    service.update_settings({"automatic_proxy_assignment": False})
    with pytest.raises(ValueError, match="proxy is required"):
        service.add_account({"name": "no-proxy"})
    explicit = service.add_account({"name": "explicit", "proxy_url": "http://specific.test:8090"})
    assert explicit["proxy_url"] == "http://specific.test:8090"


def test_online_pending_account_uses_the_same_optional_proxy_policy(service):
    pending = service.create_pending_account("online", "")
    assert pending["proxy_url"] == "socks5://host.docker.internal:20001"
    assert pending["managed_profile"] is True and pending["enabled"] is False
    with pytest.raises(ValueError, match="already exists"):
        service.create_pending_account("online", "socks5://other.test:20001")


def test_automatic_logins_have_separate_bounded_two_worker_pool(service, monkeypatch):
    guard = threading.Lock()
    release, both_started = threading.Event(), threading.Event()
    running, peak = 0, 0

    class LoginBrowser:
        def __init__(self, port):
            self.port = port

        def login_with_password(self, email, password):
            nonlocal running, peak
            assert email.endswith("@example.test") and password.startswith("password-")
            with guard:
                running += 1
                peak = max(peak, running)
                if running == 2:
                    both_started.set()
            try:
                assert release.wait(timeout=4)
                return {"access_token": "access-" + str(self.port), "refresh_token": "refresh",
                        "token_expires_at": time.time() + 3600, "firebase_api_key": "test-firebase"}
            finally:
                with guard:
                    running -= 1

    monkeypatch.setattr("app.service.CDPBrowser", LoginBrowser)
    service._login_pool = ThreadPoolExecutor(max_workers=2)
    accounts = [service.add_account({"name": f"login-{index}", "login_email": f"user{index}@example.test",
                                    "login_password": f"password-{index}"}) for index in range(3)]
    try:
        for account in accounts:
            service.queue_account_login(account["id"])
        assert both_started.wait(timeout=3)
        assert service.store.account(accounts[2]["id"])["status"] == "login_pending"
        assert service._pool.calls == []
    finally:
        release.set()
        service._login_pool.shutdown(wait=True)
    assert peak == 2
    assert all(service.store.account(account["id"])["status"] == "active" for account in accounts)
    assert not service._login_pending
    assert all(row["status"] == "active" for row in service.login_results())


def test_captcha_requires_manual_login_without_exposing_diagnostics(service, monkeypatch):
    class LoginBrowser:
        def __init__(self, port):
            pass

        def login_with_password(self, *_):
            raise BrowserRejected("CAPTCHA_REQUIRED private-password private-upstream-body")

    monkeypatch.setattr("app.service.CDPBrowser", LoginBrowser)
    account = service.add_account({"name": "captcha", "login_email": "person@example.test",
                                   "login_password": "private-password"})
    service.queue_account_login(account["id"])
    service._run_account_login(account["id"])
    current = service.store.account(account["id"])
    assert current["status"] == "verification_required"
    assert "private" not in json.dumps({"account": current, "jobs": service.login_results()})
    assert Client.submissions == 0


def test_restart_does_not_replay_automatic_account_logins(service, monkeypatch):
    account = service.add_account({"name": "interrupted", "login_email": "person@example.test",
                                   "login_password": "private-password"})
    service.store.update_account(account["id"], {"status": "logging_in"})
    monkeypatch.setattr(service, "_maintain_accounts", lambda: None)
    service.start()
    assert service.store.account(account["id"])["status"] == "login_required"
    assert service._login_pool.calls == []


def test_login_queue_capacity_is_enforced_without_dispatching_generation(service, monkeypatch):
    monkeypatch.setattr("app.service.MAX_LOGIN_JOBS", 2)
    accounts = [service.add_account({"name": f"bounded-{index}", "login_email": f"user{index}@example.test",
                                    "login_password": "private-password"}) for index in range(3)]
    service.queue_account_login(accounts[0]["id"])
    service.queue_account_login(accounts[1]["id"])
    with pytest.raises(RuntimeError, match="queue is full"):
        service.queue_account_login(accounts[2]["id"])
    assert len(service._login_pool.calls) == 2 and service._pool.calls == []


def test_batch_login_api_sanitizes_private_fields_and_protects_login_identity(service, monkeypatch):
    monkeypatch.setenv("ELE_API_KEY", "test-api")
    monkeypatch.setenv("ELE_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("ELE_DATA_DIR", str(service.store.path.parent / "unused-main"))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "store", service.store)
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "ADMIN_TOKEN", "login-admin")
    monkeypatch.setattr(main, "API_KEY", "login-api")
    api = TestClient(main.app)
    admin = {"X-Admin-Token": "login-admin"}
    body = {"text": "person@example.test|private-password|socks5://specific.test:20038"}
    assert api.post("/api/accounts/batch-import", json=body).status_code == 401
    imported = api.post("/api/accounts/batch-import", json=body, headers=admin)
    assert imported.status_code == 200
    account_id = imported.json()["results"][0]["account_id"]
    accounts = api.get("/api/accounts", headers=admin)
    results = api.get("/api/accounts/login-results", headers=admin)
    assert all(secret not in imported.text + accounts.text + results.text for secret in
               ("private-password", "person@example.test", "private-access", "private-refresh"))
    assert api.post(f"/api/accounts/{account_id}/login", headers=admin).status_code == 200
    assert len(service._login_pool.calls) == 1
    assert api.patch(f"/api/accounts/{account_id}", headers=admin,
                     json={"login_password": "different"}).status_code == 409
    assert api.delete(f"/api/accounts/{account_id}", headers=admin).status_code == 409
    assert api.post(f"/api/accounts/{account_id}/profile/reset", headers=admin, json={}).status_code == 409
    caller = {"Authorization": "Bearer login-api"}
    accidental = api.post("/v1/videos", headers=caller,
                          json={"model": "sd-2-0-mini", "prompt": "lake", "login_password": "private-password"})
    assert accidental.status_code == 422
    assert service.store.tasks() == [] and service._pool.calls == []


@pytest.mark.parametrize("enabled", [False, True])
def test_strict_prompt_reference_setting_persists_after_service_restart(service, enabled):
    result = service.update_settings({"strict_prompt_media_references": enabled})
    assert result["strict_prompt_media_references"] is enabled
    assert service.store.get_settings()["strict_prompt_media_references"] is enabled
    restarted = Service(Store(str(service.store.path)))
    restarted._pool = Queue()
    try:
        assert restarted.settings()["strict_prompt_media_references"] is enabled
    finally:
        restarted.stop()


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_strict_prompt_reference_setting_requires_json_boolean(service, invalid):
    with pytest.raises(ValueError, match="strict_prompt_media_references must be a boolean"):
        service.update_settings({"strict_prompt_media_references": invalid})
    assert service.settings()["strict_prompt_media_references"] is False
    assert "strict_prompt_media_references" not in service.store.get_settings()


def test_strict_reference_rejection_never_persists_or_queues_task(service):
    service.update_settings({"strict_prompt_media_references": True})
    request = {"model": "sd-2-0-mini", "prompt": "Use image2 in the next scene",
               "image_urls": ["https://example.test/image.png"]}
    with pytest.raises(ValueError):
        service.create(request, idempotency_key="strict-rejected")
    assert service.store.tasks() == []
    assert service._pool.calls == []
    assert service.store.idempotent_task("strict-rejected", request) is None
    assert Client.submissions == 0


def test_accepted_idempotency_precedes_changed_reference_settings(service, monkeypatch):
    request = {"model": "sd-2-0-mini", "prompt": "Use image2 in the next scene",
               "image_urls": ["https://example.test/image.png"]}
    task = service.create(request, idempotency_key="accepted-permissive")
    service.update_settings({"strict_prompt_media_references": True})
    # Recovery must not re-read settings or re-normalize an accepted request.
    def settings_must_not_be_read():
        raise AssertionError("accepted idempotency lookup must precede settings")

    monkeypatch.setattr(service, "settings", settings_must_not_be_read)
    again = service.create(request, idempotency_key="accepted-permissive")
    assert again["id"] == task["id"]
    assert len(service.store.tasks()) == len(service._pool.calls) == 1
    assert service.store.task(task["id"])["normalized"]["strict_prompt_media_references"] is False


def test_queued_tasks_keep_reference_validation_snapshot_after_settings_change(service, monkeypatch):
    observed = []

    class SnapshotClient(Client):
        def upload_inputs(self, normalized):
            observed.append((normalized["prompt"], normalized["strict_prompt_media_references"]))
            return super().upload_inputs(normalized)

    monkeypatch.setattr("app.service.EleClient", SnapshotClient)
    first = service.create({"model": "sd-2-0-mini", "prompt": "Use image2",
                            "image_urls": ["https://example.test/image.png"]})
    service.update_settings({"strict_prompt_media_references": True})
    second = service.create({"model": "sd-2-0-mini", "prompt": "Use image1",
                             "image_urls": ["https://example.test/image.png"]})
    # The permissive task runs after strict validation was enabled.
    service._run(first["id"])
    service.update_settings({"strict_prompt_media_references": False})
    # The strict task runs after the setting was disabled.
    service._run(second["id"])
    assert observed == [("Use image2", False), ("Use image1", True)]
    for task, expected in ((first, False), (second, True)):
        stored = service.store.task(task["id"])
        assert stored["status"] == "succeeded"
        assert stored["normalized"]["strict_prompt_media_references"] is expected


def test_settings_api_boolean_validation_and_reference_rejection_are_isolated(service, monkeypatch):
    monkeypatch.setenv("ELE_API_KEY", "test-api")
    monkeypatch.setenv("ELE_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("ELE_DATA_DIR", str(service.store.path.parent / "unused-main"))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "store", service.store)
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "API_KEY", "settings-api")
    monkeypatch.setattr(main, "ADMIN_TOKEN", "settings-admin")
    # Without a context manager, TestClient never enters the real lifespan or
    # starts account maintenance/workers. The fixture's Queue records dispatch.
    api = TestClient(main.app)
    admin = {"X-Admin-Token": "settings-admin"}
    caller = {"Authorization": "Bearer settings-api", "Idempotency-Key": "permissive-api-case"}
    assert api.get("/api/settings").status_code == 401
    assert api.get("/api/settings", headers=admin).json()["strict_prompt_media_references"] is False
    for headers in ({}, caller):
        unauthorized = api.patch("/api/settings", headers=headers,
                                 json={"strict_prompt_media_references": True})
        assert unauthorized.status_code == 401
        assert service.settings()["strict_prompt_media_references"] is False
    strict = api.patch("/api/settings", headers=admin,
                       json={"strict_prompt_media_references": True})
    assert strict.status_code == 200
    assert strict.json()["strict_prompt_media_references"] is True
    invalid = api.patch("/api/settings", headers=admin,
                        json={"strict_prompt_media_references": "false"})
    assert invalid.status_code == 422
    assert api.get("/api/settings", headers=admin).json()["strict_prompt_media_references"] is True
    request = {"model": "sd-2-0-mini", "prompt": "Use image2",
               "image_urls": ["https://example.test/image.png"]}
    rejected = api.post("/v1/videos", headers=caller, json=request)
    assert rejected.status_code == 422
    assert rejected.headers["X-ELEAPI-Submit-Outcome"] == "not-accepted"
    assert service.store.tasks() == [] and service._pool.calls == []
    permissive = api.patch("/api/settings", headers=admin,
                           json={"strict_prompt_media_references": False})
    assert permissive.status_code == 200
    assert permissive.json()["strict_prompt_media_references"] is False
    accepted = api.post("/v1/videos", headers=caller, json=request)
    assert accepted.status_code == 200
    assert service.store.task(accepted.json()["id"])["normalized"]["strict_prompt_media_references"] is False
    assert api.patch("/api/settings", headers=admin,
                     json={"strict_prompt_media_references": True}).status_code == 200
    replay = api.post("/v1/videos", headers=caller, json=request)
    assert replay.status_code == 200 and replay.json()["id"] == accepted.json()["id"]
    assert len(service.store.tasks()) == len(service._pool.calls) == 1
    assert Client.submissions == 0


def test_authorized_account_executes_one_task_with_isolated_browser_lease(service):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake", "resolution": "480p"}, idempotency_key="one")
    service._run(task["id"])
    result = service.public_task(service.store.task(task["id"]))
    assert result["status"] == "succeeded"
    assert result["data"] == [{"type": "video", "url": "https://cdn.test/result.mp4"}]
    assert result["credits_consumed"] is None
    assert result["cost_source"] == "unknown"
    assert Client.submissions == 1
    assert service.create({"model": "sd-2-0-mini", "prompt": "lake", "resolution": "480p"}, idempotency_key="one")["id"] == task["id"]
    assert len(service._pool.calls) == 1


def test_same_account_tasks_prepare_concurrently_with_distinct_generation_ids(service, monkeypatch):
    service.store.update_account(1, {"max_concurrency": 2})
    barrier = threading.Barrier(2)
    submissions = []
    guard = threading.Lock()

    class ConcurrentClient(Client):
        def upload_inputs(self, normalized):
            self.label = normalized["prompt"]
            task = next(task for task in service.store.tasks() if task["prompt"] == self.label)
            assert task["status"] == "preparing" and task["account_id"] == 1
            # The previous account-wide lock prevented a second task from
            # reaching this point until the first finished all preparation.
            barrier.wait(timeout=3)
            return {"images": [], "videos": [], "audios": []}

        def submit(self, normalized, assets):
            with guard:
                submissions.append(self.label)
            return {"run_id": "run-" + self.label, "thread_id": "thread-" + self.label}

        def poll(self, run_id, thread_id):
            assert (run_id, thread_id) == ("run-" + self.label, "thread-" + self.label)
            return {"status": "succeeded", "result_urls": ["https://cdn.test/" + self.label + ".mp4"]}

    monkeypatch.setattr("app.service.EleClient", ConcurrentClient)
    tasks = [service.create({"model": "sd-2-0-mini", "prompt": label}) for label in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service._run, task["id"]) for task in tasks]
        for future in futures:
            future.result(timeout=5)
    assert sorted(submissions) == ["one", "two"]
    stored = [service.store.task(task["id"]) for task in tasks]
    assert all(task["status"] == "succeeded" for task in stored)
    assert {task["run_id"] for task in stored} == {"run-one", "run-two"}
    assert {task["thread_id"] for task in stored} == {"thread-one", "thread-two"}
    assert service._active[1] == 0


@pytest.mark.parametrize("ambiguous", [False, True])
def test_duplicate_dispatch_while_still_queued_never_submits_twice(service, monkeypatch, ambiguous):
    service.store.update_account(1, {"max_concurrency": 2})
    reserved, release = threading.Event(), threading.Event()
    reserve = service._reserve_account

    def paused_reserve(*args, **kwargs):
        account = reserve(*args, **kwargs)
        if account:
            reserved.set()
            assert release.wait(timeout=3)
        return account

    monkeypatch.setattr(service, "_reserve_account", paused_reserve)
    Client.error = BrowserError("connection closed after generation POST") if ambiguous else None
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service._run, task["id"])
        try:
            assert reserved.wait(timeout=2)
            assert service.store.task(task["id"])["status"] == "queued"
            duplicate = pool.submit(service._run, task["id"])
            duplicate.result(timeout=1)
        finally:
            release.set()
        first.result(timeout=4)
    stored = service.store.task(task["id"])
    assert Client.submissions == 1
    assert stored["status"] == ("submission_unknown" if ambiguous else "succeeded")
    if ambiguous:
        assert not stored["run_id"]
        service._run(task["id"])
        assert Client.submissions == 1
    else:
        assert (stored["run_id"], stored["thread_id"]) == ("upstream-task", "account-thread")
    assert service._active[1] == 0


def test_ambiguous_generation_is_never_replayed_or_counted_as_executing(service):
    Client.error = BrowserError("connection closed after POST")
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert service.public_task(stored)["error"]["category"] == "submission_unknown"
    service._run(task["id"])
    assert Client.submissions == 1
    assert service.store.active_account_count(1) == 0
    assert service._active[1] == 0


def test_explicit_rejection_is_terminal_without_ambiguous_state(service):
    Client.error = BrowserRejected("ElevenLabs rejected unsupported reference")
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service._run(task["id"])
    assert service.store.task(task["id"])["status"] == "failed"
    assert Client.submissions == 1


def test_explicit_generation_failure_preserves_upstream_reason(service, monkeypatch):
    class FailedClient(Client):
        def poll(self, run_id, thread_id):
            return {"status": "failed", "error_code": "UPSTREAM_FAILED",
                    "error_message": "Generation rejected by content policy"}

    monkeypatch.setattr("app.service.EleClient", FailedClient)
    task = service.create({"model": "sd-2-0", "prompt": "landscape"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "failed" and stored["error_code"] == "UPSTREAM_FAILED"
    assert stored["error_message"] == "Generation rejected by content policy"
    assert service.public_task(stored)["error"]["category"] == "content_policy"
    assert FailedClient.submissions == 1


def extra_account(service, name, *, enabled=True):
    account = service.store.upsert_account({"name": name, "proxy_url": "socks5://proxy.test:1081", "priority": 0,
                                           "enabled": enabled, "access_token": "synthetic-session"})
    return service.store.update_account(account["id"], {"status": "active", "balance": 100})


def test_unaccepted_quota_rejection_uses_another_eligible_account_without_enabling_disabled_accounts(service, monkeypatch):
    second = extra_account(service, "second")
    calls = []

    class QuotaClient(Client):
        def submit(self, normalized, assets):
            calls.append(self.account["id"])
            if self.account["id"] == 1:
                raise AccountQuotaRejected("synthetic quota rejection", remaining_credits=5, required_credits=20)
            return super().submit(normalized, assets)

    monkeypatch.setattr("app.service.EleClient", QuotaClient)
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "succeeded" and stored["account_id"] == second["id"]
    assert calls == [1, second["id"]] and QuotaClient.submissions == 1
    assert stored["estimated_cost"] == 20 and service.store.account(1)["balance"] == 5
    assert service._active[1] == service._active[second["id"]] == 0


def test_user_disabled_account_is_never_reenabled_or_used_during_account_fallback(service, monkeypatch):
    service.store.update_account(1, {"enabled": False, "balance": 0})
    second = extra_account(service, "second")
    calls = []

    class SelectedClient(Client):
        def __init__(self, account, **kwargs):
            calls.append(account["id"])
            super().__init__(account, **kwargs)

    monkeypatch.setattr("app.service.EleClient", SelectedClient)
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service._run(task["id"])
    assert calls == [second["id"]] and service.store.task(task["id"])["status"] == "succeeded"
    assert not service.store.account(1)["enabled"] and service.store.account(1)["balance"] == 0


def test_model_restrictions_are_per_account_model_and_do_not_consume_credit_retry_limit(service, monkeypatch):
    accounts = [extra_account(service, "other-" + str(i)) for i in range(3)]
    allowed = accounts[-1]["id"]
    checked = []

    class ModelClient(Client):
        def upload_inputs(self, normalized):
            checked.append(self.account["id"])
            if self.account["id"] != allowed:
                raise AccountModelUnavailable("synthetic account model restriction", model=normalized["upstream_model"])
            return super().upload_inputs(normalized)

    monkeypatch.setattr("app.service.EleClient", ModelClient)
    task = service.create({"model": "sd-2-0-mini", "prompt": "landscape", "resolution": "480p"})
    service._run(task["id"])
    assert checked == [1, *[item["id"] for item in accounts]]
    assert service.store.task(task["id"])["status"] == "succeeded"
    assert service.store.task(task["id"])["attempted_account_ids"] == [allowed]
    assert all(account["enabled"] for account in service.store.accounts())
    target = service.store.task(task["id"])["normalized"]["upstream_model"]
    assert not service._account_model_available(1, target)
    assert service._account_model_available(1, "another-authorized-model")
    service._model_restrictions[(1, target)] = time.monotonic() - 1
    assert service._account_model_available(1, target)


def test_all_model_restrictions_stop_promptly_and_cached_selection_does_not_wait(service, monkeypatch):
    extra_account(service, "second")
    checked = []

    class RestrictedClient(Client):
        def upload_inputs(self, normalized):
            checked.append(self.account["id"])
            raise AccountModelUnavailable("synthetic model is restricted for this account", model=normalized["upstream_model"])

    monkeypatch.setattr("app.service.EleClient", RestrictedClient)
    monkeypatch.setattr(service._stop, "wait", lambda *args: pytest.fail("all restricted accounts should not idle-wait"))
    for _ in range(2):
        task = service.create({"model": "sd-2-0-mini", "prompt": "landscape", "resolution": "480p"})
        service._run(task["id"])
        stored = service.store.task(task["id"])
        assert stored["status"] == "failed" and stored["error_code"] == "ACCOUNT_MODEL_UNAVAILABLE"
        assert not stored["run_id"]
    assert checked == [1, 2] and RestrictedClient.submissions == 0
    assert all(account["enabled"] for account in service.store.accounts())


def test_one_task_does_not_revisit_model_rejections_after_cross_task_cache_expires(service, monkeypatch):
    extra_account(service, "second")
    checked = []
    clock = [0.0]

    class SlowRestrictedClient(Client):
        def upload_inputs(self, normalized):
            assert self.account["id"] not in checked, "one task revisited a rejected account after the cache TTL"
            checked.append(self.account["id"])
            clock[0] += 301
            raise AccountModelUnavailable("synthetic model is restricted for this account", model=normalized["upstream_model"])

    monkeypatch.setattr("app.service.EleClient", SlowRestrictedClient)
    monkeypatch.setattr("app.service.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(service._stop, "wait", lambda *args: pytest.fail("all rejected accounts must finish without waiting"))
    task = service.create({"model": "sd-2-0-mini", "prompt": "landscape", "resolution": "480p"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert checked == [1, 2] and stored["error_code"] == "ACCOUNT_MODEL_UNAVAILABLE"
    assert stored["status"] == "failed" and stored["attempted_account_ids"] == []
    assert SlowRestrictedClient.submissions == 0


@pytest.mark.parametrize("code", ["UPSTREAM_MODEL_ERROR", "CONTENT_MODERATION_FAILED"])
def test_definitive_provider_model_and_moderation_failures_keep_original_run_without_switching(service, monkeypatch, code):
    extra_account(service, "second")

    class TerminalClient(Client):
        def poll(self, run_id, thread_id):
            return {"status": "failed", "error_code": code, "error_message": "Synthetic provider failure"}

    monkeypatch.setattr("app.service.EleClient", TerminalClient)
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "failed" and stored["error_code"] == code
    assert stored["account_id"] == 1 and stored["run_id"] == "upstream-task"
    assert TerminalClient.submissions == 1


def test_accepted_poll_quota_error_never_switches_or_reposts(service, monkeypatch):
    extra_account(service, "second")

    class PollQuotaClient(Client):
        def poll(self, *_):
            raise AccountQuotaRejected("synthetic query rejection")

    monkeypatch.setattr("app.service.EleClient", PollQuotaClient)
    monkeypatch.setattr("app.service.time.sleep", lambda *args: None)
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown" and stored["run_id"] == "upstream-task"
    assert stored["account_id"] == 1 and PollQuotaClient.submissions == 1


def test_resume_initialization_rejection_preserves_accepted_task_identity(service, monkeypatch):
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service.store.update_task(task["id"], {"status": "submitted", "account_id": 1,
                                         "run_id": "synthetic-run", "thread_id": "synthetic-session"})

    def unavailable(*args, **kwargs):
        raise BrowserRejected("LOGIN_REQUIRED: synthetic session expired")

    monkeypatch.setattr("app.service.EleClient", unavailable)
    service._resume_poll(task["id"])
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert (stored["run_id"], stored["thread_id"]) == ("synthetic-run", "synthetic-session")
    assert Client.submissions == 0 and service._active[1] == 0


def test_existing_explicit_terminal_credit_rejection_can_use_one_other_account(service, monkeypatch):
    second = extra_account(service, "second")
    selected = []

    class CreditClient(Client):
        def submit(self, normalized, assets):
            selected.append(self.account["id"])
            return super().submit(normalized, assets)

        def poll(self, run_id, thread_id):
            if self.account["id"] == 1:
                return {"status": "failed", "error_code": "UPSTREAM_FAILED", "error_message": "not enough credits"}
            return super().poll(run_id, thread_id)

    monkeypatch.setattr("app.service.EleClient", CreditClient)
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service._run(task["id"])
    assert selected == [1, second["id"]]
    assert service.store.task(task["id"])["status"] == "succeeded"


def test_quota_switch_limit_remains_two_accounts(service, monkeypatch):
    extra_account(service, "second")
    extra_account(service, "third")
    selected = []

    class QuotaClient(Client):
        def submit(self, normalized, assets):
            selected.append(self.account["id"])
            raise AccountQuotaRejected("synthetic quota rejection")

    monkeypatch.setattr("app.service.EleClient", QuotaClient)
    task = service.create({"model": "sd-2-5-480p", "prompt": "landscape"})
    service._run(task["id"])
    stored = service.store.task(task["id"])
    assert selected == [1, 2] and stored["error_code"] == "INSUFFICIENT_CREDITS"
    assert stored["status"] == "failed" and not stored["run_id"]


def test_manual_reward_does_not_mark_daily_claim_complete(service):
    result = service.checkin_account(1)
    assert result["manual_required"]
    assert service.store.account(1)["last_checkin_date"] == ""


def test_restart_requeues_only_upload_phase_not_generation_boundary(service, monkeypatch):
    body = {"model": "sd-2-0-mini", "prompt": "lake"}
    first, second = service.create(body), service.create(body)
    service.store.update_task(first["id"], {"status": "preparing", "progress": 12})
    service.store.update_task(second["id"], {"status": "preparing", "progress": 30})
    service._pool.calls.clear()
    monkeypatch.setattr(service, "_maintain_accounts", lambda: None)
    service.start()
    assert service.store.task(first["id"])["status"] == "queued"
    assert service.store.task(second["id"])["status"] == "submission_unknown"
    assert len(service._pool.calls) == 1
    assert service._pool.calls[0][1] == (first["id"],)


def test_poll_network_loss_preserves_accepted_task(service, monkeypatch):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service.store.update_task(task["id"], {"status": "running", "run_id": "upstream-task", "thread_id": "thread", "account_id": 1})

    class Offline:
        def poll(self, *_):
            raise BrowserError("network unavailable")

    monkeypatch.setattr("app.service.time.sleep", lambda _: None)
    service._poll(task["id"], Offline(), time.monotonic() + 100)
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert stored["run_id"] == "upstream-task"


def test_poll_deadline_keeps_confirmed_ids_and_resume_only_polls(service):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service.store.update_task(task["id"], {"status": "running", "run_id": "upstream-task",
                                         "thread_id": "account-thread", "account_id": 1})
    service._poll(task["id"], Client({"id": 1}), time.monotonic() - 1)
    stored = service.store.task(task["id"])
    assert stored["status"] == "submission_unknown"
    assert stored["run_id"] == "upstream-task"
    result = service.resume(task["id"])
    assert result["status"] == "submitted"
    assert service._pool.calls[-1][0] == service._resume_poll
    service._resume_poll(task["id"])
    assert service.store.task(task["id"])["status"] == "succeeded"
    assert Client.submissions == 0


def test_resume_rejects_missing_upstream_identity_and_duplicate_resume(service):
    task = service.create({"model": "sd-2-0-mini", "prompt": "lake"})
    service._fail(task["id"], "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "network unavailable")
    with pytest.raises(ValueError, match="no confirmed"):
        service.resume(task["id"])
    service.store.update_task(task["id"], {"run_id": "upstream-task", "thread_id": "account-thread", "account_id": 1})
    service.resume(task["id"])
    with pytest.raises(ValueError, match="only interrupted"):
        service.resume(task["id"])


def test_api_auth_not_accepted_header_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("ELE_API_KEY", "test-api")
    monkeypatch.setenv("ELE_ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("ELE_DATA_DIR", str(tmp_path))
    main = importlib.import_module("app.main")
    main.service._pool = Queue()
    api = TestClient(main.app)
    assert api.get("/health").json()["service"] == "ele2api"
    assert api.get("/health").json()["models"] > 0
    assert api.get("/api/accounts").status_code == 401
    headers = {"Authorization": "Bearer test-api", "Idempotency-Key": "task-one"}
    request = {"model": "sd-2-0-mini", "prompt": "lake"}
    rejected = api.post("/v1/videos", json=request, headers=headers)
    assert rejected.status_code == 503
    assert rejected.headers["X-ELEAPI-Submit-Outcome"] == "not-accepted"
    account = main.store.upsert_account({"name": "one", "cookie": "private", "dfp": "device",
                                         "proxy_url": "socks5://proxy.test:1080"})
    main.store.update_account(account["id"], {"status": "active"})
    first = api.post("/v1/videos", json=request, headers=headers)
    second = api.post("/api/v3/contents/generations/tasks", json=request, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    conflict = api.post("/v1/videos", json={**request, "prompt": "different"}, headers=headers)
    assert conflict.status_code == 409
    accounts = api.get("/api/accounts", headers={"X-Admin-Token": "test-admin"}).json()
    assert not {"cookie", "dfp", "user_agent"} & accounts[0].keys()
    main.store.update_task(first.json()["id"], {"account_id": account["id"], "status": "submission_unknown"})
    protected = api.patch(f"/api/accounts/{account['id']}", json={"cookie": "different-session"},
                          headers={"X-Admin-Token": "test-admin"})
    assert protected.status_code == 409
    assert main.store.account(account["id"], private=True)["cookie"] == "private"
    main.service.stop()
