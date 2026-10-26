import pytest

from app.catalog import MODELS, normalize_request, public_models


def test_real_model_ids_and_unobserved_vidu_are_distinguished():
    assert len(MODELS) == 17
    assert normalize_request({"model": "sd-2-0", "prompt": "A sunrise"})["upstream_model"] == "bytedance-seedance-v2-us"
    assert MODELS["sd-2-0-mini"].upstream_model == "bytedance-seedance-v2-mini"
    assert MODELS["minimax-h3-768p"].resolution == "768p"
    assert MODELS["kling-4.0"].upstream_model == "kling-4"
    models = {m["id"]: m for m in public_models()}
    assert models["vidu-q4"]["meta"]["upstream_mapping_observed"] is False
    with pytest.raises(ValueError, match="no upstream mapping"):
        normalize_request({"model": "vidu-q4", "prompt": "A sunrise"})


@pytest.mark.parametrize("model", ["sd-2-0", "sd-2-0-fast", "sd-2-0-mini"])
def test_seedance_two_per_type_caps_also_enforce_real_total_twelve(model):
    def body(images):
        return {"model": model, "prompt": "A sunrise", "image_urls": ["https://source.example.test/i.png"] * images,
                "video_urls": ["https://source.example.test/v.mp4"] * 3,
                "audio_urls": ["https://source.example.test/a.mp3"] * 3}
    value = normalize_request(body(6))
    assert sum(len(value[k]) for k in ("images", "videos", "audios")) == 12
    with pytest.raises(ValueError, match="12 total"):
        normalize_request(body(9))


def test_seedance_two_point_five_can_take_all_fifty_without_default_fifty_first():
    value = normalize_request({"model": "sd-2-5", "prompt": "A sunrise",
                               "image_urls": ["https://source.example.test/i.png"] * 30,
                               "video_urls": ["https://source.example.test/v.mp4"] * 10,
                               "audio_urls": ["https://source.example.test/a.mp3"] * 10})
    assert sum(len(value[k]) for k in ("images", "videos", "audios")) == 50
    assert value["fixed_reference_video"] is False
    assert value["default_reference_video"] is None


@pytest.mark.parametrize("mode", ["t2v", "i2v", "reference_material"])
def test_default_clip_policy_is_admin_controlled_and_snapshotted(mode):
    body = {"model": "sd-2-5", "prompt": "A sunrise", "mode": mode, "use_default_reference_video": False}
    if mode == "i2v":
        body["image_urls"] = ["https://source.example.test/i.png"]
    value = normalize_request(body, default_reference_video_resolution="720p")
    assert value["fixed_reference_video"] is True
    assert value["mode"] == "reference_material"
    assert value["default_reference_video"]["duration"] == 2
    assert value["default_reference_video"]["resolution"] == "720p"
    assert value["prompt_reference_counts"]["videos"] == 0
    disabled = normalize_request(body, use_default_reference_video=False)
    assert disabled["videos"] == [] and disabled["fixed_reference_video"] is False
    assert disabled["mode"] == mode


def test_inline_or_content_video_counts_as_caller_video():
    url = "https://source.example.test/v.mp4"
    for body in ({"prompt": f"reference video: {url}"},
                 {"content": [{"type": "text", "text": "A sunrise"}, {"type": "video_url", "video_url": {"url": url}}]}):
        value = normalize_request({"model": "sd-2-5", **body})
        assert value["videos"] == [url] and not value["fixed_reference_video"]


@pytest.mark.parametrize("body,match", [
    ({"model": "sd-2-0-mini", "resolution": "1080p"}, "resolution"),
    ({"model": "sd-2-0-1080p", "resolution": "720p"}, "actual 1080p"),
    ({"model": "kling-4.0", "audio_urls": ["https://source.example.test/a.mp3"]}, "0 audios"),
    ({"model": "kling-4.0", "ratio": "4:3"}, "aspect ratio"),
    ({"model": "sd-2-5", "duration": 31}, "duration"),
    ({"model": "minimax-h3-768p", "duration": 4}, "5"),
    ({"model": "sd-2-0", "audio_urls": ["https://source.example.test/a.mp3"]}, "requires at least one"),
    ({"model": "sd-2-0", "seed": 123}, "seed"),
    ({"model": "sd-2-0", "mode": "t2v", "prompt": "reference video: https://source.example.test/v.mp4"}, "cannot include"),
])
def test_unsupported_combinations_fail_before_upload(body, match):
    with pytest.raises(ValueError, match=match):
        normalize_request({"prompt": "A sunrise", **body})


def test_material_duration_constraints_are_from_elevenlabs_frontend_schema():
    spec = MODELS["sd-2-0"].material_constraints
    assert spec["videos"]["min_total_duration"] == 2
    assert spec["videos"]["max_total_duration"] == 15.1
    assert MODELS["sd-2-5"].material_constraints["audios"]["max_total_duration"] == 30.1
    assert MODELS["kling-4.0"].material_constraints["videos"]["min_dimension"] == 700
    assert MODELS["minimax-h3-768p"].resolutions == ("480p", "768p", "2k", "4k")