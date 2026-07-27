#!/usr/bin/env python3
"""Reclassify lv records that were wrongly classified as 不相关."""
import json, re, subprocess, sys, time
from pathlib import Path

MODEL='glm-5.2'; PROVIDER='zai'
REPO=Path('/home/rhett/tech-db-fresh')
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
        return result if isinstance(result, list) else []
    except: return []

def main():
    data=json.loads(LITE.read_text('utf-8'))
    todo=[(i,r) for i,r in enumerate(data) if r.get('lv') and r.get('c')=='不相关']
    print(f'LV records to reclassify: {len(todo)}',flush=True)
    if not todo: print('None to do.'); return

    prompt_tmpl=(SKILL/'unified_prompt.txt').read_text('utf-8')
    BATCH=5; done=0; start=time.time()
    batches=[todo[i:i+BATCH] for i in range(0,len(todo),BATCH)]

    for bi,batch in enumerate(batches):
        items=[]
        for idx,r in batch:
            items.append({'id':len(items),'type':r.get('i','n'),'title':(r.get('t','')or'')[:120],'body':(r.get('fb','')or r.get('b',''))[:250]})
        fmt='\n\n只输出JSON数组，每个元素格式：\n{"id":0,"category":"完整路径","tag":"专属标签","topic":"核心主题"}\n注意：这些是精选/重点/预警情报，绝对不能分类为"不相关"，必须归入某个具体技术领域。'
        raw=call_glm(prompt_tmpl+json.dumps(items,ensure_ascii=False)+fmt)
        results=parse(raw)
        for r in results:
            if not isinstance(r,dict): continue
            lid=r.get('id')
            if lid is None or lid<0 or lid>=len(batch): continue
            gi=batch[lid][0]; cat=r.get('category','').strip(); tag=r.get('tag','').strip(); topic=r.get('topic','').strip()
            if cat and cat!='不相关':
                nc=norm(cat); rt=data[gi].get('i','n'); vt=NEWS_TAGS if rt=='n' else LIT_TAGS
                if tag not in vt: tag=''
                if not topic or len(topic)>15: topic=''
                data[gi]['c']=nc; data[gi]['tg']=tag; data[gi]['tp']=topic
                done+=1
                print(f'  [{done}] {data[gi]["t"][:40]} → {nc}',flush=True)

        if (bi+1)%2==0 or bi==len(batches)-1:
            LITE.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),'utf-8')
            CH=3000; chunks=[data[i:i+CH] for i in range(0,len(data),CH)]
            for ci,ch in enumerate(chunks):
                c=f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push({json.dumps(ch,ensure_ascii=False,separators=(",",":"))});\n'
                (REPO/'data'/'processed'/f'lite-part-{ci}.js').write_text(c,'utf-8')
            print(f'[{bi+1}/{len(batches)}] Done={done} Saved.',flush=True)

    LITE.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),'utf-8')
    still_bad=sum(1 for r in data if r.get('lv') and r.get('c')=='不相关')
    print(f'Complete! Reclassified={done} Still 不相关={still_bad} Time={time.time()-start:.0f}s',flush=True)

if __name__=='__main__':
    main()
