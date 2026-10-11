// Pure state/route helpers, shared by the UI and behavioral regression tests.
export const viewNames = ['library', 'import', 'review', 'duplicates', 'editor', 'collections', 'settings'];

export function parseRoute(value) {
  const [rawView, query = ''] = String(value || '').replace(/^#/, '').split('?', 2);
  const view = viewNames.includes(rawView) ? rawView : 'library';
  const params = new URLSearchParams(query);
  const positiveId = key => /^\d+$/.test(params.get(key) || '') && Number.isSafeInteger(Number(params.get(key))) && Number(params.get(key)) > 0 ? Number(params.get(key)) : null;
  return {view, params, id: positiveId('id'), mediaId: positiveId('media')};
}

export function libraryHref(mediaId) {
  const id = Number(mediaId);
  return Number.isSafeInteger(id) && id > 0 ? `#library?id=${id}` : '#library';
}

export function editorDraftKey(mediaId, revisionId) {
  return `${Number(mediaId)}:${Number(revisionId)}`;
}

export const imageDraftFields = [
  'tool', 'analysisId', 'box', 'boxRevisionId', 'zoom', 'cropScrollLeft', 'cropScrollTop',
  'backgroundOpen', 'backgroundAssetId', 'backgroundBlur', 'backgroundPreviewMode',
  'backgroundZoom', 'backgroundScrollLeft', 'backgroundScrollTop',
];

export function pickImageDraft(state) {
  return Object.fromEntries(imageDraftFields.filter(key => state[key] !== undefined).map(key => [key, state[key]]));
}

export function restoreImageDraft(state, mediaId, revisionId) {
  const key = editorDraftKey(mediaId, revisionId);
  let draft = state.imageDrafts?.[key];
  // Read the original session format only when its identity actually matches.
  if (!draft && !state.imageDrafts && Number(state.targetMediaId || state.backgroundMediaId) === Number(mediaId)) {
    draft = pickImageDraft(state);
    if (Number(draft.boxRevisionId) !== Number(revisionId)) {
      delete draft.box;
      delete draft.analysisId;
    }
  }
  const clean = Object.fromEntries(imageDraftFields.map(field => [field, null]));
  return {...clean, zoom: 100, backgroundZoom: 100, backgroundBlur: 12, backgroundPreviewMode: 'cutout',
    tool: draft?.tool || (draft?.backgroundOpen ? 'background' : 'crop'),
    ...draft, editingMediaId: Number(mediaId), editingRevisionId: Number(revisionId), backgroundMediaId: Number(mediaId)};
}

export function restoreTrimDraft(drafts, mediaId, revisionId) {
  const current = drafts?.[editorDraftKey(mediaId, revisionId)];
  if (current) return current;
  const legacy = drafts?.[String(mediaId)];
  return legacy && (!legacy.revisionId || Number(legacy.revisionId) === Number(revisionId)) ? legacy : null;
}

export function selectionRulesMatch(draft) {
  return Boolean(draft?.generatedRules && draft.generatedRules.query === String(draft.query || '').trim()
    && Number(draft.generatedRules.requested_count) === Number(draft.requested_count));
}
