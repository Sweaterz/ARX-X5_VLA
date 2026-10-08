import tempfile,json,time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import local_server as local
from test_local_server import make_checkpoint
from test_gui import GuiTests,Policy,client,gui

class CheckpointFolders(TestCase):
    def test_custom_folder_persist_and_launch_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);model=make_checkpoint(root/'external/025000/pretrained_model')
            with patch.object(local,'SELECTION',root/'selection.json'),patch.object(local,'RECORD',root/'record'),patch.object(local,'SERVER_ROOT',root/'server'),patch.object(local,'FOLD_CHECKPOINT_ROOT',root/'fold'):
                s=local.LocalServer();s.select_checkpoint(str(model),folder=True)
                self.assertTrue(local.checkpoint_allowed(model));self.assertEqual(s.snapshot()['checkpoint_name'],'external / 025000')
                self.assertEqual(local.LocalServer().checkpoint,model)
                (model/'model.safetensors').unlink();self.assertFalse(local.checkpoint_allowed(model))
    def test_invalid_relative_model_keeps_selection(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(local.LocalServer,'managed_pid',return_value=None):
            model=make_checkpoint(Path(tmp)/'bad',relative=True);s=local.LocalServer();before=s.checkpoint
            with self.assertRaisesRegex(ValueError,'不兼容'):s.select_checkpoint(str(model),folder=True)
            self.assertEqual(before,s.checkpoint)
    def test_running_server_blocks_change(self):
        s=local.LocalServer();s.update(status='ready')
        with self.assertRaisesRegex(ValueError,'关闭 Server'):s.select_checkpoint('/tmp/new',folder=True)
    def test_live_process_blocks_change_despite_bad_health(self):
        s=local.LocalServer();s.update(status='unavailable')
        with patch.object(s,'managed_pid',return_value=123),self.assertRaisesRegex(ValueError,'进程仍存在'):s.select_checkpoint('/tmp/new',folder=True)

class ContinuousSteps(TestCase):
    def setUp(self):
        f=GuiTests();f.setUp();self.m=f.m;self.data=f.data
    def test_above_300_and_continuous_accepted(self):
        for steps in (301,100000,0):
            self.m.submit('run',dict(self.data,steps=steps),'owner');self.m.jobs.get_nowait();self.m.busy=False
    def test_invalid_steps_rejected(self):
        for steps in (-1,1.5,True,None):
            with self.assertRaises(ValueError):self.m.submit('run',dict(self.data,steps=steps),'owner')
    def test_zero_no_motion_still_single_inference(self):
        with patch.object(client,'Policy',Policy):self.m.inference(dict(self.data,steps=0),False)
        self.assertEqual(self.m.result['steps'],1);self.assertEqual(self.m.robot.commands,[])
    def test_continuous_can_pause_and_keeps_trace_bounded(self):
        m=self.m;original=m.robot.command;count=[0]
        def command(a):
            original(a);count[0]+=1
            if count[0]==12:m.pause_event.set()
        m.robot.command=command
        with patch.object(client,'Policy',Policy),patch.object(gui.time,'sleep'),self.assertRaises(gui.PauseRequested):m.inference(dict(self.data,steps=0),True)
        self.assertEqual(count[0],12);m.pause_hold('test');self.assertEqual(m.mode,'holding')
        records=[e for e in m.events if e['message']=='SDK 动作请求记录'];self.assertTrue(records)
        self.assertTrue(all(len(e['requests'])<=m.config['control']['actions_per_chunk'] for e in records))

class PromptAssociation(TestCase):
    def test_known_training_dataset_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            self.assertEqual(local.checkpoint_prompt(p)[0],'')
            (p/'train_config.json').write_text(json.dumps({'dataset':{'repo_id':'local/fold_box_v1_pi05'}}))
            self.assertEqual(local.checkpoint_prompt(p)[0],'fold the paper boxes.')
            (p/'inference_prompt.json').write_text(json.dumps({'prompt':'custom folding task'}))
            self.assertEqual(local.checkpoint_prompt(p)[0],'custom folding task')
    def test_switch_updates_and_unknown_clears_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);known=make_checkpoint(root/'known');unknown=make_checkpoint(root/'unknown')
            (known/'inference_prompt.json').write_text(json.dumps({'prompt':'new task'}))
            with patch.object(local,'SELECTION',root/'selection.json'),patch.object(local,'RECORD',root/'record'):
                s=local.LocalServer();s.select_checkpoint(str(known),folder=True)
                self.assertEqual(s.snapshot()['default_prompt'],'new task')
                s.select_checkpoint(str(unknown),folder=True);self.assertEqual(s.snapshot()['default_prompt'],'')
    def test_start_passes_user_prompt_and_blocks_empty(self):
        s=local.LocalServer()
        with patch.object(local.threading,'Thread') as thread:
            s.start(prompt='fold updated task');self.assertEqual(s.launch_prompt,'fold updated task')
        s.worker=None
        with self.assertRaisesRegex(ValueError,'任务描述'):s.start(prompt=' ')
