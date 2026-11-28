/**
 * ZEMYIPTV IPTV Studio — Frontend Application Logic (v3)
 * - Safe event delegation (zero inline quote-escaping bugs)
 * - Global try/catch error handling on all buttons
 * - Bulk Find Source by Channel Name List (up to 100 channels) on BOTH Main Panel & Non-Working Channels Center
 */

const state = {
  playlists: [],
  currentPlaylistId: null,
  currentPlaylist: null,
  channels: [],
  selectedCategory: 'ALL',
  searchQuery: '',
  statusFilter: 'all',
  viewMode: 'grid',
  selectedChannelIds: new Set(),
  importPreviewChannels: [],
  xtreamChannels: [],
  implantPreviewChannels: [],
  bulkSourceMainResults: [],
  bulkSourceBrokenResults: [],
  networkInfo: null,
  schedulerData: null,
  hlsInstance: null,
};

function makeFallbackLogo(name = 'TV') {
  const clean = (name || 'TV').replace(/\[.*?\]|\(.*?\)/g, '').trim();
  const words = clean.split(/[\s\-_|]+/).filter(Boolean);
  let initials = words.length >= 2
    ? (words[0][0] + words[1][0]).toUpperCase()
    : (words[0] || 'TV').slice(0, 3).toUpperCase();
  initials = initials.replace(/[^A-Z0-9]/g, '') || 'TV';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120">
    <defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#3b82f6"/><stop offset="100%" stop-color="#1d4ed8"/>
    </linearGradient></defs>
    <rect width="120" height="120" rx="26" fill="#0b1120"/>
    <rect x="4" y="4" width="112" height="112" rx="22" fill="url(#g)" fill-opacity="0.2" stroke="url(#g)" stroke-width="2.5"/>
    <text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" fill="#e2e8f0" font-family="Inter,sans-serif" font-weight="800" font-size="36">${initials}</text>
  </svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

function showToast(msg) {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'zemy-admin' },
    ...options,
  });
  if (res.status === 401) {
    window.location.href = '/admin';
    throw new Error('Signed out');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatTimeAgo(unixSec) {
  if (!unixSec) return 'Never';
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - unixSec);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(unixSec * 1000).toLocaleString();
}

function formatTimeUntil(unixSec) {
  if (!unixSec) return '—';
  const diff = Math.max(0, unixSec - Math.floor(Date.now() / 1000));
  if (diff < 60) return `in ${diff}s`;
  if (diff < 3600) return `in ${Math.floor(diff / 60)}m`;
  return `in ${(diff / 3600).toFixed(1)}h`;
}

// ---------------------------------------------------------------------------
// Initialization, Network Info & Scheduler Polling
// ---------------------------------------------------------------------------

async function loadNetworkInfo() {
  try {
    state.networkInfo = await api('/api/network-info');
    updateUrlBoxes();
  } catch (_) {}
}

function updateUrlBoxes() {
  const pid = state.currentPlaylistId || 'starter-global-hd';
  const net = state.networkInfo;
  if (!net) return;

  const shareKey = ((state.playlists || []).find(p => p.id === pid) || {}).share_key || '';
  const keyQuery = shareKey ? `?key=${shareKey}` : '';
  const lanM3u = `${net.lan_url}/m3u/${pid}.m3u${keyQuery}`;
  const pubM3u = net.public_url ? `${net.public_url}/m3u/${pid}.m3u${keyQuery}` : '';

  document.getElementById('lanPlaylistUrl').value = lanM3u;
  document.getElementById('modalLanPlaylistUrl').value = lanM3u;

  if (pubM3u) {
    document.getElementById('livePlaylistUrl').value = pubM3u;
    document.getElementById('modalPublicPlaylistUrl').value = pubM3u;
    document.getElementById('liveUrlModeTitle').textContent = '🌍 Public Internet M3U URL';
    document.getElementById('publicUrlHint').textContent = 'Accessible worldwide via HTTPS + on Home Wi-Fi';
    document.getElementById('modalTunnelStatusBadge').textContent = `Online (${net.tunnel_provider})`;
    document.getElementById('modalTunnelStatusBadge').className = 'badge badge-emerald';
  } else {
    document.getElementById('livePlaylistUrl').value = lanM3u;
    document.getElementById('modalPublicPlaylistUrl').value =
      net.tunnel_status === 'starting'
        ? 'Connecting public HTTPS tunnel...'
        : 'Tunnel stopped — Click Reconnect below or use LAN URL';
    document.getElementById('liveUrlModeTitle').textContent = '🏠 Home Wi-Fi / LAN M3U URL';
    document.getElementById('modalTunnelStatusBadge').textContent = net.tunnel_status;
    document.getElementById('modalTunnelStatusBadge').className = 'badge badge-amber';
  }

  if (net.custom_base_url) {
    document.getElementById('customBaseUrlInput').value = net.custom_base_url;
  }
}

async function loadSchedulerStatus() {
  try {
    const data = await api('/api/scheduler/status');
    state.schedulerData = data;
    const sched = data.scheduler || {};
    const broken = data.broken_channels || [];

    const sidebarBadge = document.getElementById('sidebarBrokenBadge');
    sidebarBadge.textContent = `${broken.length} Non-Working`;
    sidebarBadge.classList.toggle('all-good', broken.length === 0);

    const cpd = sched.checks_per_day || 12;
    const freqDesc = cpd === 12 ? '2-Hour Fallback Guard' : `Auto-check ${cpd}x/day`;
    const failovers = sched.total_failovers || 0;
    document.getElementById('sidebarSchedulerSub').textContent =
      `${freqDesc} · ${failovers} Failover(s)`;
  } catch (_) {}
}

async function loadPlaylists(selectId = null) {
  try {
    const data = await api('/api/playlists');
    state.playlists = data.playlists || [];

    if (selectId) {
      state.currentPlaylistId = selectId;
    } else if (!state.currentPlaylistId && state.playlists.length > 0) {
      state.currentPlaylistId = state.playlists[0].id;
    } else if (
      state.currentPlaylistId &&
      !state.playlists.some((p) => p.id === state.currentPlaylistId)
    ) {
      state.currentPlaylistId = state.playlists[0]?.id || null;
    }

    renderPlaylistsSidebar();
    if (state.currentPlaylistId) {
      await loadCurrentPlaylistDetails();
    }
    await loadNetworkInfo();
    await loadSchedulerStatus();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function loadCurrentPlaylistDetails() {
  if (!state.currentPlaylistId) return;
  try {
    const data = await api(`/api/playlists/${state.currentPlaylistId}`);
    state.currentPlaylist = data.playlist;
    state.channels = data.channels || [];

    const validIds = new Set(state.channels.map((c) => c.id));
    for (const id of Array.from(state.selectedChannelIds)) {
      if (!validIds.has(id)) state.selectedChannelIds.delete(id);
    }

    renderHeroAndStats();
    renderCategoriesSidebar();
    renderChannels();
    updateBulkBar();
    updateUrlBoxes();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderPlaylistsSidebar() {
  const list = document.getElementById('playlistList');
  list.innerHTML = state.playlists
    .map((pl) => {
      const active = pl.id === state.currentPlaylistId ? 'active' : '';
      return `
        <div class="nav-item ${active}" data-playlist-id="${escapeHtml(pl.id)}">
          <div class="nav-item-title">
            <span>📺</span>
            <span>${escapeHtml(pl.name)}</span>
          </div>
          <span class="nav-count">${pl.channel_count || 0}</span>
        </div>
      `;
    })
    .join('');

  list.querySelectorAll('.nav-item').forEach((el) => {
    el.addEventListener('click', () => {
      const pid = el.getAttribute('data-playlist-id');
      if (pid !== state.currentPlaylistId) {
        state.currentPlaylistId = pid;
        state.selectedCategory = 'ALL';
        state.selectedChannelIds.clear();
        renderPlaylistsSidebar();
        loadCurrentPlaylistDetails();
      }
    });
  });
}

function getCategoryCounts() {
  const map = new Map();
  for (const ch of state.channels) {
    const cat = ch.category || 'Uncategorized';
    map.set(cat, (map.get(cat) || 0) + 1);
  }
  return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
}

function renderCategoriesSidebar() {
  const catEntries = getCategoryCounts();
  const list = document.getElementById('categoryList');
  const brokenCount = state.channels.filter((c) => c.status === 'offline' || c.status === 'warning').length;

  let html = `
    <div class="nav-item ${state.selectedCategory === 'ALL' && state.statusFilter === 'all' ? 'active' : ''}" data-cat="ALL">
      <div class="nav-item-title"><span>🌐</span><span>All Channels</span></div>
      <span class="nav-count">${state.channels.length}</span>
    </div>
    <div class="nav-item ${state.statusFilter === 'offline' ? 'active' : ''}" data-cat="BROKEN_FILTER">
      <div class="nav-item-title"><span style="color:#fca5a5;">🚨</span><span>Non-Working Channels</span></div>
      <span class="nav-count" style="color:#fca5a5;">${brokenCount}</span>
    </div>
  `;

  for (const [cat, count] of catEntries) {
    const active = state.selectedCategory === cat && state.statusFilter !== 'offline' ? 'active' : '';
    html += `
      <div class="nav-item ${active}" data-cat="${escapeHtml(cat)}">
        <div class="nav-item-title"><span>📁</span><span>${escapeHtml(cat)}</span></div>
        <span class="nav-count">${count}</span>
      </div>
    `;
  }

  list.innerHTML = html;

  const datalist = document.getElementById('categoryDatalist');
  datalist.innerHTML = catEntries
    .map(([cat]) => `<option value="${escapeHtml(cat)}"></option>`)
    .join('');

  list.querySelectorAll('.nav-item').forEach((el) => {
    el.addEventListener('click', () => {
      const cat = el.getAttribute('data-cat');
      if (cat === 'BROKEN_FILTER') {
        state.selectedCategory = 'ALL';
        state.statusFilter = 'offline';
        document.getElementById('statusFilter').value = 'offline';
      } else {
        state.selectedCategory = cat;
        if (state.statusFilter === 'offline') {
          state.statusFilter = 'all';
          document.getElementById('statusFilter').value = 'all';
        }
      }
      renderCategoriesSidebar();
      renderChannels();
    });
  });
}

function renderHeroAndStats() {
  const pl = state.currentPlaylist;
  if (!pl) return;

  document.getElementById('currentPlaylistName').textContent = pl.name;
  document.getElementById('currentPlaylistDesc').textContent =
    pl.description || 'Custom IPTV M3U/M3U8 playlist';

  const total = state.channels.length;
  const catCount = getCategoryCounts().length;
  const withLogo = state.channels.filter((c) => c.logo && c.logo.trim() !== '').length;
  const onlineCount = state.channels.filter((c) => c.status === 'online').length;
  const brokenCount = state.channels.filter((c) => c.status === 'offline' || c.status === 'warning').length;
  const logoPct = total > 0 ? Math.round((withLogo / total) * 100) : 0;

  document.getElementById('statChannels').textContent = `${total} Channels`;
  document.getElementById('statCategories').textContent = `${catCount} Categories`;
  document.getElementById('statLogos').textContent = `${logoPct}% Logos (${withLogo}/${total})`;
  document.getElementById('statOnline').textContent = `${onlineCount} Online`;
  document.getElementById('statBrokenBadge').textContent = `🚨 ${brokenCount} Non-Working`;
  document.getElementById('toolbarBrokenCount').textContent = String(brokenCount);
}

function getFilteredChannels() {
  return state.channels.filter((ch) => {
    if (state.selectedCategory !== 'ALL' && (ch.category || 'Uncategorized') !== state.selectedCategory) {
      return false;
    }
    if (state.statusFilter === 'missing_logo' && ch.logo) return false;
    if (state.statusFilter === 'has_logo' && !ch.logo) return false;
    if (state.statusFilter === 'online' && ch.status !== 'online') return false;
    if (state.statusFilter === 'offline' && ch.status !== 'offline' && ch.status !== 'warning') return false;

    if (state.searchQuery) {
      const q = state.searchQuery.toLowerCase();
      const hay = `${ch.name} ${ch.category} ${ch.tvg_id} ${ch.url}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderStatusBadge(ch) {
  if (ch.status === 'online') {
    return `<span class="status-dot status-online">● Online ${ch.latency_ms ? ch.latency_ms + 'ms' : ''}</span>`;
  }
  if (ch.status === 'offline') {
    return `<span class="status-dot status-offline" title="${escapeHtml(ch.last_error || 'Stream unreachable')}">● Offline ${ch.last_error ? '(' + escapeHtml(ch.last_error) + ')' : ''}</span>`;
  }
  if (ch.status === 'warning') {
    return `<span class="status-dot status-warning" title="${escapeHtml(ch.last_error || '')}">● Warning ${escapeHtml(ch.last_error || '')}</span>`;
  }
  return '';
}

function renderChannels() {
  const container = document.getElementById('channelsContainer');
  const filtered = getFilteredChannels();

  if (filtered.length === 0) {
    container.className = 'channels-grid';
    container.innerHTML = `
      <div class="empty-state">
        <h3>No channels match this view</h3>
        <p>Add a channel, find sources for up to 100 channel names, import from Xtream Codes, or implant a batch of <code>.m3u8</code> links.</p>
        <div style="display:flex; gap:10px; justify-content:center; flex-wrap:wrap;">
          <button class="btn btn-source-finder" onclick="openBulkSourceFinderModal()">🔎 Find Source (1–100 Names)</button>
          <button class="btn btn-xtream" onclick="openModal('xtreamModal')">🔐 Xtream Codes Import</button>
          <button class="btn btn-implant" onclick="openModal('bulkImplantModal')">⚡ Bulk .m3u8 Implant</button>
        </div>
      </div>
    `;
    return;
  }

  if (state.viewMode === 'table') {
    container.className = '';
    container.innerHTML = `
      <table class="channels-table">
        <thead>
          <tr>
            <th width="36"><input type="checkbox" id="tableSelectAll" /></th>
            <th width="52">Logo</th>
            <th width="65">#</th>
            <th>Channel Name</th>
            <th>Category</th>
            <th>Stream (.m3u8)</th>
            <th>Status</th>
            <th width="270">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${filtered
            .map((ch) => {
              const checked = state.selectedChannelIds.has(ch.id) ? 'checked' : '';
              const fallback = makeFallbackLogo(ch.name);
              const logoSrc = ch.logo || fallback;
              return `
                <tr data-id="${escapeHtml(ch.id)}">
                  <td><input type="checkbox" class="row-check card-check" data-id="${escapeHtml(ch.id)}" ${checked} /></td>
                  <td>
                    <img class="table-logo ch-img-el" src="${escapeHtml(logoSrc)}" data-fallback="${escapeHtml(fallback)}" data-edit-logo-id="${escapeHtml(ch.id)}" style="cursor:pointer;" />
                  </td>
                  <td><span class="ch-chno">${escapeHtml(ch.chno || '-')}</span></td>
                  <td><strong>${escapeHtml(ch.name)}</strong></td>
                  <td><span class="ch-cat-pill">${escapeHtml(ch.category || 'Uncategorized')}</span></td>
                  <td><div class="ch-url-preview" style="max-width:210px;">${escapeHtml(ch.url)}</div></td>
                  <td>
                    ${renderStatusBadge(ch) || '<small style="color:#64748b;">Unchecked</small>'}
                    ${ch.backup_count > 0 ? `<span class="badge badge-indigo" style="font-size:0.7rem; margin-left:4px;" title="${ch.backup_count} backup stream sources">🛡️ ${ch.backup_count}</span>` : ''}
                  </td>
                  <td>
                    <div class="card-btn-group">
                      <button class="card-btn play-btn" data-act="play" data-id="${escapeHtml(ch.id)}">▶ Play</button>
                      <button class="card-btn" data-act="source" data-id="${escapeHtml(ch.id)}" title="Auto-find stream source by name">🔎 Source</button>
                      <button class="card-btn" data-act="edit" data-id="${escapeHtml(ch.id)}">✏️ Edit</button>
                      <button class="card-btn del-btn" data-act="delete" data-id="${escapeHtml(ch.id)}">🗑️</button>
                    </div>
                  </td>
                </tr>
              `;
            })
            .join('')}
        </tbody>
      </table>
    `;

    const tblAll = document.getElementById('tableSelectAll');
    if (tblAll) {
      tblAll.checked = filtered.every((c) => state.selectedChannelIds.has(c.id));
      tblAll.addEventListener('change', (e) => toggleSelectAllFiltered(e.target.checked));
    }
  } else {
    container.className = 'channels-grid';
    container.innerHTML = filtered
      .map((ch) => {
        const isSelected = state.selectedChannelIds.has(ch.id);
        const fallback = makeFallbackLogo(ch.name);
        const logoSrc = ch.logo || fallback;
        return `
          <div class="channel-card ${isSelected ? 'selected' : ''}" data-id="${escapeHtml(ch.id)}">
            <div class="card-top">
              <input type="checkbox" class="row-check card-check" data-id="${escapeHtml(ch.id)}" ${isSelected ? 'checked' : ''} />
              <div class="ch-logo-wrap" data-edit-logo-id="${escapeHtml(ch.id)}" title="Click to change or Auto-Find Logo">
                <img class="ch-img-el" src="${escapeHtml(logoSrc)}" alt="${escapeHtml(ch.name)}" data-fallback="${escapeHtml(fallback)}" />
                <div class="ch-logo-overlay">✨ Change<br/>Logo</div>
              </div>
              <div class="ch-main-info">
                <div class="ch-title-row">
                  ${ch.chno ? `<span class="ch-chno">#${escapeHtml(ch.chno)}</span>` : ''}
                  <div class="ch-name" title="${escapeHtml(ch.name)}">${escapeHtml(ch.name)}</div>
                </div>
                <div class="ch-meta-row">
                  <span class="ch-cat-pill">📁 ${escapeHtml(ch.category || 'Uncategorized')}</span>
                  ${renderStatusBadge(ch)}
                  ${ch.backup_count > 0 ? `<span class="badge badge-indigo" style="font-size:0.7rem;" title="${ch.backup_count} backup stream sources">🛡️ ${ch.backup_count} Backups</span>` : ''}
                </div>
              </div>
            </div>

            <div class="ch-url-preview" title="${escapeHtml(ch.url)}">${escapeHtml(ch.url)}</div>

            <div class="card-actions">
              <div class="card-btn-group">
                <button class="card-btn play-btn" data-act="play" data-id="${escapeHtml(ch.id)}" title="Watch live .m3u8 stream">▶ Play</button>
                <button class="card-btn" data-act="source" data-id="${escapeHtml(ch.id)}" title="Auto-find working .m3u8 source by name">🔎 Source</button>
                <button class="card-btn" data-act="edit" data-id="${escapeHtml(ch.id)}" title="Rename channel, change logo, edit URL">✏️ Edit</button>
                <button class="card-btn" data-act="autologo" data-id="${escapeHtml(ch.id)}" title="Instant Auto-Find Logo">✨ Logo</button>
              </div>
              <div class="card-btn-group">
                <button class="card-btn" data-act="uncat" data-id="${escapeHtml(ch.id)}" title="Remove from Category">🏷️</button>
                <button class="card-btn del-btn" data-act="delete" data-id="${escapeHtml(ch.id)}" title="Remove channel from playlist">🗑️</button>
              </div>
            </div>
          </div>
        `;
      })
      .join('');
  }

  // Bind image fallback without inline JS quotes
  container.querySelectorAll('.ch-img-el').forEach((img) => {
    img.addEventListener('error', function () {
      const fb = this.getAttribute('data-fallback');
      if (fb && this.src !== fb) this.src = fb;
    });
  });

  container.querySelectorAll('[data-edit-logo-id]').forEach((el) => {
    el.addEventListener('click', () => {
      openEditChannelModal(el.getAttribute('data-edit-logo-id'), true);
    });
  });

  container.querySelectorAll('[data-act]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const act = btn.getAttribute('data-act');
      const id = btn.getAttribute('data-id');
      if (act === 'play') playChannel(id);
      else if (act === 'source') openSourceFinderForChannel(id);
      else if (act === 'edit') openEditChannelModal(id, false);
      else if (act === 'autologo') quickAutoLogo(id);
      else if (act === 'uncat') removeChannelFromCategory(id);
      else if (act === 'delete') deleteSingleChannel(id);
    });
  });

  container.querySelectorAll('.row-check').forEach((chk) => {
    chk.addEventListener('change', (e) => {
      const id = e.target.getAttribute('data-id');
      if (e.target.checked) state.selectedChannelIds.add(id);
      else state.selectedChannelIds.delete(id);
      renderChannels();
      updateBulkBar();
    });
  });
}

function toggleSelectAllFiltered(checked) {
  const filtered = getFilteredChannels();
  for (const ch of filtered) {
    if (checked) state.selectedChannelIds.add(ch.id);
    else state.selectedChannelIds.delete(ch.id);
  }
  renderChannels();
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('bulkActionBar');
  const count = state.selectedChannelIds.size;
  if (count > 0) {
    bar.classList.remove('hidden');
    document.getElementById('bulkSelectedCount').textContent = `${count} channel${count > 1 ? 's' : ''} selected`;
    const filtered = getFilteredChannels();
    document.getElementById('selectAllCheckbox').checked =
      filtered.length > 0 && filtered.every((c) => state.selectedChannelIds.has(c.id));
  } else {
    bar.classList.add('hidden');
  }
}

// ---------------------------------------------------------------------------
// Modal Helpers
// ---------------------------------------------------------------------------

function openModal(id) {
  document.getElementById(id).classList.remove('hidden');
}

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
}

document.querySelectorAll('[data-close]').forEach((btn) => {
  btn.addEventListener('click', () => closeModal(btn.getAttribute('data-close')));
});

// ---------------------------------------------------------------------------
// 1. Auto-Find Channel Stream Source by Name (Bulk ≤100 Names & Single Deep Search)
// ---------------------------------------------------------------------------

function switchSourceModalTab(mode) {
  const isBulk = mode === 'bulk';
  const isSingle = mode === 'single';
  const isGithub = mode === 'github';
  const isProv = mode === 'providers';

  document.getElementById('tabBtnBulkSource').classList.toggle('active', isBulk);
  document.getElementById('tabBtnSingleSource').classList.toggle('active', isSingle);
  const tabBtnGh = document.getElementById('tabBtnGithubSource');
  if (tabBtnGh) tabBtnGh.classList.toggle('active', isGithub);
  const tabBtnProv = document.getElementById('tabBtnProviders');
  if (tabBtnProv) tabBtnProv.classList.toggle('active', isProv);

  document.getElementById('sourceTabBulk').classList.toggle('hidden', !isBulk);
  document.getElementById('sourceTabSingle').classList.toggle('hidden', !isSingle);
  const srcTabGh = document.getElementById('sourceTabGithub');
  if (srcTabGh) srcTabGh.classList.toggle('hidden', !isGithub);
  const srcTabProv = document.getElementById('sourceTabProviders');
  if (srcTabProv) srcTabProv.classList.toggle('hidden', !isProv);

  if (isProv) {
    loadSourceProviders();
  }
}

async function loadSourceProviders() {
  const wrap = document.getElementById('providersListWrap');
  if (!wrap) return;

  try {
    const data = await api('/api/sources/providers');
    const providers = data.providers || [];
    const totalStreams = data.total_streams || 0;

    const countBadge = document.getElementById('tabProvidersStreamCount');
    if (countBadge) {
      countBadge.textContent = totalStreams > 0 ? `${totalStreams.toLocaleString()}` : '25,000+';
    }

    if (providers.length === 0) {
      wrap.innerHTML = `<div class="empty-state" style="padding:24px;"><p>No stream providers configured yet.</p></div>`;
      return;
    }

    wrap.innerHTML = `
      <table class="channels-table">
        <thead>
          <tr>
            <th>Provider / Platform</th>
            <th>Type</th>
            <th>M3U Feed URL</th>
            <th>Indexed Streams</th>
            <th>Last Synced</th>
            <th width="120">Status</th>
            <th width="80">Action</th>
          </tr>
        </thead>
        <tbody>
          ${providers
            .map((p) => {
              const badgeClass = p.is_system ? 'badge badge-indigo' : 'badge badge-emerald';
              const typeLabel =
                p.provider_type === 'fast_platform'
                  ? 'FAST Network'
                  : p.provider_type === 'public_broadcasts'
                  ? 'Public Broadcast'
                  : p.provider_type === 'global_directory'
                  ? 'Global Directory'
                  : 'Custom M3U';

              return `
                <tr>
                  <td>
                    <div style="display:flex;align-items:center;gap:8px;">
                      <span class="${badgeClass}" style="font-weight:700;">${escapeHtml(p.badge || '📺')}</span>
                      <strong>${escapeHtml(p.name)}</strong>
                    </div>
                  </td>
                  <td><span class="ch-cat-pill">${typeLabel}</span></td>
                  <td><div class="ch-url-preview ch-url-wrap" style="max-width:240px;" title="${escapeHtml(p.url)}">${escapeHtml(p.url)}</div></td>
                  <td><strong style="color:#38bdf8;">${(p.channel_count || 0).toLocaleString()} streams</strong></td>
                  <td><small style="color:#94a3b8;">${p.last_synced_at ? formatTimeAgo(p.last_synced_at) : 'Not synced yet'}</small></td>
                  <td>
                    <label class="switch-label" style="display:inline-flex;align-items:center;cursor:pointer;gap:6px;">
                      <input type="checkbox" class="prov-toggle-checkbox" data-prov-id="${escapeHtml(p.id)}" ${p.enabled ? 'checked' : ''} />
                      <span class="status-dot ${p.enabled ? 'status-online' : 'status-offline'}">${p.enabled ? 'Enabled' : 'Disabled'}</span>
                    </label>
                  </td>
                  <td>
                    ${
                      p.is_system
                        ? `<small style="color:#64748b;">System</small>`
                        : `<button class="card-btn del-btn" data-del-prov-id="${escapeHtml(p.id)}" title="Delete custom provider">🗑️</button>`
                    }
                  </td>
                </tr>
              `;
            })
            .join('')}
        </tbody>
      </table>
    `;

    wrap.querySelectorAll('.prov-toggle-checkbox').forEach((chk) => {
      chk.addEventListener('change', async (e) => {
        const provId = chk.getAttribute('data-prov-id');
        const enabled = e.target.checked;
        try {
          await api('/api/sources/providers/toggle', {
            method: 'POST',
            body: JSON.stringify({ id: provId, enabled }),
          });
          showToast(`Provider ${enabled ? 'enabled' : 'disabled'}!`);
          await loadSourceProviders();
        } catch (err) {
          showToast(`❌ ${err.message}`);
          e.target.checked = !enabled;
        }
      });
    });

    wrap.querySelectorAll('[data-del-prov-id]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const provId = btn.getAttribute('data-del-prov-id');
        if (!confirm('Remove this custom stream provider?')) return;
        try {
          await api('/api/sources/providers/delete', {
            method: 'POST',
            body: JSON.stringify({ id: provId }),
          });
          showToast('🗑️ Provider removed');
          await loadSourceProviders();
        } catch (err) {
          showToast(`❌ ${err.message}`);
        }
      });
    });
  } catch (err) {
    wrap.innerHTML = `<div style="padding:20px;color:#fca5a5;">❌ ${escapeHtml(err.message)}</div>`;
  }
}

function openBulkSourceFinderModal() {
  switchSourceModalTab('bulk');
  updateBulkSourceLineCounter('bulkSourceNamesInput', 'bulkSourceLineCount');
  openModal('sourceFinderModal');
}

function openSingleSourceFinderModal(query = '', replaceChannelId = '', replaceChannelName = '') {
  switchSourceModalTab('single');
  document.getElementById('sourceFinderReplaceChannelId').value = replaceChannelId || '';
  const banner = document.getElementById('sourceFinderReplaceBanner');
  if (replaceChannelId) {
    banner.classList.remove('hidden');
    document.getElementById('sourceFinderReplaceName').textContent = replaceChannelName;
  } else {
    banner.classList.add('hidden');
  }
  document.getElementById('sourceFinderQuery').value = query || '';
  openModal('sourceFinderModal');
  if (query) {
    runDedicatedSourceSearch();
  }
}

function openSourceFinderForChannel(channelId) {
  const ch = state.channels.find((c) => c.id === channelId);
  if (!ch) return;
  openSingleSourceFinderModal(ch.name, ch.id, ch.name);
}

function updateBulkSourceLineCounter(textareaId, badgeId) {
  const raw = document.getElementById(textareaId).value;
  const lines = raw
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
  const badge = document.getElementById(badgeId);
  badge.textContent = `${Math.min(lines.length, 100)} / 100 channels`;
  if (lines.length > 100) {
    badge.textContent = `100 / 100 (first 100 used)`;
  }
}

function renderSourceMatchBadges(opt) {
  if (!opt) return '';
  let html = '';
  if (opt.country_badge) {
    html += `<span class="ch-cat-pill" style="background:rgba(59,130,246,0.18); color:#93c5fd; border:1px solid rgba(59,130,246,0.3); font-weight:600;">${escapeHtml(opt.country_badge)}</span>`;
  }
  if (opt.match_confidence) {
    const isHigh = opt.match_confidence >= 80;
    const bg = isHigh ? 'rgba(16,185,129,0.18)' : 'rgba(234,179,8,0.18)';
    const fg = isHigh ? '#6ee7b7' : '#fde047';
    html += `<span class="ch-cat-pill" style="background:${bg}; color:${fg}; border:1px solid rgba(255,255,255,0.12); font-weight:700;">🎯 ${opt.match_confidence}% Match</span>`;
  }
  return html;
}

function renderBulkSourceResultsTable(results, containerId) {
  const wrap = document.getElementById(containerId);
  wrap.classList.remove('hidden');

  if (!results || results.length === 0) {
    wrap.innerHTML = `<div style="padding:16px;color:#94a3b8;">No channel names provided.</div>`;
    return;
  }

  wrap.innerHTML = `
    <table class="channels-table">
      <thead>
        <tr>
          <th>Requested Name</th>
          <th>Available Tested Stream Feeds</th>
          <th>Stream URL (.m3u8)</th>
          <th>Live Status & Latency</th>
          <th width="110">Action</th>
        </tr>
      </thead>
      <tbody>
        ${results
          .map((r, idx) => {
            if (!r.found || !r.url) {
              return `
                <tr>
                  <td><strong>${escapeHtml(r.requested_name)}</strong></td>
                  <td><span class="badge badge-danger">No Working Match Found</span></td>
                  <td><small style="color:#64748b;">—</small></td>
                  <td><span class="status-dot status-offline">Not Found</span></td>
                  <td>—</td>
                </tr>
              `;
            }

            const options = r.options && r.options.length > 0 ? r.options : [
              {
                name: r.name,
                url: r.url,
                quality: r.quality || 'HD',
                source: r.source || 'Live CDN',
                country: r.country || '',
                country_badge: r.country_badge || '',
                match_confidence: r.match_confidence || 0,
                latency_ms: r.latency_ms || 0,
                status: r.status || 'online',
              }
            ];

            let optionsSelectorHtml = '';
            if (options.length > 1) {
              optionsSelectorHtml = `
                <div style="display:flex; flex-direction:column; gap:4px;">
                  <select class="bulk-opt-select" data-ch-idx="${idx}" title="Choose from ${options.length} tested working stream options">
                    ${options.map((opt, oIdx) => {
                      const cPrefix = opt.country_badge ? `[${opt.country_badge}] ` : '';
                      const confText = opt.match_confidence ? ` • 🎯 ${opt.match_confidence}% Match` : '';
                      return `
                        <option value="${oIdx}" ${oIdx === (r.selected_option_index || 0) ? 'selected' : ''}>
                          Option ${oIdx + 1}: ${cPrefix}[${escapeHtml(opt.quality || 'HD')}] ${opt.latency_ms ? opt.latency_ms + 'ms' : 'Online'} • ${escapeHtml(opt.source || 'Verified Feed')}${confText} ${oIdx === 0 ? '★ Best Match' : ''}
                        </option>
                      `;
                    }).join('')}
                  </select>
                  <div id="bulkOptMeta_${containerId}_${idx}" style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                    ${renderSourceMatchBadges(options[r.selected_option_index || 0])}
                  </div>
                </div>
              `;
            } else {
              const single = options[0];
              optionsSelectorHtml = `
                <div style="display:flex;align-items:center;gap:6px; flex-wrap:wrap;">
                  <strong>${escapeHtml(single.name || r.name)}</strong>
                  ${renderSourceMatchBadges(single)}
                  <span class="ch-cat-pill">${escapeHtml(single.quality || 'HD')}</span>
                  <span class="ch-cat-pill">${escapeHtml(single.source || 'Live Feed')}</span>
                </div>
              `;
            }

            const statusHtml =
              r.status === 'online'
                ? `<span class="status-dot status-online" id="statusPill_${containerId}_${idx}">● Online (${r.latency_ms}ms)</span>`
                : r.status === 'warning'
                ? `<span class="status-dot status-warning" id="statusPill_${containerId}_${idx}">● Slow (${r.latency_ms}ms)</span>`
                : `<span class="status-dot status-offline" id="statusPill_${containerId}_${idx}">● Offline</span>`;

            return `
              <tr>
                <td><strong>${escapeHtml(r.requested_name)}</strong></td>
                <td>${optionsSelectorHtml}</td>
                <td>
                  <div class="ch-url-preview" id="urlPreview_${containerId}_${idx}" style="max-width:260px;" title="${escapeHtml(r.url)}">${escapeHtml(r.url)}</div>
                </td>
                <td>${statusHtml}</td>
                <td>
                  <button type="button" class="card-btn play-btn" data-bulk-play-idx="${idx}" title="Preview stream with CORS Proxy">▶ Test</button>
                </td>
              </tr>
            `;
          })
          .join('')}
      </tbody>
    </table>
  `;

  // Attach change listeners to stream options selectors
  wrap.querySelectorAll('.bulk-opt-select').forEach((sel) => {
    sel.addEventListener('change', (e) => {
      const chIdx = parseInt(sel.getAttribute('data-ch-idx'), 10);
      const chosenOptIdx = parseInt(e.target.value, 10);
      const item = results[chIdx];
      if (item && item.options && item.options[chosenOptIdx]) {
        const chosen = item.options[chosenOptIdx];
        item.selected_option_index = chosenOptIdx;
        item.url = chosen.url;
        item.name = chosen.name || item.name;
        item.quality = chosen.quality;
        item.latency_ms = chosen.latency_ms;
        item.status = chosen.status;
        item.source = chosen.source;
        item.country = chosen.country;
        item.country_badge = chosen.country_badge;
        item.match_confidence = chosen.match_confidence;
        if (chosen.logo) item.logo = chosen.logo;
        if (chosen.tvg_id) item.tvg_id = chosen.tvg_id;
        if (chosen.category) item.category = chosen.category;

        // Dynamically update URL and Status display in row
        const urlEl = document.getElementById(`urlPreview_${containerId}_${chIdx}`);
        if (urlEl) {
          urlEl.textContent = chosen.url;
          urlEl.title = chosen.url;
        }
        const statusEl = document.getElementById(`statusPill_${containerId}_${chIdx}`);
        if (statusEl) {
          statusEl.className = chosen.status === 'online' ? 'status-dot status-online' : 'status-dot status-warning';
          statusEl.textContent = `● Online (${chosen.latency_ms}ms)`;
        }
        const metaEl = document.getElementById(`bulkOptMeta_${containerId}_${chIdx}`);
        if (metaEl) {
          metaEl.innerHTML = renderSourceMatchBadges(chosen);
        }
      }
    });
  });

  // Attach test play buttons
  wrap.querySelectorAll('[data-bulk-play-idx]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.getAttribute('data-bulk-play-idx'), 10);
      const item = results[idx];
      if (item && item.url) {
        playStreamUrl(item.name || item.requested_name, item.url, false, item.referrer || '', item.user_agent || '', item.play_mode || '');
      }
    });
  });
}

async function runDedicatedSourceSearch() {
  const q = document.getElementById('sourceFinderQuery').value.trim();
  if (!q) {
    showToast('Enter a channel name to search sources');
    return;
  }
  const country = document.getElementById('sourceFinderCountryFilter') ? document.getElementById('sourceFinderCountryFilter').value : '';
  const strict = (document.getElementById('sourceFinderStrictMode') && document.getElementById('sourceFinderStrictMode').checked) ? '1' : '0';
  const probe = document.getElementById('sourceFinderProbeLive').checked ? '1' : '0';
  const includeWeb = (document.getElementById('sourceFinderIncludeWeb') && document.getElementById('sourceFinderIncludeWeb').checked) ? '1' : '0';
  const includeYt = (document.getElementById('sourceFinderIncludeYoutube') && document.getElementById('sourceFinderIncludeYoutube').checked) ? '1' : '0';
  const includeRutube = (document.getElementById('sourceFinderIncludeRutube') && document.getElementById('sourceFinderIncludeRutube').checked) ? '1' : '0';
  const includeTelevizo = (document.getElementById('sourceFinderIncludeTelevizo') && document.getElementById('sourceFinderIncludeTelevizo').checked) ? '1' : '0';
  const includeSmotret = (document.getElementById('sourceFinderIncludeSmotret') && document.getElementById('sourceFinderIncludeSmotret').checked) ? '1' : '0';
  const container = document.getElementById('sourceFinderResults');
  container.innerHTML = `<div class="empty-state" style="padding:28px;"><p>🔎 Searching 38,000+ indexed streams, the open m3u8 web index, Televizo, RuTube, YouTube Live & web for "<strong>${escapeHtml(q)}</strong>"...</p></div>`;

  try {
    const data = await api(`/api/sources/search?q=${encodeURIComponent(q)}&probe=${probe}&include_web=${includeWeb}&include_yt=${includeYt}&include_rutube=${includeRutube}&include_televizo=${includeTelevizo}&include_smotret=${includeSmotret}&country=${encodeURIComponent(country)}&strict=${strict}`);
    const sources = data.sources || [];
    const replaceId = document.getElementById('sourceFinderReplaceChannelId').value;

    if (sources.length === 0) {
      container.innerHTML = `<div class="empty-state" style="padding:28px;"><p>No matching public stream sources found for "${escapeHtml(q)}". Try a broader keyword, change target country, or disable Max Accuracy mode.</p></div>`;
      return;
    }

    container.innerHTML = sources
      .map((s, idx) => {
        const fallback = makeFallbackLogo(s.name);
        const logoSrc = s.logo || fallback;
        const statusHtml =
          s.status === 'online'
            ? `<span class="status-dot status-online">● Online ${s.latency_ms}ms</span>`
            : s.status === 'offline'
            ? `<span class="status-dot status-offline">● Offline</span>`
            : `<span class="ch-cat-pill">Unprobed</span>`;

        const countryBadgeHtml = s.country_badge
          ? `<span class="ch-cat-pill" style="background:rgba(59,130,246,0.18); color:#93c5fd; border:1px solid rgba(59,130,246,0.3); font-weight:600;">${escapeHtml(s.country_badge)}</span>`
          : '';

        const confidenceHtml = s.match_confidence
          ? `<span class="ch-cat-pill" style="background:${s.match_confidence >= 80 ? 'rgba(16,185,129,0.18)' : 'rgba(234,179,8,0.18)'}; color:${s.match_confidence >= 80 ? '#6ee7b7' : '#fde047'}; border:1px solid rgba(255,255,255,0.12); font-weight:700;">🎯 ${s.match_confidence}% Match</span>`
          : '';

        return `
          <div class="source-row-card">
            <div class="source-row-left">
              <img class="ch-img-el" src="${escapeHtml(logoSrc)}" data-fallback="${escapeHtml(fallback)}" />
              <div class="source-row-meta">
                <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                  <strong>${escapeHtml(s.name)}</strong>
                  ${countryBadgeHtml}
                  ${confidenceHtml}
                  <span class="ch-cat-pill">${escapeHtml(s.quality || 'HD')}</span>
                  <span class="ch-cat-pill">${escapeHtml(s.source)}</span>
                  ${statusHtml}
                </div>
                <div class="source-row-url" title="${escapeHtml(s.url)}">${escapeHtml(s.url)}</div>
              </div>
            </div>
            <div class="card-btn-group">
              <button class="card-btn play-btn" data-play-idx="${idx}">▶ Test</button>
              ${
                replaceId
                  ? `<button class="btn-sm btn-source-finder" data-apply-idx="${idx}">🔄 Replace Broken URL</button>`
                  : `<button class="btn-sm btn-primary" data-add-idx="${idx}">＋ Add to Playlist</button>`
              }
            </div>
          </div>
        `;
      })
      .join('');

    container.querySelectorAll('[data-play-idx]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const s = sources[parseInt(btn.getAttribute('data-play-idx'), 10)];
        playStreamUrl(s.name, s.url, false, s.referrer || '', s.user_agent || '', s.play_mode || '');
      });
    });

    container.querySelectorAll('[data-add-idx]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        try {
          const s = sources[parseInt(btn.getAttribute('data-add-idx'), 10)];
          await api('/api/import/commit', {
            method: 'POST',
            body: JSON.stringify({
              playlist_id: state.currentPlaylistId,
              channels: [
                {
                  name: s.name,
                  url: s.url,
                  logo: s.logo,
                  category: s.category || 'Live TV',
                  tvg_id: s.tvg_id,
                  status: s.status,
                  latency_ms: s.latency_ms,
                },
              ],
              deduplicate_urls: false,
              deduplicate_internal: false,
            }),
          });
          showToast(`＋ Added "${s.name}" to playlist!`);
          await loadPlaylists(state.currentPlaylistId);
        } catch (err) {
          showToast(`❌ ${err.message}`);
        }
      });
    });

    container.querySelectorAll('[data-apply-idx]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        try {
          const s = sources[parseInt(btn.getAttribute('data-apply-idx'), 10)];
          await api(`/api/playlists/${state.currentPlaylistId}/channels/update`, {
            method: 'POST',
            body: JSON.stringify({
              id: replaceId,
              url: s.url,
              logo: s.logo || undefined,
              tvg_id: s.tvg_id || undefined,
              status: s.status === 'online' ? 'online' : 'unknown',
            }),
          });
          showToast(`🔄 Updated stream source for channel!`);
          closeModal('sourceFinderModal');
          await loadPlaylists(state.currentPlaylistId);
          if (!document.getElementById('brokenChannelsModal').classList.contains('hidden')) {
            await refreshBrokenChannelsModal();
          }
        } catch (err) {
          showToast(`❌ ${err.message}`);
        }
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state"><p>❌ ${escapeHtml(err.message)}</p></div>`;
  }
}

// ---------------------------------------------------------------------------
// 1b. GitHub & Open-Web Stream Discovery Tool
// ---------------------------------------------------------------------------

async function runGithubRepoSearch(customQuery = '') {
  const queryInput = document.getElementById('githubRepoSearchQuery');
  const q = (typeof customQuery === 'string' && customQuery.trim()) ? customQuery.trim() : (queryInput ? queryInput.value.trim() : '');
  if (!q) {
    showToast('Enter a keyword or click a quick topic (e.g. Sports, News, Movies)');
    return;
  }
  if (queryInput) queryInput.value = q;

  const btn = document.getElementById('btnRunGithubRepoSearch');
  const wrap = document.getElementById('githubRepoResultsWrap');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🔎 Searching GitHub, YouTube & Web...';
  }
  wrap.innerHTML = `<div class="empty-state" style="padding:28px;"><p>🌐 Searching the open m3u8 web index, GitHub IPTV repositories, YouTube Live & live web for "<strong>${escapeHtml(q)}</strong>"...</p></div>`;

  try {
    const [ghRes, webRes, ytRes] = await Promise.allSettled([
      api(`/api/github/search-repos?q=${encodeURIComponent(q)}`),
      api(`/api/sources/web-search?q=${encodeURIComponent(q)}&probe=1`),
      api(`/api/sources/youtube-live?q=${encodeURIComponent(q)}&probe=1&limit=6`)
    ]);

    const repos = ghRes.status === 'fulfilled' ? (ghRes.value.repositories || []) : [];
    const webStreams = webRes.status === 'fulfilled' ? (webRes.value.sources || []) : [];
    const ytStreams = ytRes.status === 'fulfilled' ? (ytRes.value.sources || []) : [];

    renderGithubSearchResults(repos, webStreams, ytStreams, q);
  } catch (err) {
    wrap.innerHTML = `<div class="empty-state"><p>❌ Search failed: ${escapeHtml(err.message)}</p></div>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔎 Search GitHub & Web';
    }
  }
}

async function runYoutubeLiveSearch(customQuery = '') {
  const queryInput = document.getElementById('ytLiveSearchQuery');
  const q = (typeof customQuery === 'string' && customQuery.trim()) ? customQuery.trim() : (queryInput ? queryInput.value.trim() : '');
  if (!q) {
    showToast('Enter a channel name (e.g. Sky News, DW News, NASA TV)');
    return;
  }
  if (queryInput) queryInput.value = q;

  const btn = document.getElementById('btnRunYtLiveSearch');
  const wrap = document.getElementById('githubRepoResultsWrap');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '▶️ Detecting YouTube Live...';
  }
  wrap.innerHTML = `<div class="empty-state" style="padding:28px;"><p>▶️ Probing YouTube & extracting verified live broadcast for "<strong>${escapeHtml(q)}</strong>"...</p></div>`;

  try {
    const res = await api(`/api/sources/youtube-live?q=${encodeURIComponent(q)}&probe=1&limit=8`);
    const ytStreams = res.sources || [];
    renderGithubSearchResults([], [], ytStreams, q);
    if (ytStreams.length > 0) {
      showToast(`▶️ Found ${ytStreams.length} verified YouTube Live channel(s)!`);
    } else {
      showToast(`⚠️ No live 24/7 broadcast currently found for "${q}"`);
    }
  } catch (err) {
    wrap.innerHTML = `<div class="empty-state"><p>❌ YouTube search error: ${escapeHtml(err.message)}</p></div>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '▶️ Detect YouTube Live';
    }
  }
}

function renderGithubSearchResults(repos, webStreams, ytStreams = [], query = '') {
  const wrap = document.getElementById('githubRepoResultsWrap');
  if (!wrap) return;

  if (repos.length === 0 && webStreams.length === 0 && ytStreams.length === 0) {
    wrap.innerHTML = `<div class="empty-state" style="padding:28px;"><p>No GitHub IPTV repositories, YouTube Live or web streams found for "${escapeHtml(query)}". Try another search keyword like "sports", "news", "lithuania", or "latino".</p></div>`;
    return;
  }

  let html = '';

  // 1. YouTube Live Streams section (if any)
  if (ytStreams && ytStreams.length > 0) {
    html += `
      <div class="github-section-header" style="color:#f87171;">
        <span>▶️ Verified YouTube 24/7 Live Television Feeds (${ytStreams.length})</span>
        <small style="color:var(--text-muted); font-weight:normal;">Auto-refreshing permanent proxy streams</small>
      </div>
      <div class="source-results-container mb-12" style="max-height:240px; overflow-y:auto; margin-bottom:14px;">
    `;
    html += ytStreams.map((s, idx) => {
      const fallback = makeFallbackLogo(s.name);
      const logoSrc = s.logo || fallback;
      const statusHtml = s.status === 'online'
        ? `<span class="status-dot status-online">● Online ${s.latency_ms}ms</span>`
        : `<span class="status-dot status-offline">● Offline</span>`;
      const countryBadge = s.country_badge ? `<span class="ch-cat-pill">${escapeHtml(s.country_badge)}</span>` : '';
      return `
        <div class="source-row-card" style="border-color:rgba(239,68,68,0.3); background:rgba(239,68,68,0.06);">
          <div class="source-row-left">
            <img class="ch-img-el" src="${escapeHtml(logoSrc)}" data-fallback="${escapeHtml(fallback)}" />
            <div class="source-row-meta">
              <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <strong>${escapeHtml(s.name)}</strong>
                ${countryBadge}
                <span class="ch-cat-pill" style="background:rgba(239,68,68,0.2); color:#fca5a5; font-weight:700;">▶️ YouTube Live</span>
                <span class="ch-cat-pill">${escapeHtml(s.quality || '1080p')}</span>
                ${statusHtml}
              </div>
              <div class="source-row-url" title="${escapeHtml(s.url)}">${escapeHtml(s.url)}</div>
            </div>
          </div>
          <div class="card-btn-group">
            <button class="card-btn play-btn" data-yt-play-idx="${idx}">▶ Test</button>
            <button class="btn-sm btn-primary" data-yt-add-idx="${idx}">＋ Add</button>
          </div>
        </div>
      `;
    }).join('');
    html += `</div>`;
  }

  // 2. Live Web Streams section (if any)
  if (webStreams.length > 0) {
    html += `
      <div class="github-section-header">
        <span>🌐 Discovered Live Web & Direct Streams (${webStreams.length})</span>
        <small style="color:var(--text-muted); font-weight:normal;">Directly discovered from web search index</small>
      </div>
      <div class="source-results-container mb-12" style="max-height:220px; overflow-y:auto; margin-bottom:14px;">
    `;
    html += webStreams.map((s, idx) => {
      const fallback = makeFallbackLogo(s.name);
      const logoSrc = s.logo || fallback;
      const statusHtml = s.status === 'online'
        ? `<span class="status-dot status-online">● Online ${s.latency_ms}ms</span>`
        : `<span class="status-dot status-offline">● Offline</span>`;
      return `
        <div class="source-row-card">
          <div class="source-row-left">
            <img class="ch-img-el" src="${escapeHtml(logoSrc)}" data-fallback="${escapeHtml(fallback)}" />
            <div class="source-row-meta">
              <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <strong>${escapeHtml(s.name)}</strong>
                <span class="ch-cat-pill">${escapeHtml(s.quality || 'HD')}</span>
                <span class="ch-cat-pill">${escapeHtml(s.source || '🌐 Web Crawl')}</span>
                ${statusHtml}
              </div>
              <div class="source-row-url" title="${escapeHtml(s.url)}">${escapeHtml(s.url)}</div>
            </div>
          </div>
          <div class="card-btn-group">
            <button class="card-btn play-btn" data-web-play-idx="${idx}">▶ Test</button>
            <button class="btn-sm btn-primary" data-web-add-idx="${idx}">＋ Add</button>
          </div>
        </div>
      `;
    }).join('');
    html += `</div>`;
  }

  // 3. GitHub Repositories section
  if (repos.length > 0) {
    html += `
      <div class="github-section-header">
        <span>🐙 Open Community IPTV Repositories on GitHub (${repos.length})</span>
        <small style="color:var(--text-muted); font-weight:normal;">Community-maintained playlist collections</small>
      </div>
    `;

    html += repos.map((repo, rIdx) => {
      const filesHtml = (repo.files && repo.files.length > 0)
        ? repo.files.map((f, fIdx) => `
            <div class="github-file-row">
              <div class="github-file-info">
                <span>📄</span>
                <span class="github-file-name">${escapeHtml(f.name)}</span>
              </div>
              <div class="github-file-actions">
                <button type="button" class="btn btn-secondary btn-sm" data-preview-url="${escapeHtml(f.download_url)}" data-preview-title="${escapeHtml(repo.full_name + ' / ' + f.name)}">
                  👁️ Inspect Channels
                </button>
                <button type="button" class="btn btn-primary btn-sm" data-import-gh-url="${escapeHtml(f.download_url)}" data-import-gh-name="${escapeHtml(repo.full_name)}">
                  📥 Import All
                </button>
                <button type="button" class="btn btn-source-finder btn-sm" data-add-prov-url="${escapeHtml(f.download_url)}" data-add-prov-name="${escapeHtml(repo.full_name + ' (' + f.name + ')')}">
                  🌐 Connect Source
                </button>
              </div>
            </div>
          `).join('')
        : `<p style="font-size:0.78rem; color:#94a3b8; margin:4px 0;">No direct .m3u files found in root.</p>`;

      return `
        <div class="github-repo-card">
          <div class="github-repo-header">
            <div class="github-repo-title">
              <span>🐙</span>
              <a href="${escapeHtml(repo.html_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(repo.full_name)}</a>
            </div>
            <div class="github-stars-badge">
              ⭐ ${repo.stars.toLocaleString()} stars
            </div>
          </div>
          <p class="github-repo-desc">${escapeHtml(repo.description || 'Public IPTV repository on GitHub')}</p>
          <div class="github-files-list">
            ${filesHtml}
          </div>
        </div>
      `;
    }).join('');
  }

  wrap.innerHTML = html;

  // Attach YouTube Live action listeners
  if (ytStreams && ytStreams.length > 0) {
    wrap.querySelectorAll('[data-yt-play-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const s = ytStreams[parseInt(btn.getAttribute('data-yt-play-idx'), 10)];
        playStreamUrl(s.name, s.url, false, s.referrer || '', s.user_agent || '', s.play_mode || '');
      });
    });
    wrap.querySelectorAll('[data-yt-add-idx]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const s = ytStreams[parseInt(btn.getAttribute('data-yt-add-idx'), 10)];
        try {
          await api('/api/import/commit', {
            method: 'POST',
            body: JSON.stringify({
              playlist_id: state.currentPlaylistId,
              channels: [{
                name: s.name,
                url: s.url,
                logo: s.logo,
                category: s.category || 'Live TV',
                tvg_id: s.tvg_id,
                status: s.status,
                latency_ms: s.latency_ms,
              }],
              deduplicate_urls: false,
              deduplicate_internal: false,
            }),
          });
          showToast(`＋ Added "${s.name}" to playlist!`);
          await loadPlaylists(state.currentPlaylistId);
        } catch (err) {
          showToast(`❌ ${err.message}`);
        }
      });
    });
  }

  // Attach Web Streams listeners
  if (webStreams.length > 0) {
    wrap.querySelectorAll('[data-web-play-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const s = webStreams[parseInt(btn.getAttribute('data-web-play-idx'), 10)];
        playStreamUrl(s.name, s.url, false, s.referrer || '', s.user_agent || '', s.play_mode || '');
      });
    });
    wrap.querySelectorAll('[data-web-add-idx]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const s = webStreams[parseInt(btn.getAttribute('data-web-add-idx'), 10)];
        try {
          await api('/api/import/commit', {
            method: 'POST',
            body: JSON.stringify({
              playlist_id: state.currentPlaylistId,
              channels: [{
                name: s.name,
                url: s.url,
                logo: s.logo,
                category: s.category || 'Live TV',
                tvg_id: s.tvg_id,
                status: s.status,
                latency_ms: s.latency_ms,
              }],
              deduplicate_urls: false,
              deduplicate_internal: false,
            }),
          });
          showToast(`＋ Added "${s.name}" to playlist!`);
          await loadPlaylists(state.currentPlaylistId);
        } catch (err) {
          showToast(`❌ ${err.message}`);
        }
      });
    });
  }

  // Attach GitHub Repo file action listeners
  wrap.querySelectorAll('[data-preview-url]').forEach(btn => {
    btn.addEventListener('click', () => {
      const url = btn.getAttribute('data-preview-url');
      const title = btn.getAttribute('data-preview-title');
      openGithubPlaylistPreview(url, title);
    });
  });

  wrap.querySelectorAll('[data-import-gh-url]').forEach(btn => {
    btn.addEventListener('click', () => {
      const url = btn.getAttribute('data-import-gh-url');
      const name = btn.getAttribute('data-import-gh-name');
      importDiscoveredGithubPlaylistDirectly(url, name);
    });
  });

  wrap.querySelectorAll('[data-add-prov-url]').forEach(btn => {
    btn.addEventListener('click', () => {
      const url = btn.getAttribute('data-add-prov-url');
      const name = btn.getAttribute('data-add-prov-name');
      addDiscoveredGithubAsProvider(name, url);
    });
  });
}

async function probeSourceKind(kind, btn) {
  const out = document.getElementById('probeSourceResult');
  if (btn) btn.disabled = true;
  try {
    const data = await api(`/api/sources/providers/probe?type=${encodeURIComponent(kind)}`);
    const ok = data.status === 'ok';
    if (out) {
      out.classList.remove('hidden');
      out.style.color = ok ? '#6ee7b7' : '#f8b4b4';
      out.textContent = `${ok ? '✅' : '⚠️'} ${kind}: ${data.status} (HTTP ${data.http_status || '—'}) — ${data.detail || ''}`;
    }
  } catch (err) {
    if (out) {
      out.classList.remove('hidden');
      out.style.color = '#f8b4b4';
      out.textContent = `❌ ${kind}: ${err.message}`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function addDiscoveredGithubAsProvider(name, url) {
  try {
    const res = await api('/api/sources/providers/add', {
      method: 'POST',
      body: JSON.stringify({
        name,
        url,
        badge: '🐙 GitHub',
        provider_type: 'github_m3u',
      }),
    });
    showToast(`🎉 Connected "${name}" as a stream provider!`);
    await loadSourceProviders();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function openGithubPlaylistPreview(downloadUrl, title = '') {
  state.currentGithubPreviewUrl = downloadUrl;
  state.currentGithubPreviewTitle = title;
  state.githubPreviewData = null;

  const titleEl = document.getElementById('githubPreviewModalTitle');
  const subEl = document.getElementById('githubPreviewModalSubtitle');
  if (titleEl) titleEl.textContent = `🐙 Inspect: ${title || 'GitHub Playlist'}`;
  if (subEl) subEl.textContent = downloadUrl;

  const filterInput = document.getElementById('githubPreviewFilterInput');
  if (filterInput) filterInput.value = '';

  const wrap = document.getElementById('githubPreviewChannelsWrap');
  if (wrap) {
    wrap.innerHTML = `<div class="empty-state" style="padding:28px;"><p>⏳ Downloading & parsing playlist from GitHub...</p></div>`;
  }

  openModal('githubPreviewModal');

  try {
    const data = await api('/api/github/preview-playlist', {
      method: 'POST',
      body: JSON.stringify({ url: downloadUrl }),
    });
    if (data.error) {
      if (wrap) wrap.innerHTML = `<div class="empty-state"><p>❌ ${escapeHtml(data.error)}</p></div>`;
      return;
    }
    state.githubPreviewData = data;
    renderGithubPreviewChannelsTable(data.channels || []);
  } catch (err) {
    if (wrap) wrap.innerHTML = `<div class="empty-state"><p>❌ Error loading preview: ${escapeHtml(err.message)}</p></div>`;
  }
}

function renderGithubPreviewChannelsTable(channels) {
  const wrap = document.getElementById('githubPreviewChannelsWrap');
  if (!wrap) return;

  if (!channels || channels.length === 0) {
    wrap.innerHTML = `<div class="empty-state" style="padding:24px;"><p>No channels found in this playlist preview.</p></div>`;
    return;
  }

  wrap.innerHTML = `
    <table class="channels-table">
      <thead>
        <tr>
          <th style="width:50px;">#</th>
          <th>Channel</th>
          <th>Category</th>
          <th>Stream URL</th>
          <th>Status</th>
          <th style="width:130px; text-align:right;">Actions</th>
        </tr>
      </thead>
      <tbody>
        ${channels.map((ch, idx) => {
          const fallback = makeFallbackLogo(ch.name);
          const logoSrc = ch.logo || fallback;
          const statusDot = ch.status === 'online'
            ? `<span class="status-dot status-online">● Online ${ch.latency_ms || ''}ms</span>`
            : ch.status === 'offline'
            ? `<span class="status-dot status-offline">● Offline</span>`
            : `<span class="ch-cat-pill">Unchecked</span>`;

          return `
            <tr data-preview-idx="${idx}">
              <td style="color:#64748b; font-size:0.75rem;">${idx + 1}</td>
              <td>
                <div class="ch-flex">
                  <img class="ch-img-el" src="${escapeHtml(logoSrc)}" data-fallback="${escapeHtml(fallback)}" />
                  <div>
                    <div style="font-weight:600; font-size:0.88rem;">${escapeHtml(ch.name)}</div>
                    ${ch.tvg_id ? `<div style="font-size:0.72rem; color:#64748b;">${escapeHtml(ch.tvg_id)}</div>` : ''}
                  </div>
                </div>
              </td>
              <td><span class="ch-cat-pill">${escapeHtml(ch.category || 'Live TV')}</span></td>
              <td><div class="url-cell" title="${escapeHtml(ch.url)}">${escapeHtml(ch.url)}</div></td>
              <td class="preview-status-cell">${statusDot}</td>
              <td style="text-align:right;">
                <div style="display:inline-flex; gap:6px;">
                  <button type="button" class="btn-sm btn-secondary" data-play-prev-idx="${idx}">▶</button>
                  <button type="button" class="btn-sm btn-primary" data-import-prev-idx="${idx}">＋ Import</button>
                </div>
              </td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
  `;

  // Attach per-channel actions
  wrap.querySelectorAll('[data-play-prev-idx]').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.getAttribute('data-play-prev-idx'), 10);
      const ch = channels[idx];
      if (ch) playStreamUrl(ch.name, ch.url, false, ch.referrer || '', ch.user_agent || '', ch.play_mode || '');
    });
  });

  wrap.querySelectorAll('[data-import-prev-idx]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.getAttribute('data-import-prev-idx'), 10);
      const ch = channels[idx];
      if (!ch) return;
      try {
        await api('/api/import/commit', {
          method: 'POST',
          body: JSON.stringify({
            playlist_id: state.currentPlaylistId,
            channels: [{
              name: ch.name,
              url: ch.url,
              logo: ch.logo,
              category: ch.category || 'Live TV',
              tvg_id: ch.tvg_id,
              status: ch.status || 'unknown',
            }],
            deduplicate_urls: false,
            deduplicate_internal: false,
          }),
        });
        showToast(`＋ Imported "${ch.name}" to playlist!`);
        btn.textContent = '✓ Added';
        btn.disabled = true;
        await loadPlaylists(state.currentPlaylistId);
      } catch (err) {
        showToast(`❌ ${err.message}`);
      }
    });
  });
}

async function probeGithubPreviewChannels() {
  if (!state.githubPreviewData || !state.githubPreviewData.channels) {
    showToast('No channels to probe in preview');
    return;
  }
  const btn = document.getElementById('btnProbeGithubPreviewChannels');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🩺 Probing Preview Streams...';
  }
  const channels = state.githubPreviewData.channels.slice(0, 30);
  try {
    const urls = channels.map(c => c.url);
    const res = await api('/api/channels/check-batch', {
      method: 'POST',
      body: JSON.stringify({ urls }),
    });
    const results = res.results || {};
    channels.forEach(ch => {
      if (results[ch.url]) {
        ch.status = results[ch.url].status;
        ch.latency_ms = results[ch.url].latency_ms;
      }
    });
    renderGithubPreviewChannelsTable(state.githubPreviewData.channels);
    showToast('🩺 Probed streams in playlist preview!');
  } catch (err) {
    showToast(`❌ Probe failed: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🩺 Probe Preview Streams';
    }
  }
}

async function importAllGithubPreviewChannels() {
  if (!state.githubPreviewData || !state.githubPreviewData.channels || state.githubPreviewData.channels.length === 0) {
    showToast('No channels available to import');
    return;
  }
  const filterVal = (document.getElementById('githubPreviewFilterInput')?.value || '').toLowerCase().trim();
  const toImport = state.githubPreviewData.channels.filter(ch => {
    if (!filterVal) return true;
    return (ch.name || '').toLowerCase().includes(filterVal) || (ch.category || '').toLowerCase().includes(filterVal);
  });

  if (toImport.length === 0) {
    showToast('No matching channels to import');
    return;
  }

  const btn = document.getElementById('btnImportAllGithubPreview');
  if (btn) {
    btn.disabled = true;
    btn.textContent = `📥 Importing ${toImport.length}...`;
  }

  try {
    const res = await api('/api/import/commit', {
      method: 'POST',
      body: JSON.stringify({
        playlist_id: state.currentPlaylistId,
        channels: toImport.map(ch => ({
          name: ch.name,
          url: ch.url,
          logo: ch.logo,
          category: ch.category || 'Live TV',
          tvg_id: ch.tvg_id,
          status: ch.status || 'unknown',
        })),
        deduplicate_urls: true,
        deduplicate_internal: true,
      }),
    });
    showToast(`🎉 Imported ${res.inserted} channels into your playlist!`);
    closeModal('githubPreviewModal');
    await loadPlaylists(state.currentPlaylistId);
  } catch (err) {
    showToast(`❌ ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '📥 Import Channels to Playlist';
    }
  }
}

async function importDiscoveredGithubPlaylistDirectly(downloadUrl, repoName) {
  showToast(`⏳ Loading channels from ${repoName}...`);
  try {
    const data = await api('/api/github/preview-playlist', {
      method: 'POST',
      body: JSON.stringify({ url: downloadUrl }),
    });
    if (data.error) {
      showToast(`❌ ${data.error}`);
      return;
    }
    const channels = data.channels || [];
    if (channels.length === 0) {
      showToast('⚠️ No channels found in this GitHub playlist');
      return;
    }
    const res = await api('/api/import/commit', {
      method: 'POST',
      body: JSON.stringify({
        playlist_id: state.currentPlaylistId,
        channels: channels.map(ch => ({
          name: ch.name,
          url: ch.url,
          logo: ch.logo,
          category: ch.category || 'Live TV',
          tvg_id: ch.tvg_id,
          status: 'unknown',
        })),
        deduplicate_urls: true,
        deduplicate_internal: true,
      }),
    });
    showToast(`🎉 Imported ${res.inserted} channels from ${repoName}!`);
    await loadPlaylists(state.currentPlaylistId);
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function searchSourcesInsideChannelModal() {
  const q = document.getElementById('chName').value.trim();
  const tvgId = document.getElementById('chTvgId').value.trim();
  if (!q) {
    showToast('Type a Channel Name first, then click Find Stream');
    return;
  }
  const btn = document.getElementById('btnModalFindSource');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🔎 Finding...';
  }
  const box = document.getElementById('modalSourceCandidatesBox');
  const list = document.getElementById('modalSourceCandidatesList');
  const statusEl = document.getElementById('modalSourceStatusText');

  box.classList.remove('hidden');
  statusEl.textContent = `🔎 Searching 38,000+ indexed streams, the open m3u8 web index, Televizo, RuTube & YouTube for "${q}"...`;
  list.innerHTML = '';

  try {
    const data = await api(`/api/sources/search?q=${encodeURIComponent(q)}&tvg_id=${encodeURIComponent(tvgId)}&probe=1&include_televizo=1&include_rutube=1&include_yt=1`);
    const sources = data.sources || [];
    statusEl.textContent = `Found ${sources.length} stream source(s) for "${q}"`;

    if (sources.length === 0) {
      list.innerHTML = `
        <div style="padding:10px 0; color:#94a3b8; font-size:0.82rem;">
          No indexed sources matched "${escapeHtml(q)}".
          <button type="button" class="btn-sm btn-primary" id="btnInlineTelevizoExtract" style="margin-left:8px;">
            📺 Try Televizo Live Extract
          </button>
        </div>
      `;
      const btnInline = document.getElementById('btnInlineTelevizoExtract');
      if (btnInline) {
        btnInline.addEventListener('click', () => extractTelevizoForModalChannel());
      }
      return;
    }

    list.innerHTML = sources
      .map((s, idx) => {
        const badge =
          s.status === 'online'
            ? `<span class="status-dot status-online">● Online ${s.latency_ms}ms</span>`
            : `<span class="status-dot status-offline">● Offline</span>`;
        return `
          <div class="source-row-card" style="padding:8px 10px;">
            <div class="source-row-meta">
              <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
                <strong>${escapeHtml(s.name)}</strong>
                <span class="ch-cat-pill">${escapeHtml(s.quality || 'HD')}</span>
                <span class="ch-cat-pill">${escapeHtml(s.source || 'Stream')}</span>
                ${badge}
              </div>
              <div class="source-row-url">${escapeHtml(s.url)}</div>
            </div>
            <button type="button" class="btn-sm btn-source-finder" data-pick-src="${idx}">Use This Stream</button>
          </div>
        `;
      })
      .join('');

    list.querySelectorAll('[data-pick-src]').forEach((btnPick) => {
      btnPick.addEventListener('click', () => {
        const s = sources[parseInt(btnPick.getAttribute('data-pick-src'), 10)];
        document.getElementById('chUrl').value = s.url;
        if (s.logo && !document.getElementById('chLogo').value) {
          document.getElementById('chLogo').value = s.logo;
          document.getElementById('chLogoPreview').src = s.logo;
        }
        if (s.tvg_id && !document.getElementById('chTvgId').value) {
          document.getElementById('chTvgId').value = s.tvg_id;
        }
        showToast('✓ Applied working stream URL!');
      });
    });
  } catch (err) {
    statusEl.textContent = `❌ ${err.message}`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔎 Find Stream';
    }
  }
}

async function extractTelevizoForModalChannel() {
  const name = document.getElementById('chName').value.trim();
  const url = document.getElementById('chUrl').value.trim();
  const target = name || url;
  if (!target) {
    showToast('Enter a Channel Name or current URL first');
    return;
  }
  const btn = document.getElementById('btnModalTelevizoExtract');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '📺 Extracting...';
  }
  showToast(`📺 Extracting fresh live token from televizo.live for "${target}"...`);
  try {
    const res = await api('/api/televizo/extract', {
      method: 'POST',
      body: JSON.stringify({ name: target }),
    });
    if (res.stream && res.stream.url) {
      document.getElementById('chUrl').value = res.stream.url;
      if (res.stream.logo && !document.getElementById('chLogo').value) {
        document.getElementById('chLogo').value = res.stream.logo;
        document.getElementById('chLogoPreview').src = res.stream.logo;
      }
      showToast(`✅ Extracted fresh live stream: ${res.stream.name} (${res.stream.latency_ms}ms)!`);
    } else {
      showToast(`⚠️ No live stream found on televizo.live for "${target}"`);
    }
  } catch (err) {
    showToast(`❌ Televizo extract failed: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '📺 Televizo Live';
    }
  }
}

let _progressPollTimer = null;

function renderProgressOverlay(taskKey, data) {
  const banner = document.getElementById('globalProgressBanner');
  if (!banner) return;
  const labels = {
    televizo_sync: '📺 Televizo Stream Sync',
    bulk_search: '🔎 Multi-Source Channel Search',
    fallback_guard: '🛡️ 2-Hour Fallback Guard',
  };
  const titleEl = document.getElementById('gpTitle');
  const pctEl = document.getElementById('gpPercent');
  const barEl = document.getElementById('gpBar');
  const detailEl = document.getElementById('gpDetail');

  banner.classList.remove('hidden');
  if (titleEl) titleEl.textContent = labels[taskKey] || '🔄 Processing...';
  const pct = data.percent || 0;
  if (pctEl) pctEl.textContent = `${pct}% (${data.current || 0}/${data.total || 0})`;
  if (barEl) barEl.style.width = `${pct}%`;
  if (detailEl) detailEl.textContent = data.message || data.current_item || 'Working...';
}

function startProgressPolling(taskKey) {
  if (_progressPollTimer) clearInterval(_progressPollTimer);
  renderProgressOverlay(taskKey, { active: true, current: 0, total: 0, percent: 5, message: 'Starting...' });
  _progressPollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/progress').then((r) => r.json());
      const prog = res.progress || {};
      const item = prog[taskKey];
      if (!item) return;
      renderProgressOverlay(taskKey, item);
      if (!item.active && item.updated_at > 0) {
        clearInterval(_progressPollTimer);
        _progressPollTimer = null;
        setTimeout(() => {
          const b = document.getElementById('globalProgressBanner');
          if (b) b.classList.add('hidden');
          if (taskKey === 'televizo_sync' && state.currentPlaylistId) {
            loadPlaylists(state.currentPlaylistId);
          }
        }, 2800);
      }
    } catch (_) {}
  }, 800);
}

function stopProgressPolling() {
  if (_progressPollTimer) {
    clearInterval(_progressPollTimer);
    _progressPollTimer = null;
  }
  setTimeout(() => {
    const b = document.getElementById('globalProgressBanner');
    if (b) b.classList.add('hidden');
  }, 2000);
}

async function syncTelevizoPlaylist(btnEl) {
  if (!state.currentPlaylistId) {
    showToast('Select a playlist first');
    return;
  }
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = '📺 Syncing Televizo...';
  }
  showToast('📺 Connecting to televizo.live & extracting fresh tokens for matching channels...');
  startProgressPolling('televizo_sync');
  try {
    const res = await api('/api/televizo/sync-playlist', {
      method: 'POST',
      body: JSON.stringify({ playlist_id: state.currentPlaylistId }),
    });
    showToast(`🎉 ${res.message || `Televizo sync started for ${res.matched || 0} channels!`}`);
    await loadPlaylists(state.currentPlaylistId);
    if (!document.getElementById('brokenChannelsModal').classList.contains('hidden')) {
      await refreshBrokenChannelsModal();
    }
  } catch (err) {
    stopProgressPolling();
    showToast(`❌ Televizo sync failed: ${err.message}`);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = '📺 Sync Televizo';
    }
  }
}

// ---------------------------------------------------------------------------
// 2. 🚨 Non-Working Channels Center & 3x/Day Scheduled Health Monitor
// ---------------------------------------------------------------------------

async function openBrokenChannelsCenter() {
  openModal('brokenChannelsModal');
  await refreshBrokenChannelsModal();
}

function populateBrokenBulkTextarea() {
  const broken = (state.schedulerData?.broken_channels || []).filter(
    (c) => !state.currentPlaylistId || c.playlist_id === state.currentPlaylistId
  );
  const names = broken.slice(0, 100).map((c) => c.name);
  document.getElementById('brokenBulkNamesTextarea').value = names.join('\n');
  updateBulkSourceLineCounter('brokenBulkNamesTextarea', 'brokenBulkLineCount');
}

async function refreshBrokenChannelsModal() {
  const wrap = document.getElementById('brokenChannelsListWrap');
  wrap.innerHTML = `<div style="padding:24px;text-align:center;color:#94a3b8;">Loading non-working channels & 3x/day schedule...</div>`;

  try {
    const data = await api('/api/scheduler/status');
    state.schedulerData = data;
    const sched = data.scheduler || {};
    const broken = (data.broken_channels || []).filter(
      (c) => !state.currentPlaylistId || c.playlist_id === state.currentPlaylistId
    );

    const cpd = sched.checks_per_day || 12;
    const freqText = cpd === 12 ? '12x / Day (Every 2h)' : `${cpd}x / Day`;
    document.getElementById('schedFrequencyBadge').textContent = freqText;
    document.getElementById('schedFrequencySelect').value = String(cpd);
    const failovers = sched.total_failovers || 0;
    const badgeFailover = document.getElementById('schedFailoversBadge');
    if (badgeFailover) badgeFailover.textContent = `${failovers} Auto-Failovers`;
    document.getElementById('schedTimestampsText').textContent =
      `Last check: ${formatTimeAgo(sched.last_run_at)} · Next scheduled check: ${formatTimeUntil(sched.next_run_at)}`;

    const tvRenew = data.televizo_renew || {};
    const tvBadge = document.getElementById('televizoRenewBadge');
    if (tvBadge) {
      const secs = tvRenew.interval_seconds || (tvRenew.interval_hours || 5) * 3600;
      const tvEvery = secs < 3600 ? `${Math.round(secs / 60)}m` : `${Math.round(secs / 3600)}h`;
      tvBadge.textContent = tvRenew.is_running ? '🔄 Televizo: Syncing Now...' : `🔄 Televizo Auto-Renew: Every ${tvEvery}`;
    }
    const tvMeta = document.getElementById('televizoRenewMeta');
    if (tvMeta) {
      const nextTxt = formatTimeUntil(tvRenew.next_run_at);
      const lastRenewed = tvRenew.last_renewed_count || 0;
      tvMeta.textContent = `Next Televizo Token Refresh: in ${nextTxt} · Last renewed: ${lastRenewed} channels`;
    }

    if (!document.getElementById('brokenBulkNamesTextarea').value.trim() && broken.length > 0) {
      populateBrokenBulkTextarea();
    }

    if (broken.length === 0) {
      wrap.innerHTML = `
        <div class="empty-state" style="padding:40px; border:none;">
          <h3>🎉 Zero Non-Working Channels in This Playlist!</h3>
          <p>All checked channels are currently responding. ZEMYIPTV automatically checks your channels ${sched.checks_per_day || 3} times a day, or you can use <strong>🔎 Find Source by Name List (≤100)</strong> above to add new channels.</p>
        </div>
      `;
      return;
    }

    wrap.innerHTML = `
      <table class="channels-table">
        <thead>
          <tr>
            <th>Channel (${broken.length} broken)</th>
            <th>Category</th>
            <th>Broken Stream URL</th>
            <th>Failure Reason</th>
            <th>Last Checked</th>
            <th width="310">Fix or Remove</th>
          </tr>
        </thead>
        <tbody>
          ${broken
            .map(
              (ch) => `
            <tr>
              <td><strong>${escapeHtml(ch.name)}</strong></td>
              <td><span class="ch-cat-pill">${escapeHtml(ch.category)}</span></td>
              <td><div class="ch-url-preview" style="max-width:210px;">${escapeHtml(ch.url)}</div></td>
              <td><span class="status-dot status-offline">${escapeHtml(ch.last_error || 'Offline')}</span></td>
              <td><small>${formatTimeAgo(ch.last_checked_at)}</small></td>
              <td>
                <div class="card-btn-group">
                  <button class="card-btn" style="color:#6ee7b7;border-color:rgba(16,185,129,0.4);" data-broken-act="source" data-id="${escapeHtml(ch.id)}" data-pid="${escapeHtml(ch.playlist_id)}">🔎 Find Source</button>
                  <button class="card-btn" data-broken-act="edit" data-id="${escapeHtml(ch.id)}" data-pid="${escapeHtml(ch.playlist_id)}">✏️ Edit</button>
                  <button class="card-btn" data-broken-act="test" data-id="${escapeHtml(ch.id)}" data-pid="${escapeHtml(ch.playlist_id)}">🩺 Test</button>
                  <button class="card-btn del-btn" data-broken-act="delete" data-id="${escapeHtml(ch.id)}" data-pid="${escapeHtml(ch.playlist_id)}">🗑️ Delete</button>
                </div>
              </td>
            </tr>
          `
            )
            .join('')}
        </tbody>
      </table>
    `;

    // Safe event delegation for Broken Channels rows
    wrap.querySelectorAll('[data-broken-act]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const act = btn.getAttribute('data-broken-act');
        const cid = btn.getAttribute('data-id');
        const pid = btn.getAttribute('data-pid');
        const chObj = broken.find((b) => b.id === cid);

        if (act === 'source' && chObj) {
          openSingleSourceFinderModal(chObj.name, chObj.id, chObj.name);
        } else if (act === 'edit') {
          closeModal('brokenChannelsModal');
          openEditChannelModal(cid, false);
        } else if (act === 'test') {
          await recheckSingleBroken(pid, cid);
        } else if (act === 'delete') {
          await deleteBrokenChannel(pid, cid);
        }
      });
    });
  } catch (err) {
    wrap.innerHTML = `<div style="padding:20px;color:#fca5a5;">❌ ${escapeHtml(err.message)}</div>`;
  }
}

async function recheckSingleBroken(playlistId, channelId) {
  try {
    showToast('🩺 Re-testing channel...');
    await api(`/api/playlists/${playlistId}/channels/check-streams`, {
      method: 'POST',
      body: JSON.stringify({ ids: [channelId] }),
    });
    await loadPlaylists(state.currentPlaylistId);
    await refreshBrokenChannelsModal();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function deleteBrokenChannel(playlistId, channelId) {
  try {
    await api(`/api/playlists/${playlistId}/channels/bulk-delete`, {
      method: 'POST',
      body: JSON.stringify({ ids: [channelId] }),
    });
    showToast('🗑️ Deleted non-working channel');
    await loadPlaylists(state.currentPlaylistId);
    await refreshBrokenChannelsModal();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// 3. Per-Channel Upload Check Table Renderer
// ---------------------------------------------------------------------------

function renderUploadVerificationTable(channels, containerId) {
  const wrap = document.getElementById(containerId);
  wrap.classList.remove('hidden');

  if (!channels || channels.length === 0) {
    wrap.innerHTML = `<div style="padding:14px;color:#94a3b8;">No channels to preview.</div>`;
    return;
  }

  wrap.innerHTML = `
    <table class="channels-table">
      <thead>
        <tr>
          <th>Channel Name</th>
          <th>Category</th>
          <th>Stream URL</th>
          <th>Duplicate Detection</th>
          <th>Live Stream Check</th>
        </tr>
      </thead>
      <tbody>
        ${channels
          .slice(0, 150)
          .map((ch) => {
            let dupBadge = `<span class="badge badge-emerald">✓ Unique</span>`;
            if (ch.dup_playlist) {
              dupBadge = `<span class="badge badge-amber">⚠️ Already in Current Playlist</span>`;
            } else if (ch.dup_internal) {
              dupBadge = `<span class="badge badge-violet">⚠️ Duplicate in Upload List</span>`;
            }

            let statusBadge = `<small style="color:#64748b;">Not probed yet</small>`;
            if (ch.status === 'online') {
              statusBadge = `<span class="status-dot status-online">● Online (${ch.latency_ms}ms)</span>`;
            } else if (ch.status === 'offline' || ch.status === 'warning') {
              statusBadge = `<span class="status-dot status-offline">● Offline ${escapeHtml(ch.error || '')}</span>`;
            }

            return `
              <tr>
                <td><strong>${escapeHtml(ch.name)}</strong></td>
                <td><span class="ch-cat-pill">${escapeHtml(ch.category || 'Uncategorized')}</span></td>
                <td><div class="ch-url-preview" style="max-width:210px;">${escapeHtml(ch.url)}</div></td>
                <td>${dupBadge}</td>
                <td>${statusBadge}</td>
              </tr>
            `;
          })
          .join('')}
      </tbody>
    </table>
  `;
}

async function probeCandidateBatch(channels, containerId) {
  if (!channels || channels.length === 0) return;
  try {
    showToast(`🩺 Checking ${Math.min(channels.length, 200)} channel streams concurrently...`);
    const res = await api('/api/streams/probe-batch', {
      method: 'POST',
      body: JSON.stringify({ channels: channels.slice(0, 200) }),
    });
    const results = res.results || [];
    let online = 0;
    for (let i = 0; i < results.length; i++) {
      if (channels[i]) {
        channels[i].status = results[i].status;
        channels[i].latency_ms = results[i].latency_ms;
        channels[i].error = results[i].error;
        if (results[i].status === 'online') online++;
      }
    }
    renderUploadVerificationTable(channels, containerId);
    showToast(`🩺 Upload check complete: ${online}/${results.length} streams online!`);
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Channel Add / Edit / Auto Logo Finder
// ---------------------------------------------------------------------------

function openAddChannelModal() {
  document.getElementById('channelModalTitle').textContent = '＋ Add New Channel';
  document.getElementById('chId').value = '';
  document.getElementById('chName').value = '';
  document.getElementById('chCategory').value =
    state.selectedCategory !== 'ALL' ? state.selectedCategory : 'Entertainment';
  document.getElementById('chUrl').value = '';
  document.getElementById('chLogo').value = '';
  document.getElementById('chTvgId').value = '';
  document.getElementById('chTvgName').value = '';
  document.getElementById('chNumber').value = state.channels.length + 1;
  document.getElementById('chUserAgent').value = '';
  document.getElementById('chReferrer').value = '';
  document.getElementById('chLogoPreview').src = makeFallbackLogo('New');
  document.getElementById('modalLogoCandidates').innerHTML = '';
  document.getElementById('modalSourceCandidatesBox').classList.add('hidden');
  const backupsPanel = document.getElementById('channelBackupsPanel');
  if (backupsPanel) backupsPanel.classList.add('hidden');
  openModal('channelModal');
}

async function openEditChannelModal(channelId, autoTriggerLogoSearch = false) {
  const ch = state.channels.find((c) => c.id === channelId);
  if (!ch) return;

  document.getElementById('channelModalTitle').textContent = `✏️ Edit Channel — ${ch.name}`;
  document.getElementById('chId').value = ch.id;
  document.getElementById('chName').value = ch.name;
  document.getElementById('chCategory').value = ch.category || 'Uncategorized';
  document.getElementById('chUrl').value = ch.url || '';
  document.getElementById('chLogo').value = ch.logo || '';
  document.getElementById('chTvgId').value = ch.tvg_id || '';
  document.getElementById('chTvgName').value = ch.tvg_name || ch.name;
  document.getElementById('chNumber').value = ch.chno || '';
  document.getElementById('chUserAgent').value = ch.user_agent || '';
  document.getElementById('chReferrer').value = ch.referrer || '';
  document.getElementById('chLogoPreview').src = ch.logo || makeFallbackLogo(ch.name);
  document.getElementById('modalLogoCandidates').innerHTML = '';
  document.getElementById('modalSourceCandidatesBox').classList.add('hidden');

  const backupsPanel = document.getElementById('channelBackupsPanel');
  if (backupsPanel) backupsPanel.classList.remove('hidden');

  openModal('channelModal');
  loadChannelBackupSources(ch.id);
  if (autoTriggerLogoSearch) {
    await searchLogosInModal();
  }
}

async function loadChannelBackupSources(channelId) {
  const listEl = document.getElementById('channelBackupsList');
  if (!listEl) return;
  listEl.innerHTML = `<div style="color:#94a3b8; font-size:0.85rem; padding:8px 0;">⏳ Loading backup sources...</div>`;

  try {
    const data = await api(`/api/channels/${encodeURIComponent(channelId)}/sources`);
    const sources = data.sources || [];
    if (sources.length === 0) {
      listEl.innerHTML = `
        <div style="color:#64748b; font-size:0.85rem; padding:8px 0;">
          No backup sources stored yet. Click <strong>⚡ Auto-Find 3-4 Backups</strong> to find alternative live streams or add a custom URL below.
        </div>
      `;
      return;
    }

    listEl.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:8px; margin-top:6px;">
        ${sources.map((s, idx) => {
          const isPrimary = (s.url === data.primary_url) || (s.priority === 1);
          const statusDot = s.status === 'online'
            ? `<span class="status-dot status-online" style="font-size:0.75rem;">● Online ${s.latency_ms ? s.latency_ms + 'ms' : ''}</span>`
            : s.status === 'offline'
            ? `<span class="status-dot status-offline" style="font-size:0.75rem;">● Offline</span>`
            : `<span class="status-dot" style="font-size:0.75rem; color:#94a3b8;">● Unchecked</span>`;

          const priorityBadge = isPrimary
            ? `<span class="badge badge-success" style="font-size:0.7rem;">⭐ Active Primary</span>`
            : `<span class="badge badge-indigo" style="font-size:0.7rem;">Backup #${idx + 1}</span>`;

          return `
            <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; background:rgba(15,23,42,0.6); padding:8px 12px; border-radius:8px; border:1px solid rgba(255,255,255,0.06);">
              <div style="flex:1; min-width:0;">
                <div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">
                  ${priorityBadge}
                  <strong style="font-size:0.82rem; color:#f1f5f9;">${escapeHtml(s.source_name || 'Backup Stream')}</strong>
                  ${statusDot}
                </div>
                <div style="font-size:0.75rem; color:#94a3b8; font-family:monospace; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(s.url)}">
                  ${escapeHtml(s.url)}
                </div>
              </div>
              <div style="display:flex; align-items:center; gap:6px; flex-shrink:0;">
                <button type="button" class="card-btn play-btn" data-backup-act="play" data-src-url="${escapeHtml(s.url)}" style="padding:3px 8px; font-size:0.75rem;">▶ Play</button>
                ${!isPrimary ? `<button type="button" class="card-btn" data-backup-act="promote" data-src-id="${escapeHtml(s.id)}" style="padding:3px 8px; font-size:0.75rem; color:#38bdf8; border-color:rgba(56,189,248,0.4);" title="Make this the active stream URL in your playlist">⭐ Make Primary</button>` : ''}
                <button type="button" class="card-btn del-btn" data-backup-act="delete" data-src-id="${escapeHtml(s.id)}" style="padding:3px 8px; font-size:0.75rem;" title="Delete backup source">🗑️</button>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;

    listEl.querySelectorAll('[data-backup-act]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const act = btn.getAttribute('data-backup-act');
        const srcId = btn.getAttribute('data-src-id');
        const srcUrl = btn.getAttribute('data-src-url');
        if (act === 'play') {
          playStreamUrl('Backup Stream', srcUrl);
        } else if (act === 'promote') {
          await promoteBackupSource(channelId, srcId);
        } else if (act === 'delete') {
          await deleteBackupSource(channelId, srcId);
        }
      });
    });
  } catch (err) {
    listEl.innerHTML = `<div style="color:#f87171; font-size:0.82rem;">Failed to load backup sources: ${escapeHtml(err.message)}</div>`;
  }
}

async function promoteBackupSource(channelId, sourceId) {
  try {
    const res = await api(`/api/channels/${encodeURIComponent(channelId)}/promote-source`, {
      method: 'POST',
      body: JSON.stringify({ source_id: sourceId }),
    });
    showToast('⭐ Promoted backup to primary stream URL!');
    if (res.new_primary_url) {
      document.getElementById('chUrl').value = res.new_primary_url;
    }
    await loadChannelBackupSources(channelId);
    await loadPlaylists(state.currentPlaylistId);
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function deleteBackupSource(channelId, sourceId) {
  try {
    await api(`/api/channels/${encodeURIComponent(channelId)}/delete-source`, {
      method: 'POST',
      body: JSON.stringify({ source_id: sourceId }),
    });
    showToast('🗑️ Backup source deleted');
    await loadChannelBackupSources(channelId);
    await loadPlaylists(state.currentPlaylistId);
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function searchLogosInModal() {
  const name = document.getElementById('chName').value.trim();
  const tvgId = document.getElementById('chTvgId').value.trim();
  if (!name && !tvgId) {
    showToast('Enter a Channel Name first to search logos');
    return;
  }

  const row = document.getElementById('modalLogoCandidates');
  row.innerHTML = `<span style="font-size:0.76rem;color:#a5b4fc;">Searching logo database...</span>`;

  try {
    const data = await api(
      `/api/logos/search?q=${encodeURIComponent(name)}&tvg_id=${encodeURIComponent(tvgId)}`
    );
    const cands = data.candidates || [];

    if (cands.length === 0) {
      row.innerHTML = `<span style="font-size:0.78rem;color:#94a3b8;padding:4px 0;">No direct logos matched in catalog. You can paste an image URL directly into the Logo field.</span>`;
      showToast('⚠️ No matching logos found in catalog');
      return;
    }

    if (cands.length > 0) {
      document.getElementById('chLogo').value = cands[0].url;
      document.getElementById('chLogoPreview').src = cands[0].url;
      if (!document.getElementById('chTvgId').value && cands[0].id) {
        document.getElementById('chTvgId').value = cands[0].id;
      }
    }

    row.innerHTML = cands
      .map(
        (c) => `
        <div class="logo-cand-item" title="${escapeHtml(c.name)} (${escapeHtml(c.source)})" data-url="${escapeHtml(c.url)}" data-id="${escapeHtml(c.id || '')}">
          <img src="${escapeHtml(c.url)}" alt="${escapeHtml(c.name)}" />
        </div>
      `
      )
      .join('');

    row.querySelectorAll('.logo-cand-item').forEach((item) => {
      item.addEventListener('click', () => {
        const url = item.getAttribute('data-url');
        const cid = item.getAttribute('data-id');
        document.getElementById('chLogo').value = url;
        document.getElementById('chLogoPreview').src = url;
        if (cid && !document.getElementById('chTvgId').value) {
          document.getElementById('chTvgId').value = cid;
        }
      });
    });
  } catch (err) {
    row.innerHTML = `<small style="color:#fca5a5;">${escapeHtml(err.message)}</small>`;
  }
}

async function quickAutoLogo(channelId) {
  try {
    const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/auto-logos`, {
      method: 'POST',
      body: JSON.stringify({ ids: [channelId], overwrite: true, include_generated: true }),
    });
    showToast(
      res.matched_real > 0
        ? '✨ Found & applied official channel logo!'
        : '✨ Applied custom studio badge logo!'
    );
    await loadCurrentPlaylistDetails();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function deleteSingleChannel(channelId) {
  try {
    await api(`/api/playlists/${state.currentPlaylistId}/channels/bulk-delete`, {
      method: 'POST',
      body: JSON.stringify({ ids: [channelId] }),
    });
    state.selectedChannelIds.delete(channelId);
    showToast('🗑️ Channel removed from playlist');
    await loadPlaylists(state.currentPlaylistId);
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

async function removeChannelFromCategory(channelId) {
  try {
    await api(`/api/playlists/${state.currentPlaylistId}/channels/bulk-category`, {
      method: 'POST',
      body: JSON.stringify({ ids: [channelId], category: 'Uncategorized' }),
    });
    showToast('🏷️ Channel removed from category (moved to Uncategorized)');
    await loadCurrentPlaylistDetails();
  } catch (err) {
    showToast(`❌ ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// Live HLS (.m3u8) Stream Player
// ---------------------------------------------------------------------------

// hls.js is loaded on demand (first playback) instead of blocking page load.
const HLS_CDN_URL = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.13';
let hlsLoaderPromise = null;

function loadHls() {
  if (window.Hls) return Promise.resolve();
  if (!hlsLoaderPromise) {
    hlsLoaderPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = HLS_CDN_URL;
      script.onload = resolve;
      script.onerror = () => {
        hlsLoaderPromise = null;
        reject(new Error('hls.js failed to load'));
      };
      document.head.appendChild(script);
    });
  }
  return hlsLoaderPromise;
}

// dembed.st serves its player only to pages referred from thedaddy.li; from any
// other referrer it answers 403 and renders "Not allowed to stream". Their own
// watch page is the sanctioned embedder, so map a dembed URL to it.
function providerWatchUrl(target) {
  try {
    const parsed = new URL(target, window.location.href);
    if (!/(^|\.)dembed\.st$/i.test(parsed.hostname)) return '';
    const id = parsed.searchParams.get('id') || '';
    return /^\d+$/.test(id) ? `https://thedaddy.li/stream/stream-${id}.php` : '';
  } catch (_) {
    return '';
  }
}

function playStreamUrl(name, url, forceProxy = false, referrer = '', userAgent = '', playMode = '') {
  const dock = document.getElementById('livePlayerDock');
  const video = document.getElementById('hlsVideo');
  const overlay = document.getElementById('playerStatusOverlay');
  const proxyBadge = document.getElementById('playerProxyBadge');
  const proxyToggleState = document.getElementById('proxyToggleState');

  dock.classList.remove('hidden');
  overlay.classList.add('hidden');
  document.getElementById('playerChannelName').textContent = name || 'Stream Preview';
  document.getElementById('playerStreamUrl').value = url;

  const staleFrame = document.getElementById('embedPlayerFrame');
  if (staleFrame) staleFrame.remove();
  const staleOpenLink = document.getElementById('embedOpenLink');
  if (staleOpenLink) staleOpenLink.remove();
  video.style.display = '';

  if (state.hlsInstance) {
    state.hlsInstance.destroy();
    state.hlsInstance = null;
  }

  // Streams that need a Referer/User-Agent cannot send one from a <video> tag,
  // so they always start through the proxy, which forwards those headers.
  // tvnow's cache edge (wonvt.st) only answers the tvnow.st referrer and its CORS
  // allows that origin alone, so it has to go through the proxy even when the
  // channel record carries no referrer; the proxy supplies the default.
  let refererLockedHost = false;
  try {
    refererLockedHost = /(^|\.)wonvt\.st$/i.test(new URL(url, window.location.href).hostname);
  } catch (_) {}
  const needsHeaders = Boolean(referrer || userAgent || refererLockedHost);
  const useProxy = Boolean(forceProxy || needsHeaders || state.forceStreamProxy);
  const proxyUrlFor = (target) => {
    let out = `/api/proxy-stream?url=${encodeURIComponent(target)}`;
    if (referrer) out += `&ref=${encodeURIComponent(referrer)}`;
    if (userAgent) out += `&ua=${encodeURIComponent(userAgent)}`;
    return out;
  };
  if (proxyToggleState) {
    proxyToggleState.textContent = useProxy ? 'ON (Proxy)' : 'Auto';
  }
  if (proxyBadge) {
    proxyBadge.classList.toggle('hidden', !useProxy);
  }

  // Provider pages that run their own player (dembed.st, thedaddy.li watch pages,
  // pflix.net) refuse this server's datacenter IP and set a signed/Cloudflare
  // challenge no plain fetch can satisfy. Embedding the page lets the viewer's own
  // browser load it from their IP, exactly like the provider's site does.
  const EMBED_HOSTS = /(^|\.)(dembed\.st|pflix\.net|thedaddy\.li|dlhd\.dad)$/i;
  let embedMode = playMode === 'iframe';
  try {
    embedMode = embedMode || EMBED_HOSTS.test(new URL(url, window.location.href).hostname);
  } catch (_) {}

  if (embedMode) {
    const frame = document.createElement('iframe');
    frame.id = 'embedPlayerFrame';
    frame.className = 'embed-player-frame';
    // dembed.st answers 403 and "Not allowed to stream" unless its own request
    // carries a thedaddy.li referrer, which a frame we create never has. Its watch
    // page embeds the player with that referrer, so frame that and let it embed.
    const frameSrc = providerWatchUrl(url) || url;
    frame.src = frameSrc;
    frame.setAttribute('allow', 'autoplay; fullscreen; encrypted-media; picture-in-picture');
    frame.setAttribute('allowfullscreen', '');
    // The provider player refuses to play when it sees no referrer, so send our origin
    // instead of stripping it; cross-origin embeds only ever receive the origin.
    frame.referrerPolicy = 'origin';
    video.style.display = 'none';
    video.parentElement.insertBefore(frame, video);
    const openLink = document.createElement('a');
    openLink.id = 'embedOpenLink';
    openLink.className = 'embed-open-link';
    openLink.href = `/watch?u=${encodeURIComponent(frameSrc)}&name=${encodeURIComponent(name || '')}`;
    openLink.target = '_blank';
    openLink.rel = 'noopener';
    openLink.textContent = 'Open player in its own page';
    video.parentElement.insertBefore(openLink, video);
    if (proxyBadge) proxyBadge.classList.add('hidden');
    if (proxyToggleState) proxyToggleState.textContent = 'Embedded player';
    return;
  }

  let triedProxyFallback = useProxy;

  function initPlayer(srcUrl, isProxied) {
    if (state.hlsInstance) {
      state.hlsInstance.destroy();
      state.hlsInstance = null;
    }

    if (proxyBadge) {
      proxyBadge.classList.toggle('hidden', !isProxied);
    }

    if (window.Hls && Hls.isSupported() && (srcUrl.includes('.m3u8') || !srcUrl.endsWith('.mp4') || isProxied)) {
      const hls = new Hls({
        enableWorker: true,
        manifestLoadingTimeOut: 12000,
        levelLoadingTimeOut: 12000,
        fragLoadingTimeOut: 14000,
      });
      state.hlsInstance = hls;
      hls.loadSource(srcUrl);
      hls.attachMedia(video);

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        overlay.classList.add('hidden');
        video.play().catch(() => {});
      });

      hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
          // If direct load failed due to browser CORS or network error, immediately retry via our server CORS proxy!
          if (!triedProxyFallback && (data.type === Hls.ErrorTypes.NETWORK_ERROR || data.details === 'manifestLoadError')) {
            triedProxyFallback = true;
            console.log('[Player] Direct playback blocked by CORS. Auto-switching to built-in CORS Proxy...');
            const proxyUrl = proxyUrlFor(url);
            initPlayer(proxyUrl, true);
            return;
          }

          overlay.classList.remove('hidden');
          const details = data.details || data.type;
          overlay.innerHTML = `
            <div style="text-align:center; padding:12px 16px;">
              <p style="font-weight:700; color:#f87171; margin-bottom:6px; font-size:0.85rem;">⚠️ Stream could not be decoded in-browser</p>
              <p style="font-size:0.75rem; color:#cbd5e1; margin-bottom:12px; line-height:1.4;">${isProxied ? 'Remote stream server is offline, geo-restricted (403), or unreachable.' : 'Remote server did not respond with browser-compatible CORS headers.'}</p>
              <div style="display:flex; gap:8px; justify-content:center; flex-wrap:wrap;">
                <button type="button" class="btn btn-sm btn-primary" id="btnRetryProxyNow">⚡ Retry via Proxy</button>
                <button type="button" class="btn btn-sm btn-secondary" id="btnCopyForVlcNow">📋 Copy for VLC</button>
              </div>
            </div>
          `;
          const btnRetry = document.getElementById('btnRetryProxyNow');
          if (btnRetry) {
            btnRetry.addEventListener('click', () => {
              playStreamUrl(name, url, true);
            });
          }
          const btnCopy = document.getElementById('btnCopyForVlcNow');
          if (btnCopy) {
            btnCopy.addEventListener('click', () => {
              navigator.clipboard.writeText(url);
              showToast('📋 Stream URL copied to clipboard for VLC / TiviMate!');
            });
          }
        }
      });
    } else {
      video.src = srcUrl;
      video.play().catch(() => {
        if (!triedProxyFallback) {
          triedProxyFallback = true;
          initPlayer(proxyUrlFor(url), true);
          return;
        }
        overlay.classList.remove('hidden');
        overlay.textContent = 'Unable to play stream directly in browser.';
      });
    }
  }

  const initialUrl = useProxy ? proxyUrlFor(url) : url;
  loadHls()
    .catch(() => {})
    .then(() => initPlayer(initialUrl, useProxy));
}

function playChannel(channelId) {
  const ch = state.channels.find((c) => c.id === channelId);
  if (!ch) return;
  playStreamUrl(ch.name, ch.url, false, ch.referrer || '', ch.user_agent || '', ch.play_mode || '');
}

// ---------------------------------------------------------------------------
// Category Manager Modal (Safe Event Delegation)
// ---------------------------------------------------------------------------

function openCategoryManager() {
  const cats = getCategoryCounts();
  const list = document.getElementById('categoryManagerList');

  if (cats.length === 0) {
    list.innerHTML = `<p style="color:#94a3b8;">No categories in this playlist yet.</p>`;
  } else {
    list.innerHTML = cats
      .map(
        ([cat, count], i) => `
        <div class="cat-mgr-row">
          <div class="cat-mgr-left">
            <span>📁</span>
            <input type="text" id="catInput_${i}" value="${escapeHtml(cat)}" />
            <span class="nav-count">${count} ch</span>
          </div>
          <div class="card-btn-group">
            <button class="btn-sm btn-glass" data-cat-act="rename" data-cat-idx="${i}">Rename</button>
            <button class="btn-sm btn-glass" data-cat-act="empty" data-cat-idx="${i}">Empty Category</button>
            <button class="btn-sm btn-danger" data-cat-act="delete" data-cat-idx="${i}">Delete Channels</button>
          </div>
        </div>
      `
      )
      .join('');

    list.querySelectorAll('[data-cat-act]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const act = btn.getAttribute('data-cat-act');
        const idx = parseInt(btn.getAttribute('data-cat-idx'), 10);
        const oldCat = cats[idx][0];
        if (act === 'rename') {
          await renameCategory(oldCat, `catInput_${idx}`);
        } else if (act === 'empty') {
          await unassignCategory(oldCat);
        } else if (act === 'delete') {
          await deleteCategoryAndChannels(oldCat);
        }
      });
    });
  }

  openModal('categoryModal');
}

async function renameCategory(oldCat, inputId) {
  const newCat = document.getElementById(inputId).value.trim();
  if (!newCat || newCat === oldCat) return;
  await api(`/api/playlists/${state.currentPlaylistId}/channels/category-action`, {
    method: 'POST',
    body: JSON.stringify({ operation: 'rename', category: oldCat, new_category: newCat }),
  });
  if (state.selectedCategory === oldCat) state.selectedCategory = newCat;
  showToast(`📁 Renamed category "${oldCat}" → "${newCat}"`);
  await loadPlaylists(state.currentPlaylistId);
  openCategoryManager();
}

async function unassignCategory(cat) {
  await api(`/api/playlists/${state.currentPlaylistId}/channels/category-action`, {
    method: 'POST',
    body: JSON.stringify({ operation: 'unassign_category', category: cat }),
  });
  if (state.selectedCategory === cat) state.selectedCategory = 'ALL';
  showToast(`🏷️ Removed channels from "${cat}"`);
  await loadPlaylists(state.currentPlaylistId);
  openCategoryManager();
}

async function deleteCategoryAndChannels(cat) {
  if (!confirm(`Delete category "${cat}" and remove all channels inside it from this playlist?`)) {
    return;
  }
  await api(`/api/playlists/${state.currentPlaylistId}/channels/category-action`, {
    method: 'POST',
    body: JSON.stringify({ operation: 'remove_channels', category: cat }),
  });
  if (state.selectedCategory === cat) state.selectedCategory = 'ALL';
  showToast(`🗑️ Deleted category "${cat}" and its channels`);
  await loadPlaylists(state.currentPlaylistId);
  openCategoryManager();
}

// ---------------------------------------------------------------------------
// Event Listeners Setup
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  loadPlaylists();
  setInterval(() => {
    loadNetworkInfo();
    loadSchedulerStatus();
  }, 12000);

  const heroCta = document.getElementById('btnHeroStart');
  if (heroCta) {
    heroCta.addEventListener('click', () => {
      const target = document.getElementById('searchInput');
      if (target) target.focus();
    });
  }

  // Search & Filter
  const searchInput = document.getElementById('searchInput');
  const clearBtn = document.getElementById('btnClearSearch');
  searchInput.addEventListener('input', (e) => {
    state.searchQuery = e.target.value.trim();
    clearBtn.classList.toggle('hidden', !state.searchQuery);
    renderChannels();
  });
  clearBtn.addEventListener('click', () => {
    searchInput.value = '';
    state.searchQuery = '';
    clearBtn.classList.add('hidden');
    renderChannels();
  });

  document.getElementById('statusFilter').addEventListener('change', (e) => {
    state.statusFilter = e.target.value;
    renderCategoriesSidebar();
    renderChannels();
  });

  // View Mode
  document.getElementById('btnViewGrid').addEventListener('click', () => {
    state.viewMode = 'grid';
    document.getElementById('btnViewGrid').classList.add('active');
    document.getElementById('btnViewTable').classList.remove('active');
    renderChannels();
  });
  document.getElementById('btnViewTable').addEventListener('click', () => {
    state.viewMode = 'table';
    document.getElementById('btnViewTable').classList.add('active');
    document.getElementById('btnViewGrid').classList.remove('active');
    renderChannels();
  });

  // Public Accessible URL & Tunnel Modal Controls
  const openPublicUrlHandler = async () => {
    await loadNetworkInfo();
    openModal('publicUrlModal');
  };
  document.getElementById('btnOpenTunnelModal').addEventListener('click', openPublicUrlHandler);
  document.getElementById('btnOpenTunnelHero').addEventListener('click', openPublicUrlHandler);

  document.getElementById('btnCopyLiveUrl').addEventListener('click', () => {
    navigator.clipboard.writeText(document.getElementById('livePlaylistUrl').value);
    showToast('🔗 Copied M3U subscription URL!');
  });
  document.getElementById('btnCopyLanUrl').addEventListener('click', () => {
    navigator.clipboard.writeText(document.getElementById('lanPlaylistUrl').value);
    showToast('🏠 Copied Local Wi-Fi / LAN M3U URL!');
  });
  document.getElementById('btnModalCopyPublicUrl').addEventListener('click', () => {
    navigator.clipboard.writeText(document.getElementById('modalPublicPlaylistUrl').value);
    showToast('🌍 Copied Public Internet M3U URL!');
  });
  document.getElementById('btnModalCopyLanUrl').addEventListener('click', () => {
    navigator.clipboard.writeText(document.getElementById('modalLanPlaylistUrl').value);
    showToast('🏠 Copied LAN M3U URL!');
  });

  document.getElementById('btnRestartTunnelLhr').addEventListener('click', async () => {
    try {
      showToast('🔄 Starting public HTTPS tunnel (localhost.run)...');
      state.networkInfo = await api('/api/tunnel/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'start', provider: 'localhost.run' }),
      });
      updateUrlBoxes();
      showToast(
        state.networkInfo.public_url
          ? `🌍 Public URL Live: ${state.networkInfo.public_url}`
          : '⏳ Tunnel connecting...'
      );
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnRestartTunnelPinggy').addEventListener('click', async () => {
    try {
      showToast('🔄 Starting public HTTPS tunnel (Pinggy.io)...');
      state.networkInfo = await api('/api/tunnel/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'start', provider: 'pinggy' }),
      });
      updateUrlBoxes();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnStopTunnel').addEventListener('click', async () => {
    try {
      state.networkInfo = await api('/api/tunnel/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'stop' }),
      });
      updateUrlBoxes();
      showToast('🛑 Public tunnel stopped');
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnSaveCustomBaseUrl').addEventListener('click', async () => {
    try {
      const custom_base_url = document.getElementById('customBaseUrlInput').value.trim();
      state.networkInfo = await api('/api/tunnel/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'custom', custom_base_url }),
      });
      updateUrlBoxes();
      showToast('✅ Saved custom public domain!');
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // =========================================================================
  // Main Panel Source Finder Modal (Bulk ≤100 Channel Names + Single Search)
  // =========================================================================

  document.getElementById('btnOpenSourceFinder').addEventListener('click', openBulkSourceFinderModal);
  document.getElementById('tabBtnBulkSource').addEventListener('click', () => switchSourceModalTab('bulk'));
  document.getElementById('tabBtnSingleSource').addEventListener('click', () => switchSourceModalTab('single'));
  const tabBtnGh = document.getElementById('tabBtnGithubSource');
  if (tabBtnGh) tabBtnGh.addEventListener('click', () => switchSourceModalTab('github'));
  const tabBtnProv = document.getElementById('tabBtnProviders');
  if (tabBtnProv) tabBtnProv.addEventListener('click', () => switchSourceModalTab('providers'));

  const btnToggleAdd = document.getElementById('btnToggleAddProvider');
  if (btnToggleAdd) {
    btnToggleAdd.addEventListener('click', () => {
      document.getElementById('addCustomProviderBox').classList.toggle('hidden');
    });
  }
  const btnCancelAdd = document.getElementById('btnCancelAddProvider');
  if (btnCancelAdd) {
    btnCancelAdd.addEventListener('click', () => {
      document.getElementById('addCustomProviderBox').classList.add('hidden');
    });
  }
  const btnCommitAdd = document.getElementById('btnCommitAddProvider');
  if (btnCommitAdd) {
    btnCommitAdd.addEventListener('click', async () => {
      const name = document.getElementById('newProvName').value.trim();
      const url = document.getElementById('newProvUrl').value.trim();
      const badge = document.getElementById('newProvBadge').value.trim();
      if (!name || !url) {
        showToast('Please enter both Provider Name and M3U URL');
        return;
      }
      btnCommitAdd.disabled = true;
      btnCommitAdd.textContent = 'Fetching & Indexing...';
      try {
        const res = await api('/api/sources/providers/add', {
          method: 'POST',
          body: JSON.stringify({ name, url, badge }),
        });
        showToast(`🎉 ${res.message}`);
        document.getElementById('newProvName').value = '';
        document.getElementById('newProvUrl').value = '';
        document.getElementById('newProvBadge').value = '';
        document.getElementById('addCustomProviderBox').classList.add('hidden');
        await loadSourceProviders();
      } catch (err) {
        showToast(`❌ ${err.message}`);
      } finally {
        btnCommitAdd.disabled = false;
        btnCommitAdd.textContent = 'Save & Index Source';
      }
    });
  }
  const btnSyncAll = document.getElementById('btnSyncAllProviders');
  if (btnSyncAll) {
    btnSyncAll.addEventListener('click', async () => {
      btnSyncAll.disabled = true;
      btnSyncAll.textContent = '🔄 Syncing...';
      try {
        await api('/api/sources/providers/sync', { method: 'POST' });
        showToast('🔄 Background sync started across all active platforms!');
        setTimeout(loadSourceProviders, 2500);
      } catch (err) {
        showToast(`❌ ${err.message}`);
      } finally {
        setTimeout(() => {
          btnSyncAll.disabled = false;
          btnSyncAll.textContent = '🔄 Sync All Sources';
        }, 1500);
      }
    });
  }

  document.querySelectorAll('[data-probe-source]').forEach((btn) => {
    btn.addEventListener('click', () => probeSourceKind(btn.getAttribute('data-probe-source'), btn));
  });

  document.getElementById('bulkSourceNamesInput').addEventListener('input', () => {
    updateBulkSourceLineCounter('bulkSourceNamesInput', 'bulkSourceLineCount');
  });

  document.getElementById('btnLoadSampleBulkNames').addEventListener('click', () => {
    document.getElementById('bulkSourceNamesInput').value = [
      'BBC News',
      'Sky News',
      'Red Bull TV',
      'Bloomberg TV',
      'CBS News',
      'ABC News Live',
      'NASA TV Public',
      'DW English',
      'Al Jazeera English',
      'France 24 English',
      'NHK World Japan',
      'Euronews English',
    ].join('\n');
    updateBulkSourceLineCounter('bulkSourceNamesInput', 'bulkSourceLineCount');
  });

  document.getElementById('btnLoadBrokenIntoBulkNames').addEventListener('click', () => {
    const broken = state.channels.filter((c) => c.status === 'offline' || c.status === 'warning');
    if (broken.length === 0) {
      showToast('No non-working channels in current playlist — loaded sample names instead');
      document.getElementById('btnLoadSampleBulkNames').click();
      return;
    }
    document.getElementById('bulkSourceNamesInput').value = broken
      .slice(0, 100)
      .map((c) => c.name)
      .join('\n');
    updateBulkSourceLineCounter('bulkSourceNamesInput', 'bulkSourceLineCount');
    showToast(`Loaded ${Math.min(broken.length, 100)} non-working channel names`);
  });

  document.getElementById('btnRunBulkSourceSearch').addEventListener('click', async () => {
    const raw_text = document.getElementById('bulkSourceNamesInput').value.trim();
    if (!raw_text) {
      showToast('Enter at least 1 channel name (up to 100, one per line)');
      return;
    }
    const country = document.getElementById('bulkSourceCountryFilter') ? document.getElementById('bulkSourceCountryFilter').value : '';
    const strict = document.getElementById('bulkSourceStrictMode') ? document.getElementById('bulkSourceStrictMode').checked : true;
    const probe_live = document.getElementById('bulkSourceProbeLive').checked;
    const include_web = document.getElementById('bulkSourceIncludeWeb') ? document.getElementById('bulkSourceIncludeWeb').checked : true;
    const include_yt = document.getElementById('bulkSourceIncludeYoutube') ? document.getElementById('bulkSourceIncludeYoutube').checked : true;
    const include_rutube = document.getElementById('bulkSourceIncludeRutube') ? document.getElementById('bulkSourceIncludeRutube').checked : true;
    const include_televizo = document.getElementById('bulkSourceIncludeTelevizo') ? document.getElementById('bulkSourceIncludeTelevizo').checked : true;
    const include_smotret = document.getElementById('bulkSourceIncludeSmotret') ? document.getElementById('bulkSourceIncludeSmotret').checked : true;
    const btn = document.getElementById('btnRunBulkSourceSearch');
    btn.disabled = true;
    btn.textContent = '🔎 Searching & Probing (≤100)...';
    startProgressPolling('bulk_search');

    try {
      const res = await api('/api/sources/bulk-search', {
        method: 'POST',
        body: JSON.stringify({ raw_text, probe_live, include_web, include_yt, include_rutube, include_televizo, include_smotret, country, strict }),
      });
      stopProgressPolling();
      state.bulkSourceMainResults = res.results || [];
      renderBulkSourceResultsTable(state.bulkSourceMainResults, 'bulkSourceResultsWrap');
      document.getElementById('btnImportBulkFoundSources').classList.toggle(
        'hidden',
        res.found_count === 0
      );
      showToast(
        `🔎 Matched ${res.found_count}/${res.total_requested} channels (${res.online_count} online)!`
      );
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '🔎 Find Sources for List (≤100)';
    }
  });

  document.getElementById('btnImportBulkFoundSources').addEventListener('click', async () => {
    const onlyOnline = document.getElementById('bulkSourceOnlyOnline').checked;
    const skipDups = document.getElementById('bulkSourceSkipDups').checked;
    const targetCat = document.getElementById('bulkSourceTargetCategory').value.trim();

    const toImport = state.bulkSourceMainResults
      .filter((r) => r.found && r.url && (!onlyOnline || r.status === 'online'))
      .map((r) => ({
        name: r.requested_name || r.name,
        url: r.url,
        logo: r.logo,
        category: targetCat || r.category || 'Live TV',
        tvg_id: r.tvg_id,
        status: r.status,
        latency_ms: r.latency_ms,
      }));

    if (toImport.length === 0) {
      showToast('⚠️ No matching channels to import');
      return;
    }

    try {
      const res = await api('/api/import/commit', {
        method: 'POST',
        body: JSON.stringify({
          playlist_id: state.currentPlaylistId,
          channels: toImport,
          deduplicate_urls: skipDups,
          deduplicate_internal: true,
        }),
      });
      showToast(`🎉 Added ${res.inserted} channel(s) from your name list!`);
      closeModal('sourceFinderModal');
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // Single channel deep search listeners
  document.getElementById('btnRunSourceFinder').addEventListener('click', runDedicatedSourceSearch);
  document.getElementById('sourceFinderQuery').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runDedicatedSourceSearch();
  });
  const sfCountryFilter = document.getElementById('sourceFinderCountryFilter');
  if (sfCountryFilter) {
    sfCountryFilter.addEventListener('change', () => {
      if (document.getElementById('sourceFinderQuery').value.trim()) runDedicatedSourceSearch();
    });
  }
  const sfStrictMode = document.getElementById('sourceFinderStrictMode');
  if (sfStrictMode) {
    sfStrictMode.addEventListener('change', () => {
      if (document.getElementById('sourceFinderQuery').value.trim()) runDedicatedSourceSearch();
    });
  }
  document.getElementById('btnClearReplaceMode').addEventListener('click', () => {
    document.getElementById('sourceFinderReplaceChannelId').value = '';
    document.getElementById('sourceFinderReplaceBanner').classList.add('hidden');
  });
  document.querySelectorAll('.source-preset-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.getElementById('sourceFinderQuery').value = chip.getAttribute('data-q');
      runDedicatedSourceSearch();
    });
  });

  // GitHub & Open-Web Stream Finder Listeners
  const btnRunGh = document.getElementById('btnRunGithubRepoSearch');
  if (btnRunGh) btnRunGh.addEventListener('click', () => runGithubRepoSearch());
  const inputGhQuery = document.getElementById('githubRepoSearchQuery');
  if (inputGhQuery) {
    inputGhQuery.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') runGithubRepoSearch();
    });
  }
  document.querySelectorAll('.gh-preset-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      runGithubRepoSearch(chip.getAttribute('data-q'));
    });
  });

  // YouTube 24/7 Live Stream Detector Listeners
  const btnRunYt = document.getElementById('btnRunYtLiveSearch');
  if (btnRunYt) btnRunYt.addEventListener('click', () => runYoutubeLiveSearch());
  const inputYtQuery = document.getElementById('ytLiveSearchQuery');
  if (inputYtQuery) {
    inputYtQuery.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') runYoutubeLiveSearch();
    });
  }
  document.querySelectorAll('.yt-preset-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      runYoutubeLiveSearch(chip.getAttribute('data-q'));
    });
  });

  // GitHub Preview Modal Action Listeners
  const ghFilterInput = document.getElementById('githubPreviewFilterInput');
  if (ghFilterInput) {
    ghFilterInput.addEventListener('input', (e) => {
      if (!state.githubPreviewData || !state.githubPreviewData.channels) return;
      const q = e.target.value.toLowerCase().trim();
      const filtered = state.githubPreviewData.channels.filter((c) => {
        if (!q) return true;
        return (
          (c.name || '').toLowerCase().includes(q) ||
          (c.category || '').toLowerCase().includes(q) ||
          (c.url || '').toLowerCase().includes(q)
        );
      });
      renderGithubPreviewChannelsTable(filtered);
    });
  }
  const btnProbeGhPreview = document.getElementById('btnProbeGithubPreviewChannels');
  if (btnProbeGhPreview) {
    btnProbeGhPreview.addEventListener('click', probeGithubPreviewChannels);
  }
  const btnImportAllGh = document.getElementById('btnImportAllGithubPreview');
  if (btnImportAllGh) {
    btnImportAllGh.addEventListener('click', importAllGithubPreviewChannels);
  }

  // =========================================================================
  // Non-Working Channels Center + Embedded 100-Channel Name List Source Finder
  // =========================================================================

  document.getElementById('btnOpenBrokenCenter').addEventListener('click', openBrokenChannelsCenter);
  document.getElementById('statBrokenBadge').addEventListener('click', openBrokenChannelsCenter);
  document.getElementById('btnOpenBrokenToolbar').addEventListener('click', openBrokenChannelsCenter);

  document.getElementById('btnToggleBrokenBulkFinder').addEventListener('click', () => {
    const box = document.getElementById('brokenBulkFinderBox');
    box.classList.toggle('hidden');
    if (!box.classList.contains('hidden')) {
      populateBrokenBulkTextarea();
    }
  });

  document.getElementById('brokenBulkNamesTextarea').addEventListener('input', () => {
    updateBulkSourceLineCounter('brokenBulkNamesTextarea', 'brokenBulkLineCount');
  });

  document.getElementById('btnReloadBrokenNamesList').addEventListener('click', () => {
    populateBrokenBulkTextarea();
    showToast('🔄 Loaded up to 100 non-working channel names');
  });

  document.getElementById('btnRunBrokenBulkSearch').addEventListener('click', async () => {
    const raw_text = document.getElementById('brokenBulkNamesTextarea').value.trim();
    if (!raw_text) {
      showToast('Enter or load up to 100 channel names first');
      return;
    }

    const btn = document.getElementById('btnRunBrokenBulkSearch');
    btn.disabled = true;
    btn.textContent = '🔎 Searching & Probing (≤100)...';

    try {
      // Match entered names against broken channel IDs if names match
      const brokenList = (state.schedulerData?.broken_channels || []).filter(
        (c) => !state.currentPlaylistId || c.playlist_id === state.currentPlaylistId
      );
      const brokenByName = new Map();
      for (const b of brokenList) {
        const key = b.name.trim().toLowerCase();
        if (!brokenByName.has(key)) brokenByName.set(key, b);
      }

      const lines = raw_text
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 100);

      const queries = lines.map((line) => {
        const hit = brokenByName.get(line.toLowerCase());
        return hit
          ? { id: hit.id, name: line, tvg_id: hit.tvg_id, category: hit.category }
          : { id: '', name: line, tvg_id: '', category: 'Live TV' };
      });

      startProgressPolling('bulk_search');
      const res = await api('/api/sources/bulk-search', {
        method: 'POST',
        body: JSON.stringify({ names: queries, probe_live: true, include_yt: true, include_rutube: true, include_televizo: true, include_web: true, strict: true }),
      });
      stopProgressPolling();

      state.bulkSourceBrokenResults = res.results || [];
      renderBulkSourceResultsTable(state.bulkSourceBrokenResults, 'brokenBulkResultsWrap');

      const hasFound = res.found_count > 0;
      document
        .getElementById('btnApplyBrokenBulkReplacements')
        .classList.toggle('hidden', !hasFound);
      document.getElementById('btnAddBrokenBulkAsNew').classList.toggle('hidden', !hasFound);

      showToast(
        `🔎 Found ${res.found_count}/${res.total_requested} sources (${res.online_count} verified online)!`
      );
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '🔎 Search & Probe Sources (≤100)';
    }
  });

  document.getElementById('btnApplyBrokenBulkReplacements').addEventListener('click', async () => {
    const valid = state.bulkSourceBrokenResults.filter((r) => r.found && r.url && r.channel_id);
    if (valid.length === 0) {
      showToast('No existing broken channels matched — click "＋ Add Found as New Channels" instead');
      return;
    }
    try {
      const res = await api('/api/sources/bulk-apply', {
        method: 'POST',
        body: JSON.stringify({ replacements: valid }),
      });
      showToast(`🔄 Replaced stream URLs for ${res.updated} broken channel(s)!`);
      await loadPlaylists(state.currentPlaylistId);
      await refreshBrokenChannelsModal();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnAddBrokenBulkAsNew').addEventListener('click', async () => {
    const toAdd = state.bulkSourceBrokenResults
      .filter((r) => r.found && r.url)
      .map((r) => ({
        name: r.requested_name || r.name,
        url: r.url,
        logo: r.logo,
        category: r.category || 'Live TV',
        tvg_id: r.tvg_id,
        status: r.status,
        latency_ms: r.latency_ms,
      }));
    if (toAdd.length === 0) return;
    try {
      const res = await api('/api/import/commit', {
        method: 'POST',
        body: JSON.stringify({
          playlist_id: state.currentPlaylistId,
          channels: toAdd,
          deduplicate_urls: true,
        }),
      });
      showToast(`＋ Added ${res.inserted} channel(s) to playlist!`);
      await loadPlaylists(state.currentPlaylistId);
      await refreshBrokenChannelsModal();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('schedFrequencySelect').addEventListener('change', async (e) => {
    try {
      const checks_per_day = parseInt(e.target.value, 10);
      await api('/api/scheduler/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'configure', checks_per_day, enabled: true }),
      });
      showToast(`⏰ Scheduled automatic channel checks ${checks_per_day}x per day!`);
      await refreshBrokenChannelsModal();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnRunScheduledCheckNow').addEventListener('click', async () => {
    const btn = document.getElementById('btnRunScheduledCheckNow');
    btn.disabled = true;
    btn.textContent = '🛡️ Running Fallback Guard...';
    startProgressPolling('fallback_guard');
    try {
      const res = await api('/api/scheduler/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'run_fallback_guard', playlist_id: state.currentPlaylistId }),
      });
      stopProgressPolling();
      const summary = res.summary || {};
      showToast(
        `🛡️ Fallback Guard: ${summary.online || 0} online, ${summary.failovers || 0} auto-failovers performed!`
      );
      await loadPlaylists(state.currentPlaylistId);
      await refreshBrokenChannelsModal();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '🛡️ Run Fallback Guard Now';
    }
  });

  const btnBrokenTelevizo = document.getElementById('btnBrokenSyncTelevizo');
  if (btnBrokenTelevizo) {
    btnBrokenTelevizo.addEventListener('click', () => syncTelevizoPlaylist(btnBrokenTelevizo));
  }

  const btnPopAll = document.getElementById('btnAutoPopulateAllBackups');
  if (btnPopAll) {
    btnPopAll.addEventListener('click', async () => {
      if (!state.currentPlaylistId) {
        showToast('Select a playlist first');
        return;
      }
      btnPopAll.disabled = true;
      btnPopAll.textContent = '⚡ Finding Backups (3-4/ch)...';
      try {
        const res = await api(`/api/playlists/${state.currentPlaylistId}/auto-populate-backups`, {
          method: 'POST',
        });
        showToast(
          `🛡️ Auto-populated backups! Updated ${res.updated_channels} channels with ${res.total_backups_found} backup sources total.`
        );
        await loadPlaylists(state.currentPlaylistId);
        await refreshBrokenChannelsModal();
      } catch (err) {
        showToast(`❌ ${err.message}`);
      } finally {
        btnPopAll.disabled = false;
        btnPopAll.textContent = '⚡ Auto-Populate Backups (3-4 Sources)';
      }
    });
  }

  document.getElementById('btnAutoHealAllBroken').addEventListener('click', async () => {
    const btn = document.getElementById('btnAutoHealAllBroken');
    btn.disabled = true;
    btn.textContent = '🩹 Healing (≤100)...';
    try {
      const res = await api('/api/scheduler/action', {
        method: 'POST',
        body: JSON.stringify({ action: 'auto_heal_broken', playlist_id: state.currentPlaylistId }),
      });
      showToast(
        `🩹 Auto-healed ${res.healed} of ${res.total_broken} broken channel(s) with verified live sources!`
      );
      await loadPlaylists(state.currentPlaylistId);
      await refreshBrokenChannelsModal();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '🩹 Auto-Heal Broken (≤100)';
    }
  });

  document.getElementById('btnDeleteAllBroken').addEventListener('click', async () => {
    const brokenIds = state.channels
      .filter((c) => c.status === 'offline' || c.status === 'warning')
      .map((c) => c.id);
    if (brokenIds.length === 0) {
      showToast('No broken channels in this playlist');
      return;
    }
    if (!confirm(`Delete all ${brokenIds.length} non-working channel(s) from this playlist?`)) return;
    try {
      await api(`/api/playlists/${state.currentPlaylistId}/channels/bulk-delete`, {
        method: 'POST',
        body: JSON.stringify({ ids: brokenIds }),
      });
      showToast(`🗑️ Deleted ${brokenIds.length} non-working channel(s)`);
      await loadPlaylists(state.currentPlaylistId);
      await refreshBrokenChannelsModal();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // Export .M3U & .M3U8
  document.getElementById('btnExportM3u').addEventListener('click', () => {
    if (!state.currentPlaylistId) return;
    window.location.href = `/m3u/${state.currentPlaylistId}.m3u?download=1`;
    showToast('⬇ Downloading .m3u playlist...');
  });
  document.getElementById('btnExportM3u8').addEventListener('click', () => {
    if (!state.currentPlaylistId) return;
    window.location.href = `/m3u/${state.currentPlaylistId}.m3u8?download=1`;
    showToast('⬇ Downloading .m3u8 playlist...');
  });

  // Raw M3U Code Modal
  document.getElementById('btnPreviewRawM3u').addEventListener('click', async () => {
    if (!state.currentPlaylistId) return;
    const res = await fetch(`/m3u/${state.currentPlaylistId}.m3u`);
    const text = await res.text();
    document.getElementById('rawM3uTextarea').value = text;
    openModal('rawM3uModal');
  });
  document.getElementById('btnCopyRawM3u').addEventListener('click', () => {
    navigator.clipboard.writeText(document.getElementById('rawM3uTextarea').value);
    showToast('📋 Copied raw #EXTM3U playlist to clipboard!');
  });

  // Create / Rename / Duplicate / Delete Playlist
  document.getElementById('btnNewPlaylist').addEventListener('click', () => {
    document.getElementById('playlistModalTitle').textContent = '＋ Create New IPTV Playlist';
    document.getElementById('plModalMode').value = 'create';
    document.getElementById('plNameInput').value = '';
    document.getElementById('plDescInput').value = '';
    document.getElementById('plEpgInput').value = 'https://iptv-org.github.io/epg/guides/en.xml';
    openModal('playlistModal');
  });

  document.getElementById('btnEditPlaylistMeta').addEventListener('click', () => {
    if (!state.currentPlaylist) return;
    document.getElementById('playlistModalTitle').textContent = '✏️ Rename Playlist & Settings';
    document.getElementById('plModalMode').value = 'edit';
    document.getElementById('plNameInput').value = state.currentPlaylist.name;
    document.getElementById('plDescInput').value = state.currentPlaylist.description || '';
    document.getElementById('plEpgInput').value = state.currentPlaylist.epg_url || '';
    openModal('playlistModal');
  });

  document.getElementById('btnSavePlaylistMeta').addEventListener('click', async () => {
    try {
      const mode = document.getElementById('plModalMode').value;
      const name = document.getElementById('plNameInput').value.trim() || 'Untitled Playlist';
      const description = document.getElementById('plDescInput').value.trim();
      const epg_url = document.getElementById('plEpgInput').value.trim();

      if (mode === 'create') {
        const res = await api('/api/playlists', {
          method: 'POST',
          body: JSON.stringify({ name, description, epg_url }),
        });
        closeModal('playlistModal');
        showToast(`🎉 Created playlist "${name}"`);
        await loadPlaylists(res.playlist.id);
      } else {
        await api(`/api/playlists/${state.currentPlaylistId}/update`, {
          method: 'POST',
          body: JSON.stringify({ name, description, epg_url }),
        });
        closeModal('playlistModal');
        showToast(`✅ Updated playlist "${name}"`);
        await loadPlaylists(state.currentPlaylistId);
      }
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnDuplicatePlaylist').addEventListener('click', async () => {
    if (!state.currentPlaylistId) return;
    try {
      const res = await api(`/api/playlists/${state.currentPlaylistId}/duplicate`, {
        method: 'POST',
      });
      showToast(`📋 Duplicated playlist as "${res.playlist.name}"`);
      await loadPlaylists(res.playlist.id);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnDeletePlaylist').addEventListener('click', async () => {
    if (!state.currentPlaylist) return;
    if (state.playlists.length <= 1) {
      showToast('⚠️ You must keep at least one playlist');
      return;
    }
    if (!confirm(`Delete playlist "${state.currentPlaylist.name}" and all its channels?`)) return;
    try {
      await api(`/api/playlists/${state.currentPlaylistId}/delete`, { method: 'POST' });
      showToast('🗑️ Playlist deleted');
      state.currentPlaylistId = null;
      await loadPlaylists();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // Channel Modal Events
  document.getElementById('btnAddChannel').addEventListener('click', openAddChannelModal);
  document.getElementById('btnModalFindLogo').addEventListener('click', searchLogosInModal);
  document.getElementById('btnModalFindSource').addEventListener('click', searchSourcesInsideChannelModal);
  const btnModTelevizo = document.getElementById('btnModalTelevizoExtract');
  if (btnModTelevizo) {
    btnModTelevizo.addEventListener('click', extractTelevizoForModalChannel);
  }
  document.getElementById('btnCloseModalSources').addEventListener('click', () => {
    document.getElementById('modalSourceCandidatesBox').classList.add('hidden');
  });

  document.getElementById('chLogo').addEventListener('input', (e) => {
    document.getElementById('chLogoPreview').src =
      e.target.value.trim() || makeFallbackLogo(document.getElementById('chName').value);
  });
  document.getElementById('btnTestInModal').addEventListener('click', () => {
    const url = document.getElementById('chUrl').value.trim();
    const name = document.getElementById('chName').value.trim() || 'Test Channel';
    if (!url) {
      showToast('Enter a stream URL first');
      return;
    }
    playStreamUrl(name, url);
  });

  const btnAutoFindBackups = document.getElementById('btnAutoFindChannelBackups');
  if (btnAutoFindBackups) {
    btnAutoFindBackups.addEventListener('click', async () => {
      const channelId = document.getElementById('chId').value;
      if (!channelId) {
        showToast('Save channel first before finding backups');
        return;
      }
      btnAutoFindBackups.disabled = true;
      btnAutoFindBackups.textContent = '⏳ Finding 3-4 Backups...';
      try {
        const res = await api(`/api/channels/${encodeURIComponent(channelId)}/auto-backups`, {
          method: 'POST',
        });
        const count = res.total_backups ?? (res.sources ? res.sources.length : 0);
        showToast(`🛡️ Stored ${count} backup source(s) for channel!`);
        await loadChannelBackupSources(channelId);
        await loadPlaylists(state.currentPlaylistId);
      } catch (err) {
        showToast(`❌ ${err.message}`);
      } finally {
        btnAutoFindBackups.disabled = false;
        btnAutoFindBackups.textContent = '⚡ Auto-Find 3-4 Backups';
      }
    });
  }

  const btnAddManual = document.getElementById('btnAddManualBackupSource');
  if (btnAddManual) {
    btnAddManual.addEventListener('click', async () => {
      const channelId = document.getElementById('chId').value;
      const urlInput = document.getElementById('newBackupUrlInput');
      const url = (urlInput?.value || '').trim();
      if (!channelId) {
        showToast('Save channel first before adding backups');
        return;
      }
      if (!url.startsWith('http')) {
        showToast('Please enter a valid stream URL starting with http:// or https://');
        return;
      }
      btnAddManual.disabled = true;
      try {
        await api(`/api/channels/${encodeURIComponent(channelId)}/sources`, {
          method: 'POST',
          body: JSON.stringify({ url, source_name: 'Manual Backup' }),
        });
        showToast('✅ Added manual backup source!');
        if (urlInput) urlInput.value = '';
        await loadChannelBackupSources(channelId);
        await loadPlaylists(state.currentPlaylistId);
      } catch (err) {
        showToast(`❌ ${err.message}`);
      } finally {
        btnAddManual.disabled = false;
      }
    });
  }

  document.getElementById('btnSaveChannel').addEventListener('click', async () => {
    const id = document.getElementById('chId').value;
    const payload = {
      id,
      name: document.getElementById('chName').value.trim() || 'Untitled Channel',
      category: document.getElementById('chCategory').value.trim() || 'Uncategorized',
      url: document.getElementById('chUrl').value.trim(),
      logo: document.getElementById('chLogo').value.trim(),
      tvg_id: document.getElementById('chTvgId').value.trim(),
      tvg_name: document.getElementById('chTvgName').value.trim(),
      chno: document.getElementById('chNumber').value.trim(),
      user_agent: document.getElementById('chUserAgent').value.trim(),
      referrer: document.getElementById('chReferrer').value.trim(),
    };
    if (!payload.url) {
      showToast('⚠️ Please provide a stream URL (.m3u8 / http)');
      return;
    }

    try {
      if (id) {
        await api(`/api/playlists/${state.currentPlaylistId}/channels/update`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        showToast(`✅ Saved changes to "${payload.name}"`);
      } else {
        await api(`/api/playlists/${state.currentPlaylistId}/channels/add`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        showToast(`＋ Added "${payload.name}" to playlist`);
      }
      closeModal('channelModal');
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // =========================================================================
  // 4. 🔐 IPTV Xtream Codes Login Bulk Importer Events
  // =========================================================================

  document.getElementById('btnOpenXtreamImport').addEventListener('click', () => {
    openModal('xtreamModal');
  });

  document.getElementById('btnLoadDemoXtream').addEventListener('click', () => {
    document.getElementById('xtreamServerUrl').value = 'demo.xtream.local';
    document.getElementById('xtreamUsername').value = 'vip_streamforge';
    document.getElementById('xtreamPassword').value = 'demo1234';
    document.getElementById('btnConnectXtream').click();
  });

  document.getElementById('btnConnectXtream').addEventListener('click', async () => {
    const server_url = document.getElementById('xtreamServerUrl').value.trim();
    const username = document.getElementById('xtreamUsername').value.trim();
    const password = document.getElementById('xtreamPassword').value.trim();

    if (!server_url) {
      showToast('Enter Xtream Server URL (or click Load Demo Xtream Server)');
      return;
    }

    const btn = document.getElementById('btnConnectXtream');
    btn.disabled = true;
    btn.textContent = '🔌 Connecting to Xtream API...';

    try {
      const res = await api('/api/xtream/inspect', {
        method: 'POST',
        body: JSON.stringify({
          server_url,
          username,
          password,
          playlist_id: state.currentPlaylistId,
        }),
      });

      state.xtreamChannels = res.channels || [];
      const dup = res.duplicate_summary || {};
      const acc = res.account || {};

      document.getElementById('xtreamAnalysisPanel').classList.remove('hidden');
      document.getElementById('btnProbeXtreamChannels').classList.remove('hidden');

      document.getElementById('xtreamAccountBadge').textContent =
        `Account: ${acc.status || 'Active'} (${acc.username || 'User'})`;
      document.getElementById('xtreamTotalBadge').textContent = `${dup.total || 0} Total Fetched`;
      document.getElementById('xtreamUniqueBadge').textContent = `${dup.unique_count || 0} Unique New`;
      document.getElementById('xtreamDupPlaylistBadge').textContent =
        `${dup.dup_in_playlist || 0} Already in Current Playlist`;
      document.getElementById('xtreamDupInternalBadge').textContent =
        `${dup.dup_in_new_list || 0} Duplicates in Imported List`;

      const grid = document.getElementById('xtreamCategoriesGrid');
      grid.innerHTML = (res.categories || [])
        .map(
          (c) => `
          <label class="import-cat-chip">
            <input type="checkbox" class="xtream-cat-check" value="${escapeHtml(c.name)}" checked />
            <span>${escapeHtml(c.name)} (${c.count})</span>
          </label>
        `
        )
        .join('');

      renderUploadVerificationTable(state.xtreamChannels, 'xtreamPreviewTableWrap');
      showToast(`🔐 Connected! Fetched ${state.xtreamChannels.length} channels.`);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '🔌 Login & Fetch Xtream Channels';
    }
  });

  document.getElementById('btnProbeXtreamChannels').addEventListener('click', async () => {
    await probeCandidateBatch(state.xtreamChannels, 'xtreamPreviewTableWrap');
  });

  document.getElementById('btnXtreamSelectAllCats').addEventListener('click', () => {
    document.querySelectorAll('.xtream-cat-check').forEach((c) => (c.checked = true));
  });
  document.getElementById('btnXtreamClearCats').addEventListener('click', () => {
    document.querySelectorAll('.xtream-cat-check').forEach((c) => (c.checked = false));
  });

  document.getElementById('btnCommitXtreamImport').addEventListener('click', async () => {
    if (state.xtreamChannels.length === 0) {
      showToast('Connect & fetch Xtream channels first');
      return;
    }
    const allowedCats = new Set(
      Array.from(document.querySelectorAll('.xtream-cat-check:checked')).map((c) => c.value)
    );
    const selectedChannels = state.xtreamChannels.filter((c) =>
      allowedCats.has(c.category || 'Uncategorized')
    );

    const btn = document.getElementById('btnCommitXtreamImport');
    btn.disabled = true;
    btn.textContent = '📥 Checking & Importing Xtream Channels...';

    try {
      const res = await api('/api/import/commit', {
        method: 'POST',
        body: JSON.stringify({
          playlist_id: state.currentPlaylistId,
          channels: selectedChannels,
          deduplicate_urls: document.getElementById('xtreamSkipPlaylistDups').checked,
          deduplicate_internal: document.getElementById('xtreamSkipInternalDups').checked,
          deduplicate_by_name: document.getElementById('xtreamDedupByName').checked,
          check_streams_on_upload: document.getElementById('xtreamCheckOnUpload').checked,
          only_import_working: document.getElementById('xtreamOnlyWorking').checked,
          auto_find_logos: true,
        }),
      });
      closeModal('xtreamModal');
      showToast(
        `🎉 Imported ${res.inserted} Xtream channel(s) (${res.skipped_existing} in-playlist dups, ${res.skipped_internal} internal dups, ${res.skipped_offline} offline skipped)!`
      );
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '📥 Import Xtream Channels';
    }
  });

  // =========================================================================
  // 5. Bulk .m3u8 Implant Modal
  // =========================================================================

  document.getElementById('btnBulkImplant').addEventListener('click', () => {
    if (state.selectedCategory !== 'ALL') {
      document.getElementById('implantCategory').value = state.selectedCategory;
    }
    document.getElementById('implantCheckResultsWrap').classList.add('hidden');
    openModal('bulkImplantModal');
  });

  document.getElementById('btnLoadSampleImplant').addEventListener('click', () => {
    document.getElementById('implantTextarea').value = [
      'BBC News | https://vs-hls-push-ww-live.akamaized.net/x=4/i=urn:bbc:pips:service:bbc_news_channel_hd/t=3840/v=pv14/b=5070016/main.m3u8 | News',
      'Sky News International | https://skynews2-plutolive-vo.akamaized.net/cdnhls/2112/master.m3u8 | News',
      'Bloomberg TV | https://liveproduseast.global.ssl.fastly.net/us/Channel-USTV-AWS-virginia-1/Source-USTV-1000-1_live.m3u8 | Business',
      'CBS News 24/7 | https://cbsn-us.cbsnstream.cbsnews.com/out/v1/55a8648e8f134e82a470f83d562deeca/master.m3u8 | News',
      'Dead Test Stream | https://dead-link.example.invalid/live/broken.m3u8 | Test',
    ].join('\n');
  });

  document.getElementById('btnVerifyImplantPreview').addEventListener('click', async () => {
    const raw = document.getElementById('implantTextarea').value.trim();
    if (!raw) {
      showToast('Paste .m3u8 links first');
      return;
    }
    try {
      const default_category = document.getElementById('implantCategory').value.trim() || 'Implanted';
      const preview = await api('/api/import/preview', {
        method: 'POST',
        body: JSON.stringify({
          content: raw,
          default_category,
          playlist_id: state.currentPlaylistId,
        }),
      });
      state.implantPreviewChannels = preview.channels || [];
      await probeCandidateBatch(state.implantPreviewChannels, 'implantCheckResultsWrap');
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnCommitImplant').addEventListener('click', async () => {
    const raw = document.getElementById('implantTextarea').value.trim();
    if (!raw) {
      showToast('Paste at least one .m3u8 URL or channel line');
      return;
    }
    const default_category = document.getElementById('implantCategory').value.trim() || 'Implanted';
    const btn = document.getElementById('btnCommitImplant');
    btn.disabled = true;
    btn.textContent = '⚡ Checking & Implanting Channels...';
    try {
      const res = await api('/api/import/commit', {
        method: 'POST',
        body: JSON.stringify({
          playlist_id: state.currentPlaylistId,
          raw_content: raw,
          default_category,
          auto_find_logos: document.getElementById('implantAutoLogos').checked,
          deduplicate_urls: document.getElementById('implantDeduplicate').checked,
          deduplicate_internal: document.getElementById('implantDeduplicate').checked,
          check_streams_on_upload: document.getElementById('implantCheckStreams').checked,
          only_import_working: document.getElementById('implantOnlyWorking').checked,
        }),
      });
      closeModal('bulkImplantModal');
      document.getElementById('implantTextarea').value = '';
      showToast(
        `⚡ Implanted ${res.inserted} channel(s) (${res.online_count} online, ${res.skipped} dups skipped, ${res.skipped_offline} dead skipped)!`
      );
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '⚡ Check & Implant Channels Now';
    }
  });

  // =========================================================================
  // 6. Auto-Import M3U Modal
  // =========================================================================

  document.getElementById('btnImportM3u').addEventListener('click', () => {
    state.importPreviewChannels = [];
    document.getElementById('importPreviewBox').classList.add('hidden');
    document.getElementById('importChannelCheckWrap').classList.add('hidden');
    openModal('importM3uModal');
  });

  document.querySelectorAll('#importM3uModal .tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#importM3uModal .tab-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.getAttribute('data-tab');
      document.getElementById('importTabUrl').classList.toggle('hidden', tab !== 'url');
      document.getElementById('importTabFile').classList.toggle('hidden', tab !== 'file');
      document.getElementById('importTabRaw').classList.toggle('hidden', tab !== 'raw');
    });
  });

  document.querySelectorAll('.preset-chip:not(.source-preset-chip)').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.getElementById('importRemoteUrl').value = chip.getAttribute('data-url');
      document.getElementById('btnFetchRemotePreview').click();
    });
  });

  document.getElementById('importDestinationMode').addEventListener('change', (e) => {
    document
      .getElementById('newPlaylistNameGroup')
      .classList.toggle('hidden', e.target.value !== 'new');
  });

  async function runImportPreview(payload) {
    try {
      showToast('🔍 Analyzing M3U playlist & duplicates...');
      const res = await api('/api/import/preview', {
        method: 'POST',
        body: JSON.stringify({ ...payload, playlist_id: state.currentPlaylistId }),
      });
      state.importPreviewChannels = res.channels || [];
      const dup = res.duplicate_summary || {};
      const box = document.getElementById('importPreviewBox');
      box.classList.remove('hidden');
      document.getElementById('importPreviewSummary').textContent =
        `Found ${res.total_channels} channels across ${res.categories.length} categories`;

      document.getElementById('importDupBadges').innerHTML = `
        <span class="badge badge-cyan">${dup.unique_count || 0} Unique New</span>
        <span class="badge badge-amber">${dup.dup_in_playlist || 0} Already in Current Playlist</span>
        <span class="badge badge-violet">${dup.dup_in_new_list || 0} Duplicates in Imported List</span>
      `;

      const grid = document.getElementById('importCategoriesGrid');
      grid.innerHTML = res.categories
        .map(
          (c) => `
          <label class="import-cat-chip">
            <input type="checkbox" class="import-cat-check" value="${escapeHtml(c.name)}" checked />
            <span>${escapeHtml(c.name)} (${c.count})</span>
          </label>
        `
        )
        .join('');
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  }

  document.getElementById('btnProbeM3uPreview').addEventListener('click', async () => {
    await probeCandidateBatch(state.importPreviewChannels, 'importChannelCheckWrap');
  });

  document.getElementById('btnFetchRemotePreview').addEventListener('click', () => {
    const url = document.getElementById('importRemoteUrl').value.trim();
    if (!url) {
      showToast('Enter a remote M3U URL');
      return;
    }
    runImportPreview({ url });
  });

  document.getElementById('importFileInput').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    document.getElementById('selectedFileName').textContent = `Selected: ${file.name}`;
    const text = await file.text();
    runImportPreview({ content: text });
  });

  document.getElementById('btnParseRawPreview').addEventListener('click', () => {
    const text = document.getElementById('importRawContent').value.trim();
    if (!text) return;
    runImportPreview({ content: text });
  });

  document.getElementById('btnSelectAllImportCats').addEventListener('click', () => {
    document.querySelectorAll('.import-cat-check').forEach((c) => (c.checked = true));
  });
  document.getElementById('btnDeselectAllImportCats').addEventListener('click', () => {
    document.querySelectorAll('.import-cat-check').forEach((c) => (c.checked = false));
  });

  document.getElementById('btnExecuteImport').addEventListener('click', async () => {
    let channelsToImport = state.importPreviewChannels;

    try {
      if (channelsToImport.length === 0) {
        const rawText = document.getElementById('importRawContent').value.trim();
        const remoteUrl = document.getElementById('importRemoteUrl').value.trim();
        if (rawText) {
          const p = await api('/api/import/preview', {
            method: 'POST',
            body: JSON.stringify({ content: rawText, playlist_id: state.currentPlaylistId }),
          });
          channelsToImport = p.channels || [];
        } else if (remoteUrl) {
          const p = await api('/api/import/preview', {
            method: 'POST',
            body: JSON.stringify({ url: remoteUrl, playlist_id: state.currentPlaylistId }),
          });
          channelsToImport = p.channels || [];
        }
      } else {
        const allowedCats = new Set(
          Array.from(document.querySelectorAll('.import-cat-check:checked')).map((c) => c.value)
        );
        channelsToImport = channelsToImport.filter((ch) =>
          allowedCats.has(ch.category || 'Uncategorized')
        );
      }

      if (channelsToImport.length === 0) {
        showToast('⚠️ No channels selected to import');
        return;
      }

      const destMode = document.getElementById('importDestinationMode').value;
      const newName =
        destMode === 'new'
          ? document.getElementById('importNewPlaylistName').value.trim() || 'Imported M3U Playlist'
          : '';

      const btn = document.getElementById('btnExecuteImport');
      btn.disabled = true;
      btn.textContent = `📥 Checking & Importing ${channelsToImport.length} channels...`;

      try {
        const res = await api('/api/import/commit', {
          method: 'POST',
          body: JSON.stringify({
            playlist_id: state.currentPlaylistId,
            new_playlist_name: newName,
            channels: channelsToImport.slice(0, 1500),
            auto_find_logos: document.getElementById('importAutoLogos').checked,
            deduplicate_urls: document.getElementById('importDeduplicate').checked,
            deduplicate_internal: document.getElementById('importDeduplicate').checked,
            check_streams_on_upload: document.getElementById('importCheckOnUpload').checked,
            only_import_working: document.getElementById('importOnlyWorking').checked,
          }),
        });
        closeModal('importM3uModal');
        showToast(
          `🎉 Imported ${res.inserted} channels (${res.skipped} duplicates & ${res.skipped_offline} offline skipped)!`
        );
        await loadPlaylists(res.playlist_id);
      } finally {
        btn.disabled = false;
        btn.textContent = '📥 Import Selected Channels';
      }
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // Bulk Auto Logo Finder on Toolbar
  document.getElementById('btnAutoFindAllLogos').addEventListener('click', async () => {
    if (!state.currentPlaylistId) {
      showToast('⚠️ Please select or create a playlist first');
      return;
    }
    const btn = document.getElementById('btnAutoFindAllLogos');
    btn.disabled = true;
    btn.innerHTML = '<span>⏳ Finding Logos...</span>';
    try {
      showToast('✨ Scanning playlist channels & matching official logos...');
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/auto-logos`, {
        method: 'POST',
        body: JSON.stringify({ overwrite: false, include_generated: true }),
      });
      if (res.total_updated === 0) {
        showToast('ℹ️ All channels already have logos! Use bulk select to overwrite specific channels.');
      } else {
        showToast(
          `✨ Updated ${res.total_updated} logos (${res.matched_real} official + ${res.matched_generated} studio badges)!`
        );
      }
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<span>✨ Auto Logos</span>';
    }
  });

  // Sync Televizo Live Streams Toolbar Button
  const btnLoadAllTelevizo = document.getElementById('btnLoadAllTelevizo');
  if (btnLoadAllTelevizo) {
    btnLoadAllTelevizo.addEventListener('click', async () => {
      if (!state.currentPlaylistId) {
        showToast('Select or create a playlist first');
        return;
      }
      btnLoadAllTelevizo.disabled = true;
      btnLoadAllTelevizo.textContent = '📡 Loading Televizo...';
      try {
        const res = await api('/api/televizo/import-all', {
          method: 'POST',
          body: JSON.stringify({ playlist_id: state.currentPlaylistId }),
        });
        showToast(`📡 ${res.message || `Loaded ${res.added} Televizo channels!`}`);
        await loadPlaylists(state.currentPlaylistId);
      } catch (err) {
        showToast(`❌ Failed to load Televizo channels: ${err.message}`);
      } finally {
        btnLoadAllTelevizo.disabled = false;
        btnLoadAllTelevizo.innerHTML = '<span>📡 Load All Televizo</span>';
      }
    });
  }

  const btnSyncTelevizo = document.getElementById('btnSyncTelevizoStreams');
  if (btnSyncTelevizo) {
    btnSyncTelevizo.addEventListener('click', () => syncTelevizoPlaylist(btnSyncTelevizo));
  }

  // Stream Health Checker on Toolbar
  document.getElementById('btnCheckStreams').addEventListener('click', async () => {
    if (!state.currentPlaylistId) return;
    const btn = document.getElementById('btnCheckStreams');
    btn.disabled = true;
    btn.textContent = '🩺 Probing...';
    try {
      showToast('🩺 Probing visible stream URLs...');
      const filteredIds = getFilteredChannels().slice(0, 60).map((c) => c.id);
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/check-streams`, {
        method: 'POST',
        body: JSON.stringify({ ids: filteredIds }),
      });
      const online = (res.results || []).filter((r) => r.status === 'online').length;
      showToast(`🩺 Stream check complete: ${online}/${res.results.length} online!`);
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '🩺 Check Streams';
    }
  });

  // Category Manager & Smart Tools Modals
  document.getElementById('btnManageCategories').addEventListener('click', openCategoryManager);
  document.getElementById('btnSmartTools').addEventListener('click', () => openModal('smartToolsModal'));

  document.getElementById('btnRunNameCleaner').addEventListener('click', async () => {
    try {
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/clean-names`, {
        method: 'POST',
        body: JSON.stringify({
          find_text: document.getElementById('cleanFindText').value,
          replace_text: document.getElementById('cleanReplaceText').value,
          strip_brackets: document.getElementById('cleanBrackets').checked,
          strip_country_prefix: document.getElementById('cleanCountry').checked,
          strip_quality_tags: document.getElementById('cleanQuality').checked,
        }),
      });
      showToast(`✨ Cleaned ${res.updated} channel names!`);
      await loadCurrentPlaylistDetails();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnDedupByUrl').addEventListener('click', async () => {
    try {
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/deduplicate`, {
        method: 'POST',
        body: JSON.stringify({ by: 'url' }),
      });
      showToast(`🛡️ Removed ${res.removed} duplicate stream URLs`);
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnDedupByName').addEventListener('click', async () => {
    try {
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/deduplicate`, {
        method: 'POST',
        body: JSON.stringify({ by: 'name' }),
      });
      showToast(`🛡️ Removed ${res.removed} duplicate channel names`);
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnRunAutoNumber').addEventListener('click', async () => {
    try {
      const start = parseInt(document.getElementById('autoNumStart').value || '1', 10);
      const sort_by = document.getElementById('autoNumSort').value;
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/auto-number`, {
        method: 'POST',
        body: JSON.stringify({ start, sort_by }),
      });
      closeModal('smartToolsModal');
      showToast(`🔢 Numbered & sorted ${res.updated} channels!`);
      await loadCurrentPlaylistDetails();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // Bulk Selection Bar Actions
  document.getElementById('selectAllCheckbox').addEventListener('change', (e) => {
    toggleSelectAllFiltered(e.target.checked);
  });

  document.getElementById('btnBulkMoveCategory').addEventListener('click', async () => {
    const newCat = prompt('Enter new Category / Group name for selected channels:', 'Sports');
    if (!newCat) return;
    try {
      const ids = Array.from(state.selectedChannelIds);
      await api(`/api/playlists/${state.currentPlaylistId}/channels/bulk-category`, {
        method: 'POST',
        body: JSON.stringify({ ids, category: newCat.trim() }),
      });
      showToast(`📁 Moved ${ids.length} channels to "${newCat.trim()}"`);
      await loadCurrentPlaylistDetails();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnBulkRemoveFromCategory').addEventListener('click', async () => {
    try {
      const ids = Array.from(state.selectedChannelIds);
      await api(`/api/playlists/${state.currentPlaylistId}/channels/bulk-category`, {
        method: 'POST',
        body: JSON.stringify({ ids, category: 'Uncategorized' }),
      });
      showToast(`🏷️ Removed ${ids.length} channels from their category`);
      await loadCurrentPlaylistDetails();
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnBulkAutoLogos').addEventListener('click', async () => {
    const ids = Array.from(state.selectedChannelIds);
    if (!state.currentPlaylistId) {
      showToast('⚠️ Please select a playlist first');
      return;
    }
    if (ids.length === 0) {
      showToast('⚠️ Please select at least one channel first using the checkboxes');
      return;
    }
    const btn = document.getElementById('btnBulkAutoLogos');
    btn.disabled = true;
    try {
      showToast(`✨ Finding & assigning logos for ${ids.length} selected channels...`);
      const res = await api(`/api/playlists/${state.currentPlaylistId}/channels/auto-logos`, {
        method: 'POST',
        body: JSON.stringify({ ids, overwrite: true, include_generated: true }),
      });
      showToast(`✨ Successfully updated ${res.total_updated} logos!`);
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('btnBulkCheckStatus').addEventListener('click', async () => {
    try {
      const ids = Array.from(state.selectedChannelIds);
      showToast(`🩺 Checking ${ids.length} selected streams...`);
      await api(`/api/playlists/${state.currentPlaylistId}/channels/check-streams`, {
        method: 'POST',
        body: JSON.stringify({ ids }),
      });
      showToast('🩺 Stream status updated!');
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  document.getElementById('btnBulkDeleteChannels').addEventListener('click', async () => {
    const ids = Array.from(state.selectedChannelIds);
    if (!confirm(`Remove ${ids.length} selected channel(s) from this playlist?`)) return;
    try {
      await api(`/api/playlists/${state.currentPlaylistId}/channels/bulk-delete`, {
        method: 'POST',
        body: JSON.stringify({ ids }),
      });
      state.selectedChannelIds.clear();
      showToast(`🗑️ Removed ${ids.length} channels`);
      await loadPlaylists(state.currentPlaylistId);
    } catch (err) {
      showToast(`❌ ${err.message}`);
    }
  });

  // Toggle Live Player CORS Bypass Proxy Mode
  const btnToggleProxy = document.getElementById('btnTogglePlayerProxy');
  if (btnToggleProxy) {
    btnToggleProxy.addEventListener('click', () => {
      state.forceStreamProxy = !state.forceStreamProxy;
      const toggleLabel = document.getElementById('proxyToggleState');
      if (toggleLabel) {
        toggleLabel.textContent = state.forceStreamProxy ? 'ON (Proxy)' : 'Auto';
      }
      showToast(
        state.forceStreamProxy
          ? '🛡️ CORS Bypass Proxy forced ON (Bypasses browser CORS)'
          : '🛡️ CORS Proxy set to Auto (Automatic fallback on failure)'
      );
      const curUrl = document.getElementById('playerStreamUrl').value;
      const curName = document.getElementById('playerChannelName').textContent;
      if (curUrl) {
        playStreamUrl(curName, curUrl, state.forceStreamProxy);
      }
    });
  }

  // Open 24/7 Cloud Host Modal
  const btnCloud = document.getElementById('btnOpenCloudHostModal');
  if (btnCloud) {
    btnCloud.addEventListener('click', () => {
      openModal('cloudHostModal');
    });
  }

  // Close Live Player
  document.getElementById('btnClosePlayer').addEventListener('click', () => {
    const video = document.getElementById('hlsVideo');
    video.pause();
    video.removeAttribute('src');
    if (state.hlsInstance) {
      state.hlsInstance.destroy();
      state.hlsInstance = null;
    }
    document.getElementById('livePlayerDock').classList.add('hidden');
  });

  const btnPlayerLoadUrl = document.getElementById('btnPlayerLoadUrl');
  if (btnPlayerLoadUrl) {
    btnPlayerLoadUrl.addEventListener('click', () => {
      const raw = (document.getElementById('playerStreamUrl').value || '').trim();
      if (!/^https?:\/\//i.test(raw)) {
        showToast('❌ Enter an http(s) stream or player-page URL');
        return;
      }
      playStreamUrl('Custom URL', raw);
    });
  }
});
