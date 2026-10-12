from types import SimpleNamespace
from unittest.mock import Mock
from ai_phone.agent.trajectory_cache.capture_sources.window_size import live_window_size


def test_live_orientation_and_no_cache():
    device=SimpleNamespace(info={'displayWidth':1080,'displayHeight':2400})
    fallback=Mock()
    assert live_window_size(device,fallback)==(1080,2400)
    device.info={'displayWidth':2400,'displayHeight':1080}
    assert live_window_size(device,fallback)==(2400,1080)
    fallback.assert_not_called()


def test_invalid_info_uses_original():
    fallback=Mock(return_value=(1080,2400))
    assert live_window_size(SimpleNamespace(info={'displayWidth':0,'displayHeight':2400}),fallback)==(1080,2400)
    fallback.assert_called_once()
