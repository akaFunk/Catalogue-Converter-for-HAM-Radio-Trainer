#!/usr/bin/env python3
"""Build an offline question-to-course-page index from 50ohm.de.
Usage: python build_50ohm_links.py --catalog-zip PruefungsfragenZIP.zip
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import zipfile
import requests
from bs4 import BeautifulSoup

BASE = 'https://50ohm.de/'
SEEDS = [BASE + s + '_course_index.html' for s in ('N','NE','NEA')]
AGENT = 'HamRadioTrainer-LinkIndexer/1.0'
QUESTION = re.compile(r'\b([NEABV][A-Z]\d{3})\s*:')
PAGE = re.compile(r'^(?:N|E|A|NE|NEA)_[A-Za-z0-9_-]+\.html$')


def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ('https', 'http') or p.hostname != '50ohm.de':
        return None
    if p.query or not PAGE.fullmatch(p.path.lstrip('/')):
        return None
    name = p.path.rsplit('/',1)[-1]
    if any(x in name.lower() for x in ('slide_', '_slide', 'print', 'quiz', 'exam', 'flashcard')):
        return None
    return urlunsplit(('https','50ohm.de',p.path,'',''))


def parse_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for a in soup.find_all('a',href=True):
        target = canonical(urljoin(url,a['href']))
        if target:
            links.add(target)
    content = soup.find('main') or soup.find('article') or soup.body or soup
    title_node = content.find('h1') or soup.find('title')
    title = title_node.get_text(' ',strip=True) if title_node else url.rsplit('/',1)[-1]
    # Ignore navigation and scripts when finding actual question labels.
    cleaned = BeautifulSoup(str(content),'html.parser')
    for node in cleaned.select('script,style,nav,header,footer,aside'):
        node.decompose()
    ids = set(QUESTION.findall(cleaned.get_text(' ',strip=True)))
    # Only add anchors when the anchor itself contains the exact question ID.
    anchors = {}
    for node in cleaned.find_all(attrs={'id': True}):
        anchor = node['id']
        for qid in ids:
            if anchor in (qid,'question-'+qid,'question_'+qid,'q-'+qid,'q_'+qid):
                anchors.setdefault(qid,anchor)
    return title, ids, links, anchors


def load_question_ids(path):
    def walk(node):
        if isinstance(node,dict):
            if 'number' in node and 'question' in node:
                yield str(node['number'])
            else:
                for v in node.values():
                    yield from walk(v)
        elif isinstance(node,list):
            for v in node:
                yield from walk(v)
    with zipfile.ZipFile(path) as z:
        matches=[]
        for name in z.namelist():
            if name.lower().endswith('.json'):
                d=json.loads(z.read(name).decode('utf-8-sig'))
                if isinstance(d,dict) and 'sections' in d:
                    matches.append(d)
    if len(matches)!=1:
        raise ValueError('Nicht genau eine Katalog-JSON mit sections gefunden.')
    return set(walk(matches[0]))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--catalog-zip',type=Path,required=True)
    ap.add_argument('--output',type=Path,default=Path('50ohm_links.json'))
    ap.add_argument('--cache',type=Path,default=Path('.50ohm_cache'))
    ap.add_argument('--refresh',action='store_true')
    ap.add_argument('--delay',type=float,default=1.0)
    ap.add_argument('--max-pages',type=int,default=1000)
    args=ap.parse_args()
    expected=load_question_ids(args.catalog_zip)
    args.cache.mkdir(parents=True,exist_ok=True)
    session=requests.Session();session.headers['User-Agent']=AGENT
    # Fetch robots.txt first; unexpected errors abort rather than ignore policy.
    resp=session.get(BASE+'robots.txt',timeout=30)
    robots=RobotFileParser(BASE+'robots.txt')
    if resp.status_code==404:
        robots.parse([])
    else:
        resp.raise_for_status();robots.parse(resp.text.splitlines())
    delay=max(0.5,args.delay,float(robots.crawl_delay(AGENT) or robots.crawl_delay('*') or 0))
    last=time.monotonic()
    def fetch(url):
        nonlocal last
        if not robots.can_fetch(AGENT,url):
            raise RuntimeError('robots.txt verbietet Abruf: '+url)
        cache=args.cache/(hashlib.sha256(url.encode()).hexdigest()+'.html')
        if cache.exists() and not args.refresh:
            return cache.read_text(encoding='utf-8')
        for attempt in range(3):
            time.sleep(max(0,delay-(time.monotonic()-last)))
            last=time.monotonic()
            try:
                response=session.get(url,timeout=40,allow_redirects=False)
                response.raise_for_status()
                if response.status_code!=200:
                    raise RuntimeError(f'Unerwarteter HTTP-Status {response.status_code}: {url}')
                text=response.content.decode('utf-8-sig')
                cache.write_text(text,encoding='utf-8')
                return text
            except (requests.RequestException,UnicodeError):
                if attempt==2:raise
                time.sleep(2**attempt)
    queue=list(SEEDS);seen=set();index={};errors=[];unknown=set();page_count=0
    while queue:
        url=queue.pop(0)
        if url in seen:continue
        if len(seen)>=args.max_pages:
            errors.append({'url':url,'error':'max-pages erreicht; Index unvollstaendig'})
            break
        seen.add(url)
        try:
            title,qids,links,anchors=parse_page(fetch(url),url)
        except Exception as exc:
            errors.append({'url':url,'error':str(exc)})
            print('FEHLER:',url,exc)
            continue
        page_count+=1
        queue.extend(sorted(links-seen))
        if url.endswith('_course_index.html'):continue
        variant=url.rsplit('/',1)[-1].split('_',1)[0]
        unknown.update(qids-expected)
        for qid in sorted(qids & expected):
            target=url+('#'+anchors[qid] if qid in anchors else '')
            entry={'variant':variant,'title':title,'url':target}
            index.setdefault(qid,[])
            if entry not in index[qid]:index[qid].append(entry)
        print(f'{page_count:4}: {len(qids & expected):3} Fragen | {url}')
    for entries in index.values():entries.sort(key=lambda x:(x['variant'],x['url']))
    missing=sorted(expected-set(index))
    payload={'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
             'seeds':SEEDS,'pages_ok':page_count,'errors':errors,'missing_questions':missing,
             'unknown_question_labels':sorted(unknown),'questions':dict(sorted(index.items()))}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temp=args.output.with_suffix(args.output.suffix+'.tmp')
    temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(args.output)
    args.output.with_suffix('.missing.txt').write_text('\n'.join(missing)+'\n',encoding='utf-8')
    print(f'Fragen mit Links: {len(index)}/{len(expected)}; Fundstellen: {sum(map(len,index.values()))}; fehlend: {len(missing)}')
    print(f'Abruffehler: {len(errors)}; Index: {args.output}')
    if errors:raise SystemExit(2)

if __name__=='__main__':
    main()
