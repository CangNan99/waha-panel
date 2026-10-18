"""Authenticated UI shell and a fixed set of view/module resources."""
import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .visual_theme import SHARED_CSS, ICON_SPRITE
except ImportError:
    from visual_theme import SHARED_CSS, ICON_SPRITE

VIEWS = frozenset({"dashboard", "chat", "settings", "overview", "logs"})
STATIC = Path(__file__).with_name("static")
ASSETS = {f"/assets/ui/{name}": STATIC / name for name in (
    "shell.js", "lifetime.js", "views/dashboard.js", "views/chat.js", "views/settings.js",
    "views/overview.js", "views/logs.js",
)}
SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _session(value):
    if not SESSION.fullmatch(str(value or "")):
        raise ValueError("会话技术名称无效")
    return value


def _legacy_document(view, session):
    # Import lazily: app.py owns the canonical legacy templates.
    try:
        from .app import multi_session_html_page, settings_page
        from .chat_page import chat_management_page
    except ImportError:
        from app import multi_session_html_page, settings_page
        from chat_page import chat_management_page
    if view == "dashboard":
        return multi_session_html_page()
    if view == "settings":
        return settings_page()
    if view == "chat":
        return chat_management_page(session)
    try:
        if view == "overview":
            from .overview_page import overview_page
            return overview_page()
        from .logs_page import logs_page
        return logs_page()
    except ImportError:
        try:
            if view == "overview":
                from overview_page import overview_page
                return overview_page()
            from logs_page import logs_page
            return logs_page()
        except ImportError:
            title = "状态概览" if view == "overview" else "系统日志"
            return f'<html><head><title>{title}</title></head><body><h1>{title}</h1><p>正在准备页面…</p></body></html>'


def view_payload(view, session="default"):
    if view not in VIEWS:
        raise ValueError("不支持的页面")
    _session(session)
    page = _legacy_document(view, session)
    body = re.search(r"<body\b[^>]*>([\s\S]*?)</body>", page, re.I).group(1)
    # Scripts are served only from the module allowlist, never executed from HTML.
    body = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", body, flags=re.I)
    styles = "\n".join(re.findall(r"<style[^>]*>([\s\S]*?)</style>", page, re.I))
    # Each native shadow root isolates selectors and duplicate legacy control IDs.
    styles = re.sub(r"(?<![\w.-])html\b", ":host", styles)
    styles = re.sub(r"(?<![\w.-])body\b", ".view-body", styles)
    styles = re.sub(r":host(\[[^\]]+\])", r":host(\1)", styles)
    styles = styles.replace(":root", ":host")
    styles += VIEW_LAYOUT_CSS
    title = html.unescape(re.search(r"<title>(.*?)</title>", page, re.S).group(1))
    return {"view": view, "session": session, "html": body, "css": styles,
            "title": title, "module": f"/assets/ui/views/{view}.js"}


def controller_source(view):
    if view not in ("dashboard", "settings", "chat"):
        raise ValueError("不支持的页面控制器")
    script = re.findall(r"<script>([\s\S]*?)</script>", _legacy_document(view, "default"))[-1].strip()
    if view == "chat":
        prefix = "(()=>{'use strict';"
        if not script.startswith(prefix) or not script.endswith("})();"):
            raise ValueError("聊天控制器结构无效")
        script = script[len(prefix):-len("})();")]
        resume = "messageFollowLatest=true;setMobileView(state.selected?'conversation':'list');loadChats().catch(()=>{});if(state.selected){requestAnimationFrame(scrollToBottom);loadMessages({open:true}).catch(()=>{});}connectEvents();"
        pause = "resetNoteWork();state.eventSource?.close();state.eventSource=null;stopPolling();stopMachineTranslation();state.messageAbort?.abort();messageResizeObserver?.disconnect();messageResizeObserver=null;messageScrollProgrammatic=false;state.messageLoading=false;state.chatLoading=false;state.messageReloadQueued=false;state.chatReloadQueued=false;"
        dispose = "removeImage();composerDraftByChat.clear();state.messageCache.clear();state.messages=[];state.chats=[];state.selected=null;state.translationByMessage.clear();state.csrf='';"
    elif view == "dashboard":
        resume = "refresh();"
        pause = "qrRequestGeneration++;pairingRequestGeneration++;releaseQrObjectUrl();$('qrImageLayer').replaceChildren();setQrState('idle','二维码待刷新','点击刷新二维码');"
        dispose = "csrfToken='';statusData=null;"
    else:
        resume = "if(!document.body.dataset.dirty)loadSettings().catch(()=>{});loadKnowledge().catch(()=>{});"
        pause = ""
        dispose = "csrfToken='';$('aiKey').value='';"
    # The adapter gives canonical code a root-scoped document and bounded resources.
    return (
        "import {createLegacyView} from '/assets/ui/lifetime.js';\n"
        "export function createView(){return createLegacyView(environment=>{\n"
        "const {document,window,location,history,fetch,EventSource,setTimeout,clearTimeout,setInterval,clearInterval,requestAnimationFrame,cancelAnimationFrame,navigateSession}=environment;\n"
        + script + "\nfunction applyTheme(){const theme=environment.sharedTheme();$('themeSelect').value=theme;}\n"
        + "return {resume(){" + resume + "},pause(){" + pause + "},dispose(){" + dispose + "}};\n});}\n"
    )


def asset_response(route):
    if route in ASSETS:
        return ASSETS[route].read_bytes(), "text/javascript; charset=utf-8"
    match = re.fullmatch(r"/assets/ui/controllers/(dashboard|settings|chat)\.js", route)
    if match:
        return controller_source(match.group(1)).encode(), "text/javascript; charset=utf-8"
    raise FileNotFoundError("页面资源不存在")


VIEW_LAYOUT_CSS = r'''
:host{display:block;min-width:0;height:100%;min-height:0}.view-body{height:100%;min-height:0;min-width:0!important;background:transparent!important}
.view-body>.app-navigation,.window>.app-navigation{display:none!important}
.window,.window[data-view="chat"]{margin:0!important;width:100%;height:100%!important;min-height:0!important;border:0;border-radius:0;grid-template-columns:minmax(0,1fr)!important}
.window[data-view="dashboard"]{grid-template-rows:66px minmax(0,1fr)}
.window[data-view="dashboard"] .dashboard-workspace{grid-column:1;grid-row:2}
.window[data-view="chat"]{grid-template-rows:minmax(0,1fr)!important}
.window[data-view="chat"] .workspace.chat-grid{grid-column:1;grid-row:1;padding:14px}
.shell{max-width:none;margin:0;height:100%;overflow:auto;padding:24px}
.view-body>h1,.view-body>p{margin:24px}
@media(max-width:820px){.window[data-view="chat"] .workspace.chat-grid{padding:0}.window[data-view="dashboard"]{grid-template-rows:auto minmax(0,1fr)}.shell{padding:16px}}
'''


def app_shell_page(initial_path="/"):
    parsed = urlparse(initial_path)
    query = parse_qs(parsed.query)
    session = query.get("session", ["default"])[0]
    if parsed.path.startswith("/sessions/"):
        session = parsed.path.split("/")[2]
    _session(session)
    return '''<!doctype html><html lang="zh-CN" data-theme="daylight"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark"><title>WhatsApp AI 管理面板</title><style>''' + SHARED_CSS + SHELL_CSS + '''</style></head><body>''' + ICON_SPRITE + '''
<div class="application-shell" id="applicationShell">
<aside class="shell-navigation" aria-label="主导航"><div class="shell-brand"><div class="brand-mark">W</div><div><strong>WhatsApp AI</strong><span>管理工作区</span></div></div>
<nav class="shell-menu">''' + "".join(
        f'<a href="{href}" data-shell-nav="{view}"><span class="shell-menu-icon" aria-hidden="true"><svg class="icon"><use href="#{symbol}"/></svg></span><span>{label}</span></a>'
        for view, href, label, symbol in (
            ("dashboard", "/", "工作台", "ui-home"), ("chat", f"/sessions/{html.escape(session)}/chats", "聊天管理", "ui-chat"),
            ("settings", "/settings", "自动回复", "ui-bolt"), ("overview", "/overview?scope=all", "状态概览", "ui-chart"),
            ("logs", "/logs", "系统日志", "ui-file"),
        )
    ) + '''</nav><div class="shell-context"><label for="shellSession">当前会话</label><select id="shellSession" aria-label="当前会话"><option value="''' + html.escape(session) + '''">''' + html.escape(session) + '''</option></select><label for="shellTheme">界面主题</label><select id="shellTheme" aria-label="界面主题"><option value="daylight">白天</option><option value="night">夜间</option></select><button id="shellTranslationSettings" type="button" hidden>机器翻译设置</button><div id="shellDirtyNotice" hidden role="status">当前设置尚未保存</div></div></aside>
<main class="shell-content" id="shellContent" aria-live="off"></main></div>
<div class="shell-error" id="shellError" hidden role="alert"></div>
<script type="module" src="/assets/ui/shell.js"></script></body></html>'''


SHELL_CSS = r'''
html,body{height:100%;min-width:320px}.application-shell{margin:20px 24px;height:calc(100dvh - 40px);display:grid;grid-template-columns:190px minmax(0,1fr);border:1px solid var(--white-line);border-radius:25px;overflow:hidden;background:rgba(248,250,255,.68);box-shadow:0 20px 80px rgba(96,111,177,.055)}
.shell-navigation{display:flex;flex-direction:column;gap:30px;padding:24px 13px;background:linear-gradient(150deg,#e5e1fc,#e6f2ff 72%,#dcf1f9);border-right:1px solid var(--line);min-height:0;overflow:auto}
.shell-brand{display:flex;gap:10px;align-items:center;padding:0 4px}.shell-brand strong{font-size:15px;display:block}.shell-brand span{font-size:11px;color:var(--muted);display:block;margin-top:3px}.shell-menu{display:flex;flex-direction:column;gap:8px}.shell-menu a{display:flex;align-items:center;gap:12px;min-height:45px;padding:0 12px;color:var(--secondary);text-decoration:none;border-radius:13px;font-weight:600;font-size:13px}.shell-menu a:hover{background:rgba(255,255,255,.4)}.shell-menu a[aria-current="page"]{background:var(--selected);color:var(--blue);box-shadow:inset 0 0 0 1px var(--white-line)}.shell-menu-icon{font-size:23px;width:23px;text-align:center;font-weight:400}
.shell-context{margin-top:auto;display:grid;gap:7px}.shell-context label{font-size:11px;color:var(--muted)}.shell-context select{width:100%;min-height:35px;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:0 8px}.shell-content{min-width:0;min-height:0;overflow:hidden;position:relative}.view-host[hidden]{display:none}.view-host{display:block;width:100%;height:100%;min-height:0}.shell-loading{padding:36px;color:var(--muted)}.shell-error{position:fixed;z-index:100;bottom:18px;left:50%;transform:translateX(-50%);padding:14px 18px;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);max-width:calc(100% - 24px)}#shellDirtyNotice{padding:8px;border-radius:9px;background:var(--soft);font-size:11px;color:var(--violet)}
html[data-theme="night"] .application-shell{background:#182034}html[data-theme="night"] .shell-navigation{background:linear-gradient(150deg,#373457,#273d57)}
@media(max-width:820px){.application-shell{margin:0;height:100dvh;border:0;border-radius:0;grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}.shell-navigation{padding:9px 12px;gap:8px;overflow:visible;border-right:0;border-bottom:1px solid var(--line);display:grid;grid-template-columns:minmax(0,1fr) auto}.shell-brand{padding:0}.shell-brand strong{font-size:12px}.shell-brand span{display:none}.shell-menu{grid-column:1/-1;grid-row:2;display:flex;flex-direction:row;gap:4px}.shell-menu a{flex:1;justify-content:center;font-size:11px;min-height:35px;padding:0 4px;gap:4px}.shell-menu-icon{font-size:17px;width:17px}.shell-context{grid-column:2;grid-row:1;display:flex;gap:5px;align-items:center}.shell-context label{display:none}.shell-context select{width:auto;max-width:115px;min-height:32px;font-size:11px;padding:0 5px}.brand-mark{width:31px;height:31px}.shell-context #shellDirtyNotice{position:absolute;top:80px;right:12px;z-index:20}.shell-content{height:100%}}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
'''

SHELL_CSS += r'''
#shellTranslationSettings{font-size:11px;min-height:36px;margin-top:7px}
@media(max-width:820px){.shell-context{display:grid;grid-template-columns:minmax(65px,1fr) 60px}.shell-context select{width:100%;max-width:115px}#shellTranslationSettings{grid-column:1/-1;margin-top:0;min-height:32px}.shell-context #shellDirtyNotice{grid-column:1/-1}}
'''
