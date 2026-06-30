const state = { data: null, filter: 'all', query: '', selectedId: null };
const $ = (id) => document.getElementById(id);
async function load() {
  const res = await fetch('./data/processed/intelligence.json', { cache: 'no-store' });
  state.data = await res.json();
  state.selectedId = state.data.records[0]?.content_hash || null;
  render();
}
function matches(item) {
  if (state.filter === 'news' && item.intelligence_type !== 'news') return false;
  if (state.filter === 'literature' && item.intelligence_type !== 'literature') return false;
  if (state.filter === 'alerts' && !item.is_alert) return false;
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  return [item.title, item.category, item.source, item.url, item.authors, item.body].join(' ').toLowerCase().includes(q);
}
function categoryList(records) {
  const counts = new Map();
  records.forEach((item) => counts.set(item.category || '未分类', (counts.get(item.category || '未分类') || 0) + 1));
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
}
function render() {
  if (!state.data) return;
  const filtered = state.data.records.filter(matches);
  const selected = filtered.find((item) => item.content_hash === state.selectedId) || filtered[0] || state.data.records[0];
  if (selected) state.selectedId = selected.content_hash;
  const meta = state.data.meta;
  $('meta').innerHTML = `<div><strong>最近处理：</strong>${meta.processed_at || ''}</div><div><strong>税onomy版本：</strong>${meta.taxonomy_version || '未知'}</div><div><strong>抽取版本：</strong>${meta.extractor_version || '未知'}</div><div><strong>缓存命中：</strong>${meta.cache_hits || 0}</div>`;
  $('stats').innerHTML = `<div class="stat"><div class="num">${meta.records_total || 0}</div><div class="label">总情报</div></div><div class="stat"><div class="num">${meta.records_by_type?.news || 0}</div><div class="label">新闻</div></div><div class="stat"><div class="num">${meta.records_by_type?.literature || 0}</div><div class="label">文献</div></div><div class="stat"><div class="num">${state.data.records.filter((x) => x.is_alert).length}</div><div class="label">预警</div></div>`;
  $('count').textContent = `显示 ${filtered.length} 条`;
  $('nav').innerHTML = categoryList(state.data.records).slice(0, 120).map(([cat, count]) => `<a href="#" data-cat="${cat}"><div class="card"><div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><strong>${cat}</strong><span class="badge">${count}</span></div></div></a>`).join('');
  $('list').innerHTML = filtered.slice(0, 120).map((item) => `<div class="item ${item.content_hash === state.selectedId ? 'active' : ''}" data-id="${item.content_hash}"><div class="item-title">${item.title || '未命名情报'}</div><div class="item-meta">${item.date || '未知日期'} · ${item.source || '未知来源'} · ${item.intelligence_type === 'literature' ? '文献' : '新闻'}</div><div class="muted">${(item.body || '').slice(0, 220) || '无正文摘要'}</div><div class="badges">${item.is_alert ? '<span class="badge warn">预警</span>' : ''}<span class="badge">${item.category || '未分类'}</span>${(item.key_parameters || []).slice(0, 3).map((k) => `<span class="badge">${k.value_raw}</span>`).join('')}</div></div>`).join('');
  $('detail').innerHTML = selected ? `<div class="block"><h4>${selected.title || '未命名情报'}</h4><div class="kv"><div>分类</div><div>${selected.category || '未分类'}</div><div>日期</div><div>${selected.date || '未知'}</div><div>来源</div><div>${selected.source || '未知'}</div><div>类型</div><div>${selected.intelligence_type === 'literature' ? '文献' : '新闻'}</div><div>预警</div><div>${selected.is_alert ? '是' : '否'}</div></div></div><div class="block"><h4>正文</h4><p>${selected.body || '无正文内容'}</p><p class="small">${selected.url ? `<a href="${selected.url}" target="_blank" rel="noreferrer">打开原文</a>` : '无原文链接'}</p></div><div class="block"><h4>关键参数</h4>${(selected.key_parameters || []).length ? selected.key_parameters.map((k) => `<div class="block" style="margin-top:10px"><div><strong>${k.value_raw || '参数'}</strong></div><div class="small">数值：${k.value_numeric ?? ''} · 单位：${k.unit || '无'} · 依据：${k.extraction_reason || '规则抽取'}</div><div class="small">证据：${k.evidence_text || ''}</div></div>`).join('') : '<div class="small">暂无关键参数</div>'}</div>` : '';
  $('knowledge').innerHTML = (state.data.knowledge || []).slice(0, 24).map((doc) => `<div class="block"><h4>${doc.category || '未分类'} <span>${doc.evidence?.length || 0} 条证据</span></h4><div class="small">${doc.summary?.basic_profile || '暂无摘要'}</div><div>${(doc.evidence || []).slice(0, 3).map((e) => `<span class="tag">${e.title || '未命名'}</span>`).join('')}</div></div>`).join('');
  document.querySelectorAll('[data-filter]').forEach((btn) => btn.classList.toggle('active', btn.dataset.filter === state.filter));
}
document.addEventListener('click', (event) => { const filterButton = event.target.closest('button[data-filter]'); if (filterButton) { state.filter = filterButton.dataset.filter; render(); return; } const categoryLink = event.target.closest('[data-cat]'); if (categoryLink) { event.preventDefault(); state.query = categoryLink.dataset.cat; $('search').value = state.query; render(); return; } const item = event.target.closest('[data-id]'); if (item) { state.selectedId = item.dataset.id; render(); } });
$('search').addEventListener('input', (event) => { state.query = event.target.value; render(); });
load();
