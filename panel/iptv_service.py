"""Fixed-supplier demo accounts, encrypted at rest, with durable at-most-once creation."""
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

PROVIDER = 'activationpanel'
ENDPOINT = 'https://activationpanel.ru/api/api.php'
TRIAL_SECONDS = 12 * 60 * 60
LANGUAGES = ('zh', 'en', 'pt', 'es', 'fr', 'ar', 'tr', 'de', 'ru', 'it')
TZ = ZoneInfo('Asia/Shanghai')
TRIAL_SUBSCRIPTIONS = ('99', '1', '3', '6', '12')
_AFFIRMATIVE = re.compile(
    r'^(?:yes(?: please)?|y|ok(?:ay)?(?: please)?|sure|please(?: do)?|send(?: it)?|go ahead|si|sí|sim|oui|да|تمام|نعم|是的|好的|可以|请发|お願いします|네(?: 부탁합니다)?|ja|ja graag|được|vâng|iya|ya)[\s!,.。！？，؟]*$',
    re.I,
)
_NEGATIVE = re.compile(
    r'不要|不需要|不用|不想|取消|别开|拒绝|do not|don\'t|dont|no quiero|no necesito|não quero|nao quero|ne veux pas|je ne veux pas|لا أريد|не хочу|no thanks|not interested|不要',
    re.I,
)
_PRICING = re.compile(
    r'多少钱|价格|费用|怎么收费|\b(?:price|cost|pricing|quanto custa|precio|prix|wie viel|quanto)\b|how much',
    re.I,
)
_FAULT = re.compile(
    r'不能用|无法播放|播放不了|故障|失效|不工作|打不开|\b(?:not working|doesn\'t work|doesnt work|not playing|playback issue|problem|broken)\b|no funciona|ne fonctionne pas|не работает|لا يعمل',
    re.I,
)
_TRIAL_WORD = re.compile(
    r'试用|测试|体验|试试看|\b(?:test|teste|trial|try|probar|prueba|essai|essayer|deneme|testen|probe|проб|тест|demo|provare)\b|تجرب|試したい|試し|試用|テスト|테스트|proberen|thử|coba',
    re.I,
)
_REQUEST_WORD = re.compile(
    r'我想|我要|想要|给我|申请|请给|请发|可以吗|能不能|想试|试一下|\b(?:want|need|please|quero|queria|quiero|quisiera|voudrais|souhaite|möchte|istiyorum|хочу|vorrei|можно|можешь|can i|could i|may i|send me|give me|get me|please send|me das|kann ich|posso)\b|أريد|اريد|من فضلك|試したい|ください|부탁|mag ik|ik wil|tôi muốn|muốn thử|saya ingin|ingin coba',
    re.I,
)
_SHORT_TRIAL_REQUEST = re.compile(
    r'^(?:试用|测试|体验|试试看|试一下|test|trial|try|demo|teste|prueba|probar|essai|deneme|testen|probe|тест|проба|تجربة|テスト|テストしたい|테스트|proberen|thử|coba)[\s!,.。！？，?、…~～]*$',
    re.I,
)


def _normalize_intent_text(value, limit=8000):
    return unicodedata.normalize('NFKC', str(value or ''))[:limit].casefold()


def infer_trial_language(text, context=''):
    """Infer one of the supported reply languages from the current request first."""
    value = _normalize_intent_text(text)
    fallback = _normalize_intent_text(context, 3000)
    fallback = re.sub(r'(?m)^(?:客户|客服|customer|agent)\s*[:：]\s*', '', fallback)
    fallback = fallback.replace('[iptv_trial_invitation]', ' ')
    for candidate in (value, fallback):
        language = _language_signal(candidate)
        if language:
            return language
    return 'en'


def _language_signal(text):
    """Return a supported language for one text, or ``None`` when it is ambiguous."""
    candidate = _normalize_intent_text(text, 3000)
    if not candidate:
        return None
    if re.search(r'[\u3040-\u30ff]', candidate):
        return 'en'
    if re.search(r'[\u4e00-\u9fff]', candidate):
        return 'zh'
    if re.search(r'[\u0600-\u06ff]', candidate):
        return 'ar'
    if re.search(r'[\u0400-\u04ff]', candidate):
        return 'ru'
    if re.search(r'\b(?:quero|queria|teste|seu|sua|por favor)\b', candidate):
        return 'pt'
    if re.search(r'\b(?:quiero|prueba|pruébalo|por favor|puedo)\b', candidate):
        return 'es'
    if re.search(r"\b(?:voudrais|essai|essayer|s'il vous plaît|peux)\b", candidate):
        return 'fr'
    if re.search(r'\b(?:möchte|testen|bitte|kann ich)\b', candidate):
        return 'de'
    if re.search(r'\b(?:istiyorum|deneme|lütfen)\b', candidate):
        return 'tr'
    if re.search(r'\b(?:vorrei|prova|posso)\b', candidate):
        return 'it'
    return None


def _history_message_text(item):
    if isinstance(item, dict):
        for key in ('body', 'text', 'content', 'caption'):
            value = item.get(key)
            if value:
                return str(value)
        return ''
    return str(item or '')


def _is_customer_history_item(item):
    if not isinstance(item, dict):
        return True
    if item.get('from_me') is True:
        return False
    direction = str(item.get('direction') or item.get('role') or item.get('sender') or '').strip().casefold()
    if direction in {'outbound', 'out', 'agent', 'assistant', '客服', '人工', 'ai'}:
        return False
    if direction in {'inbound', 'in', 'customer', 'user', '客户', 'received'}:
        return True
    if item.get('from_me') is False:
        return True
    return True


def infer_customer_language_from_history(history_or_context):
    """Infer the latest language used by the customer, ignoring agent replies."""
    customer_texts = []
    if isinstance(history_or_context, (list, tuple)):
        items = list(history_or_context)
        if items and all(
            isinstance(item, dict) and (item.get("timestamp") is not None or item.get("created_at") is not None)
            for item in items
        ):
            def history_time(item):
                value = item.get("timestamp", item.get("created_at", 0))
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0
            items = sorted(enumerate(items), key=lambda pair: (history_time(pair[1]), pair[0]))
            items = [item for _index, item in items]
        for item in items:
            if _is_customer_history_item(item):
                text = _history_message_text(item).strip()
                if text:
                    customer_texts.append(text)
    elif isinstance(history_or_context, dict):
        return infer_customer_language_from_history([history_or_context])
    else:
        raw = str(history_or_context or '')
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            lines = [raw.strip()] if raw.strip() else []
        for line in lines:
            match = re.match(r'^(客户|客服|customer|agent|user|assistant)\s*[:：]\s*(.*)$', line, re.I)
            if match:
                if match.group(1).casefold() in {'客服', 'agent', 'assistant'}:
                    continue
                line = match.group(2)
            customer_texts.append(line)
    for text in reversed(customer_texts):
        language = _language_signal(text)
        if language:
            return language
    return 'en'


def classify_trial_intent(text, context=''):
    """Return a conservative, language-tolerant IPTV test intent decision."""
    value = _normalize_intent_text(text)
    recent = _normalize_intent_text(context, 3000)
    language = infer_trial_language(value, recent)
    if not value:
        return {'requested': False, 'language': language, 'reason': 'empty'}
    if _NEGATIVE.search(value):
        return {'requested': False, 'language': language, 'reason': 'negative'}
    if _PRICING.search(value):
        return {'requested': False, 'language': language, 'reason': 'pricing'}
    if _FAULT.search(value):
        return {'requested': False, 'language': language, 'reason': 'fault'}
    has_iptv = 'iptv' in value or 'iptv' in recent
    invitation = '[iptv_trial_invitation]' in recent
    if invitation and has_iptv and _AFFIRMATIVE.match(value):
        return {'requested': True, 'language': language, 'reason': 'invitation_confirmation'}
    if not has_iptv or not _TRIAL_WORD.search(value):
        return {'requested': False, 'language': language, 'reason': 'missing_trial_signal'}
    if _SHORT_TRIAL_REQUEST.match(value):
        return {'requested': True, 'language': language, 'reason': 'short_context_request'}
    if _REQUEST_WORD.search(value):
        return {'requested': True, 'language': language, 'reason': 'explicit_request'}
    return {'requested': False, 'language': language, 'reason': 'missing_request_signal'}


class IptvError(ValueError):
    def __init__(self, code, message, uncertain=False):
        self.code = str(code)
        self.public_message = str(message)
        self.uncertain = bool(uncertain)
        super().__init__(self.public_message)


def explicit_trial_request(text, context=''):
    """Conservative second gate; a model tool call alone cannot allocate a trial."""
    return bool(classify_trial_intent(text, context).get('requested'))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _public_http_origin(parsed):
    host = (parsed.hostname or '').rstrip('.').lower()
    if (parsed.scheme not in ('http', 'https') or not host or parsed.username is not None
            or parsed.password is not None or (parsed.port is not None and parsed.port == 0)):
        raise ValueError('invalid origin')
    if host == 'localhost' or host.endswith('.localhost'):
        raise ValueError('local origin')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise ValueError('local origin')
    # Browsers interpret numeric final labels as legacy IPv4 (127.1,
    # octal/hex forms or one integer). Do not accept those as DNS names.
    elif (re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]*)', host.split('.')[-1])
          or len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                   for label in host.split('.'))):
        raise ValueError('invalid host')
    return parsed.scheme + '://' + parsed.netloc


def _playback_server(value):
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('invalid playback server')
    value = value.strip()
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.path not in ('', '/') or '?' in value or '#' in value:
        raise ValueError('playback server must be an origin')
    return _public_http_origin(parsed)


class ActivationClient:
    def __init__(self, api_key, transport=None, playback_server=''):
        self._key = str(api_key)
        self._open = transport or build_opener(_NoRedirect()).open
        self._playback_server = playback_server

    def _request(self, action, **params):
        creating = action == 'new'
        request = Request(ENDPOINT + '?' + urlencode({'action': action, 'api_key': self._key, **params}),
                          headers={'Accept': 'application/json', 'User-Agent': 'WAHA-panel-IPTV/1'})
        try:
            with self._open(request, timeout=18) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError('oversized response')
            value = json.loads(raw)
            if isinstance(value, list):
                if len(value) != 1 or not isinstance(value[0], dict):
                    raise ValueError('unexpected response')
                value = value[0]
            if not isinstance(value, dict):
                raise ValueError('unexpected response')
            return value
        except Exception:
            # urllib errors can contain the complete URL including the key.
            raise IptvError('upstream_unknown' if creating else 'check_unavailable',
                            '供应商响应不确定，系统将自动重试' if creating else '供应商检查暂不可用',
                            uncertain=creating) from None

    @staticmethod
    def account_from_response(row, playback_server=''):
        state = str(row.get('status', '')).lower()
        if state in ('false', 'error', 'failed'):
            raise IptvError('supplier_rejected', '供应商拒绝创建试用，请检查试用额度及账号状态')
        try:
            if state != 'true':
                raise ValueError('missing status')
            url = str(row['url'])
            if any(ord(c) < 32 or ord(c) == 127 for c in url) or len(url) > 4000:
                raise ValueError('invalid playlist')
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or parsed.fragment:
                raise ValueError('invalid playlist')
            if not parsed.netloc:
                # Repair only the supplier's successful http:///get.php shape.
                # Never replace an authority supplied by the upstream service.
                origin = _playback_server(playback_server)
                if not origin or parsed.path != '/get.php' or not url.startswith(parsed.scheme + ':///'):
                    raise ValueError('missing playlist origin')
                base = urlparse(origin)
                parsed = parsed._replace(scheme=base.scheme, netloc=base.netloc)
                url = parsed.geturl()
            server = _public_http_origin(parsed)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
            if len(query.get('username', [])) != 1 or len(query.get('password', [])) != 1:
                raise ValueError('missing credentials')
            username, password = query['username'][0], query['password'][0]
            if not username or not password or any(ord(c) < 32 or ord(c) == 127 for c in username + password):
                raise ValueError('invalid credentials')
            if len(username) > 256 or len(password) > 256:
                raise ValueError('invalid credentials')
            return {'url': url, 'server': server,
                    'username': username, 'password': password, 'user_id': str(row.get('user_id', ''))[:128]}
        except (KeyError, TypeError, ValueError):
            raise IptvError('upstream_unknown', '开通结果无法核实，系统将自动重试', uncertain=True) from None

    def create_account(self, pack, subscription, note):
        return self.account_from_response(
            self._request('new', type='m3u', sub=str(subscription), pack=pack, note=note),
            playback_server=self._playback_server,
        )

    def create_trial(self, pack, note):
        return self.create_account(pack, '99', note)

    def check(self):
        row = self._request('reseller_info')
        enabled = str(row.get('status')).lower() == 'true' and str(row.get('enabled')) == '1'
        if not enabled:
            raise IptvError('account_disabled', '供应商账号未启用或密钥无效')
        # Empty custom bouquets is an expected supplier response; pack=all is
        # documented and does not depend on a custom bouquet existing.
        request = Request(ENDPOINT + '?' + urlencode({'action': 'bouquet', 'api_key': self._key}),
                          headers={'Accept': 'application/json'})
        try:
            with self._open(request, timeout=18) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError('oversized response')
            value = json.loads(raw)
            if isinstance(value, dict) and str(value.get('result', '')).casefold() == 'empty custom bouquet':
                value = []
            if not isinstance(value, list):
                raise ValueError('invalid bouquet')
            packages = [{'id': str(x['id'])[:32], 'name': str(x['name'])[:180]}
                        for x in value[:500] if isinstance(x, dict) and 'id' in x and 'name' in x]
        except Exception:
            raise IptvError('check_unavailable', '账号验证成功，但套餐列表暂不可用') from None
        return {'account_enabled': True, 'packages': packages}


class IptvService:
    def __init__(self, database_path, cipher, hmac_secret, clock=None, client_factory=None,
                 send_callback=None, label_callback=None, retry_allowed=None,
                 manual_delivery_callback=None):
        self.database_path = str(database_path)
        self.cipher = cipher
        self._secret = bytes(hmac_secret)
        self.clock = clock or time.time
        self.client_factory = client_factory
        self.send_callback = send_callback
        self.label_callback = label_callback
        self.retry_allowed = retry_allowed
        self.manual_delivery_callback = manual_delivery_callback
        self._external_secret_lock = threading.Lock()
        self._external_secret_cache = (None, ())
        self._retry_lock = threading.Lock()
        self._record_lock_guard = threading.Lock()
        self._record_locks = {}
        self._retry_stop = threading.Event()
        self._retry_wakeup = threading.Event()
        self._retry_thread = None
        with closing(self._connect()) as connection:
            connection.executescript(Path(__file__).with_name('migrations').joinpath('010_iptv_trials.sql').read_text('utf-8'))
        self._ensure_trial_scope_schema()
        with closing(self._connect()) as connection, connection:
            connection.executescript(Path(__file__).with_name('migrations').joinpath('014_iptv_manual_cards.sql').read_text('utf-8'))
            connection.executescript(Path(__file__).with_name('migrations').joinpath('015_iptv_trial_retries.sql').read_text('utf-8'))
            connection.executescript(Path(__file__).with_name('migrations').joinpath('016_iptv_manual_request_scope.sql').read_text('utf-8'))

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_trial_scope_schema(self):
        with closing(self._connect()) as connection:
            indexes = connection.execute('PRAGMA index_list(iptv_trials)').fetchall()
            scoped = False
            for index in indexes:
                if not int(index['unique']):
                    continue
                columns = [
                    row['name']
                    for row in connection.execute('PRAGMA index_info(' + repr(index['name']) + ')').fetchall()
                ]
                if columns == ['provider', 'session_name', 'customer_key']:
                    scoped = True
                    break
            if scoped:
                return
            migration = Path(__file__).with_name('migrations').joinpath('013_iptv_trial_per_chat_window.sql')
            connection.executescript(migration.read_text('utf-8'))
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'INSERT OR IGNORE INTO iptv_trials_scoped '
                'SELECT id,provider,customer_key,session_name,message_key,state,credentials_ciphertext,'
                'expires_at,error_code,created_at,updated_at FROM iptv_trials'
            )
            connection.execute('DROP TABLE iptv_trials')
            connection.execute('ALTER TABLE iptv_trials_scoped RENAME TO iptv_trials')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_iptv_trials_created ON iptv_trials(provider, created_at)')
            connection.commit()

    def _hash(self, value):
        return hmac.new(self._secret, ('iptv-v1\0' + value).encode(), hashlib.sha256).hexdigest()

    def _customer_key(self, chat_id):
        value = str(chat_id or '').lower()
        identity, _, domain = value.partition('@')
        if not identity or domain not in ('c.us', 's.whatsapp.net', 'lid') or len(value) > 256:
            raise IptvError('customer_invalid', '只能为有效的私聊客户创建试用')
        identity = identity.split(':', 1)[0]
        return self._hash(('lid:' if domain == 'lid' else 'phone:') + identity)

    def _config(self, connection):
        row = connection.execute('SELECT config_ciphertext FROM iptv_provider_settings WHERE provider=?', (PROVIDER,)).fetchone()
        if not row:
            return {'api_key': '', 'daily_limit': 20, 'duration_confirmed': False}
        try:
            return self.cipher.decrypt_json(row[0])
        except Exception:
            raise IptvError('config_invalid', 'IPTV 加密配置无法读取，自动开通已停止') from None

    def settings_payload(self, session):
        with closing(self._connect()) as connection:
            config = self._config(connection)
            row = connection.execute('SELECT enabled,pack FROM iptv_session_settings WHERE session_name=?', (session,)).fetchone()
        return {'enabled': bool(row['enabled']) if row else False, 'pack': row['pack'] if row else 'all',
                'api_key_configured': bool(config.get('api_key')), 'daily_limit': int(config.get('daily_limit', 20)),
                'playback_server': config.get('playback_server', ''),
                'duration_confirmed': bool(config.get('duration_confirmed')), 'sub': 99, 'trial_hours': 12,
                'api_host': 'activationpanel.ru'}

    def policy_token(self, session):
        with closing(self._connect()) as connection:
            provider = connection.execute('SELECT config_ciphertext FROM iptv_provider_settings WHERE provider=?', (PROVIDER,)).fetchone()
            setting = connection.execute('SELECT enabled,pack,updated_at FROM iptv_session_settings WHERE session_name=?', (session,)).fetchone()
        return self._hash((provider[0] if provider else '') + '\0' + json.dumps(list(setting) if setting else []))

    def prepare_settings(self, session, payload, connection):
        if not isinstance(payload, dict) or set(payload) - {'enabled', 'pack', 'api_key', 'clear_api_key', 'daily_limit', 'duration_confirmed', 'playback_server'}:
            raise IptvError('settings_invalid', 'IPTV 设置格式无效')
        config = dict(self._config(connection))
        if 'playback_server' in payload:
            try:
                config['playback_server'] = _playback_server(payload['playback_server'])
            except ValueError:
                raise IptvError('settings_invalid', '播放服务器须为公开的 HTTP(S) 地址，不含账号、路径、参数或片段') from None
        row = connection.execute('SELECT enabled,pack FROM iptv_session_settings WHERE session_name=?', (session,)).fetchone()
        enabled = bool(row[0]) if row else False
        pack = row[1] if row else 'all'
        for field in ('enabled', 'clear_api_key', 'duration_confirmed'):
            if field in payload and not isinstance(payload[field], bool):
                raise IptvError('settings_invalid', 'IPTV 开关必须为布尔值')
        if 'enabled' in payload: enabled = payload['enabled']
        if 'pack' in payload:
            pack = str(payload['pack']).strip()
            if not re.fullmatch(r'all|[1-9][0-9]{0,11}', pack):
                raise IptvError('settings_invalid', '套餐必须为 all 或有效的套餐编号')
        if payload.get('clear_api_key'): config['api_key'] = ''
        key = payload.get('api_key', '')
        if not isinstance(key, str): raise IptvError('settings_invalid', 'IPTV 密钥格式无效')
        if key.strip():
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key.strip()):
                raise IptvError('settings_invalid', 'IPTV 密钥格式无效')
            config['api_key'] = key.strip()
        if 'daily_limit' in payload:
            try:
                if isinstance(payload['daily_limit'], bool): raise ValueError()
                limit = int(str(payload['daily_limit']))
                if not 1 <= limit <= 1000: raise ValueError()
            except (ValueError, TypeError):
                raise IptvError('settings_invalid', '每日上限必须为 1–1000') from None
            config['daily_limit'] = limit
        if 'duration_confirmed' in payload: config['duration_confirmed'] = payload['duration_confirmed']
        others = connection.execute('SELECT count(*) FROM iptv_session_settings WHERE enabled=1 AND session_name<>?', (session,)).fetchone()[0]
        if (enabled or others) and (not config.get('api_key') or not config.get('duration_confirmed')):
            raise IptvError('settings_invalid', '启用前请保存密钥并确认供应商 Demo 为 12 小时；清除前先关闭所有会话')
        return (self.cipher.encrypt_json(config), int(enabled), pack)

    def persist_settings(self, session, prepared, connection):
        encrypted, enabled, pack = prepared
        now = int(self.clock())
        connection.execute('INSERT INTO iptv_provider_settings VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET config_ciphertext=excluded.config_ciphertext,updated_at=excluded.updated_at', (PROVIDER, encrypted, now))
        connection.execute('INSERT INTO iptv_session_settings VALUES (?,?,?,?) ON CONFLICT(session_name) DO UPDATE SET enabled=excluded.enabled,pack=excluded.pack,updated_at=excluded.updated_at', (session, enabled, pack, now))

    def save_settings(self, session, payload):
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self.persist_settings(session, self.prepare_settings(session, payload, connection), connection)
        return self.settings_payload(session)

    def check(self):
        with closing(self._connect()) as connection:
            config = self._config(connection)
        if not config.get('api_key'): raise IptvError('not_configured', '请先保存 IPTV 密钥')
        return self._client(config).check()

    def _client(self, config):
        if self.client_factory is not None:
            return self.client_factory(config['api_key'])
        return ActivationClient(config['api_key'], playback_server=config.get('playback_server', ''))

    def _encrypt_chat_id(self, chat_id):
        return self.cipher.encrypt_json({'chat_id': str(chat_id)})

    def _decrypt_chat_id(self, value):
        payload = self.cipher.decrypt_json(value)
        chat_id = str(payload.get('chat_id') or '')
        if not chat_id:
            raise ValueError('chat id unavailable')
        return chat_id

    def _retry_backoff(self, attempt_count):
        return min(6 * 60 * 60, 60 * (2 ** min(max(0, int(attempt_count)), 10)))

    def _enqueue_retry(self, record_type, record_id, phase, session, chat_id, language='en', error_code=''):
        now = int(self.clock())
        encrypted_chat = self._encrypt_chat_id(chat_id)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''INSERT INTO iptv_trial_retries
                   (record_type,record_id,phase,chat_id_ciphertext,session_name,language,
                    attempt_count,next_retry_at,last_error_code,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(record_type,record_id) DO UPDATE SET
                     phase=excluded.phase,
                     chat_id_ciphertext=excluded.chat_id_ciphertext,
                     session_name=excluded.session_name,
                     language=excluded.language,
                     next_retry_at=excluded.next_retry_at,
                     last_error_code=excluded.last_error_code,
                     updated_at=excluded.updated_at''',
                (record_type, str(record_id), phase, encrypted_chat, str(session), str(language or 'en'),
                 0, now, str(error_code or ''), now, now),
            )
        self._retry_wakeup.set()

    def _defer_retry(self, row, error_code=''):
        now = int(self.clock())
        attempt = int(row['attempt_count']) + 1
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'UPDATE iptv_trial_retries SET attempt_count=?,next_retry_at=?,last_error_code=?,updated_at=? WHERE id=?',
                (attempt, now + self._retry_backoff(attempt), str(error_code or ''), now, row['id']),
            )

    def _clear_retry(self, record_type, record_id):
        with closing(self._connect()) as connection, connection:
            connection.execute('DELETE FROM iptv_trial_retries WHERE record_type=? AND record_id=?',
                               (record_type, str(record_id)))

    def _record_lock(self, record_type, record_id):
        key = (str(record_type), str(record_id))
        with self._record_lock_guard:
            return self._record_locks.setdefault(key, threading.Lock())

    @contextmanager
    def delivery_attempt(self, record_type, record_id):
        lock = self._record_lock(record_type, record_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def delivery_state(self, record_type, record_id):
        table = 'iptv_trials' if str(record_type) == 'automatic' else 'iptv_manual_cards'
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT state FROM ' + table + ' WHERE id=?', (str(record_id),)).fetchone()
        return str(row['state']) if row else ''

    def retry_pending(self, record_type, record_id):
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT 1 FROM iptv_trial_retries WHERE record_type=? AND record_id=?',
                (str(record_type), str(record_id)),
            ).fetchone()
        return bool(row)

    @staticmethod
    def _send_retry_message(callback, session, chat_id, text, origin):
        try:
            return callback(session, chat_id, text, origin=origin)
        except TypeError:
            return callback(session, chat_id, text)

    def start(self):
        if self._retry_thread and self._retry_thread.is_alive():
            return
        self._retry_stop.clear()
        self._retry_thread = threading.Thread(target=self._retry_loop, name='iptv-retry', daemon=True)
        self._retry_thread.start()

    def stop(self):
        self._retry_stop.set()
        self._retry_wakeup.set()
        thread = self._retry_thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._retry_thread = None

    def _retry_loop(self):
        while not self._retry_stop.is_set():
            processed = self.retry_due_once()
            self._retry_wakeup.wait(2 if processed else 30)
            self._retry_wakeup.clear()

    @staticmethod
    def _allowed(allowed):
        try: return bool(allowed())
        except Exception: return False

    def create_trial(self, session, chat_id, message_id, allowed=lambda: True, language='en'):
        if not self._allowed(allowed): raise IptvError('cancelled', '自动开通条件已变化')
        customer_key, now = self._customer_key(chat_id), int(self.clock())
        retry_recovery = None
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            config = self._config(connection)
            setting = connection.execute('SELECT enabled,pack FROM iptv_session_settings WHERE session_name=?', (session,)).fetchone()
            if not setting or not setting['enabled'] or not config.get('api_key') or not config.get('duration_confirmed'):
                raise IptvError('not_enabled', '当前会话的 IPTV 自动开通未就绪')
            previous = connection.execute(
                'SELECT * FROM iptv_trials WHERE provider=? AND session_name=? AND customer_key=?',
                (PROVIDER, session, customer_key),
            ).fetchone()
            if previous:
                if previous['state'] in ('created', 'sent') and previous['credentials_ciphertext']:
                    if previous['expires_at'] <= now:
                        raise IptvError('trial_expired', '客户已领取过试用，每位客户永久仅限一次，不可再次开通')
                    try: account = self.cipher.decrypt_json(previous['credentials_ciphertext'])
                    except Exception: raise IptvError('account_unavailable', '试用记录暂不可读，请人工核对') from None
                    return {'id': previous['id'], 'account': account, 'expires_at': previous['expires_at'],
                            'reused': True, 'subscription': '99', 'language': language or 'en'}
                if previous['state'] == 'unknown':
                    retry = connection.execute(
                        'SELECT 1 FROM iptv_trial_retries WHERE record_type=? AND record_id=?',
                        ('automatic', previous['id']),
                    ).fetchone()
                    if retry:
                        raise IptvError('trial_pending', '此客户已有开通记录，请先人工核对；系统不会重复开通')
                    retry_recovery = (previous['id'], session, chat_id, language)
                elif previous['state'] != 'cancelled' or previous['message_key'] == self._hash(str(message_id)):
                    raise IptvError('trial_pending', '此客户已有开通记录，请先人工核对；系统不会重复开通')
            if not retry_recovery:
                day_start = int(datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                count = connection.execute("SELECT count(*) FROM iptv_trials WHERE provider=? AND created_at>=? AND state NOT IN ('failed','cancelled')", (PROVIDER, day_start)).fetchone()[0]
                if count >= int(config.get('daily_limit', 20)):
                    raise IptvError('daily_limit', '已达到今日 IPTV 自动开通上限')
                trial_id = uuid.uuid4().hex
                if previous:
                    # Only cancelled-before-call rows can be reclaimed. Quota and
                    # the new reservation are checked under one write transaction.
                    connection.execute("UPDATE iptv_trials SET id=?,session_name=?,message_key=?,state='creating',error_code='',created_at=?,updated_at=? WHERE id=? AND state='cancelled'", (trial_id, session, self._hash(str(message_id)), now, now, previous['id']))
                else:
                    connection.execute('INSERT INTO iptv_trials(id,provider,customer_key,session_name,message_key,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)', (trial_id, PROVIDER, customer_key, session, self._hash(str(message_id)), 'creating', now, now))
        if retry_recovery:
            if not self._allowed(allowed):
                raise IptvError('cancelled', '自动开通条件已变化')
            self._enqueue_retry(
                'automatic', retry_recovery[0], 'create', retry_recovery[1],
                retry_recovery[2], retry_recovery[3], 'legacy_retry_recovery',
            )
            raise IptvError(
                'retry_queued',
                '已有一次未确认的开通请求，系统已安排恢复重试',
                uncertain=True,
            )
        if not self._allowed(allowed):
            self._set_state(trial_id, 'cancelled', 'cancelled')
            raise IptvError('cancelled', '自动开通条件已变化')
        try:
            account = self._client(config).create_trial(setting['pack'], 'waha-' + trial_id)
            account = ActivationClient.account_from_response({'status': 'true', **account})
            expires = now + TRIAL_SECONDS
            encrypted = self.cipher.encrypt_json(account)
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE iptv_trials SET state='created',credentials_ciphertext=?,expires_at=?,updated_at=? WHERE id=?", (encrypted, expires, int(self.clock()), trial_id))
        except IptvError as error:
            self._set_state(trial_id, 'unknown' if error.uncertain else 'failed', error.code)
            self._enqueue_retry('automatic', trial_id, 'create', session, chat_id, language, error.code)
            raise
        except Exception:
            self._set_state(trial_id, 'unknown', 'upstream_unknown')
            self._enqueue_retry('automatic', trial_id, 'create', session, chat_id, language, 'upstream_unknown')
            raise IptvError('upstream_unknown', '开通结果不确定，系统将自动重试', uncertain=True) from None
        return {'id': trial_id, 'account': account, 'expires_at': expires, 'reused': False,
                'subscription': '99', 'language': language or 'en'}

    def _set_state(self, trial_id, state, code=''):
        with closing(self._connect()) as connection, connection:
            connection.execute('UPDATE iptv_trials SET state=?,error_code=?,updated_at=? WHERE id=?', (state, code, int(self.clock()), trial_id))

    def mark_delivery(self, trial_id, success, session=None, chat_id=None, language='en'):
        self._set_state(trial_id, 'sent' if success else 'created', '' if success else 'send_failed')
        if success:
            self._clear_retry('automatic', trial_id)
        elif session and chat_id:
            self._enqueue_retry('automatic', trial_id, 'send', session, chat_id, language, 'send_failed')

    def create_manual_card(self, session, chat_id, message_id, subscription, request_id, language='auto'):
        subscription = str(subscription or '')
        if subscription not in TRIAL_SUBSCRIPTIONS:
            raise IptvError('subscription_invalid', '卡密时长选项无效')
        requested_language = str(language or 'auto').strip().casefold()
        selected_language = (
            requested_language
            if requested_language in LANGUAGES
            else (infer_trial_language(language, '') if requested_language != 'auto' else 'en')
        )
        customer_key, now = self._customer_key(chat_id), int(self.clock())
        request_key = self._hash(str(request_id or ''))
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            config = self._config(connection)
            setting = connection.execute(
                'SELECT enabled,pack FROM iptv_session_settings WHERE session_name=?', (session,)
            ).fetchone()
            if not config.get('api_key'):
                raise IptvError('not_configured', '请先保存 IPTV 密钥')
            if subscription == '99' and not config.get('duration_confirmed'):
                raise IptvError('not_enabled', '请先确认供应商 Demo 时长')
            pack = setting['pack'] if setting else 'all'
            previous = connection.execute(
                'SELECT * FROM iptv_manual_cards WHERE provider=? AND session_name=? AND customer_key=? AND request_key=?',
                (PROVIDER, session, customer_key, request_key),
            ).fetchone()
            if previous:
                if previous['state'] in ('created', 'sent') and previous['credentials_ciphertext']:
                    try:
                        account = self.cipher.decrypt_json(previous['credentials_ciphertext'])
                    except Exception:
                        raise IptvError('account_unavailable', '卡密记录暂不可读，请重新生成') from None
                    return {'id': previous['id'], 'account': account, 'expires_at': previous['expires_at'],
                            'subscription': previous['subscription'], 'language': previous['language'],
                            'reused': True, 'delivery_state': previous['state']}
                if previous['state'] in ('creating', 'unknown', 'failed'):
                    raise IptvError('manual_pending' if previous['state'] == 'creating' else 'manual_failed',
                                    '本次手动开卡未完成，请使用新的请求重新生成')
            card_id = uuid.uuid4().hex
            connection.execute(
                '''INSERT INTO iptv_manual_cards
                   (id,provider,customer_key,session_name,chat_id_ciphertext,message_key,request_key,
                    subscription,pack,language,state,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, ?, ?)''',
                (card_id, PROVIDER, customer_key, session, self._encrypt_chat_id(chat_id),
                 self._hash(str(message_id)), request_key, subscription, pack,
                 selected_language, 'creating', now, now),
            )
        try:
            account = self._client(config).create_account(pack, subscription, 'waha-manual-' + card_id)
            account = ActivationClient.account_from_response({'status': 'true', **account})
            expires = now + TRIAL_SECONDS if subscription == '99' else 0
            encrypted = self.cipher.encrypt_json(account)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE iptv_manual_cards SET state='created',credentials_ciphertext=?,expires_at=?,language=?,updated_at=? WHERE id=?",
                    (encrypted, expires, selected_language, int(self.clock()), card_id),
                )
        except IptvError as error:
            with closing(self._connect()) as connection, connection:
                connection.execute('UPDATE iptv_manual_cards SET state=?,error_code=?,updated_at=? WHERE id=?',
                                   ('unknown' if error.uncertain else 'failed', error.code, int(self.clock()), card_id))
            raise
        except Exception:
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE iptv_manual_cards SET state='unknown',error_code=?,updated_at=? WHERE id=?",
                                   ('upstream_unknown', int(self.clock()), card_id))
            raise IptvError('upstream_unknown', '手动开卡结果不确定，请重新生成', uncertain=True) from None
        return {'id': card_id, 'account': account, 'expires_at': expires, 'subscription': subscription,
                'language': selected_language, 'reused': False, 'delivery_state': 'created'}

    def mark_manual_delivery(self, card_id, success, session=None, chat_id=None, language='en'):
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT * FROM iptv_manual_cards WHERE id=?', (str(card_id),)).fetchone()
        if not row:
            raise IptvError('manual_missing', '手动卡密记录不存在')
        if success:
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE iptv_manual_cards SET state='sent',error_code='',updated_at=? WHERE id=?",
                                   (int(self.clock()), str(card_id)))
            self._clear_retry('manual', card_id)
            return
        session = session or row['session_name']
        if not chat_id:
            chat_id = self._decrypt_chat_id(row['chat_id_ciphertext'])
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE iptv_manual_cards SET state='created',error_code='send_failed',updated_at=? WHERE id=?",
                               (int(self.clock()), str(card_id)))
        self._enqueue_retry('manual', card_id, 'send', session, chat_id, language or row['language'], 'send_failed')

    def _notify_manual_delivery_success(self, callback, session, chat_id, card):
        if not callback:
            return
        try:
            callback(session, chat_id, card)
        except Exception:
            # Delivery is already confirmed; a bookkeeping failure must never
            # make the retry worker send the card again.
            return

    def retry_due_once(self, send_callback=None, label_callback=None, manual_delivery_callback=None):
        callback = send_callback or self.send_callback
        label = label_callback or self.label_callback
        manual_success = manual_delivery_callback or self.manual_delivery_callback
        now = int(self.clock())
        if not self._retry_lock.acquire(blocking=False):
            return []
        processed = []
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    'SELECT * FROM iptv_trial_retries WHERE next_retry_at<=? ORDER BY next_retry_at,id LIMIT 20',
                    (now,),
                ).fetchall()
            for retry in rows:
                retry_phase = retry['phase']
                try:
                    chat_id = self._decrypt_chat_id(retry['chat_id_ciphertext'])
                    retry_policy = self.retry_allowed(retry['session_name'], chat_id, retry['record_type']) if self.retry_allowed else True
                    if retry_policy == 'deleted':
                        if retry['record_type'] == 'automatic':
                            self._set_state(retry['record_id'], 'cancelled', 'session_deleted')
                        self._clear_retry(retry['record_type'], retry['record_id'])
                        continue
                    if not retry_policy:
                        self._defer_retry(retry, 'paused')
                        continue
                    if retry['record_type'] == 'automatic':
                        with closing(self._connect()) as connection:
                            record = connection.execute('SELECT * FROM iptv_trials WHERE id=?', (retry['record_id'],)).fetchone()
                        if not record:
                            self._clear_retry('automatic', retry['record_id'])
                            continue
                        if retry_phase == 'create':
                            with closing(self._connect()) as connection:
                                config = self._config(connection)
                                setting = connection.execute('SELECT pack FROM iptv_session_settings WHERE session_name=?',
                                                             (retry['session_name'],)).fetchone()
                            if not setting or not config.get('api_key'):
                                self._defer_retry(retry, 'not_enabled')
                                continue
                            account = self._client(config).create_trial(setting['pack'], 'waha-' + retry['record_id'])
                            account = ActivationClient.account_from_response({'status': 'true', **account})
                            expires = int(record['created_at']) + TRIAL_SECONDS
                            with closing(self._connect()) as connection, connection:
                                connection.execute("UPDATE iptv_trials SET state='created',credentials_ciphertext=?,expires_at=?,updated_at=? WHERE id=?",
                                                   (self.cipher.encrypt_json(account), expires, now, retry['record_id']))
                                connection.execute(
                                    "UPDATE iptv_trial_retries SET phase='send',updated_at=? WHERE id=?",
                                    (now, retry['id']),
                                )
                            retry_phase = 'send'
                            record = dict(record)
                            record.update({'credentials_ciphertext': self.cipher.encrypt_json(account), 'expires_at': expires})
                        else:
                            account = self.cipher.decrypt_json(record['credentials_ciphertext'])
                            expires = record['expires_at']
                        if not callback:
                            self._defer_retry(retry, 'send_unavailable')
                            continue
                        result = {'id': retry['record_id'], 'account': account, 'expires_at': expires,
                                  'subscription': '99', 'language': retry['language'], 'reused': True}
                        with self.delivery_attempt('automatic', retry['record_id']):
                            if self.delivery_state('automatic', retry['record_id']) == 'sent':
                                self._clear_retry('automatic', retry['record_id'])
                                processed.append(retry['record_id'])
                                continue
                            self._send_retry_message(callback, retry['session_name'], chat_id,
                                                     self.reply_text(result, retry['language']), 'iptv_trial_retry')
                            self._set_state(retry['record_id'], 'sent', '')
                            self._clear_retry('automatic', retry['record_id'])
                            if label:
                                try:
                                    try:
                                        label(retry['session_name'], chat_id, 'IPTV测试过')
                                    except TypeError:
                                        label(retry['session_name'], chat_id)
                                except Exception:
                                    pass
                        processed.append(retry['record_id'])
                    else:
                        with closing(self._connect()) as connection:
                            record = connection.execute('SELECT * FROM iptv_manual_cards WHERE id=?', (retry['record_id'],)).fetchone()
                        if not record or not record['credentials_ciphertext']:
                            self._clear_retry('manual', retry['record_id'])
                            continue
                        if not callback:
                            self._defer_retry(retry, 'send_unavailable')
                            continue
                        account = self.cipher.decrypt_json(record['credentials_ciphertext'])
                        result = {'id': record['id'], 'account': account, 'expires_at': record['expires_at'],
                                  'subscription': record['subscription'], 'language': record['language'], 'reused': True}
                        with self.delivery_attempt('manual', record['id']):
                            if self.delivery_state('manual', record['id']) == 'sent':
                                if manual_success:
                                    self._notify_manual_delivery_success(manual_success, retry['session_name'], chat_id, result)
                                self._clear_retry('manual', record['id'])
                                processed.append(record['id'])
                                continue
                            self._send_retry_message(callback, record['session_name'], chat_id,
                                                     self.reply_text(result, record['language']), 'iptv_manual_retry')
                            with closing(self._connect()) as connection, connection:
                                connection.execute("UPDATE iptv_manual_cards SET state='sent',error_code='',updated_at=? WHERE id=?",
                                                   (now, record['id']))
                            self._clear_retry('manual', record['id'])
                            if manual_success:
                                self._notify_manual_delivery_success(manual_success, record['session_name'], chat_id, result)
                        processed.append(record['id'])
                except IptvError as error:
                    if retry['record_type'] == 'automatic' and retry_phase == 'create':
                        self._set_state(retry['record_id'], 'unknown' if error.uncertain else 'failed', error.code)
                    self._defer_retry(retry, error.code)
                except Exception:
                    self._defer_retry(retry, 'retry_failed')
        finally:
            self._retry_lock.release()
        return processed

    def records(self, session):
        with closing(self._connect()) as connection:
            rows = connection.execute('SELECT id,state,error_code,created_at,expires_at FROM iptv_trials WHERE session_name=? ORDER BY created_at DESC LIMIT 30', (session,)).fetchall()
        return {'items': [dict(row) for row in rows]}

    def reply_text(self, result, language='en'):
        subscription = str(result.get('subscription') or '99')
        duration_labels = {
            'zh': {'1': '1个月', '3': '3个月', '6': '6个月', '12': '12个月'},
            'en': {'1': '1 month', '3': '3 months', '6': '6 months', '12': '12 months'},
            'pt': {'1': '1 mês', '3': '3 meses', '6': '6 meses', '12': '12 meses'},
            'es': {'1': '1 mes', '3': '3 meses', '6': '6 meses', '12': '12 meses'},
            'fr': {'1': '1 mois', '3': '3 mois', '6': '6 mois', '12': '12 mois'},
            'ar': {'1': 'شهر واحد', '3': '3 أشهر', '6': '6 أشهر', '12': '12 شهرًا'},
            'tr': {'1': '1 ay', '3': '3 ay', '6': '6 ay', '12': '12 ay'},
            'de': {'1': '1 Monat', '3': '3 Monate', '6': '6 Monate', '12': '12 Monate'},
            'ru': {'1': '1 месяц', '3': '3 месяца', '6': '6 месяцев', '12': '12 месяцев'},
            'it': {'1': '1 mese', '3': '3 mesi', '6': '6 mesi', '12': '12 mesi'},
        }
        duration = duration_labels.get(language, duration_labels['en']).get(subscription)
        introductions = {'zh': (f'您的 IPTV {duration}账号已开通。' if duration else '您的 IPTV 测试账号已开通，试用时长为 12 小时。'),
                        'en': 'Your IPTV trial is ready. The trial lasts 12 hours.',
                        'pt': 'Seu teste IPTV está pronto. O teste dura 12 horas.',
                        'es': 'Tu prueba de IPTV está lista. La prueba dura 12 horas.',
                        'fr': 'Votre essai IPTV est prêt. Il dure 12 heures.',
                        'ar': 'حساب تجربة IPTV جاهز. مدة التجربة 12 ساعة.',
                        'tr': 'IPTV deneme hesabınız hazır. Deneme süresi 12 saattir.',
                        'de': 'Dein IPTV-Testkonto ist bereit. Der Test dauert 12 Stunden.',
                        'ru': 'Ваш тестовый аккаунт IPTV готов. Срок теста — 12 часов.',
                        'it': 'La tua prova IPTV è pronta. La prova dura 12 ore.'}
        if duration:
            introductions.update({
                'en': f'Your IPTV {duration} account is ready.',
                'pt': f'Sua conta IPTV de {duration} está pronta.',
                'es': f'Tu cuenta IPTV de {duration} está lista.',
                'fr': f'Votre compte IPTV de {duration} est prêt.',
                'ar': f'حساب IPTV لمدة {duration} جاهز.',
                'tr': f'{duration} IPTV hesabınız hazır.',
                'de': f'Ihr IPTV-Konto für {duration} ist bereit.',
                'ru': f'Ваш IPTV-аккаунт на {duration} готов.',
                'it': f'Il tuo account IPTV di {duration} è pronto.',
            })
        account = result['account']
        expiry = datetime.fromtimestamp(result['expires_at'], TZ).strftime('%Y-%m-%d %H:%M') if result.get('expires_at') else ''
        labels = ('服务器', '用户名', '密码', '预计到期') if language == 'zh' else ('Server', 'Username', 'Password', 'Estimated expiry')
        text = (introductions.get(language, introductions['en']) + '\n\n' + labels[0] + ': ' + account['server'] +
                '\n' + labels[1] + ': ' + account['username'] + '\n' + labels[2] + ': ' + account['password'] +
                '\nM3U: ' + account['url'])
        return text + (('\n' + labels[3] + ': ' + expiry + ' (UTC+8)') if expiry else '')

    def sanitize_context(self, chat_id, text, session=None):
        value = str(text or '')
        try:
            customer_key = self._customer_key(chat_id)
            with closing(self._connect()) as connection:
                if session:
                    row = connection.execute(
                        '''SELECT credentials_ciphertext FROM (
                               SELECT credentials_ciphertext,updated_at FROM iptv_trials
                               WHERE provider=? AND session_name=? AND customer_key=?
                               UNION ALL
                               SELECT credentials_ciphertext,updated_at FROM iptv_manual_cards
                               WHERE provider=? AND session_name=? AND customer_key=?
                           ) WHERE credentials_ciphertext<>'' ORDER BY updated_at DESC LIMIT 1''',
                        (PROVIDER, str(session), customer_key, PROVIDER, str(session), customer_key),
                    ).fetchone()
                else:
                    row = connection.execute(
                        '''SELECT credentials_ciphertext FROM (
                               SELECT credentials_ciphertext,updated_at FROM iptv_trials
                               WHERE provider=? AND customer_key=?
                               UNION ALL
                               SELECT credentials_ciphertext,updated_at FROM iptv_manual_cards
                               WHERE provider=? AND customer_key=?
                           ) WHERE credentials_ciphertext<>'' ORDER BY updated_at DESC LIMIT 1''',
                        (PROVIDER, customer_key, PROVIDER, customer_key),
                    ).fetchone()
            if row and row[0]:
                account = self.cipher.decrypt_json(row[0])
                for field in ('url', 'password', 'username'):
                    secret = str(account.get(field, ''))
                    if secret: value = value.replace(secret, '[IPTV 凭据已隐藏]')
        except Exception:
            # Never fall back to the original credential-bearing body on a
            # decryption or storage error.
            if re.search(r'username=|password=|\bPassword\s*:|密码\s*[:：]', value, re.I):
                return '[IPTV 登录信息已隐藏]'
        return re.sub(r'https?://[^\s<>]*[?&](?:username|password)=[^\s<>]*', '[IPTV 播放地址已隐藏]', value, flags=re.I)

    def sanitize_external_text(self, text):
        """Final third-party boundary, also protecting tools without a chat ID.

        Credential rows are immutable after creation. Count/max-rowid detects
        new credentials; only a changed set needs to be decrypted again.
        """
        with self._external_secret_lock:
            try:
                with closing(self._connect()) as connection:
                    config = self._config(connection)
                    revision = tuple(connection.execute("SELECT count(*),max(rowid) FROM iptv_trials WHERE credentials_ciphertext<>''").fetchone())
                    manual_revision = tuple(connection.execute("SELECT count(*),max(rowid) FROM iptv_manual_cards WHERE credentials_ciphertext<>''").fetchone())
                    revision = revision + manual_revision
                    if revision != self._external_secret_cache[0]:
                        secrets = set()
                        for row in connection.execute("SELECT credentials_ciphertext FROM iptv_trials WHERE credentials_ciphertext<>''"):
                            account = self.cipher.decrypt_json(row[0])
                            for field in ('url', 'password', 'username'):
                                if account.get(field): secrets.add(str(account[field]))
                        for row in connection.execute("SELECT credentials_ciphertext FROM iptv_manual_cards WHERE credentials_ciphertext<>''"):
                            account = self.cipher.decrypt_json(row[0])
                            for field in ('url', 'password', 'username'):
                                if account.get(field): secrets.add(str(account[field]))
                        self._external_secret_cache = (revision, tuple(sorted(secrets, key=len, reverse=True)))
                secrets = list(self._external_secret_cache[1])
                if config.get('api_key'): secrets.insert(0, config['api_key'])
            except Exception:
                raise IptvError('sensitive_context_unavailable', '敏感信息过滤暂不可用，已停止第三方请求') from None
        value = str(text or '')
        for secret in secrets:
            for form in {secret, quote(secret, safe=''), quote_plus(secret)}:
                value = value.replace(form, '[IPTV 凭据已隐藏]')
        return re.sub(r'https?://[^\s<>]*[?&](?:username|password)=[^\s<>]*', '[IPTV 播放地址已隐藏]', value, flags=re.I)
