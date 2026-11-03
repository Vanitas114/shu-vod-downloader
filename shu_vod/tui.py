"""Persistent Textual UI; all network and aria2 work stays off the UI thread."""
from __future__ import annotations

import json
import sys
from concurrent.futures import Future, TimeoutError
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from rich.segment import Segment
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import (Button, Checkbox, DataTable, Footer, Input, Label, OptionList,
                             ProgressBar, RichLog, Select, SelectionList, Static,
                             TabbedContent, TabPane)

from .accounts import AccountRef, SingleAccountStore, default_state_dir, private_write
from .api import VodClient, plain
from .auth import AuthManager
from .download import download_lessons
from .download_manager import DownloadManager
from .errors import AuthError, DownloadCancelled, VodError
from .transport import Transport


@dataclass
class Preferences:
    output: str = str(Path("downloads").resolve())
    aria2: str = "aria2c"
    connections: int = 8
    jobs: int = 2
    verify: bool = True

    def validate(self):
        if not isinstance(self.output, str) or not self.output.strip():
            raise VodError("请选择下载目录。")
        if not isinstance(self.aria2, str) or not self.aria2.strip():
            raise VodError("请填写 aria2c 命令名或路径。")
        if any(ord(c) < 32 for c in self.output + self.aria2):
            raise VodError("目录或程序路径不能包含控制字符。")
        if any(type(n) is not int or not 1 <= n <= 16 for n in (self.connections, self.jobs)):
            raise VodError("连接数和并发数必须为 1–16。")
        if not isinstance(self.verify, bool):
            raise VodError("证书设置无效。")


def checkbox_marker(checked: bool) -> Content:
    return Content.assemble(("[", "#c4cbd3"), ("✓" if checked else " ", "#82df9b"), ("]", "#c4cbd3"))


class BracketCheckbox(Checkbox):
    @property
    def _button(self) -> Content:
        return checkbox_marker(self.value)


class Dialog(ModalScreen):
    BINDINGS = [("escape", "cancel", "返回")]

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def cancel(self, event):
        event.stop()
        self.action_cancel()

    def error(self, message):
        self.query_one("#form-error", Static).update(message)


class LoginDialog(Dialog):
    def __init__(self, username="", vpn=False):
        super().__init__()
        self.username, self.vpn = username, vpn

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("登录校园账号", classes="dialog-title")
            yield Label("学号 / 工号")
            yield Input(self.username, id="username", placeholder="输入学号或工号")
            yield Label("密码")
            yield Input(password=True, id="password", placeholder="密码不会保存")
            yield Select([("校园网直连", "direct"), ("校外 WebVPN", "webvpn")],
                         value="webvpn" if self.vpn else "direct", allow_blank=False, id="transport")
            yield Select([("企业微信验证码", "wecom"), ("短信验证码", "sms")],
                         value="wecom", allow_blank=False, id="method")
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("登录", id="submit", variant="primary")
                yield Button("返回", id="cancel")

    def on_mount(self):
        self.query_one("#username", Input).focus()

    @on(Button.Pressed, "#submit")
    @on(Input.Submitted, "#password")
    def submit(self, event):
        event.stop()
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        if not username or not password:
            self.error("请填写账号和密码。")
            return
        self.query_one("#password", Input).value = ""
        self.dismiss({"username": username, "password": password,
                      "vpn": self.query_one("#transport", Select).value == "webvpn",
                      "method": self.query_one("#method", Select).value})


class CodeDialog(Dialog):
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("两步验证", classes="dialog-title")
            yield Label("请输入企业微信或短信收到的验证码")
            yield Input(id="code", password=True, placeholder="验证码")
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("验证", id="submit", variant="primary")
                yield Button("取消登录", id="cancel")

    def on_mount(self):
        self.query_one("#code", Input).focus()

    @on(Button.Pressed, "#submit")
    @on(Input.Submitted, "#code")
    def submit(self, event):
        event.stop()
        code = self.query_one("#code", Input).value.strip()
        if not code:
            self.error("请输入验证码。")
            return
        self.query_one("#code", Input).value = ""
        self.dismiss(code)


class SettingsDialog(Dialog):
    def __init__(self, preferences):
        super().__init__()
        self.preferences = preferences

    def compose(self) -> ComposeResult:
        p = self.preferences
        with VerticalScroll(classes="dialog"):
            yield Label("下载设置", classes="dialog-title")
            yield Label("保存目录")
            yield Input(p.output, id="output")
            yield Label("aria2c 命令名或完整路径")
            yield Input(p.aria2, id="aria2")
            yield Label("每个视频的连接数 / 同时下载的视频数（1–16）")
            with Horizontal(classes="buttons"):
                yield Input(str(p.connections), id="connections", type="integer")
                yield Input(str(p.jobs), id="jobs", type="integer")
            yield BracketCheckbox("验证 HTTPS 证书", value=p.verify, id="verify")
            yield Static("设置会保存在本机，下次打开继续使用。", markup=False)
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("保存", id="submit", variant="primary")
                yield Button("返回", id="cancel")

    @on(Button.Pressed, "#submit")
    def submit(self, event):
        event.stop()
        try:
            p = Preferences(self.query_one("#output", Input).value.strip(),
                            self.query_one("#aria2", Input).value.strip(),
                            int(self.query_one("#connections", Input).value),
                            int(self.query_one("#jobs", Input).value),
                            self.query_one("#verify", Checkbox).value)
            p.validate()
            p.output = str(Path(p.output).expanduser().resolve())
        except (ValueError, OSError, VodError) as exc:
            self.error(str(exc) if isinstance(exc, VodError) else "请检查目录以及 1–16 的连接数和并发数。")
            return
        self.dismiss(p)


class DeleteDownloadDialog(Dialog):
    def __init__(self, task):
        super().__init__()
        self.download_task = task

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("删除下载任务", classes="dialog-title")
            yield Static(Path(self.download_task["path"]).name, markup=False)
            yield Static(self.download_task["path"], markup=False)
            yield BracketCheckbox("同时删除这个视频及其续传文件", id="delete-files")
            yield Static("默认只移除任务记录，保留本地视频。", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("删除任务", id="confirm-delete", variant="error")
                yield Button("返回", id="cancel")

    @on(Button.Pressed, "#confirm-delete")
    def confirm(self, event):
        event.stop()
        self.dismiss(self.query_one("#delete-files", Checkbox).value)


def byte_label(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024


def lesson_label(item):
    timestamp = item.get("begin_time")
    if isinstance(timestamp, (int, float)):
        try:
            seconds = timestamp / 1000 if abs(timestamp) > 10**11 else timestamp
            timestamp = datetime.fromtimestamp(seconds, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            timestamp = None
    return Text(f"{plain(timestamp) or '时间待定'}  第{item.get('week', 0)}周 / 第{item.get('section', 0)}节"
                f"  {plain(item.get('classroom'))}  [课次 {item['lesson_id']}]")


class LessonSelectionList(SelectionList[int]):
    def render_line(self, y: int) -> Strip:
        prompt = OptionList.render_line(self, y)
        row_style = next((segment.style for segment in prompt), None)
        if row_style is None or (index := row_style.meta.get("option")) is None:
            return prompt
        marker = checkbox_marker(self.get_option_at_index(index).value in self.selected)
        # The marker inherits the row's highlight and mouse target.
        prefix = Segment.apply_style(marker.render_segments(), row_style)
        return Strip([*prefix, Segment(" ", row_style), *prompt])


class DownloadTable(DataTable):
    def on_resize(self):
        self.app.call_after_refresh(self.app.resize_download_table)


class VodApp(App):
    TITLE = "SHU VOD · 课程录播"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+q", "request_quit", "退出", priority=True),
                Binding("ctrl+c", "stop", "停止任务", priority=True),
                Binding("ctrl+l", "login", "登录"), Binding("escape", "back", "返回课程")]
    CSS = """
    Screen { background: $surface; }
    #topbar { height: 1; padding: 0 1; background: $panel; }
    #account { width: auto; max-width: 50%; height: 1; margin-right: 2; }
    #topbar Button { height: 1; min-width: 8; margin: 0 1 0 0; }
    #brand { width: 1fr; text-align: right; color: $text-muted; }
    #status { height: 1; margin: 0 1; }
    #toolbar { height: 3; margin: 0 1; }
    .buttons, .filters { height: 3; }
    Button { min-width: 10; margin-right: 1; }
    #toolbar Button { width: 1fr; }
    TabbedContent { height: 1fr; min-height: 12; }
    TabPane { padding: 0 1; }
    #term { width: 1fr; }
    #by { width: 20; }
    #keyword { width: 1fr; }
    #catalog-table, #lesson-list, #activity, #download-table { height: 1fr; min-height: 3; }
    #lesson-list > .option-list--option-highlighted {
        background: #9acafa 22%; color: $text; text-style: none;
    }
    #download-summary { height: 1; color: $accent; margin: 1 0; }
    #download-detail { height: 3; color: $text-muted; }
    #download-actions Button { min-width: 10; }
    #destination { color: $text-muted; }
    #catalog-hint, #course-title, #destination { height: auto; max-height: 2; }
    #view { width: 17; }
    #view-note { width: 1fr; padding-top: 1; }
    #source { width: 18; }
    #progress { height: 1; margin: 0 1; }
    #progress Bar { width: 1fr; }
    #progress PercentageStatus { width: 6; }
    #form-error { color: $error; height: auto; min-height: 1; }
    Dialog { align: center middle; background: $background 70%; }
    .dialog { width: 68; max-width: 95%; height: auto; max-height: 95%;
              border: round $accent; background: $surface; padding: 1 2; }
    .dialog-title { text-style: bold; color: $accent; margin-bottom: 1; }
    .dialog Input, .dialog Select { margin-bottom: 1; }
    .dialog .buttons Input { width: 1fr; }
    """

    def __init__(self, *, state_dir=None, output=None, aria2=None, insecure=False, timeout=30):
        super().__init__()
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.preferences = Preferences()
        self.startup_note = ""
        try:
            data = json.loads((self.state_dir / "tui-settings.json").read_text(encoding="utf-8"))
            candidate = Preferences(**data)
            candidate.validate()
            self.preferences = candidate
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError, VodError):
            self.startup_note = "未能读取界面设置，已使用默认值；可在下载设置中重新保存。"
        if output is not None:
            self.preferences.output = str(Path(output).expanduser().resolve())
        if aria2:
            self.preferences.aria2 = aria2
        if insecure:
            self.preferences.verify = False
        self.timeout = timeout
        self.http = Transport(verify=self.preferences.verify, timeout=timeout)
        self.store = SingleAccountStore(self.state_dir)
        self.auth = AuthManager(self.http, self.store)
        self.client = VodClient(self.auth)
        self.logged_in = False
        self.busy = False
        self.closing = False
        self.stop_event = Event()
        self.catalog = []
        self.current_course = None
        self.lessons = []
        self.pending_code = None
        self.history = []
        self.download_manager = DownloadManager(self.state_dir)
        self.download_rows = []
        self.manager_active = False
        self.manager_owner = ""
        self.download_widths = ()

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("尚未登录", id="account", markup=False)
            yield Button("登录", id="login", variant="primary", compact=True)
            yield Button("退出登录", id="logout", compact=True)
            yield Static("SHU VOD · 课程录播", id="brand", markup=False)
        with Horizontal(id="toolbar"):
            yield Button("刷新学期", id="terms")
            yield Button("下载设置", id="settings")
            yield Button("下载管理", id="manage", variant="primary")
            yield Button("关闭", id="quit")
        with TabbedContent(initial="catalog", id="tabs"):
            with TabPane("课程检索", id="catalog"):
                with Horizontal(classes="filters"):
                    yield Select([], prompt="登录后选择学期", id="term")
                    yield Button("浏览课程", id="browse")
                with Horizontal(classes="filters"):
                    yield Input(placeholder="课程名称、教师姓名或课程号", id="keyword")
                    yield Select([("课程 / 教师", "keyword"), ("课程号", "code")],
                                 value="keyword", allow_blank=False, id="by")
                    yield Button("搜索", id="search", variant="primary")
                yield Static("选择课程后按 Enter，或点击「查看课次」。", id="catalog-hint", markup=False)
                yield DataTable(id="catalog-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="buttons"):
                    yield Button("查看课次", id="open-course", variant="primary")
                    yield Select([("搜索目录", "preview"), ("账号目录", "catalog")],
                                 value="preview", allow_blank=False, id="source")
            with TabPane("课次下载", id="recordings"):
                yield Static("请先在课程检索中选择课程。", id="course-title", markup=False)
                yield Static(self.preferences.output, id="destination", markup=False)
                with Horizontal(classes="buttons"):
                    yield Button("返回课程", id="back")
                    yield Button("全选", id="select-all")
                    yield Button("清空选择", id="select-none")
                    yield Button("查看视角", id="inspect-views")
                yield LessonSelectionList(id="lesson-list")
                with Horizontal(classes="filters"):
                    yield Input("all", placeholder="all 或视角编号", id="view")
                    yield Static("all 下载全部视角。[✓] 已选，空格切换。", id="view-note", markup=False)
                with Horizontal(classes="buttons"):
                    yield Button("预览路径", id="preview")
                    yield Button("下载所选", id="download", variant="primary")
                    yield Button("整课下载", id="download-all")
            with TabPane("下载管理", id="downloads"):
                yield Static("暂无下载任务", id="download-summary", markup=False)
                yield DownloadTable(id="download-table", cursor_type="row", zebra_stripes=True)
                yield Static("选择视频可暂停、续传或删除任务。", id="download-detail", markup=False)
                with Horizontal(classes="buttons", id="download-actions"):
                    yield Button("暂停", id="pause-download")
                    yield Button("续传 / 重试", id="resume-download", variant="primary")
                    yield Button("删除任务", id="delete-download")
                    yield Button("全部暂停", id="stop", variant="warning", disabled=True)
                    yield Button("全部续传", id="resume-all")
                with Horizontal(classes="buttons"):
                    yield Button("返回课次", id="manager-to-lessons")
                    yield Button("查看日志", id="show-log")
            with TabPane("任务日志", id="tasks"):
                yield RichLog(id="activity", wrap=True, markup=False, highlight=False, max_lines=1000)
                with Horizontal(classes="buttons"):
                    yield Button("返回课次", id="to-lessons")
        yield ProgressBar(total=1, show_eta=False, id="progress")
        yield Static("就绪 · Tab 切换控件，方向键选择，Enter 确定", id="status", markup=False)
        yield Footer()

    def on_mount(self):
        self.theme = "textual-dark"
        self.query_one("#catalog-table", DataTable).add_columns("课程", "教师", "课程号", "教学班 ID")
        table = self.query_one("#download-table", DataTable)
        for key, label, width in [("video", "视频 / 视角", 28), ("status", "状态", 6),
                                  ("progress", "进度", 7), ("size", "已下载 / 大小", 20),
                                  ("speed", "速度", 12)]:
            table.add_column(label, key=key, width=width)
        if self.download_manager.note:
            self._report(self.download_manager.note)
        if self.startup_note:
            self._report(self.startup_note)
        try:
            self.auth.restore()
            self.logged_in = True
        except AuthError:
            self._report("欢迎使用。请先登录校园账号，之后可连续搜索和下载。")
        except OSError:
            self._report("无法读取本地会话，请检查目录权限。")
        self._account_label()
        self.refresh_downloads(self.download_manager.snapshot())
        self.call_after_refresh(self.resize_download_table)
        if self.logged_in:
            self.load_terms()

    def _account_label(self):
        label = f"账号 {plain(self.auth.username)}" if self.logged_in else "尚未登录"
        if self.logged_in and self.auth.vpn:
            label += " · WebVPN"
        if not self.preferences.verify:
            label += " · HTTPS 证书验证已关闭"
        self.query_one("#account", Static).update(label)
        self.query_one("#login", Button).display = not self.logged_in
        self.query_one("#logout", Button).display = self.logged_in

    def _report(self, message):
        message = str(message)
        self.history.append(message)
        self.history = self.history[-1000:]
        self.query_one("#activity", RichLog).write(Text(message))
        self.query_one("#status", Static).update(message.splitlines()[0])

    def _thread_report(self, message):
        self.call_from_thread(self._report, message)

    def _set_busy(self, value):
        self.busy = value
        available = {"stop", "quit", "back", "to-lessons", "manage", "manager-to-lessons", "show-log"}
        if self.manager_active:
            available |= {"download-table", "pause-download", "resume-download", "delete-download", "resume-all"}
        for widget in self.query("Button, Input, Select, DataTable, SelectionList"):
            widget.disabled = value and widget.id not in available
        self.query_one("#stop", Button).disabled = not value

    def begin(self, description, operation, success):
        if self.busy:
            self.notify("当前任务仍在进行，请等待完成或停止。")
            return
        self.stop_event.clear()
        self._set_busy(True)
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self._report(description)
        self._operate(operation, success)

    @work(thread=True, exit_on_error=False)
    def _operate(self, operation, success):
        try:
            result = operation()
            error = None
        except Exception as exc:
            result = None
            error = exc
        self.call_from_thread(self._finish, result, error, success)

    def _finish(self, result, error, success):
        was_manager = self.manager_active
        self.manager_active = False
        self._set_busy(False)
        if not was_manager:
            self.query_one("#progress", ProgressBar).update(total=1, progress=1)
        if self.closing:
            self.http.close()
            self.exit()
            return
        if self.stop_event.is_set() or isinstance(error, DownloadCancelled):
            self._report("任务已停止；已下载文件保留，可在下载管理中续传。")
            return
        if error:
            message = str(error) if isinstance(error, VodError) else "操作失败，请检查本地文件权限和网络后重试。"
            self._report(message)
            self.notify(message, severity="error", timeout=8)
            return
        success(result)
        if was_manager and not self.download_manager.commands.empty():
            self.start_manager(owner=self.manager_owner)

    def require_login(self):
        if self.busy:
            return False
        if not self.logged_in:
            self.notify("请先登录校园账号。", severity="warning")
            self.action_login()
            return False
        return True

    def action_login(self):
        if not self.busy:
            self.push_screen(LoginDialog(self.auth.username, self.auth.vpn), self.login)

    def login(self, values):
        if values is None:
            return
        # Invalidate the visible catalog before a login attempt changes the session.
        self.logged_in = False
        self.clear_catalog()
        self._account_label()

        def operation():
            try:
                self.auth.login(**values, read_code=self.read_code, report=self._thread_report)
            finally:
                values["password"] = ""

        def success(_):
            self.logged_in = True
            self._account_label()
            self.refresh_downloads(self.download_manager.snapshot())
            self._report("登录成功，正在加载学期。")
            self.load_terms()

        self.begin("正在登录，请稍候……", operation, success)

    def read_code(self):
        answer = Future()
        self.pending_code = answer

        def show():
            self.push_screen(CodeDialog(), lambda value: answer.set_result(value) if not answer.done() else None)

        self.call_from_thread(show)
        try:
            while not self.stop_event.is_set():
                try:
                    value = answer.result(timeout=0.2)
                    if not value:
                        raise AuthError("已取消登录。")
                    return value
                except TimeoutError:
                    continue
            raise DownloadCancelled("已取消登录。")
        finally:
            self.pending_code = None

    def clear_catalog(self):
        self.catalog, self.lessons, self.current_course = [], [], None
        self.query_one("#catalog-table", DataTable).clear()
        self.query_one("#lesson-list", SelectionList).clear_options()
        self.query_one("#course-title", Static).update("请先选择课程。")

    def load_terms(self):
        if not self.require_login():
            return

        def success(terms):
            select = self.query_one("#term", Select)
            old = select.value
            select.set_options([(t["name"] + (" · 当前" if t["current"] else ""), t["term_id"]) for t in terms])
            ids = {t["term_id"] for t in terms}
            current = next((t["term_id"] for t in terms if t["current"]), Select.BLANK)
            select.value = old if old in ids else current
            self._report(f"已加载 {len(terms)} 个学期，可浏览或搜索课程。")

        self.begin("正在加载学期……", self.client.terms, success)

    @on(Select.Changed, "#term")
    def term_changed(self, event):
        self.clear_catalog()

    def load_courses(self, search=False):
        if not self.require_login():
            return
        term = self.query_one("#term", Select).value
        if term is Select.BLANK:
            self.notify("请先选择学期。", severity="warning")
            return
        keyword = self.query_one("#keyword", Input).value.strip()
        by_code = self.query_one("#by", Select).value == "code"
        if search and len(keyword) < 2:
            self.notify("搜索关键词至少需要 2 个字符。", severity="warning")
            return
        self.clear_catalog()

        def success(values):
            self.catalog = list({r["course_id"]: r for r in values}.values())
            table = self.query_one("#catalog-table", DataTable)
            for row in self.catalog:
                table.add_row(Text(row["name"]), Text("、".join(row.get("teachers", []))),
                              Text(row.get("code", "")), str(row["course_id"]), key=str(row["course_id"]))
            message = f"找到 {len(self.catalog)} 个教学班。选择课程后按 Enter 查看课次。"
            if search and not by_code:
                message += " 关键词接口最多匹配 50 个课次。"
            self.query_one("#catalog-hint", Static).update(message)
            self._report(message)
            table.focus()

        self.begin("正在检索课程……", lambda: self.client.search(keyword, term, by_code=by_code)
                   if search else self.client.courses(term), success)

    @on(Input.Submitted, "#keyword")
    def search_enter(self):
        self.load_courses(search=True)

    @on(DataTable.RowSelected, "#catalog-table")
    def row_selected(self):
        self.open_course()

    def open_course(self):
        if not self.require_login() or not self.catalog:
            return
        table = self.query_one("#catalog-table", DataTable)
        row = self.catalog[table.cursor_row]
        source = self.query_one("#source", Select).value

        def success(items):
            self.current_course, self.lessons = row, items
            choices = self.query_one("#lesson-list", SelectionList)
            choices.clear_options()
            choices.add_options([(lesson_label(item), item["lesson_id"], False) for item in items])
            self.query_one("#course-title", Static).update(
                f"{row['name']} · {'、'.join(row.get('teachers', [])) or '教师待查询'} · 教学班 {row['course_id']} · {len(items)} 课次")
            self.query_one("#view", Input).value = "all"
            self.show_tab("recordings")
            self._report("勾选课次后下载，或选择「整课下载」。" if items else "该教学班没有可用课次。")
            choices.focus()

        self.begin("正在读取课次目录……", lambda: self.client.lessons(
            row["course_id"], term_id=row["term_id"], source=source), success)

    def inspect_views(self):
        if self.busy or not self.lessons or not self.current_course:
            return
        choices = self.query_one("#lesson-list", SelectionList)
        selected = set(choices.selected)
        item = next((item for item in self.lessons if item["lesson_id"] in selected),
                    self.lessons[choices.highlighted or 0])

        def success(views):
            message = f"课次 {item['lesson_id']} 可用视角：" + "、".join(str(v["view"]) for v in views)
            self.query_one("#view-note", Static).update(message)
            self._report(message)

        self.begin("正在查询视角……", lambda: self.client.playback(self.current_course["course_id"], item["lesson_id"]), success)

    def download(self, *, all_lessons=False, preview=False):
        if self.busy or not self.current_course:
            return
        selected = set(self.query_one("#lesson-list", SelectionList).selected)
        items = [item for item in self.lessons if all_lessons or item["lesson_id"] in selected]
        if not items:
            self.notify("请先勾选课次，或选择整课下载。", severity="warning")
            return
        raw_view = self.query_one("#view", Input).value.strip()
        try:
            view = None if raw_view.lower() in {"all", ""} else int(raw_view)
            if view is not None and view < 0:
                raise ValueError
        except ValueError:
            self.notify("视角请填写 all 或非负整数。", severity="warning")
            return
        prefs, row = self.preferences, self.current_course
        self.show_tab("tasks" if preview else "downloads")

        def operation():
            course_info = self.client.course_metadata(row["course_id"], self.lessons, known=row)
            self.call_from_thread(self.update_progress, 0, len(items))
            result = download_lessons(self.client, items, output=Path(prefs.output), view=view,
                                      dry_run=True, course_info=course_info,
                                      report=self._thread_report, cancelled=self.stop_event.is_set,
                                      progress=lambda done, total: self.call_from_thread(self.update_progress, done, total))
            if not preview:
                self.download_manager.add(result["planned"], course_info, prefs.output, self.auth.username)
            return result

        def success(result):
            if preview:
                for item in result["planned"]:
                    self._report(item["path"])
            self._report(f"{'预览结束' if preview else '加入下载管理'}：{len(result['planned'])} 个视频，"
                         f"{len(result['unavailable'])} 个课次暂无录播，{len(result['failed'])} 个课次失败。")
            if not preview:
                self.refresh_downloads(self.download_manager.snapshot())
                if result["planned"]:
                    self.start_manager()

        self.begin(f"正在{'预览' if preview else '准备下载'} {len(items)} 个课次……", operation, success)

    def start_manager(self, owner=None):
        if self.busy:
            return
        self.manager_active = True
        owner = owner or self.auth.username
        self.manager_owner = owner

        def operation():
            self.download_manager.run(
                self.client, self.preferences, owner=owner, timeout=self.timeout, stopped=self.stop_event,
                changed=lambda rows: self.call_from_thread(self.refresh_downloads, rows), report=self._thread_report)

        def success(_):
            rows = self.download_rows
            complete = sum(r["status"] == "complete" for r in rows)
            paused = sum(r["status"] == "paused" for r in rows)
            errors = sum(r["status"] == "error" for r in rows)
            self._report(f"下载管理：{complete} 个已完成，{paused} 个已暂停，{errors} 个失败。")

        self.begin("正在处理下载任务……", operation, success)

    def selected_download(self):
        table = self.query_one("#download-table", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((row for row in self.download_rows if row["id"] == key), None)

    def resize_download_table(self):
        table = self.query_one("#download-table", DataTable)
        if table.size.width == 0:
            return
        widths = (max(21, table.size.width - 57), 6, 7, 20, 12)
        if widths == self.download_widths:
            return
        self.download_widths = widths
        cursor = table.cursor_row
        table.clear(columns=True)
        for key, label, width in zip(("video", "status", "progress", "size", "speed"),
                                     ("课次 / 视角 · 视频", "状态", "进度", "已下载 / 大小", "速度"), widths):
            table.add_column(label, key=key, width=width)
        self.refresh_downloads(self.download_rows)
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False)

    def refresh_downloads(self, rows):
        rows = [r for r in rows if not self.logged_in or r["owner"] == self.auth.username]
        self.download_rows = rows
        table = self.query_one("#download-table", DataTable)
        keys = {key.value for key in table.rows}
        current = {row["id"] for row in rows}
        for key in keys - current:
            table.remove_row(key)
        labels = {"queued": ("等待中", "dim"), "resolving": ("解析中", "cyan"),
                  "active": ("下载中", "cyan"), "paused": ("已暂停", "yellow"),
                  "complete": ("已完成", "green"), "error": ("失败", "red")}
        for row in rows:
            state, color = labels[row["status"]]
            percent = (f"{min(100, row['completed'] / row['total'] * 100):.1f}%" if row["total"]
                       else ("100%" if row["status"] == "complete" else "—"))
            values = [Text(f"{row['lesson_id']} · 视角{row['view']} · {Path(row['path']).name}"), Text(state, style=color), percent,
                      f"{byte_label(row['completed'])} / {byte_label(row['total']) if row['total'] else '未知'}",
                      byte_label(row["speed"]) + "/s" if row["status"] == "active" else "—"]
            if row["id"] not in keys:
                table.add_row(*values, key=row["id"])
            else:
                for key, value in zip(("video", "status", "progress", "size", "speed"), values):
                    table.update_cell(row["id"], key, value)
        active = sum(r["status"] in {"active", "resolving"} for r in rows)
        complete = sum(r["status"] == "complete" for r in rows)
        paused = sum(r["status"] == "paused" for r in rows)
        speed = sum(r["speed"] for r in rows)
        self.query_one("#download-summary", Static).update(
            f"共 {len(rows)} 个视频 · 下载中 {active} · 已完成 {complete} · 已暂停 {paused} · {byte_label(speed)}/s"
            if rows else "暂无下载任务 · 从课次列表添加视频")
        if self.manager_active:
            # Equal weight per file; unknown sizes stay at zero until known.
            done = sum(1 if r["status"] == "complete" else min(1, r["completed"] / r["total"])
                       if r["total"] else 0 for r in rows)
            self.update_progress(done, max(1, len(rows)))
        self.download_detail()

    @on(DataTable.RowHighlighted, "#download-table")
    def download_detail(self):
        row = self.selected_download()
        self.query_one("#download-detail", Static).update(
            f"{row['course']} · 课次 {row['lesson_id']} · 视角 {row['view']}\n{row['path']}"
            + (f"\n{row['error']}" if row["error"] else "")
            if row else "选择视频可暂停、续传或删除任务；未完成任务会保留到下次启动。")

    def manage_action(self, action, *, all_tasks=False):
        if self.busy and not self.manager_active:
            return
        row = self.selected_download()
        rows = self.download_rows if all_tasks else ([row] if row else [])
        if not rows:
            self.notify("请先选择一个下载任务。")
            return
        if action == "resume" and not self.logged_in:
            self.action_login()
            return
        if action == "delete":
            def confirmed(delete_files):
                if delete_files is not None:
                    self.download_manager.request("delete", [row["id"]], delete_files=delete_files)
                    if not self.busy:
                        self.start_manager(owner=row["owner"])
            self.push_screen(DeleteDownloadDialog(row), confirmed)
            return
        self.download_manager.request(action, [r["id"] for r in rows])
        if not self.busy:
            self.start_manager(owner=rows[0]["owner"])

    def update_progress(self, done, total):
        self.query_one("#progress", ProgressBar).update(total=total, progress=done)

    def save_settings(self, preferences):
        if preferences is None:
            return
        try:
            private_write(self.state_dir / "tui-settings.json", json.dumps(asdict(preferences), ensure_ascii=False, indent=2))
        except OSError:
            self.notify("设置保存失败，请检查目录权限。", severity="error")
            return
        self.preferences = preferences
        self.http.session.verify = preferences.verify
        self.query_one("#destination", Static).update(preferences.output)
        self._account_label()
        self._report("下载设置已保存。")

    def action_back(self):
        if isinstance(self.screen, Dialog):
            return
        self.show_tab("catalog")

    def show_tab(self, name):
        # A focused control in the previous pane can reactivate that pane.
        # Move focus after layout so the destination owns keyboard navigation.
        self.set_focus(None)
        self.query_one("#tabs", TabbedContent).active = name
        target = {"catalog": "#catalog-table", "recordings": "#lesson-list", "tasks": "#activity",
                  "downloads": "#download-table"}[name]
        self.call_after_refresh(self.query_one(target).focus)
        if name == "downloads":
            self.call_after_refresh(self.resize_download_table)

    def action_stop(self):
        if not self.busy:
            return
        self.stop_event.set()
        if self.pending_code and not self.pending_code.done():
            self.pending_code.set_result(None)
        if isinstance(self.screen, CodeDialog):
            self.screen.dismiss(None)
        self._report("正在停止；已发出的网络请求会在返回或超时后结束。")

    def action_request_quit(self):
        if self.busy:
            self.closing = True
            self.action_stop()
        else:
            self.http.close()
            self.exit()

    @on(Button.Pressed)
    def button_pressed(self, event):
        button = event.button.id
        if button == "quit":
            self.action_request_quit()
        elif button == "stop":
            self.action_stop()
        elif button == "back":
            self.action_back()
        elif button in {"to-lessons", "manager-to-lessons"}:
            self.show_tab("recordings")
        elif button == "manage":
            self.show_tab("downloads")
        elif button == "show-log":
            self.show_tab("tasks")
        elif button in {"pause-download", "resume-download", "delete-download", "resume-all"}:
            self.manage_action(button.split("-")[0], all_tasks=button == "resume-all")
        elif not self.busy:
            if button == "login":
                self.action_login()
            elif button == "terms":
                self.load_terms()
            elif button == "settings":
                self.push_screen(SettingsDialog(self.preferences), self.save_settings)
            elif button == "logout":
                try:
                    self.store.delete(AccountRef())
                except OSError:
                    self.notify("无法删除本地会话，请检查权限。", severity="error")
                    return
                self.http.session.cookies.clear()
                self.auth.jwt, self.auth.username, self.logged_in = "", "", False
                self.clear_catalog()
                self.query_one("#term", Select).set_options([])
                self._account_label()
                self.refresh_downloads(self.download_manager.snapshot())
                self._report("已退出登录，可在当前界面重新登录。")
            elif button == "browse":
                self.load_courses()
            elif button == "search":
                self.load_courses(search=True)
            elif button == "open-course":
                self.open_course()
            elif button == "select-all":
                self.query_one("#lesson-list", SelectionList).select_all()
            elif button == "select-none":
                self.query_one("#lesson-list", SelectionList).deselect_all()
            elif button == "inspect-views":
                self.inspect_views()
            elif button in {"preview", "download", "download-all"}:
                self.download(all_lessons=button == "download-all", preview=button == "preview")

    def on_unmount(self):
        self.stop_event.set()
        self.http.close()


def main():
    from .cli import main as cli_main
    return cli_main(["tui", *sys.argv[1:]])
