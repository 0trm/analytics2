// analytics² dashboard renderer.
// Reads ./data.json, populates each tile by id. Tiles whose payload is missing
// or in error state render a placeholder with the error message.

(async function () {
  const $ = (sel, el = document) => el.querySelector(sel);

  // Concise definition shown on hover over each tile's title — surfaces
  // what the tile measures + the relevant free-tier rule, without cluttering
  // the always-visible UI. Wired onto each <h3 data-tip="..."> in the
  // rendering loop; CSS renders the bubble.
  const TILE_DEFS = {
    A1: "GA4 free-tier hard cap: 1M events ingested per property per day. Crossing it can throttle ingestion.",
    A2: "GA4 Data API quotas (Standard tier): 200k tokens/day, 40k tokens/hour, 10 concurrent. 429s above.",
    A3: "GA4 property caps: 50 custom dimensions, 50 custom metrics, 500 distinct event names registered.",
    A4: "GA4 free-tier cap: 30 key events per property (formerly 'conversion events').",
    A5: "GA4 free-tier cap: 100 audiences per property.",
    B1: "BigQuery free-tier: 1 TiB of query bytes scanned per month, then $6.25/TiB on-demand.",
    B2: "BigQuery free-tier: 10 GiB of active storage (tables modified in last 90 days), then ~$0.02/GiB/mo.",
    B4: "Share of queries served from BigQuery's 24h cache — cached jobs are free and instant. Higher is better.",
    C1: "Heaviest queries by bytes scanned in the last 48h. Source: INFORMATION_SCHEMA.JOBS_BY_PROJECT.",
    C2: "Tables most-scanned this month, summed across all jobs. Targets for partitioning or projection cleanup.",
    C3: "Top users (by service account / email) by bytes scanned this month. Spots concentration risk.",
    C4: "Queries against events_* with `SELECT *` — the costliest anti-pattern on GA4 exports.",
  };

  // Per-tile remediation copy used by the "Actionable signals" panel.
  // Declared up here so it's defined before renderSummary runs (the previous
  // bottom-of-file declaration hit a temporal-dead-zone error at render time).
  const PLAYBOOK = {
    A1: "Sample or disable noisy events at ingestion before they push you past the 1M/day cap.",
    A2: "Batch dimensions per `runReport`; reduce poll frequency on dashboards / external clients.",
    A3: "Archive unused custom dimensions, metrics, or registered events in GA4 Admin to reclaim slots.",
    A4: "Audit key events: archive unused ones to free space below the 30 cap.",
    A5: "Archive unused audiences to free slots below the 100 cap.",
    B1: "Filter early, partition-prune, and project only needed columns; cap dashboard refresh rate.",
    B2: "Set table-level expiration on intermediates and drop unused datasets; consider partitioning hot tables.",
    B4: "Materialize repeated queries into small lookup tables; avoid `SELECT *` and changing `LIMIT`s that break the cache.",
    C1: "Rewrite the top scanners to filter by partition and project explicit columns.",
    C2: "Investigate the top-scanned tables: are they partitioned? Are downstream consumers over-fetching?",
    C3: "Talk to the heaviest user — chances are one query or notebook is responsible for the bulk of scans.",
    C4: "Replace every `SELECT *` on `events_*` with explicit column projection — those scans cost the most.",
  };

  // Per-tile chart config: cap line (red dotted, only rendered when in
  // visible range — adjustScaleRange:false stops the axis from auto-extending)
  // + optional percent-axis rendering for tiles whose absolute cap is
  // abstract enough that "% of cap" reads better than raw counts.
  const CHART_CONFIG = {
    A1: { valueMax: 1_000_000, valueAsPercent: true },  // daily cap is 1M events
    A2: { capValue: 100 },                              // bars are %, line at 100%
    A3: { capValue: 100 },                              // bars are %, line at 100%
    B1: { capValue: 100 },                              // daily-spike threshold (GiB) — above forces red
    B2: { capValue: 10 },                               // 10 GiB free tier
  };

  let data;
  try {
    const res = await fetch("./data.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
    data = await res.json();
  } catch (err) {
    document.body.insertAdjacentHTML(
      "afterbegin",
      `<div style="padding:16px;background:#5a1d1d;color:#fff">Failed to load data.json: ${err.message}</div>`
    );
    return;
  }

  // Header meta
  $("#meta-project").textContent = data.config?.project_id ?? "—";
  $("#meta-property").textContent = data.config?.property_id ?? "—";
  $("#meta-refreshed").textContent = data.refreshed_at ?? "—";

  // Render each tile
  document.querySelectorAll(".tile").forEach((el) => {
    const id = el.dataset.tile;
    const tile = data.tiles?.[id];
    renderTile(el, id, tile);
    const h3 = el.querySelector("h3");
    if (h3) {
      h3.insertAdjacentHTML("afterbegin", `<span class="tile-code">${id}</span>`);
      if (TILE_DEFS[id]) h3.setAttribute("data-tip", TILE_DEFS[id]);
    }
  });

  // Bottom-of-page summary: tile-by-tile table + status panel (tally + attention).
  renderSummary(data);

  function renderSummary(data) {
    const tbody = document.getElementById("summary-body");
    if (!tbody) return;

    const rows = [];
    const tally = { green: 0, yellow: 0, red: 0, error: 0, empty: 0 };
    const attention = []; // non-green tiles in document order

    document.querySelectorAll("article.tile").forEach((el) => {
      const id = el.dataset.tile;
      const nameEl = el.querySelector("h3");
      // Strip the tile-code badge so the summary row doesn't double-print "A1 A1Events/...".
      let name = id;
      if (nameEl) {
        const clone = nameEl.cloneNode(true);
        clone.querySelector(".tile-code")?.remove();
        name = clone.textContent.trim();
      }
      const tile = data.tiles?.[id];
      const state = tile?.state ?? "empty";
      const headlineText = summaryHeadline(tile);

      tally[state] = (tally[state] || 0) + 1;
      if (state === "yellow" || state === "red" || state === "error") {
        attention.push({ id, name, state, headlineText });
      }

      rows.push(
        `<tr>` +
          `<td><strong>${id}</strong> <span class="summary-name">${escapeHtml(name)}</span></td>` +
          `<td class="state-col"><span class="state-dot ${state}" title="${state}"></span></td>` +
          `<td>${escapeHtml(headlineText)}</td>` +
        `</tr>`
      );
    });
    tbody.innerHTML = rows.join("");

    renderTally(tally);
    renderAttention(attention);
    renderSignals(attention);
  }

  function renderSignals(items) {
    const el = document.getElementById("status-signals");
    if (!el) return;
    if (items.length === 0) {
      el.innerHTML = `<li class="sig-empty">no signals to action — all 13 tiles are green</li>`;
      return;
    }
    const weight = { red: 0, yellow: 1, error: 2 };
    const sorted = [...items].sort((a, b) => (weight[a.state] ?? 9) - (weight[b.state] ?? 9));
    el.innerHTML = sorted
      .map((i) => {
        const action = PLAYBOOK[i.id] || "(no playbook entry — review tile body)";
        return `<li>
          <span class="sig-id">${i.id}</span>
          <span class="sig-action">${escapeHtml(action)}</span>
        </li>`;
      })
      .join("");
  }

  function summaryHeadline(tile) {
    if (!tile) return "—";
    if (tile.state === "error") return `error: ${tile.error ?? "unknown"}`;
    if (tile.headline && typeof tile.headline === "object") {
      const entries = Object.entries(tile.headline);
      if (entries.length > 0) {
        const [label, val] = entries[0];
        const formatted = typeof val === "number" ? val.toLocaleString() : String(val ?? "—");
        return `${formatted} ${label}`;
      }
    }
    return "—";
  }

  function renderTally(tally) {
    const el = document.getElementById("status-tally");
    if (!el) return;
    // Show red on the left so the eye lands on the worst news first.
    const order = [
      { state: "red", label: "red" },
      { state: "yellow", label: "yellow" },
      { state: "green", label: "green" },
    ];
    el.innerHTML = order
      .map((o) => `<div class="tally-item">
          <div class="tally-n ${o.state}">${tally[o.state] || 0}</div>
          <div class="tally-label">${o.label}</div>
        </div>`)
      .join("");
  }

  function renderAttention(items) {
    const el = document.getElementById("status-attention");
    if (!el) return;
    if (items.length === 0) {
      el.innerHTML = `<li class="att-empty">all 13 tiles green — nothing to do</li>`;
      return;
    }
    // Sort red first, then yellow, then error.
    const weight = { red: 0, yellow: 1, error: 2 };
    items.sort((a, b) => (weight[a.state] ?? 9) - (weight[b.state] ?? 9));
    el.innerHTML = items
      .map((i) =>
        `<li>
          <span class="state-dot ${i.state}"></span>
          <span class="att-id">${i.id}</span>
          <span class="att-name">${escapeHtml(i.name)}</span>
          <span class="att-headline">${escapeHtml(i.headlineText)}</span>
        </li>`
      )
      .join("");
  }

  function renderTile(el, id, tile) {
    const body = el.querySelector(".body");
    addPill(el, tile?.state ?? "empty");

    if (!tile) {
      body.innerHTML = `<div class="empty">no data for tile ${id}</div>`;
      return;
    }
    if (tile.state === "error") {
      body.innerHTML = `<div class="error">error: ${escapeHtml(tile.error ?? "unknown")}</div>`;
      return;
    }

    // Render order: headline first, then either a table or a chart.
    // tile.table wins if present; otherwise tile.series renders as a Chart.js canvas.
    const headlineHtml = renderHeadline(tile.headline);
    const chartId = `chart-${id}`;
    const hasTable = tile.table && Array.isArray(tile.table.rows);
    const hasSeries = !hasTable && Array.isArray(tile.series) && tile.series.length > 0;
    let bodyHtml = headlineHtml;
    if (hasTable) {
      // C1 gets a CSV-download affordance — every other table tile shows the
      // full population by definition, but C1 deliberately caps at top-10.
      if (id === "C1") {
        bodyHtml += `<button class="csv-btn" type="button">Download CSV ↓</button>`;
      }
      bodyHtml += renderTableHtml(tile.table);
    } else if (hasSeries) {
      // All charts flex to fill the tile height (Chart.js has
      // maintainAspectRatio: false, so it follows its sized parent).
      bodyHtml += `<div class="chart-wrap chart-fill"><canvas id="${chartId}"></canvas></div>`;
    }
    body.innerHTML = bodyHtml;

    if (hasSeries) renderChart(chartId, id, tile);
    if (id === "C1" && hasTable) {
      const btn = body.querySelector(".csv-btn");
      if (btn) btn.addEventListener("click", () =>
        downloadCsv("c1-top-queries-48h.csv", tile.table.columns, tile.table.rows));
    }
  }

  function renderTableHtml(table) {
    const cols = table.columns || [];
    if (cols.length === 0) return "";
    const head = cols.map((c) => `<th>${escapeHtml(c.label || c.key)}</th>`).join("");
    const body = (table.rows || [])
      .map((row) => {
        const cells = cols
          .map((c) => `<td>${formatCell(row[c.key], c.fmt)}</td>`)
          .join("");
        return `<tr>${cells}</tr>`;
      })
      .join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function formatCell(val, fmt) {
    if (val == null || val === "") return "—";
    if (fmt === "gib") {
      return `<span class="num">${Number(val).toFixed(2)}</span>`;
    }
    if (fmt === "mono-short") {
      const s = String(val);
      const shown = s.length > 12 ? s.slice(0, 12) + "…" : s;
      return `<span class="mono" title="${escapeHtml(s)}">${escapeHtml(shown)}</span>`;
    }
    return escapeHtml(String(val));
  }

  function renderHeadline(h) {
    if (!h || typeof h !== "object") return "";
    const entries = Object.entries(h);
    if (entries.length === 0) return "";
    const [label, val] = entries[0];
    const rest = entries.slice(1);
    // Subs collapse onto one inline line with bullet separators, instead of
    // one <div> per sub — keeps tile height tight even for 4-entry headlines.
    const subsHtml = rest.length === 0
      ? ""
      : `<div class="sub">` +
        rest.map(([k, v]) => `<span class="sub-item"><span class="sub-key">${escapeHtml(k)}</span> ${formatVal(v)}</span>`)
            .join(`<span class="sub-sep">·</span>`) +
        `</div>`;
    return (
      `<div class="headline">${formatVal(val)}<span class="unit">${escapeHtml(label)}</span></div>` +
      subsHtml
    );
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function renderChart(canvasId, tileId, tile) {
    const ctx = document.getElementById(canvasId);
    if (!ctx || typeof Chart === "undefined") return;
    const series = tile.series;
    const indexAxis = tile.indexAxis || "x";
    const chartType = tile.chartType || "bar";
    const accent = cssVar("--accent", "#2563eb");
    const muted = cssVar("--muted", "#71717a");
    const border = cssVar("--border", "#e8e8e3");
    const cfg = CHART_CONFIG[tileId] || {};

    // Value axis is x for horizontal bars (indexAxis='y'), else y.
    const valueAxis = indexAxis === "y" ? "x" : "y";
    const valueScale = { ticks: { color: muted, font: { size: 10 } } };
    const categoryScale = { ticks: { color: muted, font: { size: 10 } } };
    valueScale.grid = { color: border, display: valueAxis === "y" };
    categoryScale.grid = { display: valueAxis === "x" };
    if (cfg.valueMax != null) {
      valueScale.min = 0;
      valueScale.max = cfg.valueMax;
      if (cfg.valueAsPercent) {
        valueScale.ticks.callback = (v) => `${Math.round((v / cfg.valueMax) * 100)}%`;
      }
    }

    // Cap line: red dotted, only rendered when within scale range.
    // adjustScaleRange:false keeps Chart.js from auto-expanding to include it,
    // which is what we want — the line should appear only when bars get close.
    const annotations = {};
    if (cfg.capValue != null) {
      const lineSpec = { type: "line", borderColor: "rgba(239, 68, 68, 0.9)", borderWidth: 1, borderDash: [4, 4], adjustScaleRange: false };
      if (valueAxis === "y") { lineSpec.yMin = cfg.capValue; lineSpec.yMax = cfg.capValue; }
      else { lineSpec.xMin = cfg.capValue; lineSpec.xMax = cfg.capValue; }
      annotations.cap = lineSpec;
    }

    new Chart(ctx, {
      type: chartType,
      data: {
        labels: series.map((p) => p.x),
        datasets: [{
          data: series.map((p) => p.y),
          backgroundColor: accent,
          borderColor: accent,
          tension: 0.3,
          pointRadius: 2,
          fill: false,
        }],
      },
      options: {
        indexAxis,
        plugins: {
          legend: { display: false },
          annotation: { annotations },
        },
        scales: {
          x: valueAxis === "x" ? valueScale : categoryScale,
          y: valueAxis === "y" ? valueScale : categoryScale,
        },
        maintainAspectRatio: false,
      },
    });
  }

  function addPill(el, state) {
    const pill = document.createElement("span");
    pill.className = `pill ${state}`;
    pill.textContent = state;
    el.appendChild(pill);
  }

  function formatVal(v) {
    if (typeof v === "number") return v.toLocaleString();
    return escapeHtml(String(v ?? "—"));
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function downloadCsv(filename, columns, rows) {
    const esc = (v) => {
      if (v == null) return "";
      const s = String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const head = columns.map((c) => esc(c.label || c.key)).join(",");
    const body = rows.map((r) => columns.map((c) => esc(r[c.key])).join(",")).join("\n");
    const blob = new Blob([head + "\n" + body], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

})();
