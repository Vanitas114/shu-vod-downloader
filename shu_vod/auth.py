"""SSO/2FA and WebVPN handshake adapted from vendor/shu-sso-poc (AGPL-3.0).

VOD CAS exchange follows vendor/ShuYo's VodNativeSessionService (GPL-3.0).
Passwords, 2FA codes, OAuth codes and response traces are never persisted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .accounts import AccountRef, AccountStore, SessionState
from .errors import AuthError, SessionExpired, VodError
from .transport import (API_PATH, MAPPING, SSO, UI_PATH, VOD_HOST, VPN_HOST,
                        Transport, normalize_auth, validate_auth)

FALLBACK_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDl/aCgRl9f/4ON9MewoVnV58OL
OU2ALBi2FKc5yIsfSpivKxe7A6FitJjHva3WpM7gvVOinMehp6if2UNIkbaN+plW
f5IwqEVxsNZpeixc4GsbY9dXEk3WtRjwGSyDLySzEESH/kpJVoxO7ijRYqU+2oSR
wTBNePOk1H+LRQokgQIDAQAB
-----END PUBLIC KEY-----"""
VPN_CLIENT = "nn7sbb22j2tKE100T024tEp42777p755"


def encoded(value: dict, *, urlsafe: bool = False) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if urlsafe:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return base64.b64encode(raw).decode()


def public_key(http: Transport):
    """Discover the login chunk using POC's patterns, preserving TLS verification."""
    html = http.request("GET", SSO + "/oauth2/login/").text
    manifests = re.findall(r'''["'](/(?:preload_helper|umi)\.[0-9a-f]+\.js)["']''', html)
    for manifest in manifests:
        text = http.request("GET", SSO + manifest).text
        chunk = re.search(r'''["'](/?p__oauth2__login__index\.[0-9a-f]+\.async\.js)["']''', text)
        if not chunk:
            continue
        bundle = http.request("GET", SSO + "/" + chunk[1].lstrip("/")).text
        key = re.search(r"-----BEGIN PUBLIC KEY-----[\s\S]*?-----END PUBLIC KEY-----", bundle)
        if key:
            pem = key[0].replace("\\n", "\n").encode()
            try:
                return serialization.load_pem_public_key(pem)
            except ValueError:
                continue
    return serialization.load_pem_public_key(FALLBACK_PEM)


def password_login(http: Transport, username: str, password: str, method: str,
                   read_code, report) -> None:
    if not username.strip() or not password:
        raise AuthError("学号/工号和密码不能为空。")
    encrypted = base64.b64encode(public_key(http).encrypt(password.encode(), padding.PKCS1v15())).decode()
    params = encoded({
        "responseType": "code", "clientId": "Km5t225E8KECKQ6ZDm5K2P6aS2459Cua",
        "clientName": "本科生教务系统", "scope": "jw",
        "redirectUri": "https://jwxt.shu.edu.cn/sso/shulogin", "state": "",
    }, urlsafe=True)
    body = {"username": username, "password": encrypted, "tenantId": "上海大学", "params": params}
    headers = {"Origin": SSO, "Referer": SSO + "/"}

    def post(path, payload, extra=None):
        value = http.json("POST", SSO + path, json=payload, headers={**headers, **(extra or {})})
        if not isinstance(value, dict) or value.get("message") != "success":
            raise AuthError("统一认证未通过，请检查凭据、验证码或校园账户状态。")
        return value

    data = post("/oauth/userLogin", body, {"Request-Id": str(uuid.uuid4())})
    if data.get("twoStepRequired"):
        methods = data.get("twoStepMethods") or {}
        if not isinstance(methods, dict) or method not in methods:
            raise AuthError("所选两步验证方式不可用，请用 --method wecom 或 --method sms 重新登录。")
        post("/oauth/twoStep/send", {"method": method})
        report("验证码已发送，请完成两步验证。")
        code = read_code().strip()
        if not code:
            raise AuthError("验证码不能为空。")
        post("/oauth/twoStep/verify", {**body, "code": code, "method": method})
    if not any(c.name == "SHU_OAUTH2" and c.value and c.domain.lstrip(".") == "newsso.shu.edu.cn"
               and not c.is_expired() for c in http.session.cookies):
        raise AuthError("统一认证未返回 SSO 会话，请重新登录。")


def webvpn_login(http: Transport, username: str) -> None:
    base = "https://" + VPN_HOST
    headers = {"Origin": base, "Referer": base + "/"}

    def call(method, path, **kwargs):
        value = http.json(method, base + "/api/access/" + path, headers=headers, **kwargs)
        if not isinstance(value, dict) or str(value.get("code")) != "0":
            raise AuthError("WebVPN 握手失败，请重新登录。")
        data = value.get("data") or {}
        if not isinstance(data, dict):
            raise AuthError("WebVPN 返回的数据不完整。")
        return data

    methods = call("GET", "authentication/list", params={"type": 0}).get("list", [])
    if not isinstance(methods, list):
        raise AuthError("WebVPN 认证方式列表不完整。")
    external = next((v.get("externalId") for v in methods
                     if isinstance(v, dict) and v.get("authType") == 5), None)
    if not external:
        raise AuthError("WebVPN 未提供 OAuth 认证方式。")
    state = encoded({"externalId": external})
    callback = base + "/callback/oauth2"
    payload = {"callbackUrl": callback, "state": state}
    call("POST", "auth/start", json={"externalId": external, "data": json.dumps(payload)})
    response = http.request("GET", SSO + "/oauth/authorize", params={
        "response_type": "code", "client_id": VPN_CLIENT, "redirect_uri": callback,
        "state": state, "access_type": "offline",
    })
    location = urlsplit(response.headers.get("Location", ""))
    query = parse_qs(location.query)
    code = query.get("code", [None])[0]
    if (response.status_code not in {301, 302, 303, 307, 308} or not code
            or location.scheme != "https" or location.netloc != VPN_HOST
            or location.path != "/callback/oauth2" or query.get("state", [None])[0] != state):
        raise SessionExpired("WebVPN 授权未完成，请重新登录。")
    device = hashlib.md5(("shu-sso-poc::" + username.lower()).encode()).hexdigest()
    call("POST", "auth/finish", json={"externalId": external, "data": json.dumps({
        **payload, "code": code, "deviceId": device,
    })})
    user = call("GET", "user/info")
    if not user.get("userId") or not http.gateway_cookie():
        raise AuthError("WebVPN 尚未建立有效会话。")
    if any(user.get(key) for key in ("needTriggerTFA", "needChangePwd", "needToBindLocalAccount")):
        raise AuthError("请先在 WebVPN 网页完成二次验证、改密或账号绑定，再重新登录。")


class AuthManager:
    def __init__(self, http: Transport, store: AccountStore, account: AccountRef = AccountRef()):
        self.http = http
        self.store = store
        self.account = account
        self.username = ""
        self.vpn = False
        self.jwt = ""

    @property
    def base(self) -> str:
        return "https://" + (MAPPING[(VOD_HOST, 443)] if self.vpn else VOD_HOST)

    def restore(self):
        state = self.store.load(self.account)
        self.username, self.jwt = state.username, state.jwt
        self.vpn = state.transport == "webvpn"
        self.http.session.cookies.clear()
        try:
            for cookie in state.cookies:
                self.http.session.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
        except (TypeError, ValueError):
            raise AuthError("已保存的 Cookie 无效，请重新登录。") from None

    def save(self):
        cookies = []
        for c in self.http.session.cookies:
            if not c.is_expired():
                cookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                                "path": c.path, "secure": c.secure, "expires": c.expires,
                                "rest": dict(c._rest)})
        self.store.save(self.account, SessionState(self.username, "webvpn" if self.vpn else "direct",
                                                   self.jwt, cookies))

    def login(self, username, password, *, vpn, method, read_code, report):
        self.http.session.cookies.clear()
        self.username, self.vpn, self.jwt = username.strip(), vpn, ""
        password_login(self.http, self.username, password, method, read_code, report)
        self.refresh()

    def refresh(self):
        self.jwt = ""
        if self.vpn:
            webvpn_login(self.http, self.username)
        url = self.base + API_PATH + "/oauth2/authorize?" + urlencode({
            "json": "0", "returnUri": self.base + UI_PATH + "/?type=cas",
        })
        for _ in range(16):
            url = normalize_auth(url, self.vpn)
            validate_auth(url, self.vpn)
            parsed = urlsplit(url)
            if parsed.hostname == urlsplit(self.base).hostname and (
                    parsed.path == UI_PATH or parsed.path.startswith(UI_PATH + "/")):
                break
            headers = self.http.platform_headers(url, self.vpn) if parsed.hostname in MAPPING.values() else {}
            response = self.http.request("GET", url, headers=headers)
            if 300 <= response.status_code < 400 and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                continue
            if parsed.hostname == "newsso.shu.edu.cn":
                raise SessionExpired("校园登录已失效，请重新运行 shu-vod login。")
            raise AuthError(f"录播授权未完成（HTTP {response.status_code}）。")
        else:
            raise AuthError("录播认证跳转次数过多。")
        self._exchange("/authority/me")
        token = self._exchange("/oauth2/token").get("jwt_token")
        if not isinstance(token, str) or not token or any(ord(c) < 32 for c in token):
            raise AuthError("录播平台没有返回有效的登录凭据。")
        self.jwt = token
        user = self.http.json("GET", self.base + API_PATH + "/v1/currentuser",
                              headers=self.api_headers())
        data = user.get("data") if isinstance(user, dict) else None
        if (not isinstance(data, dict) or not data.get("id")
                or str(data.get("userCode", "")).strip() != self.username):
            self.jwt = ""
            raise AuthError("录播会话身份与登录账号不一致，请重新登录。")
        self.save()

    def _exchange(self, path):
        url = self.base + API_PATH + path
        value = self.http.json("GET", url, headers=self.http.platform_headers(url, self.vpn))
        if not isinstance(value, dict) or str(value.get("status")) != "200":
            raise AuthError("录播登录兑换失败。")
        result = value.get("result", value)
        if not isinstance(result, dict):
            raise AuthError("录播登录响应不完整。")
        return result

    def api_headers(self):
        return {"jwt-token": self.jwt, **self.http.platform_headers(self.base + API_PATH, self.vpn)}
