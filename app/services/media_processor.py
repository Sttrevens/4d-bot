"""音视频处理工具

使用 ffmpeg 处理语音和视频：
- 语音：AMR/SILK → WAV（可送 ASR 转写）
- 视频：提取首帧为 JPEG（可送视觉模型分析）
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


async def convert_voice_to_wav(voice_bytes: bytes) -> bytes:
    """将 AMR/SILK 语音转为 WAV 格式

    企微客服语音为 AMR(实际 SILK) 格式。ffmpeg 可以直接处理。

    Returns:
        WAV 字节，失败返回 b""
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.amr"
        output_path = Path(tmpdir) / "output.wav"

        input_path.write_bytes(voice_bytes)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(input_path),
            "-ar", "16000", "-ac", "1",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.error("ffmpeg voice conversion failed: %s", stderr.decode()[-500:])
            return b""

        if not output_path.exists():
            return b""

        wav_bytes = output_path.read_bytes()
        logger.info("voice converted: %dKB AMR → %dKB WAV",
                     len(voice_bytes) // 1024, len(wav_bytes) // 1024)
        return wav_bytes


async def convert_audio_to_ogg(audio_bytes: bytes) -> bytes:
    """将 AMR/任意音频转为 OGG Opus 格式（Gemini inline_data 原生支持）

    Gemini inline_data 支持: wav, mp3, ogg, flac, aac
    但 AMR 不在支持列表中，需要先转换。

    Returns:
        OGG 字节，失败返回 b""
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.amr"
        output_path = Path(tmpdir) / "output.ogg"

        input_path.write_bytes(audio_bytes)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:a", "libopus", "-b:a", "32k", "-ar", "16000", "-ac", "1",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.error("ffmpeg AMR→OGG conversion failed: %s", stderr.decode()[-500:])
            return b""

        if not output_path.exists():
            return b""

        ogg_bytes = output_path.read_bytes()
        logger.info("audio converted: %dKB AMR → %dKB OGG",
                     len(audio_bytes) // 1024, len(ogg_bytes) // 1024)
        return ogg_bytes


async def compress_audio_for_inline(audio_bytes: bytes, threshold_kb: int = 1024) -> bytes:
    """压缩大音频用于 Gemini inline_data 传输

    语音消息（m4a/mp4 容器）原始码率常 >200kbps，4-8MB 很常见。
    Gemini 只需听懂内容，64kbps mono OGG Opus 足够，语音信息零损失。

    Args:
        audio_bytes: 原始音频字节
        threshold_kb: 超过此大小才压缩（默认 1MB）

    Returns:
        压缩后的 OGG 字节，失败返回原始字节（不阻塞流程）
    """
    if len(audio_bytes) <= threshold_kb * 1024:
        return audio_bytes

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.m4a"
        output_path = Path(tmpdir) / "compressed.ogg"

        input_path.write_bytes(audio_bytes)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:a", "libopus", "-b:a", "64k",
            "-ar", "24000", "-ac", "1",
            "-vn",  # 去掉封面图等视频轨
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.error("ffmpeg audio compression failed: %s", stderr.decode()[-500:])
            return audio_bytes  # 失败返回原始，不阻塞

        if not output_path.exists():
            return audio_bytes

        compressed = output_path.read_bytes()
        logger.info("audio compressed: %dKB → %dKB (%.0f%% reduction)",
                     len(audio_bytes) // 1024, len(compressed) // 1024,
                     (1 - len(compressed) / len(audio_bytes)) * 100)
        return compressed


async def compress_video_for_inline(video_bytes: bytes, target_mb: float = 4.0) -> bytes:
    """压缩视频用于 Gemini inline_data 传输

    大视频通过反代 inline 传输时容易超时或返回空结果。
    用 ffmpeg 压缩到目标大小以下。

    Returns:
        压缩后的 MP4 字节，失败返回 b""（调用方应回退到抽帧）
    """
    target_bytes = int(target_mb * 1024 * 1024)
    if len(video_bytes) <= target_bytes:
        return video_bytes  # 已经够小，不需要压缩

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.mp4"
        output_path = Path(tmpdir) / "compressed.mp4"

        input_path.write_bytes(video_bytes)

        # 两步压缩：降低分辨率 + 降低码率
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", "scale='min(720,iw)':-2",  # 最大宽度 720p
            "-c:v", "libx264", "-preset", "fast", "-crf", "32",
            "-c:a", "aac", "-b:a", "32k",
            "-movflags", "+faststart",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            logger.error("ffmpeg video compression failed: %s", stderr.decode()[-500:])
            return b""

        if not output_path.exists():
            return b""

        compressed = output_path.read_bytes()
        logger.info("video compressed: %dKB → %dKB (%.0f%% reduction)",
                     len(video_bytes) // 1024, len(compressed) // 1024,
                     (1 - len(compressed) / len(video_bytes)) * 100)
        return compressed


async def extract_video_frame(video_bytes: bytes) -> bytes:
    """从视频中提取第一个关键帧为 JPEG

    Returns:
        JPEG 字节，失败返回 b""
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.mp4"
        output_path = Path(tmpdir) / "frame.jpg"

        input_path.write_bytes(video_bytes)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(input_path),
            "-vframes", "1", "-q:v", "2",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.error("ffmpeg frame extraction failed: %s", stderr.decode()[-500:])
            return b""

        if not output_path.exists():
            return b""

        frame_bytes = output_path.read_bytes()
        logger.info("video frame extracted: %dKB video → %dKB JPEG",
                     len(video_bytes) // 1024, len(frame_bytes) // 1024)
        return frame_bytes


def frame_to_data_url(frame_bytes: bytes) -> str:
    """将 JPEG 帧转为 base64 data URL"""
    b64 = base64.b64encode(frame_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def detect_media_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    """通过文件头魔数检测媒体 MIME 类型"""
    if len(data) < 12:
        return fallback
    # 视频格式
    if data[4:8] in (b"ftyp", b"moov", b"mdat"):
        return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video/avi"
    # 音频格式
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:6] == b"#!AMR\n" or data[:9] == b"#!AMR-WB\n":
        return "audio/amr"
    if data[:4] == b"fLaC":
        return "audio/flac"
    # 图片格式
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def media_to_data_url(media_bytes: bytes, mime_type: str = "") -> str:
    """将任意媒体文件转为 base64 data URL（用于 Gemini 原生多模态）

    如果 mime_type 为空，自动从文件头检测。
    """
    if not mime_type:
        mime_type = detect_media_mime(media_bytes, "application/octet-stream")
    b64 = base64.b64encode(media_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


async def transcribe_audio(
    wav_bytes: bytes,
    api_key: str,
    base_url: str,
    model: str = "whisper-1",
) -> str | None:
    """尝试用 OpenAI-compatible Whisper API 转写音频

    Args:
        wav_bytes: WAV 格式音频
        api_key: STT API key
        base_url: STT base URL (会尝试调用 /audio/transcriptions)
        model: Whisper 模型名称，默认 whisper-1

    Returns:
        转写文本，不支持或失败返回 None
    """
    import httpx

    if not api_key:
        logger.info("STT api_key not configured, skipping transcription")
        return None

    url = f"{base_url.rstrip('/')}/audio/transcriptions"

    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"model": model},
            )
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("text", "")
            if text:
                logger.info("audio transcribed: %d chars", len(text))
                return text
        else:
            logger.warning("whisper API error (status=%d): %s",
                           resp.status_code, resp.text[:200])
            return None
    except Exception:
        logger.debug("whisper API call failed, transcription not available")
        return None
