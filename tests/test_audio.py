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


def test_preserves_embedded_mp3_chapters_and_transcript(tmp_path):
    """The concat demuxer normally strips ID3 CHAP/CTOC, USLT and SYLT."""
    from mutagen.id3 import (ID3, CHAP, CTOC, CTOCFlags, TIT2, USLT, SYLT)
    source = tmp_path / "chaptered.mp3"
    outro = tmp_path / "outro.mp3"
    finished = tmp_path / "finished.mp3"
    tone(source, 3)
    tone(outro, 1, frequency=800)
    tags = ID3(source)
    tags.add(CHAP(element_id="intro", start_time=0, end_time=1250,
                  sub_frames=[TIT2(encoding=3, text=["Intro"])]))
    tags.add(CHAP(element_id="discussion", start_time=1250,
                  end_time=0xffffffff,
                  sub_frames=[TIT2(encoding=3, text=["Discussion"])]))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                  child_element_ids=["intro", "discussion"]))
    tags.add(USLT(encoding=3, lang="eng", desc="Transcript",
                  text="Original source transcript text."))
    tags.add(SYLT(encoding=3, lang="eng", format=2, type=1,
                  desc="Timed transcript", text=[("Hello", 250), ("there", 620)]))
    tags.save(source, v2_version=3)

    method = asyncio.run(append_outro(str(source), str(outro), str(finished)))
    assert method == "copy"
    result = ID3(finished)
    chapters = sorted(result.getall("CHAP"), key=lambda c: c.start_time)
    assert [c.sub_frames.getall("TIT2")[0].text[0] for c in chapters] == [
        "Intro", "Discussion", "Custom Outro"]
    assert 2950 <= chapters[-1].start_time <= 3150
    assert chapters[1].end_time == chapters[-1].start_time
    toc = next(t for t in result.getall("CTOC") if t.flags & CTOCFlags.TOP_LEVEL)
    assert toc.child_element_ids == ["intro", "discussion", chapters[-1].element_id]
    assert result.getall("USLT")[0].text == "Original source transcript text."
    assert result.getall("SYLT")[0].text == [("Hello", 250), ("there", 620)]
    assert 3.8 < probe_audio(str(finished))["duration"] < 4.2


def test_m4a_chapters_and_lyrics_survive_conversion_to_mp3(tmp_path):
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    source_clean = tmp_path / "clean.m4a"
    source = tmp_path / "chapters.m4a"
    meta = tmp_path / "chapters.ffmeta"
    outro = tmp_path / "outro.mp3"
    finished = tmp_path / "finished.mp3"
    tone(source_clean, 2.5, fmt="aac")
    tone(outro, 1, frequency=800)
    meta.write_text(''';FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=1200
title=Intro
[CHAPTER]
TIMEBASE=1/1000
START=1200
END=2500
title=Interview
''', encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source_clean),
                    "-i", str(meta), "-map", "0:a:0", "-map_chapters", "1",
                    "-c:a", "copy", str(source)], check=True)
    mp4 = MP4(source)
    mp4["\xa9lyr"] = ["An embedded text transcript."]
    mp4.save()
    method = asyncio.run(append_outro(str(source), str(outro), str(finished)))
    assert method == "transcode"
    tags = ID3(finished)
    chapters = sorted(tags.getall("CHAP"), key=lambda c: c.start_time)
    assert [c.sub_frames.getall("TIT2")[0].text[0] for c in chapters] == [
        "Intro", "Interview", "Custom Outro"]
    assert tags.getall("USLT")[0].text == "An embedded text transcript."
    assert 3.3 < probe_audio(str(finished))["duration"] < 3.8


def test_no_embedded_chapters_does_not_add_only_outro_chapter(tmp_path):
    """Avoid hiding external <podcast:chapters> with a single embedded outro."""
    from mutagen.id3 import ID3
    source = tmp_path / "source.mp3"
    outro = tmp_path / "outro.mp3"
    finished = tmp_path / "finished.mp3"
    tone(source, 1.5)
    tone(outro, 1)
    asyncio.run(append_outro(str(source), str(outro), str(finished)))
    assert ID3(finished).getall("CHAP") == []


def test_transcode_mp3_keeps_chapters_and_text(tmp_path):
    """Transcoding a mismatched-bitrate/rate source must not discard ID3."""
    from mutagen.id3 import ID3, CHAP, TIT2, USLT
    source = tmp_path / "source.mp3"
    outro = tmp_path / "outro.mp3"
    output = tmp_path / "output.mp3"
    tone(source, 2, rate=44100)
    tone(outro, 1, rate=48000)
    tags = ID3(source)
    tags.add(CHAP(element_id="part1", start_time=0, end_time=2000,
                  sub_frames=[TIT2(encoding=3, text=["First part"])]))
    tags.add(USLT(encoding=3, lang="eng", desc="Text", text="Transcript retained"))
    tags.save(source, v2_version=3)
    assert asyncio.run(append_outro(str(source), str(outro), str(output))) == "transcode"
    result = ID3(output)
    assert len(result.getall("CHAP")) == 2
    assert result.getall("CHAP")[1].sub_frames.getall("TIT2")[0].text[0] == "Custom Outro"
    assert result.getall("USLT")[0].text == "Transcript retained"


def test_duration_tag_updated_without_chapters(tmp_path):
    from mutagen.id3 import ID3, TLEN
    src, outro, out = (tmp_path / p for p in ("src.mp3", "outro.mp3", "out.mp3"))
    tone(src, 2)
    tone(outro, 1)
    tag = ID3(src)
    tag.add(TLEN(encoding=3, text=["2000"]))
    tag.save(src, v2_version=3)
    asyncio.run(append_outro(str(src), str(outro), str(out)))
    result = ID3(out)
    assert result.getall("CHAP") == []
    assert 2800 <= int(result.getall("TLEN")[0].text[0]) <= 3300
