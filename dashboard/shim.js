// A stand-in for the claude.ai Dashboard runtime, so dashboard/page.html also works as a local file.
// It implements only what the page uses: dash.calc, dash.draw, dash.data, dash.onData, dash.colors and
// dash.setLink. Datasets come from window.__DASH_DATA__ ({ledger: [...], market: [...] or null}), embedded
// by `fr dashboard`. A null dataset shows as "Failed to load", the same as a broken source online.
(function () {
  const sources = window.__DASH_DATA__ || {};
  const calcs = {};
  const draws = {};
  const listeners = [];
  let cache = {};

  function data(id) {
    if (cache[id]) return cache[id];
    if (Object.prototype.hasOwnProperty.call(sources, id)) {
      const rows = sources[id];
      cache[id] = rows == null ? { status: 'error', data: [], error: `${id} was not built` } : { status: 'ok', data: rows };
      return cache[id];
    }
    const calc = calcs[id];
    if (!calc) return { status: 'error', data: [], error: `unknown source ${id}` };
    const inputs = (calc.inputs || []).map(data);
    if (inputs.some((d) => d.status === 'error')) {
      cache[id] = { status: 'error', data: [], error: 'an input failed' };
      return cache[id];
    }
    try {
      cache[id] = { status: 'ok', data: calc.fn(...inputs.map((d) => d.data)) || [] };
    } catch (e) {
      console.error(`calc ${id}`, e);
      cache[id] = { status: 'error', data: [], error: String(e) };
    }
    return cache[id];
  }

  function render() {
    for (const [name, fns] of Object.entries(draws)) {
      document.querySelectorAll(`[data-draw="${name}"]`).forEach((el) => {
        fns.forEach((fn) => {
          try { fn(el); } catch (e) { console.error(`draw ${name}`, e); }
        });
      });
    }
    listeners.forEach((cb) => {
      try { cb(); } catch (e) { console.error('onData', e); }
    });
  }

  window.dash = {
    calc(id, spec) { calcs[id] = spec; cache = {}; },
    draw(name, fn) { (draws[name] = draws[name] || []).push(fn); },
    data,
    onData(cb) { listeners.push(cb); },
    setLink(id) { history.replaceState(null, '', `#${id}`); },
    colors: ['#3b6fb6', '#d07a2a', '#3f9a6b', '#b8475c', '#7a5bb5', '#2b98a6', '#9c8a3a', '#6b7280'],
  };

  document.addEventListener('DOMContentLoaded', render);
  let pending = null;
  window.addEventListener('resize', () => {
    clearTimeout(pending);
    pending = setTimeout(render, 150);
  });
}());
