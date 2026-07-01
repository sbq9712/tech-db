const state = {
  records: [],
  knowledge: [],
  manifest: null,
  jobs: null,
  activeView: 'desk',
  filter: 'all',
  query: '',
  category: '',
  selectedRecord: null,
  selectedDoc: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[s]));

async function getJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

async function load() {
  state.manifest = await getJson('./data/processed/manifest.json');
  const shards = await Promise.all(state.manifest.shards.slice(0, 12).map((s) => getJson('./' + s.path)));
  const knowledgeIndex = await getJson('./' + state.manifest.knowledge_index).catch(() => ({ documents: [] }));
  state.jobs = await getJson('./data/processed/classification-jobs.json').catch(() => null);
  state.records = shards.flatMap((x) => x.records || []);
  state.knowledge = knowledgeIndex.documents || [];
  state.selectedRecord = state.records[0] || null;
  state.selectedDoc = state.knowledge[0] || null;
  render();
}

function visibleRecords() {
  const q = state.query.trim().toLowerCase();
  return state.records.filter((item) => {
    if (state.filter === 'news' && item.intelligence_type !== 'news') return false;
    if (state.filter === 'literature' && item.intelligence_type !== 'literature') return false;
    if (state.filter === 'alerts' && !item.is_alert) return false;
    if (state.category && item.category !== state.category) return false;
    if (!q) return true;
    return [item.title, item.body, item.category, item.source, item.url, item.authors].join(' ').toLowerCase().includes(q);
  });
}

function categoryCounts() {
  const map = new Map();
  for (const item of state.records) {
    const category = item.category || '未分类';
    map.set(category, (map.get(category) || 0) + 1);
  }
  return [...map.entries()].sort((a, b) => b[1] - a[1]);
}

function renderSummary() {
  const meta = state.manifest?.meta || {};
  const alerts = state.records.filter((x) => x.is_alert).length;
  $('summaryStrip').innerHTML = [
    ['总情报', meta.records_total || state.records.length],
    ['新闻', meta.records_by_type?.news || state.records.filter((x) => x.intelligence_type === 'news').length],
    ['文献', meta.records_by_type?.literature || state.records.filter((x) => x.intelligence_type === 'literature').length],
    ['当前载入预警', alerts],
  ].map(([label, value]) => `<div class="summary-card"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join('');
}

function renderCategories() {
  $('categoryTree').innerHTML = categoryCounts().slice(0, 120).map(([cat, count]) => `
    <a class="category-chip" href="#" data-category="${esc(cat)}">
      <span>${esc(cat)}</span><span class="count-pill">${count}</span>
    </a>`).join('');
}

function renderFeed() {
  const rows = visibleRecords();
  $('feedCount').textContent = `显示 ${rows.length} 条，当前载入 ${state.records.length} 条`;
  $('feed').innerHTML = rows.slice(0, 180).map((item) => {
    const selected = state.selectedRecord?.content_hash === item.content_hash;
    const params = (item.key_parameters || []).slice(0, 3).map((p) => `<span class="badge ok">${esc(p.value_raw || p.metric_name || '参数')}</span>`).join('');
    return `<article class="feed-card ${selected ? 'active' : ''}" data-record="${esc(item.content_hash)}">
      <div class="card-top">
        <div>
          <div class="card-title">${esc(item.title || '未命名情报')}</div>
          <div class="card-meta">${esc(item.date || '未知日期')} · ${esc(item.source || '未知来源')} · ${item.intelligence_type === 'literature' ? '文献' : '新闻'}</div>
        </div>
        ${item.is_alert ? '<span class="badge warn">预警</span>' : ''}
      </div>
      <div class="card-summary">${esc((item.body || '').slice(0, 260) || '无正文摘要')}</div>
      <div class="badges"><span class="badge">${esc(item.category || '未分类')}</span>${params}</div>
    </article>`;
  }).join('');
}

function renderInspector() {
  const item = state.selectedRecord;
  if (!item) { $('inspector').innerHTML = '<div class="small">暂无情报</div>'; return; }
  const params = (item.key_parameters || []).map((p) => `<div class="param-card">
    <strong>${esc(p.value_raw || p.metric_name || '参数')}</strong>
    <div class="small">数值：${esc(p.value_numeric ?? '')} · 单位：${esc(p.unit || '无')} · 置信：${esc(p.confidence ?? '')}</div>
    <div class="small">证据：${esc(p.evidence_text || '')}</div>
  </div>`).join('') || '<div class="small">暂无关键参数</div>';
  $('inspector').innerHTML = `
    <div class="inspect-block"><h4>${esc(item.title || '未命名情报')}</h4><div class="kv">
      <div>分类</div><div>${esc(item.category || '未分类')}</div>
      <div>日期</div><div>${esc(item.date || '未知')}</div>
      <div>来源</div><div>${esc(item.source || '未知')}</div>
      <div>类型</div><div>${item.intelligence_type === 'literature' ? '文献' : '新闻'}</div>
      <div>预警</div><div>${item.is_alert ? '是' : '否'}</div>
    </div></div>
    <div class="inspect-block"><h4>正文摘要</h4><p>${esc(item.body || '无正文内容')}</p>${item.url ? `<p class="small"><a href="${esc(item.url)}" target="_blank" rel="noreferrer">打开原文</a></p>` : ''}</div>
    <div class="inspect-block"><h4>关键参数</h4>${params}</div>`;
}

function renderKnowledge() {
  $('knowledgeCount').textContent = `${state.knowledge.length} 份行业文档`;
  const q = state.query.trim().toLowerCase();
  const docs = state.knowledge.filter((doc) => !q || [doc.category, doc.summary?.basic_profile].join(' ').toLowerCase().includes(q));
  $('knowledgeList').innerHTML = docs.slice(0, 160).map((doc, i) => {
    const active = state.selectedDoc?.category === doc.category;
    return `<article class="doc-card ${active ? 'active' : ''}" data-doc-index="${i}">
      <h4>${esc(doc.category || '未分类')}</h4>
      <p class="small">${esc(doc.summary?.basic_profile || '暂无摘要')}</p>
      <div class="badges"><span class="badge">${esc((doc.evidence || []).length)} 条证据</span></div>
    </article>`;
  }).join('');
  renderKnowledgeDetail();
}

function renderKnowledgeDetail() {
  const doc = state.selectedDoc;
  if (!doc) { $('knowledgeDetail').innerHTML = '<div class="small">请选择知识库文档</div>'; return; }
  const metrics = (doc.summary?.key_metrics || []).slice(0, 10).map((m) => `<span class="badge ok">${esc(m.unit || '指标')} · ${esc(m.count || 0)}</span>`).join('');
  const evidence = (doc.evidence || []).slice(0, 12).map((e) => `<div class="param-card"><strong>${esc(e.title || '未命名证据')}</strong><div class="small">${esc(e.date || '')} · ${e.intelligence_type === 'literature' ? '文献' : '新闻'}</div>${e.url ? `<div class="small"><a href="${esc(e.url)}" target="_blank" rel="noreferrer">来源链接</a></div>` : ''}</div>`).join('');
  $('knowledgeDetail').innerHTML = `<h3>${esc(doc.category || '未分类')}</h3><div class="inspect-block"><h4>行业概况</h4><p>${esc(doc.summary?.basic_profile || '暂无摘要')}</p><div class="badges">${metrics}</div></div><div class="inspect-block"><h4>证据情报</h4>${evidence || '<div class="small">暂无证据</div>'}</div>`;
}

function renderRadar() {
  const alerts = state.records.filter((x) => x.is_alert || (x.key_parameters || []).length).slice(0, 180);
  $('radarList').innerHTML = alerts.map((item) => `<article class="radar-card"><h4>${esc(item.title || '未命名情报')}</h4><div class="small">${esc(item.date || '')} · ${esc(item.category || '未分类')}</div><div class="badges">${item.is_alert ? '<span class="badge warn">预警</span>' : ''}${(item.key_parameters || []).slice(0, 4).map((p) => `<span class="badge ok">${esc(p.value_raw || '参数')}</span>`).join('')}</div></article>`).join('');
}

function renderPipeline() {
  const meta = state.manifest?.meta || {};
  $('pipeline').innerHTML = `<div class="pipeline-grid">
    <div class="pipeline-card"><h4>数据版本</h4><p class="small">处理时间：${esc(meta.processed_at || '')}</p><p class="small">分类版本：${esc(meta.taxonomy_version || '')}</p><p class="small">抽取版本：${esc(meta.extractor_version || '')}</p></div>
    <div class="pipeline-card"><h4>分片</h4><p class="small">当前 ${esc(state.manifest?.shards?.length || 0)} 个数据分片</p><p class="small">页面按需载入最近分片，避免一次性压垮浏览器。</p></div>
    <div class="pipeline-card"><h4>批量分类</h4><p class="small">目标未分类：${esc(state.jobs?.target_unclassified || '未生成')}</p><p class="small">策略：候选类目约束 + 小模型选择 + 低置信复核。</p></div>
    <div class="pipeline-card"><h4>审计要求</h4><p class="small">保留来源、证据句、置信度、处理版本和缓存命中信息。</p></div>
  </div>`;
}

function renderViews() {
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.dataset.panel === state.activeView));
  document.querySelectorAll('.mode-button').forEach((button) => button.classList.toggle('active', button.dataset.view === state.activeView));
  $('viewTitle').textContent = ({ desk: '情报工作台', knowledge: '知识库文档', radar: '预警雷达', pipeline: '数据流水线' })[state.activeView];
}

function render() {
  renderViews();
  renderSummary();
  renderCategories();
  renderFeed();
  renderInspector();
  renderKnowledge();
  renderRadar();
  renderPipeline();
  document.querySelectorAll('[data-filter]').forEach((button) => button.classList.toggle('active', button.dataset.filter === state.filter));
}

document.addEventListener('click', (event) => {
  const mode = event.target.closest('[data-view]');
  if (mode) { state.activeView = mode.dataset.view; render(); return; }
  const filter = event.target.closest('[data-filter]');
  if (filter) { state.filter = filter.dataset.filter; render(); return; }
  const category = event.target.closest('[data-category]');
  if (category) { event.preventDefault(); state.category = category.dataset.category; state.activeView = 'desk'; render(); return; }
  const record = event.target.closest('[data-record]');
  if (record) { state.selectedRecord = state.records.find((item) => item.content_hash === record.dataset.record) || state.selectedRecord; render(); return; }
  const doc = event.target.closest('[data-doc-index]');
  if (doc) { state.selectedDoc = state.knowledge[Number(doc.dataset.docIndex)] || state.selectedDoc; render(); }
});

$('searchInput').addEventListener('input', (event) => { state.query = event.target.value; render(); });
load().catch((error) => {
  document.body.innerHTML = `<main class="workspace"><section class="panel"><h2>页面载入失败</h2><p>${esc(error.message)}</p></section></main>`;
});
