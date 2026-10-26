// Controller input for the launcher page.
// Turns gamepad state into events ('up', 'A', 'HOME', ...) with Xbox-style key repeat,
// and finds the nearest focusable element in a direction (spatial navigation).
globalThis.Pad ??= (() => {
  // Standard-mapping button numbers. If a controller reports a button wrong, fix it here.
  const BUTTONS = { A: 0, B: 1, X: 2, Y: 3, LB: 4, RB: 5, LT: 6, RT: 7, VIEW: 8, MENU: 9, UP: 12, DOWN: 13, LEFT: 14, RIGHT: 15, GUIDE: 16 };
  const DPAD = { UP: 'up', DOWN: 'down', LEFT: 'left', RIGHT: 'right' };
  const STICK = 0.5;   // how far the left stick must tilt to count as a direction
  const DELAY = 400;   // ms a direction is held before it starts repeating
  const RATE = 90;     // ms between repeats
  const KEYS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', Enter: 'A', Escape: 'B' };

  const handlers = [];
  let held = new Set();
  const next = { up: 0, down: 0, left: 0, right: 0 }; // when each held direction repeats next

  // Newest handler first. A handler consumes the event unless it returns false.
  function emit(ev, src) {
    for (let i = handlers.length - 1; i >= 0; i--) if (handlers[i](ev, src) !== false) return true;
    return false;
  }

  const pads = () => [...(navigator.getGamepads?.() ?? [])].filter(Boolean);

  function axes() {
    for (const p of pads()) if (p.axes.some((v) => Math.abs(v) > 0.15)) return p.axes;
    return [0, 0, 0, 0];
  }

  function frame(now) {
    const list = pads();
    if (list.length) {
      const pressed = new Set();
      let x = 0, y = 0;
      for (const p of list) {
        for (const [name, i] of Object.entries(BUTTONS)) if (p.buttons[i]?.pressed) pressed.add(name);
        if (Math.abs(p.axes[0]) > Math.abs(x)) x = p.axes[0];
        if (Math.abs(p.axes[1]) > Math.abs(y)) y = p.axes[1];
      }
      const stick = {
        up: y < -STICK && -y >= Math.abs(x), down: y > STICK && y >= Math.abs(x),
        left: x < -STICK && -x > Math.abs(y), right: x > STICK && x > Math.abs(y),
      };
      for (const dir in next) {
        const dpad = pressed.has(dir.toUpperCase());
        if (!dpad && !stick[dir]) next[dir] = 0;
        else if (!next[dir] || now >= next[dir]) {
          next[dir] = now + (next[dir] ? RATE : DELAY);
          emit(dir, dpad ? 'dpad' : 'stick');
        }
      }
      let home = false;
      for (const name of pressed) {
        if (held.has(name) || name in DPAD) continue;
        if (name === 'GUIDE' || (name === 'MENU' && pressed.has('VIEW')) || (name === 'VIEW' && pressed.has('MENU'))) home = true;
        else emit(name, 'pad');
      }
      if (home) emit('HOME', 'pad');
      held = pressed;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // Arrow keys, Enter and Escape act like the controller (handy with a keyboard or on a laptop).
  function useKeyboard() {
    addEventListener('keydown', (e) => {
      const ev = KEYS[e.key];
      if (!ev || e.altKey || e.ctrlKey || e.metaKey || e.defaultPrevented) return;
      const editing = e.composedPath()[0]?.matches?.(':read-write');
      if (editing && !globalThis.OSK?.isOpen() && ev !== 'B') return; // let text fields keep their keys
      if (emit(ev, 'key')) e.preventDefault();
    }, true);
  }

  const FOCUSABLE = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

  function candidates(scope = document) {
    return [...scope.querySelectorAll(FOCUSABLE)].filter((el) => el.checkVisibility() && !el.closest('[inert]'));
  }

  // Edge-based direction test; candidates lined up with the current element win, then the
  // smallest gap, then the smallest sideways offset.
  function nearest(dir, rect, list, skip) {
    const horiz = dir === 'left' || dir === 'right', fwd = dir === 'right' || dir === 'down';
    const mid = (a, b) => (a + b) / 2;
    let best = null, bestAligned = false, bestScore = Infinity;
    for (const el of list) {
      if (el === skip) continue;
      const r = el.getBoundingClientRect();
      const gap = horiz ? (fwd ? r.left - rect.right : rect.left - r.right) : (fwd ? r.top - rect.bottom : rect.top - r.bottom);
      if (gap < -1) continue;
      const aligned = horiz ? r.top < rect.bottom && r.bottom > rect.top : r.left < rect.right && r.right > rect.left;
      // Sideways distance: centres, or leading edges when those line up better (wide tile above a row of small buttons).
      const offset = horiz
        ? Math.min(Math.abs(mid(r.top, r.bottom) - mid(rect.top, rect.bottom)), Math.abs(r.top - rect.top))
        : Math.min(Math.abs(mid(r.left, r.right) - mid(rect.left, rect.right)), Math.abs(r.left - rect.left));
      const score = aligned ? gap + offset / 1e4 : gap + 2 * offset;
      if ((aligned && !bestAligned) || (aligned === bestAligned && score < bestScore)) {
        best = el; bestAligned = aligned; bestScore = score;
      }
    }
    return best;
  }

  // Move focus within scope. Returns false when nothing lies that way, so the page can scroll.
  function move(dir, scope = document) {
    const list = candidates(scope), cur = document.activeElement;
    const target = list.includes(cur) ? nearest(dir, cur.getBoundingClientRect(), list, cur) : list[0];
    if (!target) return false;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return true;
  }

  return {
    push: (fn) => (handlers.push(fn), fn),
    pop: (fn) => { const i = handlers.lastIndexOf(fn); if (i >= 0) handlers.splice(i, 1); },
    axes, candidates, nearest, move, useKeyboard,
  };
})();
