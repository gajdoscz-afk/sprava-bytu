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

const instances = {}; // id -> { status, lastSeen, equity: [[ts, equity, balance]] }
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
    return send(res, 200, '{"ok":true}');
  }

  // --- prohlížeč (heslo)
  if (!authorized(req)) {
    return send(res, 401, "Přihlášení vyžadováno", "text/plain; charset=utf-8", {
      "WWW-Authenticate": 'Basic realm="StochGrid AOS", charset="UTF-8"',
    });
  }

  if (url.pathname === "/api/state") return send(res, 200, JSON.stringify(snapshot()));
  if (url.pathname === "/" || url.pathname === "/index.html") return send(res, 200, html, "text/html; charset=utf-8");

  send(res, 404, '{"error":"not found"}');
});

server.listen(PORT, () => console.log(`StochGrid panel běží na portu ${PORT}`));
