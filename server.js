// StochGrid AOS – webový panel
// © 2026 Michal Gajdos, všechna práva vyhrazena.
// EA posílá stav (POST /api/push s hlavičkou X-Token), prohlížeč ho zobrazuje (chráněno heslem).
// Bez závislostí, jen Node.js.

const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const PORT = process.env.PORT || 10000;
const PUSH_TOKEN = process.env.PUSH_TOKEN || "";
const DASH_USER = process.env.DASH_USER || "admin";
const DASH_PASSWORD = process.env.DASH_PASSWORD || "";
const STALE_SEC = Number(process.env.STALE_SEC || 120);
const MAX_POINTS = 2880; // equity body na jednu instanci (≈ 24 h při 30 s)

if (!PUSH_TOKEN || !DASH_PASSWORD) {
  console.error("Chybí PUSH_TOKEN nebo DASH_PASSWORD v proměnných prostředí.");
  process.exit(1);
}

const { computeStats, baskets } = require("./stats");
const news = require("./news");
news.start();
const REPORT_KEY = process.env.REPORT_KEY || "";
const notify = require("./notify");
notify.init({
  publicKey: process.env.VAPID_PUBLIC_KEY,
  privateKey: process.env.VAPID_PRIVATE_KEY,
  subject: process.env.VAPID_SUBJECT,
});

const instances = {}; // id -> { status, lastSeen, equity: [[ts, equity, balance]] }
// id -> { symbol, magic, account, currency, balance, updated, full, trades: Map, flow: Map, hours: Map }
const hist = new Map();
const MAX_HOURS = 24 * 400;

// --- ovládání na dálku: fronta příkazů pro jednotlivé instance EA
const ACTIONS = { pause: "Pozastavit", resume: "Obnovit", close_basket: "Zavřít koš", set: "Změna nastavení" };

// parametry, které lze měnit z aplikace: [typ, min, max]
const PARAMS = {
  StochK: ["int", 1, 100], StochD: ["int", 1, 100], StochSlowing: ["int", 1, 100],
  UseLevels: ["bool"], LevelLow: ["num", 0, 50], LevelHigh: ["num", 50, 100], ZoneLookbackBars: ["int", 2, 20],
  TradeDirection: ["int", 0, 2],
  StartLots: ["num", 0.01, 100], LotMultiplier: ["num", 1, 5], MaxOrders: ["int", 1, 20],
  TPFromADR: ["bool"], TakeProfitPips: ["num", 0, 1000], TP_ADRPercent: ["num", 1, 200],
  BasketTPPips: ["num", 0, 500], BasketTP_ADRPercent: ["num", 0, 100],
  StopLossPips: ["num", 0, 2000], HardStopPips: ["num", 0, 5000],
  GridMode: ["int", 0, 2], GridStepPips: ["num", 1, 1000], ADRPercent: ["num", 1, 100],
  UseTrailing: ["bool"], TrailStartPips: ["num", 0, 1000], TrailDistancePips: ["num", 1, 1000],
  TradeStartCET: ["time"], TradeEndCET: ["time"],
  MaxAccountDDPercent: ["num", 0, 99],
  BasketTPMode: ["int", 0, 1], BasketTPPerPos: ["num", 0.1, 10],
  MaxDailyLossPercent: ["num", 0, 99],
  UseVolatilityGuard: ["bool"], VolATRMult: ["num", 0.3, 10], VolPauseMinutes: ["int", 5, 1440],
  UseTrendFilter: ["bool"], TrendDistanceATR: ["num", 0.5, 20],
  UseFridayGuard: ["bool"], MaxBasketHours: ["num", 0, 720],
};
function normParams(obj) {
  const out = [];
  for (const [k, v0] of Object.entries(obj || {})) {
    const spec = PARAMS[k];
    if (!spec) throw new Error("neznámý parametr " + k);
    const [type, min, max] = spec;
    let v;
    if (type === "bool") v = v0 === true || v0 === "true" ? "true" : v0 === false || v0 === "false" ? "false" : null;
    else if (type === "time") v = /^([01]?\d|2[0-4]):[0-5]\d$/.test(String(v0)) ? String(v0).padStart(5, "0") : null;
    else {
      const n = Number(v0);
      if (!isFinite(n) || n < min || n > max || (type === "int" && !Number.isInteger(n))) v = null;
      else v = type === "int" ? String(n) : String(Math.round(n * 100) / 100);
    }
    if (v === null) throw new Error(`neplatná hodnota ${k} = ${v0}`);
    out.push(`${k}=${v}`);
  }
  if (!out.length) throw new Error("žádná změna");
  return out.join(";");
}
const CMD_TTL_MS = 10 * 60 * 1000;
const cmds = new Map(); // id -> [{ cid, action, created }]
const cmdDone = new Map(); // id -> [{ cid, action, created, done, result }] (posledních 5)
let cidSeq = Date.now() % 1000000000; // rostoucí, unikátní i po restartu serveru

function pendingFor(id) {
  const now = Date.now();
  const list = (cmds.get(id) || []).filter((c) => now - c.created < CMD_TTL_MS);
  cmds.set(id, list);
  return list;
}
const html = fs.readFileSync(path.join(__dirname, "dashboard.html"), "utf8");

const publicHtml = fs.readFileSync(path.join(__dirname, "public.html"), "utf8");

// Veřejná data: jen procenta, počty a úspěšnost – žádné částky ani velikost účtu.
function publicData(cfg) {
  const st = computeStats(hist, cfg.asset);
  const live = Object.values(instances).some((i) => Date.now() - i.lastSeen < STALE_SEC * 1000);
  if (st.empty) return { empty: true, live };
  const label = cfg.asset === "ALL" ? "Všechna aktiva" : ((st.options.find((o) => o.id === cfg.asset) || {}).label || "");
  const pick = (r) => (r ? { trades: r.trades, winPct: r.winPct, pct: r.pct, ddPct: r.hasDD ? r.ddPct : null } : null);
  return {
    label, live, updated: Date.now(),
    current: { day: pick(st.current.day), week: pick(st.current.week), month: pick(st.current.month), year: pick(st.current.year), total: pick(st.current.total) },
    days: st.tables.day.slice(0, 31).map((r) => ({ key: r.key, ...pick(r) })),
    months: st.tables.month.slice(0, 12).map((r) => ({ key: r.key, ...pick(r) })),
    calendar: (st.calendar || []).map((d) => ({ k: d.k, p: d.p, t: d.t })),
    recent: (st.recent || []).slice(0, 15).map((b) => ({ sym: b.sym, dir: b.dir, n: b.n, close: b.close, dur: b.close - b.open, pct: b.pct })),
  };
}

function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}

// --- přihlášení: cookie podepsaná heslem (iPhone v režimu aplikace neumí okno Basic auth)
const SESSION_DAYS = 365;
const sessionSig = (exp) => crypto.createHmac("sha256", DASH_PASSWORD).update(`${DASH_USER}|${exp}`).digest("hex");

function cookies(req) {
  const out = {};
  for (const part of String(req.headers.cookie || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function authorized(req) {
  // 1) cookie z přihlašovací obrazovky
  const c = cookies(req).sg_session || "";
  const [exp, sig] = c.split(".");
  if (exp && sig && Number(exp) > Date.now() && safeEqual(sig, sessionSig(exp))) return true;
  // 2) Basic auth (starší způsob, pro jistotu ponechán)
  const h = req.headers.authorization || "";
  if (!h.startsWith("Basic ")) return false;
  const [user, ...rest] = Buffer.from(h.slice(6), "base64").toString("utf8").split(":");
  return safeEqual(user, DASH_USER) && safeEqual(rest.join(":"), DASH_PASSWORD);
}

function loginPage(error) {
  return `<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>StochGrid AOS</title>
<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0e1116"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="StochGrid">
<style>
  :root{--bg:#0e1116;--card:#161a22;--line:#2a313d;--text:#dce0e6;--dim:#828a96;--head:#fad264;--bad:#eb5a5a}
  *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
  body{display:flex;align-items:center;justify-content:center;padding:24px 16px;min-height:100vh}
  form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px 22px}
  img{width:64px;height:64px;border-radius:14px;display:block;margin:0 auto 14px}
  h1{font-size:20px;text-align:center;color:var(--head);margin:0 0 4px}
  p{text-align:center;color:var(--dim);font-size:13px;margin:0 0 20px}
  label{display:block;font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin:12px 0 6px}
  input{width:100%;font:inherit;font-size:16px;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:11px 12px}
  input:focus{outline:none;border-color:var(--head)}
  button{width:100%;margin-top:20px;font:inherit;font-size:16px;font-weight:600;color:#0e1116;background:var(--head);border:0;border-radius:8px;padding:12px;cursor:pointer}
  .err{color:var(--bad);text-align:center;font-size:13px;margin-top:12px}
  .copy{text-align:center;font-size:11px;color:var(--dim);margin-top:18px}
</style></head><body>
<form method="post" action="/login">
  <img src="/icon-192.png" alt="">
  <h1>StochGrid AOS</h1><p>Přihlaste se do panelu</p>
  <label for="u">Jméno</label><input id="u" name="user" autocomplete="username" autocapitalize="none" required>
  <label for="p">Heslo</label><input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Přihlásit</button>
  ${error ? `<div class="err">${error}</div>` : ""}
  <div class="copy">© 2026 StochGrid AOS</div>
</form></body></html>`;
}

function send(res, code, body, type = "application/json; charset=utf-8", extra = {}) {
  res.writeHead(code, {
    "Content-Type": type,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    ...extra,
  });
  res.end(body);
}

function readBody(req, limit = 256 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > limit) { reject(new Error("too large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

// Křivka equity: trvalá hodinová historie od EA (přežije restart serveru) + živé body od posledního startu.
// Hodiny posledních 30 dní po hodině, starší po dnech. Bod = [ms, equity, balance|null].
function equitySeries(id, live) {
  const h = hist.get(id);
  const out = [];
  if (h && h.hours && h.hours.size) {
    const firstLive = live.length ? live[0][0] : Infinity;
    const cut = Date.now() - 30 * 86400000;
    const days = new Map();
    for (const hr of [...h.hours.keys()].sort((a, b) => a - b)) {
      const ms = hr * 1000;
      if (ms >= firstLive - 3600000) break;
      const v = h.hours.get(hr);
      const eq = Math.round((v[0] + v[1]) / 2 * 100) / 100;
      if (!(eq > 0)) continue;
      if (ms < cut) days.set(Math.floor(ms / 86400000), [ms, eq, null]);
      else out.push([ms, eq, null]);
    }
    out.unshift(...days.values());
  }
  return out.length ? out.concat(live) : live;
}

// ---- textový report za posledních N hodin (pro denní hodnocení)
const tPrague = (sec) => new Date(sec * 1000).toLocaleString("cs-CZ", { timeZone: "Europe/Prague", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
const f2 = (x) => (typeof x === "number" && isFinite(x) ? x.toFixed(2) : "–");
function buildReport(hrs) {
  const now = Math.floor(Date.now() / 1000), from = now - hrs * 3600;
  const L = [];
  L.push(`StochGrid AOS – report za posledních ${hrs} h (do ${tPrague(now)} Praha)`);
  const snap = snapshot();
  for (const it of snap.instances) {
    const s = it.status || {}, a = s.account || {}, b = s.basket || {}, cur = a.currency || "";
    L.push("", `=== ${s.symbol || it.id} ${s.tf || ""} · EA v${s.version || "?"} · ${it.online ? "online" : "OFFLINE (" + it.ageSec + " s)"}`);
    L.push(`Stav: ${s.state || "?"} · obchodování ${s.tradeAllowed === false ? "ZAKÁZÁNO" : "povoleno"} · hodiny ${s.hours || "?"} · zprávy: ${s.news || "?"}${s.newsBlocked ? " (BLOKACE)" : ""}`);
    L.push(`Účet: balance ${f2(a.balance)} ${cur}, equity ${f2(a.equity)}, DD ${f2(a.ddPct)} % (limit ${a.ddLimit ?? "?"} %), den ${f2(a.dayPct)} %, spread ${a.spread ?? "?"}`);
    L.push(`Signál: ${s.signal || "?"} · odstup ${s.grid || "?"} · ADR ${(s.riskInputs || {}).adr ?? "?"} p`);
    if (b.count) L.push(`OTEVŘENÝ KOŠ: ${b.dir || ""} ${b.count}/${b.max} pozic, ${f2(b.lots)} lot, průměr ${b.avg}, P/L ${f2(b.pl)} ${cur}`);
    else L.push(`Koš: žádný otevřený (max ${b.max ?? "?"} pozic)`);
    const r = s.risk || {};
    if (r.newBasketLoss != null) L.push(`Riziko plného koše na hard stopu: ${f2(r.newBasketLoss)} ${cur} (${f2(r.newBasketPct)} % balance)`);
    L.push(`Ochrany: ${s.guard || "v klidu"}${s.trend ? " · trend H1: " + s.trend : ""} · další zpráva: ${s.nextNews || "?"}`);
    if (s.settings) L.push(`Nastavení: ${Object.entries(s.settings).map(([k, v]) => k + "=" + v).join(", ")}`);
    const h = hist.get(it.id);
    if (!h) { L.push("Historie: zatím žádná"); continue; }
    const bs = baskets([...h.trades.values()]).filter((x) => x.c >= from).sort((x, y) => x.c - y.c);
    const hours = [...h.hours.entries()].sort((x, y) => x[0] - y[0]);
    L.push(`Uzavřené koše (${bs.length}):`);
    let sum = 0;
    for (const k of bs) {
      sum += k.p;
      // nejhorší plovoucí ztráta koše ≈ min P/L EA během koše − P/L EA před otevřením (z hodinových dat)
      const before = hours.filter(([t]) => t + 3600 <= k.o).pop();
      const during = hours.filter(([t]) => t + 3600 > k.o && t <= k.c);
      let worst = "";
      if (before && during.length) worst = ` · nejhůř ≈ ${f2(Math.min(...during.map(([, v]) => v[3])) - before[1][3])}`;
      const trades = [...h.trades.values()].filter((t) => t.c >= k.o - 1 && t.c <= k.c + 1);
      const lots = trades.reduce((x, t) => x + (t.l || 0), 0);
      L.push(`  ${tPrague(k.o)} → ${tPrague(k.c)} · ${k.ty === 0 ? "BUY" : k.ty === 1 ? "SELL" : "?"} · ${k.n} poz. · ${f2(lots)} lot · ${Math.round((k.c - k.o) / 60)} min · ${f2(k.p)} ${cur}${worst}`);
    }
    L.push(`Součet za období: ${f2(sum)} ${cur}`);
    const hh = hours.filter(([t]) => t >= from);
    if (hh.length) L.push(`Equity za období: max ${f2(Math.max(...hh.map(([, v]) => v[0])))} · min ${f2(Math.min(...hh.map(([, v]) => v[1])))} (${hh.length} hodin dat)`);
    try {
      const st = computeStats(hist, it.id);
      const c = st.current || {};
      for (const [lbl, k] of [["Den", "day"], ["Týden", "week"], ["Měsíc", "month"], ["Celkem", "total"]]) {
        const x = c[k]; if (!x) continue;
        L.push(`${lbl}: ${x.trades} košů, úspěšnost ${x.winPct == null ? "–" : x.winPct.toFixed(0) + " %"}, ${f2(x.net)} ${cur} (${f2(x.pct)} %), max DD ${f2(x.ddPct)} %`);
      }
    } catch (e) { L.push("Statistiky: chyba " + e.message); }
    const done = (cmdDone.get(it.id) || []).filter((d) => (d.done || 0) / 1000 >= from);
    if (done.length) L.push(`Příkazy z aplikace: ${done.map((d) => d.action + " → " + (d.result || "")).join("; ")}`);
  }
  const curs = new Set();
  for (const it of snap.instances) { const sy = String((it.status || {}).symbol || ""); curs.add(sy.slice(0, 3)); curs.add(sy.slice(3, 6)); }
  const upcoming = news.list(now, now + 36 * 3600).filter((e) => curs.has(e.cur));
  L.push("", `Zprávy s vysokým dopadem (příštích 36 h, ${[...curs].join("/")}):`);
  if (!upcoming.length) L.push(news.status().lastOk ? "  žádné" : "  kalendář nenačten: " + (news.status().lastErr || "?"));
  for (const e of upcoming) L.push(`  ${tPrague(e.t)} ${e.cur} ${e.title}`);
  const notes = notify.recent().filter((n) => n.t / 1000 >= from);
  if (notes.length) { L.push("", "Upozornění za období:"); for (const n of notes) L.push(`  ${tPrague(n.t / 1000)} ${n.title} – ${n.body}`); }
  return L.join("\n") + "\n";
}

function snapshot() {
  const now = Date.now();
  return {
    serverNow: now,
    calendar: { events: news.list(Math.floor(now / 1000) - 3600, Math.floor(now / 1000) + 4 * 86400), ...news.status() },
    staleSec: STALE_SEC,
    instances: Object.entries(instances)
      .map(([id, v]) => ({
        id,
        lastSeen: v.lastSeen,
        ageSec: Math.round((now - v.lastSeen) / 1000),
        online: now - v.lastSeen < STALE_SEC * 1000,
        status: v.status,
        equity: equitySeries(id, v.equity),
        pending: pendingFor(id).map((c) => ({ cid: c.cid, action: c.action, label: ACTIONS[c.action], payload: c.payload || "", ageSec: Math.round((now - c.created) / 1000) })),
        done: (cmdDone.get(id) || []).slice(0, 3),
      }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");

  if (url.pathname === "/healthz") return send(res, 200, "ok", "text/plain");

  // ikony a manifest – veřejné (telefon je stahuje bez přihlášení), neobsahují žádná data
  const STATIC = {
    "/icon.svg": "image/svg+xml",
    "/favicon.ico": "image/png",
    "/apple-touch-icon.png": "image/png",
    "/apple-touch-icon-precomposed.png": "image/png",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
    "/icon-maskable.png": "image/png",
    "/manifest.webmanifest": "application/manifest+json",
    "/sw.js": "text/javascript; charset=utf-8",
    "/calendar.js": "text/javascript; charset=utf-8",
  };
  if (STATIC[url.pathname]) {
    const file = { "/favicon.ico": "icon-192.png", "/apple-touch-icon-precomposed.png": "apple-touch-icon.png" }[url.pathname]
      || url.pathname.slice(1);
    try {
      const data = fs.readFileSync(path.join(__dirname, file));
      res.writeHead(200, {
        "Content-Type": STATIC[url.pathname],
        "Cache-Control": url.pathname.endsWith(".js") ? "no-cache" : "public, max-age=86400",
      });
      return res.end(data);
    } catch (e) {
      return send(res, 404, "not found", "text/plain");
    }
  }

  // --- EA -> server
  if (url.pathname === "/api/push" && req.method === "POST") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, '{"error":"bad token"}');
    let data;
    try {
      // MQL4 může na konec přidat nulový znak
      data = JSON.parse((await readBody(req)).replace(/\u0000+$/, ""));
    } catch (e) {
      return send(res, 400, '{"error":"bad json"}');
    }
    const id = String(data.id || `${data.symbol || "?"}_${data.magic || 0}`).slice(0, 64);
    const inst = instances[id] || (instances[id] = { equity: [] });
    const prevStatus = inst.status;
    inst.status = data;
    inst.lastSeen = Date.now();
    try { notify.onStatus(prevStatus, data); } catch (e) { console.error("notify", e); }
    if (inst.offline) { inst.offline = false; notify.onOnline(data.symbol || id); }
    const acc = data.account || {};
    if (typeof acc.equity === "number") {
      inst.equity.push([inst.lastSeen, acc.equity, acc.balance]);
      if (inst.equity.length > MAX_POINTS) inst.equity.splice(0, inst.equity.length - MAX_POINTS);
    }
    const h = hist.get(id);
    return send(res, 200, JSON.stringify({
      ok: true, needHistory: !(h && h.full),
      subsVersion: notify.getVersion(), needSubs: notify.needRestore(),
    }));
  }

  // --- EA si stahuje kalendář zpráv (text "utc|měna|název" po řádcích)
  if (url.pathname === "/api/news" && req.method === "POST") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    const n = news.importText((await readBody(req)).replace(/\u0000+$/, ""));
    return send(res, 200, JSON.stringify({ ok: true, imported: n }));
  }
  if (url.pathname === "/api/news" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    return send(res, 200, news.eaText(), "text/plain; charset=utf-8");
  }

  // --- EA si vyzvedává příkazy (text "cid|akce", prázdné = nic)
  if (url.pathname === "/api/cmd/poll" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    const c = pendingFor(String(url.searchParams.get("id") || ""))[0];
    return send(res, 200, c ? `${c.cid}|${c.action}` + (c.payload ? `|${c.payload}` : "") : "", "text/plain; charset=utf-8");
  }
  // --- EA potvrzuje provedení
  if (url.pathname === "/api/cmd/ack" && req.method === "POST") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, '{"error":"bad token"}');
    let d; try { d = JSON.parse((await readBody(req)).replace(/\u0000+$/, "")); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
    const id = String(d.id || ""), cid = Number(d.cid);
    const list = cmds.get(id) || [];
    const c = list.find((x) => x.cid === cid);
    if (c) {
      cmds.set(id, list.filter((x) => x.cid !== cid));
      const done = cmdDone.get(id) || [];
      done.unshift({ cid, action: c.action, label: ACTIONS[c.action], created: c.created, done: Date.now(), result: String(d.result || "").slice(0, 200) });
      cmdDone.set(id, done.slice(0, 5));
      const sym = (instances[id] && instances[id].status && instances[id].status.symbol) || id;
      notify.onCommand(sym, ACTIONS[c.action], String(d.result || ""), c.action === "set" ? "settings" : "halt");
    }
    return send(res, 200, '{"ok":true}');
  }

  // --- záloha registrací telefonů přes EA
  if (url.pathname === "/api/subs/export" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    return send(res, 200, notify.exportData());
  }
  if (url.pathname === "/api/subs/import" && req.method === "POST") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, '{"error":"bad token"}');
    const n = notify.importData((await readBody(req)).replace(/\u0000+$/, ""));
    return send(res, 200, JSON.stringify({ ok: true, imported: n }));
  }

  // --- EA -> server: historie obchodů, pohybů na účtu a hodinové equity
  if (url.pathname === "/api/history" && req.method === "POST") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, '{"error":"bad token"}');
    let d;
    try {
      d = JSON.parse((await readBody(req, 8 * 1024 * 1024)).replace(/\u0000+$/, ""));
    } catch (e) {
      return send(res, 400, '{"error":"bad json"}');
    }
    const id = String(d.id || "").slice(0, 64);
    if (!id) return send(res, 400, '{"error":"missing id"}');
    let h = hist.get(id);
    if (!h) {
      h = { trades: new Map(), flow: new Map(), hours: new Map() };
      hist.set(id, h);
    }
    // obchody, pohyby i hodinová data se jen slučují – VPS může vidět jen část historie,
    // domácí MT4 s celou historií pak doplní zbytek a nic se neztratí
    Object.assign(h, {
      symbol: String(d.symbol || "?"), magic: d.magic || 0, account: d.account || 0,
      currency: String(d.currency || ""), balance: Number(d.balance) || 0, updated: Date.now(),
    });
    if (d.full) h.full = true;
    for (const t of d.trades || []) {
      // [ticket, closeUTC, openUTC, type, lots, profit]
      h.trades.set(t[0], { c: t[1], o: t[2], ty: t[3], l: t[4], p: t[5] });
    }
    for (const f of d.flow || []) h.flow.set(f[0], { t: f[1], a: f[2], dep: f[3] === 1 }); // [ticket, timeUTC, amount, 1 = vklad/výběr]
    for (const r of d.hours || []) {
      // [hourUTC, eqHigh, eqLow, eaHigh, eaLow] – sloučit s tím, co už máme
      const e = h.hours.get(r[0]);
      h.hours.set(r[0], e ? [Math.max(e[0], r[1]), Math.min(e[1], r[2]), Math.max(e[2], r[3]), Math.min(e[3], r[4])]
                          : [r[1], r[2], r[3], r[4]]);
    }
    if (h.hours.size > MAX_HOURS) {
      const keys = [...h.hours.keys()].sort((a, b) => a - b);
      for (const k of keys.slice(0, h.hours.size - MAX_HOURS)) h.hours.delete(k);
    }
    return send(res, 200, JSON.stringify({ ok: true, trades: h.trades.size, hours: h.hours.size }));
  }

  // --- server -> EA: historie obchodů a pohybů na účtu (EA si ji uloží do souboru)
  if (url.pathname === "/api/trades" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    const h = hist.get(String(url.searchParams.get("id") || ""));
    if (!h) return send(res, 200, "", "text/plain; charset=utf-8");
    const lines = [];
    for (const [tk, x] of h.trades) lines.push(`T,${tk},${x.c},${x.o},${x.ty},${Number(x.l).toFixed(2)},${Number(x.p).toFixed(2)}`);
    for (const [tk, f] of h.flow) lines.push(`F,${tk},${f.t},${Number(f.a).toFixed(2)},${f.dep ? 1 : 0}`);
    return send(res, 200, lines.join("\n"), "text/plain; charset=utf-8");
  }

  // --- server -> EA: obnova hodinových dat po restartu EA (text, snadno čitelný v MQL4)
  if (url.pathname === "/api/hours" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    const h = hist.get(String(url.searchParams.get("id") || ""));
    const lines = h ? [...h.hours.entries()].sort((a, b) => a[0] - b[0]).map(([k, v]) => [k, ...v].join(",")) : [];
    return send(res, 200, lines.join("\n"), "text/plain; charset=utf-8");
  }

  // --- veřejná stránka (bez přihlášení, jen % údaje)
  const pm = url.pathname.match(/^\/p\/([A-Za-z0-9_-]{16,64})(\/data)?$/);
  if (pm) {
    const cfg = notify.getShare();
    if (!cfg.enabled || !cfg.token || !safeEqual(pm[1], cfg.token)) {
      return send(res, 404, pm[2] ? '{"error":"odkaz neplatný"}' : publicHtml.replace("__GONE__", "1"), pm[2] ? undefined : "text/html; charset=utf-8");
    }
    if (pm[2]) return send(res, 200, JSON.stringify(publicData(cfg)));
    return send(res, 200, publicHtml.replace("__GONE__", "0"), "text/html; charset=utf-8", { "X-Robots-Tag": "noindex, nofollow" });
  }

  // --- přihlášení
  // textový report pro denní hodnocení (čtení s klíčem, bez přihlášení)
  if (url.pathname === "/api/report" && req.method === "GET") {
    if (!REPORT_KEY || !safeEqual(String(url.searchParams.get("key") || ""), REPORT_KEY)) return send(res, 401, "bad key", "text/plain");
    const hrs = Math.min(24 * 14, Math.max(1, Number(url.searchParams.get("hours")) || 24));
    return send(res, 200, buildReport(hrs), "text/plain; charset=utf-8");
  }

  if (url.pathname === "/login" && req.method === "POST") {
    const form = new URLSearchParams(await readBody(req, 4096));
    await new Promise((r) => setTimeout(r, 400)); // zpomalení zkoušení hesel
    if (safeEqual(form.get("user") || "", DASH_USER) && safeEqual(form.get("password") || "", DASH_PASSWORD)) {
      const exp = Date.now() + SESSION_DAYS * 86400000;
      res.writeHead(303, {
        Location: "/",
        "Set-Cookie": `sg_session=${exp}.${sessionSig(exp)}; Max-Age=${SESSION_DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Lax`,
        "Cache-Control": "no-store",
      });
      return res.end();
    }
    return send(res, 401, loginPage("Špatné jméno nebo heslo."), "text/html; charset=utf-8");
  }
  if (url.pathname === "/logout") {
    res.writeHead(303, { Location: "/", "Set-Cookie": "sg_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax" });
    return res.end();
  }

  // --- prohlížeč (přihlášený)
  if (!authorized(req)) {
    if (url.pathname.startsWith("/api/")) return send(res, 401, '{"error":"login"}');
    return send(res, 200, loginPage(), "text/html; charset=utf-8");
  }

  if (url.pathname === "/api/state") return send(res, 200, JSON.stringify(snapshot()));
  // --- ovládání na dálku (prohlížeč): { id: "<instance>" | "ALL", action }
  if (url.pathname === "/api/cmd" && req.method === "POST") {
    let d; try { d = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
    const action = String(d.action || "");
    if (!ACTIONS[action]) return send(res, 400, '{"error":"neznámý příkaz"}');
    const ids = d.id === "ALL" ? Object.keys(instances) : [String(d.id || "")].filter((x) => instances[x]);
    if (!ids.length) return send(res, 404, '{"error":"instance nenalezena"}');
    let payload = "";
    if (action === "set") {
      if (d.id === "ALL") return send(res, 400, '{"error":"nastavení jen pro jeden trh"}');
      try { payload = normParams(d.params); } catch (e) { return send(res, 400, JSON.stringify({ error: e.message })); }
    }
    const out = [];
    for (const id of ids) {
      const list = pendingFor(id).filter((c) => c.action !== action); // stejný příkaz jen jednou
      const c = { cid: ++cidSeq, action, created: Date.now(), payload, by: "app" };
      list.push(c);
      cmds.set(id, list);
      out.push({ id, cid: c.cid });
    }
    return send(res, 200, JSON.stringify({ ok: true, queued: out }));
  }

  // --- nastavení veřejné stránky
  if (url.pathname === "/api/share" && req.method === "GET") {
    return send(res, 200, JSON.stringify(notify.getShare()));
  }
  if (url.pathname === "/api/share" && req.method === "POST") {
    let d; try { d = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
    const cur = notify.getShare();
    const token = d.regenerate || !cur.token ? crypto.randomBytes(18).toString("base64url") : cur.token;
    return send(res, 200, JSON.stringify(notify.setShare({
      enabled: d.enabled !== undefined ? !!d.enabled : cur.enabled,
      asset: d.asset || cur.asset, token,
    })));
  }

  // --- upozornění (prohlížeč)
  if (url.pathname === "/api/notify/config") {
    const ep = url.searchParams.get("endpoint");
    return send(res, 200, JSON.stringify({
      enabled: notify.enabled(), publicKey: notify.publicKey(), events: notify.EVENTS,
      prefs: ep ? notify.prefsFor(ep) : null, devices: notify.count(), recent: notify.recent(),
    }));
  }
  if (url.pathname === "/api/notify/settings") {
    if (req.method === "POST") {
      let d; try { d = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
      return send(res, 200, JSON.stringify(notify.setSettings(d)));
    }
    return send(res, 200, JSON.stringify(notify.getSettings()));
  }
  if (url.pathname === "/api/notify/subscribe" && req.method === "POST") {
    let d; try { d = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
    return send(res, notify.subscribe(d.subscription, d.prefs) ? 200 : 400, '{"ok":true}');
  }
  if (url.pathname === "/api/notify/unsubscribe" && req.method === "POST") {
    let d; try { d = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
    notify.unsubscribe(String(d.endpoint || ""));
    return send(res, 200, '{"ok":true}');
  }
  if (url.pathname === "/api/notify/test" && req.method === "POST") {
    let d; try { d = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, '{"error":"bad json"}'); }
    const code = await notify.test(String(d.endpoint || ""));
    return send(res, 200, JSON.stringify({ ok: code >= 200 && code < 300, code }));
  }

  if (url.pathname === "/api/stats") {
    try {
      return send(res, 200, JSON.stringify(computeStats(hist, url.searchParams.get("symbol") || "ALL")));
    } catch (e) {
      console.error("stats", e);
      return send(res, 500, '{"error":"stats failed"}');
    }
  }
  if (url.pathname === "/" || url.pathname === "/index.html") return send(res, 200, html, "text/html; charset=utf-8");

  send(res, 404, '{"error":"not found"}');
});

// hlídání výpadku: instance, která déle než STALE_SEC neposlala stav
setInterval(() => {
  const now = Date.now();
  for (const [id, inst] of Object.entries(instances)) {
    const age = (now - inst.lastSeen) / 1000;
    if (!inst.offline && age > STALE_SEC + 60) {
      inst.offline = true;
      notify.onOffline((inst.status && inst.status.symbol) || id, age);
    }
  }
}, 30000);

server.listen(PORT, () => console.log(`StochGrid panel běží na portu ${PORT}`));
