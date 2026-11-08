import asyncio
from pathlib import Path
import shutil
import subprocess
import pytest
from app.audio import append_outro, probe_audio, AudioError

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="FFmpeg is not installed")


def tone(path: Path, seconds: float, fmt: str = "mp3", frequency: int = 400, rate: int = 44100):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", f"sine=frequency={frequency}:duration={seconds}:sample_rate={rate}",
        "-c:a", "libmp3lame" if fmt == "mp3" else "aac", str(path),
    ], check=True, timeout=30)


def test_fast_mp3_append(tmp_path):
    source = tmp_path / "source.mp3"; outro = tmp_path / "outro.mp3"
    output = tmp_path / "finished.mp3"
    tone(source, 2, frequency=400)
    tone(outro, 1, frequency=800)
    method = asyncio.run(append_outro(str(source), str(outro), str(output)))
    assert method == "copy"
    assert output.stat().st_size > source.stat().st_size
    assert 2.8 <= probe_audio(str(output))["duration"] <= 3.3


def test_transcodes_other_formats(tmp_path):
    source = tmp_path / "source.m4a"; outro = tmp_path / "outro.mp3"
    output = tmp_path / "finished.mp3"
    tone(source, 1.5, fmt="aac")
    tone(outro, 1, frequency=800)
    method = asyncio.run(append_outro(str(source), str(outro), str(output)))
    assert method == "transcode"
    assert 2.3 <= probe_audio(str(output))["duration"] <= 2.8


def test_probe_rejects_non_audio(tmp_path):
    file = tmp_path / "bad.mp3"
    file.write_bytes(b"just text, not mp3 audio")
    with pytest.raises(AudioError):
        probe_audio(str(file))
