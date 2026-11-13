import sqlite3, tempfile, unittest
from pathlib import Path
from panel.app import init_db
from panel.product_catalog_service import ProductCatalogService, ProductCatalogValidationError

class ProductCatalogTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory(); self.db=Path(self.t.name)/'db.sqlite3'; init_db(self.db, seed_business=False)
        self.s=ProductCatalogService(self.db)
    def tearDown(self): self.t.cleanup()
    def test_schema_is_idempotent_and_catalog_roundtrip(self):
        init_db(self.db, seed_business=False)
        p=self.s.save_product({'product_key':'iptv-premium','product_name':'IPTV Premium','sku':'IPTV-P','aliases':['premium iptv']})
        self.assertEqual(self.s.get_product(p['product_id'])['product_key'],'iptv-premium')
        self.assertEqual(self.s.list_products()['total'],1)
    def test_validation_and_duplicate_key(self):
        self.s.save_product({'product_key':'one','product_name':'One'})
        with self.assertRaises(ProductCatalogValidationError): self.s.save_product({'product_key':'bad key','product_name':'Bad'})
        with self.assertRaises(ProductCatalogValidationError): self.s.save_product({'product_key':'one','product_name':'Dup'})
        disabled=self.s.save_product({'product_key':'two','product_name':'Two','active':'false','allow_ai_image_send':'false'})
        self.assertFalse(disabled['active']); self.assertFalse(disabled['allow_ai_image_send'])
    def test_inactive_filter_and_cross_session_rejection(self):
        p=self.s.save_product({'product_key':'one','product_name':'One'})
        self.s.disable_product(p['product_id'])
        self.assertEqual(self.s.list_products()['total'],0)
        self.assertEqual(self.s.list_products(True)['total'],1)
        with self.assertRaises(ProductCatalogValidationError): self.s.resolve_candidate({'product_key':'one','session_name':'other'},'default')
    def test_legacy_import_unique_and_ambiguous(self):
        c=sqlite3.connect(self.db)
        c.execute("INSERT INTO product_image_assets(id,title,caption_template,relative_path,original_filename,mime_type,byte_size,width,height,sha256,active,sort_order,created_at,updated_at,product_name,sku,aliases_json) VALUES ('a','A','','a','a.png','image/png',1,1,1,'ha',1,0,1,1,'One','SKU1','[]')")
        c.execute("INSERT INTO product_image_assets(id,title,caption_template,relative_path,original_filename,mime_type,byte_size,width,height,sha256,active,sort_order,created_at,updated_at,product_name,sku,aliases_json) VALUES ('b','B','','b','b.png','image/png',1,1,1,'hb',1,0,1,1,'Two','SKU2','[]')")
        c.commit(); c.close()
        result=self.s.import_legacy_assets(); self.assertEqual(result['imported'],1); self.assertEqual(result['review'],1)
        with sqlite3.connect(self.db) as c:
            rows=c.execute('SELECT product_id,legacy_import_status FROM product_image_assets ORDER BY id').fetchall()
            key=c.execute('SELECT product_key FROM product_catalog WHERE product_id=?',(rows[0][0],)).fetchone()[0]
        self.assertIsNotNone(rows[0][0]); self.assertEqual(rows[1][1],'review')
        self.assertEqual(key,'one')

if __name__=='__main__': unittest.main()
