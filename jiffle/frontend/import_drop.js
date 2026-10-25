const HTML_URL_ATTR = /\b(?:src|href)\s*=\s*(?:"([^"]+)"|'([^']+)')/gi;
const HASH_IN_STEM = /(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{32})(?![0-9a-f])/i;
const GENERIC_CLIPBOARD_STEMS = new Set(['', 'image', 'img', 'paste', 'clipboard', 'blob', 'upload', 'file', 'download']);

function extractHttpUrls(dataTransfer) {
  const urls = [];
  for (const type of ['text/uri-list', 'text/plain']) {
    const value = dataTransfer?.getData?.(type)?.trim();
    if (!value) continue;
    for (const line of value.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      try {
        const parsed = new URL(trimmed);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') urls.push(parsed.href);
      } catch {}
    }
  }
  const html = dataTransfer?.getData?.('text/html')?.trim();
  if (html) {
    for (const match of html.matchAll(HTML_URL_ATTR)) {
      const raw = (match[1] || match[2] || '').replaceAll('&amp;', '&').trim();
      if (!raw) continue;
      try {
        const parsed = new URL(raw);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') urls.push(parsed.href);
      } catch {}
    }
  }
  return urls;
}

export function droppedUrl(dataTransfer) {
  for (const type of ['text/uri-list', 'text/plain']) {
    const value = dataTransfer?.getData?.(type)?.trim();
    if (!value) continue;
    const candidate = value.split(/\r?\n/).find(line => line && !line.startsWith('#'));
    try {
      const url = new URL(candidate);
      if (url.protocol === 'http:' || url.protocol === 'https:') return url.href;
    } catch {}
  }
  const urls = extractHttpUrls(dataTransfer);
  return urls[0] || null;
}

export function transferFilenameHint(dataTransfer, fallbackName = '') {
  const rawName = String(fallbackName || '').trim();
  const dot = rawName.lastIndexOf('.');
  const stem = dot > 0 ? rawName.slice(0, dot) : rawName;
  const ext = dot > 0 ? rawName.slice(dot) : '';
  if (HASH_IN_STEM.test(stem)) return rawName;
  if (!GENERIC_CLIPBOARD_STEMS.has(stem.toLowerCase())) return rawName;
  const urls = extractHttpUrls(dataTransfer);
  for (const href of urls) {
    try {
      const parsed = new URL(href);
      const seg = decodeURIComponent(parsed.pathname.split('/').filter(Boolean).pop() || '');
      const segDot = seg.lastIndexOf('.');
      const segStem = segDot > 0 ? seg.slice(0, segDot) : seg;
      const segExt = segDot > 0 ? seg.slice(segDot) : '';
      if (HASH_IN_STEM.test(segStem)) {
        return `${segStem}${ext || segExt || '.png'}`;
      }
    } catch {}
  }
  return rawName;
}

