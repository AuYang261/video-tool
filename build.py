#!/usr/bin/env python3
"""Build the FFmpeg TUI for the current operating system.

Run this script separately on Windows, macOS, and Linux. PyInstaller is
not a cross-compiler, so each run creates a native artifact for that host.
FFmpeg is intentionally not bundled; the packaged program finds it via PATH.
"""

import importlib.util
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


APP_NAME = "FFmpeg 视频处理工具"
EXECUTABLE_NAME = "video-tool"
BUNDLE_ID = "local.video-tool.ffmpeg.tui"
PROJECT_DIR = Path(__file__).resolve().parent
SOURCE_FILE = PROJECT_DIR / "video_tool.py"
BUILD_ROOT = PROJECT_DIR / "build"
RELEASE_ROOT = PROJECT_DIR / "release"


def platform_tag() -> Tuple[str, str]:
    system = platform.system().casefold()
    system_names = {
        "windows": "windows",
        "darwin": "macos",
        "linux": "linux",
    }
    if system not in system_names:
        raise RuntimeError(f"暂不支持当前系统：{platform.system()}")
    machine = platform.machine().casefold()
    architecture_names = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    return system_names[system], architecture_names.get(machine, machine or "unknown")


def require_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is not None:
        return
    command = f'"{sys.executable}" -m pip install --upgrade pyinstaller'
    raise RuntimeError("未安装 PyInstaller，请先运行：\n" + command)


def pyinstaller_command(
    native_dist: Path,
    work_dir: Path,
    spec_dir: Path,
    ffmpeg_executable: Optional[str] = None,
) -> List[str]:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        EXECUTABLE_NAME,
        "--distpath",
        str(native_dist),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
    ]
    if ffmpeg_executable is not None:
        command.extend(
            ["--add-binary", f"{ffmpeg_executable}{os.pathsep}."]
        )
    command.append(str(SOURCE_FILE))
    return command


def run_command(command: List[str]) -> None:
    subprocess.run(command, cwd=PROJECT_DIR, check=True)


def create_macos_app(native_binary: Path, release_dir: Path) -> Path:
    app_path = release_dir / f"{APP_NAME}.app"
    if app_path.exists():
        shutil.rmtree(app_path)

    apple_script_lines = [
        "on run",
        "set appPath to POSIX path of (path to me)",
        'set toolPath to appPath & "Contents/Resources/video-tool"',
        'tell application "Terminal"',
        "activate",
        'do script ("clear; " & quoted form of toolPath)',
        "end tell",
        "end run",
    ]
    command = ["osacompile", "-o", str(app_path)]
    for line in apple_script_lines:
        command.extend(["-e", line])
    run_command(command)

    resources = app_path / "Contents" / "Resources"
    packaged_binary = resources / EXECUTABLE_NAME
    shutil.copy2(native_binary, packaged_binary)
    packaged_binary.chmod(packaged_binary.stat().st_mode | 0o111)

    plist_path = app_path / "Contents" / "Info.plist"
    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    info["CFBundleIdentifier"] = BUNDLE_ID
    info["NSAppleEventsUsageDescription"] = "启动终端界面以处理本机视频文件。"
    with plist_path.open("wb") as handle:
        plistlib.dump(info, handle)

    run_command(["codesign", "--force", "--deep", "--sign", "-", str(app_path)])
    run_command(["codesign", "--verify", "--deep", "--strict", str(app_path)])
    return app_path


def build() -> Path:
    if not SOURCE_FILE.is_file():
        raise RuntimeError(f"找不到源码：{SOURCE_FILE}")
    require_pyinstaller()
    system, architecture = platform_tag()
    tag = f"{system}-{architecture}"
    work_dir = BUILD_ROOT / tag / "work"
    spec_dir = BUILD_ROOT / tag / "spec"
    native_dist = BUILD_ROOT / tag / "native"
    release_dir = RELEASE_ROOT / tag

    for directory in (work_dir, spec_dir, native_dist):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
    release_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_executable = shutil.which("ffmpeg")
    if ffmpeg_executable is not None:
        print(f"检测到 FFmpeg，将自动内置：{ffmpeg_executable}")
    else:
        print("未检测到 FFmpeg；产物将在运行时从 PATH 查找。")
    print(f"正在构建 {system}/{architecture}…")
    run_command(
        pyinstaller_command(
            native_dist,
            work_dir,
            spec_dir,
            ffmpeg_executable=ffmpeg_executable,
        )
    )

    native_name = EXECUTABLE_NAME + (".exe" if system == "windows" else "")
    native_binary = native_dist / native_name
    if not native_binary.is_file() or native_binary.stat().st_size == 0:
        raise RuntimeError("PyInstaller 未生成有效的可执行文件")

    if system == "macos":
        artifact = create_macos_app(native_binary, release_dir)
    else:
        artifact = release_dir / (APP_NAME + (".exe" if system == "windows" else ""))
        shutil.copy2(native_binary, artifact)
        if system == "linux":
            artifact.chmod(artifact.stat().st_mode | 0o111)

    print(f"构建完成：{artifact}")
    return artifact


def main() -> int:
    try:
        build()
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
