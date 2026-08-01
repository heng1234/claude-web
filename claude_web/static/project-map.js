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
    relationType: '',
    viewMode: 'neighborhood',
    zoom: 1,
    panX: 0,
    panY: 0,
    dragging: null,
    selectedRelationId: '',
    impactIds: new Set(),
    impactItems: [],
    testSuggestions: [],
    impactSummary: '',
    impactLoading: false,
    contextPackLoading: false,
    freshness: null,
    inspectorMode: 'node',
    revisions: [],
    historyLoading: false,
    revisionCompare: null,
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
      const detail = payload?.detail;
      const message = typeof detail === 'string'
        ? detail
        : (detail?.message || detail?.code || payload?.error || `请求失败（${response.status}）`);
      throw new Error(message);
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
    if (state.stale) {
      const changes = state.freshness?.changes || state.freshness?.summary || {};
      const counts = changes.counts || {};
      const count = Number(changes.total || changes.changed || Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0));
      return { className: 'is-stale', label: count ? `源码有 ${count} 处变化` : '源码已有变化' };
    }
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
    const scored = nodes.map((node, order) => {
      if (state.kind && node.kind !== state.kind) return false;
      if (!needle) return { node, score: 0, order };
      const title = String(node.title || '').toLocaleLowerCase();
      const paths = (node.sources || []).map(source => String(source.path || '').toLocaleLowerCase());
      const symbols = (node.sources || []).map(source => String(source.symbol_key || '').toLocaleLowerCase());
      const summary = [node.summary, node.kind, ...(node.roles || [])].join(' ').toLocaleLowerCase();
      let score = -1;
      if (title === needle) score = 500;
      else if (title.startsWith(needle)) score = 400;
      else if (title.includes(needle)) score = 300;
      else if (symbols.some(value => value === needle || value.endsWith(`:${needle}`))) score = 260;
      else if (symbols.some(value => value.includes(needle))) score = 220;
      else if (paths.some(value => value === needle || value.endsWith(`/${needle}`))) score = 180;
      else if (paths.some(value => value.includes(needle))) score = 140;
      else if (summary.includes(needle)) score = 80;
      return score < 0 ? false : { node, score, order };
    }).filter(Boolean);
    scored.sort((a, b) => b.score - a.score || a.node.title.localeCompare(b.node.title) || a.order - b.order);
    return scored.slice(0, 300).map(item => item.node);
  }

  function filteredNodeCount() {
    const nodes = state.dataset?.nodes || [];
    const visible = visibleNodes();
    return { total: nodes.length, listed: visible.length, truncated: visible.length < nodes.length && !state.query && !state.kind };
  }

  function relationTypes() {
    return [...new Set((state.dataset?.relations || []).map(item => item.type).filter(Boolean))].sort();
  }

  function graphModel() {
    const allNodes = state.dataset?.nodes || [];
    const nodeIndex = new Map(allNodes.map(node => [node.id, node]));
    const selected = selectedNode();
    let relations = (state.dataset?.relations || []).filter(
      relation => !state.relationType || relation.type === state.relationType,
    );
    let nodes = [];
    if (state.viewMode === 'overview') {
      const overviewKinds = new Set(['project', 'module', 'entrypoint', 'capability', 'workflow', 'service', 'component']);
      nodes = allNodes.filter(node => overviewKinds.has(node.kind));
      if (!nodes.length) nodes = allNodes;
      const ids = new Set(nodes.map(node => node.id));
      relations = relations.filter(relation => ids.has(relation.source_id) && ids.has(relation.target_id));
    } else if (selected) {
      const neighborhood = relations.filter(
        relation => relation.source_id === selected.id || relation.target_id === selected.id,
      );
      const ids = new Set([selected.id]);
      neighborhood.forEach(relation => {
        ids.add(relation.source_id);
        ids.add(relation.target_id);
      });
      nodes = [...ids].map(id => nodeIndex.get(id)).filter(Boolean);
      relations = neighborhood;
    }
    const totalNodes = nodes.length;
    const totalRelations = relations.length;
    nodes = nodes.slice(0, 100);
    const ids = new Set(nodes.map(node => node.id));
    relations = relations.filter(relation => ids.has(relation.source_id) && ids.has(relation.target_id)).slice(0, 240);
    return {
      nodes,
      relations,
      totalNodes,
      totalRelations,
      nodesTruncated: totalNodes > nodes.length,
      relationsTruncated: totalRelations > relations.length,
    };
  }

  function selectedNode() {
    const nodes = state.dataset?.nodes || [];
    return nodes.find(node => node.id === state.selectedNodeId) || nodes[0] || null;
  }

  function selectedRelation() {
    return (state.dataset?.relations || []).find(item => item.id === state.selectedRelationId) || null;
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
          ${state.dataset ? `<button class="pm-button" type="button" data-pm-action="freshness">变化</button>
            <button class="pm-button" type="button" data-pm-action="history">版本</button>` : ''}
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
    const counts = filteredNodeCount();
    const graph = graphModel();
    const current = selectedNode();
    content.innerHTML = `
      <aside class="pm-panel left" aria-label="项目节点列表">
        <div class="pm-panel-head">
          <input class="pm-search" data-pm-search value="${attr(state.query)}" placeholder="搜索节点、角色或文件" aria-label="搜索项目地图" />
        </div>
        <div class="pm-list-meta" role="status">${counts.listed}/${counts.total} 个节点${counts.truncated ? ' · 列表最多显示 300 个' : ''}</div>
        <div class="pm-node-list" aria-label="项目节点">
          ${nodes.length ? nodes.map(nodeRow).join('') : `
            <div class="pm-error">没有匹配的节点。清空搜索可查看完整地图。</div>
          `}
        </div>
      </aside>
      <section class="pm-canvas-panel" aria-label="项目关系图">
        <div class="pm-canvas-head">
          <div class="pm-view-switch" role="group" aria-label="图谱视图">
            <button type="button" data-pm-view="neighborhood" aria-pressed="${state.viewMode === 'neighborhood'}">邻域</button>
            <button type="button" data-pm-view="overview" aria-pressed="${state.viewMode === 'overview'}">总览</button>
          </div>
          <div class="pm-legend">${legend()}</div>
          <div class="pm-canvas-tools">
            <span>${graph.nodes.length}/${graph.totalNodes} 节点 · ${graph.relations.length}/${graph.totalRelations} 关系${graph.nodesTruncated || graph.relationsTruncated ? ' · 已限量' : ''}</span>
            <button type="button" data-pm-zoom="out" aria-label="缩小图谱">−</button>
            <button type="button" data-pm-zoom="fit" aria-label="自动适配图谱">适配 ${Math.round(state.zoom * 100)}%</button>
            <button type="button" data-pm-zoom="in" aria-label="放大图谱">＋</button>
          </div>
        </div>
        <div class="pm-relation-filter" aria-label="关系类型筛选">
          <button type="button" data-pm-relation="" aria-pressed="${!state.relationType}">全部关系</button>
          ${relationTypes().map(type => `<button type="button" data-pm-relation="${attr(type)}" aria-pressed="${state.relationType === type}">${html(type)}</button>`).join('')}
        </div>
        <div class="pm-canvas" data-pm-canvas>${graphSvg(graph)}</div>
      </section>
      <aside class="pm-panel right" aria-label="节点详情">
        <div class="pm-panel-head">
          <button class="pm-mobile-back" type="button" data-pm-action="mobile-back" aria-label="返回节点列表">‹</button>
          <strong>节点详情</strong>
        </div>
        <div class="pm-detail-scroll">${inspectorView(current)}</div>
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
    const kinds = [...new Set((state.dataset?.nodes || []).map(node => node.kind))];
    return kinds.map(kind => `
      <button type="button" class="pm-kind-chip${state.kind === kind ? ' is-active' : ''}"
        data-pm-kind="${attr(kind)}" aria-pressed="${state.kind === kind ? 'true' : 'false'}"
        style="--pm-node-color:${kindColor(kind)}">
        <i></i>${html(kindLabel(kind))}
      </button>
    `).join('');
  }

  function graphSvg(model) {
    const shown = model.nodes;
    const relations = model.relations;
    if (!shown.length) return '<div class="pm-empty"><div class="pm-empty-card"><p>当前视图没有可显示的节点</p></div></div>';
    const positions = layoutNodes(shown, relations);
    const positionIndex = new Map(positions.map(point => [point.id, point]));
    const selected = selectedNode()?.id;
    const connected = new Set();
    relations.forEach(relation => {
      if (relation.source_id === selected) connected.add(relation.target_id);
      if (relation.target_id === selected) connected.add(relation.source_id);
    });
    const maxY = Math.max(520, ...positions.map(position => position.y + 82));

    const edgeMarkup = relations.map(relation => {
      const source = positionIndex.get(relation.source_id);
      const target = positionIndex.get(relation.target_id);
      if (!source || !target) return '';
      const x1 = source.x + 64;
      const y1 = source.y + 23;
      const x2 = target.x + 64;
      const y2 = target.y + 23;
      const curve = Math.max(28, Math.abs(x2 - x1) * .42);
      const active = relation.id === state.selectedRelationId || relation.source_id === selected || relation.target_id === selected;
      return `<path class="pm-edge-hit" data-pm-edge="${attr(relation.id)}" d="M${x1} ${y1} C${x1 + curve} ${y1},${x2 - curve} ${y2},${x2} ${y2}" />
        <path class="pm-edge${active ? ' is-highlighted' : ''}" data-pm-edge="${attr(relation.id)}" d="M${x1} ${y1} C${x1 + curve} ${y1},${x2 - curve} ${y2},${x2} ${y2}" marker-end="url(#pm-arrow)" />`;
    }).join('');

    const nodeMarkup = shown.map(node => {
      const point = positionIndex.get(node.id);
      const active = node.id === selected;
      const impacted = state.impactIds.has(node.id);
      const dimmed = selected && node.id !== selected && !connected.has(node.id);
      return `
        <g class="pm-graph-node${active ? ' is-selected' : ''}${impacted ? ' is-impacted' : ''}" data-pm-node="${attr(node.id)}"
          transform="translate(${point.x} ${point.y})" style="--pm-node-color:${kindColor(node.kind)};opacity:${dimmed ? '.48' : '1'}"
          role="button" tabindex="${active ? '0' : '-1'}" aria-label="${attr(`${node.title}，${kindLabel(node.kind)}`)}">
          <rect width="128" height="46" rx="8"></rect>
          <circle cx="11" cy="14" r="3" fill="${kindColor(node.kind)}"></circle>
          <text x="19" y="17">${html(shorten(node.title, 16))}</text>
          <text class="pm-svg-kind" x="11" y="34">${html(kindLabel(node.kind))}</text>
        </g>
      `;
    }).join('');

    return `
      <svg class="pm-graph" viewBox="0 0 960 ${maxY}" preserveAspectRatio="xMidYMid meet"
        data-pm-graph aria-label="Project Map 关系图：方向键切换节点，回车打开详情">
        <defs>
          <marker id="pm-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" opacity=".35"></path>
          </marker>
        </defs>
        <g class="pm-graph-viewport" transform="translate(${state.panX} ${state.panY}) scale(${state.zoom})">
          <g>${edgeMarkup}</g>
          <g>${nodeMarkup}</g>
        </g>
      </svg>
      ${model.nodesTruncated || model.relationsTruncated ? `<div class="pm-graph-notice" role="status">画布已限制为 ${model.nodes.length} 个节点和 ${model.relations.length} 条关系；可用类型筛选或邻域视图缩小范围。完整列表仍保留在左侧。</div>` : ''}
    `;
  }

  function layoutNodes(nodes, relations) {
    const selected = selectedNode()?.id;
    const buckets = [[], [], []];
    if (state.viewMode === 'neighborhood' && selected) {
      const incoming = new Set(relations.filter(item => item.target_id === selected).map(item => item.source_id));
      const outgoing = new Set(relations.filter(item => item.source_id === selected).map(item => item.target_id));
      nodes.forEach(node => {
        if (node.id === selected) buckets[1].push(node);
        else if (incoming.has(node.id)) buckets[0].push(node);
        else if (outgoing.has(node.id)) buckets[2].push(node);
        else buckets[2].push(node);
      });
    } else {
      const leftKinds = new Set(['project', 'module']);
      const middleKinds = new Set(['entrypoint', 'capability', 'workflow', 'service', 'component']);
      nodes.forEach(node => {
        if (leftKinds.has(node.kind)) buckets[0].push(node);
        else if (middleKinds.has(node.kind) || node.layer === 'semantic') buckets[1].push(node);
        else buckets[2].push(node);
      });
    }
    const points = [];
    const xValues = [70, 416, 762];
    buckets.forEach((bucket, column) => {
      bucket.sort((a, b) => a.title.localeCompare(b.title));
      const startY = Math.max(32, 260 - ((bucket.length - 1) * 64) / 2);
      bucket.forEach((node, index) => {
        points.push({ id: node.id, x: xValues[column], y: startY + index * 64 });
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
    const relationDetail = selectedRelation();
    return `
      <div>
        <span class="pm-kind-chip" style="--pm-node-color:${kindColor(node.kind)}"><i></i>${html(kindLabel(node.kind))} · ${node.layer === 'semantic' ? 'AI 推断' : '确定性'}</span>
        <h2 class="pm-detail-title">${html(node.title)}</h2>
        <p class="pm-detail-summary">${html(node.summary || '该节点来自项目的确定性结构或语义分析。')}</p>
        <div class="pm-fact-row" aria-label="节点可信度和状态">
          <span>可信度 ${html(node.confidence || 'unknown')}</span>
          <span>${node.stale ? '证据已过期' : '证据有效'}</span>
          <span>${html(node.layer === 'semantic' ? 'AI 语义层' : '解析器证据')}</span>
        </div>
        <div class="pm-node-actions" aria-label="节点动作">
          <button class="pm-button" type="button" data-pm-action="add-context" ${state.contextPackLoading || state.stale ? 'disabled' : ''}>添加上下文</button>
          <button class="pm-button" type="button" data-pm-action="prefill-plan" ${state.contextPackLoading || state.stale ? 'disabled' : ''}>生成 Plan</button>
          <button class="pm-button" type="button" data-pm-action="prefill-task" ${state.contextPackLoading || state.stale ? 'disabled' : ''}>创建任务</button>
          ${state.testSuggestions.length ? `<button class="pm-button" type="button" data-pm-action="prefill-tests" ${state.stale ? 'disabled' : ''}>建议测试</button>` : ''}
        </div>
        ${state.stale ? '<p class="pm-warning">源码或地图版本已经变化，刷新地图后才能创建可信上下文。</p>' : ''}
        ${node.stale_reasons?.length ? `<p class="pm-warning">${node.stale_reasons.map(html).join('；')}</p>` : ''}
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
            ${impactView(index)}
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
            return `<button class="pm-relation-row${state.selectedRelationId === relation.id ? ' is-active' : ''}" type="button" data-pm-edge="${attr(relation.id)}">
              <span>${outward ? '→' : '←'} ${html(relation.label || relation.type)}</span>
              <small>${html(other?.title || '未知节点')}</small>
            </button>`;
          }).join('') : '<p class="pm-detail-summary">暂无关系。</p>'}
        </div>
        ${relationDetail ? relationInspector(relationDetail, index) : ''}
      </div>
    `;
  }

  function inspectorView(node) {
    if (state.inspectorMode === 'freshness') return freshnessView();
    if (state.inspectorMode === 'history') return historyView();
    return detailView(node);
  }

  function freshnessView() {
    const payload = state.freshness;
    if (!payload) return '<p class="pm-detail-summary">正在读取源码变化…</p>';
    const changes = payload.changes || {};
    const groups = [
      ['新增', changes.added || []],
      ['修改', changes.modified || []],
      ['删除', changes.deleted || []],
      ['重命名', changes.renamed || []],
      ['扫描未确认', changes.unknown_missing || []],
    ];
    return `<div>
      <h2 class="pm-detail-title">源码新鲜度</h2>
      <p class="pm-detail-summary">地图 v${state.revision} · ${payload.stale ? '源码已有变化' : '与源码一致'} · ${html(payload.scan_completeness || payload.completeness || 'unknown')}</p>
      ${payload.partial ? `<p class="pm-warning">本次扫描不完整：${html(payload.partial_reason || payload.reason || 'unknown')}</p>` : ''}
      ${groups.map(([label, items]) => `<div class="pm-detail-section"><strong>${label} · ${items.length}</strong>
        ${items.length ? items.slice(0, 40).map(item => `<p class="pm-change-row">${html(item.path || `${item.from || ''} → ${item.to || ''}`)}</p>`).join('') : '<p class="pm-detail-summary">无</p>'}
      </div>`).join('')}
    </div>`;
  }

  function historyView() {
    if (state.historyLoading) return '<p class="pm-detail-summary">正在读取历史版本…</p>';
    const compare = state.revisionCompare;
    return `<div>
      <h2 class="pm-detail-title">地图版本</h2>
      <p class="pm-detail-summary">历史版本只读，不会回滚或替换当前源码对应关系。</p>
      ${state.revisions.length > 1 ? `<button class="pm-button" type="button" data-pm-action="compare-latest">比较最近两版</button>` : ''}
      ${compare ? revisionCompareView(compare) : ''}
      <div class="pm-detail-section"><strong>修订记录 · ${state.revisions.length}</strong>
        ${state.revisions.map(item => `<div class="pm-revision-row">
          <b>v${Number(item.revision)}</b><span>${Number(item.node_count || 0)} 节点 · ${Number(item.relation_count || 0)} 关系 · ${html(item.completeness || 'unknown')}</span>
          <small>scanner ${html(item.scanner_version || 'unknown')} · prompt ${html(item.prompt_version || 'unknown')}</small>
        </div>`).join('') || '<p class="pm-detail-summary">暂无历史版本。</p>'}
      </div>
    </div>`;
  }

  function revisionCompareView(compare) {
    const line = (label, payload) => `${label}：+${payload?.added?.length || 0} / −${payload?.removed?.length || 0} / ~${payload?.modified?.length || 0}`;
    return `<div class="pm-compare-card">
      <strong>v${Number(compare.from_revision)} → v${Number(compare.to_revision)}</strong>
      <span>${html(line('文件', compare.files))}</span>
      <span>${html(line('节点', compare.nodes))}</span>
      <span>${html(line('关系', compare.relations))}</span>
    </div>`;
  }

  function impactView(nodeIndex) {
    if (!state.impactItems.length) return '';
    return `<div class="pm-impact-list" aria-label="影响路径">
      ${state.impactItems.slice(0, 30).map(item => {
        const path = item.path || [];
        const pathText = path.length
          ? path.map(edge => `${nodeIndex.get(edge.source_id)?.title || edge.source_title || edge.source_id} → ${nodeIndex.get(edge.target_id)?.title || edge.target_title || edge.target_id}`).join(' · ')
          : '直接命中源码';
        return `<button type="button" class="pm-impact-row" data-pm-node="${attr(item.node_id)}">
          <strong>${html(item.title || item.node_id)}</strong>
          <span>${item.distance ? `${item.distance} 跳 · ` : ''}${html(pathText)}</span>
        </button>`;
      }).join('')}
    </div>`;
  }

  function relationInspector(relation, nodeIndex) {
    const source = nodeIndex.get(relation.source_id);
    const target = nodeIndex.get(relation.target_id);
    return `<div class="pm-detail-section pm-relation-inspector">
      <strong>关系证据</strong>
      <p class="pm-detail-summary"><b>${html(source?.title || relation.source_id)}</b> → <b>${html(target?.title || relation.target_id)}</b></p>
      <div class="pm-fact-row">
        <span>${html(relation.type)}</span>
        <span>${html(relation.provenance === 'llm_inferred' ? 'AI 推断' : '解析器')}</span>
        <span>可信度 ${html(relation.confidence || 'unknown')}</span>
      </div>
      <p class="pm-detail-summary">${html(relation.label || '该关系没有补充说明。')}${relation.evidence_ids?.length ? ` · ${relation.evidence_ids.length} 条证据` : ''}${relation.stale ? ' · 已过期' : ''}</p>
    </div>`;
  }

  function bindShellEvents() {
    const target = host();
    target?.querySelector('[data-pm-action="close"]')?.addEventListener('click', () => close());
    target?.querySelector('[data-pm-action="generate"]')?.addEventListener('click', () => generate());
    target?.querySelector('[data-pm-action="cancel"]')?.addEventListener('click', () => cancelRun());
    target?.querySelector('[data-pm-action="freshness"]')?.addEventListener('click', () => {
      openInspectorMode('freshness');
      if (!state.freshness) void checkFreshness(state.requestGeneration, context().sessionId);
    });
    target?.querySelector('[data-pm-action="history"]')?.addEventListener('click', () => {
      openInspectorMode('history');
      void loadRevisions();
    });
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
    ['add-context', 'prefill-plan', 'prefill-task'].forEach(action => {
      target?.querySelector(`[data-pm-action="${action}"]`)?.addEventListener('click', () => {
        void handleContextAction(action);
      });
    });
    target?.querySelector('[data-pm-action="prefill-tests"]')?.addEventListener('click', () => {
      if (state.stale) return;
      state.adapter?.prefillValidation?.({
        revision: state.revision,
        nodeId: state.selectedNodeId,
        suggestions: state.testSuggestions,
      });
      state.adapter?.notify?.('已把建议测试放入验证面板，请确认后运行');
    });
    target?.querySelector('[data-pm-action="compare-latest"]')?.addEventListener('click', () => {
      void compareLatestRevisions();
    });
    target?.querySelectorAll('[data-pm-kind]').forEach(button => {
      button.addEventListener('click', () => {
        state.kind = state.kind === button.dataset.pmKind ? '' : button.dataset.pmKind;
        syncSelectionWithVisibleNodes();
        renderContent();
      });
    });
    target?.querySelectorAll('[data-pm-view]').forEach(button => {
      button.addEventListener('click', () => {
        state.viewMode = button.dataset.pmView === 'overview' ? 'overview' : 'neighborhood';
        fitGraph();
        renderContent();
      });
    });
    target?.querySelectorAll('[data-pm-relation]').forEach(button => {
      button.addEventListener('click', () => {
        state.relationType = button.dataset.pmRelation || '';
        state.selectedRelationId = '';
        fitGraph();
        renderContent();
      });
    });
    target?.querySelectorAll('[data-pm-zoom]').forEach(button => {
      button.addEventListener('click', () => {
        if (button.dataset.pmZoom === 'fit') fitGraph();
        if (button.dataset.pmZoom === 'in') state.zoom = Math.min(2.2, state.zoom + .15);
        if (button.dataset.pmZoom === 'out') state.zoom = Math.max(.45, state.zoom - .15);
        renderContent();
      });
    });
    const search = target?.querySelector('[data-pm-search]');
    const applySearch = event => {
      state.query = event.target.value;
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
        } else if (element.classList.contains('pm-graph-node') && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
          event.preventDefault();
          moveGraphFocus(element.dataset.pmNode, event.key);
        }
      });
    });
    target?.querySelectorAll('[data-pm-edge]').forEach(element => {
      element.addEventListener('click', event => {
        event.stopPropagation();
        state.selectedRelationId = element.dataset.pmEdge || '';
        const relation = selectedRelation();
        if (relation && ![relation.source_id, relation.target_id].includes(state.selectedNodeId)) {
          state.selectedNodeId = relation.source_id;
        }
        renderContent();
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
    bindCanvasNavigation();
  }

  function selectNode(nodeId) {
    state.selectedNodeId = nodeId || '';
    state.selectedRelationId = '';
    state.inspectorMode = 'node';
    if (state.viewMode === 'neighborhood') fitGraph();
    renderContent();
    if (window.matchMedia('(max-width: 760px)').matches) {
      host()?.classList.add('pm-mobile-detail-open');
      host()?.querySelector('[data-pm-action="mobile-back"]')?.focus();
    } else {
      host()?.querySelector(`[data-pm-node="${CSS.escape(state.selectedNodeId)}"]`)?.focus();
    }
  }

  function openInspectorMode(mode) {
    state.inspectorMode = mode;
    renderContent();
    if (window.matchMedia('(max-width: 760px)').matches) {
      host()?.classList.add('pm-mobile-detail-open');
      host()?.querySelector('[data-pm-action="mobile-back"]')?.focus();
    }
  }

  async function loadRevisions() {
    const ctx = context();
    if (!ctx.sessionId || state.historyLoading) return;
    const generation = state.requestGeneration;
    state.historyLoading = true;
    renderContent();
    try {
      const payload = await request(`${apiBase(ctx.sessionId)}/revisions?limit=50`);
      if (generation !== state.requestGeneration || ctx.sessionId !== context().sessionId) return;
      state.revisions = payload.items || [];
    } catch (error) {
      state.error = error?.message || '历史版本加载失败';
    } finally {
      state.historyLoading = false;
      if (state.open) renderContent();
    }
  }

  async function compareLatestRevisions() {
    const ctx = context();
    if (!ctx.sessionId || state.revisions.length < 2) return;
    const [latest, previous] = state.revisions;
    const generation = state.requestGeneration;
    try {
      const payload = await request(
        `${apiBase(ctx.sessionId)}/revisions/compare?from_revision=${encodeURIComponent(previous.revision)}&to_revision=${encodeURIComponent(latest.revision)}`,
      );
      if (generation !== state.requestGeneration || ctx.sessionId !== context().sessionId) return;
      state.revisionCompare = payload;
      renderContent();
    } catch (error) {
      state.error = error?.message || '版本比较失败';
      renderShell();
    }
  }

  function fitGraph() {
    state.zoom = 1;
    state.panX = 0;
    state.panY = 0;
  }

  function moveGraphFocus(nodeId, key) {
    const nodes = graphModel().nodes;
    const currentIndex = nodes.findIndex(node => node.id === nodeId);
    if (currentIndex < 0 || !nodes.length) return;
    const delta = key === 'ArrowLeft' || key === 'ArrowUp' ? -1 : 1;
    const next = nodes[(currentIndex + delta + nodes.length) % nodes.length];
    if (next) selectNode(next.id);
  }

  function bindCanvasNavigation() {
    const canvas = host()?.querySelector('[data-pm-canvas]');
    if (!canvas) return;
    canvas.addEventListener('wheel', event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      state.zoom = Math.max(.45, Math.min(2.2, state.zoom + (event.deltaY < 0 ? .1 : -.1)));
      renderContent();
    }, { passive: false });
    canvas.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('[data-pm-node],[data-pm-edge]')) return;
      state.dragging = { x: event.clientX, y: event.clientY, panX: state.panX, panY: state.panY };
      canvas.setPointerCapture?.(event.pointerId);
      canvas.classList.add('is-panning');
    });
    canvas.addEventListener('pointermove', event => {
      if (!state.dragging) return;
      state.panX = state.dragging.panX + (event.clientX - state.dragging.x) / state.zoom;
      state.panY = state.dragging.panY + (event.clientY - state.dragging.y) / state.zoom;
      const viewport = canvas.querySelector('.pm-graph-viewport');
      viewport?.setAttribute('transform', `translate(${state.panX} ${state.panY}) scale(${state.zoom})`);
    });
    const stop = () => {
      state.dragging = null;
      canvas.classList.remove('is-panning');
    };
    canvas.addEventListener('pointerup', stop);
    canvas.addEventListener('pointercancel', stop);
  }

  function syncSelectionWithVisibleNodes() {
    const nodes = visibleNodes();
    if (!nodes.some(node => node.id === state.selectedNodeId)) {
      state.selectedNodeId = state.query ? state.selectedNodeId : (nodes[0]?.id || '');
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
      state.impactItems = [];
      state.testSuggestions = [];
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
      state.stale = Boolean(payload.stale || payload.partial);
      state.freshness = payload;
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
        body: JSON.stringify({ paths, expected_revision: revision, max_depth: 2, max_results: 120 }),
      });
      if (
        generation !== state.requestGeneration
        || sessionId !== context().sessionId
        || !state.open
        || payload.storage_key !== storageKey
        || Number(payload.revision) !== revision
      ) return;
      state.impactIds = new Set((payload.impacts || []).map(item => item.node_id));
      state.impactItems = payload.impacts || [];
      state.testSuggestions = payload.test_suggestions || [];
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

  async function handleContextAction(action) {
    const ctx = context();
    const node = selectedNode();
    if (!ctx.sessionId || !node || state.contextPackLoading || state.stale) return;
    const generation = state.requestGeneration;
    const sessionId = ctx.sessionId;
    state.contextPackLoading = true;
    renderContent();
    try {
      const payload = await request(`${apiBase(sessionId)}/context-packs`, {
        method: 'POST',
        body: JSON.stringify({ node_ids: [node.id], expected_revision: state.revision, ttl_seconds: 600 }),
      });
      if (generation !== state.requestGeneration || sessionId !== context().sessionId || !state.open) return;
      const descriptor = {
        packId: payload.pack_id,
        revision: Number(payload.revision),
        nodeIds: [node.id],
        label: node.title,
        expiresAt: payload.expires_at,
      };
      if (action === 'add-context') state.adapter?.attachContextPack?.(descriptor);
      if (action === 'prefill-plan') state.adapter?.prefillPlan?.(descriptor);
      if (action === 'prefill-task') state.adapter?.prefillTask?.(descriptor);
      close();
    } catch (error) {
      state.error = error?.message || '上下文包创建失败';
      renderShell();
    } finally {
      state.contextPackLoading = false;
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
    state.relationType = '';
    state.viewMode = 'neighborhood';
    state.zoom = 1;
    state.panX = 0;
    state.panY = 0;
    state.selectedRelationId = '';
    state.impactIds = new Set();
    state.impactItems = [];
    state.testSuggestions = [];
    state.impactSummary = '';
    state.impactLoading = false;
    state.contextPackLoading = false;
    state.freshnessGeneration = 0;
    state.freshness = null;
    state.inspectorMode = 'node';
    state.revisions = [];
    state.historyLoading = false;
    state.revisionCompare = null;
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
