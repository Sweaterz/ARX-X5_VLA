import tempfile,unittest
from pathlib import Path
from folder_browser import browse_folders
class FolderTests(unittest.TestCase):
 def test_lists_only_visible_directories_and_canonical_paths(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();(root/'B').mkdir();(root/'a').mkdir();(root/'.hidden').mkdir();(root/'file').write_text('not returned')
   data=browse_folders(str(root),root)
   self.assertEqual([x['name'] for x in data['folders']],['a','B']);self.assertEqual(data['path'],str(root));self.assertEqual(data['parent'],str(root.parent));self.assertTrue(data['writable'])
 def test_invalid_paths(self):
  with tempfile.TemporaryDirectory() as tmp:
   (Path(tmp)/'file').write_text('x')
   for value in ['relative',str(Path(tmp)/'file'),str(Path(tmp)/'missing'),'x'*5000]:
    with self.assertRaises((ValueError,OSError)):browse_folders(value,tmp)
 def test_default(self):
  with tempfile.TemporaryDirectory() as tmp:self.assertEqual(browse_folders(None,tmp)['path'],str(Path(tmp).resolve()))
