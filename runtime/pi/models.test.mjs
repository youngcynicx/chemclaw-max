import assert from "node:assert/strict";
import test from "node:test";
import {modelSettings, realModel, mockModel} from "./models.mjs";

const env = (changes = {}) => ({PI_BASE_URL:"https://open.bigmodel.cn/api/paas/v4",
  PI_MODEL:"glm-5.3-flash", PI_API_KEY:"fixture-private-model-key", ...changes});
const context = {messages:[{role:"user", content:[{type:"text",text:"Say OK"}],timestamp:1}], tools:[]};

for (const [format, effort, expected] of [
  ["openai", "medium", {reasoning_effort:"medium"}],
  ["zai", "high", {thinking:{type:"enabled"}, reasoning_effort:"high"}],
  ["deepseek", "off", {thinking:{type:"disabled"}}],
  ["qwen", "off", {enable_thinking:false}],
  ["openrouter", "low", {reasoning:{effort:"low"}}],
]) {
  test(`explicit ${format}/${effort} reaches the Chat payload`, async () => {
    const config = realModel(env({PI_BASE_URL:"https://provider.example/v1/chat/completions",
      PI_MODEL:"custom", PI_REASONING_FORMAT:format, PI_REASONING_EFFORT:effort}));
    const {body} = await capture(config);
    assert.equal(config.model.baseUrl, "https://provider.example/v1");
    for (const key of ["reasoning_effort","thinking","reasoning","enable_thinking"])
      assert.deepEqual(body[key], expected[key]);
  });
}

test("Responses and local no-key settings reach the SDK transport consistently", async () => {
  const local = realModel(env({PI_BASE_URL:"http://127.0.0.1:11434/v1", PI_MODEL:"local",
    PI_API_KEY:"", PI_REASONING_FORMAT:"none"}));
  const receipt = await capture(local);
  assert.equal(receipt.auth, null);
  assert.ok(!JSON.stringify(receipt.body).includes("chemclaw-local-no-auth"));
  const config = realModel(env({PI_BASE_URL:"https://provider.example/v1/responses", PI_MODEL:"custom",
    PI_PROTOCOL:"openai-responses", PI_REASONING_FORMAT:"openai", PI_REASONING_EFFORT:"high"}));
  let sent, url;
  await config.streamFn(config.model,context,{maxRetries:0,fetch:async (input,init)=>{
    const request = input instanceof Request ? input : new Request(input,init);
    sent = JSON.parse(await request.text()); url = request.url;
    return new Response(JSON.stringify({error:{message:"offline fixture"}}),{status:400});
  }}).result();
  assert.equal(url,"https://provider.example/v1/responses");
  assert.deepEqual(sent.reasoning,{effort:"high"});
  assert.equal(sent.max_output_tokens,16384);
});

async function capture(config, options = {}) {
  let body, auth, requests=0;
  const fetch = async (input, init) => {
    requests++;
    const request = input instanceof Request ? input : new Request(input, init);
    body = JSON.parse(await request.text());
    auth = request.headers.get("authorization");
    const chunks = [
      {id:"offline",object:"chat.completion.chunk",created:1,model:body.model,
        choices:[{index:0,delta:{role:"assistant",content:"OK"},finish_reason:null}]},
      {id:"offline",object:"chat.completion.chunk",created:1,model:body.model,
        choices:[{index:0,delta:{},finish_reason:"stop"}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}},
    ];
    return new Response(chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join("") + "data: [DONE]\n\n",
      {status:200,headers:{"content-type":"text/event-stream"}});
  };
  const result = await config.streamFn(config.model, context, {...options, fetch, maxRetries:0}).result();
  assert.equal(result.stopReason, "stop", result.errorMessage);
  assert.equal(requests, 1);
  return {body, auth, result};
}

test("native GLM-5.3 defaults low, thinking stays enabled, output ceiling is 16384", async () => {
  const config=realModel(env());
  assert.equal(config.model.reasoning,true);
  assert.equal(config.model.maxTokens,16384);
  assert.equal(config.model.contextWindow,128000);
  const {body}=await capture(config,{reasoning:"off"});
  assert.equal(body.reasoning_effort,"low");
  assert.equal(body.thinking.type,"enabled");
  assert.equal(body.max_tokens,16384);
  assert.equal(body.max_completion_tokens,undefined);
});

for (const effort of ["low","high","max"]) {
  test(`native GLM-5.3 sends exact ${effort} effort without Pi fallback`, async () => {
    const {body}=await capture(realModel(env({PI_REASONING_EFFORT:effort})));
    assert.equal(body.reasoning_effort,effort);
    assert.equal(body.thinking.type,"enabled");
    assert.notEqual(body.thinking.type,"disabled");
  });
}

test("provider hook enforces required native thinking after caller payload hooks", async () => {
  const {body}=await capture(realModel(env()), {onPayload:payload=>({...payload,
    reasoning_effort:"medium",thinking:{type:"disabled"},user:"kept-caller-field"})});
  assert.equal(body.reasoning_effort,"low");
  assert.equal(body.thinking.type,"enabled");
  assert.equal(body.user,"kept-caller-field");
});

test("native profile recognizes same supported family on ZAI endpoint", async () => {
  const {body}=await capture(realModel(env({PI_BASE_URL:"https://api.z.ai/api/paas/v4",PI_MODEL:"glm-5.3"})));
  assert.equal(body.reasoning_effort,"low");
  assert.equal(body.thinking.type,"enabled");
});

test("configured token/context limits reach descriptor and actual payload", async () => {
  const config=realModel(env({PI_MAX_TOKENS:"4096",PI_CONTEXT_WINDOW:"32768"}));
  const {body}=await capture(config);
  assert.equal(config.model.maxTokens,4096);
  assert.equal(config.model.contextWindow,32768);
  assert.equal(body.max_tokens,4096);
});

for (const other of [
  {PI_MODEL:"glm-4.7-flash"},
  {PI_MODEL:"glm-5.2"},
  {PI_MODEL:"generic-chat",PI_BASE_URL:"https://provider.example/v1"},
  {PI_MODEL:"glm-5.3-flash",PI_BASE_URL:"https://openrouter.ai/api/v1"},
  {PI_MODEL:"glm-5.3-flash",PI_BASE_URL:"https://open.bigmodel.cn.example/v1"},
]) {
  test(`unmapped endpoint/model does not receive native reasoning fields: ${other.PI_MODEL} ${other.PI_BASE_URL || "native"}`, async () => {
    const config=realModel(env(other));
    const {body}=await capture(config);
    assert.equal(config.model.reasoning,false);
    assert.equal(body.reasoning_effort,undefined);
    assert.equal(body.thinking,undefined);
    assert.equal(body.enable_thinking,undefined);
  });
}

test("invalid/unsupported effort is rejected before any network and never silently clamped", () => {
  for (const effort of ["off","disabled","none","minimal","medium","xhigh","LOW","","bogus"]) {
    assert.throws(()=>realModel(env({PI_REASONING_EFFORT:effort})), /must be low, high, or max/);
  }
  assert.throws(()=>realModel(env({PI_MODEL:"glm-4.7-flash",PI_REASONING_EFFORT:"low"})), /no supported capability mapping/);
  assert.throws(()=>realModel(env({PI_PROTOCOL:"openai-responses"})), /requires the openai-completions/);
  assert.throws(()=>realModel(env({PI_PROTOCOL:"unrecognized"})), /PI_PROTOCOL/);
});

test("existing Responses model keeps its protocol and does not acquire native fields", async () => {
  const config=realModel(env({PI_BASE_URL:"https://provider.example/v1",PI_MODEL:"responses-model",PI_PROTOCOL:"openai-responses"}));
  let body;
  const result=await config.streamFn(config.model,context,{
    onPayload:payload=>{body=payload;throw new Error("offline payload inspection");},
    fetch:()=>{throw new Error("No network is permitted");},maxRetries:0,
  }).result();
  assert.equal(result.stopReason,"error");
  assert.ok(body);
  assert.equal(config.model.api,"openai-responses");
  assert.equal(body.max_output_tokens,16384);
  assert.equal(body.thinking,undefined);
  assert.equal(body.reasoning_effort,undefined);
  assert.equal(body.reasoning,undefined);
});

test("invalid token limits are rejected without leaking their values", () => {
  for (const setting of ["PI_MAX_TOKENS","PI_CONTEXT_WINDOW"]) {
    for (const value of ["0","-1","1.5","1e4"," 2048","","9007199254740992","fixture-private-model-key"]) {
      assert.throws(()=>realModel(env({[setting]:value})), error=>{
        assert.match(error.message,new RegExp(setting));
        assert.ok(!error.message.includes("fixture-private-model-key"));
        return true;
      });
    }
  }
  assert.throws(()=>realModel(env({PI_MAX_TOKENS:"20000",PI_CONTEXT_WINDOW:"10000"})), /must not exceed/);
});

test("credentials remain in transport headers and do not enter public settings, payload or output", async () => {
  const config=realModel(env());
  const {body,auth,result}=await capture(config);
  assert.equal(auth,"Bearer fixture-private-model-key");
  for(const item of [config.settings,config.model,body,result]) {
    assert.ok(!JSON.stringify(item).includes("fixture-private-model-key"));
  }
});

test("offline mock explicitly responds after observing a tool receipt", async () => {
  const config=mockModel("b_query");
  const planned=await config.streamFn(config.model,context).result();
  const query=planned.content.find(block=>block.type==="toolCall");
  assert.equal(query.name,"query.events.search");
  const receipt={role:"toolResult",toolCallId:query.id,toolName:query.name,
    content:[{type:"text",text:'{"status":"needs_review"}'}],isError:false,timestamp:2};
  const final=await config.streamFn(config.model,{...context,messages:[...context.messages,planned,receipt]}).result();
  const respond=final.content.find(block=>block.type==="toolCall");
  assert.equal(respond.name,"agent.respond");
  assert.equal(respond.arguments.status,"completed");
  assert.match(respond.arguments.text,/needs_review/);
  assert.match(respond.arguments.text,/实际状态为准/);
  const settled=await config.streamFn(config.model,{...context,messages:[...context.messages,planned,receipt,final,
    {role:"toolResult",toolCallId:respond.id,toolName:"agent.respond",content:[{type:"text",text:"accepted"}],isError:false,timestamp:3}]}).result();
  assert.ok(!settled.content.some(block=>block.type==="toolCall"));
});
