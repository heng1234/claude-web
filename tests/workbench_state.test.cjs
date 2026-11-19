const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

function setup() {
  const elements = new Map();
  class Element {
    constructor(id='') {
      this.id=id;this.value='';this.dataset={};this.children=[];this.files=[];this.disabled=false;this.hidden=false;
      this.listeners={};this.attributes={};this.controls=[];this.clicks=0;this._html='';
      const classes=new Set();this.classList={add(name){classes.add(name);},remove(name){classes.delete(name);},contains(name){return classes.has(name);},toggle(name,force){const value=force??!classes.has(name);if(value)classes.add(name);else classes.delete(name);return value;}};
    }
    get innerHTML(){return this._html;} set innerHTML(value){this._html=value;}
    get textContent(){return this._html.replace(/<[^>]*>/g,'');} set textContent(value){this._html=value;}
    setAttribute(name,value){this.attributes[name]=value;} getAttribute(name){return this.attributes[name]??null;} removeAttribute(name){delete this.attributes[name];}
    addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);}
    async dispatch(type,target=this){for(const listener of this.listeners[type]||[])await listener({target});}
    closest(selector){if(selector==='#'+this.id)return this;const key={'[data-wb-pick]':'wbPick','[data-wb-clear]':'wbClear','[data-wb-action]':'wbAction'}[selector];return key&&this.dataset[key]!==undefined?this:null;}
    querySelector(selector){return get(selector);}
    querySelectorAll(){return this.controls.filter(el=>!el.disabled);}
    click(){this.clicks++;} focus(){} scrollIntoView(){} before(){} insertAdjacentHTML(position,html){this._html+=html;} append(el){if(el.id)elements.set(el.id,el);}
  }
  function get(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);}
  const document={getElementById:get,createElement(){return new Element();},body:new Element(),addEventListener(){}};
  const context={document,localStorage:{getItem(){return null;},setItem(){}},codeMode:false,sessionId:'session-a',console,
    FormData:class{constructor(){this.items=[];}append(...item){this.items.push(item);}},
    setInterval(){return 1;},clearInterval(){},addEventListener(){},openModal(){throw new Error('workbench must not open a modal');},closeModal(){throw new Error('workbench must not close a modal');},toast(){},confirm(){return true;},
    cliView:false,archivedView:false,searchInput:get('searchInput'),cwChatNav:get('cwChatNav'),cwSearchNav:get('cwSearchNav'),cwRecordsNav:get('cwRecordsNav'),cwCliNav:get('cwCliNav'),cwCodeNav:get('cwCodeNav'),sessionSectionTitle:get('sessionSectionTitle'),
    $:get,setText(){},sidebarSectionLabel(){return 'Recent';},
    currentController:null,agentLoopRunning:false,pendingImages:[],pendingDocs:[],
    activeImageUploadPromises:new Set(),activeDocUploadPromises:new Set(),codeContextPacks:new Map(),
    saveDraft(){},rememberWorkspaceSession(){},updateWorkspaceLabels(){},setPermissionModeMenuOpen(){},
    applyWebSearchBtnState(){context.ccwWorkbench?.renderWorkMode();},
    t(key){return key;},isWebSearchEnabled(){return webOn;},setWebSearchEnabled(value){webOn=value;context.ccwWorkbench?.renderWorkMode?.();}};
  let webOn=false;
  context.setCodeMode=value=>{context.codeMode=value;};
  context.startNewSession=()=>{context.ccwWorkbench.resetSession();context.sessionId=null;};
  context.window=context;
  context.fetch=async()=>({ok:true,json:async()=>({mode:'chat',provider_id:'provider-a',extension_ids:['plugin-a'],extensions:[],effective:[],modes:{},profiles:[],commands:[],workflows:[],providers:[],routes:[]})});
  vm.createContext(context);
  const shell=fs.readFileSync('claude_web/static/index.html','utf8');
  vm.runInContext(shell.match(/function updateShellNav\(active\) \{[\s\S]*?\n\}/)[0],context);
  vm.runInContext(fs.readFileSync('claude_web/static/workbench.js','utf8'),context);
  return context;
}

test('new sessions cannot inherit a restored provider or extension selection',async()=>{
  const page=setup();
  await page.ccwWorkbench.restoreSession('session-a');
  assert.equal(page.ccwWorkbench.requestFields().provider_id,'provider-a');
  page.ccwWorkbench.resetSession();page.sessionId=null;
  const fields=page.ccwWorkbench.requestFields();
  assert.equal(fields.provider_id,undefined);
  assert.equal(fields.extension_ids,undefined);
});

test('a stale restore response cannot overwrite a new draft',async()=>{
  const page=setup();let reply;
  page.fetch=()=>new Promise(resolve=>reply=resolve);
  const restoring=page.ccwWorkbench.restoreSession('session-a');
  page.ccwWorkbench.resetSession();page.sessionId=null;
  reply({ok:true,json:async()=>({mode:'review',provider_id:'stale'})});
  await restoring;
  assert.equal(page.ccwWorkbench.requestFields().provider_id,undefined);
  assert.equal(page.ccwWorkbench.requestFields().work_mode,'chat');
});

test('background sends use their owning session configuration',async()=>{
  const page=setup();
  await page.ccwWorkbench.restoreSession('session-a');
  page.ccwWorkbench.resetSession();page.sessionId='session-b';
  assert.equal(page.ccwWorkbench.requestFields('session-a','chat').provider_id,'provider-a');
  assert.equal(page.ccwWorkbench.requestFields('unknown','code').provider_id,undefined);
  assert.equal(page.ccwWorkbench.requestFields('unknown','code').work_mode,undefined);
});

test('request snapshots do not share the live extension array',async()=>{
  const page=setup();await page.ccwWorkbench.restoreSession('session-a');
  const fields=page.ccwWorkbench.requestFields();fields.extension_ids.push('other');
  assert.equal(page.ccwWorkbench.requestFields().extension_ids.length,1);
});

function importControls(page,name='workspace.json',text='{"format":"ccw-desk-workspace"}',size=200) {
  const get=id=>page.document.getElementById(id),modal=get('ccwWorkbench');
  const input=get('wbImportFile');input.files=[{name,size,text:async()=>text}];input.value=name;
  const button=modal.querySelector('[data-wb-pick="wbImportFile"]');button.dataset.wbPick='wbImportFile';button.innerHTML='选择配置文件';
  const enabled=get('enabled-control'),disabled=get('disabled-control');disabled.disabled=true;
  modal.controls=[button,input,enabled,disabled,get('wbClose')];
  return {modal,input,button,enabled,disabled,get};
}

test('sharing renders styled downloads and a single hidden native file input',async()=>{
  const page=setup();await page.ccwWorkbench.open('sharing');
  const html=page.document.getElementById('wbBody').innerHTML;
  assert.match(html,/export" download class="wb-button"/);
  assert.match(html,/export.zip" download class="wb-button"/);
  assert.match(html,/id="wbImportFile"[^>]*hidden/);
  assert.equal((html.match(/type="file"/g)||[]).length,1);
  assert.match(html,/data-wb-pick="wbImportFile"/);
});

test('custom file selection opens the native picker without a data mutation',async()=>{
  const page=setup(),{modal,input,button}=importControls(page);
  let requests=0;page.fetch=async()=>{requests++;throw new Error('unexpected request');};
  await modal.dispatch('click',button);
  assert.equal(input.clicks,1);assert.equal(requests,0);
});

const reviewItems=[{kind:'profiles',id:'review',name:'Review <test>',status:'replace'},{kind:'commands',id:'check',name:'Check',status:'new'}];
const response=data=>({ok:true,json:async()=>data});

function importAction(get,name){const button=get(name);button.dataset.wbAction=name;return button;}

test('JSON selection only previews; explicit confirmation submits the reviewed file once',async()=>{
  const page=setup();await page.ccwWorkbench.open('sharing');
  const {modal,input,button,enabled,disabled,get}=importControls(page);
  const originalFetch=page.fetch;let finish,entered;const started=new Promise(resolve=>entered=resolve);const requests=[];
  page.fetch=(url,init)=>{
    if(url.endsWith('/import?preview=true')) {requests.push('preview');return Promise.resolve(response({items:reviewItems,revision:'revision-1'}));}
    if(url.endsWith('/import')) {
      requests.push('commit');assert.equal(init.method,'POST');assert.equal(JSON.parse(init.body).format,'ccw-desk-workspace');
      assert.equal(init.headers['X-Workbench-Import-Revision'],'revision-1');entered();return new Promise(resolve=>finish=resolve);
    }
    return originalFetch(url,init);
  };
  await modal.dispatch('change',input);
  assert.deepEqual(requests,['preview']);assert.equal(input.value,'');assert.match(get('wbImportFileStatus').textContent,/尚未导入/);
  assert.match(get('wbImportPreview').innerHTML,/将覆盖.*Review &lt;test&gt;/);assert.match(get('wbImportPreview').innerHTML,/将新增.*Check/);
  const confirm=importAction(get,'confirm-import');modal.controls.push(confirm);
  const importing=modal.dispatch('click',confirm);await started;
  assert.equal(confirm.attributes['aria-busy'],'true');assert.equal(enabled.disabled,true);
  await modal.dispatch('click',confirm);assert.deepEqual(requests,['preview','commit']);
  finish(response({imported:2}));await importing;
  assert.match(get('wbImportFileStatus').textContent,/已导入 2 项配置/);
  assert.equal(get('wbImportPreview').hidden,true);assert.equal(enabled.disabled,false);assert.equal(disabled.disabled,true);
  assert.equal(button.innerHTML,'选择配置文件');assert.equal(confirm.attributes['aria-busy'],undefined);
});

test('cancelling a preview cannot write anything and the same filename can be selected again',async()=>{
  const page=setup();await page.ccwWorkbench.open('sharing');
  const {modal,input,get}=importControls(page);let previews=0;
  page.fetch=async url=>{assert.ok(url.endsWith('?preview=true'));previews++;return response({items:reviewItems,revision:'r'});};
  await modal.dispatch('change',input);
  await modal.dispatch('click',importAction(get,'cancel-import'));
  await modal.dispatch('click',importAction(get,'confirm-import'));
  assert.equal(previews,1);assert.equal(input.value,'');assert.equal(get('wbImportPreview').hidden,true);
  await modal.dispatch('change',input);assert.equal(previews,2);
});

test('malformed JSON can be retried using the same filename without sending invalid data',async()=>{
  const page=setup();await page.ccwWorkbench.open('sharing');
  const {modal,input,get}=importControls(page,'broken.json','{broken');let previews=0;
  page.fetch=async url=>{assert.ok(url.endsWith('?preview=true'));previews++;return response({items:reviewItems,revision:'r'});};
  await modal.dispatch('change',input);
  assert.equal(previews,0);assert.equal(input.value,'');assert.match(get('wbError').textContent,/JSON 格式无效/);
  input.files=[{name:'broken.json',size:20,text:async()=>'{}'}];input.value='broken.json';
  await modal.dispatch('change',input);
  assert.equal(previews,1);assert.equal(get('wbError').classList.contains('hidden'),true);
  assert.match(get('wbImportFileStatus').textContent,/尚未导入/);
});

test('ZIP preview and confirmation use multipart and carry the revision only when confirmed',async()=>{
  const page=setup();await page.ccwWorkbench.open('sharing');
  const {modal,input,get}=importControls(page,'TEAM.ZIP');const calls=[];
  page.fetch=async(url,init)=>{calls.push({url,init});return response(url.endsWith('?preview=true')?{items:reviewItems,revision:'zip-r'}:{imported:2});};
  await modal.dispatch('change',input);
  await modal.dispatch('click',importAction(get,'confirm-import'));
  assert.equal(calls.length,2);assert.match(calls[0].url,/import-zip\?preview=true$/);assert.match(calls[1].url,/import-zip$/);
  assert.equal(calls[0].init.headers['Content-Type'],undefined);assert.equal(calls[1].init.headers['Content-Type'],undefined);
  assert.equal(calls[1].init.headers['X-Workbench-Import-Revision'],'zip-r');
  assert.equal(calls[1].init.body.items[0][1].name,'TEAM.ZIP');
});

test('unsupported or oversized imports fail locally and release the loading lock',async()=>{
  for(const [name,size,message] of [['huge.json',2*1024*1024+1,'2 MB'],['huge.zip',32*1024*1024+1,'32 MB'],['not-a-config.txt',1,'JSON 或 ZIP']]){
    const page=setup(),{modal,input,button,get}=importControls(page,name,'{}',size);let requests=0;
    page.fetch=async()=>{requests++;throw new Error('unexpected request');};
    await modal.dispatch('change',input);
    assert.equal(requests,0);assert.equal(input.value,'');assert.ok(get('wbError').textContent.includes(message));assert.equal(button.disabled,false);
  }
});

test('server errors restore controls and preserve a useful retry message',async()=>{
  const page=setup(),{modal,input,button,enabled,disabled,get}=importControls(page);
  page.fetch=async()=>({ok:false,json:async()=>({detail:'配置文件格式不受支持'})});
  await modal.dispatch('change',input);
  assert.match(get('wbError').textContent,/配置文件格式不受支持/);assert.match(get('wbImportFileStatus').textContent,/无法预览/);
  assert.equal(input.value,'');assert.equal(button.disabled,false);assert.equal(enabled.disabled,false);assert.equal(disabled.disabled,true);
});


test('extension preview requires a path or upload and restores its action button',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id),modal=get('ccwWorkbench');
  const button=get('preview-button');button.dataset.wbAction='preview';button.innerHTML='预览内容';modal.controls=[button];
  let requests=0;page.fetch=async()=>{requests++;throw new Error('unexpected request');};
  await modal.dispatch('click',button);
  assert.equal(requests,0);assert.match(get('wbError').textContent,/请输入扩展目录/);
  assert.equal(button.innerHTML,'预览内容');assert.equal(button.disabled,false);
});

test('scope save reads enabled components before pending disables form controls',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id),modal=get('ccwWorkbench');
  const button=get('binding-save');button.dataset.wbAction='save-binding';button.innerHTML='保存';
  const skill=get('skill');skill.dataset.component='skills';skill.checked=true;
  const unavailable=get('unavailable');unavailable.dataset.component='mcp';unavailable.checked=true;unavailable.disabled=true;
  get('wbEditor').querySelectorAll=()=>[skill,unavailable];get('wbForm').dataset.id='demo';get('wbScope').value='global';get('wbEnabled').checked=true;
  modal.controls=[button,skill,unavailable];const originalFetch=page.fetch;let saved;
  page.fetch=(url,init)=>{if(url.endsWith('/binding')){saved=JSON.parse(init.body);return Promise.resolve({ok:true,json:async()=>({})});}return originalFetch(url,init);};
  await modal.dispatch('click',button);
  assert.equal(saved.components.skills,true);assert.equal(saved.components.mcp,false);
  assert.equal(skill.disabled,false);assert.equal(unavailable.disabled,true);
});

test('the primary workbench entry is always in main navigation after workspace mode',()=>{
  const html=fs.readFileSync('claude_web/static/index.html','utf8');
  const nav=html.match(/<nav class="cw-nav-list"[\s\S]*?<\/nav>/)[0];
  assert.equal((html.match(/id="cwWorkbenchNav"/g)||[]).length,1);
  assert.ok(nav.indexOf('id="cwWorkbenchNav"')>nav.indexOf('id="cwCodeNav"'));
  const entry=nav.match(/<button[^>]*id="cwWorkbenchNav"[^>]*>/)[0];
  assert.doesNotMatch(entry,/aria-haspopup/);assert.match(entry,/aria-controls="ccwWorkbench"/);
  assert.doesNotMatch(entry,/data-cw-view|cw-code-only/);
  for(const lang of ['zh','en']){
    const messages=JSON.parse(fs.readFileSync(`claude_web/static/i18n/${lang}.json`,'utf8'));
    assert.ok(messages.nav.workbench);assert.ok(messages.nav.workbench_hint);
  }
});

test('sidebar opens a shell page without changing conversation state or invoking modal code',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id);await page.ccwWorkbench.restoreSession('session-a');
  get('input').value='Unsent conversation draft';get('chatInner').innerHTML='Existing conversation';
  const before=JSON.stringify(page.ccwWorkbench.requestFields());
  await get('cwWorkbenchNav').dispatch('click');
  assert.equal(page.document.body.classList.contains('wb-view'),true);
  assert.equal(get('ccwWorkbench').classList.contains('hidden'),false);
  assert.equal(get('cwWorkbenchNav').getAttribute('aria-current'),'page');
  assert.equal(get('cwChatNav').classList.contains('active'),false);
  assert.equal(page.sessionId,'session-a');assert.equal(page.codeMode,false);
  assert.equal(JSON.stringify(page.ccwWorkbench.requestFields()),before);
  assert.equal(get('input').value,'Unsent conversation draft');assert.equal(get('chatInner').innerHTML,'Existing conversation');
  assert.match(get('mainPane').innerHTML,/<main id="ccwWorkbench"/);
  assert.doesNotMatch(get('mainPane').innerHTML,/role="dialog"|aria-modal|modal-backdrop|modal-box/);
});

test('mobile launch closes sidebar and focuses the page heading',async()=>{
  const page=setup(),events=[];page.innerWidth=390;
  page.setHeaderToolsOpen=open=>events.push(['menu',open]);page.closeMobileSidebar=()=>events.push(['sidebar']);
  page.document.getElementById('wbTitle').focus=()=>events.push(['focus','wbTitle']);
  await page.document.getElementById('cwWorkbenchNav').dispatch('click');
  assert.deepEqual(events,[['menu',false],['sidebar'],['focus','wbTitle']]);
});

test('leaving and reopening preserves unsaved workbench and conversation drafts',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id);await page.ccwWorkbench.open('profiles');
  get('input').value='Keep my message';get('wbEditor').innerHTML='Unfinished profile';get('wbName').value='Draft profile';
  await get('ccwWorkbench').dispatch('input',get('wbEditor'));
  page.confirm=()=>{throw new Error('navigation should preserve the draft without discarding it');};
  assert.equal(page.ccwWorkbench.leave(),true);
  assert.equal(page.document.body.classList.contains('wb-view'),false);
  assert.equal(get('cwChatNav').getAttribute('aria-current'),'page');
  assert.equal(get('cwWorkbenchNav').getAttribute('aria-current'),null);
  page.fetch=async()=>{throw new Error('reopening a dirty editor must not reload it');};
  await page.ccwWorkbench.open();
  assert.equal(get('wbEditor').innerHTML,'Unfinished profile');assert.equal(get('wbName').value,'Draft profile');
  assert.equal(get('input').value,'Keep my message');
});

test('return to conversation restores focus and workspace nav without resetting the draft',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id);page.codeMode=true;
  get('input').value='Draft code task';let focused=false;get('input').focus=()=>focused=true;
  await page.ccwWorkbench.open();await get('wbClose').dispatch('click');
  assert.equal(page.codeMode,true);assert.equal(page.sessionId,'session-a');assert.equal(get('input').value,'Draft code task');
  assert.equal(get('ccwWorkbench').classList.contains('hidden'),true);assert.equal(focused,true);
  assert.equal(get('cwCodeNav').getAttribute('aria-current'),'page');
});

test('active workbench stays selected during background shell updates',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id);await page.ccwWorkbench.open();
  page.updateShellNav('chat');
  assert.equal(get('cwWorkbenchNav').classList.contains('active'),true);assert.equal(get('cwChatNav').classList.contains('active'),false);
});

test('workbench page uses existing shell navigation hooks for session changes',()=>{
  const html=fs.readFileSync('claude_web/static/index.html','utf8');
  for(const signature of ['function activateShellView(view) {','function openSessionInWorkspace(targetSession) {','function startNewSession() {']){
    assert.ok(html.includes(signature+"\n  if (window.ccwWorkbench?.leave() === false) return;"));
  }
});

test('navigation is locked during imports and released when the request finishes',async()=>{
  const page=setup();await page.ccwWorkbench.open('sharing');
  const {modal,input}=importControls(page);const originalFetch=page.fetch;let finish,entered;
  const started=new Promise(resolve=>entered=resolve);
  page.fetch=(url,init)=>{if(url.endsWith('/import?preview=true')){entered();return new Promise(resolve=>finish=resolve);}return originalFetch(url,init);};
  const importing=modal.dispatch('change',input);await started;
  assert.equal(page.ccwWorkbench.leave(),false);
  assert.equal(page.document.body.classList.contains('wb-view'),true);
  finish(response({items:reviewItems,revision:'r'}));await importing;
  assert.equal(page.ccwWorkbench.leave(),true);
});


test('mobile section selector follows current tab and reverts when a dirty tab is not discarded',async()=>{
  const page=setup(),get=id=>page.document.getElementById(id);await page.ccwWorkbench.open('sharing');
  const picker=get('wbSectionSelect');assert.equal(picker.value,'sharing');
  picker.value='profiles';await picker.dispatch('change');
  assert.equal(get('wbPanel').getAttribute('aria-labelledby'),'wbTab-profiles');
  await get('ccwWorkbench').dispatch('input',get('wbEditor'));page.confirm=()=>false;
  picker.value='tasks';await picker.dispatch('change');
  assert.equal(picker.value,'profiles');assert.equal(get('wbPanel').getAttribute('aria-labelledby'),'wbTab-profiles');
});

test('current model comes from the acknowledged binding and not the selected route or native alias',async()=>{
  const page=setup(),summary=page.document.getElementById('cwSessionModelSummary');
  page.fetch=async()=>response({provider_id:'p',session_model:{service:'Fixture service',model:'bound-model',source:'binding'}});
  await page.ccwWorkbench.restoreSession('session-a',[{type:'assistant',message:{model:'wrong-history-alias'}}]);
  assert.equal(summary.textContent,'会话绑定：Fixture service · bound-model');
  page.ccwWorkbench.observeSessionModel('session-b',{type:'meta',session_model:{service:'Other',model:'different',source:'binding'}});
  page.ccwWorkbench.observeSessionModel('session-a',{type:'assistant',message:{model:'wrong-live-alias'}});
  assert.match(summary.textContent,/bound-model/);
  page.ccwWorkbench.resetSession();assert.match(summary.textContent,/尚未发送消息/);
});

test('Claude native model is sourced from the latest top-level runtime event, with no guessed model',async()=>{
  const page=setup(),summary=page.document.getElementById('cwSessionModelSummary');
  await page.ccwWorkbench.restoreSession('session-a',[]);assert.match(summary.textContent,/暂无已记录/);
  await page.ccwWorkbench.restoreSession('session-a',[
    {type:'assistant',message:{model:'earlier'}},{type:'assistant',message:{model:'actual'}},
    {type:'assistant',parent_tool_use_id:'subtask',message:{model:'subagent-model'}},
  ]);
  assert.equal(summary.textContent,'运行时报告：Claude 原生 · actual');
});


test('changing mode in an unsent Code draft keeps the picker, request and existing draft in sync',async()=>{
  const page=setup(),get=page.document.getElementById;page.codeMode=true;page.sessionId=null;page.ccwWorkbench.resetSession();
  const picker=get('wbModeSelect');let resets=0;page.startNewSession=()=>{resets++;page.ccwWorkbench.resetSession();get('input').value='';};
  get('input').value='Keep this draft';page.pendingDocs.push({name:'fixture'});
  picker.value='research';await picker.dispatch('change');
  assert.equal(resets,0);assert.equal(picker.value,'research');assert.equal(page.ccwWorkbench.requestFields().work_mode,'research');
  assert.equal(get('input').value,'Keep this draft');assert.equal(page.pendingDocs.length,1);
  assert.equal(get('cwComposerPermBtn').disabled,true);
  assert.equal(page.document.body.classList.contains('wb-readonly-mode'),true);
});

test('existing conversation asks before starting another, and cancellation keeps mode and draft',async()=>{
  const page=setup(),get=page.document.getElementById;page.codeMode=true;page.ccwWorkbench.resetSession();
  const picker=get('wbModeSelect');get('input').value='Existing unsent draft';let asked=0;
  page.confirm=()=>{asked++;return false;};picker.value='review';await picker.dispatch('change');
  assert.equal(asked,1);assert.equal(picker.value,'code');assert.equal(page.sessionId,'session-a');assert.equal(get('input').value,'Existing unsent draft');
  let saved=0;page.saveDraft=()=>saved++;page.confirm=()=>true;picker.value='review';await picker.dispatch('change');
  assert.equal(saved,1);assert.equal(page.sessionId,null);assert.equal(picker.value,'review');assert.equal(page.ccwWorkbench.requestFields().work_mode,'review');
});

test('existing conversation with an attachment does not reset or drop it on mode switch',async()=>{
  const page=setup(),get=page.document.getElementById;page.codeMode=true;page.ccwWorkbench.resetSession();
  const picker=get('wbModeSelect');page.pendingImages.push({name:'fixture'});let message;page.toast=text=>message=text;
  picker.value='review';await picker.dispatch('change');
  assert.equal(picker.value,'code');assert.equal(page.sessionId,'session-a');assert.equal(page.pendingImages.length,1);
  assert.equal(message,'work_mode.pending_attachments');
});

test('Research only enables web search through explicit action and clears the warning afterwards',async()=>{
  const page=setup(),get=page.document.getElementById;page.codeMode=true;page.sessionId=null;page.ccwWorkbench.resetSession();
  const picker=get('wbModeSelect');picker.value='research';await picker.dispatch('change');
  assert.equal(page.isWebSearchEnabled(),false);assert.equal(get('wbResearchWeb').hidden,false);
  await get('wbEnableWebSearch').dispatch('click');
  assert.equal(page.isWebSearchEnabled(),true);assert.equal(get('wbResearchWeb').hidden,true);
  assert.match(get('wbWorkModeText').textContent,/work_mode.web_on/);
  page.setWebSearchEnabled(false);assert.equal(get('wbResearchWeb').hidden,false);
  picker.value='code';await picker.dispatch('change');
  assert.equal(get('wbResearchWeb').hidden,true);assert.equal(get('cwComposerPermBtn').disabled,false);
});

test('restored read-only mode displays its badge and does not expose mutable permission control',async()=>{
  const page=setup(),get=page.document.getElementById;page.codeMode=true;
  page.fetch=async()=>response({mode:'browser'});await page.ccwWorkbench.restoreSession('session-a');
  assert.equal(get('wbModeSelect').value,'browser');assert.equal(get('cwComposerPermBtn').disabled,true);
  assert.match(get('wbWorkModeText').textContent,/work_mode.browser/);
  assert.equal(get('wbResearchWeb').hidden,true);
});
