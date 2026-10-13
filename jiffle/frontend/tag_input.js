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

export function searchTokenSuggestionQuery(value, minChars = TAG_SUGGEST_MIN_CHARS) {
  const text = String(value ?? '');
  if (!text.trim() || /\s$/.test(text)) return {prefix: '', negated: false};
  const match = text.match(/(\S+)$/);
  if (!match) return {prefix: '', negated: false};
  const last = match[1];
  if (/^(?:author:|id:|parent:|["'])/i.test(last)) {
    return {prefix: '', negated: false};
  }
  const negated = last.startsWith('-');
  const rawToken = negated ? last.slice(1) : last;
  if (/^(?:author:|id:|parent:|["'])/i.test(rawToken)) {
    return {prefix: '', negated: false};
  }
  const prefix = suggestionQuery(rawToken, minChars);
  return {prefix, negated};
}

export function applySearchTokenSuggestion(value, chosenTag) {
  const tag = normalizeTagInput(chosenTag);
  if (!tag) return String(value ?? '');
  const text = String(value ?? '').replace(/\s+$/, '');
  const match = text.match(/(\S+)$/);
  if (!match) return `${tag} `;
  const negated = match[1].startsWith('-');
  return `${text.slice(0, match.index)}${negated ? '-' : ''}${tag} `;
}
