# 参考来源与许可证

本项目的协议实现和行为适配自随项目提供的两个参考工程，未修改 vendor 内容。

- `vendor/ShuYo`：GNU GPL v3。参考 `lib/data/services/vod_api_client.dart`、
  `vod_native_session_service.dart`、`vod_auth_service.dart`、`lib/core/vod_url_resolver.dart`、
  `lib/data/models/course_recording.dart` 和 `vod_search_result.dart`。
  用于学期、课程、关键词/课程号搜索、课次、视频视角、身份核验和会话兑换。
- `vendor/shu-sso-poc`：GNU AGPL v3。参考 `src/client.py`、`src/entry_password.py`、
  `src/runner.py`、`src/rsa_key.py`、`src/utils.py` 以及 `systems/webvpn.shu.edu.cn/`。
  用于 RSA 密码加密、登录参数、两步验证码、WebVPN 握手和设备标识。
  本项目移植所需协议到独立包，保持 vendor 原状，不运行 POC 的批量业务登录或证据落盘功能。

两个参考工程保留各自 LICENSE。本项目新增与改编代码按 GNU AGPL v3 发布，完整文本见根目录 LICENSE。
aria2 为用户单独安装的外部程序，不随 Python 包分发；其参数依据
[aria2 官方手册](https://aria2.github.io/manual/en/html/aria2c.html)。
