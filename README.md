# shu-vod-downloader

用校园账号在终端里查找、选择和下载上海大学课程录播。直接启动可进入交互界面（TUI），连续浏览课程和管理下载。也可以使用命令行（CLI）按教学班、课次和视角下载，或输出 JSON 供脚本处理。

目前支持单账号登录，可选校园网直连或 WebVPN。下载由本机的 `aria2c` 执行，支持多连接、并发下载和断点续传，适用于平台提供的直链视频。暂不支持 HLS 播放列表。

## 安装与启动

准备 **Python 3.10 或更高版本**，并安装 [aria2](https://github.com/aria2/aria2/releases)。在终端运行 `aria2c --version` 可检查下载程序是否可用。下载或克隆本仓库后，在包含 `pyproject.toml` 的目录打开终端。

**Windows · PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\start-tui.cmd
```

安装完成后，双击 [start-tui.cmd](start-tui.cmd) 即可再次打开界面。

**macOS / Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/shu-vod
```

## 下载

1. 点击 **登录**，输入学号或工号和密码。校内选择「校园网直连」，校外选择「WebVPN」。如需两步验证，在弹窗中填写企业微信或短信收到的验证码。
2. 选择学期，点击 **浏览课程**，或按课程名称、教师姓名搜索。按课程号搜索时，将搜索类型切换为「课程号」。
3. 选中教学班并按 Enter 查看课次。按空格或点击可切换勾选，`[✓]` 表示已选。默认下载全部视角，也可点击 **查看视角** 后填入需要的编号。
4. 点击 **预览路径** 核对文件，再点击 **下载所选**。如需整个教学班的录播，点击 **整课下载**。
5. 在 **下载管理** 查看每个视频的进度和速度。暂停后可以续传，重启后使用原账号仍可继续。「删除任务」默认保留视频，勾选「同时删除」才会一并删除文件。

默认保存位置是启动目录下的 `downloads/`。可在 **下载设置** 中修改保存目录、`aria2c` 路径、连接数和并发数。每个教学班单独建目录，名称依次为课程名称、课程号、教师和教学班 ID。视频名包含上课日期与时间、周次、节次、课次 ID 和视角编号。

```text
downloads/
└── 高等数学-0800-张三-456/
    └── 2026-11-02_0800-第1周-第2节-课次789-视角7.mp4
```

界面建议至少使用 80 列 × 28 行。Tab 切换控件，Esc 返回课程或关闭弹窗，Ctrl+C 停止当前任务，Ctrl+Q 退出。

## 命令行

以下示例假设已激活虚拟环境。也可将 `shu-vod` 替换为 Windows 的 `.\.venv\Scripts\shu-vod.exe` 或 macOS / Linux 的 `.venv/bin/shu-vod`。示例中的 ID 请替换为查询结果中的实际值。

```shell
# 校内登录，按提示输入账号和密码
shu-vod login

# 校外登录时改用下面这条命令
shu-vod login --transport webvpn --method sms

# 查询学期，查找教学班，再列出课次
shu-vod terms
shu-vod courses --term 123
shu-vod search "高等数学" --term 123
shu-vod search "0800" --by code --term 123
shu-vod lessons --course 456

# 先预览课次 789 的视角编号和保存路径，再下载所需视角
shu-vod download --course 456 --lesson 789 --dry-run
shu-vod download --course 456 --lesson 789 --view 7

# 下载整门课程的所有视角
shu-vod download --course 456
```

- `--course` 使用教学班 ID（`course_id`），`--lesson` 使用课次 ID（`lesson_id`），可重复指定。课程号用于检索教学班。
- 课程名和教师关键词搜索最多匹配 50 个已结束的课次。要查看某教学班的完整目录，使用 `lessons --course` 加教学班 ID。
- 脚本可以使用 `shu-vod search "高等数学" --json` 读取 JSON 结果。`download` 在批量下载有失败项或全部课次都无可用录播时返回退出码 `5`。
- 使用 `shu-vod status --check` 验证登录状态，使用 `shu-vod logout` 删除本机登录会话。更多参数见 `shu-vod --help` 和 `shu-vod download --help`。

## 本地数据与问题反馈

Windows 默认将状态保存在 `%LOCALAPPDATA%\shu-vod-downloader`。macOS / Linux 使用 `$XDG_STATE_HOME/shu-vod-downloader`，未设置该变量时使用 `~/.local/state/shu-vod-downloader`。可通过环境变量 `SHU_VOD_STATE_DIR` 或子命令的 `--state-dir` 参数指定其他目录。

| 文件 | 内容 |
| --- | --- |
| `accounts/default/session.json` | 账号、连接方式、JWT 和 Cookie。登录凭据以明文存放，密码和验证码只在登录时使用。 |
| `download-tasks.json` | 下载任务、账号标识、课程信息和本机文件路径。 |
| `tui-settings.json` | 下载目录、aria2 路径和界面中的下载设置。 |

请将状态目录留在本机。提交 Issue 时，可提供操作系统、Python 和 aria2 版本、复现步骤及脱敏后的错误提示。分享截图前请遮住学号和本机用户名，并移除会话文件、Cookie 或带签名的播放地址。

## 开发与测试

在虚拟环境中运行以下命令可安装可编辑版本并执行测试。测试使用模拟的校园接口和本地 HTTP 服务。系统中有 `aria2c` 时，还会验证实际下载、续传和文件完整性。

```shell
python -m pip install -e .
python -m unittest discover -s tests -v
```

## 鸣谢与许可证

课程浏览和搜索参考了 [shuosc/ShuYo](https://github.com/shuosc/ShuYo) , [Lilin-1024/ShuYo](https://github.com/Lilin-1024/ShuYo)。统一身份认证及 WebVPN 登录参考了 [preca-hoshino/shu-sso-poc](https://github.com/preca-hoshino/shu-sso-poc)。

本项目代码采用 GNU Affero General Public License v3.0。完整条款见 [LICENSE](LICENSE)，参考项目和改编范围见 [第三方说明](THIRD_PARTY.md)。

## 使用声明

本项目供学习交流。请仅下载自己有权观看的录播，并在 24 小时内自行清理本地软件副本及其产生的数据。
