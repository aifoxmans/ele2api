"""Reject documented short Seedance inputs before a generation is submitted."""

from copy import deepcopy

import pytest

from app.browser import BrowserRejected
from app.catalog import normalize_request
from app.client import EleClient
from app.errors import public_error
from app.media import validate_material_durations


def normalized(model):
    return normalize_request({"model": model, "prompt": "A landscape",
                              "image_urls": ["https://source.example.test/image.png"]},
                             use_default_reference_video=False)


@pytest.mark.parametrize("model", ["sd-2-0", "sd-2-0-mini", "sd-2-0-fast", "sd-2-5-480p",
                                  "sd-2-5-480p-preview"])
def test_seedance_short_audio_is_rejected_without_changing_the_receipt(model):
    receipts = {"images": [], "videos": [], "audios": [{"duration": 1.28}]}
    original = deepcopy(receipts)
    with pytest.raises(BrowserRejected, match="unsupported reference duration") as error:
        validate_material_durations(normalized(model), receipts)
    assert receipts == original
    assert public_error("UPSTREAM_REJECTED", str(error.value))["message"] == "素材时长不支持，请修改后再试~"


@pytest.mark.parametrize("model", ["sd-2-0", "sd-2-0-mini", "sd-2-0-fast", "sd-2-5-480p",
                                  "sd-2-5-480p-preview"])
def test_seedance_two_second_audio_meets_the_inclusive_lower_bound(model):
    validate_material_durations(normalized(model), {"images": [], "videos": [], "audios": [{"duration": 2.0}]})


@pytest.mark.parametrize("model,maximum", [("sd-2-0", 15.1), ("sd-2-5-480p", 30.1)])
def test_seedance_preserves_the_existing_upper_duration_tolerance(model, maximum):
    validate_material_durations(normalized(model), {"images": [], "videos": [], "audios": [{"duration": maximum}]})


def test_four_audio_inputs_with_a_short_final_clip_stop_before_generation():
    value = normalized("sd-2-5-480p")
    value["audios"] = [f"https://source.example.test/audio-{index}.wav" for index in range(4)]
    durations = dict(zip(value["audios"], (3.78, 2.0, 5.3, 1.28)))
    client = object.__new__(EleClient)
    client.account = {"id": 1}
    client.browser = object()
    client._check_model = lambda model: None
    client._upload_input = lambda source, kind, account_id: {"duration": durations[source]} if kind == "audios" else {}
    client.request = lambda *args, **kwargs: pytest.fail("invalid audio must not reach generation or price requests")
    original = deepcopy(value)
    with pytest.raises(BrowserRejected, match="1.28s"):
        uploaded = client.upload_inputs(value)
        client.submit(value, uploaded)
    assert value == original
