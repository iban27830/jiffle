export function tokenizeSearch(value) {
  return String(value ?? '').match(/(?:[^\s"]+|"[^"]*")+/g) || [];
}

function unquote(value) {
  const str = String(value ?? '').trim();
  if ((str.startsWith('"') && str.endsWith('"')) || (str.startsWith("'") && str.endsWith("'"))) {
    return str.slice(1, -1).trim();
  }
  return str.replace(/^"+|"+$/g, '').trim();
}

function normalizeTagToken(raw) {
  return unquote(raw).replace(/\s+/g, '_');
}

export const ALLOWED_HAS_FLAGS = [
  'parent',
  'children',
  'family',
  'source',
  'edited',
  'animated',
  'motion',
  'moving',
  'shelf',
  'shelves',
];

function normalizeShelfToken(raw) {
  return unquote(raw).replace(/\s+/g, '_');
}

export function parseLibrarySearch(value) {
  const terms = tokenizeSearch(value);
  const authors = [];
  const excludedAuthors = [];
  const domains = [];
  const excludedDomains = [];
  const sources = [];
  const excludedSources = [];
  const shelves = [];
  const excludedShelves = [];
  const mediaIds = [];
  const excludedMediaIds = [];
  const parentIds = [];
  const types = [];
  const hasFlags = [];
  const excludedFlags = [];
  const orientations = [];
  const textTerms = [];
  const includedTags = [];
  const excludedTags = [];
  let minWidth = null;
  let maxWidth = null;
  let minHeight = null;
  let maxHeight = null;

  for (const term of terms) {
    const authorMatch = term.match(/^(-?)author:(?:"([^"]*)"|(.*))$/i);
    const domainMatch = term.match(/^(-?)domain:(?:"([^"]*)"|(.*))$/i);
    const sourceMatch = term.match(/^(-?)source:(?:"([^"]*)"|(.*))$/i);
    const shelfMatch = term.match(/^(-?)shelf:(?:"([^"]*)"|(.*))$/i);
    const idMatch = term.match(/^(-?)id:(\d+)$/i);
    const parentMatch = term.match(/^parent:(\d+)$/i);
    const typeMatch = term.match(/^type:(image|video|animated|gif|motion|moving)$/i);
    const flagMatch = term.match(/^(-?)(?:has:(parent|children|family|source|edited|animated|motion|moving|shelf|shelves)|is:(edited|animated|motion|moving))$/i);
    const orientationMatch = term.match(/^(?:orientation|ratio):(portrait|landscape|square)$/i);
    const dimMatch = term.match(/^(width|height):(>=|>|<=|<|=)?(\d+)$/i);
    const textMatch = term.match(/^text:(?:"([^"]*)"|(.*))$/i);

    if (authorMatch) {
      const val = (authorMatch[2] ?? authorMatch[3] ?? '').trim();
      if (val) {
        if (authorMatch[1] === '-') excludedAuthors.push(val);
        else authors.push(val);
      }
    } else if (domainMatch) {
      const val = (domainMatch[2] ?? domainMatch[3] ?? '').trim();
      if (val) {
        if (domainMatch[1] === '-') excludedDomains.push(val);
        else domains.push(val);
      }
    } else if (sourceMatch) {
      const val = (sourceMatch[2] ?? sourceMatch[3] ?? '').trim();
      if (val) {
        if (sourceMatch[1] === '-') excludedSources.push(val);
        else sources.push(val);
      }
    } else if (shelfMatch) {
      const val = normalizeShelfToken(shelfMatch[2] ?? shelfMatch[3] ?? '');
      if (val) {
        if (shelfMatch[1] === '-') excludedShelves.push(val);
        else shelves.push(val);
      }
    } else if (idMatch) {
      const id = Number(idMatch[2]);
      if (idMatch[1] === '-') excludedMediaIds.push(id);
      else mediaIds.push(id);
    } else if (parentMatch) {
      parentIds.push(parentMatch[1]);
    } else if (typeMatch) {
      const t = typeMatch[1].toLowerCase();
      types.push(t === 'moving' ? 'motion' : t);
    } else if (flagMatch) {
      const rawFlag = (flagMatch[2] || flagMatch[3]).toLowerCase();
      const flag = rawFlag === 'shelves' ? 'shelf' : (rawFlag === 'moving' ? 'motion' : rawFlag);
      if (flagMatch[1] === '-') excludedFlags.push(flag);
      else hasFlags.push(flag);
    } else if (orientationMatch) {
      orientations.push(orientationMatch[1].toLowerCase());
    } else if (dimMatch) {
      const dim = dimMatch[1].toLowerCase();
      const op = dimMatch[2] || '=';
      const num = Number(dimMatch[3]);
      if (dim === 'width') {
        if (op === '>=' || op === '>') minWidth = num;
        else if (op === '<=' || op === '<') maxWidth = num;
        else { minWidth = num; maxWidth = num; }
      } else {
        if (op === '>=' || op === '>') minHeight = num;
        else if (op === '<=' || op === '<') maxHeight = num;
        else { minHeight = num; maxHeight = num; }
      }
    } else if (textMatch) {
      const val = (textMatch[1] ?? textMatch[2] ?? '').trim();
      if (val) textTerms.push(val);
    } else if (term.startsWith('-') && term.length > 1) {
      const tag = normalizeTagToken(term.slice(1));
      if (tag) excludedTags.push(tag);
    } else {
      const tag = normalizeTagToken(term);
      if (tag) includedTags.push(tag);
    }
  }

  return {
    terms,
    authors: authors.filter(Boolean),
    excludedAuthors: excludedAuthors.filter(Boolean),
    domains: domains.filter(Boolean),
    excludedDomains: excludedDomains.filter(Boolean),
    sources: sources.filter(Boolean),
    excludedSources: excludedSources.filter(Boolean),
    shelves: shelves.filter(Boolean),
    excludedShelves: excludedShelves.filter(Boolean),
    mediaIds,
    excludedMediaIds,
    parentIds,
    types,
    hasFlags,
    excludedFlags,
    orientations,
    minWidth,
    maxWidth,
    minHeight,
    maxHeight,
    textTerms,
    includedTags,
    excludedTags,
  };
}

export function withAuthorFilter(value, author) {
  return withFilter(value, 'author', author);
}

export function withShelfFilter(value, shelf) {
  return withFilter(value, 'shelf', shelf ? String(shelf).trim().replace(/\s+/g, '_') : '');
}

function quote(value) {
  const clean = String(value ?? '').replaceAll('"', '').trim();
  return /\s/.test(clean) ? `"${clean}"` : clean;
}

export function withFilter(value, kind, filterValue) {
  const prefix = `${kind}:`;
  const negPrefix = `-${kind}:`;
  const retained = parseLibrarySearch(value).terms.filter(term => {
    const lower = term.toLowerCase();
    return !lower.startsWith(prefix.toLowerCase()) && !lower.startsWith(negPrefix.toLowerCase());
  });
  if (filterValue == null || String(filterValue).trim() === '') return retained.join(' ');
  return [...retained, `${prefix}${quote(filterValue)}`].join(' ');
}

function canonicalTokenKey(rawToken) {
  const str = String(rawToken ?? '').trim();
  const negated = str.startsWith('-') && str.length > 1;
  const body = negated ? str.slice(1) : str;
  const opMatch = body.match(/^(author|domain|source|shelf|has|is):(?:"([^"]*)"|(.*))$/i);
  if (opMatch) {
    const op = opMatch[1].toLowerCase();
    const rawVal = unquote(opMatch[2] ?? opMatch[3] ?? '').toLowerCase();
    const val = op === 'shelf' ? rawVal.replace(/\s+/g, '_') : (op === 'has' && rawVal === 'shelves' ? 'shelf' : rawVal);
    return {negated, key: `${op}:${val}`};
  }
  return {negated, key: normalizeTagToken(body).toLowerCase()};
}

function formatTokenForSearch(rawToken) {
  const str = String(rawToken ?? '').trim();
  const negated = str.startsWith('-') && str.length > 1;
  const body = negated ? str.slice(1) : str;
  const opMatch = body.match(/^(author|domain|source|shelf):(?:"([^"]*)"|(.*))$/i);
  if (opMatch) {
    const op = opMatch[1].toLowerCase();
    const val = unquote(opMatch[2] ?? opMatch[3] ?? '');
    if (op === 'shelf') {
      return `${negated ? '-' : ''}shelf:${val.replace(/\s+/g, '_')}`;
    }
    return `${negated ? '-' : ''}${op}:${quote(val)}`;
  }
  return str;
}

export function toggleSearchToken(searchValue, token) {
  const terms = parseLibrarySearch(searchValue).terms;
  const target = canonicalTokenKey(token);
  const index = terms.findIndex(existing => {
    const parsed = canonicalTokenKey(existing);
    return parsed.negated === target.negated && parsed.key === target.key;
  });
  if (index >= 0) {
    terms.splice(index, 1);
  } else {
    const oppositeIndex = terms.findIndex(existing => {
      const parsed = canonicalTokenKey(existing);
      return parsed.negated === !target.negated && parsed.key === target.key;
    });
    if (oppositeIndex >= 0) terms.splice(oppositeIndex, 1);
    terms.push(formatTokenForSearch(token));
  }
  return terms.join(' ');
}

export const toggleSearchTerm = toggleSearchToken;
