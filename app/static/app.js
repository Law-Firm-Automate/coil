// Small helpers: confirm buttons, running timer display, copy-to-clipboard.
document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-confirm]');
  if (el && !confirm(el.getAttribute('data-confirm'))) { e.preventDefault(); e.stopPropagation(); }
  var cp = e.target.closest('[data-copy]');
  if (cp) { navigator.clipboard.writeText(cp.getAttribute('data-copy')); cp.textContent = 'Copied'; }
});
(function () {
  var t = document.querySelector('[data-timer-seconds]');
  if (!t) return;
  var running = t.getAttribute('data-timer-running') === '1';
  var s = parseInt(t.getAttribute('data-timer-seconds'), 10) || 0;
  function fmt(x) { var h = Math.floor(x / 3600), m = Math.floor((x % 3600) / 60), sec = x % 60;
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m + ':' + (sec < 10 ? '0' : '') + sec; }
  t.textContent = fmt(s);
  if (running) setInterval(function () { s++; t.textContent = fmt(s); }, 1000);
})();
