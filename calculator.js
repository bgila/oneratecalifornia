(function () {
  'use strict';

  // Single source of truth for the campaign brand name.
  // Change this one value to update every mention of the brand on the page.
  const BRAND = "One Rate California";

  document.title = BRAND + " — Californians for Fair Property Taxes";
  document.querySelectorAll('.brand-name').forEach(function (el) {
    el.textContent = BRAND;
  });

  // ---------- Calculator ----------
  const assessedEl = document.getElementById('assessedValue');
  const marketEl = document.getElementById('marketValue');
  const countyEl = document.getElementById('county');
  const bondEl = document.getElementById('bondRate');
  const rateEl = document.getElementById('proposedRate');
  const rateNumEl = document.getElementById('proposedRateNum');
  const impliedGrowthNoteEl = document.getElementById('impliedGrowthNote');

  const currentBillOut = document.getElementById('currentBillOut');
  const currentBillDetail = document.getElementById('currentBillDetail');
  const proposedBillOut = document.getElementById('proposedBillOut');
  const proposedBillDetail = document.getElementById('proposedBillDetail');
  const billDiffOut = document.getElementById('billDiffOut');

  const CURRENT_GENERAL_RATE = 1.00; // fixed by Prop 13

  const fmtUSD = new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0
  });

  function num(el) {
    const v = parseFloat(el.value);
    return isFinite(v) ? v : 0;
  }

  function recalc() {
    const assessed = Math.max(0, num(assessedEl));
    const market = Math.max(0, num(marketEl));
    const bond = Math.max(0, num(bondEl));
    const proposedRate = num(rateEl);

    if (proposedRate <= 0) {
      impliedGrowthNoteEl.textContent = 'A 0% general rate isn\'t a realistic revenue-neutral scenario -- ' +
        'just an illustration of the low end of the slider.';
    } else if (proposedRate >= CURRENT_GENERAL_RATE) {
      impliedGrowthNoteEl.textContent = 'At or above today\'s ' + CURRENT_GENERAL_RATE.toFixed(2) + '% rate, ' +
        'this wouldn\'t need any assessment-base growth to stay revenue-neutral -- it would simply raise revenue.';
    } else {
      const impliedGrowth = (CURRENT_GENERAL_RATE / proposedRate - 1) * 100;
      impliedGrowthNoteEl.innerHTML = 'This implies a statewide assessment-base growth of about ' +
        '<strong id="impliedGrowth">' + impliedGrowth.toFixed(1) + '%</strong> relative to today, for the reform to stay revenue-neutral.';
    }

    const currentBill = assessed * (CURRENT_GENERAL_RATE + bond) / 100;
    const proposedBill = market * (proposedRate + bond) / 100;

    currentBillOut.textContent = fmtUSD.format(currentBill) + '/yr';
    currentBillDetail.textContent = assessed.toLocaleString('en-US') + ' assessed × ' +
      (CURRENT_GENERAL_RATE + bond).toFixed(2) + '% (general + bond)';

    proposedBillOut.textContent = fmtUSD.format(proposedBill) + '/yr';
    proposedBillDetail.textContent = market.toLocaleString('en-US') + ' market value × ' +
      (proposedRate + bond).toFixed(2) + '% (general + bond)';

    billDiffOut.classList.remove('up', 'down', 'flat');
    if (currentBill <= 0) {
      billDiffOut.textContent = 'Enter your numbers above.';
      billDiffOut.classList.add('flat');
      return;
    }

    const diff = proposedBill - currentBill;
    const pct = (diff / currentBill) * 100;
    const sign = diff >= 0 ? '+' : '−';

    if (Math.abs(pct) < 0.5) {
      billDiffOut.classList.add('flat');
      billDiffOut.textContent = 'About the same: ' + fmtUSD.format(Math.abs(diff)) + '/yr difference (' + pct.toFixed(1) + '%)';
    } else if (diff > 0) {
      billDiffOut.classList.add('up');
      billDiffOut.textContent = 'Increase of ' + fmtUSD.format(diff) + '/yr (' + sign + pct.toFixed(1) + '%)';
    } else {
      billDiffOut.classList.add('down');
      billDiffOut.textContent = 'Decrease of ' + fmtUSD.format(Math.abs(diff)) + '/yr (' + pct.toFixed(1) + '%)';
    }
  }

  countyEl.addEventListener('change', function () {
    bondEl.value = countyEl.value;
    recalc();
  });
  [assessedEl, marketEl, bondEl].forEach(function (el) {
    el.addEventListener('input', recalc);
  });
  rateEl.addEventListener('input', function () {
    rateNumEl.value = rateEl.value;
    recalc();
  });
  rateNumEl.addEventListener('input', function () {
    rateEl.value = rateNumEl.value;
    recalc();
  });

  recalc();

  // ---------- Address lookup (optional, autofills assessed/market value) ----------
  const addressEl = document.getElementById('calcAddress');
  const addressResultsEl = document.getElementById('calcAddressResults');
  let addrData = null; // [{addr, assessed, market}], lazily fetched on first use
  let addrLoading = false;
  let addrMatches = [];

  function parseAddressCSV(text) {
    const lines = text.split('\n');
    if (lines[lines.length - 1].trim() === '') lines.pop();
    const rows = [];
    for (let i = 1; i < lines.length; i++) {
      const c = lines[i].split(',');
      if (c.length < 7) continue;
      const assessed = parseFloat(c[4]), market = parseFloat(c[5]);
      if (!c[2] || !isFinite(assessed) || !isFinite(market)) continue;
      rows.push({ addr: c[2], assessed: assessed, market: market });
    }
    return rows;
  }

  function ensureAddrData(cb) {
    if (addrData) { cb(); return; }
    if (addrLoading) return;
    addrLoading = true;
    fetch('data/sf-map-data.csv')
      .then(function (r) { if (!r.ok) throw new Error('fetch failed'); return r.text(); })
      .then(function (text) {
        addrData = parseAddressCSV(text);
        addrLoading = false;
        cb();
      })
      .catch(function () {
        addrLoading = false;
        addressResultsEl.innerHTML = '<li class="search-empty">Could not load address data</li>';
        addressResultsEl.hidden = false;
      });
  }

  function runAddressSearch(q) {
    const ql = q.toLowerCase();
    addrMatches = addrData.filter(function (r) { return r.addr.toLowerCase().indexOf(ql) !== -1; }).slice(0, 8);
    if (!addrMatches.length) {
      addressResultsEl.innerHTML = '<li class="search-empty">No matches</li>';
      addressResultsEl.hidden = false;
      return;
    }
    addressResultsEl.innerHTML = addrMatches.map(function (m, k) {
      return '<li data-k="' + k + '">' + m.addr + '</li>';
    }).join('');
    addressResultsEl.hidden = false;
  }

  function selectAddressMatch(m) {
    assessedEl.value = Math.round(m.assessed);
    marketEl.value = Math.round(m.market);
    addressEl.value = m.addr;
    addressResultsEl.hidden = true;
    recalc();
  }

  let addrSearchTimer = null;
  addressEl.addEventListener('input', function () {
    const q = addressEl.value.trim();
    clearTimeout(addrSearchTimer);
    if (q.length < 3) { addressResultsEl.hidden = true; addressResultsEl.innerHTML = ''; return; }
    addrSearchTimer = setTimeout(function () {
      ensureAddrData(function () { runAddressSearch(q); });
    }, 150);
  });
  addressEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && addrMatches.length) {
      selectAddressMatch(addrMatches[0]);
    } else if (e.key === 'Escape') {
      addressResultsEl.hidden = true;
    }
  });
  addressResultsEl.addEventListener('mousedown', function (e) {
    const li = e.target.closest('li[data-k]');
    if (!li) return;
    selectAddressMatch(addrMatches[parseInt(li.getAttribute('data-k'), 10)]);
  });

  // ---------- Get involved: optional name feeds the mailto contact link ----------
  const involvedNameEl = document.getElementById('involvedName');
  const contactUsBtn = document.getElementById('contactUsBtn');

  function updateContactHref() {
    const name = involvedNameEl.value.trim();
    const body = name ? ('Hi, my name is ' + name + '.\n\n') : '';
    contactUsBtn.href = 'mailto:barak.gila@gmail.com?subject=' + encodeURIComponent('One Rate California') +
      '&body=' + encodeURIComponent(body);
  }
  involvedNameEl.addEventListener('input', updateContactHref);
  updateContactHref();
})();
