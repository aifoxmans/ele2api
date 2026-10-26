"""Real provider moderation and definite quota rejection use fixed messages."""
import pytest

from app.errors import public_error


POLICY = '检测到内容有敏感或违规情况，请修改后重试，积分已返还～'
MAINTENANCE = '上游维护中，请稍后再试~'


@pytest.mark.parametrize('message', [
    'Error from model provider: This generation may violate our Terms of Service and has been blocked.',
    'This generation violates our Terms of Service.',
])
def test_explicit_generation_terms_rejection_is_content_policy(message):
    result = public_error('UPSTREAM_FAILED', message)
    assert result['category'] == 'content_policy'
    assert result['message'] == result['public_message'] == POLICY


@pytest.mark.parametrize('message', [
    'Account has been blocked.',
    'Terms of Service page temporarily unavailable.',
    'Error from model provider: There was an error generating the content. Please try again.',
])
def test_generic_provider_or_account_errors_are_not_content_moderation(message):
    assert public_error('UPSTREAM_FAILED', message)['category'] != 'content_policy'


def test_legacy_definitive_quota_rejection_is_provider_unavailable():
    result = public_error('UPSTREAM_REJECTED',
                          'ElevenLabs HTTP 401: This request exceeds your quota of 100000. '
                          'You have 20000 credits remaining, while 30000 credits are required for this request.')
    assert result['message'] == MAINTENANCE
    assert result['category'] == 'provider_unavailable'


def test_structured_account_model_unavailable_never_exposes_private_reason():
    result = public_error('ACCOUNT_MODEL_UNAVAILABLE', 'private-account-model-diagnostic')
    assert result['message'] == MAINTENANCE
    assert result['category'] == 'provider_unavailable'


def test_unknown_submission_dominates_policy_words():
    result = public_error('UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                          'This generation may violate our Terms of Service; submit response unavailable')
    assert result['category'] == 'submission_unknown'
