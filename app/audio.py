"""Audio input validation, efficient MP3 concatenation and FFmpeg fallback."""
import asyncio
import json
import logging
from pathlib import Path
import subprocess
from mutagen import MutagenError
from mutagen.id3 import (CHAP, CTOC, CTOCFlags, ID3,
                        ID3NoHeaderError, TIT2, TLEN, USLT)
from mutagen.mp4 import MP4

logger = logging.getLogger("dynamic_outro.audio")


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


def _preserve_episode_metadata(original_path: str, output_path: str,
                               source_format: str, original_seconds: float,
                               finished_seconds: float) -> None:
    """Retain embedded MP3 metadata and timecoded chapters after audio joining.

    FFmpeg's concat demuxer copies audio packets but loses MP3 ID3 CHAP/CTOC,
    USLT and SYLT frames. We restore the publisher's ID3 tag after joining.
    For M4A sources FFmpeg maps container chapters into MP3 ID3 frames and we
    additionally keep the standard MP4 lyrics/transcript text when present.

    The appended material begins after the original, so the source's existing
    chapter and transcript timestamps remain correct. No source is modified.
    """
    try:
        if "mp3" in source_format:
            try:
                original_tags = ID3(original_path)
            except ID3NoHeaderError:
                original_tags = None
            if original_tags and len(original_tags):
                # Save all published frames, including artwork, chapters,
                # unsynchronised/synchronised text (USLT/SYLT), and comments.
                original_tags.save(output_path, v2_version=3)
        elif "mp4" in source_format or "mov" in source_format:
            original = MP4(original_path)
            lyrics = list((original.tags or {}).get("\xa9lyr", []) or [])
            if lyrics:
                output = ID3(output_path)
                for i, value in enumerate(lyrics[:4]):
                    if isinstance(value, str) and value.strip():
                        output.add(USLT(encoding=3, lang="und", desc=f"Original text {i+1}", text=value))
                output.save(output_path, v2_version=3)

        output = ID3(output_path)
        chapters = output.getall("CHAP")
        start = max(0, round(original_seconds * 1000))
        end = max(start + 1, round(finished_seconds * 1000))
        # If the publisher included an MP3 duration tag, update it as well.
        # Without this, players may report the length before our outro.
        had_length_tag = bool(output.getall("TLEN"))
        if not chapters:
            if had_length_tag:
                output.delall("TLEN")
                output.add(TLEN(encoding=3, text=[str(end)]))
                output.save(output_path, v2_version=3)
            return  # Avoid a lone outro chapter masking external RSS chapters.
        # Open-ended source chapters should stop where the outro begins.
        for chapter in chapters:
            if chapter.start_time < start and chapter.end_time > start:
                chapter.end_time = start
        known_ids = {frame.element_id for frame in (output.getall("CHAP") + output.getall("CTOC"))}
        outro_id = "dynamic-outro"
        i = 1
        while outro_id in known_ids:
            outro_id = f"dynamic-outro-{i}"
            i += 1
        output.add(CHAP(element_id=outro_id, start_time=start, end_time=end,
                        start_offset=0xffffffff, end_offset=0xffffffff,
                        sub_frames=[TIT2(encoding=3, text=["Custom Outro"])]))
        root = next((toc for toc in output.getall("CTOC")
                     if toc.flags & CTOCFlags.TOP_LEVEL), None)
        if root:
            root.child_element_ids = list(root.child_element_ids) + [outro_id]
        else:
            toc_id = "dynamic-outro-toc"
            n = 1
            while toc_id in known_ids:
                toc_id = f"dynamic-outro-toc-{n}"
                n += 1
            original_chapters = sorted(chapters, key=lambda c: c.start_time)
            output.add(CTOC(element_id=toc_id,
                            flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                            child_element_ids=[c.element_id for c in original_chapters] + [outro_id]))
        output.delall("TLEN")
        output.add(TLEN(encoding=3, text=[str(end)]))
        output.save(output_path, v2_version=3)
    except (MutagenError, OSError, ValueError, TypeError) as exc:
        # Metadata should never prevent someone from playing a valid episode.
        logger.warning("Could not preserve original audio metadata: %s", type(exc).__name__)


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
                await asyncio.to_thread(_preserve_episode_metadata, original_path,
                                        output_path, source["format_name"],
                                        source["duration"], info["duration"])
                return "copy"
        except AudioError:
            pass
    # If input codecs differ, decode, join, and encode an MP3 with known output.
    await _run_ffmpeg([
        "-i", original_path, "-i", outro_path,
        "-filter_complex", "[0:a:0][1:a:0]concat=n=2:v=0:a=1[a]",
        "-map", "[a]", "-map_chapters", "0", "-map_metadata", "0",
        "-c:a", "libmp3lame", "-b:a", "96k", "-ac", "2",
        "-id3v2_version", "3", output_path,
    ])
    finished = await asyncio.to_thread(probe_audio, output_path)
    await asyncio.to_thread(_preserve_episode_metadata, original_path,
                            output_path, source["format_name"],
                            source["duration"], finished["duration"])
    return "transcode"
