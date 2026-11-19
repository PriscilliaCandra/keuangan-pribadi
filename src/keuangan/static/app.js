"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];
  const $ = (sel) => document.querySelector(sel);
  const nf = new Intl.NumberFormat("id-ID");
  const state = { period: "bulan_ini", kind: "semua", category: "", q: "", offset: 0, limit: 12, data: null, total: 0, focus: false };

  // ---------- util ----------
  const rp = (n) => (n < 0 ? "−" : "") + "Rp" + nf.format(Math.abs(Math.round(n)));
  const trim1 = (x) => String(Math.round(x * 10) / 10).replace(".", ",");
  const compact = (n) => {
    const a = Math.abs(n);
    if (a >= 1e9) return trim1(n / 1e9) + " M";
    if (a >= 1e6) return trim1(n / 1e6) + " jt";
    if (a >= 1e3) return trim1(n / 1e3) + " rb";
    return String(n);
  };
  const fmtDate = (iso, withYear) => {
    const [y, m, d] = iso.split("-").map(Number);
    return `${d} ${BULAN[m - 1]}` + (withYear ? ` ${y}` : "");
  };
  // Semua teks dari data lewat textContent (nama/catatan adalah data tak tepercaya).
  const make = (ns, tag, props, kids) => {
    const e = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "text") e.textContent = v;
      else if (k === "class") e.setAttribute("class", v);
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    for (const kid of kids || []) if (kid) e.append(kid);
    return e;
  };
  const h = (tag, props, ...kids) => make(null, tag, props, kids);
  const sv = (tag, props, ...kids) => make(NS, tag, props, kids);

  async function getJSON(path, params) {
    const url = new URL(path, location.origin);
    for (const [k, v] of Object.entries(params || {})) if (v !== "" && v != null) url.searchParams.set(k, v);
    const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (res.status === 401) {  // sesi habis/dicabut: kembali ke halaman masuk
      document.getElementById("content").hidden = true;
      throw new Error("Sesi berakhir. Kirim /dashboard di Telegram untuk tautan masuk baru.");
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `Galat ${res.status}`);
    return body;
  }

  // ---------- tooltip ----------
  const tip = $("#tip");
  function showTip(title, rows, x, y) {
    tip.replaceChildren(
      h("div", { class: "t-title", text: title }),
      ...rows.map((r) => h("div", { class: "t-row" },
        h("i", { class: `t-line ${r.cls}` }),
        h("span", { class: "t-val", text: r.value }),
        h("span", { class: "t-name", text: r.name }))));
    tip.hidden = false;
    const w = tip.offsetWidth, hh = tip.offsetHeight;
    tip.style.left = `${Math.max(8, Math.min(x + 14, innerWidth - w - 8))}px`;
    tip.style.top = `${Math.max(8, Math.min(y + 14, innerHeight - hh - 8))}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  // ---------- KPI ----------
  function deltaNode(change, upIsGood) {
    if (change === null || change === undefined) return h("span", { text: "Belum ada pembanding" });
    if (change === 0) return h("span", { text: "Sama seperti periode sebelumnya" });
    const up = change > 0;
    const good = up === upIsGood;
    return h("span", { class: `${up ? "up" : "down"}-${good ? "good" : "bad"}`,
      text: `${up ? "▲" : "▼"} ${Math.abs(change)}% ${up ? "naik" : "turun"} dari periode sebelumnya` });
  }

  function renderKpis(d) {
    const k = d.kpi;
    $("#hero-label").textContent = `Selisih · ${d.label} (${fmtDate(d.from)} – ${fmtDate(d.to)})`;
    $("#hero-value").textContent = rp(k.net);
    $("#hero-value").classList.toggle("neg", k.net < 0);
    const meter = $("#hero-meter");
    if (k.income > 0) {
      const used = Math.round(k.expense * 100 / k.income);
      const fill = h("span", { class: `fill${used > 100 ? " over" : ""}` });
      fill.style.width = `${Math.min(100, used)}%`;
      meter.replaceChildren(
        h("div", { class: "row" }, h("span", { text: "Terpakai dari pemasukan" }), h("strong", { text: `${used}%` })),
        h("div", { class: "track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
          "aria-valuenow": String(Math.min(100, used)), "aria-label": `Pengeluaran ${used}% dari pemasukan` }, fill));
      meter.hidden = false;
    } else { meter.hidden = true; }
    $("#hero-sub").replaceChildren(h("span", {
      class: k.net >= 0 ? "up-good" : "down-bad",
      text: k.net >= 0 ? "▲ Surplus: pemasukan lebih besar" : "▼ Defisit: pengeluaran lebih besar" }));
    $("#kpi-income").textContent = rp(k.income);
    $("#kpi-income-d").replaceChildren(deltaNode(k.income_change, true));
    $("#kpi-expense").textContent = rp(k.expense);
    $("#kpi-expense-d").replaceChildren(deltaNode(k.expense_change, false));
    $("#kpi-rate").textContent = k.savings_rate === null ? "–" : `${k.savings_rate}%`;
    $("#kpi-count").textContent = k.savings_rate === null ? "Belum ada pemasukan" : `${k.transactions} transaksi`;
  }

  // ---------- grafik kolom: pemasukan vs pengeluaran ----------
  function niceScale(max, n) {
    const raw = max / n, pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw) || 10 * pow;
    const top = Math.ceil(max / step) * step, ticks = [];
    for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
    return { top, ticks };
  }
  const topRounded = (x, y, w, hgt, r) => {
    r = Math.min(r, w / 2, hgt);  // ujung data membulat 4px, dasar rata
    return `M${x},${y + hgt} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + hgt} Z`;
  };

  function renderSeries(d) {
    const host = $("#series-chart");
    host.replaceChildren();
    renderSeriesTable(d);
    const pts = d.series;
    if (!pts.length || pts.every((p) => !p.income && !p.expense)) {
      host.append(h("div", { class: "empty", text: "Belum ada data pada periode ini." }));
      return;
    }
    const W = Math.max(320, host.clientWidth || 640), H = 280;
    const m = { l: 54, r: 8, t: 20, b: 30 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    const maxExpense = Math.max(0, ...pts.map((p) => p.expense));
    const focus = state.focus && maxExpense > 0;  // skala mengikuti pengeluaran; pemasukan besar dipotong + diberi label
    const { top, ticks } = niceScale(Math.max(1, ...(focus ? [maxExpense] : pts.flatMap((p) => [p.income, p.expense]))), 4);
    const y = (v) => m.t + ph - (v / top) * ph;
    const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "group",
      "aria-label": `Grafik kolom pemasukan dan pengeluaran, ${d.label}. Data lengkap ada di tampilan tabel.` });

    for (const t of ticks) {
      svg.append(sv("line", { class: t === 0 ? "axis" : "grid", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
      svg.append(sv("text", { class: "tick", x: m.l - 8, y: y(t) + 4, "text-anchor": "end", text: compact(t) }));
    }
    const slot = pw / pts.length;
    const thick = Math.min(24, Math.max(3, (slot - 6) / 2 - 1));
    const step = Math.ceil(pts.length / Math.max(2, Math.floor(pw / 64)));

    pts.forEach((p, i) => {
      const cx = m.l + slot * (i + 0.5);
      const label = `${p.label}: pemasukan ${rp(p.income)}, pengeluaran ${rp(p.expense)}`;
      const g = sv("g", { class: "slot", tabindex: "0", role: "img", "aria-label": label });
      g.append(sv("rect", { class: "hit", x: m.l + slot * i, y: m.t, width: slot, height: ph }));
      const bar = (v, x, cls) => {
        if (!(v > 0)) return;
        const shown = Math.min(v, top);
        g.append(sv("path", { class: `bar ${cls}`, d: topRounded(x, y(shown), thick, y(0) - y(shown), 4) }));
        if (v > top) {  // dipotong: tandai agar tidak menyesatkan
          g.classList.add("clipped");
          g.append(sv("text", { class: "cap", x: x + thick / 2, y: m.t - 6, "text-anchor": "middle", text: `▲${compact(v)}` }));
        }
      };
      bar(p.income, cx - thick - 1, "income");  // celah 2px antar-kolom
      bar(p.expense, cx + 1, "expense");
      const rows = [{ cls: "income", name: "Pemasukan", value: rp(p.income) },
                    { cls: "expense", name: "Pengeluaran", value: rp(p.expense) }];
      const hot = (on) => g.classList.toggle("hot", on);
      g.addEventListener("pointerenter", () => hot(true));
      g.addEventListener("pointermove", (e) => showTip(p.label, rows, e.clientX, e.clientY));
      g.addEventListener("pointerleave", () => { hot(false); hideTip(); });
      g.addEventListener("focus", () => { const r = g.getBoundingClientRect(); showTip(p.label, rows, r.left + r.width / 2, r.top); });
      g.addEventListener("blur", hideTip);
      svg.append(g);
      if (i % step === 0) svg.append(sv("text", { class: "tick", x: cx, y: H - 10, "text-anchor": "middle", text: p.label }));
    });
    host.append(svg);
  }

  function renderSeriesTable(d) {
    const host = $("#series-table");
    const head = h("tr", {}, h("th", { text: "Periode" }), h("th", { class: "num", text: "Pemasukan" }),
      h("th", { class: "num", text: "Pengeluaran" }), h("th", { class: "num", text: "Selisih" }));
    const body = d.series.map((p) => h("tr", {}, h("td", { text: p.label }),
      h("td", { class: "num", text: rp(p.income) }), h("td", { class: "num", text: rp(p.expense) }),
      h("td", { class: "num", text: rp(p.income - p.expense) })));
    host.replaceChildren(h("table", {}, h("thead", {}, head), h("tbody", {}, ...body)));
  }

  // ---------- kategori (batang ranking, satu warna per jenis) ----------
  function renderRanks(host, rows, cls, kind) {
    host.replaceChildren();
    if (!rows.length) { host.append(h("div", { class: "empty", text: "Belum ada data." })); return; }
    const max = rows[0].total;
    for (const r of rows) {
      const fill = h("span", { class: `fill ${cls}` });
      fill.style.width = `${Math.max(2, (r.total / max) * 100)}%`;
      host.append(h("button", { type: "button", class: "rank",
        "aria-label": `${r.name}: ${rp(r.total)}, ${r.pct}%. Tekan untuk memfilter transaksi.`,
        onclick: () => { state.kind = kind; state.category = r.name; state.offset = 0; syncFilters(); loadTransactions(); $("#t-tx").scrollIntoView({ behavior: "smooth" }); } },
        h("span", { class: "name", text: r.name }),
        h("span", { class: "amt", text: `${rp(r.total)} · ${r.pct}%` }),
        h("span", { class: "track" }, fill)));
    }
  }

  // ---------- anggaran (meter) & tagihan ----------
  function renderBudgets(d) {
    const host = $("#budgets");
    host.replaceChildren();
    if (!d.budgets.length) { host.append(h("div", { class: "empty", text: "Belum ada anggaran. Atur lewat Telegram: /anggaran makanan 1,5jt" })); return; }
    for (const b of d.budgets) {
      const sev = b.pct >= 100 ? "crit" : b.pct >= 80 ? "warn" : "ok";
      const state_ = sev === "crit" ? `✕ Melebihi anggaran (${b.pct}%)` : sev === "warn" ? `! Hampir habis (${b.pct}%)` : `✓ Aman (${b.pct}%)`;
      const fill = h("span", { class: `fill ${sev}` });
      fill.style.width = `${Math.min(100, b.pct)}%`;
      host.append(h("div", { class: "meter" },
        h("div", { class: "row" }, h("span", { text: b.name }), h("span", { class: "amt", text: `${rp(b.spent)} / ${rp(b.limit)}` })),
        h("div", { class: "track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
          "aria-valuenow": String(Math.min(100, b.pct)), "aria-label": `${b.name}: ${state_}` }, fill),
        h("div", { class: "state", text: state_ + (b.forecast ? ` · prediksi habis ${fmtDate(b.forecast)}` : "") })));
    }
  }

  function renderSafe(d) {
    const host = $("#safe");
    host.replaceChildren();
    const s = d.safe_daily;
    if (!s) {
      host.append(h("div", { class: "empty", text: "Catat pemasukan atau atur anggaran total (/anggaran total 5jt) di Telegram untuk melihat uang aman per hari." }));
      return;
    }
    const over = s.available <= 0;
    host.append(
      h("div", { class: "safe-value", text: over ? rp(s.available) : `${rp(s.per_day)}` }),
      h("div", { class: "label", text: over ? "Sudah melewati batas sampai gajian" : `per hari selama ${s.days_left} hari, sampai ${fmtDate(s.next_payday)}` }),
      h("div", { class: "safe-lines" },
        h("div", { text: `Sisa uang: ${rp(s.available)}` }),
        s.upcoming_bills ? h("div", { text: `Sudah dikurangi tagihan sebelum gajian: ${rp(s.upcoming_bills)}` }) : null,
        s.saved ? h("div", { text: `Sudah dikurangi setoran tabungan siklus ini: ${rp(s.saved)}` }) : null,
        s.avg7 ? h("div", { class: over ? "" : (s.over_pace ? "down-bad" : "up-good"),
          text: over ? `Rata-rata 7 harimu: ${rp(s.avg7)}/hari`
            : `${s.over_pace ? "▲ Di atas batas" : "✓ Dalam batas"}: rata-rata 7 harimu ${rp(s.avg7)}/hari` }) : null,
        s.depletion ? h("div", { class: "down-bad", text: `▼ Dengan laju ini uang habis sekitar ${fmtDate(s.depletion)}` }) : null,
        s.payday_is_default ? h("div", { class: "muted", text: "Tanggal gajian belum diatur (dipakai tanggal 1): /gajian 25" }) : null));
  }

  function renderDebts(d) {
    const x = d.debts;
    $("#debt-total").textContent = (x.owed_to_me || x.owed_by_me)
      ? `Piutang ${rp(x.owed_to_me)} · Utang ${rp(x.owed_by_me)}` : "";
    const fill = (host, rows, empty) => {
      host.replaceChildren();
      if (!rows.length) { host.append(h("div", { class: "empty", text: empty })); return; }
      for (const r of rows) {
        const sub = [r.notes.join(", "), r.due ? (r.overdue ? `⚠ lewat tempo ${fmtDate(r.due)}` : `tempo ${fmtDate(r.due)}`) : ""]
          .filter(Boolean).join(" · ");
        host.append(h("div", { class: "bill" },
          h("div", {}, h("div", { text: r.person }), sub ? h("div", { class: r.overdue ? "sub down-bad" : "sub", text: sub }) : null),
          h("div", { class: "amt", text: rp(r.amount) })));
      }
    };
    fill($("#debts-to-me"), x.to_me, "Tidak ada. Catat: Budi utang 50rb");
    fill($("#debts-by-me"), x.by_me, "Tidak ada.");
  }

  function renderInsights(d) {
    const host = $("#insights");
    const kind = (t) => /^(📈|🆕|⚠️|🔮)/u.test(t) ? "warn" : /^👏/u.test(t) ? "good" : "info";
    host.replaceChildren(...d.insights.map((t) => h("p", { class: `insight ${kind(t)}`, text: t })));
    $("#insight-card").hidden = !d.insights.length;
  }

  function renderRecurring(d) {
    const host = $("#recurring");
    host.replaceChildren();
    if (!d.recurring.length) { host.append(h("div", { class: "empty", text: "Belum ada. Di Telegram: kos 1,5jt tiap bulan tgl 1" })); return; }
    for (const r of d.recurring) {
      host.append(h("div", { class: "bill" },
        h("div", {}, h("div", { text: r.name }),
          h("div", { class: "sub", text: `Tiap tgl ${r.day} · ${r.category}${r.active ? "" : " · dijeda"}` })),
        h("div", { class: "amt", text: `${r.kind === "masuk" ? "+" : ""}${rp(r.amount)}` })));
    }
  }

  function renderGoals(d) {
    const host = $("#goals");
    host.replaceChildren();
    if (!d.goals.length) { host.append(h("div", { class: "empty", text: "Belum ada target. Buat di Telegram: /target liburan bali 5jt" })); return; }
    for (const g of d.goals) {
      const fill = h("span", { class: "fill ok" });
      fill.style.width = `${g.pct}%`;
      const note = g.pct >= 100 ? "✓ Target tercapai"
        : g.monthly ? `Tabung ≈ ${rp(g.monthly)} per bulan sampai ${fmtDate(g.deadline, true)}`
        : g.deadline ? `Tenggat ${fmtDate(g.deadline, true)}` : `Sisa ${rp(g.target - g.saved)}`;
      host.append(h("div", { class: "meter" },
        h("div", { class: "row" }, h("span", { text: g.name }), h("span", { class: "amt", text: `${rp(g.saved)} / ${rp(g.target)}` })),
        h("div", { class: "track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
          "aria-valuenow": String(g.pct), "aria-label": `${g.name}: ${g.pct}%` }, fill),
        h("div", { class: "state", text: `${g.pct}% · ${note}` })));
    }
  }

  function renderBills(d) {
    const host = $("#bills");
    host.replaceChildren();
    $("#bill-total").textContent = d.bills.items.length ? `≈ ${rp(d.bills.monthly_total)} / bulan` : "";
    if (!d.bills.items.length) { host.append(h("div", { class: "empty", text: "Belum ada tagihan atau langganan." })); return; }
    for (const b of d.bills.items) {
      const when = b.days_left === 0 ? "hari ini" : `${b.days_left} hari lagi`;
      host.append(h("div", { class: "bill" },
        h("div", {}, h("div", { text: b.name }),
          h("div", { class: "sub", text: `${b.kind === "langganan" ? "Langganan" : "Tagihan"} · ${b.cycle === "yearly" ? "tahunan" : "bulanan"}` })),
        h("div", { class: "amt" }, h("div", { text: b.amount ? rp(b.amount) : "jumlah belum diketahui" }),
          h("div", { class: "sub", text: `${fmtDate(b.due)} · ${when}` }))));
    }
  }

  // ---------- catat & hapus (tulis) ----------
  async function sendJSON(method, path, payload) {
    const res = await fetch(path, { method, credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Requested-With": "kp" },
      body: payload === undefined ? undefined : JSON.stringify(payload) });
    if (res.status === 401) throw new Error("Sesi berakhir. Kirim /dashboard di Telegram untuk tautan masuk baru.");
    const body = await res.json().catch(() => ({}));
    if (!res.ok && res.status !== 404) throw new Error(body.error || `Galat ${res.status}`);
    return body;
  }

  function showNote(el, text, isErr) {
    el.hidden = !text;
    el.textContent = text || "";  // textContent: balasan memuat nama/catatan pengguna
    el.classList.toggle("err", !!isErr);
  }

  function syncEntryCategories() {
    const cats = state.data ? state.data.categories : { expense: [], income: [] };
    const list = $("#e-kind").value === "masuk" ? cats.income : cats.expense;
    $("#e-cat").replaceChildren(h("option", { value: "", text: "Otomatis" }), ...list.map((c) => h("option", { value: c, text: c })));
  }

  async function submitBusy(button, fn) {
    button.disabled = true;
    try { await fn(); } finally { button.disabled = false; }
  }

  $("#e-kind").addEventListener("change", syncEntryCategories);
  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#chat-text");
    const text = input.value.trim();
    if (!text) return;
    showNote($("#chat-reply"), "Memproses…");
    await submitBusy($("#chat-send"), async () => {
      try {
        const r = await sendJSON("POST", "/api/chat", { text });
        showNote($("#chat-reply"), r.reply || "");
        input.value = "";
        await load();
      } catch (err) { showNote($("#chat-reply"), err.message, true); }
    });
  });
  $("#entry-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = { kind: $("#e-kind").value, amount: Number($("#e-amount").value),
      category: $("#e-cat").value, date: $("#e-date").value, description: $("#e-desc").value };
    await submitBusy($("#e-save"), async () => {
      try {
        const r = await sendJSON("POST", "/api/entry", payload);
        showNote($("#entry-msg"), r.message);
        $("#e-amount").value = ""; $("#e-desc").value = "";
        await load();
      } catch (err) { showNote($("#entry-msg"), err.message, true); }
    });
  });

  async function deleteEntry(t) {
    if (!confirm(`Hapus catatan "${t.note}" (${rp(t.amount)})? Tidak bisa dibatalkan.`)) return;
    try {
      const r = await sendJSON("DELETE", `/api/entry/${encodeURIComponent(t.id)}`);
      setStatus(r.message || "", !r.ok);
      await load();
    } catch (err) { setStatus(err.message, true); }
  }

  // ---------- transaksi ----------
  function syncFilters() {
    const cats = state.data ? state.data.categories : { expense: [], income: [] };
    const list = state.kind === "keluar" ? cats.expense : state.kind === "masuk" ? cats.income
      : [...new Set([...cats.expense, ...cats.income])];
    const sel = $("#f-cat");
    sel.replaceChildren(h("option", { value: "", text: "Semua" }), ...list.map((c) => h("option", { value: c, text: c })));
    if (state.category && !list.includes(state.category)) state.category = "";
    sel.value = state.category;
    $("#f-kind").value = state.kind;
    $("#f-q").value = state.q;
  }

  async function loadTransactions() {
    try {
      const r = await getJSON("/api/transactions", { period: state.period, kind: state.kind,
        category: state.category, q: state.q, limit: state.limit, offset: state.offset });
      state.total = r.total;
      const body = $("#tx-body");
      body.replaceChildren();
      if (!r.rows.length) body.append(h("tr", {}, h("td", { colspan: "5", class: "empty", text: "Tidak ada transaksi yang cocok." })));
      for (const t of r.rows) {
        body.append(h("tr", {},
          h("td", {}, document.createTextNode(fmtDate(t.date)), h("span", { class: "yr", text: ` ${t.date.slice(0, 4)}` })),
          h("td", { class: "note" }, h("i", { class: `dot ${t.kind === "masuk" ? "income" : "expense"}`, "aria-hidden": "true" }),
            document.createTextNode(t.note), h("span", { class: "cat-m", text: t.category })),
          h("td", { text: t.category }),
          h("td", { class: "num", text: (t.kind === "masuk" ? "+" : "") + rp(t.amount),
            "aria-label": `${t.kind === "masuk" ? "pemasukan" : "pengeluaran"} ${rp(t.amount)}` }),
          h("td", {}, h("button", { type: "button", class: "del", title: "Hapus catatan ini",
            "aria-label": `Hapus ${t.note}`, text: "🗑", onclick: () => deleteEntry(t) }))));
      }
      const from = r.total ? r.offset + 1 : 0, to = Math.min(r.total, r.offset + r.rows.length);
      $("#pager-info").textContent = `${from}–${to} dari ${r.total}`;
      $("#prev").disabled = r.offset <= 0;
      $("#next").disabled = r.offset + state.limit >= r.total;
    } catch (e) { setStatus(e.message, true); }
  }

  // ---------- muat & render ----------
  function setStatus(text, isErr) {
    const s = $("#status");
    s.textContent = text || "";
    s.classList.toggle("err", !!isErr);
  }

  function renderPeriods(periods) {
    const host = $("#periods");
    host.replaceChildren(...Object.entries(periods).map(([key, label]) =>
      h("button", { type: "button", "aria-pressed": String(key === state.period), text: label,
        onclick: () => { state.period = key; state.offset = 0; load(); } })));
  }

  function render(d) {
    state.data = d;
    renderPeriods(d.periods);
    renderKpis(d);
    renderSeries(d);
    renderRanks($("#exp-cats"), d.expense_by_category, "expense", "keluar");
    renderRanks($("#inc-cats"), d.income_by_category, "income", "masuk");
    renderBudgets(d);
    renderBills(d);
    renderSafe(d);
    renderGoals(d);
    renderDebts(d);
    renderRecurring(d);
    renderInsights(d);
    syncFilters();
    syncEntryCategories();
  }

  async function load() {
    const content = $("#content");
    content.classList.add("loading");  // bingkai tetap, hanya diredupkan selama memuat
    setStatus("Memuat…");
    try {
      render(await getJSON("/api/summary", { period: state.period }));
      await loadTransactions();
      setStatus("");
    } catch (e) { setStatus(e.message, true); }
    content.classList.remove("loading");
  }

  // ---------- kontrol ----------
  $("#f-kind").addEventListener("change", (e) => { state.kind = e.target.value; state.category = ""; state.offset = 0; syncFilters(); loadTransactions(); });
  $("#f-cat").addEventListener("change", (e) => { state.category = e.target.value; state.offset = 0; loadTransactions(); });
  let timer;
  $("#f-q").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value; state.offset = 0; loadTransactions(); }, 250); });
  $("#prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadTransactions(); });
  $("#next").addEventListener("click", () => { state.offset += state.limit; loadTransactions(); });
  $("#toggle-scale").addEventListener("click", (e) => {
    state.focus = !state.focus;
    e.target.setAttribute("aria-pressed", String(state.focus));
    e.target.textContent = state.focus ? "Tampilkan semua skala" : "Fokus pengeluaran";
    if (state.data) renderSeries(state.data);
  });
  $("#toggle-table").addEventListener("click", (e) => {
    const showTable = $("#series-table").hidden;
    $("#series-table").hidden = !showTable;
    $("#series-chart").hidden = showTable;
    if (!showTable && state.data) renderSeries(state.data);  // lebar benar setelah tampil lagi
    e.target.setAttribute("aria-pressed", String(showTable));
    e.target.textContent = showTable ? "Lihat sebagai grafik" : "Lihat sebagai tabel";
  });

  $("#logout").addEventListener("click", async () => {
    try {
      await fetch("/api/logout", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: "{}" });
    } finally { location.replace("/"); }
  });

  // tema terang/gelap: ikut sistem, bisa diganti dan diingat (penyimpanan boleh gagal)
  const root = document.documentElement;
  try { const saved = localStorage.getItem("kp-theme"); if (saved) root.dataset.theme = saved; } catch (_) { /* abaikan */ }
  $("#theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("kp-theme", root.dataset.theme); } catch (_) { /* abaikan */ }
  });

  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => state.data && renderSeries(state.data), 150); });

  load();
})();
