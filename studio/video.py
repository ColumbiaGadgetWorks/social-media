"""Video editing with ffmpeg: trim, fit to a platform's shape, subtitles, music, logo, end card.

One brand style, applied the same way every time, so edits look consistent rather than
"AI-made". Renders run one at a time in the background loop and produce a new media item;
the original upload is never changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import media as media_mod
from .db import settings, utcnow
from .models import MediaAsset, MusicTrack, Post, PostMedia, RenderJob
from .security import Actor, audit

log = logging.getLogger(__name__)

SHAPES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}
MAX_SECONDS = 180  # Shorts, Reels and TikTok all take up to 3 minutes
END_CARD_SECONDS = 2.0
DEFAULT_END_CARD = "Open Hack Night · Thursdays 6 PM · Free"
ACCENT, PAPER = (191, 77, 40), (255, 255, 255)
LOGO = Path(__file__).parent / "static" / "logo.png"
SUBTITLE_FONT = "DejaVu Sans"


class RenderError(Exception):
    pass


# --- spec ---------------------------------------------------------------------


def clean_spec(raw: dict, source: MediaAsset) -> dict:
    """Validate and fill defaults. Raises RenderError with a plain message."""
    if source.kind != "video":
        raise RenderError("Only videos can be edited.")
    duration = source.duration_s or 0
    start = max(0.0, float(raw.get("start") or 0))
    end = raw.get("end")
    end = float(end) if end not in (None, "") else duration
    if duration:
        end = min(end, duration)
    if end <= start:
        raise RenderError("The end time must be after the start time.")
    if end - start > MAX_SECONDS:
        end = start + MAX_SECONDS
    shape = raw.get("shape") or "9:16"
    if shape not in SHAPES:
        raise RenderError(f"Shape must be one of {', '.join(SHAPES)}.")
    fit = raw.get("fit") or "pad"
    if fit not in ("pad", "crop"):
        raise RenderError("Fit must be pad or crop.")
    subtitles = bool(raw.get("subtitles"))
    if subtitles and not source.transcript:
        raise RenderError("This video has no transcript yet, so subtitles aren't available.")
    music = raw.get("music_track_id")
    music = int(music) if music not in (None, "", 0, "0") else None
    volume = min(1.0, max(0.05, float(raw.get("music_volume") or 0.25)))
    return {
        "start": round(start, 2), "end": round(end, 2), "shape": shape, "fit": fit,
        "subtitles": subtitles, "music_track_id": music, "music_volume": volume,
        "keep_audio": raw.get("keep_audio", True) not in (False, "0", "false", ""),
        "logo": raw.get("logo", True) not in (False, "0", "false", ""),
        "end_card": raw.get("end_card", True) not in (False, "0", "false", ""),
        "end_card_text": (raw.get("end_card_text") or DEFAULT_END_CARD).strip()[:80],
    }


def request(db: Session, actor: Actor, source: MediaAsset, raw: dict, post: Post | None = None) -> RenderJob:
    if post is not None and post.status in ("approved", "done"):
        raise RenderError("This post is approved; edit the video before approving, or send the post back first.")
    spec = clean_spec(raw, source)
    if spec["music_track_id"] and db.get(MusicTrack, spec["music_track_id"]) is None:
        raise RenderError("That music track doesn't exist.")
    job = RenderJob(source_media_id=source.id, post_id=post.id if post else None, spec=spec, requested_by=actor.label)
    db.add(job)
    db.flush()
    audit(db, actor, "render_requested", "media", source.id, job=job.id, post=job.post_id, spec=spec)
    return job


# --- subtitles ----------------------------------------------------------------


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_subtitles(segments: list[dict], start: float, end: float, size: tuple[int, int]) -> str:
    """One readable style: white text on a soft dark box, lower third."""
    w, h = size
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: CGW,{SUBTITLE_FONT},{round(w * 0.055)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,1,0,0,0,"
        f"100,100,0,0,3,14,0,2,{round(w * 0.08)},{round(w * 0.08)},{round(h * 0.16)},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for seg in segments:
        s, e = float(seg["start"]) - start, float(seg["end"]) - start
        if e <= 0 or s >= end - start:
            continue
        text = " ".join(str(seg["text"]).split()).replace("{", "(").replace("}", ")").replace("\\", "/")
        if text:
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(min(e, end - start))},CGW,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def to_srt(segments: list[dict]) -> str:
    def stamp(t: float) -> str:
        ms = round(t * 1000)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    return "\n".join(
        f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n{seg['text']}\n" for i, seg in enumerate(segments, 1)
    )


def from_srt(text: str) -> list[dict]:
    def secs(stamp: str) -> float:
        hms, _, ms = stamp.strip().replace(".", ",").partition(",")
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s + int(ms or 0) / 1000

    segments = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        rows = [r for r in block.strip().split("\n") if r.strip()]
        arrow = next((i for i, r in enumerate(rows) if "-->" in r), None)
        if arrow is None:
            continue
        a, _, b = rows[arrow].partition("-->")
        body = " ".join(rows[arrow + 1:]).strip()
        if body:
            segments.append({"start": round(secs(a), 2), "end": round(secs(b), 2), "text": body})
    return segments


# --- transcription ------------------------------------------------------------

_whisper = None


def _model():
    global _whisper
    if _whisper is None:
        from pywhispercpp.model import Model

        models = settings().data_dir / "models"
        models.mkdir(exist_ok=True)
        _whisper = Model(settings().whisper_model, models_dir=str(models), n_threads=settings().ffmpeg_threads)
    return _whisper


def transcribe(asset: MediaAsset, model=None) -> None:
    """Fill asset.transcript from the audio. Never raises: subtitles are optional."""
    if not settings().whisper_model:
        asset.transcript_status = "off"
        return
    if not _probe_has_audio(media_mod.abs_path(asset.path)):
        asset.transcript_status = "no audio"
        return
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(media_mod.abs_path(asset.path)), "-vn", "-ac", "1",
                 "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                capture_output=True, timeout=600, check=True,
            )
            segments = (model or _model()).transcribe(str(wav))
        # whisper.cpp times are in hundredths of a second
        asset.transcript = [
            {"start": round(s.t0 / 100, 2), "end": round(s.t1 / 100, 2), "text": s.text.strip()}
            for s in segments if s.text.strip() and not s.text.strip().startswith("[")
        ]
        asset.transcript_status = "done"
    except Exception as exc:
        log.warning("transcribing media %s failed: %s", asset.id, exc)
        asset.transcript_status = "failed"


# --- rendering ----------------------------------------------------------------


def _font(size: int):
    return ImageFont.load_default(size=size)


def end_card(size: tuple[int, int], text: str) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, ACCENT)
    d = ImageDraw.Draw(img)
    if LOGO.exists():
        with Image.open(LOGO) as logo:
            logo = logo.convert("RGBA").resize((w // 4, w // 4))
            img.paste(logo, ((w - logo.width) // 2, h // 2 - logo.height - 80), logo)
    for i, (line, size_px) in enumerate((("Columbia Gadget Works", w // 13), (text, w // 22),
                                         ("columbiagadgetworks.org", w // 22))):
        font = _font(size_px)
        tw = d.textlength(line, font=font)
        d.text(((w - tw) / 2, h // 2 + i * (w // 9)), line, font=font, fill=PAPER)
    return img


def _probe_has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    return bool(json.loads(out.stdout or "{}").get("streams"))


def build_commands(spec: dict, src: Path, work: Path, music: Path | None, segments: list[dict]) -> list[list[str]]:
    """The ffmpeg commands for a render (exposed for tests)."""
    w, h = SHAPES[spec["shape"]]
    dur = spec["end"] - spec["start"]
    threads = str(settings().ffmpeg_threads)
    has_audio = spec["keep_audio"] and _probe_has_audio(src)
    inputs = ["-ss", str(spec["start"]), "-t", f"{dur:.2f}", "-i", str(src)]
    vf = []
    if spec["fit"] == "pad":
        vf.append(f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                  f"boxblur=24:2,eq=brightness=-0.08[bg];[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v0]")
    else:
        vf.append(f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30[v0]")
    last = "v0"
    if spec["subtitles"] and segments:
        ass = work / "subs.ass"
        ass.write_text(ass_subtitles(segments, spec["start"], spec["end"], (w, h)))
        vf.append(f"[{last}]subtitles=filename='{ass.as_posix()}'[v1]")
        last = "v1"
    n = 1
    if spec["logo"] and LOGO.exists():
        inputs += ["-i", str(LOGO)]
        vf.append(f"[{n}:v]scale={w // 9}:-1,format=rgba,colorchannelmixer=aa=0.85[lg];"
                  f"[{last}][lg]overlay=W-w-{w // 24}:{w // 24}[v2]")
        last, n = "v2", n + 1
    vf.append(f"[{last}]format=yuv420p[vout]")

    af = []
    music_idx = None
    if music is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_idx, n = n, n + 1
        fade = max(0.0, dur - 1.5)
        af.append(f"[{music_idx}:a]atrim=0:{dur:.2f},asetpts=PTS-STARTPTS,aresample=44100,"
                  f"aformat=channel_layouts=stereo,volume={spec['music_volume']},afade=t=out:st={fade:.2f}:d=1.5[mus]")
    if has_audio:
        af.append("[0:a]aresample=44100,aformat=channel_layouts=stereo[voice]")
    if has_audio and music is not None:
        # Duck the music under speech so voices stay clear.
        af.append("[voice]asplit[speech][key];[mus][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=500[duck];"
                  "[speech][duck]amix=inputs=2:duration=first:normalize=0[aout]")
    elif has_audio:
        af.append("[voice]anull[aout]")
    elif music is not None:
        af.append("[mus]anull[aout]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
        af.append(f"[{n}:a]anull[aout]")

    main = work / "main.mp4"
    encode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac",
              "-b:a", "128k", "-ar", "44100", "-threads", threads]
    commands = [["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(vf + af),
                 "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.2f}", *encode, str(main)]]
    if spec["end_card"]:
        card_png = work / "card.png"
        end_card((w, h), spec["end_card_text"]).save(card_png)
        card = work / "card.mp4"
        commands.append(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-t", str(END_CARD_SECONDS), "-i", str(card_png),
                         "-f", "lavfi", "-t", str(END_CARD_SECONDS), "-i", "anullsrc=r=44100:cl=stereo",
                         "-vf", "fps=30,format=yuv420p", "-shortest", *encode, str(card)])
        commands.append(["ffmpeg", "-v", "error", "-y", "-i", str(main), "-i", str(card), "-filter_complex",
                         "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]",
                         *encode, "-movflags", "+faststart", str(work / "out.mp4")])
    else:
        commands.append(["ffmpeg", "-v", "error", "-y", "-i", str(main), "-c", "copy", "-movflags", "+faststart",
                         str(work / "out.mp4")])
    return commands


def run(db: Session, job: RenderJob) -> None:
    """Render one job and attach the result to its post when that's still allowed."""
    job.status = "running"
    db.commit()
    source = job.source
    try:
        music = db.get(MusicTrack, job.spec["music_track_id"]) if job.spec.get("music_track_id") else None
        rel = f"derived/renders/{job.id}-{hashlib.sha1(json.dumps(job.spec, sort_keys=True).encode()).hexdigest()[:8]}.mp4"
        dest = media_mod.abs_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            music_path = media_mod.abs_path(music.path) if music else None
            for cmd in build_commands(job.spec, media_mod.abs_path(source.path), work, music_path, source.transcript):
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                if done.returncode != 0:
                    raise RenderError((done.stderr or "ffmpeg failed").strip()[-400:])
            shutil.move(str(work / "out.mp4"), dest)
        data = dest.read_bytes()
        output = MediaAsset(
            kind="video", original_name=f"edit-{job.id}-{source.original_name}"[:256], path=rel, mime="video/mp4",
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), uploader_id=source.uploader_id,
            note=source.note, tags=sorted(set(source.tags) | {"edited"}), description=source.description,
            alt_text=source.alt_text, frames=[], transcript=[], source_media_id=source.id,
            credit=music.credit_line if music and music.credit_line else "",
        )
        db.add(output)
        db.flush()
        media_mod.process(output)
        job.output_media_id = output.id
        job.status = "done"
        job.note = _attach(db, job, output)
    except Exception as exc:
        log.warning("render %s failed: %s", job.id, exc)
        job.status = "failed"
        job.error = str(exc)[:500]
    job.finished_at = utcnow()
    audit(db, Actor.system(), f"render_{job.status}", "media", job.source_media_id, job=job.id,
          output=job.output_media_id, error=job.error)
    db.commit()


def _attach(db: Session, job: RenderJob, output: MediaAsset) -> str:
    if job.post_id is None:
        return "Saved to the media library."
    post = db.get(Post, job.post_id)
    if post is None or post.status in ("approved", "done"):
        return "The post was approved meanwhile, so the edit was saved to the library only."
    link = next((link for link in post.media_links if link.media_id == job.source_media_id), None)
    if link is None:
        post.media_links.append(PostMedia(media=output, position=len(post.media_links)))
    else:
        link.media = output
    audit(db, Actor.system(), "media_changed", "post", post.id, reason=f"render {job.id}", media=output.id)
    return f"Swapped into post {post.id}."


def run_pending(db: Session, limit: int = 1) -> int:
    jobs = db.scalars(select(RenderJob).where(RenderJob.status == "queued").order_by(RenderJob.id).limit(limit)).all()
    for job in jobs:
        run(db, job)
    return len(jobs)


def store_music(db: Session, stream, filename: str, **fields) -> MusicTrack:
    suffix = Path(filename).suffix.lower()
    if suffix not in (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"):
        raise RenderError("Music must be an MP3, M4A, AAC, WAV, OGG or FLAC file.")
    data = stream.read()
    rel = f"music/{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
    path = media_mod.abs_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    duration = None
    try:
        info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                              capture_output=True, text=True, timeout=60)
        duration = float(json.loads(info.stdout)["format"]["duration"])
    except Exception:
        path.unlink(missing_ok=True)
        raise RenderError("That file doesn't look like audio.") from None
    track = MusicTrack(path=rel, duration_s=duration, **fields)
    db.add(track)
    db.flush()
    return track

