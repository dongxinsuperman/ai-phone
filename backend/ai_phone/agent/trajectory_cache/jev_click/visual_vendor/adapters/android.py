import base64
import io
from PIL import Image
from .base import Observation

class AndroidAdapter:
    platform = 'android'
    model_note = ''
    recheck_before_click = False
    def __init__(self, serial=None, device=None, *, visual_timeout=1.0):
        if visual_timeout <= 0:raise ValueError("visual_timeout must be positive")
        self.visual_timeout=visual_timeout
        if device is None:
            try:import uiautomator2 as u2
            except ImportError as e:raise RuntimeError('Install jev-phone[android].') from e
            device=u2.connect(serial)
        self.device=device
        self.serial=serial or getattr(device,'serial','android-device')
        self.identity='android:'+self.serial

    @property
    def visual_dom(self):
        from ..settings import visual_enhancement_enabled
        return visual_enhancement_enabled()

    visual_native = visual_dom

    def viewport(self):
        # Same live dimensions and exceptional fallback as V3.
        try:
            info = self.device.info
            width, height = info['displayWidth'], info['displayHeight']
            if type(width) is not int or type(height) is not int or min(width, height) <= 0:
                raise ValueError('Invalid UiAutomator display dimensions')
            return width, height
        except Exception:
            return tuple(self.device.window_size())

    def screenshot(self):
        raw=self.device.jsonrpc.takeScreenshot(1,95)
        if not raw:raise RuntimeError('UiAutomator2 未返回截图。')
        return base64.b64decode(raw)

    def observe(self,stage,frame=None):
        viewport=stage('capture',self.viewport)
        tree=stage('tree',lambda:self.device.dump_hierarchy(compressed=False,pretty=False,max_depth=100,root_in_active=True))
        frame=frame if frame is not None else stage('screenshot',self.screenshot)
        if Image.open(io.BytesIO(frame)).size!=viewport:
            raise RuntimeError('安卓层级视口与截图尺寸不一致，未执行点击。')
        return Observation(tree,frame,viewport)

    def observe_with_frame(self,stage,frame):
        # V3 already supplied this exact settled frame; reuse its pixel dimensions.
        viewport=stage('capture',lambda:Image.open(io.BytesIO(frame)).size)
        tree=stage('tree',lambda:self.device.dump_hierarchy(compressed=False,pretty=False,max_depth=100,root_in_active=True))
        return Observation(tree,frame,viewport)

    def click(self,x,y,observation):
        result=self.device.click(int(x),int(y))
        if result is False:
            raise RuntimeError('UiAutomator2 rejected coordinate click')
    def close(self):pass  # Do not stop a shared device-side UiAutomator2 service.

    def enhance_annotation(self, observation, nodes):
        if not self.visual_dom and not self.visual_native:return {}
        from ..android_visual import enhance
        regions,status=enhance(self,observation,nodes)
        # Only drawing geometry is returned; candidate bounds remain untouched.
        safe={}
        for key,region in regions.items():
            n=next((n for n in nodes if n['id']==key),None)
            if not n:continue
            x,y,r,s=region['bounds'];cx=(n['bounds'][0]+n['bounds'][2])//2;cy=(n['bounds'][1]+n['bounds'][3])//2
            if 0<=x<=cx<=r<=observation.viewport[0] and 0<=y<=cy<=s<=observation.viewport[1] and r-x>=50 and s-y>=30:
                safe[key]=region
        observation.metadata['visual_enhancement']=status
        return safe
