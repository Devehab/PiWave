"use strict";
/* PiRadio web UI — vanilla JS, works offline on the Pi's own Wi-Fi. */

const t = window.I18N.t;
document.title = t("app_title");
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;                       // last /api/state
let lib = { version: -1, tracks: [], bad: [] };
let draftFreq = null;
let seeking = false, volDragging = false;
let settingsFilled = false;
let sysInfo = null;
let firstVersion = null;
let restarting = false;

/* ------------------------------------------------------------ helpers */
async function api(path, body, method) {
  const opt = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let j = null;
  try { j = await r.json(); } catch (_) { /* not json */ }
  if (!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ("Error " + r.status));
  return j;
}
let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), kind === "bad" ? 6000 : 3500);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); poll(true); return r; }
  catch (e) { toast(e.message, "bad"); }
}
function fmtTime(s) {
  if (s == null || !isFinite(s)) return "0:00";
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`;
}
function fmtFreq(f) {
  const r = Math.round(f * 100) / 100;
  return Math.abs(r * 10 - Math.round(r * 10)) > 1e-6 ? r.toFixed(2) : r.toFixed(1);
}
function fmtBytes(b) {
  if (b == null) return "—";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (b >= 1024 && i < 3) { b /= 1024; i++; }
  return b.toFixed(i > 1 ? 1 : 0) + " " + u[i];
}
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
};
function overlay(text) {
  $("#overlay-text").textContent = text || "";
  $("#overlay").classList.toggle("hidden", !text);
}

/* ------------------------------------------------------------ tabs */
function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  store.set("tab", name);
  if (name === "check") pollCheck();
  if (name === "settings") loadSystem();
  window.scrollTo(0, 0);
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ------------------------------------------------------------ polling */
async function poll(once) {
  try {
    const st = await api("/api/state");
    if (restarting) return;
    render(st);
    if (st.library.version !== lib.version) loadLibrary();
  } catch (_) {
    if (!restarting) {
      $("#air").className = "air err";
      $("#air-text").textContent = t("air_offline");
    }
  } finally {
    if (!once) setTimeout(poll, document.hidden ? 4000 : 1000);
  }
}

function render(st) {
  S = st;
  if (firstVersion === null) firstVersion = st.version;
  const tx = st.tx, p = st.player, f = tx.frequency;
  // header pill
  const air = $("#air");
  const label = { on_air: t("air_on", { f: fmtFreq(f) }), starting: t("air_starting"), error: t("air_error"), off: t("air_off") }[tx.status] || tx.status;
  air.className = "air " + ({ on_air: "on", starting: "starting", error: "err" }[tx.status] || "off");
  $("#air-text").textContent = label;
  const tb = $("#tx-toggle");
  tb.classList.toggle("on", tx.on);
  $("span", tb).textContent = tx.on ? t("tx_on") : t("tx_off");
  // frequency
  const shown = draftFreq ?? f;
  $("#freq-num").textContent = fmtFreq(shown);
  if (document.activeElement !== $("#freq-input")) $("#freq-input").value = fmtFreq(shown);
  $("#freq-pending").classList.toggle("hidden", draftFreq === null);
  if (draftFreq !== null) $("#freq-draft").textContent = fmtFreq(draftFreq);
  $("#car-hint").innerHTML = tx.status === "error"
    ? `<span class="bad-text">${esc(tx.error)}</span>${tx.retry_in ? t("retry_in", { s: tx.retry_in }) : ""}`
    : t("car_hint", { f: fmtFreq(f), ps: esc(st.rds.ps.trim()) });
  // now playing
  const tr = p.track;
  $("#now-title").textContent = tr ? tr.title : t("now_none");
  $("#now-artist").textContent = p.live && tr
    ? (p.live.kind === "ptt" ? t("lib_wait_ptt") : t("lib_wait_bt"))
    : tr ? (tr.artist || (p.state === "paused" ? t("now_paused") : "")) : (st.library.count ? t("now_press") : t("now_upload"));
  renderLive(st);
  $("#art").classList.toggle("playing", p.state === "playing");
  const dur = tr && tr.duration ? tr.duration : 0;
  const seek = $("#seek");
  seek.max = dur || 100;
  seek.disabled = !dur;
  if (!seeking) { seek.value = Math.min(p.position, dur || 0); $("#t-pos").textContent = fmtTime(p.position); }
  $("#t-dur").textContent = fmtTime(dur);
  $("use", $("#b-play")).setAttribute("href", p.state === "playing" ? "#i-pause" : "#i-play");
  $("#b-shuffle").classList.toggle("active", p.shuffle);
  $("#b-repeat").classList.toggle("active", p.repeat !== "off");
  $("#repeat-one").classList.toggle("hidden", p.repeat !== "one");
  if (!volDragging) { $("#volume").value = p.volume; $("#vol-val").textContent = p.volume + "%"; }
  const lvl = Math.min(100, Math.round(st.level * 100));
  $("#meter").style.width = lvl + "%";
  $("#meter2").style.width = lvl + "%";
  const pe = $("#player-error");
  pe.textContent = p.error || "";
  pe.classList.toggle("hidden", !p.error);
  $$("#tracks li").forEach((li) => li.classList.toggle("current", tr && li.dataset.id === tr.id));
  $("#v-cur").textContent = st.version;
  renderGuides(st);
  if (!settingsFilled) fillSettings(st.settings);
}

/* ------------------------------------------------------------ live audio card */
function renderLive(st) {
  const live = st.player.live, bt = st.bt || {};
  const card = $("#live-card");
  const btConn = bt.installed && bt.enabled && bt.connected;
  $("#q-bt-sub").textContent = btConn ? t("q_bt_conn", { name: bt.connected }) : bt.pairing_left ? t("q_bt_pairing") : t("q_bt_sub");
  if (!live && !btConn) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const isPtt = live && live.kind === "ptt";
  card.classList.toggle("on-air", !!live);
  $("use", $("#live-ico")).setAttribute("href", isPtt ? "#i-mic" : "#i-bt");
  $("#live-badge").classList.toggle("hidden", !live);
  if (isPtt) {
    $("#live-title").textContent = t("live_mic");
    $("#live-sub").textContent = t("live_for", { t: fmtTime(live.seconds) });
  } else {
    const tr = bt.track;
    $("#live-title").textContent = t("bt_label", { name: live ? live.label : bt.connected });
    $("#live-sub").textContent = tr ? (tr.artist ? `${tr.title} · ${tr.artist}` : tr.title)
      : live ? t("bt_streaming")
      : bt.suppressed ? t("bt_suppressed")
      : t("bt_hint");
    const playing = tr ? tr.status === "playing" : !!live;
    $("use", $('[data-btmedia="toggle"]')).setAttribute("href", playing ? "#i-pause" : "#i-play");
  }
  $("#bt-ctl").classList.toggle("hidden", isPtt);
  $("#live-back").classList.toggle("hidden", !live || isPtt);
}
$$("[data-btmedia]").forEach((b) => b.addEventListener("click", () => {
  let cmd = b.dataset.btmedia;
  if (cmd === "toggle") {
    const tr = S && S.bt && S.bt.track;
    const playing = tr ? tr.status === "playing" : !!(S && S.player.live);
    cmd = playing ? "pause" : "play";
  }
  act(() => api("/api/bluetooth/media", { command: cmd }));
}));
$("#live-back").addEventListener("click", () => act(() => api("/api/player/play", {}), t("live_back_done")));

/* ------------------------------------------------------------ Bluetooth sheet */
let btPanel = null;
function openBT() {
  $("#bt-sheet").classList.remove("hidden");
  if (!btPanel) btPanel = window.PiBT.mount($("#bt-box"), { toast });
}
function closeBT() {
  $("#bt-sheet").classList.add("hidden");
  if (btPanel) { btPanel.stop(); btPanel = null; }
  if (location.hash === "#bt") history.replaceState(null, "", "/");
  poll(true);
}
$("#q-bt").addEventListener("click", openBT);
$("#s-bt-open").addEventListener("click", openBT);
$("#bt-close").addEventListener("click", closeBT);
$("#bt-sheet").addEventListener("click", (e) => { if (e.target.id === "bt-sheet") closeBT(); });

/* ------------------------------------------------------------ frequency */
function setDraft(v) {
  v = Math.round(Math.min(108, Math.max(76, v)) * 10) / 10;
  draftFreq = S && Math.abs(v - S.tx.frequency) < 0.001 ? null : v;
  if (S) render(S);
}
$$(".stepper button").forEach((b) => b.addEventListener("click", () => {
  setDraft((draftFreq ?? (S ? S.tx.frequency : 107.9)) + parseFloat(b.dataset.step));
}));
$("#freq-input").addEventListener("change", (e) => {
  const v = parseFloat(String(e.target.value).replace(",", "."));
  if (isFinite(v)) setDraft(v);
});
$("#freq-cancel").addEventListener("click", () => { draftFreq = null; if (S) render(S); });
$("#freq-apply").addEventListener("click", async () => {
  const v = draftFreq;
  if (v === null) return;
  if (v < 87.5 && !confirm(t("freq_low"))) return;
  await act(() => api("/api/frequency", { frequency: v }), t("freq_set", { f: fmtFreq(v) }));
  draftFreq = null;
});
$("#tx-toggle").addEventListener("click", () => {
  if (!S) return;
  if (S.tx.on && !confirm(t("tx_stop_q"))) return;
  act(() => api("/api/transmitter", { on: !S.tx.on }));
});

/* ------------------------------------------------------------ player */
$("#b-play").addEventListener("click", () => act(() => api("/api/player/toggle", {})));
$("#b-next").addEventListener("click", () => act(() => api("/api/player/next", {})));
$("#b-prev").addEventListener("click", () => act(() => api("/api/player/prev", {})));
$("#b-shuffle").addEventListener("click", () => S && act(() => api("/api/player/modes", { shuffle: !S.player.shuffle })));
$("#b-repeat").addEventListener("click", () => {
  if (!S) return;
  const next = { off: "all", all: "one", one: "off" }[S.player.repeat];
  act(() => api("/api/player/modes", { repeat: next }), { off: t("rep_off"), all: t("rep_all"), one: t("rep_one") }[next]);
});
const seekEl = $("#seek");
seekEl.addEventListener("input", () => { seeking = true; $("#t-pos").textContent = fmtTime(+seekEl.value); });
seekEl.addEventListener("change", async () => {
  await act(() => api("/api/player/seek", { position: +seekEl.value }));
  setTimeout(() => { seeking = false; }, 600);
});
const volEl = $("#volume");
volEl.addEventListener("input", () => { volDragging = true; $("#vol-val").textContent = volEl.value + "%"; });
volEl.addEventListener("change", async () => {
  await act(() => api("/api/player/volume", { volume: +volEl.value }));
  setTimeout(() => { volDragging = false; }, 600);
});
function playTone() {
  act(() => api("/api/player/tone", {}), t("tone_toast"));
}
$("#q-tone").addEventListener("click", playTone);
$("#c-tone").addEventListener("click", playTone);

/* ------------------------------------------------------------ library */
async function loadLibrary() {
  try {
    lib = await api("/api/library");
    renderLibrary();
  } catch (e) { /* next poll retries */ }
}
function renderLibrary() {
  const q = $("#search").value.trim().toLowerCase();
  const list = lib.tracks.filter((x) => !q || (x.title + " " + (x.artist || "") + " " + x.file).toLowerCase().includes(q));
  $("#lib-count").textContent = lib.tracks.length ? `(${lib.tracks.length})` : "";
  $("#lib-empty").classList.toggle("hidden", lib.tracks.length > 0);
  const cur = S && S.player.track ? S.player.track.id : null;
  $("#tracks").innerHTML = list.map((x) => `
    <li data-id="${esc(x.id)}" class="${x.id === cur ? "current" : ""}">
      <button class="t-btn" data-play="${esc(x.id)}" type="button" aria-label="${t("play")}"><svg><use href="#i-play"/></svg></button>
      <div class="t-main" data-play="${esc(x.id)}">
        <div class="t-title" dir="auto">${esc(x.title)}</div>
        <div class="t-sub">${esc(x.artist || "")}${x.artist ? " · " : ""}<span dir="ltr">${fmtTime(x.duration)}</span></div>
      </div>
      <button class="t-btn del" data-del="${esc(x.id)}" data-name="${esc(x.title)}" type="button" aria-label="${t("del")}"><svg><use href="#i-trash"/></svg></button>
    </li>`).join("");
  $("#bad-tracks").innerHTML = (lib.bad || []).map((x) => `
    <li><div class="t-main"><div class="t-title" dir="auto">${esc(x.file)}</div><div class="t-sub bad-text">${t("bad_file")}</div></div>
      <button class="t-btn del" data-del="${esc(x.id)}" data-name="${esc(x.file)}" type="button" aria-label="${t("del")}"><svg><use href="#i-trash"/></svg></button></li>`).join("");
}
$("#search").addEventListener("input", renderLibrary);
document.addEventListener("click", (e) => {
  const play = e.target.closest("[data-play]");
  if (play) { act(() => api("/api/player/play", { id: play.dataset.play })); return; }
  const del = e.target.closest("[data-del]");
  if (del && confirm(t("del_q", { name: del.dataset.name }))) {
    act(() => api("/api/library/" + encodeURIComponent(del.dataset.del), undefined, "DELETE"), t("deleted")).then(loadLibrary);
  }
});

/* ------------------------------------------------------------ uploads */
function uploadFile(file, { play = false, onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append("file", file, file.name || "recording");
    if (play) fd.append("play", "1");
    const x = new XMLHttpRequest();
    x.open("POST", "/api/upload");
    x.upload.onprogress = (e) => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    x.onload = () => {
      let j = {};
      try { j = JSON.parse(x.responseText); } catch (_) { /* ignore */ }
      if (x.status === 200 && j.ok) resolve(j); else reject(new Error(j.error || t("up_failed", { code: x.status })));
    };
    x.onerror = () => reject(new Error(t("up_lost")));
    x.send(fd);
  });
}
$("#file-input").addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  const box = $("#uploads");
  const rows = files.map((f) => {
    const d = document.createElement("div");
    d.className = "upload-item";
    d.innerHTML = `<span class="name"></span><span class="pct" dir="ltr">0%</span><div class="bar"><i></i></div>`;
    $(".name", d).textContent = f.name;
    box.prepend(d);
    return d;
  });
  let ok = 0;
  for (let i = 0; i < files.length; i++) {
    const d = rows[i];
    try {
      await uploadFile(files[i], { onProgress: (p) => { $(".bar i", d).style.width = (p * 100).toFixed(0) + "%"; $(".pct", d).textContent = (p * 100).toFixed(0) + "%"; } });
      d.classList.add("done"); $(".pct", d).textContent = "✓"; $(".bar i", d).style.width = "100%";
      ok++;
    } catch (err) {
      d.classList.add("fail"); $(".pct", d).textContent = "✗"; $(".name", d).textContent = files[i].name + ": " + err.message;
    }
  }
  if (files.length) toast(t("up_done", { ok, n: files.length }), ok === files.length ? "ok" : "bad");
  loadLibrary();
  setTimeout(() => $$(".upload-item.done", box).forEach((d) => d.remove()), 8000);
});

/* ------------------------------------------------------------ signal check */
let checkTimer = null;
async function pollCheck() {
  clearTimeout(checkTimer);
  if (!$("#tab-check").classList.contains("active")) return;
  try {
    const [c, sys] = await Promise.all([api("/api/check"), api("/api/system")]);
    sysInfo = sys;
    renderChecks(c, sys);
  } catch (_) { /* retry */ }
  checkTimer = setTimeout(pollCheck, 2000);
}
function renderChecks(c, sys) {
  const f = fmtFreq(c.frequency);
  const items = [];
  const row = (state, title, sub) => items.push(`<li><span class="dot ${state}">${state === "ok" ? "✓" : state === "no" ? "✕" : "!"}</span><div><b>${title}</b><small>${sub}</small></div></li>`);
  row(c.transmitter ? "ok" : "no", t("ck_tx"), c.transmitter ? t("ck_tx_on", { f }) : (c.error ? esc(c.error) : t("ck_tx_off")));
  if (!c.transmitter) row("wait", t("ck_pin"), t("ck_pin_wait"));
  else if (c.gpio === "DEV") row("ok", t("ck_pin"), t("ck_pin_dev"));
  else row(c.pin_ok ? "ok" : "no", t("ck_pin_out"), c.pin_ok ? t("ck_pin_ok") : t("ck_pin_bad", { g: esc(c.gpio || "?") }));
  if (c.audio_flowing && (c.playing || c.level > 0.02)) row("ok", t("ck_audio"), t("ck_audio_ok"));
  else if (c.audio_flowing) row("wait", t("ck_audio"), t("ck_audio_silent"));
  else row(c.transmitter ? "no" : "wait", t("ck_audio"), t("ck_audio_none"));
  const bt = S && S.bt;
  if (bt && bt.installed && bt.enabled) {
    row(bt.available ? "ok" : "no", t("ck_bt"), !bt.available ? t("ck_bt_notready") : bt.connected
      ? t("ck_bt_conn", { name: esc(bt.connected) }) + (bt.on_air ? t("ck_bt_air") : "") : t("ck_bt_idle"));
  }
  if (sys.undervoltage) row("no", t("ck_power"), t("ck_power_now"));
  else if (sys.undervoltage_past) row("wait", t("ck_power"), t("ck_power_past"));
  else row("ok", t("ck_power"), sys.undervoltage === null ? t("ck_power_na") : t("ck_power_ok"));
  if (sys.temp != null) row(sys.temp < 75 ? "ok" : "no", t("ck_temp"), `<span dir="ltr">${sys.temp}°C</span>`);
  row("wait", t("ck_ear"), t("ck_ear_sub"));
  $("#checks").innerHTML = items.join("");
  $("#txlog").textContent = (c.log || []).join("\n") || "—";
}

/* ------------------------------------------------------------ guides */
let lastGuideKey = "";
function renderGuides(st) {
  const f = st.tx.frequency, ps = st.rds.ps.trim() || "PI RADIO";
  const key = f + "|" + ps;
  if (key === lastGuideKey) return;
  lastGuideKey = key;
  const fs = fmtFreq(f);
  const odd = Math.round(f * 10) % 2 === 1;
  $("#car-steps").innerHTML = ["car_1", "car_2", "car_3", "car_4", "car_5", "car_6"]
    .map((k) => `<li>${t(k, { f: fs, ps: esc(ps) })}${k === "car_5" && !odd ? t("car_5_us") : ""}</li>`).join("");
  $("#ant-freq").textContent = fs + " MHz";
  const quarter = Math.round(29979 / f / 4);  // cm
  $("#ant-rows").innerHTML = `
    <tr class="no"><td>${t("ant_none")}</td><td>${t("ant_none_r")}</td><td><span class="tag mid">${t("ant_weak")}</span></td></tr>
    <tr><td><b dir="ltr">10 cm</b></td><td>${t("ant_10_r")}</td><td><span class="tag mid">${t("ant_10_tag")}</span></td></tr>
    <tr class="best"><td><b dir="ltr">20 cm</b></td><td>${t("ant_20_r")}</td><td><span class="tag good">${t("ant_20_tag")}</span></td></tr>
    <tr class="no"><td><span dir="ltr">${quarter} cm</span><br><small>${t("ant_q")}</small></td><td>${t("ant_q_r")}</td><td><span class="tag bad">${t("ant_q_tag")}</span></td></tr>`;
  $("#ant-note").textContent = t("ant_note");
}

function pinoutSVG() {
  const x0 = 46, dx = 14.2, yOut = 30, yIn = 45;
  let pins = "";
  for (let i = 0; i < 20; i++) {
    const odd = 2 * i + 1, even = 2 * i + 2;
    const fill = (n) => n === 7 ? "#f5a524" : n === 2 || n === 4 ? "#ff5d5d" : n === 1 ? "#ffb547" : [6, 9, 14, 20, 25, 30, 34, 39].includes(n) ? "#111" : "#c9a86a";
    pins += `<rect x="${x0 + i * dx - 4.5}" y="${yIn - 4.5}" width="9" height="9" rx="${odd === 1 ? 0 : 4.5}" fill="${fill(odd)}" stroke="#0007"/>`;
    pins += `<rect x="${x0 + i * dx - 4.5}" y="${yOut - 4.5}" width="9" height="9" rx="4.5" fill="${fill(even)}" stroke="#0007"/>`;
  }
  const p7x = x0 + 3 * dx;
  return `<svg viewBox="0 0 380 200" role="img" aria-label="${t("pin_aria")}">
    <rect x="22" y="14" width="340" height="132" rx="12" fill="#1f5c3a" stroke="#2f7d52"/>
    <rect x="8" y="58" width="26" height="46" rx="3" fill="#8b919c"/><text x="21" y="85" font-size="9" text-anchor="middle" fill="#111">SD</text>
    <rect x="${x0 - 9}" y="${yOut - 9}" width="${19 * dx + 18}" height="${yIn - yOut + 18}" rx="3" fill="#0d2f1e"/>
    ${pins}
    <circle cx="${p7x}" cy="${yIn}" r="9" fill="none" stroke="#f5a524" stroke-width="2.4"><animate attributeName="r" values="8;12;8" dur="1.6s" repeatCount="indefinite"/></circle>
    <path d="M${p7x} ${yIn} C ${p7x} ${yIn + 40}, ${p7x + 60} ${yIn + 30}, ${p7x + 110} ${yIn + 55}" fill="none" stroke="#f5a524" stroke-width="3" stroke-linecap="round" stroke-dasharray="1 0"/>
    <text x="${p7x + 116}" y="${yIn + 60}" font-size="12" fill="#f5a524" font-weight="700">${t("pin_wire")}</text>
    <text x="${p7x + 116}" y="${yIn + 76}" font-size="10" fill="#cfd3da">${t("pin_free")}</text>
    <text x="${x0}" y="${yIn + 18}" font-size="9" text-anchor="middle" fill="#ffb547">1</text>
    <text x="${p7x}" y="${yIn + 18}" font-size="10" text-anchor="middle" fill="#f5a524" font-weight="700">7</text>
    <text x="${x0}" y="${yOut - 11}" font-size="9" text-anchor="middle" fill="#ff8a8a">2·5V</text>
    <text x="${x0 + 2 * dx}" y="${yOut - 11}" font-size="9" text-anchor="middle" fill="#cfd3da">6·GND</text>
    <rect x="58" y="128" width="40" height="20" rx="3" fill="#8b919c"/><text x="78" y="162" font-size="9" text-anchor="middle" fill="#cfd3da">HDMI</text>
    <rect x="232" y="130" width="30" height="16" rx="3" fill="#8b919c"/><text x="247" y="162" font-size="9" text-anchor="middle" fill="#cfd3da">USB</text>
    <rect x="290" y="130" width="30" height="16" rx="3" fill="#34c77b"/><text x="305" y="162" font-size="9" text-anchor="middle" fill="#34c77b" font-weight="700">PWR IN</text>
    <text x="305" y="176" font-size="9" text-anchor="middle" fill="#34c77b">${t("pin_power")}</text>
  </svg>`;
}

/* ------------------------------------------------------------ settings */
function fillSettings(s) {
  settingsFilled = true;
  $("#s-ps").value = s.station_name;
  $("#s-rt").checked = s.rds_song_text;
  $("#s-scroll").checked = s.ps_scroll;
  $("#s-radiotext").value = s.radiotext;
  $("#s-pre").value = s.preemphasis;
  $("#s-txboot").checked = s.transmit_on_boot;
  $("#s-autoplay").checked = s.autoplay;
  $("#s-pi").value = s.pi_code;
  $("#s-ppm").value = s.ppm;
  $("#s-captive").value = s.captive_mode || "once";
  $("#s-bt-name-show").textContent = s.bt_name || "PiRadio";
}
$("#s-captive").addEventListener("change", (e) => act(() => api("/api/settings", { captive_mode: e.target.value }),
  e.target.value === "once" ? t("captive_once_t") : t("captive_always_t")));
$("#u-reset").addEventListener("click", () => {
  if (!confirm(t("reset_q"))) return;
  act(() => api("/api/captive/reset", {}), t("reset_done"));
});
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* http page: fallback below */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { /* old browser */ }
  document.body.removeChild(ta);
  return ok;
}
function pageURL() { return "http://" + ((sysInfo && sysInfo.wifi && sysInfo.wifi.ip) || location.hostname); }
$("#u-copy").addEventListener("click", async () => {
  const url = pageURL();
  if (await copyText(url)) toast(t("copied", { url }), "ok");
  else toast(t("copy_fail", { url }), "bad");
});
$("#s-save-rds").addEventListener("click", () => {
  const ps = $("#s-ps").value.trim();
  if (!/^[ -~]{1,8}$/.test(ps)) { toast(t("ps_bad"), "bad"); return; }
  act(() => api("/api/settings", { station_name: ps, rds_song_text: $("#s-rt").checked, ps_scroll: $("#s-scroll").checked, radiotext: $("#s-radiotext").value }), t("saved"));
});
$("#s-save-audio").addEventListener("click", () => act(() => api("/api/settings", {
  preemphasis: $("#s-pre").value, transmit_on_boot: $("#s-txboot").checked, autoplay: $("#s-autoplay").checked,
  pi_code: $("#s-pi").value.trim(), ppm: parseFloat($("#s-ppm").value || "0"),
}), t("saved")));

async function loadSystem() {
  try {
    const [sys, ver] = await Promise.all([api("/api/system"), api("/api/version")]);
    sysInfo = sys;
    if (document.activeElement !== $("#w-ssid")) $("#w-ssid").value = sys.wifi.ssid || "";
    if (document.activeElement !== $("#w-pass")) $("#w-pass").value = sys.wifi.password || "";
    const power = sys.undervoltage ? t("pw_weak") : sys.undervoltage_past ? t("pw_past") : sys.undervoltage === null ? "—" : t("pw_ok");
    const up = sys.uptime != null ? fmtTime(sys.uptime) : "—";
    $("#sys").innerHTML = [
      [t("kv_temp"), sys.temp != null ? `<span dir="ltr">${sys.temp}°C</span>` : "—"],
      [t("kv_power"), power],
      [t("kv_disk"), sys.disk ? `<span dir="ltr">${fmtBytes(sys.disk.free)}</span>` : "—"],
      [t("kv_uptime"), `<span dir="ltr">${up}</span>`],
      [t("kv_wifi"), sys.wifi_mode === "hotspot" ? t("wifi_hot") : sys.wifi_mode === "home" ? t("wifi_home") : "—"],
      [t("kv_url"), `<span dir="ltr">http://${esc(sys.wifi.ip)}</span>`],
      [t("kv_bt"), !S || !S.bt ? "—" : !S.bt.installed ? t("bt_not_inst") : !S.bt.enabled ? t("bt_off") : S.bt.connected ? t("q_bt_conn", { name: esc(S.bt.connected) }) : t("bt_ready")],
    ].map(([k, v]) => `<li><span>${k}</span><span>${v}</span></li>`).join("");
    $("#u-url").textContent = pageURL();
    $("#v-cur").textContent = ver.running || ver.version || "?";
    $("#v-prev").textContent = ver.previous || "—";
    $("#v-rollback").disabled = !ver.previous;
    const r = ver.result, box = $("#v-result");
    if (ver.pending) {
      box.className = "note"; box.textContent = t("upd_checking", { v: ver.pending.version });
    } else if (r && r.result === "ok") {
      box.className = "note ok"; box.textContent = t("upd_ok", { from: r.from || "—", v: r.version });
    } else if (r && r.result === "rolled_back") {
      box.className = "note bad"; box.textContent = t("upd_rolled", { from: r.from, to: r.to, reason: r.reason || "—" });
    } else box.className = "note hidden";
  } catch (_) { /* offline */ }
}
$("#w-save").addEventListener("click", async () => {
  const ssid = $("#w-ssid").value.trim(), pass = $("#w-pass").value;
  if (!ssid) { toast(t("w_need_ssid"), "bad"); return; }
  if (pass.length < 8) { toast(t("w_short"), "bad"); return; }
  if (!confirm(t("w_change_q", { ssid }))) return;
  try { await api("/api/wifi", { ssid, password: pass }); overlay(t("w_changed", { ssid })); }
  catch (e) { toast(e.message, "bad"); }
});
$("#w-home").addEventListener("click", async () => {
  if (!confirm(t("w_home_q"))) return;
  try { await api("/api/wifi/home", {}); overlay(t("w_home_done")); }
  catch (e) { toast(e.message, "bad"); }
});

/* ------------------------------------------------------------ update */
function waitForRestart(text, expectVersion) {
  restarting = true;
  overlay(text);
  const started = Date.now();
  let sawDown = false;
  const tick = async () => {
    try {
      const st = await fetch("/api/state", { cache: "no-store" }).then((r) => r.json());
      const ready = expectVersion ? st.version === expectVersion : sawDown || Date.now() - started > 15000;
      if (ready && Date.now() - started > 4000) { location.reload(); return; }
    } catch (_) { sawDown = true; }
    if (Date.now() - started > 240000) { overlay(t("not_back")); return; }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 3000);
}
$("#update-input").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!confirm(t("upd_q", { name: file.name }))) return;
  const box = $("#update-progress"), bar = $(".bar i", box), text = $("#update-text"), logEl = $("#update-log");
  box.classList.remove("hidden"); logEl.classList.add("hidden"); bar.style.width = "0";
  text.textContent = t("upd_uploading");
  const fd = new FormData();
  fd.append("file", file, file.name);
  const x = new XMLHttpRequest();
  x.open("POST", "/api/update");
  x.upload.onprogress = (ev) => {
    if (!ev.lengthComputable) return;
    const p = ev.loaded / ev.total;
    bar.style.width = (p * 100).toFixed(0) + "%";
    if (p >= 1) text.textContent = t("upd_installing");
  };
  x.onload = () => {
    let j = {};
    try { j = JSON.parse(x.responseText); } catch (_) { /* ignore */ }
    if (x.status === 200 && j.ok) {
      text.textContent = t("upd_installed", { v: j.version });
      waitForRestart(t("upd_restarting", { v: j.version }), j.version);
    } else {
      text.innerHTML = t("upd_failed", { err: esc(j.error || x.status) });
      if (j.log && j.log.length) { logEl.textContent = j.log.join("\n"); logEl.classList.remove("hidden"); }
    }
  };
  x.onerror = () => { text.innerHTML = t("upd_lost"); };
  x.send(fd);
});
$("#v-rollback").addEventListener("click", async () => {
  const prev = $("#v-prev").textContent;
  if (!confirm(t("rb_q", { v: prev }))) return;
  try { await api("/api/update/rollback", {}); waitForRestart(t("rb_doing", { v: prev }), prev); }
  catch (e) { toast(e.message, "bad"); }
});
$("#p-reboot").addEventListener("click", async () => {
  if (!confirm(t("reboot_q"))) return;
  try { await api("/api/system/reboot", {}); waitForRestart(t("reboot_doing")); }
  catch (e) { toast(e.message, "bad"); }
});
$("#p-off").addEventListener("click", async () => {
  if (!confirm(t("off_q"))) return;
  try { await api("/api/system/shutdown", {}); restarting = true; overlay(t("off_doing")); }
  catch (e) { toast(e.message, "bad"); }
});

/* ------------------------------------------------------------ voice recording */
const rec = { mr: null, chunks: [], stream: null, blob: null, mime: "", t0: 0, timer: null, ctx: null, raf: 0 };
function openRecorder() {
  $("#rec").classList.remove("hidden");
  const can = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder;
  $("#rec-ok").classList.toggle("hidden", !can);
  $("#rec-insecure").classList.toggle("hidden", !!can);
  $("#rec-https").href = "https://" + location.hostname + "/#record";
  resetRecorder();
}
function resetRecorder() {
  $("#rec-done").classList.add("hidden");
  $("#rec-time").textContent = "00:00";
  $("#rec-hint").textContent = t("rec_hint");
  $("#rec-btn").classList.remove("recording");
  rec.blob = null;
}
function closeRecorder() {
  if (rec.mr && rec.mr.state === "recording") rec.mr.stop();
  stopStream();
  $("#rec").classList.add("hidden");
  if (location.hash === "#record") history.replaceState(null, "", "/");
}
function stopStream() {
  if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop());
  rec.stream = null;
  clearInterval(rec.timer);
  cancelAnimationFrame(rec.raf);
  if (rec.ctx) { rec.ctx.close().catch(() => {}); rec.ctx = null; }
  $("#rec-level").style.width = "0";
}
async function startRecording() {
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia({ audio: { noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    toast(t("mic_denied"), "bad");
    return;
  }
  rec.mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"].find((t) => window.MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
  rec.mr = new MediaRecorder(rec.stream, rec.mime ? { mimeType: rec.mime } : undefined);
  rec.chunks = [];
  rec.mr.ondataavailable = (e) => { if (e.data && e.data.size) rec.chunks.push(e.data); };
  rec.mr.onstop = () => {
    stopStream();
    rec.blob = new Blob(rec.chunks, { type: rec.mr.mimeType || rec.mime || "audio/webm" });
    $("#rec-audio").src = URL.createObjectURL(rec.blob);
    const d = new Date();
    $("#rec-name").value = `${t("rec_default")} ${String(d.getHours()).padStart(2, "0")}-${String(d.getMinutes()).padStart(2, "0")}`;
    $("#rec-done").classList.remove("hidden");
    $("#rec-btn").classList.remove("recording");
    $("#rec-hint").textContent = t("rec_hint_done");
  };
  rec.mr.start(250);
  rec.t0 = Date.now();
  $("#rec-btn").classList.add("recording");
  $("#rec-done").classList.add("hidden");
  $("#rec-hint").textContent = t("rec_hint_rec");
  rec.timer = setInterval(() => {
    const s = Math.floor((Date.now() - rec.t0) / 1000);
    $("#rec-time").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    if (s >= 600) rec.mr.stop();   // 10 minutes max
  }, 250);
  try {
    rec.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const an = rec.ctx.createAnalyser();
    an.fftSize = 512;
    rec.ctx.createMediaStreamSource(rec.stream).connect(an);
    const buf = new Uint8Array(an.fftSize);
    const loop = () => {
      an.getByteTimeDomainData(buf);
      let peak = 0;
      for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
      $("#rec-level").style.width = Math.min(100, (peak / 128) * 140) + "%";
      rec.raf = requestAnimationFrame(loop);
    };
    loop();
  } catch (_) { /* level meter is optional */ }
}
$("#rec-btn").addEventListener("click", () => {
  if (rec.mr && rec.mr.state === "recording") rec.mr.stop(); else startRecording();
});
function recFile() {
  const mt = (rec.blob && rec.blob.type) || "";
  const ext = mt.includes("mp4") ? "m4a" : mt.includes("ogg") ? "ogg" : "webm";
  const name = ($("#rec-name").value.trim() || t("rec_default")) + "." + ext;
  return new File([rec.blob], name, { type: mt });
}
async function sendRecording(play) {
  if (!rec.blob) return;
  const btns = [$("#rec-send"), $("#rec-save")];
  btns.forEach((b) => { b.disabled = true; });
  try {
    await uploadFile(recFile(), { play });
    if (play && $("#rec-loop").checked) await api("/api/player/modes", { repeat: "one" });
    toast(play ? t("rec_on_air") : t("rec_saved"), "ok");
    closeRecorder();
    loadLibrary();
    poll(true);
  } catch (e) { toast(e.message, "bad"); }
  finally { btns.forEach((b) => { b.disabled = false; }); }
}
$("#rec-send").addEventListener("click", () => sendRecording(true));
$("#rec-save").addEventListener("click", () => sendRecording(false));
$("#rec-close").addEventListener("click", closeRecorder);
$("#rec").addEventListener("click", (e) => { if (e.target.id === "rec") closeRecorder(); });
$("#q-record").addEventListener("click", openRecorder);
$("#rec-capture").addEventListener("change", async (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!f) return;
  try { await uploadFile(f, { play: true }); toast(t("rec_on_air"), "ok"); closeRecorder(); loadLibrary(); }
  catch (err) { toast(err.message, "bad"); }
});

/* ------------------------------------------------------------ push-to-talk (live microphone) */
const ptt = { stream: null, ctx: null, node: null, sid: null, on: false, starting: null, queue: [], sending: false, t0: 0, timer: null, level: 0 };
function openPTT() {
  $("#ptt").classList.remove("hidden");
  const secure = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  const modern = !!(window.AudioWorkletNode && (window.AudioContext || window.webkitAudioContext));
  $("#ptt-ok").classList.toggle("hidden", !(secure && modern));
  $("#ptt-insecure").classList.toggle("hidden", !!secure);
  $("#ptt-old").classList.toggle("hidden", !secure || modern);
  $("#ptt-https").href = "https://" + location.hostname + "/#talk";
  pttUI("ready");
  if (secure && modern && !ptt.node) {   // ask for the microphone now, so pressing starts instantly
    pttMic().catch((e) => toast(e && e.name === "NotAllowedError" ? t("mic_denied") : t("mic_fail"), "bad"));
  }
}
async function closePTT() {
  await pttStop();
  if (ptt.stream) ptt.stream.getTracks().forEach((t) => t.stop());
  if (ptt.ctx) ptt.ctx.close().catch(() => {});
  ptt.stream = ptt.ctx = ptt.node = null;
  $("#ptt").classList.add("hidden");
  if (location.hash === "#talk") history.replaceState(null, "", "/");
}
function pttUI(mode) {
  const btn = $("#ptt-btn"), latch = $("#ptt-latch").checked;
  btn.classList.toggle("live", mode === "live");
  btn.classList.toggle("wait", mode === "wait");
  $("#ptt-status").className = "ptt-status " + mode;
  $("#ptt-state").textContent = { ready: t("ptt_ready"), wait: t("ptt_wait"), live: t("ptt_live"), lost: t("ptt_lost") }[mode] || mode;
  $("#ptt-label").textContent = mode === "live" ? (latch ? t("ptt_tap_stop") : t("ptt_release")) : (latch ? t("ptt_tap") : t("ptt_hold"));
  if (mode !== "live") { $("#ptt-time").textContent = ""; $("#ptt-level").style.width = "0"; }
}
async function pttMic() {
  if (ptt.node) return;
  if (ptt.micPromise) return ptt.micPromise;
  ptt.micPromise = pttMicOpen().finally(() => { ptt.micPromise = null; });
  return ptt.micPromise;
}
async function pttMicOpen() {
  ptt.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  const AC = window.AudioContext || window.webkitAudioContext;
  ptt.ctx = new AC();
  await ptt.ctx.audioWorklet.addModule("/static/ptt-worklet.js");
  const pre = S ? S.settings.preemphasis : "50";
  ptt.node = new AudioWorkletNode(ptt.ctx, "ptt-processor", { processorOptions: { tau: pre === "75" ? 75e-6 : pre === "50" ? 50e-6 : 0 } });
  ptt.node.port.onmessage = (e) => {
    ptt.level = Math.max(e.data.peak, ptt.level * 0.7);
    if (!ptt.on) return;
    $("#ptt-level").style.width = Math.min(100, ptt.level * 130) + "%";
    ptt.queue.push(new Int16Array(e.data.pcm));
    pttPump();
  };
  const mute = ptt.ctx.createGain();
  mute.gain.value = 0;
  ptt.ctx.createMediaStreamSource(ptt.stream).connect(ptt.node);
  ptt.node.connect(mute).connect(ptt.ctx.destination);
}
async function pttStart() {
  if (ptt.on || ptt.starting) return;
  pttUI("wait");
  ptt.starting = (async () => {
    try {
      await pttMic();
      if (ptt.ctx.state !== "running") await ptt.ctx.resume();
      ptt.queue = [];
      ptt.on = true;
      const j = await api("/api/ptt/start", {});
      ptt.sid = j.sid;
      ptt.t0 = Date.now();
      clearInterval(ptt.timer);
      ptt.timer = setInterval(() => { $("#ptt-time").textContent = fmtTime((Date.now() - ptt.t0) / 1000); }, 300);
      if (navigator.vibrate) navigator.vibrate(30);
      pttUI("live");
      pttPump();
      poll(true);
    } catch (e) {
      ptt.on = false;
      pttUI("ready");
      toast(e && e.name === "NotAllowedError" ? t("mic_denied") : (e.message || t("failed")), "bad");
    }
  })();
  await ptt.starting;
  ptt.starting = null;
}
async function pttStop() {
  if (ptt.starting) await ptt.starting;
  if (!ptt.on) return;
  ptt.on = false;
  clearInterval(ptt.timer);
  pttUI("ready");
  await pttPump(true);
  const sid = ptt.sid;
  ptt.sid = null;
  if (sid) { try { await api(`/api/ptt/${sid}/end`, {}); } catch (_) { /* it ends by itself */ } }
  poll(true);
}
// Sends queued audio in order, one request at a time (a slow request just makes the next one bigger).
async function pttPump(drain) {
  if (ptt.sending) { if (drain) { while (ptt.sending) await new Promise((r) => setTimeout(r, 20)); } else return; }
  while (ptt.sid && ptt.queue.length) {
    const parts = ptt.queue.splice(0);
    const all = new Int16Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) { all.set(p, o); o += p.length; }
    ptt.sending = true;
    try {
      const r = await fetch(`/api/ptt/${ptt.sid}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: all.buffer });
      if (r.status === 410) { ptt.on = false; ptt.sid = null; clearInterval(ptt.timer); pttUI("ready"); toast(t("ptt_ended"), "bad"); }
    } catch (_) {
      if (ptt.on) pttUI("lost");
    } finally { ptt.sending = false; }
    if (!drain && !ptt.on) break;
  }
}
const pttBtn = $("#ptt-btn");
pttBtn.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  if (ptt.ctx && ptt.ctx.state !== "running") ptt.ctx.resume().catch(() => {});
  if ($("#ptt-latch").checked) { if (ptt.on) pttStop(); else pttStart(); return; }
  try { pttBtn.setPointerCapture(e.pointerId); } catch (_) { /* old browser */ }
  pttStart();
});
["pointerup", "pointercancel", "lostpointercapture"].forEach((ev) => pttBtn.addEventListener(ev, () => {
  if (!$("#ptt-latch").checked) pttStop();
}));
pttBtn.addEventListener("contextmenu", (e) => e.preventDefault());
$("#ptt-latch").addEventListener("change", () => { if (!ptt.on) pttUI("ready"); });
$("#ptt-close").addEventListener("click", closePTT);
$("#ptt").addEventListener("click", (e) => { if (e.target.id === "ptt") closePTT(); });
$("#q-talk").addEventListener("click", openPTT);
document.addEventListener("visibilitychange", () => { if (document.hidden && ptt.on) pttStop(); });

/* ------------------------------------------------------------ language */
$$("[data-lang]").forEach((b) => {
  const on = b.dataset.lang === window.I18N.lang;
  b.classList.toggle("on", on);
  b.setAttribute("aria-checked", on ? "true" : "false");
  b.addEventListener("click", async () => {
    if (b.dataset.lang === window.I18N.lang) return;
    try { await api("/api/settings", { language: b.dataset.lang }); location.reload(); }
    catch (e) { toast(e.message, "bad"); }
  });
});

/* ------------------------------------------------------------ start */
$("#pinout").innerHTML = pinoutSVG();
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });
const savedTab = store.get("tab");
showTab(["radio", "songs", "check", "settings"].includes(savedTab) ? savedTab : "radio");
if (location.hash === "#record") { showTab("radio"); openRecorder(); }
if (location.hash === "#talk") { showTab("radio"); openPTT(); }
if (location.hash === "#bt") { showTab("radio"); openBT(); }
poll();
loadLibrary();
