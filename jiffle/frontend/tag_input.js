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
