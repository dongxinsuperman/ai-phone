"""Optional native geometry providers, bound by system identity, never business labels."""
import re,json,xml.etree.ElementTree as ET
from .adapters.harmony import HarmonyAdapter


def attrs_walk(root):
 yield root
 for ch in root.get('children',[]):yield from attrs_walk(ch)
def bind_harmony(raw,size,bundle=None):
 from .core import candidates_from_tree
 tree=HarmonyAdapter.convert(raw);nodes=candidates_from_tree(tree,*size);xmlnodes=list(ET.fromstring(tree).iter('node'));rawnodes=list(attrs_walk(raw));assert len(rawnodes)==len(xmlnodes)
 bindings={};index=0;web_roots=set();owners={}
 def visit(n,web=False,owner=None):
  a=n.get('attributes',{});owner=a.get('bundleName')or owner;owners[id(n)]=owner;web=web or a.get('type','').lower()=='web' or 'rootwebarea' in a.get('type','').lower()
  if web:web_roots.add(id(n))
  for ch in n.get('children',[]):visit(ch,web,owner)
 visit(raw)
 for r,x in zip(rawnodes,xmlnodes):
  a=x.attrib;b=list(map(int,re.findall(r'-?\d+',a.get('bounds',''))))
  if a.get('clickable')!='true' or a.get('enabled')!='true' or a.get('visible-to-user')=='false' or len(b)!=4:continue
  b=[max(0,b[0]),max(0,b[1]),min(size[0],b[2]),min(size[1],b[3])]
  if b[2]<=b[0]or b[3]<=b[1]:continue
  n=nodes[index];index+=1;assert n['bounds']==b
  if id(r)not in web_roots and (bundle is None or owners[id(r)]==bundle):bindings[n['id']]=r
 assert index==len(nodes)
 return tree,nodes,bindings

def render_index(text,pid):
 marker='| pid['+str(pid)+']'
 if marker not in text:return {}
 text=text.split(marker,1)[1].split('| pid[',1)[0];out={}
 for line in text.splitlines():
  m=re.search(r'(?:CanvasNode|CanvasDrawingNode)\[(\d+)\].*?frameNodeId\[(\d+)\]',line)
  if m:out.setdefault(m[2],[]).append({'render_id':m[1],'line':line})
 return out

def paint_supported(line):
 """Reject geometry whose animation or transform has not been mapped."""
 if re.search(r'animations\[[^]\s]',line) or 'Matrix[' in line or 'Transform[' in line:return False
 for match in re.finditer(r'Scale\[x:([-\d.]+) y:([-\d.]+)\]',line):
  if any(abs(float(v)-1)>.001 for v in match.groups()):return False
 for match in re.finditer(r'Rotation[^[]*\[([^]]+)\]',line):
  if any(abs(float(v))>.001 for v in re.findall(r'[-+]?(?:\d*\.\d+|\d+)',match[1])):return False
 return not any(float(v)<=0 for v in re.findall(r'Alpha\[([-\d.]+)\]',line))

def reliable_bindings(raw,bindings,rs):
 parents={}
 def walk(n,parent=None):
  parents[id(n)]=parent
  for c in n.get('children',[]):walk(c,n)
 walk(raw);result={}
 for key,node in bindings.items():
  ancestor=node;reliable=True
  while ancestor is not None:
   a=ancestor.get('attributes',{})
   try:
    opacity=a.get('opacity')
    if opacity not in (None,'') and float(opacity)<=0:reliable=False;break
   except (TypeError,ValueError):reliable=False;break
   hits=rs.get(str(a.get('uniqueId','')),[])
   if len(hits)>1:reliable=False;break
   for hit in hits:
    if not paint_supported(hit['line']):reliable=False;break
   if not reliable:break
   ancestor=parents.get(id(ancestor))
  if reliable:result[key]=node
 return result

def harmonic_regions(bindings,nodes,rs,size):
 out={};evidence={};byid={n['id']:n for n in nodes}
 for key,raw in bindings.items():
  a=raw.get('attributes',{});uid=str(a.get('uniqueId',''));hits=rs.get(uid,[])
  if not uid.isdigit()or len(hits)!=1:continue
  c=a.get('backgroundColor','');opaque=bool(re.fullmatch(r'#[0-9A-Fa-f]{8}',c))and int(c[1:3],16)>0
  if not opaque:continue
  line=hits[0]['line'];bg=re.search(r'BackgroundColor\[(?:RGBA|ARGB)-0x([0-9A-Fa-f]{8})',line)
  if not bg:continue
  alpha=int(bg[1][-2:],16) if 'BackgroundColor[RGBA' in line else int(bg[1][:2],16)
  if not alpha:continue
  b=byid[key]['bounds'];area=(b[2]-b[0])*(b[3]-b[1])
  if area>size[0]*size[1]*.35:continue
  corner=re.search(r'CornerRadius\[x:([\d.]+) y:([\d.]+) z:([\d.]+) w:([\d.]+)\]',line);radius=0
  if corner:
   radii=list(map(float,corner.groups()))
   if max(radii)-min(radii)>.01:continue
   radius=min(min(radii),(b[2]-b[0])/2,(b[3]-b[1])/2)
  original=list(map(int,re.findall(r'-?\d+',a.get('origBounds')or a.get('bounds',''))))
  if original!=b:radius=0
  out[key]={'bounds':b,'radius':radius};evidence[key]={'unique_id':uid,'render_id':hits[0]['render_id'],'type':a.get('type'),'background':c,'radius':radius,'source':'exact uniqueId -> frameNodeId + actual background/corner properties','click_bounds':b}
 return out,evidence

def ancestor_fill_regions(raw,bindings,nodes,rs,size):
 """Clip an exact ancestor paint surface to the existing action region."""
 parents={}
 def walk(n,parent=None):
  parents[id(n)]=parent
  for c in n.get('children',[]):walk(c,n)
 walk(raw);out={};proof={}
 for n in nodes:
  r=bindings.get(n['id'])
  if r is None:continue
  b=n['bounds'];ancestor=r
  while ancestor is not None:
   a=ancestor.get('attributes',{});uid=str(a.get('uniqueId',''));hits=rs.get(uid,[])
   if len(hits)==1:
    line=hits[0]['line'];color=re.search(r'BackgroundColor\[(?:RGBA|ARGB)-0x([0-9A-Fa-f]{8})',line);ab=list(map(int,re.findall(r'-?\d+',a.get('origBounds')or a.get('bounds',''))))
    if color and len(ab)==4 and ab[0]<=b[0] and ab[1]<=b[1] and ab[2]>=b[2]-1 and ab[3]>=b[3]-1:
     alpha=int(color[1][-2:],16)if'BackgroundColor[RGBA'in line else int(color[1][:2],16)
     if alpha==255:
      corner=re.search(r'CornerRadius\[x:([\d.]+) y:([\d.]+) z:([\d.]+) w:([\d.]+)\]',line);radii=list(map(float,corner.groups()))if corner else[0]*4
      if max(radii)-min(radii)>.01:break
      same=[abs(b[0]-ab[0])<=1 and abs(b[1]-ab[1])<=1,abs(b[2]-ab[2])<=1 and abs(b[1]-ab[1])<=1,abs(b[2]-ab[2])<=1 and abs(b[3]-ab[3])<=1,abs(b[0]-ab[0])<=1 and abs(b[3]-ab[3])<=1];corners=[min(x,(b[2]-b[0])/2,(b[3]-b[1])/2)if ok else 0 for x,ok in zip(radii,same)]
      if any(corners):
       out[n['id']]={'bounds':b,'radius':0,'corners':corners};proof[n['id']]={'ancestor_unique_id':uid,'render_id':hits[0]['render_id'],'source':'exact native ancestor background intersection','ancestor_bounds':ab,'bounds':b,'corners':corners}
      break
   ancestor=parents.get(id(ancestor))
 return out,proof


def numbers(text):return list(map(float,re.findall(r'[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?',text or '')))
def clip(bounds,frame,visible,sx,sy):
 f=numbers(frame);v=numbers(visible)
 if len(f)!=4 or len(v)!=4 or v[2]*sx<40 or v[3]*sy<30:return None
 fe=[f[0],f[1],f[0]+f[2],f[1]+f[3]];ve=[v[0],v[1],v[0]+v[2],v[1]+v[3]]
 # CGRectIntegral must not turn an unchanged fractional edge into a false correction.
 q=[bounds[i] if abs(fe[i]-ve[i])<.05 else round(ve[i]*[sx,sy,sx,sy][i])for i in range(4)]
 q=[max(bounds[0],q[0]),max(bounds[1],q[1]),min(bounds[2],q[2]),min(bounds[3],q[3])]
 cx=(bounds[0]+bounds[2])/2;cy=(bounds[1]+bounds[3])/2
 if q==bounds or not(q[0]<=cx<=q[2]and q[1]<=cy<=q[3])or q[2]-q[0]<40 or q[3]-q[1]<30:return None
 return q


def ios_source(snapshot):
    """Reconstruct WDA's integral XML geometry from one enriched snapshot."""
    import math
    if snapshot.get('version',1)!=1:raise ValueError('Unsupported native geometry schema')
    raw = snapshot['tree']
    app_frame=numbers(raw.get('frame'))
    if (raw.get('type')!='XCUIElementTypeApplication' or len(app_frame)!=4
            or app_frame[0]!=0 or app_frame[1]!=0 or min(app_frame[2:])<=0):
        raise ValueError('Invalid native application viewport')
    seen = set()
    def visit(n, depth=0):
        if depth > 100:
            raise ValueError('Native snapshot depth exceeded')
        if n.get('truncated'):raise ValueError('Incomplete native snapshot')
        uid = n['uid']
        if not uid or uid in seen:
            raise ValueError('Native snapshot identity is not unique')
        seen.add(uid)
        f = numbers(n['frame'])
        if len(f) != 4 or not all(math.isfinite(v) for v in f):
            raise ValueError('Invalid native snapshot frame')
        x,y,w,h = f
        e = ET.Element('node', {'type': n['type'], 'x': str(math.floor(x)),
            'y': str(math.floor(y)), 'width': str(math.ceil(x+w)-math.floor(x)),
            'height': str(math.ceil(y+h)-math.floor(y)), 'label': n.get('label',''),
            'name': n.get('name',''), 'value': n.get('value',''), 'uid':uid,
            'visible':str(n.get('visible',False)).lower(),
            'enabled':str(n.get('enabled',False)).lower()})
        for child in n.get('children',[]):
            e.append(visit(child,depth+1))
        return e
    return ET.tostring(visit(raw),encoding='unicode')


def ios_regions(observation, nodes):
    import time
    start = time.perf_counter()
    meta = observation.metadata
    raw = meta.pop('_native_snapshot', None)
    status = dict(meta.pop('_native_capture', {'status':'unavailable','reason':'WDA extension unavailable'}))
    status['count'] = 0
    if raw is None:
        return {}, status
    try:
        native = {}
        def walk(n, web=False):
            web = web or n.get('web',False) or n.get('type')=='XCUIElementTypeWebView'
            if not web:
                native[n['uid']] = n
            for c in n.get('children',[]):
                walk(c,web)
        walk(raw['tree'])
        candidates = []
        width,height = meta['pixel_size']
        for x in ET.fromstring(observation.tree).iter('node'):
            if x.get('clickable')=='true' and x.get('enabled')=='true' and x.get('visible-to-user')!='false':
                b=list(map(int,re.findall(r'-?\d+',x.get('bounds',''))))
                if len(b)!=4:continue
                b=[max(0,b[0]),max(0,b[1]),min(width,b[2]),min(height,b[3])]
                if b[2]>b[0] and b[3]>b[1]:candidates.append((b,x.get('native-uid')))
        if len(candidates)!=len(nodes):raise ValueError('Native candidate contract differs')
        regions = {}
        for n,(bounds,uid) in zip(nodes,candidates):
            if n['bounds']!=bounds:raise ValueError('Native candidate order differs')
            if uid not in native:continue
            item=native[uid]
            q=clip(n['bounds'],item.get('frame'),item.get('visibleFrame'),meta['sx'],meta['sy'])
            if q is not None:
                regions[n['id']]={'bounds':q,'radius':0}
        status.update(status='matched' if regions else 'no_reliable_geometry',count=len(regions))
    except Exception as error:
        regions={}
        status.update(status='unavailable',reason=type(error).__name__)
    status['elapsed_ms']=round((time.perf_counter()-start)*1000,2)
    return regions,status


def harmony_capture(adapter):
    """Replace captureLayout, not append a second hierarchy on success."""
    import subprocess,tempfile,uuid,time
    from pathlib import Path
    start=time.perf_counter()
    remote='/data/local/tmp/jev-native-'+uuid.uuid4().hex+'.json'
    def command(*args):
        return subprocess.check_output(['hdc','-t',adapter.serial,*args],text=True,
            timeout=2,stderr=subprocess.DEVNULL)
    try:
        with tempfile.TemporaryDirectory(prefix='jev-native-') as directory:
            local=Path(directory)/'tree.json'
            command('shell','uitest dumpLayout -p '+remote+' -e uniqueId')
            command('file','recv',remote,str(local))
            raw=json.loads(local.read_text())
        if not any(str(n.get('attributes',{}).get('uniqueId','')).isdigit() for n in attrs_walk(raw)):
            raise ValueError('UiTest uniqueId not supplied')
    finally:
        # Only remove this call's temporary file, including after failed transfers.
        try:command('shell','rm -f '+remote)
        except Exception:pass
    return raw,{'status':'available','capture_ms':round((time.perf_counter()-start)*1000,2)}


def harmony_regions(adapter, observation, nodes, raw):
    import subprocess,time
    start=time.perf_counter()
    status=dict(observation.metadata.pop('_native_capture',{}))
    try:
        _,collected,bindings=bind_harmony(raw,observation.viewport)
        contract=lambda ns:[(n['id'],n['bounds'])for n in ns]
        if contract(collected)!=contract(nodes):
            raise ValueError('Native candidate contract differs')
        if not bindings or not any(str(n.get('attributes',{}).get('uniqueId','')).isdigit() for n in bindings.values()):
            status.update(status='unavailable' if status.get('reason') else 'not_applicable',count=0,elapsed_ms=0)
            return {},status
        bundle=adapter.driver.current_app()[0]
        if not re.fullmatch(r'[A-Za-z0-9_.]+',bundle):
            raise ValueError('Invalid application identity')
        def command(*args):
            return subprocess.check_output(['hdc','-t',adapter.serial,*args],text=True,
                timeout=2,stderr=subprocess.DEVNULL)
        # FrameNode IDs are process-local; exclude keyboard/system/other-app windows.
        _,_,bindings=bind_harmony(raw,observation.viewport,bundle=bundle)
        if not bindings:
            return {},{**status,'status':'no_reliable_geometry','count':0,'elapsed_ms':round((time.perf_counter()-start)*1000,2)}
        pid=command('shell','pidof',bundle).strip().split()
        if len(pid)!=1 or not pid[0].isdigit():
            raise ValueError('Ambiguous application process')
        rs=render_index(command('shell','hidumper -s RenderService -a "client"'),pid[0])
        bindings=reliable_bindings(raw,bindings,rs)
        regions,_=harmonic_regions(bindings,nodes,rs,observation.viewport)
        extra,_=ancestor_fill_regions(raw,bindings,nodes,rs,observation.viewport)
        regions.update(extra)
        status.update(status='matched' if regions else 'no_reliable_geometry',count=len(regions))
    except Exception as error:
        regions={}
        status.update(status='unavailable',reason=type(error).__name__,count=0)
    status['elapsed_ms']=round((time.perf_counter()-start)*1000,2)
    return regions,status
