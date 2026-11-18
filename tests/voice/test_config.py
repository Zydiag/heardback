import pytest

from heardback.voice.config import Config, config_path, load, update


def test_missing_file_gives_defaults(xdg):
    assert load() == Config()


def test_load_overrides_and_rejects_bad_values(xdg, monkeypatch, tmp_path):
    desktop = tmp_path / "desktop.toml"
    desktop.write_text('[mic]\nmode = "laptop"\n[mic.modes]\nlaptop = ["alsa_input.*"]\nbuds = ["bluez_input.*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(desktop))
    config_path().parent.mkdir(parents=True)
    config_path().write_text('mic = "buds"\nend_silence = 0.5\n')
    assert load().mic == "buds" and load().end_silence == 0.5
    config_path().write_text('mic = "phone"\n')
    with pytest.raises(ValueError, match="mic"):
        load()


def test_custom_mic_mode_overrides_voice_default(xdg, tmp_path, monkeypatch):
    desktop = tmp_path / "custom.toml"
    desktop.write_text('[mic]\nmode = "usb"\n[mic.modes]\nusb = ["usb_*"]\nroom = ["room_*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(desktop))
    assert Config().mic == load().mic == "usb"
    config_path().parent.mkdir(parents=True)
    config_path().write_text('mic = "room"\n')
    assert load().mic == "room"


def test_cli_accepts_configured_mic_modes(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    from heardback.voice import cli, miccheck
    desktop = tmp_path / "custom.toml"
    desktop.write_text('[mic]\nmode = "usb"\n[mic.modes]\nusb = ["usb_*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(desktop))
    called = []
    monkeypatch.setattr(miccheck, "run", lambda *a: called.append(a) or 0)
    assert cli.main(["miccheck", "--mic", "usb", "--distance", "desk"]) == 0
    assert called == [("usb", "desk")]
    with pytest.raises(SystemExit):
        cli.main(["miccheck", "--mic", "buds", "--distance", "desk"])


def test_type_checked_against_defaults(xdg):
    config_path().parent.mkdir(parents=True)
    config_path().write_text('end_silence = 1\nnotify = false\n')
    assert load().end_silence == 1.0 and load().notify is False
    config_path().write_text('notify = 1\n')
    with pytest.raises(ValueError, match="notify"):
        load()


def test_unknown_key_is_ignored_with_log(xdg, capsys):
    config_path().parent.mkdir(parents=True)
    config_path().write_text('mik = "buds"\n')
    assert load() == Config()
    assert '"unknown_key"' in capsys.readouterr().out


def test_speaker_on_needs_thresholds(xdg):
    config_path().parent.mkdir(parents=True)
    config_path().write_text('speaker_check = "on"\n')
    with pytest.raises(ValueError, match="heardback-voice tune"):
        load()


def test_speaker_wake_mode_needs_only_wake_threshold(xdg):
    config_path().parent.mkdir(parents=True)
    config_path().write_text('speaker_check = "wake"\n')
    with pytest.raises(ValueError, match="speaker_wake"):
        load()
    config_path().write_text('speaker_check = "wake"\nspeaker_wake = 0.4\n')
    assert load().speaker_check == "wake"


def test_update_keeps_comments_and_appends(xdg):
    p = config_path(); p.parent.mkdir(parents=True)
    p.write_text('# mine\nwake_threshold = 0.5   # tune overwrites\n')
    update(p, wake_threshold=0.62, speaker_utt=0.31)
    t = p.read_text()
    assert t.startswith("# mine\n") and "wake_threshold = 0.62" in t and "speaker_utt = 0.31" in t
    assert "# tune overwrites" in t
    assert load(p).wake_threshold == 0.62


def test_update_creates_file_0600(xdg):
    p = config_path()
    update(p, speaker_check="on", notify=False)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert 'speaker_check = "on"' in p.read_text() and "notify = false" in p.read_text()


@pytest.mark.parametrize("v", ["0", "-1.0", "5.5"])
def test_tts_volume_range(xdg, v):
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("tts_volume = 5\n")
    assert load().tts_volume == 5.0
    config_path().write_text(f"tts_volume = {v}\n")
    with pytest.raises(ValueError, match="tts_volume"):
        load()


def test_stt_hotwords_list_of_strings(xdg):
    config_path().parent.mkdir(parents=True)
    config_path().write_text('stt_hotwords = ["chess.com", "Mornye"]\n')
    assert load().stt_hotwords == ("chess.com", "Mornye")
    config_path().write_text('stt_hotwords = ["ok", 3]\n')
    with pytest.raises(ValueError, match="stt_hotwords"):
        load()


def test_stt_fixes_table_and_wake_miss_floor(xdg):
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text('wake_miss_floor = 0.12\n[stt_fixes]\n"cloud code" = "Claude Code"\n')
    assert load().stt_fixes == {"cloud code": "Claude Code"} and load().wake_miss_floor == 0.12
    config_path().write_text('[stt_fixes]\ncloud = 3\n')
    with pytest.raises(ValueError, match="stt_fixes"):
        load()


@pytest.mark.parametrize("seconds", [5.0, 60.0, 120.0])
def test_max_utterance_boundaries(xdg, seconds):
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(f"max_utterance_s = {seconds}\n")
    assert load().max_utterance_s == seconds


@pytest.mark.parametrize("seconds", [4.99, 120.01, "inf"])
def test_max_utterance_rejects_out_of_range_or_non_finite(xdg, seconds):
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(f"max_utterance_s = {seconds}\n")
    with pytest.raises(ValueError, match="max_utterance_s"):
        load()
