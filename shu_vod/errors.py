class VodError(Exception):
    """Safe, user-facing error; never include raw server bodies or signed URLs."""


class AuthError(VodError):
    pass


class SessionExpired(AuthError):
    pass


class PermissionDenied(VodError):
    pass


class NoPlayback(VodError):
    pass


class DownloadCancelled(VodError):
    pass

