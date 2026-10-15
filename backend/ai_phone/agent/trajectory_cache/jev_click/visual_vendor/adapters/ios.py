import base64
import io
import logging
import time
import xml.etree.ElementTree as ET
import httpx
from PIL import Image
from .base import Observation

class IOSAdapter:
    """Connect to an existing WDA. Real devices and simulators use this adapter.

    All candidates use screenshot pixels; WDA tap receives logical points.
    No WDA build, signing, app installation, or device lifecycle management.
    """
    platform='ios'
    recheck_before_click=False
    model_note=(
        '本端XCTest名称可能是内部别名。以当前截图控件及编号区域为准，节点名称仅作辅助；'
        '不得因名称不一致排除图中明确存在的目标。候选包含文字定位区域，不保证原生可点击；'
        '目标被弹窗或遮罩覆盖时，即使底层文字仍可见也不能选择，应返回NOT_FOUND；'
        '只有目标当前明确可操作才选择其编号。'
    )
    TYPES={'XCUIElementTypeButton','XCUIElementTypeCell','XCUIElementTypeSwitch','XCUIElementTypeTextField','XCUIElementTypeSecureTextField','XCUIElementTypeLink','XCUIElementTypeIcon','XCUIElementTypeImage'}
    def __init__(self,wda_url,session_id=None,client=None,*,udid=None):
        self.udid=udid
        self._visual_channel=None
        self.url=wda_url.rstrip('/')
        if not self.url.startswith(('http://','https://')):raise ValueError('WDA URL must use http or https.')
        self.identity='ios:'+self.url
        self.client=client or httpx.Client(base_url=self.url,timeout=20,trust_env=False)
        self.owns_client=client is None
        self.session_id=session_id
        self.owns_session=session_id is None
        self.tap_suffix='/wda/tap'
        self.click_restore=None

    def _request(self,method,path,body=None):
        r=self.client.request(method,self.url+path,json=body)
        r.raise_for_status();data=r.json()
        if data.get('status',0)!=0:raise RuntimeError('WDA reported an operation error.')
        return data

    def _session(self):
        if not self.session_id:
            data=self._request('POST','/session',{'capabilities':{'alwaysMatch':{}}})
            self.session_id=data.get('sessionId') or (data.get('value') or {}).get('sessionId')
            if not self.session_id:raise RuntimeError('WDA did not return a session ID.')
        return '/session/'+self.session_id

    def source(self):
        from ..settings import visual_enhancement_enabled
        self._native_snapshot=None
        self._native_capture={'status':'unavailable','reason':'WDA extension unavailable'}
        if visual_enhancement_enabled() and getattr(self,'_native_supported',None) is not False:
            started=time.perf_counter()
            try:
                from ..native_visual import ios_source
                snapshot=self._request('GET',self._session()+'/wda/nativeGeometryProbe')['value']
                source=ios_source(snapshot)
                self._native_snapshot=snapshot
                self._native_supported=True
                self._native_capture={'status':'available','capture_ms':round((time.perf_counter()-started)*1000,2)}
                return source
            except Exception as error:
                if isinstance(error,httpx.HTTPStatusError) and error.response.status_code in (404,405):
                    self._native_supported=False
                self._native_capture={'status':'unavailable','reason':type(error).__name__}
        return self._request('GET',self._session()+'/source?format=xml')['value']

    def viewport(self):
        root=self._session()
        size=self._request('GET',root+'/window/size')['value']
        orientation=self._request('GET',root+'/orientation')['value']
        return float(size['width']),float(size['height']),str(orientation)

    def screenshot(self):
        data=self._request('GET',self._session()+'/screenshot')['value']
        raw=base64.b64decode(data)
        with Image.open(io.BytesIO(raw)) as image:
            output=io.BytesIO()
            image.convert('RGB').save(output,format='JPEG',quality=95,optimize=True)
            return output.getvalue()

    @classmethod
    def convert(cls,source,image,viewport):
        root=ET.fromstring(source);width,height=Image.open(io.BytesIO(image)).size
        pw,ph=viewport[:2]
        if pw<=0 or ph<=0:raise RuntimeError('Invalid WDA window size.')
        sx,sy=width/pw,height/ph
        # Density should be isotropic; reject mismatched frame orientation instead of guessing.
        if abs(sx-sy)/max(sx,sy)>.03:raise RuntimeError('iOS window and screenshot aspect ratios disagree.')
        seen_regions=set()
        def convert_node(n):
            x,y,w,h=[float(n.get(k,'0')) for k in ('x','y','width','height')]
            bounds=[round(x*sx),round(y*sy),round((x+w)*sx),round((y+h)*sy)]
            label=n.get('label') or n.get('value') or n.get('name') or ''
            kind=n.get('type','')
            candidate=kind in cls.TYPES or (kind in {'XCUIElementTypeOther','XCUIElementTypeStaticText'} and bool(label.strip()))
            visible=n.get('visible')=='true'
            inside=0<=bounds[0]<bounds[2]<=width and 0<=bounds[1]<bounds[3]<=height
            active=visible and n.get('enabled')=='true' and candidate and inside
            signature=(tuple(bounds),label)
            if active:
                if signature in seen_regions:
                    active=False
                else:
                    seen_regions.add(signature)
            e=ET.Element('node',{'text':label,'resource-id':n.get('name',''),'content-desc':'','class':kind,
                'bounds':f'[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]',
                'clickable':str(active).lower(),'enabled':n.get('enabled','false'),'visible-to-user':str(visible).lower()})
            if n.get('uid'):e.set('native-uid',n.get('uid'))
            for child in n:e.append(convert_node(child))
            return e
        out=ET.Element('hierarchy');out.append(convert_node(root))
        return ET.tostring(out,encoding='unicode'),{'sx':sx,'sy':sy,'pixel_size':(width,height)}

    def observe(self,stage,frame=None):
        frame=frame if frame is not None else stage('screenshot',self.screenshot)
        source=stage('tree',self.source)
        # Same XCTest Application geometry as V3, with no extra device query.
        app=next((n for n in ET.fromstring(source).iter()
                  if n.get('type')=='XCUIElementTypeApplication'),None)
        if app is None:
            raise RuntimeError('XCTest Application frame missing')
        pw,ph=float(app.get('width','0')),float(app.get('height','0'))
        if pw<=0 or ph<=0 or float(app.get('x','0'))!=0 or float(app.get('y','0'))!=0:
            raise RuntimeError('Unsupported XCTest Application viewport')
        pixel_size=Image.open(io.BytesIO(frame)).size
        if abs(pixel_size[0]/pw-pixel_size[1]/ph)>.02:
            raise RuntimeError('XCTest/screenshot aspect mismatch')
        viewport=(pw,ph)
        tree,meta=stage('adapt',lambda:self.convert(source,frame,viewport))
        if getattr(self,'_native_snapshot',None) is not None:
            meta['_native_snapshot']=self._native_snapshot
        if getattr(self,'_native_capture',None):meta['_native_capture']=self._native_capture
        self._native_snapshot=None
        return Observation(tree,frame,viewport,meta)

    def observe_with_frame(self,stage,frame):
        return self.observe(stage,frame)

    def click(self,x,y,observation):
        root=self._session()
        path=root+'/appium/settings'
        current=self._request('GET',path)['value']
        keys=['waitForIdleTimeout','animationCoolOffTimeout']
        original={key:current[key] for key in keys}
        self.click_restore=original
        try:
            self._request('POST',path,{'settings':dict(waitForIdleTimeout=0,animationCoolOffTimeout=0)})
            body={'x':x/observation.metadata['sx'],'y':y/observation.metadata['sy']}
            try:self._request('POST',root+self.tap_suffix,body)
            except httpx.HTTPStatusError as error:
                if error.response.status_code!=404 or self.tap_suffix!='/wda/tap':raise
                self.tap_suffix='/wda/tap/0'
                self._request('POST',root+self.tap_suffix,body)
        finally:
            try:
                self._request('POST',path,{'settings':original})
                self.click_restore=None
            except Exception:
                # V3 preserves successful tap outcome; retry restoration at close.
                logging.getLogger(__name__).warning('WDA click settings restoration pending')

    def enhance_annotation(self, observation, nodes):
        from ..settings import visual_enhancement_enabled
        if not visual_enhancement_enabled():return {}
        from ..visual_channels import enhance_ios
        regions,status=enhance_ios(self,observation,nodes)
        observation.metadata['visual_enhancement']=status
        return regions

    def close(self):
        if self._visual_channel:
            try:self._visual_channel.close()
            finally:self._visual_channel=None
        try:
            if self.click_restore and self.session_id:
                self._request('POST',self._session()+'/appium/settings',{'settings':self.click_restore})
                self.click_restore=None
            if self.owns_session and self.session_id:self._request('DELETE','/session/'+self.session_id)
        finally:
            self.session_id=None
            if self.owns_client:self.client.close()
