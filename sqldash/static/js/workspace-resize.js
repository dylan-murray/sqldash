export function bindResize(handle, { axis, initial, min, max, update }) {
  const apply = value => {
    const size = Math.max(min, Math.min(max(), value));
    update(size);
    handle.setAttribute('aria-valuenow', String(Math.round(size)));
    handle.setAttribute('aria-valuemin', String(min));
    handle.setAttribute('aria-valuemax', String(Math.round(max())));
  };
  handle.addEventListener('keydown', event => {
    const delta = ({ ArrowLeft:-20, ArrowUp:-20, ArrowRight:20, ArrowDown:20 })[event.key];
    if (!delta) return;
    event.preventDefault(); apply(initial() + delta);
  });
  handle.addEventListener('pointerdown', event => {
    const start = axis === 'x' ? event.clientX : event.clientY;
    const size = initial();
    handle.setPointerCapture(event.pointerId);
    const move = next => apply(size + (axis === 'x' ? next.clientX : next.clientY) - start);
    const done = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', done); handle.removeEventListener('pointercancel', done); };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', done);
    handle.addEventListener('pointercancel', done);
  });
}
