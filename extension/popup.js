// Coil Timer popup. Talks to /api/v1 with a Bearer token stored in chrome.storage.local.
const $ = (id) => document.getElementById(id);
let cfg = { base: "", token: "" };
let picked = null;      // {id, number, name}
let timer = null;       // from /api/v1/timer
let tick = null;

function show(text, kind) {
  const m = $("msg");
  m.textContent = text; m.className = "msg " + (kind || ""); m.hidden = !text;
}

async function api(path, method, body) {
  if (!cfg.base || !cfg.token) throw new Error("Set the Coil URL and token first.");
  const r = await fetch(cfg.base.replace(/\/+$/, "") + "/api/v1" + path, {
    method: method || "GET",
    headers: { "Authorization": "Bearer " + cfg.token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

function fmt(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

function renderTimer() {
  clearInterval(tick);
  $("timerbox").hidden = !timer;
  $("startbox").hidden = !!timer;
  if (!timer) return;
  let base = timer.elapsed_seconds, started = Date.now();
  const paint = () => $("elapsed").textContent = fmt(base + (timer.paused ? 0 : Math.floor((Date.now() - started) / 1000)));
  $("timer-matter").textContent = timer.matter_number + (timer.description ? " · " + timer.description : "");
  paint(); tick = setInterval(paint, 1000);
}

function pick(m) {
  picked = m;
  $("picked").hidden = false;
  $("picked").textContent = m.number + " " + m.name + (m.client_name ? " (" + m.client_name + ")" : "");
  $("results").innerHTML = "";
}

async function search() {
  const q = $("q").value.trim();
  if (q.length < 2) { $("results").innerHTML = ""; return; }
  try {
    const data = await api("/matters?q=" + encodeURIComponent(q));
    $("results").innerHTML = "";
    data.matters.forEach((m) => {
      const li = document.createElement("li");
      li.textContent = m.number + " " + m.name + (m.client_name ? " · " + m.client_name : "");
      li.onclick = () => pick(m);
      $("results").appendChild(li);
    });
    if (!data.matters.length) $("results").innerHTML = "<li class='muted'>No open matters match.</li>";
  } catch (e) { show(e.message, "err"); }
}

async function refresh() {
  try {
    const me = await api("/me");
    $("who").textContent = me.user.name;
    timer = me.timer; renderTimer();
    $("setup").open = false;
    show("", "");
  } catch (e) { $("setup").open = true; show(e.message, "err"); }
}

let debounce;
$("q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(search, 250); });

$("save").onclick = async () => {
  cfg = { base: $("base").value.trim(), token: $("token").value.trim() };
  await chrome.storage.local.set(cfg);
  await refresh();
  if (!$("msg").hidden) return;
  show("Connected.", "ok");
};

$("start").onclick = async () => {
  if (!picked) return show("Pick a matter first.", "err");
  try {
    const data = await api("/timer/start", "POST", { matter_id: picked.id, description: $("desc").value.trim() });
    timer = data.timer; renderTimer(); show("Timer started on " + picked.number + ".", "ok");
  } catch (e) { show(e.message, "err"); }
};

$("stop").onclick = async () => {
  try {
    const body = picked ? { matter_id: picked.id } : {};
    const d = $("desc").value.trim(); if (d) body.description = d;
    const entry = await api("/timer/stop", "POST", body);
    timer = null; renderTimer();
    show("Logged " + entry.hours + " h on " + entry.matter_number + ".", "ok");
  } catch (e) { show(e.message, "err"); }
};

$("log").onclick = async () => {
  if (!picked) return show("Pick a matter first.", "err");
  const minutes = parseInt($("minutes").value, 10);
  if (!minutes || minutes < 1) return show("Enter minutes.", "err");
  try {
    const entry = await api("/time", "POST", { matter_id: picked.id, minutes, description: $("desc").value.trim() });
    $("minutes").value = ""; $("desc").value = "";
    show("Logged " + entry.hours + " h on " + entry.matter_number + ".", "ok");
  } catch (e) { show(e.message, "err"); }
};

// ---- time capture (background.js does the watching; this just shows state and settings)
function sendBg(msg) {
  return new Promise((resolve) => {
    try { chrome.runtime.sendMessage(msg, (r) => resolve(r || {})); } catch (e) { resolve({ error: e.message }); }
  });
}

function lines(v) { return String(v || "").split(/\n/).map((x) => x.trim()).filter(Boolean); }

async function loadCapture() {
  const s = await chrome.storage.local.get(["captureOn", "allowlist", "denylist"]);
  const on = s.captureOn !== false;
  $("capture-on").checked = on;
  $("capture-state").textContent = on ? "on" : "off";
  $("allowlist").value = (s.allowlist || []).join("\n");
  $("denylist").value = (s.denylist || []).join("\n");
  $("suggest-link").href = (cfg.base || "").replace(/\/+$/, "") + "/time/suggestions";
}

async function refreshPending() {
  if (!cfg.base || !cfg.token) return;
  try {
    await sendBg({ type: "flush" });  // push whatever the worker has queued before counting
    const p = await api("/capture/pending");
    $("pending").hidden = false;
    $("pending").textContent = p.pending ? p.pending + " suggestion" + (p.pending === 1 ? "" : "s") + " waiting (" +
      (p.minutes / 60).toFixed(1) + " h). Review at Time suggestions." : "No suggestions waiting.";
    const q = await sendBg({ type: "queue" });
    const bits = [];
    if (q.queued) bits.push(q.queued + " segment" + (q.queued === 1 ? "" : "s") + " queued");
    if (q.current && q.current.title) bits.push("tracking: " + q.current.title.slice(0, 40));
    if (q.lastError) bits.push("last post failed: " + q.lastError);
    $("queue").textContent = bits.join(" · ");
  } catch (e) { $("queue").textContent = e.message; }
}

$("capture-on").addEventListener("change", async () => {
  const on = $("capture-on").checked;
  await chrome.storage.local.set({ captureOn: on });
  $("capture-state").textContent = on ? "on" : "off";
  await sendBg({ type: "capture-toggled", on });
});

$("capture-save").onclick = async () => {
  await chrome.storage.local.set({ allowlist: lines($("allowlist").value), denylist: lines($("denylist").value) });
  show("Capture settings saved.", "ok");
};

(async () => {
  const saved = await chrome.storage.local.get(["base", "token"]);
  cfg = { base: saved.base || "", token: saved.token || "" };
  $("base").value = cfg.base; $("token").value = cfg.token;
  await refresh();
  await loadCapture();
  await refreshPending();
})();
