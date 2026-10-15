import {createLifetime} from '../lifetime.js';
const states={WORKING:'已连接',CONNECTED:'已连接',SCAN_QR_CODE:'待扫码',STARTING:'启动中',AUTHENTICATING:'认证中',STOPPED:'已停止',FAILED:'失败',NOT_CREATED:'待创建',CREATED:'已创建',UNKNOWN:'暂不可用'};
const levels={INFO:'信息',WARNING:'警告',WARN:'警告',ERROR:'错误',CRITICAL:'严重错误'};
const time=value=>value?new Date(Number(value)*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
const el=(tag,classes,text)=>{const node=document.createElement(tag);if(classes)node.className=classes;if(text!==undefined)node.textContent=String(text);return node;};
const number=value=>value===null||value===undefined?'暂不可用':String(value);

export function createView(){
  let root,context,life,data=null,dataScope='',filter='all',requestId=0,loading=false;
  const $=id=>root.getElementById(id);
  const scopeQuery=()=>$('overviewScope').value==='all'?'scope=all':'session='+encodeURIComponent($('overviewScope').value);
  function setChip(node,tone,text){node.className='state-chip '+tone;node.textContent=text;}
  function renderSessions(){
    const rows=(data?.sessions||[]).filter(item=>filter==='all'||filter==='connected'&&['WORKING','CONNECTED'].includes(item.state)||filter==='scan_qr'&&item.state==='SCAN_QR_CODE'||filter==='failed'&&item.state==='FAILED');
    const body=$('overviewSessions');body.replaceChildren();
    for(const item of rows){
      const row=el('tr','session-status-row');
      const identity=el('td');identity.append(el('strong','',item.display_name),el('div','session-technical',item.name));row.append(identity);
      const add=(label,node)=>{const cell=el('td');cell.dataset.label=label;cell.append(node);row.append(cell);};
      const tone=['WORKING','CONNECTED'].includes(item.state)?'good':item.state==='FAILED'?'bad':'warn';
      add('连接状态',el('span','state-chip '+tone,states[item.state]||'未知'));
      add('自动回复',el('span','',{all_day:'全时段',scheduled:'按时间段',off:'已关闭'}[item.auto_reply?.mode]||'暂不可用'));
      add('AI 设置',el('span','',item.auto_reply?.ai_configured?'已配置':'未配置'));
      add('检查时间',el('span','event-meta',time(item.checked_at)));
      const actions=el('td','table-actions');
      for(const [label,url]of [['控制','/?session='+encodeURIComponent(item.name)],['聊天','/sessions/'+encodeURIComponent(item.name)+'/chats'],['设置','/settings?session='+encodeURIComponent(item.name)]]){const a=el('a','',label);a.href=url;actions.append(a);}row.append(actions);body.append(row);
    }
    if(!rows.length){const row=el('tr');const cell=el('td','monitor-empty',data?'没有符合条件的会话':'会话状态暂不可用');cell.colSpan=6;row.append(cell);body.append(row);}
  }
  function render(){
    for(const node of root.querySelectorAll('[data-overview-count]'))node.textContent=number(data?.session_counts?.[node.dataset.overviewCount]);
    const waha=data?.services?.waha;
    setChip($('overviewWaha'),waha?.state==='available'?'good':waha?.state==='degraded'?'warn':'bad',waha?.state==='available'?'接口可用':waha?.state==='degraded'?'部分异常':'暂不可用');
    $('overviewWahaDetail').textContent='健康接口：'+(waha?.health?.ok?'响应正常':'暂不可用')+' · 版本读取：'+(waha?.version_check?.ok?(waha.version_check.version||'正常'):'暂不可用');
    setChip($('overviewDatabase'),data?.services?.database?.ok?'good':'bad',data?.services?.database?.ok?'可访问':'暂不可用');
    $('overviewCheckedAt').textContent='最后成功采样：'+time(data?.generated_at);
    renderSessions();
    const sessions=data?.sessions||[];
    $('overviewConfigSummary').textContent=data?'当前范围 '+sessions.filter(item=>item.auto_reply?.ai_configured).length+' 个会话配置 AI，'+sessions.filter(item=>item.auto_reply?.customer_memory_enabled).length+' 个会话启用客户动态记忆。':'';
    const automation=$('overviewAutomation');automation.replaceChildren();
    for(const [key,label]of [['human_takeover','人工接管中'],['pending','待执行跟进'],['running','执行中'],['failed','执行失败'],['unknown','发送待确认']]){const tile=el('div','automation-item');tile.append(el('span','',label),el('strong','',number(data?.automation_counts?.[key])));automation.append(tile);}
    $('overviewOverdue').textContent=data?.automation_counts?.overdue===null?'到期任务统计暂不可用':'其中已到期的待执行任务：'+number(data?.automation_counts?.overdue);
    $('overviewErrorCount').textContent=number(data?.recent_error_count)+' 条错误';
    const errors=$('overviewErrors');errors.replaceChildren();
    for(const item of data?.recent_errors||[]){const row=el('div','event-row');row.append(el('span','event-meta',time(item.created_at)),el('span','state-chip '+(['ERROR','CRITICAL'].includes(item.level)?'bad':'warn'),levels[item.level]||item.level));const message=el('div','event-message',item.message);message.append(el('span','',item.session_name+' · '+item.event));const link=el('a','event-link','查看同类记录');link.href='/logs?'+scopeQuery()+'&level='+encodeURIComponent(item.level)+'&event='+encodeURIComponent(item.event);message.append(link);row.append(message);errors.append(row);}
    if(!(data?.recent_errors||[]).length)errors.append(el('div','monitor-empty',data&&data.recent_error_count!==null?'最近 24 小时没有记录到警告或错误':'异常日志暂不可用'));
    $('overviewTaskLogs').href='/logs?'+scopeQuery()+'&q=follow-up%20task';
    $('overviewAllLogs').href='/logs?'+scopeQuery();
  }
  async function refresh(){
    if(loading||!life.active)return;
    loading=true;const id=++requestId;const scope=$('overviewScope').value;
    if(scope!==dataScope){data=null;render();}
    $('refreshOverview').disabled=true;$('overviewNotice').textContent='正在刷新状态…';
    try{
      const response=await life.environment.fetch('/api/overview?'+scopeQuery(),{cache:'no-store'});
      if(response.status===401)context.authLost();
      const result=await response.json();if(!response.ok)throw new Error(result.message||'状态读取失败');if(id!==requestId)return;
      data=result;dataScope=scope;render();
      $('overviewNotice').className='monitor-notice';$('overviewNotice').textContent=(result.partial_errors||[]).map(item=>item.message).join('；');
    }catch(error){if(error.name!=='AbortError'&&id===requestId){$('overviewNotice').className='monitor-notice error';$('overviewNotice').textContent=(data?'数据已过期，保留上次成功结果。':'状态暂不可用。')+' '+error.message;}}
    finally{if(id===requestId){loading=false;$('refreshOverview').disabled=false;}}
  }
  return {
    mount(r,c){root=r;context=c;life=createLifetime(root,context);$('refreshOverview').addEventListener('click',refresh);$('overviewScope').addEventListener('change',()=>{requestId++;loading=false;context.replaceURL('/overview?'+scopeQuery());refresh();});for(const button of root.querySelectorAll('[data-state-filter]'))button.addEventListener('click',()=>{filter=button.dataset.stateFilter;for(const item of root.querySelectorAll('[data-state-filter]'))item.setAttribute('aria-pressed',String(item===button));renderSessions();});life.environment.setInterval(refresh,15000);},
    activate(c){context=c;life.activate();const url=new URL(context.url);const wanted=url.searchParams.get('scope')==='all'?'all':url.searchParams.get('session')||'all';life.environment.fetch('/api/sessions',{cache:'no-store'}).then(response=>response.json()).then(result=>{const old=$('overviewScope').value;const options=[el('option','','全部会话')];options[0].value='all';for(const item of result.sessions||[]){const option=el('option','',item.display_name||item.name);option.value=item.name;options.push(option);}$('overviewScope').replaceChildren(...options);$('overviewScope').value=options.some(item=>item.value===wanted)?wanted:old;refresh();}).catch(error=>{if(error.name!=='AbortError')refresh();});},
    deactivate(){requestId++;loading=false;life.deactivate();},dispose(){requestId++;life.dispose();data=null;root=null;}
  };
}
