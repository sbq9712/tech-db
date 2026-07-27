#!/usr/bin/env python3
"""Retry unclassified records - robust version with type checks."""
import json, re, subprocess, sys, time
from pathlib import Path

MODEL='glm-5.2'; PROVIDER='zai'
REPO=Path(__file__).resolve().parent.parent
LITE=REPO/'data/processed/all-records-lite.json'
SKILL=Path.home()/'.hermes/skills/research/intelligence-classification/templates'
NEWS_TAGS={'技术突破','产业进展','政策监管','资本运作','行业观察'}
LIT_TAGS={'研究论文','观点评论'}

def norm(v): return (v or '').replace('/','-').replace('（','').replace('）','').replace('(','').replace(')','')

def call_glm(prompt):
    cmd=['hermes','--provider',PROVIDER,'-m',MODEL,'-z',prompt]
    for a in range(3):
        try:
            r=subprocess.run(cmd,text=True,capture_output=True,timeout=180)
            o=(r.stdout or '').strip()
            if r.returncode!=0 or not o: time.sleep(3); continue
            return o
        except: time.sleep(5)
    return ''

def parse(text):
    text=text.strip()
    if text.startswith('```'): text=text.strip('`').replace('json\n','',1).strip()
    m=re.search(r'\[.*\]',text,re.S)
    if m: text=m.group(0)
    try:
        result=json.loads(text)
        if isinstance(result, list): return result
        return []
    except: return []

def save_all(data):
    LITE.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),'utf-8')
    CH=8000; chunks=[data[i:i+CH] for i in range(0,len(data),CH)]
    for ci,ch in enumerate(chunks):
        c=f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push({json.dumps(ch,ensure_ascii=False,separators=(",",":"))});\n'
        (REPO/'data'/'processed'/f'lite-part-{ci}.js').write_text(c,'utf-8')

def main():
    data=json.loads(LITE.read_text('utf-8'))
    todo=[(i,r) for i,r in enumerate(data) if r.get('c')=='未分类']
    print(f'Unclassified: {len(todo)}',flush=True)
    if not todo: print('Done!'); return

    prompt_tmpl=(SKILL/'unified_prompt.txt').read_text('utf-8')
    param_tmpl=(SKILL/'param_extraction_prompt.txt').read_text('utf-8')
    BATCH=3; done=0; start=time.time()
    batches=[todo[i:i+BATCH] for i in range(0,len(todo),BATCH)]

    for bi,batch in enumerate(batches):
        items=[]
        for idx,r in batch:
            items.append({'id':len(items),'type':r.get('i','n'),'title':(r.get('t','')or'')[:120],'body':(r.get('b','')or'')[:250]})
        fmt='\n\n只输出JSON数组，每个元素格式：\n{"id":0,"category":"完整路径或\\"不相关\\"","tag":"专属标签或空字符串","topic":"核心主题或空字符串"}'
        raw=call_glm(prompt_tmpl+json.dumps(items,ensure_ascii=False)+fmt)
        results=parse(raw)
        updates={}
        for r in results:
            if not isinstance(r,dict): continue
            lid=r.get('id')
            if lid is None or lid<0 or lid>=len(batch): continue
            gi=batch[lid][0]; cat=r.get('category','').strip(); tag=r.get('tag','').strip(); topic=r.get('topic','').strip()
            if cat=='不相关': updates[gi]={'c':'不相关','tg':'','tp':''}
            else:
                nc=norm(cat); rt=data[gi].get('i','n'); vt=NEWS_TAGS if rt=='n' else LIT_TAGS
                if tag not in vt: tag=''
                if not topic or len(topic)>15: topic=''
                updates[gi]={'c':nc or cat,'tg':tag,'tp':topic}
        for idx,fields in updates.items():
            data[idx].update(fields); done+=1

        # Params (with robust type checking)
        pitems=[]; pmap={}
        for idx,fields in updates.items():
            if fields.get('c') and fields['c']!='不相关':
                r=data[idx]; pmap[len(pitems)]=idx
                pitems.append({'id':len(pitems),'type':r.get('i','n'),'category':(r.get('c','')or'')[:60],'title':(r.get('t','')or'')[:120],'body':(r.get('b','')or'')[:250]})
        if pitems:
            pfmt='\n\n只输出JSON数组，每个元素格式：\n{"id":0,"关键参数":["参数1","参数2"]}'
            praw=call_glm(param_tmpl+json.dumps(pitems,ensure_ascii=False)+pfmt)
            pres=parse(praw)
            for pr in pres:
                if not isinstance(pr,dict): continue
                pid=pr.get('id')
                if pid is not None and pid in pmap:
                    kp=pr.get('关键参数',pr.get('key_params',[]))
                    if isinstance(kp,list) and kp: data[pmap[pid]]['kp']=kp[:5]

        if (bi+1)%3==0 or bi==len(batches)-1:
            save_all(data)
            el=time.time()-start
            unc=sum(1 for r in data if r.get('c')=='未分类')
            print(f'[{bi+1}/{len(batches)}] Done={done} Unclassified={unc} ({el:.0f}s)',flush=True)

    save_all(data)
    unc=sum(1 for r in data if r.get('c')=='未分类')
    print(f'Complete! Done={done} Still unclassified={unc} Time={time.time()-start:.0f}s',flush=True)

if __name__=='__main__':
    main()
