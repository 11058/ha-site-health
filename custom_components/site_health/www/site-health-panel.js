// Site Health sidebar panel. Plain web component, no build step: it talks to
// the integration over the websocket API (site_health/overview, /subscribe,
// /family_add, /family_remove) and draws with Home Assistant's theme vars.

const SEGMENTS = [
  ["family", "Семья"],
  ["guest", "Гости"],
  ["iot", "Умный дом"],
  ["video", "Видео"],
  ["admin", "Админ"],
  ["mgmt", "Управление"],
  ["switches", "Свитчи"],
  ["dmz", "DMZ"],
  ["other", "Прочее"],
];
const SEG_LABEL = Object.fromEntries(SEGMENTS);

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function dur(s) {
  if (s === null || s === undefined) return "—";
  s = Math.round(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
    m = Math.floor((s % 3600) / 60);
  if (d) return `${d} д ${h} ч`;
  if (h) return `${h} ч ${m} мин`;
  if (m) return `${m} мин`;
  return `${s} с`;
}
const mbps = (bps) => (bps === null || bps === undefined ? "—" : (bps / 1e6).toFixed(1));
function signalClass(dbm) {
  if (dbm === null || dbm === undefined) return "";
  if (dbm >= -65) return "good";
  if (dbm >= -75) return "warn";
  return "bad";
}
const WAN_STATUS = { bound: "подключён", searching: "поиск DHCP", "searching...": "поиск DHCP",
  requesting: "запрос адреса", rebinding: "продление", stopped: "остановлен" };
// Comments written by this integration look like "Family: Name -> VLAN 30 (HA)".
const cleanComment = (c) => String(c || "").replace(/^Family:\s*/, "").replace(/\s*->\s*VLAN \d+.*$/, "").trim();
const dot = (ok) =>
  `<span class="dot ${ok === true ? "ok" : ok === false ? "bad" : "unk"}"></span>`;

class SiteHealthPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._sites = [];
    this._seg = "family";
    this._filter = "";
    this._onlyOnline = true;
    this._busy = new Set();
    this._error = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }
  set narrow(v) { this._narrow = v; }
  set panel(v) { this._panel = v; }

  async _start() {
    this._renderShell();
    await this._load();
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        () => this._scheduleLoad(), { type: "site_health/subscribe" });
    } catch (e) { /* older core: fall back to polling */ }
    this._timer = setInterval(() => this._load(), 60000);
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    clearInterval(this._timer);
    this._hass = undefined;
  }

  _scheduleLoad() {
    clearTimeout(this._debounce);
    this._debounce = setTimeout(() => this._load(), 500);
  }

  async _load() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "site_health/overview" });
      this._sites = res.sites || [];
      this._error = "";
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._render();
  }

  async _family(site, mac, name, add) {
    this._busy.add(mac);
    this._render();
    try {
      await this._hass.callWS({
        type: add ? "site_health/family_add" : "site_health/family_remove",
        mac, entry_id: site.entry_id, ...(add ? { name } : {}),
      });
    } catch (e) {
      this._error = `${mac}: ${e.message || e}`;
    }
    this._busy.delete(mac);
    await this._load();
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Здоровье сети</div>
      </div>
      <div id="body" class="body"><div class="muted">Загрузка…</div></div>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) { menu.hass = this._hass; menu.narrow = this._narrow; }
  }

  _render() {
    const body = this.shadowRoot.getElementById("body");
    if (!body) return;
    if (!this._sites.length) {
      body.innerHTML = this._error
        ? `<div class="error">${esc(this._error)}</div>`
        : `<div class="muted">Нет настроенных объектов. Добавьте интеграцию Site Health.</div>`;
      return;
    }
    body.innerHTML = (this._error ? `<div class="error">${esc(this._error)}</div>` : "")
      + this._sites.map((s) => this._site(s)).join("");
    this._bind(body);
  }

  _site(s) {
    return `
      <h1>${esc(s.title)}</h1>
      <div class="grid">
        ${this._channels(s)}
        ${this._devices(s)}
        ${this._radio(s)}
      </div>
      ${this._clients(s)}`;
  }

  _channels(s) {
    const wans = s.wans.map((w) => `
      <tr>
        <td>${dot(w.disabled ? null : w.bound)} ${esc(w.comment || w.interface)}</td>
        <td>${esc(w.disabled ? "отключён" : (WAN_STATUS[w.status] || w.status))}<div class="sub">${esc(w.address)}</div></td>
        <td class="num">↓ ${mbps(w.rx_bps)}<br>↑ ${mbps(w.tx_bps)}</td>
      </tr>`).join("");
    const probes = s.probes.map((p) => `
      <tr>
        <td>${dot(p.up)} ${esc(p.name)}</td>
        <td class="sub">${esc(p.host)}</td>
        <td class="num">${p.rtt_ms !== null ? `${p.rtt_ms.toFixed(1)} мс` : ""}${
          p.loss_pct ? `<div class="sub bad-t">потери ${p.loss_pct}%</div>` : ""}</td>
      </tr>`).join("");
    const tunnels = s.tunnels.map((t) => `
      <tr>
        <td>${dot(t.dial_in && !t.connected ? null : t.connected)} ${esc(t.name)}</td>
        <td class="sub">${t.dial_in ? "VPN-клиент" : "WireGuard"} ${esc(t.interface)}</td>
        <td class="num">${t.handshake_age_s !== null ? `${dur(t.handshake_age_s)} назад` : "нет"}</td>
      </tr>`).join("");
    return `
      <section class="card">
        <h2>Каналы связи</h2>
        <table>${wans}${tunnels}</table>
        ${probes ? `<h3>Проверки (netwatch)</h3><table>${probes}</table>` : ""}
      </section>`;
  }

  _devices(s) {
    const row = (d, role) => {
      const sys = d.system || {};
      return `
        <tr>
          <td>${dot(d.online)} <b>${esc(d.name)}</b><div class="sub">${esc(role)} · ${esc(d.host)}</div></td>
          <td class="sub">${esc(sys.model || sys.board || "")}<br>${esc(sys.version || "")}</td>
          <td class="num">${sys.cpu_load ?? "—"}% CPU${
            sys.temperature !== null && sys.temperature !== undefined ? `<br>${sys.temperature} °C` : ""}</td>
          <td class="num sub">${d.online ? `up ${dur(sys.uptime_s)}` : esc(d.error || "нет связи")}</td>
        </tr>`;
    };
    return `
      <section class="card">
        <h2>Устройства</h2>
        <table>
          ${row(s.core, "главный роутер")}
          ${s.devices.map((d) => row(d, "MikroTik")).join("")}
        </table>
      </section>`;
  }

  _radio(s) {
    const rows = [];
    for (const d of s.devices) {
      for (const l of d.radio_links || []) {
        rows.push(`
          <tr>
            <td><b>${esc(d.name)}</b> → ${esc(l.peer_name || l.mac)}<div class="sub">${esc(l.interface)} ${esc(l.ssid)}</div></td>
            <td class="num ${signalClass(l.signal_dbm)}">${l.signal_dbm ?? "—"} дБм</td>
            <td class="num sub">${l.tx_rate_mbps ?? "—"} / ${l.rx_rate_mbps ?? "—"} Мбит/с</td>
            <td class="num sub">${dur(l.uptime_s)}</td>
          </tr>`);
      }
    }
    if (!rows.length) return "";
    return `<section class="card"><h2>Радиомосты</h2><table>${rows.join("")}</table></section>`;
  }

  _clients(s) {
    const admin = this._hass.user && this._hass.user.is_admin;
    const all = s.clients;
    const counts = {};
    for (const c of all) {
      const k = c.segment_kind || (c.family ? "family" : "other");
      if (c.online) counts[k] = (counts[k] || 0) + 1;
    }
    const tabs = SEGMENTS.filter(([k]) => counts[k] || k === "family" || k === "guest")
      .map(([k, label]) => `<button class="tab ${this._seg === k ? "on" : ""}" data-seg="${k}">
        ${esc(label)} <span class="badge">${counts[k] || 0}</span></button>`).join("");
    const q = this._filter.toLowerCase();
    const list = all.filter((c) => {
      const kind = c.segment_kind || (c.family ? "family" : "other");
      if (kind !== this._seg && !(this._seg === "family" && c.family)) return false;
      if (this._onlyOnline && !c.online && !c.family) return false;
      if (!q) return true;
      return [c.hostname, c.comment, c.ip, c.mac, c.ssid].join(" ").toLowerCase().includes(q);
    }).sort((a, b) => (b.online - a.online) || (a.ip || "~").localeCompare(b.ip || "~",
      undefined, { numeric: true }));

    const rows = list.map((c) => {
      const name = cleanComment(c.comment) || c.hostname || "";
      let action = "";
      if (admin && (c.family || c.wireless || c.segment_kind === "guest")) {
        const busy = this._busy.has(c.mac);
        const ent = `data-entry="${esc(s.entry_id)}" data-mac="${esc(c.mac)}"`;
        if (busy) {
          action = `<span class="sub">…</span>`;
        } else if (this._editing === c.mac) {
          action = `<input class="nm" id="nm" value="${esc(this._editName ?? name)}" placeholder="Имя (латиницей)">
            <button class="act primary" data-do="add" ${ent}>Добавить</button>
            <button class="act" data-do="cancel">✕</button>`;
        } else if (this._confirming === c.mac) {
          action = `<span class="sub">В гостевую сеть?</span>
            <button class="act primary" data-do="remove" ${ent}>Да</button>
            <button class="act" data-do="cancel">Нет</button>`;
        } else if (c.family) {
          action = `<button class="act" data-do="ask-remove" ${ent}>В гости</button>`;
        } else if (c.access_vlan === null || c.access_vlan === undefined) {
          action = `<button class="act primary" data-do="ask-add" ${ent}>В домашние</button>`;
        } else {
          action = `<span class="sub">VLAN ${c.access_vlan}</span>`;
        }
      }
      return `
        <tr class="${c.online ? "" : "off"}">
          <td>${dot(c.online ? true : null)} <b>${esc(name || c.mac)}</b>
            <div class="sub">${esc(c.mac)}${c.static_lease ? " · статика" : ""}</div></td>
          <td>${esc(c.ip || "—")}<div class="sub">${esc(SEG_LABEL[c.segment_kind] || "")}</div></td>
          <td class="sub c-if">${c.wireless ? `📶 ${esc(c.ssid || c.wifi_interface)}` : esc(c.interface || "")}</td>
          <td class="num c-sig ${signalClass(c.signal_dbm)}">${c.signal_dbm ?? ""}</td>
          <td class="num sub">${c.online ? "в сети" : c.last_seen_s !== null ? `${dur(c.last_seen_s)} назад` : "не видели"}</td>
          <td class="num">${action}</td>
        </tr>`;
    }).join("");

    const hint = this._seg === "guest"
      ? `<div class="hint">Новые устройства попадают в гостевую сеть. «В домашние» — добавит MAC в access-list роутера (VLAN ${s.family_vlan}) и переподключит устройство. У iPhone/Android указан приватный MAC этой сети.</div>`
      : "";
    return `
      <section class="card wide">
        <h2>Подключённые устройства</h2>
        <div class="tabs">${tabs}</div>
        <div class="filters">
          <input id="q" placeholder="Поиск: имя, IP, MAC" value="${esc(this._filter)}">
          <label><input type="checkbox" id="online" ${this._onlyOnline ? "checked" : ""}> только в сети</label>
        </div>
        ${hint}
        <table class="clients">${rows || `<tr><td class="muted">Нет устройств</td></tr>`}</table>
      </section>`;
  }

  _bind(root) {
    root.querySelectorAll("[data-seg]").forEach((b) =>
      b.addEventListener("click", () => { this._seg = b.dataset.seg; this._render(); }));
    const q = root.querySelector("#q");
    if (q) {
      q.addEventListener("input", () => {
        this._filter = q.value;
        clearTimeout(this._qt);
        this._qt = setTimeout(() => {
          this._render();
          const nq = this.shadowRoot.querySelector("#q");
          if (nq) { nq.focus(); nq.setSelectionRange(nq.value.length, nq.value.length); }
        }, 250);
      });
    }
    const on = root.querySelector("#online");
    if (on) on.addEventListener("change", () => { this._onlyOnline = on.checked; this._render(); });
    const site = (el) => this._sites.find((x) => x.entry_id === el.dataset.entry);
    const nmInput = root.querySelector("#nm");
    // Keep what was typed across the re-render every poll triggers.
    if (nmInput) nmInput.addEventListener("input", () => { this._editName = nmInput.value; });
    root.querySelectorAll("[data-do]").forEach((b) => b.addEventListener("click", () => {
      const mac = b.dataset.mac;
      switch (b.dataset.do) {
        case "ask-add": this._editing = mac; this._editName = undefined; this._confirming = null; break;
        case "ask-remove": this._confirming = mac; this._editing = null; break;
        case "cancel": this._editing = this._confirming = null; break;
        case "add": {
          const nm = this.shadowRoot.getElementById("nm");
          this._editing = null;
          this._family(site(b), mac, nm ? nm.value : "", true);
          return;
        }
        case "remove":
          this._confirming = null;
          this._family(site(b), mac, "", false);
          return;
      }
      this._render();
      const nm = this.shadowRoot.getElementById("nm");
      if (nm) nm.focus();
    }));
  }
}

const STYLE = `
  :host { display:block; background: var(--primary-background-color); color: var(--primary-text-color);
    min-height: 100vh; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; gap:8px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .title { font-size: 20px; }
  .body { padding: 16px; max-width: 1400px; margin: 0 auto; }
  h1 { font-size: 22px; font-weight: 500; margin: 8px 0 12px; }
  h2 { font-size: 16px; font-weight: 500; margin: 0 0 10px; }
  h3 { font-size: 13px; font-weight: 500; margin: 14px 0 6px; color: var(--secondary-text-color); }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr)); gap: 16px; margin-bottom: 16px; }
  .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); padding: 16px; overflow-x:auto;
    min-width: 0; max-width: 100%; box-sizing: border-box; }
  table { width:100%; border-collapse: collapse; font-size: 14px; }
  td { padding: 6px 6px; border-bottom: 1px solid var(--divider-color); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .num { text-align: right; white-space: nowrap; }
  .sub { font-size: 12px; color: var(--secondary-text-color); }
  .muted { color: var(--secondary-text-color); padding: 8px; }
  .error { background: var(--error-color); color: #fff; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align: middle; }
  .dot.ok { background: var(--success-color, #2e7d32); }
  .dot.bad { background: var(--error-color, #c62828); }
  .dot.unk { background: var(--disabled-text-color, #999); }
  .good { color: var(--success-color, #2e7d32); }
  .warn { color: var(--warning-color, #ef6c00); }
  .bad, .bad-t { color: var(--error-color, #c62828); }
  tr.off td { opacity: .6; }
  .tabs { display:flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .tab { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
    border-radius: 16px; padding: 4px 12px; cursor: pointer; font-size: 13px; }
  .tab.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .badge { opacity: .8; margin-left: 4px; }
  .filters { display:flex; gap: 12px; align-items:center; margin-bottom: 10px; flex-wrap: wrap; }
  .filters input[type=text], .filters input:not([type]) { flex: 1; min-width: 200px; padding: 6px 10px;
    border-radius: 8px; border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--primary-text-color); }
  .hint { font-size: 12px; color: var(--secondary-text-color); margin-bottom: 8px; }
  .act { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
    border-radius: 8px; padding: 4px 10px; cursor: pointer; font-size: 13px; }
  .act.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .nm { width: 140px; padding: 4px 8px; border-radius: 8px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color); }
  .act[disabled] { opacity: .5; cursor: progress; }
  :host { overflow-x: hidden; }
  .body { box-sizing: border-box; }
  @media (max-width: 600px) {
    .body { padding: 8px; } td { padding: 5px 3px; } .card { padding: 12px; }
    .c-if, .c-sig { display: none; }
    .filters input:not([type]) { min-width: 0; }
  }
`;

customElements.define("site-health-panel", SiteHealthPanel);
