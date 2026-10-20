// © 2026 Michal Gajdos, všechna práva vyhrazena.
// Upozornění: registrace telefonů a události odvozené ze stavu, který posílá EA.
const webpush = require("./webpush");

const EVENTS = {
  open:    "Nový koš",
  add:     "Přikoupení",
  close:   "Uzavření koše",
  risk:    "Ztráta koše od nastavené hranice",
  halt:    "Ochrana účtu / zastavení",
  settings: "Změny nastavení",
  offline: "Výpadek EA nebo VPS",
};
const DEFAULT_PREFS = { open: true, add: true, close: true, risk: true, halt: true, settings: true, offline: true };

const subs = new Map(); // endpoint -> { sub, prefs, added }
let version = 0;          // roste při každé změně registrací (EA si podle toho dělá zálohu)
let restored = false;     // po startu serveru: už EA vrátilo zálohu?
let vapid = null;
const log = [];           // posledních pár odeslaných upozornění (pro aplikaci)
// nastavení veřejné stránky – zálohuje se spolu s registracemi přes EA
let share = { enabled: false, token: null, asset: "ALL", updated: 0 };
// obecná nastavení upozornění (zálohují se také přes EA)
let settings = { riskPct: 5, updated: 0 };
const riskLevel = new Map(); // symbol -> poslední oznámená úroveň ztráty v %

function init({ publicKey, privateKey, subject }) {
  if (!publicKey || !privateKey) {
    console.warn("Upozornění vypnuta: chybí VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY.");
    return;
  }
  vapid = { publicKey, keyObject: webpush.vapidKeyObject(publicKey, privateKey), subject: subject || "https://stochgrid-panel.onrender.com" };
}

const enabled = () => !!vapid;
const publicKey = () => (vapid ? vapid.publicKey : null);

function cleanPrefs(p) {
  const out = {};
  for (const k of Object.keys(EVENTS)) out[k] = p && typeof p[k] === "boolean" ? p[k] : DEFAULT_PREFS[k];
  return out;
}

function validSub(s) {
  return s && typeof s.endpoint === "string" && /^https:\/\//.test(s.endpoint) && s.keys &&
         typeof s.keys.p256dh === "string" && typeof s.keys.auth === "string";
}

function subscribe(sub, prefs) {
  if (!validSub(sub)) return false;
  const prev = subs.get(sub.endpoint);
  subs.set(sub.endpoint, { sub: { endpoint: sub.endpoint, keys: sub.keys }, prefs: cleanPrefs(prefs || (prev && prev.prefs)), added: prev ? prev.added : Date.now() });
  version++;
  restored = true;
  return true;
}

function unsubscribe(endpoint) {
  if (subs.delete(endpoint)) version++;
}

function prefsFor(endpoint) {
  const s = subs.get(endpoint);
  return s ? s.prefs : null;
}

async function sendTo(entry, payload) {
  const code = await webpush.send(entry.sub, payload, vapid, { topic: payload.tag });
  if (code === 404 || code === 410) { subs.delete(entry.sub.endpoint); version++; }
  return code;
}

async function broadcast(event, title, body, tag) {
  if (!vapid || !subs.size) return;
  log.unshift({ t: Date.now(), event, title, body });
  log.length = Math.min(log.length, 30);
  const payload = { title, body, tag: (tag || event).slice(0, 32), url: "/" };
  await Promise.all([...subs.values()].filter((s) => s.prefs[event] !== false).map((s) => sendTo(s, payload)));
}

async function test(endpoint) {
  const entry = subs.get(endpoint);
  if (!vapid || !entry) return 0;
  return sendTo(entry, { title: "StochGrid AOS", body: "Upozornění fungují ✔", tag: "test", url: "/" });
}

// ---- záloha přes EA (server na free tarifu po restartu zapomíná)
function exportData() {
  return JSON.stringify({ version, items: [...subs.values()], share, settings });
}
function getShare() { return { ...share }; }
function getSettings() { return { ...settings }; }
function setSettings(cfg) {
  const r = Number(cfg.riskPct);
  settings = { riskPct: isFinite(r) && r >= 0.5 && r <= 50 ? Math.round(r * 10) / 10 : settings.riskPct, updated: Date.now() };
  riskLevel.clear();
  version++;
  restored = true;
  return getSettings();
}
function setShare(cfg) {
  share = { enabled: !!cfg.enabled, token: cfg.token || null, asset: String(cfg.asset || "ALL"), updated: Date.now() };
  version++;
  restored = true;
  return getShare();
}
function importData(text) {
  restored = true;
  let d;
  try { d = JSON.parse(text); } catch (e) { return 0; }
  let n = 0;
  for (const it of (d && d.items) || []) {
    if (validSub(it.sub) && !subs.has(it.sub.endpoint)) {
      subs.set(it.sub.endpoint, { sub: it.sub, prefs: cleanPrefs(it.prefs), added: it.added || Date.now() });
      n++;
    }
  }
  // veřejná stránka: vezmeme zálohu, pokud je novější než to, co server má
  if (d && d.share && d.share.token && (d.share.updated || 0) > (share.updated || 0)) {
    share = { enabled: !!d.share.enabled, token: String(d.share.token), asset: String(d.share.asset || "ALL"), updated: d.share.updated || 0 };
    n++;
  }
  if (d && d.settings && (d.settings.updated || 0) > (settings.updated || 0)) {
    settings = { riskPct: Number(d.settings.riskPct) || 5, updated: d.settings.updated || 0 };
    n++;
  }
  if (n) version++;
  return n;
}
const needRestore = () => !restored && subs.size === 0 && !share.token;
const getVersion = () => version;

// ---- události z rozdílu dvou po sobě jdoucích stavů jedné instance
const money = (x, cur) => (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(2) + " " + (cur || "");
const isHalted = (s) => !!s && s.stateLevel === "bad" && /^ZASTAVENO/.test(s.state || "");

function onStatus(prev, cur) {
  if (!prev || !cur) return; // první stav po restartu serveru – nic neporovnáváme
  const sym = cur.symbol || "?";
  const a = cur.account || {}, pa = prev.account || {};
  const b = cur.basket || {}, pb = prev.basket || {};
  const c = a.currency || "";
  const n = b.count || 0, pn = pb.count || 0;

  if (pn === 0 && n > 0) {
    broadcast("open", `${sym}: nový koš ${b.dir || ""}`.trim(),
      `${Number(b.lots || 0).toFixed(2)} lot @ ${b.avg || "?"}`, `basket-${sym}`);
  } else if (n > pn && pn > 0) {
    broadcast("add", `${sym}: přikoupení ${n}/${b.max || "?"}`,
      `Loty ${Number(b.lots || 0).toFixed(2)}, průměr ${b.avg || "?"}, P/L koše ${money(b.pl || 0, c)}`, `basket-${sym}`);
  } else if (pn > 0 && n === 0) {
    const res = (a.balance || 0) - (pa.balance || 0);
    broadcast("close", `${sym}: koš uzavřen ${money(res, c)}`,
      `Pozic ${pn}, balance ${Number(a.balance || 0).toFixed(2)} ${c}`, `basket-${sym}`);
  }

  // ztráta otevřeného koše vůči balance: upozornit na hranici a pak po každé další polovině hranice
  const X = settings.riskPct || 5;
  const bal = a.balance || 0;
  const lossPct = n > 0 && bal > 0 && (b.pl || 0) < 0 ? -(b.pl || 0) / bal * 100 : 0;
  const last = riskLevel.get(sym) || 0;
  if (n === 0 || lossPct < X * 0.6) {
    riskLevel.delete(sym);
  } else if (lossPct >= X) {
    const level = X + Math.floor((lossPct - X) / (X / 2)) * (X / 2);
    if (level > last) {
      riskLevel.set(sym, level);
      const r = cur.risk || {};
      const sl = typeof r.lossFullPct === "number" ? ` · na hard stopu až ${r.lossFullPct.toFixed(1)} %` : "";
      broadcast("risk", `${sym}: ztráta koše ${lossPct.toFixed(1)} %`,
        `P/L ${money(b.pl || 0, c)} · pozic ${n}/${b.max || "?"}${sl}`, `risk-${sym}`);
    }
  }

  // změna nastavení čekající na zavření koše byla použita
  const pm = prev.settingsMeta || {}, cm = cur.settingsMeta || {};
  if (pm.state === "pending" && cm.state && cm.state !== "pending") {
    broadcast("settings", `${sym}: nastavení ${cm.state === "applied" ? "použito" : "odmítnuto"}`, cm.summary || "", `set-${sym}`);
  }

  // EA nemá povolené obchodování (např. odškrtnuté „Umožnit obchodování“ po synchronizaci)
  if (cur.tradeAllowed === false && (prev.tradeAllowed !== false)) {
    broadcast("halt", `${sym}: obchodování ZAKÁZÁNO`, (cur.tradeText || "EA nesmí obchodovat") + ". Zkontrolujte nastavení EA a synchronizujte VPS.", `trade-${sym}`);
  } else if (prev.tradeAllowed === false && cur.tradeAllowed === true) {
    broadcast("halt", `${sym}: obchodování povoleno`, "EA na VPS může opět obchodovat.", `trade-${sym}`);
  }

  // pojistka volatility
  if (cur.guardVol === true && prev.guardVol !== true) {
    broadcast("halt", `${sym}: prudký pohyb – pauza`, `${cur.state || ""}. Nové koše ani přikupování se teď neotevírají.`, `vol-${sym}`);
  }

  if (!isHalted(prev) && isHalted(cur)) {
    broadcast("halt", `${sym}: ZASTAVENO`, `${cur.state.replace(/^ZASTAVENO – /, "")}. Equity ${Number(a.equity || 0).toFixed(2)} ${c}`, `halt-${sym}`);
  } else if (isHalted(prev) && !isHalted(cur)) {
    broadcast("halt", `${sym}: obchodování obnoveno`, `Stav: ${cur.state || "AKTIVNÍ"}`, `halt-${sym}`);
  }
}

// výpadek: zavolá server, když instance přestane / začne posílat data
function onOffline(sym, ageSec) {
  broadcast("offline", `${sym}: EA neposílá data`, `Poslední zpráva před ${Math.round(ageSec / 60)} min. Zkontrolujte VPS / MT4.`, `offline-${sym}`);
}
function onOnline(sym) {
  broadcast("offline", `${sym}: EA opět posílá data`, "Spojení obnoveno.", `offline-${sym}`);
}

// provedený příkaz z aplikace (posílá se se skupinou "zastavení")
function onCommand(sym, label, result, event = "halt") {
  broadcast(event, `${sym}: ${label}`, result || "provedeno", `cmd-${sym}`);
}

module.exports = {
  onCommand, getShare, setShare, getSettings, setSettings,
  EVENTS, init, enabled, publicKey, subscribe, unsubscribe, prefsFor, test,
  exportData, importData, needRestore, getVersion, onStatus, onOffline, onOnline,
  recent: () => log.slice(0, 10), count: () => subs.size,
};
