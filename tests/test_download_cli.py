import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shu_vod.cli import main, parser
from shu_vod.download import Aria2Downloader, download_lessons, probe_media
from shu_vod.errors import DownloadCancelled, NoPlayback, VodError


class DownloadTests(unittest.TestCase):
    def test_job_input_rejects_injection_and_keeps_spaces(self):
        text = Aria2Downloader.input_text([{"url": "https://vod.cc.shu.edu.cn/video.mp4?signature=a%2Fb",
                                           "path": str(Path.cwd() / "with space" / "video.mp4")}])
        self.assertIn("signature=a%2Fb", text)
        self.assertIn("with space", text)
        with self.assertRaises(VodError):
            Aria2Downloader.input_text([{"url": "https://vod.cc.shu.edu.cn/x\n  header=bad", "path": "x"}])

    def test_dry_run_no_download_or_secrets_and_selects_view(self):
        client = Mock()
        client.playback.return_value = [{"view": 7, "url": "https://vod.cc.shu.edu.cn/a.mp4?token=SECRET"},
                                        {"view": 8, "url": "https://vod.cc.shu.edu.cn/b.mp4?token=SECRET"}]
        downloader = Mock()
        with tempfile.TemporaryDirectory() as root:
            result = download_lessons(client, [{"lesson_id": 31, "course_id": 42,
                                               "begin_time": "2026-11-02 08:00", "week": 1, "section": 2}],
                                      output=Path(root), view=7, dry_run=True, downloader=downloader,
                                      course_info={"course_id": 42, "name": "高等数学", "code": "0800", "teachers": ["张三"]})
            self.assertEqual(len(result["planned"]), 1)
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(Path(result["planned"][0]["path"]).parent.name, "高等数学-0800-张三-42")
            self.assertEqual(Path(result["planned"][0]["path"]).name,
                             "2026-11-02_0800-第1周-第2节-课次31-视角7.mp4")
            self.assertFalse((Path(root) / "高等数学-0800-张三-42").exists())
        downloader.run.assert_not_called()

    def test_batch_records_unavailable_and_failure_but_continues(self):
        client = Mock()
        client.playback.side_effect = [NoPlayback(), VodError("no permission"),
                                       [{"view": 7, "url": "https://vod.cc.shu.edu.cn/a.mp4"}]]
        result = download_lessons(client, [{"lesson_id": n, "course_id": 42} for n in [1, 2, 3]],
                                  output=Path("unused"), dry_run=True)
        self.assertEqual(result["unavailable"], [1])
        self.assertEqual(result["failed"][0]["lesson_id"], 2)
        self.assertEqual(result["planned"][0]["lesson_id"], 3)

    def test_cli_missing_login_is_clean_json(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["courses", "--state-dir", root, "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())["exit_code"], 3)
        self.assertNotIn("Traceback", out.getvalue())

    def test_cli_download_selection_and_partial_exit(self):
        with (patch("shu_vod.cli.AuthManager"), patch("shu_vod.cli.VodClient") as client_type,
              patch("shu_vod.cli.download_lessons") as download):
            client = client_type.return_value
            client.course_metadata.return_value = {"course_id": 42, "name": "高等数学", "code": "0800", "teachers": ["张三"]}
            client.lessons.return_value = [{"lesson_id": 1, "course_id": 42}, {"lesson_id": 2, "course_id": 42}]
            download.return_value = {"planned": [{"lesson_id": 2}], "downloaded": [], "unavailable": [], "failed": []}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["download", "--course", "42", "--lesson", "2", "--dry-run", "--json"]), 0)
            self.assertEqual(download.call_args.args[1], [{"lesson_id": 2, "course_id": 42}])
            self.assertEqual(download.call_args.kwargs["course_info"]["code"], "0800")
            self.assertEqual(len(client.course_metadata.call_args.args[1]), 2)
            json.loads(out.getvalue())
            download.return_value["failed"] = [{"lesson_id": 1, "error": "failed"}]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["download", "--course", "42", "--dry-run", "--json"]), 5)

    def test_cli_rejects_wrong_course_before_download(self):
        with patch("shu_vod.cli.AuthManager"), patch("shu_vod.cli.VodClient") as client_type:
            client_type.return_value.lessons.return_value = [{"lesson_id": 1, "course_id": 42}]
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["download", "--course", "42", "--lesson", "99", "--dry-run", "--json"])
            self.assertEqual(code, 1)

    def test_media_probe_rejects_html_and_hls(self):
        for prefix, content_type in [(b"<html>login</html>", "text/html"), (b"#EXTM3U", "application/vnd.apple.mpegurl")]:
            session = Mock()
            response = Mock(status_code=200, headers={"Content-Type": content_type})
            response.raw.read.return_value = prefix
            session.get.return_value.__enter__ = Mock(return_value=response)
            session.get.return_value.__exit__ = Mock(return_value=False)
            auth = SimpleNamespace(vpn=False, http=SimpleNamespace(session=SimpleNamespace(verify=True), timeout=30))
            with patch("shu_vod.download.requests.Session") as factory:
                factory.return_value.__enter__.return_value = session
                with self.assertRaises(VodError):
                    probe_media("https://vod.cc.shu.edu.cn/a.mp4", auth)


@unittest.skipUnless(shutil.which("aria2c"), "aria2c not installed")
class RealAria2Tests(unittest.TestCase):
    def test_cancel_real_aria2_then_resume(self):
        import time
        payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 32768
        started = threading.Event()
        cancelled = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                start, end = 0, len(payload) - 1
                value = self.headers.get("Range")
                if value:
                    left, right = value.removeprefix("bytes=").split("-", 1)
                    start, end = int(left), int(right) if right else end
                self.send_response(206 if value else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                if value:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                try:
                    for offset in range(start, end + 1, 65536):
                        self.wfile.write(payload[offset:min(offset + 65536, end + 1)])
                        self.wfile.flush()
                        started.set()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        stopper = threading.Thread(target=lambda: (started.wait(5), cancelled.set()), daemon=True)
        stopper.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "录播.mp4"
                tasks = [{"url": f"http://127.0.0.1:{server.server_port}/recording.mp4", "path": str(path)}]
                with self.assertRaises(DownloadCancelled):
                    Aria2Downloader(connections=1, cancelled=cancelled.is_set).run(tasks)
                self.assertTrue(started.is_set())
                self.assertTrue(path.exists())
                Aria2Downloader(connections=1).run(tasks)
                self.assertEqual(path.read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            stopper.join(timeout=3)

    def test_real_download_resume_unicode_paths_and_http_failure(self):
        payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 32768
        received_ranges = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/missing"):
                    self.send_error(404)
                    return
                begin = 0
                end = len(payload) - 1
                header = self.headers.get("Range")
                if header:
                    received_ranges.append(header)
                    start, stop = header.removeprefix("bytes=").split("-", 1)
                    begin = int(start)
                    end = min(int(stop), end) if stop else end
                if begin >= len(payload):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(payload)}")
                    self.end_headers()
                    return
                body = payload[begin:end + 1]
                self.send_response(206 if header else 200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                if header:
                    self.send_header("Content-Range", f"bytes {begin}-{end}/{len(payload)}")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                target = Path(root) / "课程 with space" / "录播.mp4"
                url = f"http://127.0.0.1:{server.server_port}/recording.mp4?signature=test"
                downloader = Aria2Downloader(connections=2, timeout=5)
                tasks = [{"url": url, "path": str(target)}]
                downloader.run(tasks)
                self.assertEqual(target.read_bytes(), payload)
                downloader.run(tasks)
                self.assertEqual(target.read_bytes(), payload)
                self.assertFalse(target.with_name("录播.1.mp4").exists())
                # Simulate an interrupted HTTP download lacking a control file.
                received_ranges.clear()
                target.write_bytes(payload[:3 * 1024 * 1024 + 12345])
                downloader.run(tasks)
                self.assertEqual(target.read_bytes(), payload)
                self.assertTrue(any(int(r.removeprefix("bytes=").split("-")[0]) > 0
                                    for r in received_ranges), received_ranges)
                with self.assertRaises(VodError):
                    downloader.run([{"url": f"http://127.0.0.1:{server.server_port}/missing", "path": str(target.with_name("missing.mp4"))}])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
