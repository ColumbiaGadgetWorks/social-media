import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from studio import db as db_mod
from studio import posts as post_svc
from studio import video
from studio.media import abs_path, process, store_upload
from studio.models import MediaAsset, Post, RenderJob, User
from studio.security import Actor

from .conftest import make_user


def ffmpeg_clip(path, seconds=4, size="1280x720", audio=True):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}", "-shortest", "-c:a", "aac"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.fixture
def clip(app, tmp_path, monkeypatch):
    make_user("adam", "admin")
    # No Whisper model in tests: pretend it heard two lines.
    fake = SimpleNamespace(transcribe=lambda wav: [SimpleNamespace(t0=20, t1=150, text=" The laser is warming up."),
                                                   SimpleNamespace(t0=160, t1=380, text=" Quarter-inch birch, two passes.")])
    monkeypatch.setattr(video, "_model", lambda: fake)
    src = ffmpeg_clip(tmp_path / "clip.mp4")
    with db_mod.session_scope() as s:
        adam = s.scalar(select(User))
        asset = store_upload(s, src.open("rb"), "clip.mp4", "video/mp4", adam.id)
        process(asset)
        return asset.id


def test_upload_gets_transcript(clip):
    with db_mod.session_scope() as s:
        asset = s.get(MediaAsset, clip)
        assert asset.transcript_status == "done"
        assert asset.transcript[0] == {"start": 0.2, "end": 1.5, "text": "The laser is warming up."}


def test_srt_round_trip():
    segs = [{"start": 0.2, "end": 1.5, "text": "Hello there."}, {"start": 61.25, "end": 63.0, "text": "Two lines"}]
    srt = video.to_srt(segs)
    assert "00:01:01,250 --> 00:01:03,000" in srt
    assert video.from_srt(srt) == segs


def test_spec_validation(clip):
    with db_mod.session_scope() as s:
        asset = s.get(MediaAsset, clip)
        with pytest.raises(video.RenderError, match="after the start"):
            video.clean_spec({"start": 3, "end": 2}, asset)
        spec = video.clean_spec({"start": 1, "end": 999, "music_volume": 5}, asset)
        assert spec["end"] == pytest.approx(asset.duration_s, abs=0.05) and spec["music_volume"] == 1.0


def test_full_render_swaps_into_post(clip, tmp_path):
    music_src = tmp_path / "music.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                    "-c:a", "libmp3lame", str(music_src)], check=True)
    with db_mod.session_scope() as s:
        actor = Actor("user", s.scalar(select(User)))
        track = video.store_music(s, music_src.open("rb"), "music.mp3", title="Loop", artist="Test",
                                  license="CC BY 4.0", credit_line="Music: Loop by Test (CC BY)")
        post = post_svc.create_post(s, actor, [clip], note="laser")
        job = video.request(s, actor, s.get(MediaAsset, clip),
                            {"start": 0.5, "end": 3.5, "subtitles": True, "music_track_id": track.id}, post=post)
        job_id, post_id = job.id, post.id
    with db_mod.session_scope() as s:
        assert video.run_pending(s) == 1
    with db_mod.session_scope() as s:
        job = s.get(RenderJob, job_id)
        assert job.status == "done", job.error
        out = job.output
        assert out.credit == "Music: Loop by Test (CC BY)" and out.source_media_id == clip
        info = probe(abs_path(out.path))
        v = next(st for st in info["streams"] if st["codec_type"] == "video")
        assert (v["width"], v["height"]) == (1080, 1920)
        assert float(info["format"]["duration"]) == pytest.approx(3.0 + video.END_CARD_SECONDS, abs=0.25)
        assert out.thumb_path and len(out.frames) == 5
        post = s.get(Post, post_id)
        assert [m.id for m in post.media] == [out.id]  # the edit replaced the original in the post
        # The music credit is now required in captions.
        v = post.version("tiktok")
        v.enabled, v.body = True, "Laser time"
        assert any("music credit" in p for p in post_svc.problems(post).get("tiktok", []))


def test_render_without_audio_or_music(app, tmp_path):
    make_user("adam", "admin")
    src = ffmpeg_clip(tmp_path / "silent.mp4", seconds=2, size="720x1280", audio=False)
    with db_mod.session_scope() as s:
        actor = Actor("user", s.scalar(select(User)))
        asset = store_upload(s, src.open("rb"), "silent.mp4", "video/mp4", actor.user.id)
        process(asset)
        job = video.request(s, actor, asset, {"shape": "1:1", "fit": "crop", "end_card": False, "logo": False})
        job_id = job.id
    with db_mod.session_scope() as s:
        video.run_pending(s)
        job = s.get(RenderJob, job_id)
        assert job.status == "done", job.error
        info = probe(abs_path(job.output.path))
        assert {st["codec_type"] for st in info["streams"]} == {"video", "audio"}
        assert job.note == "Saved to the media library."


def test_cannot_render_into_approved_post(clip):
    with db_mod.session_scope() as s:
        post = post_svc.create_post(s, Actor.system(), [clip])
        post.status = "approved"
        with pytest.raises(video.RenderError, match="approved"):
            video.request(s, Actor.system(), s.get(MediaAsset, clip), {}, post=post)


def test_end_card_image():
    img = video.end_card((1080, 1920), "Open Hack Night · Thursdays 6 PM · Free")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert img.size == (1080, 1920)


def test_video_page_shows_editor_and_queues_render(app, clip):
    from starlette.testclient import TestClient

    from .conftest import csrf_of, login

    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        login(c, "adam")
        page = c.get(f"/media/{clip}")
        assert "Edit video" in page.text and "The laser is warming up." in page.text  # transcript as SRT
        r = c.post(f"/media/{clip}/render", data={"csrf": csrf_of(c, f"/media/{clip}"), "start": "0", "end": "2",
                                                  "shape": "9:16", "fit": "pad", "subtitles": "1", "logo": "1"})
        assert "Render queued" in r.text
        srt = "1\n00:00:00,200 --> 00:00:01,500\nThe laser is warming up!\n"
        c.post(f"/media/{clip}/transcript", data={"csrf": csrf_of(c, f"/media/{clip}"), "srt": srt})
        assert c.get("/music").status_code == 200
    with db_mod.session_scope() as s:
        assert s.get(MediaAsset, clip).transcript[0]["text"] == "The laser is warming up!"
        assert s.scalar(select(RenderJob)).status == "queued"
