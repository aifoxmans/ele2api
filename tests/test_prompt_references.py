import pytest

from app.catalog import bind_prompt, normalize_request, prompt_references

ASSETS = {kind: [{"content_asset_id": f"{kind}-{i}"} for i in range(1, 4)]
          for kind in ("images", "videos", "audios")}


def references(bound):
    return [(p["type"], p["reference"]["content_asset_id"]) for p in bound["prompt"] if p["type"] != "text"]


def test_aliases_bind_to_typed_asset_references_in_caller_order():
    prompt = "\u56fe1 image2 IMG3 \u89c6\u98911 video2 v3 \u97f3\u98911 Audio2 \u97f33"
    bound = bind_prompt(prompt, ASSETS)
    assert references(bound) == [(kind + "_reference", f"{plural}-{i}")
                                 for kind, plural in (("image", "images"), ("video", "videos"), ("audio", "audios"))
                                 for i in (1, 2, 3)]
    tags = [p for p in bound["display_prompt"]["content"][0]["content"] if p["type"] == "prompt-tag"]
    assert [p["attrs"]["params"]["index"] for p in tags] == [0, 1, 2] * 3
    assert tags[0]["attrs"]["label"] == "Image 1"
    assert bound["display_prompt"]["type"] == "doc"


@pytest.mark.parametrize("strict", [False, True])
def test_exact_source_url_binds_uploaded_asset_without_rewriting_signed_queries(strict):
    url = "https://source.example.test/video2(clip).mp4?sig=a%2F&Image99=unchanged"
    sources = {"images": [], "videos": [url], "audios": []}
    bound = bind_prompt(f"reference video: ({url})", ASSETS, sources=sources, strict_prompt_media_references=strict)
    assert references(bound) == [("video_reference", "videos-1")]
    assert url not in str(bound)


def test_loose_invalid_numbers_and_html_remain_json_text_nodes():
    prompt = "@Image0 @Image4 \u56fe99 @Video9 @Audio7 <script>alert(1)</script>"
    assert prompt_references(prompt, ASSETS) == []
    result = bind_prompt(prompt, ASSETS)
    assert result["prompt"] == [{"type": "text", "text": prompt}]
    assert result["display_prompt"]["content"][0]["content"] == result["prompt"]


def test_adjacent_explicit_tags_do_not_introduce_literal_at_sign_text():
    bound = bind_prompt("@Image2@Audio2@Video1", ASSETS)
    assert [p["type"] for p in bound["prompt"]] == ["image_reference", "audio_reference", "video_reference"]


@pytest.mark.parametrize("token", ["@Image0", "Image4", "@Video9", "Audio7"])
def test_strict_invalid_numbers_are_rejected(token):
    with pytest.raises(ValueError, match="only"):
        prompt_references(token, ASSETS, strict_prompt_media_references=True)
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(token, ASSETS, strict_prompt_media_references=True)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("token", ["Image2", "@Image2", "https://source.example.test/2.png"])
def test_declared_valid_reference_missing_upload_receipt_is_always_an_error(strict, token):
    sources = {"images": ["https://source.example.test/1.png", "https://source.example.test/2.png"],
               "videos": [], "audios": []}
    uploaded = {**ASSETS, "images": ASSETS["images"][:1]}
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt(token, uploaded, sources=sources, strict_prompt_media_references=strict)


def test_extra_uploaded_receipts_do_not_increase_declared_number_range():
    sources = {"images": ["https://source.example.test/i.png"], "videos": [], "audios": []}
    assert bind_prompt("Image2", ASSETS, sources=sources)["prompt"] == [{"type": "text", "text": "Image2"}]
    with pytest.raises(ValueError, match="unbound"):
        bind_prompt("Image2", ASSETS, sources=sources, strict_prompt_media_references=True)


def test_absent_kind_explicit_and_zero_are_plain_in_loose_mode():
    empty = {kind: [] for kind in ASSETS}
    prompt = "@Image1 \u56fe0 @Audio2"
    assert bind_prompt(prompt, empty)["prompt"] == [{"type": "text", "text": prompt}]
    with pytest.raises(ValueError):
        bind_prompt(prompt, empty, strict_prompt_media_references=True)
    assert bind_prompt("Video1 is a chapter", empty, strict_prompt_media_references=True)["prompt"][0]["text"] == "Video1 is a chapter"


def test_policy_snapshot_ignores_caller_override():
    body = {"prompt": "Image1 @Image99", "image_urls": ["https://source.example.test/i.png"],
            "strict_prompt_media_references": True}
    assert normalize_request(body)["strict_prompt_media_references"] is False
    with pytest.raises(ValueError, match="only 1 images"):
        normalize_request({**body, "strict_prompt_media_references": False}, strict_prompt_media_references=True)


def test_inline_labels_supplement_types_deduplicate_and_preserve_order():
    first, second = "https://source.example.test/a.mp4", "https://source.example.test/b.mp4"
    request = {"model": "sd-2-0", "prompt": f"reference video: {second}, {first}",
               "video_urls": [first]}
    normalized = normalize_request(request)
    assert normalized["videos"] == [first, second]
    assert request["video_urls"] == [first]
    assert normalize_request({"prompt": "read https://docs.example.test/image99/video3.html"})["images"] == []


def test_default_reference_video_is_not_caller_video1():
    normalized = normalize_request({"model": "sd-2-5", "prompt": "@Video1 Image1",
                                    "image_urls": ["https://source.example.test/i.png"]})
    assert normalized["prompt_reference_counts"]["videos"] == 0
    from app.client import build_submit_body
    bound = build_submit_body(normalized, {**ASSETS, "images": ASSETS["images"][:1], "videos": ASSETS["videos"][:1], "audios": []})
    assert ("video_reference", "videos-1") not in references(bound)
    assert any("@Video1" in p.get("text", "") for p in bound["prompt"])
    assert bound["model_parameters"]["reference_videos"] == [{"content_asset_id": "videos-1"}]
    with pytest.raises(ValueError):
        normalize_request({"model": "sd-2-5", "prompt": "@Video1"}, strict_prompt_media_references=True)
