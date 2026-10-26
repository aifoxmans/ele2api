"""Only explicit media-length rejections become unsupported-duration messages."""
import pytest

from app.errors import public_error


DURATION = '素材时长不支持，请修改后再试~'


@pytest.mark.parametrize('message', [
    'Error from model provider: One or more audio files are too short for the model to process.',
    'The reference audio file is too short.',
    'One or more audio clips are too long.',
    'Reference video files are too short.',
    'The audio duration must be within the allowed range.',
])
def test_explicit_media_duration_rejection_uses_fixed_vocabulary(message):
    error = public_error('UPSTREAM_MODEL_ERROR', message)
    assert error['message'] == error['public_message'] == DURATION
    assert error['category'] == 'invalid_request'


@pytest.mark.parametrize('message', [
    'Error from model provider: There was an error generating the content. Please try again.',
    'Audio decoder timed out.',
    'Video transcoding service is unavailable.',
    'The prompt is too short.',
    'Request used 30 seconds of reference audio.',
])
def test_other_provider_failures_do_not_invent_a_media_duration_rejection(message):
    assert public_error('UPSTREAM_MODEL_ERROR', message)['message'] != DURATION


@pytest.mark.parametrize('code', ['UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'UPSTREAM_TIMEOUT'])
def test_unknown_submission_retains_priority_over_short_audio_words(code):
    error = public_error(code, 'One or more audio files are too short; response interrupted')
    assert error['category'] == 'submission_unknown'


def test_reference_download_diagnostic_does_not_supply_duration_or_policy():
    error = public_error('MEDIAERROR', 'reference download failed: host=sensitive.example.test '
                         'reason=audio files are too short')
    assert error['category'] == 'invalid_request'
    assert error['message'] == '素材下载失败，请检查素材链接后重试~'


def test_structured_duration_rejection_does_not_require_upstream_prose():
    error = public_error('MEDIA_DURATION_UNSUPPORTED', 'private-source-diagnostic')
    assert error['message'] == DURATION
