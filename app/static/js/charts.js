/* ════════════════════════════════════════════════════════════════════════════
   lange-invest — shared Chart.js options module
   Ported from arcticdb-viewer's chart partial so every chart on every surface
   (public strategy pages, gated portfolio, admin viewer) shares one look.
   Colors resolve from CSS theme variables at draw time, so dark/light both work.
   Usage:  LangeChart.render("canvasId", chartData, { isMain: true });
   ════════════════════════════════════════════════════════════════════════════ */
(function (global) {
    "use strict";

    const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

    // Main palette: cool-led for raw series. Study palette: signal-ordered overlays
    // (dashed). Brand accent (amber) is intentionally excluded so it stays a state color.
    const COLORS = ["#3b82f6", "#10b981", "#ef4444", "#a855f7", "#f97316", "#06b6d4", "#ec4899", "#84cc16"];
    const STUDY_COLORS = ["#ef4444", "#f97316", "#10b981", "#a855f7", "#06b6d4", "#ec4899"];

    function themeColors() {
        return {
            grid: cssVar('--chart-grid') || 'rgba(255,255,255,0.04)',
            tick: cssVar('--chart-tick') || '#5c6773',
            text: cssVar('--chart-text') || '#e6edf3',
            title: cssVar('--chart-title') || '#8b96a3',
            tooltipBg: cssVar('--chart-tooltip-bg') || '#181f28',
            tooltipBorder: cssVar('--chart-tooltip-border') || '#283040',
            zoomFill: cssVar('--chart-zoom-fill') || 'rgba(245,165,36,0.12)',
            zoomLine: cssVar('--chart-zoom-line') || '#f5a524',
        };
    }

    function render(canvasId, chartData, opts) {
        opts = opts || {};
        const isMain = opts.isMain !== false;
        const el = document.getElementById(canvasId);
        if (!el) return null;
        const ctx = el.getContext("2d");
        const c = themeColors();

        const type = chartData.chart_type || "line";
        const n = (chartData.x_values || []).length;
        const maxTicks = Math.min(20, Math.max(6, Math.floor(n / 30)));

        const zoomConfig = (global.Chart && Chart.registry.plugins.get('zoom')) ? {
            zoom: {
                wheel: { enabled: false },
                pinch: { enabled: true },
                drag: { enabled: true, backgroundColor: c.zoomFill, borderColor: c.zoomLine, borderWidth: 1 },
                mode: 'x',
            },
            pan: { enabled: true, mode: 'x' },
            limits: { x: { minRange: 5 } },
        } : undefined;

        const baseAxisStyle = {
            grid: { color: c.grid, drawBorder: false, borderColor: 'transparent', tickColor: 'transparent' },
            border: { display: false },
            ticks: { color: c.tick, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
            title: { color: c.title, font: { family: "'IBM Plex Mono', monospace", size: 10, weight: '500' } },
        };

        const tooltipStyle = {
            backgroundColor: c.tooltipBg, borderColor: c.tooltipBorder, borderWidth: 1,
            titleColor: c.text, titleFont: { family: "'IBM Plex Mono', monospace", size: 11, weight: '500' },
            bodyColor: c.text, bodyFont: { family: "'IBM Plex Mono', monospace", size: 11 },
            padding: 8, cornerRadius: 4,
        };

        // ── Candlestick (OHLC) ──
        if (type === "candlestick") {
            const ohlc = chartData.datasets[0].data;
            const upC = "#22c55e", downC = "#ef4444", upBg = "#22c55e88", downBg = "#ef444488";
            const plugin = {
                id: "candlestick",
                afterDatasetsDraw(ch) {
                    const { ctx: cx, scales: { x: sx, y: sy } } = ch;
                    const bw = Math.max(2, (sx.width / ohlc.length) * 0.6);
                    ohlc.forEach((d, i) => {
                        if (!d || d.o == null) return;
                        const px = ch.getDatasetMeta(0).data[i].x;
                        const up = d.c >= d.o;
                        cx.beginPath(); cx.strokeStyle = up ? upC : downC; cx.lineWidth = Math.max(1, bw * 0.1);
                        cx.moveTo(px, sy.getPixelForValue(d.h)); cx.lineTo(px, sy.getPixelForValue(d.l)); cx.stroke();
                        const top = sy.getPixelForValue(Math.max(d.o, d.c));
                        const bot = sy.getPixelForValue(Math.min(d.o, d.c));
                        cx.fillStyle = up ? upBg : downBg; cx.strokeStyle = up ? upC : downC; cx.lineWidth = 1;
                        cx.fillRect(px - bw / 2, top, bw, Math.max(1, bot - top));
                        cx.strokeRect(px - bw / 2, top, bw, Math.max(1, bot - top));
                    });
                },
            };
            const lo = Math.min(...ohlc.filter(d => d).map(d => d.l));
            const hi = Math.max(...ohlc.filter(d => d).map(d => d.h));
            const chart = new Chart(ctx, {
                type: "scatter",
                data: { labels: chartData.x_values, datasets: [{ data: ohlc.map((d, i) => ({ x: i, y: d ? d.c : null })), pointRadius: 0, pointHitRadius: 10 }] },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    scales: {
                        x: { ...baseAxisStyle, type: "linear", min: -0.5, max: ohlc.length - 0.5,
                             title: { ...baseAxisStyle.title, display: isMain && !!chartData.x_label, text: chartData.x_label },
                             ticks: { ...baseAxisStyle.ticks, maxTicksLimit: maxTicks,
                                      callback: (v) => { const i = Math.round(v); return (i >= 0 && i < chartData.x_values.length) ? chartData.x_values[i] : ''; } } },
                        y: { ...baseAxisStyle, min: lo * 0.998, max: hi * 1.002 },
                    },
                    plugins: {
                        legend: { display: false }, zoom: zoomConfig,
                        tooltip: { ...tooltipStyle, callbacks: {
                            title: (it) => chartData.x_values[Math.round(it[0].parsed.x)] || '',
                            label: (c2) => { const d = ohlc[Math.round(c2.parsed.x)]; return d ? [`O ${d.o}`, `H ${d.h}`, `L ${d.l}`, `C ${d.c}`] : ''; } } },
                    },
                },
                plugins: [plugin],
            });
            if (!global._langeCharts) global._langeCharts = [];
            global._langeCharts.push(chart);
            return chart;
        }

        let datasetIdx = 0, studyIdx = 0;
        const datasets = (chartData.datasets || []).map((ds) => {
            if (ds.is_study === true) {
                const sColor = STUDY_COLORS[studyIdx++ % STUDY_COLORS.length];
                return {
                    label: ds.label, data: ds.data, borderColor: sColor, backgroundColor: "transparent",
                    borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, tension: 0.2, order: 10,
                };
            }
            const color = ds.color || COLORS[datasetIdx++ % COLORS.length];
            return {
                label: ds.label, data: ds.data, borderColor: color,
                backgroundColor: type === "bar" ? color + "AA" : (ds.fill ? color + "20" : color + "14"),
                borderWidth: type === "line" ? 1.75 : 1,
                pointRadius: type === "scatter" ? 2.5 : 0, pointHoverRadius: 4,
                tension: 0.15, fill: ds.fill || false, order: 0,
            };
        });

        const chart = new Chart(ctx, {
            type: type,
            data: { labels: chartData.x_values, datasets: datasets },
            options: {
                responsive: true, maintainAspectRatio: false,
                scales: {
                    x: {
                        ...baseAxisStyle,
                        title: { ...baseAxisStyle.title, display: isMain && !!chartData.x_label, text: chartData.x_label },
                        ticks: { ...baseAxisStyle.ticks, maxTicksLimit: maxTicks, maxRotation: 0, autoSkip: true },
                    },
                    y: {
                        ...baseAxisStyle,
                        title: { ...baseAxisStyle.title, display: !!chartData.y_label, text: chartData.y_label },
                    },
                },
                plugins: {
                    legend: (datasets.length > 1) ? {
                        position: "top", align: "end",
                        labels: { color: c.text, font: { family: "'IBM Plex Mono', monospace", size: 11 }, boxWidth: 10, boxHeight: 2, padding: 12 },
                    } : { display: false },
                    zoom: zoomConfig,
                    tooltip: tooltipStyle,
                },
                interaction: { mode: "index", intersect: false },
            },
        });

        if (!global._langeCharts) global._langeCharts = [];
        global._langeCharts.push(chart);
        return chart;
    }

    function redrawAll() {
        // Theme changed — rebuild from stored config so colors track the new palette.
        (global._langeChartSpecs || []).forEach((spec) => {
            const existing = (global._langeCharts || []).find((ch) => ch.canvas && ch.canvas.id === spec.canvasId);
            if (existing) existing.destroy();
            render(spec.canvasId, spec.data, spec.opts);
        });
    }

    // Convenience: register a spec and render, so theme toggles can redraw.
    function mount(canvasId, chartData, opts) {
        if (!global._langeChartSpecs) global._langeChartSpecs = [];
        global._langeChartSpecs = global._langeChartSpecs.filter((s) => s.canvasId !== canvasId);
        global._langeChartSpecs.push({ canvasId, data: chartData, opts });
        return render(canvasId, chartData, opts);
    }

    document.addEventListener('themechange', redrawAll);

    global.LangeChart = { render, mount, redrawAll, COLORS, STUDY_COLORS };
})(window);
