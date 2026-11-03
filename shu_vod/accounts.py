"""Account boundary: only 'default' is implemented; no account switching yet."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .errors import AuthError


@dataclass(frozen=True)
class AccountRef:
    id: str = "default"


@dataclass
class SessionState:
    username: str
    transport: str
    jwt: str
    cookies: list[dict]


class AccountStore(Protocol):
    """Future multi-account stores can implement this without changing the API client."""
    def load(self, account: AccountRef) -> SessionState: ...
    def save(self, account: AccountRef, state: SessionState) -> None: ...
    def delete(self, account: AccountRef) -> None: ...


def default_state_dir() -> Path:
    if os.getenv("SHU_VOD_STATE_DIR"):
        return Path(os.environ["SHU_VOD_STATE_DIR"]).expanduser()
    base = os.getenv("LOCALAPPDATA") if os.name == "nt" else os.getenv("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "shu-vod-downloader"


def private_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class SingleAccountStore:
    def __init__(self, root: Path):
        self.root = root

    def path(self, account: AccountRef) -> Path:
        if account.id != "default":
            raise AuthError("多账号接口已预留，当前仅支持 default 单账号。")
        return self.root / "accounts" / account.id / "session.json"

    def load(self, account: AccountRef) -> SessionState:
        try:
            data = json.loads(self.path(account).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("account_id") != account.id:
                raise ValueError
            state = SessionState(**data["session"])
            if (not isinstance(state.username, str) or not state.username.strip()
                    or state.transport not in {"direct", "webvpn"}
                    or not isinstance(state.jwt, str) or not state.jwt
                    or any(ord(c) < 32 for c in state.jwt)
                    or not isinstance(state.cookies, list)
                    or any(not isinstance(c, dict) for c in state.cookies)):
                raise ValueError
            return state
        except FileNotFoundError:
            raise AuthError("尚未登录，请先运行 shu-vod login。") from None
        except (ValueError, TypeError, KeyError):
            raise AuthError("会话文件无效，请重新运行 shu-vod login。") from None

    def save(self, account: AccountRef, state: SessionState) -> None:
        private_write(self.path(account), json.dumps({
            "version": 1, "account_id": account.id, "session": asdict(state),
        }, ensure_ascii=False, indent=2))

    def delete(self, account: AccountRef) -> None:
        self.path(account).unlink(missing_ok=True)

