import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.trajectory_cache.capture_sources.ios import IosMjpegSource


def test_capture_requires_new_frame():
    s=IosMjpegSource(SimpleNamespace())
    s.frame=b'old'
    def new():
        with s.condition:
            s.frame=b'new'
            s.sequence+=1
            s.condition.notify_all()
    timer=threading.Timer(.02,new)
    timer.start()
    assert s.capture(95,None)==b'new'
    timer.join()


def test_capture_error_does_not_return_old_frame():
    s=IosMjpegSource(SimpleNamespace())
    s.frame=b'old'
    s.error='connection lost'
    with pytest.raises(RuntimeError):
        s.capture(95,None)


def test_click_restores_settings_on_failure_and_never_repeats():
    request=Mock(side_effect=[{'value':{'waitForIdleTimeout':10,'animationCoolOffTimeout':2}}, {}, {}])
    driver=SimpleNamespace(_wda=SimpleNamespace(_request=request),click=Mock(side_effect=RuntimeError('tap failed')))
    s=IosMjpegSource(driver)
    s.settings_path='/settings'
    with pytest.raises(RuntimeError):
        s.click(100,200)
    driver.click.assert_called_once_with(100,200)
    assert request.call_args.args[2]['settings']=={'waitForIdleTimeout':10,'animationCoolOffTimeout':2}


def test_restore_failure_does_not_repeat_completed_tap():
    request=Mock(side_effect=[{'value':{'waitForIdleTimeout':10,'animationCoolOffTimeout':2}}, {}, RuntimeError('restore failed'), {}])
    driver=SimpleNamespace(_wda=SimpleNamespace(_request=request),click=Mock())
    s=IosMjpegSource(driver)
    s.settings_path='/settings'
    s.click(100,200)
    driver.click.assert_called_once()
    assert s.click_restore is not None
    s.close()
    assert s.click_restore is None
