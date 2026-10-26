"""Offline normal-form regressions using the captured onboarding step semantics."""

import base64
import json
import threading

import pytest

from app.browser import BASE, BrowserError, BrowserRejected, CDPBrowser


EMAIL, PASSWORD = 'person@example.test', 'private-test-password'


def auth(email=EMAIL):
    claims = base64.urlsafe_b64encode(json.dumps({'email': email}).encode()).decode().rstrip('=')
    return {'access_token': 'test.' + claims + '.signature', 'user_agent': 'test-browser'}


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def sleep(self, seconds):
            self.now += seconds

    value = Clock()
    monkeypatch.setattr('app.browser.time.monotonic', lambda: value.now)
    monkeypatch.setattr('app.browser.time.sleep', value.sleep)
    monkeypatch.setattr(CDPBrowser, 'command', lambda *args, **kwargs: {})
    return value


class SignInPage(CDPBrowser):
    """A form/navigation model: typing old documents can be lost at commit."""

    def __init__(self, *, hydrate_once=False, cold_start=False):
        super().__init__(12345)
        self.email = self.password = ''
        self.email_fills = self.submissions = self.completed = self.collections = 0
        self.hydrate_once, self.cold_start = hydrate_once, cold_start
        self.committed = False
        self.commands = []

    def collect_auth(self):
        self.collections += 1
        if self.cold_start and self.collections == 1:
            raise BrowserError('execution context is initializing')
        return auth() if self.submissions else {}

    def command(self, method, params=None):
        self.commands.append((method, params))
        if method == 'Page.navigate':
            return {'loaderId': 'sign-in-committed'}
        if method == 'Page.bringToFront':
            return {}
        assert method == 'Page.getFrameTree'
        if not self.committed:
            self.committed = True
            return {'frameTree': {'frame': {'loaderId': 'old-document'}}}
        return {'frameTree': {'frame': {'loaderId': 'sign-in-committed'}}}

    def _login_form_state(self, email, password):
        assert self.committed
        return {'document_id': 'current-form', 'email_present': True, 'password_present': True,
                'email_matches': self.email == email, 'email_valid': self.email == email,
                'password_matches': self.password == password, 'password_valid': self.password == password,
                'submit_enabled': self.email == email and self.password == password,
                'challenge_overlay': False, 'challenge_widget': False}

    def _fill_login_field(self, selector, value):
        if 'password' in selector:
            self.password = value
            if self.hydrate_once:
                # Reproduces the real failure: email disappears, password remains.
                self.email = ''
                self.hydrate_once = False
        else:
            self.email = value
            self.email_fills += 1
        return True

    def _click_login_submit(self):
        assert self.email == EMAIL and self.password == PASSWORD
        self.submissions += 1
        return True

    def complete_onboarding(self, account_auth=None, *, expected_email=''):
        assert expected_email == EMAIL
        self._login_identity(account_auth, expected_email)
        self.completed += 1
        return account_auth


def test_sign_in_refills_email_after_navigation_or_react_hydration(clock):
    page = SignInPage(hydrate_once=True)
    assert page.login_with_password(EMAIL, PASSWORD) == auth()
    assert page.email_fills == 2
    assert page.submissions == page.completed == 1
    assert sum(method == 'Page.getFrameTree' for method, _ in page.commands) == 2
    assert clock.now < 5


def test_cold_start_auth_read_is_retried_before_normal_login(clock):
    page = SignInPage(cold_start=True)
    assert page.login_with_password(EMAIL, PASSWORD) == auth()
    assert page.submissions == page.completed == 1


def test_session_recovery_still_completes_onboarding_without_typing(clock, monkeypatch):
    page = SignInPage()
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, 'command', lambda *args: pytest.fail('authorized browser navigated before readiness'))
    assert page.login_with_password(EMAIL, PASSWORD) == auth()
    assert page.submissions == 0 and page.completed == 1


@pytest.mark.parametrize('widget', [False, True])
def test_unfilled_form_timeout_is_not_misreported_as_captcha(clock, monkeypatch, widget):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', lambda: {})
    monkeypatch.setattr(page, 'command', lambda *args: {})
    monkeypatch.setattr(page, '_login_form_state', lambda *args: {
        'email_present': True, 'password_present': True, 'submit_enabled': False,
        'email_valid': False, 'password_valid': True, 'challenge_widget': widget})
    monkeypatch.setattr(page, '_fill_login_field', lambda *args: False)
    monkeypatch.setattr(page, '_click_login_submit', lambda: pytest.fail('invalid form was submitted'))
    with pytest.raises(BrowserRejected, match='LOGIN_FAILED') as exc:
        page.login_with_password(EMAIL, PASSWORD)
    assert 'CAPTCHA' not in str(exc.value) and PASSWORD not in str(exc.value)


def test_visible_challenge_stops_sign_in_without_another_submit(clock, monkeypatch):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', lambda: {})
    monkeypatch.setattr(page, 'command', lambda *args: {})
    monkeypatch.setattr(page, '_login_form_state', lambda *args: {'challenge_overlay': True})
    monkeypatch.setattr(page, '_fill_login_field', lambda *args: pytest.fail('challenge received credentials'))
    with pytest.raises(BrowserRejected, match='CAPTCHA_REQUIRED'):
        page.login_with_password(EMAIL, PASSWORD)


class CapturedOnboarding(CDPBrowser):
    """Known semantic DOM steps; transitions occur only through normal clicks."""

    def __init__(self):
        super().__init__(12345)
        self.stage = 'platform'
        self.checked = set()
        self.marketing = True  # A restored optional preference must be opted out.
        self.actions = []
        self.complete = False
        self.readiness_checks = 0

    def collect_auth(self):
        return auth()

    def _onboarding_complete_on_server(self, account_auth):
        self._login_identity(account_auth, EMAIL)
        self.readiness_checks += 1
        return self.complete

    def _onboarding_view(self):
        names = {'platform': ['platform', 'next'], 'personal-info': ['adult', 'terms', 'updated', 'next'],
                 'persona': ['creator', 'next'], 'persona-details': ['youtube', 'shorts', 'next'],
                 'interests': ['image-video', 'next'], 'pricing': ['skip']}[self.stage]
        self.current = {index + 1: name for index, name in enumerate(names)}
        return {'stage': self.stage, 'controls': {
            name: {'x': index, 'y': 10, 'selected': self.marketing if name == 'updated' else
                   (self.stage, name) in self.checked} for index, name in self.current.items()}}

    def click(self, x, y):
        name = self.current[x]
        self.actions.append((self.stage, name))
        if name == 'updated':
            assert self.marketing is True
            self.marketing = False
        elif name in {'next', 'skip'}:
            required = {'platform': ['platform'], 'personal-info': ['adult', 'terms'],
                        'persona': ['creator'], 'persona-details': ['youtube', 'shorts'],
                        'interests': ['image-video'], 'pricing': []}[self.stage]
            assert all((self.stage, item) in self.checked for item in required)
            order = ['platform', 'personal-info', 'persona', 'persona-details', 'interests', 'pricing']
            if self.stage == 'pricing':
                self.complete = True
            else:
                self.stage = order[order.index(self.stage) + 1]
        else:
            assert (self.stage, name) not in self.checked, 'choice was toggled twice'
            self.checked.add((self.stage, name))


def test_normal_onboarding_choices_continue_and_skip_without_paid_or_marketing_actions(clock):
    page = CapturedOnboarding()
    result = page.complete_onboarding(auth(), expected_email=EMAIL)
    assert result == auth() and page.complete and page.marketing is False
    assert page.actions == [
        ('platform', 'platform'), ('platform', 'next'),
        ('personal-info', 'updated'), ('personal-info', 'adult'), ('personal-info', 'terms'),
        ('personal-info', 'next'), ('persona', 'creator'), ('persona', 'next'),
        ('persona-details', 'youtube'), ('persona-details', 'shorts'), ('persona-details', 'next'),
        ('interests', 'image-video'), ('interests', 'next'), ('pricing', 'skip')]
    assert page.readiness_checks >= 2


def test_already_completed_server_state_is_idempotent_and_does_not_touch_dom(clock, monkeypatch):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda value: True)
    monkeypatch.setattr(page, '_onboarding_view', lambda: pytest.fail('completed account touched onboarding UI'))
    monkeypatch.setattr(page, 'command', lambda *args: pytest.fail('completed account navigated'))
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()


def test_known_creator_benefits_notice_only_closes_then_uses_actual_platform_selection(clock, monkeypatch):
    page = CapturedOnboarding()
    prior_view, prior_click = page._onboarding_view, page.click
    notice = [True]
    page.checked.add(('platform', 'platform'))  # Actual captured aria-selected=true.

    def view():
        if notice[0]:
            return {'stage': 'creator-welcome', 'controls': {
                'close': {'x': 99, 'y': 10}, 'setup-two-factor': {'x': 98, 'y': 10}}}
        return prior_view()

    def click(x, y):
        if notice[0]:
            assert x == 99, 'welcome notice must not activate 2FA'
            notice[0] = False
            page.actions.append(('creator-welcome', 'close'))
        else:
            prior_click(x, y)

    monkeypatch.setattr(page, '_onboarding_view', view)
    monkeypatch.setattr(page, 'click', click)
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()
    assert page.actions[:2] == [('creator-welcome', 'close'), ('platform', 'next')]
    assert ('platform', 'platform') not in page.actions


def test_exact_image_video_intro_advances_then_enters_normal_account_onboarding(clock, monkeypatch):
    page = CapturedOnboarding()
    prior_view, prior_click = page._onboarding_view, page.click
    notice, entered, commands = [True], [], []

    def view():
        if notice[0]:
            return {'stage': 'image-video-intro', 'controls': {'get-started': {'x': 99, 'y': 10}}}
        if not entered:
            return {'stage': 'waiting', 'onboarding_path': False, 'controls': {}}
        return prior_view()

    def click(x, y):
        if notice[0]:
            assert (x, y) == (99, 10)
            notice[0] = False
            page.actions.append(('image-video-intro', 'get-started'))
        else:
            prior_click(x, y)

    def command(method, params=None):
        commands.append((method, params))
        if method == 'Page.navigate':
            assert params == {'url': BASE + '/app/onboarding'}
            entered.append(True)
        else:
            assert method == 'Page.bringToFront'
        return {}

    monkeypatch.setattr(page, '_onboarding_view', view)
    monkeypatch.setattr(page, 'click', click)
    monkeypatch.setattr(page, 'command', command)
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()
    assert page.actions[0] == ('image-video-intro', 'get-started')
    assert len(entered) == 1 and page.complete


def test_feature_intro_does_not_authorize_followup_model_approval(clock, monkeypatch):
    page = CDPBrowser(12345)
    clicks = []
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda account_auth: False)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {
        'stage': 'unknown-dialog' if clicks else 'image-video-intro',
        'controls': {'approve-models': {'x': 3, 'y': 4}} if clicks else {'get-started': {'x': 1, 'y': 2}}})
    monkeypatch.setattr(page, 'click', lambda x, y: clicks.append((x, y)))
    with pytest.raises(BrowserRejected, match='ONBOARDING_REQUIRED'):
        page.complete_onboarding(auth(), expected_email=EMAIL)
    assert clicks == [(1, 2)]


def test_feature_intro_dom_identity_requires_complete_references_content_and_single_button(monkeypatch):
    page = CDPBrowser(12345)
    expressions = []
    monkeypatch.setattr(page, 'evaluate', lambda expression: expressions.append(expression) or {})
    page._onboarding_view()
    source = expressions[0]
    assert "reference('aria-labelledby','H3','Introducing Image & Video')" in source
    assert "reference('aria-describedby','P','Onboarding flow for Image & Video feature')" in source
    for text in ('Switch seamlessly between Image and Video', 'Access to popular models in one click',
                 'Upscale, Lip Sync and more all in the same flow'):
        assert 'includes(' + repr(text) + ')' in source
    assert "buttons.length===1&&text(buttons[0])==='Get started'" in source


def test_known_welcome_scrolls_then_rechecks_close_before_clicking(clock, monkeypatch):
    page = CapturedOnboarding()
    prior_view, prior_click = page._onboarding_view, page.click
    notice, scrolled, views = [True], [], []

    def view():
        if notice[0]:
            views.append(bool(scrolled))
            return {'stage': 'creator-welcome', 'needs_scroll': not scrolled,
                    'controls': {'close': {'x': 99, 'y': 10}} if scrolled else {}}
        return prior_view()

    def scroll():
        assert not scrolled
        scrolled.append(True)
        return True

    def click(x, y):
        if notice[0]:
            assert scrolled and views == [False, True]
            assert (x, y) == (99, 10)
            notice[0] = False
            page.actions.append(('creator-welcome', 'close'))
        else:
            prior_click(x, y)

    monkeypatch.setattr(page, '_onboarding_view', view)
    monkeypatch.setattr(page, '_scroll_creator_welcome_close', scroll)
    monkeypatch.setattr(page, 'click', click)
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()
    assert scrolled == [True] and page.actions[0] == ('creator-welcome', 'close')


def test_welcome_scroll_is_bounded_and_never_clicks_unseen_close(clock, monkeypatch):
    page = CDPBrowser(12345)
    scrolls = []
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda account_auth: False)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {
        'stage': 'creator-welcome', 'needs_scroll': True, 'controls': {}})
    monkeypatch.setattr(page, '_scroll_creator_welcome_close', lambda: scrolls.append(True) or False)
    monkeypatch.setattr(page, 'click', lambda *args: pytest.fail('unseen Close was clicked'))
    with pytest.raises(BrowserRejected, match='ONBOARDING_REQUIRED'):
        page.complete_onboarding(auth(), expected_email=EMAIL)
    assert len(scrolls) == 2


def test_scroll_reidentifies_labelled_dialog_without_requiring_sr_heading_visibility(monkeypatch):
    page = CDPBrowser(12345)
    expressions = []
    monkeypatch.setattr(page, 'evaluate', lambda expression: expressions.append(expression) or True)
    assert page._scroll_creator_welcome_close() is True
    source = expressions[0]
    assert "root?.getAttribute('role')==='dialog'" in source
    assert "root.getAttribute('aria-labelledby')" in source and 'document.getElementById(id)' in source
    assert "heading.tagName==='H2'" in source and "text(heading)==='Welcome to ElevenLabs Creator'" in source
    assert 'visible(heading)' not in source
    assert "text(e)==='Close'" in source and "close.scrollIntoView({block:'center'" in source
    assert 'Setup two factor' not in source and '.click(' not in source


def test_normal_personal_form_without_adult_checkbox_does_not_invent_age_input(clock, monkeypatch):
    page = CDPBrowser(12345)
    clicked = []
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda account_auth: bool(clicked))
    monkeypatch.setattr(page, '_onboarding_view', lambda: {
        'stage': 'personal-info', 'controls': {'next': {'x': 5, 'y': 10}}})
    monkeypatch.setattr(page, 'click', lambda x, y: clicked.append((x, y)))
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()
    assert clicked == [(5.0, 10.0)]


@pytest.mark.parametrize('missing', ['adult', 'terms'])
def test_unresolved_named_required_checkbox_never_submits_next(clock, monkeypatch, missing):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda account_auth: False)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {
        'stage': 'personal-info', 'blocked_controls': [missing],
        'controls': {'next': {'x': 5, 'y': 10}}})
    monkeypatch.setattr(page, 'click', lambda *args: pytest.fail('unresolved required checkbox submitted Next'))
    with pytest.raises(BrowserRejected, match='ONBOARDING_REQUIRED'):
        page.complete_onboarding(auth(), expected_email=EMAIL)


def test_radix_checkbox_mapping_is_scoped_to_named_inputs_own_label(monkeypatch):
    page = CDPBrowser(12345)
    expressions = []
    monkeypatch.setattr(page, 'evaluate', lambda expression: expressions.append(expression) or {})
    page._onboarding_view()
    source = expressions[0]
    assert "const labels=[...(e.labels||[]),e.closest('label')]" in source
    assert "label.querySelectorAll('[role=\"checkbox\"]')" in source
    assert 'if(buttons.length===1)return buttons[0]' in source
    assert 'controls[key].selected=chosen(e)||chosen(target)' in source
    assert "else if(e&&key!=='updated')blocked.push(key)" in source


def test_onboarding_rejects_changed_browser_identity_before_server_or_ui(clock, monkeypatch):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', lambda: auth('other@example.test'))
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda *args: pytest.fail('wrong identity queried'))
    monkeypatch.setattr(page, '_onboarding_view', lambda: pytest.fail('wrong identity changed UI'))
    with pytest.raises(BrowserRejected, match='identity differs'):
        page.complete_onboarding(auth(), expected_email=EMAIL)


@pytest.mark.parametrize('stage,expected', [('unknown-dialog', 'ONBOARDING_REQUIRED'),
                                           ('captcha', 'CAPTCHA_REQUIRED')])
def test_unknown_popup_or_visible_captcha_is_left_for_normal_verification(clock, monkeypatch, stage, expected):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda value: False)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {'stage': stage, 'controls': {}})
    monkeypatch.setattr(page, 'click', lambda *args: pytest.fail('unknown popup was clicked'))
    monkeypatch.setattr(page, 'command', lambda *args: {} if args[0] == 'Page.bringToFront' else
                        pytest.fail('unknown popup was bypassed by navigation'))
    with pytest.raises(BrowserRejected, match=expected):
        page.complete_onboarding(auth(), expected_email=EMAIL)


def test_false_server_state_enters_captured_route_once_and_never_fakes_completion(clock, monkeypatch):
    page = CDPBrowser(12345)
    commands = []
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda value: False)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {'stage': 'waiting', 'onboarding_path': False, 'controls': {}})
    monkeypatch.setattr(page, 'command', lambda *args: commands.append(args) or {})
    monkeypatch.setattr(page, 'click', lambda *args: pytest.fail('unrecognized page clicked'))
    with pytest.raises(BrowserRejected, match='ONBOARDING_REQUIRED'):
        page.complete_onboarding(auth(), expected_email=EMAIL)
    assert [item for item in commands if item[0] == 'Page.navigate'] == [
        ('Page.navigate', {'url': BASE + '/app/onboarding'})]


def test_end_save_delay_waits_for_server_instead_of_restarting_flow(clock, monkeypatch):
    page = CapturedOnboarding()
    prior_view, prior_check = page._onboarding_view, page._onboarding_complete_on_server
    saved_at = []

    def view():
        if page.complete:
            if not saved_at:
                saved_at.append(clock.now)
            return {'stage': 'waiting', 'onboarding_path': False, 'controls': {}}
        return prior_view()

    def ready(value):
        return prior_check(value) and bool(saved_at) and clock.now - saved_at[0] > 2

    monkeypatch.setattr(page, '_onboarding_view', view)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', ready)
    monkeypatch.setattr(page, 'command', lambda *args: {} if args[0] == 'Page.bringToFront' else
                        pytest.fail('normal save delay restarted onboarding'))
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()
    assert saved_at and clock.now - saved_at[0] > 2


@pytest.mark.parametrize('complete', [False, 'true', 1, None])
def test_readiness_requires_real_server_boolean_and_uses_only_get(monkeypatch, complete):
    page = CDPBrowser(12345)
    expressions = []
    def evaluate(expression, **kwargs):
        expressions.append(expression)
        assert kwargs['await_promise'] is True
        return {'http': 200, 'complete': complete}
    monkeypatch.setattr(page, 'evaluate', evaluate)
    assert page._onboarding_complete_on_server(auth()) is False
    assert len(expressions) == 1
    assert "/v1/user/internal" in expressions[0] and "method:'GET'" in expressions[0]
    assert "body:" not in expressions[0] and 'localStorage' not in expressions[0]


def test_form_probe_does_not_interpolate_private_data_as_placeholders(monkeypatch):
    page = CDPBrowser(12345)
    expressions = []
    monkeypatch.setattr(page, 'evaluate', lambda expression: expressions.append(expression) or {})
    page._login_form_state('PASSWORD@example.test', 'secret-EMAIL-"-password')
    assert 'const expectedEmail="PASSWORD@example.test"' in expressions[0]
    assert 'const expectedPassword="secret-EMAIL-\\"-password"' in expressions[0]
    assert "closest('form')" in expressions[0] and 'email.value.trim()' in expressions[0]


def test_dom_probe_requires_visible_controls_and_never_writes_age_or_completion(monkeypatch):
    page = CDPBrowser(12345)
    expressions = []
    monkeypatch.setattr(page, 'evaluate', lambda expression: expressions.append(expression) or {})
    page._onboarding_view()
    source = expressions[0]
    assert "p.getAttribute('aria-hidden')==='true'" in source
    assert 'document.elementFromPoint' in source and 'innerHeight' in source
    assert 'onboarding-platform-elevencreative' in source and 'onboarding-illustration-option-image-video' in source
    assert "button,label" in source and 'creator content' in source
    assert "getAttribute('aria-selected')==='true'" in source
    assert "text(heading)==='Welcome to ElevenLabs Creator'" in source
    assert "text(root).includes('Overview of your subscription benefits')" in source
    assert "text(root).includes('Included in your plan')" in source
    assert "input#firstname" in source and "input#source-freetext" in source
    assert "h1,h2,h3,p,[data-agent-id]" in source
    assert all(word not in source for word in ['.click()', 'localStorage', 'birthday', 'fetch(', 'setItem'])


def test_activation_precedes_ui_and_network_verification_does_not_hold_ui_lock(clock, monkeypatch):
    from app.browser import _UI_LOCK
    page = CDPBrowser(12345)
    active, ready = [], []
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, 'command', lambda method, *args: active.append(method) or {})

    def server(account_auth):
        assert page.timeout == 90
        acquired = threading.Event()
        def probe():
            with _UI_LOCK:
                acquired.set()
        thread = threading.Thread(target=probe)
        thread.start()
        assert acquired.wait(.5), 'network verification held the global UI lock'
        thread.join()
        return bool(ready)

    def view():
        assert active[-1] == 'Page.bringToFront' and page.timeout == 5
        return {'stage': 'platform', 'controls': {
            'platform': {'selected': True}, 'next': {'x': 1, 'y': 2}}}

    monkeypatch.setattr(page, '_onboarding_complete_on_server', server)
    monkeypatch.setattr(page, '_onboarding_view', view)
    monkeypatch.setattr(page, 'click', lambda *args: ready.append(True))
    assert page.complete_onboarding(auth(), expected_email=EMAIL) == auth()
    assert ready == [True] and page.timeout == 90


def test_focus_steps_are_serialized_and_restore_ui_timeout_after_error(monkeypatch):
    first, second = CDPBrowser(11111), CDPBrowser(22222)
    entered, release, other_entered = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr('app.browser.time.sleep', lambda seconds: None)
    monkeypatch.setattr(CDPBrowser, 'command', lambda *args, **kwargs: {})

    def one():
        with first._ui_step():
            entered.set()
            assert release.wait(2)

    def two():
        with second._ui_step():
            other_entered.set()

    a = threading.Thread(target=one)
    b = threading.Thread(target=two)
    a.start()
    assert entered.wait(1)
    b.start()
    assert not other_entered.wait(.05), 'another account stole focus during a UI action'
    release.set()
    a.join(2)
    b.join(2)
    assert not a.is_alive() and not b.is_alive() and other_entered.is_set()
    with pytest.raises(ValueError):
        with first._ui_step():
            assert first.timeout == 5
            raise ValueError('synthetic DOM error')
    assert first.timeout == second.timeout == 90


def test_loading_timeout_is_distinct_from_unknown_dialog(clock, monkeypatch):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', auth)
    monkeypatch.setattr(page, '_onboarding_complete_on_server', lambda account_auth: False)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {'stage': 'loading', 'controls': {}})
    monkeypatch.setattr(page, 'click', lambda *args: pytest.fail('loading page was clicked'))
    with pytest.raises(BrowserRejected, match='^ONBOARDING_LOADING_TIMEOUT:'):
        page.complete_onboarding(auth(), expected_email=EMAIL)


@pytest.mark.parametrize('stage,marker', [('platform', 'ONBOARDING_NETWORK_ERROR'),
                                         ('unknown-dialog', 'ONBOARDING_REQUIRED')])
def test_network_resume_marker_requires_current_safe_page(clock, monkeypatch, stage, marker):
    page = CDPBrowser(12345)
    monkeypatch.setattr(page, 'collect_auth', auth)
    def unavailable(account_auth):
        raise BrowserError('synthetic verification transport failure')
    monkeypatch.setattr(page, '_onboarding_complete_on_server', unavailable)
    monkeypatch.setattr(page, '_onboarding_view', lambda: {'stage': stage, 'controls': {}})
    monkeypatch.setattr(page, 'click', lambda *args: pytest.fail('network failure clicked UI'))
    with pytest.raises(BrowserError, match='^' + marker + ':'):
        page.complete_onboarding(auth(), expected_email=EMAIL)
