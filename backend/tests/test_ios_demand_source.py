import io
from types import SimpleNamespace
from unittest.mock import Mock
from PIL import Image
from ai_phone.agent.trajectory_cache.capture_sources.ios_demand import IosDemandSource


def test_demand_does_not_start_stream_and_preserves_capture_arguments():
    buffer=io.BytesIO()
    Image.new('RGB',(10,20),'white').save(buffer,format='JPEG')
    shot=Mock(return_value=buffer.getvalue())
    request=Mock()
    driver=SimpleNamespace(_wda=SimpleNamespace(_ensure_session=lambda:'existing',_request=request),screenshot_jpeg=shot,window_size=lambda:(10,20))
    source=IosDemandSource(driver)
    source.initialize()
    assert source.thread is None and source.forwarder is None and source.client is None
    assert source.capture(80,None)==buffer.getvalue()
    shot.assert_called_with(quality=80,max_side=None)
    source.close()
    request.assert_not_called()
