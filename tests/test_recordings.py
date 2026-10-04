from app import recordings
from app.core.config import settings


def test_save_and_find_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATIC_DIR", tmp_path)
    assert recordings.recording_url("rnd_abc") is None

    recordings.save_recording("rnd_abc", "audio.webm", b"first")
    assert recordings.recording_url("rnd_abc") == "/static/recordings/rnd_abc.webm"

    # повторная отправка заменяет прежний файл, даже в другом формате
    recordings.save_recording("rnd_abc", "audio.m4a", b"second")
    assert recordings.recording_url("rnd_abc") == "/static/recordings/rnd_abc.m4a"
    assert sorted(p.name for p in (tmp_path / "recordings").iterdir()) == ["rnd_abc.m4a"]
    assert (tmp_path / "recordings" / "rnd_abc.m4a").read_bytes() == b"second"


def test_unknown_extension_and_unsafe_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATIC_DIR", tmp_path)
    recordings.save_recording("rnd_x", "blob", b"data")
    assert recordings.recording_url("rnd_x") == "/static/recordings/rnd_x.m4a"

    recordings.save_recording("../evil", "a.m4a", b"data")
    assert recordings.recording_url("../evil") is None
    assert not (tmp_path / "evil.m4a").exists()
