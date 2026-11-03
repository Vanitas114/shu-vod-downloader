import unittest
from types import SimpleNamespace

from shu_vod.api import VodClient, plain
from shu_vod.errors import NoPlayback, SessionExpired, VodError


class FakeHTTP:
    def __init__(self, values):
        self.values, self.calls = list(values), []

    def json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeAuth:
    base, vpn = "https://vod.cc.shu.edu.cn", False

    def __init__(self, values):
        self.http, self.renewals = FakeHTTP(values), 0

    def api_headers(self):
        return {"jwt-token": "business-secret"}

    def refresh(self):
        self.renewals += 1


def envelope(value):
    return {"status": 200, "data": value}


def page(values, total, index=1, pages=1):
    return envelope({"records": values, "rowCount": total, "pageIndex": index, "pageCount": pages})


def record(lid=31, cid=42, term=7, **extra):
    return {"id": lid, "teclId": cid, "acteId": term, "subjName": "<em>数学</em>",
            "courseStatus": 2, "teacNames": ["张老师"], **extra}


class ApiTests(unittest.TestCase):
    def client(self, *values):
        return VodClient(FakeAuth(values), page_size=1)

    def test_browse_uses_class_id_and_all_pages(self):
        c = self.client(page([record()], 2, pages=2), page([record(cid=43)], 2, 2, 2))
        result = c.courses(7)
        self.assertEqual([r["course_id"] for r in result], [42, 43])
        self.assertEqual(c.http.calls[1][2]["params"]["page.pageIndex"], 2)

    def test_repeated_incomplete_and_changing_pages_rejected(self):
        cases = [
            [page([record()], 2, pages=2), page([record()], 2, 2, 2)],
            [page([record()], 2)],
            [page([record()], 2, pages=2), page([record(cid=43)], 3, 2, 2)],
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(VodError):
                self.client(*values).courses(7)

    def test_wrong_term_rejected(self):
        with self.assertRaises(VodError):
            self.client(page([record(term=8)], 1)).courses(7)

    def test_keyword_is_post_filters_live_and_deduplicates(self):
        c = self.client(envelope({"courseSourceList": [record(), record(), record(32, courseStatus=1)]}))
        result = c.search("数学", 7)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "数学")
        self.assertEqual(result[0]["lesson_id"], 31)
        self.assertEqual(c.http.calls[0][0], "POST")
        self.assertEqual(c.http.calls[0][2]["json"], {"acteId": 7, "keyword": "数学", "queryType": 0, "maxSearchCount": 50})

    def test_search_identity_mismatch_rejected(self):
        with self.assertRaises(VodError):
            self.client(envelope({"courseSourceList": [record(), record(cid=99)]})).search("数学", 7)

    def test_code_search_paginates_and_filters_cross_term(self):
        a = {"id": 42, "acteId": 7, "subjName": "数学", "subjCode": "0800", "teacherList": [{"userName": "李"}]}
        b = {**a, "id": 43, "acteId": 6}
        c = self.client(envelope({"list": [a], "totalCount": 2, "totalPage": 2, "currPage": 1}),
                        envelope({"list": [b], "totalCount": 2, "totalPage": 2, "currPage": 2}))
        result = c.search("080", 7, by_code=True)
        self.assertEqual([r["course_id"] for r in result], [42])
        self.assertNotIn("lesson_id", result[0])
        self.assertEqual(c.http.calls[1][2]["params"]["pageIndex"], 2)
        metadata_client = self.client(envelope({"list": [b, a], "totalCount": 2, "totalPage": 1, "currPage": 1}))
        metadata = metadata_client.course_metadata(42, [{"course_id": 42, "name": "数学", "term_id": 7}])
        self.assertEqual(metadata["code"], "0800")
        self.assertEqual(metadata["course_id"], 42)
        self.assertEqual(metadata["teachers"], ["李"])

    def test_lessons_keep_stale_status_and_validate_class(self):
        c = self.client(page([record(vodStatus=0, subjCode="0800")], 1))
        items = c.lessons(42)
        self.assertEqual(items[0]["lesson_id"], 31)
        self.assertEqual(c.course_metadata(42, items)["code"], "0800")
        self.assertEqual(len(c.http.calls), 1)
        self.assertIn("/preview", c.http.calls[0][1])
        with self.assertRaises(VodError):
            self.client(page([record(cid=99)], 1)).lessons(42)

    def test_playback_checks_all_identities_and_url(self):
        good = {"id": 31, "teclId": 42, "courseVodViewList": [{"courId": 31, "viewNum": 7,
                 "url": "https://vrmvod.cc.shu.edu.cn:40443/test.mp4?sign=a%2Fb%2B"}]}
        self.assertIn("sign=a%2Fb%2B", self.client(envelope(good)).playback(42, 31)[0]["url"])
        for mutation in [{**good, "id": 32}, {**good, "teclId": 99},
                         {**good, "courseVodViewList": [{"courId": 32, "viewNum": 7, "url": "https://evil.test/a.mp4"}]}]:
            with self.assertRaises(VodError):
                self.client(envelope(mutation)).playback(42, 31)
        with self.assertRaises(NoPlayback):
            self.client(envelope({**good, "courseVodViewList": []})).playback(42, 31)

    def test_business_session_refreshed_only_once(self):
        c = self.client(SessionExpired(), [{"id": 7, "acyeCode": "2026-2027", "acteTerm": 1, "currentTerm": True}])
        self.assertEqual(c.resolve_term(), 7)
        self.assertEqual(c.auth.renewals, 1)
        c = self.client(SessionExpired(), SessionExpired())
        with self.assertRaises(SessionExpired):
            c.terms()
        self.assertEqual(c.auth.renewals, 1)

    def test_no_current_term_is_not_guessed(self):
        with self.assertRaises(VodError):
            self.client([]).resolve_term()

    def test_highlights_scripts_and_terminal_controls_removed(self):
        self.assertEqual(plain('<em>微积分</em><script>evil</script> &amp; 数学\x1b'), "微积分 & 数学")


if __name__ == "__main__":
    unittest.main()
