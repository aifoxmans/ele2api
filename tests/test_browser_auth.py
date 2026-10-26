import base64
import json
import time

import pytest

from app.browser import BrowserError, BrowserRejected, CDPBrowser, refresh_firebase_auth
from app.errors import public_error


def token(claims):
    return 'test.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'


def test_collect_auth_reads_firebase_state_without_returning_password(monkeypatch):
    browser = CDPBrowser(12345)
    monkeypatch.setattr(browser, 'cookies', lambda: [
        {'name': 'session', 'value': 'private-cookie', 'domain': '.elevenlabs.io'},
        {'name': 'other', 'value': 'private-other', 'domain': '.other.test'}])
    monkeypatch.setattr(browser, 'evaluate', lambda *a, **k: {
        'access_token': 'private-access', 'refresh_token': 'private-refresh',
        'firebase_api_key': 'public-sdk-key', 'token_expires_at': 1234.5, 'user_agent': 'unit-test'})
    auth = browser.collect_auth()
    assert auth['access_token'] == 'private-access' and auth['token_expires_at'] == 1234.5
    assert auth['cookie'] == 'session=private-cookie'
    assert not {'password', 'login_password', 'dfp'} & auth.keys()


def test_restore_session_isolated_sdk_record_and_upstream_page(monkeypatch):
    browser = CDPBrowser(12345)
    expressions, commands = [], []
    monkeypatch.setattr(browser, 'evaluate', lambda expression, **kw: expressions.append(expression) or True)
    monkeypatch.setattr(browser, 'command', lambda *args: commands.append(args) or {})
    browser.restore_auth({'access_token': token({'sub': 'test-uid', 'email': 'person@example.test'}),
                          'refresh_token': 'private-refresh', 'firebase_api_key': 'public-sdk-key',
                          'token_expires_at': time.time() + 3600})
    assert len(expressions) == 1 and 'firebase:authUser:public-sdk-key:[DEFAULT]' in expressions[0]
    assert 'test-uid' in expressions[0] and 'firebaseLocalStorageDb' in expressions[0]
    assert commands == [('Page.navigate', {'url': 'https://elevenlabs.io/app/image-video'})]


def test_restore_rejects_invalid_session_before_browser_storage_write(monkeypatch):
    browser = CDPBrowser(12345)
    monkeypatch.setattr(browser, 'evaluate', lambda *a, **k: pytest.fail('invalid token wrote storage'))
    with pytest.raises(BrowserRejected, match='LOGIN_REQUIRED'):
        browser.restore_auth({'access_token': 'invalid', 'firebase_api_key': 'public-sdk-key',
                              'token_expires_at': time.time() + 3600})


@pytest.mark.parametrize('answer', [None, {}, {'error': 'CAPTCHA_REQUIRED'}, {'token': 'short'}])
def test_normal_captcha_does_not_fabricate_or_reuse_a_token(monkeypatch, answer):
    browser = CDPBrowser(12345)
    monkeypatch.setattr(browser, 'evaluate', lambda *a, **k: answer)
    with pytest.raises(BrowserRejected, match='CAPTCHA_REQUIRED'):
        browser.hcaptcha_token()


def test_each_captcha_call_requests_a_fresh_normal_website_challenge(monkeypatch):
    browser = CDPBrowser(12345)
    values = iter(['first-normal-website-token', 'second-normal-website-token'])
    expressions = []
    def evaluate(expression, **kwargs):
        expressions.append(expression)
        assert kwargs['await_promise'] is True
        return {'token': next(values)}
    monkeypatch.setattr(browser, 'evaluate', evaluate)
    assert browser.hcaptcha_token() != browser.hcaptcha_token()
    assert len(expressions) == 2 and all('hcaptcha.execute' in expression for expression in expressions)
    assert all('https://js.hcaptcha.com/1/api.js?render=explicit' in expression for expression in expressions)


@pytest.mark.parametrize('message', ['CAPTCHA_REQUIRED: normal browser verification', 'LOGIN_FAILED: sign-in failed',
                                   'ElevenLabs model sample is not available for this account',
                                   'ElevenLabs model sample is restricted for this account'])
def test_login_and_captcha_errors_use_fixed_maintenance_message(message):
    result = public_error('BROWSERREJECTED', message)
    assert result['message'] == '上游维护中，请稍后再试~'
    assert result['category'] == 'provider_unavailable'


def test_refresh_uses_only_selected_account_proxy_and_normal_grant(monkeypatch):
    observed = {}
    class Response:
        status_code = 200
        def json(self):
            return {'id_token': 'private-fresh', 'refresh_token': 'private-rotated', 'expires_in': '3600'}
    class Session:
        def __init__(self):
            self.trust_env, self.proxies = True, {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            observed.update(url=url, kwargs=kwargs, trust_env=self.trust_env, proxies=self.proxies)
            return Response()
    monkeypatch.setattr('app.browser.requests.Session', Session)
    fresh = refresh_firebase_auth({'refresh_token': 'private-refresh', 'firebase_api_key': 'sdk-key',
                                   'proxy_url': 'socks5://proxy.example.test:20001'})
    assert observed['trust_env'] is False
    assert set(observed['proxies'].values()) == {'socks5://proxy.example.test:20001'}
    assert observed['kwargs']['data'] == {'grant_type': 'refresh_token', 'refresh_token': 'private-refresh'}
    assert fresh['access_token'] == 'private-fresh' and fresh['refresh_token'] == 'private-rotated'
    assert time.time() + 3500 < fresh['token_expires_at'] < time.time() + 3700


def test_refresh_requires_session_before_network(monkeypatch):
    monkeypatch.setattr('app.browser.requests.Session', lambda: pytest.fail('missing session used network'))
    with pytest.raises(BrowserError, match='LOGIN_REQUIRED'):
        refresh_firebase_auth({})


def test_login_fills_both_standard_fields_and_submits_once(monkeypatch):
    browser = CDPBrowser(12345)
    submitted, filled, navigation = [], [], []
    fields = {"email": "", "password": ""}
    expected = token({'email': 'person@example.test'})
    monkeypatch.setattr(browser, 'collect_auth', lambda: {'access_token': expected} if submitted else {})
    monkeypatch.setattr(browser, 'command', lambda *args: navigation.append(args) or {})
    def fill(selector, text):
        filled.append((selector, text))
        fields['password' if 'password' in selector else 'email'] = text
        return True
    def state(*args):
        return {'document_id': 'ready-document', 'email_present': True, 'password_present': True,
                'email_matches': fields['email'] == 'person@example.test',
                'email_valid': fields['email'] == 'person@example.test',
                'password_matches': fields['password'] == 'private-password',
                'password_valid': fields['password'] == 'private-password', 'submit_enabled': True}
    monkeypatch.setattr(browser, '_login_form_state', state)
    monkeypatch.setattr(browser, '_fill_login_field', fill)
    monkeypatch.setattr(browser, '_click_login_submit', lambda: submitted.append(True) or True)
    monkeypatch.setattr(browser, 'complete_onboarding', lambda auth, **kwargs: auth)
    monkeypatch.setattr('app.browser.time.sleep', lambda seconds: None)
    result = browser.login_with_password('person@example.test', 'private-password')
    assert result['access_token'] == expected
    assert navigation == [('Page.navigate', {'url': 'https://elevenlabs.io/app/sign-in'})]
    assert len(submitted) == 1
    assert [text for _, text in filled] == ['person@example.test', 'private-password']


def test_login_rejects_reusing_another_accounts_browser(monkeypatch):
    browser = CDPBrowser(12345)
    monkeypatch.setattr(browser, 'collect_auth', lambda: {'access_token': token({'email': 'other@example.test'})})
    monkeypatch.setattr(browser, 'command', lambda *args: pytest.fail('identity mismatch must not navigate or submit'))
    with pytest.raises(BrowserRejected, match='browser identity differs') as failure:
        browser.login_with_password('person@example.test', 'private-password')
    assert 'private-password' not in str(failure.value) and 'other@example.test' not in str(failure.value)


def test_late_browser_session_recovery_must_match_imported_email(monkeypatch):
    browser = CDPBrowser(12345)
    states = iter([{}, {'access_token': token({'email': 'other@example.test'})}])
    monkeypatch.setattr(browser, 'collect_auth', lambda: next(states))
    monkeypatch.setattr(browser, 'command', lambda *args: {})
    monkeypatch.setattr(browser, '_login_form_state', lambda *args: {})
    monkeypatch.setattr(browser, '_fill_login_field', lambda *args: pytest.fail('wrong recovered identity filled credentials'))
    with pytest.raises(BrowserRejected, match='browser identity differs'):
        browser.login_with_password('person@example.test', 'private-password')
