"""Account-aware queue and durable ElevenLabs video task execution."""

from __future__ import annotations

from .cost_adapter import build_observations, input_evidence, cost_values

import os
import logging
import re
import statistics
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .browser import BrowserError, BrowserRejected, CDPBrowser
from .browser_runtime import BrowserRuntime
from .catalog import normalize_request
from .client import EleClient, EleError, account_media_lock
from .errors import public_error
from .media import MediaError
from .reference_video import REFERENCE_RESOLUTIONS, ensure_reference_video
from .store import ACCOUNT_AUTH_FIELDS, Store


TERMINAL = {"succeeded", "failed", "expired"}
MAX_CREDIT_ACCOUNT_ATTEMPTS = 2
AUTO_PROXY_PORTS = (*range(20001, 20023), *range(20031, 20041))
MAX_LOGIN_JOBS = 100
logger = logging.getLogger(__name__)


def account_login_failure(error: Exception, *, phase: str = "") -> tuple[str, str]:
    """Keep account diagnostics useful without publishing private browser data."""
    reason = str(error).upper()
    if "CAPTCHA_REQUIRED" in reason:
        return "verification_required", "网站需要人工验证，请打开网页登录完成验证"
    if reason.startswith("ONBOARDING_LOADING_TIMEOUT"):
        return "onboarding_required", "已登录，年龄确认或偏好页面加载超时，请稍后重试或打开网页登录继续"
    if reason.startswith("ONBOARDING_NETWORK_ERROR"):
        return "network_error", "已登录，年龄确认或偏好设置连接上游失败，请稍后重试"
    if "ONBOARDING_REQUIRED" in reason:
        return "onboarding_required", "已登录，年龄确认或偏好设置未完成，请打开网页登录检查当前步骤"
    if reason.startswith("LOGIN_LOADING_TIMEOUT"):
        return "network_error", "登录页面加载超时，请稍后重试自动登录"
    if reason.startswith("LOGIN_NETWORK_ERROR"):
        return "network_error", "登录页面连接上游失败，请稍后重试自动登录"
    if "BROWSER IDENTITY DIFFERS" in reason:
        return "login_required", "浏览器登录账号与导入邮箱不一致，请核对账号后重试"
    if reason.startswith(("LOGIN_INVALID_CREDENTIALS", "LOGIN_CREDENTIALS_REJECTED")):
        return "login_required", "邮箱或密码被网站拒绝，请检查后重试"
    if "LOGIN_FAILED: THE WEBSITE REJECTED" in reason:
        return "login_required", "网站拒绝了本次登录，请检查邮箱密码或打开网页登录验证"
    if reason.startswith("LOGIN_REQUIRED"):
        return "login_required", "登录会话尚未就绪或已失效，请重新登录"
    if "ACCOUNT PROXY MUST BE" in reason or "CONFIGURE AN ACCOUNT PROXY" in reason:
        return "network_error", "账号代理配置无效，请检查代理地址后重试"
    if phase == "browser_start" and not isinstance(error, BrowserRejected):
        return "network_error", "浏览器初始化未完成，请稍后重试自动登录"
    if reason.startswith("LOGIN_FAILED"):
        return "login_required", "登录表单尚未完成或未获得会话，请打开网页登录检查"
    return "login_required", "登录验证未完成，请重试自动登录或完成网页登录"


def _retryable_login_browser_start(error: BrowserError) -> bool:
    """Only startup/network failures before any credentials were entered."""
    if isinstance(error, BrowserRejected):
        return False
    reason = str(error)
    return (reason.startswith(("BROWSER_INITIALIZING", "BROWSER_STARTUP_TIMEOUT", "BROWSER_NETWORK_ERROR"))
            or "managed ElevenLabs Chrome did not start" in reason
            or "account browser is still initializing" in reason
            or bool(re.match(r"ElevenLabs CDP port \d+ is unavailable:", reason)))


class AccountCreditRejected(Exception):
    """The upstream explicitly rejected this account after a terminal result."""


class AccountPoolUnavailable(Exception):
    """No enabled account can accept a new task before upstream submission."""


def _credit_rejected(message: str) -> bool:
    value = message.lower()
    return any(token in value for token in (
        "credit not enough", "credits not enough", "not enough credits",
        "insufficient credits", "insufficient balance"))


def _estimate_cost(normalized: dict[str, Any], costs: list[dict[str, Any]]) -> float:
    """Use observed credits per second when exact media counts have no quote.

    The fallback is limited to observations with the same model, resolution,
    video count and audio count. It applies only when several durations show a
    stable rate, so image-count variants cannot invent a price from one task.
    """
    comparable = [item for item in costs if
                  item["model"] == normalized["model"] and
                  item["resolution"] == normalized["resolution"] and
                  item["video_count"] == len(normalized["videos"]) and
                  item["audio_count"] == len(normalized["audios"])]
    exact = [item for item in comparable if
             item["duration"] == normalized["duration"] and
             item["image_count"] == len(normalized["images"])]
    if exact:
        return max(float(item["cost"]) for item in exact)
    observations = [item for item in comparable if
                    float(item["cost"]) > 0 and int(item["duration"]) > 0]
    if len(observations) < 3 or len({item["duration"] for item in observations}) < 2:
        return 0.0
    rates = [float(item["cost"]) / int(item["duration"]) for item in observations]
    rate = statistics.median(rates)
    if any(abs(sample - rate) > rate * 0.05 for sample in rates):
        return 0.0
    return round(rate * int(normalized["duration"]), 2)


DEFAULT_SETTINGS: dict[str, Any] = {
    "task_workers": 4,
    "task_queue_capacity": 100,
    "poll_interval_seconds": 8,
    "task_timeout_seconds": 1800,
    "account_maintenance_interval_seconds": 300,
    "daily_checkin_enabled": False,
    "chrome_executable": os.getenv("ELE_CHROME_EXECUTABLE", "/usr/bin/chromium" if os.name != "nt" else "chrome.exe"),
    "chrome_user_data_root": os.getenv("ELE_CHROME_USER_DATA_ROOT", "/app/data/chrome-profiles"),
    "low_balance_disable_threshold": 0,
    "allow_video_reference_inputs": True,
    "excess_media_policy": "reject",
    "prompt_media_reference_cleanup_enabled": True,
    "strict_prompt_media_references": False,
    "use_default_reference_video": True,
    "default_reference_video_resolution": "480p",
    "automatic_proxy_assignment": True,
    "automatic_proxy_base": "socks5://host.docker.internal",
}


class Service:
    def __init__(self, store: Store):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=50, thread_name_prefix="ele-task")
        self._login_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ele-login")
        self._lock = threading.RLock()
        self._active: dict[int, int] = {}
        self._claimed_tasks: set[str] = set()
        self._login_pending: set[int] = set()
        self._login_results: dict[int, dict[str, Any]] = {}
        self._proxy_cursor = 0
        self._worker_slots = threading.BoundedSemaphore(50)
        self._started = False
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self.browser_runtime = BrowserRuntime(store, self.settings)
        self._browser_views: dict[int, tuple[dict[str, Any], bool, float]] = {}
        self._browser_views_lock = threading.RLock()

    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **self.store.get_settings()}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        filtered = {key: value for key, value in values.items() if key in allowed}
        for key in ("strict_prompt_media_references", "use_default_reference_video", "allow_video_reference_inputs",
                    "automatic_proxy_assignment"):
            if key in filtered and not isinstance(filtered[key], bool):
                raise ValueError(f"{key} must be a boolean")
        if ("default_reference_video_resolution" in filtered
                and filtered["default_reference_video_resolution"] not in REFERENCE_RESOLUTIONS):
            raise ValueError("default_reference_video_resolution must be 480p, 720p or 1080p")
        if "automatic_proxy_base" in filtered:
            self._proxy_candidates(str(filtered["automatic_proxy_base"]))
        limits = {"task_workers": (1, 50), "task_queue_capacity": (0, 10000),
                  "poll_interval_seconds": (2, 120), "task_timeout_seconds": (60, 7200),
                  "account_maintenance_interval_seconds": (30, 86400)}
        for key, (minimum, maximum) in limits.items():
            if key in filtered:
                if isinstance(filtered[key], bool) or not minimum <= int(filtered[key]) <= maximum:
                    raise ValueError(f"invalid {key}")
                filtered[key] = int(filtered[key])
        self.store.update_settings(filtered)
        return self.settings()

    def start(self) -> None:
        for account in self.store.accounts(private=True):
            if account["status"] in {"login_pending", "logging_in"}:
                self.store.update_account(account["id"], {"status": "login_required",
                    "last_error": "自动登录中断，请重试登录或完成网页登录"})
        self._started = True
        self._maintenance = threading.Thread(target=self._maintain_accounts, daemon=True,
                                             name="ele-account-maintenance")
        self._maintenance.start()
        for task in reversed(self.store.tasks(1000)):
            if task["status"] == "queued":
                self._pool.submit(self._run, task["id"])
            elif task["status"] in {"submitted", "running"} and task["run_id"] and task["thread_id"]:
                self._pool.submit(self._resume_poll, task["id"])
            elif task["status"] == "preparing":
                if int(task["progress"]) < 30 and not task["run_id"]:
                    # Reference upload has not reached the generation RPC. It is
                    # safe to repeat after a restart, including an interrupted
                    # browser upload; the external task ID stays the same.
                    self.store.update_task(task["id"], {"status": "queued", "progress": 0,
                        "account_id": None, "error_code": "", "error_message": ""})
                    self._pool.submit(self._run, task["id"])
                else:
                    self.store.update_task(task["id"], {"status": "submission_unknown", "progress": 35,
                        "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                        "error_message": "service restarted during upstream submission; automatic resubmission is disabled",
                        "completed_at": time.time()})

    def stop(self) -> None:
        self._stop.set()
        self._login_pool.shutdown(wait=False, cancel_futures=True)
        if self._maintenance and self._maintenance.is_alive():
            self._maintenance.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=False)
        for account_id in list(self._browser_views):
            self.close_browser_view(account_id)
        self.browser_runtime.close_all_idle()

    @staticmethod
    def _proxy_candidates(base: str) -> list[str]:
        try:
            parsed = urlsplit(base.strip())
            if (parsed.scheme not in {"socks5", "http", "https"} or not parsed.hostname
                    or parsed.port is not None or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError
        except ValueError as exc:
            raise ValueError("automatic_proxy_base must contain only proxy scheme and hostname") from exc
        host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
        return [urlunsplit((parsed.scheme, f"{host}:{port}", "", "", "")) for port in AUTO_PROXY_PORTS]

    def add_account(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = dict(body)
        with self._lock:
            if not str(payload.get("proxy_url") or "").strip():
                old = next((account for account in self.store.accounts(private=True)
                            if account["name"] == str(payload.get("name") or "").strip()), None)
                if old and old["proxy_url"]:
                    payload["proxy_url"] = old["proxy_url"]
                elif self.settings()["automatic_proxy_assignment"]:
                    candidates = self._proxy_candidates(self.settings()["automatic_proxy_base"])
                    counts = {proxy: 0 for proxy in candidates}
                    for account in self.store.accounts():
                        if account["proxy_url"] in counts:
                            counts[account["proxy_url"]] += 1
                    minimum = min(counts.values())
                    for offset in range(len(candidates)):
                        index = (self._proxy_cursor + offset) % len(candidates)
                        if counts[candidates[index]] == minimum:
                            payload["proxy_url"] = candidates[index]
                            self._proxy_cursor = (index + 1) % len(candidates)
                            break
            if not str(payload.get("proxy_url") or "").strip():
                raise ValueError("account proxy is required; provide one or enable automatic assignment")
            return self.store.upsert_account(payload)

    def login_in_progress(self, account_id: int) -> bool:
        with self._lock:
            return account_id in self._login_pending

    def create_pending_account(self, name: str, proxy_url: str, max_concurrency: int = 1) -> dict[str, Any]:
        with self._lock:
            if any(account["name"] == name.strip() for account in self.store.accounts()):
                raise ValueError("account name already exists")
            account = self.add_account({"name": name, "proxy_url": proxy_url,
                                        "max_concurrency": max_concurrency, "enabled": False})
            return self.store.update_account(account["id"], {"managed_profile": True, "status": "pending"})

    def login_results(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(value) for value in self._login_results.values()]

    def queue_account_login(self, account_id: int) -> dict[str, Any]:
        with self._lock:
            if account_id in self._login_pending:
                return {**self._login_results[account_id], "already_queued": True}
            if len(self._login_pending) >= MAX_LOGIN_JOBS:
                raise RuntimeError("automatic login queue is full")
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            if account["status"] == "logging_in" or self._active.get(account_id, 0) or self.store.unresolved_account_count(account_id):
                raise ValueError("account has an active login or unresolved task")
            if not account.get("login_email") or not account.get("login_password"):
                raise ValueError("saved login email and password are required")
            if not account["proxy_url"]:
                raise ValueError("account proxy is required")
            result = {"account_id": account_id, "status": "queued", "phase": "queued", "message": "已加入自动登录队列",
                      "updated_at": time.time()}
            self._login_results[account_id] = result
            self._login_pending.add(account_id)
            self.store.update_account(account_id, {"status": "login_pending", "enabled": False, "last_error": ""})
            try:
                self._login_pool.submit(self._run_account_login, account_id)
            except RuntimeError:
                self._login_pending.discard(account_id)
                self.store.update_account(account_id, {"status": "login_required"})
                raise
            return dict(result)

    def _run_account_login(self, account_id: int) -> None:
        account, owned = None, False
        phase = "browser_start"
        browser_attempts = onboarding_resumes = 0
        try:
            if self._stop.is_set():
                raise BrowserError("automatic login stopped")
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            # Acquire before logging_in; the normal runtime protects that state
            # against competing manual login and generation operations.
            with self._lock:
                self._login_results[account_id].update(status="running", phase=phase,
                    message="正在初始化账号浏览器", updated_at=time.time())
            for attempt in range(2):
                browser_attempts += 1
                try:
                    account, owned = self.browser_runtime.acquire(account, force_managed=True)
                    break
                except BrowserError as error:
                    if attempt or not _retryable_login_browser_start(error):
                        raise
                    with self._lock:
                        self._login_results[account_id].update(
                            phase=phase, message="浏览器暂未就绪，稍后进行最后一次初始化检查", updated_at=time.time())
                    if self._stop.wait(2):
                        raise BrowserError("automatic login stopped") from error
            self.store.update_account(account_id, {"status": "logging_in"})
            phase = "login"
            with self._lock:
                self._login_results[account_id].update(status="running", phase=phase,
                    message="正在网页登录并自动完成年龄确认和偏好设置", updated_at=time.time())
            browser = CDPBrowser(int(account["cdp_port"]))
            try:
                auth = browser.login_with_password(str(account["login_email"]), str(account["login_password"]))
            except BrowserError as error:
                if not str(error).startswith(("ONBOARDING_LOADING_TIMEOUT:", "ONBOARDING_NETWORK_ERROR:")):
                    raise
                # These markers explicitly establish a signed-in profile and a
                # transient setup failure. Resume only setup, never sign-in.
                phase = "onboarding"
                with self._lock:
                    self._login_results[account_id].update(phase=phase,
                        message="已登录，年龄或偏好页面暂未就绪，稍后继续一次", updated_at=time.time())
                if self._stop.wait(2):
                    raise BrowserError("automatic login stopped") from error
                onboarding_resumes = 1
                auth = browser.complete_onboarding(expected_email=str(account["login_email"]))
            if not auth.get("access_token"):
                raise BrowserRejected("LOGIN_FAILED")
            self.store.update_account(account_id, {key: auth[key] for key in ACCOUNT_AUTH_FIELDS if key in auth})
            self.store.clear_media_cache(account_id)
            phase = "account_verification"
            with self._lock:
                self._login_results[account_id].update(phase=phase,
                    message="正在确认服务端登录设置与额度", updated_at=time.time())
            checked = self._check_account_live(self.store.account(account_id, private=True), login_owner=True)
            if checked["status"] not in {"active", "disabled_low_balance"}:
                status = checked["status"]
                message = checked["last_error"]
                return
            if checked["status"] == "active":
                self.store.update_account(account_id, {"enabled": True})
            status = checked["status"]
            phase = "complete"
            message = ("登录、年龄确认、偏好设置及额度验证成功" if status == "active"
                       else "登录及年龄偏好设置完成，余额低于停用阈值")
        except Exception as error:
            if str(error).startswith("ONBOARDING_"):
                phase = "onboarding"
            status, message = account_login_failure(error, phase=phase)
            if self.store.account(account_id):
                self.store.update_account(account_id, {"status": status, "last_error": message, "enabled": False})
        finally:
            if account is not None:
                self.browser_runtime.release(account, owned)
            with self._lock:
                self._login_pending.discard(account_id)
                self._login_results.setdefault(account_id, {"account_id": account_id}).update(
                    status=locals().get("status", "login_required"),
                    phase=phase, browser_attempts=browser_attempts, onboarding_resume_attempts=onboarding_resumes,
                    message=locals().get("message", "自动登录未完成，请重试或使用网页登录"), updated_at=time.time())

    def batch_import(self, text: str) -> dict[str, Any]:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) > MAX_LOGIN_JOBS:
            raise ValueError("one batch supports at most 100 accounts")
        results, errors = [], []
        duplicates = started = 0
        for number, line in enumerate(lines, 1):
            parts = re.split(r"\t|----|\|", line.strip(), maxsplit=2)
            if len(parts) < 2 or "@" not in parts[0] or not parts[1]:
                errors.append(f"line {number}: expected email, password and optional proxy")
                continue
            email, password = parts[0].strip(), parts[1]
            existing = next((account for account in self.store.accounts(private=True)
                             if account.get("login_email", "").lower() == email.lower()), None)
            name = existing["name"] if existing else "账号-" + uuid.uuid5(uuid.NAMESPACE_URL, email.lower()).hex[:10]
            try:
                payload = {"name": name, "login_email": email, "login_password": password,
                           "proxy_url": parts[2].strip() if len(parts) > 2 else "", "max_concurrency": 1}
                account = self.add_account(payload)
                duplicates += bool(existing)
                job = self.queue_account_login(account["id"])
                started += not bool(job.get("already_queued"))
                results.append({"line": number, "account_id": account["id"], "status": job["status"]})
            except (ValueError, RuntimeError, KeyError):
                errors.append(f"line {number}: account import or automatic login could not be queued")
        return {"input_count": len(lines), "count": len(results), "duplicate_count": duplicates,
                "login_started_count": started, "results": results, "errors": errors}

    def open_browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id in self._browser_views:
                account, owned, _ = self._browser_views[account_id]
                self._browser_views[account_id] = (account, owned, time.monotonic())
                return account
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            current, owned = self.browser_runtime.acquire(account)
            self._browser_views[account_id] = (current, owned, time.monotonic())
            return current

    def browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id not in self._browser_views:
                raise BrowserError("account browser view is closed")
            account, owned, _ = self._browser_views[account_id]
            self._browser_views[account_id] = (account, owned, time.monotonic())
            return account

    def close_browser_view(self, account_id: int) -> None:
        with self._browser_views_lock:
            entry = self._browser_views.pop(account_id, None)
        if entry:
            self.browser_runtime.release(entry[0], entry[1])

    def _maintain_accounts(self) -> None:
        while not self._stop.is_set():
            with self._browser_views_lock:
                expired = [account_id for account_id, (_, _, touched) in self._browser_views.items()
                           if time.monotonic() - touched > 20 * 60]
            for account_id in expired:
                self.close_browser_view(account_id)
            settings = self.settings()
            needs_rapid_recheck = False
            for account in self.store.accounts(private=True):
                if self._stop.is_set():
                    break
                if not account["enabled"] or not (int(account["cdp_port"] or 0)
                        or account.get("access_token") or account.get("refresh_token")):
                    continue
                try:
                    checked = self.check_account(int(account["id"]))
                    if checked["status"] in {"login_required", "network_error"}:
                        needs_rapid_recheck = True
                    if checked["status"] == "active" and settings["daily_checkin_enabled"]:
                        self.checkin_account(int(account["id"]))
                except (BrowserError, EleError, ValueError, KeyError):
                    needs_rapid_recheck = True
                    continue
            interval = max(30, int(settings["account_maintenance_interval_seconds"]))
            # A restarted Chrome often exposes its tab before the app can make
            # authenticated requests. Retry that state promptly instead of
            # leaving a valid account unavailable for the full interval.
            self._stop.wait(min(15, interval) if needs_rapid_recheck else interval)

    def checkin_account(self, account_id: int) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        today = datetime.now(ZoneInfo(os.getenv("ELE_TIME_ZONE", "Asia/Taipei"))).date().isoformat()
        if account.get("last_checkin_date") == today:
            return {"already_checked_in": True, "date": today}
        account, owned = self.browser_runtime.acquire(account)
        client = None
        try:
            client = EleClient(account, store=self.store)
            result = client.daily_checkin()
        finally:
            if client is not None:
                client.close()
            self.browser_runtime.release(account, owned)
        if result.get("supported") is False or result.get("success") is False or result.get("manual_required") or result.get("status") not in {"claimed", "already_claimed"}:
            return {**result, "date": today}
        self.store.update_account(account_id, {"last_checkin_date": today})
        return {"already_checked_in": False, "date": today,
                "received": result.get("receive_quota", 0)}

    def create(self, request: dict[str, Any], *, idempotency_key: str = "") -> dict[str, Any]:
        if idempotency_key:
            existing = self.store.idempotent_task(idempotency_key, request)
            if existing:
                return self.public_task(existing)
        if {"login_password", "login_email", "password", "access_token", "refresh_token"} & request.keys():
            raise ValueError("account credentials must use account management endpoints")
        settings = self.settings()
        normalized = normalize_request(
            request, strict_prompt_media_references=settings["strict_prompt_media_references"],
            use_default_reference_video=settings["use_default_reference_video"],
            default_reference_video_resolution=settings["default_reference_video_resolution"])
        caller_videos = len(normalized["videos"]) - int(bool(normalized.get("fixed_reference_video")))
        if not settings["allow_video_reference_inputs"] and caller_videos:
            raise ValueError("video reference inputs are disabled by service settings")
        if normalized.get("fixed_reference_video"):
            clip = normalized["default_reference_video"]
            ensure_reference_video(clip["resolution"], clip["aspect_ratio"])
        estimated_cost = _estimate_cost(normalized, self.store.model_costs())
        with self._lock:
            # Idempotent recovery precedes capacity checks, including a full queue.
            if idempotency_key:
                existing = self.store.idempotent_task(idempotency_key, request)
                if existing:
                    return self.public_task(existing)
            if not self._has_eligible_account(estimated_cost, set()):
                raise AccountPoolUnavailable("no available ElevenLabs account")
            if self.store.active_count() >= int(settings["task_workers"]) + int(settings["task_queue_capacity"]):
                raise RuntimeError("ELEAPI queue is full")
            task_id = "ele_" + uuid.uuid4().hex[:16]
            task = self.store.create_task(task_id, request, normalized, idempotency_key=idempotency_key)
        if task["id"] != task_id:
            return self.public_task(task)
        if estimated_cost:
            task = self.store.update_task(task_id, {"estimated_cost": estimated_cost})
        self._pool.submit(self._run, task_id)
        return self.public_task(task)

    def _reserve_account(self, estimated_cost: float = 0, *,
                         exclude_ids: set[int] | None = None) -> dict[str, Any] | None:
        excluded = exclude_ids or set()
        with self._lock:
            if sum(self._active.values()) >= int(self.settings()["task_workers"]):
                return None
            accounts = [item for item in self.store.accounts(private=True)
                        if int(item["id"]) not in excluded and item["enabled"]
                        and item["status"] == "active" and item["proxy_url"]
                        and (item["balance"] is None or float(item["balance"]) >= estimated_cost)]
            accounts.sort(key=lambda item: (-int(item["priority"]), -(float(item["balance"] or 0)), item["id"]))
            for account in accounts:
                account_id = int(account["id"])
                active = self._active.get(account_id, 0)
                if active >= int(account["max_concurrency"]):
                    continue
                self._active[account_id] = active + 1
                return account
        return None

    def _has_eligible_account(self, estimated_cost: float, excluded: set[int]) -> bool:
        return any(int(item["id"]) not in excluded and item["enabled"]
                   and item["status"] == "active" and item["proxy_url"]
                   and (item["balance"] is None or float(item["balance"]) >= estimated_cost)
                   for item in self.store.accounts(private=True))

    def _release_account(self, account_id: int) -> None:
        with self._lock:
            self._active[account_id] = max(self._active.get(account_id, 1) - 1, 0)

    def check_account(self, account_id: int, *, start_browser: bool = False,
                      complete_onboarding: bool = False) -> dict[str, Any]:
        if self.login_in_progress(account_id):
            raise BrowserError("account automatic login is in progress")
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        enable_after = False
        if complete_onboarding:
            # Reserve this identity before touching the page. Other accounts'
            # tasks and normal read-only maintenance keep their own leases.
            with self._lock:
                account = self.store.account(account_id, private=True)
                if (not account or self.login_in_progress(account_id) or account["status"] == "logging_in"
                        or self._active.get(account_id, 0) or self.store.unresolved_account_count(account_id)):
                    raise BrowserError("account has an active login or unresolved task")
                account, owned = self.browser_runtime.acquire(account, force_managed=start_browser)
                enable_after = bool(account["enabled"] or account["status"] in {
                    "pending", "login_required", "verification_required", "onboarding_required", "login_pending"})
                self.store.update_account(account_id, {"status": "logging_in", "enabled": False})
        else:
            account, owned = self.browser_runtime.acquire(account, force_managed=start_browser)
        try:
            if complete_onboarding:
                try:
                    auth = CDPBrowser(int(account["cdp_port"])).complete_onboarding(
                        expected_email=str(account.get("login_email") or ""))
                    if not auth.get("access_token"):
                        raise BrowserRejected("LOGIN_REQUIRED")
                    self.store.update_account(account_id, {key: auth[key] for key in ACCOUNT_AUTH_FIELDS if key in auth})
                    account = self.store.account(account_id, private=True)
                except BrowserError as error:
                    status, message = account_login_failure(error)
                    return self.store.update_account(account_id, {"status": status, "last_error": message, "enabled": False})
            checked = self._check_account_live(account, login_owner=complete_onboarding)
            if complete_onboarding and checked["status"] == "active":
                checked = self.store.update_account(account_id, {"enabled": enable_after})
            return checked
        finally:
            self.browser_runtime.release(account, owned)

    def _check_account_live(self, account: dict[str, Any], *, login_owner: bool = False) -> dict[str, Any]:
        account_id = int(account["id"])

        def update_checked(values: dict[str, Any]) -> dict[str, Any]:
            with self._lock:
                current = self.store.account(account_id)
                if not login_owner and current and current["status"] in {"logging_in", "login_pending"}:
                    # A background request started before the login reservation
                    # cannot publish its stale readiness result over that login.
                    return current
                return self.store.update_account(account_id, values)

        # Chrome exposes a CDP page before ElevenLabs's JavaScript has finished
        # loading. After a service restart, one early fetch failure should not
        # put an otherwise valid account into login_required for an entire
        # maintenance interval. Complete HTTP rejections still fail promptly.
        for attempt in range(3):
            client = None
            try:
                client = EleClient(account, store=self.store)
                if not client.onboarding_complete():
                    return update_checked({
                        "status": "onboarding_required", "enabled": False,
                        "last_error": "登录后设置尚未完成，请重试完成年龄确认和偏好设置"})
                state = client.account_state()
                break
            except BrowserRejected as exc:
                if re.match(r"ElevenLabs HTTP (?:408|425|429|5\d\d):", str(exc)):
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    return update_checked({"status": "network_error",
                                           "last_error": "上游暂时不可用，请稍后重试登录验证"})
                status, message = account_login_failure(exc)
                return update_checked({"status": status, "enabled": False, "last_error": message})
            except BrowserError as exc:
                if attempt == 2:
                    status, message = account_login_failure(exc)
                    values = {"status": status, "last_error": message}
                    if any(marker in str(exc).upper() for marker in (
                            "LOGIN_REQUIRED", "ONBOARDING_REQUIRED", "CAPTCHA_REQUIRED")):
                        values["enabled"] = False
                    return update_checked(values)
                time.sleep(1)
            finally:
                if client is not None:
                    client.close()
        balance = state["available_balance"]
        threshold = float(self.settings()["low_balance_disable_threshold"] or 0)
        values: dict[str, Any] = {"balance": balance, "balance_details": state["buckets"],
                                  "status": "active", "last_error": ""}
        if threshold > 0 and balance is not None and balance < threshold:
            values.update({"enabled": False, "status": "disabled_low_balance",
                           "last_error": f"credit balance below {threshold}"})
        return update_checked(values)

    def _run(self, task_id: str) -> None:
        # Multiple dispatcher/recovery calls must never turn one queued task
        # into concurrent generation requests, even while preparation overlaps.
        with self._lock:
            task = self.store.task(task_id)
            if not task or task["status"] != "queued" or task_id in self._claimed_tasks:
                return
            self._claimed_tasks.add(task_id)
        try:
            self._run_claimed(task_id)
        finally:
            with self._lock:
                self._claimed_tasks.discard(task_id)

    def _run_claimed(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or task["status"] != "queued":
            return
        estimated_cost = float(task.get("estimated_cost") or 0)
        if estimated_cost <= 0:
            estimated_cost = _estimate_cost(task["normalized"], self.store.model_costs())
            if estimated_cost > 0:
                self.store.update_task(task_id, {"estimated_cost": estimated_cost})
        attempted = list(task["attempted_account_ids"])
        credit_reason = task.get("error_message") or "ElevenLabs account has insufficient credits"
        unavailable_code = "INSUFFICIENT_CREDITS" if attempted else "ACCOUNT_POOL_UNAVAILABLE"
        unavailable_message = credit_reason if attempted else "no available ElevenLabs account"
        if not self._has_eligible_account(estimated_cost, set(attempted)):
            self._fail(task_id, unavailable_code, unavailable_message)
            return
        # The executor is only a waiter/dispatcher.  The configurable limit
        # applies to actual upstream work, not to the number of Python threads.
        while not self._stop.is_set():
            current = self.store.task(task_id)
            if not current or current["status"] != "queued":
                return
            if not self._has_eligible_account(estimated_cost, set(attempted)):
                self._fail(task_id, unavailable_code, unavailable_message)
                return
            if not self._worker_slots.acquire(timeout=1):
                continue
            with self._lock:
                if sum(self._active.values()) < int(self.settings()["task_workers"]):
                    break
            self._worker_slots.release()
            time.sleep(1)
        else:
            return
        try:
            while not self._stop.is_set():
                current = self.store.task(task_id)
                if not current or current["status"] != "queued":
                    return
                if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                    self._fail(task_id, "INSUFFICIENT_CREDITS", credit_reason)
                    return
                account = None
                account_deadline = time.monotonic() + int(self.settings()["task_timeout_seconds"])
                while not self._stop.is_set() and time.monotonic() < account_deadline:
                    account = self._reserve_account(estimated_cost, exclude_ids=set(attempted))
                    if account or not self._has_eligible_account(
                            estimated_cost, set(attempted)):
                        break
                    self._stop.wait(1)
                if not account:
                    code = "INSUFFICIENT_CREDITS" if attempted else "ACCOUNT_POOL_UNAVAILABLE"
                    message = credit_reason if attempted else "no available ElevenLabs account"
                    self._fail(task_id, code, message)
                    return
                account_id = int(account["id"])
                owned = False
                client = None
                try:
                    if self._stop.is_set():
                        return
                    self.store.update_task(task_id, {"account_id": account_id,
                        "status": "preparing", "progress": 5, "balance_before": None,
                        "run_id": "", "thread_id": "", "error_code": "", "error_message": "",
                        "upstream_request": {}})
                    # Only profile/auth acquisition is account-serialized. Each
                    # task then uses its own client/session for preparation and submit.
                    with account_media_lock(account_id):
                        account, owned = self.browser_runtime.acquire(account)
                        client = EleClient(account, store=self.store)
                    # A just-started Chrome can expose CDP before the app is ready.
                    for attempt in range(12):
                        try:
                            before = client.account_state()["available_balance"]
                            break
                        except BrowserError as exc:
                            if "JavaScript request failed" not in str(exc) or attempt == 11:
                                raise
                            if self._stop.wait(2):
                                return
                    self.store.update_account(account_id, {"balance": before, "status": "active"})
                    self.store.update_task(task_id, {"balance_before": before, "progress": 12})
                    assets = client.upload_inputs(task["normalized"])
                    self.store.update_task(task_id, {"cost_evidence": input_evidence(assets)})
                    self.store.update_task(task_id, {"progress": 30})
                    attempted.append(account_id)
                    self.store.update_task(task_id, {"attempted_account_ids": attempted})
                    try:
                        submitted = client.submit(task["normalized"], assets)
                    except BrowserRejected as exc:
                        if not _credit_rejected(str(exc)):
                            self._fail(task_id, "UPSTREAM_REJECTED", str(exc))
                            return
                        raise AccountCreditRejected(str(exc)) from exc
                    except BrowserError as exc:
                        self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                                   str(exc) + "; automatic resubmission is disabled")
                        return
                    finally:
                        audit = getattr(client, "last_submit_request", None)
                        if isinstance(audit, dict) and audit:
                            self.store.update_task(task_id, {"upstream_request": audit,
                                "thread_id": str((audit.get("query") or {}).get("thread_id") or "")})
                    self.store.update_task(task_id, {**cost_values(submitted), "status": "submitted", "progress": 40,
                                                  "upstream_request": submitted.get("upstream_request") or {},
                                                  "run_id": submitted["run_id"],
                                                  "thread_id": submitted["thread_id"]})
                    self._poll(task_id, client,
                               time.monotonic() + int(self.settings()["task_timeout_seconds"]))
                    return
                except AccountCreditRejected as exc:
                    credit_reason = str(exc)
                    self.store.update_account(account_id, {"last_error": credit_reason[:300]})
                    logger.warning("ELE task %s credit rejection on account %s (attempt %s/%s)",
                                   task_id, account_id, len(attempted), MAX_CREDIT_ACCOUNT_ATTEMPTS)
                    if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                        self._fail(task_id, "INSUFFICIENT_CREDITS", credit_reason)
                        return
                    self.store.update_task(task_id, {"status": "queued", "progress": 0,
                        "account_id": None, "run_id": "", "thread_id": "",
                        "balance_before": None, "error_code": "INSUFFICIENT_CREDITS",
                        "error_message": credit_reason})
                    continue
                except (BrowserError, EleError, MediaError, ValueError) as exc:
                    self._fail(task_id, type(exc).__name__.upper(), str(exc))
                    return
                except Exception as exc:
                    self._fail(task_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
                    return
                finally:
                    if client is not None:
                        client.close()
                    self.browser_runtime.release(account, owned)
                    self._release_account(account_id)
        finally:
            self._worker_slots.release()

    def _resume_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task["account_id"]:
            return
        account = self.store.account(task["account_id"], private=True)
        if not account:
            self._fail(task_id, "ACCOUNT_MISSING", "task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(task["account_id"])] = self._active.get(int(task["account_id"]), 0) + 1
        owned = False
        client = None
        retry_on_other_account = False
        try:
            account, owned = self.browser_runtime.acquire(account)
            client = EleClient(account, store=self.store)
            self._poll(task_id, client,
                       time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except AccountCreditRejected as exc:
            attempted = list(task["attempted_account_ids"])
            if int(task["account_id"]) not in attempted:
                attempted.append(int(task["account_id"]))
            reason = str(exc)
            self.store.update_account(int(task["account_id"]), {"last_error": reason[:300]})
            if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                self._fail(task_id, "INSUFFICIENT_CREDITS", reason)
            else:
                self.store.update_task(task_id, {"status": "queued", "progress": 0,
                    "account_id": None, "run_id": "", "thread_id": "",
                    "attempted_account_ids": attempted, "balance_before": None,
                    "error_code": "INSUFFICIENT_CREDITS", "error_message": reason})
                retry_on_other_account = True
        except Exception as exc:
            self._fail(task_id, type(exc).__name__.upper(), str(exc))
        finally:
            if client is not None:
                client.close()
            self.browser_runtime.release(account, owned)
            self._release_account(int(task["account_id"]))
            self._worker_slots.release()
        if retry_on_other_account and not self._stop.is_set():
            self._pool.submit(self._run, task_id)

    def resume(self, task_id: str) -> dict[str, Any]:
        """Resume queries for a confirmed upstream job; never repeat generation."""
        with self._lock:
            task = self.store.task(task_id)
            if not task:
                raise KeyError(task_id)
            if task["status"] not in {"submission_unknown", "expired"}:
                raise ValueError("only interrupted task queries can be resumed")
            if not task.get("run_id") or not task.get("thread_id") or not task.get("account_id"):
                raise ValueError("no confirmed upstream task ID; inspect the original account before retrying")
            account = self.store.account(int(task["account_id"]), private=True)
            if not account:
                raise ValueError("the original account no longer exists")
            task = self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                "completed_at": None, "error_code": "", "error_message": ""})
            self._pool.submit(self._resume_poll, task_id)
            return self.public_task(task)

    def _poll(self, task_id: str, client: EleClient, deadline: float) -> None:
        errors = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            task = self.store.task(task_id)
            if not task or task["status"] in TERMINAL:
                return
            try:
                state = client.poll(task["run_id"], task["thread_id"])
                errors = 0
            except (BrowserError, EleError):
                errors += 1
                if errors >= 8:
                    self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                               "upstream polling unavailable; the accepted task may still finish")
                    return
                time.sleep(min(60, 2 ** errors))
                continue
            if state["status"] == "succeeded":
                if not state["result_urls"]:
                    errors += 1
                    if errors < 4:
                        time.sleep(3)
                        continue
                    self._fail(task_id, "RESULT_MISSING", "ElevenLabs completed without a video URL")
                    return
                after = None
                try:
                    after = client.account_state()["available_balance"]
                    self.store.update_account(int(task["account_id"]), {"balance": after})
                except BrowserError:
                    pass
                self.store.update_task(task_id, {"status": "succeeded", "progress": 100,
                    "result_urls": state["result_urls"], "balance_after": after,
                    "completed_at": time.time(), **cost_values(state)})
                return
            if state["status"] == "failed":
                reason = state.get("error") or state.get("error_message") or "ElevenLabs video generation failed"
                if _credit_rejected(reason):
                    raise AccountCreditRejected(reason)
                code = state.get("error_code") or "UPSTREAM_FAILED"
                self.store.update_task(task_id, cost_values(state))
                self._fail(task_id, code, reason)
                return
            self.store.update_task(task_id, {"status": "running", "progress": max(45, task["progress"])})
            time.sleep(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set():
            self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                       "polling deadline reached; the accepted upstream task may continue; resume queries only")

    def _fail(self, task_id: str, code: str, message: str) -> None:
        current = self.store.task(task_id) or {}
        if current.get("run_id") and code in {"INTERNAL_ERROR", "BROWSERERROR", "ELEERROR", "VALUEERROR", "RESULT_MISSING"}:
            code = "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
        unknown = code == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"
        self.store.update_task(task_id, {"status": "submission_unknown" if unknown else "failed", "progress": 35 if unknown else 100,
            "error_code": code, "error_message": message[:600], "completed_at": time.time()})

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        result = {"id": task["id"], "object": "video", "created_at": task["created_at"],
                  "status": task["status"], "model": task["model"], "progress": task["progress"],
                  "data": [{"type": "video", "url": url} for url in task["result_urls"]],
                  "account_id": task.get("account_id")}
        rows = build_observations(task)
        row = rows[-1] if rows else {}
        result["credits_consumed"] = row.get("charged") if row.get("charged") is not None else row.get("quoted")
        result["cost_source"] = row.get("source", "unknown")
        if task["status"] in {"failed", "expired", "submission_unknown"}:
            result["error"] = public_error(task["error_code"], task["error_message"])
        return result
