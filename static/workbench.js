/* CCW Desk workbench: extends the existing composer and shell navigation. */
(() => {
  'use strict';
  const q = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {extensions:'扩展',marketplace:'扩展目录',profiles:'配置档',commands:'快捷命令',workflows:'工作流',tasks:'任务中心',providers:'模型与路由',sharing:'导入 / 导出'};
  const components = {skills:'Skills',agents:'Agents',commands:'命令',mcp:'MCP 工具',hooks:'Hooks',workflows:'工作流'};
  const workModes = ['chat','code','research','browser','review','automation'];
  const readOnlyModes = new Set(['research','browser','review']);
  let catalog = {}, tab = 'extensions', preview = null, timer = null;
  let dirty = false, refreshSerial = 0, selectionSerial = 0, actionPending = false, pageLoaded = false;
  const sessionSelections = new Map();
  let importDraft = null, sessionModel = null;
  let selection = {work_mode:localStorage.getItem('ccw-work-mode') || ''};
  const cwd = () => q('cwdInput')?.value.trim() || '';
  const sid = () => typeof sessionId === 'undefined' ? '' : sessionId || '';
  const activeMode = () => codeMode ? (workModes.includes(selection.work_mode)&&selection.work_mode!=='chat'?selection.work_mode:'code') : 'chat';
  const isReadOnlyMode = () => readOnlyModes.has(activeMode());
  function renderWorkMode() {
    const current=activeMode(),readonly=isReadOnlyMode(),research=current==='research';
    const webEnabled=research&&isWebSearchEnabled();
    q('wbModeSelect').value=current;
    q('wbWorkModeStatus').hidden=!readonly;
    document.body.classList.toggle('wb-readonly-mode',readonly);
    q('wbWorkModeText').textContent=readonly?[t('work_mode.'+current),t('work_mode.readonly'),...(webEnabled?[t('work_mode.web_on')]:[])].join(' · '):'';
    q('wbResearchWeb').hidden=!research||webEnabled;
    for(const id of ['cwComposerPermBtn','cwWorkspacePermBtn']) {
      const button=q(id);
      if(!button)continue;
      button.disabled=readonly;
      if(readonly)button.setAttribute('aria-describedby','wbWorkModeText');
      else button.removeAttribute('aria-describedby');
    }
  }
  function syncWorkMode() {
    if(isReadOnlyMode())setPermissionModeMenuOpen(false);
    applyWebSearchBtnState();updateWorkspaceLabels();
  }
  function prepareMode(mode, forceNew=false) {
    if(currentController||agentLoopRunning){toast(t('work_mode.turn_running'));return false;}
    const nextCode=mode!=='chat';
    if(!forceNew&&!sid()&&nextCode===codeMode)return true;
    if(pendingImages.length||pendingDocs.length||activeImageUploadPromises.size||activeDocUploadPromises.size||codeContextPacks.size){
      toast(t('work_mode.pending_attachments'));return false;
    }
    if((sid()||(nextCode!==codeMode&&q('input').value.trim()))&&!confirm(t('work_mode.new_session_confirm')))return false;
    saveDraft();rememberWorkspaceSession();leave({force:true});
    if(nextCode!==codeMode) {
      setCodeMode(nextCode);
      if(!nextCode){q('cwdInput').value='';q('gitBar').classList.add('hidden');}
    }
    startNewSession();
    return true;
  }
  const selectedIds = id => [...q(id).querySelectorAll('input:checked')].map(o => o.value);
  const options = (rows, selected='', empty='默认') => `<option value="">${esc(empty)}</option>` + rows.map(r => `<option value="${esc(r.id)}" ${r.id === selected ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
  const modes = selected => Object.entries(catalog.modes || {}).map(([id,m]) => `<option value="${id}" ${id === selected ? 'selected' : ''}>${esc(m.name)}</option>`).join('');
  const error = err => {const el=q('wbError');el.classList.remove('hidden');el.textContent=err.message || String(err);el.tabIndex=-1;el.focus();el.scrollIntoView({block:'nearest'});};
  const discard = () => !dirty || confirm('还有未保存的修改，确定放弃吗？');
  async function api(path, init={}) {
    const response = await fetch('/api/workbench' + path, {...init, headers: {...(init.body instanceof FormData ? {} : {'Content-Type':'application/json'}),...init.headers}});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail || response.status));
    return result;
  }
  const post = (path, data={}) => api(path, {method:'POST',body:JSON.stringify(data)});
  const put = (path, data={}) => api(path, {method:'PUT',body:JSON.stringify(data)});
  const note = text => `<div class="wb-note">${esc(text)}</div>`;
  const empty = text => `<div class="wb-empty">${esc(text)}</div>`;
  const field = (name,label,value='',type='text') => `<label>${esc(label)}<input id="${name}" type="${type}" value="${esc(value)}"></label>`;
  const area = (name,label,value='',rows=6) => `<label>${esc(label)}<textarea id="${name}" rows="${rows}">${esc(value)}</textarea></label>`;
  const select = (id,label,html) => `<label>${esc(label)}<select id="${id}">${html}</select></label>`;
  const btn = (action,text,id='',variant='') => `<button type="button" class="wb-button ${variant}" data-wb-action="${action}" data-id="${esc(id)}">${esc(text)}</button>`;
  const filePicker = (id, label, accept, hint) => `<div class="wb-file-picker"><input id="${id}" type="file" accept="${accept}" aria-label="${label}" hidden><div class="wb-file-row"><button type="button" class="wb-button" data-wb-pick="${id}" aria-describedby="${id}Status ${id}Hint">${label}</button><span id="${id}Status" class="wb-file-name" role="status">未选择文件</span><button type="button" class="wb-button wb-quiet" data-wb-clear="${id}" hidden>清除</button></div><p id="${id}Hint" class="wb-muted">${hint}</p></div>`;
  function fileLabel(input) {
    const file=input.files[0];
    q(input.id+'Status').textContent=file?file.name+' · '+(file.size/1024).toFixed(1)+' KB':'未选择文件';
    q('ccwWorkbench').querySelector('[data-wb-clear="'+input.id+'"]').hidden=!file;
  }
  async function pending(target, run, feedback=true) {
    const original=target.innerHTML;
    const locked=[...q('ccwWorkbench').querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];
    actionPending=true;q('wbError').classList.add('hidden');
    target.setAttribute('aria-busy','true');
    if(feedback)target.textContent='处理中…';
    try {
      // Read form values before disabling them; scope bindings ignore disabled components.
      const result=run();locked.forEach(el=>el.disabled=true);await result;
    } finally {
      actionPending=false;locked.forEach(el=>el.disabled=false);
      target.removeAttribute('aria-busy');target.innerHTML=original;
    }
  }
  function clearImport() {
    importDraft=null;q('wbImportPreview').innerHTML='';q('wbImportPreview').hidden=true;
    q('wbImportFileStatus').textContent='未选择文件';
  }
  async function importFile(input) {
    const file=input.files[0];if(!file||actionPending)return;
    const button=q('ccwWorkbench').querySelector('[data-wb-pick="wbImportFile"]');
    clearImport();q('wbImportFileStatus').textContent='正在检查 '+file.name;
    try {
      await pending(button,async()=>{
        const zip=/\.zip$/i.test(file.name);
        if(!zip&&!/\.json$/i.test(file.name))throw new Error('请选择 JSON 或 ZIP 配置文件');
        if(file.size>(zip?32:2)*1024*1024)throw new Error(zip?'配置包超过 32 MB':'配置内容超过 2 MB');
        const path=zip?'/import-zip':'/import';
        let body;
        if(zip){body=new FormData();body.append('file',file);}
        else {try{body=JSON.stringify(JSON.parse(await file.text()));}catch{throw new Error('JSON 格式无效，请检查文件内容后重试');}}
        const {items,revision}=await api(path+'?preview=true',{method:'POST',body});
        importDraft={path,body,revision,name:file.name};
        const group=(status,title)=>{
          const rows=items.filter(item=>item.status===status);
          return rows.length?`<div class="wb-import-group${status==='replace'?' wb-import-replace':''}"><strong>${title} · ${rows.length} 项</strong><ul>${rows.map(item=>`<li>${esc(labels[item.kind])}：${esc(item.name)} <small>(${esc(item.id)})</small></li>`).join('')}</ul></div>`:'';
        };
        const panel=q('wbImportPreview');
        panel.innerHTML=`<h4>确认导入</h4>${group('replace','将覆盖')}${group('new','将新增')}${group('unchanged','内容相同')}${items.some(item=>item.status!=='unchanged')?'':note('没有需要导入的配置。')}<div class="wb-actions">${btn('cancel-import','取消')}${items.some(item=>item.status!=='unchanged')?btn('confirm-import','确认导入','','wb-primary'):''}</div>`;
        panel.hidden=false;q('wbImportFileStatus').textContent=file.name+' · '+(file.size/1024).toFixed(1)+' KB · 尚未导入';
      });
      q('wbImportPreview').focus();
    } catch(err) {error(err);q('wbImportFileStatus').textContent='无法预览 · '+file.name+'，可重新选择后重试';}
    finally {input.value='';}
  }
  function updateSessionModel(id=sid()) {
    const info=sessionModel?.id===id?sessionModel.info:null;
    const status=info?(info.source==='binding'?'会话绑定：':'运行时报告：')+info.service+' · '+(info.model||'未记录模型 ID')
      :(id?'当前会话：暂无已记录的模型':'尚未发送消息；会话模型将在运行后显示');
    for(const id of ['wbSessionModel','cwSessionModelSummary']) {
      const el=q(id);if(el)el.textContent=status;
    }
  }
  function observeSessionModel(id,event) {
    if(!id||id!==sid()||event.parent_tool_use_id)return;
    if(event.type==='meta'&&event.session_model)sessionModel={id,info:event.session_model};
    else {
      if(sessionModel?.id===id&&sessionModel.info.source==='binding')return;
      const model=event.type==='assistant'?event.message?.model:event.type==='system'&&event.subtype==='init'?event.model:null;
      if(typeof model!=='string'||!model.trim()||model.startsWith('<'))return;
      sessionModel={id,info:{service:'Claude 原生',model,source:'runtime'}};
    }
    updateSessionModel();
  }
  function editor(title,html,action='save') {return `<section class="wb-editor"><h3>${esc(title)}</h3><form id="wbForm" class="wb-form">${html}<div class="wb-footer">${btn('close-editor','取消')}${btn(action,action==='install-confirm'?'安装':action==='preview'?'预览内容':'保存','','wb-primary')}</div></form></section>`;}
  async function load() {
    catalog = await api('/catalog?cwd='+encodeURIComponent(cwd())+'&session_id='+encodeURIComponent(sid()));
    return catalog;
  }
  function renderNav() {
    q('wbNav').innerHTML=Object.entries(labels).map(([id,label])=>`<button type="button" role="tab" id="wbTab-${id}" aria-controls="wbPanel" aria-selected="${tab===id}" tabindex="${tab===id?0:-1}" data-wb-tab="${id}">${label}</button>`).join('');
    q('wbSectionSelect').value=tab;
    q('wbPanel').setAttribute('aria-labelledby','wbTab-'+tab);
  }
  function leave({focus=false,force=false}={}) {
    if(q('ccwWorkbench').classList.contains('hidden'))return true;
    if(actionPending&&!force)return false;
    ++refreshSerial;clearInterval(timer);
    q('ccwWorkbench').classList.add('hidden');document.body.classList.remove('wb-view');
    updateShellNav();
    if(focus)q('input')?.focus();
    return true;
  }
  async function open(next=tab) {
    if(actionPending)return;
    const wasVisible=!q('ccwWorkbench').classList.contains('hidden');
    if(next!==tab&&!discard())return;
    const changed=next!==tab;tab=next;
    if(typeof setHeaderToolsOpen==='function')setHeaderToolsOpen(false);
    if(typeof closeMobileSidebar==='function')closeMobileSidebar();
    document.body.classList.add('wb-view');q('ccwWorkbench').classList.remove('hidden');
    updateShellNav();q('wbTitle').focus();
    if(changed||!pageLoaded||(!wasVisible&&!dirty&&!q('wbEditor').children.length))await refresh();
    clearInterval(timer);timer=setInterval(()=>{if(tab==='tasks'&&!q('ccwWorkbench').classList.contains('hidden')&&!q('wbEditor').children.length)renderTasks().catch(error);},3000);
  }
  async function refresh() {
    const serial=++refreshSerial;
    dirty=false;importDraft=null;renderNav();q('wbEditor').innerHTML='';q('wbError').classList.add('hidden');
    q('wbBody').innerHTML='<p class="wb-muted" role="status">正在加载…</p>';q('wbPanel').setAttribute('aria-busy','true');
    try {
      const data=await api('/catalog?cwd='+encodeURIComponent(cwd())+'&session_id='+encodeURIComponent(sid()));
      if(serial!==refreshSerial) return;
      catalog=data;pageLoaded=true;
      if(tab==='extensions') renderExtensions();
      else if(tab==='marketplace') await renderMarketplace();
      else if(['profiles','commands','workflows'].includes(tab)) renderConfigs();
      else if(tab==='tasks') await renderTasks();
      else if(tab==='providers') renderProviders();
      else renderSharing();
    } catch(err) { if(serial===refreshSerial){q('wbBody').innerHTML=empty('加载失败，请切换栏目或重新打开重试。');error(err);} }
    finally {if(serial===refreshSerial) q('wbPanel').setAttribute('aria-busy','false');}
  }
  function renderExtensions() {
    q('wbBody').innerHTML=`<div class="wb-toolbar"><h3>已安装扩展</h3>${btn('install-form','添加扩展','','wb-primary')}</div>${note('支持标准 Claude Code 插件和 ccw-plugin.json。安装后选择作用范围与组件启用；Hook 和 MCP 默认关闭。')}<div class="wb-grid">${(catalog.extensions||[]).map(p=>{
      const active=(catalog.effective||[]).find(x=>x.id===p.id);
      return `<article class="wb-card"><h4>${esc(p.name)}</h4><small>${esc(p.id)} · ${esc(p.version)}</small><p>${esc(p.description||'暂无描述')}</p><div class="wb-tags">${Object.entries(p.inventory).filter(([,v])=>v.length).map(([k,v])=>`<span class="wb-tag">${components[k]} ${v.length}</span>`).join('')}</div><div class="wb-status">${active?'已启用 · '+esc(active.binding.scope):'当前范围未启用'}</div><div class="wb-actions">${btn('binding','配置范围',p.id)}${btn('update','检查更新',p.id)}${p.history?.length?btn('rollback','回滚',p.id):''}${btn('uninstall','卸载',p.id,'wb-danger')}</div></article>`;
    }).join('')}</div>${catalog.extensions?.length?'':empty('还没有扩展。可从本地目录、ZIP 或 HTTPS Git 仓库添加。')}`;
  }
  async function renderMarketplace() {
    const serial=refreshSerial;const data=await api('/marketplace');if(serial!==refreshSerial||tab!=='marketplace')return;catalog.marketplace=data;
    q('wbBody').innerHTML=`<div class="wb-toolbar"><h3>扩展目录</h3>${btn('source-form','添加目录来源')}</div>${note('内置示例可离线预览；自定义目录只提供安装来源，安装与启用仍需分别操作。评分保存在本机。')}<div class="wb-grid">${data.plugins.map(p=>`<article class="wb-card"><h4>${esc(p.name)}</h4><small>${esc(p.id)} · ${p.builtin?'CCW Desk 内置':esc(p.catalog_id)}</small><p>${esc(p.description)}</p><label class="wb-muted">本机评分 <select data-wb-rating="${esc(p.id)}">${[0,1,2,3,4,5].map(n=>`<option value="${n}" ${p.local_rating===n?'selected':''}>${n?n+' / 5':'未评分'}</option>`).join('')}</select></label><div class="wb-actions">${btn('market-preview','预览安装',p.id)}</div></article>`).join('')}</div><h3 style="margin:22px 0 12px">自定义来源</h3>${data.sources.map(source=>`<div class="wb-card"><h4>${esc(source.name)}</h4><p>${esc(source.url)}</p>${btn('delete-source','移除来源',source.id,'wb-danger')}</div>`).join('')||empty('可添加 HTTPS JSON 目录，内含插件名称、说明和 Git 来源。')}`;
  }
  function installForm() {
    q('wbEditor').innerHTML=editor('添加扩展',select('wbSourceType','来源','<option value="local">本地目录</option><option value="git">Git / GitHub 仓库</option><option value="zip">本地 ZIP 路径</option>')+field('wbSource','目录路径或 HTTPS 仓库地址')+field('wbSubdir','仓库内插件子目录（可选）')+filePicker('wbZip','选择 ZIP 文件','.zip','可选，最大 32 MB；上传文件优先于上方路径。选择后点击“预览内容”。'),'preview');
    q('wbForm').addEventListener('submit',e=>e.preventDefault());
    q('wbForm').querySelector('[data-wb-action=preview]').textContent='预览内容';
  }
  function previewForm(data) {
    preview=data;
    q('wbEditor').innerHTML=editor('确认安装 · '+data.name,`<p class="wb-muted">${esc(data.version)} · ${data.files} 个文件 · ${(data.size/1024).toFixed(1)} KB</p><p class="wb-muted">${esc(data.description)}</p><div class="wb-tags">${Object.entries(data.inventory).map(([k,v])=>`<span class="wb-tag">${components[k]}：${v.length}</span>`).join('')}</div><pre class="wb-code">${esc(Object.entries(data.inventory).map(([k,v])=>k+'\n'+v.join('\n')).join('\n\n'))}</pre>${note('安装只保存扩展文件，之后在“配置范围”中选择需要启用的组件。')}`,'install-confirm');
  }
  function bindingForm(id) {
    const p=catalog.extensions.find(x=>x.id===id), active=catalog.effective.find(x=>x.id===id)?.binding;
    q('wbEditor').innerHTML=editor('扩展范围 · '+p.name,select('wbScope','作用范围','<option value="global">全局</option><option value="project">当前项目</option><option value="session">当前会话</option>')+`<div class="wb-checks"><label><input id="wbEnabled" type="checkbox" ${active?.enabled?'checked':''}>启用此扩展</label></div><div class="wb-checks">${Object.keys(components).map(k=>`<label><input data-component="${k}" type="checkbox" ${(active?.components?.[k]??['skills','agents','commands','workflows'].includes(k))?'checked':''} ${p.inventory[k]?.length?'':'disabled'}>${components[k]} (${p.inventory[k]?.length||0})</label>`).join('')}</div>`,'save-binding');
    q('wbForm').dataset.id=id;
    q('wbScope').addEventListener('change',()=>{
      const scope=q('wbScope').value;
      const key=scope==='global'?'global':scope==='project'?'project:'+(catalog.scope_context?.project||cwd()):'session:'+sid();
      const binding=catalog.bindings.find(b=>b.plugin_id===id&&b.scope===key);
      q('wbEnabled').checked=binding?.enabled??false;
      q('wbEditor').querySelectorAll('[data-component]').forEach(c=>{c.checked=binding?.components?.[c.dataset.component]??['skills','agents','commands','workflows'].includes(c.dataset.component);});
    });
    q('wbScope').dispatchEvent(new Event('change'));
  }
  function renderConfigs() {
    const kind=tab;
    q('wbBody').innerHTML=`<div class="wb-toolbar"><h3>${labels[kind]}</h3>${btn('new-config','新建','','wb-primary')}</div>${note(kind==='profiles'?'保存模式、模型、权限、系统提示词和扩展组合；应用时会新建会话。':kind==='commands'?'选择命令后，下一条消息作为 {{input}} 传入。命令可以附带工作模式和配置。':'步骤按顺序执行；本地命令会停在任务中心等待审批，失败后可以继续。')}<div class="wb-grid">${(catalog[kind]||[]).map(r=>`<article class="wb-card"><h4>${esc(r.name)}</h4><small>${esc(r.id)}</small><p>${esc(kind==='workflows'?r.steps.map(s=>s.name).join(' → '):(r.mode+' · '+(r.model||'默认模型')))}</p><div class="wb-actions">${btn('edit-config',r.extension_id?'另存为':'编辑',r.id)}${btn(kind==='workflows'?'run-workflow':'apply-config',kind==='workflows'?'运行':'应用',r.id)}${r.extension_id?'':btn('delete-config','删除',r.id,'wb-danger')}</div></article>`).join('')}</div>${catalog[kind]?.length?'':empty('尚无配置，点击“新建”开始。')}`;
  }
  function configForm(id='') {
    const kind=tab,row=(catalog[kind]||[]).find(x=>x.id===id)||{};
    let html=field('wbName','名称',row.name||'')+field('wbId','标识（小写英文、数字或连字符）',row.extension_id?'':row.id||'');
    if(kind==='workflows') html+=select('wbProfile','使用配置档',options(catalog.profiles,row.profile_id))+area('wbSteps','步骤 JSON',JSON.stringify(row.steps||[{type:'prompt',name:'分析任务',value:'分析以下任务并给出实施方案：{{input}}'},{type:'report',name:'生成报告',value:''}],null,2),13);
    else html+=`<div class="wb-form-row">${select('wbMode','工作模式',modes(row.mode||'code'))}${select('wbProvider','模型服务',options(catalog.providers,row.provider_id,'Claude 原生'))}</div>`+field('wbCwd','默认项目目录（可选）',row.cwd||'')+`<div class="wb-form-row">${field('wbModel','模型 ID（留空使用默认）',row.model||'')}${select('wbEffort','推理强度',['low','medium','high','max'].map(x=>`<option ${x===(row.effort||'high')?'selected':''}>${x}</option>`).join(''))}</div>`+select('wbPermission','权限模式',[['default','逐次确认'],['acceptEdits','接受文件编辑'],['plan','规划'],['bypassPermissions','自动执行']].map(([value,label])=>`<option value="${value}" ${value===(row.permission_mode||'default')?'selected':''}>${label}</option>`).join(''))+area('wbSystemPrompt','系统提示词',row.system_prompt||'',4)+`<fieldset><legend>扩展组合（仅使用当前范围已启用的扩展）</legend><div id="wbExtensions" class="wb-checks">${catalog.extensions.map(x=>`<label><input type="checkbox" value="${esc(x.id)}" ${row.extension_ids?.includes(x.id)?'checked':''}>${esc(x.name)}</label>`).join('')||'<p class="wb-muted">尚未安装扩展，可先保存配置，稍后添加。</p>'}</div></fieldset>`+(kind==='commands'?area('wbPrompt','命令内容（{{input}} 为输入内容）',row.prompt||'',5):'');
    q('wbEditor').innerHTML=editor(id?'编辑'+labels[kind]:'新建'+labels[kind],html,'save-config');q('wbForm').dataset.id=id;if(id&&!row.extension_id)q('wbId').readOnly=true;
  }
  async function applyConfig(id,kind=tab) {
    const row=catalog[kind].find(x=>x.id===id);
    if(!prepareMode(row.mode,true))return;
    if(row.cwd) q('cwdInput').value=row.cwd;
    selectedModel=row.model||'';modelSelect.value=selectedModel;LS.set('model',selectedModel);
    selectedEffort=row.effort||'high';LS.set('effort',selectedEffort);
    if(!readOnlyModes.has(row.mode))setPermissionMode(row.permission_mode||'default');
    updateSettingsLabel();syncSettingsMenuChecks();
    selection={work_mode:row.mode,[kind==='profiles'?'profile_id':'command_id']:id};
    syncWorkMode();localStorage.setItem('ccw-work-mode',row.mode);
    dirty=false;toast('已应用 '+row.name+'，下一条消息使用此配置');q('input').focus();
  }
  async function renderTasks() {
    const serial=refreshSerial;const data=await api('/tasks');if(serial!==refreshSerial||tab!=='tasks')return;
    const statuses={queued:'排队中',running:'运行中',waiting_approval:'等待命令审批',waiting_evidence:'等待浏览器证据',waiting_permission:'等待工具审批',completed:'已完成',failed:'失败',cancelled:'已停止',interrupted:'已中断'};
    q('wbBody').innerHTML=`<div class="wb-toolbar"><h3>任务中心</h3>${btn('refresh','刷新')}</div><div class="wb-grid">${data.tasks.map(t=>`<article class="wb-card"><h4>${esc(t.name)}</h4><div class="wb-status" data-status="${esc(t.status)}">${statuses[t.status]||esc(t.status)} · ${t.next_step}/${t.workflow.steps.length}</div><p>${esc(t.current_step||t.cwd)}</p><small>${(t.usage?.input_tokens||0)+(t.usage?.output_tokens||0)} tokens · ${t.changed_file_count||0} 个文件改动</small>${t.error?`<p>${esc(t.error)}</p>`:''}${t.pending_command?`<pre class="wb-code">${esc(t.pending_command)}</pre>`:''}<div class="wb-actions">${t.status==='waiting_approval'?btn('approve-task','批准命令',t.id):''}${['queued','running','waiting_approval','waiting_evidence','waiting_permission'].includes(t.status)?btn('cancel-task','停止',t.id):''}${['failed','cancelled','interrupted'].includes(t.status)?btn('resume-task','从当前步骤继续',t.id):''}${btn('task-detail','查看结果',t.id)}${btn('open-session','打开任务会话',t.session_id)}${t.browser_run?btn('open-session','打开验收会话',t.browser_run.session_id):''}<a class="wb-button" href="/api/workbench/tasks/${encodeURIComponent(t.id)}/report" download>报告</a></div></article>`).join('')}</div>${data.tasks.length?'':empty('暂无工作流任务。可在“工作流”中创建并运行。')}<h3 style="margin:20px 0 10px">其他运行中的会话</h3><div class="wb-grid">${data.sessions.map(s=>`<article class="wb-card"><h4>${esc(s.name)}</h4><p>${esc(s.cwd)}</p>${btn('open-session','打开会话',s.session_id)}</article>`).join('')}</div>`;
  }
  async function testModel(button,provider,model='') {
    const status=button.closest('.wb-actions')?.nextElementSibling;
    if(!status)return;
    status.classList.remove('hidden','wb-probe-error');status.textContent='正在测试推理…';
    try {
      if(!provider)throw new Error('先选择兼容服务。Claude 原生请通过新会话验证。');
      const result=await post('/providers/'+encodeURIComponent(provider)+'/test',{model});
      status.textContent='推理验证通过 · '+result.model+' · 返回：'+result.reply+'。不会更改当前会话。';
    } catch(err) {status.classList.add('wb-probe-error');status.textContent=err.message;}
  }
  const probeStatus='<p class="wb-probe-status hidden" role="status"></p>';
  function renderProviders() {
    q('wbBody').innerHTML=`<div class="wb-toolbar"><h3>模型服务</h3>${btn('new-provider','添加服务','','wb-primary')}</div>${note('OpenAI 兼容接口用于 Chat 文本会话；Anthropic 兼容服务可接入代码和工具运行时。工具能力取决于服务兼容程度。API Key 加密保存在本机。测试推理会使用所选模型执行一次短请求，可能产生少量费用。')}<p id="wbSessionModel" class="wb-note" role="status"></p><div class="wb-grid">${catalog.providers.map(p=>`<article class="wb-card"><h4>${esc(p.name)}</h4><p>${esc(p.base_url)}<br>${esc(p.model)} · ${esc(p.protocol||'openai')} · ${p.has_key?'已保存凭据':'无凭据'}</p><div class="wb-actions">${btn('edit-provider','编辑',p.id)}${btn('test-provider','测试推理',p.id)}${btn('delete-provider','删除',p.id,'wb-danger')}</div>${probeStatus}</article>`).join('')}</div><div class="wb-toolbar" style="margin-top:24px"><h3>按工作模式路由</h3></div>${note('路由在新会话生效；已有会话保留绑定的服务和模型。第三方模型使用此处或服务中配置的模型 ID，不使用聊天框的 Claude 模型别名。')}<div id="wbRouteForm" class="wb-form">${select('wbRouteMode','模式',modes('chat'))}${select('wbRouteProvider','服务',options(catalog.providers,'','Claude 原生'))}${field('wbRouteModel','默认模型 ID（可选）')}<div class="wb-actions">${btn('save-route','保存路由','','wb-primary')}${btn('test-route','测试所选模型')}</div>${probeStatus}</div>`;
    updateSessionModel();
    q('wbRouteForm').addEventListener('input',()=>q('wbRouteForm').querySelector('.wb-probe-status').classList.add('hidden'));
    q('wbRouteMode').dataset.selected='chat';q('wbRouteMode').addEventListener('change',()=>{const mode=q('wbRouteMode');if(!discard()){mode.value=mode.dataset.selected;return;}dirty=false;mode.dataset.selected=mode.value;q('wbRouteForm').querySelector('.wb-probe-status').classList.add('hidden');const r=catalog.routes.find(r=>r.id===mode.value);q('wbRouteProvider').value=r?.provider_id||'';q('wbRouteModel').value=r?.model||'';});q('wbRouteMode').dispatchEvent(new Event('change'));
  }
  function providerForm(id='') {
    const row=catalog.providers.find(x=>x.id===id)||{};
    q('wbEditor').innerHTML=editor('兼容模型服务',field('wbName','服务名称',row.name||'')+field('wbId','标识',row.id||'')+select('wbProtocol','接口协议',`<option value="openai" ${row.protocol==='anthropic'?'':'selected'}>OpenAI 兼容（Chat）</option><option value="anthropic" ${row.protocol==='anthropic'?'selected':''}>Anthropic 兼容（代码 / 工具）</option>`)+field('wbBaseUrl','API Base URL（例如 https://api.deepseek.com/v1）',row.base_url||'')+field('wbModel','模型 ID',row.model||'')+field('wbApiKey',row.has_key?'API Key（留空保留已保存凭据）':'API Key','','password'),'save-provider');if(id)q('wbId').readOnly=true;
  }
  function renderSharing() {
    q('wbBody').innerHTML=`<div class="wb-toolbar"><h3>共享工作区配置</h3></div>${note('导出配置档、快捷命令、工作流及扩展来源。API Key、MCP 环境变量和凭据不参与导出；扩展需要在目标电脑重新安装和启用。')}<div class="wb-share-grid"><section class="wb-card"><h4>导出配置</h4><p>备份当前配置，或分享给其他工作区。</p><div class="wb-actions"><a href="/api/workbench/export" download class="wb-button">导出 JSON</a><a href="/api/workbench/export.zip" download class="wb-button">导出 ZIP</a></div></section><section class="wb-card"><h4>导入配置</h4><p>先预览新增与覆盖的配置，确认后才写入。</p>${filePicker('wbImportFile','选择配置文件','.json,.zip','JSON 最大 2 MB，ZIP 最大 32 MB。请仅导入可信来源的配置。')}</section></div><section id="wbImportPreview" class="wb-card wb-import-preview" tabindex="-1" aria-label="配置导入预览" hidden></section><div class="wb-editor" style="margin-top:22px"><h3>扩展目录来源</h3><p class="wb-muted">可安装自己的 HTTPS Git 仓库和本地扩展。现有 Skills 市场仍在“配置中心 → Skills”中使用。</p><div class="wb-actions">${btn('install-form','添加自定义扩展')}</div></div>`;
    q('ccwWorkbench').querySelector('[data-wb-pick="wbImportFile"]').classList.add('wb-primary');
  }
  async function action(name,id,button) {
    q('wbError').classList.add('hidden');
    switch(name) {
      case 'refresh':return refresh();
      case 'install-form':return installForm();
      case 'market-preview': {const entry=catalog.marketplace.plugins.find(p=>p.id===id);return previewForm(await post('/extensions/preview',entry));}
      case 'source-form':q('wbEditor').innerHTML=editor('添加扩展目录',field('wbName','目录名称')+field('wbSourceUrl','HTTPS JSON 地址'),'save-source');return;
      case 'save-source':await put('/marketplace/sources',{name:q('wbName').value,url:q('wbSourceUrl').value});return refresh();
      case 'delete-source':if(!confirm('移除此目录来源？已安装扩展将保留。'))return;await api('/marketplace/sources/'+id,{method:'DELETE'});return refresh();
      case 'close-editor':if(!discard())return;dirty=false;q('wbEditor').innerHTML='';return;
      case 'preview': {const file=q('wbZip').files[0];let data;if(file){if(file.size>32*1024*1024)throw new Error('扩展包超过 32 MB');const form=new FormData();form.append('file',file);data=await api('/extensions/upload',{method:'POST',body:form});}else {if(!q('wbSource').value.trim())throw new Error('请输入扩展目录或 HTTPS 仓库地址，或选择 ZIP 文件');data=await post('/extensions/preview',{source_type:q('wbSourceType').value,source:q('wbSource').value.trim(),subdir:q('wbSubdir').value.trim()});}return previewForm(data);}
      case 'install-confirm':await post('/extensions/install',{preview_id:preview.preview_id});return refresh();
      case 'binding':return bindingForm(id);
      case 'save-binding':await post('/extensions/'+q('wbForm').dataset.id+'/binding',{scope:q('wbScope').value,cwd:cwd(),session_id:sid(),enabled:q('wbEnabled').checked,components:Object.fromEntries([...q('wbEditor').querySelectorAll('[data-component]')].map(x=>[x.dataset.component,x.checked&&!x.disabled]))});return refresh();
      case 'update':return previewForm(await post('/extensions/'+encodeURIComponent(id)+'/update'));
      case 'rollback':await post('/extensions/'+encodeURIComponent(id)+'/rollback');return refresh();
      case 'uninstall':if(!confirm('卸载此扩展？配置范围将同时移除。'))return;await api('/extensions/'+encodeURIComponent(id),{method:'DELETE'});return refresh();
      case 'new-config':return configForm();
      case 'edit-config':return configForm(id);
      case 'save-config': {let data={id:q('wbId').value.trim()||undefined,name:q('wbName').value.trim()};if(tab==='workflows')Object.assign(data,{steps:JSON.parse(q('wbSteps').value),profile_id:q('wbProfile').value});else Object.assign(data,{mode:q('wbMode').value,model:q('wbModel').value,effort:q('wbEffort').value,permission_mode:q('wbPermission').value,system_prompt:q('wbSystemPrompt').value,extension_ids:selectedIds('wbExtensions'),provider_id:q('wbProvider').value,cwd:q('wbCwd').value,...(tab==='commands'?{prompt:q('wbPrompt').value}:{})});await put('/configs/'+tab,data);return refresh();}
      case 'delete-config':if(!confirm('删除此配置？'))return;await api('/configs/'+tab+'/'+encodeURIComponent(id),{method:'DELETE'});return refresh();
      case 'apply-config':return applyConfig(id);
      case 'run-workflow': {const input=prompt('任务输入（替换工作流里的 {{input}}）','');if(input===null)return;await post('/tasks',{workflow_id:id,cwd:cwd(),session_id:sid(),input});tab='tasks';return refresh();}
      case 'approve-task':case 'cancel-task':case 'resume-task':await post('/tasks/'+id+'/'+name.split('-')[0]);return refresh();
      case 'task-detail': {const task=await api('/tasks/'+encodeURIComponent(id));q('wbEditor').innerHTML=`<section class="wb-editor"><h3>${esc(task.name)}</h3><pre class="wb-code">${esc(JSON.stringify(task.results,null,2))}</pre>${btn('close-editor','关闭')}</section>`;return;}
      case 'open-session':{const response=await fetch('/api/sessions/'+encodeURIComponent(id));if(!response.ok)throw new Error('任务会话尚未创建，等待第一个 Agent 步骤后重试');const data=await response.json();dirty=false;leave({force:true});return openSessionInWorkspace(data);}
      case 'new-provider':return providerForm();
      case 'edit-provider':return providerForm(id);
      case 'save-provider':await put('/providers',{id:q('wbId').value.trim()||undefined,name:q('wbName').value,protocol:q('wbProtocol').value,base_url:q('wbBaseUrl').value,model:q('wbModel').value,'api_key':q('wbApiKey').value});return refresh();
      case 'test-provider':return testModel(button,id);
      case 'delete-provider':if(!confirm('删除此模型服务？'))return;await api('/providers/'+id,{method:'DELETE'});return refresh();
      case 'test-route':return testModel(button,q('wbRouteProvider').value,q('wbRouteModel').value.trim());
      case 'save-route':await put('/routes/'+q('wbRouteMode').value,{provider_id:q('wbRouteProvider').value,model:q('wbRouteModel').value});dirty=false;toast('默认路由已保存 · 仅新会话生效');return;
      case 'cancel-import':clearImport();return;
      case 'confirm-import': {
        if(!importDraft)return;
        const {path,body,revision,name}=importDraft;
        try {
          const result=await api(path,{method:'POST',body,headers:{'X-Workbench-Import-Revision':revision}});
          clearImport();const message='已导入 '+result.imported+' 项配置';q('wbImportFileStatus').textContent=message+' · '+name;toast(message);
        } catch(err) {clearImport();q('wbImportFileStatus').textContent='未导入 · 请重新选择文件';throw err;}
        return;
      }
    }
  }
  function init() {
    const launch=q('configCenterBtn');if(!launch)return;
    const button=document.createElement('button');button.type='button';button.className='cw-more-tool-item wb-launch';button.id='ccwWorkbenchBtn';button.setAttribute('role','menuitem');button.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 17.5h7m-3.5-3.5v7"/></svg><span>扩展与工作台</span>';launch.before(button);
    q('mainPane').insertAdjacentHTML('beforeend',`<main id="ccwWorkbench" class="wb-box hidden" aria-labelledby="wbTitle"><header class="wb-header"><button id="wbSidebar" class="wb-button wb-sidebar" type="button" aria-label="打开主导航" aria-controls="sidebar"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button><div><h1 id="wbTitle" tabindex="-1">扩展与工作台</h1><p>组合工具、角色与流程，创建自己的工作方式</p></div><button id="wbClose" class="wb-button" type="button">返回会话</button></header><div class="wb-layout"><label class="wb-mobile-nav">工作台栏目<select id="wbSectionSelect" aria-controls="wbPanel">${Object.entries(labels).map(([id,label])=>`<option value="${id}">${label}</option>`).join('')}</select></label><nav id="wbNav" class="wb-nav" role="tablist" aria-label="工作台分类"></nav><div id="wbPanel" class="wb-content" role="tabpanel" tabindex="0"><div id="wbError" class="wb-error hidden" role="alert"></div><div id="wbEditor"></div><div id="wbBody"></div></div></div></main>`);
    q('ccwWorkbench').classList.add('hidden');
    [button,q('cwWorkbenchNav')].forEach(entry=>entry?.addEventListener('click',()=>open().catch(error)));
    q('wbClose').addEventListener('click',()=>leave({focus:true}));
    q('wbSidebar').addEventListener('click',()=>setSidebarOpen(true));
    q('ccwWorkbench').addEventListener('input',event=>{if((event.target.closest('#wbEditor')||(event.target.closest('#wbRouteForm')&&event.target.id!=='wbRouteMode')))dirty=true;});
    window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
    function switchTab(next) {
      if(actionPending||next===tab||!Object.hasOwn(labels,next)||!discard()){q('wbSectionSelect').value=tab;return false;}
      tab=next;refresh().catch(error);return true;
    }
    q('wbSectionSelect').addEventListener('change',event=>switchTab(event.target.value));
    q('wbNav').addEventListener('click',e=>{const target=e.target.closest('[data-wb-tab]');if(target&&switchTab(target.dataset.wbTab))q('wbTab-'+tab)?.focus();});
    q('wbNav').addEventListener('keydown',event=>{if(actionPending)return;const keys=Object.keys(labels),index=keys.indexOf(tab);let next;if(event.key==='ArrowRight'||event.key==='ArrowDown')next=(index+1)%keys.length;else if(event.key==='ArrowLeft'||event.key==='ArrowUp')next=(index+keys.length-1)%keys.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=keys.length-1;else return;event.preventDefault();if(switchTab(keys[next]))q('wbTab-'+tab)?.focus();});
    q('ccwWorkbench').addEventListener('click',async e=>{
      if(actionPending)return;
      const picker=e.target.closest('[data-wb-pick]');if(picker){q(picker.dataset.wbPick).click();return;}
      const clear=e.target.closest('[data-wb-clear]');if(clear){const input=q(clear.dataset.wbClear);input.value='';fileLabel(input);dirty=true;return;}
      const target=e.target.closest('[data-wb-action]');if(!target)return;const name=target.dataset.wbAction;
      if(dirty&&['new-config','edit-config','install-form','binding','source-form','new-provider','edit-provider','task-detail','market-preview','update'].includes(name)&&!discard())return;
      try{await pending(target,async()=>{await action(name,target.dataset.id,target);if(q('wbEditor').children.length&&['new-config','edit-config','install-form','binding','source-form','new-provider','edit-provider','market-preview','update','preview','task-detail'].includes(name)){dirty=false;q('wbEditor').scrollIntoView({block:'start'});}},!['new-config','edit-config','install-form','binding','source-form','new-provider','edit-provider','close-editor','cancel-import'].includes(name));}
      catch(err){error(err);return;}
      if(name==='cancel-import'||name==='confirm-import')q('ccwWorkbench').querySelector('[data-wb-pick="wbImportFile"]').focus();
    });
    q('ccwWorkbench').addEventListener('submit',e=>e.preventDefault());
    q('ccwWorkbench').addEventListener('change',async e=>{
      if((e.target.closest('#wbEditor')||(e.target.closest('#wbRouteForm')&&e.target.id!=='wbRouteMode')))dirty=true;
      if(e.target.dataset.wbRating){try{await put('/marketplace/ratings/'+e.target.dataset.wbRating,{stars:Number(e.target.value)});}catch(err){error(err);}}
      if(e.target.id==='wbZip')fileLabel(e.target);
      if(e.target.id==='wbImportFile')await importFile(e.target);
    });
    const mode=document.createElement('select');mode.id='wbModeSelect';mode.className='wb-inline-mode';q('cwComposerContextMenu').append(mode);
    function renderModePicker() {
      mode.setAttribute('aria-label',t('work_mode.choose'));
      mode.innerHTML=workModes.map(id=>`<option value="${id}">${esc(t('work_mode.'+id))}</option>`).join('');
      renderWorkMode();
    }
    mode.addEventListener('change',()=>{
      const value=mode.value;
      if(!workModes.includes(value)||value===activeMode()||!prepareMode(value)){renderWorkMode();return;}
      selection={work_mode:value};localStorage.setItem('ccw-work-mode',value);syncWorkMode();
      if(!codeMode)q('input').focus();
    });
    q('wbEnableWebSearch').addEventListener('click',()=>{
      if(activeMode()!=='research'||isWebSearchEnabled())return;
      setWebSearchEnabled(true);q('webSearchBtn').focus();
    });
    renderModePicker();window.addEventListener('cwLangChanged',()=>{renderModePicker();applyWebSearchBtnState();});
    function resetSession() {
      ++selectionSerial;
      sessionModel=null;
      selection={work_mode:codeMode?'code':'chat'};
      syncWorkMode();updateSessionModel('');
    }
    function requestFields(target=sid(),workspace=codeMode?'code':'chat') {
      const state=target&&target!==sid()?(sessionSelections.get(target)||{}):selection;
      const out={...state};
      if(out.extension_ids)out.extension_ids=[...out.extension_ids];
      if(!out.profile_id&&!out.command_id&&out.work_mode){
        out.work_mode=workspace==='code'?(out.work_mode==='chat'?'code':out.work_mode):'chat';
      }
      return out;
    }
    window.ccwWorkbench={open,leave,resetSession,requestFields,observeSessionModel,isReadOnlyMode,renderWorkMode,
      slashItems:async(query)=>{const data=await api('/commands?cwd='+encodeURIComponent(cwd())+'&session_id='+encodeURIComponent(sid())).catch(()=>({commands:[]}));return data.commands.filter(c=>(!query||c.id.includes(query)||c.name.includes(query))&&(!c.native||codeMode)).map(c=>({group:'CCW Desk',kind:c.native?'template':'action',trigger:c.id,label:'/'+c.id,name:c.name,meta:c.name+' · '+c.mode,content:'/'+c.id+' ',handler:async()=>{await load();await applyConfig(c.id,'commands');}}));},
      bindSession:()=>{delete selection.command_id;delete selection.profile_id;sessionSelections.set(sid(),{...selection});},
      restoreSession:async(id, events=[])=>{
        const serial=++selectionSerial;
        if(sessionModel?.id!==id)updateSessionModel(id);
        const data=await api('/selection/'+encodeURIComponent(id)).catch(()=>({}));
        if(serial!==selectionSerial||sid()!==id)return;
        selection={work_mode:data.mode||'',provider_id:data.provider_id||undefined,extension_ids:data.extension_ids};
        sessionSelections.set(id,{...selection});syncWorkMode();
        if(data.session_model)sessionModel={id,info:data.session_model};
        else if(sessionModel?.id!==id) {
          for(let i=events.length-1;i>=0;i--) {
            observeSessionModel(id,events[i]);
            if(sessionModel?.id===id)break;
          }
        }
        updateSessionModel();
      },
    };

  }
  init();
})();
