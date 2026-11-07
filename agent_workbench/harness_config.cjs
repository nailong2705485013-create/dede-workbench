// Read configuration data with Harness's bundled YAML parser. Never evaluate tags,
// load a credentials document, resolve an environment value, or print a raw error.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const messages = {
  CONFIG_UNAVAILABLE:'无法读取 Harness 桌面配置。',
  YAML_UNSUPPORTED:'Harness 模型配置格式不受支持，请在桌面端重新保存模型设置。',
  DEFAULT_MODEL_MISSING:'Harness 桌面端尚未保存完整的默认通道和模型。',
  PROVIDER_MISSING:'桌面端所选通道缺少可复用的供应商配置。',
  MODEL_MISSING:'桌面端所选模型不在该通道的配置列表中。',
  SECRET_INLINE:'所选通道包含内嵌密钥、请求头或动态表达式，无法安全保存快照；请改为 Harness 凭据引用。',
  ENDPOINT_UNSUPPORTED:'所选接口地址包含凭据、查询参数或不受支持的协议，无法安全同步。',
};
function fail(code){const error=new Error(code);error.safeCode=code;throw error;}
function safe(value,depth=0){
  if(depth>30)fail('YAML_UNSUPPORTED');
  if(value===null||typeof value==='number'||typeof value==='boolean')return;
  if(typeof value==='string'){
    if(value.includes('${{')||/^sk-[A-Za-z0-9_-]{16,}$/.test(value))fail('SECRET_INLINE');
    return;
  }
  if(Array.isArray(value)){value.forEach(v=>safe(v,depth+1));return;}
  if(typeof value!=='object')fail('YAML_UNSUPPORTED');
  for(const [key,item] of Object.entries(value)){
    if(['__proto__','prototype','constructor'].includes(key))fail('YAML_UNSUPPORTED');
    const normalized=key.replace(/[-_]/g,'').toLowerCase();
    if(['apikey','authorization','password','secret','secretkey','accesstoken','refreshtoken','token','headers','credentials'].includes(normalized))fail('SECRET_INLINE');
    if(normalized==='apikeyenv'&&(typeof item!=='string'||! /^[A-Za-z_][A-Za-z0-9_]*$/.test(item)))fail('SECRET_INLINE');
    safe(item,depth+1);
  }
}
function text(value){return typeof value==='string'&&value.trim()!==''&&value.length<=300;}
try{
  const [install,source]=process.argv.slice(2);
  const req=createRequire(path.join(install,'resources/app.asar/dsh/package.json'));
  const YAML=req('yaml');
  const input=fs.readFileSync(source,'utf8');
  if(Buffer.byteLength(input)>1024*1024)fail('YAML_UNSUPPORTED');
  const doc=YAML.parseDocument(input,{logLevel:'silent',uniqueKeys:true});
  if(doc.errors.length||doc.warnings.length)fail('YAML_UNSUPPORTED');
  const rows=doc.toJS({maxAliasCount:50});
  if(!Array.isArray(rows))fail('YAML_UNSUPPORTED');
  const row=id=>rows.findLast(r=>r&&r.id===id);
  const selection=row('agent-default-model')?.config;
  if(!selection||!text(selection.provider)||!text(selection.model))fail('DEFAULT_MODEL_MISSING');
  safe(selection);
  const picked={provider:selection.provider,model:selection.model};
  if(selection.reasoningEffort!==undefined){
    if(!text(selection.reasoningEffort))fail('YAML_UNSUPPORTED');
    picked.reasoningEffort=selection.reasoningEffort;
  }
  const provider=row('llm-pi-ai')?.config?.providers?.[picked.provider];
  const builtin=['deepseek-account','deepseek-official'].includes(picked.provider);
  if(!provider&&!builtin)fail('PROVIDER_MISSING');
  let hostname=null;
  const patch=[{id:'credentials',disabled:false},{id:'authorization',disabled:false},
    {id:'deepseek-account',disabled:picked.provider!=='deepseek-account'},
    {id:'agent-default-model',config:picked}];
  if(provider){
    safe(provider);
    if(!provider.apiKeyEnv||!text(provider.api))fail('PROVIDER_MISSING');
    if(!Array.isArray(provider.models)||!provider.models.some(m=>m.id===picked.model))fail('MODEL_MISSING');
    let endpoint;
    try{endpoint=new URL(provider.baseURL);}catch{fail('ENDPOINT_UNSUPPORTED');}
    if(!['http:','https:'].includes(endpoint.protocol)||endpoint.username||endpoint.password||endpoint.search||endpoint.hash)fail('ENDPOINT_UNSUPPORTED');
    hostname=endpoint.hostname;
    patch.push({id:'llm-pi-ai',disabled:false,config:{providers:{[picked.provider]:provider}}});
  }else{
    // Retain non-secret customizations for the selected builtin only.
    for(const id of [picked.provider==='deepseek-account'?'deepseek-account':'llm-deepseek']){
      const configured=row(id)?.config;
      if(configured){safe(configured);patch.push({id,config:configured});}
    }
    // Remove any separately configured pi-ai routes from the headless profile.
    patch.push({id:'llm-pi-ai',config:{providers:{}}});
  }
  console.log(JSON.stringify({selection:picked,endpoint_host:hostname,patch}));
}catch(error){
  const code=error.safeCode||'CONFIG_UNAVAILABLE';
  console.log(JSON.stringify({error:code,message:messages[code]||messages.CONFIG_UNAVAILABLE}));
  process.exitCode=1;
}
