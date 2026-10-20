// © 2026 Michal Gajdos, všechna práva vyhrazena.
// Ekonomický kalendář: zprávy s vysokým dopadem (stahuje server, EA si je čte přes /api/news).
const https = require("https");

// příští týden zdroj zveřejňuje až během týdne (404 je normální).
// Sdílené IP Renderu zdroj občas omezuje (429) – pak kalendář dodá EA z VPS (POST /api/news).
const FEEDS = [
  ["https://nfs.faireconomy.media/ff_calendar_thisweek.json"],
  ["https://nfs.faireconomy.media/ff_calendar_nextweek.json"],
];
const REFRESH_MS = 2 * 3600 * 1000;   // zdroj žádá šetrné stahování
const RETRY_MS = 60 * 60 * 1000;

let events = new Map();   // klíč "utc|měna|název" -> { t (s), cur, title }
let lastOk = 0, lastErr = "", lastTry = 0;

function get(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { "User-Agent": "StochGrid-AOS/1.0" }, timeout: 20000 }, (res) => {
      if (res.statusCode !== 200) { res.resume(); return reject(new Error("HTTP " + res.statusCode)); }
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    });
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
  });
}

async function refresh() {
  lastTry = Date.now();
  let got = 0, anyOk = false;
  const errs = [];
  for (const urls of FEEDS) {
    let ok = false;
    for (const url of urls) {
      try {
        const arr = JSON.parse(await get(url));
        for (const e of Array.isArray(arr) ? arr : []) {
          if (e.impact !== "High") continue;
          const ms = Date.parse(e.date);
          if (!isFinite(ms)) continue;
          const t = Math.round(ms / 1000);
          const cur = String(e.country || "").toUpperCase().slice(0, 4);
          const title = String(e.title || "").replace(/[|\r\n]/g, " ").slice(0, 60);
          events.set(`${t}|${cur}|${title}`, { t, cur, title });
          got++;
        }
        ok = true; anyOk = true;
        break;
      } catch (err) {
        if (!/404/.test(err.message)) errs.push(url.split("/")[2] + "/" + url.split("/").pop() + ": " + err.message);
      }
    }
  }
  // staré zprávy (víc než 2 dny zpět) zahodit
  const old = Date.now() / 1000 - 2 * 86400;
  for (const [k, v] of events) if (v.t < old) events.delete(k);
  if (anyOk) lastOk = Date.now();
  lastErr = errs.join("; ");
  if (errs.length) console.warn("Kalendář:", lastErr);
  return got;
}

function start() {
  const tick = () => {
    const due = Date.now() - lastOk > REFRESH_MS && Date.now() - lastTry > RETRY_MS;
    if (due) refresh().catch((e) => { lastErr = e.message; });
  };
  tick();
  setInterval(tick, 60 * 1000).unref();
}

// zprávy, které stáhlo EA přímo z VPS: řádky "utc|měna|název"
function importText(text) {
  let n = 0;
  for (const line of String(text || "").split(/\r?\n/)) {
    const [ts, cur, ...rest] = line.split("|");
    const t = Number(ts);
    if (!(t > 1.6e9) || !cur) continue;
    const c = cur.trim().toUpperCase().slice(0, 4), title = rest.join(" ").trim().slice(0, 60);
    events.set(`${t}|${c}|${title}`, { t, cur: c, title });
    n++;
  }
  if (n) { lastOk = Date.now(); lastErr = ""; }
  return n;
}

function list(fromSec, toSec) {
  return [...events.values()].filter((e) => e.t >= fromSec && e.t <= toSec).sort((a, b) => a.t - b.t);
}

// pro EA: "utc|měna|název" po řádcích (od včerejška dál)
function eaText() {
  const now = Math.floor(Date.now() / 1000);
  return list(now - 86400, now + 14 * 86400).map((e) => `${e.t}|${e.cur}|${e.title}`).join("\n");
}

const status = () => ({ count: events.size, lastOk, lastErr });

module.exports = { start, refresh, list, eaText, status, importText };
