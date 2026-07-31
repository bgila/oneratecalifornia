(function () {
  'use strict';

  var addrEl = document.getElementById('heroChartAddr');
  var svgWrap = document.getElementById('heroChartSvgWrap');
  var outerEl = svgWrap ? svgWrap.parentElement : null;
  var refreshBtn = document.getElementById('heroChartRefresh');
  var tooltipEl = document.getElementById('heroChartTooltip');
  if (!addrEl || !svgWrap || !outerEl || !refreshBtn || !tooltipEl) return;

  // 1975 is FRED's earliest available year for the SF house price index.
  // Homes in home-tax-histories.json are a random sample of SFR parcels with
  // an assessed $/sqft well below typical market rate (built pre-1976) --
  // see pipeline/12 and 13.
  var START_YEAR = 1975;
  var END_YEAR = 2025;
  var GENERAL_RATE_CURRENT = 1.00;
  var BOND_RATE_SF = 0.18;
  var GENERAL_RATE_PROPOSED = 0.70;
  var Y_MAX = 50000; // fixed axis, deliberately not auto-scaled per home -- see hero-chart.js history

  var homes = null, hpi = null, usedIndices = [];
  var currentSeries = null, currentChartEl = null;

  var fmtUSD0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  function fmtShort(v) {
    if (v >= 1000) return '$' + Math.round(v / 1000) + 'k';
    return '$' + Math.round(v);
  }

  // Reconstructs a home's assessed-value (and thus tax) trajectory from
  // Prop 13's rules: assessed value can rise at most 2%/yr from a reset
  // year, so today's assessed value implies a specific value at that reset.
  // The reset year is the home's own recorded sale year if it has one
  // (so a home that genuinely reset in, say, 1996 only shows 30 years of
  // "actual tax paid" data, not a fabricated 50), or 1975 -- FRED's and
  // this chart's earliest year -- if it doesn't. Market value uses the same
  // reset-year base scaled by the real FRED index instead of the 2% cap,
  // for the full 1975-2025 window regardless of when this owner bought.
  function computeSeries(home) {
    var years = [];
    for (var y = START_YEAR; y <= END_YEAR; y++) years.push(y);
    var resetYear = Math.max(START_YEAR, home.sale_year || START_YEAR);
    var base = home.assessed / Math.pow(1.02, END_YEAR - resetYear);
    var hpiReset = hpi[String(resetYear)];
    var assessedVal = [], marketVal = [], actual = [], marketNow = [], proposed = [];
    years.forEach(function (y) {
      var hpiY = hpi[String(y)] || hpiReset;
      var marketY = base * (hpiY / hpiReset);
      marketVal.push(marketY);
      marketNow.push(marketY * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100);
      proposed.push(marketY * (GENERAL_RATE_PROPOSED + BOND_RATE_SF) / 100);
      if (y >= resetYear) {
        var assessedY = base * Math.pow(1.02, y - resetYear);
        assessedVal.push(assessedY);
        actual.push(assessedY * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100);
      } else {
        assessedVal.push(null);
        actual.push(null);
      }
    });
    return {
      years: years, resetYear: resetYear,
      assessedVal: assessedVal, marketVal: marketVal,
      actual: actual, marketNow: marketNow, proposed: proposed
    };
  }

  var CHART_W = 460, CHART_H = 230, PAD_L = 44, PAD_R = 8, PAD_T = 14, PAD_B = 22;

  function xAt(i, n) { return PAD_L + (CHART_W - PAD_L - PAD_R) * i / (n - 1); }
  function yAt(v) {
    var y = CHART_H - PAD_B - (CHART_H - PAD_B - PAD_T) * (v / Y_MAX);
    return Math.max(PAD_T, y); // clamp so a home taller than the fixed axis flattens at the top instead of drawing off-chart
  }
  function pathFor(arr, n) {
    var d = '', started = false;
    arr.forEach(function (v, i) {
      if (v == null) return;
      d += (started ? 'L' : 'M') + xAt(i, n).toFixed(1) + ',' + yAt(v).toFixed(1) + ' ';
      started = true;
    });
    return d.trim();
  }

  function renderChart(series) {
    var n = series.years.length;
    var baseY = CHART_H - PAD_B;
    var gridY25 = yAt(Y_MAX / 2);
    var gridY50 = yAt(Y_MAX);
    svgWrap.innerHTML =
      '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H + '" role="img" aria-label="Estimated property tax, ' + series.years[0] + ' to ' + series.years[n - 1] + ', for this home">' +
        '<line x1="' + PAD_L + '" y1="' + baseY + '" x2="' + (CHART_W - PAD_R) + '" y2="' + baseY + '" class="hero-chart-axis"></line>' +
        '<line x1="' + PAD_L + '" y1="' + gridY25.toFixed(1) + '" x2="' + (CHART_W - PAD_R) + '" y2="' + gridY25.toFixed(1) + '" class="hero-chart-gridline"></line>' +
        '<line x1="' + PAD_L + '" y1="' + gridY50.toFixed(1) + '" x2="' + (CHART_W - PAD_R) + '" y2="' + gridY50.toFixed(1) + '" class="hero-chart-gridline"></line>' +
        '<text x="' + (PAD_L - 6) + '" y="' + (gridY50 + 4).toFixed(1) + '" class="hero-chart-axislabel" text-anchor="end">' + fmtShort(Y_MAX) + '</text>' +
        '<text x="' + (PAD_L - 6) + '" y="' + (gridY25 + 4).toFixed(1) + '" class="hero-chart-axislabel" text-anchor="end">' + fmtShort(Y_MAX / 2) + '</text>' +
        '<text x="' + (PAD_L - 6) + '" y="' + (baseY + 4) + '" class="hero-chart-axislabel" text-anchor="end">$0</text>' +
        '<text x="' + PAD_L + '" y="' + (CHART_H - 4) + '" class="hero-chart-axislabel">' + series.years[0] + '</text>' +
        '<text x="' + (CHART_W - PAD_R) + '" y="' + (CHART_H - 4) + '" class="hero-chart-axislabel" text-anchor="end">' + series.years[n - 1] + '</text>' +
        '<path d="' + pathFor(series.marketNow, n) + '" class="hero-chart-line hero-chart-market"></path>' +
        '<path d="' + pathFor(series.proposed, n) + '" class="hero-chart-line hero-chart-proposed"></path>' +
        '<path d="' + pathFor(series.actual, n) + '" class="hero-chart-line hero-chart-actual"></path>' +
        '<line class="hero-chart-hoverline" id="heroChartHoverline" x1="0" y1="' + PAD_T + '" x2="0" y2="' + baseY + '" hidden></line>' +
      '</svg>';
    currentChartEl = svgWrap.querySelector('svg');
    currentSeries = series;
  }

  function hideTooltip() {
    tooltipEl.hidden = true;
    var hoverline = document.getElementById('heroChartHoverline');
    if (hoverline) hoverline.setAttribute('hidden', '');
  }

  function onChartMove(e) {
    if (!currentSeries || !currentChartEl) return;
    var rect = currentChartEl.getBoundingClientRect();
    var relX = (e.clientX - rect.left) / rect.width * CHART_W;
    var n = currentSeries.years.length;
    var i = Math.round((relX - PAD_L) / (CHART_W - PAD_L - PAD_R) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));

    var hoverline = document.getElementById('heroChartHoverline');
    var xPix = xAt(i, n);
    if (hoverline) {
      hoverline.setAttribute('x1', xPix);
      hoverline.setAttribute('x2', xPix);
      hoverline.removeAttribute('hidden');
    }

    var year = currentSeries.years[i];
    var assessed = currentSeries.assessedVal[i];
    var market = currentSeries.marketVal[i];
    tooltipEl.innerHTML =
      '<strong>' + year + '</strong>' +
      '<div>Assessed value: ' + (assessed == null ? '—' : fmtUSD0.format(assessed)) + '</div>' +
      '<div>Est. market value: ' + fmtUSD0.format(market) + '</div>';
    tooltipEl.hidden = false;

    var left = (xPix / CHART_W) * rect.width;
    tooltipEl.style.left = Math.min(Math.max(left, 4), rect.width - 4) + 'px';
  }

  function pickRandomIndex() {
    if (homes.length <= 1) return 0;
    if (usedIndices.length >= homes.length) usedIndices = [];
    var idx;
    do { idx = Math.floor(Math.random() * homes.length); } while (usedIndices.indexOf(idx) !== -1);
    usedIndices.push(idx);
    return idx;
  }

  function showRandomHome() {
    if (!homes || !hpi) return;
    hideTooltip();
    var home = homes[pickRandomIndex()];
    addrEl.textContent = home.addr;
    renderChart(computeSeries(home));
  }

  refreshBtn.addEventListener('click', showRandomHome);
  outerEl.addEventListener('mousemove', onChartMove);
  outerEl.addEventListener('mouseleave', hideTooltip);

  Promise.all([
    fetch('data/home-tax-histories.json').then(function (r) { return r.json(); }),
    fetch('data/sf-hpi.json').then(function (r) { return r.json(); })
  ]).then(function (results) {
    homes = results[0];
    hpi = results[1];
    showRandomHome();
  }).catch(function () {
    addrEl.textContent = 'Could not load';
  });
})();
