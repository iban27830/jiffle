export function clampCropBox(box, width, height, minSize = 16) {
  const maxW = Math.max(0, Math.round(Number(width) || 0));
  const maxH = Math.max(0, Math.round(Number(height) || 0));
  if (maxW === 0 || maxH === 0) return [0, 0, 0, 0];
  const minW = Math.max(1, Math.min(maxW, Math.round(Number(minSize) || 16)));
  const minH = Math.max(1, Math.min(maxH, Math.round(Number(minSize) || 16)));
  const raw = Array.isArray(box) ? box : [0, 0, maxW, maxH];
  const left = Math.max(0, Math.min(maxW - minW, Math.round(Number(raw[0]) || 0)));
  const top = Math.max(0, Math.min(maxH - minH, Math.round(Number(raw[1]) || 0)));
  const right = Math.max(left + minW, Math.min(maxW, Math.round(Number(raw[2]) || 0)));
  const bottom = Math.max(top + minH, Math.min(maxH, Math.round(Number(raw[3]) || 0)));
  return [left, top, right, bottom];
}

export function dragCropBox(startBox, handle, dx, dy, width, height, minSize = 16) {
  const maxW = Math.max(0, Math.round(Number(width) || 0));
  const maxH = Math.max(0, Math.round(Number(height) || 0));
  if (maxW === 0 || maxH === 0) return [0, 0, 0, 0];
  const minW = Math.max(1, Math.min(maxW, Math.round(Number(minSize) || 16)));
  const minH = Math.max(1, Math.min(maxH, Math.round(Number(minSize) || 16)));
  const [l0, t0, r0, b0] = clampCropBox(startBox, maxW, maxH, minSize);
  const deltaX = Math.round(Number(dx) || 0);
  const deltaY = Math.round(Number(dy) || 0);
  const kind = String(handle || 'move').toLowerCase();

  if (kind === 'move') {
    const boxW = r0 - l0;
    const boxH = b0 - t0;
    const left = Math.max(0, Math.min(maxW - boxW, l0 + deltaX));
    const top = Math.max(0, Math.min(maxH - boxH, t0 + deltaY));
    return [left, top, left + boxW, top + boxH];
  }

  let left = l0;
  let top = t0;
  let right = r0;
  let bottom = b0;

  if (kind.includes('w')) {
    left = Math.max(0, Math.min(r0 - minW, l0 + deltaX));
  } else if (kind.includes('e')) {
    right = Math.max(l0 + minW, Math.min(maxW, r0 + deltaX));
  }

  if (kind.includes('n')) {
    top = Math.max(0, Math.min(b0 - minH, t0 + deltaY));
  } else if (kind.includes('s')) {
    bottom = Math.max(t0 + minH, Math.min(maxH, b0 + deltaY));
  }

  return clampCropBox([left, top, right, bottom], maxW, maxH, minSize);
}
