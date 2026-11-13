import base64, tempfile, unittest
from pathlib import Path
from panel.product_image_service import ProductImageService, ProductImageValidationError
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
class ProductImageMatchingTests(unittest.TestCase):
 def setUp(self): self.t=tempfile.TemporaryDirectory(); self.s=ProductImageService(Path(self.t.name)/'x.db',Path(self.t.name)/'data')
 def tearDown(self): self.t.cleanup()
 def test_roles_variants_and_match_test(self):
  a=self.s.upload('a.png','image/png',PNG,'A',['a'],product_id='p',variant='red',reference_role='primary')
  self.assertEqual(self.s.resolve_product_asset('p','red')['status'],'FOUND')
  self.assertEqual(self.s.match_test(PNG,'image/png')['status'],'MATCHED')
 def test_duplicate_hash_incompatible_metadata_is_rejected(self):
  a=self.s.upload('a.png','image/png',PNG,'A',['a'],product_id='p',variant='red',reference_role='primary')
  with self.assertRaises(ProductImageValidationError) as error:
   self.s.upload('b.png','image/png',PNG,'B',['b'],product_id='q',variant='blue',reference_role='reference')
  self.assertEqual(error.exception.code, 'DUPLICATE_IMAGE_CONFLICT')
  current=self.s.get_asset(a['asset_id'])
  self.assertEqual(current['product_id'], 'p')
  self.assertEqual(current['variant'], 'red')
  self.assertEqual(current['reference_role'], 'primary')

 def test_duplicate_hash_compatible_metadata_merges_aliases_and_tags(self):
  first=self.s.upload(
   'a.png','image/png',PNG,'A',['old-tag'],'Caption',
   product_name='Product',sku='SKU-1',aliases=['Old alias'],product_id='p',variant='red',
   reference_role='reference',allow_visual_match=True,allow_auto_send=True,
  )
  second=self.s.upload(
   'b.png','image/png',PNG,'A',['new-tag'],'Caption',
   product_name='Product',sku='SKU-1',aliases=['New alias'],product_id='p',variant='red',
   reference_role='reference',allow_visual_match=True,allow_auto_send=True,
  )
  self.assertTrue(second['reused'])
  self.assertEqual(second['asset_id'], first['asset_id'])
  current=self.s.get_asset(first['asset_id'])
  self.assertEqual(current['product_id'], 'p')
  self.assertEqual(current['variant'], 'red')
  self.assertEqual(current['reference_role'], 'reference')
  self.assertTrue(current['allow_visual_match'])
  self.assertTrue(current['allow_auto_send'])
  self.assertEqual(current['aliases'], ['Old alias', 'New alias'])
  self.assertEqual({item['tag_key'] for item in current['tags']}, {'old-tag', 'new-tag'})
if __name__=='__main__': unittest.main()
