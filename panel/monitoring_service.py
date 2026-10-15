"""Read-only session monitoring and bounded, redacted log queries."""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

LEVELS = frozenset({'INFO', 'WARNING', 'WARN', 'ERROR', 'CRITICAL', 'DEBUG'})
SESSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
EMPTY_AUTOMATION = dict.fromkeys(('human_takeover','pending','overdue','running','failed','unknown'))


def safe_log_message(value, secrets=()):
    text = str(value or '')[:8000]
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), '[已隐藏]')
    if 'PRIVATE KEY' in text or 'data:image/' in text.lower():
        return '[敏感内容已隐藏]'
    sensitive_key = re.compile(r'authorization|cookie|api[_-]?key|token|password|passwd|secret|credential|qr[_-]?code', re.I)
    def redact_json(item):
        if isinstance(item, dict):
            return {key: '[已隐藏]' if sensitive_key.search(key) else '[消息内容已隐藏]' if key.lower() in {'body','caption','text','message_content'} else redact_json(child) for key, child in item.items()}
        if isinstance(item, list):
            return [redact_json(child) for child in item]
        return item
    try:
        structured = json.loads(text)
        if isinstance(structured, (dict, list)):
            text = json.dumps(redact_json(structured), ensure_ascii=False)
    except (ValueError, RecursionError):
        pass
    # Consume complete quoted values, including spaces and escaped quotes.
    # Bare values conservatively consume the rest of the field/line.
    text = re.sub(r'''(?ix)(["']?[\w-]*(?:authorization|cookie|api[_-]?key|token|password|passwd|secret|credential|qr[_-]?code)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;]+)''', r'\1[已隐藏]', text)
    text = re.sub(r'''(?ix)(\b(?:body|caption|text|message_content)|客户消息|消息正文|客户原文)["']?\s*[:=]\s*[^\r\n]*''', '[消息内容已隐藏]', text)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/_.=-]+', '[认证内容已隐藏]', text)
    text = re.sub(r'\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)\bhttps?://[^\s/]*@[^\s]+', '[含凭据的地址已隐藏]', text)
    text = re.sub(r'(?i)\bhttps?://(?:localhost|127\.[\d.]+|10\.[\d.]+|192\.168\.[\d.]+|[A-Za-z_-]+)(?::\d+)?(?=/|\s|$)[^\s]*', '[内部服务地址已隐藏]', text)
    text = re.sub(r'(?<!\w)\+?\d[\d ()-]{5,}\d(?:@(?:c\.us|s\.whatsapp\.net|g\.us))?', '[号码已隐藏]', text)
    text = re.sub(r'(?i)\b[a-f0-9]{32,}\b', '[标识已隐藏]', text)
    return text[:4000]


class MonitoringService:
    def __init__(self, state, clock=time.time, cache_seconds=5):
        self.state = state
        self.database_path = Path(state.database_path)
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._snapshot = None
        self._secrets = (getattr(state, 'api_key', ''),)

    def _connect(self):
        connection = sqlite3.connect(self.database_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.create_function('safe_log', 1, lambda value: safe_log_message(value, self._secrets), deterministic=True)
        return connection

    @staticmethod
    def _scope(allowed_sessions, selected=None):
        names = list(dict.fromkeys(str(name) for name in allowed_sessions))
        if any(not SESSION_RE.fullmatch(name) for name in names):
            raise ValueError('会话范围无效')
        if selected:
            if selected not in names:
                raise PermissionError('无权查看该会话')
            return [selected]
        return names

    def _waha_snapshot(self, now):
        with self._lock:
            if self._snapshot and 0 <= now-self._snapshot['checked_at'] < self.cache_seconds:
                return self._snapshot
            errors = []
            health = {'ok': False, 'checked_at': now}
            version = {'ok': False, 'checked_at': now, 'version': None}
            remote = None
            try:
                result = self.state.client.get_health()
                health['ok'] = not (isinstance(result, dict) and str(result.get('status','')).lower() in {'error','failed','unhealthy'})
                if not health['ok']:
                    errors.append({'component':'health','message':'WAHA 健康接口报告异常'})
            except Exception:
                errors.append({'component':'health','message':'WAHA 健康接口暂不可用'})
            try:
                result = self.state.client.get_version()
                version.update(ok=True, version=str(result.get('version',''))[:80] if isinstance(result,dict) else None)
            except Exception:
                errors.append({'component':'version','message':'WAHA 版本接口暂不可用'})
            try:
                result = self.state.client.get_sessions()
                if isinstance(result,dict):
                    result = result.get('sessions',result.get('items'))
                if not isinstance(result,list):
                    raise ValueError('invalid session result')
                remote = {str(item.get('name')):str(item.get('status') or item.get('state') or 'UNKNOWN').upper() for item in result if isinstance(item,dict)}
            except Exception:
                errors.append({'component':'sessions','message':'会话状态暂不可用'})
            state = 'available' if health['ok'] and version['ok'] else 'degraded' if health['ok'] or version['ok'] else 'unavailable'
            self._snapshot = {'checked_at':now,'service':{'state':state,'health':health,'version_check':version},'remote':remote,'errors':errors}
            return self._snapshot

    def overview(self, allowed_sessions, selected_session=None, now=None):
        now = int(self.clock() if now is None else now)
        names = self._scope(allowed_sessions, selected_session)
        snapshot = self._waha_snapshot(now)
        errors = list(snapshot['errors'])
        result = {'scope':selected_session or 'all','generated_at':now,
                  'services':{'waha':snapshot['service'],'database':{'ok':False,'checked_at':now}},
                  'session_counts':{'total':None,'connected':None,'scan_qr':None,'failed':None},
                  'sessions':[],'automation_counts':dict(EMPTY_AUTOMATION),'recent_error_count':None,
                  'recent_errors':[],'partial_errors':errors}
        connection = None
        try:
            connection = self._connect()
            connection.execute('SELECT 1').fetchone()
            result['services']['database']['ok'] = True
            placeholders = ','.join('?' for _ in names) or 'NULL'
            rows = connection.execute(f'SELECT session_name,display_name FROM managed_sessions WHERE session_name IN ({placeholders}) ORDER BY id',names).fetchall()
            if len(rows) != len(names):
                raise PermissionError('会话范围已变化，请刷新页面')
            settings = {}
            for row in connection.execute(f'SELECT session_name,key,value FROM session_settings WHERE session_name IN ({placeholders})',names):
                settings.setdefault(row['session_name'],{})[row['key']] = row['value']
            for row in rows:
                name=row['session_name']
                values=settings.get(name,{})
                truth=lambda key:str(values.get(key,'')).lower() in {'1','true','yes','on'}
                all_day,enabled=truth('auto_reply_all_day'),truth('auto_reply_enabled')
                status=snapshot['remote'].get(name,'NOT_CREATED') if snapshot['remote'] is not None else 'UNKNOWN'
                result['sessions'].append({'name':name,'display_name':row['display_name'] or name,'state':status,
                    'checked_at':snapshot['checked_at'], 'auto_reply':{'enabled':enabled,'all_day':all_day,
                    'mode':'all_day' if all_day else 'scheduled' if enabled else 'off',
                    'ai_configured':bool(values.get('ai_api_key') and values.get('ai_base_url') and values.get('ai_model')),
                    'customer_memory_enabled':truth('customer_memory_enabled')}})
            result['session_counts']['total']=len(rows)
            if snapshot['remote'] is not None:
                result['session_counts'].update(connected=sum(x['state'] in {'WORKING','CONNECTED'} for x in result['sessions']),
                                               scan_qr=sum(x['state']=='SCAN_QR_CODE' for x in result['sessions']),
                                               failed=sum(x['state']=='FAILED' for x in result['sessions']))
            try:
                human=connection.execute(f"SELECT COUNT(*) FROM chat_takeovers WHERE session_name IN ({placeholders}) AND state='HUMAN_TAKEOVER' AND (auto_resume_at IS NULL OR auto_resume_at>?)",[*names,now]).fetchone()[0]
                tasks={row['state']:row['amount'] for row in connection.execute(f'SELECT state,COUNT(*) AS amount FROM follow_up_tasks WHERE session_name IN ({placeholders}) GROUP BY state',names)}
                overdue=connection.execute(f"SELECT COUNT(*) FROM follow_up_tasks WHERE session_name IN ({placeholders}) AND state='PENDING' AND due_at<=?",[*names,now]).fetchone()[0]
                result['automation_counts']={'human_takeover':human,'pending':tasks.get('PENDING',0),'overdue':overdue,
                                             'running':tasks.get('RUNNING',0),'failed':tasks.get('FAILED',0),'unknown':tasks.get('UNKNOWN',0)}
            except sqlite3.Error:
                errors.append({'component':'automation','message':'自动化任务统计暂不可用'})
            try:
                result['recent_error_count']=connection.execute(f"SELECT COUNT(*) FROM system_logs WHERE session_name IN ({placeholders}) AND level IN ('ERROR','CRITICAL') AND created_at>=? AND created_at<=?",[*names,now-86400,now]).fetchone()[0]
                events=connection.execute(f"SELECT id,session_name,level,event,safe_log(message) AS message,created_at FROM system_logs WHERE session_name IN ({placeholders}) AND level IN ('WARNING','WARN','ERROR','CRITICAL') AND created_at>=? AND created_at<=? ORDER BY id DESC LIMIT 10",[*names,now-86400,now]).fetchall()
                result['recent_errors']=[dict(row) for row in events]
            except sqlite3.Error:
                errors.append({'component':'logs','message':'异常日志统计暂不可用'})
        except sqlite3.Error:
            result['services']['database']['ok']=False
            errors.append({'component':'database','message':'数据库暂不可访问'})
        finally:
            if connection is not None:
                connection.close()
        return result

    def query_logs(self, allowed_sessions, filters):
        now=int(self.clock())
        names=self._scope(allowed_sessions,filters.get('session') if filters.get('scope')!='all' else None)
        def integer(key,default,minimum=0):
            value=filters.get(key,default)
            if value is None:
                return None
            if not re.fullmatch(r'\d{1,16}',str(value)) or int(value)<minimum:
                raise ValueError('日志数值参数无效')
            return int(value)
        limit=integer('limit',50,1)
        if limit>100:
            raise ValueError('每页最多 100 条日志')
        end=integer('to',now)
        start=integer('from',max(0,end-86400))
        if end<start or end-start>30*86400:
            raise ValueError('日志时间范围必须为 0 至 30 天')
        before,after=integer('before_id',None,1),integer('after_id',None,0)
        if before is not None and after is not None:
            raise ValueError('不能同时使用两个翻页方向')
        level=str(filters.get('level','')).upper()
        if level and level not in LEVELS:
            raise ValueError('日志级别无效')
        event=str(filters.get('event',''))
        keyword=str(filters.get('q','')).strip()
        if len(event)>100 or len(keyword)>100:
            raise ValueError('日志搜索条件最多 100 个字符')
        placeholders=','.join('?' for _ in names) or 'NULL'
        clauses=[f'session_name IN ({placeholders})','created_at>=?','created_at<=?']
        parameters=[*names,start,end]
        if level:
            clauses.append("level IN ('WARN','WARNING')" if level in {'WARN','WARNING'} else 'level=?')
            if level not in {'WARN','WARNING'}:
                parameters.append(level)
        if event:
            clauses.append('event=?');parameters.append(event)
        if keyword:
            clauses.append("safe_log(message) LIKE ? ESCAPE '\\'")
            parameters.append('%'+keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        if before is not None:
            clauses.append('id<?');parameters.append(before)
        if after is not None:
            clauses.append('id>?');parameters.append(after)
        direction='ASC' if after is not None else 'DESC'
        connection=self._connect()
        try:
            rows=connection.execute('SELECT id,session_name,level,event,safe_log(message) AS message,created_at FROM system_logs WHERE '+ ' AND '.join(clauses)+f' ORDER BY id {direction} LIMIT ?',[*parameters,limit+1]).fetchall()
        finally:
            connection.close()
        has_more=len(rows)>limit
        visible=rows[:limit]
        cursor=visible[-1]['id'] if has_more and visible else None
        if after is not None:
            if visible:
                cursor=visible[-1]['id']
            visible=list(reversed(visible))
        return {'logs':[dict(row) for row in visible],'next_cursor':cursor,'has_more':has_more,
                'generated_at':now,'from':start,'to':end}
