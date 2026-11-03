import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shu_vod.download_manager import DownloadManager
from shu_vod.errors import SessionExpired, VodError


class ManagerStoreTests(unittest.TestCase):
    def test_errors_are_visible_and_expired_session_pauses_remaining_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = DownloadManager(root / "state")
            planned = [{"path": str(root / "课程" / f"课次{n}.mp4"), "lesson_id": n, "view": 7}
                       for n in (1, 2)]
            manager.add(planned, {"course_id": 42, "name": "数学"}, root, "user")
            client = Mock()
            client.auth.vpn = False
            client.playback.return_value = [{"view": 7, "url": "https://vod.cc.shu.edu.cn/video.mp4?token=SECRET"}]
            prefs = SimpleNamespace(aria2="aria2c", connections=1, jobs=2, verify=True)
            with patch("shu_vod.download_manager.Aria2RPC") as factory, patch("shu_vod.download_manager.probe_media", side_effect=lambda url, auth: url):
                rpc = factory.return_value.__enter__.return_value
                rpc.add.side_effect = ["gid-1", "gid-2"]
                rpc.status.return_value = {"status": "error", "errorCode": "3", "errorMessage": "SECRET"}
                manager.run(client, prefs, owner="user", stopped=threading.Event())
            self.assertEqual([t.status for t in manager.tasks], ["error", "error"])
            self.assertIn("代码 3", manager.tasks[0].error)
            self.assertNotIn("SECRET", manager.file.read_text(encoding="utf-8"))
            manager.request("resume", [t.id for t in manager.tasks])
            client.playback.side_effect = SessionExpired("请重新登录")
            with patch("shu_vod.download_manager.Aria2RPC"), self.assertRaises(SessionExpired):
                manager.run(client, prefs, owner="user", stopped=threading.Event())
            self.assertEqual([t.status for t in manager.tasks], ["error", "paused"])

    def test_restores_paused_deduplicates_and_checks_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = DownloadManager(root / "state")
            planned = [{"path": str(root / "课程" / "课次1-视角7.mp4"), "lesson_id": 1, "view": 7}]
            manager.add(planned, {"course_id": 42, "name": "数学"}, root, "user")
            manager.add(planned, {"course_id": 42, "name": "数学"}, root, "user")
            self.assertEqual(len(manager.tasks), 1)
            with self.assertRaises(VodError):
                manager.add(planned, {"course_id": 42, "name": "数学"}, root, "another")
            restored = DownloadManager(root / "state")
            self.assertEqual(restored.tasks[0].status, "paused")
            self.assertEqual(restored.tasks[0].speed, 0)
            record = restored.snapshot()[0]
            self.assertNotIn("url", record)
            self.assertNotIn("cookie", record)

    def test_delete_is_scoped_to_task_and_default_keeps_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "课程" / "课次1.mp4"
            path.parent.mkdir()
            path.write_bytes(b"video")
            control = Path(str(path) + ".aria2")
            control.write_bytes(b"resume")
            unrelated = path.with_name("课次2.mp4")
            unrelated.write_bytes(b"keep")
            manager = DownloadManager(root / "state")
            planned = [{"path": str(path), "lesson_id": 1, "view": 7}]
            course = {"course_id": 42, "name": "数学"}
            manager.add(planned, course, root, "user")
            manager.tasks[0].status = "paused"
            manager.request("delete", [manager.tasks[0].id])
            manager.run(Mock(), Mock(), owner="user", stopped=threading.Event())
            self.assertTrue(path.exists())
            manager.add(planned, course, root, "user")
            manager.tasks[0].status = "paused"
            manager.request("delete", [manager.tasks[0].id], delete_files=True)
            manager.run(Mock(), Mock(), owner="user", stopped=threading.Event())
            self.assertFalse(path.exists())
            self.assertFalse(control.exists())
            self.assertTrue(unrelated.exists())


@unittest.skipUnless(shutil.which("aria2c"), "aria2c not installed")
class NativeManagerTests(unittest.TestCase):
    def test_per_video_progress_pause_restart_resume_and_active_delete(self):
        payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8192
        ranges = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                start, end = 0, len(payload) - 1
                header = self.headers.get("Range")
                if header:
                    left, right = header.removeprefix("bytes=").split("-", 1)
                    start, end = int(left), min(int(right), end) if right else end
                    ranges.append(start)
                self.send_response(206 if header else 200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                if header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                try:
                    for offset in range(start, end + 1, 16384):
                        self.wfile.write(payload[offset:min(offset + 16384, end + 1)])
                        self.wfile.flush()
                        time.sleep(0.015)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as folder, patch("shu_vod.download_manager.probe_media", side_effect=lambda url, auth: url):
                root = Path(folder)
                manager = DownloadManager(root / "state")
                course = {"course_id": 42, "name": "数学"}
                planned = [{"path": str(root / "数学-0800-张三-42" / f"课次1-视角{n}.mp4"),
                            "lesson_id": 1, "view": n} for n in (1, 2)]
                manager.add(planned, course, root, "user")
                client = Mock()
                client.auth.vpn = False
                client.playback.return_value = [{"view": n, "url": f"http://127.0.0.1:{server.server_port}/{n}.mp4?token=SECRET"}
                                                for n in (1, 2, 3)]
                prefs = SimpleNamespace(aria2="aria2c", connections=1, jobs=2, verify=True)
                paused = False
                snapshots = []

                def changed(rows):
                    nonlocal paused
                    snapshots.append(rows)
                    if not paused and rows[0]["completed"] > 0 and rows[0]["status"] == "active":
                        paused = True
                        manager.request("pause", [rows[0]["id"]])

                manager.run(client, prefs, owner="user", stopped=threading.Event(), changed=changed)
                self.assertTrue(paused)
                self.assertEqual([t.status for t in manager.tasks], ["paused", "complete"])
                self.assertTrue(any(r["speed"] > 0 for rows in snapshots for r in rows))
                self.assertTrue(any(0 < r["completed"] < r["total"] for rows in snapshots for r in rows))
                self.assertEqual(Path(planned[1]["path"]).read_bytes(), payload)
                persisted = manager.file.read_text(encoding="utf-8")
                self.assertNotIn("SECRET", persisted)
                self.assertNotIn("http", persisted)

                manager = DownloadManager(root / "state")
                ranges.clear()
                manager.request("resume", [manager.tasks[0].id])
                manager.run(client, prefs, owner="user", stopped=threading.Event())
                self.assertEqual([t.status for t in manager.tasks], ["complete", "complete"])
                self.assertEqual(Path(planned[0]["path"]).read_bytes(), payload)
                self.assertTrue(any(offset > 0 for offset in ranges), ranges)

                target = root / "数学-0800-张三-42" / "课次1-视角3.mp4"
                manager.add([{"path": str(target), "lesson_id": 1, "view": 3}], course, root, "user")
                deleted = False

                def remove_active(rows):
                    nonlocal deleted
                    if len(rows) == 3 and rows[2]["completed"] > 0 and not deleted:
                        deleted = True
                        manager.request("delete", [rows[2]["id"]], delete_files=True)

                manager.run(client, prefs, owner="user", stopped=threading.Event(), changed=remove_active)
                self.assertTrue(deleted)
                self.assertEqual(len(manager.tasks), 2)
                self.assertFalse(target.exists())
                self.assertFalse(Path(str(target) + ".aria2").exists())
                self.assertEqual(Path(planned[0]["path"]).read_bytes(), payload)
                # Stopping the whole worker must flush aria2's continuation data too.
                manager.add([{"path": str(target), "lesson_id": 1, "view": 3}], course, root, "user")
                stopped = threading.Event()

                def stop_all(rows):
                    if rows[-1]["completed"] > 0:
                        stopped.set()

                manager.run(client, prefs, owner="user", stopped=stopped, changed=stop_all)
                self.assertEqual(manager.tasks[-1].status, "paused")
                self.assertTrue(Path(str(target) + ".aria2").exists())
                self.assertEqual(DownloadManager(root / "state").tasks[-1].status, "paused")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
