// Deterministic behavior tests for the shipped UI. No model or browser is required.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';
import * as seeds from '../dist/src/data.js';
import {renderMarkdown} from '../dist/src/markdown.js';
import * as modelHelpers from '../dist/src/model-settings.js';

const source = readFileSync(new URL('../dist/src/app.js', import.meta.url), 'utf8');
function harness(overrides = {}) {
  const nodes = new Map();
  const node = () => ({innerHTML:'', value:'', disabled:false, addEventListener(){}, append(){}, remove(){}, focus(){}, classList:{add(){},remove(){},toggle(){}}});
  for (const selector of ['#task-job-panel','#task-feed','.task-send','#task-text','#task-files','.skip-link','#toast-root']) nodes.set(selector,node());
  nodes.set('textarea[name="task-text"]', nodes.get('#task-text'));
  const timers = new Map(); let timerId=0;
  const calls=[];
  const client = {modelConfigured:true, connected:true, features:{agentRuntime:true,agentJobs:true},
    jobs:async()=>({jobs:[]}), job:async()=>({job:{}}), cancelJob:async id=>({job:{job_id:id,session_id:'s1',text:'task',status:'cancelled'}}),
    submitJob:async payload=>{calls.push(payload);return{job:{job_id:'j1',session_id:'s1',text:payload.text,status:'queued',last_event_seq:0}};}, ...overrides};
  const sandbox = {projects:seeds.projects,sampleSeed:seeds.samples,evidenceSeed:seeds.evidence,inventory:seeds.inventory,taskSeed:seeds.tasks,activity:seeds.activity,resources:seeds.resources,
    CoreClient:class {constructor(){return client;}},icon:()=>'',console,crypto:{randomUUID},renderMarkdown,...modelHelpers,
    localStorage:{getItem:()=>null,setItem:()=>{throw Error('No real task may enter browser storage');}},
    document:{querySelector:s=>nodes.get(s)||null,querySelectorAll:()=>[],addEventListener(){},createElement:node},
    window:{addEventListener(){}},location:{hash:''},matchMedia:()=>({matches:false}),
    setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id)};
  const context=vm.createContext(sandbox);
  const body=source.replace(/^import .*;\r?\n/gm,'');
  vm.runInContext(body.slice(0,body.lastIndexOf('\nsyncEvidenceState();')),context);
  vm.runInContext("state.mode='live'; render=()=>{};",context);
  const run=code=>vm.runInContext(code,context);
  return {run,client,calls,nodes,timers,async tick(){const [id,fn]=timers.entries().next().value;timers.delete(id);await fn();}};
}
const tests=[];
const test=(name,fn)=>tests.push([name,fn]);

test('submit creates durable job and polling merges progress into final result',async()=>{
  const h=harness({job:async(id,after)=>({job:{job_id:id,session_id:'s1',text:'task',status:'completed',last_event_seq:2,events:[{seq:1,type:'tool_start',tool:'inventory.search'},{seq:2,type:'tool_end'}],result:{status:'completed',text:'saved',runtime:'pi',modelUsed:true}}})});
  h.nodes.get('#task-text').value='task';
  await h.run('submitTask(document)');
  assert.equal(h.calls.length,1);assert.ok(h.calls[0].requestId);assert.equal(h.run('state.taskBusy'),true);
  assert.match(h.nodes.get('#task-feed').innerHTML,/后台/);
  await h.tick();
  assert.equal(h.run('state.taskBusy'),false);assert.equal(h.run('state.taskChat.at(-1).text'),'saved');assert.equal(h.run('state.taskJobs[0].events.length'),2);
});
test('ambiguous submission retry retains request ID and payload; double submit is fenced',async()=>{
  let fail=true, release;
  const h=harness({submitJob:async payload=>{h.calls.push(payload);if(fail)throw Error('connection lost');await new Promise(resolve=>release=resolve);return{job:{job_id:'j1',session_id:'s1',text:payload.text,status:'queued'}};}});
  h.nodes.get('#task-text').value='task';await h.run('submitTask(document)');
  const id=h.calls[0].requestId;assert.equal(h.run('state.taskBusy'),false);
  fail=false;const retry=h.run('submitBackgroundTask(state.taskPending.payload,state.taskPending.submittedFiles,true)');
  await h.run('submitTask(document)');assert.equal(h.calls.length,2);assert.equal(h.calls[1].requestId,id);
  release();await retry;assert.equal(h.run('state.taskPending'),null);
});
test('reload restores server-owned jobs and followup uses same session',async()=>{
  const h=harness({jobs:async()=>({jobs:[{job_id:'j-old',session_id:'s-old',text:'previous question',status:'completed',result:{text:'previous answer',status:'completed'}}]})});
  await h.run('restoreJobs()');assert.equal(h.run('state.taskSessionId'),'s-old');assert.equal(h.run('state.taskChat.length'),2);
  h.nodes.get('#task-text').value='followup';await h.run('submitTask(document)');assert.equal(h.calls[0].sessionId,'s-old');
});
test('cleared model retains job history, restores running task, and continues polling',async()=>{
  let polls=0;
  const h=harness({modelConfigured:false,features:{task:true,agentRuntime:true,agentJobs:true},
    jobs:async()=>({jobs:[{job_id:'paused',session_id:'s-paused',text:'saved task',status:'running'}]}),
    job:async()=>{polls++;return{job:{job_id:'paused',session_id:'s-paused',status:'running',last_event_seq:1,events:[{seq:1,type:'started'}]}};}});
  await h.run('restoreJobs()');
  assert.match(h.nodes.get('#task-job-panel').innerHTML,/saved task/);
  assert.match(h.nodes.get('#task-feed').innerHTML,/任务已暂停/);
  assert.match(h.nodes.get('.task-send').innerHTML,/等待模型配置/);
  assert.equal(h.run('asyncJobsEnabled()'),false);assert.equal(h.run('jobHistoryEnabled()'),true);
  await h.tick();assert.equal(polls,1);assert.equal(h.run('state.taskSessionId'),'s-paused');assert.equal(h.calls.length,0);
  h.client.modelConfigured=true;await h.tick();assert.equal(polls,2);assert.match(h.nodes.get('#task-feed').innerHTML,/后台继续处理/);
});
test('cancel returns terminal state and clears busy control',async()=>{
  const h=harness();h.nodes.get('#task-text').value='task';await h.run('submitTask(document)');await h.run('cancelCurrentJob()');
  assert.equal(h.run('state.taskJobs[0].status'),'cancelled');assert.equal(h.run('state.taskBusy'),false);assert.equal(h.run('state.taskCancelBusy'),false);
});
test('incremental events deduplicate by sequence and polling recovers after disconnect',async()=>{
  let count=0;
  const h=harness({job:async(id,after)=>{
    count++;
    if(count===2)throw Error('temporary disconnect');
    if(count===1){assert.equal(after,0);return{job:{job_id:id,session_id:'s1',status:'running',last_event_seq:1,events:[{seq:1,type:'tool_start',tool:'search'}]}};}
    assert.equal(after,1);return{job:{job_id:id,session_id:'s1',status:'completed',last_event_seq:2,events:[{seq:1,type:'tool_start',tool:'search'},{seq:2,type:'tool_end'}],result:{status:'completed',text:'recovered'}}};
  }});
  h.nodes.get('#task-text').value='task';await h.run('submitTask(document)');await h.tick();await h.tick();
  assert.match(h.run('state.taskPollError'),/disconnect/);assert.equal(h.run('state.taskBusy'),true);
  await h.tick();assert.equal(h.run('state.taskPollError'),'');assert.equal(h.run('state.taskChat.at(-1).text'),'recovered');assert.equal(h.run('state.taskJobs[0].events.length'),2);
});
test('updating older jobs preserves session message order and escapes untrusted text',async()=>{
  const h=harness();
  h.run("rememberJob({job_id:'old',session_id:'s',text:'first',status:'completed',result:{text:'one',status:'completed'}});rememberJob({job_id:'new',session_id:'s',text:'second',status:'completed',result:{text:'<img src=x onerror=alert(1)>',status:'completed'}});showJob(rememberJob({job_id:'old',session_id:'s',status:'completed'}));");
  assert.equal(h.run('state.taskChat[0].text'),'first');assert.equal(h.run('state.taskChat[2].text'),'second');
  assert.match(h.nodes.get('#task-feed').innerHTML,/&lt;img/);assert.doesNotMatch(h.nodes.get('#task-feed').innerHTML,/<img/);
});
test('late list/poll/submission responses cannot populate another principal context',async()=>{
  let finishList;
  const h=harness({jobs:()=>new Promise(resolve=>finishList=resolve)});const listing=h.run('restoreJobs()');
  h.run('resetContext()');finishList({jobs:[{job_id:'private',session_id:'secret',status:'running'}]});await listing;assert.equal(h.run('state.taskJobs.length'),0);
  let finishSubmit;h.client.submitJob=()=>new Promise(resolve=>finishSubmit=resolve);h.nodes.get('#task-text').value='private';const submitting=h.run('submitTask(document)');
  h.run('resetContext()');finishSubmit({job:{job_id:'private',session_id:'secret',status:'queued'}});await submitting;assert.equal(h.run('state.taskJobs.length'),0);
  h.client.submitJob=async()=>({job:{job_id:'j1',session_id:'s1',text:'task',status:'running'}});h.nodes.get('#task-text').value='task';await h.run('submitTask(document)');
  let finishPoll;h.client.job=()=>new Promise(resolve=>finishPoll=resolve);const polling=h.tick();h.run('resetContext()');finishPoll({job:{job_id:'private',session_id:'secret',status:'completed',result:{text:'private'}}});await polling;
  assert.equal(h.run('state.taskChat.length'),0);assert.equal(h.run('state.taskSessionId'),null);
});
test('no-model mode retains legacy task route',async()=>{
  const h=harness({modelConfigured:false,features:{task:true},task:async payload=>({text:'legacy',status:'completed'})});h.nodes.get('#task-text').value='task';await h.run('submitTask(document)');
  assert.equal(h.calls.length,0);assert.equal(h.run('state.taskChat.at(-1).text'),'legacy');
});

test('explicit continuation reuses server sources and keeps one identity after a lost acknowledgement',async()=>{
  let fail=true, release;
  const requests=[];
  const h=harness({continueJob:async(id,payload)=>{requests.push({id,payload});if(fail)throw Error('connection lost');await new Promise(resolve=>release=resolve);return{job:{job_id:'child',session_id:'s-source',text:'continue',status:'queued'}};}});
  h.run("showJob(rememberJob({job_id:'source',session_id:'s-source',status:'partial',context:{pages:[{page_id:'p1'}]},result:{text:'partial'}}));state.taskFiles=[{name:'keep.png',data_b64:'PRIVATE_ATTACHMENT'}];");
  assert.match(h.nodes.get('#task-job-panel').innerHTML,/继续整理未完成项/);
  await h.run('continueCurrentJob()');
  assert.equal(h.run('state.taskContinuePending.requestId'),'continuation:source');
  fail=false;const pending=h.run('continueCurrentJob()');await h.run('continueCurrentJob()');
  assert.equal(requests.length,2);assert.deepEqual(requests[0],requests[1]);
  assert.equal(requests[0].payload.sessionId,'s-source');assert.equal('attachments' in requests[0].payload,false);
  release();await pending;
  assert.equal(h.run('state.taskJobId'),'child');assert.equal(h.run('state.taskContinuePending'),null);
  assert.equal(h.run('state.taskFiles[0].data_b64'),'PRIVATE_ATTACHMENT');assert.equal(h.calls.length,0);
});

test('continuation is unavailable for running tasks or missing models and never infers free-text intent',async()=>{
  let requests=0;
  const h=harness({continueJob:async()=>{requests++;}});
  h.run("showJob(rememberJob({job_id:'source',session_id:'s-source',status:'running',context:{pages:[{page_id:'p1'}]}}));");
  await h.run('continueCurrentJob()');assert.equal(requests,0);
  h.run("state.taskJobs[0].status='cancelled';state.taskBusy=false;");
  await h.run('continueCurrentJob()');assert.equal(requests,0);
  assert.doesNotMatch(h.nodes.get('#task-job-panel').innerHTML,/继续整理未完成项/);
  h.run("state.taskJobs[0].status='partial';state.taskBusy=false;");h.client.modelConfigured=false;
  await h.run('continueCurrentJob()');assert.equal(requests,0);
  h.client.modelConfigured=true;h.run("state.taskJobs[0].context={};");
  await h.run('continueCurrentJob()');assert.equal(requests,0);
});

test('a late continuation response cannot populate another principal context',async()=>{
  let resolve;
  const h=harness({continueJob:()=>new Promise(done=>resolve=done)});
  h.run("showJob(rememberJob({job_id:'source',session_id:'s-source',status:'partial',context:{pages:[{page_id:'p1'}]}}));");
  const pending=h.run('continueCurrentJob()');h.run('resetContext()');
  resolve({job:{job_id:'private-child',session_id:'private',status:'queued'}});await pending;
  assert.equal(h.run('state.taskJobs.length'),0);assert.equal(h.run('state.taskSessionId'),null);assert.equal(h.run('state.taskContinuePending'),null);
});

test('server history reconciles a lost continuation response without a second submission',async()=>{
  let requests=0;
  const h=harness({continueJob:async()=>{requests++;throw Error('connection lost');},jobs:async()=>({jobs:[{job_id:'child',request_id:'continuation:source',session_id:'s-source',status:'completed',result:{text:'continued',status:'completed'}}]})});
  h.run("showJob(rememberJob({job_id:'source',session_id:'s-source',status:'partial',context:{pages:[{page_id:'p1'}]}}));");
  await h.run('continueCurrentJob()');await h.run('restoreJobs()');
  assert.equal(requests,1);assert.equal(h.run('state.taskJobId'),'child');assert.equal(h.run('state.taskContinuePending'),null);
});

test('a confirmed review boundary leaves no ambiguous continuation pending',async()=>{
  const h=harness({continueJob:async()=>{const error=Error('review actual effect first');error.code='continuation_requires_review';throw error;}});
  h.run("showJob(rememberJob({job_id:'source',session_id:'s-source',status:'needs_review',context:{pages:[{page_id:'p1'}]}}));");
  await h.run('continueCurrentJob()');assert.equal(h.run('state.taskContinuePending'),null);assert.equal(h.run('state.taskJobId'),'source');assert.match(h.run('state.taskPollError'),/review/);
});

for (const [name,fn] of tests){await fn();console.log(`PASS ${name}`);}
console.log(`${tests.length} UI job behavior tests passed.`);
