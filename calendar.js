// Kalendář zisků – sdílený pro aplikaci i veřejnou stránku.
(function () {
  const MONTHS = ["leden","únor","březen","duben","květen","červen","červenec","srpen","září","říjen","listopad","prosinec"];
  const WD = ["po","út","st","čt","pá","so","ne"];
  const pad = (n) => String(n).padStart(2, "0");
  const pctTxt = (x, d = 2) => (typeof x === "number" && isFinite(x)) ? (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(d) + " %" : "–";

  const css = `
  .cal-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
  .cal-nav { font:inherit; font-size:18px; line-height:1; color:var(--text); background:var(--card2); border:1px solid var(--line); border-radius:8px; width:36px; height:32px; cursor:pointer; }
  .cal-nav:disabled { opacity:.35; cursor:default; }
  .cal-title { font-size:16px; font-weight:600; min-width:140px; text-align:center; }
  .cal-sum { margin-left:auto; font-family:var(--mono); font-size:14px; }
  .cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; }
  .cal-wd { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.6px; text-align:center; padding:2px 0 4px; }
  .cal-day { position:relative; min-height:64px; border-radius:8px; background:var(--card2); border:1px solid var(--line); padding:6px 7px; overflow:hidden; }
  .cal-day.out { background:transparent; border-color:transparent; }
  .cal-day.today { box-shadow: inset 0 0 0 2px var(--head); }
  .cal-num { font-size:12px; color:var(--dim); }
  .cal-p { position:absolute; left:7px; right:7px; bottom:6px; font-family:var(--mono); font-size:13px; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .cal-t { position:absolute; right:7px; top:6px; font-size:11px; color:var(--dim); }
  .cal-legend { display:flex; gap:14px; flex-wrap:wrap; margin-top:10px; font-size:12px; color:var(--dim); }
  @media (max-width:600px) {
    .cal-day { min-height:52px; padding:4px 5px; border-radius:6px; }
    .cal-p { font-size:10.5px; left:4px; right:4px; bottom:4px; text-align:center; }
    .cal-t { display:none; } .cal-num { font-size:11px; }
  }`;
  function injectCss() {
    if (document.getElementById("sg-cal-css")) return;
    const st = document.createElement("style"); st.id = "sg-cal-css"; st.textContent = css; document.head.append(st);
  }
  const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = String(x); return e; };

  function monthKeyOf(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`; }
  function shiftMonth(key, delta) { const [y, m] = key.split("-").map(Number); const d = new Date(y, m - 1 + delta, 1); return monthKeyOf(d); }
  function monthLabel(key) { const [y, m] = key.split("-").map(Number); return `${MONTHS[m - 1]} ${y}`; }

  function cellColor(p, max) {
    if (typeof p !== "number") return null;
    const r = Math.min(1, Math.abs(p) / (max || 1));
    const a = 0.16 + 0.62 * r;
    return p >= 0 ? `rgba(80,200,120,${a.toFixed(3)})` : `rgba(235,90,90,${a.toFixed(3)})`;
  }

  // výsledek měsíce: z tabulky měsíců, jinak složením denních výsledků
  function monthResult(days, key, monthPct) {
    const v = monthPct ? monthPct(key) : null;
    if (typeof v === "number") return v;
    const list = days.filter((d) => d.k.startsWith(key) && typeof d.p === "number");
    if (!list.length) return null;
    return (list.reduce((acc, d) => acc * (1 + d.p / 100), 1) - 1) * 100;
  }

  function monthData(days, key) {
    const [y, m] = key.split("-").map(Number);
    const map = new Map(days.map((d) => [d.k, d]));
    const first = new Date(y, m - 1, 1);
    const lead = (first.getDay() + 6) % 7; // Po = 0
    const len = new Date(y, m, 0).getDate();
    const cells = [];
    for (let i = 0; i < lead; i++) cells.push(null);
    for (let d = 1; d <= len; d++) { const k = `${y}-${pad(m)}-${pad(d)}`; cells.push({ day: d, k, data: map.get(k) || null }); }
    while (cells.length % 7) cells.push(null);
    const vals = cells.filter((c) => c && c.data && typeof c.data.p === "number").map((c) => c.data.p);
    const max = Math.max(0.05, ...vals.map(Math.abs));
    const trades = cells.reduce((s, c) => s + (c && c.data ? c.data.t || 0 : 0), 0);
    const green = vals.filter((v) => v > 0).length, red = vals.filter((v) => v < 0).length;
    return { cells, max, trades, green, red };
  }

  /**
   * opts: { days: [{k,p,t,n?}], monthKey?, money?: bool, cur?: string, monthPct?: fn(key), onShare?: fn(key) }
   */
  function render(container, opts) {
    injectCss();
    const days = opts.days || [];
    const today = new Date();
    const todayKey = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
    const newest = monthKeyOf(today);
    const oldest = days.length ? days.map((d) => d.k.slice(0, 7)).sort()[0] : newest;
    let key = container._calKey || opts.monthKey || newest;

    function paint() {
      container._calKey = key;
      container.replaceChildren();
      const md = monthData(days, key);
      const res = monthResult(days, key, opts.monthPct);

      const head = el("div", "cal-head");
      const prev = el("button", "cal-nav", "‹"); prev.title = "Předchozí měsíc";
      const next = el("button", "cal-nav", "›"); next.title = "Další měsíc";
      prev.disabled = key <= oldest; next.disabled = key >= newest;
      prev.onclick = () => { key = shiftMonth(key, -1); paint(); };
      next.onclick = () => { key = shiftMonth(key, 1); paint(); };
      const sum = el("span", "cal-sum " + (res == null ? "dim" : res >= 0 ? "good" : "bad"), res == null ? "bez obchodů" : pctTxt(res));
      head.append(prev, el("span", "cal-title", monthLabel(key)), next, sum);
      if (opts.onShare && md.trades) {
        const sb = el("button", "share-btn", "Sdílet"); sb.style.float = "none"; sb.onclick = () => opts.onShare(key);
        head.append(sb);
      }
      container.append(head);

      const grid = el("div", "cal-grid");
      WD.forEach((w) => grid.append(el("div", "cal-wd", w)));
      md.cells.forEach((c) => {
        if (!c) { grid.append(el("div", "cal-day out")); return; }
        const cell = el("div", "cal-day" + (c.k === todayKey ? " today" : ""));
        cell.append(el("div", "cal-num", c.day));
        if (c.data) {
          const col = cellColor(c.data.p, md.max);
          if (col) cell.style.background = col;
          if (c.data.t) cell.append(el("div", "cal-t", c.data.t + "×"));
          const narrow = window.matchMedia && window.matchMedia("(max-width: 600px)").matches;
          const sign = (v) => (v >= 0 ? "+" : "−");
          const t = opts.money && typeof c.data.n === "number"
            ? sign(c.data.n) + Math.abs(c.data.n).toFixed(narrow ? 0 : 2)
            : (typeof c.data.p === "number" ? sign(c.data.p) + Math.abs(c.data.p).toFixed(narrow ? 1 : 2) + (narrow ? "" : " %") : "–");
          cell.append(el("div", "cal-p", t));
          cell.title = `${c.day}. ${monthLabel(key)}: ${pctTxt(c.data.p)}` + (opts.money && typeof c.data.n === "number" ? ` (${c.data.n.toFixed(2)} ${opts.cur || ""})` : "") + `, obchodů ${c.data.t}`;
        }
        grid.append(cell);
      });
      container.append(grid);

      const lg = el("div", "cal-legend");
      lg.append(el("span", "", `Obchodů: ${md.trades}`), el("span", "good", `Ziskových dní: ${md.green}`), el("span", "bad", `Ztrátových dní: ${md.red}`));
      if (opts.money) lg.append(el("span", "", "V buňkách " + (opts.cur || "") + " · podržením/najetím % "));
      container.append(lg);
    }
    paint();
  }

  // obrázek měsíce pro sociální sítě (1080×1350), jen % údaje
  function drawImage({ days, key, assetLabel, monthPct, logo, footer }) {
    const W = 1080, H = 1350;
    const c = document.createElement("canvas"); c.width = W; c.height = H;
    const g = c.getContext("2d");
    const sans = '"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif';
    const mono = '"IBM Plex Mono", ui-monospace, Consolas, monospace';
    const rr = (x, y, w, h, r) => { g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); };
    const md = monthData(days, key);
    const res = monthResult(days, key, monthPct);

    const grd = g.createLinearGradient(0, 0, 0, H); grd.addColorStop(0, "#1b2029"); grd.addColorStop(1, "#0b0d12");
    g.fillStyle = grd; g.fillRect(0, 0, W, H);
    if (logo && logo.complete && logo.naturalWidth) { g.save(); rr(80, 70, 104, 104, 24); g.clip(); g.drawImage(logo, 80, 70, 104, 104); g.restore(); }
    g.fillStyle = "#fad264"; g.font = `600 52px ${sans}`; g.fillText("StochGrid AOS", 210, 120);
    g.fillStyle = "#828a96"; g.font = `400 30px ${sans}`; g.fillText("Kalendář výsledků · " + (assetLabel || "Všechna aktiva"), 210, 162);
    g.fillStyle = "#2a313d"; g.fillRect(80, 218, W - 160, 2);

    const [yy, mm] = key.split("-").map(Number);
    const title = MONTHS[mm - 1].charAt(0).toUpperCase() + MONTHS[mm - 1].slice(1) + " " + yy;
    g.fillStyle = "#dce0e6"; g.font = `500 52px ${sans}`; g.fillText(title, 80, 300);
    g.fillStyle = res == null ? "#828a96" : res >= 0 ? "#50c878" : "#eb5a5a"; g.font = `500 64px ${mono}`;
    g.textAlign = "right"; g.fillText(res == null ? "–" : pctTxt(res), W - 80, 300); g.textAlign = "left";

    // mřížka
    const x0 = 80, y0 = 350, gap = 10, cw = (W - 160 - 6 * gap) / 7;
    g.font = `500 24px ${sans}`; g.fillStyle = "#828a96";
    WD.forEach((w, i) => { g.textAlign = "center"; g.fillText(w.toUpperCase(), x0 + i * (cw + gap) + cw / 2, y0 + 24); });
    g.textAlign = "left";
    const rows = md.cells.length / 7, ch = Math.min(130, (H - 220 - (y0 + 48)) / rows - gap);
    md.cells.forEach((cell, i) => {
      const col = i % 7, row = Math.floor(i / 7);
      const x = x0 + col * (cw + gap), y = y0 + 48 + row * (ch + gap);
      if (!cell) return;
      const fill = cell.data ? cellColor(cell.data.p, md.max) : null;
      g.fillStyle = fill || "#1b2029"; rr(x, y, cw, ch, 14); g.fill();
      g.strokeStyle = "#2a313d"; g.lineWidth = 2; rr(x, y, cw, ch, 14); g.stroke();
      g.fillStyle = cell.data ? "#dce0e6" : "#5c6470"; g.font = `500 24px ${sans}`; g.fillText(String(cell.day), x + 14, y + 34);
      if (cell.data && typeof cell.data.p === "number") {
        g.fillStyle = "#ffffff"; g.font = `500 24px ${mono}`; g.textAlign = "center";
        g.fillText((cell.data.p >= 0 ? "+" : "−") + Math.abs(cell.data.p).toFixed(1) + "%", x + cw / 2, y + ch - 18);
        g.textAlign = "left";
      }
    });

    const yS = y0 + 48 + rows * (ch + gap) + 40;
    g.font = `400 30px ${sans}`;
    g.fillStyle = "#828a96"; g.fillText(`Obchodů ${md.trades}`, 80, yS);
    g.fillStyle = "#50c878"; g.fillText(`Ziskové dny ${md.green}`, 380, yS);
    g.fillStyle = "#eb5a5a"; g.fillText(`Ztrátové dny ${md.red}`, 720, yS);

    g.fillStyle = "#2a313d"; g.fillRect(80, H - 130, W - 160, 2);
    g.fillStyle = "#828a96"; g.font = `400 28px ${sans}`;
    g.fillText(footer || "© 2026 StochGrid AOS", 80, H - 72);
    g.textAlign = "right"; g.fillText(new Date().toLocaleDateString("cs-CZ"), W - 80, H - 72); g.textAlign = "left";
    return c;
  }

  window.SGCalendar = { render, drawImage, monthLabel };
})();
