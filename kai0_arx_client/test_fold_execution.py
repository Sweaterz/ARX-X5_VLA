import importlib.util,json,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import numpy as np
import client
import test_gui as fixtures

class FoldExecution(TestCase):
    def setUp(self):
        f=fixtures.GuiTests();f.setUp();self.m=f.m;self.data=dict(f.data,steps=50)
    def test_fold_full_chunk_reaches_tail_without_scaling(self):
        class Fold(fixtures.Policy):
            metadata=dict(fixtures.Policy.metadata,recommended_actions_per_chunk=50,action_horizon=50)
            def infer(self,obs):
                a=np.tile(obs['state'],(50,1));a[20:,0]+=.4;return a
        before=self.m.robot.q.copy()
        with patch.object(client,'Policy',Fold):self.m.inference(self.data,True)
        sent=self.m.robot.commands[:-1]
        self.assertEqual(len(sent),50)
        self.assertAlmostEqual(sent[0][0],before[0]);self.assertAlmostEqual(sent[49][0],before[0]+.4)
        self.assertEqual(self.m.result['actions_per_chunk'],50)
    def test_place_plate_remains_eight(self):
        self.assertEqual(client.policy_chunk_count(fixtures.Policy.metadata,self.m.config),8)
    def test_ui_chunk_steps_override_checkpoint_recommendation(self):
        class Fold(fixtures.Policy):
            calls=0
            metadata=dict(fixtures.Policy.metadata,recommended_actions_per_chunk=50,action_horizon=50)
            def infer(self,obs):
                type(self).calls+=1
                return np.tile(obs['state'],(50,1))
        with patch.object(client,'Policy',Fold):
            self.m.inference(dict(self.data,steps=25,chunk_steps=10),True)
        self.assertEqual(Fold.calls,3)
        self.assertEqual(len(self.m.robot.commands[:-1]),25)
    def test_zero_ui_chunk_steps_keeps_checkpoint_recommendation(self):
        metadata={'recommended_actions_per_chunk':50,'action_horizon':50}
        self.assertEqual(client.policy_chunk_count(metadata,self.m.config,0),50)
        self.assertEqual(client.policy_chunk_count(metadata,self.m.config,10),10)
    def test_ui_chunk_steps_cannot_exceed_policy_horizon(self):
        with self.assertRaisesRegex(ValueError,'horizon'):
            client.policy_chunk_count({'recommended_actions_per_chunk':50,'action_horizon':50},self.m.config,51)
    def test_late_schedule_stops_without_catchup(self):
        with patch.object(client.time,'monotonic',return_value=2),self.assertRaisesRegex(TimeoutError,'250ms'):
            client.wait_policy_frame(10,0,self.m.config,lambda:None)
    def test_pause_inside_long_chunk_invalidates_remaining_actions(self):
        m=self.m;original=m.robot.command
        def command(a):
            original(a)
            if len(m.robot.commands)==12:m.pause_event.set();m.dagger.invalidate()
        m.robot.command=command
        class Fold(fixtures.Policy):metadata=dict(fixtures.Policy.metadata,recommended_actions_per_chunk=50,action_horizon=50)
        with patch.object(client,'Policy',Fold),self.assertRaises(fixtures.gui.PauseRequested):m.inference(self.data,True)
        self.assertEqual(len(m.robot.commands),12);m.pause_hold('test');self.assertEqual(m.mode,'holding')
    def test_total_step_budget_still_applies(self):
        class Fold(fixtures.Policy):metadata=dict(fixtures.Policy.metadata,recommended_actions_per_chunk=50,action_horizon=50)
        with patch.object(client,'Policy',Fold):self.m.inference(dict(self.data,steps=3),True)
        self.assertEqual(len(self.m.robot.commands),4)
    def test_bad_execution_metadata_is_rejected(self):
        for count in (True,0,51,'50'):
            with self.assertRaises(ValueError):client.policy_chunk_count({'recommended_actions_per_chunk':count,'action_horizon':50},self.m.config)
    def test_server_selects_profile_from_training_metadata(self):
        source=Path(__file__).parent/'server_preview.py'
        if not source.exists():source=Path(__file__).resolve().parent.parent/'kai0_arx_server/local_scripts/serve_arx_lerobot.py'
        spec=importlib.util.spec_from_file_location('server_preview',source)
        server_preview=importlib.util.module_from_spec(spec);spec.loader.exec_module(server_preview)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);config=SimpleNamespace(chunk_size=50)
            self.assertEqual(server_preview.execution_profile(p,config)[0],8)
            (p/'train_config.json').write_text(json.dumps({'dataset':{'repo_id':'local/fold_box_v1_pi05'}}))
            self.assertEqual(server_preview.execution_profile(p,config)[0],50)
            self.assertEqual(server_preview.execution_profile(p,SimpleNamespace(chunk_size=25))[0],25)
