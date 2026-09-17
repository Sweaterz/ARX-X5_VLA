import unittest,tempfile,time,json,subprocess
from pathlib import Path
import numpy as np
from dagger_segments import inventory,select_parts
from dagger_data import Recorder,ROOT,DATA_PYTHON,worker_env
from test_dagger import metadata,frame

class SelectionTests(unittest.TestCase):
 def setUp(self):
  self.t=np.arange(30)/30;self.labels=np.repeat([0,1,0],10);self.seg=np.repeat([1,2,3],10)
 def test_default_human_only(self):
  p=select_parts(self.t,self.labels,self.seg);self.assertEqual([x.tolist() for x in p],[list(range(10,20))])
 def test_full_preserves_all_boundaries(self):
  self.assertEqual([len(p) for p in select_parts(self.t,self.labels,self.seg,{'mode':'full'})],[10,10,10])
 def test_exclusion_creates_new_boundary_and_never_bridges(self):
  p=select_parts(self.t,self.labels,self.seg,{'mode':'full','excluded_ranges':[[4,5],[5,5]]})
  self.assertEqual([x.tolist() for x in p],[list(range(4)),list(range(6,10)),list(range(10,20)),list(range(20,30))])
 def test_selected_order_is_chronological(self):
  ids=[x['id'] for x in inventory(self.t,self.labels,self.seg)]
  p=select_parts(self.t,self.labels,self.seg,{'mode':'selected','selected_segments':[ids[2],ids[0]]})
  self.assertEqual([p[0][0],p[1][0]],[0,20])
 def test_label_and_gap_split_even_same_segment(self):
  t=self.t.copy();t[20:]+=1
  self.assertEqual(len(select_parts(t,self.labels,np.ones(30),{'mode':'full'})),3)
 def test_invalid_and_empty_selection(self):
  for opts in [{'mode':'x'},{'mode':'selected'}, {'mode':'selected','selected_segments':['bad']},{'excluded_ranges':[[3,2]]},{'excluded_ranges':[[0,30]]},{'excluded_ranges':[[True,3]]}]:
   with self.assertRaises(ValueError):select_parts(self.t,self.labels,self.seg,opts)
 def test_stationary_is_not_automatically_removed(self):
  self.assertEqual(len(select_parts(self.t,self.labels,self.seg,{'mode':'full'})),3)
 def test_exclude_whole_segment(self):
  ids=[x['id'] for x in inventory(self.t,self.labels,self.seg)]
  self.assertEqual(len(select_parts(self.t,self.labels,self.seg,{'mode':'full','excluded_segments':[ids[0]]})),2)

class RealExportTests(unittest.TestCase):
 def test_full_and_selected_lerobot_preserve_labels_and_cuts(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);r=Recorder(metadata(),root);self.assertTrue(r.ready.wait(15))
   for i in range(30):
    item=frame(i,human=int(10<=i<20),segment=i//10+1)
    while True:
     try:r.send('frame',item);break
     except RuntimeError as e:
      if '缓冲' not in str(e):raise
      time.sleep(.03)
   r.finish();self.assertTrue(r.done.wait(30));self.assertEqual(r.snapshot()['status'],'saved')
   for opts,count in [({'mode':'full','excluded_ranges':[[4,5]]},4),({'mode':'selected','selected_segments':['2:10:19','3:20:29'],'excluded_ranges':[[23,25]]},3)]:
    p=subprocess.run([DATA_PYTHON,str(ROOT/'dagger_worker.py'),'export',str(root),r.id,json.dumps(opts)],env=worker_env(),capture_output=True,text=True,timeout=120)
    self.assertEqual(p.returncode,0,p.stdout+p.stderr)
    report=json.loads(p.stdout.strip().splitlines()[-1]);self.assertEqual(report['report']['segments'],count);self.assertTrue(report['report']['segment_padding_validated'])
    import pyarrow.parquet as pq
    rows=[row for path in Path(report['path']).glob('data/**/*.parquet') for row in pq.read_table(path).to_pylist()]
    self.assertEqual({int(np.asarray(row['intervention']).reshape(-1)[0]) for row in rows},{0,1})
    self.assertTrue(all(row['action'][0]>row['observation.state'][0] for row in rows))
    self.assertTrue(all(row['action'][6]<0 for row in rows))
    self.assertEqual(report['report']['selection'],opts)
