"""Optional Android drawing geometry. Never changes candidate/click bounds."""
import copy,json,math,re,socket,subprocess,time,xml.etree.ElementTree as ET
from collections import Counter
import httpx


def native_regions(raw, tree, nodes):
    stack=[];views=[]
    for line in raw.splitlines():
     m=re.match(r'^(\s+)([\w.$]+)\{([0-9a-f]+) ([A-Z.a-z]+) ([A-Z.a-z]+) (-?\d+),(-?\d+)-(-?\d+),(-?\d+)(.*?)\}',line)
     if not m:continue
     indent=len(m[1]);b=list(map(int,m.group(6,7,8,9)))
     while stack and stack[-1]['indent']>=indent:stack.pop()
     parent=stack[-1] if stack else None;ox=parent['bounds'][0] if parent else 0;oy=parent['bounds'][1] if parent else 0
     rid=re.search(r'(?:app|android):id/([\w]+)',m[10]);v={'indent':indent,'class':m[2],'object':m[3],'visible':m[4].startswith('V'),'bounds':[b[0]+ox,b[1]+oy,b[2]+ox,b[3]+oy],'id':rid[1] if rid else '', 'parent':parent['object'] if parent else None}
     views.append(v);stack.append(v)
    fitted=copy.deepcopy(nodes);matches=[]
    import xml.etree.ElementTree as ET
    native=[n.attrib for n in ET.fromstring(tree).iter('node') if n.get('clickable')=='true' and n.get('enabled')=='true']
    for n in fitted:
     refs=[x for x in native if list(map(int,re.findall(r'-?\d+',x['bounds'])))==n['bounds'] and x.get('class')!='android.webkit.WebView']
     # Require a unique physical View with the same class and measured bounds.
     vs=[v for v in views if v['visible'] and v['bounds']==n['bounds'] and any(v['class']==x.get('class') for x in refs)]
     if len(vs)!=1:continue
     v=vs[0];groups=[]
     lookup={x['object']:x for x in views}
     def ancestry(x):
      result=[];seen=set()
      while x and x['object'] not in seen:
       seen.add(x['object']);result.append(x);x=lookup.get(x['parent'])
      return result
     def near(x):
      chain=ancestry(x)
      return any(y['object']==v['object'] for y in chain[1:5]) or any(y['object']==v['parent'] for y in chain[1:4])
     for x in views:
      if not x['visible'] or x['object']==v['object'] or any(not y['visible'] for y in ancestry(x)):continue
      # Android ImageView and explicit image/animation view classes are drawing hints,
      # not application resource IDs. Custom drawing views of unknown type are skipped.
      drawing=('ImageView' in x['class'] or 'Lottie' in x['class'])
      sameparent=x['parent']==v['parent'];child=x['parent']==v['object']
      if not drawing or not near(x):continue
      bb=x['bounds'];ww=bb[2]-bb[0];hh=bb[3]-bb[1]
      if min(ww,hh)<=0:continue
      cb=v['bounds'];cw=cb[2]-cb[0];ch=cb[3]-cb[1]
      ratio=ww*hh/(cw*ch)
      distance=(( (bb[0]+bb[2]-cb[0]-cb[2])/2 )**2+((bb[1]+bb[3]-cb[1]-cb[3])/2)**2)**.5/(cw*cw+ch*ch)**.5
      if .25<=ratio<=2 and distance<=.2:groups.append(x)
     if len(groups)!=1:continue
     x=groups[0];n['_visual_bounds']=x['bounds'];matches.append({'candidate':n['id'],'click_bounds':n['bounds'],'visual_bounds':x['bounds'],'visual_class':x['class'],'same_parent':v['parent'],'method':'unique class + exact bounds physical View; recursive descendants/nearby sibling subtree with complete ancestor visibility; area ratio 0.25..2 and center distance <=0.2 diagonal; no application IDs'})
    return {n['id']:{'bounds':n['_visual_bounds'],'radius':0} for n in fitted if '_visual_bounds' in n}

def dom_regions(ctx,nodes):
    items=ctx["items"];dpr=ctx["dpr"];rows=[];fitted=copy.deepcopy(nodes)
    for n in fitted:
     b=n['bounds'];w=b[2]-b[0];h=b[3]-b[1];area=w*h;cx=(b[0]+b[2])/2;cy=(b[1]+b[3])/2;rank=[]
     for e in items:
      v=[x*dpr for x in e['rect']];vw=v[2]-v[0];vh=v[3]-v[1];va=vw*vh
      painted=e['img'] or e['bg'] not in ['rgba(0, 0, 0, 0)','transparent'] or e['shadow']!='none' or any(float(x.rstrip('px'))>0 for x in e['border'].split() if x.endswith('px'))
      if not painted or not (v[0]<=cx<=v[2] and v[1]<=cy<=v[3]):continue
      inter=max(0,min(v[2],b[2])-max(v[0],b[0]))*max(0,min(v[3],b[3])-max(v[1],b[1]))
      if va/area<.25 or va/area>1.05 or inter/va<.95:continue
      dist=math.hypot((v[0]+v[2])/2-cx,(v[1]+v[3])/2-cy)/max(1,math.hypot(w,h))
      if dist>.12:continue
      score=.65*(inter/(area+va-inter))+.35*(1-dist*4)
      rank.append((score,v,e))
     rank.sort(key=lambda x:-x[0]);unique=[]
     for x in rank:
      if not any(max(abs(a-b) for a,b in zip(x[1],y[1]))<2 for y in unique):unique.append(x)
     reason='没有明确的居中可见主体';chosen=None
     if unique:
      best=unique[0];margin=best[0]-unique[1][0] if len(unique)>1 else 1
      if best[0]>=.65 and margin>=.08:
       chosen=best;n['_visual_bounds']=[round(x) for x in best[1]]
       try:n['_radius']=min(float(best[2]['radius'].split()[0].rstrip('px'))*dpr,(best[1][2]-best[1][0])/2,(best[1][3]-best[1][1])/2)
       except ValueError:n['_radius']=0
       reason='几何推断：唯一优势候选；不是对象身份关联'
      else:reason='候选分数低或存在相近候选，保留原框'
     rows.append({'id':n['id'],'bounds':b,'visual':n.get('_visual_bounds'),'radius':n.get('_radius'),'reason':reason,'rank':[{'score':round(s,3),'bounds':v,'tag':e['tag']} for s,v,e in unique[:3]]})
    return {n['id']:{'bounds':n['_visual_bounds'],'radius':n.get('_radius',0)} for n in fitted if '_visual_bounds' in n}

DOM_EXPRESSION = "(()=>{const shared=NATIVE_IDS.filter(id=>document.getElementById(id)&&document.querySelectorAll('[id='+CSS.escape(id)+']').length===1);if(!shared.length)return null;const meta={shared,viewport:{width:innerWidth,height:innerHeight},dpr:devicePixelRatio,visible:document.visibilityState,focus:document.hasFocus()};const expected=EXPECTED_VIEWPORT;if(meta.visible!=='visible'||Math.abs(innerWidth*meta.dpr-expected[0])>2||Math.abs(innerHeight*meta.dpr-expected[1])>2)return meta;const root=document.documentElement;return {...meta,items:Array.from(root.querySelectorAll('*')).map((e,i)=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {i,tag:e.tagName,rect:[r.left,r.top,r.right,r.bottom],radius:s.borderRadius,bg:s.backgroundColor,border:s.borderWidth,shadow:s.boxShadow,display:s.display,visibility:s.visibility,opacity:s.opacity,img:e.tagName==='IMG',textOnly:e.children.length===0&&!!e.textContent.trim()};}).filter(e=>e.display!=='none'&&e.visibility!=='hidden'&&parseFloat(e.opacity)>0&&e.rect[2]>e.rect[0]&&e.rect[3]>e.rect[1])};})()"

def collect_dom(serial, package, tree, viewport, command, timeout):
    import websocket
    pid=command('shell','pidof',package).strip().split()[0]
    port=command('forward','tcp:0','localabstract:webview_devtools_remote_'+pid).strip()
    contexts=[]
    ids=sorted({n.get('resource-id') for n in ET.fromstring(tree).iter('node') if n.get('resource-id')})
    try:
        with httpx.Client(trust_env=False,timeout=timeout) as client:
            targets=client.get('http://127.0.0.1:'+port+'/json/list').json()
        for target in targets:
            ws=None
            try:
                # The target's URL is never used for matching or emitted in results.
                ws=websocket.create_connection(target['webSocketDebuggerUrl'],timeout=timeout,suppress_origin=True,http_no_proxy=['127.0.0.1','localhost'])
                ws.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':DOM_EXPRESSION.replace('NATIVE_IDS',json.dumps(ids)).replace('EXPECTED_VIEWPORT',json.dumps(list(viewport))),'returnByValue':True}}))
                deadline=time.monotonic()+timeout
                while time.monotonic()<deadline:
                    ws.settimeout(max(.001,deadline-time.monotonic()))
                    r=json.loads(ws.recv())
                    if r.get('id')==1:
                        value=r.get('result',{}).get('result',{}).get('value')
                        if value:contexts.append(value)
                        break
            finally:
                if ws:ws.close()
        counts=Counter(i for c in contexts for i in c['shared'])
        contexts=[c for c in contexts if any(counts[i]==1 for i in c['shared'])]
        if len(contexts)!=1:return None
        ctx=contexts[0]
        if 'items' not in ctx:return None
        if any(abs(ctx['viewport'][k]*ctx['dpr']-size)>2 for k,size in zip(('width','height'),viewport)):return None
        return ctx
    finally:
        command('forward','--remove','tcp:'+port)


def enhance(adapter, observation, nodes):
    regions={};status={}
    enabled=adapter.visual_dom
    def command(*args):
        return subprocess.check_output(['adb','-s',adapter.serial,*args],timeout=adapter.visual_timeout,stderr=subprocess.DEVNULL,text=True)
    package=next((n.get('package') for n in ET.fromstring(observation.tree).iter('node') if n.get('package')),None)
    if not package:return regions,{'status':'unavailable'}
    has_webview=any('WebView' in n.get('class','') for n in ET.fromstring(observation.tree).iter('node'))
    if enabled and not has_webview:status['dom']={'status':'not_applicable','count':0,'elapsed_ms':0}
    if enabled and has_webview:
        started=time.perf_counter()
        try:
            ctx=collect_dom(adapter.serial,package,observation.tree,observation.viewport,command,adapter.visual_timeout)
            found=dom_regions(ctx,nodes) if ctx else {}
            regions.update(found);status['dom']={'status':'matched' if found else 'unavailable','count':len(found)}
        except Exception as e:status['dom']={'status':'unavailable','reason':type(e).__name__,'count':0}
    if enabled and has_webview:status['dom']['elapsed_ms']=round((time.perf_counter()-started)*1000,2)
    if enabled:
        started=time.perf_counter()
        try:
            raw=command('shell','dumpsys','activity',package)
            found=native_regions(raw,observation.tree,nodes)
            regions.update(found);status['native']={'status':'matched' if found else 'unavailable','count':len(found)}
        except Exception as e:status['native']={'status':'unavailable','reason':type(e).__name__,'count':0}
    if enabled:status['native']['elapsed_ms']=round((time.perf_counter()-started)*1000,2)
    return regions,status
