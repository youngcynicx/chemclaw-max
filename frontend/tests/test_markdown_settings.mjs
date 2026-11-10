import assert from 'node:assert/strict';
import {renderMarkdown} from '../dist/src/markdown.js';
import {MODEL_DEFAULTS,modelPayload,publicModelSettings,modelSchemes,saveModelScheme} from '../dist/src/model-settings.js';
import {CoreClient} from '../dist/src/core-client.js';

let count = 0;
async function test(name, fn) { await fn(); count++; console.log(`PASS ${name}`); }
await test('headings, emphasis, fenced JSON and tables render real structural markup',()=>{
  const html=renderMarkdown('# 进展\n\n**02** 是候选\n\n|样品|容量|\n|---|---:|\n|02|3.42|\n\n```json\n{"value":3.42}\n```');
  assert.match(html,/<h1>进展<\/h1>/);assert.match(html,/<strong>02<\/strong>/);assert.match(html,/<table><thead>/);assert.match(html,/<td class="md-align-right">3.42/);assert.match(html,/<pre><code>\{&quot;value&quot;:3.42\}/);
});
await test('raw HTML, code and image tags never become executable elements',()=>{
  const html=renderMarkdown('<img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n\n`<svg onload=alert(1)>`\n\n![x](https://remote.invalid/tracker.png)');
  assert.doesNotMatch(html,/<(?:img|svg|script)\b/i);assert.match(html,/&lt;img/);assert.match(html,/<code>&lt;svg/);assert.match(html,/图片：x/);
});
await test('only HTTP HTTPS and mailto links survive; dangerous targets are inert',()=>{
  for(const url of ['javascript:alert','data:text/html;base64,YQ','vbscript:evil','file:///C:/x','jav&#x61;script:evil','%6aavascript:evil']) {
    const html=renderMarkdown(`[click](${url})`);assert.doesNotMatch(html,/<a\b/i,url);
  }
  const html=renderMarkdown('[safe](https://example.invalid/a?x=1&y=2)');assert.match(html,/href="https:\/\/example.invalid\/a\?x=1&amp;y=2"/);assert.match(html,/rel="noopener noreferrer"/);
});
await test('table escaped pipes and inline code pipes remain within their cells',()=>{
  const html=renderMarkdown('|name|value|\n|---|---|\n|a\\|b|`x|y`|');
  assert.equal((html.match(/<td /g)||[]).length,2);assert.match(html,/a\|b/);assert.match(html,/<code>x\|y<\/code>/);
});
await test('unclosed code fences remain escaped and token-like untrusted text cannot inject',()=>{
  const html=renderMarkdown('\u0000MD0\u0000\n\n```html\n</code><img onerror=x>');
  assert.doesNotMatch(html,/<img\b/);assert.match(html,/&lt;\/code&gt;/);
});
await test('model payload includes both protocols, separate vision fields and bounded integers',()=>{
  const fd=new Map(Object.entries({...MODEL_DEFAULTS,protocol:'openai-responses',visionBaseUrl:'https://vision.invalid/v1',visionModel:'vision-id',apiKey:' main ',visionApiKey:' vision '}));
  const result=modelPayload(fd);assert.equal(result.protocol,'openai-responses');assert.equal(result.visionModel,'vision-id');assert.equal(result.apiKey,'main');assert.equal(result.maxTokens,16384);
});
await test('empty keys remain empty for server-side same-endpoint reuse; invalid budgets fail',()=>{
  const fd=new Map(Object.entries({...MODEL_DEFAULTS,apiKey:'',visionApiKey:''}));assert.equal(modelPayload(fd).apiKey,'');
  fd.set('maxTokens','1.5');assert.throws(()=>modelPayload(fd),/正整数/);fd.set('maxTokens','200000');assert.throws(()=>modelPayload(fd),/大于/);
});
await test('named schemes round-trip only public fields, never keys or nested secrets',()=>{
  let value='';const storage={getItem:()=>value,setItem:(_,v)=>value=v};
  saveModelScheme(storage,'日常',{...MODEL_DEFAULTS,apiKey:'private-main',visionApiKey:'private-vision',token:'hidden',nested:{key:'hidden'}});
  assert.doesNotMatch(value,/private|hidden|apiKey|visionApiKey/);assert.equal(modelSchemes(storage)[0].name,'日常');
  saveModelScheme(storage,'日常',{...MODEL_DEFAULTS,model:'updated'});assert.equal(modelSchemes(storage).length,1);assert.equal(modelSchemes(storage)[0].model,'updated');
});
await test('secrets embedded in endpoint URLs cannot enter saved metadata or requests',()=>{
  const fd=new Map(Object.entries({...MODEL_DEFAULTS,baseUrl:'https://u:secret@example.invalid/v1?api_key=secret'}));assert.throws(()=>modelPayload(fd),/密钥栏/);
  assert.equal(publicModelSettings(Object.fromEntries(fd)).baseUrl,'');
});
await test('stored schemes are sanitized again when loaded',()=>{
  const storage={getItem:()=>JSON.stringify([{name:'hostile',...MODEL_DEFAULTS,apiKey:'private',baseUrl:'https://x.invalid/?token=private'}])};
  const result=modelSchemes(storage);assert.equal(result[0].baseUrl,'');assert.equal(result[0].apiKey,undefined);
});
await test('client config sends exact allowlisted metadata and empty-key reuse, caches no secrets',async()=>{
  const client=new CoreClient();client.csrfToken='csrf';let sent;
  client._request=async(path,body)=>{sent={path,body};return {...MODEL_DEFAULTS,configured:true,keyConfigured:true,apiKey:'should-not-cache',visionKeyConfigured:false};};
  await client.configureModel({...MODEL_DEFAULTS,apiKey:'',visionApiKey:'',reuseApiKey:false,unexpected:'not-sent'});
  assert.equal(sent.path,'/api/model/configure');assert.equal(sent.body.reuseApiKey,false);assert.equal(sent.body.apiKey,'');assert.equal(sent.body.unexpected,undefined);assert.equal(client.modelConfig.apiKey,undefined);assert.equal(client.modelConfig.keyConfigured,true);
});
await test('failed configuration preserves existing client state for retry',async()=>{
  const client=new CoreClient();client.applyModelStatus({...MODEL_DEFAULTS,configured:true,keyConfigured:true});
  client._request=async()=>{throw Error('unsupported reasoning format');};
  await assert.rejects(()=>client.configureModel({...MODEL_DEFAULTS,model:'bad'}),/unsupported/);assert.equal(client.modelName,MODEL_DEFAULTS.model);assert.equal(client.modelConfigured,true);
});
console.log(`${count} Markdown/model settings tests passed.`);
