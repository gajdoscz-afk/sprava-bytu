// Výpočet statistik z historie, kterou posílá EA.
// Časová pásma: obdobím (den/týden/měsíc/rok) se rozumí kalendář v Praze.

const TZ = "Europe/Prague";
const BASKET_GAP_SEC = 120; // pozice zavřené do 2 min po sobě = jeden koš (obchod)

const fmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
});

// Posun Prahy vůči UTC je vždy celá hodina, takže datum je v rámci UTC hodiny stejné → cache po hodinách.
const dateCache = new Map();
function pragueDate(sec) {
  const hr = Math.floor(sec / 3600);
  let v = dateCache.get(hr);
  if (!v) {
    const p = {};
    for (const x of fmt.formatToParts(new Date(hr * 3600 * 1000))) p[x.type] = x.value;
    v = { y: +p.year, m: +p.month, d: +p.day };
    if (dateCache.size > 50000) dateCache.clear();
    dateCache.set(hr, v);
  }
  return v;
}

function isoWeek(y, m, d) {
  const dt = new Date(Date.UTC(y, m - 1, d));
  const day = (dt.getUTCDay() + 6) % 7; // Po = 0
  dt.setUTCDate(dt.getUTCDate() - day + 3); // čtvrtek téhož týdne
  const wy = dt.getUTCFullYear();
  const jan4 = new Date(Date.UTC(wy, 0, 4));
  const w = 1 + Math.round(((dt - jan4) / 86400000 - 3 + ((jan4.getUTCDay() + 6) % 7)) / 7);
  return `${wy}-W${String(w).padStart(2, "0")}`;
}

const pad = (n) => String(n).padStart(2, "0");
const KEYS = {
  day:   (s) => { const p = pragueDate(s); return `${p.y}-${pad(p.m)}-${pad(p.d)}`; },
  week:  (s) => { const p = pragueDate(s); return isoWeek(p.y, p.m, p.d); },
  month: (s) => { const p = pragueDate(s); return `${p.y}-${pad(p.m)}`; },
  year:  (s) => String(pragueDate(s).y),
};

// --- koše: pozice jedné instance zavřené krátce po sobě
function baskets(trades) {
  const sorted = [...trades].sort((a, b) => a.c - b.c);
  const out = [];
  let cur = null;
  for (const t of sorted) {
    if (cur && t.c - cur.c <= BASKET_GAP_SEC) {
      cur.p += t.p; cur.n++; cur.c = t.c; cur.o = Math.min(cur.o, t.o);
    } else {
      cur = { c: t.c, o: t.o, p: t.p, n: 1 };
      out.push(cur);
    }
  }
  return out;
}

// --- max drawdown z hodinových high/low (konzervativně: high před low)
function drawdown(series) {
  let peak = -Infinity, ddAbs = 0, ddPct = 0;
  for (const [, hi, lo] of series) {
    if (hi > peak) peak = hi;
    const dd = peak - lo;
    if (dd > ddAbs) ddAbs = dd;
    if (peak > 0 && dd / peak > ddPct) ddPct = dd / peak;
  }
  return { ddAbs, ddPct: ddPct * 100 };
}

function groupBy(arr, keyFn) {
  const m = new Map();
  for (const x of arr) {
    const k = keyFn(x);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(x);
  }
  return m;
}

/**
 * hist: Map id -> { symbol, magic, account, currency, balance, trades: Map, flow: Map, hours: Map }
 * filter: "ALL" nebo id instance
 */
function computeStats(hist, filter) {
  const all = [...hist.entries()].map(([id, h]) => ({ id, ...h }));
  const options = all.map((h) => ({ id: h.id, label: h.symbol + (all.filter((x) => x.symbol === h.symbol).length > 1 ? ` #${h.magic}` : "") }));
  if (!all.length) return { options, empty: true };

  const sel = filter && filter !== "ALL" ? all.filter((h) => h.id === filter) : all;
  if (!sel.length) return { options, empty: true };
  const isAll = !(filter && filter !== "ALL");

  // účet: bereme ten s nejčerstvějšími daty
  const acc = [...all].sort((a, b) => (b.updated || 0) - (a.updated || 0))[0];
  const currency = acc.currency || "";
  const balanceNow = acc.balance || 0;

  // pohyby na účtu (všechny obchody + vklady/výběry) – sjednocené přes instance téhož účtu
  const flow = new Map();
  for (const h of all) if (h.account === acc.account) for (const [k, v] of h.flow) flow.set(k, v);
  const flows = [...flow.values()];

  // koše vybraných instancí
  let bs = [];
  for (const h of sel) bs = bs.concat(baskets([...h.trades.values()]).map((b) => ({ ...b, sym: h.symbol })));

  // hodinová řada pro DD
  let series; // [hourSec, high, low]
  if (isAll) {
    const m = new Map();
    for (const h of all) {
      if (h.account !== acc.account) continue;
      for (const [hr, v] of h.hours) {
        const e = m.get(hr);
        m.set(hr, e ? [Math.max(e[0], v[0]), Math.min(e[1], v[1])] : [v[0], v[1]]);
      }
    }
    series = [...m.entries()].map(([hr, v]) => [hr, v[0], v[1]]);
  } else {
    series = [...sel[0].hours.entries()].map(([hr, v]) => [hr, v[2], v[3]]);
  }
  series.sort((a, b) => a[0] - b[0]);

  const rowCache = {};
  function rows(level, limit) {
    if (!rowCache[level]) rowCache[level] = buildRows(level);
    return rowCache[level].slice(0, limit);
  }

  function buildRows(level) {
    const kf = KEYS[level];
    const bByKey = groupBy(bs, (b) => kf(b.c));
    const sByKey = groupBy(series, (s) => kf(s[0]));
    // součet pohybů od začátku každého období do teď (pro balanci na začátku období)
    const flowByKey = new Map(), depByKey = new Map();
    for (const f of flows) {
      const k = kf(f.t);
      flowByKey.set(k, (flowByKey.get(k) || 0) + f.a);
      if (f.dep && f.a > 0) depByKey.set(k, (depByKey.get(k) || 0) + f.a);
    }
    const flowKeys = [...flowByKey.keys()].sort();
    const keys = [...new Set([...bByKey.keys(), ...sByKey.keys()])].sort().reverse();
    return keys.map((k) => {
      const list = bByKey.get(k) || [];
      const net = list.reduce((s, b) => s + b.p, 0);
      const gp = list.filter((b) => b.p > 0).reduce((s, b) => s + b.p, 0);
      const gl = list.filter((b) => b.p < 0).reduce((s, b) => s + b.p, 0);
      const wins = list.filter((b) => b.p > 0).length;
      let after = 0;
      for (const fk of flowKeys) if (fk >= k) after += flowByKey.get(fk);
      // základ pro %: balance na začátku období + vklady během období
      const base = balanceNow - after + (depByKey.get(k) || 0);
      const dd = drawdown(sByKey.get(k) || []);
      return {
        key: k,
        trades: list.length,
        positions: list.reduce((s, b) => s + b.n, 0),
        wins,
        winPct: list.length ? (wins / list.length) * 100 : null,
        net, grossProfit: gp, grossLoss: gl,
        pct: base > 0 ? (net / base) * 100 : null,
        ddAbs: dd.ddAbs,
        ddPct: isAll ? dd.ddPct : (base > 0 ? (dd.ddAbs / base) * 100 : null),
        hasDD: (sByKey.get(k) || []).length > 0,
      };
    });
  }

  const nowSec = Math.floor(Date.now() / 1000);
  const current = {};
  for (const lvl of ["day", "week", "month", "year"]) {
    const k = KEYS[lvl](nowSec);
    current[lvl] = rows(lvl, 1000).find((r) => r.key === k) ||
      { key: k, trades: 0, positions: 0, wins: 0, winPct: null, net: 0, grossProfit: 0, grossLoss: 0, pct: 0, ddAbs: 0, ddPct: 0, hasDD: false };
  }

  // celkem
  const net = bs.reduce((s, b) => s + b.p, 0);
  const wins = bs.filter((b) => b.p > 0).length;
  const first = bs.length ? Math.min(...bs.map((b) => b.o || b.c)) : nowSec;
  const base = balanceNow - flows.filter((f) => f.t >= first).reduce((s, f) => s + f.a, 0) +
    flows.filter((f) => f.t >= first && f.dep && f.a > 0).reduce((s, f) => s + f.a, 0);
  const ddAll = drawdown(series);
  current.total = {
    key: "celkem", trades: bs.length, positions: bs.reduce((s, b) => s + b.n, 0), wins,
    winPct: bs.length ? (wins / bs.length) * 100 : null, net,
    grossProfit: bs.filter((b) => b.p > 0).reduce((s, b) => s + b.p, 0),
    grossLoss: bs.filter((b) => b.p < 0).reduce((s, b) => s + b.p, 0),
    pct: base > 0 ? (net / base) * 100 : null,
    ddAbs: ddAll.ddAbs, ddPct: isAll ? ddAll.ddPct : (base > 0 ? (ddAll.ddAbs / base) * 100 : null),
    hasDD: series.length > 0,
  };

  return {
    options, filter: isAll ? "ALL" : filter, currency, balanceNow,
    ddFrom: series.length ? series[0][0] : null,
    current,
    tables: { day: rows("day", 31), week: rows("week", 12), month: rows("month", 12), year: rows("year", 10) },
  };
}

module.exports = { computeStats, KEYS, baskets, drawdown };
