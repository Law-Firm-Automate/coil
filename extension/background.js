// Coil Timer background worker: time capture.
// Watches the active tab, turns "this tab was in front for N minutes" into segments, and posts segments of two
// minutes or more to POST /api/v1/capture every ten minutes (and when the popup opens). Coil then shows them at
// /time/suggestions with a guessed matter, where you accept or dismiss each one. Nothing is logged automatically.
//
// MV3 service workers are shut down between events, so all state lives in chrome.storage.local:
//   captureOn   boolean (default true)
//   allowlist   array of domains; empty means everything except the denylist
//   denylist    array of domains (default below)
//   current     {url, title, start} for the tab in front right now
//   segments    queue of {started_at, minutes, title, url, source} waiting to be posted
//   base, token connection settings shared with the popup

const MIN_MINUTES = 2;
const FLUSH_EVERY_MINUTES = 10;
const IDLE_SECONDS = 300;
const MAX_QUEUE = 500;
const DEFAULT_DENYLIST = [
  "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com", "reddit.com", "youtube.com",
  "netflix.com", "hulu.com", "twitch.tv", "pinterest.com", "espn.com", "amazon.com",
];

function store(keys) { return chrome.storage.local.get(keys); }
function save(obj) { return chrome.storage.local.set(obj); }

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, "").toLowerCase(); } catch (e) { return ""; }
}

function domainMatches(host, list) {
  return (list || []).some((d) => {
    d = String(d || "").trim().replace(/^www\./, "").toLowerCase();
    return d && (host === d || host.endsWith("." + d));
  });
}

async function allowed(url) {
  if (!url || !/^https?:/i.test(url)) return false;
  const host = hostOf(url);
  if (!host) return false;
  const s = await store(["allowlist", "denylist"]);
  const deny = Array.isArray(s.denylist) ? s.denylist : DEFAULT_DENYLIST;
  if (domainMatches(host, deny)) return false;
  const allow = Array.isArray(s.allowlist) ? s.allowlist.filter(Boolean) : [];
  if (allow.length) return domainMatches(host, allow);
  return true;
}

// Close the current segment (if any) and queue it when it is long enough.
async function endSegment() {
  const s = await store(["current", "segments"]);
  const cur = s.current;
  await save({ current: null });
  if (!cur || !cur.start) return;
  const minutes = Math.round((Date.now() - cur.start) / 60000);
  if (minutes < MIN_MINUTES) return;
  const segments = Array.isArray(s.segments) ? s.segments : [];
  segments.push({
    started_at: new Date(cur.start).toISOString(),
    minutes,
    title: String(cur.title || cur.url || "").slice(0, 300),
    url: String(cur.url || "").slice(0, 500),
    source: "extension",
  });
  while (segments.length > MAX_QUEUE) segments.shift();
  await save({ segments });
}

async function startSegment(tab) {
  if (!tab || !tab.url) return;
  const s = await store(["captureOn"]);
  if (s.captureOn === false) return;
  if (!(await allowed(tab.url))) return;
  await save({ current: { url: tab.url, title: tab.title || "", start: Date.now() } });
}

async function switchTo(tab) {
  await endSegment();
  await startSegment(tab);
}

async function activeTab() {
  try {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return tabs && tabs[0];
  } catch (e) { return null; }
}

async function restartFromActive() {
  await switchTo(await activeTab());
}

// ---- events
chrome.tabs.onActivated.addListener(async (info) => {
  try { await switchTo(await chrome.tabs.get(info.tabId)); } catch (e) { /* tab vanished */ }
});

chrome.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if (!tab || !tab.active) return;
  if (!change.url && !change.title) return;
  try {
    const s = await store(["current"]);
    const cur = s.current;
    // Same page, title arrived late: just update the title.
    if (cur && change.title && !change.url && cur.url === tab.url) {
      cur.title = change.title;
      await save({ current: cur });
      return;
    }
    await switchTo(tab);
  } catch (e) { /* ignore */ }
});

chrome.windows.onFocusChanged.addListener(async (windowId) => {
  try {
    if (windowId === chrome.windows.WINDOW_ID_NONE) await endSegment();
    else await restartFromActive();
  } catch (e) { /* ignore */ }
});

if (chrome.idle) {
  try { chrome.idle.setDetectionInterval(IDLE_SECONDS); } catch (e) { /* ignore */ }
  chrome.idle.onStateChanged.addListener(async (state) => {
    try {
      if (state === "active") await restartFromActive();
      else await endSegment();  // idle or locked: stop counting
    } catch (e) { /* ignore */ }
  });
}

chrome.runtime.onInstalled.addListener(async () => {
  try {
    const s = await store(["captureOn", "denylist", "allowlist"]);
    const init = {};
    if (s.captureOn === undefined) init.captureOn = true;
    if (!Array.isArray(s.denylist)) init.denylist = DEFAULT_DENYLIST;
    if (!Array.isArray(s.allowlist)) init.allowlist = [];
    if (Object.keys(init).length) await save(init);
    chrome.alarms.create("coil-flush", { periodInMinutes: FLUSH_EVERY_MINUTES });
    await restartFromActive();
  } catch (e) { /* ignore */ }
});

chrome.runtime.onStartup.addListener(async () => {
  try {
    chrome.alarms.create("coil-flush", { periodInMinutes: FLUSH_EVERY_MINUTES });
    await restartFromActive();
  } catch (e) { /* ignore */ }
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "coil-flush") return;
  try {
    // Roll the running segment over so long stays on one tab still reach Coil every ten minutes.
    const tab = await activeTab();
    await endSegment();
    await startSegment(tab);
    await flush();
  } catch (e) { /* ignore */ }
});

// ---- posting
async function flush() {
  const s = await store(["base", "token", "segments"]);
  const segments = Array.isArray(s.segments) ? s.segments : [];
  if (!segments.length) return { posted: 0 };
  if (!s.base || !s.token) return { posted: 0, error: "not connected" };
  const url = String(s.base).replace(/\/+$/, "") + "/api/v1/capture";
  let r;
  try {
    r = await fetch(url, {
      method: "POST",
      headers: { "Authorization": "Bearer " + s.token, "Content-Type": "application/json" },
      body: JSON.stringify({ segments }),
    });
  } catch (e) {
    return { posted: 0, error: "network: " + e.message };  // keep the queue, try again later
  }
  if (r.status === 401 || r.status === 403 || r.status === 400) {
    // Bad token, missing write scope or a malformed batch: drop the batch rather than retry forever.
    await save({ segments: [], lastError: "HTTP " + r.status });
    return { posted: 0, error: "HTTP " + r.status };
  }
  if (!r.ok) return { posted: 0, error: "HTTP " + r.status };
  const data = await r.json().catch(() => ({}));
  // Only drop what we sent; anything queued meanwhile stays.
  const after = await store(["segments"]);
  const remaining = (Array.isArray(after.segments) ? after.segments : []).slice(segments.length);
  await save({ segments: remaining, lastFlush: Date.now(), lastError: "" });
  return { posted: segments.length, created: data.created || 0, merged: data.merged || 0, pending: data.pending };
}

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (!msg || !msg.type) return false;
  (async () => {
    try {
      if (msg.type === "flush") {
        respond(await flush());
      } else if (msg.type === "capture-toggled") {
        if (msg.on) await restartFromActive(); else await endSegment();
        respond({ ok: true });
      } else if (msg.type === "queue") {
        const s = await store(["segments", "current", "lastFlush", "lastError"]);
        respond({ queued: (s.segments || []).length, current: s.current || null, lastFlush: s.lastFlush || 0,
                  lastError: s.lastError || "" });
      } else {
        respond({ error: "unknown message" });
      }
    } catch (e) { respond({ error: e.message }); }
  })();
  return true;  // async response
});
