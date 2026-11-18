// heardback: answers jv requests relayed by the jarvis_zen native-messaging host.
"use strict";

const CLAUDE_SELECTORS = {
  composer: 'div[contenteditable="true"].ProseMirror',
  composerFallback: 'fieldset [contenteditable="true"]',
  send: 'button[aria-label*="Send"]',
  busy: 'button[aria-label*="Stop"], button[data-testid="stop-response-button"]',
};

// Runs inside the claude.ai tab (serialised with toString; arguments only via JSON.stringify).
async function typeInPage(text, sel, send, expectedUrl) {
  const evidence = { protocol: 1, inserted: false, submission: "not_attempted" };
  const result = reason => ({ ok: true, ...evidence, ...(reason ? { reason } : {}) });
  if (location.protocol !== "https:" || location.hostname !== "claude.ai" || location.href !== expectedUrl)
    return result("Claude.ai target changed");
  const box = document.querySelector(sel.composer) || document.querySelector(sel.composerFallback);
  if (!box) return result("composer not found");
  const read = () => (box.innerText ?? box.textContent ?? "").replace(/\r\n/g, "\n");
  if (read().length) return result("existing draft left untouched");
  if (document.querySelector(sel.busy)) return result("Claude.ai is already responding");
  box.focus();
  const inserted = document.execCommand("insertText", false, text);
  evidence.inserted = read().trim() === text.trim() && !!text.trim();
  if (!inserted || !evidence.inserted) return result("insertion not confirmed");
  if (!send) return result();
  await new Promise(resolve => setTimeout(resolve, 100));
  const b = document.querySelector(sel.send);
  if (!b || b.disabled || b.getAttribute("aria-disabled") === "true")
    return result("send button unavailable");
  if (!box.isConnected || location.href !== expectedUrl || read().trim() !== text.trim())
    return result("composer or target changed before send");
  // A click alone is not submission evidence; never retry an uncertain dispatch.
  evidence.submission = "unverified";
  try { b.click(); } catch (_) { return result("send dispatch uncertain"); }
  const deadline = Date.now() + 1200;
  while (Date.now() < deadline) {
    if (location.protocol !== "https:" || location.hostname !== "claude.ai" || location.href !== expectedUrl)
      return result("Claude.ai target changed after send");
    const current = document.querySelector(sel.composer) || document.querySelector(sel.composerFallback);
    if (current && !(current.innerText ?? current.textContent ?? "").trim() && document.querySelector(sel.busy)) {
      evidence.submission = "observed";
      evidence.observation = "composer_cleared_and_generation_started";
      return result();
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return result("submission not confirmed");
}

const TEXT_CAP = 8000;
const RESULT_WAIT_MS = 12000;   // the host waits 13 s (zen_host.SLOW)
const RESULT_SELECTORS = {
  youtube: "ytd-video-renderer a#video-title",   // plain videos: the classic layout, tried first
  // any layout (yt-lockup-view-model, ytd-rich-item-renderer, ...): video links inside the results list
  youtubeRoot: "ytd-search ytd-section-list-renderer #contents, ytd-search, #page-manager",
  youtubeLink: 'a[href^="/watch?v="], a[href^="https://www.youtube.com/watch?v="]',
  youtubeSkip: "ytd-ad-slot-renderer, [is-ad], ytd-in-feed-ad-layout-renderer, ytd-promoted-video-renderer, " +
    "ytd-promoted-sparkles-web-renderer, ytd-search-pyv-renderer, ytd-playlist-renderer, ytd-radio-renderer, " +
    "ytd-reel-shelf-renderer, ytd-shelf-renderer[is-shorts], ytm-shorts-lockup-view-model, grid-shelf-view-model, " +
    // new-layout mixes / playlists: the lockup's content id is the list (RD…, PL…, OL…), not a video
    '[class*="CollectionStack"], [class*="content-id-RD"], [class*="content-id-PL"], [class*="content-id-OL"]',
  google: "#search a h3",
};

// --- YouTube results from ytInitialData: no rendering needed (a hidden window never renders the list) ---
const YT_SEARCH = "https://www.youtube.com/results?search_query=";
const YT_FETCH_MS = 6000;   // the host waits 7 s (zen_host.SLOW)
// Containers whose videos aren't plain results: ads, shorts, shelves, playlists, mixes, channels.
const YT_SKIP = new Set(["adSlotRenderer", "promotedVideoRenderer", "searchPyvRenderer", "reelShelfRenderer",
  "gridShelfViewModel", "shortsLockupViewModel", "shelfRenderer", "horizontalCardListRenderer", "playlistRenderer",
  "radioRenderer", "channelRenderer", "movieRenderer"]);

// Pure: the `ytInitialData` JSON object of a results page -> plain videos [{video_id, title}] in result order.
function ytVideos(data) {
  const out = [], seen = new Set();
  const add = (id, title) => {
    if (/^[\w-]{11}$/.test(id || "") && !seen.has(id)) { seen.add(id); out.push({ video_id: id, title: title || "" }); }
  };
  const walk = o => {
    if (Array.isArray(o)) return o.forEach(walk);
    if (!o || typeof o !== "object") return;
    for (const [k, v] of Object.entries(o)) {
      if (YT_SKIP.has(k) || !v || typeof v !== "object") continue;
      if (k === "videoRenderer") {   // shorts come as reelWatchEndpoint, plain videos as watchEndpoint
        if (v.navigationEndpoint && v.navigationEndpoint.watchEndpoint)
          add(v.videoId, v.title && (v.title.simpleText || (v.title.runs || []).map(r => r.text).join("")));
      } else if (k === "lockupViewModel") {   // new layout: only the VIDEO lockups (mixes / playlists are lists)
        if (v.contentType === "LOCKUP_CONTENT_TYPE_VIDEO") {
          const m = v.metadata && v.metadata.lockupMetadataViewModel;
          add(v.contentId, m && m.title && m.title.content);
        }
      } else walk(v);
    }
  };
  const c = data && data.contents;
  walk((c && c.twoColumnSearchResultsRenderer && c.twoColumnSearchResultsRenderer.primaryContents) || c);
  return out;
}

// Pure: a results page's HTML -> its ytInitialData object (throws if missing).
function ytData(html) {
  const m = /(?:var ytInitialData|window\["ytInitialData"\])\s*=\s*/.exec(html);
  if (!m) throw new Error("no ytInitialData in the page");
  const start = m.index + m[0].length;
  return JSON.parse(html.slice(start, html.indexOf(";</script>", start)));
}

async function ytFetch(url) {
  const r = await fetch(url, { credentials: "include", signal: AbortSignal.timeout(YT_FETCH_MS) });
  if (!r.ok) throw new Error(`youtube answered ${r.status}`);
  return ytVideos(ytData(await r.text()));
}

const isYtResults = url => /^https:\/\/(www\.|m\.)?youtube\.com\/results\?/.test(url || "");
const watchUrl = id => `https://www.youtube.com/watch?v=${id}`;

// Page functions below run inside the tab (serialised with toString; arguments only via JSON.stringify).
// Title + rendered text of main/article/body; innerText never holds input or textarea values.
function pageText(cap) {
  const root = document.querySelector("main, article, [role=main]") || document.body;
  const text = (root ? root.innerText : "").replace(/\n{3,}/g, "\n\n").trim();
  return { ok: true, title: document.title, url: location.href, text: text.slice(0, cap), truncated: text.length > cap };
}

// The Nth search result's link (1-based), polling while the page renders it. Never clicks.
// YouTube: plain videos only (no ads, shorts, playlists or mixes), deduped by video id.
function resultLink(n, sel, waitMs) {
  const shown = a => a && a.href && a.offsetParent !== null;
  const ytLinks = () => {
    const first = [...document.querySelectorAll(sel.youtube)].map(e => e.closest("a"))
      .filter(a => shown(a) && !a.closest(sel.youtubeSkip));
    const root = document.querySelector(sel.youtubeRoot) || document;
    const rest = [...root.querySelectorAll(sel.youtubeLink)].filter(a => {
      if (!shown(a) || a.closest(sel.youtubeSkip)) return false;
      const u = new URL(a.href);
      return u.pathname === "/watch" && u.searchParams.get("v");   // plain videos can carry &list=RD…
    });
    const byId = new Map();
    for (const a of [...first, ...rest]) {
      const id = new URL(a.href).searchParams.get("v");
      const h3 = a.closest("h3");   // a thumbnail link's text is "Now playing" / the duration
      const title = (a.title || (h3 && h3.title) || a.getAttribute("aria-label") || (h3 ? a.innerText : "") || "").trim();
      const seen = byId.get(id);
      if (!seen) byId.set(id, { url: a.href, title });
      else if (!seen.title && title) seen.title = title;
    }
    return [...byId.values()];
  };
  const find = () => {
    if (location.hostname.endsWith("youtube.com")) return { links: ytLinks(), kind: "youtube" };
    const links = [...document.querySelectorAll(sel.google)].map(e => e.closest("a")).filter(shown)
      .map(a => ({ url: a.href, title: (a.title || a.innerText).trim() }));
    return { links, kind: "google" };
  };
  // Why nothing matched, for the log: watch links seen + the first result renderers' tags (the layout).
  const diag = () => {
    const watch = document.querySelectorAll('a[href*="/watch"]').length;
    const tags = [...new Set([...document.querySelectorAll("ytd-search #contents > *, ytd-item-section-renderer #contents > *")]
      .map(e => e.tagName.toLowerCase()))].slice(0, 6);
    return `watch_links=${watch} tags=${tags.join(",") || "-"}`;
  };
  return new Promise(resolve => {
    const t0 = Date.now();
    const tick = () => {
      const { links, kind } = find();
      if (links.length >= n) return resolve({ ok: true, ...links[n - 1], kind });
      if (Date.now() - t0 > waitMs) {
        return resolve({ ok: false, reason: links.length ? `only ${links.length} results` : "no search results on this page",
                         diag: diag() });
      }
      setTimeout(tick, 300);
    };
    tick();
  });
}

function videoCtl(action) {
  const v = [...document.querySelectorAll("video, audio")].find(v => v.readyState > 0) || document.querySelector("video, audio");
  // Spotify's player can coexist with unrelated media elements; its own control owns playback.
  if (location.hostname === "open.spotify.com" || !v) {
    const b = document.querySelector('[data-testid="control-button-playpause"]');
    const before = b && (b.getAttribute("aria-label") || "").trim();
    if (!b || b.disabled || !/^(play|pause)$/i.test(before)) {
      const reason = !b ? "no playback control on this page" : b.disabled ? "playback control disabled" : "playback control state unknown";
      return Promise.resolve({ ok: false, reason, diag: `via=button label=${before || "missing"} disabled=${!!(b && b.disabled)}` });
    }
    const playing = /^pause$/i.test(before);
    const play = action === "play" || (action === "toggle" && !playing);
    const clicked = play !== playing;
    if (clicked) b.click();
    const after = (b.getAttribute("aria-label") || "").trim();
    return Promise.resolve({ ok: true, via: "button", clicked, before_label: before, after_label: after,
                             paused: /^play$/i.test(after) ? true : /^pause$/i.test(after) ? false : null });
  }
  const state = () => ({ paused: v.paused, ready_state: v.readyState, network_state: v.networkState,
                         error_code: v.error ? v.error.code : null });
  const before = state();
  const play = action === "play" || (action === "toggle" && v.paused);
  if (!play) { v.pause(); return Promise.resolve({ ok: true, paused: v.paused, via: "element", before, after: state() }); }
  return v.play().then(() => ({ ok: true, paused: v.paused, via: "element", before, after: state() }),
    e => ({ ok: false, reason: `play blocked: ${e.name}`, diag: JSON.stringify({ via: "element", before, after: state() }) }));
}

// The tab's own ytInitialData, only while it still belongs to the URL shown: YouTube searches in-page,
// which leaves the first search's data behind.
function pageYtData() {
  try {
    const nav = performance.getEntriesByType("navigation")[0];
    const d = window.wrappedJSObject.ytInitialData;
    if (!d || !nav || nav.name !== location.href) return { ok: false };
    return { ok: true, json: JSON.stringify(d) };
  } catch (e) {
    return { ok: false };
  }
}

async function ytTabVideos(t) {
  const p = await inPage(t.id, pageYtData);
  return p.ok ? ytVideos(JSON.parse(p.json)) : ytFetch(t.url);
}

async function currentTab(tabId) {
  if (Number.isInteger(tabId)) return browser.tabs.get(tabId);
  const w = await browser.windows.getLastFocused();
  const ts = await browser.tabs.query({ active: true, windowId: w.id });
  if (ts.length !== 1 || (await browser.windows.getLastFocused()).id !== w.id)
    throw new Error("current browser tab is uncertain");
  return ts[0];
}

// Wait until the tab has a loaded http(s) page (a fresh tab starts on about:blank).
async function loaded(tabId, deadline) {
  for (;;) {
    const t = await browser.tabs.get(tabId);
    if (t.status === "complete" && /^https?:/.test(t.url)) return t;
    if (Date.now() > deadline) throw new Error("page did not load");
    await new Promise(r => setTimeout(r, 150));
  }
}

async function inPage(tabId, fn, ...args) {
  const code = `(${fn})(${args.map(a => JSON.stringify(a)).join(", ")})`;
  const [r] = await browser.tabs.executeScript(tabId, { code });
  return r || { ok: false, reason: "no result from page" };
}

const OPS = {
  async ping() {
    return { ok: true, tabs: (await browser.tabs.query({})).length };
  },
  async list() {
    const before = (await browser.windows.getLastFocused()).id;
    const tabs = await browser.tabs.query({});
    const windowId = (await browser.windows.getLastFocused()).id;
    const active = tabs.filter(t => t.windowId === windowId && t.active);
    return {
      ok: true,
      current_window: windowId,
      current_tab: before === windowId && active.length === 1 ? active[0].id : null,
      creation_guard: true,
      ask_evidence: 1,
      navigation_focus: 1,
      coverage: { scope: "browser.tabs.query", zen_workspaces: "unknown", absence: "unconfirmed" },
      tabs: tabs.map(t => ({
        id: t.id, windowId: t.windowId, title: t.title, url: t.url,
        pinned: t.pinned, active: t.active, audible: t.audible, lastAccessed: t.lastAccessed,
      })),
    };
  },
  async activate({ tabId, windowId }) {
    if (windowId === undefined) { // preserve the existing Claude.ai activation contract
      const t = await browser.tabs.update(tabId, { active: true });
      await browser.windows.update(t.windowId, { focused: true });
      return { ok: true, tabId: t.id, title: t.title, windowId: t.windowId };
    }
    if (!Number.isInteger(tabId) || !Number.isInteger(windowId))
      return { ok: false, error: "bad browser target" };
    const before = await browser.tabs.get(tabId);
    if (!before || (windowId !== undefined && before.windowId !== windowId))
      return { ok: false, error: "browser target changed; ask again" };
    await browser.windows.get(before.windowId);
    const t = await browser.tabs.update(tabId, { active: true });
    if (t.windowId !== before.windowId)
      return { ok: true, tabId: t.id, windowId: t.windowId, target_validated: false };
    let requested = false;
    try {
      requested = true;
      await browser.windows.update(t.windowId, { focused: true });
    } catch (e) { /* activation succeeded; return its partial focus evidence */ }
    const after = await browser.tabs.get(tabId);
    const valid = !!after && after.windowId === before.windowId;
    const focused = await browser.windows.getLastFocused();
    return { ok: true, tabId: t.id, title: after?.title || "", url: after?.url || "", windowId: t.windowId,
      target_validated: valid, activation_confirmed: valid && after.active === true,
      browser_focus_requested: requested, browser_focus_observed: valid && focused.id === t.windowId && focused.focused === true };
  },
  async create({ url, windowId, expectedCurrentTab, active }) {
    if (!/^https?:\/\//i.test(String(url))) return { ok: false, error: "bad url" };
    if (active !== undefined && typeof active !== "boolean") return { ok: false, error: "bad active mode" };
    if (windowId !== undefined) {
      const current = await currentTab();
      if (current.windowId !== windowId || current.id !== expectedCurrentTab)
        return { ok: false, error: "browser target changed; ask to open again" };
    }
    const t = await browser.tabs.create({ url, ...(windowId !== undefined ? { windowId } : {}),
      ...(active !== undefined ? { active } : {}) });
    return { ok: true, tabId: t.id, windowId: t.windowId, title: t.title || "", url: t.url || url };
  },
  async search({ query, windowId, expectedCurrentTab, active }) {
    if (active !== undefined && typeof active !== "boolean") return { ok: false, error: "bad active mode" };
    if (windowId !== undefined) {
      const current = await currentTab();
      if (current.windowId !== windowId || current.id !== expectedCurrentTab)
        return { ok: false, error: "browser target changed; ask to open again" };
    }
    const t = await browser.tabs.create({ ...(windowId !== undefined ? { windowId } : {}),
      ...(active !== undefined ? { active } : {}) });
    await browser.search.search({ query: String(query), tabId: t.id });
    const after = await browser.tabs.get(t.id).catch(() => t);
    return { ok: true, tabId: t.id, windowId: t.windowId, title: after?.title || "", url: after?.url || "" };
  },
  async type({ tabId, text, send, expectedUrl }) {
    text = String(text);
    const t = await browser.tabs.get(tabId);
    const url = new URL(t.url);
    if (url.protocol !== "https:" || url.hostname !== "claude.ai" || t.url !== expectedUrl)
      return { ok: false, reason: "Claude.ai target changed" };
    const code = `(${typeInPage})(${JSON.stringify(text)}, ${JSON.stringify(CLAUDE_SELECTORS)}, ${JSON.stringify(!!send)}, ${JSON.stringify(expectedUrl)})`;
    const [r] = await browser.tabs.executeScript(tabId, { code });
    return r ? { ...r, tabId: t.id, windowId: t.windowId } : { ok: false, reason: "no result from page" };
  },
  async page_text({ tabId }) {
    const t = await currentTab(tabId);
    return { tabId: t.id, windowId: t.windowId, ...(await inPage(t.id, pageText, TEXT_CAP)) };
  },
  // Open the Nth result of the search page in `tabId` (default: current tab) in that same tab.
  // replace: the video takes the results page's history entry (zen.play leaves no results page behind).
  async click_result({ tabId, n, replace }) {
    n = Number(n);
    if (!Number.isInteger(n) || n < 1 || n > 50) return { ok: false, error: "bad result number" };
    const deadline = Date.now() + RESULT_WAIT_MS;
    const t = await loaded((await currentTab(tabId)).id, deadline);
    let r = null;
    if (isYtResults(t.url)) {   // a hidden window never renders the list: the page's data has it
      r = await inPage(t.id, resultLink, n, RESULT_SELECTORS, 1500);
      const v = r.ok ? null : (await ytTabVideos(t).catch(() => []))[n - 1];
      if (v) r = { ok: true, url: watchUrl(v.video_id), title: v.title, kind: "youtube" };
    }
    if (!r || !r.ok) r = await inPage(t.id, resultLink, n, RESULT_SELECTORS, Math.max(0, deadline - Date.now()));
    if (!r.ok) return r;
    if (!/^https:\/\//.test(r.url)) return { ok: false, reason: "result is not a web link" };
    await browser.tabs.update(t.id, { url: r.url, loadReplace: !!replace });
    return { ok: true, tabId: t.id, windowId: t.windowId, title: r.title, url: r.url, kind: r.kind };
  },
  // The Nth plain video of a YouTube search, from the results page's data (no tab, no rendering).
  async yt_first({ query, n = 1 }) {
    n = Number(n);
    query = String(query || "").trim();
    if (!query || !Number.isInteger(n) || n < 1 || n > 50) return { ok: false, error: "bad query or result number" };
    const v = (await ytFetch(YT_SEARCH + encodeURIComponent(query)))[n - 1];
    return v ? { ok: true, ...v } : { ok: false, reason: "no video in the results" };
  },
  async video({ tabId, action }) {
    if (!["play", "pause", "toggle"].includes(action)) return { ok: false, error: "bad video action" };
    const t = await currentTab(tabId);
    return { tabId: t.id, title: t.title, ...(await inPage(t.id, videoCtl, action)) };
  },
  async remove({ tabIds }) {
    // Defence in depth: jv already drops pinned tabs.
    const pinned = new Set((await browser.tabs.query({ pinned: true })).map(t => t.id));
    const ids = (tabIds || []).filter(i => Number.isInteger(i) && !pinned.has(i));
    if (ids.length) await browser.tabs.remove(ids);
    return { ok: true, closed: ids };
  },
};

async function handle(op, args) {
  const f = Object.hasOwn(OPS, op) ? OPS[op] : null;
  return f ? f(args || {}) : { ok: false, error: "unknown op" };
}

function connect() {
  const port = browser.runtime.connectNative("jarvis_zen");
  port.onMessage.addListener(async m => {
    const r = await handle(m.op, m.args).catch(e => ({ ok: false, error: String(e) }));
    port.postMessage({ id: m.id, ...r });
  });
  port.onDisconnect.addListener(p => {
    if (p.error) console.error("jarvis_zen disconnected:", p.error);
    setTimeout(connect, 2000);
  });
}

connect();
