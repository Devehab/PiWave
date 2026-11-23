"use strict";
/* PiRadio web UI — vanilla JS, works offline on the Pi's own Wi-Fi. */

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
  if (!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ("خطأ " + r.status));
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
      $("#air-text").textContent = "مش متصل بالجهاز";
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
  const label = { on_air: "على الهواء " + fmtFreq(f), starting: "جاري تشغيل البث…", error: "في مشكلة بالبث", off: "البث متوقف" }[tx.status] || tx.status;
  air.className = "air " + ({ on_air: "on", starting: "starting", error: "err" }[tx.status] || "off");
  $("#air-text").textContent = label;
  const tb = $("#tx-toggle");
  tb.classList.toggle("on", tx.on);
  $("span", tb).textContent = tx.on ? "البث شغال" : "شغّل البث";
  // frequency
  const shown = draftFreq ?? f;
  $("#freq-num").textContent = fmtFreq(shown);
  if (document.activeElement !== $("#freq-input")) $("#freq-input").value = fmtFreq(shown);
  $("#freq-pending").classList.toggle("hidden", draftFreq === null);
  if (draftFreq !== null) $("#freq-draft").textContent = fmtFreq(draftFreq);
  $("#car-hint").innerHTML = tx.status === "error"
    ? `<span class="bad-text">${esc(tx.error)}</span>${tx.retry_in ? ` · محاولة جديدة بعد ${tx.retry_in} ث` : ""}`
    : `بالسيارة: اضغط <b>FM</b> وحط التردد على <b dir="ltr">${fmtFreq(f)}</b>، وعلى الشاشة رح يطلع <b dir="ltr">${esc(st.rds.ps.trim())}</b>`;
  // now playing
  const t = p.track;
  $("#now-title").textContent = t ? t.title : "ما في شي شغال";
  $("#now-artist").textContent = t ? (t.artist || (p.state === "paused" ? "متوقف مؤقتاً" : "")) : (st.library.count ? "اضغط ▶ أو اختار أغنية" : "ارفع أغاني من تبويب «الأغاني»");
  $("#art").classList.toggle("playing", p.state === "playing");
  const dur = t && t.duration ? t.duration : 0;
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
  $$("#tracks li").forEach((li) => li.classList.toggle("current", t && li.dataset.id === t.id));
  $("#v-cur").textContent = st.version;
  renderGuides(st);
  if (!settingsFilled) fillSettings(st.settings);
}

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
  if (v < 87.5 && !confirm("التردد أقل من 87.5: معظم راديوهات السيارات ما بتوصل له. متأكد؟")) return;
  await act(() => api("/api/frequency", { frequency: v }), `تمام: ${fmtFreq(v)}. غيّر راديو السيارة لنفس التردد`);
  draftFreq = null;
});
$("#tx-toggle").addEventListener("click", () => {
  if (!S) return;
  if (S.tx.on && !confirm("توقف البث؟")) return;
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
  act(() => api("/api/player/modes", { repeat: next }), { off: "بدون تكرار", all: "تكرار الكل", one: "تكرار نفس الأغنية" }[next]);
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
  act(() => api("/api/player/tone", {}), "نغمة اختبار لمدة 20 ثانية: لازم تسمعها بالسيارة");
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
  const list = lib.tracks.filter((t) => !q || (t.title + " " + (t.artist || "") + " " + t.file).toLowerCase().includes(q));
  $("#lib-count").textContent = lib.tracks.length ? `(${lib.tracks.length})` : "";
  $("#lib-empty").classList.toggle("hidden", lib.tracks.length > 0);
  const cur = S && S.player.track ? S.player.track.id : null;
  $("#tracks").innerHTML = list.map((t) => `
    <li data-id="${esc(t.id)}" class="${t.id === cur ? "current" : ""}">
      <button class="t-btn" data-play="${esc(t.id)}" type="button" aria-label="تشغيل"><svg><use href="#i-play"/></svg></button>
      <div class="t-main" data-play="${esc(t.id)}">
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-sub">${esc(t.artist || "")}${t.artist ? " · " : ""}<span dir="ltr">${fmtTime(t.duration)}</span></div>
      </div>
      <button class="t-btn del" data-del="${esc(t.id)}" data-name="${esc(t.title)}" type="button" aria-label="حذف"><svg><use href="#i-trash"/></svg></button>
    </li>`).join("");
  $("#bad-tracks").innerHTML = (lib.bad || []).map((t) => `
    <li><div class="t-main"><div class="t-title">${esc(t.file)}</div><div class="t-sub bad-text">ملف مش صالح للتشغيل</div></div>
      <button class="t-btn del" data-del="${esc(t.id)}" data-name="${esc(t.file)}" type="button" aria-label="حذف"><svg><use href="#i-trash"/></svg></button></li>`).join("");
}
$("#search").addEventListener("input", renderLibrary);
document.addEventListener("click", (e) => {
  const play = e.target.closest("[data-play]");
  if (play) { act(() => api("/api/player/play", { id: play.dataset.play })); return; }
  const del = e.target.closest("[data-del]");
  if (del && confirm(`حذف «${del.dataset.name}» من الجهاز؟`)) {
    act(() => api("/api/library/" + encodeURIComponent(del.dataset.del), undefined, "DELETE"), "انحذفت").then(loadLibrary);
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
      if (x.status === 200 && j.ok) resolve(j); else reject(new Error(j.error || "فشل الرفع (" + x.status + ")"));
    };
    x.onerror = () => reject(new Error("انقطع الاتصال أثناء الرفع"));
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
  if (files.length) toast(`انرفع ${ok} من ${files.length}`, ok === files.length ? "ok" : "bad");
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
  row(c.transmitter ? "ok" : "no", "برنامج البث", c.transmitter ? `شغال على <b dir="ltr">${f}</b> ميغاهيرتز` : (c.error ? esc(c.error) : "البث متوقف. شغّله من تبويب الراديو"));
  if (!c.transmitter) row("wait", "الإشارة على الرجل 7", "بتنفحص لما يشتغل البث");
  else if (c.gpio === "DEV") row("ok", "الإشارة على الرجل 7", "وضع التجربة على الكمبيوتر");
  else row(c.pin_ok ? "ok" : "no", "الإشارة طالعة من الرجل 7 (GPIO4)", c.pin_ok ? "الرجل 7 مضبوطة على GPCLK0، يعني موجة الراديو طالعة عليها فعلياً" : "الرجل 7 مش طالع عليها الراديو (" + esc(c.gpio || "؟") + ")");
  if (c.audio_flowing && (c.playing || c.level > 0.02)) row("ok", "الصوت واصل للمرسل", "في صوت عم ينبث هلأ (شوف العدّاد)");
  else if (c.audio_flowing) row("wait", "الصوت واصل للمرسل", "البث شغال بس صامت. شغّل أغنية أو نغمة اختبار");
  else row(c.transmitter ? "no" : "wait", "الصوت واصل للمرسل", "ما في صوت داخل على المرسل");
  if (sys.undervoltage) row("no", "الكهربا", "الشاحن ضعيف هلأ! استعمل شاحن 5V / 2.5A وكيبل منيح");
  else if (sys.undervoltage_past) row("wait", "الكهربا", "صار ضعف بالكهربا قبل شوي. جرّب شاحن أقوى");
  else row("ok", "الكهربا", sys.undervoltage === null ? "ما في قراءة (وضع التجربة)" : "الكهربا كافية");
  if (sys.temp != null) row(sys.temp < 75 ? "ok" : "no", "الحرارة", `<span dir="ltr">${sys.temp}°C</span>`);
  row("wait", "آخر فحص: أذنك 👂", "شغّل نغمة الاختبار وتأكد إنك سامعها من سماعات السيارة");
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
  $("#car-steps").innerHTML = `
    <li>شغّل راديو السيارة واضغط زر <b>FM</b> (أو <b>RADIO</b> وبعدين FM1).</li>
    <li>استعمل زر <b>TUNE</b> أو الأسهم ‹ › عشان تبحث يدوياً لحد ما توصل <b dir="ltr">${fs}</b>. زر SEEK/SCAN ممكن يتخطى المحطة.</li>
    <li>لازم الوشيش يختفي ويطلع الصوت، وعلى الشاشة رح يطلع <b dir="ltr">${esc(ps)}</b>.</li>
    <li>اضغط مطوّل على زر حفظ (1 لـ 6) لحد ما تسمع «بيب». هيك المحطة بتنحفظ.</li>
    <li>إذا سامع محطة تانية أو تشويش: اختار تردد <b>فاضي</b> (بس وشيش) وغيّره من تبويب الراديو.${odd ? "" : " <b>ملاحظة:</b> السيارات الأمريكية بتقبل بس الأرقام الفردية (مثل 87.9 أو 107.9)."}</li>
    <li>إذا الراديو بيقفز لمحطة تانية لحاله، طفّي خاصية <b>AF</b> أو <b>REG</b> من إعدادات الراديو.</li>`;
  $("#ant-freq").textContent = fs + " MHz";
  const quarter = Math.round(29979 / f / 4);  // cm
  $("#ant-rows").innerHTML = `
    <tr class="no"><td>بدون سلك</td><td>أقل من متر</td><td><span class="tag mid">ضعيف</span></td></tr>
    <tr><td><b dir="ltr">10 cm</b></td><td>2 – 5 متر</td><td><span class="tag mid">إذا قريب من الراديو</span></td></tr>
    <tr class="best"><td><b dir="ltr">20 cm</b></td><td>5 – 15 متر</td><td><span class="tag good">الأفضل للسيارة</span></td></tr>
    <tr class="no"><td><span dir="ltr">${quarter} cm</span><br><small>(ربع الموجة)</small></td><td>50 – 100+ متر</td><td><span class="tag bad">لا تستعمله</span></td></tr>`;
  $("#ant-note").innerHTML = `الأرقام تقريبية، وبتعتمد على مكان الريسبيري بالسيارة ونوع الراديو. حط الريسبيري قريب من الراديو أو من الزجاج. السلك الطويل بيوصّل أبعد، بس بيوصل لسيارات غيرك وبيشوّش على محطات وأجهزة تانية، وهاد ممنوع بأغلب الدول بدون ترخيص.`;
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
  return `<svg viewBox="0 0 380 200" role="img" aria-label="رسمة توصيل السلك على الرجل 7">
    <rect x="22" y="14" width="340" height="132" rx="12" fill="#1f5c3a" stroke="#2f7d52"/>
    <rect x="8" y="58" width="26" height="46" rx="3" fill="#8b919c"/><text x="21" y="85" font-size="9" text-anchor="middle" fill="#111">SD</text>
    <rect x="${x0 - 9}" y="${yOut - 9}" width="${19 * dx + 18}" height="${yIn - yOut + 18}" rx="3" fill="#0d2f1e"/>
    ${pins}
    <circle cx="${p7x}" cy="${yIn}" r="9" fill="none" stroke="#f5a524" stroke-width="2.4"><animate attributeName="r" values="8;12;8" dur="1.6s" repeatCount="indefinite"/></circle>
    <path d="M${p7x} ${yIn} C ${p7x} ${yIn + 40}, ${p7x + 60} ${yIn + 30}, ${p7x + 110} ${yIn + 55}" fill="none" stroke="#f5a524" stroke-width="3" stroke-linecap="round" stroke-dasharray="1 0"/>
    <text x="${p7x + 116}" y="${yIn + 60}" font-size="12" fill="#f5a524" font-weight="700">السلك 20 سم</text>
    <text x="${p7x + 116}" y="${yIn + 76}" font-size="10" fill="#cfd3da">طرفه التاني حر</text>
    <text x="${x0}" y="${yIn + 18}" font-size="9" text-anchor="middle" fill="#ffb547">1</text>
    <text x="${p7x}" y="${yIn + 18}" font-size="10" text-anchor="middle" fill="#f5a524" font-weight="700">7</text>
    <text x="${x0}" y="${yOut - 11}" font-size="9" text-anchor="middle" fill="#ff8a8a">2·5V</text>
    <text x="${x0 + 2 * dx}" y="${yOut - 11}" font-size="9" text-anchor="middle" fill="#cfd3da">6·GND</text>
    <rect x="58" y="128" width="40" height="20" rx="3" fill="#8b919c"/><text x="78" y="162" font-size="9" text-anchor="middle" fill="#cfd3da">HDMI</text>
    <rect x="232" y="130" width="30" height="16" rx="3" fill="#8b919c"/><text x="247" y="162" font-size="9" text-anchor="middle" fill="#cfd3da">USB</text>
    <rect x="290" y="130" width="30" height="16" rx="3" fill="#34c77b"/><text x="305" y="162" font-size="9" text-anchor="middle" fill="#34c77b" font-weight="700">PWR IN</text>
    <text x="305" y="176" font-size="9" text-anchor="middle" fill="#34c77b">الكهربا هون</text>
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
}
$("#s-save-rds").addEventListener("click", () => {
  const ps = $("#s-ps").value.trim();
  if (!/^[ -~]{1,8}$/.test(ps)) { toast("اسم المحطة: من 1 لـ 8 أحرف إنجليزي أو أرقام", "bad"); return; }
  act(() => api("/api/settings", { station_name: ps, rds_song_text: $("#s-rt").checked, ps_scroll: $("#s-scroll").checked, radiotext: $("#s-radiotext").value }), "انحفظ");
});
$("#s-save-audio").addEventListener("click", () => act(() => api("/api/settings", {
  preemphasis: $("#s-pre").value, transmit_on_boot: $("#s-txboot").checked, autoplay: $("#s-autoplay").checked,
  pi_code: $("#s-pi").value.trim(), ppm: parseFloat($("#s-ppm").value || "0"),
}), "انحفظ"));

async function loadSystem() {
  try {
    const [sys, ver] = await Promise.all([api("/api/system"), api("/api/version")]);
    sysInfo = sys;
    if (document.activeElement !== $("#w-ssid")) $("#w-ssid").value = sys.wifi.ssid || "";
    if (document.activeElement !== $("#w-pass")) $("#w-pass").value = sys.wifi.password || "";
    const power = sys.undervoltage ? "ضعيفة الآن ⚠" : sys.undervoltage_past ? "صار ضعف قبل شوي" : sys.undervoltage === null ? "—" : "ممتازة";
    const up = sys.uptime != null ? fmtTime(sys.uptime) : "—";
    $("#sys").innerHTML = [
      ["الحرارة", sys.temp != null ? `<span dir="ltr">${sys.temp}°C</span>` : "—"],
      ["الكهربا", power],
      ["المساحة الفاضية", sys.disk ? `<span dir="ltr">${fmtBytes(sys.disk.free)}</span>` : "—"],
      ["مدة التشغيل", `<span dir="ltr">${up}</span>`],
      ["الواي فاي", sys.wifi_mode === "hotspot" ? "شبكة الراديو" : sys.wifi_mode === "home" ? "واي فاي البيت" : "—"],
      ["عنوان الصفحة", `<span dir="ltr">http://${esc(sys.wifi.ip)}</span>`],
    ].map(([k, v]) => `<li><span>${k}</span><span>${v}</span></li>`).join("");
    $("#v-cur").textContent = ver.running || ver.version || "?";
    $("#v-prev").textContent = ver.previous || "—";
    $("#v-rollback").disabled = !ver.previous;
    const r = ver.result, box = $("#v-result");
    if (ver.pending) {
      box.className = "note"; box.textContent = `جاري فحص الإصدار ${ver.pending.version} بعد التحديث…`;
    } else if (r && r.result === "ok") {
      box.className = "note ok"; box.textContent = `✓ آخر تحديث نجح: ${r.from || "—"} ← ${r.version}`;
    } else if (r && r.result === "rolled_back") {
      box.className = "note bad"; box.textContent = `✕ الإصدار ${r.from} ما اشتغل، فرجعنا للإصدار ${r.to}. السبب: ${r.reason || "—"}`;
    } else box.className = "note hidden";
  } catch (_) { /* offline */ }
}
$("#w-save").addEventListener("click", async () => {
  const ssid = $("#w-ssid").value.trim(), pass = $("#w-pass").value;
  if (!ssid) { toast("اكتب اسم الشبكة", "bad"); return; }
  if (pass.length < 8) { toast("كلمة السر لازم تكون 8 أحرف أو أكثر", "bad"); return; }
  if (!confirm(`رح تتغير الشبكة لـ «${ssid}» وموبايلك رح ينفصل. كمّل؟`)) return;
  try { await api("/api/wifi", { ssid, password: pass }); overlay(`اتصل بالشبكة الجديدة «${ssid}» وبعدين افتح http://192.168.4.1`); }
  catch (e) { toast(e.message, "bad"); }
});
$("#w-home").addEventListener("click", async () => {
  if (!confirm("الجهاز رح يطلع من شبكة الراديو ويتصل بواي فاي البيت لحد ما يعيد التشغيل. كمّل؟")) return;
  try { await api("/api/wifi/home", {}); overlay("الجهاز عم يتصل بواي فاي البيت. من شبكة البيت افتح http://piradio.local، وبعد إعادة التشغيل بترجع شبكة الراديو."); }
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
    if (Date.now() - started > 240000) { overlay("الجهاز لسا ما رجع. حدّث الصفحة بعد شوي."); return; }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 3000);
}
$("#update-input").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!confirm(`تثبيت التحديث «${file.name}»؟ الراديو رح يوقف ثواني ويرجع.`)) return;
  const box = $("#update-progress"), bar = $(".bar i", box), text = $("#update-text"), logEl = $("#update-log");
  box.classList.remove("hidden"); logEl.classList.add("hidden"); bar.style.width = "0";
  text.textContent = "جاري رفع الملف…";
  const fd = new FormData();
  fd.append("file", file, file.name);
  const x = new XMLHttpRequest();
  x.open("POST", "/api/update");
  x.upload.onprogress = (ev) => {
    if (!ev.lengthComputable) return;
    const p = ev.loaded / ev.total;
    bar.style.width = (p * 100).toFixed(0) + "%";
    if (p >= 1) text.textContent = "جاري الفحص والتثبيت… (ممكن ياخد دقيقة أو دقيقتين، لا تسكّر الصفحة)";
  };
  x.onload = () => {
    let j = {};
    try { j = JSON.parse(x.responseText); } catch (_) { /* ignore */ }
    if (x.status === 200 && j.ok) {
      text.textContent = `انثبت الإصدار ${j.version}`;
      waitForRestart(`تم تثبيت الإصدار ${j.version} ✓\nالجهاز عم يعيد تشغيل الراديو…`, j.version);
    } else {
      text.innerHTML = `<span class="bad-text">فشل التحديث: ${esc(j.error || x.status)}</span>. ما تغيّر شي، والإصدار الحالي لسا شغال.`;
      if (j.log && j.log.length) { logEl.textContent = j.log.join("\n"); logEl.classList.remove("hidden"); }
    }
  };
  x.onerror = () => { text.innerHTML = `<span class="bad-text">انقطع الاتصال أثناء الرفع.</span> جرّب مرة تانية.`; };
  x.send(fd);
});
$("#v-rollback").addEventListener("click", async () => {
  const prev = $("#v-prev").textContent;
  if (!confirm(`الرجوع للإصدار ${prev}؟`)) return;
  try { await api("/api/update/rollback", {}); waitForRestart(`جاري الرجوع للإصدار ${prev}…`, prev); }
  catch (e) { toast(e.message, "bad"); }
});
$("#p-reboot").addEventListener("click", async () => {
  if (!confirm("إعادة تشغيل الجهاز؟ (حوالي دقيقة)")) return;
  try { await api("/api/system/reboot", {}); waitForRestart("الجهاز عم يعيد التشغيل… إذا انفصل موبايلك، ارجع اتصل بشبكة الراديو."); }
  catch (e) { toast(e.message, "bad"); }
});
$("#p-off").addEventListener("click", async () => {
  if (!confirm("إطفاء الجهاز؟")) return;
  try { await api("/api/system/shutdown", {}); restarting = true; overlay("الجهاز عم يطفي. استنى 20 ثانية لحد ما يطفي الضو الأخضر، وبعدين افصل الكهربا."); }
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
  $("#rec-hint").textContent = "اضغط الزر الأحمر وابدأ احكي";
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
    toast("ما في صلاحية للمايكروفون. اسمح للمتصفح يستعمل المايك.", "bad");
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
    $("#rec-name").value = `تسجيل ${String(d.getHours()).padStart(2, "0")}-${String(d.getMinutes()).padStart(2, "0")}`;
    $("#rec-done").classList.remove("hidden");
    $("#rec-btn").classList.remove("recording");
    $("#rec-hint").textContent = "اسمعه، وإذا عاجبك ابعثه";
  };
  rec.mr.start(250);
  rec.t0 = Date.now();
  $("#rec-btn").classList.add("recording");
  $("#rec-done").classList.add("hidden");
  $("#rec-hint").textContent = "عم يسجّل… اضغط مرة تانية لتوقف";
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
  const name = ($("#rec-name").value.trim() || "تسجيل") + "." + ext;
  return new File([rec.blob], name, { type: mt });
}
async function sendRecording(play) {
  if (!rec.blob) return;
  const btns = [$("#rec-send"), $("#rec-save")];
  btns.forEach((b) => { b.disabled = true; });
  try {
    await uploadFile(recFile(), { play });
    if (play && $("#rec-loop").checked) await api("/api/player/modes", { repeat: "one" });
    toast(play ? "📡 التسجيل عم ينبث هلأ على الراديو" : "انحفظ التسجيل بالمكتبة", "ok");
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
  try { await uploadFile(f, { play: true }); toast("📡 التسجيل عم ينبث هلأ", "ok"); closeRecorder(); loadLibrary(); }
  catch (err) { toast(err.message, "bad"); }
});

/* ------------------------------------------------------------ start */
$("#pinout").innerHTML = pinoutSVG();
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });
const savedTab = store.get("tab");
showTab(["radio", "songs", "check", "settings"].includes(savedTab) ? savedTab : "radio");
if (location.hash === "#record") { showTab("radio"); openRecorder(); }
poll();
loadLibrary();
