from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__
from .accounts import AccountRef, SingleAccountStore, default_state_dir
from .api import VodClient, plain
from .auth import AuthManager
from .download import Aria2Downloader, download_lessons
from .errors import AuthError, VodError
from .transport import Transport


def positive(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def view_number(value):
    if value == "all":
        return None
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("视角必须是 all 或非负整数") from None
    if number < 0:
        raise argparse.ArgumentTypeError("视角不能为负数")
    return number


def parser():
    p = argparse.ArgumentParser(prog="shu-vod", description="上海大学课程录播检索与 aria2 下载")
    p.add_argument("--version", action="version", version=__version__)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--state-dir", type=Path, default=default_state_dir(), help="本地会话目录")
    shared.add_argument("--insecure", action="store_true", help="显式关闭 HTTPS 证书验证（含 aria2）")
    shared.add_argument("--timeout", type=positive, default=30, help="连接超时秒数，默认 30")
    shared.add_argument("--json", action="store_true", help="输出可供脚本使用的 JSON")
    commands = p.add_subparsers(dest="command", required=True)
    tui = commands.add_parser("tui", parents=[shared], help="打开持续交互的终端界面")
    tui.add_argument("--output", type=Path, help="初始下载目录，也可在界面内修改")
    tui.add_argument("--aria2", help="aria2c 路径，也可在界面内修改")
    login = commands.add_parser("login", parents=[shared], help="登录单个校园账号")
    login.add_argument("--username", help="学号/工号；不指定则交互输入")
    login.add_argument("--password-env", help="从指定环境变量读取密码，避免写入命令行")
    login.add_argument("--transport", choices=["direct", "webvpn"], default="direct", help="校园网直连或校外 WebVPN")
    login.add_argument("--method", choices=["wecom", "sms"], default="wecom", help="两步验证码接收方式")
    commands.add_parser("logout", parents=[shared], help="删除本地单账号会话")
    status = commands.add_parser("status", parents=[shared], help="查看本地登录状态")
    status.add_argument("--check", action="store_true", help="联网验证会话，必要时自动续期")
    commands.add_parser("terms", parents=[shared], help="列出学期及平台学期 ID")
    courses = commands.add_parser("courses", parents=[shared], help="浏览账号可见课程")
    courses.add_argument("--term", type=positive, help="学期 ID，默认当前学期")
    search = commands.add_parser("search", parents=[shared], help="搜索课程/教师或课程号")
    search.add_argument("keyword")
    search.add_argument("--term", type=positive)
    search.add_argument("--by", choices=["keyword", "code"], default="keyword")
    for name, help_text in [("lessons", "列出教学班的全部课次"), ("download", "下载指定课次或整门课")]:
        sub = commands.add_parser(name, parents=[shared], help=help_text)
        sub.add_argument("--course", type=positive, required=True, help="教学班 ID，即 course_id/teclId")
        sub.add_argument("--term", type=positive, help="可选：额外核对课次所属学期")
        sub.add_argument("--source", choices=["preview", "catalog"], default="preview", help="搜索课次目录或账号课次目录")
        if name == "download":
            sub.add_argument("--lesson", type=positive, action="append", help="课次 ID，可重复指定；不指定则下载整门课")
            sub.add_argument("--view", type=view_number, default=None, metavar="all|N", help="默认全部视角；N 为平台 viewNum")
            sub.add_argument("--output", type=Path, default=Path("downloads"))
            sub.add_argument("--aria2", default="aria2c", help="aria2c 路径或命令名")
            sub.add_argument("--connections", type=int, choices=range(1, 17), default=8, metavar="1-16")
            sub.add_argument("--jobs", type=int, choices=range(1, 17), default=2, metavar="1-16", help="每个课次同时下载的视角数")
            sub.add_argument("--dry-run", action="store_true", help="解析可用视频并显示计划，不下载文件")
    return p


def emit(data, as_json):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, list):
        if not data:
            print("没有匹配的记录。")
            return
        columns = [k for k in ("term_id", "current", "course_id", "lesson_id", "name", "code",
                                "course_number", "teachers", "begin_time", "week", "section") if k in data[0]]
        print("\t".join(columns))
        for row in data:
            print("\t".join(plain("、".join(row[k]) if isinstance(row.get(k), list) else row.get(k)) for k in columns))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def prompt(text, secret=False):
    # Prompts go to stderr, keeping --json stdout clean.
    print(text, file=sys.stderr, end="", flush=True)
    return getpass.getpass("", stream=sys.stderr) if secret else input()


def execute(args):
    if args.command == "tui":
        if args.json:
            raise VodError("交互界面不支持 --json；脚本输出请使用其他子命令。")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise VodError("请在交互式终端中打开 shu-vod；脚本调用请指定 courses/search/download 等子命令。")
        try:
            from .tui import VodApp
        except ModuleNotFoundError:
            raise VodError("缺少交互界面依赖，请在项目目录运行 python -m pip install -e .。") from None
        VodApp(state_dir=args.state_dir, output=args.output, aria2=args.aria2,
               insecure=args.insecure, timeout=args.timeout).run()
        return 0
    store, account = SingleAccountStore(args.state_dir), AccountRef()
    report = lambda message: print(message, file=sys.stderr, flush=True)
    if args.command == "logout":
        store.delete(account)
        emit({"logged_out": True}, args.json)
        return 0
    http = Transport(verify=not args.insecure, timeout=args.timeout)
    auth = AuthManager(http, store, account)
    try:
        if args.command == "login":
            username = args.username or prompt("学号/工号：")
            password = os.getenv(args.password_env) if args.password_env else prompt("密码（输入时不显示）：", True)
            if not password:
                raise AuthError("密码为空，或指定的密码环境变量不存在。")
            auth.login(username, password, vpn=args.transport == "webvpn", method=args.method,
                       read_code=lambda: prompt("验证码：", True), report=report)
            emit({"logged_in": True, "account": "default", "username": auth.username,
                  "transport": args.transport}, args.json)
            return 0
        auth.restore()
        client = VodClient(auth)
        if args.command == "status":
            if args.check:
                value = client.get("/v1/currentuser")
                user = value.get("data") if isinstance(value, dict) else None
                if not isinstance(user, dict) or str(user.get("userCode", "")).strip() != auth.username:
                    raise AuthError("平台登录身份不一致，请重新登录。")
            result = {"account": "default", "username": auth.username,
                      "transport": "webvpn" if auth.vpn else "direct", "verified": args.check}
        elif args.command == "terms":
            result = client.terms()
        elif args.command == "courses":
            result = client.courses(client.resolve_term(args.term))
        elif args.command == "search":
            result = client.search(args.keyword, client.resolve_term(args.term), by_code=args.by == "code")
            if args.by == "keyword":
                report("关键词接口最多返回 50 个录播课次；完整课次请使用 lessons --course <course_id>。")
        else:
            downloader = None
            if args.command == "download" and not args.dry_run:
                downloader = Aria2Downloader(args.aria2, connections=args.connections, jobs=args.jobs,
                                             verify=not args.insecure, timeout=args.timeout)
            items = client.lessons(args.course, term_id=args.term, source=args.source)
            if args.command == "lessons":
                result = items
            else:
                all_items = items
                if args.lesson:
                    selected = set(args.lesson)
                    if selected - {r["lesson_id"] for r in items}:
                        raise VodError("指定课次不属于当前教学班目录；请先运行 lessons 核对。")
                    items = [r for r in items if r["lesson_id"] in selected]
                if not items:
                    raise VodError("该教学班没有可下载的课次。")
                course_info = client.course_metadata(args.course, all_items)
                result = download_lessons(client, items, output=args.output, view=args.view,
                                          dry_run=args.dry_run, downloader=downloader,
                                          course_info=course_info, report=report)
                emit(result, args.json)
                return 5 if result["failed"] or not (result["downloaded"] or result["planned"]) else 0
        emit(result, args.json)
        return 0
    finally:
        http.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv or ["tui"])
    try:
        return execute(args)
    except KeyboardInterrupt:
        print("已取消；下载进度保留，可重跑原命令续传。", file=sys.stderr)
        return 130
    except EOFError:
        message = "缺少交互输入，请在终端登录或提供 --username 和 --password-env。"
        code = 2
    except (VodError, OSError) as exc:
        message = str(exc) if isinstance(exc, VodError) else "本地文件读写失败，请检查路径、权限或磁盘空间。"
        code = 3 if isinstance(exc, AuthError) else 1
    emit({"error": message, "exit_code": code}, True) if args.json else print("错误：" + message, file=sys.stderr)
    return code
