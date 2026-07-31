(function () {
  'use strict';

  var addrEl = document.getElementById('heroChartAddr');
  var svgWrap = document.getElementById('heroChartSvgWrap');
  var outerEl = svgWrap ? svgWrap.parentElement : null;
  var refreshBtn = document.getElementById('heroChartRefresh');
  var tooltipEl = document.getElementById('heroChartTooltip');
  if (!addrEl || !svgWrap || !outerEl || !refreshBtn || !tooltipEl) return;

  // 2007 is as far back as DataSF's digitized assessor rolls go (see
  // pipeline/14) -- the chart only covers real data, no pre-2007 estimate.
  var START_YEAR = 2007;
  var END_YEAR = 2025;
  var GENERAL_RATE_CURRENT = 1.00;
  var BOND_RATE_SF = 0.18;
  var GENERAL_RATE_PROPOSED = 0.70;
  var Y_DEFAULT_MAX = 10000; // soft cap -- expands per home if its own values run higher

  var homes = null, hpi = null, usedIndices = [];
  var currentSeries = null, currentChartEl = null;

  var fmtUSD0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  function fmtShort(v) {
    if (v >= 1000) return '$' + Math.round(v / 1000) + 'k';
    return '$' + Math.round(v);
  }

  // Uses home.history (see pipeline/14) directly -- the real per-year assessed
  // value from DataSF's actual digitized rolls, 2007-2025, so it shows
  // whatever really happened (a reset that current_sales_date's single
  // "most recent sale" field doesn't capture, a Prop 8 decline-in-value
  // adjustment, etc.), not a reconstructed theoretical curve. No pre-2007
  // estimate: that data doesn't exist, so the chart just doesn't claim it.
  // The reset year is the home's own recorded sale year if it has one and
  // it falls in this window (so a home that reset in, say, 2016 only shows
  // "actual tax paid" from 2016 on), or 2007 if it doesn't. All series are
  // null before the reset year: this owner didn't own the home yet, so
  // neither their actual tax nor a hypothetical market-rate tax means
  // anything before then.
  function computeSeries(home) {
    var years = [];
    for (var y = START_YEAR; y <= END_YEAR; y++) years.push(y);
    var resetYear = Math.max(START_YEAR, home.sale_year || START_YEAR);
    var history = home.history || {};

    // base/hpiReset anchor the "market value, today's rate" line (still a
    // FRED-index estimate -- there's no bulk sale-price data to draw it from
    // directly). Anchored on the real assessed value at resetYear when
    // available, else the current snapshot.
    var anchorVal = history[String(resetYear)];
    var anchorYear = resetYear;
    if (anchorVal == null) { anchorYear = END_YEAR; anchorVal = home.assessed; }
    var base = anchorVal / Math.pow(1.02, anchorYear - resetYear);
    var hpiReset = hpi[String(resetYear)];

    var assessedVal = [], marketVal = [], actual = [], marketNow = [], proposed = [];
    var lastReal = null;
    years.forEach(function (y) {
      if (y < resetYear) {
        assessedVal.push(null); marketVal.push(null);
        actual.push(null); marketNow.push(null); proposed.push(null);
        return;
      }
      var assessedY;
      if (history[String(y)] != null) {
        assessedY = history[String(y)];
        lastReal = assessedY;
      } else if (lastReal != null) {
        assessedY = lastReal; // small gap in real data -- carry the last known value forward
      } else {
        assessedY = base * Math.pow(1.02, y - resetYear); // gap with nothing yet to carry forward
      }
      var hpiY = hpi[String(y)] || hpiReset;
      var marketY = base * (hpiY / hpiReset);
      assessedVal.push(assessedY);
      marketVal.push(marketY);
      actual.push(assessedY * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100);
      marketNow.push(marketY * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100);
      proposed.push(marketY * (GENERAL_RATE_PROPOSED + BOND_RATE_SF) / 100);
    });
    return {
      years: years, resetYear: resetYear,
      assessedVal: assessedVal, marketVal: marketVal,
      actual: actual, marketNow: marketNow, proposed: proposed
    };
  }

  var CHART_W = 460, CHART_H = 230, PAD_L = 44, PAD_R = 8, PAD_T = 14, PAD_B = 22;
  var currentYMax = Y_DEFAULT_MAX;

  function seriesMax(series) {
    var max = 0;
    ['marketNow', 'proposed', 'actual'].forEach(function (key) {
      series[key].forEach(function (v) { if (v != null && v > max) max = v; });
    });
    return Math.max(Y_DEFAULT_MAX, max);
  }

  function xAt(i, n) { return PAD_L + (CHART_W - PAD_L - PAD_R) * i / (n - 1); }
  function yAt(v) {
    var y = CHART_H - PAD_B - (CHART_H - PAD_B - PAD_T) * (v / currentYMax);
    return Math.max(PAD_T, y); // clamp so a home taller than the current axis flattens at the top instead of drawing off-chart
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
    currentYMax = seriesMax(series); // soft cap: $50k by default, expands to fit this home's own data
    var baseY = CHART_H - PAD_B;
    var gridY25 = yAt(currentYMax / 2);
    var gridY50 = yAt(currentYMax);
    svgWrap.innerHTML =
      '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H + '" role="img" aria-label="Estimated property tax, ' + series.years[0] + ' to ' + series.years[n - 1] + ', for this home">' +
        '<line x1="' + PAD_L + '" y1="' + baseY + '" x2="' + (CHART_W - PAD_R) + '" y2="' + baseY + '" class="hero-chart-axis"></line>' +
        '<line x1="' + PAD_L + '" y1="' + gridY25.toFixed(1) + '" x2="' + (CHART_W - PAD_R) + '" y2="' + gridY25.toFixed(1) + '" class="hero-chart-gridline"></line>' +
        '<line x1="' + PAD_L + '" y1="' + gridY50.toFixed(1) + '" x2="' + (CHART_W - PAD_R) + '" y2="' + gridY50.toFixed(1) + '" class="hero-chart-gridline"></line>' +
        '<text x="' + (PAD_L - 6) + '" y="' + (gridY50 + 4).toFixed(1) + '" class="hero-chart-axislabel" text-anchor="end">' + fmtShort(currentYMax) + '</text>' +
        '<text x="' + (PAD_L - 6) + '" y="' + (gridY25 + 4).toFixed(1) + '" class="hero-chart-axislabel" text-anchor="end">' + fmtShort(currentYMax / 2) + '</text>' +
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
    var taxPaid = currentSeries.actual[i];
    var taxProposed = currentSeries.proposed[i];
    if (assessed == null) {
      tooltipEl.innerHTML = '<strong>' + year + '</strong><div>Not yet owned by this buyer</div>';
    } else {
      tooltipEl.innerHTML =
        '<strong>' + year + '</strong>' +
        '<div class="tt-actual">Assessed value: ' + fmtUSD0.format(assessed) + '</div>' +
        '<div class="tt-market">Est. market value: ' + fmtUSD0.format(market) + '</div>' +
        '<div class="tt-actual">Prop tax paid: ' + fmtUSD0.format(taxPaid) + '</div>' +
        '<div class="tt-proposed">Prop tax under this reform: ' + fmtUSD0.format(taxProposed) + '</div>';
    }
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
