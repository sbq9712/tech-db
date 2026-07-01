const PAGE_SIZE = 80;
const state = {
  manifest: null,
  records: [],
  filtered: [],
  selectedId: null,
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
const uniq = (arr) => [...new Set(arr.filter(Boolean))];

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
  state.selectedId = state.records[0]?.content_hash || '';
  state.calendarMonth = (state.records[0]?.date || new Date().toISOString().slice(0, 10)).slice(0, 7);
  applyFilters();
  bindEvents();
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
  if (!state.filtered.some((item) => item.content_hash === state.selectedId)) {
    state.selectedId = state.filtered[0]?.content_hash || '';
  }
  render();
}

function render() {
  renderStats();
  renderLabels();
  renderCalendar();
  renderCategoryList();
  renderRecords();
  renderDetail();
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
    $('recordList').innerHTML = '<div class="empty-state">没有匹配的情报。请放宽筛选条件。</div>';
    return;
  }
  $('recordList').innerHTML = rows.map((item) => {
    const selected = item.content_hash === state.selectedId;
    const typeLabel = item.intelligence_type === 'literature' ? '文献' : '新闻';
    const params = (item.key_parameters || []).slice(0, 3).map((p) => `<span class="badge param">${esc(p.value_raw || '参数')}</span>`).join('');
    return `<article class="record-card ${selected ? 'active' : ''}" data-record="${esc(item.content_hash)}">
      <div class="record-top">
        <div>
          <div class="record-title">${esc(item.title || '未命名情报')}</div>
          <div class="record-meta">${esc(item.date || '未知日期')} · ${esc(item.source || '未知来源')}</div>
        </div>
        ${item.is_alert ? '<span class="badge warn">预警</span>' : `<span class="badge ${item.intelligence_type === 'literature' ? 'lit' : ''}">${typeLabel}</span>`}
      </div>
      <div class="record-summary">${esc((item.body || '').slice(0, 260) || '无正文摘要')}</div>
      <div class="badges"><span class="badge">${esc(item.category || '未分类')}</span><span class="badge ${item.intelligence_type === 'literature' ? 'lit' : ''}">${typeLabel}</span>${params}</div>
    </article>`;
  }).join('');
}

function renderDetail() {
  const item = state.records.find((record) => record.content_hash === state.selectedId);
  if (!item) {
    $('detail').innerHTML = '<div class="empty-state">请选择一条情报。</div>';
    return;
  }
  const params = (item.key_parameters || []).map((p) => `<div class="param-card"><strong>${esc(p.value_raw || '参数')}</strong><div class="muted">数值：${esc(p.value_numeric ?? '')} · 单位：${esc(p.unit || '无')} · 置信度：${esc(p.confidence ?? '')}</div><div class="muted">证据：${esc(p.evidence_text || '')}</div></div>`).join('') || '<div class="muted">暂无关键参数</div>';
  $('detail').innerHTML = `
    <section class="detail-block"><h3>${esc(item.title || '未命名情报')}</h3><div class="kv"><div>日期</div><div>${esc(item.date || '未知')}</div><div>分类</div><div>${esc(item.category || '未分类')}</div><div>类型</div><div>${item.intelligence_type === 'literature' ? '文献' : '新闻'}</div><div>预警</div><div>${item.is_alert ? '是' : '否'}</div><div>来源</div><div>${esc(item.source || '')}</div></div></section>
    <section class="detail-block"><h3>正文 / 摘要</h3><p>${esc(item.body || '无正文内容')}</p>${item.url ? `<p class="muted"><a href="${esc(item.url)}" target="_blank" rel="noreferrer">打开原文链接</a></p>` : ''}</section>
    <section class="detail-block"><h3>关键参数</h3>${params}</section>`;
}

function togglePopover(id) {
  document.querySelectorAll('.popover').forEach((node) => {
    if (node.id !== id) node.classList.remove('open');
  });
  $(id).classList.toggle('open');
}

function bindEvents() {
  $('searchInput').addEventListener('input', (event) => { state.query = event.target.value; state.page = 1; applyFilters(); });
  $('dateButton').addEventListener('click', () => togglePopover('datePopover'));
  $('categoryButton').addEventListener('click', () => togglePopover('categoryPopover'));
  $('clearDate').addEventListener('click', () => { state.date = ''; state.page = 1; $('datePopover').classList.remove('open'); applyFilters(); });
  $('clearCategory').addEventListener('click', () => { state.category = ''; state.page = 1; $('categoryPopover').classList.remove('open'); applyFilters(); });
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

function handleDocumentClick(event) {
  const dateButton = event.target.closest('[data-date]');
  if (dateButton) {
    state.date = dateButton.dataset.date;
    state.page = 1;
    $('datePopover').classList.remove('open');
    applyFilters();
    return;
  }
  const categoryButton = event.target.closest('.category-option[data-category]');
  if (categoryButton) {
    state.category = categoryButton.dataset.category;
    state.page = 1;
    $('categoryPopover').classList.remove('open');
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
  const record = event.target.closest('[data-record]');
  if (record) {
    state.selectedId = record.dataset.record;
    renderRecords();
    renderDetail();
    return;
  }
  if (!event.target.closest('.popover-control')) document.querySelectorAll('.popover').forEach((node) => node.classList.remove('open'));
}

load().catch((error) => {
  document.body.innerHTML = `<main class="shell"><section class="panel"><div class="empty-state">页面载入失败：${esc(error.message)}</div></section></main>`;
});
