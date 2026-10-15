"""Local UI acceptance server. Synthetic data only; never opens a business DB."""
import argparse
import io
import json
import re
import sys
import sqlite3
import tempfile
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panel.app import multi_session_html_page, settings_page
from panel.chat_page import chat_management_page
from PIL import Image, ImageDraw

NOW = 1792022400
SESSIONS = [
    {"name": "default", "display_name": "测试工作区", "state": "SCAN_QR_CODE", "connected": False,
     "exists": True, "created_at": NOW - 3600, "updated_at": NOW,
     "auto_reply": {"enabled": True, "all_day": True, "ai_configured": True}},
    {"name": "support", "display_name": "客服测试", "state": "WORKING", "connected": True,
     "exists": True, "created_at": NOW - 3600, "updated_at": NOW,
     "auto_reply": {"enabled": True, "all_day": False, "ai_configured": False}},
]
CHAT_ITEMS = [
    {"chat_ref": "customer-a", "name": "客户甲", "display_id": "客户 A", "note": "报价跟进",
     "labels": [{"source": "ai", "label": "高意向"}], "timestamp": NOW, "last_message": "请提供最新报价",
     "unread_count": 2, "takeover_state": "AI_ELIGIBLE", "is_group": False},
    {"chat_ref": "customer-b", "name": "客户乙", "display_id": "客户 B", "note": "",
     "labels": [], "timestamp": NOW - 60, "last_message": "确认产品尺寸", "unread_count": 0,
     "takeover_state": "HUMAN_TAKEOVER", "is_group": False},
]


def image_bytes(tall=False):
    image = Image.new("RGB", (420, 720) if tall else (330, 330), "white")
    draw = ImageDraw.Draw(image)
    if tall:
        draw.rectangle((10, 10, 410, 710), fill="#bcd5f6")
        draw.text((30, 30), "SYNTHETIC TEST IMAGE", fill="#182033")
    else:
        # Deliberately synthetic checker pattern, not a login or valid QR payload.
        for y in range(24, 306, 6):
            for x in range(24, 306, 6):
                if (x // 6 * 13 + y // 6 * 7) % 5 < 3:
                    draw.rectangle((x, y, x + 5, y + 5), fill="black")
        for x, y in ((24, 24), (264, 24), (24, 264)):
            draw.rectangle((x, y, x + 42, y + 42), fill="black")
            draw.rectangle((x + 6, y + 6, x + 36, y + 36), fill="white")
            draw.rectangle((x + 12, y + 12, x + 30, y + 30), fill="black")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    counters = Counter()
    controls = {"delay_media": 0, "delay_metadata": 0, "fail": "", "latest": 80, "event_type": "heartbeat"}
    notes = {'customer-a':'报价跟进','customer-b':''}

    @classmethod
    def prepare_monitoring(cls):
        from types import SimpleNamespace
        from panel.app import init_db
        from panel.monitoring_service import MonitoringService
        cls.monitor_temp=tempfile.TemporaryDirectory()
        database=Path(cls.monitor_temp.name)/'synthetic.sqlite3'
        init_db(database,seed_business=False)
        connection=sqlite3.connect(database)
        now=int(time.time())
        try:
            for item in SESSIONS:
                connection.execute('INSERT OR IGNORE INTO managed_sessions(session_name,display_name,created_at,updated_at,is_active) VALUES (?,?,?,?,1)',(item['name'],item['display_name'],now,now))
                connection.execute('UPDATE managed_sessions SET display_name=? WHERE session_name=?',(item['display_name'],item['name']))
            for n in range(100):
                level='ERROR' if n%5==0 else 'WARNING' if n%7==0 else 'INFO'
                connection.execute('INSERT INTO system_logs(session_name,level,event,message,created_at) VALUES (?,?,?,?,?)',('support' if n%2 else 'default',level,'session.start' if level=='INFO' else 'session.check','测试记录 '+str(n)+' · '+('模拟连接超时' if level=='ERROR' else '处理完成'),now-(100-n)*10))
            connection.commit()
        finally:
            connection.close()
        client=SimpleNamespace(get_health=lambda:{'status':'ok'},get_version=lambda:{'version':'测试版本'},get_sessions=lambda:[{'name':x['name'],'status':x['state']} for x in SESSIONS])
        cls.monitoring=MonitoringService(SimpleNamespace(database_path=database,client=client,api_key=''))

    def log_message(self, *_args):
        pass

    def send(self, value, status=200, mime="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False).encode() if "json" in mime else value.encode())
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path, q = parsed.path, parse_qs(parsed.query)
        self.counters[path] += 1
        if path == "/__fixture__/metrics":
            return self.send({"requests": dict(self.counters), "controls": self.controls})
        if path == self.controls["fail"]:
            return self.send({"message": "模拟网络故障"}, int(self.controls.get('fail_status',503)))
        if path.startswith("/assets/ui/") or path.startswith("/api/ui/view") or path in ("/overview", "/logs"):
            try:
                from panel.app_shell import asset_response, view_payload, app_shell_page
                if path.startswith("/assets/ui/"):
                    data, mime = asset_response(path)
                    return self.send(data, mime=mime)
                if path == "/api/ui/view":
                    return self.send(view_payload(q.get("view", ["dashboard"])[0], q.get("session", ["default"])[0]))
                return self.send(app_shell_page(self.path), mime="text/html; charset=utf-8")
            except (ImportError, ValueError, FileNotFoundError):
                return self.send({"message": "测试资源尚未实现"}, 404)
        if path in ("/", "/settings") or re.fullmatch(r"/sessions/[\w.-]+/chats", path):
            try:
                from panel.app_shell import app_shell_page
                page = app_shell_page(self.path)
            except ImportError:
                page = multi_session_html_page() if path == "/" else settings_page() if path == "/settings" else chat_management_page(path.split("/")[2])
            return self.send(page, mime="text/html; charset=utf-8")
        if path == "/api/security/csrf":
            return self.send({"csrf_token": "fixture-only"})
        if path == "/api/sessions":
            return self.send({"sessions": SESSIONS})
        if path == "/api/status":
            selected = q.get("session", ["default"])[0]
            item = next(x for x in SESSIONS if x["name"] == selected)
            return self.send({"sessions": SESSIONS, "selected_session": selected, "whatsapp": item,
                              "waha": {"running": True, "version": "测试版本"}, "database": {"ok": True},
                              "logs": [], "errors": [], "auto_reply": {"theme":"daylight","enabled":True,"all_day":True,"ai_configured":True}})
        if path == '/api/overview':
            selected=q.get('session',[None])[0]
            return self.send(self.monitoring.overview([x['name'] for x in SESSIONS],selected))
        if path == '/api/logs':
            filters={key:value[0] for key,value in q.items()}
            try:
                return self.send(self.monitoring.query_logs([x['name'] for x in SESSIONS],filters))
            except (ValueError,PermissionError) as error:
                return self.send({'message':str(error)},400)
        if re.fullmatch(r"/api/sessions/[\w.-]+", path):
            return self.send(next((x for x in SESSIONS if x["name"] == path.split("/")[-1]), SESSIONS[0]))
        if path.endswith("/qr"):
            return self.send(image_bytes(), mime="image/png")
        if path.endswith("/avatar"):
            return self.send({}, 404)
        if path == "/api/settings":
            return self.send({"auto_reply_enabled": True, "auto_reply_all_day": True, "theme": "daylight",
                              "customer_memory_enabled": True, "weekly_reply_windows": {}, "auto_reply_context_per_side": 5,
                              "auto_reply_media_types": {"image": True, "audio": False, "video": False, "file": False},
                              "default_reply_text": "您好，我们会尽快回复。", "persona": "中文客服", "system_prompt": "",
                              "ai_base_url": "", "ai_model": "测试模型", "ai_api_key_configured": True})
        if path == "/api/knowledge":
            return self.send({"items": []})
        if path.startswith("/api/translation"):
            return self.send({"access_key_id_configured": True, "access_key_secret_configured": True,
                              "api_key_configured": True, "base_url": "", "model": "测试模型", "last_test_status": "never"})
        if path.startswith("/api/chat/sessions/"):
            action = "/".join(path.split("/")[5:])
            chat_ref = q.get("chat_ref", ["customer-a"])[0]
            if action == "overview":
                return self.send({"items": CHAT_ITEMS, "next_offset": 2, "has_more": False})
            if action == "messages":
                count = int(self.controls["latest"])
                offset = int(q.get("offset", [0])[0])
                end = max(0, count - offset)
                start = max(0, end - 50)
                items = [{"message_ref": f"{chat_ref}-{n}", "timestamp": NOW - (count - n) * 60,
                          "from_me": n % 3 == 0, "body": f"第 {n} 条测试消息 · {chat_ref}" + ("\n较长的测试内容。" * 5 if n % 7 == 0 else ""),
                          "caption": "", "has_media": n == count, "media_type": "image/png" if n == count else ""}
                         for n in range(start + 1, end + 1)]
                return self.send({"items": items, "next_offset": offset + len(items), "has_more": start > 0,
                                  "takeover": {"state": "AI_ELIGIBLE"}})
            if action == "media":
                time.sleep(float(self.controls["delay_media"]))
                return self.send(image_bytes(True), mime="image/png")
            if action == "note":
                time.sleep(float(self.controls["delay_metadata"]))
                return self.send({"note": self.notes.get(chat_ref,'')})
            if action in ("labels", "follow-ups", "machine-translations", "memory"):
                return self.send({"manual": [], "ai": [], "items": [], "enabled": False})
            if action == "summary":
                return self.send({})
            if action == "events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    kind = self.controls["event_type"]
                    self.wfile.write((f'event: {kind}\ndata: {{"type":"message","from_me":false}}\n\n').encode())
                    self.wfile.flush()
                    time.sleep(5)
                except OSError:
                    pass
                return
        if path.endswith("/logs"):
            return self.send({"logs": [], "next_cursor": None, "has_more": False})
        if path == "/favicon.ico":
            return self.send(b"", mime="image/x-icon")
        return self.send({"message": "未知测试接口"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get('Content-Type','').startswith('multipart/form-data'):
            return self.send({'state':'SENT','message':'合成图片已接收'})
        data = json.loads(raw or b"{}")
        if self.path == "/__fixture__/control":
            self.controls.update(data)
            return self.send(self.controls)
        if self.path == "/__fixture__/reload":
            import importlib
            for module in ("panel.internal_language", "panel.monitoring_service", "panel.visual_theme", "panel.chat_page", "panel.app", "panel.app_shell"):
                if module in sys.modules:
                    importlib.reload(sys.modules[module])
            return self.send({"reloaded": True})
        if self.path.endswith('/notes/normalize'):
            return self.send({'note_zh':'客户希望购买 iPhone 16，预算 800 USD'})
        if self.path.endswith('/note'):
            self.notes[data.get('chat_ref')]=data.get('note','')
            time.sleep(float(self.controls.get('delay_note_save',0)))
            return self.send({'note':data.get('note','')})
        return self.send({"state": "SENT", "note": data.get("note", ""), "message": "测试保存成功"})

    do_PUT = do_POST
    do_PATCH = do_POST


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    Handler.prepare_monitoring()
    print(f"FIXTURE_READY http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
