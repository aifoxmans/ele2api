"""Use an authenticated native Chromium tab as the ElevenLabs Web API transport."""

from __future__ import annotations

import base64
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
        if not browser.collect_auth().get("access_token"):
            browser.restore_auth(account)


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
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,960"]
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
        return self.evaluate("(()=>{const es=[...document.querySelectorAll(" + json.dumps(selector) + ")];"
            "const e=es.find(e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&!e.disabled;});"
            "if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")

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
        return True

    def _click_login_submit(self) -> bool:
        point = self.evaluate(r'''(()=>{
          const visible=e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&!e.disabled;};
          const buttons=[...document.querySelectorAll('button[type="submit"],button')].filter(visible);
          const e=buttons.find(e=>/^(sign in|log in|login|continue|登录|登入|继续)$/i.test(e.innerText.trim()));
          if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()''')
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

    def login_with_password(self, email: str, password: str) -> dict[str, Any]:
        """Fill and submit the ordinary sign-in form, retaining normal verification."""
        auth = self.collect_auth()
        if auth.get("access_token"):
            return self._login_identity(auth, email)
        self.command("Page.navigate", {"url": BASE + "/app/sign-in"})
        deadline = time.monotonic() + 100
        filled_email = filled_password = email_submitted = password_submitted = False
        while time.monotonic() < deadline:
            try:
                auth = self.collect_auth()
                if auth.get("access_token"):
                    return self._login_identity(auth, email)
                if not filled_email:
                    filled_email = self._fill_login_field('input[type="email"],input[name="email"],input[autocomplete="email"]', email)
                if filled_email and not filled_password:
                    filled_password = self._fill_login_field('input[type="password"]', password)
                    if not filled_password and not email_submitted:
                        email_submitted = self._click_login_submit()
                if filled_password and not password_submitted:
                    if self._click_login_submit():
                        password_submitted = True
            except BrowserRejected:
                raise
            except BrowserError:
                # A normal form submit can replace its execution context.
                pass
            time.sleep(.5)
        challenge = self.evaluate("[...document.querySelectorAll('iframe[src*=\"hcaptcha\"]')].some(e=>{const r=e.getBoundingClientRect();return r.width>150&&r.height>100;})")
        if challenge:
            raise BrowserRejected("CAPTCHA_REQUIRED: the account needs the website's normal verification")
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
