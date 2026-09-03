import {api, waitForJob} from './api.js';

import {parseLibrarySearch, withAuthorFilter, toggleSearchTerm} from './library_search.js';
import {droppedUrl} from './import_drop.js';
import {drawCompositionPreview, drawForegroundPreview} from './background_preview.js';

const workspace = document.querySelector('#workspace');
const title = document.querySelector('#viewTitle');
const meta = document.querySelector('#viewMeta');
const actions = document.querySelector('#headerActions');
const statusText = document.querySelector('#statusText');
const jobStatus = document.querySelector('#jobStatus');
let currentView = 'library';
let libraryOffset = 0;
let selectedMedia = null;
let includeTag = ''; let excludeTag = '';
let librarySearch = '';
let reloadLibrary = null;
let monitoredCropJob = null;
let monitoredBackgroundJob = null;
let monitoredSetJob = null;
let reloadBackgroundCandidates = null;

const UI_STATE_KEY = 'jiffle-session-state-v1';
let uiState = (() => { try { return JSON.parse(sessionStorage.getItem(UI_STATE_KEY) || '{}'); } catch { return {}; } })();
function viewState(view) { return uiState[view] || {}; }
function saveViewState(view, changes) {
  uiState = {...uiState, [view]: {...viewState(view), ...changes}};
  try { sessionStorage.setItem(UI_STATE_KEY, JSON.stringify(uiState)); } catch {}
}
function saveScrollState() {
  if (!currentView) return;
  const changes = {scrollTop:workspace.scrollTop};
  if (currentView === 'library') changes.galleryScrollTop = document.querySelector('#gallery')?.scrollTop || 0;
  saveViewState(currentView, changes);
}
function restoreScrollState(view) {
  requestAnimationFrame(() => {
    workspace.scrollTop = Number(viewState(view).scrollTop || 0);
    if (view === 'library') {
      const gallery = document.querySelector('#gallery');
      if (gallery) gallery.scrollTop = Number(viewState(view).galleryScrollTop || 0);
    }
  });
}

const FONT_SIZE_KEY = 'jiffle-font-size';
const allowedFontSizes = [14, 16, 18, 20];
const LIBRARY_PREFS_KEY = 'jiffle-library-preferences';
const defaultLibraryPrefs = {pageSize: 40, cardSize: 'medium', showCardInfo: true, inspectorWidth: 320};
function libraryPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(LIBRARY_PREFS_KEY) || '{}');
    return {...defaultLibraryPrefs, ...saved};
  } catch { return {...defaultLibraryPrefs}; }
}
function saveLibraryPrefs(changes) {
  const next = {...libraryPrefs(), ...changes};
  try { localStorage.setItem(LIBRARY_PREFS_KEY, JSON.stringify(next)); } catch {}
  return next;
}
function applyLibraryPrefs() {
  const prefs = libraryPrefs();
  const cardWidths = {small: 130, medium: 175, large: 230};
  document.documentElement.style.setProperty('--gallery-card-min', `${cardWidths[prefs.cardSize] || cardWidths.medium}px`);
  document.documentElement.style.setProperty('--inspector-width', `${prefs.inspectorWidth}px`);
  document.documentElement.classList.toggle('hide-card-info', !prefs.showCardInfo);
  return prefs;
}
function applyFontSize(value) {
  const size = allowedFontSizes.includes(Number(value)) ? Number(value) : 16;
  document.documentElement.style.setProperty('--base-font-size', `${size}px`);
  try { localStorage.setItem(FONT_SIZE_KEY, String(size)); } catch {}
  return size;
}
function savedFontSize() {
  try { return Number(localStorage.getItem(FONT_SIZE_KEY)) || 16; } catch { return 16; }
}
applyFontSize(savedFontSize());
applyLibraryPrefs();

const icons = () => window.lucide?.createIcons();
const button = (label, icon, extra = '') => `<button class="btn ${extra}"><i data-lucide="${icon}"></i>${label}</button>`;
const esc = value => String(value ?? '').replace(/[&<>"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[character]));
const operationName = operation => ({crop:'Crop',replace:'Replace',original:'Original',background_replace:'Background replacement'})[operation] || String(operation || 'Edit').replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
function editSummary(operations=[]) {
  const counts=new Map(); operations.forEach(operation=>counts.set(operation,(counts.get(operation)||0)+1));
  return [...counts].map(([operation,count])=>`${operationName(operation)}${count>1?` ×${count}`:''}`).join(' + ');
}

function toast(message, error = false) {
  const node = document.querySelector('#toast');
  const savebar = document.querySelector('.builder-savebar');
  node.style.bottom = savebar ? `${window.innerHeight-savebar.getBoundingClientRect().top+10}px` : '';
  node.textContent = message;
  node.className = `toast visible${error ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.className = 'toast', 2600);
}

function setHeader(name, detail = '', controls = '') {
  title.textContent = name;
  meta.textContent = detail;
  actions.innerHTML = controls;
  icons();
}

function activeFiltersHtml() {
  return parseLibrarySearch(librarySearch).terms.map(term => {
    const excluded = term.startsWith('-');
    const value = excluded ? term.slice(1) : term;
    return `<span class="filter-chip${excluded ? ' excluded' : ''}">${excluded ? 'Not: ' : ''}${esc(value)}<button type="button" data-remove-term="${esc(term)}" title="Remove filter"><i data-lucide="x"></i></button></span>`;
  }).join('');
}

function tagHtml(tag) {
  const terms = parseLibrarySearch(librarySearch).terms;
  const included = terms.includes(tag);
  const excluded = terms.includes(`-${tag}`);
  const state = included ? ' included' : excluded ? ' excluded' : '';
  return `<span class="tag tag-filter${state}" data-include-tag="${esc(tag)}" role="button" tabindex="0" title="Add to search">${esc(tag)}<button type="button" class="tag-exclude" data-exclude-tag="${esc(tag)}" title="Exclude from search"><i data-lucide="circle-minus"></i></button></span>`;
}

function authorHtml(author) {
  if (!author) return '<span class="muted-value">Unknown author</span>';
  const selectedAuthors = parseLibrarySearch(librarySearch).authors;
  return author.split(',').map(value => value.trim()).filter(Boolean).map(value => {
    const selected = selectedAuthors.includes(value) ? ' included' : '';
    return `<button type="button" class="tag tag-filter author-filter${selected}" data-include-author="${esc(value)}" title="Search by author">${esc(value)}</button>`;
  }).join('');
}

function bindAuthorFilters(container = document) {
  container.querySelectorAll('[data-include-author]').forEach(node => {
    node.onclick = () => applyAuthorFilter(node.dataset.includeAuthor);
  });
}

function applyAuthorFilter(author) {
  const input = document.querySelector('#search');
  if (!input) return;
  librarySearch = withAuthorFilter(input.value.trim(), author);
  input.value = librarySearch;
  libraryOffset = 0;
  if (reloadLibrary) reloadLibrary(); else showLibrary();
}

function appendSearchTerm(term) {
  const input = document.querySelector('#search');
  if (!input) return;
  librarySearch = toggleSearchTerm(input.value.trim(), term);
  input.value = librarySearch;
  libraryOffset = 0;
  if (reloadLibrary) reloadLibrary(); else showLibrary();
}

function bindLibraryFilters(load) {
  document.querySelectorAll('[data-remove-term]').forEach(node => node.onclick = () => appendSearchTerm(node.dataset.removeTerm));
}

async function runJob(start, onCreated) {
  const created = await start();
  onCreated?.(created);
  const job = await waitForJob(created.status_url, value => {
    if (value.type === 'source_set_import') renderSetStatus(value);
    else jobStatus.textContent = `${value.type}: ${value.progress}%`;
  });
  jobStatus.textContent = '';
  return job;
}

function renderSetStatus(current) {
  const processed = current.processed ?? current.result?.processed ?? 0;
  const total = current.total ?? current.result?.total ?? '?';
  const name = current.result?.set?.name || current.set?.name || 'e621 set';
  const stopping = Boolean(current.cancel_requested);
  jobStatus.innerHTML = `<span>${esc(name)}: ${current.progress}% · ${processed}/${total}${stopping ? ' · stopping after current post' : ''}</span><button id="stopSetImport" class="status-stop" title="Stop set import" ${stopping ? 'disabled' : ''}><i data-lucide="square"></i></button>`;
  const stop = document.querySelector('#stopSetImport');
  if (stop && !stopping) stop.onclick = async () => { stop.disabled = true; jobStatus.querySelector('span').textContent += ' · stopping after current post'; await api(`/api/v1/set-import-jobs/${current.id}/cancel`, {method:'POST'}); };
  icons();
}

async function monitorSetImport(job) {
  if (!job || monitoredSetJob === job.id) return;
  monitoredSetJob = job.id;
  try {
    let current = job;
    for (;;) {
      renderSetStatus(current);
      const state = await api(`/api/v1/jobs/${job.id}`);
      if (state.status === 'completed' || state.status === 'failed') break;
      current = state;
      await new Promise(resolve => setTimeout(resolve, 700));
    }
  } catch {}
  if (monitoredSetJob === job.id) { monitoredSetJob = null; jobStatus.textContent = ''; }
}

async function restoreSetImportMonitor() {
  try {
    const active = await api('/api/v1/set-import-jobs/active');
    if (active.job) monitorSetImport(active.job);
  } catch {}
}

async function monitorCropScan(job) {
  if (!job || monitoredCropJob === job.id) return;
  monitoredCropJob = job.id;
  const render = current => {
    jobStatus.innerHTML = `<span>Crop scan: ${current.progress}% · ${current.scanned ?? 0}/${current.total ?? '?'} · ${current.candidates ?? 0} found</span><button id="stopCropScan" class="status-stop" title="Stop crop scan"><i data-lucide="square"></i></button>`;
    const stop=document.querySelector('#stopCropScan'); stop.disabled=Boolean(current.cancel_requested); stop.onclick=async()=>{stop.disabled=true;await api(`/api/v1/crop-scan-jobs/${current.id}/cancel`,{method:'POST'})}; icons();
  };
  try {
    let current=job;
    for (;;) {
      render(current);
      const state=await api(current.status_url);
      if(state.status==='completed'||state.status==='failed')break;
      await new Promise(resolve=>setTimeout(resolve,700));
      const active=await api('/api/v1/crop-scan-jobs/active');
      if(!active.job)break; current=active.job;
    }
  } catch {}
  if(monitoredCropJob===job.id){monitoredCropJob=null;jobStatus.textContent='';refreshCounts();}
}

async function restoreCropScanMonitor(){try{const active=await api('/api/v1/crop-scan-jobs/active');if(active.job)monitorCropScan(active.job)}catch{}}

async function monitorBackgroundScan(job) {
  if (!job || monitoredBackgroundJob === job.id) return;
  monitoredBackgroundJob = job.id;
  const render = current => {
    jobStatus.innerHTML = `<span>Background scan: ${current.progress ?? 0}% · ${current.scanned ?? 0}/${current.total ?? '?'} · ${current.candidates ?? 0} found</span><button id="stopBackgroundScan" class="status-stop" title="Stop background scan"><i data-lucide="square"></i></button>`;
    const stop=document.querySelector('#stopBackgroundScan');
    stop.disabled=Boolean(current.cancel_requested);
    stop.onclick=async()=>{stop.disabled=true;await api(`/api/v1/background-scan-jobs/${current.id}/cancel`,{method:'POST'})};
    icons();
  };
  try {
    let current=job;
    for (;;) {
      render(current);
      const state=await api(current.status_url);
      if(state.status==='completed'||state.status==='failed')break;
      await new Promise(resolve=>setTimeout(resolve,700));
      const active=await api('/api/v1/background-scan-jobs/active');
      if(!active.job)break;
      current=active.job;
    }
  } catch {}
  if(monitoredBackgroundJob===job.id){
    monitoredBackgroundJob=null;
    jobStatus.textContent='';
    if(reloadBackgroundCandidates)reloadBackgroundCandidates();
  }
}

async function restoreBackgroundScanMonitor(){try{const active=await api('/api/v1/background-scan-jobs/active');if(active.job)monitorBackgroundScan(active.job)}catch{}}

async function showLibrary() {
  const state = viewState('library');
  librarySearch = state.search ?? librarySearch;
  libraryOffset = Number(state.offset ?? libraryOffset);
  const prefs = applyLibraryPrefs();
  setHeader('Library', '', button('Import', 'upload', 'primary'));
  actions.querySelector('button').onclick = () => navigate('import');
  workspace.innerHTML = `<div class="library-layout"><div class="library-main">
    <div class="toolbar"><input id="search" class="control search" placeholder="Tag, author, domain, source, or id:123"><select id="mediaType" class="control toolbar-select"><option value="">All types</option><option value="image">Images</option><option value="video">Videos</option></select><label class="toolbar-count" title="Items per page"><span>Per page</span><select id="pageSize" class="control">${[20,40,60,100].map(size => `<option value="${size}" ${size === prefs.pageSize ? 'selected' : ''}>${size}</option>`).join('')}</select></label><button id="searchBtn" class="icon-btn" title="Search"><i data-lucide="search"></i></button><div id="activeFilters" class="active-filters"></div></div>
    <div id="gallery" class="gallery"></div><div id="pager" class="pager"></div></div><div id="inspectorResize" class="inspector-resize" title="Resize panel"></div><aside id="inspector" class="inspector"><div class="empty">Select a file</div></aside></div>`;
  document.querySelector('#search').value = librarySearch;
  document.querySelector('#mediaType').value = state.mediaType || '';
  const load = async () => {
    const pageSize = libraryPrefs().pageSize;
    librarySearch = document.querySelector('#search').value.trim();
    const parsedSearch = parseLibrarySearch(librarySearch);
    const query = '';
    const authorFilter = parsedSearch.authors.length ? `&author=${encodeURIComponent(parsedSearch.authors.at(-1))}` : '';
    const idFilter = parsedSearch.mediaIds.length ? `&id=${parsedSearch.mediaIds.at(-1)}` : '';
    const parentFilter = parsedSearch.parentIds?.length ? `&remote_id=${encodeURIComponent(parsedSearch.parentIds.at(-1))}` : '';
    const type = document.querySelector('#mediaType').value;
    const filters = `${parsedSearch.includedTags.map(tag => `&tag=${encodeURIComponent(tag)}`).join('')}${parsedSearch.excludedTags.map(tag => `&exclude_tag=${encodeURIComponent(tag)}`).join('')}`;
    saveViewState('library', {search:librarySearch, offset:libraryOffset, mediaType:type});
    const data = await api(`/api/v1/media?limit=${pageSize}&offset=${libraryOffset}&q=${query}&type=${type}${authorFilter}${idFilter}${parentFilter}${filters}`);
    meta.textContent = `${data.page.total} files`;
    statusText.textContent = `Showing ${data.items.length}`;
    document.querySelector('#activeFilters').innerHTML = activeFiltersHtml();
    document.querySelector('#gallery').innerHTML = data.items.length ? data.items.map(item => `<button class="media-card" data-id="${item.id}"><span class="media-card-preview"><img loading="lazy" src="${item.thumbnail_url}" alt="">${item.is_edited?`<span class="edited-marker" title="Edited: ${esc(editSummary(item.edit_operations))}"><i data-lucide="wand-sparkles"></i></span>`:''}${item.has_parent?`<span class="parent-marker" data-parent-media-id="${item.parent_media_id || ''}" data-parent-remote-id="${esc(item.parent_id || '')}" title="Has parent"><i data-lucide="corner-left-up"></i></span>`:''}${item.has_family?`<span class="family-marker" title="Relatives: ${item.relatives?.length || item.family_members?.length || 0}"><i data-lucide="users-round"></i></span>`:''}</span><span class="media-card-body"><strong>${esc(item.author || 'Unknown author')}</strong><small>${esc(item.domain || item.type)} · ${item.width || '?'}×${item.height || '?'}</small></span></button>`).join('') : '<div class="empty">The library is empty</div>';
    document.querySelectorAll('.media-card').forEach(card => card.onclick = () => inspectMedia(Number(card.dataset.id)));
    document.querySelectorAll('.parent-marker').forEach(marker => marker.onclick = event => { event.stopPropagation(); const localId=Number(marker.dataset.parentMediaId||0); appendSearchTerm(localId ? `id:${localId}` : `parent:${marker.dataset.parentRemoteId}`); });
    if (selectedMedia) document.querySelector(`.media-card[data-id="${selectedMedia.id}"]`)?.classList.add('selected');
    const from = data.page.total ? libraryOffset + 1 : 0;
    const to = Math.min(libraryOffset + data.items.length, data.page.total);
    document.querySelector('#pager').innerHTML = `<span class="page-range">${from}-${to} of ${data.page.total}</span><button class="btn" id="prev" ${libraryOffset === 0 ? 'disabled' : ''}><i data-lucide="chevron-left"></i>Previous</button><button class="btn" id="next" ${libraryOffset + pageSize >= data.page.total ? 'disabled' : ''}>Next<i data-lucide="chevron-right"></i></button>`;
    document.querySelector('#prev').onclick = () => { libraryOffset = Math.max(0, libraryOffset - pageSize); load(); };
    document.querySelector('#next').onclick = () => { libraryOffset += pageSize; load(); };
    bindLibraryFilters(load);
    if (selectedMedia) {
      const authorContainer = document.querySelector('#inspector .author-tags');
      if (authorContainer) {
        authorContainer.innerHTML = authorHtml(selectedMedia.author);
        bindAuthorFilters(authorContainer);
      }
      const tagContainer = document.querySelector('#inspector .tag-scroll .tags');
      if (tagContainer) {
        tagContainer.innerHTML = selectedMedia.tags.map(tagHtml).join('') || 'No tags';
        tagContainer.querySelectorAll('[data-include-tag]').forEach(node => {
          const include = () => appendSearchTerm(node.dataset.includeTag);
          node.onclick = event => { if (!event.target.closest('[data-exclude-tag]')) include(); };
          node.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); include(); } };
        });
        tagContainer.querySelectorAll('[data-exclude-tag]').forEach(node => node.onclick = event => { event.stopPropagation(); appendSearchTerm(`-${node.dataset.excludeTag}`); });
        icons();
      }
    }
    icons();
  };
  reloadLibrary = load;
  document.querySelector('#searchBtn').onclick = () => { libraryOffset = 0; load(); };
  document.querySelector('#search').onkeydown = event => { if (event.key === 'Enter') { libraryOffset = 0; load(); } };
  document.querySelector('#mediaType').onchange = () => { libraryOffset = 0; load(); };
  document.querySelector('#pageSize').onchange = event => { saveLibraryPrefs({pageSize:Number(event.target.value)}); libraryOffset = 0; load(); };
  bindInspectorResize();
  icons(); await load();
  const selectedId = Number(state.selectedMediaId || 0);
  if (selectedId) { try { await inspectMedia(selectedId); } catch { saveViewState('library',{selectedMediaId:null}); } }
  restoreScrollState('library');
}

function bindInspectorResize() {
  const handle = document.querySelector('#inspectorResize');
  const layout = document.querySelector('.library-layout');
  if (!handle || !layout) return;
  handle.onpointerdown = event => {
    if (matchMedia('(max-width: 900px)').matches) return;
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-inspector');
  };
  handle.onpointermove = event => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    const bounds = layout.getBoundingClientRect();
    const width = Math.round(Math.min(bounds.width * .45, Math.max(260, bounds.right - event.clientX)));
    document.documentElement.style.setProperty('--inspector-width', `${width}px`);
  };
  handle.onpointerup = event => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove('resizing-inspector');
    const width = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--inspector-width'), 10);
    saveLibraryPrefs({inspectorWidth:width});
  };
}

async function inspectMedia(id) {
  selectedMedia = await api(`/api/v1/media/${id}`);
  saveViewState('library', {selectedMediaId:id});
  document.querySelectorAll('.media-card').forEach(card => card.classList.toggle('selected', Number(card.dataset.id) === id));
  const pane = document.querySelector('#inspector');
  const preview = selectedMedia.type === 'video' ? `<video class="inspector-preview" src="${selectedMedia.content_url}" controls></video>` : `<img class="inspector-preview" src="${selectedMedia.content_url}" alt="">`;
  pane.classList.add('has-media');
  const edits=selectedMedia.is_edited?`<div class="field"><label>Edits</label><span class="edit-description"><i data-lucide="wand-sparkles"></i>${esc(editSummary(selectedMedia.edit_operations))}</span></div>`:'';
  const parentField = selectedMedia.has_parent ? `<div class="field parent-field"><label>Parent</label>${selectedMedia.parent_media_id ? `<button type="button" id="searchParentMedia" class="text-link">Media #${selectedMedia.parent_media_id}</button>` : `<button type="button" id="searchParentRemote" class="text-link">Post #${esc(selectedMedia.parent_id)}</button>`}${selectedMedia.parent_url ? ` <a href="${esc(selectedMedia.parent_url)}" target="_blank" rel="noopener" title="Open parent source"><i data-lucide="external-link"></i></a>` : ''}</div>` : '';
  const relatives = (selectedMedia.relatives || selectedMedia.family_members || []).length ? `<div class="field relatives-field"><label>Relatives</label><div class="tags">${(selectedMedia.relatives || selectedMedia.family_members).map(relativeId => `<button type="button" class="text-link relative-link" data-media-id="${relativeId}">Media #${relativeId}</button>`).join('')}</div></div>` : '';
  const characters = selectedMedia.character_tags?.length ? `<div class="field"><label>Characters</label><div class="tags">${selectedMedia.character_tags.map(tagHtml).join('')}</div></div>` : '';
  pane.innerHTML = `<button id="closeInspector" class="icon-btn inspector-close" title="Close"><i data-lucide="x"></i></button>${preview}<h2>${esc(selectedMedia.author || 'Unknown author')}</h2><div class="field"><label>Media ID</label><button type="button" id="searchMediaId" class="text-link">${selectedMedia.id}</button></div><div class="field"><label>Source</label><a href="${esc(selectedMedia.source_url || '#')}" target="_blank" rel="noopener" class="ellipsis">${esc(selectedMedia.source_url || 'Not specified')}</a></div>${parentField}${relatives}${characters}<div class="field"><label>Size</label>${selectedMedia.width || '?'} × ${selectedMedia.height || '?'} · ${formatBytes(selectedMedia.file_size)}</div>${edits}<details class="tag-section field" open><summary><span>Tags</span><span class="badge">${selectedMedia.tags.length}</span></summary><div class="tag-scroll"><div class="tags">${selectedMedia.tags.map(tagHtml).join('') || 'No tags'}</div></div></details><div class="form-actions inspector-actions"><a class="icon-btn" href="${selectedMedia.content_url}" target="_blank" rel="noopener" title="Open full size"><i data-lucide="maximize-2"></i></a>${selectedMedia.source_url?'<button id="refreshMetadata" class="btn"><i data-lucide="refresh-cw"></i>Refresh metadata</button>':''}<button id="openEditor" class="btn"><i data-lucide="panel-top-open"></i>Open in Editor</button>${selectedMedia.type==='image'?'<button id="replaceMediaBackground" class="btn"><i data-lucide="image-plus"></i>Replace background</button>':''}<button id="addCollection" class="btn"><i data-lucide="folder-plus"></i>Build collection</button><button id="deleteMedia" class="icon-btn danger" title="Delete"><i data-lucide="trash-2"></i></button></div>`;
  const authorHeading = pane.querySelector('h2');
  authorHeading.insertAdjacentHTML('beforebegin', '<div class="field author-field"><label>Author</label></div>');
  authorHeading.className = 'author-value';
  authorHeading.dataset.includeAuthor = selectedMedia.author || '';
  authorHeading.title = selectedMedia.author ? 'Search by author' : '';
  authorHeading.outerHTML = `<div class="tags author-tags">${authorHtml(selectedMedia.author)}</div>`;
  bindAuthorFilters(pane);
  document.querySelector('#closeInspector').onclick = () => { pane.classList.remove('has-media'); selectedMedia=null; saveViewState('library',{selectedMediaId:null}); };
  document.querySelector('#searchMediaId').onclick = () => { librarySearch=`id:${id}`; libraryOffset=0; document.querySelector('#search').value=librarySearch; reloadLibrary(); };
  const parentSearch = (selectedMedia.parent_media_id ? `id:${selectedMedia.parent_media_id}` : `parent:${selectedMedia.parent_id}`);
  document.querySelector('#searchParentMedia')?.addEventListener('click', () => { librarySearch=parentSearch; libraryOffset=0; document.querySelector('#search').value=librarySearch; reloadLibrary(); });
  document.querySelector('#searchParentRemote')?.addEventListener('click', () => { librarySearch=parentSearch; libraryOffset=0; document.querySelector('#search').value=librarySearch; reloadLibrary(); });
  pane.querySelectorAll('.relative-link').forEach(node => node.onclick = () => openMediaInLibrary(Number(node.dataset.mediaId)));
  pane.querySelectorAll('[data-include-tag]').forEach(node => {
    const include = () => appendSearchTerm(node.dataset.includeTag);
    node.onclick = event => { if (!event.target.closest('[data-exclude-tag]')) include(); };
    node.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); include(); } };
  });
  pane.querySelectorAll('[data-exclude-tag]').forEach(node => node.onclick = event => { event.stopPropagation(); appendSearchTerm(`-${node.dataset.excludeTag}`); });
  document.querySelector('#deleteMedia').onclick = async () => { if (!confirm('Delete this file?')) return; try { await api(`/api/v1/media/${id}`, {method:'DELETE'}); toast('File deleted'); selectedMedia=null; showLibrary(); } catch(error) { toast(error.message,true); } };
  document.querySelector('#addCollection').onclick = () => showCollectionBuilder();
  document.querySelector('#openEditor').onclick = () => { saveViewState('editor',{targetMediaId:id,analysisId:null,backgroundOpen:false}); navigate('editor'); };
  const replaceBackground=document.querySelector('#replaceMediaBackground');
  if(replaceBackground)replaceBackground.onclick=()=>{saveViewState('editor',{targetMediaId:id,analysisId:null,backgroundOpen:true});navigate('editor')};
  const refreshMetadata = document.querySelector('#refreshMetadata');
  if (refreshMetadata) refreshMetadata.onclick = async () => { try { const job = await api(`/api/v1/media/${id}/metadata-refresh`, {method:'POST'}); await waitForJob(job.status_url); toast('Metadata refresh is ready in Review'); refreshCounts(); } catch(error) { toast(error.message,true); } };
  icons();
}

async function showImport() {
  setHeader('Import', 'Drop an image or paste a link');
  const history = await api('/api/v1/history?limit=100&entity_type=background_job');
  const historyLabels = {'import.pending':'Importing','import.accepted':'Imported','import.review':'Waiting for review','import.duplicate':'Already imported or awaiting review','import.failed':'Import failed','import.set_completed':'Set imported','import.set_partial':'Set partially imported','import.set_cancelled':'Set import stopped'};
  const historyRows = history.items.map(item=>{
    let details={}; try { details=JSON.parse(item.details_json || '{}'); } catch {}
    const set=details.set || {};
    const counters=['accepted','duplicate','review','blocked','failed'].filter(key=>details[key] != null).map(key=>`${key}: ${details[key]}`).join(' · ');
    const issues=Array.isArray(details.issues) && details.issues.length ? `<details class="history-issues"><summary>Issues (${details.issues.length})</summary><ul>${details.issues.map(issue=>`<li><a href="${esc(issue.url || '#')}" target="_blank" rel="noopener">${esc(issue.post_id || issue.remote_id || 'post')}</a>: ${esc(issue.message || issue.code || 'Import failed')}</li>`).join('')}</ul></details>` : '';
    const label=historyLabels[item.event_type] || item.event_type;
    return `<article class="history-row"><i data-lucide="${item.event_type === 'import.pending' ? 'loader-circle' : 'activity'}" class="${item.event_type === 'import.pending' ? 'spin' : ''}"></i><div><strong>${esc(label)}</strong><small>${set.name ? `${esc(set.name)} · ` : ''}Import #${item.entity_id}${counters ? ` · ${esc(counters)}` : ''}</small>${issues}</div><time>${esc(item.created_at)}</time></article>`;
  }).join('');
  workspace.innerHTML = `<div class="page"><section id="dropImport" class="drop-import" tabindex="0"><i data-lucide="upload-cloud"></i><strong>Drop an image, video, or link here</strong><span>or paste a supported source URL</span><input id="fileImport" type="file" accept="image/*,video/*" hidden><button id="chooseImport" type="button" class="btn primary"><i data-lucide="file-up"></i>Choose file</button><input id="pasteImport" class="control" type="url" placeholder="https://..." aria-label="Image URL"><button id="submitUrlImport" type="button" class="btn"><i data-lucide="link"></i>Import URL</button></section><section class="import-history"><div class="page-head"><h2>Import history</h2><span class="badge">${history.page.total}</span></div><div class="item-list">${historyRows || '<div class="empty">History is empty</div>'}</div></section></div>`;
  const drop = document.querySelector('#dropImport'); const fileInput = document.querySelector('#fileImport');
  const showQueued = created => { const list=document.querySelector('.import-history .item-list'); const empty=list.querySelector('.empty'); if(empty) empty.remove(); list.insertAdjacentHTML('afterbegin',`<article class="history-row"><i data-lucide="loader-circle" class="spin"></i><div><strong>Importing</strong><small>Import #${created.job_id}</small></div><time>now</time></article>`); document.querySelector('.import-history .badge').textContent=String(Number(document.querySelector('.import-history .badge').textContent)+1); icons(); };
  const submitFile = async file => { if (!file) return; try { const body = new FormData(); body.append('file', file); const job = await runJob(() => api('/api/v1/import-uploads',{method:'POST',body}),showQueued); toast(`Import: ${job.result.outcome}`); refreshCounts(); showImport(); } catch (error) { toast(error.message,true); showImport(); } };
  document.querySelector('#chooseImport').onclick = () => fileInput.click(); fileInput.onchange = () => submitFile(fileInput.files[0]);
  const submitUrl = async url => { if (!url) return; try { const job=await runJob(()=>api('/api/v1/url-import-jobs',{method:'POST',body:JSON.stringify({url})}),showQueued); toast(`Import: ${job.result?.outcome || 'failed'}`); refreshCounts(); showImport(); } catch(error){toast(error.message,true);showImport();} };
  drop.ondragover = event => { event.preventDefault(); drop.classList.add('dragging'); }; drop.ondragleave = () => drop.classList.remove('dragging'); drop.ondrop = event => { event.preventDefault(); drop.classList.remove('dragging'); const file=event.dataTransfer.files[0]; if (file) submitFile(file); else submitUrl(droppedUrl(event.dataTransfer)); };
  document.querySelector('#submitUrlImport').onclick = () => submitUrl(document.querySelector('#pasteImport').value.trim());
  icons(); restoreSetImportMonitor();
}

async function showReview() {
  setHeader('Review');
  const data = await api('/api/v1/review-items?limit=100');
  const sourceItems = data.items.map(item => item.kind === 'metadata'
    ? `<article class="queue-item"><img src="${item.thumbnail_url}" alt=""><div><strong>Media #${item.media_id}</strong><small class="ellipsis">Metadata update · ${item.source_metadata?.parent_id ? `parent #${esc(item.source_metadata.parent_id)}` : 'no parent'} · ${Number(item.source_metadata?.tag_count || 0)} tags</small></div><div class="actions"><button class="icon-btn open-review-media" data-media-id="${item.media_id}" title="Open in Library"><i data-lucide="images"></i></button><button class="btn accept-metadata" data-id="${item.suggestion_id}"><i data-lucide="check"></i>Apply</button><button class="icon-btn danger reject-metadata" data-id="${item.suggestion_id}" title="Ignore"><i data-lucide="trash-2"></i></button></div></article>`
    : `<article class="queue-item"><img src="${item.thumbnail_url}" alt=""><div><strong>${esc(item.original_name)}</strong><small class="ellipsis">${esc(item.reason)} · ${item.width || '?'}×${item.height || '?'}</small></div><div class="actions"><button class="btn accept" data-id="${item.id}"><i data-lucide="check"></i>Accept</button><button class="btn source" data-id="${item.id}"><i data-lucide="link"></i>Source</button><button class="icon-btn danger reject" data-id="${item.id}" title="Reject"><i data-lucide="trash-2"></i></button></div></article>`).join('');
  const groups = data.items.reduce((acc,item)=>(acc[item.reason]=(acc[item.reason]||0)+1,acc),{});
  const summary = Object.entries(groups).map(([reason,count])=>`<span class="badge">${esc(reason)}: ${count}</span>`).join('');
  workspace.innerHTML = `<div class="page"><div class="page-head"><h2>Needs attention</h2><span class="badge">${data.page.total}</span></div><div class="review-summary">${summary}</div><div class="item-list">${sourceItems || '<div class="empty">Nothing needs review</div>'}</div></div>`;
  document.querySelectorAll('.accept').forEach(node => node.onclick = () => reviewAction(node.dataset.id, 'accept'));
  document.querySelectorAll('.reject').forEach(node => node.onclick = () => reviewAction(node.dataset.id, 'reject'));
  document.querySelectorAll('.source').forEach(node => node.onclick = async () => {
    const url = prompt('Source URL'); if (!url) return;
    try { await runJob(() => api(`/api/v1/review-items/${node.dataset.id}/source`,{method:'POST',body:JSON.stringify({url})})); toast('Source applied'); showReview(); }
    catch (error) { toast(error.message,true); }
  });
  document.querySelectorAll('.accept-metadata').forEach(node => node.onclick = async () => { try { await api(`/api/v1/metadata-suggestions/${node.dataset.id}/accept`,{method:'POST'}); toast('Metadata applied'); showReview(); } catch(error) { toast(error.message,true); } });
  document.querySelectorAll('.reject-metadata').forEach(node => node.onclick = async () => { try { await api(`/api/v1/metadata-suggestions/${node.dataset.id}/reject`,{method:'POST'}); toast('Metadata ignored'); showReview(); } catch(error) { toast(error.message,true); } });
  document.querySelectorAll('.open-review-media').forEach(node => node.onclick = () => openMediaInLibrary(Number(node.dataset.mediaId)));
  meta.textContent = `${data.page.total} awaiting review`; icons(); await refreshCounts();
}

async function reviewAction(id, action) {
  try { await api(`/api/v1/review-items/${id}/${action}`,{method:'POST'}); toast(action === 'accept' ? 'File accepted' : 'File rejected'); showReview(); }
  catch (error) { toast(error.message,true); }
}

function openMediaInLibrary(mediaId) {
  selectedMedia=null;
  librarySearch=`id:${mediaId}`;
  libraryOffset=0;
  saveViewState('library',{search:librarySearch,offset:0,mediaType:'',selectedMediaId:mediaId,scrollTop:0});
  navigate('library');
}

async function showDuplicates() {
  setHeader('Duplicates','');
  const duplicateState=viewState('duplicates');
  const threshold=Math.min(100,Math.max(70,Number(duplicateState.threshold)||90));
  const data = await api('/api/v1/duplicate-matches');
  workspace.innerHTML = `<div class="duplicates-toolbar"><label for="duplicateThreshold">Similarity threshold</label><input id="duplicateThreshold" type="range" min="70" max="100" step="1" value="${threshold}"><output id="duplicateThresholdValue" for="duplicateThreshold">${threshold}%</output><button id="scanDuplicates" class="btn primary"><i data-lucide="scan-search"></i>Scan</button></div><div class="page item-list">${data.items.length ? data.items.map(match => `<section class="panel"><div class="panel-head">Match ${match.confidence}%</div><div class="panel-body"><div class="compare"><img src="${match.left.thumbnail_url}" alt=""><img src="${match.right.thumbnail_url}" alt=""></div><div class="actions" style="margin-top:10px"><button class="btn resolve" data-id="${match.id}" data-keep="left">Keep left</button><button class="btn resolve" data-id="${match.id}" data-keep="right">Keep right</button><button class="btn family" data-id="${match.id}"><i data-lucide="users-round"></i>Family</button><button class="btn ignore" data-id="${match.id}">Ignore</button></div></div></section>`).join('') : '<div class="empty">No matches</div>'}</div>`;
  const slider=document.querySelector('#duplicateThreshold');
  slider.oninput=()=>{document.querySelector('#duplicateThresholdValue').value=`${slider.value}%`;saveViewState('duplicates',{threshold:Number(slider.value)});};
  document.querySelector('#scanDuplicates').onclick = async () => { try { await runJob(() => api('/api/v1/duplicate-scan-jobs',{method:'POST',body:JSON.stringify({threshold:Number(slider.value)})})); showDuplicates(); } catch(error){toast(error.message,true);} };
  document.querySelectorAll('.ignore').forEach(node => node.onclick = async () => { await api(`/api/v1/duplicate-matches/${node.dataset.id}/ignore`,{method:'POST'}); showDuplicates(); });
  document.querySelectorAll('.resolve').forEach(node => node.onclick = async () => { try { await api(`/api/v1/duplicate-matches/${node.dataset.id}/resolve`,{method:'POST',body:JSON.stringify({keep:node.dataset.keep,merge_metadata:true})}); showDuplicates(); } catch(error){toast(error.message,true);} }); icons();
  document.querySelectorAll('.family').forEach(node => node.onclick = async () => { try { await api(`/api/v1/duplicate-matches/${node.dataset.id}/family`,{method:'POST'}); toast('Both files were added to the family'); showDuplicates(); } catch(error){toast(error.message,true);} }); icons();
}

async function showEditor() {
  setHeader('Editor', 'Crop and backgrounds');
  const editorState=viewState('editor');
  let cropSettings=await api('/api/v1/settings');
  const render = async (status=editorState.status || 'pending') => {
    saveViewState('editor',{status,analysisId:null,targetMediaId:null,backgroundOpen:false});
    const savedBackgroundState=viewState('editor');
    const backgroundTolerance=Math.min(80,Math.max(5,Number(savedBackgroundState.backgroundTolerance)||24));
    const backgroundMinimumArea=Math.min(95,Math.max(5,Number(savedBackgroundState.backgroundMinimumArea)||25));
    const candidateQuery=`tolerance=${backgroundTolerance}&min_background_percent=${backgroundMinimumArea}`;
    const [data,backgroundData]=await Promise.all([api(`/api/v1/crop-analyses?status=${status}`),api(`/api/v1/background-candidates?${candidateQuery}`)]);
    const backgroundItems=backgroundData.items || [];
    const asPercent=value=>{const number=Number(value)||0;return number<=1?number*100:number};
    const colorCss=value=>{
      if(Array.isArray(value)&&value.length>=3)return `rgb(${value.slice(0,3).map(channel=>Math.max(0,Math.min(255,Number(channel)||0))).join(',')})`;
      return /^#[0-9a-f]{6}$/i.test(String(value||''))?String(value):'#dfe4e6';
    };
    workspace.innerHTML=`<div class="page editor-page">
      <div class="editor-toolbar"><select id="cropStatus" class="control"><option value="pending">Pending</option><option value="cropped">Cropped</option><option value="no_crop_needed">No crop needed</option><option value="failed">Failed</option><option value="all">All</option></select><label>Minimum area <input id="cropArea" class="control" type="number" min="1" max="50" value="10"></label><label>Padding <input id="cropPadding" class="control" type="number" min="0" max="10" step=".5" value="2"></label><button id="scanSelected" class="btn"><i data-lucide="scan-line"></i>Selected</button><button id="scanAll" class="btn primary"><i data-lucide="scan-search"></i>Find crop candidates</button></div>
      <div class="page-head"><h2>Crop candidates</h2><span class="badge">${data.items.length}</span></div>
      <div class="item-list">${data.items.map(item=>`<article class="queue-item"><img src="${item.thumbnail_url}" alt=""><div><strong>Media #${item.media_id}</strong><small>${esc(item.status)} · ${Number(item.confidence).toFixed(0)}% confidence · removes ${Number(item.removed_area).toFixed(1)}%</small></div><div class="actions"><button class="icon-btn open-library" data-media-id="${item.media_id}" title="Open in Library"><i data-lucide="images"></i></button>${item.status==='pending'?`<button class="btn open-crop" data-id="${item.id}"><i data-lucide="scan-line"></i>Review</button>`:`<button class="btn reset-crop" data-id="${item.id}"><i data-lucide="rotate-ccw"></i>Reopen review</button>`}</div></article>`).join('')||'<div class="empty">No items for this status</div>'}</div>
      <section class="background-candidates-section">
        <div class="page-head"><h2>Background candidates</h2><span class="badge">${backgroundItems.length}</span></div>
        <div class="background-scan-toolbar"><label>Edge tolerance <input id="backgroundTolerance" class="control" type="number" min="5" max="80" value="${backgroundTolerance}"></label><label>Minimum background, % <input id="backgroundMinimumArea" class="control" type="number" min="5" max="95" value="${backgroundMinimumArea}"></label><button id="analyzeSelectedBackground" class="btn"><i data-lucide="scan-line"></i>Analyze selected</button><button id="scanBackgrounds" class="btn primary"><i data-lucide="scan-search"></i>Find background candidates</button></div>
        <div class="item-list background-candidate-list">${backgroundItems.map(item=>`<article class="queue-item background-candidate"><img src="${item.thumbnail_url}" alt=""><div><strong>Media #${item.media_id}</strong><small><span class="background-color-swatch" style="background:${colorCss(item.background_color)}"></span>${asPercent(item.confidence).toFixed(0)}% confidence · ${asPercent(item.background_area_percent).toFixed(1)}% background</small></div><div class="actions"><button class="icon-btn open-background-library" data-media-id="${item.media_id}" title="Open in Library"><i data-lucide="images"></i></button><button class="btn open-background-editor" data-media-id="${item.media_id}"><i data-lucide="image-plus"></i>Replace background</button></div></article>`).join('')||'<div class="empty">No background candidates</div>'}</div>
      </section>
    </div>`;
    document.querySelector('#cropStatus').value=status; document.querySelector('#cropStatus').onchange=e=>render(e.target.value);
    document.querySelector('#cropStatus').insertAdjacentHTML('afterend','<label>Preset <select id="cropPreset" class="control"><option value="14">Cautious</option><option value="20">Normal</option><option value="28">Sensitive</option></select></label><label>Selected analysis <select id="cropSelectedMethod" class="control"><option value="local">Local</option><option value="vision">Vision model</option></select></label>');
    document.querySelector('#cropPreset').value=String(cropSettings.crop_background_tolerance);document.querySelector('#cropArea').value=String(cropSettings.crop_min_area_percent);document.querySelector('#cropPadding').value=String(cropSettings.crop_padding_percent);document.querySelector('#cropSelectedMethod').value=cropSettings.crop_selected_analysis;
    const persistCropSettings=async()=>{cropSettings=await api('/api/v1/settings',{method:'PATCH',body:JSON.stringify({crop_background_tolerance:Number(document.querySelector('#cropPreset').value),crop_min_area_percent:Number(document.querySelector('#cropArea').value),crop_padding_percent:Number(document.querySelector('#cropPadding').value),crop_selected_analysis:document.querySelector('#cropSelectedMethod').value})});toast('Crop settings saved')};
    ['cropPreset','cropArea','cropPadding','cropSelectedMethod'].forEach(id=>document.querySelector(`#${id}`).onchange=persistCropSettings);
    const options=()=>({min_area:Number(document.querySelector('#cropArea').value),padding:Number(document.querySelector('#cropPadding').value)/100,tolerance:Number(document.querySelector('#cropPreset').value)});
    document.querySelector('#scanSelected').onclick=async()=>{if(!selectedMedia){toast('Select an image in Library first',true);return}try{const method=document.querySelector('#cropSelectedMethod').value;const result=method==='vision'?await api(`/api/v1/media/${selectedMedia.id}/crop-vision-analysis`,{method:'POST'}):await api('/api/v1/crop-analyses',{method:'POST',body:JSON.stringify({media_id:selectedMedia.id,...options()})});if(result.id){openCrop(result.id)}else{toast('No removable margins found')}}catch(error){toast(error.message,true)}};
    document.querySelector('#scanAll').onclick=async()=>{try{const created=await api('/api/v1/crop-scan-jobs',{method:'POST',body:JSON.stringify(options())});monitorCropScan({id:created.job_id,status_url:created.status_url,progress:0,scanned:0,total:'?',candidates:0});toast('Crop scan started')}catch(error){toast(error.message,true)}};
    document.querySelectorAll('.open-library').forEach(n=>n.onclick=()=>openMediaInLibrary(Number(n.dataset.mediaId)));
    const backgroundOptions=()=>({tolerance:Number(document.querySelector('#backgroundTolerance').value),min_background_percent:Number(document.querySelector('#backgroundMinimumArea').value)});
    const saveBackgroundOptions=()=>{const values=backgroundOptions();saveViewState('editor',{backgroundTolerance:values.tolerance,backgroundMinimumArea:values.min_background_percent});return values};
    ['backgroundTolerance','backgroundMinimumArea'].forEach(id=>document.querySelector(`#${id}`).onchange=saveBackgroundOptions);
    document.querySelector('#analyzeSelectedBackground').onclick=async()=>{if(!selectedMedia||selectedMedia.type!=='image'){toast('Select an image in Library first',true);return}try{const result=await api(`/api/v1/media/${selectedMedia.id}/background-analysis`,{method:'POST',body:JSON.stringify(saveBackgroundOptions())});if(result.status==='candidate'){saveViewState('editor',{targetMediaId:selectedMedia.id,analysisId:null,backgroundOpen:true});await openMediaEditor(selectedMedia.id)}else{toast('No replaceable background found')}}catch(error){toast(error.message,true)}};
    document.querySelector('#scanBackgrounds').onclick=async()=>{try{const created=await api('/api/v1/background-scan-jobs',{method:'POST',body:JSON.stringify(saveBackgroundOptions())});monitorBackgroundScan({id:created.job_id,status_url:created.status_url,progress:0,scanned:0,total:'?',candidates:0});toast('Background scan started')}catch(error){toast(error.message,true)}};
    document.querySelectorAll('.open-background-library').forEach(n=>n.onclick=()=>openMediaInLibrary(Number(n.dataset.mediaId)));
    document.querySelectorAll('.open-background-editor').forEach(n=>n.onclick=()=>{saveViewState('editor',{targetMediaId:Number(n.dataset.mediaId),analysisId:null,backgroundOpen:true});openMediaEditor(Number(n.dataset.mediaId))});
    reloadBackgroundCandidates=()=>{if(currentView==='editor'&&!Number(viewState('editor').targetMediaId))render(status)};
    document.querySelectorAll('.open-crop').forEach(n=>n.onclick=()=>openCrop(Number(n.dataset.id)));document.querySelectorAll('.reset-crop').forEach(n=>n.onclick=async()=>{await api(`/api/v1/crop-analyses/${n.dataset.id}/reset`,{method:'POST'});render(status)}); icons(); document.querySelector('#cropCount').textContent=status==='pending'?(data.items.length||''):document.querySelector('#cropCount').textContent;
  };
  const openMediaEditor = async mediaId => {
    saveViewState('editor',{targetMediaId:mediaId,analysisId:null,box:null,backgroundMediaId:mediaId});
    const [state,revisions,savedPreview]=await Promise.all([api(`/api/v1/media/${mediaId}/editor-state`),api(`/api/v1/media/${mediaId}/revisions`),api(`/api/v1/media/${mediaId}/background-preview`).catch(()=>({status:'none'}))]);
    const active=revisions.items.find(revision=>revision.active);
    const original=[...revisions.items].reverse().find(revision=>revision.operation==='original'&&!revision.parent_revision_id);
    const operationDetails=revision=>revision.operation==='crop'&&Array.isArray(revision.details?.box)?`Crop: ${revision.details.box.join(', ')}${revision.details.method?` · ${operationName(revision.details.method)} analysis`:''}`:operationName(revision.operation);
    const editorState=viewState('editor');
    const backgroundStateMatches=Number(editorState.backgroundMediaId)===Number(mediaId);
    const backgroundOpen=Boolean(editorState.backgroundOpen);
    const savedPreviewMode=backgroundStateMatches&&editorState.backgroundPreviewMode==='mask'?'mask':'cutout';
    const savedBlur=backgroundStateMatches?Number(editorState.backgroundBlur ?? 12):12;
    const savedAssetId=backgroundStateMatches?Number(editorState.backgroundAssetId || 0):0;
    workspace.innerHTML=`<div class="page editor-page">
      <div class="page-head"><button class="btn" id="backEditor"><i data-lucide="chevron-left"></i>Editor</button><button class="icon-btn" id="cropOpenLibrary" title="Open in Library"><i data-lucide="images"></i></button><h2>Image editor</h2>${state.is_edited?`<span class="badge good">${esc(editSummary(state.edit_operations))}</span>`:'<span class="badge">Original</span>'}</div>
      <div class="editor-version-compare"><section><h3>Current version</h3><img src="${active?.content_url||state.content_url}" alt=""></section><section><h3>Original</h3><img src="${original?.content_url||state.content_url}" alt=""></section></div>
      <div class="actions editor-state-actions"><button class="btn primary" id="analyzeCurrent"><i data-lucide="scan-line"></i>Analyze current</button><button class="btn" id="toggleBackgroundEditor" aria-expanded="${backgroundOpen}"><i data-lucide="image-plus"></i>Replace background</button><button class="btn danger" id="resetOriginal" ${state.is_edited?'':'disabled'}><i data-lucide="rotate-ccw"></i>Reset to original</button></div>
       <section id="backgroundPanel" class="background-panel" ${backgroundOpen?'':'hidden'}>
         <div class="background-panel-head"><div><h3>Background replacement</h3><span id="backgroundStatus" class="background-status">${savedPreview?.status==='ready'?'Preview ready':'Preview required'}</span></div><div class="actions"><button id="analyzeBackgroundCurrent" class="btn"><i data-lucide="scan-search"></i>Check candidate</button><button id="removeBackgroundPreview" class="btn primary"><i data-lucide="scan-eye"></i>${savedPreview?.status==='ready'?'Regenerate automatic mask':'Remove background / Preview'}</button></div></div>
         <div class="background-zoom-control"><label for="backgroundZoom">Zoom <output id="backgroundZoomValue" for="backgroundZoom">100%</output></label><input id="backgroundZoom" type="range" min="100" max="400" value="${Number(editorState.backgroundZoom || 100)}"></div>
         <div class="background-preview-comparison"><figure><figcaption>Source</figcaption><div id="backgroundSourcePane" class="background-zoom-pane"><img id="backgroundSourceImage" src="${active?.content_url||state.content_url}" alt=""></div></figure><figure class="transparent-preview"><figcaption>Removed background</figcaption><div id="backgroundPreviewPane" class="background-zoom-pane"><div id="backgroundPreviewResult" class="background-preview-placeholder"><canvas id="backgroundPreviewCanvas" aria-label="Removed background preview"></canvas><i data-lucide="image"></i></div></div></figure></div>
         <div class="background-preview-modes" role="group" aria-label="Preview mode"><span>Preview</span><button type="button" class="btn${savedPreviewMode==='cutout'?' primary':''}" data-preview-mode="cutout" aria-pressed="${savedPreviewMode==='cutout'}">Cutout</button><button type="button" class="btn${savedPreviewMode==='mask'?' primary':''}" data-preview-mode="mask" aria-pressed="${savedPreviewMode==='mask'}">Mask</button></div>
         <div id="backgroundAfterPreview" class="background-after-preview" ${savedPreview?.status==='ready'?'':'hidden'}>
           <div class="background-generation-note">The automatic mask is ready. Choose a background and apply the result.</div>
           <div class="background-selected-state"><span id="selectedBackgroundSummary">No background selected</span><button id="chooseBackground" class="btn" disabled><i data-lucide="images"></i>Choose background</button></div>
           <figure id="backgroundCompositionFigure" class="background-composition-figure" hidden><figcaption>Composition preview</figcaption><canvas id="backgroundCompositionCanvas" aria-label="Background composition preview"></canvas></figure>
           <div class="background-compose-controls"><label for="backgroundBlur">Blur <span><output id="backgroundBlurValue" for="backgroundBlur">${savedBlur}</output>%</span></label><input id="backgroundBlur" type="range" min="0" max="100" value="${savedBlur}" disabled><span class="background-save-note">The composition updates automatically. Apply creates a full-resolution PNG version.</span><button class="btn primary" id="applyBackground" disabled><i data-lucide="check"></i>Apply background</button></div>
           <div id="backgroundLibraryModal" class="background-modal" hidden role="dialog" aria-modal="true" aria-labelledby="backgroundLibraryTitle"><div class="background-modal-backdrop" data-close-background-modal></div><div class="background-modal-window"><div class="background-modal-head"><h3 id="backgroundLibraryTitle">Choose background</h3><button id="closeBackgroundModal" class="icon-btn" title="Close"><i data-lucide="x"></i></button></div><div class="background-library-toolbar"><label>Category <select id="backgroundCategoryFilter" class="control"><option value="">All backgrounds</option></select></label><label>Import category <input id="backgroundImportCategory" class="control" list="backgroundCategoryNames" maxlength="80" placeholder="Category"></label><datalist id="backgroundCategoryNames"></datalist><input id="backgroundFileInput" type="file" accept="image/*" hidden><button id="chooseBackgroundFile" class="btn"><i data-lucide="upload"></i>Import background</button></div><div id="backgroundLibrary" class="background-grid"><div class="empty">No backgrounds</div></div><div class="background-modal-actions"><button id="cancelBackground" class="btn">Cancel</button><button id="selectBackground" class="btn primary" disabled><i data-lucide="check"></i>Select background</button></div></div></div>
         </div>
       </section>
       <section class="revision-list"><h3>Versions</h3>${revisions.items.map(revision=>`<article class="${revision.in_active_chain?'active-chain':''}"><img src="${revision.content_url}" alt=""><span><strong>${esc(operationDetails(revision))}</strong><small>${revision.width}×${revision.height} · ${formatBytes(revision.file_size)}${revision.active?' · Active':''}</small></span><button class="btn restore-revision" data-id="${revision.id}" ${revision.active?'disabled':''}>${revision.active?'Active':'Restore'}</button></article>`).join('')}</section>
     </div>`;
    document.querySelector('#backEditor').onclick=()=>{saveViewState('editor',{targetMediaId:null,backgroundOpen:false});render()};
    document.querySelector('#cropOpenLibrary').onclick=()=>openMediaInLibrary(mediaId);
    document.querySelector('#analyzeCurrent').onclick=async()=>{try{const method=cropSettings.crop_selected_analysis;const result=method==='vision'?await api(`/api/v1/media/${mediaId}/crop-vision-analysis`,{method:'POST'}):await api('/api/v1/crop-analyses',{method:'POST',body:JSON.stringify({media_id:mediaId,min_area:cropSettings.crop_min_area_percent,padding:cropSettings.crop_padding_percent/100,tolerance:cropSettings.crop_background_tolerance})});if(result.id)await openCrop(result.id);else toast('No removable margins found')}catch(error){toast(error.message,true)}};
    const panel=document.querySelector('#backgroundPanel');
    const toggleBackground=document.querySelector('#toggleBackgroundEditor');
    const backgroundStatus=document.querySelector('#backgroundStatus');
    const afterPreview=document.querySelector('#backgroundAfterPreview');
    const chooseBackground=document.querySelector('#chooseBackground');
    const modal=document.querySelector('#backgroundLibraryModal');
    const modalFilter=document.querySelector('#backgroundCategoryFilter');
    const categoryFilter=modalFilter;
    const categoryInput=document.querySelector('#backgroundImportCategory');
    const categoryNames=document.querySelector('#backgroundCategoryNames');
    const fileInput=document.querySelector('#backgroundFileInput');
    const chooseFile=document.querySelector('#chooseBackgroundFile');
    const selectBackground=document.querySelector('#selectBackground');
    const blur=document.querySelector('#backgroundBlur');
    const applyBackground=document.querySelector('#applyBackground');
    const previewModeButtons=[...document.querySelectorAll('[data-preview-mode]')];
    const zoom=document.querySelector('#backgroundZoom');
    const zoomValue=document.querySelector('#backgroundZoomValue');
    const sourcePane=document.querySelector('#backgroundSourcePane');
    const previewPane=document.querySelector('#backgroundPreviewPane');
    const foregroundCanvas=document.querySelector('#backgroundPreviewCanvas');
    const compositionFigure=document.querySelector('#backgroundCompositionFigure');
    const compositionCanvas=document.querySelector('#backgroundCompositionCanvas');
    const compositionResult=compositionCanvas;
    let preview=savedPreview?.status==='ready'?savedPreview:null;
    let selectedBackground=savedAssetId;
    let temporaryBackground=savedAssetId;
    let selectedBackgroundData=null;
    let selectedBackgroundImage=null;
    let foregroundImage=null;
    const compositionForegroundCanvas=document.createElement('canvas');
    let previewMode=savedPreviewMode;
    let localFrame=0;
    let compositionReady=false;
    let assetItems=[];
    const setBackgroundStatus=(message,type='')=>{backgroundStatus.textContent=message;backgroundStatus.className=`background-status${type?` ${type}`:''}`};
    const loadImage=url=>new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>resolve(image);image.onerror=()=>reject(new Error('Preview image could not be loaded.'));image.src=url});
    const updateApply=()=>{applyBackground.disabled=!(preview&&selectedBackground);if(!compositionReady)applyBackground.disabled=true};
    const applyZoom=()=>{const scale=Math.max(100,Number(zoom.value)||100);zoomValue.value=`${scale}%`;[sourcePane,previewPane].forEach(pane=>{const image=pane?.querySelector('img,canvas');if(image)image.style.width=`${scale}%`});saveViewState('editor',{backgroundZoom:scale});};
    let syncingScroll=false;
    const syncScroll=(source,target)=>{if(syncingScroll)return;syncingScroll=true;target.scrollLeft=source.scrollLeft;target.scrollTop=source.scrollTop;saveViewState('editor',{backgroundScrollLeft:source.scrollLeft,backgroundScrollTop:source.scrollTop});requestAnimationFrame(()=>syncingScroll=false)};
    sourcePane.onscroll=()=>syncScroll(sourcePane,previewPane);previewPane.onscroll=()=>syncScroll(previewPane,sourcePane);zoom.oninput=applyZoom;
    const renderAssets=(data,category='')=>{
      const categories=data.categories||[]; assetItems=data.items||[];
      modalFilter.innerHTML=`<option value="">All backgrounds</option>${categories.map(item=>`<option value="${esc(item.name)}" ${item.name===category?'selected':''}>${esc(item.name)} (${item.count})</option>`).join('')}`;
      categoryNames.innerHTML=categories.map(item=>`<option value="${esc(item.name)}"></option>`).join('');
      if(!categoryInput.value&&categories.length)categoryInput.value=categories[0].name;
      chooseFile.disabled=!categoryInput.value.trim();
      document.querySelector('#backgroundLibrary').innerHTML=assetItems.length?assetItems.map(asset=>`<button type="button" class="background-choice${Number(asset.id)===temporaryBackground?' selected':''}" data-id="${asset.id}"><img src="${asset.content_url}" loading="lazy" alt=""><span><strong>${esc(asset.original_name)}</strong><small>${esc(asset.category)} · ${asset.width||'?'}×${asset.height||'?'}</small></span></button>`).join(''):'<div class="empty">No backgrounds in this category</div>';
      document.querySelectorAll('.background-choice').forEach(node=>node.onclick=()=>{temporaryBackground=Number(node.dataset.id);document.querySelectorAll('.background-choice').forEach(choice=>choice.classList.toggle('selected',choice===node));selectBackground.disabled=false});
      const matching=assetItems.find(asset=>Number(asset.id)===selectedBackground); if(matching)selectedBackgroundData=matching;
      selectBackground.disabled=!temporaryBackground;
    };
    const loadAssets=async(category='')=>{const [assets,categories]=await Promise.all([api(`/api/v1/background-assets${category?`?category=${encodeURIComponent(category)}`:''}`),api('/api/v1/background-assets/categories')]);renderAssets({...assets,categories:categories.items||[]},category)};
    const resolveSelectedBackground=async()=>{if(!selectedBackground)return;const data=await api('/api/v1/background-assets');selectedBackgroundData=(data.items||[]).find(asset=>Number(asset.id)===selectedBackground)||null;chooseBackground.disabled=!selectedBackgroundData;updateSelectedSummary()};
    const updateSelectedSummary=()=>{const node=document.querySelector('#selectedBackgroundSummary');node.textContent=selectedBackgroundData?`Selected background: ${selectedBackgroundData.original_name} · ${selectedBackgroundData.category}`:'No background selected';};
    const renderLocalPreview=async()=>{if(!preview||!foregroundImage)return;drawForegroundPreview(compositionForegroundCanvas.getContext('2d'),foregroundImage,0,preview.width,preview.height,0,'cutout');drawForegroundPreview(foregroundCanvas.getContext('2d'),foregroundImage,0,preview.width,preview.height,0,previewMode);if(selectedBackgroundData){if(!selectedBackgroundImage||selectedBackgroundImage.src!==selectedBackgroundData.content_url)selectedBackgroundImage=await loadImage(selectedBackgroundData.content_url);drawCompositionPreview(compositionResult.getContext('2d'),compositionForegroundCanvas,selectedBackgroundImage,Number(blur.value));compositionFigure.hidden=false;compositionReady=true}else{compositionFigure.hidden=true;compositionReady=false}applyZoom();updateApply();setBackgroundStatus(selectedBackgroundData?'Composition preview ready':'Preview ready · '+Number(preview.subject_coverage).toFixed(1)+'% subject','good')};
    const scheduleLocalPreview=()=>{if(!preview||localFrame)return;setBackgroundStatus('Updating preview...');localFrame=requestAnimationFrame(()=>{localFrame=0;renderLocalPreview().catch(error=>setBackgroundStatus(error.message,'error'))})};
    const prepareForeground=async()=>{if(!preview)return;foregroundImage=await loadImage(preview.content_url.split('?')[0]);afterPreview.hidden=false;categoryFilter.disabled=false;categoryInput.disabled=false;chooseBackground.disabled=false;blur.disabled=false;foregroundCanvas.nextElementSibling?.setAttribute('hidden','');await renderLocalPreview()};
    toggleBackground.onclick=()=>{const open=panel.hidden;panel.hidden=!open;toggleBackground.setAttribute('aria-expanded',String(open));saveViewState('editor',{backgroundOpen:open});if(open)panel.scrollIntoView({behavior:'smooth',block:'start'})};
    document.querySelector('#analyzeBackgroundCurrent').onclick=async()=>{const button=document.querySelector('#analyzeBackgroundCurrent');const saved=viewState('editor');const parameters={tolerance:Number(saved.backgroundTolerance)||24,min_background_percent:Number(saved.backgroundMinimumArea)||25};button.disabled=true;setBackgroundStatus('Analyzing background...');try{const result=await api(`/api/v1/media/${mediaId}/background-analysis`,{method:'POST',body:JSON.stringify(parameters)});if(result.status==='candidate'){const candidate=result.candidate;setBackgroundStatus(`Candidate · ${Number(candidate.background_area_percent).toFixed(1)}% background`,'good')}else setBackgroundStatus('No replaceable background found','warn')}catch(error){setBackgroundStatus(error.message,'error')}finally{button.disabled=false}};
    document.querySelector('#removeBackgroundPreview').onclick=async()=>{const button=document.querySelector('#removeBackgroundPreview');const previousPreview=preview;button.disabled=true;setBackgroundStatus(previousPreview?'Regenerating automatic mask...':'Preparing the automatic background removal...');try{preview=await api(`/api/v1/media/${mediaId}/background-preview`,{method:'POST',body:JSON.stringify({force:Boolean(previousPreview)})});button.innerHTML='<i data-lucide="scan-eye"></i>Regenerate automatic mask';const sameAsPrevious=Boolean(preview.same_as_previous);foregroundImage=null;selectedBackgroundImage=null;compositionReady=false;await resolveSelectedBackground().catch(()=>{});await prepareForeground();if(sameAsPrevious)setBackgroundStatus('The automatic model produced the same mask again.','warn')}catch(error){if(error.message&&preview!==previousPreview){setBackgroundStatus(error.message,'error');afterPreview.hidden=false;updateApply();}else{preview=previousPreview;if(previousPreview){afterPreview.hidden=false;setBackgroundStatus(error.message,'error');updateApply()}else{afterPreview.hidden=true;setBackgroundStatus(error.message,'error')}}}finally{button.disabled=false;icons()}};
    const openModal=async()=>{temporaryBackground=selectedBackground;modal.hidden=false;await loadAssets(modalFilter.value||'');document.querySelector('#selectBackground').focus()};
    const closeModal=()=>{modal.hidden=true;chooseBackground.focus()};
    chooseBackground.onclick=openModal;document.querySelector('#closeBackgroundModal').onclick=closeModal;document.querySelector('#cancelBackground').onclick=closeModal;modal.querySelector('[data-close-background-modal]').onclick=closeModal;
    modalFilter.onchange=async()=>{try{await loadAssets(modalFilter.value)}catch(error){toast(error.message,true)}};
    categoryInput.oninput=()=>{chooseFile.disabled=!categoryInput.value.trim()};chooseFile.onclick=()=>fileInput.click();
    fileInput.onchange=async()=>{const file=fileInput.files?.[0],category=categoryInput.value.trim();if(!file)return;if(!category){toast('Choose a category before importing',true);fileInput.value='';return}chooseFile.disabled=true;try{const body=new FormData();body.append('file',file);body.append('category',category);await api('/api/v1/background-assets/import',{method:'POST',body});toast('Background imported');await loadAssets(category)}catch(error){toast(error.message,true)}finally{fileInput.value='';chooseFile.disabled=!categoryInput.value.trim()}};
    // A card only changes temporaryBackground; selection is committed here
    // (selectedBackground=Number(node.dataset.id)) after confirmation.
    selectBackground.onclick=async()=>{selectedBackground=temporaryBackground;selectedBackgroundData=assetItems.find(asset=>Number(asset.id)===selectedBackground)||selectedBackgroundData;selectedBackgroundImage=null;saveViewState('editor',{backgroundMediaId:mediaId,backgroundAssetId:selectedBackground});updateSelectedSummary();closeModal();scheduleLocalPreview()};
    // The server compose preview endpoint remains available for API clients and
    // older integrations.
    const legacyComposePreviewEndpoint=media=>`/api/v1/media/${media}/background-compose-preview`;
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!modal.hidden)closeModal()},{once:false});
    blur.oninput=()=>{document.querySelector('#backgroundBlurValue').value=blur.value;saveViewState('editor',{backgroundBlur:Number(blur.value)});scheduleLocalPreview()};
    previewModeButtons.forEach(button=>button.onclick=()=>{previewMode=button.dataset.previewMode==='mask'?'mask':'cutout';previewModeButtons.forEach(item=>{const active=item.dataset.previewMode===previewMode;item.classList.toggle('primary',active);item.setAttribute('aria-pressed',String(active))});saveViewState('editor',{backgroundPreviewMode:previewMode});scheduleLocalPreview()});
    applyBackground.onclick=async()=>{if(!preview||!selectedBackground||!compositionReady)return;applyBackground.disabled=true;setBackgroundStatus('Saving and activating new version...');try{await api(`/api/v1/media/${mediaId}/background-compose`,{method:'POST',body:JSON.stringify({preview_id:preview.preview_id,background_id:selectedBackground,blur:Number(blur.value)})});toast('Background version created and activated');saveViewState('editor',{backgroundOpen:true});await openMediaEditor(mediaId)}catch(error){setBackgroundStatus(error.message,'error');updateApply()}};
    document.querySelector('#resetOriginal').onclick=async()=>{if(!confirm('Reset this image to the original version? Saved versions will remain available.'))return;try{await api(`/api/v1/media/${mediaId}/reset-to-original`,{method:'POST'});toast('Original restored');await openMediaEditor(mediaId)}catch(error){toast(error.message,true)}};
    updateSelectedSummary();
    if(preview){await resolveSelectedBackground().catch(()=>{});prepareForeground().catch(error=>setBackgroundStatus(error.message,'error'));}
    requestAnimationFrame(()=>{const saved=viewState('editor');sourcePane.scrollLeft=Number(saved.backgroundScrollLeft||0);sourcePane.scrollTop=Number(saved.backgroundScrollTop||0);previewPane.scrollLeft=sourcePane.scrollLeft;previewPane.scrollTop=sourcePane.scrollTop;applyZoom()});
    document.querySelectorAll('.restore-revision').forEach(node=>node.onclick=async()=>{if(!confirm('Restore this version?'))return;await api(`/api/v1/media/${mediaId}/revisions/${node.dataset.id}/activate`,{method:'POST'});toast('Version restored');await openMediaEditor(mediaId)}); icons();
  };
  const openCrop = async id => {
    const item=await api(`/api/v1/crop-analyses/${id}`);
    saveViewState('editor',{analysisId:id,targetMediaId:null});
    const revisions=await api(`/api/v1/media/${item.media_id}/revisions`);
    workspace.innerHTML=`<div class="page editor-page"><div class="page-head"><button class="btn" id="backEditor"><i data-lucide="chevron-left"></i>Editor</button><h2>Review crop</h2><span class="badge" id="proposalMethod">${esc(item.method)}</span></div><div class="crop-workspace"><div class="crop-source"><img id="cropSource" src="${item.content_url}" alt=""></div><canvas id="cropPreview"></canvas></div><div class="crop-controls">${['Left','Top','Right','Bottom'].map((name,index)=>`<label>${name}<input class="control crop-coordinate" data-index="${index}" type="number" value="${item.box[index]}"></label>`).join('')}<button id="resetCrop" class="icon-btn" title="Reset suggested crop"><i data-lucide="rotate-ccw"></i></button></div><div class="actions"><button class="btn" id="visionCrop"><i data-lucide="scan-eye"></i>Vision model</button><button class="btn primary" id="applyCrop"><i data-lucide="check"></i>Apply crop</button><button class="btn" id="noCrop">No crop needed</button><button class="btn" id="skipCrop">Skip</button></div><section class="revision-list"><h3>Versions</h3>${revisions.items.map(r=>`<article><img src="${r.content_url}" alt=""><span>${esc(r.operation)} · ${r.width}×${r.height} · ${formatBytes(r.file_size)}</span><button class="btn restore-revision" data-id="${r.id}" ${r.active?'disabled':''}>${r.active?'Active':'Restore'}</button></article>`).join('')}</section></div>`;
    document.querySelector('#backEditor').insertAdjacentHTML('afterend',`<button class="icon-btn" id="cropOpenLibrary" title="Open in Library"><i data-lucide="images"></i></button>`);
    document.querySelector('#cropOpenLibrary').onclick=()=>openMediaInLibrary(item.media_id);
    document.querySelector('#backEditor').onclick=()=>{saveViewState('editor',{analysisId:null});render()};
    const openNextPending = async () => {
      await render('pending');
      const next = document.querySelector('.open-crop');
      if (next) await openCrop(Number(next.dataset.id));
    };
    document.querySelector('#noCrop').onclick=async()=>{try{await api(`/api/v1/crop-analyses/${id}/no_crop_needed`,{method:'POST'});await openNextPending()}catch(error){toast(error.message,true)}};
    document.querySelector('#skipCrop').onclick=async()=>{try{await api(`/api/v1/crop-analyses/${id}/deferred`,{method:'POST'});await openNextPending()}catch(error){toast(error.message,true)}};
    const source=document.querySelector('#cropSource'),canvas=document.querySelector('#cropPreview'),inputs=[...document.querySelectorAll('.crop-coordinate')];
    const savedState=viewState('editor'); const savedBox=Number(savedState.boxRevisionId)===Number(item.revision_id)?savedState.box:null;
    if(Array.isArray(savedBox)&&savedBox.length===4)inputs.forEach((n,i)=>n.value=savedBox[i]);
    const previewPane=document.createElement('div');previewPane.className='crop-preview-pane';canvas.replaceWith(previewPane);previewPane.append(canvas);
    document.querySelector('.crop-workspace').insertAdjacentHTML('beforebegin',`<label class="crop-zoom">Zoom <input id="cropZoom" type="range" min="100" max="400" value="${Number(viewState('editor').zoom || 100)}"><output id="cropZoomValue">100%</output></label>`);
    const box=()=>inputs.map(n=>Number(n.value));
    const applyZoom=()=>{if(!source.naturalWidth||!canvas.width)return;const factor=Number(document.querySelector('#cropZoom').value)/100;const leftPane=source.parentElement;const base=Math.min(leftPane.clientWidth/source.naturalWidth,previewPane.clientWidth/canvas.width,1);source.style.maxWidth='none';source.style.maxHeight='none';source.style.width=`${source.naturalWidth*base*factor}px`;canvas.style.width=`${canvas.width*base*factor}px`;canvas.style.height=`${canvas.height*base*factor}px`;document.querySelector('#cropZoomValue').value=`${Math.round(factor*100)}%`};
    const draw=()=>{if(!source.naturalWidth)return;const [l,t,r,b]=box();canvas.width=Math.max(1,r-l);canvas.height=Math.max(1,b-t);canvas.getContext('2d').drawImage(source,l,t,r-l,b-t,0,0,r-l,b-t);saveViewState('editor',{box:box(),boxRevisionId:item.revision_id});applyZoom()}; source.onload=draw; inputs.forEach(n=>n.oninput=draw);document.querySelector('#cropZoom').oninput=()=>{saveViewState('editor',{zoom:Number(document.querySelector('#cropZoom').value)});applyZoom()};document.querySelector('#resetCrop').title='Restore proposal';document.querySelector('#resetCrop').onclick=()=>{inputs.forEach((n,i)=>n.value=item.box[i]);draw()};
    let syncingPan=false;
    const syncPan=(from,to)=>{if(syncingPan)return;syncingPan=true;const maxX=Math.max(1,from.scrollWidth-from.clientWidth),maxY=Math.max(1,from.scrollHeight-from.clientHeight);to.scrollLeft=(from.scrollLeft/maxX)*Math.max(0,to.scrollWidth-to.clientWidth);to.scrollTop=(from.scrollTop/maxY)*Math.max(0,to.scrollHeight-to.clientHeight);syncingPan=false};
    const bindPan=(pane,other)=>{pane.onscroll=()=>syncPan(pane,other);let start=null;pane.onpointerdown=event=>{if(event.button!==0)return;start={x:event.clientX,y:event.clientY,left:pane.scrollLeft,top:pane.scrollTop};pane.setPointerCapture(event.pointerId);pane.classList.add('panning')};pane.onpointermove=event=>{if(!start)return;pane.scrollLeft=start.left-(event.clientX-start.x);pane.scrollTop=start.top-(event.clientY-start.y)};pane.onpointerup=event=>{start=null;pane.classList.remove('panning');if(pane.hasPointerCapture(event.pointerId))pane.releasePointerCapture(event.pointerId)}};
    bindPan(source.parentElement,previewPane);bindPan(previewPane,source.parentElement);
    document.querySelector('#visionCrop').onclick=async()=>{try{const proposal=await api(`/api/v1/media/${item.media_id}/crop-vision-analysis`,{method:'POST'});inputs.forEach((n,i)=>n.value=proposal.box[i]);document.querySelector('#proposalMethod').textContent='vision';draw();toast('Vision proposal loaded')}catch(error){toast(error.message,true)}};
    document.querySelector('#applyCrop').onclick=async()=>{if(!confirm('Apply this crop? The original is kept in version history.'))return;try{await api(`/api/v1/crop-analyses/${id}/apply`,{method:'POST',body:JSON.stringify({box:box()})});toast('Crop applied');await openNextPending()}catch(error){toast(error.message,true)}};
    document.querySelectorAll('.restore-revision').forEach(n=>n.onclick=async()=>{if(!confirm('Restore this version?'))return;await api(`/api/v1/media/${item.media_id}/revisions/${n.dataset.id}/activate`,{method:'POST'});toast('Version restored');openMediaEditor(item.media_id)}); icons();
  };
  const targetMediaId=Number(editorState.targetMediaId || 0);
  if(targetMediaId){try{await openMediaEditor(targetMediaId);return}catch(error){toast(error.message,true);saveViewState('editor',{targetMediaId:null})}}
  if(editorState.analysisId){try{await openCrop(Number(editorState.analysisId));return}catch{saveViewState('editor',{analysisId:null})}}
  await render(editorState.status || 'pending');
}

async function showCollections() {
  const collectionState=viewState('collections');
  setHeader('Collections','',button('Create','plus','primary'));
  actions.querySelector('button').onclick = showCollectionBuilder;
  const data = await api('/api/v1/collections');
  data.items.sort((left, right) => {
    const byDate = String(right.created_at || '').localeCompare(String(left.created_at || ''));
    return byDate || Number(right.id) - Number(left.id);
  });
  const prefs = JSON.parse(localStorage.getItem('jiffle.collection-prefs') || '{"pageSize":20,"view":"list"}');
  let page = Number(collectionState.page || 1);
  const render = async () => {
    const query = (document.querySelector('#collectionSearch')?.value || '').trim().toLowerCase();
    const filtered = data.items.filter(item => item.name.toLowerCase().includes(query));
    const pageSize = Math.max(1, Number(document.querySelector('#collectionPageSize')?.value || prefs.pageSize));
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize)); page = Math.min(page, totalPages);
    const visible = filtered.slice((page-1)*pageSize, page*pageSize);
    saveViewState('collections',{search:document.querySelector('#collectionSearch')?.value || '',page});
    const smart = document.querySelector('#collectionView')?.value === 'cards';
    const warning = item => Number(item.author_warning_count) > 0 ? `<span class="badge warn" title="Author limit exceeded"><i data-lucide="triangle-alert"></i>${item.author_warning_count}</span>` : '';
    const body = smart ? visible.map(item => `<article class="smart-collection"><div class="smart-previews">${item.cover_urls.map(url => `<img src="${url}" loading="lazy" alt="">`).join('') || '<div class="empty">No files</div>'}</div><div class="smart-body"><strong>${esc(item.name)}</strong><small>${item.item_count} files · ${esc(item.created_at || '')}</small><div class="actions">${warning(item)}<button class="btn open" data-id="${item.id}"><i data-lucide="eye"></i>Open</button><button class="btn export" data-id="${item.id}"><i data-lucide="download"></i>Export</button><button class="icon-btn danger delete" data-id="${item.id}" title="Delete"><i data-lucide="trash-2"></i></button></div></div></article>`).join('') : visible.map(item => `<article class="collection-item"><i data-lucide="folder"></i><div><strong>${esc(item.name)}</strong><small>${item.item_count} files · created ${esc(item.created_at || '')}</small></div><div class="actions">${warning(item)}<button class="btn open" data-id="${item.id}"><i data-lucide="eye"></i>Open</button><button class="btn export" data-id="${item.id}"><i data-lucide="download"></i>Export</button><button class="icon-btn danger delete" data-id="${item.id}" title="Delete"><i data-lucide="trash-2"></i></button></div></article>`).join('');
    document.querySelector('#collectionResults').innerHTML = body || '<div class="empty">No collections found</div>';
    document.querySelector('#collectionPager').innerHTML = `<span>${filtered.length} collections</span><button class="icon-btn" id="collectionPrev" title="Previous" ${page===1?'disabled':''}><i data-lucide="chevron-left"></i></button><span>Page ${page} of ${totalPages}</span><button class="icon-btn" id="collectionNext" title="Next" ${page===totalPages?'disabled':''}><i data-lucide="chevron-right"></i></button>`;
    document.querySelector('#collectionPrev').onclick=()=>{page--;render()}; document.querySelector('#collectionNext').onclick=()=>{page++;render()};
    bindCollectionActions(); icons();
  };
  workspace.innerHTML = `<div class="page"><div class="collection-toolbar"><input id="collectionSearch" class="control" placeholder="Search by name"><select id="collectionPageSize" class="control"><option value="10">10 per page</option><option value="20">20 per page</option><option value="50">50 per page</option></select><select id="collectionView" class="control"><option value="list">List</option><option value="cards">Smart cards</option></select></div><div id="collectionResults" class="item-list"></div><div id="collectionPager" class="collection-pager"></div></div>`;
  document.querySelector('#collectionSearch').value=collectionState.search || ''; document.querySelector('#collectionPageSize').value=String(prefs.pageSize); document.querySelector('#collectionView').value=prefs.view;
  document.querySelector('#collectionSearch').oninput=()=>{page=1;render()}; document.querySelector('#collectionPageSize').onchange=event=>{prefs.pageSize=Number(event.target.value);localStorage.setItem('jiffle.collection-prefs',JSON.stringify(prefs));page=1;render()}; document.querySelector('#collectionView').onchange=event=>{prefs.view=event.target.value;localStorage.setItem('jiffle.collection-prefs',JSON.stringify(prefs));render()};
  await render();
  const collectionMedia = item => `<figure class="collection-media">${item.media_type === 'video' ? `<video class="collection-preview" src="${item.content_url}" poster="${item.thumbnail_url}" controls preload="metadata"></video>` : `<a href="${item.content_url}" target="_blank" rel="noopener"><img class="collection-preview" src="${item.thumbnail_url}" loading="lazy" alt=""></a>`}<figcaption>${item.source_url ? `<a href="${esc(item.source_url)}" target="_blank" rel="noopener">Source</a>` : '<span>Unknown source</span>'} <button class="icon-btn collection-library" data-media-id="${item.id}" title="Open in Library"><i data-lucide="images"></i></button></figcaption></figure>`;
  document.querySelectorAll('.open').forEach(node => node.onclick = async () => {
    const c=await api(`/api/v1/collections/${node.dataset.id}`);
    workspace.innerHTML=`<div class="page collection-page"><div class="page-head"><button class="btn" id="backCollections"><i data-lucide="chevron-left"></i>Collections</button><h2>${esc(c.name)}</h2><span class="badge">${c.items.length}</span></div><div class="collection-meta"><span>Created: ${esc(c.created_at)}</span><form id="jiggieForm"><label for="jiggieUrl">URL Jiggie</label><div class="jiggie-control"><input id="jiggieUrl" class="control" type="url" value="${esc(c.jiggie_url||'')}" placeholder="https://jiggie.fun/..."><button class="btn primary"><i data-lucide="save"></i>Save</button>${c.jiggie_url ? `<a class="icon-btn" href="${esc(c.jiggie_url)}" target="_blank" rel="noopener" title="Open Jiggie"><i data-lucide="external-link"></i></a>` : ''}</div></form></div><div class="collection-gallery">${c.items.map(collectionMedia).join('')}</div></div>`;
    document.querySelectorAll('.collection-library').forEach(n=>n.onclick=()=>openMediaInLibrary(Number(n.dataset.mediaId))); document.querySelector('#backCollections').onclick=showCollections;
    document.querySelector('#jiggieForm').onsubmit=async event=>{event.preventDefault();try{await api(`/api/v1/collections/${c.id}`,{method:'PATCH',body:JSON.stringify({jiggie_url:document.querySelector('#jiggieUrl').value})});toast('URL saved');showCollections();}catch(error){toast(error.message,true);}};
    icons();
  });
  function bindCollectionActions(){ document.querySelectorAll('.export').forEach(node => node.onclick = async () => { try{const job=await runJob(()=>api(`/api/v1/collections/${node.dataset.id}/export-jobs`,{method:'POST'}));toast(`Exported: ${job.result.item_count}`);}catch(error){toast(error.message,true);} }); document.querySelectorAll('.delete').forEach(node => node.onclick = async () => { await api(`/api/v1/collections/${node.dataset.id}`,{method:'DELETE'});showCollections(); }); document.querySelectorAll('.open').forEach(node => node.onclick = openCollection); }
  async function openCollection(event){ const c=await api(`/api/v1/collections/${event.currentTarget.dataset.id}`); workspace.innerHTML=`<div class="page collection-page"><div class="page-head"><button class="btn" id="backCollections"><i data-lucide="chevron-left"></i>Collections</button><h2>${esc(c.name)}</h2><span class="badge">${c.items.length}</span></div><div class="collection-meta"><span>Created: ${esc(c.created_at)}</span><form id="jiggieForm"><label for="jiggieUrl">URL Jiggie</label><div class="jiggie-control"><input id="jiggieUrl" class="control" type="url" value="${esc(c.jiggie_url||'')}" placeholder="https://jiggie.fun/..."><button class="btn primary"><i data-lucide="save"></i>Save</button></div></form></div><div class="collection-gallery">${c.items.map(collectionMedia).join('')}</div></div>`; document.querySelector('#backCollections').onclick=showCollections; document.querySelector('#jiggieForm').onsubmit=async e=>{e.preventDefault();try{await api(`/api/v1/collections/${c.id}`,{method:'PATCH',body:JSON.stringify({jiggie_url:document.querySelector('#jiggieUrl').value})});toast('URL saved');}catch(error){toast(error.message,true);}}; icons(); }
}

async function showCollectionBuilder() {
  setHeader('Collection builder');
  const presets = await api('/api/v1/collection-presets');
  let preview = null;
  let rejectedIds = [];
  const tagValues = value => [...new Set(String(value || '').split(/\s+/).map(tag => tag.trim().toLowerCase()).filter(Boolean))];
  workspace.innerHTML = `<div class="page collection-builder"><section class="panel"><div class="panel-head"><i data-lucide="wand-sparkles"></i>Selection rules</div><div class="panel-body builder-fields"><div class="form-row"><label>Preset</label><select id="builderPreset" class="control"><option value="">No preset</option>${presets.items.map(item => `<option value="${item.id}">${esc(item.name)}</option>`).join('')}</select></div><div class="form-row"><label>Collection name</label><input id="builderName" class="control" maxlength="120"></div><div class="form-row"><label>Search</label><input id="builderQuery" class="control" placeholder="portrait blue_eyes -comic author:artist"><div id="builderQueryChips" class="search-chips"></div></div><div class="form-row"><label>Count</label><input id="builderCount" class="control" type="number" min="1" max="1000" value="10"></div><div class="builder-actions"><button id="savePreset" class="btn"><i data-lucide="bookmark-plus"></i>Save preset</button><button id="deletePreset" class="icon-btn danger" title="Delete selected preset"><i data-lucide="trash-2"></i></button><button id="generateCollection" class="btn primary"><i data-lucide="shuffle"></i>Generate</button></div></div></section><div id="builderSummary" class="builder-summary"></div><div id="builderPreview" class="collection-preview-grid"><div class="empty">Enter tags and generate a selection</div></div><div class="builder-savebar"><button id="cancelBuilder" class="btn"><i data-lucide="chevron-left"></i>Collections</button><span id="builderStatus">No collection generated yet</span><button id="commitCollection" class="btn primary" disabled><i data-lucide="save"></i>Save collection</button></div></div>`;
  const fields = () => ({
    query: document.querySelector('#builderQuery').value.trim(),
    requested_count: Number(document.querySelector('#builderCount').value),
  });
  const renderBuilderChips = () => {
    document.querySelector('#builderQueryChips').innerHTML = tagValues(document.querySelector('#builderQuery').value).map(tag => `<span class="filter-chip${tag.startsWith('-') ? ' excluded' : ''}">${esc(tag)}</span>`).join('');
    icons();
  };
  document.querySelector('#builderQuery').oninput = renderBuilderChips;
  const renderPreview = () => {
    if (!preview) return;
    const similar = preview.most_similar_collection ? `Similarity ${Math.round(preview.max_similarity * 100)}% with "${esc(preview.most_similar_collection.name)}"` : '';
    document.querySelector('#builderSummary').innerHTML = `<span class="badge ${preview.can_save ? 'good' : 'warn'}">Available ${preview.available_count}, selected ${preview.items.length} of ${preview.requested_count}</span>${similar ? `<span>${similar}</span>` : ''}`;
    document.querySelector('#builderPreview').innerHTML = preview.items.map((item,index) => `<article class="builder-card${item.author_limit_exceeded ? ' limit-warning' : ''}"><div class="builder-image"><img src="${item.thumbnail_url}" loading="lazy" alt=""><button class="icon-btn open-builder-library" data-media-id="${item.id}" title="Open in Library"><i data-lucide="images"></i></button>${item.author_limit_exceeded ? '<span class="limit-icon" title="Author item limit exceeded"><i data-lucide="triangle-alert"></i></span>' : ''}<button class="icon-btn swap-builder-item" data-index="${index}" title="Replace"><i data-lucide="refresh-cw"></i></button></div><div><strong>${esc(item.author || 'Unknown author')}</strong><small>${item.usage_count} collections${item.last_used_at ? ` · ${esc(item.last_used_at)}` : ' · never used'}</small></div></article>`).join('') || '<div class="empty">No matching images</div>';
    document.querySelectorAll('.open-builder-library').forEach(n=>n.onclick=()=>openMediaInLibrary(Number(n.dataset.mediaId)));
    document.querySelector('#commitCollection').disabled = !preview.can_save || !document.querySelector('#builderName').value.trim();
    document.querySelector('#builderStatus').textContent = preview.can_save ? 'Selection is ready to save' : 'Not enough matching images';
    document.querySelectorAll('.swap-builder-item').forEach(node => node.onclick = async () => {
      const index = Number(node.dataset.index);
      const old = preview.items[index];
      try {
        const replacement = await api('/api/v1/collection-previews/replacement', {method:'POST', body:JSON.stringify({...fields(), current_ids:preview.items.filter((_,itemIndex)=>itemIndex!==index).map(item=>item.id), rejected_ids:[...rejectedIds,old.id]})});
        rejectedIds.push(old.id);
        preview.items[index] = replacement;
        renderPreview();
      } catch (error) { toast(error.message, true); }
    });
    icons();
  };
  document.querySelector('#builderPreset').onchange = event => {
    const preset = presets.items.find(item => item.id === Number(event.target.value));
    if (!preset) return;
    document.querySelector('#builderQuery').value = preset.query || '';
    document.querySelector('#builderCount').value = preset.requested_count;
    renderBuilderChips();
  };
  document.querySelector('#generateCollection').onclick = async () => {
    try { rejectedIds = []; preview = await api('/api/v1/collection-previews', {method:'POST',body:JSON.stringify(fields())}); renderPreview(); }
    catch (error) { toast(error.message, true); }
  };
  document.querySelector('#builderName').oninput = () => preview && renderPreview();
  document.querySelector('#savePreset').onclick = async () => {
    const name = prompt('Preset name');
    if (!name) return;
    try {
      const saved = await api('/api/v1/collection-presets', {method:'POST',body:JSON.stringify({name,...fields()})});
      presets.items.push(saved);
      document.querySelector('#builderPreset').add(new Option(saved.name,String(saved.id),true,true));
      toast('Preset saved');
    } catch (error) { toast(error.message, true); }
  };
  document.querySelector('#deletePreset').onclick = async () => {
    const id = Number(document.querySelector('#builderPreset').value);
    if (!id) return;
    try { await api(`/api/v1/collection-presets/${id}`,{method:'DELETE'}); presets.items=presets.items.filter(item=>item.id!==id); document.querySelector(`#builderPreset option[value="${id}"]`).remove(); toast('Preset deleted'); }
    catch (error) { toast(error.message, true); }
  };
  document.querySelector('#commitCollection').onclick = async () => {
    try { await api('/api/v1/collections',{method:'POST',body:JSON.stringify({name:document.querySelector('#builderName').value.trim(),preset_id:Number(document.querySelector('#builderPreset').value)||null,...fields(),media_item_ids:preview.items.map(item=>item.id)})}); toast('Collection saved'); showCollections(); }
    catch (error) {
      const status=document.querySelector('#builderStatus');
      const name=document.querySelector('#builderName');
      status.textContent=error.message;
      if(/name already exists/i.test(error.message)){name.classList.add('invalid');name.focus();}
      toast(error.message, true);
    }
  };
  document.querySelector('#builderName').oninput=event=>{event.target.classList.remove('invalid');document.querySelector('#commitCollection').disabled=!preview?.can_save||!event.target.value.trim();};
  document.querySelector('#cancelBuilder').onclick = showCollections;
  icons();
}

async function showSettingsPage() {
  setHeader('Settings', 'Interface, limits, and connections');
  const [data, tagConfig] = await Promise.all([
    api('/api/v1/settings'), api('/api/v1/tag-rules'),
  ]);
  const prefs = libraryPrefs();
  const fontSize = savedFontSize();
  const defaultPaths = {media_path:'media', thumbnail_path:'thumbnails', import_staging_path:'import-staging', export_path:'collections'};
  const field = (name, label, options = {}) => `<div class="form-row"><label for="setting-${name}">${label}</label>${options.hint ? `<small class="field-hint">${options.hint}</small>` : ''}<div class="${options.pathPicker ? 'path-control' : ''}"><input id="setting-${name}" class="control" name="${name}" type="${options.type || 'text'}" value="${options.secret ? '' : esc(data[name] || '')}" placeholder="${options.secret && data[`${name}_configured`] ? 'Configured - leave blank to keep unchanged' : esc(options.placeholder || (defaultPaths[name] ? `Default: folder ${defaultPaths[name]} next to the application` : ''))}">${options.pathPicker ? `<button type="button" class="icon-btn choose-directory" data-target="setting-${name}" title="Choose folder"><i data-lucide="folder-open"></i></button>` : ''}</div></div>`;
  const provider = (name, label, description, fields, extra = '') => `<details class="settings-subsection"><summary><span><strong>${label}</strong><small>${description}</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-subsection-body">${fields.join('')}<div class="section-actions"><button type="button" class="btn test-source" data-provider="${name}"><i data-lucide="plug-zap"></i>Test</button>${extra}</div></div></details>`;
  workspace.innerHTML = `<div class="settings-page"><form id="settingsForm" class="settings-form">
    <details class="settings-section" open><summary><span class="section-icon"><i data-lucide="hard-drive"></i></span><span><strong>Storage</strong><small>Source files, exports, and service directories</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body">
      ${field('media_path','Source media',{hint:'The folder where Jiffle stores accepted images and videos.',pathPicker:true})}
      ${field('export_path','Exported collections',{hint:'The folder for exported collections.',pathPicker:true})}
      <div class="settings-fields-2">${field('thumbnail_path','Thumbnails',{hint:'Cached previews for the media library.',pathPicker:true})}${field('import_staging_path','Temporary imports',{hint:'Working directory for incomplete imports.',pathPicker:true})}</div>
      <small class="field-hint settings-restart-note">New paths take full effect after restarting the application. Existing files are not moved automatically.</small>
    </div></details>
    <details class="settings-section" open><summary><span class="section-icon"><i data-lucide="monitor-cog"></i></span><span><strong>Interface</strong><small>Text size and library display</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body">
      <div class="setting-line"><div><label>Text size</label><small>Applies immediately and is saved in this browser.</small></div><div class="size-options">${allowedFontSizes.map(size => `<button type="button" class="size-option${size === fontSize ? ' active' : ''}" data-font-size="${size}">${size} px</button>`).join('')}</div></div>
      <div class="setting-line"><div><label for="libraryPageSize">Items per page</label><small>More items reduce paging but use more memory.</small></div><select id="libraryPageSize" class="control compact-control">${[20,40,60,100].map(size => `<option value="${size}" ${size === prefs.pageSize ? 'selected' : ''}>${size}</option>`).join('')}</select></div>
      <div class="setting-line"><div><label for="libraryCardSize">Card size</label><small>Controls grid density on large screens.</small></div><select id="libraryCardSize" class="control compact-control"><option value="small" ${prefs.cardSize==='small'?'selected':''}>Compact</option><option value="medium" ${prefs.cardSize==='medium'?'selected':''}>Medium</option><option value="large" ${prefs.cardSize==='large'?'selected':''}>Large</option></select></div>
      <label class="setting-line toggle-line"><span><strong>Labels below images</strong><small>Author, source, and resolution are already available in the side panel.</small></span><input id="showCardInfo" type="checkbox" ${prefs.showCardInfo?'checked':''}><span class="toggle" aria-hidden="true"></span></label>
    </div></details>
    <details class="settings-section" open><summary><span class="section-icon"><i data-lucide="sliders-horizontal"></i></span><span><strong>Import and limits</strong><small>Deleted media, collection, and export rules</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body"><label class="setting-line toggle-line"><span><strong>Never re-import deleted media</strong><small>When enabled, a deleted file is blocked immediately. When disabled, it is sent to Review for confirmation.</small></span><input name="block_previously_deleted" type="checkbox" ${data.block_previously_deleted?'checked':''}><span class="toggle" aria-hidden="true"></span></label><div class="settings-fields-2">${field('max_items_per_author','Items per author',{type:'number',hint:'Maximum items by one author in a collection.'})}<div class="form-row"><label>Maximum image file, MB</label><small class="field-hint">Per exported image.</small><input class="control" name="max_image_export_size_mb" type="number" min="1" value="${Math.max(1,Math.round(data.max_image_export_size_bytes/1048576))}"></div><div class="form-row"><label>Maximum video file, MB</label><small class="field-hint">Per exported video.</small><input class="control" name="max_video_export_size_mb" type="number" min="1" value="${Math.max(1,Math.round(data.max_video_export_size_bytes/1048576))}"></div></div><div class="form-row"><label>Export format conversions</label><small class="field-hint">Files are converted only in exported collections; library originals stay unchanged.</small><div id="exportFormatRules" class="format-rules"></div><button id="addExportFormatRule" class="btn" type="button"><i data-lucide="plus"></i>Add conversion</button></div></div></details>
    <details class="settings-section"><summary><span class="section-icon"><i data-lucide="waypoints"></i></span><span><strong>Sources</strong><small>Booru and FurAffinity credentials</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body settings-sources">
      ${provider('danbooru','Danbooru','Login and API key',[field('danbooru_login','Login'),field('danbooru_api_key','API key',{type:'password',secret:true})])}
      ${provider('e621','e621 / e926','Username and API key',[field('e621_login','Username'),field('e621_api_key','API key',{type:'password',secret:true})])}
      ${provider('gelbooru','Gelbooru','User ID and API key',[field('gelbooru_user_id','User ID'),field('gelbooru_api_key','API key',{type:'password',secret:true})])}
      ${provider('furaffinity','FurAffinity','Cookie a and b from an active session',[field('furaffinity_cookie_a','Cookie a',{type:'password',secret:true}),field('furaffinity_cookie_b','Cookie b',{type:'password',secret:true})],'<a class="btn" href="https://www.furaffinity.net/login/" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open login</a>')}
    </div></details>
    <details class="settings-section"><summary><span class="section-icon"><i data-lucide="eraser"></i></span><span><strong>Background removal</strong><small>Automatic subject isolation and model access</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body">
      <div class="form-row"><label for="setting-background_model">Removal model</label><small class="field-hint">Automatic (recommended) uses BiRefNet-HR on a GPU and the faster BiRefNet model on CPU. RMBG-2.0 remains available for compatibility.</small><select class="control" id="setting-background_model" name="background_model"><option value="auto" ${data.background_model==='auto' || !data.background_model?'selected':''}>Automatic (recommended)</option><option value="birefnet_hr" ${data.background_model==='birefnet_hr'?'selected':''}>BiRefNet-HR (quality)</option><option value="birefnet" ${data.background_model==='birefnet'?'selected':''}>BiRefNet (faster)</option><option value="rmbg" ${data.background_model==='rmbg'?'selected':''}>RMBG-2.0 (compatibility)</option></select></div>
      <div class="form-row"><label for="setting-background_device">Processing device</label><small class="field-hint">Automatic uses the NVIDIA GPU when CUDA is available, otherwise the processor. GPU mode reports an error when CUDA is unavailable.</small><select class="control" id="setting-background_device" name="background_device"><option value="auto" ${data.background_device==='auto' || !data.background_device?'selected':''}>Automatic (recommended)</option><option value="cuda" ${data.background_device==='cuda'?'selected':''}>GPU (CUDA)</option><option value="cpu" ${data.background_device==='cpu'?'selected':''}>Processor (CPU)</option></select></div>
      ${field('huggingface_token','Hugging Face token',{type:'password',secret:true,hint:'Optional for the public BiRefNet model. Required only when using gated RMBG-2.0 weights.'})}
      <small id="huggingFaceStatus" class="field-hint" aria-live="polite">${data.huggingface_token_configured?'A token is saved.':'Automatic mode can start without a token.'}</small>
      <div class="section-actions"><button id="testHuggingFace" type="button" class="btn" ${data.huggingface_token_configured || data.background_model!=='rmbg'?'':'disabled title="Save a token before testing RMBG-2.0 access"'}><i data-lucide="plug-zap"></i>Test access</button><a class="btn" href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Manage tokens</a></div>
    </div></details>
    <details class="settings-section" open><summary><span class="section-icon"><i data-lucide="crop"></i></span><span><strong>Crop analysis</strong><small>Local detector thresholds and selected-image analysis mode</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body"><div class="settings-fields-2"><div class="form-row"><label>Detector preset</label><select class="control" name="crop_background_tolerance"><option value="14" ${data.crop_background_tolerance===14?'selected':''}>Cautious</option><option value="20" ${data.crop_background_tolerance===20?'selected':''}>Normal</option><option value="28" ${data.crop_background_tolerance===28?'selected':''}>Sensitive</option></select></div><div class="form-row"><label>Selected image analysis</label><select class="control" name="crop_selected_analysis"><option value="local" ${data.crop_selected_analysis==='local'?'selected':''}>Local</option><option value="vision" ${data.crop_selected_analysis==='vision'?'selected':''}>Vision model</option></select></div><div class="form-row"><label>Minimum removable area, %</label><input class="control" name="crop_min_area_percent" type="number" min="1" max="50" step="1" value="${data.crop_min_area_percent}"></div><div class="form-row"><label>Content padding, %</label><input class="control" name="crop_padding_percent" type="number" min="0" max="10" step=".5" value="${data.crop_padding_percent}"></div></div></div></details>
    <details class="settings-section"><summary><span class="section-icon"><i data-lucide="scan-line"></i></span><span><strong>Crop vision model</strong><small>Optional single-image analysis through a local or remote vision model</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body"><div class="settings-fields-2"><div class="form-row"><label>Format API</label><select class="control" name="crop_vision_format"><option value="openai" ${data.crop_vision_format==='openai'?'selected':''}>OpenAI-compatible</option><option value="gemini" ${data.crop_vision_format==='gemini'?'selected':''}>Gemini</option></select></div>${field('crop_vision_url','API URL',{placeholder:'http://127.0.0.1:1234/v1/chat/completions'})}${field('crop_vision_model','Model')}${field('crop_vision_key','API key',{type:'password',secret:true})}</div><small class="field-hint">The full original is sent only when you explicitly run Vision analysis for one image.</small></div></details>
    <details class="settings-section"><summary><span class="section-icon"><i data-lucide="tags"></i></span><span><strong>Tag rules</strong><small>Preferred tags, blocked tags, and aliases</small></span><i data-lucide="chevron-down"></i></summary><div class="settings-section-body"><div class="settings-fields-2"><div class="form-row"><label>Preferred tags</label><small class="field-hint">One tag per line.</small><textarea class="control tag-rule-editor" name="preferred_tags">${esc(tagConfig.preferred.join('\n'))}</textarea></div><div class="form-row"><label>Blocked tags</label><small class="field-hint">Rejected from AI suggestions.</small><textarea class="control tag-rule-editor" name="blocked_tags">${esc(tagConfig.blocked.join('\n'))}</textarea></div></div><div class="form-row"><label>Aliases</label><small class="field-hint">Format: canonical_tag = alias_1, alias_2</small><textarea class="control tag-rule-editor" name="tag_aliases">${esc(Object.entries(tagConfig.aliases).map(([canonical,aliases]) => `${canonical} = ${aliases.join(', ')}`).join('\n'))}</textarea></div></div></details>
    <div class="settings-savebar"><span id="settingsState">Interface changes are saved immediately</span><button class="btn primary"><i data-lucide="save"></i>Save settings</button></div>
  </form></div>`;

  const openSections=viewState('settings').openSections;
  if(Array.isArray(openSections))document.querySelectorAll('.settings-section').forEach((node,index)=>node.open=openSections.includes(index));
  document.querySelectorAll('.settings-section').forEach(node=>node.ontoggle=()=>saveViewState('settings',{openSections:[...document.querySelectorAll('.settings-section')].map((section,index)=>section.open?index:null).filter(index=>index!==null)}));

  document.querySelectorAll('[data-font-size]').forEach(node => node.onclick = () => {
    applyFontSize(node.dataset.fontSize);
    document.querySelectorAll('[data-font-size]').forEach(option => option.classList.toggle('active', option === node));
  });
  document.querySelector('#libraryPageSize').onchange = event => saveLibraryPrefs({pageSize:Number(event.target.value)});
  document.querySelector('#libraryCardSize').onchange = event => { saveLibraryPrefs({cardSize:event.target.value}); applyLibraryPrefs(); };
  document.querySelector('#showCardInfo').onchange = event => { saveLibraryPrefs({showCardInfo:event.target.checked}); applyLibraryPrefs(); };
  document.querySelectorAll('.choose-directory').forEach(button => button.onclick = async () => {
    const input = document.getElementById(button.dataset.target);
    try {
      const result = await api('/api/v1/settings/choose-directory',{method:'POST',body:JSON.stringify({initial_path:input.value})});
      if (result.path) {
        const suffixes = {
          'setting-thumbnail_path': 'thumbnails',
          'setting-import_staging_path': 'import-staging',
          'setting-export_path': 'collections',
        };
        const suffix = suffixes[input.id];
        const selected = result.path.replace(/[\\/]$/, '');
        input.value = suffix && selected.split(/[\\/]/).pop().toLowerCase() !== suffix
          ? `${selected}/${suffix}`
          : selected;
      }
    } catch(error) { toast(error.message,true); }
  });
  const sourceFormats=['gif','webm','mp4','jpg','jpeg','png','webp'];
  const rulesNode=document.querySelector('#exportFormatRules');
  const targetsFor=source=>['gif','webm','mp4'].includes(source)?['mp4']:['jpg','png','webp'];
  const addRule=(source='gif',target='mp4')=>{const row=document.createElement('div');row.className='format-rule';row.innerHTML=`<select class="control rule-source">${sourceFormats.map(value=>`<option value="${value}" ${value===source?'selected':''}>.${value}</option>`).join('')}</select><i data-lucide="arrow-right"></i><select class="control rule-target"></select><button class="icon-btn danger" type="button" title="Remove conversion"><i data-lucide="trash-2"></i></button>`;const sourceNode=row.querySelector('.rule-source');const targetNode=row.querySelector('.rule-target');const renderTargets=selected=>{const targets=targetsFor(sourceNode.value);targetNode.innerHTML=targets.map(value=>`<option value="${value}" ${value===selected?'selected':''}>.${value}</option>`).join('');};renderTargets(target);sourceNode.onchange=()=>renderTargets();row.querySelector('button').onclick=()=>row.remove();rulesNode.append(row);icons();};
  Object.entries(data.export_format_rules||{}).forEach(([source,target])=>addRule(source,target));
  document.querySelector('#addExportFormatRule').onclick=()=>addRule();
  document.querySelector('#settingsForm').onsubmit = async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const export_format_rules={};document.querySelectorAll('.format-rule').forEach(row=>export_format_rules[row.querySelector('.rule-source').value]=row.querySelector('.rule-target').value);
    const payload = {media_path:form.get('media_path'),export_path:form.get('export_path'),thumbnail_path:form.get('thumbnail_path'),import_staging_path:form.get('import_staging_path'),max_items_per_author:Number(form.get('max_items_per_author')),max_image_export_size_bytes:Number(form.get('max_image_export_size_mb'))*1048576,max_video_export_size_bytes:Number(form.get('max_video_export_size_mb'))*1048576,export_format_rules,block_previously_deleted:form.has('block_previously_deleted'),crop_vision_format:form.get('crop_vision_format'),crop_vision_url:form.get('crop_vision_url')||null,crop_vision_model:form.get('crop_vision_model')||null,crop_min_area_percent:Number(form.get('crop_min_area_percent')),crop_padding_percent:Number(form.get('crop_padding_percent')),crop_background_tolerance:Number(form.get('crop_background_tolerance')),crop_selected_analysis:form.get('crop_selected_analysis'),background_model:form.get('background_model')||'auto',background_device:form.get('background_device')||'auto'};
    ['huggingface_token','crop_vision_key','danbooru_login','danbooru_api_key','e621_login','e621_api_key','gelbooru_user_id','gelbooru_api_key','furaffinity_cookie_a','furaffinity_cookie_b'].forEach(key => { if (form.get(key)) payload[key]=form.get(key); });
    const lines = name => String(form.get(name) || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    const aliases = {};
    for (const line of lines('tag_aliases')) {
      const [canonical, rawAliases] = line.split('=', 2);
      if (canonical?.trim() && rawAliases != null) aliases[canonical.trim()] = rawAliases.split(',').map(value => value.trim()).filter(Boolean);
    }
     try { await Promise.all([api('/api/v1/settings',{method:'PATCH',body:JSON.stringify(payload)}),api('/api/v1/tag-rules',{method:'PUT',body:JSON.stringify({preferred:lines('preferred_tags'),blocked:lines('blocked_tags'),aliases})})]); data.background_model=payload.background_model; data.background_device=payload.background_device; if(payload.huggingface_token){data.huggingface_token_configured=true;document.querySelector('#huggingFaceStatus').textContent='A token is saved.';} updateHuggingFaceTestButton(); toast('Settings saved'); document.querySelector('#settingsState').textContent='Settings saved'; }
    catch(error) { toast(error.message,true); }
  };
  const huggingFaceInput=document.querySelector('#setting-huggingface_token');
  const huggingFaceTestButton=document.querySelector('#testHuggingFace');
   const backgroundModelSelect=document.querySelector('#setting-background_model');
   const updateHuggingFaceTestButton=()=>{const tokenAvailable=data.huggingface_token_configured||Boolean(huggingFaceInput.value.trim());const tokenRequired=backgroundModelSelect.value==='rmbg';huggingFaceTestButton.disabled=tokenRequired&&!tokenAvailable;if(huggingFaceTestButton.disabled)huggingFaceTestButton.title='Save a token before testing RMBG-2.0 access';else huggingFaceTestButton.removeAttribute('title');};
   huggingFaceInput.oninput=updateHuggingFaceTestButton;
   backgroundModelSelect.onchange=updateHuggingFaceTestButton;
   updateHuggingFaceTestButton();
  document.querySelector('#testHuggingFace').onclick = async event => {
    const button=event.currentTarget,status=document.querySelector('#huggingFaceStatus'),token=huggingFaceInput.value.trim();button.disabled=true;status.className='field-hint';status.textContent='Checking token...';
    try { const result=await api('/api/v1/settings/huggingface/test',{method:'POST',body:JSON.stringify(token?{token}:{})});status.classList.add('good');status.textContent=result.account?`Access confirmed for ${result.account}.`:'Access confirmed.';toast('Hugging Face access confirmed'); }
    catch(error) { status.classList.add('error');status.textContent=`Access denied: ${error.message}`;toast(error.message,true); }
    finally { updateHuggingFaceTestButton(); }
  };
  document.querySelectorAll('.test-source').forEach(node => node.onclick = async () => { try { await api(`/api/v1/settings/source-providers/${node.dataset.provider}/test`,{method:'POST'});toast('Connection successful'); } catch(error) { toast(error.message,true); } });
  icons(); restoreScrollState('settings');
}

async function refreshCounts(){try{const [reviews,crops]=await Promise.all([api('/api/v1/review-items?limit=1'),api('/api/v1/crop-analyses?status=pending')]);document.querySelector('#reviewCount').textContent=reviews.page.total||'';document.querySelector('#cropCount').textContent=crops.items.length||'';}catch{}}
function formatBytes(value){if(value==null)return 'unknown size';const units=['B','KB','MB','GB'];let size=value,index=0;while(size>=1024&&index<3){size/=1024;index++;}return `${size.toFixed(index?1:0)} ${units[index]}`;}

const views={library:showLibrary,import:showImport,review:showReview,duplicates:showDuplicates,editor:showEditor,collections:showCollections,settings:showSettingsPage};
async function navigate(view){if(!views[view])view='library';saveScrollState();currentView=view;history.replaceState(null,'',`#${view}`);document.querySelectorAll('.nav-item').forEach(node=>node.classList.toggle('active',node.dataset.view===view));workspace.innerHTML='<div class="empty">Loading...</div>';try{await views[view]();restoreScrollState(view)}catch(error){workspace.innerHTML='<div class="empty">Could not load this section</div>';toast(error.message,true);}icons();}
document.querySelectorAll('.nav-item').forEach(node=>node.onclick=()=>navigate(node.dataset.view));
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)||'library'));
window.addEventListener('beforeunload',saveScrollState);
navigate(location.hash.slice(1)||'library');refreshCounts();restoreCropScanMonitor();restoreBackgroundScanMonitor();restoreSetImportMonitor();icons();
