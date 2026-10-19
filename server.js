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

const instances = {}; // id -> { status, lastSeen, equity: [[ts, equity, balance]] }
// id -> { symbol, magic, account, currency, balance, updated, full, trades: Map, flow: Map, hours: Map }
const hist = new Map();
const MAX_HOURS = 24 * 400;
const html = fs.readFileSync(path.join(__dirname, "dashboard.html"), "utf8");

function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}

function authorized(req) {
  const h = req.headers.authorization || "";
  if (!h.startsWith("Basic ")) return false;
  const [user, ...rest] = Buffer.from(h.slice(6), "base64").toString("utf8").split(":");
  return safeEqual(user, DASH_USER) && safeEqual(rest.join(":"), DASH_PASSWORD);
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
  };
  if (STATIC[url.pathname]) {
    const file = { "/favicon.ico": "icon-192.png", "/apple-touch-icon-precomposed.png": "apple-touch-icon.png" }[url.pathname]
      || url.pathname.slice(1);
    try {
      const data = fs.readFileSync(path.join(__dirname, file));
      res.writeHead(200, { "Content-Type": STATIC[url.pathname], "Cache-Control": "public, max-age=86400" });
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
    inst.status = data;
    inst.lastSeen = Date.now();
    const acc = data.account || {};
    if (typeof acc.equity === "number") {
      inst.equity.push([inst.lastSeen, acc.equity, acc.balance]);
      if (inst.equity.length > MAX_POINTS) inst.equity.splice(0, inst.equity.length - MAX_POINTS);
    }
    const h = hist.get(id);
    return send(res, 200, JSON.stringify({ ok: true, needHistory: !(h && h.full) }));
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

  // --- prohlížeč (heslo)
  if (!authorized(req)) {
    return send(res, 401, "Přihlášení vyžadováno", "text/plain; charset=utf-8", {
      "WWW-Authenticate": 'Basic realm="StochGrid AOS", charset="UTF-8"',
    });
  }

  if (url.pathname === "/api/state") return send(res, 200, JSON.stringify(snapshot()));
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

server.listen(PORT, () => console.log(`StochGrid panel běží na portu ${PORT}`));
