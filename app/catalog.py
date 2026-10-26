"""ElevenLabs video capabilities observed in the authenticated web configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .reference_video import reference_url


@dataclass(frozen=True)
class ModelSpec:
    id: str
    upstream_model: str
    resolution: str
    family: str
    inferred_alias: str = ""
    supported: bool = True

    @property
    def limits(self) -> dict[str, int]:
        return dict({"2.0": {"images": 9, "videos": 3, "audios": 3},
                     "2.5": {"images": 30, "videos": 10, "audios": 10},
                     "wan": {"images": 10, "videos": 5, "audios": 5},
                     "minimax": {"images": 9, "videos": 3, "audios": 3},
                     "kling": {"images": 10, "videos": 5, "audios": 0},
                     "vidu": {"images": 15, "videos": 0, "audios": 3}}[self.family])

    @property
    def max_duration(self) -> int:
        return {"2.0": 15, "2.5": 30, "wan": 30, "minimax": 15, "kling": 30, "vidu": 16}[self.family]

    @property
    def min_duration(self) -> int:
        return {"wan": 2, "kling": 3, "vidu": 3, "minimax": 5}.get(self.family, 4)

    @property
    def resolutions(self) -> tuple[str, ...]:
        if self.family == "2.0":
            return ("480p", "720p") if self.upstream_model.endswith(("-fast", "-mini")) else ("480p", "720p", "1080p", "4k")
        return {"2.5": ("480p", "720p", "1080p"), "wan": ("480p", "720p", "1080p"),
                "minimax": ("480p", "768p", "2k", "4k"), "kling": ("720p", "1080p"),
                "vidu": ("540p", "720p", "1080p", "2k", "4k")}[self.family]

    @property
    def ratios(self) -> set[str]:
        if self.family == "kling":
            return {"21:9", "16:9", "1:1", "9:16"}
        if self.family == "vidu":
            return {"16:9", "1:1", "9:16", "4:3", "3:4"}
        if self.family == "wan":
            return {"16:9", "4:3", "1:1", "3:4", "9:16"}
        return {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}

    @property
    def material_constraints(self) -> dict[str, Any]:
        if self.family in {"2.0", "2.5"}:
            maximum = 30.1 if self.family == "2.5" else 15.1
            return {"videos": {"max_duration": maximum, "min_total_duration": 2, "max_total_duration": maximum},
                    "audios": {"max_duration": maximum, "max_total_duration": maximum}}
        if self.family == "minimax":
            return {"videos": {"min_duration": 2, "max_duration": 15.1, "max_total_duration": 15.1},
                    "audios": {"min_duration": 2, "max_duration": 15.1, "max_total_duration": 15.1}}
        if self.family == "wan":
            return {kind: {"max_duration": 15, "max_total_duration": 15} for kind in ("videos", "audios")}
        if self.family == "kling":
            return {"videos": {"min_duration": 3, "max_duration": 30.05, "max_total_duration": 30,
                               "min_dimension": 700, "min_aspect_ratio": 0.4, "max_aspect_ratio": 2.5}}
        return {}


MODELS = {spec.id: spec for spec in (
    ModelSpec("sd-2-0", "bytedance-seedance-v2-us", "720p", "2.0"),
    ModelSpec("sd-2-0-1080p", "bytedance-seedance-v2-us", "1080p", "2.0"),
    ModelSpec("sd-2-0-4k", "bytedance-seedance-v2-us", "4k", "2.0"),
    ModelSpec("sd-2-0-fast", "bytedance-seedance-v2-fast", "720p", "2.0"),
    ModelSpec("sd-2-0-mini", "bytedance-seedance-v2-mini", "720p", "2.0"),
    ModelSpec("sd-2-5", "bytedance-seedance-v2.5-us", "720p", "2.5"),
    ModelSpec("sd-2-5-480p", "bytedance-seedance-v2.5-us", "480p", "2.5"),
    ModelSpec("sd-2-5-1080p", "bytedance-seedance-v2.5-us", "1080p", "2.5"),
    ModelSpec("sd-2-5-480p-preview", "bytedance-seedance-v2.5-us", "480p", "2.5",
              "Compatibility alias for current Seedance 2.5 480p; no separate preview model was observed."),
    ModelSpec("wan-3.0", "wan-3.0", "720p", "wan"),
    ModelSpec("wan-3.0-480p", "wan-3.0", "480p", "wan"),
    ModelSpec("wan-3.0-1080p", "wan-3.0", "1080p", "wan"),
    ModelSpec("minimax-h3-768p", "minimax-h3", "768p", "minimax"),
    ModelSpec("kling-4.0", "kling-4", "720p", "kling"),
    ModelSpec("kling-4.0-1080p", "kling-4", "1080p", "kling"),
    ModelSpec("vidu-q4", "", "720p", "vidu", "No Vidu Q4 upstream mapping was observed.", False),
    ModelSpec("vidu-q4-540p", "", "540p", "vidu", "No Vidu Q4 upstream mapping was observed.", False),
)}

RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
ALIASES = re.compile(r"(?:(?P<explicit>@)|(?<![A-Za-z0-9_@]))(?P<kind>视频|video|v|视|图片|image|img|图|音频|audio|音)\s*(?P<index>\d+)(?![A-Za-z0-9_])", re.I)
MEDIA_URL = re.compile(r"https?://[^\s<>\"'\u2018\u2019\u201c\u201d\u3000\u3001\uff0c\u3002\uff1b\uff01\uff1f\uff1a【】（）《》]+", re.I)
REFERENCE_LABEL = re.compile(
    r"参考(?P<zh_reference>视频|图片|音频)|(?P<zh_link>视频|图片|音频)链接|"
    r"reference\s+(?P<english>video|image|audio)\b", re.I)
REFERENCE_JOINERS = re.compile(r"(?:链接|地址|如下|分别|为|是|及|和|或|的|urls?|links?|and|or)", re.I)
MEDIA_EXTENSIONS = {
    "images": {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "heic", "heif", "tif", "tiff"},
    "videos": {"mp4", "mov", "webm", "m4v", "mkv", "avi", "3gp", "flv"},
    "audios": {"mp3", "wav", "m4a", "aac", "ogg", "opus", "flac", "aiff", "aif", "wma"},
}


def _url_spans(prompt: str) -> list[tuple[int, int, str]]:
    """Find whole URL tokens without decoding or rewriting signed queries."""
    result = []
    for match in MEDIA_URL.finditer(prompt):
        value = match.group(0)
        # Markdown/prose closing brackets are outside the URL when unbalanced.
        previous = None
        while value != previous:
            previous = value
            for opening, closing in (("(", ")"), ("[", "]"), ("{", "}"), ("（", "）"), ("【", "】"), ("《", "》")):
                while value.endswith(closing) and value.count(closing) > value.count(opening):
                    value = value[:-1]
        # A query is opaque: punctuation can be part of its signature. For
        # unsigned bare links only, remove ordinary sentence/list punctuation.
        if "?" not in value and "#" not in value:
            value = value.rstrip(".,;!:")
        if value:
            result.append((match.start(), match.start() + len(value), value))
    return result


def _plain_segments(prompt: str):
    end = 0
    for start, stop, value in _url_spans(prompt):
        yield False, prompt[end:start]
        yield True, value
        end = stop
    yield False, prompt[end:]


def _supplement_prompt_media(prompt: str, media: dict[str, list[str]]) -> None:
    """Import direct media links only under an explicit reference-media label."""
    spans = _url_spans(prompt)
    labels = list(REFERENCE_LABEL.finditer(prompt))
    declared = {url for values in media.values() for url in values}
    for start, _, value in spans:
        if value in declared:
            continue
        preceding = next((label for label in reversed(labels) if label.end() <= start), None)
        if preceding is None:
            continue
        between = prompt[preceding.end():start]
        # Permit a list of links after one label, including wrapped lines. Any
        # intervening prose or a different field prevents implicit extraction.
        for prior_start, prior_end, _ in reversed(spans):
            if preceding.end() <= prior_start and prior_end <= start:
                relative = prior_start - preceding.end()
                between = between[:relative] + between[prior_end - preceding.end():]
        between = REFERENCE_JOINERS.sub("", between)
        if re.search(r"[^\s\d:：=,，.。;；!！?？、|/\\()（）\[\]{}<>【】《》“”‘’*`\-]", between):
            continue
        label = preceding.group("zh_reference") or preceding.group("zh_link") or preceding.group("english")
        kind = {"视频": "videos", "图片": "images", "音频": "audios",
                "video": "videos", "image": "images", "audio": "audios"}[label.lower()]
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if not parsed.hostname or parsed.username or parsed.password:
            continue
        extension = unquote(parsed.path).rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
        if extension not in MEDIA_EXTENSIONS[kind]:
            continue
        media[kind].append(value)
        declared.add(value)


def media_alias_kind(label: str) -> str:
    if label.lower() in {"视频", "video", "v", "视"}:
        return "videos"
    if label.lower() in {"图片", "image", "img", "图"}:
        return "images"
    return "audios"


def prompt_references(prompt: str, media: dict[str, list[Any]], *,
                      strict_prompt_media_references: bool = False) -> list[tuple[str, int]]:
    found = []
    for is_url, segment in _plain_segments(prompt):
        if is_url:
            continue
        for match in ALIASES.finditer(segment):
            kind, index = media_alias_kind(match.group("kind")), int(match.group("index"))
            if not media[kind] and not match.group("explicit"):
                continue
            if not 1 <= index <= len(media[kind]):
                if strict_prompt_media_references:
                    raise ValueError(f"prompt references {match.group(0)} but only {len(media[kind])} {kind} were provided")
                continue
            found.append((kind, index))
    return found


def bind_prompt(prompt: str, assets: dict[str, list[Any]], sources: dict[str, Any] | None = None, *,
                strict_prompt_media_references: bool = False) -> dict[str, Any]:
    """Build typed prompt parts and a ProseMirror display document, never HTML."""
    provided = sources if sources is not None else assets
    parts, display = [], []

    def reference(kind, index):
        if not 1 <= index <= len(assets[kind]):
            raise ValueError("unbound media reference")
        asset = assets[kind][index - 1]
        asset_id = asset.get("content_asset_id") if isinstance(asset, dict) else None
        if not isinstance(asset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", asset_id):
            raise ValueError("unbound media reference")
        singular = {"images": "image", "videos": "video", "audios": "audio"}[kind]
        ref = {"content_asset_id": asset_id}
        parts.append({"type": singular + "_reference", "reference": ref})
        display.append({"type": "prompt-tag", "attrs": {"id": f"{singular}-{index}",
                        "type": singular + "-reference", "label": singular.title() + " " + str(index),
                        "params": {"index": index - 1, "asset": ref}}})

    def text(value):
        if not value:
            return
        if parts and parts[-1]["type"] == "text":
            parts[-1]["text"] += value
            display[-1]["text"] += value
        else:
            parts.append({"type": "text", "text": value})
            display.append({"type": "text", "text": value})

    known = {}
    for kind in ("images", "videos", "audios"):
        for index, source in enumerate((sources or {}).get(kind) or [], 1):
            if isinstance(source, str):
                known.setdefault(source, (kind, index))
    for is_url, segment in _plain_segments(prompt):
        if is_url:
            bound = known.get(segment)
            reference(*bound) if bound else text(segment)
            continue
        end = 0
        for match in ALIASES.finditer(segment):
            text(segment[end:match.start()])
            kind, index = media_alias_kind(match.group("kind")), int(match.group("index"))
            count = len(provided.get(kind) or [])
            if ((not count and not match.group("explicit"))
                    or (not strict_prompt_media_references and not 1 <= index <= count)):
                text(match.group(0))
            else:
                if not 1 <= index <= count:
                    raise ValueError("unbound media reference")
                reference(kind, index)
            end = match.end()
        text(segment[end:])
    return {"prompt": parts, "display_prompt": {"type": "doc", "content": [{"type": "paragraph", "content": display}]}}


def _url(item: Any) -> str:
    if isinstance(item, dict):
        item = item.get("url") or item.get("image_url") or item.get("video_url") or item.get("audio_url")
        if isinstance(item, dict):
            item = item.get("url")
    if not isinstance(item, str) or not item.strip():
        raise ValueError("media reference must contain a nonempty URL")
    return item.strip()


def _media_list(body: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    value: Any = []
    for name in names:
        if name in body:
            value = body[name]
            break
    if value is None:
        return []
    return [_url(item) for item in (value if isinstance(value, list) else [value])]


def normalize_request(body: dict[str, Any], *, strict_prompt_media_references: bool = False,
                      use_default_reference_video: bool = True,
                      default_reference_video_resolution: str = "480p") -> dict[str, Any]:
    name = str(body.get("model") or "sd-2-0-mini").strip()
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unsupported model: {name}")
    if not spec.supported:
        raise ValueError(f"unsupported model on ElevenLabs: {name}; no upstream mapping was observed")
    media = {"images": _media_list(body, ("image_urls", "images", "reference_images", "image_url", "image", "input_reference")),
             "videos": _media_list(body, ("video_urls", "videos", "reference_videos", "video_url", "video")),
             "audios": _media_list(body, ("audio_urls", "audios", "reference_audios", "audio_url", "audio"))}
    prompt = str(body.get("prompt") or "").strip()
    content = body.get("content")
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                raise ValueError("content items must be objects")
            kind = item.get("type")
            if kind == "text":
                texts.append(str(item.get("text") or ""))
            elif kind in {"image_url", "video_url", "audio_url"}:
                key = {"image_url": "images", "video_url": "videos", "audio_url": "audios"}[kind]
                media[key].append(_url(item.get(kind)))
            else:
                raise ValueError(f"unsupported content type: {kind}")
        if not prompt:
            prompt = "\n".join(texts).strip()
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > {"2.5": 12000, "minimax": 7000, "wan": 20000, "kling": 2500}.get(spec.family, 10000):
        raise ValueError("prompt exceeds this upstream model's limit")
    _supplement_prompt_media(prompt, media)
    raw_duration = body.get("duration", body.get("seconds", spec.min_duration))
    if isinstance(raw_duration, bool) or not str(raw_duration).isdigit():
        raise ValueError("duration must be an integer number of seconds")
    duration = int(raw_duration)
    if not spec.min_duration <= duration <= spec.max_duration:
        raise ValueError(f"duration must be {spec.min_duration}–{spec.max_duration} seconds for {name}")
    ratio = str(body.get("aspect_ratio") or body.get("ratio") or "16:9").lower()
    if ratio not in spec.ratios | {"auto"}:
        raise ValueError(f"unsupported aspect ratio for {name}: {ratio}")
    resolution = str(body.get("resolution") or spec.resolution).lower()
    if resolution not in spec.resolutions:
        raise ValueError(f"unsupported resolution for {name}: {resolution}")
    fixed = name.endswith(("-1080p", "-480p", "-540p", "-4k", "-preview", "-768p"))
    if fixed and resolution != spec.resolution:
        raise ValueError(f"{name} maps to actual {spec.resolution}; choose another model alias")
    for kind, limit in spec.limits.items():
        if len(media[kind]) > limit:
            raise ValueError(f"{name} supports at most {limit} {kind}")
    if spec.family in {"2.0", "minimax"} and sum(map(len, media.values())) > 12:
        raise ValueError(f"{name} supports at most 12 total reference materials")
    if spec.family in {"2.0", "minimax"} and media["audios"] and not (media["images"] or media["videos"]):
        raise ValueError("reference audio requires at least one reference image or video")
    if str(body.get("n", body.get("generate_count", 1))) != "1":
        raise ValueError("only one video per task is supported")
    prompt_references(prompt, media, strict_prompt_media_references=strict_prompt_media_references)
    prompt_reference_counts = {kind: len(items) for kind, items in media.items()}
    has_media = any(media.values())
    if spec.family == "vidu" and not has_media:
        raise ValueError("the observed Vidu Q4 interface requires reference materials")
    mode = str(body.get("mode") or ("reference_material" if has_media else "t2v"))
    if mode not in {"t2v", "i2v", "i2v_first_last", "reference_images", "reference_material"}:
        raise ValueError(f"unsupported video mode: {mode}")
    if mode == "t2v" and has_media:
        raise ValueError("t2v cannot include reference materials")
    if mode in {"i2v", "i2v_first_last"} and (media["videos"] or media["audios"]):
        raise ValueError("first/last frame mode supports only images")
    if mode in {"i2v", "i2v_first_last"} and not media["images"]:
        raise ValueError("image-to-video requires an image")
    if mode in {"i2v", "i2v_first_last"} and len(media["images"]) > 2:
        mode = "reference_material"
    if spec.family == "vidu" and mode in {"i2v", "i2v_first_last"} and len(media["images"]) > 1:
        raise ValueError("Vidu first-frame mode supports one image; use reference_material for multiple images")
    generate_audio = body.get("generate_audio", body.get("enable_audio", True))
    if not isinstance(generate_audio, bool):
        if str(generate_audio).lower() not in {"true", "false"}:
            raise ValueError("generate_audio must be a boolean")
        generate_audio = str(generate_audio).lower() == "true"
    if spec.family in {"kling", "minimax"} and not generate_audio:
        raise ValueError(f"{name} does not expose an audio-disable parameter in the observed ElevenLabs interface")
    seed = body.get("seed")
    if seed is not None:
        if spec.family == "kling" or spec.upstream_model.endswith("-us"):
            raise ValueError(f"{name} does not expose a seed parameter in the observed ElevenLabs interface")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 4294967295:
            raise ValueError("seed must be an integer between 0 and 4294967295")
    enhance_prompt = body.get("enhance_prompt", True)
    if not isinstance(enhance_prompt, bool):
        raise ValueError("enhance_prompt must be a boolean")
    fixed_reference_video = spec.family == "2.5" and use_default_reference_video and not media["videos"]
    default_reference_video = None
    if fixed_reference_video:
        default_ratio = "16:9" if ratio == "auto" else ratio
        source = reference_url(default_reference_video_resolution, default_ratio)
        media["videos"].append(source)
        default_reference_video = {"url": source, "duration": 2,
                                   "resolution": default_reference_video_resolution, "aspect_ratio": default_ratio}
        mode = "reference_material"
    effective_aspect_ratio = "auto" if spec.family == "2.5" and (
        media["videos"] or mode in {"i2v", "i2v_first_last"}) else ratio
    return {"model": name, "upstream_model": spec.upstream_model, "family": spec.family,
            "duration": duration, "resolution": resolution, "aspect_ratio": ratio,
            "effective_aspect_ratio": effective_aspect_ratio,
            "prompt": prompt, "mode": mode, "generate_audio": generate_audio,
            "seed": seed, "enhance_prompt": enhance_prompt, "material_constraints": spec.material_constraints,
            "strict_prompt_media_references": strict_prompt_media_references,
            "prompt_reference_counts": prompt_reference_counts, "fixed_reference_video": fixed_reference_video,
            "default_reference_video": default_reference_video, **media}


def public_models() -> list[dict[str, Any]]:
    return [{"id": spec.id, "object": "model", "owned_by": "eleapi",
             "meta": {"label": spec.id, "upstream_model": spec.upstream_model,
                      "actual_default_resolution": spec.resolution, "capability_source": "observed_frontend_schema_and_manual_capture",
                      "upstream_mapping_observed": spec.supported, "account_access_checked_before_upload": True,
                      "aspect_ratio_follows_reference_video_or_frame": spec.family == "2.5",
                      "inferred_alias": bool(spec.inferred_alias), "alias_note": spec.inferred_alias,
                      "combination_generation_verified": False},
             "capabilities": {} if not spec.supported else {"durations": list(range(spec.min_duration, spec.max_duration + 1)),
                              "resolutions": [spec.resolution] if spec.id.endswith(("-1080p", "-480p", "-540p", "-4k", "-preview", "-768p")) else list(spec.resolutions),
                              "aspect_ratios": sorted(spec.ratios | {"auto"}), "media_limits": spec.limits,
                              "max_total_references": 12 if spec.family in {"2.0", "minimax"} else None,
                              "material_constraints": spec.material_constraints}}
            for spec in MODELS.values()]
