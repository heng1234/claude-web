const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const html=fs.readFileSync('claude_web/static/index.html','utf8');
function setup(overrides={}) {
  const page={t:key=>key, isWebSearchEnabled:()=>false, mobileCwdOrStop:()=>'/project', sessionId:'session',
    modelSelect:{value:'sonnet'}, effectiveEffortForSend:()=> 'high', settings:{codeBrowser:false,systemPrompt:'original'},
    buildPermissionPayload:web=>({permission_mode:'default',disallowed_tools:web?null:['WebSearch','WebFetch']}),
    codeReviewState:{validations:[]},...overrides};
  vm.createContext(page);
  for(const name of ['captureCodeQueueSettings','codeQueueContextProblem','renderRoundValidation','renderChangedFilesSummary']) {
    vm.runInContext(html.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n\\}`))[0],page);
  }
  return page;
}

test('queue snapshots capture original model and web preference before awaiting target lookup',async()=>{
  let submitted,finish;
  const page=setup({fetch:(_url,init)=>{submitted=JSON.parse(init.body);return new Promise(resolve=>finish=resolve);}});
  const capturing=page.captureCodeQueueSettings({work_mode:'research'},true);
  page.modelSelect.value='opus';page.settings.systemPrompt='updated';
  finish({ok:true,json:async()=>({revision:'rev',model:'sonnet'})});
  const saved=await capturing;
  assert.equal(saved.model,'sonnet');assert.equal(saved.webEnabled,true);assert.equal(saved.systemPrompt,'original');
  assert.equal(submitted.work_mode,'research');assert.equal(submitted.disallowed_tools,null);
  assert.match(html,/webSearchPrefix: capture.webSearchPrefix/);
  assert.match(html,/model: queued \? queued.model : modelSelect.value/);
  assert.match(html,/queue_context: queued\?\.target\.revision/);
});

test('no snapshot, a changed project, browser access or tool grants stop automatic dispatch',()=>{
  const page=setup(), original={permission_mode:'default',allowed_tools:['Read','Edit']};
  const saved={cwd:'/project',permission:original,browserEnabled:true,target:{revision:'rev'}};
  assert.equal(page.codeQueueContextProblem(saved,'/project',true,original),'');
  assert.equal(page.codeQueueContextProblem(saved,'/other',true,original),'work_queue.project_changed');
  assert.equal(page.codeQueueContextProblem(saved,'/project',false,original),'work_queue.permission_changed');
  assert.equal(page.codeQueueContextProblem(saved,'/project',true,{...original,allowed_tools:['Read']}),'work_queue.permission_changed');
  assert.equal(page.codeQueueContextProblem(null,'/project',true,original),'work_queue.missing_snapshot');
});

test('outcome shows only verifications linked to this result, not model or another turn claims',()=>{
  const page=setup(),status={};const card={querySelector:()=>status};
  page.codeReviewState.validations.push({change_set_id:'old',returncode:0,command:'other-turn'});
  page.renderRoundValidation(card,'new');assert.equal(status.textContent,'work_queue.unverified');
  page.codeReviewState.validations.push(
    {change_set_id:'new',not_run:true,returncode:-1},
    {change_set_id:'new',timed_out:true,returncode:-1,command:'slow check'},
    {change_set_id:'new',returncode:0,command:'unit check'},
  );
  page.renderRoundValidation(card,'new');
  assert.equal(status.textContent,'work_queue.validation_not_run\nwork_queue.validation_timeout · slow check\nx.valid_pass · unit check');
  assert.doesNotMatch(status.textContent,/other-turn/);
});


test('the round card exposes recorded files and leaves verification unknown until there is evidence',async()=>{
  const status={};const card={dataset:{},querySelector:()=>status,querySelectorAll:()=>[]};let appended;
  const page=setup({codeMode:true,document:{createElement:()=>card},escapeHtml:text=>String(text).replaceAll('<','&lt;'),
    registerCodeChangeSet:(files,meta)=>files.length?{id:meta.changeSetId,files}:null,
    gitStatusLabel:status=>status,renderGitDiffHtml:text=>text,decorateCodeFileLinks(){},refreshCodeInspectorPath(){},scrollBottom(){}});
  const container={querySelector:()=>null,appendChild:node=>appended=node};
  await page.renderChangedFilesSummary(container,[{path:'fixture<.txt',diff:'-old\n+new'}],{changeSetId:'round',changesRecorded:true});
  assert.equal(appended,card);assert.equal(card.dataset.roundChangeSet,'round');
  assert.match(card.innerHTML,/1 work_queue.changed_files/);assert.match(card.innerHTML,/work_queue.view_changes/);
  assert.match(card.innerHTML,/fixture&lt;\.txt/);assert.equal(status.textContent,'work_queue.unverified');
});
