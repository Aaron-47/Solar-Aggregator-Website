/* Live slot inventory - polls the availability API and updates the
   "MW still open" counters, the fill bars and the prices on the page
   without a reload. See /api/offers. */
(function () {
  var REFRESH_MS = 15000;

  function fmt(n) {
    var v = Math.round((n || 0) * 100) / 100;
    return v.toString() + ' MW';
  }

  /* The per-slot counters live inside a span that already carries the unit
     in the surrounding text ("<span>5</span> MW still open"), so only the
     number is written back here. */
  function num(n) {
    var v = Math.round((n || 0) * 100) / 100;
    return v.toString();
  }

  function update() {
    fetch('/api/offers', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.offers || []).forEach(function (o) {
          document.querySelectorAll('[data-offer="' + o.id + '"]').forEach(function (el) {
            var kind = el.getAttribute('data-live');
            if (kind === 'remaining') { el.textContent = num(o.remaining_mw); }
            else if (kind === 'booked') { el.textContent = num(o.booked_mw); }
            else if (kind === 'pct') { el.textContent = num(o.pct); }
            else if (kind === 'bar') { el.style.width = o.pct + '%'; }
            else if (kind === 'price') { el.textContent = o.price.toFixed(2); }
            else if (kind === 'waitlist') { el.textContent = o.waitlist; }
          });
          document.querySelectorAll('[data-slot="' + o.id + '"]').forEach(function (el) {
            var kind = el.getAttribute('data-when');
            if (kind === 'full') { el.hidden = !o.full; }
            else if (kind === 'open') { el.hidden = o.full; }
          });
        });
        var cap = data.capacity || {};
        document.querySelectorAll('[data-capacity]').forEach(function (el) {
          var key = el.getAttribute('data-capacity');
          if (key === 'remaining') { el.textContent = fmt(cap.remaining_mw); }
          else if (key === 'booked') { el.textContent = fmt(cap.booked_mw); }
          else if (key === 'total') { el.textContent = fmt(cap.capacity_mw); }
          else if (key === 'slots') { el.textContent = cap.slots || 0; }
          else if (key === 'pct') { el.textContent = (cap.pct || 0).toFixed(0) + '%'; }
          else if (key === 'waitlist') { el.textContent = cap.waitlist || 0; }
        });
        var stamp = document.querySelector('[data-live-stamp]');
        if (stamp) { stamp.textContent = 'live - updated ' + (data.generated_at || '').slice(11); }
      })
      .catch(function () { /* offline - keep the last numbers on screen */ });
  }

  function start() {
    update();
    setInterval(update, REFRESH_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
