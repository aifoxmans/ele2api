"""Use an authenticated native Chromium tab as the ElevenLabs Web API transport."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import websocket
import requests


BASE = "https://elevenlabs.io"
API_BASE = "https://api.us.elevenlabs.io"
GENERATION_SITEKEY = "a672bccc-8364-4300-8fe6-840a693c087a"
_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_CAPTCHA_LOCKS: dict[int, threading.RLock] = {}
_UI_LOCK = threading.RLock()
_VISIBLE_DOM = r'''
const rendered=e=>{
  if(!e||e.disabled||e.getAttribute('aria-disabled')==='true')return false;
  for(let p=e;p&&p.nodeType===1;p=p.parentElement){
    const s=getComputedStyle(p);
    if(p.hidden||p.getAttribute('aria-hidden')==='true'||s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;
  }
  const r=e.getBoundingClientRect();return r.width>1&&r.height>1;
};
const visible=e=>{
  if(!rendered(e))return false;
  const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
  if(r.width<=1||r.height<=1||x<0||y<0||x>=innerWidth||y>=innerHeight)return false;
  const top=document.elementFromPoint(x,y);
  return !!top&&(e===top||e.contains(top)||top.contains(e));
};
const point=e=>{if(!visible(e))return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};};
const text=e=>(e?.innerText||e?.textContent||'').trim().replace(/\s+/g,' ');
const find=(root,selector)=>[...root.querySelectorAll(selector)].find(visible);
const checkedState=e=>!!e&&(e.checked===true||e.getAttribute('aria-checked')==='true'||e.getAttribute('aria-pressed')==='true'||
  e.getAttribute('aria-selected')==='true'||['checked','on'].includes(e.getAttribute('data-state'))||
  e.getAttribute('data-selected')==='true');
const chosen=e=>!!e&&(checkedState(e)||checkedState(e.control)||
  checkedState(e.htmlFor?document.getElementById(e.htmlFor):null)||
  !!e.querySelector('input:checked,[role="checkbox"][aria-checked="true"],[role="radio"][aria-checked="true"]')||
  (e.matches('[data-agent-id^="onboarding-illustration-option-"]')&&e.tabIndex===-1&&!!e.querySelector('.ring-2'))||
  (e.matches('[data-agent-id^="onboarding-icon-option-"]')&&e.tabIndex===-1&&e.classList.contains('ring-2')));
const challengeFrames=[...document.querySelectorAll('iframe')].filter(e=>
  /hcaptcha\.com|recaptcha|challenges\.cloudflare\.com/i.test(e.src||'')&&visible(e));
const challengeOverlay=challengeFrames.some(e=>e.getBoundingClientRect().height>110);
const challengeWidget=challengeFrames.some(e=>{const r=e.getBoundingClientRect();return r.width>=180&&r.height>=45;});
'''
_CREATOR_WELCOME_DOM = r'''
const creatorWelcome=root=>root?.getAttribute('role')==='dialog'&&
  (root.getAttribute('aria-labelledby')||'').split(/\s+/).some(id=>{
    const heading=document.getElementById(id);
    return heading&&root.contains(heading)&&heading.tagName==='H2'&&text(heading)==='Welcome to ElevenLabs Creator';
  })&&text(root).includes('Overview of your subscription benefits')&&text(root).includes('Included in your plan');
'''


class BrowserError(Exception):
    pass


class BrowserRejected(BrowserError):
    """A complete HTTP response established that the upstream rejected a call."""


def refresh_firebase_auth(account: dict[str, Any]) -> dict[str, Any]:
    """Refresh this account using Firebase's normal refresh-token grant."""
    refresh = str(account.get("refresh_token") or "")
    api_key = str(account.get("firebase_api_key") or "")
    if not refresh or not api_key:
        raise BrowserRejected("LOGIN_REQUIRED: the Firebase session needs authorization")
    with requests.Session() as session:
        session.trust_env = False
        proxy = account_proxy_url(account)
        session.proxies.update({"http": proxy, "https": proxy})
        try:
            response = session.post("https://securetoken.googleapis.com/v1/token",
                params={"key": api_key}, data={"grant_type": "refresh_token", "refresh_token": refresh},
                headers={"Origin": BASE, "Referer": BASE + "/", "User-Agent": str(account.get("user_agent") or "Mozilla/5.0")},
                timeout=(15, 30))
            if response.status_code >= 400:
                raise BrowserRejected("LOGIN_REQUIRED: Firebase rejected the saved session")
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BrowserError("Firebase session refresh failed: " + type(exc).__name__) from exc
    token = body.get("id_token") or body.get("access_token")
    if not isinstance(token, str) or not token:
        raise BrowserRejected("LOGIN_REQUIRED: Firebase returned no access token")
    return {"access_token": token, "refresh_token": body.get("refresh_token") or refresh,
            "firebase_api_key": api_key, "token_expires_at": time.time() + float(body.get("expires_in") or 3600)}


def _restore_managed_auth(account: dict[str, Any], port: int) -> None:
    if account.get("access_token") or account.get("refresh_token"):
        browser = CDPBrowser(port)
        deadline = time.monotonic() + 8
        while True:
            try:
                if browser.collect_auth().get("access_token"):
                    return
                if browser.evaluate("document.readyState") in {"interactive", "complete"}:
                    browser.restore_auth(account)
                    return
            except BrowserError:
                if time.monotonic() >= deadline:
                    raise
            if time.monotonic() >= deadline:
                raise BrowserError("the account browser is still initializing; retry verification")
            time.sleep(.2)


def account_proxy_url(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        valid = parsed.scheme in {"socks5", "http", "https"} and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment:
        raise BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port")
    return value


def _targets(port: int) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=4) as response:
            return json.load(response)
    except Exception as exc:
        raise BrowserError(f"ElevenLabs CDP port {port} is unavailable: {type(exc).__name__}") from exc


def _page(port: int) -> dict[str, Any]:
    pages = [target for target in _targets(port) if target.get("type") == "page" and
             (urllib.parse.urlsplit(target.get("url") or "").hostname or "") in {"elevenlabs.io", "www.elevenlabs.io"}]
    if pages:
        return pages[0]
    raise BrowserError("open and log in to elevenlabs.io in the account browser")


def browser_alive(port: int) -> bool:
    try:
        _page(port)
        return True
    except BrowserError:
        return False


def _remove_stale_profile_locks(profile: Path) -> None:
    """Discard Chromium singleton links left by a stopped container."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        link = profile / name
        if link.is_symlink():
            link.unlink()


def start_managed_browser(account: dict[str, Any], *, chrome: str, profiles: Path) -> int:
    account_id = int(account["id"])
    port = 19300 + account_id
    profile = profiles / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,960",
            "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding"]
    if os.name != "nt":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    proxy_url = account_proxy_url(account)
    args.append("--proxy-server=" + proxy_url)
    args.append(BASE + "/app/image-video")
    with _PROCESS_LOCK:
        if browser_alive(port):
            _restore_managed_auth(account, port)
            return port
        existing = _PROCESSES.get(account_id)
        if existing and existing.poll() is None:
            existing.terminate()
            try:
                existing.wait(timeout=4)
            except subprocess.TimeoutExpired:
                existing.kill()
                existing.wait(timeout=4)
        _remove_stale_profile_locks(profile)
        _PROCESSES[account_id] = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if browser_alive(port):
                _restore_managed_auth(account, port)
                return port
            time.sleep(.1)
        try:
            pages = [item for item in _targets(port) if item.get("type") == "page"]
            hosts = {urllib.parse.urlsplit(str(item.get("url") or "")).hostname for item in pages}
            redirected = sorted(host for host in hosts if host and not host.endswith("elevenlabs.io"))
            if redirected:
                raise BrowserError(f"ElevenLabs browser reached {redirected[0]}; configure an account proxy")
        except BrowserError as exc:
            if "configure an account proxy" in str(exc):
                raise
    raise BrowserError("managed ElevenLabs Chrome did not start")


def close_browser_port(port: int) -> None:
    """Ask a local CDP browser to exit, including one orphaned by a restart."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            endpoint = json.load(response).get("webSocketDebuggerUrl")
        if endpoint:
            ws = websocket.create_connection(endpoint, timeout=2, suppress_origin=True)
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                ws.close()
    except (OSError, ValueError, KeyError, websocket.WebSocketException):
        pass


def stop_managed_browser(account_id: int) -> None:
    with _PROCESS_LOCK:
        process = _PROCESSES.pop(account_id, None)
    close_browser_port(19300 + account_id)
    if process and process.poll() is None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def clear_managed_profile(account: dict[str, Any], profiles: Path) -> None:
    """Delete only the service-owned profile for this numeric account ID."""
    account_id = int(account["id"])
    port = int(account.get("cdp_port") or 0)
    if port == 19300 + account_id and browser_alive(port):
        try:
            CDPBrowser(port).command("Browser.close")
        except BrowserError:
            pass
        for _ in range(30):
            if not browser_alive(port):
                break
            time.sleep(.1)
        if browser_alive(port):
            raise BrowserError("managed Chrome is still running; profile was not removed")
    stop_managed_browser(account_id)
    root = profiles.resolve()
    profile = (root / str(account_id)).resolve()
    if profile.parent != root:
        raise BrowserError("profile path is outside the configured profile root")
    if profile.exists():
        shutil.rmtree(profile)


class CDPBrowser:
    def __init__(self, port: int, timeout: float = 90) -> None:
        self.port = int(port)
        self.timeout = timeout

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        target = _page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=self.timeout,
                                         suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                result = json.loads(ws.recv())
                if result.get("id") != 1:
                    continue
                if "error" in result:
                    raise BrowserError(f"CDP {method}: {result['error'].get('message', 'unknown error')}")
                return result.get("result") or {}
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"CDP {method} failed: {type(exc).__name__}") from exc
        finally:
            ws.close()

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression,
                                                   "awaitPromise": await_promise,
                                                   "returnByValue": True})
        if "exceptionDetails" in result:
            raise BrowserError("ElevenLabs browser JavaScript request failed")
        value = result.get("result") or {}
        if value.get("subtype") == "error":
            raise BrowserError("ElevenLabs browser JavaScript request returned an error")
        return value.get("value")

    def request(self, path: str, body: dict[str, Any] | None = None, *,
                method: str = "GET", headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Authenticated same-origin request through this account's real browser."""
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("ElevenLabs API path must be same-origin")
        options: dict[str, Any] = {"method": method.upper(), "credentials": "include",
                                   "headers": {"content-type": "application/json", **(headers or {})}}
        if body is not None:
            options["body"] = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        expression = ("(async()=>{const r=await fetch(" + json.dumps(path) + "," +
                      json.dumps(options, ensure_ascii=True) +
                      ");return {http:r.status,body:await r.json()}})()")
        value = self.evaluate(expression, await_promise=True)
        if not isinstance(value, dict):
            raise BrowserError("ElevenLabs returned an unreadable JSON response")
        if int(value.get("http") or 0) >= 400:
            body_value = value.get("body") or {}
            message = str(body_value.get("message") or body_value.get("msg") or "upstream rejection")
            raise BrowserRejected(f"ElevenLabs HTTP {value['http']}: {message[:300]}")
        body_value = value.get("body")
        if not isinstance(body_value, dict):
            raise BrowserError("ElevenLabs returned an invalid response envelope")
        return body_value

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request(path, body, method="POST")

    def collect_auth(self) -> dict[str, Any]:
        """Read only this profile's website identity; never log or expose it publicly."""
        cookies = self.cookies()
        cookie = "; ".join(str(row["name"]) + "=" + str(row["value"]) for row in cookies
                           if str(row.get("domain") or "").lstrip(".") in {"elevenlabs.io", "www.elevenlabs.io"})
        page = self.evaluate(r'''(async()=>{
          const rows=[];
          try {
            const dbs=await indexedDB.databases();
            if(dbs.some(d=>d.name==='firebaseLocalStorageDb')) {
              const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('firebaseLocalStorageDb');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(Error('db'));});
              if(db.objectStoreNames.contains('firebaseLocalStorage')) {
                rows.push(...await new Promise((resolve,reject)=>{const r=db.transaction('firebaseLocalStorage').objectStore('firebaseLocalStorage').getAll();r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(Error('read'));}));
              }
              db.close();
            }
          } catch (_) {}
          for(let i=0;i<localStorage.length;i++) {
            const key=localStorage.key(i);
            if(key?.startsWith('firebase:authUser:')) {try{rows.push({fbase_key:key,value:JSON.parse(localStorage.getItem(key))});}catch(_){}}
          }
          const record=rows.find(r=>String(r.fbase_key||'').startsWith('firebase:authUser:')&&r.value?.stsTokenManager?.accessToken);
          const value=record?.value||{}, manager=value.stsTokenManager||{};
          return {access_token:manager.accessToken||'',refresh_token:manager.refreshToken||'',
            token_expires_at:Number(manager.expirationTime||0)/1000,firebase_api_key:value.apiKey||'',user_agent:navigator.userAgent};
        })()''', await_promise=True) or {}
        return {"cookie": cookie, "user_agent": str(page.get("user_agent") or ""),
                "access_token": str(page.get("access_token") or ""),
                "refresh_token": str(page.get("refresh_token") or ""),
                "firebase_api_key": str(page.get("firebase_api_key") or ""),
                "token_expires_at": float(page.get("token_expires_at") or 0)}

    def restore_auth(self, account: dict[str, Any]) -> None:
        """Seed an imported authorized Firebase session into a fresh owned profile."""
        auth = dict(account)
        if float(auth.get("token_expires_at") or 0) <= time.time() + 60:
            auth.update(refresh_firebase_auth(auth))
        token = str(auth.get("access_token") or "")
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            uid = str(claims.get("user_id") or claims.get("sub") or "")
        except (ValueError, IndexError, TypeError) as exc:
            raise BrowserRejected("LOGIN_REQUIRED: invalid imported Firebase session") from exc
        if not uid or not auth.get("firebase_api_key"):
            raise BrowserRejected("LOGIN_REQUIRED: incomplete imported Firebase session")
        # Claims only reconstruct the SDK's local state. The upstream validates
        # the actual token on authenticated API requests.
        value = {"uid": uid, "email": claims.get("email"), "emailVerified": bool(claims.get("email_verified")),
                 "isAnonymous": False, "providerData": [], "apiKey": auth["firebase_api_key"], "appName": "[DEFAULT]",
                 "stsTokenManager": {"accessToken": token, "refreshToken": auth.get("refresh_token") or "",
                                     "expirationTime": float(auth["token_expires_at"]) * 1000}}
        record = {"fbase_key": "firebase:authUser:" + str(auth["firebase_api_key"]) + ":[DEFAULT]", "value": value}
        expression = r'''(async()=>{const record=RECORD;
          const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('firebaseLocalStorageDb',1);
            r.onupgradeneeded=()=>{if(!r.result.objectStoreNames.contains('firebaseLocalStorage'))r.result.createObjectStore('firebaseLocalStorage',{keyPath:'fbase_key'});};
            r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(Error('session storage'));});
          await new Promise((resolve,reject)=>{const t=db.transaction('firebaseLocalStorage','readwrite');t.objectStore('firebaseLocalStorage').put(record);t.oncomplete=resolve;t.onerror=()=>reject(Error('session write'));});
          db.close();return true;})()'''.replace("RECORD", json.dumps(record, ensure_ascii=True))
        if not self.evaluate(expression, await_promise=True):
            raise BrowserError("could not restore the authorized browser session")
        self.command("Page.navigate", {"url": BASE + "/app/image-video"})

    def hcaptcha_token(self) -> str:
        """Execute the normal website CAPTCHA; challenges remain normal challenges."""
        with _PROCESS_LOCK:
            guard = _CAPTCHA_LOCKS.setdefault(self.port, threading.RLock())
        expression = r'''(async()=>{
          if(!window.hcaptcha?.render&&!document.getElementById('__ele_api_hcaptcha_sdk')) {
            const script=document.createElement('script');script.id='__ele_api_hcaptcha_sdk';script.async=true;script.defer=true;
            script.src='https://js.hcaptcha.com/1/api.js?render=explicit&recaptchacompat=off';
            document.head.appendChild(script);
          }
          const deadline=Date.now()+20000;
          while(!window.hcaptcha?.render&&Date.now()<deadline)await new Promise(r=>setTimeout(r,200));
          if(!window.hcaptcha?.render)return {error:'CAPTCHA_REQUIRED'};
          try {
            let mount=document.getElementById('__ele_api_captcha');
            if(!mount){mount=document.createElement('div');mount.id='__ele_api_captcha';document.body.appendChild(mount);}
            if(window.__eleApiCaptchaId===undefined)window.__eleApiCaptchaId=hcaptcha.render(mount,{sitekey:SITEKEY,size:'invisible'});
            const widget=window.__eleApiCaptchaId;
            hcaptcha.reset(widget);
            const answer=await Promise.race([hcaptcha.execute(widget,{async:true}),new Promise((_,reject)=>setTimeout(()=>reject(Error('challenge required')),60000))]);
            const token=answer?.response||hcaptcha.getResponse(widget);
            return token?{token}:{error:'CAPTCHA_REQUIRED'};
          } catch(_){return {error:'CAPTCHA_REQUIRED'};}
        })()'''.replace("SITEKEY", json.dumps(GENERATION_SITEKEY))
        with guard:
            answer = self.evaluate(expression, await_promise=True)
        token = (answer or {}).get("token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or len(token) < 16:
            raise BrowserRejected("CAPTCHA_REQUIRED: complete the website's normal browser verification")
        return token

    def _field(self, selector: str) -> dict[str, float] | None:
        return self.evaluate("(()=>{" + _VISIBLE_DOM + "return point(find(document," + json.dumps(selector) + "));})()")

    def _login_form_state(self, email: str, password: str) -> dict[str, Any]:
        # Values stay in this profile's execution context; only equality and
        # validity booleans leave it. Recheck after every navigation/hydration.
        expression = ("(()=>{const expectedEmail=" + json.dumps(email.strip()) + ";const expectedPassword=" +
                      json.dumps(password) + ";" + _VISIBLE_DOM + r'''
          const email=find(document,'input[type="email"],input[name="email"],input[autocomplete="email"]');
          const password=find(document,'input[type="password"]');
          const form=password?.closest('form')||email?.closest('form');
          const buttons=form?[...form.querySelectorAll('button')].filter(e=>
            /^(sign in|log in|login|continue|next|登录|登入|继续|下一步)$/i.test(text(e))):[];
          const submit=buttons.find(visible);
          const emailMatch=!!email&&email.value.trim().toLowerCase()===expectedEmail.toLowerCase();
          const passwordMatch=!!password&&password.value===expectedPassword;
          const alerts=[...document.querySelectorAll('[role="alert"]')].filter(visible);
          return {document_id:String(performance.timeOrigin),email_present:!!email,password_present:!!password,
            email_matches:emailMatch,password_matches:passwordMatch,
            email_valid:emailMatch&&email.validity.valid,password_valid:passwordMatch&&password.validity.valid,
            submit_enabled:!!submit,challenge_overlay:challengeOverlay,challenge_widget:challengeWidget,
            login_error:alerts.some(e=>/invalid (email|password|credentials)|incorrect (email|password)|wrong password|too many (attempts|requests)/i.test(text(e)))};
        })()''')
        value = self.evaluate(expression)
        if not isinstance(value, dict):
            raise BrowserError("the sign-in form is still initializing")
        return value

    def _fill_login_field(self, selector: str, text: str) -> bool:
        point = self._field(selector)
        if not point:
            return False
        self.click(point["x"], point["y"])
        self.command("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2,
                                                 "windowsVirtualKeyCode": 65})
        self.command("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2,
                                                 "windowsVirtualKeyCode": 65})
        self.command("Input.insertText", {"text": text})
        return bool(self.evaluate("(()=>{" + _VISIBLE_DOM + "const e=find(document," + json.dumps(selector)
                                 + ");return !!e&&e.value===" + json.dumps(text) + ";})()"))

    def _click_login_submit(self) -> bool:
        point = self.evaluate(r'''(()=>{VISIBLE
          const input=find(document,'input[type="password"]')||find(document,'input[type="email"],input[name="email"]');
          const form=input?.closest('form');if(!form)return null;
          return point([...form.querySelectorAll('button')].find(e=>visible(e)&&
            /^(sign in|log in|login|continue|next|登录|登入|继续|下一步)$/i.test(text(e))));
        })()'''.replace("VISIBLE", _VISIBLE_DOM))
        if point:
            self.click(point["x"], point["y"])
        return bool(point)

    @staticmethod
    def _login_identity(auth: dict[str, Any], email: str) -> dict[str, Any]:
        try:
            part = str(auth["access_token"]).split(".")[1]
            existing = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            current_email = str(existing.get("email") or "")
        except (ValueError, IndexError, TypeError, KeyError):
            current_email = ""
        if not current_email or current_email.casefold() != email.strip().casefold():
            raise BrowserRejected("LOGIN_FAILED: browser identity differs from the imported account")
        return auth

    @staticmethod
    def _auth_email(auth: dict[str, Any]) -> str:
        try:
            part = str(auth["access_token"]).split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            return str(claims.get("email") or "").strip()
        except (ValueError, IndexError, TypeError, KeyError):
            return ""

    def _onboarding_complete_on_server(self, auth: dict[str, Any]) -> bool:
        expression = r'''(async()=>{
          const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
          try{const response=await fetch(ENDPOINT,{method:'GET',credentials:'omit',signal:controller.signal,
            headers:{Authorization:'Bearer '+TOKEN,Accept:'application/json'}});
            if(response.status===401||response.status===403)return {http:response.status};
            if(!response.ok)return {http:response.status};
            const body=await response.json();return {http:response.status,complete:body.is_onboarding_completed===true};
          }finally{clearTimeout(timer);}
        })()'''.replace("ENDPOINT", json.dumps(API_BASE + "/v1/user/internal")).replace(
            "TOKEN", json.dumps(str(auth.get("access_token") or "")))
        result = self.evaluate(expression, await_promise=True)
        if not isinstance(result, dict):
            raise BrowserError("onboarding verification returned no response")
        if result.get("http") in {401, 403}:
            raise BrowserRejected("LOGIN_REQUIRED: website authorization needs renewal")
        if result.get("http") != 200:
            raise BrowserError("onboarding verification is temporarily unavailable")
        return result.get("complete") is True

    @contextmanager
    def _ui_step(self):
        # Separate profiles share one desktop. Only the brief visible DOM action
        # owns focus; authentication HTTP checks and polling never hold this lock.
        with _UI_LOCK:
            previous_timeout = self.timeout
            try:
                self.timeout = min(previous_timeout, 5)
                self.command("Page.bringToFront")
                time.sleep(.25)  # Allow the website's normal 200ms entrance animation.
                yield
            finally:
                self.timeout = previous_timeout

    def _onboarding_view(self) -> dict[str, Any]:
        expression = r'''(()=>{VISIBLE WELCOME
          if(challengeOverlay)return {stage:'captcha',controls:{}};
          const dialogs=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"],dialog[open]')].filter(visible);
          const root=dialogs.at(-1)||document.querySelector('main')||document.body;
          const controls={};
          const add=(key,e)=>{const p=point(e);if(p)controls[key]={...p,selected:chosen(e)};};
          const exact=(pattern,selector='button,label')=>[...root.querySelectorAll(selector)].find(e=>visible(e)&&pattern.test(text(e)));
          const next=scope=>[...scope.querySelectorAll('button')].find(e=>visible(e)&&/^(next|continue|下一步|继续)$/i.test(text(e)));
          if(creatorWelcome(root)){
            const close=[...root.querySelectorAll('button')].find(e=>rendered(e)&&text(e)==='Close');
            add('close',close);return {stage:'creator-welcome',controls,needs_scroll:!!close&&!controls.close};
          }
          const platform=find(root,'[data-agent-id="onboarding-platform-elevencreative"],button[aria-label="ElevenCreative"]');
          if(platform){add('platform',platform);add('next',next(root));return {stage:'platform',controls};}
          const adult=root.querySelector('input[name="adult"],[role="checkbox"][name="adult"],button[name="adult"]');
          const checkbox=e=>{
            if(!e||e.disabled)return null;if(visible(e))return e;
            const labels=[...(e.labels||[]),e.closest('label')].filter(Boolean);
            for(const label of new Set(labels)){
              // Radix can associate the wrapper with its preceding button,
              // leaving this named hidden input's native labels list empty.
              const buttons=[...label.querySelectorAll('[role="checkbox"]')].filter(visible);
              if(buttons.length===1)return buttons[0];
              if(visible(label)&&label.querySelectorAll('input,button,select,textarea').length===1)return label;
            }
            return null;
          };
          const personal=find(root,'input#firstname,input[name="firstname"],input#source-freetext,input[name="source-freetext"]');
          const personalForm=personal?.closest('form');
          if((adult&&checkbox(adult))||(personalForm&&next(personalForm))){
            const form=adult?.closest('form')||personalForm||root;
            const blocked=[];
            for(const key of ['adult','terms','updated']){
              const e=form.querySelector('[name="'+key+'"]'),target=checkbox(e);
              if(target){add(key,target);if(controls[key])controls[key].selected=chosen(e)||chosen(target);}
              else if(e&&key!=='updated')blocked.push(key);
            }
            add('next',next(form));return {stage:'personal-info',controls,blocked_controls:blocked};
          }
          const creator=find(root,'[data-agent-id="onboarding-icon-option-creator"],[data-agent-id="onboarding-persona-creator"]')||
            exact(/^(creator|creator content|content creator)$/i);
          if(creator){add('creator',creator);add('next',next(root));return {stage:'persona',controls};}
          const youtube=exact(/^YouTube videos$/i),shorts=exact(/^TikTok\s*\/\s*Shorts\s*\/\s*Reels$/i);
          if(youtube||shorts){add('youtube',youtube);add('shorts',shorts);add('next',next(root));
            add('skip',exact(/^skip$/i,'button'));return {stage:'persona-details',controls};}
          const interest=find(root,'[data-agent-id="onboarding-illustration-option-image-video"],button[aria-label="Image & Video"]');
          if(interest){add('image-video',interest);add('next',next(root));return {stage:'interests',controls};}
          const pricing=location.pathname==='/app/onboarding'&&!!exact(/^skip$/i,'button')&&
            [...root.querySelectorAll('h1,h2,h3,p,[data-agent-id]')].some(e=>visible(e)&&
            (/choose.{0,24}plan|pick.{0,24}plan|pricing|upgrade.{0,24}plan|select.{0,24}plan|creator is the best way to get started|pay as you go on the creator plan/i.test(text(e))||
             /^onboarding-pricing/.test(e.getAttribute('data-agent-id')||'')));
          if(pricing){add('skip',exact(/^skip$/i,'button'));return {stage:'pricing',controls};}
          if(document.readyState==='loading'||!document.body?.innerText?.trim())return {stage:'loading',controls};
          return {stage:dialogs.length?'unknown-dialog':'waiting',onboarding_path:location.pathname==='/app/onboarding',controls};
        })()'''.replace("VISIBLE", _VISIBLE_DOM).replace("WELCOME", _CREATOR_WELCOME_DOM)
        result = self.evaluate(expression)
        if not isinstance(result, dict):
            raise BrowserError("onboarding page is still initializing")
        return result

    def _scroll_creator_welcome_close(self) -> bool:
        """Reveal the known dialog's Close control, then let the caller re-read it."""
        expression = r'''(()=>{VISIBLE WELCOME
          const root=[...document.querySelectorAll('[role="dialog"]')].filter(visible).at(-1);
          if(!creatorWelcome(root))return false;
          const close=[...root.querySelectorAll('button')].find(e=>rendered(e)&&text(e)==='Close');
          if(!close||visible(close))return false;
          close.scrollIntoView({block:'center',inline:'nearest',behavior:'auto'});return true;
        })()'''.replace("VISIBLE", _VISIBLE_DOM).replace("WELCOME", _CREATOR_WELCOME_DOM)
        return self.evaluate(expression) is True

    def complete_onboarding(self, auth: dict[str, Any] | None = None, *, expected_email: str = "") -> dict[str, Any]:
        """Complete only captured normal UI steps; the server decides completion."""
        auth = dict(auth or self.collect_auth())
        email = expected_email.strip() or self._auth_email(auth)
        self._login_identity(auth, email)
        deadline, next_verify = time.monotonic() + 120, 0.0
        acted_in_flow = False
        last_action: tuple[str, str] | None = None
        unknown_since: float | None = None
        entered_onboarding = False
        welcome_scrolls = 0
        last_stage = None
        browser_failed = False
        while time.monotonic() < deadline:
            try:
                current = self.collect_auth()
                if not current.get("access_token"):
                    time.sleep(.4)
                    continue
                self._login_identity(current, email)
                auth.update({key: value for key, value in current.items() if value not in (None, "", 0)})
                if time.monotonic() >= next_verify:
                    if self._onboarding_complete_on_server(auth):
                        return auth
                    next_verify = time.monotonic() + 1.5
                with self._ui_step():
                    view = self._onboarding_view()
                    stage, controls = view.get("stage"), view.get("controls") or {}
                    last_stage = stage
                    browser_failed = False
                    if stage == "captcha":
                        raise BrowserRejected("CAPTCHA_REQUIRED: complete normal website verification")
                    if stage == "creator-welcome" and view.get("needs_scroll") and welcome_scrolls < 2:
                        welcome_scrolls += 1
                        self._scroll_creator_welcome_close()
                        time.sleep(.4)
                        continue
                    if (stage == "waiting" and not view.get("onboarding_path") and not entered_onboarding
                            and not acted_in_flow):
                        # A saved local completion flag is not proof of server
                        # readiness. Enter the website's ordinary captured flow.
                        self.command("Page.navigate", {"url": BASE + "/app/onboarding"})
                        entered_onboarding = True
                        unknown_since = None
                        time.sleep(.4)
                        continue
                    choice = None
                    if stage == "platform":
                        choice = "next" if controls.get("platform", {}).get("selected") else "platform"
                    elif stage == "personal-info":
                        if view.get("blocked_controls"):
                            choice = None  # Never submit before a named required control can be resolved.
                        elif controls.get("updated", {}).get("selected"):
                            choice = "updated"  # Opt out through the actual checkbox; never opt in.
                        else:
                            choice = next((key for key in ("adult", "terms")
                                           if key in controls and not controls[key].get("selected")), "next")
                    elif stage in {"persona", "interests", "persona-details"}:
                        keys = {"persona": ("creator",), "interests": ("image-video",),
                                "persona-details": ("youtube", "shorts")}[stage]
                        choice = next((key for key in keys if key in controls and not controls[key].get("selected")), "next")
                        if choice == "next" and choice not in controls and stage == "persona-details":
                            choice = "skip"
                    elif stage == "pricing":
                        choice = "skip"  # No plan, trial, purchase or upgrade action exists here.
                    elif stage == "creator-welcome":
                        choice = "close"  # Captured subscription-benefits notice; no 2FA action.
                    action = controls.get(choice) if choice else None
                    key = (str(stage), str(choice))
                    if action and (key != last_action or choice in {"adult", "terms", "updated"}):
                        self.click(float(action["x"]), float(action["y"]))
                        if stage != "creator-welcome":
                            acted_in_flow = True
                        last_action = key
                        unknown_since = None
                    elif stage in {"unknown-dialog", "waiting"} or view.get("blocked_controls") or (choice and not action):
                        if unknown_since is None:
                            unknown_since = time.monotonic()
                        if time.monotonic() - unknown_since >= 6:
                            raise BrowserRejected("ONBOARDING_REQUIRED: an unknown or incomplete website step needs review")
                time.sleep(.4)
            except BrowserRejected:
                raise
            except BrowserError:
                # Navigation, hydration and the final normal save are asynchronous.
                browser_failed = True
                time.sleep(.4)
        if last_stage == "loading" and not browser_failed:
            raise BrowserRejected("ONBOARDING_LOADING_TIMEOUT: the authorized website is still loading")
        if browser_failed:
            # Retry permission needs a current, ordinary page with no unknown
            # dialog or unresolved required control; a network error alone is
            # insufficient evidence that the UI is safe to resume.
            try:
                with self._ui_step():
                    final_view = self._onboarding_view()
                if (final_view.get("stage") in {"loading", "platform", "personal-info", "persona",
                                               "persona-details", "interests", "pricing", "creator-welcome"}
                        and not final_view.get("blocked_controls")):
                    raise BrowserError("ONBOARDING_NETWORK_ERROR: authorized website verification is temporarily unavailable")
            except BrowserRejected:
                raise
            except BrowserError as exc:
                if str(exc).startswith("ONBOARDING_NETWORK_ERROR:"):
                    raise
        raise BrowserRejected("ONBOARDING_REQUIRED: normal website onboarding did not finish within its time limit")

    def login_with_password(self, email: str, password: str) -> dict[str, Any]:
        """Fill and submit the ordinary sign-in form, retaining normal verification."""
        deadline = time.monotonic() + 100
        navigated, submitted = False, set()
        loader_id = None
        while time.monotonic() < deadline:
            try:
                auth = self.collect_auth()
                if auth.get("access_token"):
                    return self.complete_onboarding(self._login_identity(auth, email), expected_email=email)
                if not navigated:
                    loader_id = self.command("Page.navigate", {"url": BASE + "/app/sign-in"}).get("loaderId")
                    navigated = True
                if loader_id:
                    if self.command("Page.getFrameTree").get("frameTree", {}).get("frame", {}).get("loaderId") != loader_id:
                        time.sleep(.3)
                        continue
                    loader_id = None
                with self._ui_step():
                    state = self._login_form_state(email, password)
                    if state.get("login_error"):
                        raise BrowserRejected("LOGIN_FAILED: the website rejected the ordinary sign-in")
                    if state.get("challenge_overlay"):
                        raise BrowserRejected("CAPTCHA_REQUIRED: complete normal website verification")
                    if state.get("email_present") and not state.get("email_matches"):
                        self._fill_login_field('input[type="email"],input[name="email"],input[autocomplete="email"]', email)
                    if state.get("password_present") and not state.get("password_matches"):
                        self._fill_login_field('input[type="password"]', password)
                    state = self._login_form_state(email, password)
                    stage = "password" if state.get("password_present") else "email"
                    valid = (state.get("password_valid") and (state.get("email_valid") or not state.get("email_present"))
                             if stage == "password" else state.get("email_valid"))
                    key = (state.get("document_id"), stage)
                    if valid and state.get("submit_enabled") and key not in submitted:
                        if self._click_login_submit():
                            submitted.add(key)
                    elif valid and state.get("challenge_widget") and not state.get("submit_enabled"):
                        raise BrowserRejected("CAPTCHA_REQUIRED: complete normal website verification")
            except BrowserRejected:
                raise
            except BrowserError:
                # A normal form submit can replace its execution context.
                pass
            time.sleep(.5)
        raise BrowserRejected("LOGIN_FAILED: the ordinary email/password sign-in did not complete")

    def screenshot(self) -> str:
        result = self.command("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        if not result.get("data"):
            raise BrowserError("ElevenLabs Chrome returned no screenshot")
        return "data:image/png;base64," + result["data"]

    def click(self, x: float, y: float) -> None:
        for count, kind in enumerate(("mousePressed", "mouseReleased"), 1):
            self.command("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                      "button": "left", "clickCount": 1})

    def scroll(self, delta_y: float) -> None:
        self.command("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 700, "y": 500,
                                                  "deltaX": 0, "deltaY": delta_y})

    def cookies(self) -> list[dict[str, Any]]:
        return self.command("Network.getCookies", {"urls": [BASE + "/"]}).get("cookies") or []
