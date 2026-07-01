from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run
from typing import Any

SOURCE_REPOS = {
    'wechat-daily-news-csv': {'kind': 'news', 'subdir': 'csv', 'patterns': [re.compile(r'^\d{4}-\d{2}-\d{2}\.csv$')], 'title_keys': ['标题', 'title'], 'body_keys': ['正文', 'content', 'summary', '摘要'], 'author_keys': ['作者', 'author'], 'url_keys': ['URL', 'url', 'link']},
    'news-spider': {'kind': 'news', 'subdir': 'data', 'patterns': [re.compile(r'^articles-\d{4}-\d{2}-\d{2}\.csv$')], 'title_keys': ['title', '标题'], 'body_keys': ['content', '正文', 'summary', '摘要'], 'author_keys': ['author', '作者'], 'url_keys': ['url', 'URL', 'link']},
    'literature-rss-spider': {'kind': 'literature', 'subdir': 'output', 'patterns': [re.compile(r'^news_with_abstract_\d{4}-\d{2}-\d{2}\.csv$')], 'title_keys': ['title', '标题'], 'body_keys': ['abstract', 'summary', '摘要', 'content'], 'author_keys': ['author', 'authors', '作者'], 'url_keys': ['url', 'URL', 'link']},
}

@dataclass
class BuildStats:
    records_total: int = 0
    records_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    source_files: int = 0
    taxonomy_version: str = 'unknown'
    extractor_version: str = 'v3-sharded'
    processed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    warnings: list[str] = field(default_factory=list)
    def to_dict(self) -> dict[str, Any]:
        return {'records_total': self.records_total, 'records_by_type': dict(self.records_by_type), 'source_files': self.source_files, 'taxonomy_version': self.taxonomy_version, 'extractor_version': self.extractor_version, 'processed_at': self.processed_at, 'warnings': self.warnings}

def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

def clean(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()

def read_csv(path: Path) -> list[dict[str, Any]]:
    for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            with path.open('r', encoding=enc, newline='') as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except UnicodeDecodeError:
            continue
    return []

def pick(row: dict[str, Any], keys: list[str]) -> str:
    lowered = {k.lower(): k for k in row}
    for key in keys:
        if key in row and clean(row[key]):
            return clean(row[key])
        lk = key.lower()
        if lk in lowered and clean(row[lowered[lk]]):
            return clean(row[lowered[lk]])
    return ''

def ensure_repo(root: Path, repo: str, stats: BuildStats) -> Path:
    local = root / 'sources' / repo
    local.parent.mkdir(exist_ok=True)
    if local.exists():
        try: run(['git', '-C', str(local), 'pull', '--ff-only'], check=True)
        except Exception as exc: stats.warnings.append(f'{repo} pull failed: {exc}')
        return local
    run(['git', 'clone', f'https://github.com/sbq9712/{repo}.git', str(local)], check=True)
    return local

def extract_params(text: str) -> list[dict[str, Any]]:
    units = r'%|nm|mm|cm|m|kg|g|t|w|kw|mw|gw|kwh|mwh|gwh|v|kv|mah|wh|℃|°c|k|ms|s|h|d|倍|x|亿元|万元|美元'
    pattern = re.compile(rf'(\d+(?:\.\d+)?)\s*({units})', re.I)
    out = []
    for sentence in re.split(r'(?<=[。！？.!?])\s+', text):
        sentence = sentence.strip()
        for match in pattern.finditer(sentence):
            out.append({'value_raw': match.group(0), 'value_numeric': float(match.group(1)), 'unit': match.group(2), 'evidence_text': sentence[:220], 'extraction_reason': '数值+单位', 'confidence': 0.85})
    return out[:8]

def classify_leaf(text: str, leaves: list[dict[str, Any]]) -> tuple[str, float]:
    low = text.lower(); best = ('未分类', 0)
    for leaf in leaves:
        score = sum(3 for t in leaf.get('keywords', []) if t and t.lower() in low) + sum(1 for t in leaf.get('path', []) if t and t.lower() in low)
        if score > best[1]: best = ('-'.join(leaf.get('path', [])) or leaf.get('label', '未分类'), score)
    return best[0], min(0.95, 0.45 + best[1] / 20) if best[1] else 0.0

def is_alert(title: str, body: str, params: list[dict[str, Any]]) -> bool:
    text = f'{title} {body}'.lower()
    return any(term in text for term in ['突破','首个','量产','商业化','刷新','record','first','pilot','commercial']) or bool(params)

def file_date(path: Path) -> str:
    m = re.search(r'(\d{4}-\d{2}-\d{2})', path.name)
    return m.group(1) if m else 'unknown'

def collect_rows(repo_root: Path, spec: dict[str, Any], stats: BuildStats) -> list[tuple[Path, dict[str, Any]]]:
    rows = []; folder = repo_root / spec['subdir']
    if not folder.exists(): stats.warnings.append(f'missing folder: {folder}'); return rows
    for csv_file in sorted(folder.glob('*.csv')):
        if any(p.match(csv_file.name) for p in spec['patterns']):
            stats.source_files += 1
            rows.extend((csv_file, row) for row in read_csv(csv_file))
    return rows

def build_knowledge(records: list[dict[str, Any]], leaves: list[dict[str, Any]], out: Path) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for r in records: grouped[r.get('category') or '未分类'].append(r)
    cats = list(dict.fromkeys(['-'.join(l.get('path', [])) or l.get('label', '未分类') for l in leaves] + sorted(grouped)))
    docs = []
    for cat in cats:
        items = grouped.get(cat, []); params = [p for x in items for p in x.get('key_parameters', [])]
        doc = {'category': cat, 'summary': {'basic_profile': f'该行业当前收录 {len(items)} 条情报，其中新闻 {sum(1 for x in items if x.get("intelligence_type") == "news")} 条，文献 {sum(1 for x in items if x.get("intelligence_type") == "literature")} 条。', 'bottlenecks': ['需在后续 LLM 生成阶段补充卡点技术。'], 'key_metrics': [{'unit': u, 'count': c} for u, c in Counter(p.get('unit') or '无单位' for p in params).most_common(8)], 'frontier_values': params[:12]}, 'evidence': [{'title': x.get('title'), 'url': x.get('url'), 'date': x.get('date'), 'intelligence_type': x.get('intelligence_type'), 'key_parameters': x.get('key_parameters', [])[:5]} for x in items[:30]], 'updated_at': datetime.now(timezone.utc).isoformat()}
        docs.append(doc); dump_json(out / f'{cat}.json', doc)
    dump_json(out / 'index.json', {'generated_at': datetime.now(timezone.utc).isoformat(), 'documents': docs})
    return docs

def write_shards(records: list[dict[str, Any]], out: Path, meta: dict[str, Any], knowledge: list[dict[str, Any]]) -> None:
    processed = out / 'processed'
    if processed.exists():
        for p in processed.glob('records-*.json'): p.unlink()
    by_month = defaultdict(list)
    for r in records: by_month[(r.get('date') or 'unknown')[:7]].append(r)
    shards = []
    for month, rows in sorted(by_month.items(), reverse=True):
        path = processed / f'records-{month}.json'
        dump_json(path, {'month': month, 'records': rows})
        shards.append({'month': month, 'path': f'data/processed/{path.name}', 'records': len(rows)})
    dump_json(processed / 'manifest.json', {'meta': meta, 'shards': shards, 'knowledge_index': 'data/knowledge/index.json'})
    dump_json(processed / 'intelligence.json', {'meta': meta, 'records': records[:500], 'knowledge': knowledge})

def load_existing_classifications(processed: Path) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    if not processed.exists():
        return existing
    for shard in processed.glob('records-*.json'):
        try:
            payload = load_json(shard, {'records': []})
        except Exception:
            continue
        for record in payload.get('records', []):
            content_hash = record.get('content_hash')
            if not content_hash:
                continue
            if record.get('classifier_model') in ('glm-5.2', 'gpt-5.4-mini') and record.get('category') and record.get('category') != '未分类':
                existing[content_hash] = {
                    'category': record.get('category'),
                    'classification_confidence': record.get('classification_confidence', 0.0),
                    'classification_reason': record.get('classification_reason', ''),
                    'classifier_model': record.get('classifier_model'),
                    'classifier_provider': record.get('classifier_provider'),
                    'key_parameters': [],
                    'cache_hit': False,
                }
    return existing


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument('--output', default='data'); args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]; out_root = root / args.output
    taxonomy = load_json(root / 'taxonomy.json', {'version': 'unknown', 'leaves': []}); leaves = taxonomy.get('leaves', [])
    preserved = load_existing_classifications(out_root / 'processed')
    stats = BuildStats(taxonomy_version=taxonomy.get('version', 'unknown')); records = []
    for repo, spec in SOURCE_REPOS.items():
        for csv_file, row in collect_rows(ensure_repo(root, repo, stats), spec, stats):
            title = pick(row, spec['title_keys']); body = pick(row, spec['body_keys']); authors = pick(row, spec['author_keys']); url = pick(row, spec['url_keys'])
            text = ' '.join([title, body, authors, url])
            params = []
            content_hash = stable_hash(text)
            item = {'title': title, 'authors': authors, 'body': body, 'category': '未分类', 'date': file_date(csv_file), 'source': f'{repo}/{csv_file.name}', 'url': url, 'intelligence_type': spec['kind'], 'is_alert': is_alert(title, body, params), 'key_parameters': params, 'content_hash': content_hash, 'classification_confidence': 0.0, 'classification_reason': 'default_unclassified_until_gpt54mini_review', 'cache_hit': False}
            if content_hash in preserved:
                item.update(preserved[content_hash])
            records.append(item); stats.records_total += 1; stats.records_by_type[spec['kind']] += 1
    records.sort(key=lambda x: (x.get('date',''), x.get('title','')), reverse=True)
    knowledge = build_knowledge(records, leaves, out_root / 'knowledge')
    write_shards(records, out_root, stats.to_dict(), knowledge)
    dump_json(out_root / 'meta.json', stats.to_dict())
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__': raise SystemExit(main())
