(() => {
  'use strict';

  const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'superseded']);
  const KIND_LABELS = {
    project: '项目',
    module: '模块',
    route: '路由',
    file: '文件',
    component: '组件',
    service: '服务',
    data: '数据',
    entrypoint: '入口',
    workflow: '流程',
    capability: '能力',
  };
  const KIND_COLORS = {
    project: '#8b5cf6',
    module: '#3b82f6',
    route: '#f59e0b',
    file: '#94a3b8',
    component: '#06b6d4',
    service: '#10b981',
    data: '#ec4899',
    entrypoint: '#f97316',
    workflow: '#6366f1',
    capability: '#14b8a6',
  };

  const state = {
    adapter: null,
    open: false,
    activeSessionId: '',
    requestGeneration: 0,
    projectName: '',
    storageKey: '',
    revision: 0,
    dataset: null,
    stale: false,
    freshnessGeneration: 0,
    selectedNodeId: '',
    query: '',
    kind: '',
    zoom: 1,
    impactIds: new Set(),
    impactSummary: '',
    impactLoading: false,
    run: null,
    runMessage: '',
    error: '',
    source: null,
  };

  const html = value => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

  const attr = value => html(value);

  function context() {
    return state.adapter?.getContext?.() || {};
  }

  function host() {
    return document.getElementById('cwProjectMapHost');
  }

  function apiBase(sessionId = context().sessionId) {
    return `/api/sessions/${encodeURIComponent(sessionId)}/project-map`;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: {
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(payload?.detail || payload?.error || `请求失败（${response.status}）`);
    }
    return payload;
  }

  function kindColor(kind) {
    return KIND_COLORS[kind] || '#64748b';
  }

  function kindLabel(kind) {
    return KIND_LABELS[kind] || kind || '节点';
  }

  function statusView() {
    if (state.run && !TERMINAL.has(state.run.status)) {
      return { className: 'is-running', label: state.runMessage || phaseLabel(state.run.phase) };
    }
    if (state.stale) return { className: 'is-stale', label: '源码已有变化' };
    if (state.error) return { className: 'is-stale', label: state.error };
    if (state.dataset) return { className: 'is-ready', label: `地图 v${state.revision}` };
    return { className: '', label: '尚未生成' };
  }

  function phaseLabel(phase) {
    return {
      queued: '等待生成',
      scanning: '扫描项目',
      extracting: '提取结构',
      generating: '生成语义层',
      validating: '校验证据',
      persisting: '保存新版本',
      completed: '更新完成',
      cancelled: '已取消',
      failed: '生成失败',
      interrupted: '生成中断',
      superseded: '结果已过期',
    }[phase] || '处理中';
  }

  function visibleNodes() {
    const nodes = state.dataset?.nodes || [];
    const needle = state.query.trim().toLocaleLowerCase();
    return nodes.filter(node => {
      if (state.kind && node.kind !== state.kind) return false;
      if (!needle) return true;
      const haystack = [
        node.title,
        node.summary,
        node.kind,
        ...(node.roles || []),
        ...(node.sources || []).map(source => source.path),
      ].join(' ').toLocaleLowerCase();
      return haystack.includes(needle);
    }).slice(0, 300);
  }

  function selectedNode() {
    const nodes = state.dataset?.nodes || [];
    return nodes.find(node => node.id === state.selectedNodeId) || nodes[0] || null;
  }

  function renderShell() {
    const target = host();
    if (!target) return;
    const running = state.run && !TERMINAL.has(state.run.status);
    const status = statusView();
    const progress = running ? Number(state.run.progress || 0) : 0;
    const actionLabel = state.dataset ? '刷新' : '生成地图';

    target.innerHTML = `
      <div class="pm-shell">
        <div class="pm-toolbar">
          <button class="pm-icon-button" type="button" data-pm-action="close" aria-label="返回对话" title="返回对话">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m15 18-6-6 6-6"/></svg>
          </button>
          <div class="pm-toolbar-title">
            <strong>Project Map</strong>
            <span>${html(state.projectName || context().cwd || '当前 Code 项目')}</span>
          </div>
          <span class="pm-status ${status.className}" role="status" aria-live="polite">
            <i class="pm-status-dot"></i>${html(status.label)}
          </span>
          ${running ? `
            <button class="pm-button danger" type="button" data-pm-action="cancel">取消</button>
          ` : `
            <button class="pm-button ${state.dataset ? '' : 'primary'}" type="button" data-pm-action="generate">${actionLabel}</button>
          `}
        </div>
        <div class="pm-progress" role="progressbar" aria-label="Project Map 生成进度"
          aria-hidden="${running ? 'false' : 'true'}" aria-valuemin="0" aria-valuemax="100"
          aria-valuenow="${progress}" style="--pm-progress:${progress}%"><span></span></div>
        <div data-pm-content class="pm-workspace"></div>
      </div>
    `;

    renderContent();
    bindShellEvents();
  }

  function renderContent() {
    const content = host()?.querySelector('[data-pm-content]');
    if (!content) return;

    if (state.error && !state.dataset) {
      content.innerHTML = emptyView('地图暂时不可用', state.error, '重试');
      bindContentEvents();
      return;
    }
    if (!state.dataset) {
      const running = state.run && !TERMINAL.has(state.run.status);
      content.innerHTML = emptyView(
        running ? '正在理解这个项目' : '为当前 Code 项目生成知识图谱',
        running
          ? (state.runMessage || '正在扫描文件、提取确定性关系，并生成带源码证据的语义地图。')
          : '地图按项目目录共享，但通过当前 Code 会话鉴权。生成结果不会进入普通聊天上下文。',
        running ? '' : '开始生成',
      );
      bindContentEvents();
      return;
    }

    const nodes = visibleNodes();
    const current = selectedNode();
    content.innerHTML = `
      <aside class="pm-panel left" aria-label="项目节点列表">
        <div class="pm-panel-head">
          <input class="pm-search" data-pm-search value="${attr(state.query)}" placeholder="搜索节点、角色或文件" aria-label="搜索项目地图" />
        </div>
        <div class="pm-node-list" aria-label="项目节点">
          ${nodes.length ? nodes.map(nodeRow).join('') : `
            <div class="pm-error">没有匹配的节点。清空搜索可查看完整地图。</div>
          `}
        </div>
      </aside>
      <section class="pm-canvas-panel" aria-label="项目关系图">
        <div class="pm-canvas-head">
          <div class="pm-legend">${legend(nodes)}</div>
          <div class="pm-canvas-tools">
            <span>${nodes.length}/${(state.dataset.nodes || []).length} 节点</span>
            <button type="button" data-pm-zoom="out" aria-label="缩小图谱">−</button>
            <button type="button" data-pm-zoom="fit" aria-label="重置图谱缩放">${Math.round(state.zoom * 100)}%</button>
            <button type="button" data-pm-zoom="in" aria-label="放大图谱">＋</button>
          </div>
        </div>
        <div class="pm-canvas">${graphSvg(nodes)}</div>
      </section>
      <aside class="pm-panel right" aria-label="节点详情">
        <div class="pm-panel-head">
          <button class="pm-mobile-back" type="button" data-pm-action="mobile-back" aria-label="返回节点列表">‹</button>
          <strong>节点详情</strong>
        </div>
        <div class="pm-detail-scroll">${detailView(current)}</div>
      </aside>
    `;
    bindContentEvents();
  }

  function emptyView(title, description, actionLabel) {
    return `
      <div class="pm-empty" style="grid-column:1/-1;">
        <div class="pm-empty-card">
          <div class="pm-empty-icon" aria-hidden="true">
            <svg width="27" height="27" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="5" cy="6" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="18" cy="18" r="2"/><circle cx="7" cy="19" r="2"/><circle cx="12" cy="12" r="2"/><path d="m6.8 7 3.5 3.5M13.7 10.7l3.5-4M13.8 13.4l2.8 3M10.4 13.5 8.2 17"/></svg>
          </div>
          <h2>${html(title)}</h2>
          <p>${html(description)}</p>
          ${actionLabel ? `<button class="pm-button primary" type="button" data-pm-action="generate">${html(actionLabel)}</button>` : ''}
          ${state.error ? `<div class="pm-error">${html(state.error)}</div>` : ''}
        </div>
      </div>
    `;
  }

  function nodeRow(node) {
    const source = node.sources?.[0]?.path || node.roles?.[0] || kindLabel(node.kind);
    const impacted = state.impactIds.has(node.id);
    return `
      <button class="pm-node-row${impacted ? ' is-impacted' : ''}" type="button"
        aria-pressed="${node.id === selectedNode()?.id ? 'true' : 'false'}"
        data-pm-node="${attr(node.id)}" style="--pm-node-color:${kindColor(node.kind)}">
        <i class="pm-node-dot"></i>
        <span class="pm-node-copy">
          <strong>${html(node.title)}</strong>
          <span>${impacted ? '可能受影响 · ' : ''}${html(source)}</span>
        </span>
      </button>
    `;
  }

  function legend() {
    const kinds = [...new Set((state.dataset?.nodes || []).map(node => node.kind))].slice(0, 6);
    return kinds.map(kind => `
      <button type="button" class="pm-kind-chip${state.kind === kind ? ' is-active' : ''}"
        data-pm-kind="${attr(kind)}" aria-pressed="${state.kind === kind ? 'true' : 'false'}"
        style="--pm-node-color:${kindColor(kind)}">
        <i></i>${html(kindLabel(kind))}
      </button>
    `).join('');
  }

  function graphSvg(nodes) {
    if (!nodes.length) return '<div class="pm-empty"><div class="pm-empty-card"><p>没有可显示的节点</p></div></div>';
    const shown = nodes.slice(0, 100);
    const shownIds = new Set(shown.map(node => node.id));
    const positions = layoutNodes(shown);
    const relations = (state.dataset?.relations || []).filter(
      relation => shownIds.has(relation.source_id) && shownIds.has(relation.target_id),
    ).slice(0, 240);
    const selected = selectedNode()?.id;
    const connected = new Set();
    relations.forEach(relation => {
      if (relation.source_id === selected) connected.add(relation.target_id);
      if (relation.target_id === selected) connected.add(relation.source_id);
    });
    const maxY = Math.max(560, ...positions.map(position => position.y + 70));

    const edgeMarkup = relations.map(relation => {
      const source = positions.find(position => position.id === relation.source_id);
      const target = positions.find(position => position.id === relation.target_id);
      if (!source || !target) return '';
      const x1 = source.x + 64;
      const y1 = source.y + 23;
      const x2 = target.x + 64;
      const y2 = target.y + 23;
      const curve = Math.max(28, Math.abs(x2 - x1) * .42);
      const active = relation.source_id === selected || relation.target_id === selected;
      return `<path class="pm-edge${active ? ' is-highlighted' : ''}" d="M${x1} ${y1} C${x1 + curve} ${y1},${x2 - curve} ${y2},${x2} ${y2}" marker-end="url(#pm-arrow)" />`;
    }).join('');

    const nodeMarkup = shown.map(node => {
      const point = positions.find(position => position.id === node.id);
      const active = node.id === selected;
      const impacted = state.impactIds.has(node.id);
      const dimmed = selected && node.id !== selected && !connected.has(node.id);
      return `
        <g class="pm-graph-node${active ? ' is-selected' : ''}${impacted ? ' is-impacted' : ''}" data-pm-node="${attr(node.id)}"
          transform="translate(${point.x} ${point.y})" style="--pm-node-color:${kindColor(node.kind)};opacity:${dimmed ? '.48' : '1'}"
          role="button" tabindex="0" aria-label="${attr(`${node.title}，${kindLabel(node.kind)}`)}">
          <rect width="128" height="46" rx="8"></rect>
          <circle cx="11" cy="14" r="3" fill="${kindColor(node.kind)}"></circle>
          <text x="19" y="17">${html(shorten(node.title, 16))}</text>
          <text class="pm-svg-kind" x="11" y="34">${html(kindLabel(node.kind))}</text>
        </g>
      `;
    }).join('');

    return `
      <svg class="pm-graph" viewBox="0 0 960 ${maxY}" preserveAspectRatio="xMidYMin meet"
        style="width:${Math.round(960 * state.zoom)}px;height:${Math.round(maxY * state.zoom)}px;"
        aria-label="Project Map 关系图">
        <defs>
          <marker id="pm-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" opacity=".35"></path>
          </marker>
        </defs>
        <g>${edgeMarkup}</g>
        <g>${nodeMarkup}</g>
      </svg>
    `;
  }

  function layoutNodes(nodes) {
    const columns = [
      ['project'],
      ['module', 'entrypoint'],
      ['capability', 'workflow', 'service', 'component', 'data'],
      ['route', 'file'],
    ];
    const buckets = [[], [], [], []];
    nodes.forEach(node => {
      let column = columns.findIndex(kinds => kinds.includes(node.kind));
      if (column < 0) column = node.layer === 'semantic' ? 2 : 3;
      buckets[column].push(node);
    });
    const points = [];
    const xValues = [55, 280, 520, 760];
    buckets.forEach((bucket, column) => {
      bucket.sort((a, b) => a.title.localeCompare(b.title));
      bucket.forEach((node, index) => {
        points.push({ id: node.id, x: xValues[column], y: 34 + index * 68 });
      });
    });
    return points;
  }

  function shorten(value, length) {
    const text = String(value || '');
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  }

  function detailView(node) {
    if (!node) return '<p class="pm-detail-summary">选择一个节点查看说明与源码证据。</p>';
    const sources = node.sources || [];
    const related = (state.dataset?.relations || []).filter(
      relation => relation.source_id === node.id || relation.target_id === node.id,
    );
    const index = new Map((state.dataset?.nodes || []).map(item => [item.id, item]));
    return `
      <div>
        <span class="pm-kind-chip" style="--pm-node-color:${kindColor(node.kind)}"><i></i>${html(kindLabel(node.kind))} · ${node.layer === 'semantic' ? 'AI 推断' : '确定性'}</span>
        <h2 class="pm-detail-title">${html(node.title)}</h2>
        <p class="pm-detail-summary">${html(node.summary || '该节点来自项目的确定性结构或语义分析。')}</p>
        ${node.roles?.length ? `
          <div class="pm-detail-section">
            <strong>角色</strong>
            <p class="pm-detail-summary">${node.roles.map(html).join(' · ')}</p>
          </div>
        ` : ''}
        <div class="pm-detail-section">
          <strong>源码证据 ${sources.length ? `· ${sources.length}` : ''}</strong>
          ${sources.length ? `
            <button class="pm-button" type="button" data-pm-action="impact" ${state.impactLoading ? 'disabled' : ''}>
              ${state.impactLoading ? '分析中…' : '分析该节点的可能影响'}
            </button>
            ${state.impactSummary ? `<p class="pm-detail-summary" style="margin-top:8px;">${html(state.impactSummary)}</p>` : ''}
          ` : ''}
          ${sources.length ? sources.slice(0, 20).map((source, indexValue) => `
            <button class="pm-source-row" type="button" data-pm-source="${indexValue}">
              <code>${html(source.path)}</code>
              <span>${html(source.symbol_key || '')}${source.line_start ? ` · L${Number(source.line_start)}${source.line_end && source.line_end !== source.line_start ? `–${Number(source.line_end)}` : ''}` : ''}</span>
            </button>
          `).join('') : '<p class="pm-detail-summary">暂无可跳转的源码证据。</p>'}
        </div>
        <div class="pm-detail-section">
          <strong>关系 ${related.length ? `· ${related.length}` : ''}</strong>
          ${related.length ? related.slice(0, 20).map(relation => {
            const outward = relation.source_id === node.id;
            const other = index.get(outward ? relation.target_id : relation.source_id);
            return `<p class="pm-detail-summary" style="margin-bottom:7px;">${outward ? '→' : '←'} ${html(relation.label || relation.type)} · ${html(other?.title || '未知节点')}</p>`;
          }).join('') : '<p class="pm-detail-summary">暂无关系。</p>'}
        </div>
      </div>
    `;
  }

  function bindShellEvents() {
    const target = host();
    target?.querySelector('[data-pm-action="close"]')?.addEventListener('click', () => close());
    target?.querySelector('[data-pm-action="generate"]')?.addEventListener('click', () => generate());
    target?.querySelector('[data-pm-action="cancel"]')?.addEventListener('click', () => cancelRun());
  }

  function bindContentEvents() {
    const target = host();
    target?.querySelectorAll('[data-pm-action="generate"]').forEach(button => {
      button.addEventListener('click', () => generate());
    });
    target?.querySelector('[data-pm-action="mobile-back"]')?.addEventListener('click', () => {
      target.classList.remove('pm-mobile-detail-open');
      target.querySelector(`[data-pm-node="${CSS.escape(state.selectedNodeId)}"]`)?.focus();
    });
    target?.querySelector('[data-pm-action="impact"]')?.addEventListener('click', () => {
      void analyzeImpact();
    });
    target?.querySelectorAll('[data-pm-kind]').forEach(button => {
      button.addEventListener('click', () => {
        state.kind = state.kind === button.dataset.pmKind ? '' : button.dataset.pmKind;
        syncSelectionWithVisibleNodes();
        renderContent();
      });
    });
    target?.querySelectorAll('[data-pm-zoom]').forEach(button => {
      button.addEventListener('click', () => {
        if (button.dataset.pmZoom === 'fit') state.zoom = 1;
        if (button.dataset.pmZoom === 'in') state.zoom = Math.min(1.8, state.zoom + .2);
        if (button.dataset.pmZoom === 'out') state.zoom = Math.max(.6, state.zoom - .2);
        renderContent();
      });
    });
    const search = target?.querySelector('[data-pm-search]');
    const applySearch = event => {
      state.query = event.target.value;
      syncSelectionWithVisibleNodes();
      renderContent();
      const input = host()?.querySelector('[data-pm-search]');
      if (input) {
        input.focus();
        input.setSelectionRange(state.query.length, state.query.length);
      }
    };
    search?.addEventListener('input', event => {
      if (!event.isComposing) applySearch(event);
    });
    search?.addEventListener('compositionend', applySearch);
    target?.querySelectorAll('[data-pm-node]').forEach(element => {
      const select = () => selectNode(element.dataset.pmNode);
      element.addEventListener('click', select);
      element.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          select();
        }
      });
    });
    target?.querySelectorAll('[data-pm-source]').forEach(button => {
      button.addEventListener('click', () => {
        const node = selectedNode();
        const source = node?.sources?.[Number(button.dataset.pmSource)];
        if (!source?.path) return;
        state.adapter?.openFile?.(source.path, {
          mode: 'source',
          lineStart: Number(source.line_start || 1),
          lineEnd: Number(source.line_end || source.line_start || 1),
        });
      });
    });
  }

  function selectNode(nodeId) {
    state.selectedNodeId = nodeId || '';
    renderContent();
    if (window.matchMedia('(max-width: 760px)').matches) {
      host()?.classList.add('pm-mobile-detail-open');
      host()?.querySelector('[data-pm-action="mobile-back"]')?.focus();
    } else {
      host()?.querySelector(`[data-pm-node="${CSS.escape(state.selectedNodeId)}"]`)?.focus();
    }
  }

  function syncSelectionWithVisibleNodes() {
    const nodes = visibleNodes();
    if (!nodes.some(node => node.id === state.selectedNodeId)) {
      state.selectedNodeId = nodes[0]?.id || '';
    }
  }

  async function loadMap() {
    const ctx = context();
    if (!state.open || !ctx.codeMode || !ctx.sessionId) return;
    const generation = ++state.requestGeneration;
    state.error = '';
    try {
      const payload = await request(apiBase(ctx.sessionId));
      if (generation !== state.requestGeneration || !state.open) return;
      state.projectName = payload.project_name || ctx.cwd || '';
      state.activeSessionId = ctx.sessionId;
      state.storageKey = payload.storage_key || '';
      state.revision = Number(payload.revision || 0);
      state.dataset = payload.dataset || null;
      state.run = payload.active_run || null;
      state.impactIds = new Set();
      state.impactSummary = '';
      if (!state.selectedNodeId || !(state.dataset?.nodes || []).some(node => node.id === state.selectedNodeId)) {
        state.selectedNodeId = state.dataset?.nodes?.[0]?.id || '';
      }
      renderShell();
      if (state.run && !TERMINAL.has(state.run.status)) subscribe(state.run.run_id);
      void checkFreshness(generation, ctx.sessionId);
    } catch (error) {
      if (generation !== state.requestGeneration || !state.open) return;
      state.error = error?.message || 'Project Map 加载失败';
      renderShell();
    }
  }

  async function checkFreshness(generation, sessionId) {
    if (!state.dataset || state.freshnessGeneration === generation) return;
    state.freshnessGeneration = generation;
    try {
      const payload = await request(`${apiBase(sessionId)}/freshness`);
      if (generation !== state.requestGeneration || !state.open) return;
      state.stale = Boolean(payload.stale);
      renderShell();
    } catch {
      // Freshness is advisory. Keep the last valid map available.
    } finally {
      if (state.freshnessGeneration === generation) state.freshnessGeneration = 0;
    }
  }

  async function generate() {
    const ctx = context();
    if (!state.open || !ctx.codeMode || !ctx.sessionId) return;
    const generation = state.requestGeneration;
    const sessionId = ctx.sessionId;
    const storageKey = state.storageKey;
    state.error = '';
    try {
      const endpoint = state.dataset ? 'refresh' : 'generate';
      const payload = await request(`${apiBase(sessionId)}/${endpoint}`, {
        method: 'POST',
        body: JSON.stringify({ preferred_language: 'zh' }),
      });
      if (
        generation !== state.requestGeneration
        || sessionId !== context().sessionId
        || !state.open
        || (storageKey && payload.storage_key !== storageKey)
      ) return;
      state.run = payload.run;
      state.runMessage = payload.deduplicated ? '已有生成任务正在运行' : '已加入生成队列';
      renderShell();
      subscribe(state.run.run_id);
    } catch (error) {
      if (generation === state.requestGeneration && sessionId === context().sessionId && state.open) {
        state.error = error?.message || '无法启动 Project Map 生成';
        renderShell();
      }
    }
  }

  async function analyzeImpact() {
    const ctx = context();
    const node = selectedNode();
    const paths = [...new Set((node?.sources || []).map(source => source.path).filter(Boolean))].slice(0, 50);
    if (!ctx.sessionId || !state.storageKey || !paths.length || state.impactLoading) return;
    const generation = state.requestGeneration;
    const sessionId = ctx.sessionId;
    const storageKey = state.storageKey;
    const revision = state.revision;
    state.impactLoading = true;
    state.impactSummary = '';
    renderContent();
    try {
      const payload = await request(`${apiBase(sessionId)}/impact`, {
        method: 'POST',
        body: JSON.stringify({ paths }),
      });
      if (
        generation !== state.requestGeneration
        || sessionId !== context().sessionId
        || !state.open
        || payload.storage_key !== storageKey
        || Number(payload.revision) !== revision
      ) return;
      state.impactIds = new Set((payload.impacts || []).map(item => item.node_id));
      state.stale = Boolean(payload.stale);
      const direct = (payload.impacts || []).filter(item => item.level === 'direct').length;
      const indirect = Math.max(0, (payload.impacts || []).length - direct);
      state.impactSummary = `找到 ${direct} 个直接节点、${indirect} 个可能受影响的上游节点${payload.truncated ? '（结果已截断）' : ''}。`;
      renderShell();
    } catch (error) {
      if (generation === state.requestGeneration && sessionId === context().sessionId && state.open) {
        state.impactSummary = error?.message || '影响分析失败';
      }
    } finally {
      if (generation === state.requestGeneration && sessionId === context().sessionId) {
        state.impactLoading = false;
        if (state.open) renderContent();
      }
    }
  }

  function subscribe(runId) {
    if (!runId || !state.open) return;
    if (state.source) state.source.close();
    const ctx = context();
    if (!ctx.sessionId) return;
    const generation = state.requestGeneration;
    const sessionId = ctx.sessionId;
    const source = new EventSource(`${apiBase(sessionId)}/runs/${encodeURIComponent(runId)}/stream`);
    state.source = source;
    const handle = event => {
      if (
        state.source !== source
        || generation !== state.requestGeneration
        || sessionId !== context().sessionId
        || state.run?.run_id !== runId
        || !state.open
      ) return;
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      state.run = { ...(state.run || {}), ...payload };
      state.runMessage = payload.message || phaseLabel(payload.phase || payload.status);
      if (payload.error_message) state.error = payload.error_message;
      renderShell();
      if (TERMINAL.has(payload.status)) {
        source.close();
        if (state.source === source) state.source = null;
        if (payload.status === 'completed') {
          state.adapter?.notify?.('Project Map 已更新');
          void loadMap();
        }
      }
    };
    source.addEventListener('status', handle);
    source.addEventListener('cancel_requested', handle);
    source.onerror = () => {
      if (
        state.source === source
        && generation === state.requestGeneration
        && sessionId === context().sessionId
        && state.run?.run_id === runId
        && !TERMINAL.has(state.run.status)
      ) {
        source.close();
        state.source = null;
        window.setTimeout(() => {
          if (state.open && state.run?.run_id === runId) void recoverRun(runId);
        }, 1200);
      }
    };
  }

  async function recoverRun(runId) {
    const ctx = context();
    if (!ctx.sessionId || !state.open) return;
    const generation = state.requestGeneration;
    const sessionId = ctx.sessionId;
    const storageKey = state.storageKey;
    try {
      const payload = await request(`${apiBase(sessionId)}/runs/${encodeURIComponent(runId)}`);
      if (
        generation !== state.requestGeneration
        || sessionId !== context().sessionId
        || !state.open
        || payload.storage_key !== storageKey
      ) return;
      state.run = payload.run;
      if (TERMINAL.has(state.run.status)) {
        if (state.run.status === 'completed') await loadMap();
        else renderShell();
      } else {
        renderShell();
        subscribe(runId);
      }
    } catch (error) {
      if (generation === state.requestGeneration && sessionId === context().sessionId && state.open) {
        state.error = error?.message || '生成状态连接中断';
        renderShell();
      }
    }
  }

  async function cancelRun() {
    const ctx = context();
    const runId = state.run?.run_id;
    if (!ctx.sessionId || !runId) return;
    const generation = state.requestGeneration;
    const sessionId = ctx.sessionId;
    const storageKey = state.storageKey;
    try {
      const payload = await request(`${apiBase(sessionId)}/runs/${encodeURIComponent(runId)}/cancel`, {
        method: 'POST',
      });
      if (
        generation !== state.requestGeneration
        || sessionId !== context().sessionId
        || !state.open
        || payload.storage_key !== storageKey
      ) return;
      state.runMessage = '正在取消';
      renderShell();
    } catch (error) {
      if (generation === state.requestGeneration && sessionId === context().sessionId && state.open) {
        state.error = error?.message || '取消失败';
        renderShell();
      }
    }
  }

  async function open() {
    const ctx = context();
    if (!ctx.codeMode || !ctx.sessionId) return;
    if (state.activeSessionId && state.activeSessionId !== ctx.sessionId) {
      state.dataset = null;
      state.run = null;
      state.selectedNodeId = '';
      state.storageKey = '';
      state.revision = 0;
      state.stale = false;
      state.error = '';
    }
    state.open = true;
    document.body.classList.add('cw-project-map-open');
    host()?.classList.remove('hidden');
    document.getElementById('cwProjectMapBtn')?.setAttribute('aria-pressed', 'true');
    renderShell();
    await loadMap();
  }

  function close(options = {}) {
    state.open = false;
    state.requestGeneration += 1;
    state.source?.close();
    state.source = null;
    document.body.classList.remove('cw-project-map-open');
    host()?.classList.add('hidden');
    host()?.classList.remove('pm-mobile-detail-open');
    const button = document.getElementById('cwProjectMapBtn');
    button?.setAttribute('aria-pressed', 'false');
    if (!options.silent) button?.focus();
  }

  function onSessionChanged() {
    if (!state.open) return;
    state.requestGeneration += 1;
    state.source?.close();
    state.source = null;
    state.dataset = null;
    state.activeSessionId = '';
    state.selectedNodeId = '';
    state.kind = '';
    state.zoom = 1;
    state.impactIds = new Set();
    state.impactSummary = '';
    state.impactLoading = false;
    state.freshnessGeneration = 0;
    state.projectName = '';
    state.storageKey = '';
    state.revision = 0;
    state.stale = false;
    state.run = null;
    state.runMessage = '';
    state.error = '';
    host()?.classList.remove('pm-mobile-detail-open');
    renderShell();
    void loadMap();
  }

  window.CWProjectMap = {
    configure(adapter) {
      state.adapter = adapter;
    },
    open,
    close,
    isOpen: () => state.open,
    onSessionChanged,
  };
})();
