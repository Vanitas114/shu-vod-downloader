"""Read-only catalog/search/viewer endpoints from vendor/ShuYo's VodApiClient."""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from .errors import NoPlayback, SessionExpired, VodError
from .transport import API_PATH, media_url


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value) -> str:
    parser = _PlainText()
    parser.feed(str(value or ""))
    text = unescape("".join(parser.parts))
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    return " ".join(text.split())


def integer(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    return default


def identifier(value):
    result = integer(value)
    if result <= 0:
        raise VodError("平台返回了无效的课程标识。")
    return result


def rows(value):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise VodError("平台列表数据不完整。")
    return value


def teacher_names(row):
    teachers = []
    for field in ("teacNames", "teclTeacNames", "teacherList"):
        values = row.get(field) or []
        if not isinstance(values, list):
            raise VodError("课程教师列表格式无效。")
        teachers.extend(values)
    return list(dict.fromkeys(filter(None, (
        plain(t.get("userName")) if isinstance(t, dict) else plain(t) for t in teachers))))


def course(row, *, selector=False):
    name = plain(row.get("subjName")) or plain(row.get("teclName"))
    if not name:
        raise VodError("课程名称缺失。")
    return {
        "course_id": identifier(row.get("id" if selector else "teclId")),
        "term_id": identifier(row.get("acteId")), "name": name,
        "class_name": plain(row.get("teclName")), "code": plain(row.get("subjCode")),
        "course_number": plain(row.get("courseNo")),
        "teachers": teacher_names(row),
        "lesson_count": integer(row.get("courTimes")),
    }


def lesson(row):
    return {"lesson_id": identifier(row.get("id")), "course_id": identifier(row.get("teclId")),
            "name": plain(row.get("subjName")) or plain(row.get("teclName")),
            "code": plain(row.get("subjCode")), "course_number": plain(row.get("courseNo")),
            "class_name": plain(row.get("teclName")), "term_id": integer(row.get("acteId")),
            "teachers": teacher_names(row),
            "begin_time": row.get("courBeginTime"), "end_time": row.get("courEndTime"),
            "week": integer(row.get("weekNo", row.get("weekNumber"))),
            "section": integer(row.get("letiNumber")), "classroom": plain(row.get("clroName")),
            "recorded_hint": all(integer(row.get(k), -1) == v for k, v in
                                 (("vodStatus", 5), ("vodEnable", 1), ("vodDeleteStatus", 0)))}


class VodClient:
    def __init__(self, auth, page_size=100):
        self.auth = auth
        self.http = auth.http
        self.page_size = page_size

    def get(self, path, params=None, body=None):
        for attempt in range(2):
            try:
                return self.http.json("POST" if body is not None else "GET",
                                      self.auth.base + API_PATH + path,
                                      params=params, json=body, headers=self.auth.api_headers())
            except SessionExpired:
                if attempt:
                    raise SessionExpired("会话续期失败，请重新运行 shu-vod login。") from None
                self.auth.refresh()

    def data(self, path, params=None, body=None):
        value = self.get(path, params, body)
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
            raise VodError("平台响应缺少 data 对象。")
        return value["data"]

    def terms(self):
        values = rows(self.get("/v1/list/termYear"))
        result = []
        for row in values:
            year, number = plain(row.get("acyeCode")), integer(row.get("acteTerm"))
            if not re.match(r"^\d{4}(?:-|$)", year) or number < 1 or integer(row.get("id")) <= 0:
                continue
            result.append({"term_id": identifier(row["id"]), "name": f"{year}学年第{number}学期",
                           "current": row.get("currentTerm") is True})
        return sorted(result, key=lambda r: (not r["current"], -r["term_id"]))

    def resolve_term(self, selected=None):
        if selected is not None:
            return identifier(selected)
        current = [r for r in self.terms() if r["current"]]
        if len(current) != 1:
            raise VodError("未能确定当前学期，请先运行 terms，再通过 --term 指定学期 ID。")
        return current[0]["term_id"]

    def _pages(self, path, filters, identity, order="courBeginTime", ascending=True, selector=False):
        result, seen, expected = [], set(), None
        for page in range(1, 1001):
            params = {**filters, **({"pageIndex": page, "pageSize": self.page_size} if selector else {
                "page.pageIndex": page, "page.pageSize": self.page_size,
                "page.orders[0].field": order, "page.orders[0].asc": str(ascending).lower(),
            })}
            data = self.data(path, params)
            values = rows(data.get("list" if selector else "records"))
            total = integer(data.get("totalCount" if selector else "rowCount"), -1)
            pages = integer(data.get("totalPage" if selector else "pageCount"), 0)
            index = integer(data.get("currPage" if selector else "pageIndex", page), -1)
            if total < 0 or pages < 0 or index != page or (expected is not None and expected != total):
                raise VodError("课程列表分页变化或异常，请重试。")
            expected = total
            for row in values:
                item_id = identifier(row.get(identity))
                if item_id in seen:
                    raise VodError("课程分页出现重复记录，无法保证列表完整，请重试。")
                seen.add(item_id)
                result.append(row)
            complete = page >= pages if pages else len(result) >= total
            if complete:
                if len(result) != total:
                    raise VodError("课程列表未完整加载，请重试。")
                return result
            if not values or len(result) >= total:
                raise VodError("课程列表分页异常，请重试。")
        raise VodError("课程列表超过分页上限。")

    def courses(self, term_id):
        term_id = identifier(term_id)
        result = [course(r) for r in self._pages("/v1/group_subject_vod_list/t-1",
                  {"acteId": term_id}, "teclId", "updateTime", False)]
        if any(r["term_id"] != term_id for r in result):
            raise VodError("课程列表与所选学期不一致。")
        return result

    def search(self, keyword, term_id, *, by_code=False):
        term_id = identifier(term_id)
        keyword = keyword.strip()
        if len(keyword) < 2:
            raise VodError("搜索关键词至少需要 2 个字符。")
        if by_code:
            values = self._pages("/v1/teachingclass/list",
                                 {"allType": 1, "type": 1, "condition": keyword}, "id", selector=True)
            result = [course(r, selector=True) for r in values]
            return [r for r in result if r["term_id"] == term_id and
                    any(keyword.lower() in r[k].lower() for k in ("code", "course_number"))]
        data = self.data("/v1/course/source/search", body={
            "acteId": term_id, "keyword": keyword, "queryType": 0, "maxSearchCount": 50,
        })
        result, seen = [], {}
        for row in rows(data.get("courseSourceList")):
            cid, lid = identifier(row.get("teclId")), identifier(row.get("id"))
            if identifier(row.get("acteId")) != term_id or (lid in seen and seen[lid] != cid):
                raise VodError("搜索结果与学期或教学班不一致。")
            if integer(row.get("courseStatus"), -1) != 2 or lid in seen:
                continue
            seen[lid] = cid
            result.append({**course(row), **lesson(row)})
        return result

    def lessons(self, course_id, *, term_id=None, source="preview"):
        course_id = identifier(course_id)
        path = "/v1/subject_vod_list/preview" if source == "preview" else "/v1/subject_vod_list_new"
        values = self._pages(path, {"teclIds": course_id, "schoolOpenStatusFlag": "false"}, "id")
        for row in values:
            if identifier(row.get("teclId")) != course_id or (term_id is not None and
                    row.get("acteId") is not None and identifier(row["acteId"]) != term_id):
                raise VodError("课次与所选学期或教学班不一致。")
        # Status hints can lag behind the media server; never discard these candidates.
        return sorted((lesson(row) for row in values), key=lambda r: (str(r["begin_time"] or ""), r["lesson_id"]))

    def course_metadata(self, course_id, lessons, known=None):
        course_id = identifier(course_id)
        if known and identifier(known.get("course_id")) != course_id:
            raise VodError("课程信息与所选教学班不一致。")
        matching = [item for item in lessons if item["course_id"] == course_id]
        sources = ([known] if known else []) + matching
        result = {"course_id": course_id}
        for key in ("name", "code", "course_number", "class_name", "term_id"):
            result[key] = next((item[key] for item in sources if item.get(key)), "")
        result["teachers"] = sorted({name for item in sources for name in item.get("teachers", [])})
        if result["name"] and (result["code"] or result["course_number"]) and result["teachers"]:
            return result
        # Fill absent course metadata from the selector, matching the exact class
        # ID so another teacher's class with the same subject cannot be used.
        keyword = result["code"] or result["course_number"] or result["name"]
        values = self._pages("/v1/teachingclass/list",
                             {"allType": 1, "type": 1, "condition": keyword}, "id", selector=True)
        for row in values:
            if identifier(row.get("id")) == course_id:
                details = course(row, selector=True)
                if result["term_id"] and details["term_id"] != result["term_id"]:
                    raise VodError("课程信息与所选学期不一致。")
                result.update({key: value for key, value in details.items() if value})
                result["teachers"] = sorted(result["teachers"])
                break
        return result

    def course_teachers(self, course_id, lessons):
        return self.course_metadata(course_id, lessons)["teachers"]

    def playback(self, course_id, lesson_id):
        course_id, lesson_id = identifier(course_id), identifier(lesson_id)
        data = self.data("/v1/course_vod_urls_new", {"courseId": lesson_id})
        if identifier(data.get("id")) != lesson_id or identifier(data.get("teclId")) != course_id:
            raise VodError("视频详情与所选课次或教学班不一致。")
        result, seen = [], set()
        for row in rows(data.get("courseVodViewList")):
            if identifier(row.get("courId")) != lesson_id:
                raise VodError("视频视角与所选课次不一致。")
            if not row.get("url"):
                continue
            number = integer(row.get("viewNum"), -1)
            if number < 0 or number in seen or not isinstance(row["url"], str):
                raise VodError("视频视角标识缺失或重复。")
            seen.add(number)
            result.append({"view": number, "url": media_url(row["url"], self.auth.vpn)})
        if not result:
            raise NoPlayback("该课次暂无可用录播。")
        return result
