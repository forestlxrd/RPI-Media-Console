// Launcher page: talks to server.py and drives everything with the controller.
const $ = (sel, root = document) => root.querySelector(sel);
const screens = { home: $('#home'), games: $('#games'), settings: $('#settings') };
let current = 'home';
let state = {};
let lastHomeFocus = null;
let openPanel = null;
let gamesShownAt = 0;
const seenMessages = new Set();

async function api(path, body) {
  const init = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Console': '1' },
    body: JSON.stringify(body),
  };
  const res = await fetch(`/api/${path}`, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `The launcher answered ${res.status}`), data);
  return data;
}

// Small DOM builder: h('button', { className: 'row', onclick }, child, ...). Text is never parsed as HTML.
function h(tag, props = {}, ...kids) {
  const el = Object.assign(document.createElement(tag), props);
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

function toast(text) {
  const t = $('#toast');
  t.textContent = text;
  t.hidePopover?.();
  t.showPopover();
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.hidePopover(), 5000);
}
const fail = (e) => toast(e.message);

// ---------------------------------------------------------------- screens and navigation

function show(name, focus) {
  for (const [key, el] of Object.entries(screens)) el.hidden = key !== name;
  current = name;
  (focus ?? $('[autofocus]', screens[name]) ?? Pad.candidates(screens[name])[0])?.focus();
  hints();
}

function goHome() {
  for (const d of document.querySelectorAll('dialog[open]')) d.close('');
  OSK.close();
  if (current === 'games') api('cancel', {}).catch(fail);
  show('home', lastHomeFocus);
}

function back() {
  const dialog = document.querySelector('dialog:modal');
  if (dialog) return dialog.close('');
  if (current === 'games') return api('cancel', {}).catch(fail);
  if (current === 'settings') {
    if (document.activeElement?.closest('.panel')) return $(`[data-panel="${openPanel}"]`).focus();
    return show('home', lastHomeFocus);
  }
}

function hints() {
  const el = document.activeElement;
  const spec = el?.closest?.('[data-hint]')?.dataset.hint
    ?? (document.querySelector('dialog:modal') ? 'A:Select|B:Close'
      : current === 'home' ? 'A:Open' : current === 'games' ? 'B:Cancel' : 'A:Select|B:Back');
  $('#hints').replaceChildren(...spec.split('|').map((pair) => {
    const [btn, what] = pair.split(':');
    return h('span', {}, h('kbd', { textContent: btn }), what);
  }));
}
document.addEventListener('focusin', hints);

Pad.useKeyboard();
Pad.push((ev) => {
  switch (ev) {
    case 'up': case 'down': case 'left': case 'right': {
      const el = document.activeElement;
      if ((ev === 'up' || ev === 'down') && el?.matches('[data-scroll]')) {  // scroll until the end, then move on
        const room = ev === 'down' ? el.scrollHeight - el.clientHeight - el.scrollTop : el.scrollTop;
        if (room > 1) return el.scrollBy({ top: (ev === 'down' ? 1 : -1) * el.clientHeight * 0.3 });
      }
      return Pad.move(ev, document.querySelector('dialog:modal') ?? document.body);
    }
    case 'A': {
      const el = document.activeElement;
      if (OSK.isText(el)) OSK.open(el);
      else el?.click();
      return;
    }
    case 'B': return back();
    case 'HOME': return goHome();
    default: return false;
  }
});

const actions = {
  games() {
    gamesShownAt = Date.now();
    show('games');
    api('launch', { app: 'games' }).catch((e) => {
      show('home', lastHomeFocus);
      if (e.needs_password) installSteamLink();
      else fail(e);
    });
  },
  movies() {
    toast(state.settings?.movies_app === 'kodi' ? 'Opening Kodi…' : 'Opening Jellyfin…');
    api('launch', { app: 'movies' }).catch(fail);
  },
  settings: () => openSettings('wifi'),
  wifi: () => openSettings('wifi'),
  controllers: () => openSettings('controllers'),
  updates: () => openSettings('updates'),
  async power() {
    const choice = await choose($('#dlg-power'));
    if (choice === 'poweroff' || choice === 'reboot') api('power', { action: choice }).catch(fail);
  },
};

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el || !actions[el.dataset.action]) return;
  if (current === 'home' && el.closest('#home')) lastHomeFocus = el;
  actions[el.dataset.action]();
});
$('#games-cancel').onclick = () => api('cancel', {}).catch(fail);

// ---------------------------------------------------------------- dialogs

function choose(dialog) {
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => {
    const value = dialog.returnValue;
    dialog.returnValue = '';
    resolve(value);
  }, { once: true }));
}

function confirmDialog(title, body, ok) {
  const d = $('#dlg-confirm');
  $('h2', d).textContent = title;
  $('.dlg-body', d).textContent = body;
  $('[value=ok]', d).textContent = ok;
  return choose(d).then((v) => v === 'ok');
}

async function askText(title, { body = '', ok = 'OK', type = 'text' } = {}) {
  const d = $('#dlg-text'), input = $('input', d);
  $('h2', d).textContent = title;
  $('.dlg-body', d).textContent = body;
  $('[value=ok]', d).textContent = ok;
  input.type = type;
  input.value = '';
  delete input.dataset.wasPassword;
  const result = choose(d);
  input.focus();
  OSK.open(input);
  const value = await result;
  OSK.close();
  return value === 'ok' ? input.value : null;
}

// ---------------------------------------------------------------- settings

const panels = { wifi: loadWifi, controllers: loadPads, streaming: renderStreaming, updates: renderUpdates, terminal: loadTerminal };

function openSettings(name) {
  show('settings', $(`[data-panel="${name}"]`));
  showPanel(name);
}

function showPanel(name) {
  if (openPanel === name && !screens.settings.hidden && !$(`#panel-${name}`).hidden) return;
  openPanel = name;
  for (const p of document.querySelectorAll('.panel')) p.hidden = p.id !== `panel-${name}`;
  for (const b of document.querySelectorAll('[data-panel]')) b.setAttribute('aria-current', b.dataset.panel === name);
  panels[name]();
}

// Focusing an item in the side list shows its panel, like a TV settings menu.
$('.side').addEventListener('focusin', (e) => {
  const b = e.target.closest('[data-panel]');
  if (b) showPanel(b.dataset.panel);
});

function bars(signal) {
  const on = signal > 75 ? 4 : signal > 50 ? 3 : signal > 25 ? 2 : 1;
  return h('span', { className: 'bars', ariaLabel: `Signal ${signal}%` },
    [1, 2, 3, 4].map((i) => h('i', { className: i <= on ? 'on' : '' })));
}

const LOCK = '<svg viewBox="0 0 24 24" aria-label="Needs a password"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';

let wifiLoading = false;
async function loadWifi() {
  if (wifiLoading) return;
  wifiLoading = true;
  const list = $('#wifi-list');
  const now = state.net?.wifi;
  $('#wifi-now').textContent = now ? `Connected to ${now.ssid}.` : 'Not connected to Wi-Fi.';
  list.replaceChildren(h('li', { className: 'empty', textContent: 'Looking for networks…' }));
  try {
    const { networks, saved } = await api('wifi');
    list.replaceChildren(...(networks.length ? networks.map((n) => h('li', {},
      h('button', { className: 'row', onclick: () => joinNetwork(n) },
        bars(n.signal),
        h('span', { className: 'row-name', textContent: n.ssid }),
        h('span', { className: 'row-meta', textContent: n.bands.join(', ') }),
        n.secure && h('span', { innerHTML: LOCK }),
        (n.active || n.saved) && h('span', { className: `row-state${n.active ? '' : ' off'}`, textContent: n.active ? 'Connected' : 'Saved' }),
      ))) : [h('li', { className: 'empty', textContent: 'No networks found. Scan again in a moment.' })]));
    $('#wifi-saved').replaceChildren(...(saved.length ? saved.map((s) => h('li', {},
      h('button', { className: 'row', onclick: () => forgetNetwork(s) },
        h('span', { className: 'row-name', textContent: s.ssid }),
        h('span', { className: 'row-meta', textContent: 'Forget' }),
      ))) : [h('li', { className: 'empty', textContent: 'None yet.' })]));
  } catch (e) {
    list.replaceChildren(h('li', { className: 'empty', textContent: e.message }));
  } finally {
    wifiLoading = false;
  }
}

async function joinNetwork(n, password = '', hidden = false) {
  if (n.active) return toast(`Already connected to ${n.ssid}`);
  if (n.secure && !n.saved && !password) {
    password = await askText(`Password for ${n.ssid}`, { ok: 'Connect', type: 'password' });
    if (password == null) return;
  }
  toast(`Connecting to ${n.ssid}…`);
  try {
    await api('wifi/connect', { ssid: n.ssid, password, hidden });
    toast(`Connected to ${n.ssid}`);
    loadWifi();
  } catch (e) {
    if (!e.needs_password) return fail(e);
    const again = await askText(`Password for ${n.ssid}`, { body: 'That password did not work. Try again.', ok: 'Connect', type: 'password' });
    if (again != null) joinNetwork({ ...n, saved: false }, again, hidden);
  }
}

async function forgetNetwork(s) {
  if (!await confirmDialog(`Forget ${s.ssid}?`, 'The Pi stops joining it on its own. You can add it again later.', 'Forget')) return;
  try {
    await api('wifi/forget', { uuid: s.uuid });
    toast(`Forgot ${s.ssid}`);
    loadWifi();
  } catch (e) { fail(e); }
}

$('#wifi-rescan').onclick = () => loadWifi();
$('#wifi-hidden').onclick = async () => {
  const ssid = await askText('Network name', { ok: 'Next' });
  if (!ssid) return;
  const password = await askText(`Password for ${ssid}`, { body: 'Leave it empty if the network is open.', ok: 'Connect', type: 'password' });
  if (password != null) joinNetwork({ ssid, secure: false, saved: false }, password, true);
};

async function loadPads() {
  const list = $('#pads-list');
  try {
    const { controllers } = await api('controllers');
    list.replaceChildren(...(controllers.length ? controllers.map((c) => h('li', {},
      h('button', { className: 'row', onclick: () => forgetPad(c) },
        h('span', { className: 'row-name', textContent: c.name }),
        c.battery != null && h('span', { className: 'row-meta', textContent: `${c.battery}%` }),
        h('span', { className: `row-state${c.connected ? '' : ' off'}`, textContent: c.connected ? 'Connected' : 'Off' }),
      ))) : [h('li', { className: 'empty', textContent: 'No controllers paired yet.' })]));
  } catch (e) {
    list.replaceChildren(h('li', { className: 'empty', textContent: e.message }));
  }
}

async function forgetPad(c) {
  if (!await confirmDialog(`Forget ${c.name}?`, 'To use it again, pair it from this screen.', 'Forget')) return;
  try {
    await api('controllers/forget', { mac: c.mac });
    toast(`Forgot ${c.name}`);
    loadPads();
  } catch (e) { fail(e); }
}

$('#pads-pair').onclick = () => api('controllers/pair', {}).then(poll).catch(fail);

const STREAM_OPTIONS = {
  movies_app: { label: 'Movies open in', values: () => ['jellyfin', 'kodi'], show: (v) => ({ jellyfin: 'Jellyfin', kodi: 'Kodi' })[v] ?? v },
  stream_app: { label: 'Stream games with', values: () => ['moonlight', 'steamlink'], show: (v) => ({ moonlight: 'Moonlight', steamlink: 'Steam Link' })[v] ?? v },
  // Steam Link keeps its own settings, in its own menu.
  moonlight_app: { label: 'Games start in', values: () => state.settings.moonlight_apps, show: (v) => v, moonlight: true },
  resolution: { label: 'Game picture', values: () => ['1280x720', '1920x1080'], show: (v) => ({ '1280x720': '720p', '1920x1080': '1080p' })[v] ?? v, moonlight: true },
  bitrate_kbps: { label: 'Game bitrate (lower it if games stutter)', values: () => [8000, 10000, 15000, 20000, 30000], show: (v) => `${v / 1000} Mbps`, moonlight: true },
};

function renderStreaming() {
  const s = state.settings ?? {};
  const focusedKey = document.activeElement?.dataset?.key;
  const shown = Object.entries(STREAM_OPTIONS).filter(([, o]) => !o.moonlight || s.stream_app !== 'steamlink');
  $('#stream-list').replaceChildren(...shown.map(([key, o]) => {
    const row = h('button', { className: 'row setting', onclick: () => cycle(key) },
      h('span', { className: 'row-name', textContent: o.label }),
      h('strong', { textContent: o.show(s[key]) }));
    row.dataset.key = key;
    return h('li', {}, row);
  }));
  if (focusedKey) $(`#stream-list [data-key="${focusedKey}"]`)?.focus();
}

async function cycle(key) {
  const values = STREAM_OPTIONS[key].values();
  const next = values[(values.indexOf(state.settings[key]) + 1) % values.length];
  try {
    await api('settings', { [key]: next });
    state.settings[key] = next;
    renderStreaming();
  } catch (e) { return fail(e); }
  if (key === 'stream_app' && next === 'steamlink') installSteamLink();
}

// Installs Steam Link if it's missing. apt runs through sudo, which may want the Pi password;
// it goes straight to sudo and isn't kept.
async function installSteamLink(password) {
  try {
    await api('steamlink/install', password == null ? {} : { password });
  } catch (e) {
    if (!e.needs_password) return fail(e);
    const again = await askText('Pi password', {
      body: password == null ? 'Installing Steam Link needs the password you log in to the Pi with.' : 'That password did not work. Try again.',
      ok: 'Install', type: 'password',
    });
    if (again != null) installSteamLink(again);
  }
}

function renderUpdates() {
  const u = state.update ?? {};
  $('#upd-version').textContent = state.version ?? '…';
  $('#upd-latest').textContent = u.busy ? 'Checking…'
    : u.error ? u.error
    : u.latest ? `${u.latest}${u.checked ? `, checked at ${clock(new Date(u.checked * 1000))}` : ''}`
    : 'Not checked yet';
  $('#upd-apply').hidden = !u.pending;
  $('#upd-apply').textContent = `Restart and update to ${u.pending}`;
  $('#upd-auto strong').textContent = state.settings?.auto_update ? 'On' : 'Off';
}

$('#upd-check').onclick = async () => {
  toast('Checking GitHub…');
  try {
    toast((await api('update', {})).text);
  } catch (e) { fail(e); }
  poll();
};
$('#upd-apply').onclick = () => api('restart', {}).catch(fail);
$('#upd-auto').onclick = async () => {
  try {
    await api('settings', { auto_update: !state.settings.auto_update });
    state.settings.auto_update = !state.settings.auto_update;
    renderUpdates();
  } catch (e) { fail(e); }
};

// Terminal: one command at a time, with the commands run before kept in this browser.
const HISTORY_KEY = 'terminal-history';

function termHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY)) ?? []; } catch { return []; }
}

function renderHistory() {
  const list = termHistory();
  $('#term-history').replaceChildren(...(list.length ? list.map((cmd) => h('li', {},
    h('button', { className: 'row', onclick: () => runCommand(cmd) },
      h('span', { className: 'row-name', textContent: cmd }))))
    : [h('li', { className: 'empty', textContent: 'Commands you run show up here.' })]));
  for (const b of document.querySelectorAll('#term-history .row')) b.dataset.hint = 'A:Run again|B:Back';
}

async function loadTerminal() {
  renderHistory();
  $('#term-quick').replaceChildren(...(state.settings?.quick_commands ?? []).map(([label, cmd]) =>
    h('button', { className: 'btn', textContent: label, title: cmd, onclick: () => runCommand(cmd, label) })));
  const where = $('#term-where');
  try {
    const { host, addresses } = await api('addresses');
    where.replaceChildren(h('dt', { textContent: 'Name' }), h('dd', { textContent: `${host}.local` }),
      ...addresses.flatMap((a) => [h('dt', { textContent: a.kind }), h('dd', { textContent: a.addr })]));
  } catch (e) {
    where.replaceChildren(h('dt', { textContent: 'Addresses' }), h('dd', { textContent: e.message }));
  }
}

let termBusy = false;
async function runCommand(cmd, label) {
  cmd = cmd.trim();
  if (!cmd || termBusy) return;
  termBusy = true;
  if (!label) $('#term-cmd').value = cmd;
  const out = $('#term-out');
  out.hidden = false;
  out.textContent = `$ ${cmd}\nRunning…`;
  if (!label) {  // the quick commands have their own buttons
    const list = [cmd, ...termHistory().filter((c) => c !== cmd)].slice(0, 20);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list)); } catch { /* history is a nicety */ }
    renderHistory();
  }
  try {
    const r = await api('shell', { cmd });
    const text = r.output && !r.output.endsWith('\n') ? `${r.output}\n` : r.output;
    out.textContent = `$ ${cmd}\n${r.cut ? '[only the end is shown]\n' : ''}${text}`
      + (r.timed_out ? '[stopped after 30 seconds]' : `[exit code ${r.code}]`);
  } catch (e) {
    out.textContent = `$ ${cmd}\n${e.message}`;
  } finally {
    termBusy = false;
  }
  out.scrollTop = 0;
  out.focus();
}

$('#term-form').onsubmit = (e) => {
  e.preventDefault();
  runCommand($('#term-cmd').value);
};

// ---------------------------------------------------------------- live status

const clock = (d) => d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

function render() {
  const net = state.net ?? {};
  $('#clock').textContent = clock(new Date());

  const homeChip = $('#chip-home');
  $('.dot', homeChip).className = `dot ${net.home ? 'ok' : 'bad'}`;
  $('span', homeChip).textContent = net.home ? 'Home connected' : net.vpn ? 'Home not answering' : 'VPN not connected';
  const wifiChip = $('#chip-wifi');
  $('.bars', wifiChip).replaceWith(net.wifi ? bars(net.wifi.signal) : h('span', { className: 'bars' }));
  $('span:last-child', wifiChip).textContent = net.wifi?.ssid ?? 'No Wi-Fi';
  $('#chip-pads span').textContent = state.pads === 1 ? '1 controller' : state.pads ? `${state.pads} controllers` : 'No controller';
  $('#chip-update').hidden = !state.update?.pending;
  $('#games-sub').textContent = `Play on ${state.settings?.pc_name ?? 'your PC'}`;

  const banner = $('#banner');
  const problem = net.connectivity === 'portal' ? "This Wi-Fi wants a sign-in page, which the Pi can't open. Pick another network."
    : net.connectivity === 'none' ? "You're not connected to Wi-Fi." : null;
  banner.hidden = !problem;
  if (problem) $('p', banner).textContent = problem;

  // Games: follow the server, which knows whether the PC is still waking up.
  const act = state.activity;
  if (act?.name === 'games' && current !== 'games') show('games');
  if (current === 'games') {
    if (!act) {
      if (Date.now() - gamesShownAt > 4000) show('home', lastHomeFocus); // ignore polls that left before the launch
    } else {
      const pc = state.settings?.pc_name ?? 'your PC';
      $('#games-title').textContent = act.phase === 'waking' ? `Waking ${pc}` : 'Starting the stream';
      $('#games-detail').textContent = act.phase === 'waking' ? 'This usually takes about half a minute.' : `${state.settings?.stream_app === 'steamlink' ? 'Steam Link' : 'Moonlight'} opens in a moment.`;
      const secs = Math.max(0, Math.round(Date.now() / 1000 - act.since));
      $('#games-elapsed').textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
    }
  }

  // Pairing runs on the server; show it whenever it's going (it also starts on its own at boot).
  const pair = $('#dlg-pair');
  if (state.pairing && !pair.open) {
    choose(pair).then(() => { if (state.pairing) api('controllers/cancel', {}).catch(fail); });
  } else if (!state.pairing && pair.open) {
    pair.close('done');
    if (openPanel === 'controllers' && current === 'settings') loadPads();
  }

  const msg = state.message;
  if (msg && !seenMessages.has(msg.id)) {
    seenMessages.add(msg.id);
    if (Date.now() / 1000 - msg.at < 60) toast(msg.text);
  }

  if (current === 'settings' && openPanel === 'updates') renderUpdates();
}

async function poll() {
  clearTimeout(poll.timer);
  try {
    state = await api('state');
    render();
  } catch { /* server restarting; try again shortly */ }
  poll.timer = setTimeout(poll, 1500);
}

show('home');
poll();
