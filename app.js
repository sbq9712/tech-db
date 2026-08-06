const PAGE_SIZE = 50;
const HIDDEN_AUTHORS = '手动导入';

// Fuzzy text search: matches query against haystack allowing non-contiguous keywords.
// Strategy: exact substring first, then bigram-based fuzzy match (≥70% bigrams hit).
function fuzzyMatch(haystack, q) {
  if (haystack.includes(q)) return true;
  if (q.length < 3) return false;
  let total = 0, hit = 0;
  for (let i = 0; i < q.length - 1; i++) {
    total++;
    if (haystack.includes(q.slice(i, i + 2))) hit++;
  }
  return hit / total >= 0.7;
}

// Theme: restore saved preference before anything renders
(function initTheme() {
  // Always default to light, ignore any previously saved preference
  document.documentElement.setAttribute('data-theme', 'light');
})();

const state = {
  manifest: null,
  records: [],
  filtered: [],
  categoryOrder: [],
  page: 1,
  query: '',
  dateFrom: '',
  dateTo: '',
  alertLevel: 'aicurated', // default to AI精选
  selectedTypes: { news: true, literature: true },
  selectedTags: { news: new Set(), literature: new Set() }, // empty = all
  selectedCategories: null, // null = all selected; Set = specific selection
  categoryQuery: '',
  collapsedGroups: new Set(),
  expandedClusters: new Set(),
  sortBy: 'date',
  // Report view state
  currentView: 'intelligence', // 'intelligence' | 'reports'
  reportType: 'daily', // 'daily' | 'weekly' | 'monthly'
  reportDate: '',
  reportDatesCache: {}, // { daily: [...], weekly: [...], monthly: [...] }
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (s) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[s]));

// ── On-demand body shard loading (Tier 3) ──
// Fetches a single lite-part shard when user clicks [展开全文]
window.__BODY_SHARDS__ = window.__BODY_SHARDS__ || {};
window.fetchBodyShard = async function(shardIdx, dataVersion) {
  if (window.__BODY_SHARDS__[shardIdx]) return window.__BODY_SHARDS__[shardIdx];
  try {
    const response = await fetch(`data/processed/lite-part-${shardIdx}.js?v=${encodeURIComponent(dataVersion || '')}`);
    if (!response.ok) return null;
    const text = await response.text();
    const marker = 'window.__LITE_PARTS__.push(';
    const start = text.indexOf(marker);
    const end2 = text.lastIndexOf(');');
    if (start < 0 || end2 <= start) return null;
    const payload = text.slice(start + marker.length, end2);
    const records = JSON.parse(payload);
    window.__BODY_SHARDS__[shardIdx] = records;
    return records;
  } catch(e) { return null; }
};

// ── View switching: intelligence ↔ reports ↔ calendar ──
function switchView(view) {
  state.currentView = view;
  const tabs = document.querySelectorAll('.view-tab');
  tabs.forEach((t) => t.classList.toggle('active', t.dataset.view === view));
  const reportView = $('reportView');
  const calendarSidebar = $('calendarView');
  const filterBlocks = document.querySelectorAll('.sidebar > .filter-block');
  const contentToolbar = document.querySelector('.content-toolbar');
  const recordList = $('recordList');
  let reportContent = $('reportContent');
  let calendarContent = $('calendarContent');

  // Hide all special views first
  if (reportView) reportView.style.display = 'none';
  if (calendarSidebar) calendarSidebar.style.display = 'none';
  if (reportContent) reportContent.style.display = 'none';
  if (calendarContent) calendarContent.style.display = 'none';
  const qaSidebar = $('qaSidebar');
  if (qaSidebar) qaSidebar.style.display = 'none';

  const qaView = $('qaView');
  const mainContent = document.querySelector('.content');

  if (view === 'qa') {
    if (qaView) qaView.style.display = 'flex';
    if (mainContent) mainContent.style.display = 'none';
    filterBlocks.forEach((b) => { b.style.display = 'none'; });
    if (qaSidebar) qaSidebar.style.display = '';
    if (contentToolbar) contentToolbar.style.display = 'none';
    recordList.style.display = 'none';
    if (window.qaModule && window.qaModule.switchToQAView) {
      window.qaModule.switchToQAView();
    }
  } else if (view === 'reports') {
    if (qaView) qaView.style.display = 'none';
    if (mainContent) mainContent.style.display = '';
    if (reportView) reportView.style.display = '';
    filterBlocks.forEach((b) => { b.style.display = 'none'; });
    if (contentToolbar) contentToolbar.style.display = 'none';
    if (!reportContent) {
      reportContent = document.createElement('section');
      reportContent.id = 'reportContent';
      reportContent.className = 'report-content';
      recordList.parentNode.appendChild(reportContent);
    }
    recordList.style.display = 'none';
    reportContent.style.display = '';
    renderReportView();
  } else if (view === 'calendar') {
    if (qaView) qaView.style.display = 'none';
    if (mainContent) mainContent.style.display = '';
    if (calendarSidebar) calendarSidebar.style.display = '';
    filterBlocks.forEach((b) => { b.style.display = 'none'; });
    if (contentToolbar) contentToolbar.style.display = 'none';
    if (!calendarContent) {
      calendarContent = document.createElement('section');
      calendarContent.id = 'calendarContent';
      calendarContent.className = 'calendar-content';
      recordList.parentNode.appendChild(calendarContent);
    }
    recordList.style.display = 'none';
    calendarContent.style.display = '';
    renderCalendarView();
  } else {
    if (qaView) qaView.style.display = 'none';
    if (mainContent) mainContent.style.display = '';
    filterBlocks.forEach((b) => { b.style.display = ''; });
    if (contentToolbar) contentToolbar.style.display = '';
    recordList.style.display = '';
  }
}

// ── Report filename helpers ──
// Daily:   YYYY-MM-DD     e.g. 2026-07-21
// Weekly:  YYYY-W##       e.g. 2026-W30  (ISO 8601 week number)
// Monthly: YYYY-MM        e.g. 2026-07
function isoWeek(d) {
  // Copy date so don't modify original
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  // Set to nearest Thursday: current date + 4 - current day number
  // Make Sunday's day number 7 (ISO weekday)
  date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
  // Get first day of year for week calc
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  // Calculate full weeks to nearest Thursday
  const weekNo = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
  return { year: date.getUTCFullYear(), week: weekNo };
}

function reportFileName(type, key) {
  if (type === 'weekly') return `${key}.json`;     // key already YYYY-W##
  if (type === 'monthly') return `${key}.json`;    // key already YYYY-MM
  return `${key}.json`;                              // daily: YYYY-MM-DD
}

// ── Report list: discover available report dates via fetch ──
async function discoverReportDates(type) {
  if (state.reportDatesCache[type]) return state.reportDatesCache[type];
  const keys = [];
  const today = new Date();
  const candidates = [];
  if (type === 'daily') {
    // Scan from 2024-01-01 to today — limit to last 90 days to avoid too many HEAD requests
    const start = new Date(today);
    start.setDate(start.getDate() - 90);
    for (let d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) {
      candidates.push(d.toISOString().slice(0, 10));
    }
  } else if (type === 'weekly') {
    // ISO week naming: YYYY-W## (zero-padded)
    // Scan from 2024-W01 to current week
    const { year: curYear, week: curWeek } = isoWeek(today);
    for (let y = 2024; y <= curYear; y++) {
      const maxWeek = (y < curYear) ? 53 : curWeek;
      for (let w = 1; w <= maxWeek; w++) {
        candidates.push(`${y}-W${String(w).padStart(2, '0')}`);
      }
    }
  } else if (type === 'monthly') {
    // YYYY-MM naming
    for (let y = 2024; y <= today.getFullYear(); y++) {
      for (let m = 0; m < 12; m++) {
        const dt = new Date(y, m, 1);
        if (dt > today) break;
        candidates.push(`${y}-${String(m + 1).padStart(2, '0')}`);
      }
    }
  }
  // Probe candidates in parallel batches
  const BATCH = 12;
  for (let i = 0; i < candidates.length; i += BATCH) {
    const slice = candidates.slice(i, i + BATCH);
    await Promise.all(slice.map(async (key) => {
      const path = `data/reports/${type}/${reportFileName(type, key)}`;
      try {
        const resp = await fetch(path, { method: 'HEAD' });
        if (resp.ok) keys.push(key);
      } catch (e) { /* ignore */ }
    }));
  }
  keys.sort().reverse();
  state.reportDatesCache[type] = keys;
  return keys;
}

function renderReportList(dates) {
  const list = $('reportList');
  if (!list) return;
  if (!dates.length) {
    list.innerHTML = '<div class="report-list-empty">暂无可用报告</div>';
    return;
  }
  if (state.reportType === 'weekly') {
    list.innerHTML = renderWeeklyList(dates);
  } else {
    list.innerHTML = dates.map((d) => {
      const isActive = d === state.reportDate ? ' active' : '';
      return `<div class="report-list-item${isActive}" data-report-date="${esc(d)}">${esc(d)}</div>`;
    }).join('');
  }
}

// ── Weekly list: hierarchical by month with expand/collapse ──
function renderWeeklyList(dates) {
  // dates is sorted descending: ["2026-W30", "2026-W29", ...]
  // Group by the month of each ISO week's Monday
  const groups = {}; // { "2026年7月": [{key, label}, ...] }
  const groupOrder = []; // preserve insertion order (newest first)

  for (const key of dates) {
    const monday = isoWeekKeyToDate(key);
    if (!monday) continue;
    const monthLabel = `${monday.getFullYear()}年${monday.getMonth() + 1}月`;
    if (!groups[monthLabel]) {
      groups[monthLabel] = [];
      groupOrder.push(monthLabel);
    }
    groups[monthLabel].push(key);
  }

  // Determine which month to expand (the one containing the selected report, or the latest)
  let expandMonth = groupOrder[0] || '';
  if (state.reportDate) {
    const selMonday = isoWeekKeyToDate(state.reportDate);
    if (selMonday) {
      const selLabel = `${selMonday.getFullYear()}年${selMonday.getMonth() + 1}月`;
      if (groups[selLabel]) expandMonth = selLabel;
    }
  }

  let html = '';
  for (const monthLabel of groupOrder) {
    const weeks = groups[monthLabel];
    const isExpanded = monthLabel === expandMonth;
    const arrow = isExpanded ? '▾' : '▸';

    html += `<div class="report-week-group" data-month="${esc(monthLabel)}">`;
    html += `<div class="report-week-month" data-toggle-month="${esc(monthLabel)}">${arrow} ${esc(monthLabel)}</div>`;
    html += `<div class="report-week-items" style="display:${isExpanded ? '' : 'none'}">`;

    // Label weeks within the month: 第一周 = earliest, displayed newest-first
    // weeks[0] is newest (dates sorted descending), so label = 第N周 where N = total - idx
    const total = weeks.length;
    weeks.forEach((key, idx) => {
      const weekNum = total - idx;
      const weekLabel = `第${'一二三四五六七八九十'[weekNum - 1]}周`;
      const isActive = key === state.reportDate ? ' active' : '';
      html += `<div class="report-list-item${isActive}" data-report-date="${esc(key)}">${esc(weekLabel)}</div>`;
    });

    html += `</div></div>`;
  }
  return html;
}

// Convert ISO week key (e.g. "2026-W29") to the Monday Date
function isoWeekKeyToDate(key) {
  const m = key.match(/^(\d{4})-W(\d{2})$/);
  if (!m) return null;
  const year = parseInt(m[1]);
  const week = parseInt(m[2]);
  // ISO week: Jan 4 is always in week 1
  const jan4 = new Date(year, 0, 4);
  const jan4Day = (jan4.getDay() + 6) % 7; // 0=Monday
  const week1Monday = new Date(jan4);
  week1Monday.setDate(jan4.getDate() - jan4Day);
  const monday = new Date(week1Monday);
  monday.setDate(week1Monday.getDate() + (week - 1) * 7);
  return monday;
}

async function renderReportView() {
  const reportContent = $('reportContent');
  if (!reportContent) return;
  reportContent.innerHTML = '<div class="report-empty">正在加载报告列表…</div>';
  const dates = await discoverReportDates(state.reportType);
  renderReportList(dates);
  // Auto-select latest report if none selected or selected date not available
  if (!state.reportDate || !dates.includes(state.reportDate)) {
    state.reportDate = dates[0] || '';
    renderReportList(dates);
  }
  if (state.reportDate) {
    await loadReport(state.reportType, state.reportDate);
  } else {
    reportContent.innerHTML = '<div class="report-empty"><strong>暂无报告</strong>请先生成报告文件。</div>';
  }
}

async function loadReport(type, key) {
  const reportContent = $('reportContent');
  if (!reportContent) return;
  reportContent.innerHTML = '<div class="report-empty">正在加载报告…</div>';
  const path = `data/reports/${type}/${reportFileName(type, key)}`;
  try {
    const resp = await fetch(path);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const text = await resp.text();
    // Try JSON first (new format), fall back to markdown (old format)
    try {
      const report = JSON.parse(text);
      reportContent.innerHTML = renderReportJSON(report);
    } catch {
      reportContent.innerHTML = renderMarkdown(text);
    }
    document.querySelector('.content').scrollTo({ top: 0, behavior: 'instant' });
  } catch (e) {
    reportContent.innerHTML = `<div class="report-error">报告加载失败：${esc(e.message)}<br><span style="color:var(--text-quaternary);font-size:12px">${esc(path)}</span></div>`;
  }
}

// ── Render structured report JSON → beautiful HTML ──
function renderReportJSON(report) {
  const typeLabels = { daily: '日报', weekly: '周报', monthly: '月报' };
  const typeBadge = typeLabels[report.type] || '报告';

  // ── Header ──
  let html = `<div class="rp-header">`;
  html += `<h1 class="rp-title">TechDB${esc(typeBadge)}</h1>`;
  html += `<div class="rp-date-range">${esc(report.date_label || report.date_range || '')}</div>`;
  html += `</div>`;

  // ── Main theme (weekly/monthly only) ──
  if (report.main_theme) {
    html += `<div class="rp-theme-card">`;
    html += `<div class="rp-theme-label">本期主线</div>`;
    html += `<div class="rp-theme-text">${esc(report.main_theme)}</div>`;
    html += `</div>`;
  }

  // ── Overview (weekly/monthly only) ──
  if (report.overview) {
    html += `<p class="rp-overview">${esc(report.overview)}</p>`;
  }

  // ── Headlines grid ──
  const headlines = report.headlines || {};
  const sectors = report.sectors || [];
  const hasHeadlines = sectors.some(s => (headlines[s.name] || []).length > 0);
  if (hasHeadlines) {
    html += `<div class="rp-headlines-grid">`;
    for (const sector of sectors) {
      const hl = headlines[sector.name] || [];
      if (!hl.length) continue;
      html += `<div class="rp-headlines-col">`;
      html += `<div class="rp-headlines-sector">${esc(sector.name)}</div>`;
      html += `<ul class="rp-headlines-list">`;
      for (const h of hl) {
        html += `<li>${esc(h)}</li>`;
      }
      html += `</ul>`;
      html += `</div>`;
    }
    html += `</div>`;
  }

  // ── Sector sections with entries ──
  for (let si = 0; si < sectors.length; si++) {
    const sector = sectors[si];
    if (!sector.entries || !sector.entries.length) continue;

    html += `<div class="rp-sector">`;
    html += `<div class="rp-sector-header"><span class="rp-sector-num">${String(si + 1).padStart(2, '0')}</span><span class="rp-sector-name">${esc(sector.name)}</span></div>`;

    if (sector.review) {
      html += `<p class="rp-sector-review">${esc(sector.review)}</p>`;
    }

    // Render entries as record cards
    for (const entry of sector.entries) {
      const card = renderReportEntry(entry);
      if (card) html += card;
    }

    html += `</div>`;
  }

  return html;
}

// Render a single report entry — maps short keys to renderRecordCard format
// so report entries look IDENTICAL to main table entries (and stay in sync)
function renderReportEntry(entry) {
  if (!entry || !entry.t) return '';
  // Map report entry (short keys) to the long-key format renderRecordCard expects
  const item = {
    _idx: -1,
    title: entry.t || '',
    body: entry.b || '',
    has_body: entry.hb || 0,
    date: entry.d || '',
    intelligence_type: entry.i === 'l' ? 'literature' : 'news',
    url: entry.u || '',
    category: entry.c || '未分类',
    authors: entry.a || '',
    tag: entry.tg || '',
    topic: entry.tp || '',
    key_params: entry.kp || [],
    level: entry.lv || 0,
    is_dup: 0,
    full_body: '',
    comment: entry.cm || '',
    alert_reason: entry.wr || '',
    ai_summary: entry.as || '',
    score: entry.sc || 0,
    score_dims: entry.scd || null,
    ai_picked: entry.aip || 0,
    cluster_id: '',
    cluster_name: '',
    cluster_parent: 0,
  };
  return renderRecordCard(item);
}

// ── Simple markdown → HTML renderer (safe: escapes HTML first) ──
function renderMarkdown(md) {
  // 1. Escape all HTML to prevent injection
  let text = esc(md);
  // 2. Code blocks (fenced ```) — extract first to protect content
  const codeBlocks = [];
  text = text.replace(/```([\s\S]*?)```/g, (m, code) => {
    codeBlocks.push(code.replace(/^\n/, '').replace(/\n$/, ''));
    return ` CODE${codeBlocks.length - 1} `;
  });
  // 3. Inline code `code`
  const inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, (m, code) => {
    inlineCodes.push(code);
    return ` INLINE${inlineCodes.length - 1} `;
  });
  // Split into lines for block-level parsing
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  let inList = false;
  let listType = null; // 'ul' or 'ol'
  const closeList = () => { if (inList) { out.push(`</${listType}>`); inList = false; listType = null; } };
  while (i < lines.length) {
    let line = lines[i];
    // Code block placeholder (whole line)
    if (/^ CODE\d+ $/.test(line.trim())) {
      closeList();
      const idx = Number(line.trim().replace(/ CODE/, '').replace(/ /, ''));
      out.push(`<pre><code>${codeBlocks[idx]}</code></pre>`);
      i++;
      continue;
    }
    // Horizontal rule
    if (/^---+\s*$/.test(line) || /^\*\*\*+\s*$/.test(line)) {
      closeList();
      out.push('<hr>');
      i++;
      continue;
    }
    // Headings
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      const level = h[1].length;
      out.push(`<h${level}>${inlineFmt(h[2])}</h${level}>`);
      i++;
      continue;
    }
    // Blockquote
    if (/^&gt;\s?/.test(line)) {
      closeList();
      const quoteLines = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^&gt;\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${inlineFmt(quoteLines.join(' '))}</blockquote>`);
      continue;
    }
    // Unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      if (!inList || listType !== 'ul') { closeList(); out.push('<ul>'); inList = true; listType = 'ul'; }
      out.push(`<li>${inlineFmt(line.replace(/^\s*[-*+]\s+/, ''))}</li>`);
      i++;
      continue;
    }
    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      if (!inList || listType !== 'ol') { closeList(); out.push('<ol>'); inList = true; listType = 'ol'; }
      out.push(`<li>${inlineFmt(line.replace(/^\s*\d+\.\s+/, ''))}</li>`);
      i++;
      continue;
    }
    // Blank line
    if (line.trim() === '') {
      closeList();
      i++;
      continue;
    }
    // Paragraph: collect consecutive non-blank, non-special lines
    closeList();
    const paraLines = [line];
    let j = i + 1;
    while (j < lines.length) {
      const nxt = lines[j];
      if (nxt.trim() === '') break;
      if (/^(#{1,6})\s+/.test(nxt)) break;
      if (/^\s*[-*+]\s+/.test(nxt)) break;
      if (/^\s*\d+\.\s+/.test(nxt)) break;
      if (/^&gt;\s?/.test(nxt)) break;
      if (/^---+\s*$/.test(nxt)) break;
      paraLines.push(nxt);
      j++;
    }
    out.push(`<p>${inlineFmt(paraLines.join(' '))}</p>`);
    i = j;
  }
  closeList();
  let html = out.join('\n');
  // Restore inline code
  html = html.replace(/ INLINE(\d+) /g, (m, idx) => `<code>${inlineCodes[Number(idx)]}</code>`);
  return html;

  // Inline formatting: bold, italic, links
  function inlineFmt(s) {
    // Restore code placeholders first (already escaped), protect from bold/italic
    // Bold **text** or __text__
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    // Italic *text* or _text_ (avoid matching ** by requiring non-* before/after)
    s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
    // Links [text](url)
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    return s;
  }
}

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function normalizeCategory(value) {
  return String(value || '');
}

function parseCategoryPath(cat) {
  const parts = cat.split('/');
  return { top: parts[0], full: cat };
}

function buildCategoryTree(useFiltered = false) {
  const counts = new Map();
  for (const item of (useFiltered ? state.filtered : state.records)) {
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
  // Taxonomy is immutable: never append categories invented by data/LLM.
  // Records outside categoryOrder must be repaired by the pipeline, not shown as new tree nodes.

  const q = state.categoryQuery.trim().toLowerCase();
  // Build multi-level tree using / as separator
  const tree = {}; // top-level group name → nested tree
  for (const cat of orderedCats) {
    const count = counts.get(cat) || 0;
    if (q && !cat.toLowerCase().includes(q)) continue;
    const parts = cat.split('/');
    const top = parts[0];
    if (!tree[top]) tree[top] = { count: 0, children: {} };
    // Walk/create nested children
    let node = tree[top];
    node.count += count;
    for (let i = 1; i < parts.length; i++) {
      const key = parts.slice(0, i + 1).join('/');
      if (!node.children[key]) node.children[key] = { count: 0, children: {}, label: parts[i], cat: key };
      node = node.children[key];
      node.count += count;
    }
  }
  // Compute group totals
  const groupTotals = {};
  for (const [top, node] of Object.entries(tree)) groupTotals[top] = node.count;
  return { tree, groupTotals };
}

function renderTreeNode(node, level) {
  const childKeys = Object.keys(node.children).sort((a, b) => node.children[b].count - node.children[a].count);
  if (!childKeys.length) return '';
  const pad = level * 12;
  const html = childKeys.map(key => {
    const child = node.children[key];
    const grandChildren = Object.keys(child.children);
    const hasChildren = grandChildren.length > 0;
    const collapsed = state.collapsedGroups.has(key);
    const fullyChecked = isCategoryFullySelected(key);
    const partialChecked = isCategoryPartiallySelected(key);
    const checkedClass = fullyChecked ? 'checked' : (partialChecked ? 'partial' : '');
    let inner = `<div class="tree-node tree-lvl-${level}" style="padding-left:${pad}px">`;
    if (hasChildren) {
      inner += `<span class="tree-arrow" data-toggle="${esc(key)}">${collapsed ? '▸' : '▾'}</span>`;
    } else {
      inner += `<span class="tree-arrow-spacer"></span>`;
    }
    inner += `<span class="tree-checkbox ${checkedClass}" data-category="${esc(key)}"></span>`;
    inner += `<span class="tree-label" data-category="${esc(key)}">${esc(child.label)}</span><span class="leaf-count">${child.count}</span></div>`;
    if (hasChildren && !collapsed) {
      inner += renderTreeNode(child, level + 1);
    }
    return inner;
  }).join('');
  return html;
}

function getLatestDate() {
  const today = new Date().toISOString().slice(0, 10);
  const dates = [...new Set(state.records.map((r) => r.date).filter(Boolean))].sort();
  // Find latest date that is today or earlier
  const valid = dates.filter(d => d <= today);
  return valid[valid.length - 1] || dates[dates.length - 1] || '';
}

async function load() {
  const loadingHint = document.querySelector('.loading-hint');
  // Inline data (file:// protocol) or fetch (http:// protocol)
  if (window.__MANIFEST__) {
    state.manifest = window.__MANIFEST__;
  } else {
    state.manifest = await getJson('./data/processed/manifest.json');
  }
  if (window.__CATEGORY_ORDER__) {
    state.categoryOrder = window.__CATEGORY_ORDER__.categories || [];
  } else {
    state.categoryOrder = (await getJson('./data/category-order.json').catch(() => ({ categories: [] }))).categories || [];
  }
  state.allRecords = [];
  try {
    let raw;
    if (window.__LITE_LOADED__ && window.__LITE_DATA__) {
      raw = window.__LITE_DATA__;
    } else if (!window.__LITE_LOADED__) {
      // Wait for async lite data to finish loading.
      await loadLiteData();
      raw = window.__LITE_DATA__ || [];
    } else {
      raw = await getJson('./data/processed/all-records-lite.json');
    }
    const arr = Array.isArray(raw) ? raw : (raw.records || []);
    // Expand short keys to full names — optimized loop
    state.allRecords = new Array(arr.length);
    state._bodyMap = new Map(); // index → body text (populated in Phase 2)
    for (let i = 0; i < arr.length; i++) {
      const r = arr[i];
      state.allRecords[i] = {
        _idx: i,
        title: r.t || '',
        body: r.b || '',
        has_body: r.hb || 0,
        date: r.d || '',
        intelligence_type: r.i === 'l' ? 'literature' : 'news',
        url: r.u || '',
        category: r.c || '未分类',
        authors: r.a || '',
        tag: r.tg || '',
        topic: r.tp || '',
        key_params: r.kp || [],
        level: r.lv || 0,
        is_dup: r.dp || 0,
        full_body: r.fb || '',
        comment: r.cm || '',
        alert_reason: r.wr || '',
        ai_summary: r.as || '',
        score: r.sc || 0,
        score_dims: r.scd || null,
        ai_picked: r.aip || 0,
        ai_summary_pending: !r.as && (r.b || r.hb) ? 1 : 0,
        cluster_id: r.cl || '',
        cluster_name: r.cln || '',
        cluster_parent: r.cp === undefined ? 0 : r.cp,
      };
    }
  } catch (e) {
    console.error('Failed to load data:', e);
    if (loadingHint) loadingHint.textContent = '数据加载失败，请刷新页面重试。';
    return;
  }
  state.records = state.allRecords;
  state.filtered = state.records;

  // Pre-compute display total (excluding cluster children and duplicates)
  state._displayTotal = state.records.filter(r => r.cluster_parent !== 1 && !r.is_dup).length;

  // Pre-compute performance maps
  state.clusterChildCounts = new Map();
  state.categoryLeaves = new Map();
  for (const r of state.records) {
    // Cluster child counts: cp=1 is a hidden child; count children by cluster id.
    if (r.cluster_id && r.cluster_parent === 1) {
      state.clusterChildCounts.set(r.cluster_id, (state.clusterChildCounts.get(r.cluster_id) || 0) + 1);
    }
    // Category leaves
    const cat = r.category || '未分类';
    if (!state.categoryLeaves.has(cat)) state.categoryLeaves.set(cat, new Set([cat]));
    else state.categoryLeaves.get(cat).add(cat);
  }

  renderHeader();
  bindEvents();
  // Set initial sort active state
  const w1 = $('sortWord1'); if (w1) w1.classList.add('active');
  applyFilters();

  // Tier 2: When summary shards load in background, update AI summaries + score dims
  document.addEventListener('summary-ready', () => {
    const summaryParts = window.__SUMMARY_PARTS__ || [];
    for (let i = 0; i < summaryParts.length; i++) {
      if (!summaryParts[i]) continue;
      for (const item of summaryParts[i]) {
        const idx = item.i;
        if (idx >= 0 && idx < state.allRecords.length) {
          const rec = state.allRecords[idx];
          if (item.as) rec.ai_summary = item.as;
          if (item.scd) rec.score_dims = item.scd;
          if (item.kp && item.kp.length) rec.key_params = item.kp;
        }
      }
    }
    // Re-render visible cards to show summaries
    const list = $('recordList');
    if (list && state.filtered.length) {
      renderRecords();
    }
  });
}

const ALERT_LEVELS = [
  { id: 'crawl', label: '信息爬取', desc: '收集到的全量情报（含重复）', test: () => true },
  { id: 'filtered', label: '信息筛选', desc: '全量情报中去除不相关情报及重复情报', test: (r) => (r.category || '') !== '不相关' && !r.is_dup },
  { id: 'aicurated', label: 'AI精选', desc: '相关且AI评分超过领域精选阈值的情报', test: (r) => (r.category || '') !== '不相关' && r.ai_picked === 1 },
  { id: 'curated', label: '精选情报', desc: '入选前沿技术追踪月报的情报', test: (r) => r.level >= 1 },
  { id: 'key', label: '重点情报', desc: '需要重点关注的重要情报', test: (r) => r.level >= 2 },
  { id: 'alert', label: '预警情报', desc: '值得高度关注的预警情报', test: (r) => r.level >= 3 },
];

function getAlertLevelCounts() {
  const records = getFilteredForCounts('alert');
  const counts = { crawl: 0, filtered: 0, aicurated: 0, curated: 0, key: 0, alert: 0 };
  for (const r of records) {
    // 信息爬取: count ALL records (including cp=1, dp=1)
    counts.crawl++;
    // All other counts: skip cluster children and duplicates (matching list display)
    if (r.cluster_parent === 1) continue;
    if (r.is_dup) continue;
    if ((r.category || '') !== '不相关') counts.filtered++;
    if ((r.category || '') !== '不相关' && r.ai_picked === 1) counts.aicurated++;
    if (r.level >= 1) counts.curated++;
    if (r.level >= 2) counts.key++;
    if (r.level >= 3) counts.alert++;
  }
  return counts;
}

function renderAlertLevels() {
  const counts = getAlertLevelCounts();
  state.alertLevelCounts = counts;
  const max = Math.max(...Object.values(counts), 1);
  const activeId = state.alertLevel;
  const html = ALERT_LEVELS.map(level => {
    const cnt = counts[level.id] || 0;
    const pct = (cnt / max * 100).toFixed(1);
    const isActive = activeId === level.id;
    return `<div class="alert-level-row ${isActive ? 'active' : ''}" data-alert-level="${level.id}">
      <div class="alert-level-info">
        <span class="alert-level-label">${esc(level.label)}</span>
        <span class="alert-level-tooltip">${esc(level.desc)}</span>
      </div>
      <div class="alert-level-bar-wrap">
        <div class="alert-level-bar" style="width:${pct}%"></div>
      </div>
      <span class="alert-level-count">${cnt.toLocaleString()}</span>
    </div>`;
  }).join('');
  $('alertLevels').innerHTML = html;
}

function isAlertLevelMatch(item) {
  if (state.alertLevel === 'all' || state.alertLevel === 'crawl') return true;
  const level = ALERT_LEVELS.find(l => l.id === state.alertLevel);
  if (!level) return true;
  return level.test(item);
}

// Get filtered records excluding a specific filter (for cross-filter count linking)
// exclude: 'category' | 'type' | 'alert' | 'none'
function getFilteredForCounts(exclude = 'none') {
  const q = state.query.trim().toLowerCase().replace(/\s+/g, '');
  const cq = state.categoryQuery.trim().toLowerCase();
  return state.records.filter((item) => {
    if (state.dateFrom && (!item.date || item.date < state.dateFrom)) return false;
    if (state.dateTo && (!item.date || item.date > state.dateTo)) return false;
    if (exclude !== 'category' && state.selectedCategories && !state.selectedCategories.has(item.category || '未分类')) return false;
    if (exclude !== 'category' && cq) {
      const cat = (item.category || '未分类').toLowerCase();
      if (!cat.includes(cq)) return false;
    }
    if (exclude !== 'type' && !isTypeSelected(item.intelligence_type, item.tag)) return false;
    if (exclude !== 'alert' && !isAlertLevelMatch(item)) return false;
    if (!q) return true;
    const haystack = [item.title, item.body, item.category, item.authors, item.url].join(' ').toLowerCase().replace(/\s+/g, '');
    return fuzzyMatch(haystack, q);
  });
}

function renderHeader() {
  // Count by type/tag — exclude 'type' filter so type counts reflect other filters
  const filtered = getFilteredForCounts('type');
  const tagCounts = {};
  let newsTotal = 0, litTotal = 0;
  const isCrawl = state.alertLevel === 'crawl';
  for (const item of filtered) {
    // Skip cluster children and duplicates — except in crawl mode which shows everything
    if (!isCrawl) {
      if (item.cluster_parent === 1) continue;
      if (item.is_dup) continue;
    }
    if (item.intelligence_type === 'news') newsTotal++;
    if (item.intelligence_type === 'literature') litTotal++;
    if (item.tag) tagCounts[item.tag] = (tagCounts[item.tag] || 0) + 1;
  }

  // Update counts
  const setCnt = (id, val) => { const el = $(id); if (el) el.textContent = val || ''; };
  setCnt('newsCount', newsTotal.toLocaleString());
  setCnt('litCount', litTotal.toLocaleString());
  setCnt('cnt-tech', tagCounts['技术突破'] || 0);
  setCnt('cnt-industry', tagCounts['产业进展'] || 0);
  setCnt('cnt-policy', tagCounts['政策监管'] || 0);
  setCnt('cnt-capital', tagCounts['资本运作'] || 0);
  setCnt('cnt-observe', tagCounts['行业观察'] || 0);
  setCnt('cnt-paper', tagCounts['研究论文'] || 0);
  setCnt('cnt-review', tagCounts['观点评论'] || 0);

  // Sync checkbox visual state with state
  document.querySelectorAll('[data-type-toggle]').forEach((cb) => {
    const t = cb.dataset.typeToggle;
    cb.classList.toggle('checked', !!state.selectedTypes[t]);
  });
  // Sync popover sub-tag checkboxes
  document.querySelectorAll('.type-popover-item').forEach((item) => {
    const t = item.dataset.type;
    const tag = item.dataset.tag;
    const cb = item.querySelector('.tree-checkbox');
    if (cb) {
      const tags = state.selectedTags[t];
      // Checked if type is on and (no specific tags selected = all, OR this tag is selected)
      const isChecked = state.selectedTypes[t] && (!tags || tags.size === 0 || tags.has(tag));
      cb.classList.toggle('checked', isChecked);
    }
  });
}

function isCategorySelected(cat) {
  if (!state.selectedCategories) return true; // null = all
  // Check if cat is a selected leaf
  if (state.selectedCategories.has(cat)) return true;
  // For intermediate nodes: selected if ANY descendant leaf is selected
  for (const s of state.selectedCategories) {
    if (s.startsWith(cat + '/')) return true;
  }
  return false;
}

// Get all leaf categories (full paths) under a prefix
function getDescendantLeaves(prefix) {
  const leaves = new Set();
  for (const item of state.records) {
    const cat = item.category || '未分类';
    if (cat === prefix || cat.startsWith(prefix + '/')) {
      leaves.add(cat);
    }
  }
  for (const path of state.categoryOrder) {
    const normalized = normalizeCategory(path);
    if (normalized === prefix || normalized.startsWith(prefix + '/')) {
      leaves.add(normalized);
    }
  }
  return [...leaves];
}

// Lazy-init: convert null (all) to a Set of all leaf categories
function materializeSelection() {
  if (!state.selectedCategories) {
    state.selectedCategories = new Set();
    const seen = new Set();
    for (const item of state.records) {
      const cat = item.category || '未分类';
      if (!seen.has(cat)) { seen.add(cat); state.selectedCategories.add(cat); }
    }
  }
}

function selectCategory(cat) {
  materializeSelection();
  const leaves = getDescendantLeaves(cat);
  if (leaves.length === 0) leaves.push(cat);
  for (const l of leaves) state.selectedCategories.add(l);
}

function deselectCategory(cat) {
  materializeSelection();
  const leaves = getDescendantLeaves(cat);
  if (leaves.length === 0) leaves.push(cat);
  for (const l of leaves) state.selectedCategories.delete(l);
}

function isCategoryFullySelected(cat) {
  if (!state.selectedCategories) return true; // null = all
  const leaves = getDescendantLeaves(cat);
  if (leaves.length === 0) return state.selectedCategories.has(cat);
  return leaves.every(l => state.selectedCategories.has(l));
}

function isCategoryPartiallySelected(cat) {
  if (!state.selectedCategories) return false; // null = all fully selected
  const leaves = getDescendantLeaves(cat);
  if (leaves.length === 0) return false;
  const selected = leaves.filter(l => state.selectedCategories.has(l));
  return selected.length > 0 && selected.length < leaves.length;
}

function selectAllCategories() {
  state.selectedCategories = null; // null = all
}

function deselectAllCategories() {
  state.selectedCategories = new Set();
}

function isTypeSelected(intelligenceType, tag) {
  if (!state.selectedTypes[intelligenceType]) return false;
  const tags = state.selectedTags[intelligenceType];
  if (!tags || tags.size === 0) return true; // empty = all tags
  return tags.has(tag);
}

function applyFilters() {
  const q = state.query.trim().toLowerCase().replace(/\s+/g, '');
  const cq = state.categoryQuery.trim().toLowerCase();
  state.filtered = state.records.filter((item) => {
    if (state.dateFrom && (!item.date || item.date < state.dateFrom)) return false;
    if (state.dateTo && (!item.date || item.date > state.dateTo)) return false;
    if (state.selectedCategories && !state.selectedCategories.has(item.category || '未分类')) return false;
    if (!isTypeSelected(item.intelligence_type, item.tag)) return false;
    if (!isAlertLevelMatch(item)) return false;
    // Hide cluster children and duplicates in ALL modes (including crawl)
    // But crawl mode's total count still includes them
    if (item.cluster_parent === 1) return false;
    if (item.is_dup) return false;
    if (cq) {
      const cat = (item.category || '未分类').toLowerCase();
      if (!cat.includes(cq)) return false;
    }
    if (!q) return true;
    const haystack = [item.title, item.body, item.category, item.authors, item.url].join(' ').toLowerCase().replace(/\s+/g, '');
    return fuzzyMatch(haystack, q);
  });
  // Sort
  const sortBy = state.sortBy || 'date';
  if (sortBy === 'score') {
    state.filtered.sort((a, b) => (b.score || 0) - (a.score || 0) || (b.date || '').localeCompare(a.date || ''));
  } else {
    state.filtered.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
  }
  state.page = 1;
  renderAll();
}

function renderAll() {
  renderCategoryTree();
  renderAlertLevels();
  renderHeader();
  renderToolbar();
  renderRecords();
}

function renderToolbar() {
  // Show the count matching the currently selected alert level (mirrors the alert level bar)
  const levelCounts = state.alertLevelCounts || {};
  const displayCount = levelCounts[state.alertLevel] !== undefined
    ? levelCounts[state.alertLevel]
    : state.filtered.length;
  $('resultCount').innerHTML = `共 <strong>${displayCount.toLocaleString()}</strong> 条结果`;
  // Alert level clear button removed (now in top bar)
  const chips = [];
  if (state.query.trim()) chips.push({ label: '检索', value: state.query.trim(), key: 'query' });
  if (state.dateFrom || state.dateTo) {
    let dv = state.dateFrom === state.dateTo ? state.dateFrom : `${state.dateFrom || '…'} ~ ${state.dateTo || '…'}`;
    chips.push({ label: '日期', value: dv, key: 'date' });
  }
  // Alert level chip is not shown — cannot be cleared
  // if (state.alertLevel !== 'all') {
  //   const lvl = ALERT_LEVELS.find(l => l.id === state.alertLevel);
  //   if (lvl) chips.push({ label: '情报等级', value: lvl.label, key: 'alert' });
  // }
  if (state.selectedCategories && state.selectedCategories.size < state.records.length) {
    const cnt = state.selectedCategories.size;
    chips.push({ label: '分类', value: `已选 ${cnt} 类`, key: 'category' });
  }
  const typeChips = [];
  if (!state.selectedTypes.news && !state.selectedTypes.literature) {
    typeChips.push('无');
  } else {
    const parts = [];
    if (state.selectedTypes.news) parts.push('新闻');
    if (state.selectedTypes.literature) parts.push('文献');
    if (parts.length < 2) typeChips.push(parts.join('+'));
  }
  const newsTags = state.selectedTags.news;
  const litTags = state.selectedTags.literature;
  const tagParts = [];
  if (newsTags && newsTags.size > 0) tagParts.push(`新闻: ${[...newsTags].join('/')}`);
  if (litTags && litTags.size > 0) tagParts.push(`文献: ${[...litTags].join('/')}`);
  if (typeChips.length) chips.push({ label: '情报类型', value: typeChips[0], key: 'type' });
  if (tagParts.length) chips.push({ label: '标签', value: tagParts.join(', '), key: 'type' });
  $('activeChips').innerHTML = chips.length
    ? chips.map((c) => `<button class="chip" data-clear="${c.key}" type="button">${esc(c.label)}: ${esc(c.value)} ×</button>`).join('')
    : '';
  $('clearDateBtn').style.display = (state.dateFrom || state.dateTo) ? '' : 'none';
}

function renderCategoryTree() {
  const filtered = getFilteredForCounts('category');
  const origRecords = state.records;
  state.records = filtered;
  const { tree, groupTotals } = buildCategoryTree(false);
  state.records = origRecords;
  const groupOrder = ['零碳产业', 'AI与智能科技', '通用技术', '不相关', '未分类'];
  const allGroups = [...Object.keys(tree)].sort((a, b) => {
    const ia = groupOrder.indexOf(a), ib = groupOrder.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  const html = allGroups.map((group) => {
    const node = tree[group];
    if (!node) return '';
    const collapsed = state.collapsedGroups.has(group);
    const childrenHtml = collapsed ? '' : renderTreeNode(node, 1);
    const fullyChecked = isCategoryFullySelected(group);
    const partialChecked = isCategoryPartiallySelected(group);
    const checkedClass = fullyChecked ? 'checked' : (partialChecked ? 'partial' : '');
    return `<div class="tree-group" data-group="${esc(group)}">
      <div class="tree-group-head" data-toggle="${esc(group)}">
        <span class="group-arrow">${collapsed ? '▸' : '▾'}</span>
        <span class="tree-checkbox ${checkedClass}" data-category="${esc(group)}"></span>
        <span class="group-label">${esc(group)}</span>
        <span class="group-count">${groupTotals[group]}</span>
      </div>
      <div class="tree-children">${childrenHtml}</div>
    </div>`;
  }).join('');
  $('categoryTree').innerHTML = html;
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
  const url = item.url;
  const titleHtml = url
    ? `<a class="record-title-link" href="${esc(url)}" target="_blank" rel="noreferrer">${esc(item.title || '未命名情报')}</a>`
    : `<span class="record-title-link">${esc(item.title || '未命名情报')}</span>`;
  const summary = item.ai_summary || item.body || '';
  const isUnrelated = (item.category || '') === '不相关';
  const typeTagLabel = `${typeLabel}${item.tag ? '-' + item.tag : ''}`;
  const catStr = item.category || '未分类';
  const typeBadge = isUnrelated ? '' : `<span class="type-tag ${typeClass}">${esc(typeTagLabel)}</span>`;
  const topicHtml = item.topic
    ? `<div class="record-topic">${esc(item.topic)} <span class="topic-badges"><span class="type-tag cat-tag">${esc(catStr)}</span> ${typeBadge}</span></div>`
    : `<div class="record-topic"><span class="topic-badges"><span class="type-tag cat-tag">${esc(catStr)}</span> ${typeBadge}</span></div>`;
  const lv = item.level || 0;

  // AI score box (score + AI精选 in one frame, before level badges)
  let scoreBox = '';
  if (!isUnrelated && item.score > 0) {
    const s = item.score;
    const scoreCls = s >= 7.0 ? 'hi' : s >= 5.0 ? 'mid' : 'lo';
    const aiLabel = item.ai_picked ? '<span class="score-box-ai">AI精选</span>' : '';
    const dims = item.score_dims;
    // Build tooltip data as data-attributes (no nested HTML)
    const tipData = dims ? `data-tip-score="${s.toFixed(1)}" data-tip-b="${(dims.b||0).toFixed(1)}" data-tip-i="${(dims.i||0).toFixed(1)}" data-tip-r="${(dims.r||0).toFixed(1)}" data-tip-d="${(dims.d||0).toFixed(1)}" data-tip-t="${(dims.t||0).toFixed(1)}"` : '';
    scoreBox = `<span class="score-box ${scoreCls}" tabindex="0" ${tipData}>${aiLabel}<span class="score-box-num">${s.toFixed(1)}</span></span>`;
  }

  const levelBadges = scoreBox + [
    lv >= 3 ? '<span class="level-badge alert">预警</span>' : '',
    lv >= 2 ? '<span class="level-badge key">重点</span>' : '',
    lv >= 1 ? '<span class="level-badge curated">精选</span>' : '',
  ].join('');

  // Meta line
  const metaParts = [];
  if (item.date) metaParts.push(`<span class="meta-item">📅&nbsp;${esc(item.date)}</span>`);
  if (item.authors && item.authors !== HIDDEN_AUTHORS) metaParts.push(`<span class="meta-item">✍️&nbsp;${esc(item.authors)}</span>`);
  const metaLine = metaParts.length ? `<div class="record-meta-info">${metaParts.join('<span class="meta-sep"></span>')}</div>` : '';

  let paramsHtml = '';
  if (Array.isArray(item.key_params) && item.key_params.length) {
    paramsHtml = '<div class="key-params">' +
      item.key_params.map(p => `<span class="param-chip">${esc(p)}</span>`).join('') +
      '</div>';
  }

  // Body: AI summary + expand full text + comment + alert reason
  let bodyHtml = '';
  const cardId = 'c' + Math.random().toString(36).slice(2, 9);
  const aiSummary = item.ai_summary || '';
  const fullText = item.full_body || item.body || '';
  const hasFullBody = (fullText && fullText.trim().length > 0) || item.has_body === 1;

  // Show body block for: curated records, records with comment/alert, records with body,
  // records with AI summary, OR any record that is not "不相关" (must show AI summary line)
  const isNotUnrelated = (item.category || '') !== '不相关' && (item.category || '') !== '未分类' && item.category;
  const hasNoBody = !hasFullBody && !item.has_body;
  if (lv > 0 || item.comment || item.alert_reason || hasFullBody || isNotUnrelated) {
    bodyHtml = `<div class="record-body-wrap">`;
    if (isUnrelated) {
      // Unrelated records: no AI summary text, just show [展开全文] button
      if (hasFullBody) {
        bodyHtml += `<p class="record-ai-summary"><span class="expand-btn" data-target="${cardId}-full" data-idx="${item._idx ?? -1}" data-action="expand">[展开全文]</span></p>`;
      }
    } else if (aiSummary && aiSummary.trim()) {
      bodyHtml += `<p class="record-ai-summary"><span class="summary-label">AI摘要</span>${esc(aiSummary)}`;
      if (hasFullBody) {
        bodyHtml += ` <span class="expand-btn" data-target="${cardId}-full" data-idx="${item._idx ?? -1}" data-action="expand">[展开全文]</span>`;
      }
      bodyHtml += `</p>`;
    } else if (hasFullBody) {
      // Has body but no AI summary yet (Tier 2 not loaded) — show [展开全文] only
      bodyHtml += `<p class="record-ai-summary"><span class="expand-btn" data-target="${cardId}-full" data-idx="${item._idx ?? -1}" data-action="expand">[展开全文]</span></p>`;
    } else if (hasNoBody) {
      // Empty body: show [展开全文] button, no AI summary text at all
      bodyHtml += `<p class="record-ai-summary"><span class="expand-btn" data-target="${cardId}-full" data-idx="${item._idx ?? -1}" data-action="expand">[展开全文]</span></p>`;
    }
    // Full text (hidden initially; may be empty if body not yet loaded)
    if (hasFullBody) {
      bodyHtml += `<div class="record-full-text" id="${cardId}-full" style="display:none">`;
      bodyHtml += `<div class="full-text-inner">${fullText ? esc(fullText) : '<span class="body-loading">载入中…</span>'}</div>`;
      bodyHtml += `</div>`;
    } else {
      // Empty body: placeholder for [展开全文] to show "无正文内容"
      bodyHtml += `<div class="record-full-text" id="${cardId}-full" style="display:none">`;
      bodyHtml += `<div class="full-text-inner"><span class="body-empty">无正文内容</span></div>`;
      bodyHtml += `</div>`;
    }
    // Comment (full, no truncation)
    if (item.comment) {
      bodyHtml += `<div class="record-comment-full-block"><span class="comment-label">Comment</span><p class="record-comment-text">${esc(item.comment)}</p></div>`;
    }
    // Alert reason (独立模块, Comment下方, 红色系)
    if (item.alert_reason) {
      const wrParts = item.alert_reason.split('||');
      const wrLabel = wrParts.length === 2 ? `预警原因：${wrParts[0]}` : '预警原因：';
      const wrText = wrParts.length === 2 ? wrParts[1] : item.alert_reason;
      bodyHtml += `<div class="alert-reason-block"><span class="alert-reason-label">${esc(wrLabel)}</span><span class="alert-reason-text">${esc(wrText)}</span></div>`;
    }
    bodyHtml += `</div>`;
  } else {
    if (isUnrelated) {
      // Unrelated records without body: show nothing
      bodyHtml = '';
    } else if (aiSummary && aiSummary.trim()) {
      bodyHtml = `<p class="record-summary"><span class="summary-label">AI摘要</span>${esc(aiSummary)}</p>`;
    } else {
      // No body, no summary, not unrelated — show nothing
      bodyHtml = '';
    }
  }

  // Cluster indicator (rendered next to title)
  let clusterHtml = '';
  if (item.cluster_id && item.cluster_parent === 0) {
    const childCount = state.clusterChildCounts ? (state.clusterChildCounts.get(item.cluster_id) || 0) : 0;
    if (childCount > 0) {
      clusterHtml = `<span class="cluster-badge" data-cluster="${esc(item.cluster_id)}" title="${esc(item.cluster_name || '')}">展开事件聚类 · ${childCount}</span>`;
    }
  }

  return `<article class="record-card">
    <div class="record-title">${levelBadges}${titleHtml}${clusterHtml}</div>
    ${topicHtml}
    ${metaLine}
    ${bodyHtml}
    ${paramsHtml}
  </article>`;
}

function clearFilter(key) {
  if (key === 'query') { state.query = ''; $('searchInput').value = ''; }
  if (key === 'date') { state.dateFrom = ''; state.dateTo = ''; $('dateFrom').value = ''; $('dateTo').value = ''; }
  if (key === 'category') selectAllCategories();
  if (key === 'type') { state.selectedTypes = { news: true, literature: true }; state.selectedTags = { news: new Set(), literature: new Set() }; }
  if (key === 'alert') { return; } // Cannot deselect alert level
  applyFilters();
}

function setQuickDate(type) {
  const today = new Date();
  const fmt = (d) => d.toISOString().slice(0, 10);
  if (type === 'today') { state.dateFrom = fmt(today); state.dateTo = fmt(today); }
  else if (type === '7d') {
    const past = new Date(today); past.setDate(past.getDate() - 6);
    state.dateFrom = fmt(past); state.dateTo = fmt(today);
  } else if (type === '30d') {
    const past = new Date(today); past.setDate(past.getDate() - 29);
    state.dateFrom = fmt(past); state.dateTo = fmt(today);
  } else { state.dateFrom = ''; state.dateTo = ''; }
  $('dateFrom').value = state.dateFrom;
  $('dateTo').value = state.dateTo;
  applyFilters();
}

  // Detail modal code removed

function bindEvents() {
  // Expand/collapse full text
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('expand-btn')) {
      const targetId = e.target.dataset.target;
      const action = e.target.dataset.action;
      const el = document.getElementById(targetId);
      if (!el) return;
      if (action === 'expand') {
        // On-demand body loading: fetch the shard containing this record's body
        const idx = parseInt(e.target.dataset.idx, 10);
        const inner = el.querySelector('.full-text-inner');
        if (inner && (!inner.textContent.trim() || inner.querySelector('.body-loading'))) {
          const shardIdx = Math.floor(idx / 2000); // CHUNK_SIZE
          const localIdx = idx % 2000;
          const dv = (state.manifest?.meta?.data_version) || '';
          // Check if already loaded
          if (window.__BODY_SHARDS__[shardIdx]) {
            const bodyText = window.__BODY_SHARDS__[shardIdx][localIdx]?.b || '';
            if (bodyText) inner.textContent = bodyText;
          } else {
            // Show loading state and fetch on demand
            inner.innerHTML = '<span class="body-loading">正在加载全文…</span>';
            window.fetchBodyShard(shardIdx, dv).then(records => {
              if (records && records[localIdx]) {
                const bodyText = records[localIdx].b || '';
                if (bodyText) inner.textContent = bodyText;
                else inner.innerHTML = '<span class="body-loading">暂无全文</span>';
              } else {
                inner.innerHTML = '<span class="body-loading">加载失败</span>';
              }
            });
          }
        }
        el.style.display = '';
        e.target.dataset.action = 'collapse';
        e.target.textContent = '[收起全文]';
      } else {
        el.style.display = 'none';
        e.target.dataset.action = 'expand';
        e.target.textContent = '[展开全文]';
      }    }
  });
  // Close popovers when clicking outside
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.type-inline-item')) {
      document.querySelectorAll('.type-popover.show').forEach(p => p.classList.remove('show'));
    }
  });

  // Safe event binding helper
  const on = (id, evt, fn) => { const el = $(id); if (el) el.addEventListener(evt, fn); };

  on('themeToggle', 'click', () => {
    const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
    else document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('techdb-theme', next);
  });
  let searchTimer = null;
  on('searchInput', 'input', (e) => { 
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.query = e.target.value; applyFilters(); }, 200);
  });
  let catSearchTimer = null;
  on('categorySearch', 'input', (e) => { 
    state.categoryQuery = e.target.value;
    clearTimeout(catSearchTimer);
    catSearchTimer = setTimeout(() => { applyFilters(); }, 200);
  });
  on('sortToggle', 'click', () => {
    state.sortBy = state.sortBy === 'date' ? 'score' : 'date';
    const w1 = $('sortWord1'), w2 = $('sortWord2');
    if (w1 && w2) {
      if (state.sortBy === 'date') {
        w1.classList.add('active'); w2.classList.remove('active');
      } else {
        w1.classList.remove('active'); w2.classList.add('active');
      }
    }
    applyFilters();
  });
  on('clearDateBtn', 'click', () => clearFilter('date'));
  on('dateFrom', 'change', (e) => { state.dateFrom = e.target.value; applyFilters(); });
  on('dateTo', 'change', (e) => { state.dateTo = e.target.value; applyFilters(); });

  // ── View tabs (intelligence ↔ reports) ──
  document.querySelectorAll('.view-tab').forEach((tab) => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });
  // ── Report type buttons ──
  document.querySelectorAll('.report-type-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const type = btn.dataset.reportType;
      state.reportType = type;
      document.querySelectorAll('.report-type-btn').forEach((b) => {
        b.classList.toggle('active', b.dataset.reportType === type);
      });
      state.reportDate = '';
      renderReportView();
    });
  });
  // ── Report list item click (delegated, handles weekly expand/collapse) ──
  const reportListEl = $('reportList');
  if (reportListEl) {
    reportListEl.addEventListener('click', (e) => {
      // Handle month group expand/collapse for weekly
      const monthToggle = e.target.closest('[data-toggle-month]');
      if (monthToggle) {
        const month = monthToggle.dataset.toggleMonth;
        const items = monthToggle.nextElementSibling;
        if (items) {
          const isHidden = items.style.display === 'none';
          items.style.display = isHidden ? '' : 'none';
          monthToggle.textContent = monthToggle.textContent.replace(isHidden ? '▸' : '▾', isHidden ? '▾' : '▸');
        }
        return;
      }
      // Handle report selection
      const item = e.target.closest('.report-list-item');
      if (!item) return;
      state.reportDate = item.dataset.reportDate;
      document.querySelectorAll('.report-list-item').forEach((it) => {
        it.classList.toggle('active', it.dataset.reportDate === state.reportDate);
      });
      loadReport(state.reportType, state.reportDate);
    });
  }
  document.querySelectorAll('.date-quick button').forEach((btn) => {
    btn.addEventListener('click', () => setQuickDate(btn.dataset.quick));
  });
  on('prevPage', 'click', () => { if (state.page > 1) { state.page -= 1; renderRecords(); document.querySelector('.content').scrollTo({ top: 0, behavior: 'instant' }); } });
  on('nextPage', 'click', () => { const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE)); if (state.page < pages) { state.page += 1; renderRecords(); document.querySelector('.content').scrollTo({ top: 0, behavior: 'instant' }); } });

  // ── Global score tooltip (single floating div, not a child of score-box) ──
  const scoreTooltip = document.createElement('div');
  scoreTooltip.className = 'score-float-tip';
  scoreTooltip.style.display = 'none';
  document.body.appendChild(scoreTooltip);

  document.addEventListener('mouseover', (e) => {
    const sb = e.target.closest('.score-box[data-tip-score]');
    if (!sb) { return; }
    const b = sb.dataset.tipB, i = sb.dataset.tipI, r = sb.dataset.tipR, d = sb.dataset.tipD, t = sb.dataset.tipT;
    const score = sb.dataset.tipScore;
    scoreTooltip.innerHTML =
      `<div class="sft-total">质量分 ${score}</div>` +
      `<div class="sft-row"><span>突破性</span><span>${b}</span></div>` +
      `<div class="sft-row"><span>产业力</span><span>${i}</span></div>` +
      `<div class="sft-row"><span>稀缺性</span><span>${r}</span></div>` +
      `<div class="sft-row"><span>数据量</span><span>${d}</span></div>` +
      `<div class="sft-row"><span>时效性</span><span>${t}</span></div>` +
      `<div class="sft-note">总分=加权分+ATSC偏好附加分</div>`;
    const rect = sb.getBoundingClientRect();
    scoreTooltip.style.display = 'block';
    // Position: below the score-box, left-aligned
    let left = rect.left;
    let top = rect.bottom + 6;
    // Keep within viewport
    const tipW = scoreTooltip.offsetWidth;
    if (left + tipW > window.innerWidth - 8) left = window.innerWidth - tipW - 8;
    scoreTooltip.style.left = left + 'px';
    scoreTooltip.style.top = top + 'px';
  });
  document.addEventListener('mouseout', (e) => {
    const sb = e.target.closest('.score-box[data-tip-score]');
    if (sb) scoreTooltip.style.display = 'none';
  });

  document.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-clear]');
    if (chip) { clearFilter(chip.dataset.clear); return; }

    // Alert level click — cannot deselect AI精选 (stays as minimum)
    const alRow = e.target.closest('[data-alert-level]');
    if (alRow) {
      const lvl = alRow.dataset.alertLevel;
      // 点击当前已选中的等级不会取消，而是保持选中
      state.alertLevel = lvl;
      applyFilters();
      return;
    }

    // Cluster toggle - directly manipulate DOM without full re-render
    const clusterTog = e.target.closest('[data-cluster]');
    if (clusterTog) {
      e.preventDefault();
      e.stopPropagation();
      const cid = clusterTog.dataset.cluster;
      const parentCard = clusterTog.closest('.record-card');
      
      if (state.expandedClusters.has(cid)) {
        // Collapse: remove child cards after parent
        state.expandedClusters.delete(cid);
        let next = parentCard.nextElementSibling;
        while (next && next.classList.contains('cluster-child') && next.dataset.clusterChild === cid) {
          const toRemove = next;
          next = next.nextElementSibling;
          toRemove.remove();
        }
        // Update badge text
        const childCount = state.clusterChildCounts ? (state.clusterChildCounts.get(cid) || 0) : 0;
        clusterTog.textContent = `展开事件聚类 · ${childCount}`;
        clusterTog.classList.remove('expanded');
      } else {
        // Expand: find child records and insert compact cards after parent
        state.expandedClusters.add(cid);
        const children = state.records.filter(r => r.cluster_id === cid && r.cluster_parent === 1);
        const frag = document.createDocumentFragment();
        for (const child of children) {
          const div = document.createElement('article');
          div.className = 'record-card cluster-child';
          div.dataset.clusterChild = cid;
          const metaParts = [];
          if (child.date) metaParts.push(`📅&nbsp;${esc(child.date)}`);
          if (child.authors && child.authors !== HIDDEN_AUTHORS) metaParts.push(`✍️&nbsp;${esc(child.authors)}`);
          const metaLine = metaParts.length ? `<div class="record-meta-info">${metaParts.join('<span class="meta-sep"></span>')}</div>` : '';
          div.innerHTML = `<div class="record-title"><a class="record-title-link" href="${esc(child.url || '#')}" target="_blank" rel="noreferrer">${esc(child.title || '未命名情报')}</a></div>${metaLine}`;
          frag.appendChild(div);
        }
        parentCard.after(frag);
        // Update badge text
        clusterTog.textContent = `收起事件聚类 · ${children.length}`;
        clusterTog.classList.add('expanded');
      }
      return;
    }

    // Click checkbox → toggle category selection
    const checkbox = e.target.closest('.tree-checkbox[data-category]');
    if (checkbox) {
      const cat = checkbox.dataset.category;
      // If fully selected or partially selected → deselect; if unselected → select
      if (isCategoryFullySelected(cat) || isCategoryPartiallySelected(cat)) {
        deselectCategory(cat);
      } else {
        selectCategory(cat);
      }
      renderCategoryTree();
      applyFilters();
      return;
    }

    // Click tree arrow → toggle expand/collapse
    const arrow = e.target.closest('.tree-arrow, .group-arrow');
    if (arrow) {
      const key = arrow.dataset.toggle || arrow.closest('.tree-group-head')?.dataset.toggle;
      if (key) {
        if (state.collapsedGroups.has(key)) state.collapsedGroups.delete(key);
        else state.collapsedGroups.add(key);
        renderCategoryTree();
      }
      return;
    }

    // Click tree label → toggle selection (same as checkbox)
    const label = e.target.closest('.tree-label[data-category]');
    if (label) {
      const cat = label.dataset.category;
      if (isCategoryFullySelected(cat) || isCategoryPartiallySelected(cat)) {
        deselectCategory(cat);
      } else {
        selectCategory(cat);
      }
      renderCategoryTree();
      applyFilters();
      return;
    }

    // Click group label → toggle expand
    const groupLabel = e.target.closest('.group-label');
    if (groupLabel) {
      const head = groupLabel.closest('.tree-group-head');
      if (head) {
        const key = head.dataset.toggle;
        if (state.collapsedGroups.has(key)) state.collapsedGroups.delete(key);
        else state.collapsedGroups.add(key);
        renderCategoryTree();
      }
      return;
    }

    // Click select-all / deselect-all
    const selAll = e.target.closest('[data-cat-action="all"]');
    if (selAll) { selectAllCategories(); renderCategoryTree(); applyFilters(); return; }
    const selNone = e.target.closest('[data-cat-action="none"]');
    if (selNone) { deselectAllCategories(); renderCategoryTree(); applyFilters(); return; }

    // Type checkbox toggle (news/literature)
    const typeCheckbox = e.target.closest('[data-type-toggle]');
    if (typeCheckbox) {
      const t = typeCheckbox.dataset.typeToggle;
      state.selectedTypes[t] = !state.selectedTypes[t];
      applyFilters();
      return;
    }

    // Type popover arrow toggle
    const typeArrow = e.target.closest('[data-type-popover]');
    if (typeArrow) {
      e.stopPropagation();
      const popId = 'popover-' + typeArrow.dataset.typePopover;
      document.querySelectorAll('.type-popover').forEach(p => { if (p.id !== popId) p.classList.remove('show'); });
      $(popId).classList.toggle('show');
      return;
    }

    // Type popover item checkbox
    const popItem = e.target.closest('.type-popover-item');
    if (popItem) {
      const t = popItem.dataset.type;
      const tag = popItem.dataset.tag;
      const tags = state.selectedTags[t];
      if (tags.has(tag)) { tags.delete(tag); }
      else { tags.add(tag); }
      applyFilters();
      return;
    }
  });

  // Source panel
  if (window.__SOURCES__) {
    renderSourcePanel();
    $('sourceTrigger').addEventListener('click', () => {
      $('sourcePanel').classList.add('open');
      $('sourceOverlay').classList.add('show');
    });
    $('sourceClose').addEventListener('click', closeSourcePanel);
    $('sourceOverlay').addEventListener('click', closeSourcePanel);
  }
}

function closeSourcePanel() {
  $('sourcePanel').classList.remove('open');
  $('sourceOverlay').classList.remove('show');
}

function renderSourcePanel() {
  const data = window.__SOURCES__;
  if (!data) return;
  $('sourceSummary').innerHTML = `本数据库共捕获 <strong>${data.total}</strong> 个信息来源`;
  const html = data.categories.map(cat => {
    const groupsHtml = cat.groups.map(g => {
      const items = g.items.map(s => `<span class="source-item">${esc(s)}</span>`).join('');
      return `<div class="source-group">
        <div class="source-group-head"><span>${esc(g.name)}</span><span class="source-group-count">${g.count}</span></div>
        <div class="source-items">${items}</div>
      </div>`;
    }).join('');
    return `<div class="source-category">
      <div class="source-cat-head"><span class="source-cat-icon">${cat.icon}</span><span class="source-cat-name">${esc(cat.name)}</span><span class="source-cat-count">${cat.count}个来源</span></div>
      ${groupsHtml}
    </div>`;
  }).join('');
  $('sourceBody').innerHTML = html;
}

load().catch((error) => {
  document.body.innerHTML = `<main class="content"><div class="empty-hint"><strong>页面载入失败</strong>${esc(error.message)}</div></main>`;
});

// ═══════════════════════════════════════════════════════════════
// ── Conference Calendar View ──
// ═══════════════════════════════════════════════════════════════

state.calendarData = null;
state.calendarYear = new Date().getFullYear();
state.calendarMonth = new Date().getMonth();
state.calendarCategories = new Set(['零碳产业', 'AI与智能科技', '通用技术']);

async function loadCalendarData() {
  if (state.calendarData) return state.calendarData;
  try {
    const resp = await fetch('data/processed/conferences.json?v=' + (window.__DATA_VERSION__ || '1'));
    if (!resp.ok) return [];
    state.calendarData = await resp.json();
    return state.calendarData;
  } catch(e) {
    return [];
  }
}

function renderCalendarView() {
  const container = $('calendarContent');
  if (!container) return;

  const yearSel = $('calYear');
  const monthPicker = $('calMonthPicker');
  const catList = $('calCatList');

  // Attach sidebar event listeners (once)
  if (!container._calInit) {
    container._calInit = true;

    if (yearSel) yearSel.addEventListener('change', () => {
      state.calendarYear = parseInt(yearSel.value);
      calRender();
    });
    if (monthPicker) monthPicker.addEventListener('click', (e) => {
      const btn = e.target.closest('.cal-month-btn');
      if (!btn) return;
      monthPicker.querySelectorAll('.cal-month-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.calendarMonth = parseInt(btn.dataset.month);
      calRender();
    });
    if (catList) catList.addEventListener('change', (e) => {
      if (e.target.type === 'checkbox') {
        const cat = e.target.value;
        if (e.target.checked) state.calendarCategories.add(cat);
        else state.calendarCategories.delete(cat);
        calRender();
      }
    });
    // Sync month button highlight with actual state.calendarMonth
    if (monthPicker) {
      monthPicker.querySelectorAll('.cal-month-btn').forEach(b => {
        b.classList.toggle('active', parseInt(b.dataset.month, 10) === state.calendarMonth);
      });
    }
    // Sync year selector with actual state.calendarYear
    if (yearSel) yearSel.value = String(state.calendarYear);
  }

  container.innerHTML = `<div class="calendar-main" id="calMain"><p class="empty-hint">正在载入会议数据…</p></div>`;
  loadCalendarData().then(() => {
    // Populate year selector dynamically based on conference data
    const confData = state.calendarData || [];
    const confYears = [...new Set(confData.map(c => {
      if (!c.start_date) return null;
      const d = new Date(c.start_date);
      return isNaN(d.getTime()) ? null : d.getFullYear();
    }).filter(y => y !== null))].sort((a, b) => a - b);
    if (yearSel) {
      const currentVal = state.calendarYear;
      // Compare by option values (robust against browser innerHTML normalization)
      const existingValues = Array.from(yearSel.options).map(o => o.value);
      const targetValues = confYears.length > 0 ? confYears.map(String) : [String(currentVal)];
      const needsRebuild = existingValues.length !== targetValues.length ||
        targetValues.some((v, i) => existingValues[i] !== v);
      if (needsRebuild) {
        yearSel.innerHTML = targetValues.map(v => `<option value="${v}">${v}年</option>`).join('');
      }
      // If current year not in data, default to the most recent available year
      if (confYears.length > 0 && !confYears.includes(currentVal)) {
        state.calendarYear = confYears[confYears.length - 1]; // most recent
      }
      yearSel.value = String(state.calendarYear);
    }
    calRender();
  });
}

function calGetFiltered() {
  const data = state.calendarData || [];
  return data.filter(c => {
    if (!state.calendarCategories.has(c.category || '通用技术')) return false;
    if (!c.start_date) return false;
    const d = new Date(c.start_date);
    if (d.getFullYear() !== state.calendarYear) return false;
    if (state.calendarMonth >= 0 && d.getMonth() !== state.calendarMonth) return false;
    return true;
  });
}

function calRender() {
  const main = $('calMain');
  if (!main) return;
  const data = calGetFiltered();
  if (data.length === 0) { main.innerHTML = `<p class="empty-hint">当前筛选条件下暂无会议</p>`; return; }

  const byDate = {};
  data.forEach(c => {
    const dateKey = c.start_date;
    if (!byDate[dateKey]) byDate[dateKey] = [];
    byDate[dateKey].push(c);
  });

  main.innerHTML = state.calendarMonth >= 0 ? calMonthGrid(byDate) : calYearList(byDate);
  document.querySelectorAll('.cal-event').forEach(el => {
    el.addEventListener('click', () => calShowPopup(el.dataset.name));
  });
}

function calMonthGrid(byDate) {
  const year = state.calendarYear;
  const month = state.calendarMonth;
  const months = ['一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月'];
  const firstDay = new Date(year, month, 1);
  const lastDay = new Date(year, month + 1, 0);
  const startWeekday = (firstDay.getDay() + 6) % 7;
  const daysInMonth = lastDay.getDate();
  const weekdays = ['一','二','三','四','五','六','日'];

  let html = `<div class="cal-month-header">${year}年 ${months[month]}</div><div class="cal-grid">`;
  weekdays.forEach(w => { html += `<div class="cal-weekday">${w}</div>`; });
  for (let i = 0; i < startWeekday; i++) html += `<div class="cal-day cal-day-empty"></div>`;

  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const events = byDate[dateStr] || [];
    const isToday = (year === new Date().getFullYear() && month === new Date().getMonth() && d === new Date().getDate());
    html += `<div class="cal-day${isToday ? ' cal-day-today' : ''}"><span class="cal-day-num">${d}</span>`;
    events.forEach(e => {
      const catClass = (e.category || '').startsWith('零碳') ? 'cat-zero' : (e.category || '').startsWith('AI') ? 'cat-ai' : 'cat-gen';
      html += `<div class="cal-event ${catClass}" data-name="${esc(e.name)}" title="${esc(e.name)}">${esc(e.name.substring(0, 12))}${e.name.length > 12 ? '…' : ''}</div>`;
    });
    html += `</div>`;
  }
  html += `</div>`;
  return html;
}

function calYearList(byDate) {
  const months = ['一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月'];
  let html = `<div class="cal-month-header">${state.calendarYear}年 会议列表</div>`;
  let hasAny = false;
  for (let m = 0; m < 12; m++) {
    const monthEvents = Object.entries(byDate).filter(([date]) => parseInt(date.split('-')[1]) === m + 1).sort((a, b) => a[0].localeCompare(b[0]));
    if (monthEvents.length === 0) continue;
    hasAny = true;
    html += `<div class="cal-month-section"><h3>${months[m]}</h3>`;
    monthEvents.forEach(([date, events]) => {
      html += `<div class="cal-list-date">${date}</div>`;
      events.forEach(e => {
        const catClass = (e.category || '').startsWith('零碳') ? 'cat-zero' : (e.category || '').startsWith('AI') ? 'cat-ai' : 'cat-gen';
        html += `<div class="cal-event ${catClass}" data-name="${esc(e.name)}">${esc(e.name)}</div>`;
      });
    });
    html += `</div>`;
  }
  if (!hasAny) html += `<p class="empty-hint">${state.calendarYear}年暂无会议</p>`;
  return html;
}

function calShowPopup(name) {
  const data = state.calendarData || [];
  const conf = data.find(c => c.name === name);
  if (!conf) return;
  const dateRange = conf.end_date ? `${conf.start_date} ~ ${conf.end_date}` : conf.start_date;
  let sourcesHtml = '';
  if (conf.sources && conf.sources.length > 0) {
    sourcesHtml = '<div class="cal-popup-sources"><strong>情报来源：</strong><ul>';
    conf.sources.forEach(s => { sourcesHtml += `<li><a href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.title || s.url)}</a></li>`; });
    sourcesHtml += '</ul></div>';
  }
  const overlay = document.createElement('div');
  overlay.className = 'cal-popup-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `
    <div class="cal-popup">
      <button class="cal-popup-close" onclick="this.parentElement.parentElement.remove()">×</button>
      <h3 class="cal-popup-title">${esc(conf.name)}</h3>
      <div class="cal-popup-row"><strong>时间：</strong> ${esc(dateRange)}</div>
      ${conf.location ? `<div class="cal-popup-row"><strong>地点：</strong> ${esc(conf.location)}</div>` : ''}
      ${conf.organizer ? `<div class="cal-popup-row"><strong>主办单位：</strong> ${esc(conf.organizer)}</div>` : ''}
      ${conf.category ? `<div class="cal-popup-row"><strong>领域：</strong> ${esc(conf.category)}</div>` : ''}
      ${sourcesHtml}
    </div>`;
  document.body.appendChild(overlay);
}
