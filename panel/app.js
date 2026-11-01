// soma · panel agenta. Wszystkie teksty z danych (nazwy tokenów wpisują ich twórcy) idą przez esc().
const TOKEN = document.querySelector('meta[name="panel-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const usd = (v, d) => v == null || isNaN(v) ? '—' : (Math.abs(v) >= 1000 ? '$' + (v / 1000).toFixed(v >= 1e6 ? 0 : 0) + 'k' : '$' + Number(v).toFixed(d ?? (Math.abs(v) < 10 ? 2 : 0)));
const big = (v) => v == null ? '—' : v >= 1e6 ? '$' + (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? '$' + Math.round(v / 1e3) + 'k' : '$' + Math.round(v);
const signed = (v, d = 0, suffix = '%') => v == null || isNaN(v) ? '—' : `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : ''}">${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(Number(v)).toFixed(d)}${suffix}</span>`;
const signedUsd = (v) => v == null ? '—' : `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : ''}">${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(Number(v)).toFixed(2)} USD</span>`;
const short = (a) => a ? `${a.slice(0, 4)}…${a.slice(-4)}` : '';
const ago = (ts) => { const s = Date.now() / 1000 - ts; return s < 90 ? `${Math.round(s)} s temu` : s < 5400 ? `${Math.round(s / 60)} min temu` : `${(s / 3600).toFixed(1)} h temu`; };
const hm = (ts) => new Date(ts * 1000).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });
const empty = (t) => `<p class="empty">${esc(t)}</p>`;

async function api(path, body) {
  const opt = { headers: { 'X-Panel-Token': TOKEN } };
  if (body) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch('/api/' + path, opt);
  const data = await r.json().catch(() => ({ error: 'Panel nie odpowiedział poprawnie.' }));
  if (!r.ok) throw new Error(data.error || `Błąd ${r.status}`);
  return data;
}

let toastTimer;
function toast(text, err) {
  const t = $('#toast');
  t.textContent = text; t.className = 'toast show' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = 'toast', 4200);
}

// ------------------------------------------------------------------ motyw i nawigacja
const root = document.documentElement;
try { const t = localStorage.getItem('panel-theme'); if (t) root.dataset.theme = t; } catch (e) {}
$('#theme').onclick = () => {
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('panel-theme', root.dataset.theme); } catch (e) {}
};

let view = 'przeglad';
function show(v) {
  view = v;
  $$('.nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach((s) => s.classList.toggle('active', s.dataset.panel === v));
  try { history.replaceState(null, '', '#' + v); } catch (e) {}
  refresh();
}
$$('.nav button').forEach((b) => b.onclick = () => show(b.dataset.view));

// ------------------------------------------------------------------ usługi w tle
const SVC = {
  czuwaj: { name: 'Czuwanie', desc: 'Co 2 min ceny obserwowanych tokenów, flipy na sucho i alarmy pozycji agenta.' },
  snajper: { name: 'Snajper na sucho', desc: 'Nowe tokeny z pump.fun na żywo. Niczego nie kupuje.' },
};
let services = {};

function svcState(name) {
  const s = services[name];
  if (!s) return '<span class="dot"></span> sprawdzam';
  if (name === 'snajper' && s.live) return '<span class="dot live"></span> działa naprawdę (Terminal)';
  if (!s.running) return '<span class="dot"></span> wyłączone';
  return `<span class="dot on"></span> działa${s.panel ? '' : ' w Terminalu'}`;
}
function svcButton(name) {
  const s = services[name] || {};
  return s.running
    ? `<button class="btn ghost sm" data-svc="${name}" data-act="stop">Zatrzymaj</button>`
    : `<button class="btn primary sm" data-svc="${name}" data-act="start">Uruchom</button>`;
}
function renderServices() {
  $('#svc-mini').innerHTML = Object.entries(SVC).map(([n, m]) => `<span class="mini"><b>${m.name.split(' ')[0]}</b><span>${svcState(n)}</span></span>`).join('');
  $('#svc-cards').innerHTML = Object.entries(SVC).map(([n, m]) => `
    <div class="glass svc"><div class="top"><span class="name">${m.name}</span>${svcButton(n)}</div>
    <p>${m.desc}</p><span class="svc-inline">${svcState(n)}</span></div>`).join('');
  for (const n of Object.keys(SVC)) {
    const el = $('#svc-' + n);
    if (el) el.innerHTML = svcState(n) + svcButton(n);
    const log = $('#log-' + n);
    if (log && services[n]) log.textContent = services[n].log.join('\n') || 'Jeszcze nic. Uruchom z panelu, a tu pojawi się dziennik.';
  }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-svc]');
  if (!b) return;
  b.disabled = true;
  try { const r = await api('usluga', { name: b.dataset.svc, action: b.dataset.act }); toast(`${SVC[b.dataset.svc].name}: ${r.msg}`); }
  catch (err) { toast(err.message, true); }
  setTimeout(loadServices, 900);
});
async function loadServices() { try { services = await api('uslugi'); renderServices(); } catch (e) {} }

// ------------------------------------------------------------------ przegląd
function kpi(k, v, s) { return `<div class="glass kpi"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="s">${s || ''}</div></div>`; }

async function loadOverview() {
  const d = await api('stan');
  const a = d.agent || {}, p = d.private || {};
  const pnl = (a.positions || []).reduce((s, x) => s + (x.value_usd - x.value_usd / (1 + x.pnl_pct / 100)), 0);
  $('#kpis').innerHTML = [
    kpi('Agent', usd(a.total_usd), `SOL ${Number(a.sol || 0).toFixed(4)} · pozycje ${signedUsd(pnl)}`),
    kpi('Prywatny', usd(p.total_usd), `SOL ${Number(p.sol || 0).toFixed(4)}`),
    kpi('Dziennik sygnałów', d.journal_entries ?? '—', d.journal_stats?.p72 ? `po 72 h mediana ${signed(d.journal_stats.p72.median_pct)}` : ''),
    kpi('Helius w tym miesiącu', (d.helius_month ?? 0).toLocaleString('pl-PL'), `z ${(d.helius_budget ?? 0).toLocaleString('pl-PL')} kredytów · raport ${d.report_age_h != null ? d.report_age_h.toFixed(0) + ' h temu' : 'brak'}`),
  ].join('');
  $('#agent-addr').textContent = short(a.wallet);
  const posTable = (rows, click) => rows.length ? `<table><thead><tr><th>Token</th><th class="r">Wartość</th><th class="r">Wynik</th><th>Podpowiedź</th></tr></thead><tbody>${
    rows.map((x) => `<tr${click ? ` class="click" data-sell="${esc(x.mint)}"` : ''}><td class="sym">${esc(x.symbol)}</td><td class="r">${usd(x.value_usd)}</td><td class="r">${signed(x.pnl_pct)}</td><td>${x.hint ? `<span class="tag rose">${esc(x.hint)}</span>` : '<span class="dim">trzymaj</span>'}</td></tr>`).join('')}</tbody></table>` : empty('Brak pozycji.');
  $('#agent-pos').innerHTML = posTable(a.positions || []);
  $('#trade-pos').innerHTML = posTable(a.positions || [], true);
  const priv = (p.positions || []).filter((x) => !x.dust);
  const dust = (p.positions || []).length - priv.length;
  $('#private-pos').innerHTML = priv.length ? `<table><thead><tr><th>Token</th><th class="r">Wartość</th><th class="r">24 h</th><th></th></tr></thead><tbody>${
    priv.map((x) => `<tr><td class="sym">${esc(x.symbol)}</td><td class="r">${usd(x.value_usd)}</td><td class="r">${signed(x.change_24h, 1)}</td><td>${(x.flags || []).map((f) => `<span class="tag warn">${esc(f)}</span>`).join(' ')}</td></tr>`).join('')}</tbody></table>${dust ? `<p class="dim">Do tego ${dust} tokenów pyłu (poniżej $0,50).</p>` : ''}` : empty('Brak danych portfela prywatnego.');
}

// ------------------------------------------------------------------ czuwanie
function statTile(name, st, extra) {
  if (!st) return kpi(name, '—', 'jeszcze brak zamkniętych');
  return kpi(name, signed(st.median_pct, 1), `mediana · ${st.n} zamkniętych · na plusie ${st.win_pct}% · ${signedUsd(st.usd)}${extra ? ' · ' + extra(st) : ''}`);
}

async function loadFlips() {
  const d = await api('flipy');
  $('#flip-stats').innerHTML = statTile('Dołek', d.stats['dołek']) + statTile('Wybicie', d.stats['wybicie']) + statTile('Razem', d.stats['razem'],
    (st) => st.n < 20 ? `wnioski przy ok. 20` : 'próbka wystarczy na pierwszy wniosek');
  $('#watch').innerHTML = d.watch.length ? `<table><thead><tr><th>Token</th><th class="r">Płynność</th><th class="r">15 min</th><th class="r">1 h</th><th class="r">4 h</th><th></th></tr></thead><tbody>${
    d.watch.map((w) => `<tr><td class="sym">${esc(w.symbol)}${w.low_liq ? ' <span class="tag warn">mała płynność</span>' : ''}</td><td class="r">${big(w.liquidity)}</td><td class="r">${signed(w.ch15)}</td><td class="r">${signed(w.ch60)}</td><td class="r">${signed(w.ch240)}</td>
      <td class="r"><button class="btn ghost sm" data-unwatch="${esc(w.mint)}">Usuń</button></td></tr>`).join('')}</tbody></table>` : empty('Lista jest pusta. Dodaj token poniżej.');
  $('#flips-open').innerHTML = d.open.length ? `<table><thead><tr><th>Token</th><th>Strategia</th><th class="r">Wejście</th><th class="r">Od</th></tr></thead><tbody>${
    d.open.map((f) => `<tr><td class="sym">${esc(f.symbol)}</td><td><span class="tag blue">${esc(f.strategy)}</span></td><td class="r">${hm(f.entry_ts)}</td><td class="r">${ago(f.entry_ts)}</td></tr>`).join('')}</tbody></table>` : empty('Brak otwartych flipów.');
}
$('#watch-add').onsubmit = async (e) => {
  e.preventDefault();
  const mint = e.target.mint.value.trim(), msg = $('#watch-msg');
  try { const r = await api('obserwuj', { dodaj: mint }); msg.textContent = r.msg; msg.className = 'msg' + (r.ok ? '' : ' err'); if (r.ok) e.target.reset(); loadFlips(); }
  catch (err) { msg.textContent = err.message; msg.className = 'msg err'; }
};
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-unwatch]');
  if (!b) return;
  try { const r = await api('obserwuj', { usun: b.dataset.unwatch }); toast(r.msg, !r.ok); loadFlips(); } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------------ snajper
let snRange = 'day', snData;
$$('#sn-range button').forEach((b) => b.onclick = () => { snRange = b.dataset.r; $$('#sn-range button').forEach((x) => x.classList.toggle('on', x === b)); renderSniper(); });
$('#sn-hide-start').onchange = () => renderSniper();

function renderSniper() {
  const d = snData; if (!d) return;
  const s = services.snajper || {}, L = d.limits;
  const live = s.live;
  $('#sn-mode').innerHTML = `<div><div class="t">${live ? '<span class="dot live"></span> Prawdziwy handel działa w Terminalu' : s.running ? '<span class="dot on"></span> Snajper działa na sucho' : '<span class="dot"></span> Snajper wyłączony'}
      ${d.stop ? '<span class="tag bad">nowe zakupy wstrzymane</span>' : ''}</div>
      <p>${live ? `Naprawdę handluje strategia „${esc(L.strategy)}”: ${usd(L.usd)} na pozycję, najwyżej ${L.max_open} otwarte, ${L.max_buys} zakupów i ${usd(L.max_loss)} straty na dobę.`
        : d.wallet ? 'Prawdziwy tryb uruchamiasz w Terminalu: <code>python3 agent.py snajper --naprawde</code>'
        : 'Do prawdziwego trybu potrzebny jest osobny portfel: <code>python3 agent.py nowy-portfel --snajper</code>'}</p></div>
    <div class="actions">${live ? '' : svcButton('snajper')}
      <button class="btn ${d.stop ? 'primary' : 'warm'} sm" id="sn-stop">${d.stop ? 'Wznów zakupy' : 'Wstrzymaj zakupy'}</button></div>`;
  $('#sn-stop').onclick = async () => { try { const r = await api('snajper-stop', { on: !d.stop }); toast(r.stop ? 'Nowe zakupy snajpera wstrzymane.' : 'Snajper znów może kupować.'); loadSniper(); } catch (e) { toast(e.message, true); } };

  const st = d[snRange] || {};
  const tile = (name, x) => x ? kpi(name, signed(x.median_pct, 1), `mediana · ${x.n} pozycji · ${signedUsd(x.usd)} · do celu ${x.hit_tp_pct}%`) : kpi(name, '—', 'brak zamkniętych');
  $('#sn-stats').innerHTML = tile('Start', st.start) + tile('Rozpęd', st['rozpęd']) + tile('Naprawdę', st['naprawdę']);
  const rows = Object.entries(st);
  $('#sn-table').innerHTML = rows.length ? `<table><thead><tr><th>Strategia</th><th class="r">Pozycje</th><th class="r">Na plusie</th><th class="r">Mediana</th><th class="r">Razem</th><th class="r">≥ 80% w dół</th><th class="r">Najlepsza</th></tr></thead><tbody>${
    rows.map(([k, x]) => `<tr><td class="sym">${esc(k)}</td><td class="r">${x.n}</td><td class="r">${x.win_pct}%</td><td class="r">${signed(x.median_pct, 1)}</td><td class="r">${signedUsd(x.usd)}</td><td class="r">${x.dead_pct}%</td><td class="r">${signed(x.best_pct)}</td></tr>`).join('')}</tbody></table>
    <p class="dim">Wnioski dopiero przy kilkuset pozycjach. Pojedyncze +500% to nie przewaga.</p>` : empty('Jeszcze brak zamkniętych pozycji.');
  const open = d.open || [];
  $('#sn-open-n').textContent = open.length ? `${open.length}` : '';
  const shown = open.filter((p) => p.live || p.strategy !== 'start' || !$('#sn-hide-start').checked);
  $('#sn-open').innerHTML = shown.length ? `<table><thead><tr><th>Token</th><th>Strategia</th><th class="r">Szczyt</th><th class="r">Od</th></tr></thead><tbody>${
    shown.map((p) => `<tr><td class="sym">${esc(p.symbol)} ${p.live ? '<span class="tag grad">naprawdę</span>' : ''}</td><td><span class="tag blue">${esc(p.strategy)}</span></td><td class="r">${signed((p.peak / p.entry - 1) * 100)}</td><td class="r">${ago(p.entry_ts)}</td></tr>`).join('')}</tbody></table>`
    : empty(open.length ? `Otwarte są tylko pozycje „start” (${open.length}).` : 'Brak otwartych pozycji.');
  const recent = (d.recent || []).filter((r) => r.live || r.strategy !== 'start' || !$('#sn-hide-start').checked).slice(0, 14);
  $('#sn-recent').innerHTML = recent.length ? `<table><thead><tr><th>Token</th><th>Strategia</th><th>Wyjście</th><th class="r">Wynik</th><th class="r">Szczyt</th><th class="r">Czas</th></tr></thead><tbody>${
    recent.map((r) => `<tr><td class="sym">${esc(r.symbol)} ${r.live ? '<span class="tag grad">naprawdę</span>' : ''}</td><td><span class="tag blue">${esc(r.strategy)}</span></td><td class="dim">${esc((r.exits || []).map((x) => x.reason).join(' → '))}</td><td class="r">${signed(r.net_pct)}</td><td class="r">${signed(r.max_pct)}</td><td class="r">${r.hold_s} s</td></tr>`).join('')}</tbody></table>` : empty('Jeszcze nic się nie zamknęło.');
}
async function loadSniper() { snData = await api('snajper'); renderSniper(); }

// ------------------------------------------------------------------ wieloryby
async function loadWhales() {
  const d = await api('wieloryby');
  const r = d.report;
  $('#rep-when').textContent = r ? `${new Date(r.created).toLocaleString('pl-PL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })} · ${r.wallets_checked} portfeli` : '';
  $('#signals').innerHTML = r && r.signals.length ? `<table><thead><tr><th>Token</th><th class="r">Wynik</th><th class="r">Portfele</th><th class="r">Kupili netto</th><th class="r">Płynność</th><th class="r">24 h</th><th>Uwagi</th><th></th></tr></thead><tbody>${
    r.signals.map((s) => `<tr><td><span class="sym">${esc(s.symbol)}</span> <span class="dim">${esc(s.name)}</span></td><td class="r">${Number(s.score).toFixed(0)}</td><td class="r">${s.wallets}</td><td class="r">${usd(s.net_usd)}</td><td class="r">${big(s.liquidity)}</td><td class="r">${signed(s.change_24h)}</td>
      <td>${(s.flags || []).map((f) => `<span class="tag warn">${esc(f)}</span>`).join(' ') || '<span class="dim">brak</span>'}</td>
      <td class="r"><button class="btn ghost sm" data-buy="${esc(s.mint)}">Podgląd zakupu</button></td></tr>`).join('')}</tbody></table>` : empty('Brak zapisanego raportu z sygnałami.');
  const j = d.journal || {};
  $('#jr-n').textContent = j.entries ? `${j.entries} wpisów` : '';
  const groups = Object.entries(j.stats || {});
  $('#journal').innerHTML = groups.length ? `<table><thead><tr><th>Grupa</th><th class="r">Po 24 h</th><th class="r">Po 72 h</th><th class="r">Do 2×</th><th class="r">−40%</th></tr></thead><tbody>${
    groups.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="r">${v.p24 ? signed(v.p24.median_pct) + ` <span class="dim">(${v.p24.n})</span>` : '—'}</td><td class="r">${v.p72 ? signed(v.p72.median_pct) + ` <span class="dim">(${v.p72.n})</span>` : '—'}</td><td class="r">${v.p72 ? v.p72.hit_2x_pct + '%' : '—'}</td><td class="r">${v.p72 ? v.p72.stop_pct + '%' : '—'}</td></tr>`).join('')}</tbody></table>` : empty('Dziennik jest pusty.');
  const w = Object.entries(d.wallets || {});
  $('#wallets').innerHTML = w.length ? `<table><thead><tr><th>Portfel</th><th class="r">Ocena</th><th class="r">Tokeny na plusie</th><th class="r">Trzyma</th><th class="r">Wynik</th></tr></thead><tbody>${
    w.map(([addr, x]) => `<tr><td><a class="mono" href="https://solscan.io/account/${esc(addr)}" target="_blank" rel="noopener">${esc(short(addr))}</a></td><td class="r">${Number(x.score).toFixed(1)}</td><td class="r">${x.won}/${x.tokens}</td><td class="r">${Number(x.hold_h).toFixed(0)} h</td><td class="r">${signedUsd(x.pnl_usd)}</td></tr>`).join('')}</tbody></table>` : empty('Lista portfeli jest pusta.');
}
$$('[data-job]').forEach((b) => b.onclick = async () => {
  const label = b.dataset.job === 'raport' ? 'nowy raport' : 'odkrywanie portfeli';
  if (!confirm(`Uruchomić ${label}? To zużywa kredyty Helius.`)) return;
  try { await api('zadanie', { kind: b.dataset.job }); toast(`Uruchomiono: ${label}.`); loadJobs(); } catch (e) { toast(e.message, true); }
});
let jobsPoll;
async function loadJobs() {
  const list = await api('zadania');
  $('#jobs').innerHTML = list.slice(0, 3).map((j) => `<div class="glass job"><div class="top"><span>${j.kind === 'raport' ? 'Raport' : 'Odkrywanie'} · ${hm(j.started)}</span>
    ${j.done ? (j.code === 0 ? '<span class="tag ok">gotowe</span>' : '<span class="tag bad">błąd</span>') : '<span class="tag blue">trwa…</span>'}</div>
    ${j.done ? `<pre class="log">${esc(j.kind === 'raport' && j.code === 0 ? 'Raport zapisany, sygnały poniżej.' : j.out.slice(-3000))}</pre>` : ''}</div>`).join('');
  clearTimeout(jobsPoll);
  if (list.some((j) => !j.done)) jobsPoll = setTimeout(loadJobs, 3000);
  else if (list.length) loadWhales();
}

// ------------------------------------------------------------------ handel
let side = 'kup', pending = null;
$$('#side button').forEach((b) => b.onclick = () => setSide(b.dataset.s));
function setSide(s) {
  side = s;
  $$('#side button').forEach((x) => x.classList.toggle('on', x.dataset.s === s));
  $$('#trade [data-for]').forEach((f) => f.hidden = f.dataset.for !== s);
  resetConfirm();
}
function resetConfirm() { pending = null; $('#confirm').hidden = true; }
function tradeBody(confirm) {
  const f = $('#trade');
  const body = { side, mint: f.mint.value.trim() };
  if (side === 'kup') body.usd = parseFloat(f.usd.value); else body.procent = parseFloat(f.procent.value);
  if (confirm) body.confirm = true;
  return body;
}
$('#trade').onsubmit = async (e) => {
  e.preventDefault();
  resetConfirm();
  const btn = e.submitter; btn.disabled = true;
  const body = tradeBody(false);
  $('#preview').textContent = 'Pytam Jupitera o cenę…';
  try {
    const r = await api('handel', body);
    $('#preview').textContent = r.msg.replace(/\n*To tylko podgląd[\s\S]*$/, '');
    if (r.ok && /To tylko podgląd/.test(r.msg)) {
      pending = body;
      $('#confirm-text').textContent = side === 'kup' ? `Wykonać zakup za ${body.usd} USD z portfela agenta?` : `Wykonać sprzedaż ${body.procent}% pozycji z portfela agenta?`;
      $('#confirm').hidden = false;
    }
  } catch (err) { $('#preview').textContent = err.message; }
  btn.disabled = false;
};
$('#do-trade').onclick = async () => {
  if (!pending) return;
  const body = { ...pending, confirm: true };
  // Formularz mógł się zmienić po podglądzie: wykonujemy tylko dokładnie to, co było w podglądzie.
  const now = tradeBody(false);
  if (now.mint !== pending.mint || now.usd !== pending.usd || now.procent !== pending.procent) { resetConfirm(); toast('Formularz zmienił się po podglądzie. Pokaż podgląd jeszcze raz.', true); return; }
  $('#do-trade').disabled = true;
  $('#preview').textContent = 'Wysyłam transakcję…';
  try {
    const r = await api('handel', body);
    $('#preview').textContent = r.msg;
    toast(r.executed ? 'Transakcja wykonana.' : 'Transakcja nie przeszła. Szczegóły w podglądzie.', !r.executed);
    loadOverview();
  } catch (err) { $('#preview').textContent = err.message; }
  $('#do-trade').disabled = false;
  resetConfirm();
};
$('#cancel-trade').onclick = resetConfirm;
$('#trade').oninput = resetConfirm;
document.addEventListener('click', (e) => {
  const sell = e.target.closest('[data-sell]'), buy = e.target.closest('[data-buy]');
  if (!sell && !buy) return;
  setSide(sell ? 'sprzedaj' : 'kup');
  $('#trade').mint.value = (sell || buy).dataset[sell ? 'sell' : 'buy'];
  show('handel');
  $('#trade').mint.focus();
});

// ------------------------------------------------------------------ odświeżanie
const loaders = { przeglad: [loadOverview, 60], czuwanie: [loadFlips, 20], snajper: [loadSniper, 5], wieloryby: [loadWhales, 120], handel: [loadOverview, 60] };
let timer;
async function refresh() {
  clearTimeout(timer);
  const [fn, every] = loaders[view];
  try { await Promise.all([fn(), loadServices()]); if (view === 'wieloryby') loadJobs(); }
  catch (e) { toast(e.message, true); }
  renderServices(); if (view === 'snajper') renderSniper();
  timer = setTimeout(() => { if (!document.hidden) refresh(); else timer = setTimeout(refresh, 2000); }, every * 1000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
const start = location.hash.slice(1);
show(loaders[start] ? start : 'przeglad');
