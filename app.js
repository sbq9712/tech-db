const PAGE_SIZE = 50;

const state = {
  manifest: null,
  records: [],
  filtered: [],
  categoryOrder: [],
  page: 1,
  query: '',
  date: '',
  type: 'all',
  alert: 'all',
  category: '',
  categoryQuery: '',
  calendarMonth: '',
  collapsedGroups: new Set(),
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[s]));

async function getJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function normalizeCategory(value) {
  return String(value || '').replaceAll('/', '-').replaceAll('（', '').replaceAll('）', '').replaceAll('(', '').replaceAll(')', '');
}

// Parse a category path like "零碳产业-能量循环-能源测-发电技术-光伏" into hierarchy
function parseCategoryPath(cat) {
  const parts = cat.replaceAll('-', '/').split('/');
  return { top: parts[0], leaf: parts[parts.length - 1], full: cat.replaceAll('-', '/'), parts };
}

// Group categories by top-level domain
function buildCategoryTree() {
  const counts = new Map();
  for (const item of state.records) {
    const cat = item.category || '未分类';
    counts.set(cat, (counts.get(cat) || 0) + 1);
  }

  const orderedCats = [];
  const seen = new Set();
  for (const path of state.categoryOrder) {
    const normalized = normalizeCategory(path);
    orderedCats.push(normalized);
    seen.add(normalized);
  }
  for (const cat of counts.keys()) {
    if (!seen.has(cat)) orderedCats.push(cat);
  }

  const q = state.categoryQuery.trim().toLowerCase();
  const tree = {};
  for (const cat of orderedCats) {
    const count = counts.get(cat) || 0;
    if (q && !cat.toLowerCase().includes(q) && !cat.replaceAll('-', '/').toLowerCase().includes(q)) continue;
    const { top, full } = parseCategoryPath(cat);
    if (!tree[top]) tree[top] = [];
    tree[top].push({ cat, count, label: full });
  }

  // Compute group totals
  const groupTotals = {};
  for (const [top, leaves] of Object.entries(tree)) {
    groupTotals[top] = leaves.reduce((sum, l) => sum + l.count, 0);
  }
  return { tree, groupTotals };
}

async function load() {
  state.manifest = await getJson('./data/processed/manifest.json');
  state.categoryOrder = (await getJson('./data/category-order.json').catch(() => ({ categories: [] }))).categories || [];
  const shards = await Promise.all(state.manifest.shards.map((s) => getJson('./' + s.path)));
  state.records = shards.flatMap((payload) => payload.records || []);
  state.filtered = state.records;
  state.calendarMonth = (state.records[0]?.date || new Date().toISOString().slice(0, 10)).slice(0, 7);
  renderHeader();
  bindEvents();
  applyFilters();
}

function renderHeader() {
  const meta = state.manifest?.meta || {};
  const newsCount = meta.records_by_type?.news || state.records.filter((i) => i.intelligence_type === 'news').length;
  const litCount = meta.records_by_type?.literature || state.records.filter((i) => i.intelligence_type === 'literature').length;
  $('headerMeta').innerHTML = `
    <span>总计 <strong>${(meta.records_total || state.records.length).toLocaleString()}</strong></span>
    <span>新闻 <strong>${newsCount.toLocaleString()}</strong></span>
    <span>文献 <strong>${litCount.toLocaleString()}</strong></span>
  `;
}

function availableDates() {
  return new Set(state.records.map((item) => item.date).filter(Boolean));
}

function applyFilters() {
  const q = state.query.trim().toLowerCase();
  state.filtered = state.records.filter((item) => {
    if (state.date && item.date !== state.date) return false;
    if (state.category && item.category !== state.category) return false;
    if (state.type !== 'all' && item.intelligence_type !== state.type) return false;
    if (state.alert === 'yes' && !item.is_alert) return false;
    if (state.alert === 'no' && item.is_alert) return false;
    if (!q) return true;
    return [item.title, item.body, item.category, item.source, item.url, item.authors].join(' ').toLowerCase().includes(q);
  });
  state.page = 1;
  render();
}

function render() {
  renderCategoryTree();
  renderCalendar();
  renderToolbar();
  renderRecords();
}

function renderToolbar() {
  $('resultCount').innerHTML = `显示 <strong>${state.filtered.length.toLocaleString()}</strong> 条 / 共 ${state.records.length.toLocaleString()} 条`;

  const chips = [];
  if (state.query.trim()) chips.push({ label: '检索', value: state.query.trim(), key: 'query' });
  if (state.date) chips.push({ label: '日期', value: state.date, key: 'date' });
  if (state.category) chips.push({ label: '分类', value: state.category.replaceAll('-', ' / '), key: 'category' });
  if (state.type !== 'all') chips.push({ label: '类型', value: state.type === 'news' ? '新闻' : '文献', key: 'type' });
  if (state.alert !== 'all') chips.push({ label: '预警', value: state.alert === 'yes' ? '预警' : '非预警', key: 'alert' });
  $('activeChips').innerHTML = chips.length
    ? chips.map((c) => `<button class="chip" data-clear="${c.key}" type="button">${esc(c.label)}: ${esc(c.value)} ×</button>`).join('')
    : '';
}

function renderCalendar() {
  const dates = availableDates();
  const [year, month] = state.calendarMonth.split('-').map(Number);
  const first = new Date(year, month - 1, 1);
  const last = new Date(year, month, 0);
  const offset = (first.getDay() + 6) % 7;
  $('calendarTitle').textContent = `${year} 年 ${String(month).padStart(2, '0')} 月`;
  const cells = [];
  for (let i = 0; i < offset; i++) cells.push('<div class="cal-day empty"></div>');
  for (let day = 1; day <= last.getDate(); day++) {
    const date = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const hasData = dates.has(date);
    cells.push(`<div class="cal-day ${hasData ? 'available' : ''} ${state.date === date ? 'active' : ''}" data-date="${date}">${day}</div>`);
  }
  $('calendarGrid').innerHTML = cells.join('');
}

function renderCategoryTree() {
  const { tree, groupTotals } = buildCategoryTree();
  const groupOrder = ['零碳产业', 'AI与智能科技', '通用技术', '不相关', '未分类'];
  const allGroups = [...Object.keys(tree)].sort((a, b) => {
    const ia = groupOrder.indexOf(a);
    const ib = groupOrder.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  const html = allGroups.map((group) => {
    const leaves = tree[group];
    if (!leaves || !leaves.length) return '';
    const collapsed = state.collapsedGroups.has(group);
    const leavesHtml = leaves.map(({ cat, count, label }) => {
      const isActive = state.category === cat;
      const shortLabel = label.split('/').slice(1).join(' / ') || label;
      return `<div class="tree-leaf ${isActive ? 'active' : ''}" data-category="${esc(cat)}">
        <span>${esc(shortLabel)}</span>
        <span class="leaf-count">${count}</span>
      </div>`;
    }).join('');

    return `<div class="tree-group ${collapsed ? 'collapsed' : ''}" data-group="${esc(group)}">
      <div class="tree-group-head">
        <span><span class="group-arrow">▾</span> ${esc(group)}</span>
        <span class="group-count">${groupTotals[group]}</span>
      </div>
      <div class="tree-children">${leavesHtml}</div>
    </div>`;
  }).join('');

  $('categoryTree').innerHTML = html || '<div style="padding:12px;color:var(--text-muted);font-size:12px;">无匹配分类</div>';
}

function renderRecords() {
  const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const start = (state.page - 1) * PAGE_SIZE;
  const rows = state.filtered.slice(start, start + PAGE_SIZE);
  $('pageLabel').textContent = `第 ${state.page} / ${pages} 页`;
  $('prevPage').disabled = state.page <= 1;
  $('nextPage').disabled = state.page >= pages;
  if (!rows.length) {
    $('recordList').innerHTML = '<div class="empty-hint"><strong>没有匹配的情报</strong>请清除部分筛选条件或更换关键词。</div>';
    return;
  }
  $('recordList').innerHTML = rows.map(renderRecordCard).join('');
}

function renderRecordCard(item) {
  const typeLabel = item.intelligence_type === 'literature' ? '文献' : '新闻';
  const typeClass = item.intelligence_type === 'literature' ? 'literature' : 'news';
  const url = item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noreferrer">原文 ↗</a>` : '';
  const summary = (item.body || '').slice(0, 300) || '无正文摘要';
  const cat = (item.category || '未分类').replaceAll('-', ' / ');
  const isUnrelated = (item.category || '') === '不相关';
  return `<article class="record-card">
    <div class="record-meta-line">
      <span>${esc(item.date || '未知日期')}</span>
      <span class="type-tag ${typeClass}">${typeLabel}</span>
      ${item.is_alert ? '<span class="type-tag" style="color:var(--warn);background:var(--warn-light)">预警</span>' : ''}
    </div>
    <div class="record-title">
      <h3>${esc(item.title || '未命名情报')}</h3>
      ${url}
    </div>
    <p class="record-summary">${esc(summary)}</p>
    <div class="record-footer">
      <span class="cat-badge ${isUnrelated ? 'unrelated' : ''}">${esc(cat)}</span>
    </div>
  </article>`;
}

function clearFilter(key) {
  if (key === 'query') { state.query = ''; $('searchInput').value = ''; }
  if (key === 'date') state.date = '';
  if (key === 'category') state.category = '';
  if (key === 'type') { state.type = 'all'; document.querySelectorAll('[data-type]').forEach((b) => b.classList.toggle('active', b.dataset.type === 'all')); }
  if (key === 'alert') { state.alert = 'all'; document.querySelectorAll('[data-alert]').forEach((b) => b.classList.toggle('active', b.dataset.alert === 'all')); }
  applyFilters();
}

function shiftMonth(delta) {
  const [year, month] = state.calendarMonth.split('-').map(Number);
  const next = new Date(year, month - 1 + delta, 1);
  state.calendarMonth = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}`;
  renderCalendar();
}

function bindEvents() {
  $('searchInput').addEventListener('input', (e) => { state.query = e.target.value; applyFilters(); });
  $('categorySearch').addEventListener('input', (e) => { state.categoryQuery = e.target.value; renderCategoryTree(); });
  $('clearDate').addEventListener('click', () => { state.date = ''; applyFilters(); });
  $('prevMonth').addEventListener('click', () => shiftMonth(-1));
  $('nextMonth').addEventListener('click', () => shiftMonth(1));
  $('prevPage').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; renderRecords(); } });
  $('nextPage').addEventListener('click', () => { const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE)); if (state.page < pages) { state.page += 1; renderRecords(); } });

  // Filter toggles
  $('categoryToggle').addEventListener('click', () => {
    const el = $('categoryTree').parentElement;
    const input = $('categorySearch');
    const hidden = el.style.display === 'none';
    el.style.display = hidden ? '' : 'none';
    input.style.display = hidden ? '' : 'none';
    $('categoryToggle').textContent = hidden ? '收起 ▴' : '展开 ▾';
  });
  $('dateToggle').addEventListener('click', () => {
    const el = $('datePicker');
    const hidden = el.style.display === 'none';
    el.style.display = hidden ? '' : 'none';
    $('dateToggle').textContent = hidden ? '收起 ▴' : '展开 ▾';
  });

  document.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-clear]');
    if (chip) { clearFilter(chip.dataset.clear); return; }

    const dateCell = e.target.closest('[data-date]');
    if (dateCell && dateCell.classList.contains('available')) {
      state.date = dateCell.dataset.date;
      applyFilters();
      return;
    }

    const leaf = e.target.closest('.tree-leaf[data-category]');
    if (leaf) {
      state.category = state.category === leaf.dataset.category ? '' : leaf.dataset.category;
      renderCategoryTree();
      applyFilters();
      return;
    }

    const groupHead = e.target.closest('.tree-group-head');
    if (groupHead) {
      const group = groupHead.parentElement.dataset.group;
      if (state.collapsedGroups.has(group)) state.collapsedGroups.delete(group);
      else state.collapsedGroups.add(group);
      renderCategoryTree();
      return;
    }

    const typeBtn = e.target.closest('[data-type]');
    if (typeBtn) {
      state.type = typeBtn.dataset.type;
      document.querySelectorAll('[data-type]').forEach((b) => b.classList.toggle('active', b === typeBtn));
      applyFilters();
      return;
    }

    const alertBtn = e.target.closest('[data-alert]');
    if (alertBtn) {
      state.alert = alertBtn.dataset.alert;
      document.querySelectorAll('[data-alert]').forEach((b) => b.classList.toggle('active', b === alertBtn));
      applyFilters();
      return;
    }
  });
}

load().catch((error) => {
  document.body.innerHTML = `<main class="content"><div class="empty-hint"><strong>页面载入失败</strong>${esc(error.message)}</div></main>`;
});
