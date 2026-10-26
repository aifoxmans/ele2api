"""Download anonymous reference sources and use ElevenLabs's asset upload API."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import mimetypes
import math
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests

from .browser import BrowserError, BrowserRejected


MAX_MEDIA_BYTES = 200 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_DOWNLOAD_ATTEMPTS = 3
MAX_DOWNLOAD_REDIRECTS = 3
MIME_EXTENSIONS = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "image/jpeg": ".jpg",
                   "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4",
                   "video/quicktime": ".mov"}


class MediaError(BrowserError):
    pass


def _http_url(source: str):
    try:
        parsed = urlsplit(source)
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not any(ord(char) <= 32 or ord(char) == 127 for char in source))
        parsed.port
    except ValueError as exc:
        raise MediaError("media URL is invalid") from exc
    if not valid:
        raise MediaError("media URL is invalid")
    return parsed


def _public_direct_url(source: str) -> bool:
    """Anonymous direct downloads are limited to public caller media origins."""
    parsed = _http_url(source)
    host = str(parsed.hostname).rstrip(".").lower()
    if (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            or host == "elevenlabs.io" or host.endswith(".elevenlabs.io") or host == "intl-upload.iqiyi.com"):
        return False

    def public(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
            return address.is_global and not address.is_multicast
        except ValueError:
            return False

    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return False
        try:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                           type=socket.SOCK_STREAM)
        except OSError:
            return False
        return bool(addresses) and all(public(row[4][0]) for row in addresses)
    return public(host)


def _exception_classes(error: BaseException) -> list[str]:
    classes: list[str] = []
    seen: set[int] = set()

    def visit(value: Any) -> None:
        if not isinstance(value, BaseException) or id(value) in seen or len(seen) >= 16:
            return
        seen.add(id(value))
        name = type(value).__name__
        if name not in classes:
            classes.append(name)
        visit(value.__cause__)
        visit(value.__context__)
        for arg in value.args:
            visit(arg)

    visit(error)
    return classes


class _DownloadFailure(Exception):
    def __init__(self, details: dict[str, Any], *, retryable: bool = False):
        super().__init__(details["reason"])
        self.details = details
        self.retryable = retryable


def _http_download_attempt(source: str, kind: str, route: str, proxy: str) -> tuple[bytes, str, str]:
    started = time.monotonic()
    current, redirects = source, 0
    details: dict[str, Any] = {"route": route, "stage": "headers", "bytes": 0,
                               "expected": None, "host": str(_http_url(source).hostname), "classes": []}
    session = requests.Session()
    session.trust_env = False
    session.auth = None
    session.proxies.clear()
    proxies = {"http": proxy, "https": proxy} if route == "account_proxy" else {}

    def fail(reason: str, *, retryable: bool = False) -> None:
        details.update(reason=reason, elapsed=round(time.monotonic() - started, 3), redirects=redirects)
        raise _DownloadFailure(dict(details), retryable=retryable)

    try:
        while True:
            details.update(stage="headers", host=str(_http_url(current).hostname))
            if route == "direct" and not _public_direct_url(current):
                fail("direct media destination is not public")
            session.cookies.clear()
            with session.get(current, timeout=(15, 90), stream=True, allow_redirects=False, proxies=proxies,
                             headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}) as response:
                details["http"] = response.status_code
                if response.status_code in {301, 302, 303, 307, 308}:
                    details["stage"] = "redirect"
                    location = response.headers.get("Location")
                    if not location or redirects >= MAX_DOWNLOAD_REDIRECTS:
                        fail("media redirect limit or missing destination")
                    target = urljoin(current, location)
                    _http_url(target)
                    if route == "direct" and not _public_direct_url(target):
                        fail("direct media redirect destination is not public")
                    current = target
                    redirects += 1
                    continue
                if not 200 <= response.status_code < 300:
                    details["stage"] = "http_status"
                    fail("HTTP " + str(response.status_code), retryable=response.status_code in {
                        408, 429, 500, 502, 503, 504})
                details["stage"] = "length_check"
                length = response.headers.get("Content-Length")
                expected = None
                if length is not None:
                    try:
                        expected = int(length)
                    except (TypeError, ValueError):
                        fail("invalid Content-Length")
                    if expected < 0:
                        fail("invalid Content-Length")
                    if expected > MAX_MEDIA_BYTES:
                        fail("reference media exceeds 200 MB")
                encoding = str(response.headers.get("Content-Encoding") or "").strip().lower()
                details["expected"] = expected if encoding in {"", "identity"} else None
                mime = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                if mime in {"text/html", "application/xhtml+xml"}:
                    fail(f"{kind} reference has the wrong content type")
                details["stage"] = "body"
                chunks: list[bytes] = []
                for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    details["bytes"] += len(chunk)
                    if details["bytes"] > MAX_MEDIA_BYTES:
                        fail("reference media exceeds 200 MB")
                    chunks.append(chunk)
                details["stage"] = "length_check"
                if details["expected"] is not None and details["bytes"] != details["expected"]:
                    fail("incomplete response body", retryable=True)
                data = b"".join(chunks)
                if not data:
                    fail("reference media is empty")
                if data.lstrip()[:64].lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                    fail(f"{kind} reference has the wrong content type")
                filename = Path(unquote(_http_url(current).path)).name or kind + MIME_EXTENSIONS.get(mime, ".bin")
                prefix = {"images": "image/", "videos": "video/", "audios": "audio/"}[kind]
                if not mime.startswith(prefix):
                    guessed = mimetypes.guess_type(filename)[0] or ""
                    if not guessed.startswith(prefix):
                        fail(f"{kind} reference has the wrong content type")
                    mime = guessed
                return data, mime, filename
    except requests.RequestException as exc:
        classes = _exception_classes(exc)
        details["classes"] = classes
        reason = ("read timeout" if any(name in classes for name in {"ReadTimeout", "ReadTimeoutError"}) else
                  "connect timeout" if "ConnectTimeout" in classes else
                  "incomplete response body" if any(name in classes for name in {"IncompleteRead", "ChunkedEncodingError"}) else
                  type(exc).__name__)
        fail(reason, retryable=isinstance(exc, (requests.Timeout, requests.ConnectionError,
                                                requests.exceptions.ChunkedEncodingError)))
    finally:
        session.close()


def _download_http(source: str, kind: str, proxy_url: str) -> tuple[bytes, str, str]:
    parsed = _http_url(source)
    if not proxy_url:
        raise MediaError("account proxy is required for media download")
    proxy = proxy_url.replace("socks5://", "socks5h://", 1)
    routes = ["direct", "account_proxy", "direct"] if _public_direct_url(source) else ["account_proxy"] * MAX_DOWNLOAD_ATTEMPTS
    history: list[dict[str, Any]] = []
    for attempt, route in enumerate(routes[:MAX_DOWNLOAD_ATTEMPTS], 1):
        try:
            return _http_download_attempt(source, kind, route, proxy)
        except _DownloadFailure as exc:
            history.append({"attempt": attempt, **exc.details})
            if not exc.retryable or attempt == min(MAX_DOWNLOAD_ATTEMPTS, len(routes)):
                break
            time.sleep(.1)
    source_hash = hashlib.sha256(source.encode()).hexdigest()[:16]
    details = {"kind": kind, "source_sha256": source_hash, "host": str(parsed.hostname), "attempts": history}
    descriptions = []
    for row in history:
        classes = "/".join(row["classes"][:3])
        descriptions.append(f"#{row['attempt']} {row['route']} stage={row['stage']} reason={row['reason']} "
                            f"bytes={row['bytes']} expected={row['expected']} elapsed={row['elapsed']:.2f}s"
                            + (" classes=" + classes if classes else ""))
    error = MediaError(f"reference download failed: kind={kind} host={parsed.hostname} source_sha256={source_hash} "
                       f"attempts={len(history)}; " + "; ".join(descriptions))
    error.details = details
    raise error from None


def _download(source: str, kind: str, proxy_url: str = "") -> tuple[bytes, str, str]:
    if kind not in {"images", "videos", "audios"}:
        raise MediaError("unsupported reference media kind")
    if source.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator or ";base64" not in header:
            raise MediaError("media data URL must be base64 encoded")
        mime = header[5:].split(";", 1)[0].lower()
        if len(payload) > (MAX_MEDIA_BYTES * 4 // 3 + 8):
            raise MediaError("reference media exceeds 200 MB")
        try:
            data = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise MediaError("media data URL contains invalid base64") from exc
        filename = kind + MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
    elif source.startswith(("https://", "http://")):
        return _download_http(source, kind, proxy_url)
    else:
        raise MediaError("media must be an HTTPS URL, HTTP URL, or base64 data URL")
    if not data:
        raise MediaError("reference media is empty")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("reference media exceeds 200 MB")
    prefix = {"images": "image/", "videos": "video/", "audios": "audio/"}[kind]
    if not mime.startswith(prefix):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith(prefix) or data.lstrip().startswith((b"<!DOCTYPE", b"<html")):
            raise MediaError(f"{kind} reference has the wrong content type")
        mime = guessed
    return data, mime, filename


def _uploaded_reference(record: dict[str, Any], filename: str, kind: str, size: int) -> dict[str, Any]:
    asset_id = record.get("id")
    if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 128 or not all(c.isalnum() or c in "_-" for c in asset_id):
        raise MediaError("ElevenLabs upload returned no valid content asset ID")
    url = record.get("content_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise MediaError("ElevenLabs upload returned no HTTPS material URL")
    metadata = record.get("file_properties") or {}
    duration = float(metadata.get("duration_secs") or 0)
    return {"asset_id": asset_id, "content_asset_id": asset_id,
            "name": filename, "url": url, "thumbnailUrl": record.get("thumbnail_url") or "",
            "width": int(metadata.get("width") or 0), "height": int(metadata.get("height") or 0),
            "withAudio": bool(metadata.get("has_audio")), "duration": duration,
            "size": int(metadata.get("size_bytes") or size), "kind": kind, "status": record.get("status")}


def upload_media_bytes(data: bytes, mime: str, filename: str, kind: str, *, client: Any) -> dict[str, Any]:
    filename = Path(filename).name
    suffix = Path(filename).suffix.lower().lstrip(".") or MIME_EXTENSIONS.get(mime, ".bin").lstrip(".")
    if not filename or not Path(filename).suffix:
        filename = kind + "." + suffix
    # Captured multipart bodies contain an `asset` file part and a `name` part.
    # Uploads stay on the authenticated API and its original account proxy.
    record = client.request("POST", "/v1/assets/internal", files={"asset": (filename, data, mime)},
                            data={"name": filename}, timeout=(15, 180))
    if not isinstance(record, dict):
        raise MediaError("ElevenLabs upload returned no material record")
    asset_id = record.get("id")
    if (record.get("status") != "finalized" and isinstance(asset_id, str)
            and asset_id and len(asset_id) <= 128 and all(c.isalnum() or c in "_-" for c in asset_id)):
        for _ in range(10):
            if record.get("status") in {"failed", "error"}:
                raise BrowserRejected("ElevenLabs reference asset processing failed")
            time.sleep(0.5)
            record = client.request("GET", "/v1/assets/internal/" + asset_id, timeout=(10, 20))
            if record.get("id") != asset_id:
                raise MediaError("ElevenLabs asset confirmation ID does not match the upload")
            if record.get("status") == "finalized":
                break
    if record.get("status") != "finalized":
        raise MediaError("ElevenLabs reference asset is not finalized; no generation was submitted")
    return _uploaded_reference(record, filename, kind, len(data))


def upload_media(source: str, kind: str, *, client: Any, proxy_url: str = "") -> dict[str, Any]:
    data, mime, filename = _download(source, kind, proxy_url)
    return upload_media_bytes(data, mime, filename, kind, client=client)


def validate_material_durations(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> None:
    constraints = normalized.get("material_constraints") or {}
    for kind in ("videos", "audios"):
        for item in assets[kind]:
            duration = float(item.get("duration") or 0)
            if not math.isfinite(duration) or duration <= 0:
                raise BrowserRejected(f"{normalized['model']} {kind} reference has no valid duration metadata")
            limits = constraints.get(kind) or {}
            minimum, maximum = limits.get("min_duration"), limits.get("max_duration")
            if minimum is not None and duration < minimum or maximum is not None and duration > maximum:
                raise BrowserRejected(f"{normalized['model']} {kind} reference duration exceeds the model's observed limits")
            if kind == "videos" and limits.get("min_dimension"):
                width, height = int(item.get("width") or 0), int(item.get("height") or 0)
                if min(width, height) < limits["min_dimension"]:
                    raise BrowserRejected(f"{normalized['model']} reference video dimensions are below the model's observed limits")
                ratio = width / height
                if not limits["min_aspect_ratio"] <= ratio <= limits["max_aspect_ratio"]:
                    raise BrowserRejected(f"{normalized['model']} reference video aspect ratio exceeds the model's observed limits")
        total_max = (constraints.get(kind) or {}).get("max_total_duration")
        if total_max is not None and sum(float(i.get("duration") or 0) for i in assets[kind]) > total_max:
            raise BrowserRejected(f"{normalized['model']} {kind} total duration exceeds the model's observed limits")
        total_min = (constraints.get(kind) or {}).get("min_total_duration")
        if total_min is not None and assets[kind] and sum(float(i.get("duration") or 0) for i in assets[kind]) < total_min:
            raise BrowserRejected(f"{normalized['model']} {kind} total duration is below the model's observed limits")
