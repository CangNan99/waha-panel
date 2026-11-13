import base64, tempfile, unittest
from pathlib import Path
from panel.product_image_service import ProductImageService
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
class ProductImageMatchingTests(unittest.TestCase):
 def setUp(self): self.t=tempfile.TemporaryDirectory(); self.s=ProductImageService(Path(self.t.name)/'x.db',Path(self.t.name)/'data')
 def tearDown(self): self.t.cleanup()
 def test_roles_variants_and_match_test(self):
  a=self.s.upload('a.png','image/png',PNG,'A',['a'],product_id='p',variant='red',reference_role='primary')
  self.assertEqual(self.s.resolve_product_asset('p','red')['status'],'FOUND')
  self.assertEqual(self.s.match_test(PNG,'image/png')['status'],'MATCHED')
 def test_duplicate_hash_association(self):
  a=self.s.upload('a.png','image/png',PNG,'A',['a'],product_id='p')
  b=self.s.upload('b.png','image/png',PNG,'B',['b'],product_id='q',variant='blue')
  self.assertTrue(b['reused']); self.assertEqual(self.s.get_asset(a['asset_id'])['product_id'],'q')
if __name__=='__main__': unittest.main()
