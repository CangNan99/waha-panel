"""Fixed-supplier demo accounts, encrypted at rest, with durable at-most-once creation."""
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
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


class IptvError(ValueError):
    def __init__(self, code, message, uncertain=False):
        self.code = str(code)
        self.public_message = str(message)
        self.uncertain = bool(uncertain)
        super().__init__(self.public_message)


def explicit_trial_request(text, context=''):
    """Conservative second gate; a model tool call alone cannot allocate a trial."""
    text = str(text or '')[:8000].casefold()
    if re.search(r"不要|不需要|不用|不想|取消|do not|don't|dont|no quiero|não quero|nao quero|ne veux pas|لا أريد", text):
        return False
    if re.search(r'多少钱|价格|费用|\b(?:price|cost|pricing|quanto|precio|prix)\b|how much', text):
        return False
    if 'iptv' not in text and 'iptv' not in str(context or '')[-2500:].casefold():
        return False
    if not re.search(r'试用|测试|体验|\b(?:test|teste|trial|try|probar|prueba|essai|essayer|deneme|testen|проб|тест)|تجرب', text):
        return False
    return bool(re.search(r'我想|我要|想要|给我|申请|试用|测试|体验|\b(?:want|need|please|quero|queria|quiero|quisiera|voudrais|souhaite|möchte|istiyorum|хочу|vorrei)\b|can i|could i|may i|send me|give me|أريد|اريد|من فضلك', text))


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
    elif (re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', host.split('.')[-1])
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
                            '供应商响应不确定，请人工核对；系统不会重复开通' if creating else '供应商检查暂不可用',
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
            raise IptvError('upstream_unknown', '试用可能已创建，但返回数据无法核实，请人工核对', uncertain=True) from None

    def create_trial(self, pack, note):
        return self.account_from_response(self._request('new', type='m3u', sub='99', pack=pack, note=note),
                                          playback_server=self._playback_server)

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
    def __init__(self, database_path, cipher, hmac_secret, clock=None, client_factory=None):
        self.database_path = str(database_path)
        self.cipher = cipher
        self._secret = bytes(hmac_secret)
        self.clock = clock or time.time
        self.client_factory = client_factory
        self._external_secret_lock = threading.Lock()
        self._external_secret_cache = (None, ())
        with closing(self._connect()) as connection:
            connection.executescript(Path(__file__).with_name('migrations').joinpath('010_iptv_trials.sql').read_text('utf-8'))
        self._ensure_trial_scope_schema()

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

    @staticmethod
    def _allowed(allowed):
        try: return bool(allowed())
        except Exception: return False

    def create_trial(self, session, chat_id, message_id, allowed=lambda: True):
        if not self._allowed(allowed): raise IptvError('cancelled', '自动开通条件已变化')
        customer_key, now = self._customer_key(chat_id), int(self.clock())
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
                    return {'id': previous['id'], 'account': account, 'expires_at': previous['expires_at'], 'reused': True}
                if previous['state'] != 'cancelled' or previous['message_key'] == self._hash(str(message_id)):
                    raise IptvError('trial_pending', '此客户已有开通记录，请先人工核对；系统不会重复开通')
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
            raise
        except Exception:
            self._set_state(trial_id, 'unknown', 'upstream_unknown')
            raise IptvError('upstream_unknown', '开通结果不确定，请人工核对；系统不会重复开通', uncertain=True) from None
        return {'id': trial_id, 'account': account, 'expires_at': expires, 'reused': False}

    def _set_state(self, trial_id, state, code=''):
        with closing(self._connect()) as connection, connection:
            connection.execute('UPDATE iptv_trials SET state=?,error_code=?,updated_at=? WHERE id=?', (state, code, int(self.clock()), trial_id))

    def mark_delivery(self, trial_id, success):
        self._set_state(trial_id, 'sent' if success else 'created', '' if success else 'send_failed')

    def records(self, session):
        with closing(self._connect()) as connection:
            rows = connection.execute('SELECT id,state,error_code,created_at,expires_at FROM iptv_trials WHERE session_name=? ORDER BY created_at DESC LIMIT 30', (session,)).fetchall()
        return {'items': [dict(row) for row in rows]}

    def reply_text(self, result, language='en'):
        introductions = {'zh': '您的 IPTV 测试账号已开通，试用时长为 12 小时。',
                        'en': 'Your IPTV trial is ready. The trial lasts 12 hours.',
                        'pt': 'Seu teste IPTV está pronto. O teste dura 12 horas.',
                        'es': 'Tu prueba de IPTV está lista. La prueba dura 12 horas.',
                        'fr': 'Votre essai IPTV est prêt. Il dure 12 heures.',
                        'ar': 'حساب تجربة IPTV جاهز. مدة التجربة 12 ساعة.',
                        'tr': 'IPTV deneme hesabınız hazır. Deneme süresi 12 saattir.',
                        'de': 'Dein IPTV-Testkonto ist bereit. Der Test dauert 12 Stunden.',
                        'ru': 'Ваш тестовый аккаунт IPTV готов. Срок теста — 12 часов.',
                        'it': 'La tua prova IPTV è pronta. La prova dura 12 ore.'}
        account = result['account']
        expiry = datetime.fromtimestamp(result['expires_at'], TZ).strftime('%Y-%m-%d %H:%M')
        labels = ('服务器', '用户名', '密码', '预计到期') if language == 'zh' else ('Server', 'Username', 'Password', 'Estimated expiry')
        return (introductions.get(language, introductions['en']) + '\n\n' + labels[0] + ': ' + account['server'] +
                '\n' + labels[1] + ': ' + account['username'] + '\n' + labels[2] + ': ' + account['password'] +
                '\nM3U: ' + account['url'] + '\n' + labels[3] + ': ' + expiry + ' (UTC+8)')

    def sanitize_context(self, chat_id, text, session=None):
        value = str(text or '')
        try:
            customer_key = self._customer_key(chat_id)
            with closing(self._connect()) as connection:
                if session:
                    row = connection.execute(
                        'SELECT credentials_ciphertext FROM iptv_trials WHERE provider=? AND session_name=? AND customer_key=?',
                        (PROVIDER, str(session), customer_key),
                    ).fetchone()
                else:
                    row = connection.execute(
                        'SELECT credentials_ciphertext FROM iptv_trials WHERE provider=? AND customer_key=? ORDER BY updated_at DESC LIMIT 1',
                        (PROVIDER, customer_key),
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
                    if revision != self._external_secret_cache[0]:
                        secrets = set()
                        for row in connection.execute("SELECT credentials_ciphertext FROM iptv_trials WHERE credentials_ciphertext<>''"):
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
