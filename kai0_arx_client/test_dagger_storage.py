import tempfile,threading,json,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from dagger_runtime import Dagger
import dagger_runtime
class StorageTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  with patch('threading.Thread.start'):self.d=Dagger(self.root/'old')
 def tearDown(self):self.tmp.cleanup()
 def test_change_does_not_move_existing_data(self):
  self.d.root.mkdir();(self.d.root/'keep').write_text('keep');old=self.d.root
  self.d.configure_storage(str(self.root/'new'))
  self.assertTrue((old/'keep').exists());self.assertEqual(self.d.snapshot()['data_root'],str(self.root/'new'))
 def test_reject_relative_and_root(self):
  for value in ['relative','/','',None]:
   with self.assertRaises(ValueError):self.d.configure_storage(value)
 def test_writer_and_export_block_change(self):
  self.d.recorder=Mock(closed=False)
  with self.assertRaises(ValueError):self.d.configure_storage(str(self.root/'new'))
  self.d.recorder=Mock(closed=True);self.d.recorder.process.poll.return_value=None
  with self.assertRaises(ValueError):self.d.configure_storage(str(self.root/'new'))
  self.d.recorder=None;self.d.task={'status':'exporting'}
  with self.assertRaises(ValueError):self.d.configure_storage(str(self.root/'new'))
 def test_settings_survive_restart(self):
  with patch.object(dagger_runtime,'ROOT',self.root),patch.dict('os.environ',{},clear=True),patch('threading.Thread.start'):
   d=Dagger();d.configure_storage(str(self.root/'chosen'));other=Dagger()
   self.assertEqual(other.root,self.root/'chosen')
 def test_failed_path_does_not_replace_current(self):
  bad=self.root/'file';bad.write_text('x');old=self.d.root
  with self.assertRaises(OSError):self.d.configure_storage(str(bad))
  self.assertEqual(self.d.root,old)
