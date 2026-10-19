const menu=document.querySelector('.shell-menu');
const content=document.getElementById('shellContent');
const errorBox=document.getElementById('shellError');
const sessionSelect=document.getElementById('shellSession');
const themeSelect=document.getElementById('shellTheme');
const dirtyNotice=document.getElementById('shellDirtyNotice');
const translationSettings=document.getElementById('shellTranslationSettings');
const records=new Map();
let current=null,routeGeneration=0,authenticated=true,pendingController=null;

// The chat inherits the space below the shell navigation. Safari's keyboard
// changes the visual viewport without always resizing its layout viewport.
function syncChatViewport(){
  const shell=document.getElementById('applicationShell'),viewport=window.visualViewport;
  if(shell.dataset.view==='chat'&&innerWidth<=820&&viewport&&Math.abs(viewport.scale-1)<.01){
    shell.style.setProperty('--app-viewport-height',Math.min(innerHeight,viewport.height)+'px');
  }else shell.style.removeProperty('--app-viewport-height');
}
window.addEventListener('resize',syncChatViewport);
window.visualViewport?.addEventListener('resize',syncChatViewport);
window.visualViewport?.addEventListener('scroll',syncChatViewport);

function parseRoute(value){
  const url=new URL(value,location.origin);
  if(url.origin!==location.origin)return null;
  const match=/^\/sessions\/([A-Za-z0-9][A-Za-z0-9._-]{0,63})\/chats$/.exec(url.pathname);
  const view=match?'chat':({'/':'dashboard','/settings':'settings','/overview':'overview','/logs':'logs'}[url.pathname]);
  if(!view)return null;
  const session=match?match[1]:(url.searchParams.get('session')||current?.context.session||sessionSelect.value||'default');
  if(!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(session))return null;
  if(!match&&view!=='overview')url.searchParams.set('session',session);
  return {view,session,url:url.href};
}
function showError(message){errorBox.replaceChildren(document.createTextNode(message));errorBox.hidden=false;}
function applyTheme(value){const theme=['daylight','night'].includes(value)?value:'daylight';document.documentElement.dataset.theme=theme;themeSelect.value=theme;localStorage.setItem('waha-panel-theme',theme);for(const record of records.values()){record.host.dataset.theme=theme;const select=record.host.shadowRoot.getElementById('themeSelect');if(select)select.value=theme;}}
function updateMenu(){
  if(!current)return;
  document.getElementById('applicationShell').dataset.view=current.context.view;
  syncChatViewport();
  for(const link of menu.querySelectorAll('a')){
    const view=link.dataset.shellNav;
    if(view===current.context.view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
    link.href=view==='chat'?'/sessions/'+encodeURIComponent(current.context.session)+'/chats':view==='overview'?'/overview?scope=all':(view==='dashboard'?'/':'/'+(view==='settings'?'settings':view))+'?session='+encodeURIComponent(current.context.session);
  }
  sessionSelect.value=current.context.session;
  translationSettings.hidden=current.context.view!=='chat';
  dirtyNotice.hidden=![...records.values()].some(record=>record.host.shadowRoot.querySelector('.view-body')?.dataset.dirty);
}
function authLost(){
  if(!authenticated)return;
  authenticated=false;routeGeneration++;pendingController?.abort();
  for(const record of records.values()){record.controller.dispose();record.host.remove();}
  records.clear();current=null;content.replaceChildren();
  showError('登录状态已失效，请重新登录。');
  const button=document.createElement('button');button.type='button';button.textContent='重新登录';button.onclick=()=>location.reload();errorBox.append(button);
}
function replaceURL(record,value){
  if(record!==current)return;
  const route=parseRoute(value);if(!route)return;
  const previousKey=record.context.view+':'+record.context.session;
  Object.assign(record.context,route);
  const key=route.view+':'+route.session;
  if(previousKey!==key){const duplicate=records.get(key);if(duplicate&&duplicate!==record){duplicate.controller.dispose();duplicate.host.remove();}records.delete(previousKey);records.set(key,record);}
  history.replaceState(null,'',route.url);updateMenu();
}
async function navigate(value,{push=true}={}){
  if(!authenticated)return;
  const route=parseRoute(value);if(!route)return;
  if(current && route.url===current.context.url)push=false;
  const generation=++routeGeneration;
  pendingController?.abort();pendingController=new AbortController();
  const key=route.view+':'+route.session;
  let record=records.get(key);
  try{
    if(!record){
      const query=new URLSearchParams({view:route.view,session:route.session});
      const response=await fetch('/api/ui/view?'+query,{cache:'no-store',signal:pendingController.signal});
      if(response.status===401){authLost();return;}
      const payload=await response.json();if(!response.ok)throw new Error(payload.message||'页面读取失败');
      const module=await import(payload.module);
      if(generation!==routeGeneration)return;
      const host=document.createElement('section');host.className='view-host';host.dataset.viewHost=route.view;host.dataset.theme=document.documentElement.dataset.theme;host.hidden=true;
      const root=host.attachShadow({mode:'open'});
      const style=document.createElement('style');style.textContent=payload.css;
      const body=document.createElement('div');body.className='view-body';body.dataset.session=route.session;body.innerHTML=payload.html;
      root.append(style,body);
      record={host,controller:module.createView(),title:payload.title,context:{...route,navigate,replaceURL:url=>replaceURL(record,url),authLost,setTheme:applyTheme}};
      const markDirty=event=>{if(route.view==='settings'&&event.target.closest('#settingsForm')){body.dataset.dirty='1';updateMenu();}};
      body.addEventListener('input',markDirty);
      body.addEventListener('change',markDirty);
      new MutationObserver(updateMenu).observe(body,{attributes:true,attributeFilter:['data-dirty']});
      records.set(key,record);content.append(host);
      // Mount only after the previous active view is suspended, below.
      record.unmounted=true;
    }
    if(generation!==routeGeneration)return;
    if(current&&current!==record){current.controller.deactivate();current.host.hidden=true;}
    current=record;Object.assign(record.context,route);record.host.hidden=false;
    if(record.unmounted){record.controller.mount(record.host.shadowRoot,record.context);record.unmounted=false;}
    record.controller.activate(record.context);
    document.title=record.title;
    if(push)history.pushState(null,'',route.url);else history.replaceState(null,'',route.url);
    errorBox.hidden=true;updateMenu();
    const heading=record.host.shadowRoot.querySelector('h1,.pane-title');if(heading){heading.setAttribute('tabindex','-1');heading.focus({preventScroll:true});}
  }catch(error){if(error.name!=='AbortError')showError(error.message+'，请重新选择页面重试。');}
}
document.addEventListener('click',event=>{
  if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
  const link=event.composedPath().find(item=>item instanceof HTMLAnchorElement);
  if(!link||link.target||link.hasAttribute('download'))return;
  const raw=link.getAttribute('href');if(!raw||raw.startsWith('#'))return;
  const route=parseRoute(link.href);if(!route)return;
  event.preventDefault();navigate(route.url);
});
window.addEventListener('popstate',()=>navigate(location.href,{push:false}));
document.addEventListener('visibilitychange',()=>{if(!current)return;if(document.hidden)current.controller.deactivate();else current.controller.activate(current.context);});
window.addEventListener('beforeunload',event=>{if(!dirtyNotice.hidden){event.preventDefault();event.returnValue='';}});
window.addEventListener('pagehide',()=>{for(const record of records.values())record.controller.deactivate();});
sessionSelect.addEventListener('change',()=>{if(!current)return;const session=sessionSelect.value;const view=current.context.view;navigate(view==='chat'?'/sessions/'+encodeURIComponent(session)+'/chats':(view==='dashboard'?'/':'/'+view)+'?session='+encodeURIComponent(session));});
themeSelect.addEventListener('change',()=>applyTheme(themeSelect.value));
translationSettings.addEventListener('click',()=>current?.host.shadowRoot.getElementById('translationSettingsButton')?.click());
new MutationObserver(()=>{const value=document.documentElement.dataset.theme;themeSelect.value=value;for(const record of records.values())record.host.dataset.theme=value;}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
applyTheme(localStorage.getItem('waha-panel-theme'));
navigate(location.href,{push:false});
fetch('/api/sessions',{cache:'no-store'}).then(response=>{if(response.status===401)authLost();return response.json();}).then(data=>{if(!authenticated)return;const value=current?.context.session||sessionSelect.value;sessionSelect.replaceChildren();for(const item of data.sessions||[]){const option=document.createElement('option');option.value=item.name;option.textContent=item.display_name||item.name;sessionSelect.append(option);}sessionSelect.value=value;}).catch(()=>{});
