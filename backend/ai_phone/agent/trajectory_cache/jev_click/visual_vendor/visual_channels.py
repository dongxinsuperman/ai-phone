"""Optional H5 drawing providers. Never modify candidate or click geometry."""
import asyncio
import contextlib
import io
import json
import re
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from PIL import Image
from .android_visual import DOM_EXPRESSION, dom_regions


def safe_regions(regions, nodes, viewport):
    result = {}
    for node in nodes:
        region = regions.get(node['id'])
        if not region:
            continue
        x, y, right, bottom = region['bounds']
        cx = (node['bounds'][0] + node['bounds'][2]) // 2
        cy = (node['bounds'][1] + node['bounds'][3]) // 2
        if (0 <= x <= cx <= right <= viewport[0] and
                0 <= y <= cy <= bottom <= viewport[1] and
                right - x >= 50 and bottom - y >= 30):
            result[node['id']] = region
    return result


def expression(viewport=None):
    text = DOM_EXPRESSION.replace('if(!shared.length)return null;', '')
    text = text.replace('NATIVE_IDS', '[]').replace('EXPECTED_VIEWPORT', json.dumps(list(viewport or ())))
    if viewport is None:
        text = text.replace("if(meta.visible!=='visible'||Math.abs(innerWidth*meta.dpr-expected[0])>2||Math.abs(innerHeight*meta.dpr-expected[1])>2)return meta;",
                            "if(meta.visible!=='visible')return meta;")
    return text


def select_ios_context(contexts, tree, viewport):
    rects = {tuple(map(int, re.findall(r'-?\d+', n.get('bounds', ''))))
             for n in ET.fromstring(tree).iter('node')
             if n.get('class') == 'XCUIElementTypeWebView' and n.get('visible-to-user') == 'true'}
    if rects != {(0, 0, *viewport)}:
        return None
    eligible = [c for c in contexts if c.get('items') and c.get('visible') == 'visible'
                and all(abs(c['viewport'][key] * c['dpr'] - size) <= 2
                        for key, size in zip(('width', 'height'), viewport))]
    return eligible[0] if len(eligible) == 1 else None


def web_members(tree, nodes, web_class):
    bounds = set()
    def visit(n, inside=False):
        inside = inside or n.get('class') == web_class
        if inside:
            bounds.add(n.get('bounds'))
        for child in n:
            visit(child, inside)
    visit(ET.fromstring(tree))
    return [n for n in nodes if '[%d,%d][%d,%d]' % tuple(n['bounds']) in bounds]


class IOSVisualChannel:
    """Own one protocol connection, releasing each page socket after reading."""
    def __init__(self, udid=None):
        self.udid = udid
        self.service = self.lockdown = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    async def _open(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices
        from pymobiledevice3.services.webinspector import WebinspectorService, ApplicationPage
        if not self.udid:
            devices = [d for d in await list_devices() if d.connection_type == 'USB']
            if len(devices) != 1:
                raise RuntimeError('iOS enhancement requires a unique USB device or explicit udid')
            self.udid = devices[0].serial
        class ResponseDrivenInspector(WebinspectorService):
            def __init__(self, lockdown):
                super().__init__(lockdown)
                self.bundle = None
                self.ready = asyncio.Event()
            async def _handle_application_sent_listing(self, arg):
                await super()._handle_application_sent_listing(arg)
                app = self.connected_application.get(arg['WIRApplicationIdentifierKey'])
                if app and app.bundle == self.bundle:
                    self.ready.set()
            async def pages(self, bundle):
                self.bundle = bundle
                self.ready.clear()
                await self._get_connected_applications()
                await asyncio.wait_for(self.ready.wait(), 2)
                return [ApplicationPage(app, page) for app in self.connected_application.values()
                        if app.bundle == bundle for page in self.application_pages.get(app.id_, {}).values()]
        self.lockdown = await create_using_usbmux(serial=self.udid)
        self.service = ResponseDrivenInspector(self.lockdown)
        await self.service.connect()

    async def _collect(self, bundle, viewport):
        if self.service is None:
            await self._open()
        contexts = []
        for ap in await self.service.pages(bundle):
            session = None
            try:
                session = await asyncio.wait_for(self.service.inspector_session(ap.application, ap.page), 2)
                value = await asyncio.wait_for(session.runtime_evaluate('JSON.stringify(' + expression(viewport) + ')'), 2)
                contexts.append(json.loads(value))
            finally:
                if session:
                    session._receive_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await session._receive_task
                    await self.service.teardown_inspector_socket(session.protocol.id_, session.protocol.app.id_, session.protocol.page.id_)
        return contexts

    def collect(self, bundle, viewport):
        future = asyncio.run_coroutine_threadsafe(self._collect(bundle, viewport), self.loop)
        try:
            return future.result(timeout=15)
        except Exception:
            future.cancel()
            raise

    async def _close(self):
        try:
            if self.service:
                await self.service.close()
        finally:
            if self.lockdown:
                await self.lockdown.close()
            self.service = self.lockdown = None

    def close(self):
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(timeout=3)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=1)
            if not self.thread.is_alive():
                self.loop.close()


def _enhance_ios_dom(adapter, observation, nodes):
    if not any(n.get('class') == 'XCUIElementTypeWebView' and n.get('visible-to-user') == 'true'
               for n in ET.fromstring(observation.tree).iter('node')):
        return {}, {'dom': {'status': 'not_applicable', 'count': 0, 'elapsed_ms': 0},
                    'native': {'status': 'not_supported', 'count': 0}}
    start = time.perf_counter()
    viewport = Image.open(io.BytesIO(observation.image)).size
    try:
        if adapter._visual_channel is None:
            from urllib.parse import urlsplit
            if not adapter.udid and urlsplit(adapter.url).hostname not in ('localhost', '127.0.0.1', '::1'):
                raise RuntimeError('Remote WDA enhancement requires explicit USB udid')
            adapter._visual_channel = IOSVisualChannel(adapter.udid)
        bundle = adapter._request('GET', adapter._session() + '/wda/activeAppInfo')['value']['bundleId']
        contexts = adapter._visual_channel.collect(bundle, viewport)
        ctx = select_ios_context(contexts, observation.tree, viewport)
        members = web_members(observation.tree, nodes, 'XCUIElementTypeWebView')
        regions = safe_regions(dom_regions(ctx, members) if ctx else {}, nodes, viewport)
        status = {'status': 'matched' if regions else 'unavailable', 'count': len(regions)}
    except Exception as error:
        regions = {}
        status = {'status': 'unavailable', 'reason': type(error).__name__, 'count': 0}
        if adapter._visual_channel:
            with contextlib.suppress(Exception):
                adapter._visual_channel.close()
            adapter._visual_channel = None
    status['elapsed_ms'] = round((time.perf_counter() - start) * 1000, 2)
    return regions, {'dom': status, 'native': {'status': 'not_supported', 'count': 0}}


def _enhance_harmony_dom(adapter, observation, nodes, raw):
    def walk(n):
        yield n.get('attributes', {})
        for child in n.get('children', []):
            yield from walk(child)
    roots = [n for n in walk(raw) if n.get('type') == 'rootWebArea']
    if len(roots) != 1:
        return {}, {'dom': {'status': 'not_applicable', 'count': 0, 'elapsed_ms': 0},
                    'native': {'status': 'not_supported', 'count': 0}}
    started = time.perf_counter()
    port = endpoint = None
    owned = False
    def command(*args):
        return subprocess.check_output(['hdc', '-t', adapter.serial, *args], text=True,
                                       timeout=2, stderr=subprocess.DEVNULL)
    try:
        import httpx
        import websocket
        bundle = adapter.driver.current_app()[0]
        pid = command('shell', 'pidof', bundle).strip().split()[0]
        endpoint = 'localabstract:webview_devtools_remote_' + pid
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        output = command('fport', 'tcp:' + str(port), endpoint)
        if '[Fail]' in output:
            raise RuntimeError('HDC forward unavailable')
        owned = True
        with httpx.Client(trust_env=False, timeout=2) as client:
            response = client.get('http://127.0.0.1:%d/json/list' % port)
            response.raise_for_status()
            targets = response.json()
        contexts = []
        for target in targets:
            ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=2,
                                             suppress_origin=True, http_no_proxy=['localhost', '127.0.0.1'])
            try:
                ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                    'params': {'expression': expression(), 'returnByValue': True}}))
                deadline = time.monotonic() + 2
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('ArkWeb response deadline')
                    ws.settimeout(remaining)
                    reply = json.loads(ws.recv())
                    if reply.get('id') == 1:
                        contexts.append(reply['result']['result']['value'])
                        break
            finally:
                ws.close()
        visible = [c for c in contexts if c.get('items') and c.get('visible') == 'visible' and c.get('focus')]
        regions = {}
        if len(visible) == 1:
            ctx = visible[0]
            b = list(map(int, re.findall(r'-?\d+', roots[0].get('origBounds') or roots[0].get('bounds', ''))))
            if len(b) != 4 or min(b[2] - b[0], b[3] - b[1]) <= 0:
                raise ValueError('Invalid WebView geometry')
            scale = (b[2] - b[0]) / ctx['viewport']['width']
            sy = (b[3] - b[1]) / ctx['viewport']['height']
            if abs(scale - sy) / max(scale, sy) > .03:
                raise ValueError('Unsupported WebView transform')
            for e in ctx['items']:
                x, y, r, bottom = e['rect']
                e['rect'] = [b[0] + x * scale, b[1] + y * scale, b[0] + r * scale, b[1] + bottom * scale]
                try:
                    e['radius'] = str(float(e['radius'].split()[0].rstrip('px')) * scale) + 'px'
                except ValueError:
                    e['radius'] = '0px'
            ctx['dpr'] = 1
            members = web_members(observation.tree, nodes, 'rootWebArea')
            regions = safe_regions(dom_regions(ctx, members), nodes, observation.viewport)
        status = {'status': 'matched' if regions else 'unavailable', 'count': len(regions)}
    except Exception as error:
        regions = {}
        status = {'status': 'unavailable', 'reason': type(error).__name__, 'count': 0}
    finally:
        if owned:
            with contextlib.suppress(Exception):
                command('fport', 'rm', 'tcp:' + str(port), endpoint)
    status['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return regions, {'dom': status, 'native': {'status': 'not_supported', 'count': 0}}


def enhance_ios(adapter, observation, nodes):
    from .native_visual import ios_regions
    native, native_status = ios_regions(observation, nodes)
    dom, status = _enhance_ios_dom(adapter, observation, nodes)
    native = safe_regions(native, nodes, Image.open(io.BytesIO(observation.image)).size)
    native_status['count'] = len(native)
    return {**dom, **native}, {'dom': status['dom'], 'native': native_status}


def enhance_harmony(adapter, observation, nodes, raw):
    from .native_visual import harmony_regions
    native, native_status = harmony_regions(adapter, observation, nodes, raw)
    dom, status = _enhance_harmony_dom(adapter, observation, nodes, raw)
    native = safe_regions(native, nodes, observation.viewport)
    native_status['count'] = len(native)
    return {**dom, **native}, {'dom': status['dom'], 'native': native_status}
