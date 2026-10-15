import {createLifetime} from '../lifetime.js';
const labels={INFO:'信息',WARNING:'警告',WARN:'警告',ERROR:'错误',CRITICAL:'严重错误',DEBUG:'调试'};
const formatTime=value=>new Date(Number(value)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
const el=(tag,classes,text)=>{const node=document.createElement(tag);if(classes)node.className=classes;if(text!==undefined)node.textContent=String(text);return node;};
const localTime=value=>new Date(value*1000).toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(' ','T');

export function createView(){
  let root,context,life,items=[],pending=[],cursor=null,hasMore=false,lastSeen=0,requestId=0,loading=false,detail=null,lastRoute='',applied=null,retrySnapshot=null;
  const $=id=>root.getElementById(id);
  function notice(text,error=false){$('systemLogNotice').textContent=text;$('systemLogNotice').className='monitor-notice'+(error?' error':'');}
  function filters(){
    const query=new URLSearchParams({limit:'50'});
    const session=$('logSession').value;if(session==='all')query.set('scope','all');else query.set('session',session||context.session);
    if($('logLevel').value)query.set('level',$('logLevel').value);
    if($('logEvent').value.trim())query.set('event',$('logEvent').value.trim());
    if($('logSearch').value.trim())query.set('q',$('logSearch').value.trim());
    const range=$('logRange').value;
    let from,to;
    if(range==='custom'){
      from=Math.floor(Date.parse($('logFrom').value+':00+08:00')/1000);to=Math.floor(Date.parse($('logTo').value+':00+08:00')/1000);
      if(!Number.isFinite(from)||!Number.isFinite(to)||to<from||to-from>30*86400)throw new Error('请选择正确的上海时间，单次范围最多 30 天');
    }else{to=Math.floor(Date.now()/1000);from=to-({'1h':3600,'24h':86400,'7d':604800}[range]||86400);}
    query.set('from',String(from));query.set('to',String(to));return query;
  }
  function formSnapshot(){return {query:filters(),range:$('logRange').value};}
  function refreshedSnapshot(){
    if(!applied)return formSnapshot();
    const query=new URLSearchParams(applied.query);
    if(applied.range!=='custom'){const to=Math.floor(Date.now()/1000);query.set('to',String(to));query.set('from',String(to-({'1h':3600,'24h':86400,'7d':604800}[applied.range]||86400)));}
    return {query,range:applied.range};
  }
  function updateURL(snapshot){
    const query=new URLSearchParams(snapshot.query);query.delete('limit');if(snapshot.range!=='custom'){query.delete('from');query.delete('to');}
    query.set('range',snapshot.range);context.replaceURL('/logs?'+query);lastRoute=context.url;
  }
  function showDetail(item){
    detail=item;$('logDetailMeta').textContent=formatTime(item.created_at)+' · '+item.session_name+' · '+(labels[item.level]||item.level)+' · '+item.event;
    $('logDetailBody').textContent=item.message;$('logCopyStatus').textContent='';$('logDetailDialog').showModal();$('closeLogDetail').focus();
  }
  function render(){
    const list=$('systemLogs');list.replaceChildren();
    for(const item of items){
      const row=el('article','system-log-row');row.dataset.logId=item.id;
      row.append(el('span','system-log-time',formatTime(item.created_at)),el('span','system-log-session',item.session_name));
      const level=el('span','state-chip '+(['ERROR','CRITICAL'].includes(item.level)?'bad':['WARN','WARNING'].includes(item.level)?'warn':'good'),labels[item.level]||item.level);level.dataset.logLevel=item.level;row.append(level);
      const message=el('div','system-log-copy');message.append(el('div','system-log-event',item.event),el('span','',item.message));row.append(message);
      const button=el('button','log-detail-button','详情');button.type='button';button.addEventListener('click',()=>showDetail(item));row.append(button);list.append(row);
    }
    if(!items.length)list.append(el('div','monitor-empty',applied&&['q','event','level'].some(key=>applied.query.has(key))?'没有匹配结果':'该范围暂无记录'));
    $('systemLogCount').textContent='已显示 '+items.length+' 条';$('loadOlderLogs').hidden=!hasMore;
    $('newSystemLogs').hidden=!pending.length;$('newSystemLogs').textContent='有 '+pending.length+' 条新日志 · 点击查看';
  }
  async function load(older=false,candidate=null){
    if(loading||!life.active||older&&!hasMore)return;
    let snapshot,query;try{snapshot=candidate||applied||formSnapshot();query=new URLSearchParams(snapshot.query);}catch(error){notice(error.message,true);return;}
    const id=++requestId;loading=true;$('applyLogFilters').disabled=true;$('refreshSystemLogs').disabled=true;
    if(older&&cursor)query.set('before_id',String(cursor));notice(older?'正在加载更早记录…':'正在读取日志…');
    try{
      const response=await life.environment.fetch('/api/logs?'+query,{cache:'no-store'});if(response.status===401)context.authLost();
      const data=await response.json();if(!response.ok)throw new Error(data.message||'日志读取失败');if(id!==requestId)return;
      const incoming=data.logs||[];
      if(older){const unique=new Map(items.map(item=>[item.id,item]));for(const item of incoming)unique.set(item.id,item);items=[...unique.values()].sort((a,b)=>b.id-a.id);}
      else{items=incoming;pending=[];lastSeen=Math.max(0,...items.map(item=>item.id));applied=snapshot;retrySnapshot=null;}
      cursor=data.next_cursor;hasMore=Boolean(data.has_more);render();notice('');$('logUpdatedAt').textContent='最后读取：'+formatTime(data.generated_at);if(!older)updateURL(snapshot);
    }catch(error){if(error.name!=='AbortError'&&id===requestId){if(!older)retrySnapshot=snapshot;notice((items.length?'日志读取失败，保留上次结果及已应用条件。':'日志读取失败。')+' '+error.message,true);}}
    finally{if(id===requestId){loading=false;$('applyLogFilters').disabled=false;$('refreshSystemLogs').disabled=false;}}
  }
  async function checkNew(){
    if(!life.active||loading||!$('logAutoRefresh').checked)return;
    if(!lastSeen){load(false,retrySnapshot||refreshedSnapshot());return;}
    if(!applied)return;
    const query=new URLSearchParams(applied.query);
    if(applied.range!=='custom')query.set('to',String(Math.floor(Date.now()/1000)));
    if(Number(query.get('to'))-Number(query.get('from'))>30*86400){$('logAutoRefresh').checked=false;notice('查询时间范围已过期，请刷新日志后重新开启自动检查。');return;}
    query.set('after_id',String(lastSeen));
    const id=++requestId;loading=true;
    try{
      const response=await life.environment.fetch('/api/logs?'+query,{cache:'no-store'});if(response.status===401)context.authLost();
      const data=await response.json();if(!response.ok)throw new Error(data.message||'新日志读取失败');if(id!==requestId)return;
      const incoming=data.logs||[];if(incoming.length){lastSeen=Math.max(lastSeen,...incoming.map(item=>item.id));const unique=new Map(pending.map(item=>[item.id,item]));for(const item of incoming)unique.set(item.id,item);pending=[...unique.values()].sort((a,b)=>b.id-a.id);$('newSystemLogs').hidden=false;$('newSystemLogs').textContent='有 '+pending.length+' 条新日志 · 点击查看';}
      if(pending.length>=500){$('logAutoRefresh').checked=false;notice('新记录较多，已暂停自动检查。请点击刷新日志查看最新记录。');}
      $('logUpdatedAt').textContent='最后检查：'+formatTime(data.generated_at);
    }catch(error){if(error.name!=='AbortError'&&id===requestId)notice('新日志检查失败。'+error.message,true);}
    finally{if(id===requestId)loading=false;}
  }
  function readRoute(){
    const url=new URL(context.url);const q=url.searchParams;
    $('logSession').value=q.get('scope')==='all'?'all':q.get('session')||context.session;
    $('logLevel').value=q.get('level')||'';$('logEvent').value=q.get('event')||'';$('logSearch').value=q.get('q')||'';
    $('logRange').value=q.get('range')||(q.has('from')&&q.has('to')?'custom':'24h');
    const now=Math.floor(Date.now()/1000);$('logFrom').value=localTime(Number(q.get('from'))||now-86400);$('logTo').value=localTime(Number(q.get('to'))||now);
    $('logCustomRange').hidden=$('logRange').value!=='custom';
  }
  return {
    mount(r,c){root=r;context=c;life=createLifetime(root,context);life.environment.setInterval(checkNew,5000);
      $('logFilterForm').addEventListener('submit',event=>{event.preventDefault();try{load(false,formSnapshot());}catch(error){notice(error.message,true);}});
      $('refreshSystemLogs').addEventListener('click',()=>{try{load(false,retrySnapshot||refreshedSnapshot());}catch(error){notice(error.message,true);}});$('loadOlderLogs').addEventListener('click',()=>load(true));
      $('logRange').addEventListener('change',()=>{$('logCustomRange').hidden=$('logRange').value!=='custom';});
      $('newSystemLogs').addEventListener('click',()=>{const unique=new Map(items.map(item=>[item.id,item]));for(const item of pending)unique.set(item.id,item);items=[...unique.values()].sort((a,b)=>b.id-a.id);pending=[];render();$('logsPage').scrollTop=0;});
      $('closeLogDetail').addEventListener('click',()=>$('logDetailDialog').close());
      $('copyLogDetail').addEventListener('click',async()=>{if(!detail)return;try{await navigator.clipboard.writeText($('logDetailMeta').textContent+'\n'+detail.message);$('logCopyStatus').textContent='已复制脱敏内容';}catch{$('logCopyStatus').textContent='复制失败，请手动选择文本复制';}});
    },
    activate(c){context=c;life.activate();life.environment.fetch('/api/sessions',{cache:'no-store'}).then(response=>response.json()).then(data=>{const old=$('logSession').value;const options=[el('option','','全部会话')];options[0].value='all';for(const item of data.sessions||[]){const option=el('option','',item.display_name||item.name);option.value=item.name;options.push(option);}$('logSession').replaceChildren(...options);$('logSession').value=old;const routeChanged=context.url!==lastRoute;if(routeChanged)readRoute();load(false,routeChanged?formSnapshot():retrySnapshot||refreshedSnapshot());}).catch(error=>{if(error.name!=='AbortError')notice('会话范围读取失败，请重新进入日志页。',true);});},
    deactivate(){requestId++;loading=false;life.deactivate();},dispose(){requestId++;life.dispose();items=[];pending=[];detail=null;applied=null;retrySnapshot=null;root=null;}
  };
}
