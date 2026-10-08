"""Batch export reuse/upload checks; no hardware, HDF5, or network is opened."""
import copy
import io
import json
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import Mock,patch

import batch_clean_human as batch
from dagger_segments import DEFAULT_HUMAN_MOTION_TRIM


def report(episode='ep_test'):
    return {'source':episode,'source_files_modified':False,'human_only':True,
            'selection':copy.deepcopy(batch.OPTIONS),'human_motion_trim':{'config':dict(DEFAULT_HUMAN_MOTION_TRIM)},
            'processor_validated':True,'segment_padding_validated':True,'action_chunk':[50,14],
            'action_shift_frames':1,'fps':30,'frames':12,'segments':2}


class BatchExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def make_export(self,suffix='100',payload=None):
        path=self.root/'exports'/f'ep_test_{suffix}'
        path.mkdir(parents=True)
        (path/'dagger_export.json').write_text(json.dumps(report() if payload is None else payload))
        (path/'meta').mkdir()
        (path/'meta'/'info.json').write_text(json.dumps({'codebase_version':'v3.0','fps':30,'total_frames':12,'total_episodes':2}))
        (path/'data').mkdir();(path/'data'/'file-000.parquet').write_bytes(b'fixture')
        for role in ('head','left','right'):
            video=path/'videos'/f'observation.images.{role}'/'chunk-000'
            video.mkdir(parents=True);(video/'file-000.mp4').write_bytes(b'fixture')
        return path

    def test_reuses_validated_exact_default_human_export(self):
        path=self.make_export()
        self.assertEqual(batch.completed_export(self.root,'ep_test'),str(path))

    def test_skips_newer_selected_or_excluded_export_and_reuses_older_match(self):
        older=self.make_export('100')
        for selection in ({'mode':'full','human_motion_trim':True},
                          {'mode':'selected','selected_segments':['1:0:9'],'human_motion_trim':True},
                          {**batch.OPTIONS,'selected_segments':['1:0:9']},
                          {**batch.OPTIONS,'excluded_segments':['1:0:9']},
                          {**batch.OPTIONS,'excluded_ranges':[[0,5]]},
                          {**batch.OPTIONS,'unknown_option':True}):
            with self.subTest(selection=selection):
                value=report();value['selection']=selection
                newer=self.make_export('200',value)
                self.assertEqual(batch.completed_export(self.root,'ep_test'),str(older))
                shutil.rmtree(newer)

    def test_custom_or_disabled_trim_is_not_the_default(self):
        for trim in (False,None,{'displacement_threshold_rad':.02}):
            value=report();value['selection']['human_motion_trim']=trim
            self.assertFalse(batch.default_human_report(value,'ep_test'))
        value=report();value['human_motion_trim']['config']['pre_roll_frames']=2
        self.assertFalse(batch.default_human_report(value,'ep_test'))

    def test_explicit_equivalent_default_options_are_allowed(self):
        value=report();value['selection'].update(selected_segments=[],excluded_segments=[],excluded_ranges=[],
                                               human_motion_trim=dict(DEFAULT_HUMAN_MOTION_TRIM))
        self.assertTrue(batch.default_human_report(value,'ep_test'))

    def test_incomplete_report_is_never_reused_even_with_validation_flags(self):
        self.make_export('999.incomplete')
        self.assertIsNone(batch.completed_export(self.root,'ep_test'))

    def test_missing_validation_or_inconsistent_metadata_is_not_reused(self):
        value=report();value['processor_validated']=False
        path=self.make_export(payload=value)
        self.assertIsNone(batch.completed_export(self.root,'ep_test'))
        (path/'dagger_export.json').write_text(json.dumps(report()))
        (path/'meta'/'info.json').write_text(json.dumps({'codebase_version':'v3.0','fps':30,'total_frames':11,'total_episodes':2}))
        self.assertIsNone(batch.completed_export(self.root,'ep_test'))

    def test_missing_camera_or_data_is_not_reused(self):
        path=self.make_export()
        (path/'videos'/'observation.images.right'/'chunk-000'/'file-000.mp4').unlink()
        self.assertIsNone(batch.completed_export(self.root,'ep_test'))
        (path/'videos'/'observation.images.right'/'chunk-000'/'file-000.mp4').write_bytes(b'fixture')
        (path/'data'/'file-000.parquet').unlink()
        self.assertIsNone(batch.completed_export(self.root,'ep_test'))

    def test_corrupt_report_is_skipped(self):
        path=self.make_export();(path/'dagger_export.json').write_text('{broken')
        self.assertIsNone(batch.completed_export(self.root,'ep_test'))

    def test_run_export_retains_terminal_report_after_unrelated_json(self):
        final={'status':'complete','path':'/fixture/export','report':report()}
        process=Mock(stdout=io.StringIO('\n'.join([json.dumps({'status':'exporting','progress':50}),
            json.dumps(final),'not json',json.dumps({'debug':'cleanup done'}),'[]'])))
        process.wait.return_value=0
        with patch.object(batch.subprocess,'Popen',return_value=process) as launch,patch('builtins.print'):
            self.assertEqual(batch.run_export(self.root,'ep_test'),final)
        self.assertEqual(json.loads(launch.call_args.args[0][-1]),batch.OPTIONS)

    def test_failed_exit_or_missing_terminal_validation_never_claims_success(self):
        for result,code in (({'status':'complete','path':'/fixture','report':report()},1),
                            ({'status':'exporting','progress':99},0),
                            ({'status':'complete','path':'/fixture'},0),
                            ({'status':'error','error':'encoder failed'},0)):
            with self.subTest(result=result,code=code):
                process=Mock(stdout=io.StringIO(json.dumps(result)));process.wait.return_value=code
                with patch.object(batch.subprocess,'Popen',return_value=process),patch('builtins.print'):
                    with self.assertRaises(RuntimeError):batch.run_export(self.root,'ep_test')

    def test_first_cloud_report_creates_remote_directory_before_atomic_upload(self):
        local=self.root/'report with spaces.json';local.write_text('{"status":"running"}')
        key=self.root/'key';key.write_text('fixture')
        uploader=batch.CloudUpload('fixture-host',22,key,'/fixture/root with spaces')
        with patch.object(batch.subprocess,'run',return_value=Mock(returncode=0,stdout=b'',stderr=b'')) as run:
            destination=uploader.report(local)
        command=run.call_args.args[0][-1]
        self.assertTrue(command.startswith('install -d -m 0775 '+shlex.quote('/fixture/root with spaces/reports')+' && cat > '))
        self.assertIn(' && mv ',command)
        self.assertEqual(run.call_args.kwargs['input'],local.read_bytes())
        self.assertEqual(destination,'/fixture/root with spaces/reports/report with spaces.json')


if __name__=='__main__':unittest.main()
