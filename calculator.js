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
  const rateOutEl = document.getElementById('proposedRateOut');
  const impliedGrowthEl = document.getElementById('impliedGrowth');

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

    rateOutEl.textContent = proposedRate.toFixed(2) + '%';
    const impliedGrowth = (CURRENT_GENERAL_RATE / proposedRate - 1) * 100;
    impliedGrowthEl.textContent = impliedGrowth.toFixed(1) + '%';

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
  [assessedEl, marketEl, bondEl, rateEl].forEach(function (el) {
    el.addEventListener('input', recalc);
  });

  recalc();

  // ---------- Get involved (non-functional by design) ----------
  const signupForm = document.getElementById('signupForm');
  const formStatus = document.getElementById('formStatus');
  signupForm.addEventListener('submit', function (e) {
    e.preventDefault();
    formStatus.textContent = 'Thanks for the interest — sign-ups aren\'t active yet, so nothing was sent or saved.';
  });
})();
