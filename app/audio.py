"""Audio input validation, efficient MP3 concatenation and FFmpeg fallback."""
import asyncio
import json
from pathlib import Path
import subprocess


class AudioError(ValueError):
    pass


def probe_audio(path: str) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels:format=duration,format_name",
             "-of", "json", path],
            capture_output=True, text=True, timeout=20, check=True,
        )
        payload = json.loads(result.stdout)
        stream = (payload.get("streams") or [None])[0]
        if not stream or not stream.get("codec_name"):
            raise AudioError("No playable audio stream found")
        duration = float(payload.get("format", {}).get("duration", 0))
        if duration <= 0:
            raise AudioError("Audio duration could not be determined")
        return {**stream, "duration": duration,
                "format_name": payload.get("format", {}).get("format_name", "")}
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError,
            ValueError, TypeError, IndexError) as exc:
        if isinstance(exc, AudioError):
            raise
        raise AudioError("Unsupported or damaged audio file") from exc


async def _run_ffmpeg(args: list[str], timeout: int = 720) -> None:
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise AudioError("Audio processing exceeded its time limit") from exc
    if process.returncode != 0:
        raise AudioError("FFmpeg could not join these audio files: " + stderr.decode(errors="replace")[-350:])


async def append_outro(original_path: str, outro_path: str, output_path: str) -> str:
    """Fast stream-copy for compatible MP3s, transcode otherwise."""
    source = await asyncio.to_thread(probe_audio, original_path)
    outro = await asyncio.to_thread(probe_audio, outro_path)
    compatible_mp3 = (
        source["codec_name"] == "mp3" and outro["codec_name"] == "mp3"
        and source.get("sample_rate") == outro.get("sample_rate")
        and source.get("channels") == outro.get("channels")
        and "mp3" in source.get("format_name", "")
        and "mp3" in outro.get("format_name", "")
    )
    if compatible_mp3:
        list_path = str(Path(output_path).with_suffix(".ffconcat"))
        # Only filenames under our temporary directory are passed to FFmpeg.
        with open(list_path, "w", encoding="utf-8") as file:
            for path in (original_path, outro_path):
                file.write("file '" + path.replace("'", "'\\''") + "'\n")
        try:
            await _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path,
                               "-map", "0:a:0", "-c:a", "copy",
                               "-id3v2_version", "3", output_path])
            info = await asyncio.to_thread(probe_audio, output_path)
            if info["duration"] + 1 >= source["duration"] + outro["duration"]:
                return "copy"
        except AudioError:
            pass
    # If input codecs differ, decode, join, and encode an MP3 with known output.
    await _run_ffmpeg([
        "-i", original_path, "-i", outro_path,
        "-filter_complex", "[0:a:0][1:a:0]concat=n=2:v=0:a=1[a]",
        "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "96k", "-ac", "2",
        "-id3v2_version", "3", output_path,
    ])
    await asyncio.to_thread(probe_audio, output_path)
    return "transcode"
