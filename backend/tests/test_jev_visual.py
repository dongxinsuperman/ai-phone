"""Enhancement changes drawing only and reuses host screenshot/session ownership."""
import io
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image

from ai_phone.agent.trajectory_cache.jev_click.adapters.base import TreeAdapter
from ai_phone.agent.trajectory_cache.jev_click.adapters.ios import IosAdapter
from ai_phone.agent.trajectory_cache.jev_click.types import Frame, annotate_with_mode, tree_data
from ai_phone.agent.trajectory_cache.jev_click.visual import VisualEnhancer
from ai_phone.agent.trajectory_cache.jev_click.visual_vendor.adapters.ios import IOSAdapter
from ai_phone.agent.trajectory_cache.jev_click.visual_vendor.settings import configure


def image():
    out = io.BytesIO()
    Image.new('RGB', (200, 100), 'white').save(out, 'JPEG')
    return out.getvalue()


TREE = '<hierarchy><node clickable="true" enabled="true" bounds="[0,0][200,100]" text="button"/></hierarchy>'
SOURCE = '<node type="XCUIElementTypeApplication" x="0" y="0" width="100" height="50"><node type="XCUIElementTypeButton" label="button" visible="true" enabled="true" x="0" y="0" width="100" height="50"/></node>'


class Host(TreeAdapter):
    def tree(self, size):
        return TREE


def test_disabled_does_not_load_visual_provider(monkeypatch):
    monkeypatch.setattr(VisualEnhancer, '_provider', Mock(side_effect=AssertionError))
    host = Host(SimpleNamespace(platform='android'))
    frame = host.capture(image(), (200, 100), (200, 100))
    assert len(frame.candidates) == 1
    assert not hasattr(host, '_visual')


def test_capture_failure_uses_original_tree(monkeypatch):
    host = Host(SimpleNamespace(platform='android'))
    host.visual_enabled = True
    monkeypatch.setattr(VisualEnhancer, 'capture_tree', Mock(side_effect=RuntimeError))
    frame = host.capture(image(), (200, 100), (200, 100))
    assert frame.candidates == tree_data(TREE, (200, 100))[0]


def test_provider_cannot_mutate_execution_candidates():
    host = Host(SimpleNamespace(platform='android'))
    visual = VisualEnhancer(host)
    visual.observation = SimpleNamespace(metadata={})
    def enhance(obs, nodes):
        nodes[0]['bounds'] = [0, 0, 10, 10]
        return {'N1': {'bounds': [10, 10, 190, 90], 'radius': 10}}
    visual.provider = SimpleNamespace(enhance_annotation=enhance)
    frame = Frame(image(), (200, 100), tree_data(TREE, (200, 100))[0])
    before = deepcopy(frame.candidates)
    regions, _ = visual.enhance(frame)
    assert regions and frame.candidates == before


def test_region_excluding_original_click_center_is_rejected():
    visual = VisualEnhancer(Host(SimpleNamespace(platform='android')))
    visual.observation = SimpleNamespace(metadata={})
    visual.provider = SimpleNamespace(enhance_annotation=lambda *a: {'N1': {'bounds': [0, 0, 60, 35]}})
    frame = Frame(image(), (200, 100), tree_data(TREE, (200, 100))[0])
    assert visual.enhance(frame)[0] == {}


def test_failed_enhancement_preserves_original_drawing():
    host = Host(SimpleNamespace(platform='android'))
    host.visual_enabled = True
    host._visual = SimpleNamespace(enhance=Mock(side_effect=RuntimeError))
    frame = Frame(image(), (200, 100), tree_data(TREE, (200, 100))[0])
    baseline = annotate_with_mode(frame)
    host.enhance_frame(frame)
    assert frame.visual_metadata['status'] == 'unavailable'
    assert annotate_with_mode(frame) == baseline


def test_renderer_failure_falls_back_without_changing_points(monkeypatch):
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor import core
    frame = Frame(image(), (200, 100), tree_data(TREE, (200, 100))[0])
    baseline = annotate_with_mode(frame)
    before = deepcopy(frame.candidates)
    frame.visual_regions = {'N1': {'bounds': [0, 0, 200, 100]}}
    monkeypatch.setattr(core, 'annotate', Mock(side_effect=ValueError))
    assert annotate_with_mode(frame) == baseline
    assert frame.candidates == before and frame.visual_metadata['render_fallback']


def test_ios_missing_extension_probed_once_without_second_session_or_screenshot():
    configure(visual_enhancement=True)
    requests = []
    def respond(req):
        requests.append(req.url.path)
        if req.url.path.endswith('nativeGeometryProbe'):
            return httpx.Response(404)
        assert req.url.path.endswith('/source')
        return httpx.Response(200, json={'value': SOURCE})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = IOSAdapter('http://localhost:8100', 'borrowed', client=client)
    host = IosAdapter(SimpleNamespace(platform='ios'))
    visual = VisualEnhancer(host)
    visual.provider = provider
    try:
        first = visual.capture_tree(image(), (200, 100))
        second = visual.capture_tree(image(), (200, 100))
        assert first == second == host.convert(SOURCE, (200, 100))
        assert requests.count('/session/borrowed/wda/nativeGeometryProbe') == 1
        assert requests.count('/session/borrowed/source') == 2
        visual.close()
        assert not client.is_closed
    finally:
        client.close()


def test_ios_native_snapshot_replaces_source_and_keeps_candidate_geometry():
    configure(visual_enhancement=True)
    raw = {'version': 1, 'tree': {'uid': 'app', 'type': 'XCUIElementTypeApplication',
        'frame': '{{0,0},{100,50}}', 'enabled': True, 'visible': True, 'children': [
            {'uid': 'button', 'type': 'XCUIElementTypeButton', 'frame': '{{0,0},{100,50}}',
             'visibleFrame': '{{0,0},{80,50}}', 'label': 'button', 'enabled': True, 'visible': True}]}}
    def respond(req):
        assert req.url.path.endswith('nativeGeometryProbe')
        return httpx.Response(200, json={'value': raw})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    host = IosAdapter(SimpleNamespace(platform='ios'))
    visual = VisualEnhancer(host)
    visual.provider = IOSAdapter('http://localhost:8100', 'borrowed', client=client)
    tree = visual.capture_tree(image(), (200, 100))
    frame = Frame(image(), (200, 100), tree_data(tree, (200, 100))[0])
    baseline = tree_data(host.convert(SOURCE, (200, 100)), (200, 100))[0]
    assert frame.candidates == baseline
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor.native_visual import ios_regions
    regions, status = ios_regions(visual.observation, frame.candidates)
    assert regions['N1']['bounds'] == [0, 0, 160, 100]
    assert frame.candidates == baseline
    client.close()


def test_harmony_uses_host_converter_and_one_raw_capture():
    raw = {'attributes': {'type': 'Button', 'clickable': 'true', 'enabled': 'true',
                           'visible': 'true', 'bounds': '[0,0][200,100]', 'text': 'button'}}
    from ai_phone.agent.trajectory_cache.jev_click.adapters.harmony import HarmonyAdapter
    host = HarmonyAdapter(SimpleNamespace(platform='harmony', _raw=object()))
    provider = SimpleNamespace(driver=None, _visual_raw=raw, _native_capture={})
    def source():
        provider.raw_tree()
        return '<hierarchy/>'
    provider.source = source
    host.read_raw_tree = Mock(return_value=raw)
    visual = VisualEnhancer(host)
    visual.provider = provider
    assert visual.capture_tree(image(), (200, 100)) == host.convert(raw)
    host.read_raw_tree.assert_called_once()

@pytest.mark.parametrize('platform', ['android', 'ios', 'harmony'])
def test_internal_master_switch_skips_collection_on_all_platforms(monkeypatch, platform):
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor import settings
    monkeypatch.setattr(settings, 'VISUAL_ENHANCEMENT_ENABLED', False)
    host = Host(SimpleNamespace(platform=platform))
    host.visual_enabled = True
    monkeypatch.setattr(VisualEnhancer, '_provider', Mock(side_effect=AssertionError('no provider')))
    frame = host.capture(image(), (200, 100), (200, 100))
    host.enhance_frame(frame)
    assert frame.candidates == tree_data(TREE, (200, 100))[0]
    assert not frame.visual_regions
    assert not hasattr(host, '_visual')
    assert settings.visual_enhancement_enabled() is False


def test_provider_initialization_never_resets_internal_master_switch(monkeypatch):
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor import settings
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor.adapters.android import AndroidAdapter
    monkeypatch.setattr(settings, 'VISUAL_ENHANCEMENT_ENABLED', True)
    device = object()
    host = Host(SimpleNamespace(platform='android', serial='unit'))
    host.device = device
    visual = VisualEnhancer(host)
    provider = visual._provider()
    assert isinstance(provider, AndroidAdapter)
    settings.configure(visual_enhancement=False)
    assert visual.capture_tree(image(), (200, 100)) == TREE
    assert visual._provider() is None
    assert visual.enhance(Frame(image(), (200, 100), [])) == ({}, {'status': 'disabled'})
    assert not settings.visual_enhancement_enabled()
    # An instance created later must also respect the same False value.
    assert VisualEnhancer(host)._provider() is None
    settings.configure(visual_enhancement=True)
    assert visual._provider() is provider
    assert provider.device is device


def test_harmony_bridge_hands_original_tree_to_sdk_enhancement(monkeypatch):
    from ai_phone.agent.trajectory_cache.jev_click.adapters.harmony import HarmonyAdapter
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor.adapters.harmony import HarmonyAdapter as SDKHarmony
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor import visual_channels, settings
    monkeypatch.setattr(settings, 'VISUAL_ENHANCEMENT_ENABLED', True)
    raw = {'attributes': {'type': 'Button', 'clickable': 'true', 'enabled': 'true',
                         'visible': 'true', 'bounds': '[0,0][200,100]', 'text': 'button'}}
    host = HarmonyAdapter(SimpleNamespace(platform='harmony', _raw=object()))
    provider = SDKHarmony('unit', driver=object())
    def source():
        provider._visual_raw = raw
        provider._native_capture = {'status': 'available'}
        return SDKHarmony.convert(raw)
    provider.source = source
    visual = VisualEnhancer(host)
    visual.provider = provider
    tree = visual.capture_tree(image(), (200,100))
    frame = Frame(image(), (200,100), tree_data(tree, (200,100))[0])
    before = deepcopy(frame.candidates)
    def enhance(adapter, obs, nodes, received):
        assert received is raw
        assert nodes == before
        return {'N1': {'bounds': [10,10,190,90], 'radius': 10}}, {
            'native': {'status': 'matched', 'count': 1}, 'dom': {'status': 'not_applicable'}}
    collect = Mock(side_effect=enhance)
    monkeypatch.setattr(visual_channels, 'enhance_harmony', collect)
    regions, status = visual.enhance(frame)
    collect.assert_called_once()
    assert regions['N1']['bounds'] == [10,10,190,90]
    assert status['native']['count'] == 1
    assert provider._visual_raw is None
    assert '_visual_raw' not in visual.observation.metadata
    assert frame.candidates == before


def test_harmony_bridge_preserves_sdk_observation_handoff(monkeypatch):
    from ai_phone.agent.trajectory_cache.jev_click.adapters.harmony import HarmonyAdapter as Host
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor.adapters.harmony import HarmonyAdapter as SDK
    from ai_phone.agent.trajectory_cache.jev_click.visual_vendor import settings
    monkeypatch.setattr(settings, 'VISUAL_ENHANCEMENT_ENABLED', True)
    raw = {'attributes': {'type':'Button','clickable':'true','enabled':'true',
                         'visible':'true','bounds':'[0,0][200,100]'}}
    provider = SDK('unit', driver=object())
    provider.viewport = lambda:(200,100)
    def source():
        provider._visual_raw = raw
        provider._native_capture = {'status':'available','capture_ms':12}
        return SDK.convert(raw)
    provider.source = source
    sdk_observation = provider.observe(lambda name, fn:fn(), frame=image())
    host = Host(SimpleNamespace(platform='harmony', _raw=object()))
    bridge = VisualEnhancer(host)
    bridge.provider = provider
    bridge.capture_tree(image(), (200,100))
    assert bridge.observation.metadata == sdk_observation.metadata
    assert bridge.observation.tree == sdk_observation.tree
    assert provider._visual_raw is None
