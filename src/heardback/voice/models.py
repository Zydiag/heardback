"""Pinned model downloads (`heardback-voice setup`) and their local paths."""
import os
from pathlib import Path

from .config import data_dir

REPOS = {
    "stt": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "stt_cpu": "Systran/faster-distil-whisper-small.en",
    "tts": "hexgrad/Kokoro-82M",
    "ecapa": "speechbrain/spkrec-ecapa-voxceleb",
}
PINS = {  # commit SHAs taken 2026-10-30
    REPOS["stt"]: "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    REPOS["stt_cpu"]: "ef77d90526ccd62cde3808ee70626a01e5cf83e4",
    REPOS["tts"]: "f3ff3571791e39611d31c381e3a41a3af07b4987",
    REPOS["ecapa"]: "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286",
}


def hf_home() -> Path:
    return data_dir() / "models" / "hf"


def snapshot(repo: str) -> Path:
    """Local path of the pinned snapshot (cache only when HF_HUB_OFFLINE=1)."""
    from huggingface_hub import snapshot_download
    offline = os.environ.get("HF_HUB_OFFLINE") == "1"
    # cache_dir explicit: HF_HOME is read once at huggingface_hub import, often before us
    return Path(snapshot_download(repo, revision=PINS[repo], cache_dir=hf_home() / "hub",
                                  local_files_only=offline))


IMPOSTORS_URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"   # LibriSpeech, CC-BY-4.0
IMPOSTOR_SPEAKERS, IMPOSTOR_S = 30, 60.0


def impostors_dir() -> Path:
    return data_dir() / "voice" / "impostors"


def setup_impostors(url: str = IMPOSTORS_URL) -> int:
    """First 30 LibriSpeech test-clean speakers → <data>/voice/impostors/<spk>.wav (60 s each). Returns the count."""
    import tarfile
    import tempfile
    import urllib.request
    import numpy as np
    import soundfile as sf   # torchaudio 2.11 load needs torchcodec (not installed); LibriSpeech is 16 kHz already
    from .enroll import write_wav
    out = impostors_dir()
    out.mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data_dir()) as tmp:      # not /tmp: the tarball is ~350 MB
        tgz = Path(tmp) / "test-clean.tar.gz"
        print(f"fetching {url}", flush=True)
        urllib.request.urlretrieve(url, tgz)
        with tarfile.open(tgz) as tar:
            members, speakers = [], []
            for m in tar:
                parts = Path(m.name).parts      # LibriSpeech/test-clean/<spk>/<chapter>/<file>.flac
                if not m.isfile() or len(parts) < 5 or not m.name.endswith(".flac"):
                    continue
                if parts[2] not in speakers:
                    if len(speakers) == IMPOSTOR_SPEAKERS:
                        continue
                    speakers.append(parts[2])
                members.append(m)
            tar.extractall(tmp, members=members, filter="data")
        tgz.unlink()
        for spk in speakers:
            got, n = [], 0
            for f in sorted((Path(tmp) / "LibriSpeech" / "test-clean" / spk).rglob("*.flac")):
                a, sr = sf.read(f, dtype="int16")
                if sr != 16000:
                    continue
                got.append(a)
                n += len(a)
                if n >= IMPOSTOR_S * 16000:
                    break
            if got:
                write_wav(out / f"{spk}.wav", np.concatenate(got)[:int(IMPOSTOR_S * 16000)])
    print(f"impostors: {len(speakers)} speakers in {out}")
    return len(speakers)


def setup(impostors: bool = False) -> None:
    if impostors:
        setup_impostors()
    for repo in REPOS.values():
        print(f"fetching {repo}", flush=True)
        snapshot(repo)
    import openwakeword.utils as owu
    if hasattr(owu, "download_models"):  # 0.4.0 ships the feature models in the wheel
        owu.download_models([])
