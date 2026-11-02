// Site Health sidebar panel. Plain web component, no build step: it talks to
// the integration over the websocket API (site_health/overview, /subscribe,
// /set_segment, /modem_sim) and draws with Home Assistant's theme vars.

const SEGMENTS = [
  ["family", "Семья"],
  ["guest", "Гости"],
  ["admin", "Админ"],
  ["iot", "Умный дом"],
  ["asic", "ASIC"],
  ["video", "Видео"],
  ["mgmt", "Управление"],
  ["dmz", "DMZ"],
  ["other", "Прочее"],
];
const SEG_LABEL = Object.fromEntries(SEGMENTS);
const SEG_BY_VLAN = { 10: "управление", 11: "NVR", 12: "камеры", 20: "умный дом",
  30: "семья", 40: "гости", 50: "DMZ", 64: "ASIC", 99: "админ" };
const PLACE_LABEL = { family: "домашние", admin: "админы", guest: "гости" };
const WAN_KIND = { dhcp: "DHCP", pppoe: "PPPoE", lte: "LTE" };
const WAN_STATUS = { bound: "подключён", searching: "поиск DHCP", "searching...": "поиск DHCP",
  requesting: "запрос адреса", rebinding: "продление", stopped: "остановлен",
  connected: "подключён", down: "нет связи", disabled: "выключен" };

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
const QUALITY = { good: ["good", "хороший"], fair: ["warn", "средний"], poor: ["bad", "слабый"],
  unknown: ["", "—"] };
// Comments written by this integration look like "Family: Name -> VLAN 30 (HA)".
const cleanComment = (c) => String(c || "").replace(/^(Family|Admin):\s*/, "")
  .replace(/\s*->\s*VLAN \d+.*$/, "").trim();
const nameOf = (c) => (c && (c.alias || cleanComment(c.comment) || c.hostname)) || "";
const FAILOVER = { "": "выключено", reconnect: "перезапуск соединения", sim1: "переход на SIM 1",
  sim2: "переход на SIM 2" };
const dot = (ok) =>
  `<span class="dot ${ok === true ? "ok" : ok === false ? "bad" : "unk"}"></span>`;
const placeOf = (c) => (c.family ? "family" : c.admin ? "admin" : null);
const tabOf = (c) => placeOf(c) || c.segment_kind || "other";
const inNet = (ip, cidr) => {
  if (!ip || !cidr) return false;
  const [net, bits] = cidr.split("/");
  const n = (x) => x.split(".").reduce((a, o) => (a << 8) + Number(o), 0) >>> 0;
  const mask = bits === "0" ? 0 : (~0 << (32 - Number(bits))) >>> 0;
  return (n(ip) & mask) === (n(net) & mask);
};

// --- step illustrations (simplified screens, not screenshots) -----------------

const SVG_C = { bg: "#f2f2f7", card: "#ffffff", text: "#1c1c1e", sub: "#8e8e93",
  blue: "#007aff", green: "#34c759", hl: "#ff9500", line: "#e5e5ea" };

function phone(title, rows, back = "") {
  const W = 220, H = 360;
  let y = 64, body = "";
  for (const r of rows) {
    if (r.section) {
      body += `<text x="16" y="${y + 14}" font-size="10" fill="${SVG_C.sub}">${esc(r.section)}</text>`;
      y += 22;
      continue;
    }
    const hh = 34;
    const hl = r.hl ? `stroke="${SVG_C.hl}" stroke-width="2.5"` : `stroke="${SVG_C.line}"`;
    body += `<rect x="10" y="${y}" width="${W - 20}" height="${hh}" rx="8" fill="${SVG_C.card}" ${hl}/>`;
    body += `<text x="20" y="${y + 21}" font-size="12" fill="${r.color || SVG_C.text}">${esc(r.label)}</text>`;
    if (r.toggle !== undefined) {
      const on = r.toggle;
      body += `<rect x="${W - 58}" y="${y + 8}" width="34" height="18" rx="9" fill="${on ? SVG_C.green : SVG_C.line}"/>`;
      body += `<circle cx="${on ? W - 33 : W - 49}" cy="${y + 17}" r="7.5" fill="#fff"/>`;
    } else if (r.value !== undefined) {
      body += `<text x="${W - 22}" y="${y + 21}" font-size="11" text-anchor="end" fill="${r.valueColor || SVG_C.sub}">${esc(r.value)}</text>`;
    } else if (r.check) {
      body += `<text x="${W - 26}" y="${y + 22}" font-size="14" fill="${SVG_C.blue}">✓</text>`;
    } else if (r.info) {
      body += `<circle cx="${W - 30}" cy="${y + 17}" r="8" fill="none" stroke="${SVG_C.blue}" stroke-width="1.5"/>`;
      body += `<text x="${W - 30}" y="${y + 21}" font-size="10" text-anchor="middle" fill="${SVG_C.blue}">i</text>`;
    } else if (r.gear) {
      body += `<text x="${W - 32}" y="${y + 22}" font-size="14" fill="${SVG_C.sub}">⚙</text>`;
    } else if (r.chevron !== false) {
      body += `<text x="${W - 24}" y="${y + 22}" font-size="13" fill="${SVG_C.sub}">›</text>`;
    }
    if (r.hl) {
      body += `<circle cx="${W - 12}" cy="${y + hh / 2}" r="9" fill="${SVG_C.hl}"/>`;
      body += `<text x="${W - 12}" y="${y + hh / 2 + 4}" font-size="11" text-anchor="middle" fill="#fff" font-weight="bold">!</text>`;
    }
    y += hh + 6;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(title)}">
    <rect x="1" y="1" width="${W - 2}" height="${H - 2}" rx="26" fill="${SVG_C.bg}" stroke="#c7c7cc" stroke-width="2"/>
    <rect x="${W / 2 - 30}" y="8" width="60" height="8" rx="4" fill="#c7c7cc"/>
    ${back ? `<text x="12" y="44" font-size="11" fill="${SVG_C.blue}">‹ ${esc(back.length > 7 ? "Назад" : back)}</text>` : ""}
    <text x="${W / 2}" y="44" font-size="13" font-weight="600" text-anchor="middle" fill="${SVG_C.text}">${esc(title)}</text>
    ${body}</svg>`;
}

function laptop(title, rows) {
  const W = 300, H = 230;
  let y = 52, body = "";
  for (const r of rows) {
    const hl = r.hl ? `stroke="${SVG_C.hl}" stroke-width="2.5"` : `stroke="${SVG_C.line}"`;
    body += `<rect x="22" y="${y}" width="${W - 44}" height="28" rx="6" fill="${SVG_C.card}" ${hl}/>`;
    body += `<text x="32" y="${y + 18}" font-size="11" fill="${SVG_C.text}">${esc(r.label)}</text>`;
    if (r.toggle !== undefined) {
      body += `<rect x="${W - 74}" y="${y + 6}" width="30" height="16" rx="8" fill="${r.toggle ? SVG_C.green : SVG_C.line}"/>`;
      body += `<circle cx="${r.toggle ? W - 52 : W - 66}" cy="${y + 14}" r="6.5" fill="#fff"/>`;
    } else if (r.value !== undefined) {
      body += `<text x="${W - 34}" y="${y + 18}" font-size="10" text-anchor="end" fill="${r.valueColor || SVG_C.sub}">${esc(r.value)}</text>`;
    }
    if (r.hl) body += `<circle cx="${W - 24}" cy="${y + 14}" r="8" fill="${SVG_C.hl}"/><text x="${W - 24}" y="${y + 18}" font-size="10" text-anchor="middle" fill="#fff" font-weight="bold">!</text>`;
    y += 34;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(title)}">
    <rect x="12" y="6" width="${W - 24}" height="${H - 34}" rx="10" fill="${SVG_C.bg}" stroke="#c7c7cc" stroke-width="2"/>
    <circle cx="28" cy="20" r="4" fill="#ff5f57"/><circle cx="40" cy="20" r="4" fill="#febc2e"/><circle cx="52" cy="20" r="4" fill="#28c840"/>
    <text x="${W / 2}" y="40" font-size="12" font-weight="600" text-anchor="middle" fill="${SVG_C.text}">${esc(title)}</text>
    ${body}
    <rect x="0" y="${H - 26}" width="${W}" height="10" rx="5" fill="#c7c7cc"/></svg>`;
}

function guides(ssid) {
  const net = ssid || "сеть объекта";
  return {
    ios: { label: "iPhone / iPad", steps: [
      { text: "Откройте <b>Настройки → Wi-Fi</b> и нажмите <b>ⓘ</b> справа от сети.",
        svg: phone("Wi-Fi", [{ label: "Wi-Fi", toggle: true }, { section: "МОЯ СЕТЬ" },
          { label: `✓ ${net}`, info: true, hl: true }, { section: "СЕТИ" }, { label: "Другая сеть", chevron: true }]) },
      { text: "Найдите <b>Частный адрес Wi-Fi</b>.",
        svg: phone(net, [{ label: "Забыть эту сеть", color: SVG_C.blue, chevron: false },
          { label: "Автоподключение", toggle: true }, { label: "Частный адрес Wi-Fi", value: "Ротация", hl: true },
          { label: "Режим энергосбер.", toggle: false }], "Wi-Fi") },
      { text: "Выберите <b>Выкл.</b> и подтвердите переподключение к сети.",
        svg: phone("Частный адрес", [{ label: "Выкл.", check: true, hl: true },
          { label: "Фиксированный", chevron: false }, { label: "Ротация", chevron: false }], net) },
    ] },
    android: { label: "Android", steps: [
      { text: "<b>Настройки → Подключения (Сеть) → Wi-Fi</b>, нажмите <b>⚙</b> у сети.",
        svg: phone("Wi-Fi", [{ label: "Wi-Fi", toggle: true }, { section: "ТЕКУЩАЯ СЕТЬ" },
          { label: net, gear: true, hl: true }]) },
      { text: "Откройте <b>Дополнительно / Ещё</b> → <b>Тип MAC-адреса</b> (или «Конфиденциальность»).",
        svg: phone(net, [{ label: "Автоподключение", toggle: true }, { section: "ДОПОЛНИТЕЛЬНО" },
          { label: "Тип MAC-адреса", value: "Случайный", hl: true }, { label: "IP-настройки", value: "DHCP" }], "Wi-Fi") },
      { text: "Выберите <b>MAC-адрес телефона</b> (на Pixel: «Использовать MAC устройства»).",
        svg: phone("Тип MAC-адреса", [{ label: "Случайный MAC", chevron: false },
          { label: "MAC-адрес телефона", check: true, hl: true }], net) },
    ] },
    mac: { label: "Mac", steps: [
      { text: "<b>Системные настройки → Wi-Fi</b>, у сети нажмите <b>Подробнее…</b>",
        svg: laptop("Wi-Fi", [{ label: "Wi-Fi", toggle: true }, { label: `✓ ${net}`, value: "Подробнее…", valueColor: SVG_C.blue, hl: true }]) },
      { text: "<b>Частный адрес Wi-Fi</b> → <b>Выкл.</b> → OK.",
        svg: laptop(net, [{ label: "Автоподключение", toggle: true }, { label: "Частный адрес Wi-Fi", value: "Выкл.", hl: true },
          { label: "Ограничить IP-трекинг", toggle: false }]) },
    ] },
    windows: { label: "Windows", steps: [
      { text: "<b>Параметры → Сеть и Интернет → Wi-Fi</b> → свойства сети.",
        svg: laptop("Wi-Fi", [{ label: "Wi-Fi", toggle: true }, { label: `${net} — свойства`, hl: true }]) },
      { text: "<b>Случайные аппаратные адреса</b> → <b>Откл.</b>",
        svg: laptop(net, [{ label: "Подключаться автоматически", toggle: true },
          { label: "Случайные аппаратные адреса", value: "Откл.", hl: true }]) },
    ] },
  };
}

// --- the panel ----------------------------------------------------------------

class SiteHealthPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._sites = [];
    this._seg = "family";
    this._filter = "";
    this._onlyOnline = true;
    this._busy = new Set();
    this._confirm = null;  // {mac, place} or {modem, slot}
    this._error = "";
    this._wiz = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }
  set narrow(v) { this._narrow = v; }
  set panel(v) { this._panel = v; }

  get _admin() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }

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
    clearInterval(this._wizTimer);
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

  async _call(msg, busyKey) {
    if (busyKey) this._busy.add(busyKey);
    this._render();
    let res = null;
    try {
      res = await this._hass.callWS(msg);
    } catch (e) {
      this._error = `${busyKey || msg.type}: ${e.message || e}`;
    }
    if (busyKey) this._busy.delete(busyKey);
    await this._load();
    return res;
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Здоровье сети</div>
      </div>
      <div id="body" class="body"><div class="muted">Загрузка…</div></div>
      <div id="modal"></div>`;
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
    } else {
      const focus = this.shadowRoot.activeElement && this.shadowRoot.activeElement.id;
      if (focus === "nmedit") return;  // never re-render under the user's typing
      body.innerHTML = (this._error ? `<div class="error">${esc(this._error)}</div>` : "")
        + this._sites.map((s) => this._site(s)).join("");
      this._bind(body);
      if (this._editing) {
        const ne = this.shadowRoot.getElementById("nmedit");
        if (ne) { ne.focus(); ne.setSelectionRange(ne.value.length, ne.value.length); }
      }
      if (focus === "q") {
        const q = this.shadowRoot.getElementById("q");
        if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
      }
    }
    // The wizard follows live data only once it is waiting for the device;
    // never re-render under the user's typing.
    const typing = this.shadowRoot.activeElement && this.shadowRoot.activeElement.id === "wname";
    if (!this._wiz || (this._wiz.step > 1 && !typing)) this._renderWizard();
  }

  _site(s) {
    return `
      <h1>${esc(s.title)}</h1>
      <div class="grid">
        ${this._channels(s)}
        ${(s.modems || []).map((m) => this._modem(s, m)).join("")}
        ${this._devices(s)}
        ${this._radio(s)}
      </div>
      ${(s.switches || []).map((w) => this._switch(s, w)).join("")}
      ${this._clients(s)}`;
  }

  _channels(s) {
    const modemHosts = Object.fromEntries((s.modems || []).map((m) => [m.host, m.name]));
    const wans = s.wans.map((w) => `
      <tr>
        <td>${dot(w.disabled ? null : w.bound)} ${esc(w.comment || w.interface)}
          <div class="sub">${esc(WAN_KIND[w.kind] || w.kind)} · ${esc(w.interface)}${
            modemHosts[w.gateway] ? ` · через ${esc(modemHosts[w.gateway])}` : ""}</div></td>
        <td>${esc(w.disabled ? "отключён" : (WAN_STATUS[w.status] || w.status))}<div class="sub">${esc(w.address)}</div></td>
        <td class="num">↓ ${mbps(w.rx_bps)}<br>↑ ${mbps(w.tx_bps)}</td>
      </tr>`).join("");
    // Probes of switches and management devices are shown under "Devices".
    const infraNets = s.segments.filter((g) => g.kind === "switches" || g.kind === "mgmt")
      .map((g) => g.network);
    const probes = s.probes.filter((p) => !infraNets.some((n) => inNet(p.host, n)))
      .filter((p) => !s.devices.some((d) => d.host === p.host))
      .map((p) => `
      <tr>
        <td>${dot(p.up)} ${esc(p.name)}</td>
        <td class="sub">${esc(p.host)}</td>
        <td class="num">${p.rtt_ms !== null ? `${p.rtt_ms.toFixed(1)} мс` : ""}${
          p.loss_pct ? `<div class="sub bad">потери ${p.loss_pct}%</div>` : ""}</td>
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

  _modem(s, m) {
    const i = m.info || {};
    const [qc, ql] = QUALITY[i.quality || "unknown"];
    const slots = i.sim_slots || 2;
    const others = [];
    for (let k = 1; k <= slots; k++) {
      if (k === i.active_sim) continue;
      const last = (i.last_by_slot || {})[String(k)];
      others.push(`<div class="sub">SIM ${k}: ${last
        ? `${esc(last.operator || "?")}, ${esc(last.sim_status || "")} — когда была активной` : "не использовалась, состояние неизвестно"}</div>`);
    }
    let actions = "";
    if (this._admin && m.online && i.balance_supported) {
      actions += `<button class="act" data-do="balance" data-modem="${esc(m.subentry_id)}" ${this._busy.has(`bal${m.subentry_id}`) ? "disabled" : ""}>Запросить баланс</button>`;
    }
    if (this._admin && m.online) {
      for (let k = 1; k <= slots; k++) {
        if (k === i.active_sim) continue;
        const busy = this._busy.has(`sim${k}`);
        if (this._confirm && this._confirm.modem === m.subentry_id && this._confirm.slot === k) {
          actions += `<span class="sub">Интернет пропадёт на 6–9 с.</span>
            <button class="act primary" data-do="sim" data-modem="${esc(m.subentry_id)}" data-slot="${k}">Переключить</button>
            <button class="act" data-do="cancel">Отмена</button>`;
        } else {
          actions += `<button class="act" ${busy ? "disabled" : ""} data-do="ask-sim" data-modem="${esc(m.subentry_id)}" data-slot="${k}">Перейти на SIM ${k}</button>`;
        }
      }
    }
    const val = (v, u) => (v === null || v === undefined ? "—" : `${v}${u || ""}`);
    return `
      <section class="card">
        <h2>${esc(m.name)} <span class="sub">${esc(m.host)}</span></h2>
        ${!m.online ? `<div class="sub bad">Нет связи с модемом: ${esc(m.error || "")}</div>` : `
        <table>
          <tr><td>${dot(i.connected === true ? true : i.connecting ? null : false)} Подключение</td>
            <td>${i.connected ? `на связи ${dur(i.uptime_s)}` : i.connecting ? "подключается…" : "нет"}</td></tr>
          <tr><td>SIM ${val(i.active_sim)} из ${slots}</td><td><b>${esc(i.operator || "—")}</b> · ${esc(i.sim_status || "")}${
            i.roaming ? ' · <span class="warn">роуминг</span>' : ""}</td></tr>
          <tr><td>Сеть</td><td>${esc(i.tech || "—")} ${esc(i.band || "")}${i.bandwidth_mhz ? ` · ${i.bandwidth_mhz} МГц` : ""}
            <div class="sub">${esc(i.registration || "")}</div></td></tr>
          <tr><td>Сигнал <span class="${qc}">${ql}</span></td><td class="${qc}">RSRP ${val(i.rsrp, " дБм")} · SINR ${val(i.sinr, " дБ")}
            <div class="sub">RSRQ ${val(i.rsrq, " дБ")} · RSSI ${val(i.rssi, " дБм")} · ${val(i.signal_pct, "%")}</div></td></tr>
          <tr><td>Температура</td><td>${val(i.temperature, " °C")}</td></tr>
          <tr><td>Баланс</td><td>${i.balance !== null && i.balance !== undefined
            ? `<b class="${i.balance < 100 ? "warn" : ""}">${i.balance} ₽</b> <span class="sub">${esc(i.balance_when || "")}</span>`
            : i.balance_supported ? '<span class="sub">ждём ответ оператора…</span>' : '<span class="sub">для этого оператора запрос не настроен</span>'}
            ${i.balance_text ? `<div class="sub">${esc(i.balance_text)}</div>` : ""}</td></tr>
          <tr><td>Автопереключение</td><td>${esc(FAILOVER[(i.failover || {}).sim1_control || ""] || (i.failover || {}).sim1_control)}${
            (i.failover || {}).sim1_host1 ? ` <span class="sub">пинг ${esc(i.failover.sim1_host1)} каждые ${esc(i.failover.sim1_ping)} с, тайм-аут ${esc(i.failover.sim1_timeout)} с</span>` : ""}</td></tr>
        </table>
        ${others.join("")}
        ${actions ? `<div class="actions">${actions}</div>` : ""}`}
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
    const switchNets = s.segments.filter((g) => g.kind === "switches").map((g) => g.network);
    const known = new Set([s.core.host, ...s.devices.map((d) => d.host),
      ...(s.switches || []).map((w) => w.host)]);
    const watched = s.probes.filter((p) => !known.has(p.host)
      && (switchNets.some((n) => inNet(p.host, n)) || s.segments.some((g) => g.kind === "mgmt" && inNet(p.host, g.network))));
    const extra = watched.map((p) => `
        <tr>
          <td>${dot(p.up)} <b>${esc(p.name)}</b><div class="sub">${
            switchNets.some((n) => inNet(p.host, n)) ? "коммутатор" : "устройство"} · ${esc(p.host)}</div></td>
          <td class="sub" colspan="2">по проверке netwatch на роутере</td>
          <td class="num sub">${p.up ? "на связи" : p.up === false ? "не отвечает" : "—"}</td>
        </tr>`).join("");
    return `
      <section class="card">
        <h2>Устройства</h2>
        <table>
          ${row(s.core, "главный роутер")}
          ${s.devices.map((d) => row(d, "MikroTik")).join("")}
          ${extra}
        </table>
      </section>`;
  }

  _whoIs(s, mac) {
    const c = s.clients.find((x) => x.mac === mac);
    if (c) return nameOf(c) || c.ip || mac;
    const n = (s.neighbors || []).find((x) => x.mac === mac);
    if (n) return n.identity || mac;
    for (const d of [s.core, ...s.devices]) {
      if ((d.interfaces || []).some((i) => i.mac === mac)) return d.name;
    }
    return mac;
  }

  _switch(s, w) {
    const i = w.info || {};
    const sel = this._port && this._port.sw === w.subentry_id ? this._port.idx : null;
    const tiles = (i.ports || []).map((p) => {
      const cls = p.uplink || p.trunk ? "trunk" : p.up ? "up" : "down";
      return `<button class="port ${cls} ${sel === p.index ? "sel" : ""}" data-do="port" data-sw="${esc(w.subentry_id)}" data-idx="${p.index}">
        <span class="pn">${esc(p.name)}</span>
        <span class="pv">${p.trunk ? "транк" : `VLAN ${p.vlan}`}</span>
        <span class="ps">${p.up ? esc(p.speed || "") : "—"}${p.poe_w ? ` ⚡${p.poe_w}` : ""}</span>
      </button>`;
    }).join("");
    let detail = "";
    if (sel !== null && i.ports && i.ports[sel]) {
      const p = i.ports[sel];
      const who = p.macs.length
        ? p.macs.slice(0, 12).map((m) => `<div>${esc(this._whoIs(s, m))} <span class="sub">${esc(m)}</span></div>`).join("")
          + (p.macs.length > 12 ? `<div class="sub">и ещё ${p.macs.length - 12}</div>` : "")
        : `<div class="sub">${p.up ? "MAC не видно (устройство молчит)" : "ничего не подключено"}</div>`;
      let ctl = "";
      if (this._admin && w.online) {
        const busy = this._busy.has(`${w.subentry_id}:${sel}`);
        if (p.uplink) {
          ctl = `<div class="sub">Это аплинк к роутеру — менять его нельзя, коммутатор потеряет связь.</div>`;
        } else if (this._confirm && this._confirm.sw === w.subentry_id && this._confirm.idx === sel) {
          const c = this._confirm;
          const what = c.action === "vlan" ? (c.vlan === "trunk" ? "сделать транком" : `перевести в VLAN ${c.vlan}`)
            : c.action === "restart" ? "перезапустить порт (питание PoE пропадёт на несколько секунд)"
            : c.action === "poe_off" ? "выключить PoE" : "включить PoE";
          ctl = `<span class="sub">Порт ${esc(p.name)}: ${what}?</span>
            <button class="act primary" data-do="sw-go" ${busy ? "disabled" : ""}>Да</button>
            <button class="act" data-do="cancel">Нет</button>`;
        } else {
          const vlans = [1, ...(i.vlans || [])];
          const opts = vlans.map((v) => `<option value="${v}" ${!p.trunk && p.vlan === v ? "selected" : ""}>VLAN ${v}${
            v === 1 ? " (по умолчанию)" : SEG_BY_VLAN[v] ? ` — ${SEG_BY_VLAN[v]}` : ""}</option>`).join("")
            + `<option value="trunk" ${p.trunk ? "selected" : ""}>Транк (все VLAN с тегами)</option>`;
          ctl = `<label class="sub">VLAN порта <select id="vlansel">${opts}</select></label>
            <button class="act" data-do="sw-vlan" ${busy ? "disabled" : ""}>Применить</button>
            ${p.poe_capable ? `<button class="act" data-do="sw-ask" data-action="restart" ${busy ? "disabled" : ""}>Перезапустить (PoE)</button>
              <button class="act" data-do="sw-ask" data-action="${p.poe_enabled ? "poe_off" : "poe_on"}" ${busy ? "disabled" : ""}>${p.poe_enabled ? "Выключить PoE" : "Включить PoE"}</button>` : ""}`;
        }
      }
      detail = `<div class="portinfo">
        <div><b>Порт ${esc(p.name)}</b> · ${p.up ? `линк ${esc(p.speed || "")}` : "нет линка"} · ${p.trunk ? "транк" : `VLAN ${p.vlan}`}${
          p.uplink ? " · аплинк" : ""}${p.poe_capable ? ` · PoE ${p.poe_enabled ? "вкл" : "выкл"}${p.poe_w ? `, ${p.poe_w} Вт` : ""}` : ""}</div>
        <div class="who">${who}</div>
        ${ctl ? `<div class="actions">${ctl}</div>` : ""}
      </div>`;
    }
    return `
      <section class="card wide sw">
        <h2>${dot(w.online)} ${esc(w.name)} <span class="sub">${esc(i.model || "")} · ${esc(w.host)}${
          i.temperature !== null && i.temperature !== undefined ? ` · ${i.temperature} °C` : ""}${
          i.poe_total_w ? ` · PoE ${i.poe_total_w} Вт` : ""}</span></h2>
        ${!w.online ? `<div class="sub bad">${esc(w.error || "нет связи")}</div>` : ""}
        ${w.error && w.online ? `<div class="sub warn">${esc(w.error)}</div>` : ""}
        <div class="ports">${tiles}</div>
        ${detail}
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
            <td class="num sub">${l.tx_rate_mbps ?? "—"} / ${l.rx_rate_mbps ?? "—"} Мбит/с
              <div>трафик ↓ ${mbps(l.rx_bps)} ↑ ${mbps(l.tx_bps)}</div></td>
            <td class="num sub">${dur(l.uptime_s)}</td>
          </tr>`);
      }
    }
    if (!rows.length) return "";
    return `<section class="card"><h2>Радиомосты</h2><table>${rows.join("")}</table></section>`;
  }

  _actions(s, c) {
    if (!this._admin) return "";
    const place = placeOf(c);
    // Only Wi-Fi people go through the access list; IoT, cameras etc. are
    // placed by their SSID or switch port, so they get no buttons.
    const managed = place || (c.wireless && c.segment_kind === "guest");
    if (!managed) {
      return c.access_vlan ? `<span class="sub">VLAN ${c.access_vlan} (вручную)</span>` : "";
    }
    const ent = `data-entry="${esc(s.entry_id)}" data-mac="${esc(c.mac)}"`;
    if (this._busy.has(c.mac)) return `<span class="sub">…</span>`;
    if (this._confirm && this._confirm.mac === c.mac) {
      const to = this._confirm.place;
      return `<span class="sub">${to === "guest" ? "Вернуть в гостевую сеть?" : `Перевести в ${PLACE_LABEL[to]}?`}</span>
        <button class="act primary" data-do="place" data-place="${to}" ${ent}>Да</button>
        <button class="act" data-do="cancel">Нет</button>`;
    }
    if (place === "family") {
      return `<button class="act" data-do="ask" data-place="guest" ${ent}>Убрать из домашних</button>
        <button class="act" data-do="ask" data-place="admin" ${ent}>В админы</button>`;
    }
    if (place === "admin") {
      return `<button class="act" data-do="ask" data-place="guest" ${ent}>Убрать из админов</button>
        <button class="act" data-do="ask" data-place="family" ${ent}>В домашние</button>`;
    }
    return `<button class="act primary" data-do="wizard" data-place="family" ${ent}>В домашние</button>
      <button class="act" data-do="wizard" data-place="admin" ${ent}>В админы</button>`;
  }

  _nameCell(s, c, name) {
    if (this._editing && this._editing.mac === c.mac) {
      return `${dot(c.online ? true : null)} <input class="nm" id="nmedit" value="${esc(this._editing.value)}" placeholder="Имя (можно по-русски)">
        <button class="act primary" data-do="name-save" data-mac="${esc(c.mac)}" data-entry="${esc(s.entry_id)}">✓</button>
        <button class="act" data-do="name-cancel">✕</button>`;
    }
    const pen = this._admin ? ` <button class="pen" data-do="name-edit" data-mac="${esc(c.mac)}" title="Подписать">✎</button>` : "";
    const hint = c.alias && c.hostname && c.alias !== c.hostname ? ` <span class="sub">(${esc(c.hostname)})</span>` : "";
    return `${dot(c.online ? true : null)} <b>${esc(name || c.mac)}</b>${hint}${pen}`;
  }

  _clients(s) {
    const all = s.clients;
    const counts = {};
    for (const c of all) {
      const k = tabOf(c);
      if (c.online || placeOf(c)) counts[k] = (counts[k] || 0) + 1;
    }
    const tabs = SEGMENTS.filter(([k]) => counts[k] || k === "family" || k === "guest")
      .map(([k, label]) => `<button class="tab ${this._seg === k ? "on" : ""}" data-seg="${k}">
        ${esc(label)} <span class="badge">${counts[k] || 0}</span></button>`).join("");
    const q = this._filter.toLowerCase();
    const list = all.filter((c) => {
      if (tabOf(c) !== this._seg) return false;
      if (this._onlyOnline && !c.online && !placeOf(c)) return false;
      if (!q) return true;
      return [c.hostname, c.comment, c.ip, c.mac, c.ssid].join(" ").toLowerCase().includes(q);
    }).sort((a, b) => (b.online - a.online) || (a.ip || "~").localeCompare(b.ip || "~",
      undefined, { numeric: true }));

    const rows = list.map((c) => {
      const name = nameOf(c);
      const waiting = placeOf(c) && c.online && c.segment_kind !== placeOf(c)
        ? `<div class="sub warn">ещё в сети «${esc(SEG_LABEL[c.segment_kind] || c.segment_kind)}» — переподключится</div>` : "";
      return `
        <tr class="${c.online ? "" : "off"}">
          <td>${this._nameCell(s, c, name)}
            <div class="sub">${esc(c.mac)}${c.random_mac ? " · частный MAC" : ""}${c.static_lease ? " · статика" : ""}</div>${waiting}</td>
          <td>${esc(c.ip || "—")}<div class="sub">${esc(SEG_LABEL[c.segment_kind] || "")}</div></td>
          <td class="sub c-if">${c.wireless ? `📶 ${esc(c.ssid || c.wifi_interface)}` : esc(c.interface || "")}</td>
          <td class="num c-sig ${signalClass(c.signal_dbm)}">${c.signal_dbm ?? ""}</td>
          <td class="num sub">${c.online ? (c.wifi_uptime_s !== null && c.wifi_uptime_s !== undefined ? `${dur(c.wifi_uptime_s)}` : "в сети")
            : c.last_seen_s !== null ? `${dur(c.last_seen_s)} назад` : "не видели"}</td>
          <td class="num acts">${this._actions(s, c)}</td>
        </tr>`;
    }).join("");

    const hint = this._seg === "guest"
      ? `<div class="hint">Новые Wi-Fi-устройства попадают в гостевую сеть. «В домашние» / «В админы» проведёт по шагам: отключить частный адрес Wi-Fi на устройстве, найти его новое подключение и добавить в access-list роутера (VLAN ${s.family_vlan} / ${s.admin_vlan}).</div>`
      : ["iot", "video", "mgmt", "asic"].includes(this._seg)
        ? `<div class="hint">Сюда устройства попадают по своему Wi-Fi (скрытая IoT-сеть) или по VLAN порта коммутатора — переносить их отсюда не нужно.</div>` : "";
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
        this._qt = setTimeout(() => this._render(), 250);
      });
    }
    const ne = root.querySelector("#nmedit");
    if (ne) {
      ne.addEventListener("input", () => { if (this._editing) this._editing.value = ne.value; });
      ne.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") root.querySelector('[data-do="name-save"]').click();
        if (ev.key === "Escape") { this._editing = null; this._render(); }
      });
    }
    const on = root.querySelector("#online");
    if (on) on.addEventListener("change", () => { this._onlyOnline = on.checked; this._render(); });
    const site = (el) => this._sites.find((x) => x.entry_id === el.dataset.entry);
    root.querySelectorAll("[data-do]").forEach((b) => b.addEventListener("click", () => {
      const d = b.dataset;
      switch (d.do) {
        case "ask": this._confirm = { mac: d.mac, place: d.place }; break;
        case "ask-sim": this._confirm = { modem: d.modem, slot: Number(d.slot) }; break;
        case "cancel": this._confirm = null; break;
        case "place":
          this._confirm = null;
          this._call({ type: "site_health/set_segment", mac: d.mac, segment: d.place,
            entry_id: d.entry }, d.mac);
          return;
        case "sim":
          this._confirm = null;
          this._call({ type: "site_health/modem_sim", modem: d.modem, slot: Number(d.slot) },
            `sim${d.slot}`);
          return;
        case "port": {
          const idx = Number(d.idx);
          this._port = this._port && this._port.sw === d.sw && this._port.idx === idx ? null : { sw: d.sw, idx };
          this._confirm = null;
          break;
        }
        case "sw-ask":
          this._confirm = { sw: this._port.sw, idx: this._port.idx, action: d.action };
          break;
        case "sw-vlan": {
          const v = this.shadowRoot.getElementById("vlansel").value;
          this._confirm = { sw: this._port.sw, idx: this._port.idx, action: "vlan",
            vlan: v === "trunk" ? "trunk" : Number(v) };
          break;
        }
        case "sw-go": {
          const c = this._confirm;
          this._confirm = null;
          const msg = c.action === "vlan"
            ? { type: "site_health/switch_vlan", switch: c.sw, port: c.idx, vlan: c.vlan }
            : { type: "site_health/switch_port", switch: c.sw, port: c.idx, action: c.action };
          this._call(msg, `${c.sw}:${c.idx}`);
          return;
        }
        case "name-edit": {
          const site0 = this._sites.find((x) => x.clients.some((c) => c.mac === d.mac));
          const c = site0 && site0.clients.find((x) => x.mac === d.mac);
          this._editing = { mac: d.mac, value: c ? (c.alias || nameOf(c)) : "" };
          break;
        }
        case "name-cancel": this._editing = null; break;
        case "name-save": {
          const val = this._editing ? this._editing.value : "";
          this._editing = null;
          this._call({ type: "site_health/set_name", mac: d.mac, name: val, entry_id: d.entry }, d.mac);
          return;
        }
        case "balance":
          this._call({ type: "site_health/modem_balance", modem: d.modem }, `bal${d.modem}`);
          return;
        case "wizard":
          this._openWizard(site(b), d.mac, d.place);
          return;
      }
      this._render();
    }));
  }

  // --- private-address wizard -------------------------------------------------

  _client(site, mac) { return site && site.clients.find((c) => c.mac === mac); }

  _openWizard(site, mac, place) {
    const c = this._client(site, mac) || { mac };
    const ua = navigator.userAgent;
    this._wiz = {
      entry: site.entry_id, mac, place, step: 1, done: false, chosen: null,
      name: nameOf(c),
      hostname: c.hostname || "", ssid: c.ssid || "",
      platform: /iPhone|iPad|Mac/.test(ua) && !/Android/.test(ua) ? "ios" : /Android/.test(ua) ? "android" : "ios",
      started: Date.now(), result: null, error: "",
    };
    this._renderWizard();
  }

  _closeWizard() {
    this._wiz = null;
    clearInterval(this._wizTimer);
    this._wizTimer = null;
    this._renderWizard();
  }

  _wizSite() { return this._wiz && this._sites.find((s) => s.entry_id === this._wiz.entry); }

  _candidates() {
    const w = this._wiz, s = this._wizSite();
    if (!s) return [];
    const since = (Date.now() - w.started) / 1000 + 15;
    return s.clients.filter((c) => c.wireless && c.online && !placeOf(c)
      && c.wifi_uptime_s !== null && c.wifi_uptime_s !== undefined && c.wifi_uptime_s <= since)
      .map((c) => ({ ...c,
        score: (c.mac === w.mac ? 1 : 0) + (w.hostname && c.hostname === w.hostname ? 3 : 0)
          + (!c.random_mac ? 2 : 0) + (w.ssid && c.ssid === w.ssid ? 1 : 0) }))
      .sort((a, b) => b.score - a.score || a.wifi_uptime_s - b.wifi_uptime_s);
  }

  async _wizAssign(mac) {
    const w = this._wiz;
    w.chosen = mac;
    w.step = 3;
    w.error = "";
    this._renderWizard();
    try {
      w.result = await this._hass.callWS({ type: "site_health/set_segment", mac,
        segment: w.place, name: w.name, entry_id: w.entry });
    } catch (e) {
      w.error = e.message || String(e);
    }
    await this._load();
  }

  _renderWizard() {
    const host = this.shadowRoot.getElementById("modal");
    if (!host) return;
    const w = this._wiz;
    if (!w) { host.innerHTML = ""; return; }
    const s = this._wizSite();
    const target = PLACE_LABEL[w.place];
    const orig = this._client(s, w.mac) || { mac: w.mac };
    let body = "";
    if (w.step === 1) {
      const g = guides(w.ssid);
      const tabs = Object.entries(g).map(([k, v]) =>
        `<button class="tab ${w.platform === k ? "on" : ""}" data-w="plat" data-p="${k}">${esc(v.label)}</button>`).join("");
      const steps = g[w.platform].steps.map((st, i) => `
        <figure class="step"><div class="pic">${st.svg}</div>
          <figcaption><span class="n">${i + 1}</span> ${st.text}</figcaption></figure>`).join("");
      body = `
        <h2>Шаг 1. Отключите частный адрес Wi-Fi</h2>
        <p>Телефоны и ноутбуки по умолчанию подключаются к каждой сети со <b>случайным (частным) MAC-адресом</b> и могут его менять.
        Роутер узнаёт «домашние» устройства по MAC, поэтому для сети <b>${esc(w.ssid || "объекта")}</b> частный адрес нужно выключить.</p>
        <p class="sub">Сейчас у устройства ${esc(orig.mac)} — ${orig.random_mac
          ? '<b class="warn">частный адрес</b>, отключение обязательно'
          : '<b class="good">похоже на настоящий адрес</b>, шаг можно пропустить'}.</p>
        <div class="tabs">${tabs}</div>
        <div class="steps">${steps}</div>
        <label class="confirm"><input type="checkbox" id="wdone" ${w.done ? "checked" : ""}>
          Я отключил(а) частный адрес, устройство переподключилось к Wi-Fi</label>
        <div class="actions">
          <button class="act" data-w="close">Отмена</button>
          <button class="act" data-w="same">Адрес не менялся — использовать ${esc(orig.mac)}</button>
          <button class="act primary" data-w="next" ${w.done ? "" : "disabled"}>Дальше</button>
        </div>`;
    } else if (w.step === 2) {
      const cands = this._candidates();
      const rows = cands.map((c, i) => `
        <tr class="${i === 0 ? "best" : ""}">
          <td><b>${esc(nameOf(c) || "без имени")}</b>
            <div class="sub">${esc(c.mac)} · ${c.random_mac ? "частный MAC" : '<span class="good">настоящий MAC</span>'}${
              c.mac === w.mac ? " · тот же адрес" : ""}${w.hostname && c.hostname === w.hostname ? " · то же имя" : ""}</div></td>
          <td>${esc(c.ip || "")}<div class="sub">📶 ${esc(c.ssid || "")} ${c.signal_dbm ?? ""}</div></td>
          <td class="num sub">подключился ${dur(c.wifi_uptime_s)} назад</td>
          <td class="num"><button class="act ${i === 0 ? "primary" : ""}" data-w="pick" data-mac="${esc(c.mac)}">Это оно</button></td>
        </tr>`).join("");
      body = `
        <h2>Шаг 2. Найдём устройство после переподключения</h2>
        <p>Ниже — Wi-Fi-подключения, появившиеся после начала мастера. После отключения частного адреса устройство приходит
        <b>с новым MAC</b>, поэтому ищем по времени подключения, имени и «настоящему» адресу. Если списка нет —
        выключите и включите Wi-Fi на устройстве; список обновляется сам.</p>
        <table class="cands">${rows || `<tr><td class="muted">Ждём новое подключение… <span class="spin"></span></td></tr>`}</table>
        <div class="actions">
          <button class="act" data-w="back">Назад</button>
          <button class="act" data-w="same">Использовать прежний ${esc(w.mac)}</button>
        </div>`;
    } else {
      const c = this._client(s, w.chosen) || { mac: w.chosen };
      const net = (s && s.segments.find((g) => g.vlan_id === (w.place === "admin" ? s.admin_vlan : s.family_vlan))) || null;
      const landed = c.online && net && inNet(c.ip, net.network);
      body = `
        <h2>Шаг 3. ${w.place === "admin" ? "Админ" : "Домашние"}: ${esc(w.chosen)}</h2>
        ${w.error ? `<div class="error">${esc(w.error)}</div>` : ""}
        ${!w.result && !w.error ? `<p>Записываю в роутер… <span class="spin"></span></p>` : ""}
        ${w.result ? `<p>MAC добавлен в «${esc(target)}», устройство переподключено к Wi-Fi.</p>
          <p>${landed ? `<span class="good">✓ Устройство уже в сети «${esc(target)}»: ${esc(c.ip)}</span>`
            : `Ждём, когда оно получит адрес в новой сети${net ? ` (${esc(net.network)})` : ""}… <span class="spin"></span>`}</p>` : ""}
        <div class="actions"><button class="act primary" data-w="close">${landed ? "Готово" : "Закрыть"}</button></div>`;
    }
    const nameRow = w.step < 3 ? `
      <label class="nm-row">Имя устройства (пойдёт в комментарий, латиницей):
        <input id="wname" value="${esc(w.name)}" placeholder="Например: iPhone Masha"></label>` : "";
    host.innerHTML = `<div class="overlay"><div class="dialog" role="dialog" aria-modal="true">
      <div class="dlg-head">В ${esc(target)}: ${esc(nameOf(orig) || w.mac)}
        <button class="x" data-w="close" aria-label="Закрыть">✕</button></div>
      ${body}${nameRow}</div></div>`;
    host.querySelectorAll("[data-w]").forEach((b) => b.addEventListener("click", () => {
      const d = b.dataset.w;
      if (d === "close") return this._closeWizard();
      if (d === "plat") w.platform = b.dataset.p;
      if (d === "next") { w.step = 2; this._startWizPoll(); }
      if (d === "back") w.step = 1;
      if (d === "same") return this._wizAssign(w.mac);
      if (d === "pick") return this._wizAssign(b.dataset.mac);
      this._renderWizard();
    }));
    const cb = host.querySelector("#wdone");
    if (cb) cb.addEventListener("change", () => { w.done = cb.checked; this._renderWizard(); });
    const nm = host.querySelector("#wname");
    if (nm) nm.addEventListener("input", () => { w.name = nm.value; });
  }

  _startWizPoll() {
    clearInterval(this._wizTimer);
    this._wizTimer = setInterval(() => {
      if (!this._wiz) return clearInterval(this._wizTimer);
      this._hass.callService("site_health", "refresh", {}).catch(() => {});
    }, 5000);
  }
}

const STYLE = `
  :host { display:block; background: var(--primary-background-color); color: var(--primary-text-color);
    min-height: 100vh; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); overflow-x: hidden; }
  .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; gap:8px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .title { font-size: 20px; }
  .body { padding: 16px; max-width: 1400px; margin: 0 auto; box-sizing: border-box; }
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
  .acts { white-space: normal; }
  .sub { font-size: 12px; color: var(--secondary-text-color); }
  .muted { color: var(--secondary-text-color); padding: 8px; }
  .error { background: var(--error-color); color: #fff; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align: middle; }
  .dot.ok { background: var(--success-color, #2e7d32); }
  .dot.bad { background: var(--error-color, #c62828); }
  .dot.unk { background: var(--disabled-text-color, #999); }
  .good { color: var(--success-color, #2e7d32); }
  .warn { color: var(--warning-color, #ef6c00); }
  .bad { color: var(--error-color, #c62828); }
  tr.off td { opacity: .6; }
  .tabs { display:flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .tab { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
    border-radius: 16px; padding: 4px 12px; cursor: pointer; font-size: 13px; }
  .tab.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .badge { opacity: .8; margin-left: 4px; }
  .filters { display:flex; gap: 12px; align-items:center; margin-bottom: 10px; flex-wrap: wrap; }
  .filters input:not([type]) { flex: 1; min-width: 200px; padding: 6px 10px;
    border-radius: 8px; border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--primary-text-color); }
  .hint { font-size: 12px; color: var(--secondary-text-color); margin-bottom: 8px; }
  .actions { display:flex; gap: 8px; flex-wrap: wrap; align-items:center; margin-top: 12px; }
  .act { border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
    border-radius: 8px; padding: 4px 10px; cursor: pointer; font-size: 13px; margin: 2px 0 2px 4px; }
  .act.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .act[disabled] { opacity: .5; cursor: not-allowed; }
  .pen { border: none; background: transparent; color: var(--secondary-text-color); cursor: pointer; font-size: 13px; padding: 0 4px; }
  .nm { width: 180px; padding: 4px 8px; border-radius: 8px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color); }
  .ports { display:flex; flex-wrap: wrap; gap: 6px; }
  .port { display:flex; flex-direction: column; align-items:center; min-width: 64px; padding: 6px 4px;
    border-radius: 8px; border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--primary-text-color); cursor: pointer; font-size: 11px; gap: 2px; }
  .port .pn { font-size: 14px; font-weight: 600; }
  .port.up { border-color: var(--success-color, #2e7d32); }
  .port.up .pn { color: var(--success-color, #2e7d32); }
  .port.trunk { border-color: var(--primary-color); }
  .port.trunk .pn { color: var(--primary-color); }
  .port.down { opacity: .7; }
  .port.sel { box-shadow: 0 0 0 2px var(--primary-color); }
  .portinfo { margin-top: 10px; padding: 10px; border-radius: 10px; background: var(--secondary-background-color); font-size: 13px; }
  .portinfo .who { margin-top: 6px; display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 2px 12px; }
  .portinfo select { margin-left: 6px; padding: 3px 6px; border-radius: 6px; background: var(--card-background-color);
    color: var(--primary-text-color); border: 1px solid var(--divider-color); }
  .sw { margin-bottom: 16px; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.45); display:flex; align-items:flex-start;
    justify-content:center; padding: 24px 12px; overflow-y:auto; z-index: 10; }
  .dialog { background: var(--card-background-color); color: var(--primary-text-color); border-radius: 16px;
    max-width: 920px; width: 100%; padding: 18px 20px; box-sizing: border-box; }
  .dlg-head { display:flex; justify-content: space-between; align-items:center; font-size: 13px;
    color: var(--secondary-text-color); margin-bottom: 6px; }
  .x { border: none; background: transparent; color: var(--secondary-text-color); font-size: 18px; cursor:pointer; }
  .dialog p { font-size: 14px; line-height: 1.45; margin: 6px 0 10px; }
  .steps { display:grid; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); gap: 14px; margin: 8px 0 12px; }
  .step { margin: 0; display:flex; flex-direction: column; align-items:center; gap: 8px; }
  .step .pic svg { max-width: 100%; height: auto; }
  .step figcaption { font-size: 13px; line-height: 1.4; text-align: left; width: 100%; }
  .step .n { display:inline-flex; width:20px; height:20px; border-radius:50%; background: var(--primary-color);
    color: var(--text-primary-color, #fff); align-items:center; justify-content:center; font-size: 12px; margin-right: 4px; }
  .confirm { display:flex; gap: 8px; align-items:center; font-size: 14px; padding: 10px; border-radius: 10px;
    background: var(--secondary-background-color); }
  .nm-row { display:flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--secondary-text-color); margin-top: 12px; }
  .nm-row input { padding: 6px 10px; border-radius: 8px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color); font-size: 14px; }
  .cands tr.best td { background: color-mix(in srgb, var(--primary-color) 10%, transparent); }
  .spin { display:inline-block; width: 12px; height: 12px; border: 2px solid var(--divider-color);
    border-top-color: var(--primary-color); border-radius: 50%; animation: sp 1s linear infinite; vertical-align: middle; }
  @keyframes sp { to { transform: rotate(360deg); } }
  @media (max-width: 600px) {
    .body { padding: 8px; } td { padding: 5px 3px; } .card { padding: 12px; }
    .c-if, .c-sig { display: none; }
    .filters input:not([type]) { min-width: 0; }
    .dialog { padding: 14px; }
  }
`;

// HA may load a newer bundle (new ?v=) into a page that already has one.
if (!customElements.get("site-health-panel")) {
  customElements.define("site-health-panel", SiteHealthPanel);
}
