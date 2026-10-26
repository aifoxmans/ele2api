from concurrent.futures import ThreadPoolExecutor

import pytest

from app.cost_adapter import build_observations, cost_values
from app.errors import public_error
from app.store import ACCOUNT_SECRET_FIELDS, IdempotencyConflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "ele2api.db"))


def normalized():
    return {"model": "sd-2-0-mini", "upstream_model": "seedance-2.0-mini", "prompt": "lake",
            "duration": 4, "resolution": "480p", "aspect_ratio": "16:9",
            "images": [], "videos": [], "audios": []}


def test_account_auth_is_private_but_importable(store):
    public = store.upsert_account({"name": "one", "proxy_url": "socks5://proxy.test:1080",
                                   "cookie": "sid=secret", "dfp": "device", "user_agent": "ua",
                                   "access_token": "private-access", "refresh_token": "private-refresh",
                                   "firebase_api_key": "private-firebase", "token_expires_at": 1000,
                                   "login_email": "private@example.test", "login_password": "private-password"})
    assert public["authorized"] is True
    assert public["password_configured"] is True
    assert public["login_identifier"] == "pr***@example.test"
    assert not set(ACCOUNT_SECRET_FIELDS) & public.keys()
    assert not set(ACCOUNT_SECRET_FIELDS) & store.accounts()[0].keys()
    private = store.account(public["id"], private=True)
    assert private["cookie"] == "sid=secret"
    assert private["dfp"] == "device"
    assert private["login_password"] == "private-password"
    assert private["token_expires_at"] == 1000
    assert Store(str(store.path)).account(public["id"], private=True)["refresh_token"] == "private-refresh"


@pytest.mark.parametrize("expiry", [-1, True, "not-a-timestamp", float("inf")])
def test_invalid_token_expiry_is_rejected_without_partial_account(store, expiry):
    with pytest.raises(ValueError, match="token_expires_at"):
        store.upsert_account({"name": "invalid", "token_expires_at": expiry})
    assert store.accounts() == []


def test_idempotency_is_durable_atomic_and_conflict_safe(store):
    body = {"model": "sd-2-0-mini", "prompt": "lake"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: store.create_task(f"ele_{n}", body, normalized(),
                                                           idempotency_key="unique"), range(20)))
    assert len({task["id"] for task in results}) == 1
    assert len(store.tasks()) == 1
    restarted = Store(str(store.path))
    assert restarted.idempotent_task("unique", body)["id"] == results[0]["id"]
    with pytest.raises(IdempotencyConflict):
        restarted.idempotent_task("unique", {**body, "prompt": "different"})


def test_clear_keeps_active_and_unknown_and_idempotency_tombstone(store):
    for status in ("queued", "running", "submission_unknown", "succeeded", "failed", "expired"):
        store.create_task("ele_" + status, {"model": "sd-2-0-mini"}, normalized(), idempotency_key=status)
        store.update_task("ele_" + status, {"status": status})
    assert store.clear_terminal_tasks() == 3
    assert {task["status"] for task in store.tasks()} == {"queued", "running", "submission_unknown"}
    with pytest.raises(IdempotencyConflict, match="cleared"):
        store.idempotent_task("succeeded", {"model": "sd-2-0-mini"})


def test_media_cache_is_account_and_type_isolated(store):
    receipt = {"url": "https://cdn.test/image.png", "width": 100, "height": 100}
    store.media_cache_put(1, "image", "digest", receipt)
    assert store.media_cache_get(1, "image", "digest") == receipt
    assert store.media_cache_get(2, "image", "digest") is None
    assert store.media_cache_get(1, "video", "digest") is None


def test_unknown_protects_account_identity_without_execution_slot(store):
    account = store.upsert_account({"name": "one", "cookie": "sid=one", "dfp": "device",
                                    "proxy_url": "socks5://proxy.test:1080"})
    task = store.create_task("ele_unknown", {"model": "sd-2-0-mini"}, normalized())
    store.update_task(task["id"], {"account_id": account["id"], "status": "submission_unknown"})
    assert store.active_account_count(account["id"]) == 0
    assert store.unresolved_account_count(account["id"]) == 1
    with pytest.raises(ValueError, match="protected"):
        store.upsert_account({"name": "one", "proxy_url": "socks5://other.test:1080"})
    with pytest.raises(ValueError, match="protected"):
        store.upsert_account({"name": "one", "cookie": "sid=different"})
    assert store.account(account["id"], private=True)["cookie"] == "sid=one"


def test_balance_delta_never_becomes_actual_cost(store):
    task = store.create_task("ele_cost", {"model": "sd-2-0-mini"}, normalized())
    task = store.update_task(task["id"], {"actual_cost": 20, "balance_before": 200, "balance_after": 180,
                                         "cost_evidence": {"balance_isolated": True}})
    row = build_observations(task)[0]
    assert row["charged"] is None
    assert row["quoted"] is None
    assert row["source"] == "unknown"


def test_explicit_task_receipt_and_quote_remain_distinct(store):
    task = store.create_task("ele_cost", {"model": "sd-2-0-mini"}, normalized())
    quote = cost_values({"cost_source": "precharged", "credits_consumed": 12})
    task = store.update_task(task["id"], quote)
    assert build_observations(task)[0]["quoted"] == 12
    assert build_observations(task)[0]["charged"] is None
    task = store.update_task(task["id"], cost_values({"cost_source": "actual", "credits_consumed": 11}))
    assert build_observations(task)[0]["charged"] == 11


def test_upstream_price_evidence_is_an_estimate_until_task_receipt_exists(store):
    task = store.create_task("ele_quote", {"model": "sd-2-5"}, normalized())
    values = cost_values({"credits_consumed": None, "cost_evidence": {
        "source": "quote", "quoted": 125, "endpoint": "POST /v1/image-video/price", "field": "credits"}})
    assert values["estimated_cost"] == 125 and "actual_cost" not in values
    task = store.update_task(task["id"], values)
    row = build_observations(task)[0]
    assert row["source"] == "quote" and row["quoted"] == 125 and row["charged"] is None


@pytest.mark.parametrize("code,raw", [
    ("INSUFFICIENT_CREDITS", "not enough credits, private account"),
    ("CONTENT_MODERATION_FAILED", "sensitive prompt rejected"),
    ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "fetch failed"),
    ("MEDIA_DOWNLOAD_FAILED", "http://private.test/a?secret=token"),
])
def test_public_messages_use_only_fixed_vocabulary(code, raw):
    error = public_error(code, raw)
    assert error["message"] == error["public_message"]
    assert raw not in error["message"]
    assert error["category"]
    assert "?" not in error["message"]


@pytest.mark.parametrize("raw", [
    "生成的视频涉及版权限制，请修改后重试。",
    "Artifact rejected by copyright policy: private upstream diagnostic",
])
def test_copyright_failure_uses_existing_content_policy_message(raw):
    error = public_error("UPSTREAM_FAILED", raw)
    assert error["category"] == "content_policy"
    assert error["message"] == "检测到内容有敏感或违规情况，请修改后重试，积分已返还～"
    assert raw not in error["message"]


def test_pre_submit_source_timeout_is_a_download_error():
    error = public_error("MEDIAERROR", "reference download failed: ReadTimeout")
    assert error["category"] == "invalid_request"
    assert error["message"] == "素材下载失败，请检查素材链接后重试~"


def test_unconfirmed_submission_stays_unknown_despite_policy_words():
    error = public_error("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "copyright check response unavailable")
    assert error["category"] == "submission_unknown"


def test_operator_queue_cancellation_does_not_claim_generation_or_refund():
    error = public_error("QUEUE_CANCELLED", "operator cancelled before upstream submission; no generation request sent")
    assert error["message"] == "队列排队服务中断，请稍后再试~"
    assert "积分" not in error["message"]


@pytest.mark.parametrize("reason,expected", [
    ("host=copyright.example.test reason=read timeout", "素材下载失败，请检查素材链接后重试~"),
    ("host=source.example.test reason=images reference has the wrong content type", "素材格式不支持，请修改后再试~"),
    ("host=source.example.test reason=reference media exceeds 200 MB", "素材超限，请修改后再试~"),
])
def test_source_download_diagnostics_keep_validation_and_transport_categories(reason, expected):
    result = public_error("MEDIAERROR", "images[6]: reference download failed: " + reason)
    assert result["category"] == "invalid_request"
    assert result["message"] == expected
