"""Shared human-readable paths for CLI downloads, TUI downloads and previews."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .api import identifier, integer

SHANGHAI = timezone(timedelta(hours=8))


def path_part(value, fallback, limit):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]', "_", str(value or ""))
    return text.strip(" .")[:limit].rstrip(" .") or fallback


def course_directory(course, teachers=()):
    names = sorted({name.strip() for name in teachers if name.strip()})
    name = path_part(course.get("name"), "未知课程", 40)
    code = path_part(course.get("code") or course.get("course_number"), "课程号未知", 24)
    teacher = path_part("、".join(names), "未知教师", 48)
    return f"{name}-{code}-{teacher}-{identifier(course.get('course_id'))}"


def lesson_time(value):
    try:
        if isinstance(value, bool) or value is None:
            return "时间未知"
        if isinstance(value, str) and re.fullmatch(r"\d{10}(?:\d{3})?", value.strip()):
            value = int(value)
        if isinstance(value, (int, float)):
            seconds = value / 1000 if abs(value) > 10**11 else value
            date = datetime.fromtimestamp(seconds, SHANGHAI)
        elif isinstance(value, datetime):
            date = value
        elif isinstance(value, str):
            date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return "时间未知"
        if date.tzinfo is not None:
            date = date.astimezone(SHANGHAI)
        return date.strftime("%Y-%m-%d_%H%M")
    except (ValueError, OSError, OverflowError):
        return "时间未知"


def lesson_filename(lesson, view, suffix):
    week, section = integer(lesson.get("week")), integer(lesson.get("section"))
    week_label = f"第{week}周" if week > 0 else "周次未知"
    section_label = f"第{section}节" if section > 0 else "节次未知"
    return (f"{lesson_time(lesson.get('begin_time'))}-{week_label}-{section_label}"
            f"-课次{identifier(lesson.get('lesson_id'))}-视角{view}{suffix}")
