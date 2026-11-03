import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import Button, Checkbox, DataTable, Input, Select, SelectionList, Static, TabbedContent

from shu_vod.cli import main
from shu_vod.errors import DownloadCancelled, VodError
from shu_vod.tui import CodeDialog, DeleteDownloadDialog, LoginDialog, Preferences, SettingsDialog, VodApp


COURSE = {"course_id": 42, "term_id": 7, "name": "高等数学", "teachers": ["张三"], "code": "0800"}
LESSONS = [{"lesson_id": n, "course_id": 42, "name": "高等数学", "week": 1, "section": n,
            "begin_time": "2026-11-02 08:00", "teachers": ["张三"]} for n in (1, 2)]


async def wait_until(pilot, predicate):
    for _ in range(100):
        await pilot.pause(0.02)
        if predicate():
            return
    raise AssertionError(f"UI operation did not finish: {type(pilot.app.screen).__name__}; {pilot.app.history}")


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = VodApp(state_dir=Path(self.temp.name))
        self.app.client = Mock()
        self.app.client.terms.return_value = [{"term_id": 7, "name": "2026 秋季", "current": True}]
        self.app.client.courses.return_value = [COURSE]
        self.app.client.search.return_value = [{**COURSE, "lesson_id": 1}, {**COURSE, "lesson_id": 2}]
        self.app.client.lessons.return_value = LESSONS
        self.app.client.playback.return_value = [{"view": 7, "url": "https://vod.cc.shu.edu.cn/a.mp4?token=SECRET"}]
        self.app.client.course_metadata.return_value = COURSE
        self.app.client.auth.vpn = False

    async def seed_login(self, pilot):
        self.app.logged_in = True
        self.app.auth.username = "test-user"
        self.app._account_label()
        self.app.load_terms()
        await wait_until(pilot, lambda: not self.app.busy)

    async def open_course(self, pilot):
        await self.seed_login(pilot)
        await pilot.click("#browse")
        await wait_until(pilot, lambda: not self.app.busy)
        self.assertEqual(self.app.query_one("#catalog-table", DataTable).row_count, 1)
        await pilot.click("#open-course")
        await wait_until(pilot, lambda: not self.app.busy and
                         self.app.query_one("#tabs", TabbedContent).active == "recordings")
        self.assertEqual(self.app.query_one("#tabs", TabbedContent).active, "recordings")

    async def test_login_and_two_factor_stay_in_ui(self):
        received = []

        def login(username, password, *, vpn, method, read_code, report):
            received.append((username, password, vpn, method))
            report("验证码已发送。")
            received.append(read_code())
            self.app.auth.username = username

        self.app.auth.login = login
        async with self.app.run_test(size=(110, 36)) as pilot:
            await pilot.click("#login")
            await pilot.pause(0.2)
            self.assertIsInstance(self.app.screen, LoginDialog)
            self.app.screen.query_one("#username", Input).value = "12345"
            self.app.screen.query_one("#password", Input).value = "password-secret"
            self.app.screen.query_one("#password", Input).focus()
            await pilot.press("enter")
            await wait_until(pilot, lambda: isinstance(self.app.screen, CodeDialog))
            await pilot.pause(0.2)
            self.app.screen.query_one("#code", Input).value = "123456"
            self.app.screen.query_one("#code", Input).focus()
            await pilot.press("enter")
            await wait_until(pilot, lambda: self.app.logged_in and not self.app.busy)
            self.assertEqual(received[-1], "123456")
            self.assertEqual(self.app.query_one("#term", Select).value, 7)
            self.assertNotIn("password-secret", "\n".join(self.app.history))
            self.assertNotIn("123456", "\n".join(self.app.history))

    async def test_search_open_select_preview_and_return(self):
        async with self.app.run_test(size=(110, 36)) as pilot:
            await self.seed_login(pilot)
            self.app.query_one("#keyword", Input).value = "数学"
            await pilot.click("#search")
            await wait_until(pilot, lambda: not self.app.busy)
            self.assertEqual(len(self.app.catalog), 1)
            self.app.client.search.assert_called_once_with("数学", 7, by_code=False)
            await pilot.click("#open-course")
            await wait_until(pilot, lambda: not self.app.busy)
            self.app.query_one("#lesson-list", SelectionList).select(2)
            await pilot.click("#preview")
            await wait_until(pilot, lambda: not self.app.busy)
            history = "\n".join(self.app.history)
            self.assertIn("高等数学-0800-张三-42", history)
            self.assertIn("2026-11-02_0800-第1周-第2节-课次2-视角7.mp4", history)
            self.assertNotIn("SECRET", history)
            await pilot.click("#to-lessons")
            await pilot.click("#back")
            self.assertEqual(self.app.query_one("#tabs", TabbedContent).active, "catalog")
            self.assertEqual(self.app.query_one("#keyword", Input).value, "数学")

    async def test_download_selection_errors_do_not_exit(self):
        async with self.app.run_test(size=(110, 36)) as pilot:
            await self.open_course(pilot)
            self.app.query_one("#lesson-list", SelectionList).select(2)
            self.app.query_one("#view", Input).value = "7"
            with patch("shu_vod.tui.download_lessons") as download:
                download.return_value = {"downloaded": [], "planned": [], "unavailable": [],
                                         "failed": [{"lesson_id": 2, "error": "not allowed"}]}
                await pilot.click("#download")
                await wait_until(pilot, lambda: not self.app.busy)
                self.assertEqual(download.call_args.args[1], [LESSONS[1]])
                self.assertEqual(download.call_args.kwargs["view"], 7)
                self.assertEqual(download.call_args.kwargs["course_info"], COURSE)
                self.assertIn("1 个课次失败", self.app.history[-1])
            await pilot.click("#manager-to-lessons")
            self.assertEqual(self.app.query_one("#lesson-list", SelectionList).selected, [2])

    async def test_stopping_background_download_keeps_app_usable(self):
        def download(*args, stopped, **kwargs):
            import time
            while not stopped.is_set():
                time.sleep(0.01)

        async with self.app.run_test(size=(110, 36)) as pilot:
            await self.open_course(pilot)
            with patch.object(self.app.download_manager, "run", side_effect=download):
                await pilot.click("#download-all")
                await wait_until(pilot, lambda: self.app.manager_active)
                self.assertTrue(self.app.busy)
                await pilot.click("#stop")
                await wait_until(pilot, lambda: not self.app.busy)
            await pilot.click("#manager-to-lessons")
            self.assertEqual(len(self.app.lessons), 2)
            self.assertIn("任务已停止", self.app.history[-1])

    async def test_manager_navigation_controls_and_delete_confirmation(self):
        root = Path(self.temp.name) / "downloads"
        path = root / "高等数学-0800-张三-42" / "课次1-视角7.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"partial")
        self.app.download_manager.add([{"path": str(path), "lesson_id": 1, "view": 7}],
                                      COURSE, root, "test-user")
        self.app.download_manager.tasks[0].status = "paused"
        async with self.app.run_test(size=(100, 32)) as pilot:
            await self.seed_login(pilot)
            self.app.refresh_downloads(self.app.download_manager.snapshot())
            self.assertFalse(self.app.query_one("#login", Button).display)
            self.assertTrue(self.app.query_one("#logout", Button).display)
            self.assertEqual(self.app.query_one("#account").region.y, self.app.query_one("#logout").region.y)
            self.assertNotIn("校园网直连", str(self.app.query_one("#account", Static).render()))
            await pilot.click("#manage")
            self.assertEqual(self.app.query_one("#tabs", TabbedContent).active, "downloads")
            self.assertEqual(self.app.query_one("#download-table", DataTable).row_count, 1)
            await pilot.resize_terminal(80, 28)
            await pilot.pause()
            self.assertLessEqual(self.app.query_one("#resume-all").region.right, 80)
            table = self.app.query_one("#download-table", DataTable)
            self.assertLessEqual(table.virtual_size.width, table.size.width)
            commands = []
            with patch.object(self.app.download_manager, "run", side_effect=lambda *a, **kw:
                              commands.append(self.app.download_manager.commands.get_nowait())):
                await pilot.click("#resume-download")
                await wait_until(pilot, lambda: not self.app.busy)
                action, ids, _ = commands[0]
                self.assertEqual(action, "resume")
                self.assertEqual(ids, {self.app.download_rows[0]["id"]})
            await pilot.click("#delete-download")
            await pilot.pause(0.2)
            self.assertIsInstance(self.app.screen, DeleteDownloadDialog)
            self.assertFalse(self.app.screen.query_one("#delete-files", Checkbox).value)
            await pilot.click("#cancel")
            self.assertTrue(path.exists())
            await pilot.click("#delete-download")
            await pilot.pause(0.2)
            await pilot.click("#confirm-delete")
            await wait_until(pilot, lambda: not self.app.busy and not self.app.download_rows)
            self.assertTrue(path.exists())

    async def test_error_and_cancelled_login_leave_main_screen(self):
        def login(**kwargs):
            kwargs["read_code"]()

        self.app.auth.login = login
        async with self.app.run_test(size=(100, 32)) as pilot:
            await pilot.click("#login")
            await pilot.pause(0.2)
            self.app.screen.query_one("#username", Input).value = "12345"
            self.app.screen.query_one("#password", Input).value = "secret"
            await pilot.click("#submit")
            await wait_until(pilot, lambda: isinstance(self.app.screen, CodeDialog))
            await pilot.press("escape")
            await wait_until(pilot, lambda: not self.app.busy)
            self.assertFalse(self.app.logged_in)
            self.assertNotIsInstance(self.app.screen, CodeDialog)
            self.assertIn("已取消登录", self.app.history[-1])

    async def test_settings_persist_and_small_terminal_is_usable(self):
        async with self.app.run_test(size=(80, 28)) as pilot:
            await pilot.click("#settings")
            self.assertIsInstance(self.app.screen, SettingsDialog)
            output = str(Path(self.temp.name) / "课程录播")
            self.app.screen.query_one("#output", Input).value = output
            await pilot.click("#submit")
            await pilot.pause()
            stored = json.loads((Path(self.temp.name) / "tui-settings.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["output"], output)
            self.assertNotIn("password", stored)
        restored = VodApp(state_dir=Path(self.temp.name))
        self.assertEqual(restored.preferences.output, output)
        restored.http.close()


class EntryTests(unittest.TestCase):
    def test_no_args_opens_tui_and_cli_still_works(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch("shu_vod.tui.VodApp") as app:
            self.assertEqual(main([]), 0)
            app.return_value.run.assert_called_once()
        with contextlib.redirect_stderr(io.StringIO()), patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(main([]), 1)
