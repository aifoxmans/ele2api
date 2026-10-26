from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from app.browser import BrowserRejected
from app.catalog import normalize_request
from app.client import GENERATION, EleClient, EleError, build_submit_body, build_video_param, extract_result_urls
from app.media import MediaError, upload_media_bytes, validate_material_durations


def bare_client():
    value = object.__new__(EleClient)
    value.account = {"id": 1, "proxy_url": "socks5://proxy.example.test:1080",
                     "access_token": "synthetic-access", "token_expires_at": time.time() + 3600}
    value.store = None
    value.browser = SimpleNamespace(hcaptcha_token=lambda: "synthetic-one-use-challenge")
    value._models_checked = set()
    return value


def assets(images=1, videos=0, audios=0):
    return {kind: [{"content_asset_id": f"{kind}-{i}", "asset_id": f"{kind}-{i}", "status": "finalized",
                    "url": f"https://uploaded.example.test/{kind}/{i}", "duration": 3, "width": 1280, "height": 720}
                   for i in range(1, count + 1)]
            for kind, count in (("images", images), ("videos", videos), ("audios", audios))}


def normalized(model="sd-2-0", **kw):
    return normalize_request({"model": model, "prompt": "Image1", "image_urls": ["https://source.example.test/i.png"], **kw})


def session_response(generation="generation-1", session="session-1"):
    return {"id": session, "iterations": [{"id": "iteration-1", "generations": [{"id": generation, "status": "pending"}]}]}


def test_submit_body_uses_real_asset_references_and_parameter_names():
    value = normalize_request({"model": "sd-2-5-480p", "prompt": "Image1 @Video1 Audio1", "duration": 4,
                               "ratio": "9:16", "generate_audio": False, "image_urls": ["https://source.example.test/i.png"],
                               "video_urls": ["https://source.example.test/v.mp4"], "audio_urls": ["https://source.example.test/a.mp3"]})
    body = build_submit_body(value, assets(1, 1, 1))
    assert body["model_id"] == "bytedance-seedance-v2.5-us"
    assert body["model_parameters"] == {
        "aspect_ratio": "auto", "duration_secs": 4, "resolution": "480p", "generate_audio": False,
        "reference_images": [{"content_asset_id": "images-1"}], "reference_videos": [{"content_asset_id": "videos-1"}],
        "reference_audios": [{"content_asset_id": "audios-1"}]}
    assert [p["type"] for p in body["prompt"]] == ["image_reference", "text", "video_reference", "text", "audio_reference"]
    assert "contents" not in body and "input" not in body and "hcaptcha_token" not in body
    assert value["aspect_ratio"] == "9:16" and value["effective_aspect_ratio"] == "auto"


@pytest.mark.parametrize("mode,video,expected", [("reference_material", True, "auto"),
                                               ("i2v", False, "auto"),
                                               ("reference_images", False, "9:16")])
def test_seedance_two_point_five_effective_ratio_follows_only_video_or_frame(mode, video, expected):
    value = normalize_request({"model": "sd-2-5", "prompt": "Image1", "ratio": "9:16", "mode": mode,
                               "image_urls": ["https://source.example.test/i.png"],
                               "video_urls": ["https://source.example.test/v.mp4"] if video else []},
                              use_default_reference_video=False)
    assert value["aspect_ratio"] == "9:16" and value["effective_aspect_ratio"] == expected
    assert build_video_param(value, assets(1, int(video)))["aspect_ratio"] == expected


def test_old_tasks_without_snapshot_default_loose_and_missing_receipts_still_fail():
    value = normalized(prompt="Image1 @Image99")
    value.pop("strict_prompt_media_references")
    assert build_submit_body(value, assets())["prompt"][-1]["text"] == " @Image99"
    with pytest.raises(ValueError, match="unbound"):
        build_submit_body(value, assets(0))


@pytest.mark.parametrize("model,expected", [("kling-4.0", {}), ("minimax-h3-768p", {"enhance_prompt": True})])
def test_models_without_generate_audio_never_receive_invented_parameter(model, expected):
    params = build_video_param(normalized(model), assets())
    assert "generate_audio" not in params
    for key, item in expected.items():
        assert params[key] == item


def test_first_last_frame_mode_uses_start_end_without_omni_reference_arrays():
    value = normalized(mode="i2v_first_last", image_urls=["https://source.example.test/1.png", "https://source.example.test/2.png"])
    params = build_video_param(value, assets(2))
    assert params["start_frame"] == {"content_asset_id": "images-1"}
    assert params["end_frame"] == {"content_asset_id": "images-2"}
    assert "reference_images" not in params


@pytest.mark.parametrize("status,body", [
    (500, {"detail": {"status": "error", "message": "temporary"}}), (502, {"detail": "temporary"}),
    (408, {"detail": "timeout"}), (425, {"detail": "early"}), (429, {"detail": "rate_limit"}),
    (400, {"id": "accepted-maybe", "detail": {"message": "quota"}}),
])
def test_ambiguous_generation_http_is_never_a_confirmed_rejection_or_retried(status, body):
    client = bare_client()
    calls = []
    client.session = SimpleNamespace(request=lambda *a, **kw: calls.append((a, kw)) or
                                     SimpleNamespace(status_code=status, ok=status < 400, json=lambda: body))
    with pytest.raises(EleError, match="ambiguous"):
        client.request("POST", GENERATION, {})
    assert len(calls) == 1


def test_generation_transport_and_non_json_failures_are_unknown_without_retry():
    client = bare_client()
    for failure in (requests.ReadTimeout("synthetic"), ValueError("synthetic")):
        calls = []
        def execute(*a, **kw):
            calls.append(kw)
            if isinstance(failure, requests.RequestException):
                raise failure
            def invalid():
                raise failure
            return SimpleNamespace(status_code=502, ok=False, json=invalid)
        client.session = SimpleNamespace(request=execute)
        with pytest.raises(EleError):
            client.request("POST", GENERATION, {})
        assert len(calls) == 1


def test_explicit_generation_business_rejection_is_distinct():
    client = bare_client()
    client.session = SimpleNamespace(request=lambda *a, **kw:
                                     SimpleNamespace(status_code=400, ok=False, json=lambda: {"detail": {"status": "insufficient_credits", "message": "Insufficient credits"}}))
    with pytest.raises(BrowserRejected, match="Insufficient credits"):
        client.request("POST", GENERATION, {})


def test_submit_normal_flow_has_one_captcha_and_one_generation_without_secret_audit():
    client = bare_client()
    captcha_calls, calls = [], []
    client.browser.hcaptcha_token = lambda: captcha_calls.append(True) or "one-use-private-value"
    def request(method, path, body=None, **kw):
        calls.append((method, path, body))
        if path.endswith("models-available"):
            assert kw["params"] == {"purpose": "generation"}
            return {"models": [{"model_id": "bytedance-seedance-v2-us", "is_region_restricted": False}]}
        if path.endswith("check-references"):
            return {"references_allowed": True}
        if path.endswith("/price"):
            return {"credits": 123.45, "price_cents": 1.5, "cost_fiat_currency": "usd"}
        assert path == GENERATION
        assert body["hcaptcha_token"] == "one-use-private-value"
        return session_response()
    client.request = request
    result = client.submit(normalized(), assets())
    assert (result["run_id"], result["thread_id"]) == ("generation-1", "session-1")
    assert len(captcha_calls) == 1
    assert sum(path == GENERATION for _, path, _ in calls) == 1
    assert "hcaptcha_token" not in result["upstream_request"]["body"]
    assert "one-use-private-value" not in str(result)
    assert result["credits_consumed"] is None
    assert result["cost_evidence"]["source"] == "quote"
    assert result["cost_evidence"]["quoted"] == 123.45


def test_no_browser_or_restricted_account_rejects_before_download(monkeypatch):
    client = bare_client()
    monkeypatch.setattr(client, "_upload_input", lambda *a: pytest.fail("must not download"))
    client.browser = None
    with pytest.raises(BrowserRejected, match="CAPTCHA_REQUIRED"):
        client.upload_inputs(normalized())
    client.browser = object()
    client.request = lambda *a, **kw: {"models": [{"model_id": "bytedance-seedance-v2-us", "is_region_restricted": True}]}
    with pytest.raises(BrowserRejected, match="restricted"):
        client.upload_inputs(normalized())


def test_captcha_failure_never_calls_generation_or_claims_submission_unknown():
    client = bare_client()
    client._check_model = lambda model: None
    calls = []
    client.request = lambda method, path, body=None: calls.append(path) or (
        {"references_allowed": True} if path.endswith("check-references") else {"credits": 20})
    def challenge():
        raise BrowserRejected("CAPTCHA_REQUIRED")
    client.browser.hcaptcha_token = challenge
    with pytest.raises(BrowserRejected, match="CAPTCHA_REQUIRED"):
        client.submit(normalized(), assets())
    assert GENERATION not in calls and not hasattr(client, "last_submit_request")


@pytest.mark.parametrize("response", [{}, {"id": "session-1", "iterations": []},
                                     {"id": "session-1", "iterations": [{"generations": [{"id": "a"}, {"id": "b"}]}]}])
def test_missing_or_multiple_generation_ids_never_trigger_resubmission(response):
    client = bare_client()
    client._check_model = lambda model: None
    def request(method, path, body=None):
        return {"references_allowed": True} if path.endswith("check-references") else {"credits": 20} if path.endswith("price") else response
    client.request = request
    with pytest.raises(EleError, match="unambiguous"):
        client.submit(normalized(), assets())


def test_poll_uses_exact_ids_original_result_and_task_specific_final_price():
    client = bare_client()
    value = {"id": "generation-1", "generation_session_id": "session-1", "status": "completed",
             "content_url": "https://output.example.test/original.mp4", "preview_content_url": "https://output.example.test/preview.mp4",
             "file_properties": {"width": 560, "height": 752}, "price": {"credits": 123.45}}
    calls = []
    client.request = lambda method, path: calls.append((method, path)) or value
    result = client.poll("generation-1", "session-1")
    assert calls == [("GET", GENERATION + "/generation-1")]
    assert result["result_urls"] == [value["content_url"]]
    assert result["credits_consumed"] == 123.45 and result["cost_evidence"]["charged"] == 123.45
    value["generation_session_id"] = "unrelated-session"
    with pytest.raises(EleError, match="identity"):
        client.poll("generation-1", "session-1")
    assert extract_result_urls({"status": "pending", "content_url": value["content_url"]}) == []


def test_generating_poll_keeps_original_task_running_until_completed():
    client = bare_client()
    value = {"id": "generation-1", "generation_session_id": "session-1", "status": "generating"}
    calls = []
    client.request = lambda method, path: calls.append((method, path)) or value
    result = client.poll("generation-1", "session-1")
    assert result["status"] == "running" and result["upstream_status"] == "generating"
    assert not result.get("result_urls") and result.get("credits_consumed") is None
    value.update(status="completed", content_url="https://output.example.test/original.mp4", price={"credits": 123.45})
    completed = client.poll("generation-1", "session-1")
    assert completed["status"] == "succeeded" and completed["credits_consumed"] == 123.45
    assert calls == [("GET", GENERATION + "/generation-1")] * 2


def test_failed_poll_preserves_real_cause_and_never_invents_deducted_credits():
    client = bare_client()
    client.request = lambda *a: {"id": "g", "generation_session_id": "s", "status": "failed",
                                 "error_message": "Synthetic copyright restriction", "price": {"credits": 100}}
    result = client.poll("g", "s")
    assert result["status"] == "failed" and "copyright" in result["error_message"]
    assert result["cost_evidence"]["source"] == "unknown"


def test_multipart_upload_uses_asset_and_name_and_seconds_metadata():
    calls = []
    receipt = {"id": "asset-1", "status": "finalized", "content_url": "https://uploaded.example.test/a.mp4",
               "file_properties": {"duration_secs": 2.02, "width": 1280, "height": 720, "has_audio": True, "size_bytes": 17}}
    client = SimpleNamespace(request=lambda *a, **kw: calls.append((a, kw)) or receipt)
    result = upload_media_bytes(b"synthetic-video", "video/mp4", "clip.mp4", "videos", client=client)
    assert calls[0][0] == ("POST", "/v1/assets/internal")
    assert list(calls[0][1]["files"]) == ["asset"]
    assert calls[0][1]["data"] == {"name": "clip.mp4"}
    assert result["duration"] == 2.02 and result["content_asset_id"] == "asset-1"


def test_asset_confirmation_is_bounded_and_checks_original_asset_identity(monkeypatch):
    monkeypatch.setattr("app.media.time.sleep", lambda *a: None)
    responses = iter([{"id": "asset-1", "status": "processing"}, {"id": "wrong-asset", "status": "finalized"}])
    client = SimpleNamespace(request=lambda *a, **kw: next(responses))
    with pytest.raises(MediaError, match="ID"):
        upload_media_bytes(b"synthetic", "video/mp4", "v.mp4", "videos", client=client)


def test_real_duration_total_caps_and_kling_dimension_constraints():
    value = normalized()
    with pytest.raises(BrowserRejected, match="total duration"):
        validate_material_durations(value, assets(1, 3, 0) | {"videos": [{"duration": 6}] * 3})
    kling = normalized("kling-4.0")
    with pytest.raises(BrowserRejected, match="dimensions"):
        validate_material_durations(kling, assets(1, 1) | {"videos": [{"duration": 3, "width": 640, "height": 480}]})


def test_unobserved_rewards_never_send_requests():
    client = bare_client()
    client.request = lambda *a, **kw: pytest.fail("no invented reward endpoint")
    assert client.daily_checkin()["manual_required"] is True


def test_empty_browser_auth_does_not_erase_valid_private_session():
    client = bare_client()
    client._save_auth({"access_token": "", "refresh_token": ""})
    assert client.account["access_token"] == "synthetic-access"


@pytest.mark.parametrize("response,ready", [
    ({"is_onboarding_completed": True}, True), ({"is_onboarding_completed": False}, False),
    ({}, False), ({"is_onboarding_completed": "true"}, False),
    ({"is_onboarding_completed": 1}, False), ({"user": {"is_onboarding_completed": True}}, False),
])
def test_onboarding_readiness_requires_explicit_server_boolean(response, ready):
    client = bare_client()
    calls = []
    client.request = lambda *args: calls.append(args) or response
    assert client.onboarding_complete() is ready
    assert calls == [("GET", "/v1/user/internal")]


def test_wrong_browser_identity_is_rejected_before_account_or_store_changes():
    import base64
    import json

    client = bare_client()
    client.account["login_email"] = "person@example.test"
    saved = []
    client.store = SimpleNamespace(update_account=lambda *args: saved.append(args))
    original = dict(client.account)
    payload = base64.urlsafe_b64encode(json.dumps({"email": "other@example.test"}).encode()).decode().rstrip("=")
    with pytest.raises(BrowserRejected, match="identity differs"):
        client._save_auth({"access_token": "header." + payload + ".signature", "refresh_token": "private-other-refresh"})
    assert client.account == original and not saved


def test_matching_browser_identity_can_save_private_session_case_insensitively():
    import base64
    import json

    client = bare_client()
    client.account["login_email"] = "person@example.test"
    saved = []
    client.store = SimpleNamespace(update_account=lambda *args: saved.append(args))
    payload = base64.urlsafe_b64encode(json.dumps({"email": "PERSON@example.test"}).encode()).decode().rstrip("=")
    auth = {"access_token": "header." + payload + ".signature", "refresh_token": "private-new-refresh"}
    client._save_auth(auth)
    assert client.account["access_token"] == auth["access_token"]
    assert saved == [(1, auth)]


def test_stale_browser_token_falls_back_to_normal_firebase_refresh(monkeypatch):
    client = bare_client()
    client.account.update(token_expires_at=1, refresh_token="synthetic-refresh")
    client.browser = SimpleNamespace(collect_auth=lambda: {"access_token": "", "token_expires_at": 0})
    client.session = SimpleNamespace(headers={})
    monkeypatch.setattr("app.client.refresh_firebase_auth", lambda a: {"access_token": "fresh-synthetic", "token_expires_at": time.time() + 3600})
    client._fresh_token()
    assert client.session.headers["Authorization"] == "Bearer fresh-synthetic"


def test_concurrent_source_preparation_is_singleflight_and_not_cross_account(monkeypatch):
    one, two = bare_client(), bare_client()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def prepare(source, kind, account_id):
        calls.append(account_id)
        entered.set()
        assert release.wait(2)
        return assets()["images"][0]
    monkeypatch.setattr(one, "_prepare_input", prepare)
    monkeypatch.setattr(two, "_prepare_input", prepare)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(one._upload_input, "https://source.example.test/i.png", "images", 1)
        assert entered.wait(1)
        second = pool.submit(two._upload_input, "https://source.example.test/i.png", "images", 1)
        time.sleep(0.02)
        release.set()
        assert first.result() == second.result()
    assert calls == [1]
    two._upload_input("https://source.example.test/i.png", "images", 2)
    assert calls == [1, 2]
