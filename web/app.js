/* KinoPub Offline — single page client (no build step, no CDN). */
'use strict';

const State = {
  authorized: false,
  hasCredentials: false,
  settings: {},
  online: navigator.onLine,
  apiReachable: true,
  downloads: [],
  activeDownloads: 0,
  ffmpeg: false,
};

const QUALITY_ORDER = ['2160p', '4k', '1440p', '1080p', '720p', '480p', '360p'];

/* ----------------------------------------------------------------- helpers */
const $ = (sel, root) => (root || document).querySelector(sel);
const el = (tag, attrs, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
};

function bytes(value) {
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return (value / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + units[i];
}

function duration(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

function eta(seconds) {
  if (!seconds || seconds <= 0) return '';
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

function toast(message, kind) {
  const node = el('div', { class: 'toast ' + (kind || '') }, message);
  $('#overlays').appendChild(node);
  setTimeout(() => node.remove(), kind === 'err' ? 6000 : 3200);
}

/* --------------------------------------------------------------- transport */
async function request(url, options) {
  const response = await fetch(url, options);
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

// X-KP-App cannot be set by a cross-origin <img>/<form>/<script>, so it marks
// requests as coming from this page rather than from a site you are visiting.
const APP_HEADER = { 'X-KP-App': '1' };

const local = {
  get: (path, params) => request('/api' + path + (params ? '?' + new URLSearchParams(params) : ''),
    { headers: APP_HEADER }),
  post: (path, body) => request('/api' + path, {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, APP_HEADER),
    body: JSON.stringify(body || {}),
  }),
};

function offlineError() {
  State.apiReachable = false;
  renderStatus();
  const error = new Error('No internet connection');
  error.status = 503;
  return error;
}

async function kp(path, params) {
  if (!navigator.onLine) throw offlineError();
  try {
    const data = await local.get('/kp/' + path, params);
    State.apiReachable = true;
    renderStatus();
    return data;
  } catch (error) {
    if (error.status === 503) { State.apiReachable = false; renderStatus(); }
    if (error.status === 401) { State.authorized = false; location.hash = '#/auth'; }
    throw error;
  }
}

async function kpPost(path, body) {
  if (!navigator.onLine) throw offlineError();
  try {
    const data = await local.post('/kp/' + path, body || {});
    State.apiReachable = true;
    renderStatus();
    return data;
  } catch (error) {
    if (error.status === 503) { State.apiReachable = false; renderStatus(); }
    if (error.status === 401) { State.authorized = false; location.hash = '#/auth'; }
    throw error;
  }
}

/* ------------------------------------------------------------------- chrome */
const NAV = [
  { hash: '#/home', icon: '🏠', label: 'Home' },
  { hash: '#/catalog/movie', icon: '🎬', label: 'Movies' },
  { hash: '#/catalog/serial', icon: '📺', label: 'Series' },
  { hash: '#/collections', icon: '🗂', label: 'Collections' },
  { sep: true },
  { hash: '#/bookmarks', icon: '⭐️', label: 'Bookmarks' },
  { hash: '#/watching', icon: '👁', label: 'Watching' },
  { sep: true },
  { hash: '#/downloads', icon: '⬇️', label: 'Downloads', badge: () => State.activeDownloads },
  { hash: '#/library', icon: '✈️', label: 'Offline' },
  { hash: '#/settings', icon: '⚙️', label: 'Settings' },
  { action: 'quit', icon: '⏻', label: 'Shut down' },
];

function renderNav() {
  const host = $('#nav');
  host.textContent = '';
  const current = location.hash || '#/home';
  for (const item of NAV) {
    if (item.sep) { host.appendChild(el('div', { class: 'nav-sep' })); continue; }
    const badgeValue = item.badge ? item.badge() : 0;
    host.appendChild(el('div', {
      class: 'nav-item' + (item.hash && current.startsWith(item.hash) ? ' active' : ''),
      onclick: item.action === 'quit' ? confirmShutdown : () => { location.hash = item.hash; },
    }, el('span', {}, item.icon), el('span', { class: 'label' }, item.label),
       badgeValue ? el('span', { class: 'badge' }, String(badgeValue)) : null));
  }
}

/** Stop the server from the browser, so you never need the Terminal. */
function confirmShutdown() {
  const active = State.downloads.filter((entry) =>
    entry.status === 'downloading' || entry.status === 'queued').length;

  const backdrop = el('div', {
    class: 'modal-backdrop',
    onclick: (event) => { if (event.target === backdrop) backdrop.remove(); },
  }, el('div', { class: 'modal' },
    el('h3', {}, '⏻ Shut down'),
    el('p', { class: 'plot' }, active
      ? `${active} download${active === 1 ? '' : 's'} still running — they will stop and resume next time you open the app.`
      : 'This stops the server. The app closes and this page stops working.'),
    el('div', { class: 'actions' },
      el('button', {
        class: 'btn danger',
        onclick: async () => {
          backdrop.remove();
          try { await local.post('/quit'); } catch (_) { /* it went away mid-reply */ }
          showStoppedScreen();
        },
      }, 'Shut down'),
      el('button', { class: 'btn ghost', onclick: () => backdrop.remove() }, 'Cancel'))));
  $('#overlays').appendChild(backdrop);
}

function showStoppedScreen() {
  closePlayer();
  document.body.textContent = '';
  const panel = el('div', {
    style: 'display:flex;align-items:center;justify-content:center;height:100vh;padding:24px',
  }, el('div', { style: 'text-align:center;max-width:420px' },
    el('div', { style: 'font-size:46px;margin-bottom:14px' }, '⏻'),
    el('h1', { style: 'font-size:22px;margin:0 0 8px' }, 'Server stopped'),
    el('p', { class: 'subtitle' },
      'Downloads are paused and will carry on where they left off. '
      + 'Open KinoPub Offline from the Desktop to start again.'),
    el('p', { class: 'card-meta' }, 'You can close this tab.')));
  document.body.appendChild(panel);
}

function renderStatus() {
  const online = navigator.onLine && State.apiReachable;
  $('#status-pill').firstChild.className = 'dot' + (online ? '' : ' off');
  $('#status-text').textContent = !navigator.onLine ? 'Offline'
    : !State.apiReachable ? 'API unreachable'
    : State.authorized ? 'Connected' : 'Not signed in';
}

function setView(...nodes) {
  const view = $('#view');
  view.textContent = '';
  nodes.flat().forEach((node) => node && view.appendChild(node));
  window.scrollTo(0, 0);
}

/** Re-render without yanking the reader back to the top. */
function setViewKeepingScroll(...nodes) {
  const top = window.scrollY;
  setView(...nodes);
  window.scrollTo(0, top);
}

function loading() { setView(el('div', { class: 'spinner' })); }

function empty(icon, title, hint, action) {
  return el('div', { class: 'empty' },
    el('div', { class: 'big' }, icon),
    el('div', { style: 'font-size:16px;color:var(--text);margin-bottom:6px' }, title),
    hint ? el('div', {}, hint) : null,
    action ? el('div', { style: 'margin-top:18px' }, action) : null);
}

function errorView(error) {
  const offline = error.status === 503 || !navigator.onLine;
  return el('div', {},
    el('div', { class: 'banner err' }, '⚠️ ' + (error.message || 'Error')),
    offline ? empty('✈️', 'No connection to kino.pub',
      'Downloaded films are always available.',
      el('button', { class: 'btn primary', onclick: () => { location.hash = '#/library'; } }, 'Open offline library')) : null);
}

/* ------------------------------------------------------------------- cards */
function posterUrl(item) {
  const posters = item.posters || {};
  return posters.medium || posters.small || posters.big || '';
}

const PLACEHOLDER = 'data:image/svg+xml,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="#1d2430"/>' +
  '<text x="100" y="155" font-size="40" text-anchor="middle" fill="#3a4556">🎬</text></svg>');

/* `rating` is kino.pub's own vote tally and `imdb`/`kinopoisk` are database
   ids - the numbers worth showing are the *_rating fields. */
function posterScore(item) {
  const value = Number(item.imdb_rating || item.kinopoisk_rating || 0);
  return value > 0 && value <= 10 ? value.toFixed(1) : '';
}

function card(item, options) {
  options = options || {};
  const img = el('img', { src: posterUrl(item) || PLACEHOLDER, loading: 'lazy', alt: item.title || '' });
  img.addEventListener('error', () => { img.src = PLACEHOLDER; });
  const quality = item.quality ? qualityLabel(item.quality) : null;
  return el('div', {
    class: 'card',
    onclick: options.onclick || (() => { location.hash = '#/item/' + item.id; }),
  },
    el('div', { class: 'poster' }, img,
      posterScore(item) ? el('span', { class: 'chip right' }, '★ ' + posterScore(item)) : null,
      quality ? el('span', { class: 'chip' }, quality) : null,
      options.badge ? el('span', { class: 'chip dl' }, options.badge) : null,
      options.corner ? el('button', {
        class: 'chip corner',
        type: 'button',
        title: options.corner.title || '',
        onclick: (event) => { event.stopPropagation(); options.corner.onclick(); },
      }, options.corner.label) : null,
      options.progress ? el('div', { class: 'progress-strip' }, el('i', { style: `width:${Math.round(options.progress * 100)}%` })) : null),
    el('div', { class: 'card-title' }, item.title || 'Untitled'),
    el('div', { class: 'card-meta' }, [item.year, options.meta].filter(Boolean).join(' · ')));
}

function cardGrid(items, options) {
  return el('div', { class: 'grid' }, (items || []).map((item) => card(item, options)));
}

function cardRow(items) {
  return el('div', { class: 'row' }, (items || []).map((item) => card(item)));
}

/* -------------------------------------------------------------------- home */
/* fresh/hot/popular reject a request without a type, whatever the docs say. */
const HOME_TYPES = [['movie', 'Movies'], ['serial', 'Series']];

function homeType() {
  try {
    const saved = localStorage.getItem('kp-home-type');
    if (saved && HOME_TYPES.some(([value]) => value === saved)) return saved;
  } catch (_) { /* private window */ }
  return 'movie';
}

async function viewHome() {
  loading();
  const type = homeType();
  try {
    const [fresh, hot, popular] = await Promise.all([
      kp('items/fresh', { type, perpage: 18 }),
      kp('items/hot', { type, perpage: 18 }),
      kp('items/popular', { type, perpage: 18 }),
    ]);
    const toggle = el('div', { class: 'tabs' }, HOME_TYPES.map(([value, label]) =>
      el('button', {
        class: 'tab' + (value === type ? ' active' : ''),
        type: 'button',
        onclick: () => {
          try { localStorage.setItem('kp-home-type', value); } catch (_) { /* ignore */ }
          viewHome();
        },
      }, label)));
    setView(
      el('h1', {}, 'Home'),
      el('p', { class: 'subtitle' }, 'Fresh, hot and popular on kino.pub'),
      toggle,
      el('h2', {}, '🆕 New', el('span', {
        class: 'more',
        onclick: () => { location.hash = `#/catalog/${type}?sort=-created`; },
      }, 'browse all →')),
      cardRow(fresh.items),
      el('h2', {}, '🔥 Hot'), cardRow(hot.items),
      el('h2', {}, '📈 Popular'), cardRow(popular.items));
  } catch (error) {
    setView(errorView(error));
  }
}

/* ----------------------------------------------------------------- catalog */
const SORTS = [
  ['-created', 'Date added'], ['-year', 'Year'], ['-rating', 'Rating'],
  ['-views', 'Views'], ['title', 'Title'],
];

async function viewCatalog(type, query) {
  loading();
  const params = {
    type,
    page: query.page || 1,
    perpage: 42,
    sort: query.sort || '-created',
  };
  if (query.genre) params.genre = query.genre;
  try {
    const [data, genres] = await Promise.all([kp('items', params), kp('genres', { type }).catch(() => ({ items: [] }))]);
    const pagination = data.pagination || {};
    const pages = Math.ceil((pagination.total || 0) / (pagination.perpage || 42));
    const navigate = (patch) => {
      const next = Object.assign({}, query, patch);
      location.hash = `#/catalog/${type}?` + new URLSearchParams(next);
    };
    setView(
      el('h1', {}, type === 'serial' ? 'Series' : type === 'movie' ? 'Movies' : 'Catalog'),
      el('p', { class: 'subtitle' }, `${pagination.total || 0} titles`),
      el('div', { class: 'filters' },
        el('select', { class: 'btn', onchange: (e) => navigate({ genre: e.target.value, page: 1 }) },
          el('option', { value: '' }, 'All genres'),
          (genres.items || []).map((g) => el('option', {
            value: String(g.id), selected: String(g.id) === String(query.genre || ''),
          }, g.title))),
        el('select', { class: 'btn', onchange: (e) => navigate({ sort: e.target.value, page: 1 }) },
          SORTS.map(([value, label]) => el('option', {
            value, selected: value === (query.sort || '-created'),
          }, label)))),
      cardGrid(data.items),
      pages > 1 ? el('div', { class: 'actions', style: 'justify-content:center;margin-top:26px' },
        el('button', { class: 'btn', disabled: Number(params.page) <= 1, onclick: () => navigate({ page: Number(params.page) - 1 }) }, '← Back'),
        el('span', { style: 'color:var(--muted);font-size:13px' }, `Page ${params.page} of ${pages}`),
        el('button', { class: 'btn', disabled: Number(params.page) >= pages, onclick: () => navigate({ page: Number(params.page) + 1 }) }, 'Next →')) : null);
  } catch (error) {
    setView(errorView(error));
  }
}

/* ------------------------------------------------------------------ search */
async function viewSearch(query) {
  const term = query.q || '';
  if (term.length < 3) return setView(empty('🔍', 'Type at least 3 characters'));
  loading();
  try {
    const data = await kp('items/search', { q: term, perpage: 48 });
    setView(
      el('h1', {}, 'Search: ' + term),
      el('p', { class: 'subtitle' }, `Found: ${(data.items || []).length}`),
      (data.items || []).length ? cardGrid(data.items) : empty('🤷', 'Nothing found'));
  } catch (error) {
    setView(errorView(error));
  }
}

/* ------------------------------------------------------- collections etc. */
async function viewCollections() {
  loading();
  try {
    const data = await kp('collections', { perpage: 48, sort: '-updated' });
    setView(el('h1', {}, 'Collections'),
      el('p', { class: 'subtitle' }, 'Curated kino.pub collections'),
      el('div', { class: 'grid' }, (data.items || []).map((collection) =>
        card(collection, { onclick: () => { location.hash = '#/collection/' + collection.id; } }))));
  } catch (error) { setView(errorView(error)); }
}

async function viewCollection(id) {
  loading();
  try {
    const data = await kp('collections/view', { id });
    setView(el('h1', {}, (data.collection || {}).title || 'Collection'), cardGrid(data.items));
  } catch (error) { setView(errorView(error)); }
}

async function viewBookmarks(folderId) {
  loading();
  try {
    if (folderId) {
      const data = await kp('bookmarks/' + folderId);
      return setView(
        el('div', { class: 'actions' }, el('button', { class: 'btn ghost small', onclick: () => { location.hash = '#/bookmarks'; } }, '← All folders')),
        el('h1', {}, (data.folder || {}).title || 'Bookmarks'),
        (data.items || []).length ? cardGrid(data.items) : empty('📭', 'Folder is empty'));
    }
    const data = await kp('bookmarks');
    const folders = data.items || [];
    setView(el('h1', {}, 'Bookmarks'),
      el('p', { class: 'subtitle' }, 'Your folders on kino.pub'),
      folders.length ? el('div', { class: 'episodes' }, folders.map((folder) =>
        el('div', { class: 'episode', style: 'cursor:pointer', onclick: () => { location.hash = '#/bookmarks/' + folder.id; } },
          el('span', {}, '📁'),
          el('span', { class: 'name' }, folder.title),
          el('span', { class: 'dur' }, `${folder.count || 0} ${(folder.count || 0) === 1 ? 'item' : 'items'}`)))) : empty('📭', 'No folders yet'));
  } catch (error) { setView(errorView(error)); }
}

async function viewWatching() {
  loading();
  try {
    const [serials, movies] = await Promise.all([
      kp('watching/serials', { subscribed: 0 }).catch(() => ({ items: [] })),
      kp('watching/movies').catch(() => ({ items: [] })),
    ]);
    const mapped = (list) => (list || []).map((entry) => ({
      id: entry.id, title: entry.title, year: entry.year,
      posters: entry.posters || { medium: entry.poster },
    }));
    setView(el('h1', {}, 'Watching'),
      el('p', { class: 'subtitle' }, 'Series and films you have not finished'),
      (serials.items || []).length ? el('div', {}, el('h2', {}, '📺 Series'),
        el('div', { class: 'grid' }, mapped(serials.items).map((item, index) =>
          card(item, { meta: (serials.items[index].new || 0) ? `+${serials.items[index].new} new` : '' })))) : null,
      (movies.items || []).length ? el('div', {}, el('h2', {}, '🎬 Movies'), cardGrid(mapped(movies.items))) : null,
      !(serials.items || []).length && !(movies.items || []).length ? empty('👀', 'Nothing here yet') : null);
  } catch (error) { setView(errorView(error)); }
}

/* --------------------------------------------------------------- item page */
/* The API is inconsistent about quality: a plain number (1080), a label
   ('1080p'), or something decorated ('1080p HEVC', '4K'). Everything is reduced
   to a pixel height so sorting and matching never depend on the spelling. */
function qualityHeight(value) {
  const text = String(value === null || value === undefined ? '' : value).toLowerCase();
  if (text.includes('4k') || text.includes('uhd')) return 2160;
  if (text.includes('2k')) return 1440;
  const match = text.match(/(\d{3,4})/);
  return match ? Number(match[1]) : 0;
}

function qualityLabel(value) {
  const height = qualityHeight(value);
  return height ? height + 'p' : String(value || '?');
}

function sortFiles(files) {
  // best first; unknown qualities sink to the bottom rather than winning by luck
  return (files || []).slice().sort((a, b) => qualityHeight(b.quality) - qualityHeight(a.quality));
}

function pickFile(files, preferred) {
  const sorted = sortFiles(files);
  if (!sorted.length) return null;
  const wanted = qualityHeight(preferred);
  const exact = wanted && sorted.find((file) => qualityHeight(file.quality) === wanted);
  return exact || sorted[0];
}

function fileUrl(file, streamType) {
  if (!file) return null;
  const urls = file.url || file.urls || {};
  return urls[streamType] || urls.http || urls.hls4 || urls.hls2 || urls.hls || null;
}

function mediaList(item) {
  const list = [];
  if (item.seasons && item.seasons.length) {
    for (const season of item.seasons) {
      for (const episode of season.episodes || []) {
        list.push({
          media_id: episode.id,
          season: season.number,
          episode: episode.number,
          title: episode.title || `Episode ${episode.number}`,
          duration: episode.duration,
          files: episode.files,
          subtitles: episode.subtitles,
          watching: episode.watching,
          watched: episode.watched,
          audios: episode.audios,
          thumbnail: episode.thumbnail,
        });
      }
    }
  } else {
    (item.videos || []).forEach((video, index) => {
      list.push({
        media_id: video.id || index + 1,
        title: video.title || item.title,
        duration: video.duration,
        files: video.files,
        subtitles: video.subtitles,
        audios: video.audios,
        watching: video.watching,
        thumbnail: video.thumbnail,
        index: index + 1,
      });
    });
  }
  return list;
}

/* An episode can carry two dozen subtitle tracks; grabbing them all would mean
   hundreds of files for one season. Keep the languages worth having. */
const SUBTITLE_LANGS = ['rus', 'ru', 'eng', 'en', 'ukr', 'uk', 'lit', 'lt'];
const SUBTITLE_LIMIT = 4;

function chooseSubtitles(subtitles) {
  const usable = (subtitles || []).filter((sub) => sub.url);
  const wanted = usable.filter((sub) => SUBTITLE_LANGS.includes(String(sub.lang || '').toLowerCase()));
  return (wanted.length ? wanted : usable)
    .slice(0, SUBTITLE_LIMIT)
    .map((sub) => ({ lang: sub.lang, url: sub.url, forced: !!sub.forced }));
}

function downloadPayload(item, media, file) {
  return {
    item_id: item.id,
    media_id: String(media.media_id),
    title: media.season ? `${item.title} S${String(media.season).padStart(2, '0')}E${String(media.episode).padStart(2, '0')}` : item.title,
    show_title: media.season ? item.title : null,
    episode_title: media.season ? media.title : null,
    season: media.season || null,
    episode: media.episode || null,
    year: item.year,
    plot: item.plot,
    genres: (item.genres || []).map((g) => g.title),
    duration: media.duration || 0,
    quality: qualityLabel(file.quality),
    url: fileUrl(file, State.settings.stream_type || 'hls4'),
    urls: (file || {}).url || (file || {}).urls || {},
    stream_type: (fileUrl(file, State.settings.stream_type || 'hls4') || '').includes('.m3u8') ? 'hls' : 'http',
    poster: posterUrl(item),
    rating: item.rating,
    subtitles: chooseSubtitles(media.subtitles),
  };
}

async function queueDownload(item, media, file) {
  const payload = downloadPayload(item, media, file);
  if (!payload.url) return toast('No link available for this file', 'err');
  try {
    const result = await local.post('/downloads', payload);
    toast(result.duplicate ? 'Already queued or downloaded' : `Queued: ${payload.title} [${qualityLabel(file.quality)}]`, 'ok');
    refreshDownloads();
  } catch (error) {
    toast('Could not queue: ' + error.message, 'err');
  }
}

async function bookmarksModal(itemId) {
  const body = el('div', { class: 'list-pick' }, el('div', { class: 'spinner' }));
  const newFolder = el('input', {
    type: 'text', placeholder: 'New folder…',
    style: 'flex:1;background:var(--bg-elev-2);border:1px solid var(--line);border-radius:10px;padding:9px 12px;outline:none',
  });
  const backdrop = el('div', { class: 'modal-backdrop', onclick: (e) => { if (e.target === backdrop) backdrop.remove(); } },
    el('div', { class: 'modal' },
      el('h3', {}, '⭐️ Bookmarks'),
      body,
      el('div', { class: 'actions' }, newFolder,
        el('button', {
          class: 'btn',
          onclick: async () => {
            const title = newFolder.value.trim();
            if (!title) return;
            try {
              await kpPost('bookmarks/create', { title });
              newFolder.value = '';
              await load();
              toast('Folder created', 'ok');
            } catch (error) { toast(error.message, 'err'); }
          },
        }, '＋ Create')),
      el('button', { class: 'btn ghost', style: 'margin-top:14px', onclick: () => backdrop.remove() }, 'Close')));
  $('#overlays').appendChild(backdrop);

  async function load() {
    try {
      const [all, mine] = await Promise.all([
        kp('bookmarks'),
        kp('bookmarks/get-item-folders', { item: itemId }).catch(() => ({ folders: [] })),
      ]);
      const chosen = new Set((mine.folders || []).map((folder) => String(folder.id)));
      body.textContent = '';
      for (const folder of all.items || []) {
        const inFolder = chosen.has(String(folder.id));
        body.appendChild(el('button', {
          class: 'opt',
          type: 'button',
          'aria-label': `${folder.title}, ${folder.count || 0} items`,
          'aria-pressed': inFolder ? 'true' : 'false',
          onclick: async () => {
            try {
              await kpPost('bookmarks/toggle-item', { item: itemId, folder: folder.id });
              await load();
              toast(inFolder ? 'Removed from \u00ab' + folder.title + '\u00bb' : 'Added to \u00ab' + folder.title + '\u00bb', 'ok');
            } catch (error) { toast(error.message, 'err'); }
          },
        },
          el('span', {}, (inFolder ? '☑︎ ' : '☐ ') + folder.title),
          el('span', { style: 'color:var(--muted);font-size:12.5px' }, `${folder.count || 0} ${(folder.count || 0) === 1 ? 'item' : 'items'}`)));
      }
      if (!(all.items || []).length) body.appendChild(el('div', { class: 'card-meta' }, 'No folders yet — create the first one.'));
    } catch (error) {
      body.textContent = '';
      body.appendChild(el('div', { class: 'banner err' }, error.message));
    }
  }
  load();
}

function qualityModal(title, files, onPick) {
  const backdrop = el('div', { class: 'modal-backdrop', onclick: (e) => { if (e.target === backdrop) backdrop.remove(); } },
    el('div', { class: 'modal' },
      el('h3', {}, title),
      el('div', { class: 'list-pick' }, sortFiles(files).map((file) =>
        el('button', {
          class: 'opt',
          type: 'button',
          'aria-label': `Download in ${qualityLabel(file.quality)} quality`,
          onclick: () => { backdrop.remove(); onPick(file); },
        },
          el('span', {}, `${qualityLabel(file.quality)}  ${file.w && file.h ? `· ${file.w}×${file.h}` : ''}`),
          el('span', { style: 'color:var(--muted);font-size:12.5px' }, 'Download')))),
      el('button', { class: 'btn ghost', onclick: () => backdrop.remove() }, 'Cancel')));
  $('#overlays').appendChild(backdrop);
}

/* What the download queue currently says about one video, so the series page
   can show it without you having to go and look at Downloads. */
/* If it is already on disk, play that copy. Streaming a file you have
   downloaded wastes bandwidth and can stall on a slow CDN. */
function playPreferringLocal(build) {
  return (media) => {
    const saved = downloadStateFor(media.media_id);
    if (saved && saved.status === 'done') {
      playLibraryEntry(saved);
      return;
    }
    openPlayer(build(media));
  };
}

function downloadStateFor(mediaId) {
  if (mediaId === null || mediaId === undefined) return null;
  const wanted = String(mediaId);
  return State.downloads.find((entry) => String(entry.media_id) === wanted) || null;
}

function downloadBadgeText(entry) {
  if (!entry) return '';
  if (entry.status === 'done') return '✓ saved';
  if (entry.status === 'downloading') return `↓ ${Math.round((entry.progress || 0) * 100)}%`;
  if (entry.status === 'queued') return '… queued';
  if (entry.status === 'paused') return '❚❚ paused';
  if (entry.status === 'error') return '! failed';
  return '';
}

let repaintItemDownloads = null;

async function viewItem(id) {
  loading();
  try {
    const data = await kp('items/' + id);
    const item = data.item || {};
    const medias = mediaList(item);
    const isSerial = !!(item.seasons && item.seasons.length);
    const preferred = State.settings.preferred_quality;

    const playFirst = () => {
      const media = medias[0];
      if (!media) return toast('No files available', 'err');
      const saved = downloadStateFor(media.media_id);
      if (saved && saved.status === 'done') return playLibraryEntry(saved);
      const file = pickFile(media.files, preferred);
      openPlayer({
        url: fileUrl(file, State.settings.stream_type || 'http'),
        urls: (file || {}).url || (file || {}).urls || {},
        title: item.title,
        subtitle: media.season ? `S${media.season}E${media.episode} · ${file.quality}` : file.quality,
        itemId: item.id, season: media.season, video: media.episode || media.index || 1,
        position: (media.watching || {}).time || 0,
        duration: media.duration,
        subtitles: chooseSubtitles(media.subtitles),
        audios: media.audios,
      });
    };

    // the API reports watched as -1 (no), 0 (started) or 1 (yes) - and -1 is
    // truthy in JS, so this has to compare rather than test for truthiness
    const isWatched = (media) => Number(media.watched) === 1
      || Number((media.watching || {}).status) === 1;
    const isUnwatched = (media) => !isWatched(media);

    const downloadUnwatched = () => {
      const pending = medias.filter((media) => isUnwatched(media) && (media.files || []).length);
      if (!pending.length) return toast('All episodes already watched', 'ok');
      qualityModal(`Unwatched episodes (${pending.length}) — choose quality`, pending[0].files, async (file) => {
        for (const media of pending) {
          const match = pickFile(media.files, file.quality);
          if (match) await queueDownload(item, media, match);
        }
        toast(`Queued: ${pending.length}`, 'ok');
      });
    };

    const downloadAll = () => {
      const all = medias.filter((media) => (media.files || []).length);
      if (!all.length) return toast('Nothing to download', 'err');
      qualityModal(`Download everything (${all.length} videos) — choose quality`, all[0].files, async (file) => {
        for (const media of all) {
          const match = pickFile(media.files, file.quality);
          if (match) await queueDownload(item, media, match);
        }
        toast(`Queued: ${all.length}`, 'ok');
      });
    };

    const posterImg = el('img', { src: posterUrl(item) || PLACEHOLDER, alt: item.title });
    posterImg.addEventListener('error', () => { posterImg.src = PLACEHOLDER; });

    const facts = [
      item.year && ['Year', item.year],
      item.rating_percentage && ['kino.pub', item.rating_percentage + '%'],
      item.imdb_rating && ['IMDb', Number(item.imdb_rating).toFixed(1)],
      item.kinopoisk_rating && ['Kinopoisk', Number(item.kinopoisk_rating).toFixed(1)],
      (item.duration || {}).average && ['Runtime', duration(item.duration.average)],
      item.quality && ['Quality', qualityLabel(item.quality)],
      (item.countries || []).length && ['Country', item.countries.map((c) => c.title).join(', ')],
      (item.genres || []).length && ['Genre', item.genres.map((g) => g.title).join(', ')],
    ].filter(Boolean);

    const hero = el('div', { class: 'hero' },
      el('div', { class: 'poster' }, posterImg),
      el('div', {},
        el('h1', {}, item.title || ''),
        item.subtitle || item.original_title ? el('p', { class: 'subtitle' }, item.subtitle || item.original_title) : null,
        el('div', { class: 'facts' }, facts.map(([key, value]) => el('span', { class: 'fact' }, key + ': ', el('b', {}, String(value))))),
        el('div', { class: 'actions' },
          el('button', { class: 'btn primary', onclick: playFirst }, '▶︎ Watch'),
          el('button', {
            class: 'btn',
            onclick: () => {
              const media = medias[0];
              if (!media) return toast('No files', 'err');
              qualityModal('Download quality', media.files, (file) => queueDownload(item, media, file));
            },
          }, '⬇︎ Download'),
          isSerial ? el('button', { class: 'btn', onclick: downloadUnwatched }, '⬇︎ Unwatched') : null,
          isSerial ? el('button', { class: 'btn', onclick: downloadAll }, '⬇︎ Everything') : null,
          el('button', { class: 'btn ghost', onclick: () => bookmarksModal(item.id) }, '⭐️ Bookmark'),
          el('button', {
            class: 'btn ghost',
            onclick: async () => {
              try { await kp('watching/togglewatchlist', { id: item.id }); toast('Watchlist updated', 'ok'); }
              catch (error) { toast(error.message, 'err'); }
            },
          }, '＋ Watchlist')),
        item.plot ? el('p', { class: 'plot' }, item.plot) : null,
        item.cast ? el('p', { class: 'card-meta', style: 'margin-top:14px' }, 'Cast: ' + item.cast) : null,
        item.director ? el('p', { class: 'card-meta' }, 'Director: ' + item.director) : null));

    const painters = [];

    const episodeRow = (media) => {
      const file = pickFile(media.files, preferred);
      const row = el('div', { class: 'episode' });

      const tick = el('button', {
        class: 'btn small ghost tick',
        type: 'button',
        title: 'Mark watched / unwatched',
        onclick: async () => {
          const params = { id: item.id, video: media.episode || media.index || 1 };
          if (media.season) params.season = media.season;
          try {
            const result = await kp('watching/toggle', params);
            const value = result && result.watched !== undefined ? result.watched : result;
            const seen = Number(value) === 1;
            media.watched = seen ? 1 : -1;
            media.watching = Object.assign({}, media.watching, { status: seen ? 1 : -1 });
            paint();
          } catch (error) {
            toast(error.message, 'err');
          }
        },
      }, '✓');

      const resumeMark = el('span', { class: 'dur resume' }, '');
      const dlMark = el('span', { class: 'dur dlstate' }, '');

      function paintDownload() {
        const state = downloadStateFor(media.media_id);
        dlMark.textContent = downloadBadgeText(state);
        dlMark.className = 'dur dlstate' + (state ? ' ' + state.status : '');
      }
      painters.push(paintDownload);

      function paint() {
        const seen = isWatched(media);
        const left = Number((media.watching || {}).time) || 0;
        row.classList.toggle('seen', seen);
        tick.classList.toggle('on', seen);
        tick.setAttribute('aria-pressed', seen ? 'true' : 'false');
        resumeMark.textContent = (!seen && left > 30) ? `▸ ${duration(left)}` : '';
      }

      const play = el('button', {
        class: 'btn small',
        title: 'Play',
        onclick: () => playPreferringLocal((one) => ({
          url: fileUrl(file, State.settings.stream_type || 'http'),
          urls: (file || {}).url || (file || {}).urls || {},
          title: item.title,
          subtitle: one.season ? `S${one.season}E${one.episode} · ${(file || {}).quality || ''}` : (file || {}).quality || '',
          itemId: item.id, season: one.season, video: one.episode || one.index || 1,
          position: (one.watching || {}).time || 0,
          duration: one.duration,
          subtitles: chooseSubtitles(one.subtitles),
          audios: one.audios,
        }))(media),
      }, '▶︎');

      const grab = el('button', {
        class: 'btn small',
        onclick: () => qualityModal(media.title, media.files, (picked) => queueDownload(item, media, picked)),
      }, '⬇︎');

      row.appendChild(el('span', { class: 'num' },
        media.season ? `${media.season}×${String(media.episode).padStart(2, '0')}` : '▶︎'));
      row.appendChild(el('span', { class: 'name' }, media.title));
      row.appendChild(resumeMark);
      row.appendChild(media.duration ? el('span', { class: 'dur' }, duration(media.duration)) : el('span', {}));
      row.appendChild(dlMark);
      row.appendChild(tick);
      row.appendChild(play);
      row.appendChild(grab);
      paint();
      paintDownload();
      return row;
    };

    const seasonBlocks = [];
    if (isSerial) {
      for (const season of item.seasons) {
        const episodes = medias.filter((media) => media.season === season.number);
        seasonBlocks.push(el('h2', {}, `Season ${season.number}`,
          el('span', {
            class: 'more',
            onclick: () => {
              const first = episodes.find((media) => (media.files || []).length);
              if (!first) return;
              qualityModal(`Season ${season.number} — quality`, first.files, async (file) => {
                for (const media of episodes) {
                  const match = pickFile(media.files, file.quality);
                  if (match) await queueDownload(item, media, match);
                }
                toast(`Season ${season.number} queued`, 'ok');
              });
            },
          }, '⬇︎ download season')));
        seasonBlocks.push(el('div', { class: 'episodes' }, episodes.map(episodeRow)));
      }
    } else if (medias.length > 1) {
      seasonBlocks.push(el('h2', {}, 'Episodes / parts'));
      seasonBlocks.push(el('div', { class: 'episodes' }, medias.map(episodeRow)));
    }

    setView(hero, seasonBlocks);
    repaintItemDownloads = () => painters.forEach((paintOne) => paintOne());
  } catch (error) {
    setView(errorView(error));
  }
}

/* ------------------------------------------------------------------ player */
let playerTimer = null;
let playerKeys = null;
let activeHls = null;

/* Audio tracks live inside the file itself, so the browser does the switching.
   The API already labels them in latin short form (DUB, MVO, Orig). */
const LANG_NAMES = {
  rus: 'Russian', ru: 'Russian', eng: 'English', en: 'English',
  ukr: 'Ukrainian', uk: 'Ukrainian', lit: 'Lithuanian', lt: 'Lithuanian',
  jpn: 'Japanese', fra: 'French', deu: 'German', ger: 'German',
  spa: 'Spanish', ita: 'Italian', pol: 'Polish', kor: 'Korean', chi: 'Chinese',
};

const CHANNEL_NAMES = { 1: 'mono', 2: 'stereo', 6: '5.1', 8: '7.1' };

function audioLabel(audio, position) {
  if (!audio) return `Track ${position + 1}`;
  const parts = [];
  const lang = String(audio.lang || '').toLowerCase();
  parts.push(LANG_NAMES[lang] || (lang ? lang.toUpperCase() : `Track ${position + 1}`));
  const kind = (audio.type || {}).short_title || (audio.type || {}).title;
  if (kind) parts.push(kind);
  const author = (audio.author || {}).short_title || (audio.author || {}).title;
  if (author) parts.push(author);
  const channels = CHANNEL_NAMES[audio.channels];
  if (channels) parts.push(channels);
  if (audio.codec) parts.push(String(audio.codec).toUpperCase());
  return parts.join(' · ');
}

const nativeAudioTracks = !!el('video', {}).audioTracks;

function canPlayHlsNatively() {
  return !!el('video', {}).canPlayType('application/vnd.apple.mpegurl');
}

function hlsSupported() {
  return typeof window.Hls !== 'undefined' && window.Hls.isSupported();
}

/** Switch audio through hls.js when it drives playback, natively otherwise. */
function audioSelector(video, audios, getHls, engage) {
  const list = (audios || []).slice().sort((a, b) => (a.index || 0) - (b.index || 0));
  if (list.length < 2) return null;

  const select = el('select', { class: 'btn small', title: 'Audio track' },
    list.map((audio, position) =>
      el('option', { value: String(position) }, audioLabel(audio, position))));

  select.addEventListener('change', () => {
    const wanted = Number(select.value);
    const label = audioLabel(list[wanted], wanted);

    const tracks = video.audioTracks;                 // Safari can do it in place
    if (tracks && tracks.length > 1) {
      for (let i = 0; i < tracks.length; i += 1) tracks[i].enabled = (i === wanted);
      toast(`Audio: ${label}`, 'ok');
      return;
    }

    const live = getHls && getHls();
    if (live && (live.audioTracks || []).length > 1) {
      live.audioTrack = Math.min(wanted, live.audioTracks.length - 1);
      toast(`Audio: ${label}`, 'ok');
      return;
    }

    // still on the mp4: move to the HLS stream, which carries the tracks apart
    if (engage && engage(wanted)) {
      toast(`Audio: ${label} — switching stream…`, 'ok');
      return;
    }
    select.value = '0';
  });
  return select;
}

/* Where to actually start. A finished episode should begin again, not open on
   its closing seconds, and a few seconds in is not worth resuming. */
function resumeFrom(position, total) {
  const at = Number(position) || 0;
  if (at <= 5) return 0;
  const length = Number(total) || 0;
  if (length && at > length - 60) return 0;      // watched to the end
  return at;
}

function openPlayer(options) {
  const urls = options.urls || {};
  const audios = options.audios || [];
  const stream = urls.hls4 || urls.hls2 || urls.hls;
  /* Stream over HLS wherever possible. A 1080p film is a ~6 GB mp4 and the CDN
     takes tens of seconds to answer a byte range deep inside one; HLS jumps to
     a small segment instead - measured at ~0.2s against ~20s for the same seek.
     It also carries the audio tracks separately, which is the only way Chrome
     can switch between them. The mp4 stays the download format. */
  const source = stream || options.url || urls.http;
  if (!source) return toast('Video link unavailable', 'err');
  closePlayer();

  const isHls = String(source).includes('.m3u8');
  const needsHlsJsForAudio = audios.length > 1 && !nativeAudioTracks;
  const useHlsJs = isHls && hlsSupported()
    && (needsHlsJsForAudio || !canPlayHlsNatively());
  let hls = null;

  const video = el('video', { controls: 'controls', playsinline: 'playsinline' });

  function startHls(url, resumeAt) {
    hls = new window.Hls({
      enableWorker: true,
      startFragPrefetch: true,      // fetch the next piece while this one plays
      maxBufferLength: 90,          // ride out a slow patch without stalling
      maxMaxBufferLength: 180,
      backBufferLength: 30,
      fragLoadingMaxRetry: 6,
      manifestLoadingMaxRetry: 4,
      levelLoadingMaxRetry: 4,
      // 0 means the beginning. Passing -1 lets the playlist decide, which is
      // how a fresh episode could open partway in.
      startPosition: resumeAt > 0 ? resumeAt : 0,
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    activeHls = hls;

    let recoveries = 0;
    hls.on(window.Hls.Events.ERROR, (_event, data) => {
      if (!data || !data.fatal) return;         // hls.js handles the small ones
      const kinds = window.Hls.ErrorTypes;
      if (recoveries < 3 && data.type === kinds.NETWORK_ERROR) {
        recoveries += 1;
        hls.startLoad();                        // usually just a lost segment
        return;
      }
      if (recoveries < 3 && data.type === kinds.MEDIA_ERROR) {
        recoveries += 1;
        hls.recoverMediaError();
        return;
      }
      showStall('Playback failed. Try reconnecting, or pick http in Settings.');
    });
    return hls;
  }

  if (useHlsJs) {
    startHls(source, resumeFrom(options.position, options.duration));
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
  } else {
    video.src = source;
    video.autoplay = true;
  }

  /* Swap the mp4 for the HLS stream so a different audio track can be chosen,
     keeping the position and whether it was playing. */
  function engageHlsForAudio(trackIndex) {
    if (!stream || !hlsSupported()) {
      toast('This film offers no switchable audio stream.', 'err');
      return false;
    }
    const at = video.currentTime;
    const wasPlaying = !video.paused;
    video.removeAttribute('src');
    video.load();
    startHls(stream, at);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
      if (hls.audioTracks && hls.audioTracks.length > trackIndex) {
        hls.audioTrack = trackIndex;
      }
      if (at > 1) video.currentTime = at;
      if (wasPlaying) video.play().catch(() => {});
    });
    return true;
  }

  for (const sub of options.subtitles || []) {
    video.appendChild(el('track', {
      kind: 'subtitles', label: sub.lang || 'sub', srclang: (sub.lang || 'ru').slice(0, 2), src: sub.url || sub.file,
    }));
  }

  const bannerText = el('span', {}, '');
  const retryButton = el('button', { class: 'btn small primary' }, '⟳ Reconnect');
  const banner = el('div', { class: 'player-stall', hidden: 'hidden' }, bannerText, retryButton);

  const overlay = el('div', { class: 'player-overlay' }, video, banner,
    el('div', { class: 'player-bar' },
      el('button', { class: 'btn ghost small', onclick: closePlayer }, '✕ Close'),
      el('div', {}, el('div', { class: 'player-title' }, options.title || ''),
        el('div', { class: 'player-sub' }, options.subtitle || '')),
      el('div', { style: 'margin-left:auto;display:flex;gap:8px;align-items:center' },
        audioSelector(video, options.audios, () => hls, engageHlsForAudio),
        el('button', { class: 'btn small', onclick: () => { video.currentTime = Math.max(0, video.currentTime - 30); } }, '↺ 30s'),
        el('button', { class: 'btn small', onclick: () => { video.currentTime += 30; } }, '30s ↻'),
        el('select', {
          class: 'btn small',
          onchange: (e) => { video.playbackRate = Number(e.target.value); },
        }, [0.75, 1, 1.25, 1.5, 2].map((rate) => el('option', { value: String(rate), selected: rate === 1 }, rate + '×'))))));

  $('#overlays').appendChild(overlay);

  video.addEventListener('loadedmetadata', () => {
    const at = resumeFrom(options.position, video.duration || options.duration);
    if (at > 0) {
      video.currentTime = at;
      toast(`Resuming from ${duration(at)}`);
    }
  });
  video.addEventListener('error', () => {
    const hls = String(options.url).includes('.m3u8');
    if (options.entryId) {
      return toast('Cannot read the file — it may have been deleted from disk.', 'err');
    }
    if (hls) {
      return toast('The browser could not open HLS. Switch stream type to http in Settings.', 'err');
    }
    showStall(navigator.onLine ? 'Playback interrupted.' : 'Internet connection lost.');
  });

  const save = () => {
    if (!video.currentTime) return;
    if (options.entryId) {
      local.post(`/library/${options.entryId}/progress`, {
        position: video.currentTime,
        watched: video.duration ? video.currentTime / video.duration > 0.92 : false,
        sync: navigator.onLine,
      }).catch(() => {});
    } else if (options.itemId && State.settings.sync_watching && navigator.onLine) {
      const params = { id: options.itemId, time: Math.round(video.currentTime), video: options.video || 1 };
      if (options.season) params.season = options.season;
      kp('watching/marktime', params).catch(() => {});
    }
  };
  playerTimer = setInterval(save, 20000);
  video.addEventListener('pause', save);
  overlay.addEventListener('remove', save);
  // --- live-stream recovery -------------------------------------------
  let lastPosition = resumeFrom(options.position, options.duration);
  let stallTimer = null;
  video.addEventListener('timeupdate', () => {
    if (video.currentTime) lastPosition = video.currentTime;
  });

  const reconnect = () => {
    const at = lastPosition;
    banner.hidden = true;
    if (hls) {
      hls.loadSource(source);
      hls.startLoad();
      video.play().catch(() => {});
      return;
    }
    video.src = source;
    video.load();
    video.addEventListener('loadedmetadata', () => {
      if (at > 1) video.currentTime = at;
      video.play().catch(() => {});
    }, { once: true });
  };
  retryButton.addEventListener('click', reconnect);

  const showStall = (message) => {
    bannerText.textContent = message;
    banner.hidden = false;
  };
  const clearStall = () => {
    banner.hidden = true;
    if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; }
  };
  const armStall = () => {
    if (options.entryId || stallTimer) return;   // local files never stall on network
    stallTimer = setTimeout(() => {
      stallTimer = null;
      if (!video.paused && video.readyState < 3) {
        showStall('Stream stalled — the connection may have dropped.');
      }
    }, 8000);
  };
  video.addEventListener('waiting', armStall);
  video.addEventListener('stalled', armStall);
  video.addEventListener('playing', clearStall);
  video.addEventListener('canplay', clearStall);

  // A pause issued while the opening autoplay promise is still settling can be
  // undone when that promise resolves, so confirm it actually took.
  const togglePlay = () => {
    if (video.paused) {
      video.play().catch(() => {});
      return;
    }
    video.pause();
    setTimeout(() => { if (!video.paused) video.pause(); }, 150);
  };

  // --- keyboard ---------------------------------------------------------
  document.addEventListener('keydown', escClose);
  playerKeys = (event) => {
    const tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || event.metaKey || event.ctrlKey) return;
    const key = event.key;
    const seek = (delta) => { video.currentTime = Math.max(0, video.currentTime + delta); };
    if (key === ' ' || key === 'k' || key === 'K') {
      event.preventDefault();
      togglePlay();
    } else if (key === 'ArrowRight' || key === 'l' || key === 'L') { event.preventDefault(); seek(10); }
    else if (key === 'ArrowLeft' || key === 'j' || key === 'J') { event.preventDefault(); seek(-10); }
    else if (key === 'ArrowUp') { event.preventDefault(); video.volume = Math.min(1, video.volume + 0.1); }
    else if (key === 'ArrowDown') { event.preventDefault(); video.volume = Math.max(0, video.volume - 0.1); }
    else if (key === 'm' || key === 'M') { video.muted = !video.muted; }
    else if (key === 'f' || key === 'F') {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (overlay.requestFullscreen) overlay.requestFullscreen().catch(() => {});
    } else if (key >= '0' && key <= '9' && video.duration) {
      video.currentTime = video.duration * (Number(key) / 10);
    }
  };
  document.addEventListener('keydown', playerKeys);
  activeHls = hls;
}

function escClose(event) { if (event.key === 'Escape') closePlayer(); }

function closePlayer() {
  if (activeHls) {
    try { activeHls.destroy(); } catch (_) { /* already gone */ }
    activeHls = null;
  }
  const overlay = $('.player-overlay');
  if (overlay) {
    const video = $('video', overlay);
    if (video && video.currentTime) video.dispatchEvent(new Event('pause'));
    overlay.remove();
  }
  if (playerTimer) { clearInterval(playerTimer); playerTimer = null; }
  document.removeEventListener('keydown', escClose);
  if (playerKeys) { document.removeEventListener('keydown', playerKeys); playerKeys = null; }
}

function revealButton(entryId, label) {
  return el('button', {
    class: 'btn small ghost',
    onclick: async () => {
      try { await local.post('/reveal', entryId ? { entry_id: entryId } : {}); }
      catch (error) { toast(error.message, 'err'); }
    },
  }, label || '📂 Reveal in Finder');
}

/* --------------------------------------------------------------- downloads */
async function refreshDownloads() {
  try {
    const data = await local.get('/downloads');
    State.downloads = data.entries || [];
    State.freeSpace = data.free_space || 0;
    State.activeDownloads = State.downloads.filter((e) => ['queued', 'downloading', 'paused', 'error'].includes(e.status)).length;
    renderNav();
    if ((location.hash || '').startsWith('#/downloads')) renderDownloads();
    if (repaintItemDownloads && (location.hash || '').startsWith('#/item/')) {
      repaintItemDownloads();
    }
  } catch (_) { /* ignore */ }
}

const STATE_LABEL = {
  queued: 'Queued', downloading: 'Downloading', paused: 'Paused',
  done: 'Done', error: 'Error',
};

const SPEED_CHOICES = [
  ['default', 'Speed: default'], ['0', 'Unlimited'], ['1', '1 MB/s'], ['2', '2 MB/s'],
  ['5', '5 MB/s'], ['10', '10 MB/s'], ['20', '20 MB/s'],
];

function speedPicker(entry) {
  const current = entry.speed_limit_mb === null || entry.speed_limit_mb === undefined
    ? 'default' : String(entry.speed_limit_mb);
  return el('select', {
    class: 'btn small',
    title: 'Speed limit for this download',
    onchange: async (event) => {
      try {
        await local.post(`/downloads/${entry.id}/limit`, { mb: event.target.value });
        toast(event.target.value === 'default'
          ? 'Using the default speed limit'
          : `Limit: ${event.target.value === '0' ? 'unlimited' : event.target.value + ' MB/s'}`, 'ok');
        refreshDownloads();
      } catch (error) { toast(error.message, 'err'); }
    },
  }, SPEED_CHOICES.map(([value, label]) =>
    el('option', { value, selected: value === current }, label)));
}

function downloadRow(entry) {
  const img = el('img', { src: entry.poster_file ? '/poster/' + entry.poster_file : PLACEHOLDER, alt: '' });
  img.addEventListener('error', () => { img.src = PLACEHOLDER; });

  const action = (name, label, cls, hint) => el('button', {
    class: 'btn small ' + (cls || ''),
    title: hint || '',
    onclick: async () => {
      await local.post(`/downloads/${entry.id}/${name}`);
      if (name === 'restart') toast('Downloading again from the start', 'ok');
      refreshDownloads();
    },
  }, label);

  const openItem = entry.item_id
    ? () => { location.hash = '#/item/' + entry.item_id; }
    : null;
  if (openItem) {
    img.style.cursor = 'pointer';
    img.title = 'Open on kino.pub';
    img.addEventListener('click', openItem);
  }

  const name = el('div', { class: 'dl-name' },
    openItem
      ? el('button', { class: 'linklike', type: 'button', onclick: openItem }, entry.title + ' ↗')
      : entry.title);
  const fill = el('i', {});
  const bar = el('div', { class: 'bar' }, fill);
  const meta = el('div', { class: 'dl-meta' });
  const actions = el('div', { class: 'dl-actions' });
  const row = el('div', { class: 'dl-row' }, img, el('div', {}, name, bar, meta), actions);

  let shownStatus = null;

  /* Called on every poll. Only the numbers are touched, so the page keeps its
     scroll position and an open speed dropdown is not torn out mid-choice. */
  function update(next) {
    entry = next || entry;
    const percent = Math.round((entry.progress || 0) * 100);
    fill.style.width = percent + '%';
    bar.className = 'bar ' + (entry.status === 'done' ? 'done' : entry.status === 'error' ? 'err' : '');

    meta.textContent = '';
    [
      el('span', { class: 'state ' + entry.status }, STATE_LABEL[entry.status] || entry.status),
      el('span', {}, `${percent}%`),
      entry.quality ? el('span', {}, entry.quality) : null,
      entry.total_bytes
        ? el('span', {}, `${bytes(entry.downloaded_bytes)} / ${bytes(entry.total_bytes)}`)
        : entry.downloaded_bytes ? el('span', {}, bytes(entry.downloaded_bytes)) : null,
      entry.status === 'downloading' && entry.speed ? el('span', {}, `${bytes(entry.speed)}/s`) : null,
      entry.status === 'downloading' && entry.eta ? el('span', {}, '' + eta(entry.eta) + ' left') : null,
      entry.error ? el('span', { style: 'color:var(--err)' }, entry.error) : null,
    ].forEach((node) => node && meta.appendChild(node));

    if (entry.status !== shownStatus) {        // buttons only change with status
      shownStatus = entry.status;
      actions.textContent = '';
      [
        entry.status === 'downloading' || entry.status === 'queued' ? action('pause', '❚❚') : null,
        entry.status === 'paused' ? action('resume', '▶︎') : null,
        entry.status === 'error' ? action('retry', '⟳') : null,
        entry.status !== 'done' ? action('restart', '↻', '', 'Start this download over from zero') : null,
        entry.status !== 'done' ? speedPicker(entry) : null,
        entry.status === 'done'
          ? el('button', { class: 'btn small primary', onclick: () => playLibraryEntry(entry) }, '▶︎')
          : null,
        action('delete', '🗑', 'danger'),
      ].forEach((node) => node && actions.appendChild(node));
    }
  }

  update(entry);
  row.update = update;
  return row;
}

let downloadRows = new Map();
let downloadLayout = '';

function downloadsLayoutKey(active, done) {
  return active.map((e) => e.id).join(',') + '|' + done.map((e) => e.id).join(',');
}

function renderDownloads(options) {
  const active = State.downloads.filter((e) => e.status !== 'done');
  const done = State.downloads.filter((e) => e.status === 'done').slice(0, 40);
  const layout = downloadsLayoutKey(active, done);

  /* Nothing appeared or finished, so keep the page exactly as it is and let
     each row refresh its own numbers - otherwise a poll every two seconds
     would throw you back to the top of the list mid-scroll. */
  if (!options || !options.rebuild) {
    if (layout === downloadLayout && downloadRows.size) {
      for (const entry of State.downloads) {
        const row = downloadRows.get(entry.id);
        if (row) row.update(entry);
      }
      const note = $('#free-space-note');
      if (note) note.textContent = freeSpaceNote();
      return;
    }
  }

  downloadRows = new Map();
  const build = (entry) => {
    const row = downloadRow(entry);
    downloadRows.set(entry.id, row);
    return row;
  };

  // arriving on the page starts at the top; a list that changed under you
  // (something finished) is rebuilt where you were reading
  const arrivedFresh = !!(options && options.rebuild);
  const hadRows = !!downloadLayout;
  downloadLayout = layout;
  const render = (!arrivedFresh && hadRows) ? setViewKeepingScroll : setView;
  render(
    el('h1', {}, 'Downloads'),
    el('p', { class: 'subtitle', id: 'free-space-note' }, freeSpaceNote()),
    el('div', { class: 'actions', style: 'margin-top:0' }, revealButton(null, '📂 Open folder')),
    active.length ? el('div', {}, active.map(build)) : empty('✅', 'No active downloads',
      'Open a film and press Download.'),
    done.length ? el('div', {}, el('h2', {}, 'Completed'), done.map(build)) : null);
}

function freeSpaceNote() {
  return `Folder: ${State.settings.library_dir || ''}`
    + (State.freeSpace ? ` · ${bytes(State.freeSpace)} free` : '');
}

function viewDownloads() {
  downloadLayout = '';                 // arriving fresh: full render, top of page
  downloadRows = new Map();
  renderDownloads({ rebuild: true });
  refreshDownloads();
}

/* ----------------------------------------------------------------- library */
function playLibraryEntry(entry) {
  const subtitles = (entry.subtitles || []).map((sub) => ({
    lang: sub.lang,
    url: '/media/' + entry.rel_path.split('/').slice(0, -1).concat(sub.file).map(encodeURIComponent).join('/'),
  }));
  openPlayer({
    url: '/media/' + entry.rel_path.split('/').map(encodeURIComponent).join('/'),
    title: entry.show_title || entry.title,
    subtitle: [entry.season ? `S${entry.season}E${entry.episode}` : null, entry.quality, 'offline'].filter(Boolean).join(' · '),
    position: entry.position || 0,
    duration: entry.duration,
    entryId: entry.id,
    subtitles,
  });
}

async function viewLibrary() {
  loading();
  try {
    const data = await local.get('/library');
    const entries = data.entries || [];
    if (!entries.length) {
      return setView(el('h1', {}, 'Offline library'),
        empty('✈️', 'Nothing downloaded yet',
          'Download films ahead of time — they play with no internet on the plane.',
          el('button', { class: 'btn primary', onclick: () => { location.hash = '#/home'; } }, 'Browse catalog')));
    }
    const groups = new Map();
    for (const entry of entries) {
      const key = entry.show_title || entry.title;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entry);
    }
    const totalSize = entries.reduce((sum, entry) => sum + (entry.total_bytes || 0), 0);
    const blocks = [];
    for (const [name, list] of groups) {
      const first = list[0];
      if (list.length === 1) {
        blocks.push(card({
          id: first.item_id, title: first.title, year: first.year,
          posters: { medium: first.poster_file ? '/poster/' + first.poster_file : '' },
        }, {
          badge: first.quality,
          meta: bytes(first.total_bytes),
          progress: first.position && first.duration ? Math.min(first.position / first.duration, 1) : 0,
          onclick: () => playLibraryEntry(first),
          corner: first.item_id ? {
            label: '↗',
            title: first.season ? 'Open the series — all episodes' : 'Open on kino.pub',
            onclick: () => { location.hash = '#/item/' + first.item_id; },
          } : null,
        }));
      } else {
        blocks.push(card({
          id: first.item_id, title: name, year: first.year,
          posters: { medium: first.poster_file ? '/poster/' + first.poster_file : '' },
        }, {
          badge: `${list.length} ${list.length === 1 ? 'ep' : 'eps'}`,
          meta: bytes(list.reduce((sum, e) => sum + (e.total_bytes || 0), 0)),
          onclick: () => showLibraryGroup(name, list),
          corner: first.item_id ? {
            label: '↗',
            title: 'Open the series — all episodes',
            onclick: () => { location.hash = '#/item/' + first.item_id; },
          } : null,
        }));
      }
    }
    setView(el('h1', {}, 'Offline library'),
      el('p', { class: 'subtitle' }, `${entries.length} ${entries.length === 1 ? 'file' : 'files'} · ${bytes(totalSize)} · ${data.library_dir}`),
      el('div', { class: 'actions', style: 'margin-top:0;margin-bottom:18px' }, revealButton(null, '📂 Open folder')),
      el('div', { class: 'grid' }, blocks));
  } catch (error) { setView(errorView(error)); }
}

function showLibraryGroup(name, list) {
  const itemId = (list.find((entry) => entry.item_id) || {}).item_id;
  const backdrop = el('div', { class: 'modal-backdrop', onclick: (e) => { if (e.target === backdrop) backdrop.remove(); } },
    el('div', { class: 'modal' }, el('h3', {}, name),
      el('p', { class: 'card-meta', style: 'margin:-6px 0 12px' },
        `${list.length} downloaded${itemId ? ' — open the series for every episode' : ''}`),
      el('div', { class: 'episodes' }, list.map((entry) =>
        el('div', { class: 'episode' },
          el('span', { class: 'num' }, entry.season ? `${entry.season}×${String(entry.episode).padStart(2, '0')}` : '▶︎'),
          el('span', { class: 'name' }, entry.episode_title || entry.title),
          entry.available === false ? el('span', { class: 'dur', style: 'color:var(--err)' }, 'file deleted') : null,
          el('button', { class: 'btn small primary', onclick: () => { backdrop.remove(); playLibraryEntry(entry); } }, '▶︎'),
          el('button', {
            class: 'btn small danger',
            onclick: async () => { await local.post(`/downloads/${entry.id}/delete`); backdrop.remove(); viewLibrary(); },
          }, '🗑')))),
      el('div', { class: 'actions' },
        itemId ? el('button', {
          class: 'btn primary',
          onclick: () => { backdrop.remove(); location.hash = '#/item/' + itemId; },
        }, 'All episodes →') : null,
        el('button', { class: 'btn ghost', onclick: () => backdrop.remove() }, 'Close'))));
  $('#overlays').appendChild(backdrop);
}

/* ---------------------------------------------------------------- settings */
function field(label, input, hint) {
  return el('div', { class: 'field' }, el('label', {}, label), input, hint ? el('div', { class: 'hint' }, hint) : null);
}

async function viewSettings() {
  const settings = State.settings;
  const clientId = el('input', { type: 'text', value: settings.client_id || '', placeholder: 'client_id from kino.pub' });
  const clientSecret = el('input', { type: 'password', placeholder: settings.client_secret_set ? '•••••••• (saved)' : 'client_secret' });
  const libraryDir = el('input', { type: 'text', value: settings.library_dir || '' });
  const quality = el('select', {}, QUALITY_ORDER.map((value) =>
    el('option', { value, selected: value === settings.preferred_quality }, value)));
  const streamType = el('select', {}, [
    ['http', 'http — direct MP4 (recommended)'],
    ['hls4', 'hls4'], ['hls2', 'hls2'], ['hls', 'hls'],
  ].map(([value, label]) => el('option', { value, selected: value === settings.stream_type }, label)));
  const parallel = el('select', {}, [1, 2, 3, 4].map((value) =>
    el('option', { value: String(value), selected: value === settings.max_parallel_downloads }, String(value))));
  const perSpeed = el('input', { type: 'number', min: '0', step: '0.5',
    value: String(settings.speed_limit_mb || 0) });
  const totalSpeed = el('input', { type: 'number', min: '0', step: '0.5',
    value: String(settings.speed_limit_total_mb || 0) });
  const subs = el('input', { type: 'checkbox', checked: settings.download_subtitles ? 'checked' : null });
  const sync = el('input', { type: 'checkbox', checked: settings.sync_watching ? 'checked' : null });

  const save = async () => {
    const patch = {
      client_id: clientId.value.trim(),
      library_dir: libraryDir.value.trim(),
      preferred_quality: quality.value,
      stream_type: streamType.value,
      max_parallel_downloads: Number(parallel.value),
      speed_limit_mb: Number(perSpeed.value) || 0,
      speed_limit_total_mb: Number(totalSpeed.value) || 0,
      download_subtitles: subs.checked,
      sync_watching: sync.checked,
    };
    if (clientSecret.value.trim()) patch.client_secret = clientSecret.value.trim();
    try {
      const result = await local.post('/settings', patch);
      State.settings = result.settings;
      await bootState();
      toast('Settings saved', 'ok');
    } catch (error) { toast(error.message, 'err'); }
  };

  setView(
    el('h1', {}, 'Settings'),
    el('p', { class: 'subtitle' }, 'Stored locally in ~/.config/kinopub-offline'),
    el('div', { class: 'form' },
      field('client_id', clientId, 'Issued by kino.pub support (support@kino.pub) for the OAuth device flow.'),
      field('client_secret', clientSecret, 'Leave empty to keep the saved value.'),
      field('Download folder', libraryDir, 'Files are saved as: Title (Year)/Title - S01E01 [1080p].mp4'),
      field('Default quality', quality),
      field('Download format', streamType, 'http saves the ready-made mp4 and resumes cleanly. HLS is rebuilt with ffmpeg' + (State.ffmpeg ? ' (found ✓)' : ' (not found ✗)')),
      field('Parallel downloads', parallel),
      field('Speed limit per download', perSpeed, '0 = unlimited. Pacing can help: some CDNs throttle or drop a connection that pulls flat out.'),
      field('Speed limit for all downloads', totalSpeed, '0 = unlimited. Applies across everything downloading at once.'),
      el('label', { class: 'checkbox' }, subs, 'Download subtitles (converted to .vtt)'),
      el('label', { class: 'checkbox' }, sync, 'Sync playback position with kino.pub'),
      el('div', { class: 'actions' },
        el('button', { class: 'btn primary', onclick: save }, 'Save'),
        State.authorized ? el('button', {
          class: 'btn danger',
          onclick: async () => { await local.post('/auth/logout'); await bootState(); toast('Signed out'); location.hash = '#/auth'; },
        }, 'Sign out') : el('button', { class: 'btn', onclick: () => { location.hash = '#/auth'; } }, 'Sign in'))));
}

/* -------------------------------------------------------------------- auth */
let authPoll = null;

async function viewAuth() {
  if (authPoll) { clearInterval(authPoll); authPoll = null; }
  if (!State.hasCredentials) {
    return setView(el('h1', {}, 'Sign in'),
      el('div', { class: 'banner' }, 'Enter client_id and client_secret in Settings first.'),
      empty('🔑', 'No app credentials',
        'client_id / client_secret are issued by kino.pub support (support@kino.pub).',
        el('button', { class: 'btn primary', onclick: () => { location.hash = '#/settings'; } }, 'Open Settings')));
  }
  loading();
  try {
    const device = await local.post('/auth/start');
    const statusLine = el('p', { class: 'subtitle' }, 'Waiting for confirmation…');
    setView(el('h1', {}, 'Link this device'),
      el('p', { class: 'subtitle' }, 'Open the page and enter this code:'),
      el('div', { class: 'code-box' }, device.user_code || '——————'),
      el('div', { class: 'actions' },
        el('a', { class: 'btn primary', href: device.verification_uri, target: '_blank', rel: 'noopener' },
          '↗︎ ' + (device.verification_uri || 'kino.pub/device')),
        el('button', { class: 'btn ghost', onclick: viewAuth }, 'New code')),
      statusLine);

    authPoll = setInterval(async () => {
      try {
        const result = await local.post('/auth/poll');
        if (result.authorized) {
          clearInterval(authPoll); authPoll = null;
          await bootState();
          toast('Device authorised', 'ok');
          location.hash = '#/home';
        }
      } catch (error) {
        clearInterval(authPoll); authPoll = null;
        statusLine.textContent = 'Error: ' + error.message;
        statusLine.style.color = 'var(--err)';
      }
    }, Math.max((device.interval || 5) * 1000, 3000));
  } catch (error) {
    setView(el('h1', {}, 'Sign in'), el('div', { class: 'banner err' }, error.message),
      el('button', { class: 'btn', onclick: viewAuth }, 'Retry'));
  }
}

/* ------------------------------------------------------------------ router */
function parseHash() {
  const raw = (location.hash || '#/home').slice(1);
  const [path, search] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  const query = Object.fromEntries(new URLSearchParams(search || ''));
  return { parts, query };
}

async function route() {
  if (authPoll && !location.hash.startsWith('#/auth')) { clearInterval(authPoll); authPoll = null; }
  closePlayer();
  renderNav();
  repaintItemDownloads = null;
  const { parts, query } = parseHash();
  const section = parts[0] || 'home';

  const needsAuth = !['settings', 'auth', 'library', 'downloads'].includes(section);
  if (needsAuth && !State.authorized) return viewAuth();

  switch (section) {
    case 'home': return viewHome();
    case 'catalog': return viewCatalog(parts[1] || 'movie', query);
    case 'search': return viewSearch(query);
    case 'item': return viewItem(parts[1]);
    case 'collections': return viewCollections();
    case 'collection': return viewCollection(parts[1]);
    case 'bookmarks': return viewBookmarks(parts[1]);
    case 'watching': return viewWatching();
    case 'downloads': return viewDownloads();
    case 'library': return viewLibrary();
    case 'settings': return viewSettings();
    case 'auth': return viewAuth();
    default: return setView(empty('🤔', 'Page not found'));
  }
}

/* -------------------------------------------------------------------- boot */
async function bootState() {
  try {
    const data = await local.get('/state');
    State.authorized = data.authorized;
    State.hasCredentials = data.has_credentials;
    State.settings = data.settings || {};
    State.ffmpeg = data.ffmpeg;
  } catch (_) { /* server down */ }
  renderStatus();
  renderNav();
}

/** Decide which screen to open on launch. Returns the hash it chose. */
async function chooseStartRoute() {
  const openable = ['#/settings', '#/auth', '#/library', '#/downloads'];
  const current = location.hash || '';
  const atStart = !current || current === '#/' || current === '#/home';
  if (!navigator.onLine && atStart) {
    // no network on launch: the downloaded shelf is the only useful screen
    const library = await local.get('/library').catch(() => ({ entries: [] }));
    if ((library.entries || []).length) {
      location.hash = '#/library';
      return '#/library';
    }
  }
  if (!State.hasCredentials && !openable.some((prefix) => current.startsWith(prefix))) {
    location.hash = '#/settings';
    return '#/settings';
  }
  return current || '#/home';
}

function wireSearch() {
  const input = $('#search-input');
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const value = input.value.trim();
      if (value.length >= 3) location.hash = '#/search?q=' + encodeURIComponent(value);
    }, 450);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && input.value.trim().length >= 3) {
      location.hash = '#/search?q=' + encodeURIComponent(input.value.trim());
    }
  });
}

window.addEventListener('hashchange', route);
window.addEventListener('online', () => { State.apiReachable = true; renderStatus(); });
window.addEventListener('offline', renderStatus);

(async function main() {
  wireSearch();
  await bootState();
  await chooseStartRoute();
  await route();
  refreshDownloads();
  setInterval(refreshDownloads, 2000);
})();
