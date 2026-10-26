"""Local two-second neutral reference clips; never website credentials/media."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path


REFERENCE_SECONDS = 2.0
REFERENCE_RESOLUTIONS = ("480p", "720p", "1080p")
REFERENCE_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
URL_PREFIX = "ele2api-internal://default-reference-video"
_LOCK = threading.Lock()


class ReferenceVideoError(RuntimeError):
    pass


def _options(resolution: str, ratio: str) -> tuple[str, str]:
    resolution = str(resolution).lower()
    ratio = "16:9" if ratio in {"auto", "adaptive"} else str(ratio)
    if resolution not in REFERENCE_RESOLUTIONS:
        raise ValueError("default reference video resolution must be 480p, 720p or 1080p")
    if ratio not in REFERENCE_RATIOS:
        raise ValueError("unsupported default reference video aspect ratio")
    return resolution, ratio


def reference_dimensions(resolution: str, ratio: str) -> tuple[int, int]:
    resolution, ratio = _options(resolution, ratio)
    short = int(resolution[:-1])
    left, right = map(int, ratio.split(":"))
    if left >= right:
        return round(short * left / right / 2) * 2, short
    return short, round(short * right / left / 2) * 2


def reference_url(resolution: str, ratio: str) -> str:
    resolution, ratio = _options(resolution, ratio)
    return f"{URL_PREFIX}/{resolution}/{ratio.replace(':', 'x')}"


def reference_spec(source: str) -> dict | None:
    if not isinstance(source, str) or not source.startswith(URL_PREFIX + "/"):
        return None
    parts = source[len(URL_PREFIX) + 1:].split("/")
    if len(parts) != 2:
        return None
    resolution, ratio = parts[0], parts[1].replace("x", ":")
    try:
        resolution, ratio = _options(resolution, ratio)
    except ValueError:
        return None
    if source != reference_url(resolution, ratio):
        return None
    return {"url": source, "duration": REFERENCE_SECONDS,
            "resolution": resolution, "aspect_ratio": ratio}


def _path(resolution: str, ratio: str, data_dir: str | Path | None) -> Path:
    resolution, ratio = _options(resolution, ratio)
    root = Path(data_dir or os.getenv("ELE_DATA_DIR", "/app/data"))
    return root / "reference-videos" / f"white-2s-{resolution}-{ratio.replace(':', 'x')}.mp4"


def _valid(path: Path, resolution: str, ratio: str) -> bool:
    if not path.is_file() or not path.stat().st_size:
        return False
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,codec_name,nb_frames,sample_aspect_ratio:format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=15, check=True)
        info = json.loads(result.stdout)
        stream = info["streams"][0]
        return (len(info["streams"]) == 1 and stream["codec_type"] == "video"
                and (stream["width"], stream["height"]) == reference_dimensions(resolution, ratio)
                and stream["codec_name"] == "h264"
                and stream.get("sample_aspect_ratio", "1:1") in {"1:1", "N/A"}
                and int(stream["nb_frames"]) == 50
                and abs(float(info["format"]["duration"]) - REFERENCE_SECONDS) < .01)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        return False


def ensure_reference_video(resolution: str, ratio: str, data_dir: str | Path | None = None) -> Path:
    resolution, ratio = _options(resolution, ratio)
    width, height = reference_dimensions(resolution, ratio)
    path = _path(resolution, ratio, data_dir)
    with _LOCK:
        if _valid(path, resolution, ratio):
            return path
        temporary = path.with_name(f".{path.stem}-{uuid.uuid4().hex}.mp4")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                 "-i", f"color=c=white:s={width}x{height}:r=25:d=2", "-frames:v", "50", "-an",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", str(temporary)],
                capture_output=True, text=True, timeout=30, check=True)
            if not _valid(temporary, resolution, ratio):
                raise ReferenceVideoError("generated default reference video failed validation")
            os.replace(temporary, path)
            return path
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReferenceVideoError("could not prepare the default two-second reference video") from exc
        finally:
            temporary.unlink(missing_ok=True)


def read_reference(source: str) -> tuple[bytes, str, str]:
    spec = reference_spec(source)
    if spec is None:
        raise ReferenceVideoError("invalid internal default reference video")
    path = ensure_reference_video(spec["resolution"], spec["aspect_ratio"])
    return path.read_bytes(), "video/mp4", path.name
