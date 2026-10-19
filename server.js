// StochGrid AOS – webový panel
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

const { computeStats } = require("./stats");
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
const ACTIONS = { pause: "Pozastavit", resume: "Obnovit", close_basket: "Zavřít koš" };
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
</style></head><body>
<form method="post" action="/login">
  <img src="/icon-192.png" alt="">
  <h1>StochGrid AOS</h1><p>Přihlaste se do panelu</p>
  <label for="u">Jméno</label><input id="u" name="user" autocomplete="username" autocapitalize="none" required>
  <label for="p">Heslo</label><input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Přihlásit</button>
  ${error ? `<div class="err">${error}</div>` : ""}
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

function snapshot() {
  const now = Date.now();
  return {
    serverNow: now,
    staleSec: STALE_SEC,
    instances: Object.entries(instances)
      .map(([id, v]) => ({
        id,
        lastSeen: v.lastSeen,
        ageSec: Math.round((now - v.lastSeen) / 1000),
        online: now - v.lastSeen < STALE_SEC * 1000,
        status: v.status,
        equity: v.equity,
        pending: pendingFor(id).map((c) => ({ cid: c.cid, action: c.action, label: ACTIONS[c.action], ageSec: Math.round((now - c.created) / 1000) })),
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
  };
  if (STATIC[url.pathname]) {
    const file = { "/favicon.ico": "icon-192.png", "/apple-touch-icon-precomposed.png": "apple-touch-icon.png" }[url.pathname]
      || url.pathname.slice(1);
    try {
      const data = fs.readFileSync(path.join(__dirname, file));
      res.writeHead(200, {
        "Content-Type": STATIC[url.pathname],
        "Cache-Control": url.pathname === "/sw.js" ? "no-cache" : "public, max-age=86400",
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

  // --- EA si vyzvedává příkazy (text "cid|akce", prázdné = nic)
  if (url.pathname === "/api/cmd/poll" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    const c = pendingFor(String(url.searchParams.get("id") || ""))[0];
    return send(res, 200, c ? `${c.cid}|${c.action}` : "", "text/plain; charset=utf-8");
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
      notify.onCommand(sym, ACTIONS[c.action], String(d.result || ""));
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
    // plné odeslání nahradí obchody a pohyby (zdroj pravdy je historie MT4);
    // hodinová data se vždy jen slučují, aby se nic neztratilo
    if (d.full) { h.trades = new Map(); h.flow = new Map(); }
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

  // --- server -> EA: obnova hodinových dat po restartu EA (text, snadno čitelný v MQL4)
  if (url.pathname === "/api/hours" && req.method === "GET") {
    if (!safeEqual(req.headers["x-token"] || "", PUSH_TOKEN)) return send(res, 401, "bad token", "text/plain");
    const h = hist.get(String(url.searchParams.get("id") || ""));
    const lines = h ? [...h.hours.entries()].sort((a, b) => a[0] - b[0]).map(([k, v]) => [k, ...v].join(",")) : [];
    return send(res, 200, lines.join("\n"), "text/plain; charset=utf-8");
  }

  // --- přihlášení
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
    const out = [];
    for (const id of ids) {
      const list = pendingFor(id).filter((c) => c.action !== action); // stejný příkaz jen jednou
      const c = { cid: ++cidSeq, action, created: Date.now() };
      list.push(c);
      cmds.set(id, list);
      out.push({ id, cid: c.cid });
    }
    return send(res, 200, JSON.stringify({ ok: true, queued: out }));
  }

  // --- upozornění (prohlížeč)
  if (url.pathname === "/api/notify/config") {
    const ep = url.searchParams.get("endpoint");
    return send(res, 200, JSON.stringify({
      enabled: notify.enabled(), publicKey: notify.publicKey(), events: notify.EVENTS,
      prefs: ep ? notify.prefsFor(ep) : null, devices: notify.count(), recent: notify.recent(),
    }));
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
