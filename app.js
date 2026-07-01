const PAGE_SIZE = 80;
const state = {
  manifest: null,
  records: [],
  filtered: [],
  page: 1,
  query: '',
  date: '',
  type: 'all',
  alert: 'all',
  category: '',
  categoryQuery: '',
  calendarMonth: '',
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[s]));
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

async function getJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

async function load() {
  state.manifest = await getJson('./data/processed/manifest.json');
  const shards = await Promise.all(state.manifest.shards.map((s) => getJson('./' + s.path)));
  state.records = shards.flatMap((payload) => payload.records || []);
  state.filtered = state.records;
  state.calendarMonth = (state.records[0]?.date || new Date().toISOString().slice(0, 10)).slice(0, 7);
  bindEvents();
  applyFilters();
}

function categoryCounts() {
  const counts = new Map();
  for (const item of state.records) counts.set(item.category || '未分类', (counts.get(item.category || '未分类') || 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
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
  state.page = Math.min(state.page, Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE)));
  render();
}

function render() {
  renderStats();
  renderLabels();
  renderCalendar();
  renderCategoryList();
  renderActiveFilters();
  renderRecords();
}

function renderStats() {
  const meta = state.manifest?.meta || {};
  const alerts = state.records.filter((item) => item.is_alert).length;
  const cards = [
    ['总情报', meta.records_total || state.records.length],
    ['新闻', meta.records_by_type?.news || state.records.filter((i) => i.intelligence_type === 'news').length],
    ['文献', meta.records_by_type?.literature || state.records.filter((i) => i.intelligence_type === 'literature').length],
    ['预警', alerts],
  ];
  $('stats').innerHTML = cards.map(([label, value]) => `<div class="stat-card"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join('');
}

function renderLabels() {
  $('dateLabel').textContent = state.date || '全部日期';
  $('categoryLabel').textContent = state.category || '全部分类';
  $('resultMeta').textContent = `筛选结果 ${state.filtered.length} 条 / 全库 ${state.records.length} 条`;
}

function renderActiveFilters() {
  const chips = [];
  if (state.query.trim()) chips.push(['检索', state.query.trim(), 'query']);
  if (state.date) chips.push(['日期', state.date, 'date']);
  if (state.category) chips.push(['分类', state.category, 'category']);
  if (state.type !== 'all') chips.push(['类型', state.type === 'news' ? '新闻' : '文献', 'type']);
  if (state.alert !== 'all') chips.push(['预警', state.alert === 'yes' ? '预警' : '非预警', 'alert']);
  $('activeFilters').innerHTML = chips.length
    ? chips.map(([label, value, key]) => `<button class="filter-chip" data-clear="${key}" type="button"><span>${esc(label)}</span>${esc(value)} ×</button>`).join('')
    : '<span class="filter-hint">当前显示全部日期、全部分类、全部类型。</span>';
}

function renderCalendar() {
  const dates = availableDates();
  const [year, month] = state.calendarMonth.split('-').map(Number);
  const first = new Date(year, month - 1, 1);
  const last = new Date(year, month, 0);
  const offset = (first.getDay() + 6) % 7;
  $('calendarTitle').textContent = `${year} 年 ${String(month).padStart(2, '0')} 月`;
  const cells = [];
  for (let i = 0; i < offset; i += 1) cells.push('<button class="day-button empty" type="button"></button>');
  for (let day = 1; day <= last.getDate(); day += 1) {
    const date = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const hasData = dates.has(date);
    cells.push(`<button class="day-button ${hasData ? 'has-data' : ''} ${state.date === date ? 'active' : ''}" data-date="${date}" type="button" ${hasData ? '' : 'disabled'}>${day}</button>`);
  }
  $('calendarGrid').innerHTML = cells.join('');
}

function renderCategoryList() {
  const q = state.categoryQuery.trim().toLowerCase();
  const rows = categoryCounts().filter(([cat]) => !q || cat.toLowerCase().includes(q));
  $('categoryList').innerHTML = rows.map(([cat, count]) => `<button class="category-option ${state.category === cat ? 'active' : ''}" data-category="${esc(cat)}" type="button"><span>${esc(cat)}</span><span>${count}</span></button>`).join('');
}

function renderRecords() {
  const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  const start = (state.page - 1) * PAGE_SIZE;
  const rows = state.filtered.slice(start, start + PAGE_SIZE);
  $('pageLabel').textContent = `${state.page} / ${pages}`;
  $('prevPage').disabled = state.page <= 1;
  $('nextPage').disabled = state.page >= pages;
  if (!rows.length) {
    $('recordList').innerHTML = '<div class="empty-state"><strong>没有匹配情报</strong><span>请清除部分筛选条件，或换一个关键词。</span></div>';
    return;
  }
  $('recordList').innerHTML = rows.map(renderRecordCard).join('');
}

function confidenceScore(item) {
  const base = Number(item.classification_confidence || 0);
  const paramBoost = Math.min((item.key_parameters || []).length * 0.06, 0.24);
  return clamp(base || 0.48 + paramBoost, 0.18, 0.98);
}

function renderRecordCard(item) {
  const typeLabel = item.intelligence_type === 'literature' ? '文献' : '新闻';
  const params = (item.key_parameters || []).slice(0, 2);
  const score = confidenceScore(item);
  const url = item.url ? `<a class="source-link" href="${esc(item.url)}" target="_blank" rel="noreferrer">原文</a>` : '';
  const paramBadges = params.map((p) => `<span class="badge param">${esc(p.value_raw || '参数')}</span>`).join('');
  const summary = (item.body || '').slice(0, 240) || '无正文摘要';
  return `<article class="record-card">
    <div class="record-topline">
      <span class="date-chip">${esc(item.date || '未知')}</span>
      <span class="source-text">${esc(item.source || '未知来源')}</span>
      <span class="spacer"></span>
      ${item.is_alert ? '<span class="badge warn">预警</span>' : ''}
      <span class="badge ${item.intelligence_type === 'literature' ? 'lit' : ''}">${typeLabel}</span>
    </div>
    <div class="record-title-row"><h3>${esc(item.title || '未命名情报')}</h3>${url}</div>
    <p class="record-summary">${esc(summary)}</p>
    <div class="record-foot">
      <div class="badges"><span class="badge category">${esc(item.category || '未分类')}</span>${paramBadges}</div>
      <div class="meter-wrap"><span>${Math.round(score * 100)}%</span><div class="mini-meter"><i style="width:${Math.round(score * 100)}%"></i></div></div>
    </div>
  </article>`;
}

function toggleDrawer(id, buttonId) {
  document.querySelectorAll('.drawer').forEach((node) => {
    if (node.id !== id) node.classList.remove('open');
  });
  const drawer = $(id);
  drawer.classList.toggle('open');
  document.querySelectorAll('.drawer-button').forEach((button) => button.setAttribute('aria-expanded', 'false'));
  $(buttonId).setAttribute('aria-expanded', drawer.classList.contains('open') ? 'true' : 'false');
}

function closeDrawers() {
  document.querySelectorAll('.drawer').forEach((node) => node.classList.remove('open'));
  document.querySelectorAll('.drawer-button').forEach((button) => button.setAttribute('aria-expanded', 'false'));
}

function bindEvents() {
  $('searchInput').addEventListener('input', (event) => { state.query = event.target.value; state.page = 1; applyFilters(); });
  $('dateButton').addEventListener('click', () => toggleDrawer('datePopover', 'dateButton'));
  $('categoryButton').addEventListener('click', () => toggleDrawer('categoryPopover', 'categoryButton'));
  $('clearDate').addEventListener('click', () => { state.date = ''; state.page = 1; closeDrawers(); applyFilters(); });
  $('clearCategory').addEventListener('click', () => { state.category = ''; state.page = 1; closeDrawers(); applyFilters(); });
  $('categorySearch').addEventListener('input', (event) => { state.categoryQuery = event.target.value; renderCategoryList(); });
  $('prevMonth').addEventListener('click', () => shiftMonth(-1));
  $('nextMonth').addEventListener('click', () => shiftMonth(1));
  $('prevPage').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; renderRecords(); } });
  $('nextPage').addEventListener('click', () => { const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE)); if (state.page < pages) { state.page += 1; renderRecords(); } });
  document.addEventListener('click', handleDocumentClick);
}

function shiftMonth(delta) {
  const [year, month] = state.calendarMonth.split('-').map(Number);
  const next = new Date(year, month - 1 + delta, 1);
  state.calendarMonth = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, '0')}`;
  renderCalendar();
}

function clearFilter(key) {
  if (key === 'query') { state.query = ''; $('searchInput').value = ''; }
  if (key === 'date') state.date = '';
  if (key === 'category') state.category = '';
  if (key === 'type') { state.type = 'all'; document.querySelectorAll('[data-type]').forEach((button) => button.classList.toggle('active', button.dataset.type === 'all')); }
  if (key === 'alert') { state.alert = 'all'; document.querySelectorAll('[data-alert]').forEach((button) => button.classList.toggle('active', button.dataset.alert === 'all')); }
  state.page = 1;
  applyFilters();
}

function handleDocumentClick(event) {
  const clear = event.target.closest('[data-clear]');
  if (clear) { clearFilter(clear.dataset.clear); return; }
  const dateButton = event.target.closest('[data-date]');
  if (dateButton) {
    state.date = dateButton.dataset.date;
    state.page = 1;
    closeDrawers();
    applyFilters();
    return;
  }
  const categoryButton = event.target.closest('.category-option[data-category]');
  if (categoryButton) {
    state.category = categoryButton.dataset.category;
    state.page = 1;
    closeDrawers();
    applyFilters();
    return;
  }
  const typeButton = event.target.closest('[data-type]');
  if (typeButton) {
    state.type = typeButton.dataset.type;
    state.page = 1;
    document.querySelectorAll('[data-type]').forEach((button) => button.classList.toggle('active', button === typeButton));
    applyFilters();
    return;
  }
  const alertButton = event.target.closest('[data-alert]');
  if (alertButton) {
    state.alert = alertButton.dataset.alert;
    state.page = 1;
    document.querySelectorAll('[data-alert]').forEach((button) => button.classList.toggle('active', button === alertButton));
    applyFilters();
    return;
  }
}

load().catch((error) => {
  document.body.innerHTML = `<main class="results-panel"><div class="empty-state"><strong>页面载入失败</strong><span>${esc(error.message)}</span></div></main>`;
});
