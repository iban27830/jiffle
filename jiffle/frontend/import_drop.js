export function droppedUrl(dataTransfer) {
  for (const type of ['text/uri-list', 'text/plain']) {
    const value = dataTransfer?.getData(type)?.trim();
    if (!value) continue;
    const candidate = value.split(/\r?\n/).find(line => line && !line.startsWith('#'));
    try {
      const url = new URL(candidate);
      if (url.protocol === 'http:' || url.protocol === 'https:') return url.href;
    } catch {}
  }
  return null;
}
