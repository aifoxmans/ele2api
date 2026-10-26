"""ElevenLabs browser-authorized protocol; paid generation is never retried."""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from concurrent.futures import Future
from typing import Any
from urllib.parse import urlsplit

import requests

from .browser import API_BASE, BASE, BrowserError, BrowserRejected, CDPBrowser, account_proxy_url, refresh_firebase_auth
from .catalog import bind_prompt
from .media import MediaError, _download, upload_media_bytes, validate_material_durations
from .reference_video import read_reference, reference_spec
from .store import Store

GENERATION = "/v1/content/generations"
_MEDIA_LOCKS_GUARD = threading.RLock()
_MEDIA_LOCKS: dict[int, threading.RLock] = {}
_MEDIA_CACHE_LOCKS_GUARD = threading.Lock()
_MEDIA_CACHE_LOCKS: dict[tuple[int, str, str], threading.RLock] = {}
_MEDIA_SOURCE_FLIGHTS_GUARD = threading.Lock()
_MEDIA_SOURCE_FLIGHTS: dict[tuple[int, str, str], Future[dict[str, Any]]] = {}
_MATERIAL_PREPARATION_SLOTS = threading.BoundedSemaphore(4)
_MATERIAL_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def account_media_lock(account_id: int) -> threading.RLock:
    with _MEDIA_LOCKS_GUARD:
        return _MEDIA_LOCKS.setdefault(account_id, threading.RLock())


def _media_cache_lock(account_id: int, kind: str, digest: str) -> threading.RLock:
    with _MEDIA_CACHE_LOCKS_GUARD:
        return _MEDIA_CACHE_LOCKS.setdefault((account_id, kind, digest), threading.RLock())


def _complete_media_receipt(receipt: Any) -> bool:
    return (isinstance(receipt, dict) and bool(_VALID_ID.fullmatch(str(receipt.get("content_asset_id") or "")))
            and receipt.get("status") == "finalized")


class EleError(BrowserError):
    pass


def _reference(item: dict[str, Any]) -> dict[str, str]:
    asset_id = str(item.get("content_asset_id") or "")
    if not _VALID_ID.fullmatch(asset_id):
        raise ValueError("unbound media reference")
    return {"content_asset_id": asset_id}


def build_video_param(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    params: dict[str, Any] = {"aspect_ratio": normalized["aspect_ratio"], "duration_secs": normalized["duration"],
                              "resolution": normalized["resolution"].replace("2k", "2K").replace("4k", "4K")
                              if normalized["family"] == "minimax" else normalized["resolution"]}
    family, mode = normalized["family"], normalized.get("mode", "reference_material")
    params["aspect_ratio"] = normalized.get("effective_aspect_ratio") or (
        "auto" if family == "2.5" and (assets["videos"] or mode in {"i2v", "i2v_first_last"}) else normalized["aspect_ratio"])
    if family in {"minimax", "wan"}:
        params["enhance_prompt"] = normalized.get("enhance_prompt", True)
    if family not in {"minimax", "kling"}:
        params["generate_audio"] = normalized.get("generate_audio", True)
    if family == "minimax" and normalized["aspect_ratio"] == "auto":
        params["aspect_ratio"] = None
    if normalized.get("seed") is not None:
        params["seed"] = normalized["seed"]
    if mode in {"i2v", "i2v_first_last"}:
        if assets["images"]:
            params["start_frame"] = _reference(assets["images"][0])
        if len(assets["images"]) == 2:
            params["end_frame"] = _reference(assets["images"][1])
    else:
        for kind in ("images", "videos", "audios"):
            if assets[kind]:
                params["reference_" + kind] = [_reference(item) for item in assets[kind]]
    return params


def build_submit_body(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sources = {kind: normalized[kind][:int((normalized.get("prompt_reference_counts") or {}).get(kind, len(normalized[kind])))]
               for kind in ("images", "videos", "audios")}
    bound = bind_prompt(normalized["prompt"], assets, sources=sources,
                        strict_prompt_media_references=normalized.get("strict_prompt_media_references", False))
    return {**bound, "model_id": normalized["upstream_model"], "generations_count": 1,
            "model_parameters": build_video_param(normalized, assets)}


def extract_result_urls(value: dict[str, Any]) -> list[str]:
    """Return only the finalized original output, never previews or references."""
    if value.get("status") != "completed":
        return []
    url = value.get("content_url") or value.get("download_url")
    parsed = urlsplit(str(url or ""))
    return [url] if parsed.scheme == "https" and parsed.hostname else []


def _price_evidence(value: Any, *, actual: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"credits_consumed": None, "cost_evidence": {"source": "unknown"}}
    amount = value.get("credits")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
        return {"credits_consumed": None, "cost_evidence": {"source": "unknown"}}
    evidence = {"source": "actual" if actual else "quote", "credits": float(amount),
                "field": "price.credits" if actual else "credits", "price_cents": value.get("price_cents"),
                "currency": value.get("cost_fiat_currency"),
                "endpoint": "GET /v1/content/generations/{id}" if actual else "POST /v1/content/generations/price"}
    if actual:
        evidence["charged"] = float(amount)
    else:
        evidence["quoted"] = float(amount)
    return {"credits_consumed": float(amount) if actual else None, "cost_evidence": evidence}


class EleClient:
    def __init__(self, account: dict[str, Any], *, store: Store | None = None):
        self.account = dict(account)
        self.store = store
        self.browser = CDPBrowser(int(account["cdp_port"])) if int(account.get("cdp_port") or 0) else None
        if self.browser is not None:
            self._save_auth(self.browser.collect_auth())
        if float(self.account.get("token_expires_at") or 0) <= time.time() + 60 and self.account.get("refresh_token"):
            self._save_auth(refresh_firebase_auth(self.account))
        if not self.account.get("access_token"):
            raise EleError("ElevenLabs session unavailable; complete browser login and verification")
        proxy = account_proxy_url(self.account).replace("socks5://", "socks5h://", 1)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({"Accept": "application/json", "Origin": BASE, "Referer": BASE + "/",
                                     "User-Agent": str(self.account.get("user_agent") or "Mozilla/5.0"),
                                     "Authorization": "Bearer " + str(self.account["access_token"]),
                                     "x-generation-actor": "User", "x-generation-surface": "Image Video"})
        self._models_checked: set[str] = set()

    def _save_auth(self, auth: dict[str, Any]) -> None:
        if not isinstance(auth, dict) or not isinstance(auth.get("access_token"), str) or not auth["access_token"].strip():
            return
        expected_email = str(self.account.get("login_email") or "").strip()
        if expected_email:
            CDPBrowser._login_identity(auth, expected_email)
        fields = {key: auth[key] for key in ("access_token", "refresh_token", "firebase_api_key", "token_expires_at",
                                             "cookie", "user_agent") if key in auth}
        self.account.update(fields)
        if self.store is not None:
            self.store.update_account(int(self.account["id"]), fields)

    def _fresh_token(self) -> None:
        if float(self.account.get("token_expires_at") or 0) <= time.time() + 60 and self.account.get("refresh_token"):
            if self.browser:
                self._save_auth(self.browser.collect_auth())
            if float(self.account.get("token_expires_at") or 0) <= time.time() + 60:
                self._save_auth(refresh_firebase_auth(self.account))
            self.session.headers["Authorization"] = "Bearer " + str(self.account["access_token"])

    def close(self) -> None:
        self.session.close()

    def request(self, method: str, path: str, body: Any = None, *, params: dict[str, Any] | None = None,
                files: Any = None, data: Any = None, timeout: tuple[int, int] = (15, 120)) -> dict[str, Any]:
        if not path.startswith("/v1/") or urlsplit(path).netloc or "?" in path or "#" in path:
            raise EleError("ElevenLabs API path is invalid")
        self._fresh_token()
        kwargs: dict[str, Any] = {"params": params, "timeout": timeout, "allow_redirects": False}
        if files is not None:
            kwargs.update(files=files, data=data)
        elif method.upper() != "GET":
            kwargs["json"] = body
        generation = method.upper() == "POST" and path == GENERATION
        try:
            response = self.session.request(method, API_BASE + path, **kwargs)
        except requests.RequestException as exc:
            raise EleError(f"ElevenLabs {method} request outcome unavailable: {type(exc).__name__}") from exc
        try:
            value = response.json()
        except ValueError as exc:
            if not generation and response.status_code >= 400:
                raise BrowserRejected(f"ElevenLabs HTTP {response.status_code}: non-JSON rejection") from exc
            raise EleError("ElevenLabs response is unreadable; request outcome is unknown") from exc
        if not isinstance(value, dict):
            raise EleError("ElevenLabs response envelope is invalid")
        ambiguous_http = response.status_code >= 500 or response.status_code in {408, 425, 429}
        if generation and (ambiguous_http or (not response.ok and (value.get("id") or value.get("iterations")))):
            raise EleError(f"ElevenLabs generation response is ambiguous (HTTP {response.status_code}); do not resubmit")
        if not response.ok or "detail" in value:
            detail = value.get("detail") or {}
            if generation and response.ok and not isinstance(detail, (dict, str)):
                raise EleError("ElevenLabs generation acceptance is uncertain; do not resubmit")
            message = str(detail.get("message") or detail.get("status") or detail.get("code") or "request rejected") if isinstance(detail, dict) else str(detail)
            message = re.sub(r"https?://[^\s\"']+", "[url]", message)[:500]
            raise BrowserRejected(f"ElevenLabs HTTP {response.status_code}: {message}")
        return value

    def account_state(self) -> dict[str, Any]:
        value = self.request("GET", "/v1/user/subscription")
        used, limit = value.get("character_count"), value.get("character_limit")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (used, limit)):
            raise EleError("ElevenLabs credit balance was not returned; verify the login session")
        return {"available_balance": max(0.0, float(limit) - float(used)),
                "buckets": {"used": used, "limit": limit, "tier": value.get("tier"), "status": value.get("status")}}

    def onboarding_complete(self) -> bool:
        """A valid subscription alone does not complete the website's setup."""
        value = self.request("GET", "/v1/user/internal")
        return value.get("is_onboarding_completed") is True

    verify = account_state

    def ensure_no_watermark(self) -> str:
        return "original_content_url"

    def daily_checkin(self) -> dict[str, Any]:
        return {"status": "manual_required", "manual_required": True, "daily_signin_observed": False,
                "reason": "No daily check-in or claim endpoint was observed in the recorded ElevenLabs flow."}

    checkin = daily_checkin

    def _check_model(self, model: str) -> None:
        if model in self._models_checked:
            return
        value = self.request("GET", "/v1/content-generation/models-available", params={"purpose": "generation"})
        matches = [m for m in value.get("models", []) if isinstance(m, dict) and m.get("model_id") == model]
        if not matches:
            raise BrowserRejected(f"ElevenLabs model {model} is not available for this account")
        info = matches[0]
        if info.get("is_region_restricted") or info.get("is_synthetic_restricted") or info.get("is_approved_by_workspace_admin") is False:
            raise BrowserRejected(f"ElevenLabs model {model} is restricted for this account")
        self._models_checked.add(model)

    def _upload_input(self, source: str, kind: str, account_id: int) -> dict[str, Any]:
        key = (account_id, kind, hashlib.sha256(source.encode("utf-8")).hexdigest())
        with _MEDIA_SOURCE_FLIGHTS_GUARD:
            flight = _MEDIA_SOURCE_FLIGHTS.get(key)
            owner = flight is None or flight.done()
            if owner:
                flight = Future()
                _MEDIA_SOURCE_FLIGHTS[key] = flight
        if not owner:
            return dict(flight.result())
        try:
            receipt = self._prepare_input(source, kind, account_id)
        except BaseException as exc:
            flight.set_exception(exc)
            raise
        else:
            flight.set_result(receipt)
            return dict(receipt)
        finally:
            with _MEDIA_SOURCE_FLIGHTS_GUARD:
                if _MEDIA_SOURCE_FLIGHTS.get(key) is flight:
                    del _MEDIA_SOURCE_FLIGHTS[key]

    def _prepare_input(self, source: str, kind: str, account_id: int) -> dict[str, Any]:
        with _MATERIAL_PREPARATION_SLOTS:
            if reference_spec(source):
                if kind != "videos":
                    raise MediaError("internal reference clip is valid only for videos")
                data, mime, filename = read_reference(source)
            else:
                data, mime, filename = _download(source, kind, str(self.account["proxy_url"]))
            digest = hashlib.sha256(data).hexdigest()
            with _media_cache_lock(account_id, kind, digest):
                receipt = self.store.media_cache_get(account_id, kind, digest) if self.store else None
                if not _complete_media_receipt(receipt):
                    with _MATERIAL_UPLOAD_SLOTS:
                        receipt = upload_media_bytes(data, mime, filename, kind, client=self)
                    if not _complete_media_receipt(receipt):
                        raise EleError("ElevenLabs upload returned no complete material receipt")
                    if self.store:
                        self.store.media_cache_put(account_id, kind, digest, receipt)
                return dict(receipt)

    def upload_inputs(self, normalized: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        # Reject missing authorization or regional access before downloading any material.
        if self.browser is None:
            raise BrowserRejected("CAPTCHA_REQUIRED: complete ElevenLabs browser authorization before generating")
        self._check_model(normalized["upstream_model"])
        result: dict[str, list[dict[str, Any]]] = {"images": [], "videos": [], "audios": []}
        for kind in result:
            for index, source in enumerate(normalized[kind], 1):
                try:
                    result[kind].append(self._upload_input(source, kind, int(self.account["id"])))
                except MediaError as exc:
                    raise MediaError(f"{kind}[{index}]: {exc}") from exc
        validate_material_durations(normalized, result)
        return result

    def submit(self, normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        if self.browser is None:
            raise BrowserRejected("CAPTCHA_REQUIRED: complete ElevenLabs browser authorization before generating")
        self._check_model(normalized["upstream_model"])
        body = build_submit_body(normalized, assets)
        parameters = {"model_id": body["model_id"], "model_parameters": body["model_parameters"]}
        checked = self.request("POST", GENERATION + "/check-references", parameters)
        if checked.get("references_allowed") is not True:
            raise BrowserRejected("ElevenLabs references are not allowed for this model and account")
        quote = self.request("POST", GENERATION + "/price", {**parameters, "generations_count": 1})
        token = self.browser.hcaptcha_token()
        if not isinstance(token, str) or not token:
            raise BrowserRejected("CAPTCHA_REQUIRED: complete the challenge in the ElevenLabs account browser")
        # Audit payload deliberately excludes the one-use challenge token and all auth headers.
        self.last_submit_request = {"endpoint": GENERATION, "query": {}, "body": dict(body)}
        value = self.request("POST", GENERATION, {**body, "hcaptcha_token": token})
        iterations = value.get("iterations")
        generations = [g for it in iterations or [] if isinstance(it, dict) for g in it.get("generations") or [] if isinstance(g, dict)]
        if (not _VALID_ID.fullmatch(str(value.get("id") or "")) or len(generations) != 1
                or not _VALID_ID.fullmatch(str(generations[0].get("id") or ""))):
            raise EleError("ElevenLabs returned no unambiguous generation ID; do not automatically resubmit")
        return {"run_id": generations[0]["id"], "thread_id": value["id"], "state": "RUNNING",
                **_price_evidence(quote), "upstream_request": self.last_submit_request}

    def poll(self, run_id: str, thread_id: str) -> dict[str, Any]:
        if not _VALID_ID.fullmatch(str(run_id)) or not _VALID_ID.fullmatch(str(thread_id)):
            raise EleError("ElevenLabs polling requires confirmed generation and session IDs")
        value = self.request("GET", GENERATION + "/" + run_id)
        if value.get("id") != run_id or value.get("generation_session_id") != thread_id:
            raise EleError("ElevenLabs generation identity does not match the accepted task")
        status = str(value.get("status") or "").lower()
        if status in {"failed", "error", "cancelled", "canceled"}:
            message = value.get("error_message") or value.get("failure_reason") or "Generation failed"
            return {"status": "failed", "error_code": "UPSTREAM_FAILED", "error_message": str(message)[:1000],
                    "upstream_status": status, "cost_evidence": {"source": "unknown"}}
        if status == "completed":
            urls = extract_result_urls(value)
            if not urls:
                raise EleError("ElevenLabs completed generation returned no original result URL")
            return {"status": "succeeded", "progress": 100, "result_urls": urls, "upstream_status": status,
                    "metadata": value.get("file_properties") or {}, **_price_evidence(value.get("price"), actual=True)}
        if status not in {"pending", "queued", "processing", "running", "in_progress", "generating"}:
            raise EleError("ElevenLabs generation returned an unrecognized status")
        return {"status": "running", "progress": 45, "upstream_status": status}
