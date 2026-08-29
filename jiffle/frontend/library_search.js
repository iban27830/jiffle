export function tokenizeSearch(value) {
  return String(value ?? '').match(/(?:[^\s"]+|"[^"]*")+/g) || [];
}

export function parseLibrarySearch(value) {
  const terms = tokenizeSearch(value);
  const authors = [];
  const mediaIds = [];
  const includedTags = [];
  const excludedTags = [];
  for (const term of terms) {
    const authorMatch = term.match(/^author:(?:"([^"]*)"|(.*))$/i);
    const idMatch = term.match(/^id:(\d+)$/i);
    if (authorMatch) authors.push((authorMatch[1] ?? authorMatch[2]).trim());
    else if (idMatch) mediaIds.push(Number(idMatch[1]));
    else if (term.startsWith('-') && term.length > 1) excludedTags.push(term.slice(1));
    else includedTags.push(term);
  }
  return {terms, authors: authors.filter(Boolean), mediaIds, includedTags, excludedTags};
}

export function withAuthorFilter(value, author) {
  return withFilter(value, 'author', author);
}

function quote(value) {
  const clean = String(value ?? '').replaceAll('"', '').trim();
  return /\s/.test(clean) ? `"${clean}"` : clean;
}

export function withFilter(value, kind, filterValue) {
  const prefix = `${kind}:`;
  const retained = parseLibrarySearch(value).terms.filter(term => !term.toLowerCase().startsWith(prefix));
  if (filterValue == null || String(filterValue).trim() === '') return retained.join(' ');
  return [...retained, `${prefix}${quote(filterValue)}`].join(' ');
}

export function toggleSearchTerm(value, term) {
  const terms = parseLibrarySearch(value).terms;
  const opposite = term.startsWith('-') ? term.slice(1) : `-${term}`;
  const index = terms.indexOf(term);
  if (index >= 0) terms.splice(index, 1);
  else { const oppositeIndex = terms.indexOf(opposite); if (oppositeIndex >= 0) terms.splice(oppositeIndex, 1); terms.push(term); }
  return terms.join(' ');
}
