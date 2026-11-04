/*
 * Visitor log card for EZVIZ HP7 / CP7 Video Intercom.
 *
 *   type: custom:ezviz-visitor-log-card
 *   title: Door              # optional
 *   serial: BE0000001-BE0000002   # optional, all door stations when left out
 *   days: 2                  # 1-7, default 1
 *   show_unlock: true        # "Open door" button, default false
 *   unlock: door             # door | gate (what the button opens), default door
 *   event_entity: event.tor_doorbell   # optional: reload the moment it changes
 *
 * Everything the cloud sends is put on the page as text, never as HTML.
 */
const DOMAIN = "ezviz_instant_bell";
const KINDS = { all: "All", ring: "Rings", unlock: "Unlocks", other: "Other" };
const BADGES = { ring: "Ring", unlock: "Unlock", other: "Other" };
const REFRESH_MS = 60000;

const css = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .head { display: flex; align-items: center; gap: 8px; padding: 12px 16px 4px; }
  .head h2 { flex: 1; margin: 0; font-size: 1.2em; font-weight: 500; }
  button { font: inherit; cursor: pointer; border: 1px solid var(--divider-color); border-radius: 16px;
    background: transparent; color: var(--primary-text-color); padding: 4px 12px; }
  button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
  button.open { background: var(--error-color, #db4437); color: #fff; border-color: transparent; }
  button:disabled { opacity: .5; cursor: default; }
  .chips { display: flex; gap: 6px; padding: 4px 16px 8px; flex-wrap: wrap; }
  .day { padding: 8px 16px 2px; font-size: .85em; color: var(--secondary-text-color); text-transform: uppercase; }
  .row { display: flex; gap: 12px; align-items: center; padding: 8px 16px; cursor: pointer; }
  .row:hover { background: var(--secondary-background-color); }
  .thumb { width: 84px; height: 56px; border-radius: 6px; object-fit: cover; flex: none;
    background: var(--secondary-background-color); }
  .meta { flex: 1; min-width: 0; }
  .meta .t { font-weight: 500; }
  .meta .s { font-size: .85em; color: var(--secondary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { font-size: .75em; padding: 1px 8px; border-radius: 10px; flex: none;
    background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .badge.ring { background: var(--warning-color, #ffa600); color: #000; }
  .badge.unlock { background: var(--success-color, #43a047); color: #fff; }
  .msg { padding: 16px; color: var(--secondary-text-color); }
  .msg.err { color: var(--error-color, #db4437); }
  .overlay { position: fixed; inset: 0; z-index: 9; background: rgba(0,0,0,.8); display: flex;
    flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 16px; }
  .overlay img, .overlay video { max-width: 100%; max-height: 70vh; border-radius: 8px; background: #000; }
  .overlay .cap { color: #fff; }
  .overlay .bar { display: flex; gap: 8px; }
  .overlay button { color: #fff; border-color: #fff; }
`;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node[key] = value;
  }
  for (const child of children) node.append(child);
  return node;
}

class EzvizVisitorLogCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._items = [];
    this._filter = "all";
    this._error = "";
    this._loaded = false;
    this._watched = undefined;
    this._busy = false;
    this._timer = null;
    this._overlay = null;
    this._stale = false;
  }

  setConfig(config) {
    const days = Number(config.days ?? 1);
    if (!Number.isInteger(days) || days < 1 || days > 7) throw new Error("days must be 1 to 7");
    this._config = { show_unlock: false, unlock: "door", ...config, days };
    this._loaded = false;
    this._render();
  }

  getCardSize() { return 6; }

  static getStubConfig() { return { days: 1 }; }

  set hass(hass) {
    this._hass = hass;
    const entity = this._config && this._config.event_entity;
    const seen = entity && hass.states[entity] ? hass.states[entity].last_changed : undefined;
    if (!this._loaded) this._load();
    else if (entity && seen !== this._watched) this._load();
    this._watched = seen;
  }

  connectedCallback() {
    this._timer = setInterval(() => this._hass && this._load(), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  async _load() {
    if (this._busy || !this._hass || !this._config) return;
    this._busy = true;
    this._loaded = true;
    try {
      const msg = { type: `${DOMAIN}/visitor_log`, days: this._config.days };
      if (this._config.serial) msg.serial = this._config.serial;
      const answer = await this._hass.callWS(msg);
      this._items = answer.items;
      this._partial = answer.partial;
      this._doors = answer.doors;
      this._error = "";
    } catch (err) {
      this._error = (err && err.message) || "Could not load the log";
    } finally {
      this._busy = false;
      // Redrawing would throw away a video that is playing; do it when it is closed.
      if (this._overlay) this._stale = true;
      else this._render();
    }
  }

  async _open() {
    const serial = this._config.serial || (this._doors && this._doors.length === 1 ? this._doors[0].serial : "");
    if (!serial) { this._error = "Set `serial` on the card to use the Open button"; this._render(); return; }
    const what = this._config.unlock === "gate" ? "gate" : "door";
    if (!window.confirm(`Open the ${what}?`)) return;
    try {
      await this._hass.callService(DOMAIN, `unlock_${what}`, { serial });
    } catch (err) {
      this._error = (err && err.message) || "Could not open";
      this._render();
    }
  }

  _show(item) {
    const bar = el("div", { class: "bar" });
    const close = () => {
      overlay.remove();
      this._overlay = null;
      if (this._stale) { this._stale = false; this._render(); }
    };
    bar.append(el("button", { textContent: "Close", onclick: close }));
    const overlay = el("div", { class: "overlay", onclick: (e) => { if (e.target === overlay) close(); } });
    if (item.clip) {
      overlay.append(el("video", { src: item.clip, controls: true, autoplay: true, poster: item.picture || "" }));
    } else if (item.picture) {
      overlay.append(el("img", { src: item.picture, alt: "" }));
    }
    overlay.append(el("div", { class: "cap" }, this._when(item.time) + "  " + (item.title || "")), bar);
    this._overlay = overlay;
    this.shadowRoot.append(overlay);
  }

  _when(iso) {
    if (!iso) return "--:--";
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  _dayLabel(iso) {
    if (!iso) return "Unknown day";
    const day = new Date(iso);
    const today = new Date();
    const yesterday = new Date(today.getTime() - 86400000);
    if (day.toDateString() === today.toDateString()) return "Today";
    if (day.toDateString() === yesterday.toDateString()) return "Yesterday";
    return day.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  }

  _render() {
    const root = this.shadowRoot;
    root.replaceChildren(el("style", { textContent: css }));
    if (!this._config) return;
    const card = el("ha-card");
    const head = el("div", { class: "head" }, el("h2", { textContent: this._config.title || "Visitors" }));
    if (this._config.show_unlock) {
      head.append(el("button", { class: "open", textContent: this._config.unlock === "gate" ? "Open gate" : "Open door", onclick: () => this._open() }));
    }
    head.append(el("button", { textContent: "↻", title: "Refresh", disabled: this._busy, onclick: () => this._load() }));
    card.append(head);

    const chips = el("div", { class: "chips" });
    for (const [key, label] of Object.entries(KINDS)) {
      chips.append(el("button", { class: key === this._filter ? "on" : "", textContent: label,
        onclick: () => { this._filter = key; this._render(); } }));
    }
    card.append(chips);

    if (this._error) card.append(el("div", { class: "msg err", textContent: this._error }));
    const shown = this._items.filter((i) => this._filter === "all" || i.kind === this._filter);
    if (!this._error && !shown.length) {
      card.append(el("div", { class: "msg", textContent: this._loaded && !this._busy ? "Nothing in this period." : "Loading…" }));
    }
    let lastDay = "";
    for (const item of shown) {
      const label = this._dayLabel(item.time);
      if (label !== lastDay) { card.append(el("div", { class: "day", textContent: label })); lastDay = label; }
      const playable = item.clip || item.picture;
      const thumb = item.picture
        ? el("img", { class: "thumb", src: item.picture, loading: "lazy", alt: "" })
        : el("div", { class: "thumb" });
      const sub = [item.title, item.clip_seconds ? `${item.clip_seconds} s video` : ""].filter(Boolean).join(" · ");
      const row = el("div", { class: "row", onclick: () => playable && this._show(item) },
        thumb,
        el("div", { class: "meta" }, el("div", { class: "t", textContent: this._when(item.time) }), el("div", { class: "s", textContent: sub })),
        el("span", { class: `badge ${item.kind}`, textContent: BADGES[item.kind] || item.kind }));
      card.append(row);
    }
    if (this._partial) card.append(el("div", { class: "msg", textContent: "Some days could not be read from EZVIZ." }));
    root.append(card);
  }
}

if (!customElements.get("ezviz-visitor-log-card")) {
  customElements.define("ezviz-visitor-log-card", EzvizVisitorLogCard);
}
window.customCards = window.customCards || [];
window.customCards.push({
  type: "ezviz-visitor-log-card",
  name: "EZVIZ visitor log",
  description: "Rings, unlocks and their pictures and recordings, with an optional Open door button.",
});
