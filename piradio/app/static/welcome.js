"use strict";
/* Welcome page: shown by the phone's "sign in to Wi-Fi" pop-up the first time
   it joins the PiRadio Wi-Fi. "Done" remembers the phone so it does not pop up again. */
const t = window.I18N.t;
document.title = t("w_title");
const $ = (s) => document.querySelector(s);

let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), kind === "bad" ? 6000 : 3500);
}
const fmtFreq = (f) => {
  const r = Math.round(f * 100) / 100;
  return Math.abs(r * 10 - Math.round(r * 10)) > 1e-6 ? r.toFixed(2) : r.toFixed(1);
};

const url = "http://" + (location.hostname || "192.168.4.1");
document.querySelectorAll(".url").forEach((el) => { el.textContent = url; });

/* "Open in the browser": Android opens Chrome with an intent link, iPhone (iOS 17+)
   with the x-safari- scheme; anything else just opens a new tab. */
(function browserButton() {
  const a = $("#w-browser"), ua = navigator.userAgent;
  if (/Android/i.test(ua)) a.href = `intent://${location.host}/#Intent;scheme=http;action=android.intent.action.VIEW;end`;
  else if (/iPhone|iPad|iPod/i.test(ua)) a.href = "x-safari-" + url + "/";
  else a.href = url + "/";
})();

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
async function copyURL() {
  if (await copyText(url)) toast(t("w_copied"), "ok");
  else toast(t("copy_fail", { url }), "bad");
}
$("#w-copy").addEventListener("click", copyURL);
document.querySelector("[data-copy]").addEventListener("click", copyURL);

async function status() {
  try {
    const st = await fetch("/api/state", { cache: "no-store" }).then((r) => r.json());
    const tx = st.tx, air = $("#w-air");
    air.className = "air " + ({ on_air: "on", starting: "starting", error: "err" }[tx.status] || "off");
    $("#w-air-text").textContent = tx.status === "on_air" ? t("w_air", { f: fmtFreq(tx.frequency) }) : tx.status === "starting" ? t("air_starting") : tx.status === "error" ? t("air_error") : t("air_off");
    const p = st.player, live = p.live;
    $("#w-now").textContent = live ? (live.kind === "bt" ? t("w_now_bt", { name: live.label }) : t("w_now_mic"))
      : p.state === "playing" && p.track ? "🎵 " + p.track.title
      : t("w_tune", { f: fmtFreq(tx.frequency) });
  } catch (_) { /* retry */ }
  setTimeout(status, 3000);
}

function finished() {
  $("#w-main").classList.add("hidden");
  $("#w-finished").classList.remove("hidden");
  window.scrollTo(0, 0);
}

$("#w-done").addEventListener("click", async () => {
  const b = $("#w-done");
  b.disabled = true;
  try {
    const r = await fetch("/api/captive/ack", { method: "POST" });
    if (!r.ok) throw new Error();
    finished();
    // Reload once: the phone checks the connection again after a page load,
    // sees it is "connected" now and turns its button into "Done".
    setTimeout(() => location.replace("/welcome?done=1"), 1200);
  } catch (_) {
    b.disabled = false;
    toast(t("w_retry"), "bad");
  }
});

if (new URLSearchParams(location.search).get("done") === "1") finished();
window.PiBT.mount($("#bt-box"), { toast, settings: false });
status();
