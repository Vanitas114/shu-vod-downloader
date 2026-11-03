from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import requests

from .errors import PermissionDenied, SessionExpired, VodError

SSO = "https://newsso.shu.edu.cn"
API_PATH = "/jy-application-resourcemanage"
UI_PATH = "/jy-application-resourcemanage-ui"
VOD_HOST = "vod.cc.shu.edu.cn"
CAS_HOST = "vrm.cc.shu.edu.cn"
MEDIA_HOST = "vrmvod.cc.shu.edu.cn"
VPN_HOST = "webvpn.shu.edu.cn"
MAPPING = {
    (VOD_HOST, 443): "https-vod-cc-shu-edu-cn-443.webvpn.shu.edu.cn",
    (CAS_HOST, 443): "https-vrm-cc-shu-edu-cn-443.webvpn.shu.edu.cn",
    (MEDIA_HOST, 40443): "https-vrmvod-cc-shu-edu-cn-40443.webvpn.shu.edu.cn",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36"


def safe_url(url: str):
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or not p.hostname or p.username or p.password
                or p.fragment or any(ord(c) <= 32 for c in url)):
            raise ValueError
        _ = p.port
        return p
    except ValueError:
        raise VodError("平台返回了不支持的地址。") from None


def map_url(url: str, vpn: bool) -> str:
    p = safe_url(url)
    host = MAPPING.get((p.hostname, p.port or 443)) if vpn else None
    return urlunsplit(p._replace(netloc=host)) if host else url


def media_url(url: str, vpn: bool) -> str:
    p = safe_url(url)
    media_hosts = {(VOD_HOST, 443), (MEDIA_HOST, 40443)}
    direct = (p.hostname, p.port or 443) in media_hosts
    proxied = p.hostname in {MAPPING[key] for key in media_hosts} and (p.port or 443) == 443
    if not direct and not (vpn and proxied):
        raise VodError("录播媒体地址不属于当前连接方式支持的平台域名。")
    return map_url(url, vpn)


def normalize_auth(url: str, vpn: bool) -> str:
    # SPA callbacks may append #/login or #/; fragments are not sent over HTTP.
    url = url.split("#", 1)[0]
    p = safe_url(url)
    if p.hostname in {"newsso.shu.edu.cn", "oauth.shu.edu.cn",
                      "https-newsso-shu-edu-cn-443.webvpn.shu.edu.cn",
                      "https-oauth-shu-edu-cn-443.webvpn.shu.edu.cn"}:
        if (p.port or 443) != 443:
            raise VodError("认证地址端口无效。")
        query = []
        for part in p.query.split("&"):
            callback = parse_qs(part).get("redirect_uri", [None])[0]
            if vpn and callback:
                c = safe_url(callback)
                if (c.hostname == MAPPING[(CAS_HOST, 443)] and (c.port or 443) == 443
                        and re.fullmatch(r"/jy-cas-proxy/proxy/callback/[a-fA-F0-9]{32}", c.path)):
                    part = "redirect_uri=" + quote(urlunsplit(c._replace(netloc=CAS_HOST)), safe="")
            query.append(part)
        url = urlunsplit(p._replace(netloc="newsso.shu.edu.cn", query="&".join(query)))
    return map_url(url, vpn)


def validate_auth(url: str, vpn: bool) -> None:
    p = safe_url(url)
    if (p.port or 443) != 443:
        raise VodError("认证地址端口无效。")
    if p.hostname == "newsso.shu.edu.cn":
        if p.path in {"/login", "/login/"} or p.path.startswith("/oauth2/login/"):
            raise SessionExpired("校园登录已失效，请重新运行 shu-vod login。")
        if p.path.startswith(("/oauth/", "/oauth2/")):
            return
    if p.hostname == (MAPPING[(VOD_HOST, 443)] if vpn else VOD_HOST):
        if any(p.path == prefix or p.path.startswith(prefix + "/")
               for prefix in (API_PATH, UI_PATH, "/cloud-rbac")):
            return
    if p.hostname == (MAPPING[(CAS_HOST, 443)] if vpn else CAS_HOST):
        if p.path == "/jy-cas-proxy/cas/login" or re.fullmatch(
                r"/jy-cas-proxy/proxy/callback/[a-fA-F0-9]{32}", p.path):
            return
    if p.hostname == VPN_HOST:
        raise SessionExpired("WebVPN 会话已失效，请重新运行 shu-vod login --transport webvpn。")
    raise VodError("录播认证返回了不支持的跳转地址。")


def decode_response(response):
    if response.status_code == 401 or 300 <= response.status_code < 400:
        raise SessionExpired("录播会话已失效。")
    if response.status_code == 403:
        raise PermissionDenied("当前账号没有该资源的访问权限。")
    if not 200 <= response.status_code < 300:
        raise VodError(f"平台请求失败（HTTP {response.status_code}）。")
    if response.text.lstrip().startswith("<"):
        if re.search(r"newsso\.shu|oauth2?/|#/login|统一身份认证|webvpn", response.text, re.I):
            raise SessionExpired("平台要求重新登录。")
        raise VodError("平台返回网页而非课程数据。")
    try:
        data = response.json()
    except ValueError:
        raise VodError("平台返回的数据不是有效 JSON。") from None
    if isinstance(data, dict):
        codes = {str(data.get(k, 0)) for k in ("status", "code")}
        if codes & {"401", "10013", "10014"}:
            raise SessionExpired("录播会话已失效。")
        if "403" in codes:
            raise PermissionDenied("当前账号没有该资源的访问权限。")
        if codes - {"0", "200", "None"} or data.get("ok") is False or data.get("dataOk") is False:
            raise VodError("平台未能完成请求，请稍后重试。")
    return data


class Transport:
    def __init__(self, *, verify: bool = True, timeout: int = 30, session=None):
        self.session = session or requests.Session()
        self.session.verify = verify
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
        self.timeout = timeout

    def request(self, method, url, **kwargs):
        try:
            return self.session.request(method, url, timeout=self.timeout,
                                        allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise VodError("TLS 证书验证失败。可设置 REQUESTS_CA_BUNDLE 为校园 CA；确认需要时使用 --insecure。") from None
        except requests.RequestException:
            raise VodError("网络请求失败或超时，请检查网络与 direct/webvpn 连接方式。") from None

    def json(self, method, url, **kwargs):
        return decode_response(self.request(method, url, **kwargs))

    def gateway_cookie(self) -> str:
        for c in self.session.cookies:
            if (c.name == "webvpn-token" and not c.is_expired()
                    and c.domain.lstrip(".") == VPN_HOST):
                if any(ord(ch) < 32 or ch == ";" for ch in c.value):
                    raise VodError("WebVPN Cookie 无效。")
                return c.value
        return ""

    def platform_headers(self, url: str, vpn: bool) -> dict:
        headers = {}
        if vpn:
            # The gateway cookie can be host-only; explicitly scope it to known proxies.
            if safe_url(url).hostname not in MAPPING.values():
                raise VodError("不支持的 WebVPN 业务地址。")
            prepared = self.session.prepare_request(requests.Request("GET", url))
            cookie = prepared.headers.get("Cookie", "")
            gateway = self.gateway_cookie()
            if not gateway:
                raise SessionExpired("WebVPN 会话已失效，请重新登录。")
            parts = [part.strip() for part in cookie.split(";") if part.strip()
                     and not part.strip().startswith("webvpn-token=")]
            headers["Cookie"] = "; ".join([*parts, "webvpn-token=" + gateway])
        return headers

    def close(self):
        self.session.close()
