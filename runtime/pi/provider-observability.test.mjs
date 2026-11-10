import test from "node:test";
import assert from "node:assert/strict";
import {createProviderObserver, summarizeProviderRequests} from "./provider-observability.mjs";
import {realModel} from "./models.mjs";

const secrets = ["private-api-fixture", "private-thinking", "private-image", "private-prompt"];
const env = {PI_BASE_URL:"https://open.bigmodel.cn/api/paas/v4", PI_MODEL:"glm-5.3-flash", PI_API_KEY:secrets[0]};
const context = {messages:[{role:"user",content:[{type:"text",text:secrets[3]}],timestamp:1}],tools:[]};
const sse = (reason = "stop", usage = {prompt_tokens:17,completion_tokens:5,total_tokens:22,prompt_tokens_details:{cached_tokens:9},completion_tokens_details:{reasoning_tokens:3}}) => {
  const chunks = [{choices:[{index:0,delta:{role:"assistant",content:"OK",reasoning_content:secrets[1]},finish_reason:null}]},
    {choices:[{index:0,delta:{},finish_reason:reason}],...(usage ? {usage} : {})}];
  return new Response(chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("")+"data: [DONE]\n\n", {headers:{"content-type":"text/event-stream"}});
};

test("real stream keeps model output and records actual usage without prompt/thinking/auth", async () => {
  const saved=[];
  const config=realModel(env,{runId:"run_a1",agentId:"b_capture",onRecord:row=>saved.push(row)});
  const result=await config.streamFn(config.model,context,{maxRetries:0,fetch:async()=>sse()}).result();
  assert.equal(result.stopReason,"stop");
  assert.equal(saved.length,1);
  assert.deepEqual(saved[0].usage,{input_tokens:17,output_tokens:5,total_tokens:22,cache_read_tokens:9,cache_write_tokens:null,reasoning_tokens:3});
  assert.equal(saved[0].cost,null);
  assert.equal(saved[0].stop_reason,"stop");
  assert.equal(saved[0].run_id,"run_a1");
  assert.equal(saved[0].retry_index,0);
  for(const secret of secrets) assert.ok(!JSON.stringify(saved).includes(secret));
});

test("truncation retains real usage; missing usage stays unknown", async()=>{
  for (const usage of [null,{prompt_tokens:9,completion_tokens:4,total_tokens:13}]) {
    const config=realModel(env);
    const result=await config.streamFn(config.model,context,{maxRetries:0,fetch:async()=>sse("length",usage)}).result();
    assert.equal(result.stopReason,"length");
    const row=config.providerObserver.records()[0];
    assert.equal(row.stop_reason,"length");
    assert.equal(row.usage.total_tokens,usage?13:null);
  }
});

test("SDK provider quota error remains an error with an audited real HTTP request", async()=>{
  const config=realModel(env);
  const result=await config.streamFn(config.model,context,{maxRetries:0,fetch:async()=>new Response(
    JSON.stringify({error:{code:"1113",message:secrets[0]}}),{status:429})}).result();
  assert.equal(result.stopReason,"error");
  assert.equal(config.providerObserver.records().length,1);
  assert.equal(config.providerObserver.records()[0].error_class,"provider_quota_exhausted");
  assert.ok(!JSON.stringify(config.providerObserver.records()).includes(secrets[0]));
});

test("each HTTP retry is distinct within a logical stream and quota 1113 is classified safely",async()=>{
  const observer=createProviderObserver({runId:"run_a"});
  const stream=observer.wrapStream(async(_model,_context,options)=>{
    const first=await options.fetch("https://provider.invalid"); await first.text();
    const second=await options.fetch("https://provider.invalid"); await second.text();
  });
  let attempt=0;
  await stream({id:"test"},{},{fetch:async()=>++attempt===1
    ? new Response(JSON.stringify({error:{code:1113,message:secrets[0]}}),{status:429}) : sse()});
  const rows=observer.records();
  assert.equal(rows.length,2);
  assert.equal(rows[0].error_class,"provider_quota_exhausted");
  assert.equal(rows[0].provider_error_code,"1113");
  assert.equal(rows[0].call_id,rows[1].call_id);
  assert.notEqual(rows[0].request_id,rows[1].request_id);
  assert.equal(rows[1].retry_index,1);
  assert.equal(observer.summary().retry_count,1);
  assert.ok(!JSON.stringify(rows).includes(secrets[0]));
});

test("aborted fetch preserves exception and creates one aborted row",async()=>{
  const controller=new AbortController();
  const observer=createProviderObserver();
  const sentinel=new DOMException("private-thinking","AbortError");
  const wrapped=observer.wrapStream(async(_m,_c,options)=>options.fetch("https://unused.invalid"));
  controller.abort();
  await assert.rejects(wrapped({id:"test"},{},{signal:controller.signal,fetch:async()=>{throw sentinel;}}),error=>error===sentinel);
  assert.equal(observer.records()[0].stop_reason,"aborted");
  assert.equal(observer.records()[0].error_class,"provider_aborted");
});

test("reader cancellation propagates once; completed DONE is not misclassified as user abort",async()=>{
  const observer=createProviderObserver(); let cancelled=0;
  const wrapped=observer.wrapStream(async(_m,_c,options)=>options.fetch("https://unused.invalid"));
  const response=await wrapped({id:"test"},{},{fetch:async()=>new Response(new ReadableStream({
    pull(controller){controller.enqueue(new TextEncoder().encode("data: {}\n\n"));},cancel(){cancelled++;}
  }),{headers:{"content-type":"text/event-stream"}})});
  const reader=response.body.getReader(); await reader.read(); await reader.cancel();
  assert.equal(cancelled,1); assert.equal(observer.records().length,1);
  assert.equal(observer.records()[0].error_class,"provider_aborted");
});

test("delegation/checkpoint unions deduplicate request IDs; unknown usage does not become zero",async()=>{
  const observer=createProviderObserver();
  const wrapped=observer.wrapStream(async(_m,_c,options)=>{const response=await options.fetch("https://unused.invalid");await response.text();});
  await wrapped({id:"test"},{},{fetch:async()=>sse()});
  const rows=observer.records();
  assert.equal(summarizeProviderRequests([...rows,...rows]).request_count,1);
  assert.equal(summarizeProviderRequests([...rows,...rows]).usage.total_tokens,22);
  assert.equal(summarizeProviderRequests([...rows,{...rows[0],request_id:"other",usage:{}}]).usage.total_tokens,null);
  assert.equal(summarizeProviderRequests([]).usage.total_tokens,null);
});

test("chunk splits keep unicode/provider bytes identical and response cost only when explicit",async()=>{
  const observer=createProviderObserver();
  const text='data: '+JSON.stringify({choices:[{finish_reason:"stop",delta:{content:"化学"}}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2,cost:0.02,cost_currency:"CNY"}})+'\n\ndata: [DONE]\n\n';
  const bytes=new TextEncoder().encode(text);
  const wrapped=observer.wrapStream(async(_m,_c,options)=>{const response=await options.fetch("https://unused.invalid");return response.text();});
  const result=await wrapped({id:"test"},{},{fetch:async()=>new Response(new ReadableStream({start(controller){
    for(let i=0;i<bytes.length;i++)controller.enqueue(bytes.slice(i,i+1));controller.close();
  }}),{headers:{"content-type":"text/event-stream"}})});
  assert.equal(result,text);assert.equal(observer.records()[0].cost,0.02);assert.equal(observer.summary().cost_currency,"CNY");
});

test("awaited request-intent hook precedes network; terminal overwrites the same safe ID",async()=>{
  const events=[];let fetched=0;let observer;
  observer=createProviderObserver({onRequest:async row=>{
    events.push(row);
    assert.equal(row.phase,"started");
    assert.equal(fetched,0);
    assert.equal(observer.records()[0].request_id,row.request_id);
    assert.equal(observer.summary().unfinished_requests,1);
    assert.equal(observer.summary().usage.total_tokens,null);
    await Promise.resolve();
  },onRecord:row=>events.push(row)});
  const wrapped=observer.wrapStream(async(_m,_c,options)=>{const response=await options.fetch("https://unused.invalid");await response.text();});
  await wrapped({id:"test"},{messages:[secrets[3]]},{fetch:async()=>{fetched++;return sse();}});
  assert.equal(fetched,1);assert.equal(events.length,2);
  assert.equal(events[0].request_id,events[1].request_id);
  assert.equal(events[1].phase,"finished");
  assert.equal(observer.records().length,1);
  assert.equal(summarizeProviderRequests(events).request_count,1);
  assert.equal(observer.summary().unfinished_requests,0);
  for(const secret of secrets)assert.ok(!JSON.stringify(events).includes(secret));
});

test("failed request-intent checkpoint prevents all remote fetches and retains unknown billing",async()=>{
  const sentinel=new Error("checkpoint fixture-private-key");
  const observer=createProviderObserver({onRequest:async()=>{throw sentinel;}});
  let fetched=0;
  const wrapped=observer.wrapStream(async(_m,_c,options)=>options.fetch("https://unused.invalid"));
  await assert.rejects(wrapped({id:"test"},{},{fetch:async()=>{fetched++;return sse();}}),error=>error===sentinel);
  assert.equal(fetched,0);
  assert.equal(observer.records()[0].phase,"not_sent");
  assert.equal(observer.summary().not_sent_requests,1);
  assert.equal(observer.summary().request_count,0);
  assert.equal(observer.summary().usage.total_tokens,null);
  assert.equal(observer.summary().cost,null);
  assert.ok(!JSON.stringify(observer.records()).includes(secrets[0]));
});
