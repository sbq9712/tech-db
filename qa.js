/**
 * Tech-DB Q&A Module
 * Handles chat interface, streaming responses, knowledge graph visualization,
 * and multi-conversation management.
 */

// ── Config ──
const QA_API_BASE = 'https://providers-armor-kruger-literary.trycloudflare.com';

// ── State ──
const qaState = {
  conversations: [],       // [{id, title, messages: []}]
  activeConversationId: null,
  isStreaming: false,
  abortController: null,   // For aborting SSE stream
  abortController: null,
};

// ── DOM helpers ──
function qa$(id) { return document.getElementById(id); }

// ── Initialize Q&A view ──
function initQAView() {
  // New chat button
  qa$('qaNewChatBtn').addEventListener('click', () => createNewConversation());

  // Send button
  qa$('qaSendBtn').addEventListener('click', () => {
    if (qaState.isStreaming && qaState.abortController) {
      qaState.abortController.abort();
      return;
    }
    sendQuestion();
  });

  // Input: Enter to send, Shift+Enter for newline
  const input = qa$('qaInput');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!qaState.isStreaming) sendQuestion();
    }
  });

  // Auto-resize textarea
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });

  // Example question clicks
  document.querySelectorAll('.qa-example-item').forEach(el => {
    el.addEventListener('click', () => {
      const q = el.dataset.q;
      if (q) {
        qa$('qaInput').value = q;
        sendQuestion();
      }
    });
  });

  // Load saved conversations from localStorage
  loadConversationsFromStorage();

  // Check if graph data is available and render
  loadAndRenderGraph();
  loadStats();
}

// ── Conversation management ──
function createNewConversation() {
  const conv = {
    id: 'conv_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
    title: '新对话',
    messages: [],
  };
  qaState.conversations.unshift(conv);
  qaState.activeConversationId = conv.id;
  renderConversationList();
  renderMessages();
  qa$('qaInput').focus();
}

function switchConversation(id) {
  qaState.activeConversationId = id;
  renderConversationList();
  renderMessages();
}

function deleteConversation(id) {
  qaState.conversations = qaState.conversations.filter(c => c.id !== id);
  if (qaState.activeConversationId === id) {
    qaState.activeConversationId = qaState.conversations[0]?.id || null;
  }
  renderConversationList();
  renderMessages();
  saveConversationsToStorage();
}

function getActiveConversation() {
  return qaState.conversations.find(c => c.id === qaState.activeConversationId);
}

function renderConversationList() {
  const list = qa$('qaConversationList');
  if (qaState.conversations.length === 0) {
    list.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-quaternary)">点击上方按钮开始新对话</div>';
    return;
  }
  list.innerHTML = qaState.conversations.map(c => `
    <div class="qa-conversation-item ${c.id === qaState.activeConversationId ? 'active' : ''}" data-conv-id="${c.id}">
      <span>${escHtml(c.title)}</span>
      <button class="qa-conv-delete" data-conv-id="${c.id}" title="删除">✕</button>
    </div>
  `).join('');

  list.querySelectorAll('.qa-conversation-item').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.classList.contains('qa-conv-delete')) {
        e.stopPropagation();
        deleteConversation(e.target.dataset.convId);
      } else {
        switchConversation(el.dataset.convId);
      }
    });
  });
}

function renderMessages() {
  const conv = getActiveConversation();
  const emptyState = qa$('qaEmptyState');
  const messagesEl = qa$('qaMessages');

  if (!conv || conv.messages.length === 0) {
    emptyState.style.display = 'flex';
    messagesEl.style.display = 'none';
    return;
  }

  emptyState.style.display = 'none';
  messagesEl.style.display = 'block';

  messagesEl.innerHTML = conv.messages.map((msg, idx) => {
    if (msg.role === 'user') {
      return `
        <div class="qa-message user">
          <div class="qa-message-avatar">👤</div>
          <div class="qa-message-content">
            <div class="qa-message-bubble">${escHtml(msg.content)}</div>
          </div>
        </div>
      `;
    } else {
      return renderAssistantMessage(msg, idx);
    }
  }).join('');

  // Scroll to bottom
  messagesEl.scrollTop = messagesEl.scrollHeight;

  // Attach action button handlers
  messagesEl.querySelectorAll('.qa-action-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const action = btn.dataset.action;
      const msgIdx = parseInt(btn.dataset.msgIdx);
      handleAction(action, msgIdx);
    });
  });

  // Attach citation ref click handlers
  messagesEl.querySelectorAll('.qa-citation-ref').forEach(ref => {
    ref.addEventListener('click', (e) => {
      e.preventDefault();
      const citationNum = parseInt(ref.dataset.citation);
      // Scroll to the specific citation item
      const citationItem = messagesEl.querySelector(`.qa-citation-item[data-citation-num="${citationNum}"]`);
      if (citationItem) {
        citationItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        citationItem.style.transition = 'background 0.3s';
        citationItem.style.background = 'var(--brand-bg)';
        setTimeout(() => { citationItem.style.background = ''; }, 1500);
      } else {
        const citationsBlock = messagesEl.querySelector('.qa-citations-block');
        if (citationsBlock) {
          citationsBlock.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      }
    });
  });
}

function renderAssistantMessage(msg, idx) {
  const content = msg.content || '';
  const citations = msg.citations || [];
  const isStreaming = msg.streaming || false;

  // Render content with citation refs [1] -> clickable
  let renderedContent = renderMarkdown(content);
  // Replace [1], [2], etc. with clickable citation refs
  renderedContent = renderedContent.replace(/\[(\d+)\]/g, (match, num) => {
    const citationNum = parseInt(num);
    if (citations.find(c => c.id === citationNum)) {
      return `<span class="qa-citation-ref" data-citation="${citationNum}">[${num}]</span>`;
    }
    return match;
  });

  const typingCursor = isStreaming ? '<span class="qa-typing-cursor"></span>' : '';

  let html = `
    <div class="qa-message assistant">
      <div class="qa-message-avatar">🤖</div>
      <div class="qa-message-content">
        <div class="qa-message-bubble">${renderedContent}${typingCursor}</div>
  `;

  // Citations
  if (citations.length > 0 && !isStreaming) {
    html += `
      <div class="qa-citations-block">
        <div class="qa-citations-title">📎 来源引用（${citations.length}条）</div>
        ${citations.map(c => `
          <div class="qa-citation-item" data-citation-num="${c.id}" data-record-id="${c.record_id}">
            <div class="qa-citation-header">
              <span class="qa-citation-number">[${c.id}]</span>
              <span class="qa-citation-title">${escHtml(c.title)}</span>
            </div>
            <div class="qa-citation-meta">
              <span>📅 ${c.date || ''}</span>
              <span>📰 ${escHtml(c.source || '')}</span>
              ${c.score ? `<span>⭐ ${c.score}</span>` : ''}
              ${c.tag ? `<span>🏷️ ${escHtml(c.tag)}</span>` : ''}
              <a class="qa-citation-link" href="${escHtml(c.url || '#')}" target="_blank" rel="noreferrer">🔗 原文</a>
            </div>
            ${c.body_snippet ? `<div class="qa-citation-snippet">${escHtml(c.body_snippet)}...</div>` : ''}
          </div>
        `).join('')}
      </div>
    `;
  }

  // Actions (only when not streaming)
  if (!isStreaming && content) {
    html += `
      <div class="qa-actions">
        <button class="qa-action-btn" data-action="copy" data-msg-idx="${idx}">📋 复制</button>
        <button class="qa-action-btn" data-action="regenerate" data-msg-idx="${idx}">🔄 重新生成</button>
        <button class="qa-action-btn" data-action="export" data-msg-idx="${idx}">📤 导出</button>
      </div>
    `;
  }

  html += `
      </div>
    </div>
  `;

  return html;
}

function updateSendButton() {
  const btn = qa$('qaSendBtn');
  if (qaState.isStreaming) {
    btn.disabled = false;
    btn.textContent = '⏹ 停止';
    btn.classList.add('qa-send-btn-stop');
  } else {
    btn.disabled = false;
    btn.textContent = '发送';
    btn.classList.remove('qa-send-btn-stop');
  }
}

// ── Send question and handle streaming response ──
async function sendQuestion() {
  const input = qa$('qaInput');
  const question = input.value.trim();
  if (!question || qaState.isStreaming) return;

  // Create conversation if none exists
  if (!getActiveConversation()) {
    createNewConversation();
  }

  const conv = getActiveConversation();
  
  // Set conversation title from first question
  if (conv.messages.length === 0) {
    conv.title = question.slice(0, 20) + (question.length > 20 ? '...' : '');
    renderConversationList();
  }

  // Add user message
  conv.messages.push({ role: 'user', content: question });

  // Add placeholder assistant message
  const assistantMsg = { role: 'assistant', content: '', citations: [], streaming: true };
  conv.messages.push(assistantMsg);

  input.value = '';
  input.style.height = 'auto';

  renderMessages();
  qaState.isStreaming = true;
  qaState.abortController = new AbortController();
  updateSendButton();

  // Build history for API (exclude current question and empty assistant placeholder)
  const history = conv.messages.slice(0, -2).map(m => ({
    role: m.role,
    content: m.content,
  }));

  try {
    // Show status indicators
    showStatusIndicator('retrieving', '🔍 正在检索相关知识...');

    const response = await fetch(`${QA_API_BASE}/api/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: question,
        conversation_id: conv.id,
        history: history,
      }),
      signal: qaState.abortController.signal,
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // Keep incomplete line in buffer

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const dataStr = line.slice(6).trim();
          if (!dataStr || dataStr === '[DONE]') continue;
          try {
            const data = JSON.parse(dataStr);
            handleSSEData(data, assistantMsg);
          } catch (e) {
            // Ignore parse errors for partial data
          }
        }
      }
    }

    // Finalize
    assistantMsg.streaming = false;
    qaState.isStreaming = false;
    qaState.abortController = null;
    updateSendButton();
    removeStatusIndicator();
    renderMessages();
    saveConversationsToStorage();

  } catch (error) {
    if (error.name === 'AbortError') {
      // User aborted - keep whatever content was streamed
      assistantMsg.content = assistantMsg.content || '（已停止生成）';
    } else {
      assistantMsg.content = '抱歉，连接服务器失败：' + error.message;
    }
    assistantMsg.streaming = false;
    qaState.isStreaming = false;
    qaState.abortController = null;
    updateSendButton();
    removeStatusIndicator();
    renderMessages();
    saveConversationsToStorage();
  }
}

function handleSSEData(data, assistantMsg) {
  if (data.step) {
    // Status update
    showStatusIndicator(data.step, data.message || '');
  } else if (data.text !== undefined) {
    // Streaming token
    assistantMsg.content += data.text;
    removeStatusIndicator();
    updateStreamingMessage(assistantMsg);
  } else if (data.citations) {
    // Citations received (before or during streaming)
    assistantMsg.citations = data.citations;
  }

  // Handle done event (may contain both answer and citations)
  if (data.answer !== undefined) {
    if (data.answer && !assistantMsg.content) {
      assistantMsg.content = data.answer;
    }
    if (data.citations) {
      assistantMsg.citations = data.citations;
    }
  }

  // Handle error
  if (data.message && !data.step) {
    assistantMsg.content = data.message;
  }
}

function showStatusIndicator(step, message) {
  let indicator = document.querySelector('.qa-status-indicator');
  if (!indicator) {
    indicator = document.createElement('div');
    indicator.className = 'qa-status-indicator';
    const messagesEl = qa$('qaMessages');
    messagesEl.appendChild(indicator);
  }
  indicator.innerHTML = `<span class="qa-status-spinner"></span> ${escHtml(message)}`;
  indicator.style.display = 'flex';
  qa$('qaMessages').scrollTop = qa$('qaMessages').scrollHeight;
}

function removeStatusIndicator() {
  const indicator = document.querySelector('.qa-status-indicator');
  if (indicator) indicator.remove();
}

function updateStreamingMessage(assistantMsg) {
  const messagesEl = qa$('qaMessages');
  const lastMsg = messagesEl.querySelector('.qa-message.assistant:last-child .qa-message-bubble');
  if (lastMsg) {
    let html = renderMarkdown(assistantMsg.content);
    // Add typing cursor
    html += '<span class="qa-typing-cursor"></span>';
    lastMsg.innerHTML = html;
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Actions (copy, regenerate, export) ──
function handleAction(action, msgIdx) {
  const conv = getActiveConversation();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg) return;

  if (action === 'copy') {
    navigator.clipboard.writeText(msg.content).then(() => {
      showToast('已复制到剪贴板');
    });
  } else if (action === 'regenerate') {
    // Remove this message and re-ask
    const prevUserMsg = conv.messages[msgIdx - 1];
    if (prevUserMsg && prevUserMsg.role === 'user') {
      conv.messages.splice(msgIdx); // Remove this and subsequent
      renderMessages();
      // Re-send (need to remove the user message too and re-add it)
      const question = prevUserMsg.content;
      conv.messages.splice(msgIdx - 1); // Remove the user message
      qa$('qaInput').value = question;
      sendQuestion();
    }
  } else if (action === 'export') {
    exportMessage(msg, conv);
  }
}

function exportMessage(msg, conv) {
  // Export as Markdown
  let md = `# 技术情报问答\n\n`;
  md += `**对话：** ${conv.title}\n\n`;
  md += `**时间：** ${new Date().toLocaleString('zh-CN')}\n\n---\n\n`;

  // Find the user question
  const msgIdx = conv.messages.indexOf(msg);
  if (msgIdx > 0) {
    md += `## ❓ 问题\n\n${conv.messages[msgIdx - 1].content}\n\n`;
  }
  md += `## 💡 回答\n\n${msg.content}\n\n`;

  if (msg.citations && msg.citations.length > 0) {
    md += `## 📎 来源引用\n\n`;
    msg.citations.forEach(c => {
      md += `[${c.id}] ${c.title} (${c.date}, ${c.source})\n`;
      if (c.body_snippet) md += `> ${c.body_snippet}\n`;
      if (c.url) md += `> 🔗 ${c.url}\n`;
      md += `\n`;
    });
  }

  // Download
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `qa_${Date.now()}.md`;
  a.click();
  URL.revokeObjectURL(url);
  showToast('已导出为 Markdown');
}

function showToast(message) {
  const toast = document.createElement('div');
  toast.style.cssText = `
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: var(--bg-surface); color: var(--text-primary);
    padding: 8px 16px; border-radius: 8px; font-size: 13px;
    border: 1px solid var(--border-subtle); z-index: 9999;
    box-shadow: 0 2px 12px rgba(0,0,0,0.1); transition: opacity 0.3s;
  `;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; }, 2000);
  setTimeout(() => toast.remove(), 2500);
}

// ── Simple markdown renderer (reuses app.js renderMarkdown if available) ──
function renderMarkdown(text) {
  if (typeof window.renderMarkdownSafe === 'function') {
    return window.renderMarkdownSafe(text);
  }
  // Fallback: basic markdown
  let html = escHtml(text);
  // Code blocks
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => `<pre><code>${code}</code></pre>`);
  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Headers
  html = html.replace(/^###\s(.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^##\s(.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^#\s(.+)$/gm, '<h3>$1</h3>');
  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Links [text](url)
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener" style="color:var(--brand-light)">$1</a>');
  // Lists - group consecutive <li> into <ul>
  html = html.replace(/(?:^(\d+)\.\s(.+)$\n?)+/gm, (m) => {
    const items = m.trim().split('\n').map(line => {
      const match = line.match(/^\d+\.\s(.+)$/);
      return match ? `<li>${match[1]}</li>` : '';
    }).filter(Boolean);
    return items.length ? `<ul>${items.join('')}</ul>` : m;
  });
  html = html.replace(/(?:^[-•]\s(.+)$\n?)+/gm, (m) => {
    const items = m.trim().split('\n').map(line => {
      const match = line.match(/^[-•]\s(.+)$/);
      return match ? `<li>${match[1]}</li>` : '';
    }).filter(Boolean);
    return items.length ? `<ul>${items.join('')}</ul>` : m;
  });
  // Paragraphs
  html = html.split('\n\n').map(p => {
    if (p.startsWith('<ul>') || p.startsWith('<pre>') || p.startsWith('<h')) {
      return p;
    }
    return `<p>${p.replace(/\n/g, '<br>')}</p>`;
  }).join('');
  return html;
}

function escHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

// ── Knowledge Graph Visualization ──
async function loadAndRenderGraph() {
  const container = qa$('qaGraphContainer');
  if (!container) return;

  try {
    const resp = await fetch(`${QA_API_BASE}/api/graph?limit=200`);
    const data = await resp.json();

    if (!data.nodes || data.nodes.length === 0) {
      container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-quaternary);font-size:13px">知识图谱正在构建中...</div>';
      return;
    }

    renderGraphCanvas(container, data);
  } catch (error) {
    // If backend is not running, show placeholder
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-quaternary);font-size:13px">知识图谱将在服务启动后显示</div>';
  }
}

function renderGraphCanvas(container, data) {
  // Create canvas
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  container.innerHTML = '';
  container.appendChild(canvas);

  // Tooltip
  const tooltip = document.createElement('div');
  tooltip.className = 'qa-graph-tooltip';
  container.appendChild(tooltip);

  // Size
  function resize() {
    canvas.width = container.clientWidth;
    canvas.height = container.clientHeight;
  }
  resize();
  window.addEventListener('resize', resize);

  // Node colors by type (support both Chinese and English type names)
  const typeColors = {
    '公司': '#6366f1', '机构': '#8b5cf6', '技术': '#3b82f6',
    '材料': '#10b981', '产品': '#f59e0b', '人物': '#ef4444',
    '地点': '#6b7280', '政策': '#ec4899', '指标': '#14b8a6',
    '事件': '#f97316', '项目': '#8b5cf6', '设备': '#06b6d4',
    '方法': '#a855f7', '未知': '#9ca3af',
    // English types from LightRAG
    'organization': '#6366f1', 'artifact': '#f59e0b', 'concept': '#3b82f6',
    'location': '#6b7280', 'person': '#ef4444', 'method': '#a855f7',
    'event': '#f97316', 'content': '#ec4899', 'naturalobject': '#10b981',
    'data': '#14b8a6', 'other': '#9ca3af', 'UNKNOWN': '#9ca3af',
    '组织': '#6366f1', '人工制品': '#f59e0b', '概念': '#3b82f6',
    '其他': '#9ca3af',
  };

  const nodes = data.nodes.map(n => ({
    ...n,
    x: canvas.width / 2 + (Math.random() - 0.5) * canvas.width * 0.8,
    y: canvas.height / 2 + (Math.random() - 0.5) * canvas.height * 0.8,
    vx: 0, vy: 0,
    radius: Math.max(4, Math.min(16, 4 + (n.degree || 0) * 0.5)),
    color: typeColors[n.type] || typeColors['未知'],
  }));

  const nodeMap = {};
  nodes.forEach(n => { nodeMap[n.label] = n; });

  const edges = data.edges.filter(e => nodeMap[e.source] && nodeMap[e.target]);

  // Force simulation
  let animationId = null;
  let temperature = 1;
  let transform = { x: 0, y: 0, scale: 1 };
  let isDragging = false;
  let dragNode = null;
  let hoverNode = null;
  let mouseX = 0, mouseY = 0;
  let isPanning = false;
  let panStart = { x: 0, y: 0 };

  function simulate() {
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;

    // Repulsion
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[j].x - nodes[i].x;
        const dy = nodes[j].y - nodes[i].y;
        const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        const force = 800 / (dist * dist);
        nodes[i].vx -= (dx / dist) * force * temperature;
        nodes[i].vy -= (dy / dist) * force * temperature;
        nodes[j].vx += (dx / dist) * force * temperature;
        nodes[j].vy += (dy / dist) * force * temperature;
      }
    }

    // Attraction (edges)
    edges.forEach(e => {
      const a = nodeMap[e.source];
      const b = nodeMap[e.target];
      if (!a || !b) return;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const force = (dist - 80) * 0.01 * temperature;
      a.vx += (dx / dist) * force;
      a.vy += (dy / dist) * force;
      b.vx -= (dx / dist) * force;
      b.vy -= (dy / dist) * force;
    });

    // Center gravity
    nodes.forEach(n => {
      n.vx += (cx - n.x) * 0.001 * temperature;
      n.vy += (cy - n.y) * 0.001 * temperature;
    });

    // Update positions
    nodes.forEach(n => {
      if (n === dragNode) return;
      n.vx *= 0.85;
      n.vy *= 0.85;
      n.x += n.vx;
      n.y += n.vy;
    });

    temperature = Math.max(0.02, temperature * 0.998);
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    ctx.save();
    ctx.translate(transform.x, transform.y);
    ctx.scale(transform.scale, transform.scale);

    // Draw edges
    ctx.strokeStyle = 'rgba(150,150,160,0.15)';
    ctx.lineWidth = 1;
    edges.forEach(e => {
      const a = nodeMap[e.source];
      const b = nodeMap[e.target];
      if (!a || !b) return;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });

    // Draw nodes
    nodes.forEach(n => {
      // Glow for hover
      if (n === hoverNode) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.radius + 4, 0, Math.PI * 2);
        ctx.fillStyle = n.color + '30';
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
      ctx.fillStyle = n.color;
      ctx.fill();

      // Label for larger nodes
      if (n.radius > 8 || n === hoverNode) {
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(n.label.slice(0, 12), n.x, n.y - n.radius - 4);
      }
    });

    ctx.restore();
  }

  function animate() {
    simulate();
    draw();
    animationId = requestAnimationFrame(animate);
  }

  // Mouse interaction
  canvas.addEventListener('mousedown', (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left - transform.x) / transform.scale;
    const my = (e.clientY - rect.top - transform.y) / transform.scale;

    // Check if clicking a node
    for (const n of nodes) {
      const dx = mx - n.x;
      const dy = my - n.y;
      if (Math.sqrt(dx * dx + dy * dy) < n.radius + 3) {
        dragNode = n;
        isDragging = true;
        temperature = Math.max(temperature, 0.3);
        return;
      }
    }

    // Otherwise pan
    isPanning = true;
    panStart = { x: e.clientX - transform.x, y: e.clientY - transform.y };
  });

  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    mouseY = e.clientY - rect.top;

    if (isDragging && dragNode) {
      dragNode.x = (mouseX - transform.x) / transform.scale;
      dragNode.y = (mouseY - transform.y) / transform.scale;
      temperature = Math.max(temperature, 0.2);
    } else if (isPanning) {
      transform.x = e.clientX - panStart.x;
      transform.y = e.clientY - panStart.y;
    } else {
      // Hover detection
      const mx = (mouseX - transform.x) / transform.scale;
      const my = (mouseY - transform.y) / transform.scale;
      hoverNode = null;
      for (const n of nodes) {
        const dx = mx - n.x;
        const dy = my - n.y;
        if (Math.sqrt(dx * dx + dy * dy) < n.radius + 3) {
          hoverNode = n;
          break;
        }
      }

      if (hoverNode) {
        canvas.style.cursor = 'pointer';
        tooltip.textContent = `${hoverNode.label} (${hoverNode.type}) — 连接度: ${hoverNode.degree || 0}`;
        tooltip.style.left = (mouseX + 10) + 'px';
        tooltip.style.top = (mouseY + 10) + 'px';
        tooltip.classList.add('visible');
      } else {
        canvas.style.cursor = 'grab';
        tooltip.classList.remove('visible');
      }
    }
  });

  canvas.addEventListener('mouseup', () => {
    isDragging = false;
    isPanning = false;
    dragNode = null;
  });

  canvas.addEventListener('mouseleave', () => {
    isDragging = false;
    isPanning = false;
    dragNode = null;
    hoverNode = null;
    tooltip.classList.remove('visible');
  });

  // Zoom with wheel
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.max(0.3, Math.min(5, transform.scale * delta));
    // Zoom towards mouse position
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    transform.x = mx - (mx - transform.x) * (newScale / transform.scale);
    transform.y = my - (my - transform.y) * (newScale / transform.scale);
    transform.scale = newScale;
  }, { passive: false });

  animate();
}

// ── View switching integration ──
function switchToQAView() {
  // Hide main content
  document.querySelector('.content').style.display = 'none';
  qa$('qaView').style.display = 'flex';

  // Show QA sidebar in the main sidebar
  const qaSidebar = document.getElementById('qaSidebar');
  if (qaSidebar) qaSidebar.style.display = '';

  // Initialize if not already
  if (!qaState._initialized) {
    initQAView();
    qaState._initialized = true;
  }
}

function switchFromQAView() {
  // Restore content
  document.querySelector('.content').style.display = '';
  qa$('qaView').style.display = 'none';

  // Hide QA sidebar
  const qaSidebar = document.getElementById('qaSidebar');
  if (qaSidebar) qaSidebar.style.display = 'none';
}

// ── Stats loading ──
async function loadStats() {
  try {
    const resp = await fetch(`${QA_API_BASE}/api/stats`);
    const data = await resp.json();
    const statsEl = qa$('qaEmptyStats');
    if (statsEl && data.total_records) {
      statsEl.innerHTML = `
        <span>📊 ${data.total_records.toLocaleString()} 条情报</span>
        <span>🧠 ${data.indexed_records.toLocaleString()} 条已索引</span>
        <span>🕸️ ${data.graph_nodes || 0} 个知识节点</span>
      `;
    }
  } catch (e) {
    // Silent fail
  }
}

// ── Conversation persistence (localStorage) ──
function saveConversationsToStorage() {
  try {
    // Only save conversations that have messages (skip empty ones)
    const toSave = qaState.conversations
      .filter(c => c.messages.length > 0)
      .slice(0, 50) // Keep at most 50 conversations
      .map(c => ({
        id: c.id,
        title: c.title,
        messages: c.messages.map(m => ({
          role: m.role,
          content: m.content,
          citations: m.citations || [],
        })),
      }));
    localStorage.setItem('qa_conversations', JSON.stringify(toSave));
  } catch (e) {
    // localStorage might be full, silently fail
  }
}

function loadConversationsFromStorage() {
  try {
    const saved = localStorage.getItem('qa_conversations');
    if (saved) {
      const conversations = JSON.parse(saved);
      if (Array.isArray(conversations) && conversations.length > 0) {
        qaState.conversations = conversations;
        renderConversationList();
        // Don't auto-select any conversation - show empty state
      }
    }
  } catch (e) {
    // Parse error, ignore
  }
}

// Export for global access
window.qaModule = {
  switchToQAView,
  switchFromQAView,
  initQAView,
};
