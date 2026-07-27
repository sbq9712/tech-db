#!/usr/bin/env python3
"""Generate AI summaries for lv records using GLM 5.2."""
import json, re, subprocess, sys, time
from pathlib import Path

MODEL='glm-5.2'; PROVIDER='zai'
REPO=Path('/home/rhett/tech-db-fresh')
LITE=REPO/'data/processed/all-records-lite.json'

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

def main():
    data=json.loads(LITE.read_text('utf-8'))
    todo=[(i,r) for i,r in enumerate(data) if r.get('lv') and r.get('fb') and not r.get('as')]
    print(f'Need AI summary: {len(todo)}',flush=True)
    if not todo: print('None to do.'); return

    BATCH=3; done=0; start=time.time()
    batches=[todo[i:i+BATCH] for i in range(0,len(todo),BATCH)]

    for bi,batch in enumerate(batches):
        items_text = '\n\n'.join([f'[{j}] 标题：{(r.get("t","") or "")[:100]}\n正文：{(r.get("fb","") or r.get("b",""))[:500]}' for j,(idx,r) in enumerate(batch)])
        prompt = f'''请为以下每篇技术情报生成一段100-200字的中文摘要。摘要应客观概括核心内容、关键技术点或主要发现。

{items_text}

只输出JSON数组，每个元素格式：{{"id":0,"summary":"摘要文本"}}'''

        raw=call_glm(prompt)
        if not raw:
            print(f'[{bi+1}/{len(batches)}] Empty response, skip',flush=True)
            continue

        text=raw.strip()
        if text.startswith('```'): text=text.strip('`').replace('json\n','',1).strip()
        m=re.search(r'\[.*\]',text,re.S)
        if m: text=m.group(0)
        try:
            results=json.loads(text)
            if not isinstance(results,list): results=[]
        except: results=[]

        for r in results:
            if not isinstance(r,dict): continue
            lid=r.get('id')
            if lid is None or lid<0 or lid>=len(batch): continue
            gi=batch[lid][0]
            summary=r.get('summary','').strip()
            if summary:
                data[gi]['as']=summary
                done+=1

        if (bi+1)%5==0 or bi==len(batches)-1:
            LITE.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),'utf-8')
            CH=3000; chunks=[data[i:i+CH] for i in range(0,len(data),CH)]
            for f in (REPO/'data/processed').glob('lite-part-*.js'): f.unlink()
            for ci,ch in enumerate(chunks):
                c=f'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push({json.dumps(ch,ensure_ascii=False,separators=(",",":"))});\n'
                (REPO/'data/processed'/f'lite-part-{ci}.js').write_text(c,'utf-8')
            el=time.time()-start
            no_as=sum(1 for r in data if r.get('lv') and r.get('fb') and not r.get('as'))
            print(f'[{bi+1}/{len(batches)}] Done={done} Remaining={no_as} ({el:.0f}s)',flush=True)

    LITE.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),'utf-8')
    no_as=sum(1 for r in data if r.get('lv') and r.get('fb') and not r.get('as'))
    print(f'Complete! Summaries={done} Remaining={no_as} Time={time.time()-start:.0f}s',flush=True)

if __name__=='__main__':
    main()
