// Pure helpers for the Library inspector tag field.  They mirror the
// normalization applied by the server so a typed tag is stored consistently
// with tags imported from sources (lowercase, underscores instead of spaces).

export const TAG_SUGGEST_MIN_CHARS = 2;

export function normalizeTagInput(raw) {
  let value = String(raw ?? '');
  value = value.trim().replace(/^"+|"+$/g, '').trim();
  value = value.replace(/^'+|'+$/g, '').trim();
  value = value.replace(/\s+/g, '_').toLowerCase();
  return value.replace(/^_+|_+$/g, '');
}

export function suggestionQuery(raw, minChars = TAG_SUGGEST_MIN_CHARS) {
  const value = normalizeTagInput(raw);
  return value.length >= minChars ? value : '';
}

export function searchTokenSuggestionQuery(value, cursorIndex = undefined, minChars = TAG_SUGGEST_MIN_CHARS) {
  const text = String(value ?? '');
  if (!text.trim()) return {prefix: '', negated: false};
  let rawSlice = '';
  if (cursorIndex === undefined || cursorIndex === null || cursorIndex >= text.length) {
    if (/\s$/.test(text)) return {prefix: '', negated: false};
    const match = text.match(/(\S+)$/);
    if (!match) return {prefix: '', negated: false};
    rawSlice = match[1];
  } else {
    const pos = Math.max(0, Math.min(text.length, Number(cursorIndex)));
    let start = pos;
    while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
    let end = pos;
    while (end < text.length && !/\s/.test(text[end])) end += 1;
    rawSlice = pos > start ? text.slice(start, pos) : text.slice(start, end);
  }
  if (!rawSlice || /^["']/.test(rawSlice)) {
    return {prefix: '', negated: false};
  }
  const negated = rawSlice.startsWith('-');
  const rawToken = negated ? rawSlice.slice(1) : rawSlice;
  if (!rawToken || /^["']/.test(rawToken)) {
    return {prefix: '', negated: false};
  }
  const opMatch = rawToken.match(/^([a-z_]+):(.*)$/i);
  if (opMatch) {
    const op = opMatch[1].toLowerCase();
    if (op === 'author' || op === 'source' || op === 'domain' || op === 'shelf' || op === 'has') {
      const queryPart = opMatch[2].replace(/^["']+|["']+$/g, '').trim().toLowerCase();
      if ((op === 'shelf' || op === 'has') ? queryPart.length >= 0 : queryPart.length >= 1) {
        return {prefix: `${op}:${queryPart}`, negated};
      }
    }
    return {prefix: '', negated: false};
  }
  const prefix = suggestionQuery(rawToken, minChars);
  return {prefix, negated};
}

function formatSuggestedToken(selectedTag, negated) {
  const rawTag = String(selectedTag ?? '').trim();
  const alreadyNegated = rawTag.startsWith('-');
  const effectiveNegated = alreadyNegated || negated;
  const raw = rawTag.replace(/^-+/, '');
  const opMatch = raw.match(/^(author|source|domain|shelf|has|is):(.*)$/i);
  if (opMatch) {
    const op = opMatch[1].toLowerCase();
    const val = opMatch[2].trim().replace(/^"+|"+$/g, '').trim();
    if (!val) return '';
    const formattedVal = op === 'shelf'
      ? val.replace(/\s+/g, '_')
      : (op === 'author' && /\s/.test(val) ? `"${val}"` : val);
    return `${effectiveNegated ? '-' : ''}${op}:${formattedVal}`;
  }
  const tag = normalizeTagInput(raw);
  if (!tag) return '';
  return `${effectiveNegated ? '-' : ''}${tag}`;
}

export function applySearchTokenSuggestion(value, selectedTag, cursorIndex = undefined) {
  const text = String(value ?? '');
  if (cursorIndex !== undefined && cursorIndex !== null && cursorIndex < text.length) {
    const pos = Math.max(0, Math.min(text.length, Number(cursorIndex)));
    let start = pos;
    while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
    let end = pos;
    while (end < text.length && !/\s/.test(text[end])) end += 1;
    const tokenSlice = text.slice(start, end);
    const negated = tokenSlice.startsWith('-');
    const replacement = formatSuggestedToken(selectedTag, negated);
    if (!replacement) return text;
    const before = text.slice(0, start);
    const after = text.slice(end);
    if (after.trim().length > 0) {
      const separator = /^\s/.test(after) ? '' : ' ';
      return `${before}${replacement}${separator}${after}`;
    }
    return `${before}${replacement} `;
  }
  const trimmed = text.replace(/\s+$/, '');
  const match = trimmed.match(/(\S+)$/);
  const negated = match ? match[1].startsWith('-') : false;
  const replacement = formatSuggestedToken(selectedTag, negated);
  if (!replacement) return text;
  if (!match) return `${replacement} `;
  return `${trimmed.slice(0, match.index)}${replacement} `;
}
