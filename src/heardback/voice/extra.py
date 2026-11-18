"""Voice dependency checks that also work in a standard-library-only install."""
from importlib.util import find_spec

INSTALL = "install heardback[voice] or [voice-cpu]"
MODULES = ("numpy", "torch", "torchaudio", "faster_whisper", "kokoro", "silero_vad",
           "onnxruntime", "openwakeword", "speechbrain", "huggingface_hub", "en_core_web_sm")


def missing() -> list[str]:
    return [name for name in MODULES if find_spec(name) is None]


def default_device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except (ImportError, OSError):
        return "cpu"
