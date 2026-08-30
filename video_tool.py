#!/usr/bin/env python3
"""A dependency-free terminal interface for local FFmpeg video processing."""

import os
import re
import json
import shutil
import subprocess
import sys
import unicodedata
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional, Tuple


VIDEO_SUFFIXES = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
    ".ts",
    ".mts",
    ".mpeg",
    ".mpg",
}


@dataclass(frozen=True)
class JobSettings:
    input_path: Path
    output_dir: Path
    extract_audio: bool
    convert_video: bool
    video_bitrate_kbps: Optional[int]
    start: Optional[float]
    end: Optional[float]
    keep_audio: bool = True
    compression_mode: str = "crf"
    crf: int = 18
    frame_rate_fps: float = 15.0
    tune_stillimage: bool = True


@dataclass(frozen=True)
class BrowserEntry:
    kind: str
    label: str
    path: Path


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: Optional[float]
    video_bitrate_kbps: Optional[int]
    frame_rate_fps: Optional[float]
    size_bytes: int


@dataclass(frozen=True)
class TuiState:
    input_path: Optional[Path] = None
    last_video_dir: Optional[Path] = None
    output_dir: Optional[Path] = None
    extract_audio: bool = True
    convert_video: bool = False
    keep_audio: bool = True
    compression_mode: str = "crf"
    bitrate_kbps: int = 300
    crf: int = 18
    frame_rate_fps: float = 15.0
    tune_stillimage: bool = True
    start_time: Optional[Tuple[int, int, int]] = None
    end_time: Optional[Tuple[int, int, int]] = None


def application_directory() -> Path:
    """Return the directory beside the script, executable, or macOS .app."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        for parent in executable.parents:
            if parent.suffix.casefold() == ".app":
                return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent


def settings_file_path() -> Path:
    return application_directory() / "video_tool_settings.json"


def _valid_saved_time(value: object) -> Optional[Tuple[int, int, int]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("保存的时间格式无效")
    if any(isinstance(part, bool) or not isinstance(part, int) for part in value):
        raise ValueError("保存的时间格式无效")
    hours, minutes, seconds = value
    if not (0 <= hours <= 99 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        raise ValueError("保存的时间超出范围")
    return hours, minutes, seconds


def load_tui_state(path: Optional[Path] = None) -> TuiState:
    config_path = path or settings_file_path()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return TuiState()
        defaults = TuiState()
        last_video_dir = Path(raw["last_video_dir"]) if isinstance(
            raw.get("last_video_dir"), str
        ) else None
        output_dir = Path(raw["output_dir"]) if isinstance(
            raw.get("output_dir"), str
        ) else None
        if last_video_dir is not None and not last_video_dir.is_dir():
            last_video_dir = None
        if output_dir is not None and not output_dir.is_dir():
            output_dir = None
        compression_mode = raw.get("compression_mode")
        if compression_mode not in {"bitrate", "crf"}:
            compression_mode = defaults.compression_mode
        bitrate = raw.get("bitrate_kbps")
        if isinstance(bitrate, bool) or not isinstance(bitrate, int):
            bitrate = defaults.bitrate_kbps
        bitrate = bitrate if 1 <= bitrate <= 1_000_000 else defaults.bitrate_kbps
        crf = raw.get("crf")
        if isinstance(crf, bool) or not isinstance(crf, int):
            crf = defaults.crf
        crf = crf if 0 <= crf <= 51 else defaults.crf
        frame_rate = raw.get("frame_rate_fps")
        if isinstance(frame_rate, bool) or not isinstance(frame_rate, (int, float)):
            frame_rate = defaults.frame_rate_fps
        frame_rate = (
            float(frame_rate)
            if 1 <= float(frame_rate) <= 240
            else defaults.frame_rate_fps
        )

        def saved_bool(name: str, default: bool) -> bool:
            value = raw.get(name)
            return value if isinstance(value, bool) else default

        return TuiState(
            last_video_dir=last_video_dir,
            output_dir=output_dir,
            extract_audio=saved_bool("extract_audio", defaults.extract_audio),
            convert_video=saved_bool("convert_video", defaults.convert_video),
            keep_audio=saved_bool("keep_audio", defaults.keep_audio),
            compression_mode=compression_mode,
            bitrate_kbps=bitrate,
            crf=crf,
            frame_rate_fps=frame_rate,
            tune_stillimage=saved_bool(
                "tune_stillimage", defaults.tune_stillimage
            ),
            start_time=_valid_saved_time(raw.get("start_time")),
            end_time=_valid_saved_time(raw.get("end_time")),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return TuiState()


def save_tui_state(state: TuiState, path: Optional[Path] = None) -> None:
    config_path = path or settings_file_path()
    payload = {
        "last_video_dir": str(state.last_video_dir) if state.last_video_dir else None,
        "output_dir": str(state.output_dir) if state.output_dir else None,
        "extract_audio": state.extract_audio,
        "convert_video": state.convert_video,
        "keep_audio": state.keep_audio,
        "compression_mode": state.compression_mode,
        "bitrate_kbps": state.bitrate_kbps,
        "crf": state.crf,
        "frame_rate_fps": state.frame_rate_fps,
        "tune_stillimage": state.tune_stillimage,
        "start_time": list(state.start_time) if state.start_time else None,
        "end_time": list(state.end_time) if state.end_time else None,
    }
    temporary_path = config_path.with_name(config_path.name + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, config_path)


def find_ffmpeg_executable() -> Optional[str]:
    """Locate bundled FFmpeg first, then fall back to the system PATH."""
    executable_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    if getattr(sys, "frozen", False):
        bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        bundled_ffmpeg = bundle_dir / executable_name
        if bundled_ffmpeg.is_file():
            return str(bundled_ffmpeg)
    return shutil.which(executable_name)


def parse_time(value: str, field_name: str) -> Optional[float]:
    """Parse seconds, MM:SS, or HH:MM:SS into seconds."""
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or any(not part.strip() for part in parts):
        raise ValueError(f"{field_name}格式无效")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"{field_name}格式无效") from exc
    if any(number < 0 for number in numbers):
        raise ValueError(f"{field_name}不能为负数")
    if len(numbers) >= 2 and numbers[-1] >= 60:
        raise ValueError(f"{field_name}的秒数必须小于 60")
    if len(numbers) == 3 and numbers[-2] >= 60:
        raise ValueError(f"{field_name}的分钟数必须小于 60")
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def validate_time_range(start: Optional[float], end: Optional[float]) -> None:
    if start is not None and end is not None and end <= start:
        raise ValueError("结束时间必须晚于开始时间")


def parse_bitrate(value: str) -> int:
    try:
        bitrate = int(value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("视频码率必须是整数") from exc
    if not 1 <= bitrate <= 1_000_000:
        raise ValueError("视频码率必须在 1 到 1000000 kbps 之间")
    return bitrate


def validate_job(settings: JobSettings) -> None:
    if not settings.input_path.is_file():
        raise ValueError("请选择存在的视频文件")
    if not settings.extract_audio and not settings.convert_video:
        raise ValueError("请至少选择一个处理功能")
    if settings.output_dir.exists() and not settings.output_dir.is_dir():
        raise ValueError("输出路径不是目录")
    if settings.convert_video:
        if settings.compression_mode not in {"bitrate", "crf"}:
            raise ValueError("压缩方式必须是目标码率或 CRF")
        if settings.compression_mode == "bitrate":
            bitrate = settings.video_bitrate_kbps
            if bitrate is None or not 1 <= bitrate <= 1_000_000:
                raise ValueError("视频码率必须在 1 到 1000000 kbps 之间")
        if not 0 <= settings.crf <= 51:
            raise ValueError("CRF 必须在 0 到 51 之间")
        if not 1 <= settings.frame_rate_fps <= 240:
            raise ValueError("帧率必须在 1 到 240 fps 之间")
    validate_time_range(settings.start, settings.end)


def create_job_settings(
    input_path_text: str,
    output_dir_text: str,
    extract_audio: bool,
    convert_video: bool,
    bitrate_text: str,
    start_text: str,
    end_text: str,
    keep_audio: bool = True,
    compression_mode: str = "crf",
    crf: int = 18,
    frame_rate_fps: float = 15.0,
    tune_stillimage: bool = True,
) -> JobSettings:
    if not input_path_text.strip():
        raise ValueError("请选择视频文件")
    if not output_dir_text.strip():
        raise ValueError("请选择输出目录")
    settings = JobSettings(
        input_path=Path(input_path_text.strip()).expanduser(),
        output_dir=Path(output_dir_text.strip()).expanduser(),
        extract_audio=extract_audio,
        convert_video=convert_video,
        video_bitrate_kbps=(
            parse_bitrate(bitrate_text)
            if convert_video and compression_mode == "bitrate"
            else None
        ),
        start=parse_time(start_text, "开始时间"),
        end=parse_time(end_text, "结束时间"),
        keep_audio=keep_audio,
        compression_mode=compression_mode,
        crf=crf,
        frame_rate_fps=frame_rate_fps,
        tune_stillimage=tune_stillimage,
    )
    validate_job(settings)
    return settings


def unique_output_path(output_dir: Path, base_name: str, suffix: str) -> Path:
    candidate = output_dir / f"{base_name}{suffix}"
    sequence = 1
    while candidate.exists():
        candidate = output_dir / f"{base_name}_{sequence}{suffix}"
        sequence += 1
    return candidate


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _input_and_range_arguments(
    input_path: Path,
    start: Optional[float],
    end: Optional[float],
    ffmpeg_executable: str = "ffmpeg",
) -> List[str]:
    validate_time_range(start, end)
    arguments = [ffmpeg_executable, "-hide_banner", "-nostdin", "-y"]
    if start is not None:
        arguments.extend(["-ss", _format_seconds(start)])
    arguments.extend(["-i", str(input_path)])
    if end is not None:
        arguments.extend(["-t", _format_seconds(end - (start or 0.0))])
    return arguments


def build_audio_command(
    input_path: Path,
    output_path: Path,
    start: Optional[float],
    end: Optional[float],
    ffmpeg_executable: str = "ffmpeg",
) -> List[str]:
    return _input_and_range_arguments(
        input_path, start, end, ffmpeg_executable
    ) + [
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(output_path),
    ]


def build_video_command(
    input_path: Path,
    output_path: Path,
    bitrate_kbps: int,
    start: Optional[float],
    end: Optional[float],
    ffmpeg_executable: str = "ffmpeg",
    keep_audio: bool = True,
    compression_mode: str = "crf",
    crf: int = 18,
    frame_rate_fps: float = 15.0,
    tune_stillimage: bool = True,
) -> List[str]:
    arguments = _input_and_range_arguments(
        input_path, start, end, ffmpeg_executable
    ) + [
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
    ]
    if compression_mode == "crf":
        arguments.extend(["-crf", str(crf)])
    else:
        arguments.extend(["-b:v", f"{bitrate_kbps}k"])
    arguments.extend(["-r", _format_seconds(frame_rate_fps)])
    if tune_stillimage:
        arguments.extend(["-tune", "stillimage"])
    if keep_audio:
        arguments.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", "128k"])
    else:
        arguments.append("-an")
    arguments.extend(["-movflags", "+faststart", str(output_path)])
    return arguments


def run_ffmpeg(
    command: List[str],
    progress_callback: Optional[Callable[[Optional[float], float, str], None]] = None,
    expected_duration: Optional[float] = None,
) -> None:
    """Run an internally generated FFmpeg command without a shell."""
    output_path = Path(command[-1])
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if progress_callback is not None:
        _run_ffmpeg_streaming(
            command,
            output_path,
            progress_callback,
            expected_duration,
            creation_flags,
        )
        return
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=creation_flags,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 FFmpeg，请重新安装应用") from exc
    if completed.returncode == 0:
        return
    try:
        output_path.unlink(missing_ok=True)
    except TypeError:
        if output_path.exists():
            output_path.unlink()
    error_lines = (completed.stderr or "未知错误").strip().splitlines()
    raise RuntimeError("FFmpeg 处理失败：\n" + "\n".join(error_lines[-20:]))


def _run_ffmpeg_streaming(
    command: List[str],
    output_path: Path,
    progress_callback: Callable[[Optional[float], float, str], None],
    expected_duration: Optional[float],
    creation_flags: int,
) -> None:
    progress_command = command[:-1] + [
        "-stats_period",
        "0.25",
        "-progress",
        "pipe:1",
        "-nostats",
        command[-1],
    ]
    recent_output = deque(maxlen=6)
    error_output = deque(maxlen=20)
    processed_seconds = 0.0
    try:
        process = subprocess.Popen(
            progress_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 FFmpeg，请重新安装应用") from exc

    if process.stdout is not None:
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            error_output.append(line)
            if line.startswith("out_time="):
                try:
                    processed_seconds = parse_time(
                        line.split("=", 1)[1], "FFmpeg 时间"
                    ) or 0.0
                except ValueError:
                    processed_seconds = 0.0
                percent = None
                if expected_duration and expected_duration > 0:
                    percent = min(100.0, processed_seconds / expected_duration * 100)
                progress_callback(percent, processed_seconds, "\n".join(recent_output))
            elif not re.match(
                r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|"
                r"out_time_ms|dup_frames|drop_frames|speed|progress)=",
                line,
            ):
                recent_output.append(line)
    return_code = process.wait()
    if return_code == 0:
        progress_callback(100.0, processed_seconds, "\n".join(recent_output))
        return
    try:
        output_path.unlink(missing_ok=True)
    except TypeError:
        if output_path.exists():
            output_path.unlink()
    summary = "\n".join(line for line in error_output if line).strip() or "未知错误"
    raise RuntimeError("FFmpeg 处理失败：\n" + summary)


def process_job(
    settings: JobSettings,
    runner: Callable[[List[str]], None] = run_ffmpeg,
    progress_callback: Callable[[str], None] = lambda _message: None,
    ffmpeg_executable: Optional[str] = None,
) -> List[Path]:
    validate_job(settings)
    executable = ffmpeg_executable or find_ffmpeg_executable()
    if executable is None:
        raise RuntimeError("未找到 FFmpeg，请重新安装应用")
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    outputs: List[Path] = []
    stem = settings.input_path.stem
    if settings.extract_audio:
        audio_path = unique_output_path(settings.output_dir, f"{stem}_audio", ".mp3")
        progress_callback("正在提取 MP3 音频…")
        runner(
            build_audio_command(
                settings.input_path,
                audio_path,
                settings.start,
                settings.end,
                executable,
            )
        )
        outputs.append(audio_path)
    if settings.convert_video:
        bitrate = settings.video_bitrate_kbps
        if settings.compression_mode == "bitrate" and bitrate is None:
            raise ValueError("请输入视频码率")
        output_label = (
            f"{bitrate}k"
            if settings.compression_mode == "bitrate"
            else f"crf{settings.crf}"
        )
        video_path = unique_output_path(
            settings.output_dir, f"{stem}_{output_label}", ".mp4"
        )
        if settings.compression_mode == "bitrate":
            progress_callback(f"正在按目标码率转换：{bitrate} kbps…")
        else:
            progress_callback(f"正在按 CRF 压缩：CRF {settings.crf}…")
        runner(
            build_video_command(
                settings.input_path,
                video_path,
                bitrate or 300,
                settings.start,
                settings.end,
                executable,
                keep_audio=settings.keep_audio,
                compression_mode=settings.compression_mode,
                crf=settings.crf,
                frame_rate_fps=settings.frame_rate_fps,
                tune_stillimage=settings.tune_stillimage,
            )
        )
        outputs.append(video_path)
    return outputs


def list_browser_entries(current: Path, select_directory: bool) -> List[BrowserEntry]:
    """Return deterministic entries for the keyboard directory browser."""
    resolved = current.expanduser().resolve()
    entries: List[BrowserEntry] = []
    if select_directory:
        entries.append(BrowserEntry("select_current", "选择当前目录", resolved))
    if resolved.parent != resolved:
        entries.append(BrowserEntry("parent", "..", resolved.parent))
    children = list(resolved.iterdir())
    directories = sorted(
        (path for path in children if path.is_dir()),
        key=lambda path: path.name.casefold(),
    )
    files = sorted(
        (
            path
            for path in children
            if not select_directory
            and path.is_file()
            and path.suffix.casefold() in VIDEO_SUFFIXES
        ),
        key=lambda path: path.name.casefold(),
    )
    entries.extend(BrowserEntry("directory", path.name, path) for path in directories)
    entries.extend(BrowserEntry("file", path.name, path) for path in files)
    return entries


def adjust_bitrate(current: int, direction: int) -> int:
    return min(1_000_000, max(100, current + direction * 100))


def adjust_time_component(
    value: Tuple[int, int, int], component: int, direction: int
) -> Tuple[int, int, int]:
    parts = list(value)
    if component == 0:
        parts[0] = min(99, max(0, parts[0] + direction))
    elif component in (1, 2):
        parts[component] = (parts[component] + direction) % 60
    else:
        raise ValueError("时间分量必须是 0、1 或 2")
    return parts[0], parts[1], parts[2]


def format_time_tuple(value: Tuple[int, int, int]) -> str:
    return f"{value[0]:02d}:{value[1]:02d}:{value[2]:02d}"


def time_tuple_to_seconds(value: Optional[Tuple[int, int, int]]) -> Optional[float]:
    if value is None:
        return None
    return float(value[0] * 3600 + value[1] * 60 + value[2])


def probe_media_info(
    input_path: Path, ffmpeg_executable: Optional[str] = None
) -> MediaInfo:
    """Read duration and bitrate from FFmpeg's input summary."""
    size_bytes = input_path.stat().st_size
    executable = ffmpeg_executable or find_ffmpeg_executable()
    if executable is None:
        return MediaInfo(None, None, None, size_bytes)
    completed = subprocess.run(
        [executable, "-hide_banner", "-i", str(input_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    summary = completed.stderr or ""
    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", summary
    )
    duration = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    overall_match = re.search(r"bitrate:\s*(\d+)\s*kb/s", summary)
    overall_bitrate = int(overall_match.group(1)) if overall_match else None
    video_bitrate = None
    audio_bitrate = None
    frame_rate = None
    for line in summary.splitlines():
        if "Video:" in line and frame_rate is None:
            frame_rate_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if frame_rate_match:
                frame_rate = float(frame_rate_match.group(1))
        bitrate_match = re.search(r"(\d+)\s*kb/s", line)
        if not bitrate_match:
            continue
        if "Video:" in line and video_bitrate is None:
            video_bitrate = int(bitrate_match.group(1))
        elif "Audio:" in line and audio_bitrate is None:
            audio_bitrate = int(bitrate_match.group(1))
    if video_bitrate is None and overall_bitrate is not None:
        video_bitrate = max(1, overall_bitrate - (audio_bitrate or 0))
    return MediaInfo(duration, video_bitrate, frame_rate, size_bytes)


def format_file_size(size_bytes: Optional[int]) -> str:
    if size_bytes is None:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "未知"


def effective_output_duration(
    media_duration: Optional[float],
    start: Optional[Tuple[int, int, int]],
    end: Optional[Tuple[int, int, int]],
) -> Optional[float]:
    start_seconds = time_tuple_to_seconds(start) or 0.0
    end_seconds = time_tuple_to_seconds(end)
    if end_seconds is not None:
        return max(0.0, end_seconds - start_seconds)
    if media_duration is not None:
        return max(0.0, media_duration - start_seconds)
    return None


def estimate_video_size(
    duration_seconds: Optional[float], bitrate_kbps: int, keep_audio: bool
) -> Optional[int]:
    if duration_seconds is None or duration_seconds <= 0:
        return None
    total_bitrate = bitrate_kbps + (128 if keep_audio else 0)
    return int(total_bitrate * 1000 / 8 * duration_seconds * 1.01)


def _cell_width(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(_cell_width(character) for character in text)


def truncate_display(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    result = ""
    used = 0
    for character in text:
        size = _cell_width(character)
        if used + size > width - 1:
            break
        result += character
        used += size
    return result + "…"


class TerminalController:
    """Small cross-platform raw-key and ANSI terminal adapter."""

    def __init__(self) -> None:
        self._saved_attributes = None
        self._fd: Optional[int] = None

    def __enter__(self) -> "TerminalController":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("需要在终端窗口中运行此程序")
        if os.name == "nt":
            os.system("")
        else:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved_attributes = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        self.write("\x1b[?1049h\x1b[?25l\x1b]0;FFmpeg 视频处理工具\x07")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.write("\x1b[?25h\x1b[?1049l")
        if self._saved_attributes is not None and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attributes)

    @staticmethod
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def draw(self, lines: List[str]) -> None:
        self.write("\x1b[H\x1b[2J" + "\r\n".join(lines))

    def read_key(self) -> str:
        if os.name == "nt":
            return self._read_windows_key()
        return self._read_posix_key()

    @staticmethod
    def _read_windows_key() -> str:
        import msvcrt

        character = msvcrt.getwch()
        if character in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "other")
        if character in ("\r", "\n"):
            return "enter"
        if character == "\x08":
            return "backspace"
        if character == "\x1b":
            return "esc"
        if character == "\x03":
            return "quit"
        if character.isdigit():
            return "digit:" + character
        if character == ".":
            return "dot"
        return "other"

    def _read_posix_key(self) -> str:
        import select

        if self._fd is None:
            return "other"
        first = os.read(self._fd, 1)
        if first in (b"\r", b"\n"):
            return "enter"
        if first in (b"\x08", b"\x7f"):
            return "backspace"
        if first == b"\x03":
            return "quit"
        if first.isdigit():
            return "digit:" + first.decode("ascii")
        if first == b".":
            return "dot"
        if first != b"\x1b":
            return "other"
        sequence = b""
        while select.select([self._fd], [], [], 0.03)[0]:
            sequence += os.read(self._fd, 1)
            if len(sequence) >= 2:
                break
        if sequence == b"[A":
            return "up"
        if sequence == b"[B":
            return "down"
        if not sequence:
            return "esc"
        return "other"


class VideoToolTUI:
    BLUE = "\x1b[38;5;39m"
    MUTED = "\x1b[38;5;245m"
    REVERSE = "\x1b[7m"
    BOLD = "\x1b[1m"
    RESET = "\x1b[0m"

    def __init__(
        self, terminal: TerminalController, config_path: Optional[Path] = None
    ) -> None:
        self.terminal = terminal
        self._config_path = config_path
        self.state = load_tui_state(config_path)
        self.selection = 0
        self._processing_message = "正在准备 FFmpeg…"

    def run(self) -> None:
        try:
            while True:
                items = self._main_items()
                self.selection %= len(items)
                self._draw_main(items)
                key = self.terminal.read_key()
                if key == "up":
                    self.selection = (self.selection - 1) % len(items)
                elif key == "down":
                    self.selection = (self.selection + 1) % len(items)
                elif key in ("esc", "quit"):
                    return
                elif key == "enter" and not self._activate_main_item(
                    items[self.selection][0]
                ):
                    return
        finally:
            try:
                save_tui_state(self.state, self._config_path)
            except OSError:
                pass

    def _main_items(self) -> List[Tuple[str, str]]:
        input_text = str(self.state.input_path) if self.state.input_path else "未选择"
        output_text = str(self.state.output_dir) if self.state.output_dir else "未选择"
        start_text = (
            format_time_tuple(self.state.start_time)
            if self.state.start_time is not None
            else "不限制"
        )
        end_text = (
            format_time_tuple(self.state.end_time)
            if self.state.end_time is not None
            else "不限制"
        )
        items = [
            ("input", f"视频文件      {input_text}"),
            ("output", f"输出目录      {output_text}"),
            (
                "extract_audio",
                f"[{'✓' if self.state.extract_audio else ' '}] 提取 MP3 音频（192 kbps）",
            ),
            (
                "convert_video",
                f"[{'✓' if self.state.convert_video else ' '}] 压缩/转换视频（MP4 / H.264）",
            ),
        ]
        if self.state.convert_video:
            items.append(
                (
                    "compression_mode",
                    "压缩方式      "
                    + (
                        "目标码率"
                        if self.state.compression_mode == "bitrate"
                        else "CRF"
                    ),
                )
            )
            if self.state.compression_mode == "bitrate":
                items.append(
                    ("bitrate", f"目标码率      {self.state.bitrate_kbps} kbps")
                )
            else:
                items.append(("crf", f"CRF           {self.state.crf}"))
            items.extend(
                [
                    (
                        "frame_rate",
                        "目标帧率      "
                        f"{_format_seconds(self.state.frame_rate_fps)} fps",
                    ),
                    (
                        "keep_audio",
                        f"[{'✓' if self.state.keep_audio else ' '}] 转码后保留音频",
                    ),
                    (
                        "tune_stillimage",
                        f"[{'✓' if self.state.tune_stillimage else ' '}] "
                        "静态画面优化（-tune stillimage）",
                    ),
                ]
            )
        items.extend(
            [
                ("start_time", f"开始时间      {start_text}"),
                ("end_time", f"结束时间      {end_text}"),
                ("process", "开始处理"),
                ("exit", "退出"),
            ]
        )
        return items

    def _draw_main(self, items: List[Tuple[str, str]]) -> None:
        columns, _rows = shutil.get_terminal_size((100, 30))
        width = max(48, min(columns - 4, 108))
        lines = [
            f"{self.BOLD}{self.BLUE}FFmpeg 视频处理工具{self.RESET}",
            f"{self.MUTED}本机处理 · 不上传 · ↑/↓ 选择 · Enter 确认 · Esc 退出{self.RESET}",
            "",
        ]
        for index, (_action, item) in enumerate(items):
            visible = "  " + truncate_display(item, width - 4)
            if index == self.selection:
                visible = f"{self.REVERSE}› {truncate_display(item, width - 4):<{width - 2}}{self.RESET}"
            lines.append(visible)
        lines.extend(
            [
                "",
                f"{self.MUTED}提示：选择路径时 Enter 进入目录，选择 .. 返回上级。{self.RESET}",
            ]
        )
        if self.state.convert_video:
            lines.append(
                f"{self.MUTED}stillimage：优化图片、幻灯片和静态录屏的细节；高运动视频可关闭。{self.RESET}"
            )
        self.terminal.draw(lines)

    def _activate_main_item(self, action: str) -> bool:
        if action == "input":
            start = (
                self.state.input_path.parent
                if self.state.input_path
                else self.state.last_video_dir or Path.cwd()
            )
            selected = self._browse(start, select_directory=False)
            if selected is not None:
                output = self.state.output_dir or selected.parent
                self.terminal.draw(
                    [
                        f"{self.BOLD}{self.BLUE}读取视频信息{self.RESET}",
                        "",
                        f"  {selected.name}",
                    ]
                )
                try:
                    media_info = probe_media_info(selected)
                    frame_rate = media_info.frame_rate_fps or 15.0
                except OSError:
                    frame_rate = 15.0
                self.state = replace(
                    self.state,
                    input_path=selected,
                    last_video_dir=selected.parent,
                    output_dir=output,
                    frame_rate_fps=frame_rate,
                )
        elif action == "output":
            start = self.state.output_dir
            if start is None and self.state.input_path is not None:
                start = self.state.input_path.parent
            selected = self._browse(start or Path.cwd(), select_directory=True)
            if selected is not None:
                self.state = replace(self.state, output_dir=selected)
        elif action == "extract_audio":
            self.state = replace(
                self.state, extract_audio=not self.state.extract_audio
            )
        elif action == "convert_video":
            self.state = replace(
                self.state, convert_video=not self.state.convert_video
            )
        elif action == "compression_mode":
            mode = "crf" if self.state.compression_mode == "bitrate" else "bitrate"
            self.state = replace(self.state, compression_mode=mode)
        elif action == "bitrate":
            value = self._edit_bitrate(self.state.bitrate_kbps)
            self.state = replace(self.state, bitrate_kbps=value)
        elif action == "crf":
            value = self._edit_crf(self.state.crf)
            self.state = replace(self.state, crf=value)
        elif action == "frame_rate":
            value = self._edit_frame_rate(self.state.frame_rate_fps)
            self.state = replace(self.state, frame_rate_fps=value)
        elif action == "keep_audio":
            self.state = replace(self.state, keep_audio=not self.state.keep_audio)
        elif action == "tune_stillimage":
            self.state = replace(
                self.state, tune_stillimage=not self.state.tune_stillimage
            )
        elif action == "start_time":
            value = self._edit_time("开始时间", self.state.start_time)
            self.state = replace(self.state, start_time=value)
        elif action == "end_time":
            value = self._edit_time("结束时间", self.state.end_time)
            self.state = replace(self.state, end_time=value)
        elif action == "process":
            self._process_current_job()
        elif action == "exit":
            return False
        return True

    def _browse(self, start: Path, select_directory: bool) -> Optional[Path]:
        current = start.expanduser()
        if not current.is_dir():
            current = Path.home()
        selected_index = 0
        error_message = ""
        while True:
            try:
                entries = list_browser_entries(current, select_directory)
                error_message = ""
            except (OSError, PermissionError) as exc:
                entries = []
                error_message = f"无法读取目录：{exc}"
            selected_index = min(selected_index, max(0, len(entries) - 1))
            self._draw_browser(
                current,
                entries,
                selected_index,
                select_directory,
                error_message,
            )
            key = self.terminal.read_key()
            if key in ("esc", "quit"):
                return None
            if not entries:
                if key == "enter" and current.parent != current:
                    current = current.parent
                continue
            if key == "up":
                selected_index = (selected_index - 1) % len(entries)
            elif key == "down":
                selected_index = (selected_index + 1) % len(entries)
            elif key == "enter":
                entry = entries[selected_index]
                if entry.kind in {"parent", "directory"}:
                    current = entry.path
                    selected_index = 0
                elif entry.kind in {"file", "select_current"}:
                    return entry.path

    def _draw_browser(
        self,
        current: Path,
        entries: List[BrowserEntry],
        selected_index: int,
        select_directory: bool,
        error_message: str,
    ) -> None:
        columns, rows = shutil.get_terminal_size((100, 30))
        width = max(48, min(columns - 4, 120))
        visible_count = max(5, rows - 9)
        start = max(0, selected_index - visible_count // 2)
        start = min(start, max(0, len(entries) - visible_count))
        visible_entries = entries[start : start + visible_count]
        title = "选择输出目录" if select_directory else "选择视频文件"
        lines = [
            f"{self.BOLD}{self.BLUE}{title}{self.RESET}",
            f"{self.MUTED}当前目录：{truncate_display(str(current), width - 10)}{self.RESET}",
            f"{self.MUTED}↑/↓ 选择 · Enter 进入/确认 · Esc 取消{self.RESET}",
            "",
        ]
        if error_message:
            lines.append(error_message)
        if not entries:
            lines.append("  （无可选内容，按 Enter 返回上级）")
        for offset, entry in enumerate(visible_entries):
            absolute_index = start + offset
            prefix = {
                "select_current": "✓ ",
                "parent": "↰ ",
                "directory": "├─▸ ",
                "file": "└── ",
            }.get(entry.kind, "  ")
            text = truncate_display(prefix + entry.label, width - 4)
            if absolute_index == selected_index:
                lines.append(f"{self.REVERSE}› {text:<{width - 2}}{self.RESET}")
            else:
                lines.append("  " + text)
        if start > 0:
            lines.append(f"{self.MUTED}  ↑ 还有 {start} 项{self.RESET}")
        remaining = len(entries) - (start + len(visible_entries))
        if remaining > 0:
            lines.append(f"{self.MUTED}  ↓ 还有 {remaining} 项{self.RESET}")
        self.terminal.draw(lines)

    def _edit_bitrate(self, original: int) -> int:
        media_info = None
        if self.state.input_path is not None:
            self.terminal.draw(
                [
                    f"{self.BOLD}{self.BLUE}设置视频码率{self.RESET}",
                    "",
                    "  正在读取原视频信息…",
                ]
            )
            try:
                media_info = probe_media_info(self.state.input_path)
            except OSError:
                media_info = None
        buffer = str(original)
        typing_started = False
        error_message = ""
        while True:
            try:
                candidate = int(buffer) if buffer else 0
            except ValueError:
                candidate = 0
            original_bitrate = (
                f"约 {media_info.video_bitrate_kbps} kbps"
                if media_info and media_info.video_bitrate_kbps is not None
                else "未知"
            )
            original_size = (
                format_file_size(media_info.size_bytes) if media_info else "未知"
            )
            original_fps = (
                f"{_format_seconds(media_info.frame_rate_fps)} fps"
                if media_info and media_info.frame_rate_fps is not None
                else "未知"
            )
            estimated_duration = effective_output_duration(
                media_info.duration_seconds if media_info else None,
                self.state.start_time,
                self.state.end_time,
            )
            estimated_size = estimate_video_size(
                estimated_duration,
                candidate if candidate > 0 else original,
                self.state.keep_audio,
            )
            estimated_size_text = format_file_size(estimated_size)
            self.terminal.draw(
                [
                    f"{self.BOLD}{self.BLUE}设置视频码率{self.RESET}",
                    "",
                    f"  原视频码率          {original_bitrate}",
                    f"  原文件大小          {original_size}",
                    f"  原视频帧率          {original_fps}",
                    "",
                    f"  目标视频码率        {self.REVERSE}  {(buffer or ' '):>7} kbps  {self.RESET}",
                    f"  预计输出大小        {estimated_size_text}",
                    "",
                    (
                        f"\x1b[31m  {error_message}{self.RESET}"
                        if error_message
                        else ""
                    ),
                    f"{self.MUTED}直接输入数字 · Backspace 删除 · ↑/↓ 调整 100 kbps{self.RESET}",
                    f"{self.MUTED}Enter 保存 · Esc 取消 · 有效范围 1–1000000 kbps{self.RESET}",
                ]
            )
            key = self.terminal.read_key()
            if key == "up":
                value = adjust_bitrate(candidate if candidate > 0 else original, 1)
                buffer = str(value)
                typing_started = False
                error_message = ""
            elif key == "down":
                value = adjust_bitrate(candidate if candidate > 0 else original, -1)
                buffer = str(value)
                typing_started = False
                error_message = ""
            elif key.startswith("digit:"):
                digit = key.split(":", 1)[1]
                if not typing_started:
                    buffer = digit
                    typing_started = True
                elif len(buffer) < 7:
                    buffer += digit
                error_message = ""
            elif key == "backspace":
                if not typing_started:
                    typing_started = True
                buffer = buffer[:-1]
                error_message = ""
            elif key == "enter":
                if buffer and 1 <= candidate <= 1_000_000:
                    return candidate
                error_message = "码率必须在 1 到 1000000 kbps 之间"
            elif key in ("esc", "quit"):
                return original

    def _edit_crf(self, original: int) -> int:
        media_info = self._read_selected_media_info("设置 CRF")
        buffer = str(original)
        typing_started = False
        error_message = ""
        while True:
            try:
                candidate = int(buffer) if buffer else -1
            except ValueError:
                candidate = -1
            details = self._media_detail_lines(media_info)
            self.terminal.draw(
                [
                    f"{self.BOLD}{self.BLUE}设置 CRF{self.RESET}",
                    "",
                    *details,
                    "",
                    f"  CRF                  {self.REVERSE}  {(buffer or ' '):>3}  {self.RESET}",
                    "",
                    f"{self.MUTED}CRF 越低，画质越高、文件通常越大；常用范围 18–28。{self.RESET}",
                    (
                        f"\x1b[31m  {error_message}{self.RESET}"
                        if error_message
                        else ""
                    ),
                    f"{self.MUTED}直接输入数字 · Backspace 删除 · ↑/↓ 调整 1{self.RESET}",
                    f"{self.MUTED}Enter 保存 · Esc 取消 · 有效范围 0–51（默认 18）{self.RESET}",
                ]
            )
            key = self.terminal.read_key()
            if key in {"up", "down"}:
                base = candidate if 0 <= candidate <= 51 else original
                value = min(51, max(0, base + (1 if key == "up" else -1)))
                buffer = str(value)
                typing_started = False
                error_message = ""
            elif key.startswith("digit:"):
                digit = key.split(":", 1)[1]
                if not typing_started:
                    buffer = digit
                    typing_started = True
                elif len(buffer) < 2:
                    buffer += digit
                error_message = ""
            elif key == "backspace":
                if not typing_started:
                    typing_started = True
                buffer = buffer[:-1]
                error_message = ""
            elif key == "enter":
                if 0 <= candidate <= 51:
                    return candidate
                error_message = "CRF 必须在 0 到 51 之间"
            elif key in {"esc", "quit"}:
                return original

    def _edit_frame_rate(self, original: float) -> float:
        media_info = self._read_selected_media_info("设置目标帧率")
        buffer = _format_seconds(original)
        typing_started = False
        error_message = ""
        while True:
            try:
                candidate = float(buffer) if buffer else 0.0
            except ValueError:
                candidate = 0.0
            original_fps = (
                f"{_format_seconds(media_info.frame_rate_fps)} fps"
                if media_info and media_info.frame_rate_fps is not None
                else "未知（选择视频时采用 15 fps）"
            )
            self.terminal.draw(
                [
                    f"{self.BOLD}{self.BLUE}设置目标帧率{self.RESET}",
                    "",
                    f"  原视频帧率          {original_fps}",
                    f"  目标帧率            {self.REVERSE}  {(buffer or ' '):>7} fps  {self.RESET}",
                    "",
                    f"{self.MUTED}选择视频后默认使用原帧率；无法识别时默认 15 fps。{self.RESET}",
                    (
                        f"\x1b[31m  {error_message}{self.RESET}"
                        if error_message
                        else ""
                    ),
                    f"{self.MUTED}直接输入数字/小数点 · Backspace 删除 · ↑/↓ 调整 1 fps{self.RESET}",
                    f"{self.MUTED}Enter 保存 · Esc 取消 · 有效范围 1–240 fps{self.RESET}",
                ]
            )
            key = self.terminal.read_key()
            if key in {"up", "down"}:
                base = candidate if 1 <= candidate <= 240 else original
                value = min(240.0, max(1.0, base + (1 if key == "up" else -1)))
                buffer = _format_seconds(value)
                typing_started = False
                error_message = ""
            elif key.startswith("digit:"):
                digit = key.split(":", 1)[1]
                if not typing_started:
                    buffer = digit
                    typing_started = True
                elif len(buffer) < 8:
                    buffer += digit
                error_message = ""
            elif key == "dot":
                if not typing_started:
                    buffer = "0."
                    typing_started = True
                elif "." not in buffer and len(buffer) < 8:
                    buffer += "."
                error_message = ""
            elif key == "backspace":
                if not typing_started:
                    typing_started = True
                buffer = buffer[:-1]
                error_message = ""
            elif key == "enter":
                if 1 <= candidate <= 240:
                    return candidate
                error_message = "帧率必须在 1 到 240 fps 之间"
            elif key in {"esc", "quit"}:
                return original

    def _read_selected_media_info(self, title: str) -> Optional[MediaInfo]:
        if self.state.input_path is None:
            return None
        self.terminal.draw(
            [
                f"{self.BOLD}{self.BLUE}{title}{self.RESET}",
                "",
                "  正在读取原视频信息…",
            ]
        )
        try:
            return probe_media_info(self.state.input_path)
        except OSError:
            return None

    @staticmethod
    def _media_detail_lines(media_info: Optional[MediaInfo]) -> List[str]:
        bitrate = (
            f"约 {media_info.video_bitrate_kbps} kbps"
            if media_info and media_info.video_bitrate_kbps is not None
            else "未知"
        )
        size = format_file_size(media_info.size_bytes) if media_info else "未知"
        fps = (
            f"{_format_seconds(media_info.frame_rate_fps)} fps"
            if media_info and media_info.frame_rate_fps is not None
            else "未知"
        )
        return [
            f"  原视频码率          {bitrate}",
            f"  原文件大小          {size}",
            f"  原视频帧率          {fps}",
        ]

    def _edit_time(
        self, title: str, original: Optional[Tuple[int, int, int]]
    ) -> Optional[Tuple[int, int, int]]:
        mode = 0 if original is None else 1
        while True:
            options = ["不限制", "设置具体时间"]
            lines = [f"{self.BOLD}{self.BLUE}{title}{self.RESET}", ""]
            for index, option in enumerate(options):
                line = f"  {option}"
                if index == mode:
                    line = f"{self.REVERSE}› {option:<24}{self.RESET}"
                lines.append(line)
            lines.extend(
                ["", f"{self.MUTED}↑/↓ 选择 · Enter 确认 · Esc 取消{self.RESET}"]
            )
            self.terminal.draw(lines)
            key = self.terminal.read_key()
            if key in ("up", "down"):
                mode = 1 - mode
            elif key == "enter":
                if mode == 0:
                    return None
                break
            elif key in ("esc", "quit"):
                return original

        value = original or (0, 0, 0)
        names = ("小时", "分钟", "秒")
        for component in range(3):
            while True:
                rendered = []
                for index, number in enumerate(value):
                    part = f"{number:02d}"
                    if index == component:
                        part = f"{self.REVERSE} {part} {self.RESET}"
                    rendered.append(part)
                self.terminal.draw(
                    [
                        f"{self.BOLD}{self.BLUE}{title} · 设置{names[component]}{self.RESET}",
                        "",
                        "               " + " : ".join(rendered),
                        "",
                        f"{self.MUTED}↑/↓ 调整 · Enter 下一项/保存 · Esc 取消{self.RESET}",
                    ]
                )
                key = self.terminal.read_key()
                if key == "up":
                    value = adjust_time_component(value, component, 1)
                elif key == "down":
                    value = adjust_time_component(value, component, -1)
                elif key == "enter":
                    break
                elif key in ("esc", "quit"):
                    return original
        return value

    def _process_current_job(self) -> None:
        try:
            if self.state.input_path is None:
                raise ValueError("请先选择视频文件")
            if self.state.output_dir is None:
                raise ValueError("请先选择输出目录")
            settings = JobSettings(
                input_path=self.state.input_path,
                output_dir=self.state.output_dir,
                extract_audio=self.state.extract_audio,
                convert_video=self.state.convert_video,
                video_bitrate_kbps=(
                    self.state.bitrate_kbps if self.state.convert_video else None
                ),
                start=time_tuple_to_seconds(self.state.start_time),
                end=time_tuple_to_seconds(self.state.end_time),
                keep_audio=self.state.keep_audio,
                compression_mode=self.state.compression_mode,
                crf=self.state.crf,
                frame_rate_fps=self.state.frame_rate_fps,
                tune_stillimage=self.state.tune_stillimage,
            )
            executable = find_ffmpeg_executable()
            if executable is None:
                raise RuntimeError("未找到 FFmpeg，请重新安装应用")
            media_info = probe_media_info(self.state.input_path, executable)
            expected_duration = effective_output_duration(
                media_info.duration_seconds,
                self.state.start_time,
                self.state.end_time,
            )

            def stage_callback(message: str) -> None:
                self._processing_message = message
                self._draw_processing(message)

            def streaming_runner(command: List[str]) -> None:
                run_ffmpeg(
                    command,
                    progress_callback=self._draw_ffmpeg_progress,
                    expected_duration=expected_duration,
                )

            outputs = process_job(
                settings,
                runner=streaming_runner,
                progress_callback=stage_callback,
                ffmpeg_executable=executable,
            )
            lines = [f"{self.BOLD}{self.BLUE}处理完成{self.RESET}", ""]
            lines.extend(f"  ✓ {output}" for output in outputs)
            lines.extend(["", f"{self.MUTED}按 Enter 或 Esc 返回主界面{self.RESET}"])
            self._wait_on_screen(lines)
        except Exception as exc:
            self._wait_on_screen(
                [
                    f"{self.BOLD}\x1b[31m处理失败{self.RESET}",
                    "",
                    *str(exc).splitlines(),
                    "",
                    f"{self.MUTED}按 Enter 或 Esc 返回主界面{self.RESET}",
                ]
            )

    def _draw_processing(self, message: str) -> None:
        self.terminal.draw(
            [
                f"{self.BOLD}{self.BLUE}正在处理{self.RESET}",
                "",
                f"  {message}",
                "",
                f"{self.MUTED}大文件可能需要一些时间，请勿关闭终端。{self.RESET}",
            ]
        )

    def _draw_ffmpeg_progress(
        self, percent: Optional[float], processed_seconds: float, output: str
    ) -> None:
        columns, _rows = shutil.get_terminal_size((100, 30))
        width = max(30, min(54, columns - 18))
        if percent is None:
            bar = "░" * width
            percent_text = "  --.-%"
        else:
            filled = min(width, max(0, int(width * percent / 100)))
            bar = "█" * filled + "░" * (width - filled)
            percent_text = f"{percent:6.1f}%"
        hours = int(processed_seconds // 3600)
        minutes = int((processed_seconds % 3600) // 60)
        seconds = processed_seconds % 60
        elapsed = f"{hours:02d}:{minutes:02d}:{seconds:05.2f}"
        lines = [
            f"{self.BOLD}{self.BLUE}正在处理{self.RESET}",
            "",
            f"  {self._processing_message}",
            f"  [{bar}] {percent_text}",
            f"  已处理时间：{elapsed}",
            "",
            f"{self.MUTED}FFmpeg 最近输出：{self.RESET}",
        ]
        recent_lines = output.splitlines()[-6:] if output else ["等待输出…"]
        output_width = max(30, columns - 6)
        lines.extend("  " + truncate_display(line, output_width) for line in recent_lines)
        self.terminal.draw(lines)

    def _wait_on_screen(self, lines: List[str]) -> None:
        self.terminal.draw(lines)
        while self.terminal.read_key() not in {"enter", "esc", "quit"}:
            pass


def main() -> None:
    try:
        with TerminalController() as terminal:
            VideoToolTUI(terminal).run()
    except RuntimeError as exc:
        print(exc)
        if sys.stdin.isatty():
            input("按 Enter 退出…")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
