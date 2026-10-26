import json
import shutil
import subprocess

import pytest

from app.reference_video import (REFERENCE_RATIOS, REFERENCE_RESOLUTIONS,
                                 ensure_reference_video, reference_dimensions,
                                 reference_spec, reference_url)
from app.browser_runtime import BrowserRuntime
from app.store import Store


@pytest.mark.parametrize("resolution", REFERENCE_RESOLUTIONS)
@pytest.mark.parametrize("ratio", REFERENCE_RATIOS)
def test_internal_reference_has_fixed_two_second_resolution_and_ratio_snapshot(resolution, ratio):
    source = reference_url(resolution, ratio)
    assert reference_spec(source) == {"url": source, "duration": 2.0,
                                      "resolution": resolution, "aspect_ratio": ratio}
    width, height = reference_dimensions(resolution, ratio)
    assert min(width, height) == int(resolution[:-1])
    assert width % 2 == height % 2 == 0
    left, right = map(int, ratio.split(":"))
    assert abs(width / height - left / right) < .005


def test_default_reference_rejects_unknown_internal_paths_and_preserves_auto_output_ratio():
    assert reference_spec("ele2api-internal://default-reference-video/../../private") is None
    assert reference_spec(reference_url("480p", "16:9") + "?private=token") is None
    assert reference_url("480p", "auto") == reference_url("480p", "16:9")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required")
def test_generated_default_clip_is_two_seconds_silent_and_matches_resolution(tmp_path):
    path = ensure_reference_video("480p", "9:16", tmp_path)
    original = path.read_bytes()
    assert ensure_reference_video("480p", "9:16", tmp_path).read_bytes() == original
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "stream=codec_type,width,height,codec_name,nb_frames:format=duration",
                             "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=15)
    info = json.loads(result.stdout)
    assert len(info["streams"]) == 1
    stream = info["streams"][0]
    assert stream["codec_type"] == "video" and stream["codec_name"] == "h264"
    assert (stream["width"], stream["height"]) == reference_dimensions("480p", "9:16")
    assert int(stream["nb_frames"]) == 50
    assert abs(float(info["format"]["duration"]) - 2) < .01


def test_firebase_tokens_do_not_bypass_browser_needed_for_normal_captcha(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "ele.db"))
    account = store.upsert_account({"name": "one", "access_token": "private-access",
                                    "refresh_token": "private-refresh", "proxy_url": "socks5://proxy.test:1080"})
    calls = []
    monkeypatch.setattr("app.browser_runtime.start_managed_browser",
                        lambda value, **kwargs: calls.append(value["id"]) or 19301)
    runtime = BrowserRuntime(store, lambda: {"chrome_executable": "fake", "chrome_user_data_root": str(tmp_path)})
    current, owned = runtime.acquire(store.account(account["id"], private=True))
    assert calls == [account["id"]] and current["cdp_port"] == 19301 and owned is True
    runtime.release(current, owned)
