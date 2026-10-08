import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import local_server as local
import test_gui as fixtures


def make_checkpoint(path,relative=False,policy='pi05'):
    path.mkdir(parents=True)
    features={'observation.state':{'shape':[14]},'observation.images.head':{},
              'observation.images.left':{},'observation.images.right':{}}
    config={'type':policy,'use_relative_actions':relative,'input_features':features,
            'output_features':{'action':{'shape':[14]}},
            'action_feature_names':local.ACTION_NAMES if policy=='pi05' else None}
    (path/'config.json').write_text(json.dumps(config))
    (path/'model.safetensors').write_bytes(b'model')
    (path/'policy_preprocessor.json').write_text('{"steps":[]}')
    (path/'policy_postprocessor.json').write_text('{"steps":[]}')
    (path/'tokenizer').mkdir();(path/'tokenizer/tokenizer.json').write_text('{}')
    (path/'tokenizer/tokenizer_config.json').write_text('{}')
    return path


class Tests(unittest.TestCase):
    def test_missing_checkpoint_never_spawns(self):
        server=local.LocalServer()
        with tempfile.TemporaryDirectory() as root,patch.object(local,'SERVER_ROOT',Path(root)), \
             patch.object(server,'health',side_effect=OSError('not running')),patch.object(local.subprocess,'Popen') as spawn:
            server._run()
            spawn.assert_not_called()
            self.assertEqual(server.snapshot()['status'],'error')

    def test_busy_port_never_spawns(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);checkpoint=make_checkpoint(base/'checkpoints/task/010000/pretrained_model')
            (base/'CHECKPOINT_VERIFIED.json').write_text(json.dumps({'checkpoint':str(checkpoint)}))
            with patch.object(local,'SERVER_ROOT',base),patch.object(local,'FOLD_CHECKPOINT_ROOT',base/'fold'):
                server=local.LocalServer()
                with patch.object(server,'health',side_effect=OSError()),patch.object(local.socket,'socket') as sock, \
                     patch.object(server,'managed_pid',return_value=None),patch.object(local.subprocess,'Popen') as spawn:
                    sock.return_value.__enter__.return_value.bind.side_effect=OSError('in use')
                    server._run();spawn.assert_not_called()
                    self.assertIn('占用',server.snapshot()['message'])

    def test_discovers_and_selects_verified_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);checkpoint=make_checkpoint(base/'checkpoints/fold_box/020000/pretrained_model')
            (base/'CHECKPOINT_VERIFIED.json').write_text(json.dumps({'checkpoint':str(checkpoint)}))
            with patch.object(local,'SERVER_ROOT',base),patch.object(local,'FOLD_CHECKPOINT_ROOT',base/'fold'):
                server=local.LocalServer()
            state=server.snapshot()
            self.assertEqual(state['checkpoint'],str(checkpoint))
            self.assertEqual(state['checkpoint_name'],'fold_box / 020000')
            self.assertEqual(state['checkpoint_options'],[{'name':'fold_box / 020000','path':str(checkpoint),
                              'verified':True,'source':'bundled','selectable':True}])

    def test_discovers_compatible_fold_checkpoints_without_transfer_marker(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);fold=base/'fold';checkpoint=make_checkpoint(fold/'006250/pretrained_model')
            make_checkpoint(fold/'013964/pretrained_model',relative=True)
            make_checkpoint(fold/'act/pretrained_model',policy='act')
            with patch.object(local,'SERVER_ROOT',base/'server'),patch.object(local,'FOLD_CHECKPOINT_ROOT',fold):
                server=local.LocalServer();options=server.snapshot()['checkpoint_options']
            self.assertEqual(options,[{'name':'fold / 006250','path':str(checkpoint),'verified':False,
                                      'source':'fold','selectable':True}])
            self.assertEqual(server.checkpoint,checkpoint)

    def test_fold_mixture_is_included_in_checkpoint_name(self):
        with tempfile.TemporaryDirectory() as root:
            fold=Path(root)/'fold'
            with patch.object(local,'FOLD_CHECKPOINT_ROOT',fold):
                for mixture in ('dagger50_orig50','dagger20_orig80'):
                    checkpoint=fold/f'rtc_d10_{mixture}_epoch4_from007500_20260924/017560/pretrained_model'
                    self.assertEqual(local.checkpoint_name(checkpoint),f'fold / {mixture} / 017560')

    def test_unverified_checkpoint_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);checkpoint=make_checkpoint(base/'checkpoints/fold_box/020000/pretrained_model')
            with patch.object(local,'SERVER_ROOT',base),patch.object(local,'FOLD_CHECKPOINT_ROOT',base/'fold'):
                server=local.LocalServer()
                with self.assertRaisesRegex(ValueError,'未通过兼容性检查'):server.select_checkpoint(str(checkpoint))

    def test_selected_checkpoint_is_passed_to_server_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);checkpoint=make_checkpoint(base/'checkpoints/fold_box/020000/pretrained_model')
            (base/'logs').mkdir();(base/'CHECKPOINT_VERIFIED.json').write_text(json.dumps({'checkpoint':str(checkpoint)}))
            process=Mock(pid=321);process.poll.return_value=None
            meta={'checkpoint':str(checkpoint),'weights_strictly_loaded':813}
            with patch.object(local,'SERVER_ROOT',base),patch.object(local,'ROOT',base/'client'), \
                 patch.object(local,'RECORD',base/'logs/managed-server.json'), \
                 patch.object(local,'FOLD_CHECKPOINT_ROOT',base/'fold'):
                server=local.LocalServer();(base/'client').mkdir()
                with patch.object(server,'health',side_effect=[OSError(),meta]), \
                     patch.object(server,'managed_pid',return_value=None),patch.object(local.socket,'socket'), \
                     patch.object(local,'process_identity',return_value='start'), \
                     patch.object(local.subprocess,'Popen',return_value=process) as spawn:
                    server._run()
            command=spawn.call_args.args[0]
            self.assertEqual(command[-4:-2],['--checkpoint',str(checkpoint)])

    def test_unrelated_health_is_not_accepted(self):
        server=local.LocalServer()
        response=io.BytesIO(json.dumps({'service':'unrelated'}).encode())
        opener=Mock();opener.open.return_value=response
        with patch.object(local.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(RuntimeError,'不是预期'):server.health()

    def test_no_duplicate_start_worker(self):
        server=local.LocalServer();server.worker=Mock();server.worker.is_alive.return_value=True
        with patch.object(local.threading,'Thread') as worker:
            server.start();worker.assert_not_called()

    def test_gui_allows_model_load_while_robot_connected(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m
        robot=m.robot
        m.submit('server_start',{},'owner')
        self.assertEqual(m.jobs.get_nowait()[0],'server_start')
        self.assertIs(m.robot,robot);self.assertTrue(m.connected)

    def test_gui_allows_connect_during_model_load(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m;m.robot=None;m.connected=False
        m.local_server.update(status='starting')
        m.submit('connect',{'confirmed':True},'owner')
        self.assertEqual(m.jobs.get_nowait()[0],'connect')

    def test_gui_start_server_does_not_initialize_robot(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m;m.robot=None;m.connected=False
        with patch.object(m.local_server,'start') as start,patch.object(fixtures.client,'SDKRobot') as sdk:
            m.execute('server_start',{})
            start.assert_called_once_with(prompt=None);sdk.assert_not_called()

    def test_gui_checkpoint_selection_while_connected(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m
        m.local_server.update(status='stopped')
        robot=m.robot
        with patch.object(m.local_server,'select_checkpoint') as select:
            m.submit('server_checkpoint',{'checkpoint':'/tmp/model'},'owner')
            select.assert_called_once_with('/tmp/model')
        self.assertIs(m.robot,robot);self.assertTrue(m.connected)

    def test_stop_waits_for_launch_lock_release(self):
        server=local.LocalServer()
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);(base/'logs').mkdir()
            with patch.object(local,'SERVER_ROOT',base),patch.object(local,'RECORD',base/'record.json'),patch.object(server,'managed_pid',side_effect=[12,None,None]),patch.object(local,'process_identity',return_value='start'),patch.object(local.subprocess,'run') as signal_process,patch.object(local.fcntl,'flock',side_effect=[BlockingIOError(),None,None]),patch.object(local.time,'sleep') as sleep:
                server._terminate()
                sleep.assert_called_once_with(.1)
                signal_process.assert_called_once()
                self.assertEqual(server.snapshot()['status'],'stopped')

    def test_auto_refresh_reports_ready_and_checkpoint(self):
        server=local.LocalServer()
        with patch.object(server,'health',return_value={'checkpoint':str(local.CHECKPOINT),'weights_strictly_loaded':813}),patch.object(server,'managed_pid',return_value=123):
            server.refresh()
        self.assertEqual(server.snapshot()['status'],'ready')
        self.assertTrue(server.snapshot()['managed'])

    def test_unmanaged_server_cannot_be_terminated(self):
        server=local.LocalServer()
        with patch.object(server,'managed_pid',return_value=None),patch.object(local.os,'pidfd_open',create=True) as opened:
            with self.assertRaisesRegex(RuntimeError,'受管'):server._terminate()
            opened.assert_not_called()

    def test_close_during_inference_holds_before_stopping_server(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m;m.busy=True
        class Policy(fixtures.Policy):
            def infer(self,obs):
                m.submit('server_stop',{},'owner')
                return super().infer(obs)
        with patch.object(fixtures.client,'Policy',Policy),patch.object(m.local_server,'stop') as stop:
            with self.assertRaises(fixtures.gui.PauseRequested):m.inference(fixture.data,True)
            stop.assert_not_called()
            m.pause_hold('pause to stop server')
            self.assertEqual(m.mode,'holding')
            self.assertEqual(len(m.robot.commands),1)
            m.finish_server_stop()
            stop.assert_called_once()
            self.assertTrue(m.connected)

    def test_failed_feedback_cancels_server_stop(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m
        m.mode='holding';m.server_stop_request={'must_hold':True}
        with patch.object(m.robot,'maintain_hold',side_effect=RuntimeError('fault')),patch.object(m.local_server,'stop') as stop:
            with self.assertRaisesRegex(RuntimeError,'fault'):m.finish_server_stop()
            stop.assert_not_called()
            self.assertIsNone(m.server_stop_request)

    def test_server_stop_checks_control_owner(self):
        fixture=fixtures.GuiTests();fixture.setUp();m=fixture.m
        with self.assertRaisesRegex(ValueError,'控制权'):m.submit('server_stop',{},'other')
        self.assertIsNone(m.server_stop_request)


if __name__=='__main__':unittest.main()
