# FFmpeg 视频处理工具

一个无第三方运行时依赖的 Python TUI，用于直接处理本机视频文件。界面仅使用方向键、Enter 和 Esc，不上传文件。

## 功能

- 从视频提取 MP3 音频，可选择使用自定义压缩参数
- 使用目标码率或 CRF 压缩为 MP4/H.264
- 默认使用 CRF 18
- 调整输出帧率
- 选择是否保留音频
- 可独立控制音频压缩、码率、采样率和声道数
- 支持 `-tune stillimage`，适合图片、幻灯片和静态录屏
- 设置开始和结束时间
- 显示原视频码率、大小、帧率以及 FFmpeg 实时进度，并高亮预计大小、当前码率和预计剩余时间
- 保存上次配置和视频所在目录
- 键盘目录浏览器，不需要输入文件路径

## 直接运行源码

需要 Python 3.9 或更高版本，并确保 `ffmpeg` 位于系统 `PATH`：

```bash
python video_tool.py
```

Windows 也可以使用：

```powershell
py video_tool.py
```

检查 FFmpeg：

```bash
ffmpeg -version
```

## 构建原生程序

PyInstaller 不能跨系统编译。需要在 Windows、macOS、Linux 上分别运行同一份构建脚本：

```bash
python -m pip install --upgrade pyinstaller
python build.py
```

产物位于：

```text
release/<系统>-<架构>/
```

- Windows：生成 `.exe`
- macOS：生成可双击的 `.app`，通过 Terminal 启动 TUI
- Linux：生成可执行文件

构建时如果能从 `PATH` 找到 FFmpeg，脚本会自动将其和必要动态库加入产物；如果找不到，仍会完成构建，程序运行时再从目标机器的 `PATH` 查找。

## 操作方式

- `↑` / `↓`：移动或调整数值
- `Enter`：进入目录、确认或执行
- `Esc`：返回或退出
- 数字键与小数点：输入码率、CRF、帧率和时间各分量
- `Backspace`：删除输入

## 配置文件

程序正常退出时，会在源码、可执行文件或 `.app` 的同级目录生成：

```text
video_tool_settings.json
```

配置文件不会保存具体视频文件，只保存上次视频所在目录、输出目录和处理参数。

## 说明

- CRF 数值越小，画质越高、文件通常越大；常用范围为 18–28。
- 需要准确控制文件大小时，建议使用目标码率模式。
- `tune stillimage` 更适合静态画面较多的视频，高运动视频可以关闭。
- 音频压缩默认使用 32 kbps、16000 Hz、单声道；提取音频时编码为 MP3，保留在 MP4 中时编码为 AAC。
- 关闭音频压缩后，提取音频时仍转码为 MP3、保留在 MP4 中时仍转码为 AAC，目标码率沿用源音频码率（无法识别时使用 128 kbps，MP3 最高 320 kbps）。程序不会主动指定采样率和声道数；输出编码支持时 FFmpeg 会保持源参数，否则会自动转换为兼容参数。
