import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.trajectory_cache.capture_sources.harmony import HarmonyFastChannel


class Socket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.timeout = 9

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        return next(self.chunks, b'')


def channel(chunks):
    sock = Socket(chunks)
    original = Mock()
    client = SimpleNamespace(sock=sock, _recv_msg=original)
    def invoke(api):
        assert api == 'captureLayout'
        return SimpleNamespace(result=json.loads(client._recv_msg(decode=True)))
    client.invoke_captures = invoke
    raw = SimpleNamespace(_client=client, _invoke=Mock(return_value='done'))
    driver = SimpleNamespace(_raw=raw, _call_with_reconnect=lambda fn: fn())
    return HarmonyFastChannel(driver), client, original


def test_complete_large_unicode_tree_and_restore_reader():
    data=json.dumps({'attributes':{'text':'鸿蒙'*4000},'children':[]},ensure_ascii=False).encode()
    c, client, original=channel([data[:4095],data[4095:8000],data[8000:]])
    assert c.tree()['attributes']['text']=='鸿蒙'*4000
    assert client._recv_msg is original and client.sock.gettimeout()==9


def test_truncated_tree_fails_and_restores_reader():
    c, client, original=channel([b'{"attributes":'])
    with pytest.raises(ConnectionError):
        c.tree()
    assert client._recv_msg is original and client.sock.gettimeout()==9


def test_direct_click_waits_for_single_invoke():
    c, _, _=channel([])
    assert c.click(100,200)=='done'
    c.driver._raw._invoke.assert_called_once_with('Driver.click',args=[100,200])
