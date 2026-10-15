// Bound asynchronous resources for a root-scoped legacy controller.
const nativeDocument=globalThis.document;
const nativeWindow=globalThis.window;
let nextId=1;

export function createLifetime(root,context){
  let active=false,disposed=false,generation=0;
  const timers=new Map(),frames=new Map(),requests=new Set(),sources=new Set(),listeners=[];
  const guard=fn=>(...args)=>{if(active&&!disposed)return fn(...args);};
  const schedule=(fn,ms,repeat,args)=>{
    if(disposed)return 0;
    const id=nextId++;
    const record={fn,ms,repeat,args,native:0};
    timers.set(id,record);
    const run=()=>{if(!repeat)timers.delete(id);if(active&&!disposed)fn(...args);};
    if(active)record.native=(repeat?nativeWindow.setInterval:nativeWindow.setTimeout)(run,ms);
    return id;
  };
  const cancel=id=>{const item=timers.get(id);if(item){nativeWindow.clearTimeout(item.native);nativeWindow.clearInterval(item.native);timers.delete(id);}};
  const addListener=(target,type,listener,options)=>{
    const bound=guard(listener);target.addEventListener(type,bound,options);listeners.push([target,type,bound,options]);
  };
  const documentProxy=new Proxy(nativeDocument,{get(target,key){
    if(key==='getElementById')return id=>root.querySelector('[id="'+CSS.escape(id)+'"]');
    if(key==='querySelector')return selector=>root.querySelector(selector);
    if(key==='querySelectorAll')return selector=>root.querySelectorAll(selector);
    if(key==='body')return root.querySelector('.view-body');
    if(key==='hidden')return !active||nativeDocument.hidden;
    if(key==='addEventListener')return (type,listener,options)=>addListener(target,type,listener,options);
    const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  const windowProxy=new Proxy(nativeWindow,{get(target,key){
    if(key==='addEventListener')return (type,listener,options)=>addListener(target,type,listener,options);
    if(key==='document')return documentProxy;
    const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  const locationProxy=new Proxy({}, {get(_target,key){
    if(key==='href')return context.url;
    if(key==='origin')return nativeWindow.location.origin;
    return new URL(context.url)[key];
  },set(_target,key,value){if(key==='href'){context.navigate(String(value));return true;}return false;}});
  const historyProxy={replaceState(_state,_title,url){if(active&&url)context.replaceURL(String(url));},pushState(_state,_title,url){if(active&&url)context.navigate(String(url));}};
  const scopedFetch=async(url,options={})=>{
    if(!active||disposed)throw new DOMException('页面已离开','AbortError');
    const epoch=generation,controller=new AbortController();
    const method=String(options.method||'GET').toUpperCase();
    const entry={controller,read:method==='GET'||method==='HEAD'};requests.add(entry);
    const abort=()=>controller.abort();options.signal?.addEventListener('abort',abort,{once:true});
    if(options.signal?.aborted)controller.abort();
    const cleanup=()=>{requests.delete(entry);options.signal?.removeEventListener('abort',abort);};
    const assertCurrent=()=>{if(disposed||(entry.read&&(!active||epoch!==generation)))throw new DOMException('页面已切换','AbortError');};
    try{
      const response=await nativeWindow.fetch(url,{...options,signal:controller.signal});
      if(response.status===401)context.authLost();
      assertCurrent();
      return new Proxy(response,{get(target,key){
        if(['json','blob','text','arrayBuffer','formData'].includes(key))return async()=>{try{const value=await target[key]();assertCurrent();return value;}finally{cleanup();}};
        const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
      }});
    }catch(error){cleanup();throw error;}
  };
  class ScopedEventSource{
    constructor(url,options){if(!active||disposed)throw new DOMException('页面已离开','AbortError');this.source=new nativeWindow.EventSource(url,options);sources.add(this);}
    addEventListener(type,callback,options){this.source.addEventListener(type,guard(callback),options);}
    close(){this.source.close();sources.delete(this);}
    set onopen(fn){this.source.onopen=guard(fn);}
    set onerror(fn){this.source.onerror=guard(fn);}
    set onmessage(fn){this.source.onmessage=guard(fn);}
    get readyState(){return this.source.readyState;}
  }
  const environment={document:documentProxy,window:windowProxy,location:locationProxy,history:historyProxy,fetch:scopedFetch,EventSource:ScopedEventSource,sharedTheme:()=>nativeDocument.documentElement.dataset.theme,
    setTimeout:(fn,ms,...args)=>schedule(fn,ms,false,args),clearTimeout:cancel,
    setInterval:(fn,ms,...args)=>schedule(fn,ms,true,args),clearInterval:cancel,
    requestAnimationFrame:fn=>{const id=nextId++;if(active)frames.set(id,nativeWindow.requestAnimationFrame(t=>{frames.delete(id);if(active&&!disposed)fn(t);}));return id;},
    cancelAnimationFrame:id=>{nativeWindow.cancelAnimationFrame(frames.get(id));frames.delete(id);}};
  function activate(){if(disposed||active)return;active=true;generation++;for(const [id,item]of timers){if(item.repeat)item.native=nativeWindow.setInterval(()=>{if(active&&!disposed)item.fn(...item.args);},item.ms);else timers.delete(id);}}
  function deactivate(){if(!active)return;active=false;generation++;for(const entry of requests)if(entry.read)entry.controller.abort();for(const source of [...sources])source.close();for(const [id,item]of timers){nativeWindow.clearTimeout(item.native);nativeWindow.clearInterval(item.native);if(!item.repeat)timers.delete(id);}for(const id of frames.values())nativeWindow.cancelAnimationFrame(id);frames.clear();for(const dialog of root.querySelectorAll('dialog[open]'))dialog.close();}
  function dispose(){deactivate();disposed=true;for(const item of requests)item.controller.abort();requests.clear();timers.clear();for(const item of listeners)item[0].removeEventListener(item[1],item[2],item[3]);listeners.length=0;}
  return {environment,activate,deactivate,dispose,get active(){return active;}};
}

export function createLegacyView(factory){
  let lifetime,controller,first=true;
  return {
    mount(root,context){lifetime=createLifetime(root,context);lifetime.activate();root.getElementById('themeSelect')?.addEventListener('change',event=>context.setTheme(event.target.value));controller=factory(lifetime.environment);},
    activate(){if(first){first=false;return;}lifetime.activate();controller.resume?.();},
    deactivate(){controller.pause?.();lifetime.deactivate();},
    dispose(){controller.pause?.();controller.dispose?.();lifetime.dispose();}
  };
}
