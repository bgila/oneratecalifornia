(function () {
  'use strict';

  var mapEl = document.getElementById('sfmap-leaflet');
  var wrap = mapEl ? mapEl.closest('.sfmap-wrap') : null;
  if (!mapEl || !wrap || typeof L === 'undefined') return;

  var loadingEl = document.getElementById('sfmap-loading');
  var countEl = document.getElementById('sfmap-count');
  var nbSelect = document.getElementById('sfmap-neighborhood');
  var nbInfoEl = document.getElementById('sfmap-nbinfo');
  var mfToggle = document.getElementById('sfmap-mf-toggle');
  var searchInput = document.getElementById('sfmap-search');
  var searchResults = document.getElementById('sfmap-search-results');
  var sfmapSectionEl = document.getElementById('sfmap');
  var countySelectEl = document.getElementById('sfmap-county');
  var leadEl = document.getElementById('sfmap-lead');
  var sfMethodTextEl = document.getElementById('sfmap-methodology-sf-text');
  var countyMethodTextEl = document.getElementById('sfmap-methodology-county-text');

  // Every county here shares the exact same map-data CSV schema SF's own
  // pipeline produces (lat,lon,addr,sqft,assessed,market,subsidy,change[,county]),
  // just from a different estimation method depending on what that county's own
  // assessor data actually supports -- see each pipeline/<county>_methodology.json
  // for the full writeup. center/zoom are each county's own real data's median
  // point and a 90th-percentile-spread-derived zoom, not guessed.
  var COUNTIES = {
    sf: {
      label: 'San Francisco', file: 'data/sf-map-data.csv', hasMF: true, hasNeighborhoods: true,
      center: [37.7627, -122.4494], zoom: 12.4,
      lead: 'We mapped the subsidy in SF, down to each single-family home, condo, and multi-family building. Prop 13 applies to commercial property too, but we focused on residential property.'
    },
    alameda: {
      label: 'Alameda', file: 'data/alameda-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [37.6957, -122.0948], zoom: 10,
      lead: 'Alameda County, single-family homes and condos.',
      methodology: [
        'Comps are real reassessment events, confirmed the same way as San Francisco’s: a parcel whose total assessed value jumps ≥ 8% year-over-year, matched to the county’s own recorded transfer-date field, is treated as a real market reset. Older comps are appreciation-adjusted to today’s-equivalent price via FRED’s house price index for Alameda County, the same technique used for SF’s own older comps.',
        'One real difference from SF: Alameda’s public data doesn’t include a home’s building square footage at all, so market value is estimated per-parcel in whole dollars (the median total value of the 7 nearest confirmed comps), not per-square-foot. That’s a coarser estimate than SF’s — it can’t distinguish a larger home from a smaller one on a similar lot as precisely.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    inyo: {
      label: 'Inyo', file: 'data/inyo-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [37.3515, -118.4145], zoom: 8,
      lead: 'Inyo County (Bishop, Independence, Lone Pine), single-family homes.',
      methodology: [
        'This is the roughest estimate on this map, included because it’s the best available given what Inyo County publishes: there’s no sale-date field and no year-by-year assessment history at all, so there’s no way to detect a real reassessment event the way SF’s or Alameda’s data allows.',
        'Instead, market value is benchmarked off Zillow’s public home-value index (ZHVI) for each ZIP code. Inyo’s own data also doesn’t distinguish a home’s building size from its lot size, so every home in a given ZIP code gets that ZIP’s same typical-home-value estimate, regardless of its own square footage — it can’t tell a large home from a small one. A few rural ZIP codes with too few sales for Zillow to index borrow their nearest covered neighbor’s rate instead.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    losangeles: {
      label: 'Los Angeles', file: 'data/losangeles-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [34.1861, -118.5738], zoom: 11,
      lead: 'A slice of Los Angeles County — the western San Fernando Valley and Conejo corridor — single-family homes.',
      methodology: [
        'Los Angeles County’s own assessor data is enormous (tens of gigabytes), so this map covers a real, contiguous slice of it — the western San Fernando Valley and Conejo corridor (Woodland Hills, West Hills, Chatsworth, Canoga Park, Winnetka, Reseda, Tarzana, Encino, Van Nuys, Calabasas, Agoura Hills, Westlake Village, Hidden Hills) — rather than the whole county, for now.',
        'Within that slice, the method matches San Francisco’s exactly: comps are real reassessment events (an ≥ 8% year-over-year value jump matched to a recorded sale date), appreciation-adjusted to today’s-equivalent price via FRED’s house price index for LA County.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    placer: {
      label: 'Placer', file: 'data/placer-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [38.8146, -121.2609], zoom: 9,
      lead: 'Placer County (Roseville, Rocklin, Auburn, Tahoe City), single-family homes and condos.',
      methodology: [
        'Placer County’s assessor data is a single current snapshot, with no year-by-year history, so a reassessment jump can’t be observed the way it can for San Francisco or Alameda. Instead, comps are identified by how recently a parcel last changed hands (its own recorded transaction date) — a parcel transferred in the last few years almost certainly has its assessed value already reset near true market value.',
        'One data quirk found and corrected: roughly three-quarters of transaction-date records shared one exact timestamp, evidently a one-time system migration rather than real simultaneous sales — those were excluded, and only genuinely distinct, recent transaction dates were used as comps.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    riverside: {
      label: 'Riverside', file: 'data/riverside-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [33.7927, -117.1817], zoom: 9,
      lead: 'Riverside County, a sample of single-family homes.',
      methodology: [
        'Riverside County’s public data has no recorded sale-date field, and (unlike initial scouting suggested) its year-by-year value history turned out to only hold a single active roll year, not a real archive. So comps here are identified a third way: each parcel’s own recorded Prop 13 base-year (the year its assessment was last legally reset, by sale or new construction), rather than a value jump or a transfer-date field. Older comps are appreciation-adjusted to today’s-equivalent price via FRED’s house price index for Riverside County.',
        'Because there’s no way to independently confirm a base-year reset against an actual recorded sale, this method accepts a higher false-positive rate on what counts as a valid comp than San Francisco’s approach — some flagged resets may be corrections or exemption changes rather than real transactions. This map also covers roughly 1 in 5 single-family parcels countywide (a systematic sample), not every one.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    sanjoaquin: {
      label: 'San Joaquin', file: 'data/sanjoaquin-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [37.9295, -121.3025], zoom: 10,
      lead: 'San Joaquin County (Stockton, Tracy, Manteca, Lodi), single-family homes.',
      methodology: [
        'San Joaquin County’s assessor data has no sale-date field and no year-by-year history at all, so there’s no way to detect a real reassessment event the way SF’s or Alameda’s data allows.',
        'Instead, market value is benchmarked off Zillow’s public home-value index (ZHVI) for each ZIP code, converted to an implied $/sqft using that ZIP’s own median home size, then applied to each home’s own square footage — a real, if less rigorous, alternative to the comps-based method used elsewhere on this map. A handful of ZIP codes without enough sales for Zillow to index borrow their nearest covered neighbor’s rate instead.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    santabarbara: {
      label: 'Santa Barbara', file: 'data/santabarbara-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [34.6330, -120.2717], zoom: 9,
      lead: 'Santa Barbara County, single-family homes.',
      methodology: [
        'Santa Barbara County’s assessor data is a single current snapshot, with no year-by-year history, so comps are identified by how recently a parcel last changed hands (its own recorded transfer date) instead of a reassessment jump. Every comp found this way transacted around 2017–2019, so those older prices are appreciation-adjusted to today’s-equivalent value via FRED’s house price index for Santa Barbara County.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    },
    sonoma: {
      label: 'Sonoma', file: 'data/sonoma-map-data.csv', hasMF: false, hasNeighborhoods: false,
      center: [38.4350, -122.7019], zoom: 10,
      lead: 'Sonoma County, single-family homes.',
      methodology: [
        'Sonoma County’s public data includes something no other county here has: real recorded sale prices, not just assessed-value jumps used as a stand-in for them. Market value is built directly from the nearest actual sold comps (real transaction price ÷ building size), appreciation-adjusted to today’s-equivalent price via FRED’s house price index for Sonoma County — the most direct method used anywhere on this map.',
        'As elsewhere on this map, an estimate is never allowed to fall below a home’s own current assessed value.'
      ]
    }
  };
  var currentCounty = 'sf';

  var mapPanelEl = document.getElementById('sfmap-panel-map');
  var tfpPanelEl = document.getElementById('sfmap-panel-tfp');
  var tabButtons = document.querySelectorAll('.sfmap-tab');
  var jumpToEl = document.getElementById('sfmap-jumpto');
  var extraHomesEl = document.getElementById('sfmap-extra-homes');
  var extraNbEl = document.getElementById('sfmap-extra-neighborhoods');
  var legendHomesEl = document.getElementById('sfmap-legend-homes');
  var legendNbEl = document.getElementById('sfmap-legend-neighborhoods');
  var legendNbTitleEl = document.getElementById('sfmap-legend-nb-title');
  var legendNbMinEl = document.getElementById('sfmap-legend-nb-min');
  var legendNbMaxEl = document.getElementById('sfmap-legend-nb-max');
  var hintHomesEl = document.getElementById('sfmap-hint-homes');
  var hintNbEl = document.getElementById('sfmap-hint-neighborhoods');
  var methodHomesEl = document.getElementById('sfmap-methodology-homes');
  var methodNbEl = document.getElementById('sfmap-methodology-neighborhoods');
  var methodTfpEl = document.getElementById('sfmap-methodology-tfp');
  // The three per-tab methodology blocks are separate <details> elements, but should
  // read as a single "is methodology open" preference that survives tab switches --
  // opening it on one tab keeps it open when you switch to another.
  var methodElements = [methodHomesEl, methodNbEl, methodTfpEl];
  var methodologyOpen = false;
  methodElements.forEach(function (el) {
    el.addEventListener('toggle', function () { methodologyOpen = el.open; });
  });
  // Selected narrowly (not just '.metric-btn') since the homes-color-metric toggle
  // below reuses the same button styling but is a separate, independently-wired
  // control group.
  var metricButtons = document.querySelectorAll('.metric-btn[data-metric]');
  var homesMetricButtons = document.querySelectorAll('.metric-btn[data-homes-metric]');
  var homesMetric = 'dollar';

  var fmtUSD0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

  // Fuzzy address search (Fuse.js, approximate/edit-distance matching) -- indices
  // are built once per dataset right after it loads, not per keystroke. ignoreLocation
  // lets a match land anywhere in the string rather than assuming it starts near the
  // beginning; threshold is how forgiving of typos/missing chars a match can be.
  var FUSE_OPTS = { includeScore: false, threshold: 0.35, ignoreLocation: true, distance: 100 };
  var sfrFuse = null, mfFuse = null;

  // Parchment (lowest) to dark forest green (highest) -- same single-hue lightness
  // ramp as the offline neighborhood-subsidy analysis maps (pipeline/11), so the
  // live neighborhood-averages tab matches them exactly. Deliberately not a
  // red-green diverging scale.
  var GRADIENT_STOPS = [[239, 233, 218], [91, 125, 99], [27, 51, 46]];
  function gradientColor(t) {
    t = Math.max(0, Math.min(1, isFinite(t) ? t : 0.5));
    var seg = t < 0.5 ? 0 : 1;
    var localT = t < 0.5 ? t / 0.5 : (t - 0.5) / 0.5;
    var c0 = GRADIENT_STOPS[seg], c1 = GRADIENT_STOPS[seg + 1];
    var r = Math.round(c0[0] + (c1[0] - c0[0]) * localT);
    var g = Math.round(c0[1] + (c1[1] - c0[1]) * localT);
    var b = Math.round(c0[2] + (c1[2] - c0[2]) * localT);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  // Tier colors: single source of truth is styles.css (--tier-0..4), read here at load
  // time so the legend swatches and the map markers can never silently drift apart.
  // Falls back to a hardcoded copy (keep in sync with styles.css) if the CSS read comes
  // back empty -- observed on iOS Safari, where getComputedStyle can run before the
  // external stylesheet is fully applied, silently turning every marker Leaflet's
  // default blue instead of erroring.
  var TIER_COLORS_FALLBACK = ['#ffffff', '#efe9da', '#a3bc9f', '#5b7d63', '#1b332e'];
  var TIER_COLORS = (function () {
    var cs = getComputedStyle(document.documentElement);
    return [0, 1, 2, 3, 4].map(function (i) {
      return cs.getPropertyValue('--tier-' + i).trim() || TIER_COLORS_FALLBACK[i];
    });
  })();

  function tierOfDollar(subsidy) {
    if (subsidy < 0) return 0;
    if (subsidy < 5000) return 1;
    if (subsidy < 15000) return 2;
    if (subsidy < 30000) return 3;
    return 4;
  }
  // Subsidy as a % of the home's own market value -- (market - assessed) / market,
  // the same ratio the "Neighborhood averages" tab's "% subsidy" metric uses.
  // Unlike the dollar tiers, this is comparable across counties with very different
  // price levels, and (for multi-family) needs no per-unit division: the ratio is
  // already unit-count-independent.
  function tierOfPct(pct) {
    if (pct <= 0) return 0;
    if (pct < 25) return 1;
    if (pct < 50) return 2;
    if (pct < 75) return 3;
    return 4;
  }
  function pctSubsidy(assessed, market) {
    return market > 0 ? (market - assessed) / market * 100 : 0;
  }

  var HOMES_LEGEND_TEXT = {
    dollar: {
      title: 'Est. yearly subsidy',
      t0: 'pays more than market rate', t1: '$0 – 5k', t2: '$5k – 15k', t3: '$15k – 30k', t4: '$30k +'
    },
    pct: {
      title: 'Est. subsidy, % of value',
      t0: 'pays more than market rate', t1: '0% – 25%', t2: '25% – 50%', t3: '50% – 75%', t4: '75%+'
    }
  };
  function applyHomesLegendText() {
    var t = HOMES_LEGEND_TEXT[homesMetric];
    document.getElementById('sfmap-legend-homes-title').textContent = t.title;
    document.getElementById('sfmap-legend-homes-t0').textContent = t.t0;
    document.getElementById('sfmap-legend-homes-t1').textContent = t.t1;
    document.getElementById('sfmap-legend-homes-t2').textContent = t.t2;
    document.getElementById('sfmap-legend-homes-t3').textContent = t.t3;
    document.getElementById('sfmap-legend-homes-t4').textContent = t.t4;
  }

  // ---------- map + basemap ----------
  var map = L.map(mapEl, { preferCanvas: true, zoomControl: false }).setView([37.7627, -122.4494], 12.4);
  // Default zoom control sits top-left, which collides with our legend panel there --
  // move it to top-right instead.
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
  }).addTo(map);

  // Shared canvas renderer so all circleMarkers batch onto one canvas layer instead of
  // each getting its own DOM node -- essential at this point count (~190k).
  var canvasRenderer = L.canvas({ padding: 0.5 });

  // Multi-family buildings render as squares instead of circles (shape, not a rim
  // color, differentiates them now) -- Leaflet's canvas renderer has no built-in
  // square marker, so draw one directly for any circleMarker flagged
  // `squareMarker: true`, falling back to the normal circle draw otherwise. Hit-test
  // is similarly overridden to an axis-aligned box so the clickable area matches
  // what's drawn.
  var origUpdateCircle = L.Canvas.prototype._updateCircle;
  L.Canvas.prototype._updateCircle = function (layer) {
    if (!layer.options.squareMarker) { return origUpdateCircle.call(this, layer); }
    if (!this._drawing || layer._empty()) { return; }
    var p = layer._point, ctx = this._ctx, r = Math.max(Math.round(layer._radius), 1);
    ctx.beginPath();
    ctx.rect(p.x - r, p.y - r, r * 2, r * 2);
    this._fillStroke(ctx, layer);
  };
  var origContainsPoint = L.CircleMarker.prototype._containsPoint;
  L.CircleMarker.prototype._containsPoint = function (p) {
    if (!this.options.squareMarker) { return origContainsPoint.call(this, p); }
    var r = this._radius;
    return Math.abs(p.x - this._point.x) <= r && Math.abs(p.y - this._point.y) <= r;
  };

  var sfrLayer = L.layerGroup().addTo(map);
  var mfLayer = L.layerGroup().addTo(map);

  var sfrLoaded = false, mfLoaded = false, sfrCount = 0, mfCount = 0;
  var sfrLat, sfrLon, sfrSqft, sfrAssessed, sfrMarket, sfrSubsidy, sfrChange, sfrAddr;
  var mfLat, mfLon, mfUnits, mfSqft, mfAssessed, mfMarket, mfSubsidy, mfChange, mfAddr;

  function updateCountText() {
    var parts = [];
    if (sfrLoaded) parts.push(sfrCount.toLocaleString() + ' homes/condos');
    if (mfLoaded) parts.push(mfCount.toLocaleString() + ' buildings');
    countEl.textContent = parts.join(', ') + ' loaded';
  }

  function popupRow(label, value) {
    return '<dt>' + label + '</dt><dd>' + value + '</dd>';
  }

  function sfrPopupHtml(i) {
    var rows = [
      popupRow('Sqft', Math.round(sfrSqft[i]).toLocaleString()),
      popupRow('Assessed value', fmtUSD0.format(sfrAssessed[i])),
      popupRow('Est. market value', fmtUSD0.format(sfrMarket[i])),
      popupRow('Est. subsidy today', fmtUSD0.format(sfrSubsidy[i]) + '/yr'),
      popupRow('Subsidy, % of value', Math.round(pctSubsidy(sfrAssessed[i], sfrMarket[i])) + '%'),
      popupRow('Change under reform', (sfrChange[i] >= 0 ? '+' : '') + fmtUSD0.format(sfrChange[i]) + '/yr'),
    ];
    return '<div class="sfmap-popup-title">' + (sfrAddr[i] || 'Property') + '</div>' +
      '<dl class="sfmap-popup-dl">' + rows.join('') + '</dl>';
  }

  function mfPopupHtml(i) {
    var units = mfUnits[i];
    var rows = [
      popupRow('Units', units ? Math.round(units).toLocaleString() : '—'),
      popupRow('Building sqft', Math.round(mfSqft[i]).toLocaleString()),
      popupRow('Assessed value', fmtUSD0.format(mfAssessed[i])),
      popupRow('Est. market value', fmtUSD0.format(mfMarket[i])),
    ];
    if (units > 0) {
      rows.push(popupRow('Assessed value / unit', fmtUSD0.format(mfAssessed[i] / units)));
      rows.push(popupRow('Est. market value / unit', fmtUSD0.format(mfMarket[i] / units)));
    }
    rows.push(popupRow('Est. subsidy today', fmtUSD0.format(mfSubsidy[i]) + '/yr'));
    rows.push(popupRow('Subsidy, % of value', Math.round(pctSubsidy(mfAssessed[i], mfMarket[i])) + '%'));
    rows.push(popupRow('Change under reform', (mfChange[i] >= 0 ? '+' : '') + fmtUSD0.format(mfChange[i]) + '/yr'));
    return '<div class="sfmap-popup-title">' + (mfAddr[i] || 'Building') + ' (multi-family)</div>' +
      '<dl class="sfmap-popup-dl">' + rows.join('') + '</dl>' +
      '<div class="sfmap-popup-footnote">Building-level estimate' +
      (units ? '; per-unit figures divide the whole building evenly' : ', not per-unit') + '.</div>';
  }

  // Quote-aware CSV field split (RFC4180-ish) -- SF's addresses are a single
  // token with no commas, so a naive split(',') worked fine there, but several
  // of the other counties' pipelines format addresses as "street, city, state"
  // and quote the field, which a naive split silently shreds into extra
  // columns and shifts every numeric field that follows.
  function splitCSVLine(line) {
    var out = [], field = '', inQuotes = false;
    for (var i = 0; i < line.length; i++) {
      var ch = line[i];
      if (inQuotes) {
        if (ch === '"') {
          if (line[i + 1] === '"') { field += '"'; i++; } else { inQuotes = false; }
        } else {
          field += ch;
        }
      } else if (ch === '"') {
        inQuotes = true;
      } else if (ch === ',') {
        out.push(field); field = '';
      } else {
        field += ch;
      }
    }
    out.push(field);
    return out;
  }

  function parseSFRCSV(text) {
    var lines = text.split('\n');
    if (lines[lines.length - 1].trim() === '') lines.pop();
    var count = lines.length - 1;
    sfrLat = new Float64Array(count); sfrLon = new Float64Array(count);
    sfrSqft = new Float32Array(count);
    sfrAssessed = new Float64Array(count);
    sfrMarket = new Float64Array(count);
    sfrSubsidy = new Float64Array(count);
    sfrChange = new Float64Array(count);
    sfrAddr = new Array(count);

    var written = 0;
    for (var i = 1; i < lines.length; i++) {
      var line = lines[i];
      if (!line) continue;
      var c = splitCSVLine(line);
      if (c.length < 8) continue;
      var lat = parseFloat(c[0]), lon = parseFloat(c[1]);
      if (!isFinite(lat) || !isFinite(lon)) continue;
      var idx = written;
      sfrLat[idx] = lat; sfrLon[idx] = lon;
      sfrAddr[idx] = c[2];
      sfrSqft[idx] = parseFloat(c[3]);
      sfrAssessed[idx] = parseFloat(c[4]);
      sfrMarket[idx] = parseFloat(c[5]);
      sfrSubsidy[idx] = parseFloat(c[6]);
      sfrChange[idx] = parseFloat(c[7]);
      written++;
    }
    return written;
  }

  function parseMFCSV(text) {
    var lines = text.split('\n');
    if (lines[lines.length - 1].trim() === '') lines.pop();
    var count = lines.length - 1;
    mfLat = new Float64Array(count); mfLon = new Float64Array(count);
    mfUnits = new Float32Array(count);
    mfSqft = new Float32Array(count);
    mfAssessed = new Float64Array(count);
    mfMarket = new Float64Array(count);
    mfSubsidy = new Float64Array(count);
    mfChange = new Float64Array(count);
    mfAddr = new Array(count);

    var written = 0;
    for (var i = 1; i < lines.length; i++) {
      var line = lines[i];
      if (!line) continue;
      var c = splitCSVLine(line);
      if (c.length < 9) continue;
      var lat = parseFloat(c[0]), lon = parseFloat(c[1]);
      if (!isFinite(lat) || !isFinite(lon)) continue;
      var idx = written;
      mfLat[idx] = lat; mfLon[idx] = lon;
      mfAddr[idx] = c[2];
      mfUnits[idx] = parseFloat(c[3]);
      mfSqft[idx] = parseFloat(c[4]);
      mfAssessed[idx] = parseFloat(c[5]);
      mfMarket[idx] = parseFloat(c[6]);
      mfSubsidy[idx] = parseFloat(c[7]);
      mfChange[idx] = parseFloat(c[8]);
      written++;
    }
    return written;
  }

  // Marker size scales with zoom: a fixed pixel radius that looks reasonable zoomed
  // out (whole city) is a much-too-small click target once zoomed in on a single
  // block, since Leaflet's circleMarker hit-test is tied directly to the rendered
  // radius (no separate invisible click-tolerance buffer available). At the highest
  // zoom levels this roughly doubles the radius (~4x the click area) vs. the
  // city-wide default.
  function sfrRadiusForZoom(z) {
    if (z >= 18) return 10;
    if (z >= 16) return 8;
    if (z >= 14) return 6;
    return 4;
  }
  function mfRadiusForZoom(z) {
    if (z >= 18) return 14;
    if (z >= 16) return 11;
    if (z >= 14) return 8;
    return 6;
  }

  // Kept parallel to the data arrays so toggling the color metric can restyle
  // markers already on the map in place, instead of tearing down and rebuilding
  // ~100k-300k circleMarkers (slow) just to change their fill color.
  var sfrMarkers = [], mfMarkers = [];

  function sfrTierColor(i) {
    var t = homesMetric === 'pct' ? tierOfPct(pctSubsidy(sfrAssessed[i], sfrMarket[i])) : tierOfDollar(sfrSubsidy[i]);
    return TIER_COLORS[t];
  }
  function mfTierColor(i) {
    var t = homesMetric === 'pct'
      ? tierOfPct(pctSubsidy(mfAssessed[i], mfMarket[i]))
      : tierOfDollar(mfUnits[i] > 0 ? mfSubsidy[i] / mfUnits[i] : mfSubsidy[i]);
    return TIER_COLORS[t];
  }

  function buildSFRMarkers(count) {
    var r = sfrRadiusForZoom(map.getZoom());
    sfrMarkers = new Array(count);
    for (var i = 0; i < count; i++) {
      var marker = L.circleMarker([sfrLat[i], sfrLon[i]], {
        renderer: canvasRenderer,
        radius: r,
        weight: 0,
        fillColor: sfrTierColor(i),
        fillOpacity: 0.85,
      });
      marker.bindPopup(makeSFRPopupFn(i), { maxWidth: popupMaxWidth() });
      marker.addTo(sfrLayer);
      sfrMarkers[i] = marker;
    }
  }
  function makeSFRPopupFn(i) { return function () { return sfrPopupHtml(i); }; }

  function buildMFMarkers(count) {
    var r = mfRadiusForZoom(map.getZoom());
    mfMarkers = new Array(count);
    for (var i = 0; i < count; i++) {
      var marker = L.circleMarker([mfLat[i], mfLon[i]], {
        renderer: canvasRenderer,
        radius: r,
        weight: 0,
        squareMarker: true,
        fillColor: mfTierColor(i),
        fillOpacity: 0.9,
      });
      marker.bindPopup(makeMFPopupFn(i), { maxWidth: popupMaxWidth() });
      marker.addTo(mfLayer);
      mfMarkers[i] = marker;
    }
  }
  function makeMFPopupFn(i) { return function () { return mfPopupHtml(i); }; }

  function popupMaxWidth() {
    return Math.min(280, window.innerWidth - 48);
  }

  map.on('zoomend', function () {
    var sr = sfrRadiusForZoom(map.getZoom());
    var mr = mfRadiusForZoom(map.getZoom());
    sfrLayer.eachLayer(function (m) { m.setRadius(sr); });
    mfLayer.eachLayer(function (m) { m.setRadius(mr); });
  });

  mfToggle.addEventListener('change', function () {
    if (mfToggle.checked) { map.addLayer(mfLayer); } else { map.removeLayer(mfLayer); }
  });

  // ---------- neighborhoods: boundaries, avg subsidy, dropdown, centroids ----------
  var NB_BOUNDARIES = null, NB_AVG_SUBSIDY = null, NB_AVG_PCT_SUBSIDY = null, NB_CENTROIDS = null;
  var activeNeighborhood = null, neighborhoodPinned = false;
  var boundaryLayer = null;
  var ZOOM_FOR_AUTO_NEIGHBORHOOD = 14;
  var activeTab = 'homes';

  function nearestNeighborhood(lat, lon) {
    if (!NB_CENTROIDS) return null;
    var best = null, bestD = Infinity;
    for (var name in NB_CENTROIDS) {
      var c = NB_CENTROIDS[name];
      var dLat = c[0] - lat, dLon = c[1] - lon;
      var d = dLat * dLat + dLon * dLon;
      if (d < bestD) { bestD = d; best = name; }
    }
    return best;
  }

  function showBoundary(name) {
    if (boundaryLayer) { map.removeLayer(boundaryLayer); boundaryLayer = null; }
    var rings = NB_BOUNDARIES && NB_BOUNDARIES[name];
    if (!rings || !rings.length) return;
    boundaryLayer = L.polygon(rings, {
      renderer: canvasRenderer,
      color: '#1c332e', weight: 2, dashArray: '6 4', fill: false, interactive: false
    }).addTo(map);
  }

  function updateActiveNeighborhood() {
    if (!NB_CENTROIDS || activeTab === 'neighborhoods') return;
    var target = null;
    if (neighborhoodPinned) {
      target = activeNeighborhood;
    } else if (map.getZoom() >= ZOOM_FOR_AUTO_NEIGHBORHOOD) {
      var c = map.getCenter();
      target = nearestNeighborhood(c.lat, c.lng);
    }
    if (target !== activeNeighborhood) {
      activeNeighborhood = target;
      showBoundary(target);
    }
    if (!target) {
      nbInfoEl.hidden = true;
      return;
    }
    var avg = NB_AVG_SUBSIDY ? NB_AVG_SUBSIDY[target] : undefined;
    nbInfoEl.hidden = false;
    nbInfoEl.innerHTML = '<strong>' + target + '</strong> — avg. subsidy ' +
      (avg === undefined ? '—' : fmtUSD0.format(avg) + '/yr');
  }
  map.on('moveend zoomend', updateActiveNeighborhood);

  nbSelect.addEventListener('change', function () {
    if (!nbSelect.value) {
      neighborhoodPinned = false;
      activeNeighborhood = null;
      map.setView([37.7627, -122.4494], 12.4);
      return;
    }
    var bar = nbSelect.value.indexOf('|');
    var name = nbSelect.value.slice(0, bar);
    var parts = nbSelect.value.slice(bar + 1).split(',');
    neighborhoodPinned = true;
    activeNeighborhood = name;
    showBoundary(name);
    map.setView([parseFloat(parts[0]), parseFloat(parts[1])], 15);
    updateActiveNeighborhood();
  });

  function loadNeighborhoodExtras() {
    fetch('data/sf-neighborhoods.json')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (json) {
        if (!json) return;
        NB_BOUNDARIES = json.boundaries;
        NB_AVG_SUBSIDY = json.avg_subsidy;
        NB_AVG_PCT_SUBSIDY = json.avg_pct_subsidy;
        NB_CENTROIDS = json.centroids || {};
        Object.keys(NB_CENTROIDS).sort().forEach(function (name) {
          var opt = document.createElement('option');
          var latlon = NB_CENTROIDS[name];
          opt.value = name + '|' + latlon[0] + ',' + latlon[1];
          opt.textContent = name;
          nbSelect.appendChild(opt);
        });
        buildNeighborhoodFillLayer();
      })
      .catch(function () { /* boundaries/dropdown are a nice-to-have; map still works without them */ });
  }

  // ---------- neighborhood-averages tab: filled polygons + text labels ----------
  var nbFillLayer = L.layerGroup();
  var nbFillRefs = []; // [{ name, layer }]
  var currentMetric = 'dollar';

  function formatMetric(metric, value) {
    if (value === undefined || value === null) return '—';
    return metric === 'pct' ? Math.round(value) + '%' : fmtUSD0.format(value);
  }

  function buildNeighborhoodFillLayer() {
    if (!NB_BOUNDARIES || !NB_AVG_SUBSIDY || nbFillRefs.length) return;
    Object.keys(NB_BOUNDARIES).forEach(function (name) {
      var rings = NB_BOUNDARIES[name];
      if (!rings || !rings.length) return;
      var layer = L.polygon(rings, {
        renderer: canvasRenderer,
        color: '#33332e', weight: 1, fillOpacity: 0.85, interactive: false
      });
      layer.bindTooltip('', { permanent: true, direction: 'center', className: 'nb-label' });
      layer.addTo(nbFillLayer);
      nbFillRefs.push({ name: name, layer: layer });
    });
    applyNeighborhoodMetric(currentMetric);
    if (activeTab === 'neighborhoods') { nbFillLayer.addTo(map); }
  }

  function applyNeighborhoodMetric(metric) {
    currentMetric = metric;
    var dict = metric === 'pct' ? NB_AVG_PCT_SUBSIDY : NB_AVG_SUBSIDY;
    if (!dict) return;
    var values = nbFillRefs.map(function (ref) { return dict[ref.name]; }).filter(function (v) { return v !== undefined; });
    var vmin = Math.min.apply(null, values), vmax = Math.max.apply(null, values);
    nbFillRefs.forEach(function (ref) {
      var v = dict[ref.name];
      var t = (v === undefined) ? 0.5 : (vmax > vmin ? (v - vmin) / (vmax - vmin) : 0.5);
      ref.layer.setStyle({ fillColor: gradientColor(t) });
      ref.layer.setTooltipContent(formatMetric(metric, v));
    });
    legendNbTitleEl.textContent = metric === 'pct' ? 'Avg. % subsidy' : 'Avg. $ subsidy / home';
    legendNbMinEl.textContent = formatMetric(metric, vmin);
    legendNbMaxEl.textContent = formatMetric(metric, vmax);
  }

  metricButtons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      metricButtons.forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      applyNeighborhoodMetric(btn.getAttribute('data-metric'));
    });
  });

  // ---------- individual-homes color metric ($ subsidy vs % of value) ----------
  // Works for every county (unlike the neighborhoods tab's $/% toggle, which is
  // SF-only): restyles already-built markers in place rather than rebuilding them,
  // since a full rebuild is noticeably slow at this point count.
  function recolorHomes() {
    for (var i = 0; i < sfrMarkers.length; i++) {
      if (sfrMarkers[i]) sfrMarkers[i].setStyle({ fillColor: sfrTierColor(i) });
    }
    for (var j = 0; j < mfMarkers.length; j++) {
      if (mfMarkers[j]) mfMarkers[j].setStyle({ fillColor: mfTierColor(j) });
    }
  }

  homesMetricButtons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      homesMetricButtons.forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      homesMetric = btn.getAttribute('data-homes-metric');
      applyHomesLegendText();
      recolorHomes();
    });
  });

  // ---------- map view tabs ----------
  function setActiveTab(tab) {
    activeTab = tab;
    tabButtons.forEach(function (btn) {
      var isActive = btn.getAttribute('data-tab') === tab;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    mapPanelEl.hidden = tab === 'tfp';
    tfpPanelEl.hidden = tab !== 'tfp';
    jumpToEl.style.visibility = tab === 'tfp' ? 'hidden' : 'visible';
    extraHomesEl.hidden = tab !== 'homes';
    extraNbEl.hidden = tab !== 'neighborhoods';
    legendHomesEl.hidden = tab !== 'homes';
    legendNbEl.hidden = tab !== 'neighborhoods';
    hintHomesEl.hidden = tab !== 'homes';
    hintNbEl.hidden = tab !== 'neighborhoods';
    methodHomesEl.hidden = tab !== 'homes';
    methodNbEl.hidden = tab !== 'neighborhoods';
    methodTfpEl.hidden = tab !== 'tfp';
    methodElements.forEach(function (el) { el.open = methodologyOpen; });

    if (tab === 'homes') {
      map.addLayer(sfrLayer);
      if (mfToggle.checked) map.addLayer(mfLayer);
      map.removeLayer(nbFillLayer);
    } else if (tab === 'neighborhoods') {
      map.removeLayer(sfrLayer);
      map.removeLayer(mfLayer);
      if (nbFillRefs.length) map.addLayer(nbFillLayer);
      nbInfoEl.hidden = true;
      if (boundaryLayer) { map.removeLayer(boundaryLayer); boundaryLayer = null; }
    }
    if (tab !== 'tfp') {
      // The map container was possibly just un-hidden (display:none -> visible) by
      // removing the `hidden` attribute above. Leaflet caches its container size and
      // won't notice that change until told to -- a single setTimeout(0) isn't
      // reliably after the browser's layout pass, so do it on the next animation
      // frame instead (and once more shortly after, since some browsers still need
      // a second pass to pick up the new size correctly).
      requestAnimationFrame(function () {
        map.invalidateSize();
        setTimeout(function () { map.invalidateSize(); }, 150);
      });
    }
  }

  tabButtons.forEach(function (btn) {
    btn.addEventListener('click', function () { setActiveTab(btn.getAttribute('data-tab')); });
  });

  // ---------- address search ----------
  var searchTimer = null, searchMatches = [];

  function jumpToMatch(m) {
    neighborhoodPinned = false;
    map.setView([m.lat, m.lon], 17);
    m.marker.openPopup();
  }

  function runSearch(q) {
    searchMatches = [];
    if (q.length < 3) { searchResults.hidden = true; searchResults.innerHTML = ''; return; }
    if (sfrFuse) {
      var sfrHits = sfrFuse.search(q, { limit: 8 });
      for (var i = 0; i < sfrHits.length && searchMatches.length < 8; i++) {
        var si = sfrHits[i].refIndex;
        searchMatches.push({ label: sfrAddr[si], lat: sfrLat[si], lon: sfrLon[si], marker: null, popupFn: makeSFRPopupFn(si) });
      }
    }
    if (mfFuse) {
      var mfHits = mfFuse.search(q, { limit: 8 });
      for (var j = 0; j < mfHits.length && searchMatches.length < 8; j++) {
        var mi = mfHits[j].refIndex;
        searchMatches.push({ label: mfAddr[mi] + ' (multi-family)', lat: mfLat[mi], lon: mfLon[mi], marker: null, popupFn: makeMFPopupFn(mi) });
      }
    }
    if (!searchMatches.length) {
      searchResults.innerHTML = '<li class="search-empty">No matches</li>';
      searchResults.hidden = false;
      return;
    }
    searchResults.innerHTML = searchMatches.map(function (m, k) {
      return '<li data-k="' + k + '">' + m.label + '</li>';
    }).join('');
    searchResults.hidden = false;
  }

  function jumpToSearchMatch(m) {
    neighborhoodPinned = false;
    map.setView([m.lat, m.lon], 17);
    // open a transient popup at the matched location using the same content builder
    L.popup({ maxWidth: 280 }).setLatLng([m.lat, m.lon]).setContent(m.popupFn()).openOn(map);
  }

  searchInput.addEventListener('input', function () {
    var q = searchInput.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { runSearch(q); }, 150);
  });
  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && searchMatches.length) {
      jumpToSearchMatch(searchMatches[0]);
      searchResults.hidden = true;
    } else if (e.key === 'Escape') {
      searchResults.hidden = true;
    }
  });
  searchResults.addEventListener('mousedown', function (e) {
    var li = e.target.closest('li[data-k]');
    if (!li) return;
    var m = searchMatches[parseInt(li.getAttribute('data-k'), 10)];
    jumpToSearchMatch(m);
    searchResults.hidden = true;
    searchInput.value = m.label;
  });

  // ---------- data loading (lazy: only once the map scrolls into view) ----------
  // requestToken guards against a slow-to-arrive response from a county the user
  // has since switched away from overwriting the currently-selected county's data.
  var requestToken = 0;
  var neighborhoodExtrasLoaded = false;

  function loadSFRData(fileUrl, token) {
    fetch(fileUrl)
      .then(function (r) { if (!r.ok) throw new Error('fetch failed: ' + r.status); return r.text(); })
      .then(function (text) {
        if (token !== requestToken) return;
        sfrCount = parseSFRCSV(text);
        buildSFRMarkers(sfrCount);
        sfrLoaded = true;
        if (typeof Fuse !== 'undefined') { sfrFuse = new Fuse(sfrAddr, FUSE_OPTS); }
        loadingEl.style.display = 'none';
        updateCountText();
      })
      .catch(function (err) {
        if (token !== requestToken) return;
        loadingEl.textContent = 'Could not load map data (' + err.message + ').';
      });
  }

  function loadMFData() {
    var token = requestToken;
    fetch('data/sf-map-data-mf.csv')
      .then(function (r) { if (!r.ok) throw new Error('fetch failed: ' + r.status); return r.text(); })
      .then(function (text) {
        if (token !== requestToken) return;
        mfCount = parseMFCSV(text);
        buildMFMarkers(mfCount);
        mfLoaded = true;
        if (typeof Fuse !== 'undefined') { mfFuse = new Fuse(mfAddr, FUSE_OPTS); }
        updateCountText();
      })
      .catch(function () { /* multi-family layer is additive; map still works without it */ });
  }

  function renderCountyMethodology(cfg) {
    if (cfg.methodology) {
      sfMethodTextEl.hidden = true;
      countyMethodTextEl.hidden = false;
      countyMethodTextEl.innerHTML = cfg.methodology.map(function (p) {
        return '<p class="faq-answer">' + p + '</p>';
      }).join('');
    } else {
      sfMethodTextEl.hidden = false;
      countyMethodTextEl.hidden = true;
      countyMethodTextEl.innerHTML = '';
    }
  }

  function switchCounty(id) {
    var cfg = COUNTIES[id];
    if (!cfg || id === currentCounty && sfrLoaded) return;
    currentCounty = id;
    requestToken++;
    var token = requestToken;

    sfrLayer.clearLayers();
    mfLayer.clearLayers();
    sfrLoaded = false; mfLoaded = false; sfrFuse = null; mfFuse = null;
    sfrCount = 0; mfCount = 0;
    sfrMarkers = []; mfMarkers = [];

    neighborhoodPinned = false;
    activeNeighborhood = null;
    if (boundaryLayer) { map.removeLayer(boundaryLayer); boundaryLayer = null; }
    nbInfoEl.hidden = true;
    nbSelect.value = '';

    sfmapSectionEl.classList.toggle('non-sf-county', id !== 'sf');
    if (activeTab === 'neighborhoods' && id !== 'sf') { setActiveTab('homes'); }

    leadEl.textContent = cfg.lead;
    renderCountyMethodology(cfg);
    mapEl.setAttribute('aria-label', 'Map of ' + cfg.label + ' properties colored by estimated property tax subsidy');

    map.setView(cfg.center, cfg.zoom);
    loadingEl.style.display = '';
    loadingEl.textContent = 'Loading properties…';
    updateCountText();

    loadSFRData(cfg.file, token);
    if (cfg.hasMF) { loadMFData(); }
    if (cfg.hasNeighborhoods && !neighborhoodExtrasLoaded) {
      neighborhoodExtrasLoaded = true;
      loadNeighborhoodExtras();
    }
  }

  countySelectEl.addEventListener('change', function () { switchCounty(this.value); });

  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { switchCounty(countySelectEl.value || 'sf'); io.disconnect(); }
      });
    }, { rootMargin: '300px' });
    io.observe(wrap);
  } else {
    switchCounty(countySelectEl.value || 'sf');
  }
})();
