import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import requests
from requests.adapters import BaseAdapter

from shu_vod.accounts import AccountRef, SessionState, SingleAccountStore
from shu_vod.auth import AuthManager, FALLBACK_PEM, password_login, webvpn_login
from shu_vod.errors import AuthError, PermissionDenied, SessionExpired, VodError
from shu_vod.transport import (API_PATH, CAS_HOST, MAPPING, SSO, UI_PATH, VOD_HOST, Transport,
                               decode_response, media_url, normalize_auth, validate_auth)
from cryptography.hazmat.primitives import serialization


def response(data=None, status=200, headers=None, text=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = (json.dumps(data) if text is None else text).encode()
    return r


class QueueAdapter(BaseAdapter):
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def send(self, request, **kwargs):
        self.calls.append(request)
        r = self.responses.pop(0)
        r.request, r.url = request, request.url
        return r

    def close(self):
        pass


class AuthTests(unittest.TestCase):
    def test_store_roundtrip_isolates_account_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as root:
            store = SingleAccountStore(Path(root))
            state = SessionState("12345", "direct", "jwt", [{"name": "x", "value": "y", "domain": "vod.cc.shu.edu.cn"}])
            store.save(AccountRef(), state)
            self.assertEqual(store.load(AccountRef()), state)
            self.assertNotIn("password", store.path(AccountRef()).read_text())
            with self.assertRaises(AuthError):
                store.load(AccountRef("another"))
            for malformed in ["{}", "[]", "null"]:
                store.path(AccountRef()).write_text(malformed)
                with self.assertRaises(AuthError):
                    store.load(AccountRef())
            store.delete(AccountRef())
            self.assertFalse(store.path(AccountRef()).exists())

    def test_sso_password_and_two_factor_flow(self):
        http = Transport()
        adapter = QueueAdapter([response({"message": "success", "twoStepRequired": True,
                                          "twoStepMethods": {"sms": {}}}),
                                response({"message": "success"}), response({"message": "success"})])
        http.session.mount("https://", adapter)
        http.session.cookies.set("SHU_OAUTH2", "sso", domain="newsso.shu.edu.cn")
        with patch("shu_vod.auth.public_key", return_value=serialization.load_pem_public_key(FALLBACK_PEM)):
            password_login(http, "12345", "password-secret", "sms", lambda: "123456", lambda _: None)
        self.assertEqual([r.path_url for r in adapter.calls], ["/oauth/userLogin", "/oauth/twoStep/send", "/oauth/twoStep/verify"])
        bodies = [json.loads(r.body) for r in adapter.calls]
        self.assertNotEqual(bodies[0]["password"], "password-secret")
        self.assertTrue(bodies[0]["params"])
        self.assertEqual(bodies[2]["code"], "123456")

    def test_vod_exchange_validates_identity_before_save(self):
        for user_code, succeeds in [("12345", True), ("other", False)]:
            with self.subTest(user_code=user_code), tempfile.TemporaryDirectory() as root:
                http = Transport()
                adapter = QueueAdapter([
                    response(status=302, headers={"Location": "https://vrm.cc.shu.edu.cn/jy-cas-proxy/cas/login"}),
                    response(status=302, headers={"Location": SSO + "/oauth/authorize?state=server"}),
                    response(status=302, headers={"Location": "https://vrm.cc.shu.edu.cn/jy-cas-proxy/proxy/callback/" + "a" * 32 + "?code=one-time"}),
                    response(status=302, headers={"Location": "https://vod.cc.shu.edu.cn" + UI_PATH + "/?type=cas#/"}),
                    response({"status": 200, "result": {}}),
                    response({"status": 200, "result": {"jwt_token": "business-jwt"}}),
                    response({"status": 200, "data": {"id": 5, "userCode": user_code}}),
                ])
                http.session.mount("https://", adapter)
                store = SingleAccountStore(Path(root))
                auth = AuthManager(http, store)
                auth.username = "12345"
                if succeeds:
                    auth.refresh()
                    self.assertEqual(store.load(AccountRef()).jwt, "business-jwt")
                else:
                    with self.assertRaises(AuthError):
                        auth.refresh()
                    self.assertFalse(store.path(AccountRef()).exists())
                self.assertTrue(all("jwt-token" not in r.headers for r in adapter.calls[:-1]))
                self.assertEqual(adapter.calls[-1].headers["jwt-token"], "business-jwt")

    def test_webvpn_handshake(self):
        from shu_vod.auth import encoded
        state = encoded({"externalId": "external"})
        http = Transport()
        adapter = QueueAdapter([
            response({"code": 0, "data": {"list": [{"authType": 5, "externalId": "external"}]}}),
            response({"code": 0, "data": None}),
            response(status=302, headers={"Location": "https://webvpn.shu.edu.cn/callback/oauth2?" + urlencode({"code": "code", "state": state})}),
            response({"code": 0, "data": {}}),
            response({"code": 0, "data": {"userId": 9}}),
        ])
        http.session.mount("https://", adapter)
        http.session.cookies.set("webvpn-token", "vpn-secret", domain="webvpn.shu.edu.cn")
        webvpn_login(http, "12345")
        finish = json.loads(json.loads(adapter.calls[3].body)["data"])
        self.assertEqual(finish["code"], "code")
        self.assertEqual(len(finish["deviceId"]), 32)
        headers = http.platform_headers("https://" + MAPPING[(VOD_HOST, 443)] + API_PATH, True)
        self.assertEqual(headers["Cookie"], "webvpn-token=vpn-secret")

    def test_auth_redirect_whitelist_and_callback_normalization(self):
        callback = "https://" + MAPPING[(CAS_HOST, 443)] + "/jy-cas-proxy/proxy/callback/" + "a" * 32
        url = "https://https-oauth-shu-edu-cn-443.webvpn.shu.edu.cn/oauth/authorize?" + urlencode({"redirect_uri": callback, "state": "a/b+"})
        normalized = normalize_auth(url, True)
        self.assertTrue(normalized.startswith(SSO))
        self.assertIn("vrm.cc.shu.edu.cn", normalized)
        self.assertIn("state=a%2Fb%2B", normalized)
        for bad in ["https://evil.test/oauth/", "http://newsso.shu.edu.cn/oauth/", "https://newsso.shu.edu.cn:8443/oauth/", "https://u:p@newsso.shu.edu.cn/oauth/"]:
            with self.assertRaises(VodError):
                validate_auth(bad, False)
        with self.assertRaises(SessionExpired):
            validate_auth(SSO + "/oauth2/login/index", False)

    def test_media_url_mapping_preserves_signature_and_blocks_unknown_domains(self):
        url = "https://vrmvod.cc.shu.edu.cn:40443/a.mp4?a=%2f%2B&b=+"
        self.assertTrue(media_url(url, True).endswith("/a.mp4?a=%2f%2B&b=+"))
        for url in ["https://evil.test/a.mp4", "https://vod.cc.shu.edu.cn@evil.test/a", "https://vod.cc.shu.edu.cn/a\n"]:
            with self.assertRaises(VodError):
                media_url(url, False)

    def test_http_and_business_errors_are_not_treated_as_media(self):
        cases = [(response(status=403), PermissionDenied), (response({"code": 10013}), SessionExpired),
                 (response(text="<html>newsso.shu.edu.cn/oauth/login</html>"), SessionExpired),
                 (response(text="not json"), VodError), (response({"status": 500}), VodError)]
        for r, error in cases:
            with self.assertRaises(error):
                decode_response(r)


if __name__ == "__main__":
    unittest.main()
