"""The add-on's resultLink against a real YouTube results list (playfix), in headless Firefox.

Fixture: trimmed `ytd-item-section-renderer #contents` of "lofi hip hop" (2026-10-31): a mix lockup
(yt-lockup-view-model), plain videos (ytd-video-renderer), a shorts shelf (grid-shelf-view-model).
Skipped without Firefox. Throwaway profile, never the user's Zen.
"""
import json
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIRST4 = ["rFZHOHl-L8A", "lTRiuFIWV54", "n61ULEU7CO0", "CLeZyIID9Bo"]   # probe order, same on the live page
AD = '<ytd-ad-slot-renderer><a href="/watch?v=ADADADADADA" title="An ad">An ad</a></ytd-ad-slot-renderer>'

pytestmark = pytest.mark.skipif(not shutil.which("firefox"), reason="needs firefox")


class Marionette:
    def __init__(self, port: int):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.f, self.mid = self.s.makefile("rb"), 0
        self._read()

    def _read(self):
        n = b""
        while (c := self.f.read(1)) != b":":
            n += c
        return json.loads(self.f.read(int(n)))

    def __call__(self, name: str, params: dict):
        self.mid += 1
        b = json.dumps([0, self.mid, name, params]).encode()
        self.s.sendall(str(len(b)).encode() + b":" + b)
        r = self._read()
        assert not r[2], r[2]
        return r[3]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    prof = tmp_path_factory.mktemp("ffprofile")
    (prof / "user.js").write_text('user_pref("marionette.port", 0);\n')
    ff = subprocess.Popen(["firefox", "--headless", "--marionette", "--no-remote", "--profile", str(prof)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = prof / "MarionetteActivePort"
        for _ in range(150):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.2)
        m = Marionette(int(port_file.read_text()))
        m("WebDriver:NewSession", {"capabilities": {}})
        m("WebDriver:SetTimeouts", {"script": 20000})
        yield m
    finally:
        ff.terminate()
        ff.wait()


def run(page, html: str, ns: list[int], classic: bool = True) -> list[dict]:
    js = (ROOT / "extension/background.js").read_text()
    sel = re.search(r"const RESULT_SELECTORS = (\{.*?\n\});", js, re.S)[1]
    fn = re.search(r"(function resultLink.*?\n\}\n)", js, re.S)[1].replace('location.hostname.endsWith("youtube.com")', "true")
    page("WebDriver:Navigate", {"url": "about:blank"})
    body = f"<ytd-search><ytd-section-list-renderer><ytd-item-section-renderer>{html}</ytd-item-section-renderer></ytd-section-list-renderer></ytd-search>"
    script = (f"const done = arguments[arguments.length - 1]; document.head.innerHTML = '<base href=\"https://www.youtube.com/\">';"
              f"document.body.innerHTML = {json.dumps(body)};"
              f"const sel = {sel}; {'' if classic else 'sel.youtube = \"x-none\";'}"
              f"Promise.all({json.dumps(ns)}.map(n => ({fn})(n, sel, 600))).then(done);")
    return page("WebDriver:ExecuteAsyncScript", {"script": script, "args": []})["value"]


def vid(r: dict) -> str:
    return re.search(r"v=([\w-]+)", r["url"])[1]


@pytest.mark.parametrize("classic", [True, False], ids=["classic", "any-layout"])
def test_plain_videos_in_order_no_mix_shorts_or_ads(page, classic):
    fixture = (ROOT / "tests/fixtures/zen/youtube-results.html").read_text()
    rs = run(page, AD + fixture, [1, 2, 3, 4], classic)
    assert all(r["ok"] and r["kind"] == "youtube" for r in rs) and [vid(r) for r in rs] == FIRST4
    assert rs[0]["title"].startswith("lofi hip hop radio")


def test_empty_page_names_the_layout(page):
    [r] = run(page, "<div id=contents><yt-lockup-view-model><a href='/shorts/x'>s</a></yt-lockup-view-model></div>", [1], False)
    assert r == {"ok": False, "reason": "no search results on this page",
                 "diag": "watch_links=0 tags=yt-lockup-view-model"}


# --- bgplay: ytVideos / ytData, the pure parser behind yt_first (no rendering) -------------------
# Fixture: trimmed real ytInitialData of "lofi hip hop" (headless fetch 2026-10-31): plain videos, a mix
# lockup (LOCKUP_CONTENT_TYPE_PLAYLIST), a shorts shelf (gridShelfViewModel + shortsLockupViewModel).
def _fn(js: str, name: str) -> str:
    return re.search(rf"(function {name}\(.*?\n\}}\n)", js, re.S)[1]


def parse(page, data: dict | None = None, html: str | None = None) -> list[dict]:
    js = (ROOT / "extension/background.js").read_text()
    skip = re.search(r"(const YT_SKIP = new Set\(.*?\);)", js, re.S)[1]
    script = (f"{skip} {_fn(js, 'ytVideos')} {_fn(js, 'ytData')}"
              f"const a = arguments[0]; return ytVideos(a.html === null ? a.data : ytData(a.html));")
    page("WebDriver:Navigate", {"url": "about:blank"})
    return page("WebDriver:ExecuteScript", {"script": script, "args": [{"data": data, "html": html}]})["value"]


def _data() -> dict:
    return json.loads((ROOT / "tests/fixtures/zen/youtube-initial-data.json").read_text())


def _items(d: dict) -> list:
    return d["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"][0][
        "itemSectionRenderer"]["contents"]


def test_yt_videos_plain_in_order_skips_mix_and_shorts(page):
    vs = parse(page, _data())
    assert [v["video_id"] for v in vs[:4]] == FIRST4 and len(vs) == 8   # 10 items - mix - shorts shelf
    assert vs[0]["title"].startswith("lofi hip hop radio")


def test_yt_videos_skips_ads_and_shorts_takes_video_lockups(page):
    d = _data()
    ad = {"adSlotRenderer": {"content": {"videoRenderer": {"videoId": "ADADADADADA",
                                                            "navigationEndpoint": {"watchEndpoint": {}}}}}}
    short = {"videoRenderer": {"videoId": "SHSHSHSHSHS", "navigationEndpoint": {"reelWatchEndpoint": {}}}}
    lockup = {"lockupViewModel": {"contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": "LKLKLKLKLKL",
                                  "metadata": {"lockupMetadataViewModel": {"title": {"content": "New layout video"}}}}}
    _items(d)[:0] = [ad, short, lockup]
    vs = parse(page, d)
    assert vs[0] == {"video_id": "LKLKLKLKLKL", "title": "New layout video"} and [v["video_id"] for v in vs[1:5]] == FIRST4


def test_yt_data_from_page_html(page):
    html = f'<script nonce="x">var ytInitialData = {json.dumps(_data())};</script><script>var other = 1;</script>'
    assert [v["video_id"] for v in parse(page, html=html)[:4]] == FIRST4


# --- mediafix: videoCtl on a page with no media element in the DOM (Spotify) clicks its play/pause button ------
def ctl(page, label: str, action: str) -> tuple[dict, int]:
    js = (ROOT / "extension/background.js").read_text()
    body = f'<button data-testid="control-button-playpause" aria-label="{label}" onclick="window.n++">x</button>'
    script = (f"const done = arguments[arguments.length - 1]; window.n = 0; document.body.innerHTML = {json.dumps(body)};"
              f"({_fn(js, 'videoCtl')})({json.dumps(action)}).then(r => done([r, window.n]));")
    page("WebDriver:Navigate", {"url": "about:blank"})
    return tuple(page("WebDriver:ExecuteAsyncScript", {"script": script, "args": []})["value"])


@pytest.mark.parametrize("label,action,clicks", [
    ("Play", "play", 1), ("Pause", "play", 0), ("Pause", "pause", 1), ("Play", "pause", 0),
    ("Play", "toggle", 1), ("Pause", "toggle", 1),
])
def test_video_ctl_spotify_button(page, label, action, clicks):
    r, n = ctl(page, label, action)
    # The fixture leaves the label unchanged after clicking: report observation, not desired state.
    assert r == {"ok": True, "paused": label == "Play", "via": "button", "clicked": bool(clicks),
                 "before_label": label, "after_label": label} and n == clicks
