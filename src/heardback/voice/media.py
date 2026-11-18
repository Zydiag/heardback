"""Duck: pause what's playing while the voice listens, resume it at turn end (via `jv.media`, i.e. playerctl).

The players paused are written to `$XDG_RUNTIME_DIR/heardback-ducked` (`name\turl\ttitle` per line). A playback verb run during the turn
(`media`, `zen.play`, `zen.pick`, `zen.video`) or a bare "stop"/"pause" removes that file (`jv.media.claim`),
and then nothing is resumed. Zen has ONE MPRIS player for all its tabs, so a player is resumed only if it
still shows what was paused (same url; same title when there's no url), else it's kept (duckfix). Runs on the voice's io thread, never the audio thread.
"""
from jv import media as jvmedia
from jv import trace
from jv.verb import JvError

from .config import log


class Media:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.ours: dict[str, tuple[str, str]] = {}   # name → (url, title) when paused
        self._warned = False
        jvmedia.claim()   # a previous daemon's duck file is stale

    def _players(self) -> list[dict]:
        try:
            return jvmedia.players()
        except JvError as e:
            if not self._warned:
                self._warned = True
                log("media_error", error=e.msg)
            return []

    def _pc(self, name: str, action: str) -> bool:
        try:
            jvmedia._pc("-p", name, action)
            return True
        except JvError as e:
            log("media_error", error=e.msg)
            return False

    def pause(self, tid: str | None = None) -> None:
        """Pause every Playing player except the phone (KDE Connect plays on the phone, not into this mic)."""
        if not self.enabled:
            return
        new = {p["name"]: (p["url"], p["title"]) for p in self._players() if p["status"] == "Playing"
               and p["name"] not in self.ours and not p["name"].startswith("kdeconnect")}
        new = {n: item for n, item in new.items() if self._pc(n, "pause")}
        if new:
            self.ours |= new
            jvmedia.duck_path().write_text("".join(f"{n}\t{u}\t{t}\n" for n, (u, t) in self.ours.items()))
            trace.emit("voice", "duck", tid, action="paused", players=list(new))

    def keep(self) -> None:
        """The user said a bare stop/pause: leave the music paused."""
        jvmedia.claim()

    def resume(self, tid: str | None = None) -> None:
        ours, self.ours = self.ours, {}
        if not ours:
            return
        if not jvmedia.duck_path().exists():   # claimed: the turn played something / the user said stop
            return trace.emit("voice", "duck", tid, action="kept", players=list(ours))
        retained = jvmedia.ducked()
        ours = {n: item for n, item in ours.items() if n in retained}
        jvmedia.claim()   # before reading: with the file gone, statuses are the real ones
        paused = [p for p in self._players() if p["name"] in ours and p["status"] == "Paused"]
        back = [p["name"] for p in paused if _same(p, *ours[p["name"]])]
        changed = [p["name"] for p in paused if p["name"] not in back]
        back = [n for n in back if self._pc(n, "play")]
        if changed:   # e.g. "close youtube": Zen's one player now points at the Spotify tab
            trace.emit("voice", "duck", tid, action="kept", reason="changed", players=changed)
        if back or not changed:
            trace.emit("voice", "duck", tid, action="resumed", players=back)


def _same(p: dict, url: str, title: str) -> bool:
    return p["url"] == url if url or p["url"] else p["title"] == title
