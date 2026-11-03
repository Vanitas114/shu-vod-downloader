"""Persistent, credential-free task records and a private native aria2 worker."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

import requests

from .accounts import private_write
from .download import Aria2Downloader, probe_media, validate_video
from .errors import SessionExpired, VodError
from .transport import MAPPING, UA


STATES = {"queued", "resolving", "active", "paused", "complete", "error"}
RUNNING = {"queued", "resolving", "active"}


@dataclass
class DownloadTask:
    id: str
    owner: str
    course_id: int
    lesson_id: int
    view: int
    course: str
    root: str
    path: str
    status: str = "queued"
    completed: int = 0
    total: int = 0
    speed: int = 0
    error: str = ""


class Aria2RPC:
    """One child process, loopback-only RPC, random secret, no media URLs on disk."""

    def __init__(self, executable="aria2c", *, connections=8, jobs=2, verify=True, timeout=30,
                 gateway_cookie=""):
        self.executable = shutil.which(executable)
        if not self.executable:
            raise VodError("找不到 aria2c，请在下载设置中填写程序路径。")
        self.connections, self.jobs = connections, jobs
        self.verify, self.timeout = verify, timeout
        self.gateway_cookie = gateway_cookie
        self.secret = secrets.token_hex(24)
        self.session = requests.Session()
        self.session.trust_env = False
        self.process = None
        self.temp = None

    def __enter__(self):
        try:
            self.temp = tempfile.TemporaryDirectory(prefix="shu-vod-manager-")
            root = Path(self.temp.name)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            self.endpoint = f"http://127.0.0.1:{port}/jsonrpc"
            cookie = self.gateway_cookie
            if any(ord(c) < 32 or c == ";" for c in cookie):
                raise VodError("下载 Cookie 无效。")
            lines = ["# Netscape HTTP Cookie File"]
            if cookie:
                for host in sorted(MAPPING.values()):
                    lines.append(f"{host}\tFALSE\t/\tTRUE\t0\twebvpn-token\t{cookie}")
            private_write(root / "cookies.txt", "\n".join(lines) + "\n")
            options = {
                "enable-rpc": "true", "rpc-listen-all": "false", "rpc-listen-port": port,
                "rpc-secret": self.secret, "rpc-allow-origin-all": "false",
                "continue": "true", "always-resume": "true", "auto-file-renaming": "false",
                "allow-overwrite": "false", "file-allocation": "none", "no-netrc": "true",
                "follow-torrent": "false", "follow-metalink": "false", "enable-dht": "false",
                "enable-dht6": "false", "quiet": "true", "download-result": "hide",
                "auto-save-interval": 1, "max-tries": 3, "retry-wait": 2,
                "connect-timeout": self.timeout, "timeout": self.timeout,
                "split": self.connections, "max-connection-per-server": self.connections,
                "max-concurrent-downloads": self.jobs, "user-agent": UA,
                "check-certificate": str(self.verify).lower(), "load-cookies": root / "cookies.txt",
                "stop-with-process": os.getpid(),
            }
            config = root / "aria2.conf"
            private_write(config, "\n".join(f"{key}={value}" for key, value in options.items()))
            self.process = subprocess.Popen(
                [self.executable, f"--conf-path={config}"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and self.process.poll() is None:
                try:
                    self.call("getVersion")
                    return self
                except VodError:
                    time.sleep(0.1)
            raise VodError("无法启动下载管理，请检查 aria2c 程序及本机端口。")
        except BaseException:
            self.close()
            raise

    def call(self, method, *params):
        try:
            response = self.session.post(self.endpoint, json={
                "jsonrpc": "2.0", "id": "shu-vod", "method": "aria2." + method,
                "params": ["token:" + self.secret, *params]}, timeout=2)
            response.raise_for_status()
            data = response.json()
            if "error" in data or "result" not in data:
                raise ValueError
            return data["result"]
        except (requests.RequestException, ValueError):
            # aria2's error text may contain a signed URL; do not surface it.
            raise VodError("下载管理连接中断或操作失败，可重试续传。") from None

    def add(self, url, path):
        path = Path(path)
        Aria2Downloader.input_text([{"url": url, "path": str(path)}])
        path.parent.mkdir(parents=True, exist_ok=True)
        return self.call("addUri", [url], {"dir": str(path.parent), "out": path.name})

    def status(self, gid):
        return self.call("tellStatus", gid, ["status", "totalLength", "completedLength",
                                             "downloadSpeed", "errorCode"])

    def remove(self, gid):
        state = self.status(gid)
        if state["status"] not in {"complete", "error", "removed"}:
            self.call("remove", gid)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = self.status(gid)
                if state["status"] in {"complete", "error", "removed"}:
                    break
                time.sleep(0.1)
            else:
                raise VodError("视频仍在停止中，请稍后重试。")
        self.call("removeDownloadResult", gid)
        return state

    def close(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.call("forcePauseAll")
                self.call("shutdown")
                self.process.wait(timeout=8)
            except (VodError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.session.close()
        if self.temp is not None:
            self.temp.cleanup()

    def __exit__(self, *args):
        self.close()


class DownloadManager:
    """Mutations run in one worker. UI commands cross a queue, snapshots are copies."""

    def __init__(self, state_dir):
        self.file = Path(state_dir) / "download-tasks.json"
        self.tasks = []
        self.commands = Queue()
        self.note = ""
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if data["version"] != 1:
                raise ValueError
            tasks = [DownloadTask(**item) for item in data["tasks"]]
            ids, paths = set(), set()
            for task in tasks:
                if any(not isinstance(value, str) for value in
                       (task.id, task.owner, task.course, task.root, task.path, task.status, task.error)):
                    raise ValueError
                path = Path(task.path)
                if (task.status not in STATES or task.id in ids or os.path.normcase(task.path) in paths
                        or not task.id or not task.owner or not path.is_absolute()
                        or not Path(task.root).is_absolute() or path.parent.parent != Path(task.root)
                        or any(type(n) is not int or n < 0 for n in
                               (task.course_id, task.lesson_id, task.view, task.completed, task.total, task.speed))
                        or not task.course_id or not task.lesson_id):
                    raise ValueError
                ids.add(task.id)
                paths.add(os.path.normcase(task.path))
                if task.status in RUNNING:
                    task.status = "paused"
                task.speed = 0
            self.tasks = tasks
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self.note = "下载记录无法读取，未恢复任务；原有视频不受影响。"

    def snapshot(self):
        return [asdict(task) for task in self.tasks]

    def save(self):
        private_write(self.file, json.dumps({"version": 1, "tasks": self.snapshot()},
                                           ensure_ascii=False, indent=2))

    def add(self, planned, course, root, owner):
        root = Path(root).resolve()
        for item in planned:
            path = Path(item["path"]).resolve()
            if path.parent.parent != root:
                raise VodError("下载路径不在所选目录中。")
            old = next((t for t in self.tasks if os.path.normcase(t.path) == os.path.normcase(str(path))), None)
            if old:
                if old.owner != owner:
                    raise VodError("相同保存路径已有其他账号的任务，请更换目录。")
                if old.status == "complete" and path.is_file():
                    continue
                old.status, old.error = "queued", ""
            else:
                self.tasks.append(DownloadTask(uuid4().hex, owner, course["course_id"], item["lesson_id"],
                                               item["view"], course["name"], str(root), str(path)))
        self.save()

    def request(self, action, ids, *, delete_files=False):
        self.commands.put((action, set(ids), delete_files))

    @staticmethod
    def _delete_files(task):
        root, path = Path(task.root).resolve(), Path(task.path)
        # Only this task's video/control file; never recurse through a directory.
        if path.resolve().parent.parent != root or path.resolve() != path.absolute():
            raise VodError("文件路径已改变，未删除文件。")
        control = Path(str(path) + ".aria2")
        if control.is_symlink() or control.resolve().parent != path.resolve().parent:
            raise VodError("续传文件路径已改变，未删除文件。")
        path.unlink(missing_ok=True)
        control.unlink(missing_ok=True)

    def _commands(self, owner, rpc, active, report):
        while True:
            try:
                action, ids, delete_files = self.commands.get_nowait()
            except Empty:
                return
            for task in list(self.tasks):
                if task.id not in ids or task.owner != owner:
                    continue
                try:
                    if action in {"pause", "delete"}:
                        if task.id in active:
                            state = rpc.remove(active[task.id])
                            self._counts(task, state)
                            del active[task.id]
                            if state["status"] == "complete":
                                validate_video(task.path)
                                task.status, task.speed = "complete", 0
                        if task.status in RUNNING:
                            task.status, task.speed = "paused", 0
                        if action == "delete":
                            if delete_files:
                                self._delete_files(task)
                            self.tasks.remove(task)
                            report("已删除下载任务" + ("及对应文件。" if delete_files else "，本地文件保留。"))
                    elif action == "resume" and task.status in {"paused", "error"}:
                        task.status, task.error = "queued", ""
                except (VodError, OSError) as exc:
                    report(str(exc) if isinstance(exc, VodError) else "删除失败，请检查文件权限或占用情况。")
            self.save()

    @staticmethod
    def _counts(task, state):
        task.completed = int(state.get("completedLength", 0))
        task.total = int(state.get("totalLength", 0))
        task.speed = int(state.get("downloadSpeed", 0))

    def run(self, client, preferences, *, owner, timeout=30, stopped,
            changed=lambda _: None, report=lambda _: None):
        active = {}
        self._commands(owner, None, active, report)
        changed(self.snapshot())
        if not any(t.owner == owner and t.status == "queued" for t in self.tasks):
            return
        try:
            gateway = client.http.gateway_cookie() if client.auth.vpn else ""
            with Aria2RPC(preferences.aria2, connections=preferences.connections, jobs=preferences.jobs,
                          verify=preferences.verify, timeout=timeout, gateway_cookie=gateway) as rpc:
                last_save = time.monotonic()
                while not stopped.is_set():
                    self._commands(owner, rpc, active, report)
                    for task in self.tasks:
                        if task.id not in active:
                            continue
                        state = rpc.status(active[task.id])
                        self._counts(task, state)
                        if state["status"] in {"complete", "error", "removed"}:
                            if state["status"] == "complete":
                                try:
                                    validate_video(task.path)
                                    task.status, task.error = "complete", ""
                                except (VodError, OSError):
                                    task.status, task.error = "error", "视频不完整或返回了网页，请重试。"
                            else:
                                code = str(state.get("errorCode", ""))
                                task.status, task.error = "error", f"下载失败（代码 {code if code.isdigit() else '?'}），可续传重试。"
                            task.speed = 0
                            if task.status == "error":
                                report(f"课次 {task.lesson_id} · 视角 {task.view}：{task.error}")
                            rpc.call("removeDownloadResult", active.pop(task.id))
                            self.save()
                    # Resolve just one URL per tick, so controls and progress keep updating.
                    task = next((t for t in self.tasks if t.owner == owner and t.status == "queued"), None)
                    if task and len(active) < preferences.jobs:
                        task.status = "resolving"
                        changed(self.snapshot())
                        try:
                            views = client.playback(task.course_id, task.lesson_id)
                            video = next((v for v in views if v["view"] == task.view), None)
                            if video is None:
                                raise VodError("该课次已无此视角。")
                            url = probe_media(video["url"], client.auth)
                            self._commands(owner, rpc, active, report)
                            if stopped.is_set():
                                break
                            if task in self.tasks and task.status == "resolving":
                                active[task.id] = rpc.add(url, task.path)
                                task.status, task.error = "active", ""
                        except VodError as exc:
                            task.status, task.error, task.speed = "error", str(exc), 0
                            report(f"课次 {task.lesson_id} · 视角 {task.view}：{task.error}")
                            if isinstance(exc, SessionExpired):
                                raise
                        except OSError:
                            task.status, task.error, task.speed = "error", "无法写入视频，请检查目录权限。", 0
                        self.save()
                    changed(self.snapshot())
                    if not active and not any(t.owner == owner and t.status in RUNNING for t in self.tasks):
                        break
                    if time.monotonic() - last_save >= 2:
                        self.save()
                        last_save = time.monotonic()
                    stopped.wait(0.35)
        finally:
            # aria2 shuts down gracefully before the final persisted state is written.
            if stopped.is_set():
                self._commands(owner, None, {}, report)
            for task in self.tasks:
                if task.owner == owner and task.status in RUNNING:
                    task.status = "paused"
                task.speed = 0
            self.save()
            changed(self.snapshot())
