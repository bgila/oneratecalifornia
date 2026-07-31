(function () {
  'use strict';

  var addrEl = document.getElementById('heroChartAddr');
  var svgWrap = document.getElementById('heroChartSvgWrap');
  var refreshBtn = document.getElementById('heroChartRefresh');
  if (!addrEl || !svgWrap || !refreshBtn) return;

  // Both data files share this window: 1975 is FRED's earliest available year
  // for the SF house price index, and every home in home-tax-histories.json
  // was picked specifically because it clears "50 years since last recorded
  // sale" against the pipeline's own run date -- see pipeline/12 and 13.
  var START_YEAR = 1975;
  var END_YEAR = 2025;
  var GENERAL_RATE_CURRENT = 1.00;
  var BOND_RATE_SF = 0.18;
  var GENERAL_RATE_PROPOSED = 0.70;

  var homes = null, hpi = null, usedIndices = [];

  function fmtShort(v) {
    if (v >= 1000) return '$' + Math.round(v / 1000) + 'k';
    return '$' + Math.round(v);
  }

  // Reconstructs a home's assessed-value (and thus tax) trajectory from
  // Prop 13's rules: assessed value can rise at most 2%/yr from a base year,
  // so today's assessed value implies a specific value 50 years ago. That
  // implied 1975 value, scaled forward by the real FRED house price index
  // instead of the artificial 2% cap, gives an estimated true market value
  // for every year in between -- the gap between the two is the whole point.
  function computeSeries(home) {
    var years = [];
    for (var y = START_YEAR; y <= END_YEAR; y++) years.push(y);
    var base = home.assessed / Math.pow(1.02, END_YEAR - START_YEAR);
    var hpiStart = hpi[String(START_YEAR)];
    var actual = [], marketNow = [], proposed = [];
    years.forEach(function (y) {
      var assessedY = base * Math.pow(1.02, y - START_YEAR);
      var hpiY = hpi[String(y)] || hpiStart;
      var marketY = base * (hpiY / hpiStart);
      actual.push(assessedY * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100);
      marketNow.push(marketY * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100);
      proposed.push(marketY * (GENERAL_RATE_PROPOSED + BOND_RATE_SF) / 100);
    });
    return { years: years, actual: actual, marketNow: marketNow, proposed: proposed };
  }

  function renderChart(series) {
    var W = 460, H = 230, padL = 44, padR = 8, padT = 14, padB = 22;
    var allVals = series.actual.concat(series.marketNow, series.proposed);
    var maxV = Math.max.apply(null, allVals);
    var n = series.years.length;

    function xAt(i) { return padL + (W - padL - padR) * i / (n - 1); }
    function yAt(v) { return H - padB - (H - padB - padT) * (v / maxV); }
    function pathFor(arr) {
      return arr.map(function (v, i) { return (i === 0 ? 'M' : 'L') + xAt(i).toFixed(1) + ',' + yAt(v).toFixed(1); }).join(' ');
    }

    var gridY = yAt(maxV);
    var baseY = H - padB;
    svgWrap.innerHTML =
      '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Estimated property tax, ' + series.years[0] + ' to ' + series.years[n - 1] + ', for this home">' +
        '<line x1="' + padL + '" y1="' + baseY + '" x2="' + (W - padR) + '" y2="' + baseY + '" class="hero-chart-axis"></line>' +
        '<line x1="' + padL + '" y1="' + gridY.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + gridY.toFixed(1) + '" class="hero-chart-gridline"></line>' +
        '<text x="' + (padL - 6) + '" y="' + (gridY + 4).toFixed(1) + '" class="hero-chart-axislabel" text-anchor="end">' + fmtShort(maxV) + '</text>' +
        '<text x="' + (padL - 6) + '" y="' + (baseY + 4) + '" class="hero-chart-axislabel" text-anchor="end">$0</text>' +
        '<text x="' + padL + '" y="' + (H - 4) + '" class="hero-chart-axislabel">' + series.years[0] + '</text>' +
        '<text x="' + (W - padR) + '" y="' + (H - 4) + '" class="hero-chart-axislabel" text-anchor="end">' + series.years[n - 1] + '</text>' +
        '<path d="' + pathFor(series.marketNow) + '" class="hero-chart-line hero-chart-market"></path>' +
        '<path d="' + pathFor(series.proposed) + '" class="hero-chart-line hero-chart-proposed"></path>' +
        '<path d="' + pathFor(series.actual) + '" class="hero-chart-line hero-chart-actual"></path>' +
      '</svg>';
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
    var home = homes[pickRandomIndex()];
    addrEl.textContent = home.addr;
    renderChart(computeSeries(home));
  }

  refreshBtn.addEventListener('click', showRandomHome);

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
