from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .errors import DownloadCancelled, NoPlayback, PermissionDenied, SessionExpired, VodError
from .naming import course_directory, lesson_filename
from .transport import MAPPING, UA, media_url


def validate_video(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0 or Path(str(path) + ".aria2").exists():
        raise VodError("aria2 未生成完整视频，请重试。")
    with path.open("rb") as stream:
        prefix = stream.read(256).lstrip().lower()
    if prefix.startswith((b"<!doctype", b"<html", b"{", b"[", b"#extm3u")):
        raise VodError("下载结果是网页、错误响应或播放列表，请重新登录后重试。")


class Aria2Downloader:
    """Native aria2 process, stdin job list, scoped cookie file, deterministic paths."""
    def __init__(self, executable="aria2c", *, connections=8, jobs=2, verify=True, timeout=30,
                 cancelled=lambda: False):
        self.executable = shutil.which(executable)
        if not self.executable:
            raise VodError("找不到 aria2c，请安装 aria2 或使用 --aria2 指定可执行文件。")
        if not 1 <= connections <= 16 or not 1 <= jobs <= 16:
            raise VodError("connections 和 jobs 必须在 1–16 之间。")
        self.connections, self.jobs = connections, jobs
        self.verify, self.timeout = verify, timeout
        self.cancelled = cancelled

    @staticmethod
    def input_text(tasks):
        lines = []
        for task in tasks:
            path = Path(task["path"]).resolve()
            values = [task["url"], str(path.parent), path.name]
            if any(any(ord(ch) < 32 for ch in value) for value in values):
                raise VodError("下载参数包含无效控制字符。")
            if any(ch.isspace() for ch in task["url"]):
                raise VodError("下载地址包含未编码的空白字符。")
            lines += [task["url"], f"  dir={path.parent}", f"  out={path.name}"]
        return "\n".join(lines) + "\n"

    def run(self, tasks, gateway_cookie=""):
        if self.cancelled():
            raise DownloadCancelled("已停止下载，已有数据保留。")
        if not tasks:
            return
        for task in tasks:
            Path(task["path"]).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="shu-vod-aria2-") as temp:
            cookie_path = Path(temp) / "cookies.txt"
            cookie_lines = ["# Netscape HTTP Cookie File"]
            if gateway_cookie:
                if any(ord(ch) < 32 or ch == ";" for ch in gateway_cookie):
                    raise VodError("下载 Cookie 无效。")
                for host in sorted({urlsplit(t["url"]).hostname for t in tasks}):
                    if host not in MAPPING.values():
                        raise VodError("拒绝向非 WebVPN 媒体域名发送 Cookie。")
                    cookie_lines.append(f"{host}\tFALSE\t/\tTRUE\t0\twebvpn-token\t{gateway_cookie}")
            cookie_path.write_text("\n".join(cookie_lines) + "\n", encoding="utf-8")
            cookie_path.chmod(0o600)
            command = [self.executable, "--no-conf=true", "--input-file=-", "--continue=true",
                       "--always-resume=true", "--auto-file-renaming=false", "--allow-overwrite=false",
                       "--file-allocation=none", "--no-netrc=true", "--enable-rpc=false",
                       "--follow-torrent=false", "--follow-metalink=false", "--quiet=true",
                       "--download-result=hide", "--auto-save-interval=5", "--max-tries=3",
                       "--retry-wait=2", f"--connect-timeout={self.timeout}", f"--timeout={self.timeout}",
                       f"--split={self.connections}", f"--max-connection-per-server={self.connections}",
                       f"--max-concurrent-downloads={self.jobs}", f"--user-agent={UA}",
                       f"--check-certificate={str(self.verify).lower()}", f"--load-cookies={cookie_path}"]
            # No URLs or tokens in argv or logs. Cookie matching remains domain scoped on redirects.
            try:
                with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL) as process:
                    try:
                        pending_input = self.input_text(tasks).encode("utf-8")
                        while True:
                            if self.cancelled():
                                raise DownloadCancelled("已停止下载，已有数据保留，可再次选择相同课次续传。")
                            try:
                                process.communicate(pending_input, timeout=0.25)
                                break
                            except subprocess.TimeoutExpired:
                                pending_input = None
                    except (KeyboardInterrupt, DownloadCancelled):
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise
                    code = process.returncode
            except OSError:
                raise VodError("无法启动 aria2c，请检查可执行文件路径。") from None
            if code:
                raise VodError(f"aria2 下载失败（退出码 {code}）；保留了已下载数据，可重跑同一命令续传。")
        for task in tasks:
            validate_video(task["path"])


def probe_media(url: str, auth) -> str:
    """Check an authenticated media response without downloading the video body."""
    # Separate session: never forward the SSO cookie or business JWT to media.
    with requests.Session() as session:
        session.verify = auth.http.session.verify
        session.headers.update({"User-Agent": UA})
        gateway = auth.http.gateway_cookie() if auth.vpn else ""
        for host in MAPPING.values():
            if gateway:
                session.cookies.set("webvpn-token", gateway, domain=host, path="/", secure=True)
        for _ in range(6):
            url = media_url(url, auth.vpn)
            try:
                with session.get(url, headers={"Range": "bytes=0-255"}, stream=True,
                                 allow_redirects=False, timeout=auth.http.timeout) as response:
                    if response.status_code in {401, 403}:
                        raise PermissionDenied("媒体访问被拒绝，请确认观看权限或重新登录。")
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if not location:
                            raise VodError("媒体重定向缺少目标地址。")
                        url = urljoin(url, location)
                        continue
                    if response.status_code not in {200, 206}:
                        raise VodError(f"媒体不可用（HTTP {response.status_code}）。")
                    kind = response.headers.get("Content-Type", "").lower()
                    prefix = response.raw.read(256).lstrip().lower()
                    if "html" in kind or "json" in kind or prefix.startswith((b"<!doctype", b"<html", b"{", b"[")):
                        raise SessionExpired("媒体返回登录页或错误响应，请重新登录。")
                    if "mpegurl" in kind or prefix.startswith(b"#extm3u"):
                        raise VodError("当前资源是 HLS 播放列表；此版本仅下载平台返回的直链视频。")
                    return url
            except (requests.RequestException, OSError):
                raise VodError("媒体检查失败，请检查网络或 TLS 证书。") from None
    raise VodError("媒体重定向次数过多。")


def download_lessons(client, lessons, *, output: Path, view=None, dry_run=False,
                     downloader=None, teachers=(), course_info=None, report=lambda _: None,
                     cancelled=lambda: False, progress=lambda completed, total: None):
    result = {"downloaded": [], "planned": [], "unavailable": [], "failed": []}
    for index, item in enumerate(lessons):
        if cancelled():
            raise DownloadCancelled("已停止下载，已有数据保留。")
        lid, cid = item["lesson_id"], item["course_id"]
        try:
            views = client.playback(cid, lid)
            if view is not None:
                views = [v for v in views if v["view"] == view]
                if not views:
                    raise VodError("所选视角在该课次中不存在。")
            tasks = []
            metadata = course_info if course_info is not None else item
            if metadata.get("course_id") != cid:
                raise VodError("下载目录的课程信息与课次不一致。")
            directory = output.resolve() / course_directory(metadata, teachers or metadata.get("teachers", []))
            for v in views:
                suffix = Path(urlsplit(v["url"]).path).suffix.lower()
                if suffix not in {".mp4", ".flv", ".mkv", ".webm", ".mov", ".ts"}:
                    suffix = ".mp4"
                path = directory / lesson_filename(item, v["view"], suffix)
                url = v["url"] if dry_run else probe_media(v["url"], client.auth)
                tasks.append({"url": url, "path": str(path), "lesson_id": lid, "view": v["view"]})
            if cancelled():
                raise DownloadCancelled("已停止下载，已有数据保留。")
            safe_tasks = [{k: v for k, v in t.items() if k != "url"} for t in tasks]
            if dry_run:
                result["planned"].extend(safe_tasks)
            else:
                report(f"正在下载课次 {lid}，共 {len(tasks)} 个视角……")
                downloader.run(tasks, client.http.gateway_cookie() if client.auth.vpn else "")
                result["downloaded"].extend(safe_tasks)
                report(f"课次 {lid} 下载完成。")
        except NoPlayback:
            result["unavailable"].append(lid)
        except (SessionExpired, DownloadCancelled):
            raise
        except VodError as exc:
            result["failed"].append({"lesson_id": lid, "error": str(exc)})
            report(f"课次 {lid}：{exc}")
        progress(index + 1, len(lessons))
    return result
