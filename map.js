(function () {
  'use strict';

  const canvas = document.getElementById('sfmap-canvas');
  const wrap = canvas ? canvas.closest('.sfmap-wrap') : null;
  if (!canvas || !wrap) return;
  const ctx = canvas.getContext('2d');
  const loadingEl = document.getElementById('sfmap-loading');
  const countEl = document.getElementById('sfmap-count');
  const nbSelect = document.getElementById('sfmap-neighborhood');
  const popupEl = document.getElementById('sfmap-popup');
  const popupAddr = document.getElementById('sfmap-popup-addr');
  const popupBody = document.getElementById('sfmap-popup-body');

  const fmtUSD0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

  const LAT0 = 37.7749, COS_LAT0 = Math.cos(LAT0 * Math.PI / 180);
  function project(lat, lon) {
    return { x: (lon + 122.4194) * COS_LAT0, y: -(lat - LAT0) };
  }

  let pts = null;      // Float32Array pairs [x,y,...]
  let subsidy = null, change = null, sqft = null, assessed = null, market = null, addr = null;
  let pathsByTier = null;
  let grid = null, gridCell = 0.0015;
  let loaded = false, loading = false;

  // multi-family (building-level) layer -- same tier colors, drawn as diamonds, toggleable
  let mfPts = null, mfSubsidy = null, mfChange = null, mfSqft = null, mfAssessed = null,
      mfMarket = null, mfAddr = null, mfUnits = null;
  let mfPathsByTier = null, mfGrid = null;
  let mfLoaded = false;
  let showMF = true;

  let view = { scale: 1, cx: 0, cy: 0 }; // world units per... ; cx/cy = world center shown
  let fitScale = 1, fitCx = 0, fitCy = 0;

  let NB_BOUNDARIES = null, NB_AVG_SUBSIDY = null;   // filled once sf-neighborhoods.json loads
  let NB_PROJECTED = null;                            // [{name, x, y}] projected centroids
  let activeNeighborhood = null, neighborhoodPinned = false;
  let boundaryPath = null;                            // Path2D for the currently-shown boundary
  let nbInfoEl = document.getElementById('sfmap-nbinfo');
  let ZOOM_FOR_AUTO_NEIGHBORHOOD = 3;

  // streets (OpenStreetMap-derived, simplified and projected ahead of time)
  let majorStreetPath = null, minorStreetPath = null;
  let labelsMajor = [], labelsMinor = [];
  let ZOOM_FOR_MINOR_STREETS = 6;
  let ZOOM_FOR_MAJOR_LABELS = 2;
  let ZOOM_FOR_MINOR_LABELS = 8;

  function buildStreetPaths(json) {
    function toPath(lines) {
      let p = new Path2D();
      lines.forEach(function (line) {
        line.forEach(function (pt, i) {
          if (i === 0) p.moveTo(pt[0], pt[1]); else p.lineTo(pt[0], pt[1]);
        });
      });
      return p;
    }
    majorStreetPath = toPath(json.major || []);
    minorStreetPath = toPath(json.minor || []);
    labelsMajor = json.labelsMajor || [];
    labelsMinor = json.labelsMinor || [];
  }

  function loadStreets() {
    fetch('data/sf-streets.json')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (json) {
        if (!json) return;
        buildStreetPaths(json);
        if (loaded) render();
      })
      .catch(function () { /* streets are a visual nice-to-have; map still works without them */ });
  }

  function drawStreetLabels(labels, s, fontPx, haloWidthPx) {
    let dpr = window.devicePixelRatio || 1;
    // visible world-space bounds, so we skip the cost of drawing off-screen labels
    let halfW = (canvas.width / 2) / s, halfH = (canvas.height / 2) / s;
    let minX = view.cx - halfW, maxX = view.cx + halfW;
    let minY = view.cy - halfH, maxY = view.cy + halfH;

    // Text does NOT reliably scale the way vector fills/strokes do when drawn while the
    // extreme world-scale transform is active: a font-size that's a tiny fraction in
    // world-space (meant to be rescaled up by the transform, same trick used for
    // lineWidth elsewhere) rasterizes as literally invisible sub-pixel glyphs, even
    // though canvas accepts the string and the math is otherwise correct. So for the
    // glyph draw specifically, we switch to plain device-pixel space and do the
    // world->screen math ourselves instead of leaning on the ambient transform.
    let m = ctx.getTransform();
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.font = (fontPx * dpr) + 'px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    for (let i = 0; i < labels.length; i++) {
      let l = labels[i];
      if (l.x < minX || l.x > maxX || l.y < minY || l.y > maxY) continue;
      let devX = m.a * l.x + m.c * l.y + m.e;
      let devY = m.b * l.x + m.d * l.y + m.f;
      ctx.save();
      ctx.translate(devX, devY);
      ctx.rotate(l.a);
      ctx.lineWidth = haloWidthPx * dpr;
      ctx.strokeStyle = 'rgba(246,244,238,0.88)';
      ctx.strokeText(l.n, 0, 0);
      ctx.fillStyle = '#5c5346';
      ctx.fillText(l.n, 0, 0);
      ctx.restore();
    }
    ctx.restore();
  }

  function nearestNeighborhood(wx, wy) {
    if (!NB_PROJECTED) return null;
    let best = null, bestD = Infinity;
    for (let i = 0; i < NB_PROJECTED.length; i++) {
      let n = NB_PROJECTED[i];
      let dx = n.x - wx, dy = n.y - wy;
      let d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = n.name; }
    }
    return best;
  }

  function buildBoundaryPath(name) {
    let rings = NB_BOUNDARIES && NB_BOUNDARIES[name];
    if (!rings || !rings.length) { boundaryPath = null; return; }
    let p = new Path2D();
    rings.forEach(function (ring) {
      ring.forEach(function (pt, i) {
        if (i === 0) p.moveTo(pt[0], pt[1]); else p.lineTo(pt[0], pt[1]);
      });
      p.closePath();
    });
    boundaryPath = p;
  }

  function updateActiveNeighborhood() {
    if (!NB_PROJECTED) return;
    let target = null;
    if (neighborhoodPinned) {
      target = activeNeighborhood;
    } else if (view.scale >= ZOOM_FOR_AUTO_NEIGHBORHOOD) {
      target = nearestNeighborhood(view.cx, view.cy);
    }
    if (target !== activeNeighborhood || (target && !boundaryPath)) {
      activeNeighborhood = target;
      buildBoundaryPath(target);
    }
    if (!target) {
      nbInfoEl.hidden = true;
      return;
    }
    let avg = NB_AVG_SUBSIDY ? NB_AVG_SUBSIDY[target] : undefined;
    nbInfoEl.hidden = false;
    nbInfoEl.innerHTML = '<strong>' + target + '</strong> — avg. subsidy ' +
      (avg === undefined ? '—' : fmtUSD0.format(avg) + '/yr');
  }

  function tierOf(s) {
    if (s < 0) return 0;
    if (s < 5000) return 1;
    if (s < 15000) return 2;
    if (s < 30000) return 3;
    return 4;
  }
  let TIER_COLORS = (function () {
    // Single source of truth lives in styles.css (--tier-0..4) so the legend swatches
    // and this canvas rendering can never silently drift apart from each other.
    let cs = getComputedStyle(document.documentElement);
    return [0, 1, 2, 3, 4].map(function (i) { return cs.getPropertyValue('--tier-' + i).trim(); });
  })();

  function parseCSV(text) {
    let lines = text.split('\n');
    if (lines[lines.length - 1].trim() === '') lines.pop(); // drop trailing blank line
    let count = lines.length - 1; // minus header row
    pts = new Float32Array(count * 2);
    subsidy = new Float64Array(count);
    change = new Float64Array(count);
    sqft = new Float32Array(count);
    assessed = new Float64Array(count);
    market = new Float64Array(count);
    addr = new Array(count);

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let written = 0;
    for (let i = 1; i < lines.length; i++) {
      let line = lines[i];
      if (!line) continue;
      let c = line.split(',');
      if (c.length < 8) continue;
      let lat = parseFloat(c[0]), lon = parseFloat(c[1]);
      if (!isFinite(lat) || !isFinite(lon)) continue;
      let p = project(lat, lon);
      let idx = written;
      pts[idx * 2] = p.x; pts[idx * 2 + 1] = p.y;
      addr[idx] = c[2];
      sqft[idx] = parseFloat(c[3]);
      assessed[idx] = parseFloat(c[4]);
      market[idx] = parseFloat(c[5]);
      subsidy[idx] = parseFloat(c[6]);
      change[idx] = parseFloat(c[7]);
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
      written++;
    }
    // trim arrays to actual written length
    pts = pts.subarray(0, written * 2);
    subsidy = subsidy.subarray(0, written);
    change = change.subarray(0, written);
    sqft = sqft.subarray(0, written);
    assessed = assessed.subarray(0, written);
    market = market.subarray(0, written);
    addr.length = written;

    fitCx = (minX + maxX) / 2;
    fitCy = (minY + maxY) / 2;
    let spanX = Math.max(maxX - minX, 0.001), spanY = Math.max(maxY - minY, 0.001);
    fitScale = Math.min(canvas.clientWidth / spanX, canvas.clientHeight / spanY) * 0.94;

    return written;
  }

  function buildTierPaths() {
    pathsByTier = [];
    for (let t = 0; t < 5; t++) pathsByTier.push(new Path2D());
    let r = 0.00035; // dot half-size in world units
    for (let i = 0; i < subsidy.length; i++) {
      let t2 = tierOf(subsidy[i]);
      let x = pts[i * 2], y = pts[i * 2 + 1];
      pathsByTier[t2].rect(x - r, y - r, r * 2, r * 2);
    }
  }

  function buildGrid() {
    grid = new Map();
    for (let i = 0; i < subsidy.length; i++) {
      let gx = Math.round(pts[i * 2] / gridCell);
      let gy = Math.round(pts[i * 2 + 1] / gridCell);
      let key = gx + '_' + gy;
      let arr = grid.get(key);
      if (!arr) { arr = []; grid.set(key, arr); }
      arr.push(i);
    }
  }

  function parseMFCSV(text) {
    let lines = text.split('\n');
    if (lines[lines.length - 1].trim() === '') lines.pop();
    let count = lines.length - 1;
    mfPts = new Float32Array(count * 2);
    mfSubsidy = new Float64Array(count);
    mfChange = new Float64Array(count);
    mfSqft = new Float32Array(count);
    mfAssessed = new Float64Array(count);
    mfMarket = new Float64Array(count);
    mfUnits = new Float32Array(count);
    mfAddr = new Array(count);

    let written = 0;
    for (let i = 1; i < lines.length; i++) {
      let line = lines[i];
      if (!line) continue;
      let c = line.split(',');
      if (c.length < 9) continue;
      let lat = parseFloat(c[0]), lon = parseFloat(c[1]);
      if (!isFinite(lat) || !isFinite(lon)) continue;
      let p = project(lat, lon);
      let idx = written;
      mfPts[idx * 2] = p.x; mfPts[idx * 2 + 1] = p.y;
      mfAddr[idx] = c[2];
      mfUnits[idx] = parseFloat(c[3]);
      mfSqft[idx] = parseFloat(c[4]);
      mfAssessed[idx] = parseFloat(c[5]);
      mfMarket[idx] = parseFloat(c[6]);
      mfSubsidy[idx] = parseFloat(c[7]);
      mfChange[idx] = parseFloat(c[8]);
      written++;
    }
    mfPts = mfPts.subarray(0, written * 2);
    mfSubsidy = mfSubsidy.subarray(0, written);
    mfChange = mfChange.subarray(0, written);
    mfSqft = mfSqft.subarray(0, written);
    mfAssessed = mfAssessed.subarray(0, written);
    mfMarket = mfMarket.subarray(0, written);
    mfUnits = mfUnits.subarray(0, written);
    mfAddr.length = written;
    return written;
  }

  function buildMFTierPaths() {
    mfPathsByTier = [];
    for (let t = 0; t < 5; t++) mfPathsByTier.push(new Path2D());
    let r = 0.00042; // a bit larger than home dots -- fewer buildings, should still read at a glance
    for (let i = 0; i < mfSubsidy.length; i++) {
      let t2 = tierOf(mfSubsidy[i]);
      let x = mfPts[i * 2], y = mfPts[i * 2 + 1];
      // diamond: rotated square, visually distinct from the single-family dots
      mfPathsByTier[t2].moveTo(x, y - r);
      mfPathsByTier[t2].lineTo(x + r, y);
      mfPathsByTier[t2].lineTo(x, y + r);
      mfPathsByTier[t2].lineTo(x - r, y);
      mfPathsByTier[t2].closePath();
    }
  }

  function buildMFGrid() {
    mfGrid = new Map();
    for (let i = 0; i < mfSubsidy.length; i++) {
      let gx = Math.round(mfPts[i * 2] / gridCell);
      let gy = Math.round(mfPts[i * 2 + 1] / gridCell);
      let key = gx + '_' + gy;
      let arr = mfGrid.get(key);
      if (!arr) { arr = []; mfGrid.set(key, arr); }
      arr.push(i);
    }
  }

  function resizeCanvas() {
    let dpr = window.devicePixelRatio || 1;
    let w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }

  function render() {
    if (!pathsByTier) return;
    let dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    let s = fitScale * view.scale * dpr;
    ctx.setTransform(s, 0, 0, s, canvas.width / 2 - view.cx * s, canvas.height / 2 - view.cy * s);

    for (let t = 0; t < 5; t++) {
      ctx.fillStyle = TIER_COLORS[t];
      ctx.fill(pathsByTier[t]);
    }
    if (showMF && mfPathsByTier) {
      ctx.save();
      ctx.globalAlpha = 0.92;
      ctx.strokeStyle = 'rgba(255,255,255,0.6)';
      ctx.lineWidth = 0.5 / s;
      for (let t3 = 0; t3 < 5; t3++) {
        ctx.fillStyle = TIER_COLORS[t3];
        ctx.fill(mfPathsByTier[t3]);
        ctx.stroke(mfPathsByTier[t3]);
      }
      ctx.restore();
    }

    // Streets are drawn ON TOP of the property markers (not underneath): at high zoom
    // a single marker can cover most of a block, and a street drawn first would simply
    // be painted over and vanish. A dark semi-transparent casing + light centerline
    // keeps the line readable whether it crosses cream background or a colored marker.
    if (minorStreetPath && view.scale >= ZOOM_FOR_MINOR_STREETS) {
      ctx.save();
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.strokeStyle = 'rgba(58,50,38,0.35)';
      ctx.lineWidth = 1.6 / s;
      ctx.stroke(minorStreetPath);
      ctx.strokeStyle = 'rgba(255,255,255,0.8)';
      ctx.lineWidth = 0.7 / s;
      ctx.stroke(minorStreetPath);
      ctx.restore();
    }
    if (majorStreetPath) {
      ctx.save();
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.strokeStyle = 'rgba(40,34,24,0.45)';
      ctx.lineWidth = 3.4 / s;
      ctx.stroke(majorStreetPath);
      ctx.strokeStyle = 'rgba(255,255,255,0.85)';
      ctx.lineWidth = 1.6 / s;
      ctx.stroke(majorStreetPath);
      ctx.restore();
    }

    updateActiveNeighborhood();
    if (boundaryPath) {
      ctx.save();
      ctx.lineWidth = 2.2 / s;
      ctx.strokeStyle = 'rgba(28,51,46,0.85)';
      ctx.setLineDash([6 / s, 4 / s]);
      ctx.stroke(boundaryPath);
      ctx.restore();
    }

    if (labelsMajor.length && view.scale >= ZOOM_FOR_MAJOR_LABELS) {
      drawStreetLabels(labelsMajor, s, 12, 3);
    }
    if (labelsMinor.length && view.scale >= ZOOM_FOR_MINOR_LABELS) {
      drawStreetLabels(labelsMinor, s, 10.5, 2.6);
    }
  }

  function screenToWorld(px, py) {
    let dpr = window.devicePixelRatio || 1;
    let s = fitScale * view.scale * dpr;
    let cx = canvas.width / 2 - view.cx * s;
    let cy = canvas.height / 2 - view.cy * s;
    return { x: (px * dpr - cx) / s, y: (py * dpr - cy) / s };
  }

  function nearestInGrid(gridMap, ptsArr, wx, wy) {
    let gx = Math.round(wx / gridCell), gy = Math.round(wy / gridCell);
    let best = -1, bestD = Infinity;
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        let arr = gridMap.get((gx + dx) + '_' + (gy + dy));
        if (!arr) continue;
        for (let k = 0; k < arr.length; k++) {
          let i = arr[k];
          let ddx = ptsArr[i * 2] - wx, ddy = ptsArr[i * 2 + 1] - wy;
          let d = ddx * ddx + ddy * ddy;
          if (d < bestD) { bestD = d; best = i; }
        }
      }
    }
    return { index: best, distSq: bestD };
  }

  function nearestPoint(wx, wy) {
    let r = nearestInGrid(grid, pts, wx, wy);
    let maxPixelDist = 14, dpr = window.devicePixelRatio || 1;
    let s = fitScale * view.scale * dpr;
    let maxWorldDist = maxPixelDist * dpr / s;
    if (r.index >= 0 && r.distSq <= maxWorldDist * maxWorldDist) return r.index;
    return -1;
  }

  function findNearestAny(wx, wy) {
    let maxPixelDist = 14, dpr = window.devicePixelRatio || 1;
    let s = fitScale * view.scale * dpr;
    let maxWorldDist = maxPixelDist * dpr / s;
    let maxD2 = maxWorldDist * maxWorldDist;
    let sfr = grid ? nearestInGrid(grid, pts, wx, wy) : { index: -1, distSq: Infinity };
    let mf = (showMF && mfGrid) ? nearestInGrid(mfGrid, mfPts, wx, wy) : { index: -1, distSq: Infinity };
    if (sfr.index < 0 && mf.index < 0) return null;
    if (mf.index < 0 || (sfr.index >= 0 && sfr.distSq <= mf.distSq)) {
      return sfr.distSq <= maxD2 ? { type: 'sfr', index: sfr.index } : null;
    }
    return mf.distSq <= maxD2 ? { type: 'mf', index: mf.index } : null;
  }

  function showPopup(i) {
    popupAddr.textContent = addr[i] || 'Property';
    let rows = [
      ['Sqft', Math.round(sqft[i]).toLocaleString()],
      ['Assessed value', fmtUSD0.format(assessed[i])],
      ['Est. market value', fmtUSD0.format(market[i])],
      ['Est. subsidy today', fmtUSD0.format(subsidy[i]) + '/yr'],
      ['Change under reform', (change[i] >= 0 ? '+' : '') + fmtUSD0.format(change[i]) + '/yr'],
    ];
    popupBody.innerHTML = rows.map(function (r) {
      return '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>';
    }).join('');
    popupEl.hidden = false;
  }

  function showMFPopup(i) {
    popupAddr.textContent = (mfAddr[i] || 'Building') + ' (multi-family)';
    let units = mfUnits[i];
    let rows = [
      ['Units', units ? Math.round(units).toLocaleString() : '—'],
      ['Building sqft', Math.round(mfSqft[i]).toLocaleString()],
      ['Assessed value', fmtUSD0.format(mfAssessed[i])],
      ['Est. market value', fmtUSD0.format(mfMarket[i])],
    ];
    if (units > 0) {
      rows.push(['Assessed value / unit', fmtUSD0.format(mfAssessed[i] / units)]);
      rows.push(['Est. market value / unit', fmtUSD0.format(mfMarket[i] / units)]);
    }
    rows.push(['Est. subsidy today', fmtUSD0.format(mfSubsidy[i]) + '/yr']);
    rows.push(['Change under reform', (mfChange[i] >= 0 ? '+' : '') + fmtUSD0.format(mfChange[i]) + '/yr']);
    popupBody.innerHTML = rows.map(function (r) {
      return '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>';
    }).join('') + '<dd class="popup-footnote">' +
      'Building-level estimate' + (units ? '; per-unit figures divide the whole building evenly' : ', not per-unit') + '.</dd>';
    popupEl.hidden = false;
  }

  document.getElementById('sfmap-popup-close').addEventListener('click', function () {
    popupEl.hidden = true;
  });

  const mfToggle = document.getElementById('sfmap-mf-toggle');
  mfToggle.addEventListener('change', function () {
    showMF = mfToggle.checked;
    if (loaded) render();
  });

  // --- pan/zoom interaction ---
  let dragging = false, dragStart = null, dragged = false;
  canvas.addEventListener('mousedown', function (e) {
    dragging = true; dragged = false;
    dragStart = { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy };
  });
  window.addEventListener('mousemove', function (e) {
    if (!dragging || !dragStart) return;
    let dpr = window.devicePixelRatio || 1;
    let s = fitScale * view.scale * dpr;
    let dx = (e.clientX - dragStart.x) * dpr, dy = (e.clientY - dragStart.y) * dpr;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) dragged = true;
    view.cx = dragStart.cx - dx / s;
    view.cy = dragStart.cy - dy / s;
    render();
  });
  window.addEventListener('mouseup', function (e) {
    if (dragging && !dragged && loaded) {
      let rect = canvas.getBoundingClientRect();
      let wpt = screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
      let hit = findNearestAny(wpt.x, wpt.y);
      if (hit) { hit.type === 'mf' ? showMFPopup(hit.index) : showPopup(hit.index); }
    }
    dragging = false; dragStart = null;
  });

  function zoomAt(factor, px, py) {
    let before = screenToWorld(px, py);
    view.scale = Math.max(0.6, Math.min(60, view.scale * factor));
    let after = screenToWorld(px, py);
    view.cx += (before.x - after.x);
    view.cy += (before.y - after.y);
    render();
  }

  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    let rect = canvas.getBoundingClientRect();
    let factor = Math.pow(1.0015, -e.deltaY);
    zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });

  document.getElementById('sfmap-zoom-in').addEventListener('click', function () {
    zoomAt(1.5, canvas.clientWidth / 2, canvas.clientHeight / 2);
  });
  document.getElementById('sfmap-zoom-out').addEventListener('click', function () {
    zoomAt(1 / 1.5, canvas.clientWidth / 2, canvas.clientHeight / 2);
  });

  canvas.addEventListener('dblclick', function (e) {
    e.preventDefault();
    let rect = canvas.getBoundingClientRect();
    zoomAt(2, e.clientX - rect.left, e.clientY - rect.top);
  });

  // touch support (single-finger pan, pinch zoom)
  let touchState = null;
  function touchMid(touches) {
    return { x: (touches[0].clientX + touches[1].clientX) / 2, y: (touches[0].clientY + touches[1].clientY) / 2 };
  }
  function touchDist(touches) {
    return Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
  }
  function beginTouch(e) {
    let rect = canvas.getBoundingClientRect();
    if (e.touches.length === 1) {
      touchState = { mode: 'pan', x: e.touches[0].clientX, y: e.touches[0].clientY, cx: view.cx, cy: view.cy };
    } else if (e.touches.length >= 2) {
      let mid = touchMid(e.touches);
      touchState = { mode: 'pinch', dist: touchDist(e.touches), midX: mid.x - rect.left, midY: mid.y - rect.top };
    }
  }
  canvas.addEventListener('touchstart', function (e) { beginTouch(e); }, { passive: true });
  canvas.addEventListener('touchmove', function (e) {
    if (!touchState) return;
    e.preventDefault();
    let dpr = window.devicePixelRatio || 1;
    let rect = canvas.getBoundingClientRect();
    if (touchState.mode === 'pan' && e.touches.length === 1) {
      let s = fitScale * view.scale * dpr;
      let dx = (e.touches[0].clientX - touchState.x) * dpr, dy = (e.touches[0].clientY - touchState.y) * dpr;
      view.cx = touchState.cx - dx / s;
      view.cy = touchState.cy - dy / s;
      render();
    } else if (touchState.mode === 'pinch' && e.touches.length >= 2) {
      let newDist = touchDist(e.touches);
      let mid = touchMid(e.touches);
      let midX = mid.x - rect.left, midY = mid.y - rect.top;
      // zoom about the pinch midpoint, tracked continuously each move (not just gesture start)
      // so the map stays glued to the fingers instead of drifting
      if (touchState.dist > 10 && newDist > 10) {
        let factor = Math.max(0.85, Math.min(1.18, newDist / touchState.dist));
        zoomAt(factor, midX, midY);
      }
      // two fingers moving together also pans
      let s2 = fitScale * view.scale * dpr;
      view.cx -= (midX - touchState.midX) * dpr / s2;
      view.cy -= (midY - touchState.midY) * dpr / s2;
      render();
      touchState.dist = newDist;
      touchState.midX = midX; touchState.midY = midY;
    }
  }, { passive: false });
  canvas.addEventListener('touchend', function (e) {
    if (e.touches.length === 0) { touchState = null; }
    else { beginTouch(e); }
  });
  canvas.addEventListener('touchcancel', function () { touchState = null; });

  window.addEventListener('resize', function () {
    if (!loaded) return;
    resizeCanvas();
    render();
  });

  nbSelect.addEventListener('change', function () {
    if (!nbSelect.value) {
      neighborhoodPinned = false;
      activeNeighborhood = null;
      view.scale = 1; view.cx = fitCx; view.cy = fitCy;
      render();
      return;
    }
    let bar = nbSelect.value.indexOf('|');
    let name = nbSelect.value.slice(0, bar);
    let parts = nbSelect.value.slice(bar + 1).split(',');
    let p = project(parseFloat(parts[0]), parseFloat(parts[1]));
    neighborhoodPinned = true;
    activeNeighborhood = name;
    buildBoundaryPath(name);
    view.cx = p.x; view.cy = p.y; view.scale = 9;
    render();
  });

  // --- address search ---
  const searchInput = document.getElementById('sfmap-search');
  const searchResults = document.getElementById('sfmap-search-results');
  let searchTimer = null, searchMatches = [];

  function jumpToPoint(match) {
    neighborhoodPinned = false;
    if (match.type === 'mf') {
      view.cx = mfPts[match.index * 2]; view.cy = mfPts[match.index * 2 + 1]; view.scale = 16;
      render();
      showMFPopup(match.index);
    } else {
      view.cx = pts[match.index * 2]; view.cy = pts[match.index * 2 + 1]; view.scale = 16;
      render();
      showPopup(match.index);
    }
  }

  function runSearch(q) {
    searchMatches = [];
    if (!loaded || q.length < 3) { searchResults.hidden = true; searchResults.innerHTML = ''; return; }
    let ql = q.toLowerCase();
    for (let i = 0; i < addr.length && searchMatches.length < 8; i++) {
      if (addr[i] && addr[i].toLowerCase().indexOf(ql) !== -1) searchMatches.push({ type: 'sfr', index: i, label: addr[i] });
    }
    if (mfLoaded) {
      for (let j = 0; j < mfAddr.length && searchMatches.length < 8; j++) {
        if (mfAddr[j] && mfAddr[j].toLowerCase().indexOf(ql) !== -1) {
          searchMatches.push({ type: 'mf', index: j, label: mfAddr[j] + ' (multi-family)' });
        }
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

  searchInput.addEventListener('input', function () {
    let q = searchInput.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { runSearch(q); }, 150);
  });
  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && searchMatches.length) {
      jumpToPoint(searchMatches[0]);
      searchResults.hidden = true;
    } else if (e.key === 'Escape') {
      searchResults.hidden = true;
    }
  });
  searchResults.addEventListener('mousedown', function (e) {
    // mousedown (not click) so this fires before the input's blur hides the list
    let li = e.target.closest('li[data-k]');
    if (!li) return;
    let m = searchMatches[parseInt(li.getAttribute('data-k'), 10)];
    jumpToPoint(m);
    searchResults.hidden = true;
    searchInput.value = m.label;
  });

  function loadNeighborhoodExtras() {
    fetch('data/sf-neighborhoods.json')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (json) {
        if (!json) return;
        NB_BOUNDARIES = json.boundaries;
        NB_AVG_SUBSIDY = json.avg_subsidy;
        // Centroids drive both the "Jump to" dropdown and nearest-neighborhood lookups --
        // populated here (not hardcoded in source) so this data has one home, matching how
        // boundaries and avg_subsidy already work.
        let centroids = json.centroids || {};
        NB_PROJECTED = Object.keys(centroids).map(function (name) {
          let p = project(centroids[name][0], centroids[name][1]);
          return { name: name, x: p.x, y: p.y };
        });
        Object.keys(centroids).sort().forEach(function (name) {
          let opt = document.createElement('option');
          let latlon = centroids[name];
          opt.value = name + '|' + latlon[0] + ',' + latlon[1];
          opt.textContent = name;
          nbSelect.appendChild(opt);
        });
        if (loaded) render();
      })
      .catch(function () { /* boundaries/dropdown are a nice-to-have; map still works without them */ });
  }

  let mfCount = 0;
  function updateCountText() {
    let parts = [];
    if (loaded) parts.push((subsidy.length).toLocaleString() + ' homes/condos');
    if (mfLoaded) parts.push(mfCount.toLocaleString() + ' buildings');
    countEl.textContent = parts.join(', ') + ' loaded';
  }

  function loadMFData() {
    fetch('data/sf-map-data-mf.csv')
      .then(function (r) {
        if (!r.ok) throw new Error('fetch failed: ' + r.status);
        return r.text();
      })
      .then(function (text) {
        mfCount = parseMFCSV(text);
        buildMFTierPaths();
        buildMFGrid();
        mfLoaded = true;
        updateCountText();
        if (loaded) render();
      })
      .catch(function () { /* multi-family layer is additive; map still works without it */ });
  }

  function loadData() {
    if (loaded || loading) return;
    loading = true;
    loadNeighborhoodExtras();
    loadMFData();
    loadStreets();
    fetch('data/sf-map-data.csv')
      .then(function (r) {
        if (!r.ok) throw new Error('fetch failed: ' + r.status);
        return r.text();
      })
      .then(function (text) {
        let n = parseCSV(text);
        buildTierPaths();
        buildGrid();
        resizeCanvas();
        view.scale = 1; view.cx = fitCx; view.cy = fitCy;
        loaded = true;
        loadingEl.style.display = 'none';
        updateCountText();
        render();
      })
      .catch(function (err) {
        loadingEl.textContent = 'Could not load map data (' + err.message + ').';
      });
  }

  if ('IntersectionObserver' in window) {
    let io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { loadData(); io.disconnect(); }
      });
    }, { rootMargin: '300px' });
    io.observe(wrap);
  } else {
    loadData();
  }
})();
