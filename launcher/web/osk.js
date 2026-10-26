// Xbox-style on-screen keyboard. Move with the stick or D-pad, A types the highlighted key.
// Needs pad.js first.
globalThis.OSK ??= (() => {
  const ROWS = {
    abc: ['1234567890', 'qwertzuiop', 'asdfghjkl@', 'yxcvbnm,.-', 'áéíóöőúüű_'],
    sym: ['1234567890', '!?#$%&*()/', '+=<>[]{}~^', '\'":;|\\`€£§', '°«»¿¡…•÷×¤'],
  };
  const COLS = 20; // grid is in half-key steps so the bottom row can have wide keys
  const LEGEND = [['A', 'Type'], ['X', 'Delete'], ['Y', 'Space'], ['LT', 'Caps'], ['RT', 'Symbols'], ['☰', 'Done'], ['B', 'Close']];
  const TEXT = 'input:not([type]), input[type=text], input[type=search], input[type=password], input[type=email], input[type=url], input[type=tel], input[type=number], textarea';

  const CSS = `
    .kb { width: 70vh; padding: 1.6vh; border-radius: 2.4vh; background: var(--osk-bg, #1a3a2e);
      font: 600 2.6vh/1 var(--osk-font, system-ui, sans-serif); color: var(--osk-text, #ece7da);
      box-shadow: 0 2vh 6vh rgb(0 0 0 / .55); }
    .keys { display: grid; grid-template-columns: repeat(${COLS}, 1fr); gap: .8vh; }
    .key { height: 5.6vh; display: grid; place-items: center; border-radius: 1vh; background: var(--osk-key, #275142); }
    .key.wide { font-size: 2.1vh; font-weight: 500; }
    .key.on { background: var(--osk-focus, #f3b63f); color: var(--osk-on-focus, #10241d); }
    .key.lit:not(.on) { box-shadow: inset 0 0 0 .3vh var(--osk-focus, #f3b63f); }
    .legend { display: flex; flex-wrap: wrap; gap: .6vh 1.6vh; margin-top: 1.4vh; font-size: 1.65vh; font-weight: 450;
      color: var(--osk-muted, #a3b8a8); }
    kbd { font: inherit; font-weight: 700; color: var(--osk-text, #ece7da); margin-right: .5vh; }`;

  let host, keysEl, legendEl, target = null, layer = 'abc', caps = false, row = 1, x = 1, handler = null;

  function rows() {
    const chars = ROWS[layer].map((r) => Array.from(r, (ch) => ({ label: caps ? ch.toLocaleUpperCase('hu') : ch, span: 2, act: 'char' })));
    return [...chars, [
      { label: 'Caps', span: 3, act: 'caps', lit: caps },
      { label: layer === 'abc' ? '&123' : 'abc', span: 3, act: 'sym' },
      { label: 'Space', span: 6, act: 'space' },
      { label: 'Delete', span: 4, act: 'del' },
      { label: 'Done', span: 4, act: 'enter' },
    ]];
  }

  // Each key covers [start, start + span) half-columns of its row.
  function layout() {
    return rows().map((keys) => { let s = 0; return keys.map((k) => ({ ...k, start: (s += k.span) - k.span })); });
  }
  const keyAt = (r, col) => r.find((k) => col >= k.start && col < k.start + k.span) ?? r.at(-1);

  function render() {
    const grid = layout();
    keysEl.replaceChildren(...grid.flatMap((r, ri) => r.map((k) => {
      const el = document.createElement('div');
      el.className = `key${k.span > 2 ? ' wide' : ''}${k.lit ? ' lit' : ''}${ri === row && k === keyAt(r, x) ? ' on' : ''}`;
      el.style.gridColumn = `span ${k.span}`;
      el.textContent = k.label;
      return el;
    })));
    legend();
  }

  function build() {
    host = document.createElement('console-keyboard');
    const root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = `<style>${CSS}</style><div class="kb" aria-hidden="true"><div class="keys"></div><div class="legend"></div></div>`;
    host.style.cssText = 'all: initial; position: fixed; left: 50%; bottom: 3vh; translate: -50% 0; z-index: 2147483647;';
    keysEl = root.querySelector('.keys');
    legendEl = root.querySelector('.legend');
  }

  function legend() {
    const secret = target.type === 'password' || target.dataset.wasPassword;
    const items = secret ? [...LEGEND.slice(0, -1), ['View', target.type === 'password' ? 'Show' : 'Hide'], LEGEND.at(-1)] : LEGEND;
    legendEl.replaceChildren(...items.map(([btn, what]) => {
      const kbd = document.createElement('kbd');
      kbd.textContent = btn;
      const span = document.createElement('span');
      span.append(kbd, what);
      return span;
    }));
  }

  function type(text) {
    target.focus();
    // insertText goes through the browser's own editing, so frameworks like React see the change.
    if (!document.execCommand('insertText', false, text)) setValue((target.value ?? '') + text);
  }
  function del() {
    target.focus();
    if (!document.execCommand('delete')) setValue((target.value ?? '').slice(0, -1));
  }
  function setValue(v) {
    const proto = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, v);
    target.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function caret(step) {
    try {
      const p = Math.max(0, Math.min(target.value.length, target.selectionStart + step));
      target.setSelectionRange(p, p);
    } catch { /* email and number fields have no caret API */ }
  }

  function enter() {
    const el = target;
    close();
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
    const proceed = el.dispatchEvent(new KeyboardEvent('keydown', opts));
    el.dispatchEvent(new KeyboardEvent('keyup', opts));
    if (proceed && el.form) {
      const submit = el.form.querySelector('button:not([type=button]):not([type=reset]), input[type=submit]');
      el.form.requestSubmit(submit ?? undefined);
    }
  }

  function press(k) {
    if (k.act === 'char') type(k.label);
    else if (k.act === 'space') type(' ');
    else if (k.act === 'del') del();
    else if (k.act === 'caps') caps = !caps;
    else if (k.act === 'sym') layer = layer === 'abc' ? 'sym' : 'abc';
    else if (k.act === 'enter') return enter();
    render();
  }

  function onPad(ev) {
    const grid = layout();
    const cur = keyAt(grid[row], x);
    switch (ev) {
      case 'left': case 'right': {
        const r = grid[row], i = r.indexOf(cur);
        const k = r[(i + (ev === 'right' ? 1 : -1) + r.length) % r.length];
        x = k.start + k.span / 2 - 0.5;
        break;
      }
      case 'up': row = Math.max(0, row - 1); break;
      case 'down': row = Math.min(grid.length - 1, row + 1); break;
      case 'A': return press(cur);
      case 'X': del(); break;
      case 'Y': type(' '); break;
      case 'LT': caps = !caps; break;
      case 'RT': layer = layer === 'abc' ? 'sym' : 'abc'; break;
      case 'LB': caret(-1); break;
      case 'RB': caret(1); break;
      case 'VIEW': if (target.type === 'password' || target.dataset.wasPassword) {
        target.dataset.wasPassword = '1';
        target.type = target.type === 'password' ? 'text' : 'password';
      } break;
      case 'MENU': return enter();
      case 'B': return close();
      case 'HOME': close(); return false; // let the page handle going home
      default: return;
    }
    render();
  }

  function onBlur() {
    setTimeout(() => { if (target && document.activeElement !== target) close(); });
  }

  function open(el) {
    if (target === el) return;
    if (target) close();
    if (!host) build();
    target = el;
    caps = false;
    layer = el.type === 'number' || el.type === 'tel' ? 'sym' : 'abc';
    (el.closest('dialog') ?? document.documentElement).append(host);
    el.addEventListener('blur', onBlur);
    handler = Pad.push(onPad);
    render();
  }

  function close() {
    if (!target) return;
    target.removeEventListener('blur', onBlur);
    target = null;
    Pad.pop(handler);
    host.remove();
  }

  return {
    open, close,
    isOpen: () => !!target,
    isText: (el) => !!el?.matches?.(TEXT) && !el.readOnly && !el.disabled,
  };
})();
