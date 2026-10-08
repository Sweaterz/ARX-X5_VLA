import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import device_release as release

UNITS=('arx-data-station.service','arx-button-control.service','tate-arx-ui.service')
class ReleaseTests(unittest.TestCase):
    def manager(self):
        return NS(demo=False, robot=None, owner=None, detached=False, busy=False,
                  mode='disconnected', server_stop_request=None,
                  dagger=NS(active=False, recorder=None, task={}),
                  _service_state=Mock(return_value='inactive'),
                  _read_local_status=Mock(), event=Mock(), disconnect=Mock(),
                  _stop_service_group=Mock(), _arm_lock_available=Mock(return_value=True),
                  _video_device_owners=Mock(return_value=[]), stop_event=threading.Event(),
                  pause_event=threading.Event(), release_status={}, error='')
    def test_confirmation_and_support_are_required(self):
        for data in ({},{'confirmed':True},{'supported':True}):
            with self.assertRaises(ValueError):release.validate_request(self.manager(),data,'page')
    def test_other_page_cannot_release_live_owner(self):
        m=self.manager();m.robot=object();m.owner='old';m.mode='holding'
        with self.assertRaises(ValueError):release.validate_request(m,{'confirmed':True,'supported':True},'new')
    def test_unsaved_recorder_blocks_release(self):
        m=self.manager();m.dagger.recorder=NS(done=threading.Event())
        with self.assertRaises(ValueError):release.validate_request(m,{'confirmed':True,'supported':True},'page')
    def test_recording_blocks_all_shutdowns(self):
        m=self.manager();m._service_state.side_effect=lambda u:'active' if u==UNITS[0] else 'inactive'
        m._read_local_status.return_value={'recording':{'state':'saving'}}
        with patch.object(release,'ownership',return_value={'known':True}):
            release.release(m,UNITS,UNITS[2],'/tmp/lock')
        m.disconnect.assert_not_called();m._stop_service_group.assert_not_called()
        self.assertEqual(m.release_status['state'],'error')
    def test_unknown_owner_is_not_stopped(self):
        m=self.manager()
        with patch.object(release,'ownership',return_value={'known':False}):
            release.release(m,UNITS,UNITS[2],'/tmp/lock')
        m.disconnect.assert_not_called();m._stop_service_group.assert_not_called()
    def test_fault_is_allowed_with_fresh_idle_recording(self):
        m=self.manager();m._service_state.side_effect=lambda u:'inactive' if u==UNITS[2] else 'active'
        m._read_local_status.return_value={'recording':{'state':'idle'},'arm':{'online':True,'age_ms':10,'mode':'fault'}}
        with patch.object(release,'ownership',return_value={'known':True}):
            self.assertEqual(release.preflight(m,UNITS,UNITS[2],'/tmp/lock')[UNITS[1]],'active')
    def test_station_unreachable_blocks_before_disconnect(self):
        m=self.manager();m._service_state.side_effect=lambda u:'active' if u==UNITS[0] else 'inactive'
        m._read_local_status.side_effect=OSError('offline')
        with patch.object(release,'ownership',return_value={'known':True}):
            release.release(m,UNITS,UNITS[2],'/tmp/lock')
        m.disconnect.assert_not_called()
    def test_idle_release_confirms_actual_lock_and_video(self):
        m=self.manager()
        with patch.object(release,'ownership',return_value={'known':True}):
            release.release(m,UNITS,UNITS[2],'/tmp/lock')
        self.assertEqual(m.release_status['state'],'success');m.disconnect.assert_called_once()
        m._arm_lock_available.assert_called();m._video_device_owners.assert_called()
    def test_demo_never_stops_real_services(self):
        m=self.manager();m.demo=True
        release.release(m,UNITS,UNITS[2],'/tmp/lock')
        m._service_state.assert_not_called();m._stop_service_group.assert_not_called()
    def test_station_release_keeps_station_http_running(self):
        m=self.manager();m._service_state.side_effect=lambda u:'active' if u==UNITS[0] else 'inactive'
        m._read_local_status.return_value={'recording':{'state':'idle'}}
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'{"state":"released"}'
        with patch.object(release,'ownership',return_value={'known':True}),patch.object(release.urllib.request,'urlopen',return_value=response) as post:
            release.release(m,UNITS,UNITS[2],'/tmp/lock')
        self.assertEqual(m.release_status['state'],'success')
        m._stop_service_group.assert_not_called()
        self.assertEqual(post.call_args.args[0].full_url,'http://127.0.0.1:8090/api/devices/release')
if __name__=='__main__':unittest.main()
