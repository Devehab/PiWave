"use strict";
/* Bluetooth panel: status, pairing mode, paired phones (connect / disconnect /
   unpair), settings. Shared by the main page (sheet) and the welcome page. */
(function () {
  const t = window.I18N.t;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let j = null;
    try { j = await r.json(); } catch (_) { /* not json */ }
    if (!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ("Error " + r.status));
    return j;
  }

  function mount(box, opts = {}) {
    const toast = opts.toast || ((m) => window.alert(m));
    const full = opts.settings !== false;
    let st = null, timer = null, alive = true, busy = false, pairingEnd = 0, lastJust = null, moreOpen = false;

    async function load() {
      clearTimeout(timer);
      try {
        st = await fetch("/api/bluetooth", { cache: "no-store" }).then((r) => r.json());
        pairingEnd = st.pairing_left ? Date.now() + st.pairing_left * 1000 : 0;
        if (st.just_paired && st.just_paired !== lastJust) {
          lastJust = st.just_paired;
          toast(t("bt_just_paired", { name: st.just_paired }), "ok");
        }
        render();
      } catch (_) { /* offline: try again */ }
      if (alive) timer = setTimeout(load, st && (st.pairing_left || (st.install && st.install.running)) ? 1500 : 3000);
    }

    function render() {
      const a = document.activeElement;
      if (a && box.contains(a) && a.matches("input[type=text]")) return;   // don't clobber typing
      const more = box.querySelector("details.bt-more");
      if (more) moreOpen = more.open;
      box.innerHTML = html();
    }

    function html() {
      if (!st) return `<p class="muted">…</p>`;
      if (!st.installed) {
        const ins = st.install || {};
        return `
          <div class="note">${t("bt_not_installed")}</div>
          <ol class="steps small-text"><li>${t("bt_inst_1")}</li><li>${t("bt_inst_2")}</li><li>${t("bt_inst_3")}</li></ol>
          ${ins.running ? `<div class="note">${t("bt_installing")}</div>` : ""}
          ${ins.ok === true ? `<div class="note ok">${t("bt_installed")}</div>` : ""}
          ${ins.ok === false ? `<div class="note bad">${t("bt_inst_fail")}</div><pre dir="auto">${esc(ins.log)}</pre>` : ""}
          <button class="btn primary block" type="button" data-act="install" ${ins.running ? "disabled" : ""}>${t("bt_install")}</button>`;
      }
      let h = "";
      if (full) h += `<label class="switch"><input type="checkbox" data-set="bt_enabled" ${st.enabled ? "checked" : ""}><span>${t("bt_on")}</span></label>`;
      if (!st.enabled) return h + `<div class="note">${t("bt_is_off")}</div>`;
      if (!st.available) {
        return h + `<div class="note bad">${esc(st.error || t("bt_not_ready"))}</div>
          <button class="btn ghost block" type="button" data-act="retry">${t("bt_retry")}</button>`;
      }
      const devs = st.devices || [];
      const conn = devs.find((d) => d.connected);
      const sub = conn
        ? (st.on_air ? t("bt_on_air") : st.suppressed ? t("bt_suppressed") : t("bt_hint"))
        : t("bt_find", { name: esc(st.name) });
      h += `<div class="bt-state ${conn ? "on" : ""}"><span class="bt-ico"><svg><use href="#i-bt"/></svg></span>
              <div><b dir="auto">${conn ? t("bt_conn_to", { name: esc(conn.name) }) : t("bt_none")}</b><small>${sub}</small></div></div>`;
      if (st.pairing_left) {
        h += `<div class="pairing">
            <div class="pairing-head"><span class="pulse"></span><b>${t("bt_pairing_on")}</b><span class="muted" dir="ltr" data-left>${mmss(st.pairing_left)}</span></div>
            <ol class="steps small-text">
              <li>${t("bt_pair_1")}</li>
              <li>${t("bt_pair_2", { name: esc(st.name) })}</li>
              <li>${t("bt_pair_3")}</li>
            </ol>
            <button class="btn ghost block" type="button" data-act="pair-off">${t("bt_pair_off")}</button>
          </div>`;
      } else {
        h += `<button class="btn primary block" type="button" data-act="pair-on"><svg><use href="#i-bt"/></svg> ${t("bt_pair_on")}</button>`;
      }
      h += `<h3 class="sub-h">${t("bt_paired")}</h3>`;
      if (!devs.length) h += `<p class="muted small-text">${t("bt_no_devs")}</p>`;
      else {
        h += `<ul class="devs">` + devs.map((d) => `
          <li class="${d.connected ? "on" : ""}">
            <span class="dev-ico"><svg><use href="#i-phone"/></svg></span>
            <div class="dev-main"><b dir="auto">${esc(d.name)}</b><small>${d.connected ? (d.playing ? t("bt_dev_playing") : t("bt_dev_conn")) : t("bt_dev_off")}</small></div>
            ${d.connected
              ? `<button class="btn small ghost" type="button" data-act="disconnect" data-mac="${esc(d.mac)}">${t("bt_disconnect")}</button>`
              : `<button class="btn small" type="button" data-act="connect" data-mac="${esc(d.mac)}">${t("bt_connect")}</button>`}
            <button class="t-btn del" type="button" data-act="remove" data-mac="${esc(d.mac)}" data-name="${esc(d.name)}" title="${t("bt_unpair")}" aria-label="${t("bt_unpair")}"><svg><use href="#i-trash"/></svg></button>
          </li>`).join("") + `</ul>`;
      }
      if (st.error) h += `<p class="bad-text small-text">${esc(st.error)}</p>`;
      if (full) {
        h += `<label class="switch"><input type="checkbox" data-set="bt_takeover" ${st.takeover ? "checked" : ""}><span>${t("bt_takeover")}</span></label>
          <details class="bt-more" ${moreOpen ? "open" : ""}><summary class="muted">${t("bt_name_h")}</summary>
            <label class="field"><span>${t("bt_name_l")}</span><input type="text" data-name-input maxlength="30" dir="auto" value="${esc(st.name)}"></label>
            <button class="btn ghost small" type="button" data-act="save-name">${t("bt_name_save")}</button>
          </details>`;
      }
      return h;
    }

    async function onClick(e) {
      const b = e.target.closest("[data-act]");
      if (!b || busy) return;
      const act = b.dataset.act, mac = b.dataset.mac;
      if (act === "remove" && !window.confirm(t("bt_unpair_q", { name: b.dataset.name }))) return;
      busy = true;
      b.disabled = true;
      try {
        if (act === "pair-on") await post("/api/bluetooth/pairing", { on: true });
        else if (act === "pair-off") await post("/api/bluetooth/pairing", { on: false });
        else if (act === "connect") { await post("/api/bluetooth/connect", { mac }); toast(t("bt_connecting"), ""); }
        else if (act === "disconnect") await post("/api/bluetooth/disconnect", { mac });
        else if (act === "remove") { await post("/api/bluetooth/remove", { mac }); toast(t("bt_unpaired"), "ok"); }
        else if (act === "install") await post("/api/bluetooth/install", {});
        else if (act === "save-name") {
          const v = box.querySelector("[data-name-input]").value.trim();
          await post("/api/settings", { bt_name: v });
          box.querySelector("[data-name-input]").blur();
          toast(t("bt_name_saved"), "ok");
        }
      } catch (err) { toast(err.message, "bad"); }
      finally { busy = false; b.disabled = false; load(); }
    }

    async function onChange(e) {
      const i = e.target.closest("[data-set]");
      if (!i) return;
      try { await post("/api/settings", { [i.dataset.set]: i.checked }); }
      catch (err) { toast(err.message, "bad"); i.checked = !i.checked; }
      load();
    }

    const tick = setInterval(() => {
      const el = box.querySelector("[data-left]");
      if (el && pairingEnd) el.textContent = mmss(Math.max(0, Math.round((pairingEnd - Date.now()) / 1000)));
    }, 500);
    box.addEventListener("click", onClick);
    box.addEventListener("change", onChange);
    load();
    return {
      refresh: load,
      stop() {
        alive = false;
        clearTimeout(timer);
        clearInterval(tick);
        box.removeEventListener("click", onClick);
        box.removeEventListener("change", onChange);
      },
    };
  }

  window.PiBT = { mount, post };
})();
