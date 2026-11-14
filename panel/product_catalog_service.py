"""Product catalog and additive compatibility migration for the image library."""
from __future__ import annotations
import hashlib, json, re, sqlite3, time, uuid, unicodedata
from pathlib import Path
from contextlib import contextmanager
try:
    from .product_image_service import normalize_tag
except ImportError:
    from product_image_service import normalize_tag

class ProductCatalogError(RuntimeError):
    code = "PRODUCT_CATALOG_ERROR"
    def __init__(self, message, code=None):
        self.public_message = str(message); self.code = code or self.code; super().__init__(self.public_message)
class ProductCatalogValidationError(ValueError, ProductCatalogError):
    code = "INVALID_PRODUCT_CATALOG"
class ProductCatalogNotFoundError(ProductCatalogError):
    code = "PRODUCT_NOT_FOUND"

class ProductCatalogService:
    KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
    def __init__(self, database_path, clock=None):
        self.database_path = Path(database_path); self.clock = clock or time.time; self._ensure_schema()
    def _connect(self):
        c=sqlite3.connect(self.database_path, timeout=10); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c
    @contextmanager
    def _db(self):
        c=self._connect()
        try: yield c; c.commit()
        except Exception: c.rollback(); raise
        finally: c.close()
    def _ensure_schema(self):
        with self._db() as c:
            # A standalone catalog service may receive a brand-new database.
            # Ensure the legacy image tables exist before adding catalog columns.
            migration_root = Path(__file__).with_name('migrations')
            c.executescript((migration_root / '014_product_image_library.sql').read_text(encoding='utf-8'))
            c.executescript((migration_root / '019_product_image_matching.sql').read_text(encoding='utf-8'))
            c.executescript((Path(__file__).with_name('migrations')/'021_product_catalog.sql').read_text(encoding='utf-8'))
            cols={r[1] for r in c.execute('PRAGMA table_info(product_image_assets)')}
            defs=("product_id TEXT REFERENCES product_catalog(product_id) ON DELETE SET NULL", "variant TEXT NOT NULL DEFAULT ''", "reference_role TEXT NOT NULL DEFAULT 'primary'", "allow_visual_match INTEGER NOT NULL DEFAULT 1", "allow_auto_send INTEGER NOT NULL DEFAULT 1", "legacy_import_status TEXT NOT NULL DEFAULT 'pending'")
            for d in defs:
                n=d.split()[0]
                if n not in cols: c.execute('ALTER TABLE product_image_assets ADD COLUMN '+d)
    @staticmethod
    def _bool(value, default=True):
        if value is None: return bool(default)
        if isinstance(value, bool): return value
        text = str(value).strip().casefold()
        if text in {'1','true','yes','on','enabled'}: return True
        if text in {'0','false','no','off','disabled'}: return False
        raise ProductCatalogValidationError('布尔开关参数无效','INVALID_FLAG')
    @staticmethod
    def _text(v, limit, field, required=False):
        s=unicodedata.normalize('NFKC',str(v or '')).strip()
        if required and not s: raise ProductCatalogValidationError(f'{field}不能为空','REQUIRED')
        if len(s)>limit: raise ProductCatalogValidationError(f'{field}过长','TOO_LONG')
        return s
    @classmethod
    def _payload(cls,p):
        if not isinstance(p,dict): raise ProductCatalogValidationError('产品数据格式无效')
        key=cls._text(p.get('product_key'),64,'产品标识',True).casefold()
        if not cls.KEY_RE.fullmatch(key): raise ProductCatalogValidationError('产品标识格式无效','INVALID_PRODUCT_KEY')
        name=cls._text(p.get('product_name'),200,'产品名称',True)
        sku=cls._text(p.get('sku'),120,'SKU')
        aliases=p.get('aliases',[])
        if isinstance(aliases,str): aliases=[aliases]
        if not isinstance(aliases,(list,tuple,set)): raise ProductCatalogValidationError('产品别名格式无效')
        out=[]; seen=set()
        for a in aliases:
            x=cls._text(a,120,'产品别名'); k=normalize_tag(x)
            if k and k not in seen: out.append(x); seen.add(k)
        lang=p.get('language_content',p.get('language_content_json',{}))
        if isinstance(lang,str):
            try: lang=json.loads(lang)
            except ValueError: raise ProductCatalogValidationError('多语言内容格式无效')
        if not isinstance(lang,dict): raise ProductCatalogValidationError('多语言内容格式无效')
        return dict(product_key=key,product_name=name,sku=sku,aliases=out,category=cls._text(p.get('category'),120,'分类'),description=cls._text(p.get('description'),4000,'介绍'),selling_points=cls._text(p.get('selling_points'),4000,'卖点'),usage_scenarios=cls._text(p.get('usage_scenarios'),4000,'适用场景'),exclusion_scenarios=cls._text(p.get('exclusion_scenarios'),4000,'排除场景'),language_content=lang,active=cls._bool(p.get('active'),True),allow_ai_image_send=cls._bool(p.get('allow_ai_image_send'),True),allow_marketing_image_send=cls._bool(p.get('allow_marketing_image_send'),True))
    @staticmethod
    def _dict(row):
        if row is None:return None
        d=dict(row); d['active']=bool(d['active']); d['allow_ai_image_send']=bool(d['allow_ai_image_send']); d['allow_marketing_image_send']=bool(d['allow_marketing_image_send']); d['aliases']=json.loads(d.pop('aliases_json') or '[]'); d['language_content']=json.loads(d.pop('language_content_json') or '{}'); return d
    def list_products(self, include_inactive=False):
        with self._db() as c:
            rows=c.execute('SELECT * FROM product_catalog'+('' if include_inactive else ' WHERE active=1')+' ORDER BY product_name,product_key').fetchall()
        items=[self._dict(r) for r in rows]; return {'items':items,'total':len(items)}
    def get_product(self, product_id, include_inactive=False):
        with self._db() as c:r=c.execute('SELECT * FROM product_catalog WHERE product_id=?'+('' if include_inactive else ' AND active=1'),(str(product_id),)).fetchone()
        if not r: raise ProductCatalogNotFoundError('产品不存在或已停用')
        return self._dict(r)
    def save_product(self,payload):
        p=self._payload(payload); now=int(self.clock()); pid=str(payload.get('product_id') or uuid.uuid4())
        with self._db() as c:
            try:
                c.execute('''INSERT INTO product_catalog(product_id,product_key,product_name,sku,aliases_json,category,description,selling_points,usage_scenarios,exclusion_scenarios,language_content_json,active,allow_ai_image_send,allow_marketing_image_send,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET product_key=excluded.product_key,product_name=excluded.product_name,sku=excluded.sku,aliases_json=excluded.aliases_json,category=excluded.category,description=excluded.description,selling_points=excluded.selling_points,usage_scenarios=excluded.usage_scenarios,exclusion_scenarios=excluded.exclusion_scenarios,language_content_json=excluded.language_content_json,active=excluded.active,allow_ai_image_send=excluded.allow_ai_image_send,allow_marketing_image_send=excluded.allow_marketing_image_send,updated_at=excluded.updated_at''',(pid,p['product_key'],p['product_name'],p['sku'],json.dumps(p['aliases'],ensure_ascii=False),p['category'],p['description'],p['selling_points'],p['usage_scenarios'],p['exclusion_scenarios'],json.dumps(p['language_content'],ensure_ascii=False),int(p['active']),int(p['allow_ai_image_send']),int(p['allow_marketing_image_send']),now,now))
            except sqlite3.IntegrityError as e: raise ProductCatalogValidationError('产品标识已存在','DUPLICATE_PRODUCT_KEY') from e
            return self._dict(c.execute('SELECT * FROM product_catalog WHERE product_id=?',(pid,)).fetchone())
    def disable_product(self, product_id):
        with self._db() as c:
            c.execute('UPDATE product_catalog SET active=0,updated_at=? WHERE product_id=?',(int(self.clock()),str(product_id)))
            if c.total_changes==0: raise ProductCatalogNotFoundError('产品不存在')
        return self.get_product(product_id,True)
    def resolve_candidate(self,candidate,session_name):
        if not isinstance(candidate,dict): raise ProductCatalogValidationError('候选数据格式无效')
        if candidate.get('session_name') and str(candidate['session_name']) != str(session_name): raise ProductCatalogValidationError('会话不匹配','SESSION_MISMATCH')
        terms=[candidate.get(k) for k in ('product_key','product_name','sku','alias','product_tag')]
        terms=[normalize_tag(x) for x in terms if x]
        with self._db() as c: rows=c.execute('SELECT * FROM product_catalog WHERE active=1 ORDER BY product_key').fetchall()
        matches=[]
        for r in rows:
            d=self._dict(r); vals={normalize_tag(d['product_key']),normalize_tag(d['product_name']),normalize_tag(d['sku'])}|{normalize_tag(a) for a in d['aliases']}
            if any(t in vals for t in terms): matches.append(d)
        if len(matches)!=1: return {'match_status':'ambiguous' if len(matches)>1 else 'unmatched','product':matches[0] if len(matches)==1 else None,'candidates':matches}
        return {'match_status':'matched','product':matches[0],'candidates':matches}
    def reference_assets(self,product_id,variant=''):
        with self._db() as c:
            q='SELECT * FROM product_image_assets WHERE product_id=? AND active=1 AND allow_visual_match=1'; args=[str(product_id)]
            if variant: q+=' AND variant=?'; args.append(str(variant))
            rows=c.execute(q+' ORDER BY sort_order,created_at,id',args).fetchall()
        return [dict(r) for r in rows]
    def import_legacy_assets(self):
        imported=review=0
        with self._db() as c:
            rows=c.execute("SELECT * FROM product_image_assets WHERE product_id IS NULL AND legacy_import_status='pending' ORDER BY id").fetchall()
            grouped={}
            for r in rows:
                # Product name is the strongest legacy identity. Fall back to
                # SKU, then the first alias when older rows lack a name.
                identity = normalize_tag(r['product_name']) or normalize_tag(r['sku'])
                if not identity:
                    try: identity = next((normalize_tag(v) for v in json.loads(r['aliases_json'] or '[]') if normalize_tag(v)), '')
                    except ValueError: identity = ''
                grouped.setdefault(identity, []).append(r)
            for r in rows:
                keys={normalize_tag(v) for v in [r['product_name'],r['sku']] if normalize_tag(v)}
                try: keys |= {normalize_tag(v) for v in json.loads(r['aliases_json'] or '[]') if normalize_tag(v)}
                except ValueError: pass
                if not keys: c.execute("UPDATE product_image_assets SET legacy_import_status='review' WHERE id=?",(r['id'],)); review+=1; continue
                identity = normalize_tag(r['product_name']) or normalize_tag(r['sku'])
                if not identity:
                    identity = next(iter(keys), '')
                members = grouped.get(identity, [])
                # Duplicate legacy rows sharing an identity cannot all be
                # deterministically assigned. Keep the first stable row and
                # mark subsequent rows for manual review.
                if members and members[0]['id'] != r['id']:
                    c.execute("UPDATE product_image_assets SET legacy_import_status='review' WHERE id=?",(r['id'],)); review+=1; continue
                # Keep key derivation stable when multiple legacy fields exist.
                key = normalize_tag(r['product_name']) or normalize_tag(r['sku'])
                if not key:
                    key = sorted(keys)[0]
                product_key=re.sub(r'[^a-z0-9_-]+','-',key.casefold()).strip('-_')[:56]
                if not product_key: product_key='legacy-'+hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]
                if not self.KEY_RE.fullmatch(product_key):
                    c.execute("UPDATE product_image_assets SET legacy_import_status='review' WHERE id=?",(r['id'],)); review+=1; continue
                prow=c.execute('SELECT * FROM product_catalog WHERE product_key=?',(product_key,)).fetchone()
                if not prow:
                    pid=str(uuid.uuid4()); name=r['product_name'] or r['sku'] or key; c.execute("INSERT INTO product_catalog(product_id,product_key,product_name,sku,aliases_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",(pid,product_key,name,r['sku'] or '',r['aliases_json'] or '[]',int(self.clock()),int(self.clock())))
                else: pid=prow['product_id']
                c.execute("UPDATE product_image_assets SET product_id=?,legacy_import_status='imported' WHERE id=?",(pid,r['id'])); imported+=1
        return {'imported':imported,'review':review,'total':imported+review}
