from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run
from typing import Any


SOURCE_REPOS = {
    'wechat-daily-news-csv': {'kind': 'news', 'subdir': 'csv', 'patterns': [re.compile(r'^\d{4}-\d{2}-\d{2}\.csv$')], 'title_keys': ['标题', 'title'], 'body_keys': ['正文', 'content', 'summary'], 'author_keys': ['作者', 'author'], 'url_keys': ['URL', 'url', 'link']},
    'news-spider': {'kind': 'news', 'subdir': 'data', 'patterns': [re.compile(r'^articles-\d{4}-\d{2}-\d{2}\.csv$')], 'title_keys': ['title', '标题'], 'body_keys': ['content', '正文', 'summary'], 'author_keys': ['author', '作者'], 'url_keys': ['url', 'URL', 'link']},
    'literature-rss-spider': {'kind': 'literature', 'subdir': 'output', 'patterns': [re.compile(r'^news_with_abstract_\d{4}-\d{2}-\d{2}\.csv$')], 'title_keys': ['title', '标题'], 'body_keys': ['abstract', 'summary', '摘要', 'content'], 'author_keys': ['author', 'authors', '作者'], 'url_keys': ['url', 'URL', 'link']},
}


@dataclass
class BuildStats:
    records_total: int = 0
    records_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cache_hits: int = 0
    cache_misses: int = 0
    taxonomy_version: str = '2026-06-30-fresh'
    extractor_version: str = 'v1'
    processed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            'records_total': self.records_total,
            'records_by_type': dict(self.records_by_type),
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'taxonomy_version': self.taxonomy_version,
            'extractor_version': self.extractor_version,
            'processed_at': self.processed_at,
        }


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def pick(row: dict[str, Any], keys: list[str]) -> str:
    lowered = {k.lower(): k for k in row}
    for key in keys:
        if key in row and normalize_text(row[key]):
            return normalize_text(row[key])
        lk = key.lower()
        if lk in lowered and normalize_text(row[lowered[lk]]):
            return normalize_text(row[lowered[lk]])
    return ''


def extract_params(text: str) -> list[dict[str, Any]]:
    patterns = [
        (re.compile(r'(\d+(?:\.\d+)?)\s*(%|nm|mm|cm|m|kg|g|t|w|kw|mw|gw|kwh|mwh|gwh|v|kv|mah|wh|℃|°c|k|ms|s|h|d|倍|x)', re.I), '数值+单位'),
        (re.compile(r'(\d+(?:\.\d+)?)\s*[\-~–到]\s*(\d+(?:\.\d+)?)\s*(%|nm|mm|cm|m|kg|g|t|w|kw|mw|gw|kwh|mwh|gwh|v|kv|mah|wh|℃|°c|k|ms|s|h|d|倍|x)', re.I), '范围数值+单位'),
    ]
    result = []
    for sentence in re.split(r'(?<=[。！？.!?])\s+', text):
        sentence = sentence.strip()
        if not sentence:
            continue
        for pattern, reason in patterns:
            for match in pattern.finditer(sentence):
                numeric = match.group(1)
                unit = match.group(match.lastindex or 2) if match.lastindex else ''
                result.append({'value_raw': match.group(0), 'value_numeric': float(numeric), 'unit': unit, 'evidence_text': sentence[:240], 'extraction_reason': reason, 'confidence': 0.85})
    return result[:12]


def classify_leaf(text: str, taxonomy: list[dict[str, Any]]) -> str:
    best_label = '未分类'
    best_score = 0
    low = text.lower()
    for leaf in taxonomy:
        score = 0
        for token in leaf.get('keywords', []):
            if token and token.lower() in low:
                score += 3
        for token in leaf.get('path', []):
            if token and token.lower() in low:
                score += 1
        if score > best_score:
            best_score = score
            best_label = '-'.join(leaf.get('path', [])) or leaf.get('label', '未分类')
    return best_label


def is_alert(title: str, body: str, params: list[dict[str, Any]]) -> bool:
    text = f'{title} {body}'.lower()
    terms = ['突破', '首个', '量产', '商业化', '刷新', 'record', 'first', 'pilot', 'commercial']
    return any(term in text for term in terms) or bool(params)


def ensure_repo(root: Path, repo: str) -> Path:
    base = root / 'sources'
    base.mkdir(exist_ok=True)
    local = base / repo
    if local.exists():
        return local
    run(['git', 'clone', f'git@github.com:sbq9712/{repo}.git', str(local)], check=True)
    return local


def collect_rows(repo_root: Path, spec: dict[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    folder = repo_root / spec['subdir']
    if not folder.exists():
        return rows
    for csv_file in sorted(folder.glob('*.csv')):
        if any(pattern.match(csv_file.name) for pattern in spec['patterns']):
            for row in read_csv_rows(csv_file):
                rows.append((csv_file, row))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='data')
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    out_root = root / args.output
    taxonomy = load_json(root / 'taxonomy.json', {}).get('leaves', [])
    cache = load_json(out_root / 'processed' / 'cache.json', {})

    stats = BuildStats()
    records: list[dict[str, Any]] = []

    for repo, spec in SOURCE_REPOS.items():
        repo_root = ensure_repo(root, repo)
        for csv_file, row in collect_rows(repo_root, spec):
            title = pick(row, spec['title_keys'])
            body = pick(row, spec['body_keys'])
            authors = pick(row, spec['author_keys'])
            url = pick(row, spec['url_keys'])
            source_text = ' '.join([title, body, authors, url])
            content_hash = stable_hash(source_text)
            cache_key = stable_hash('|'.join([url, content_hash, stats.taxonomy_version, stats.extractor_version]))
            cached = cache.get(cache_key)
            if cached:
                item = cached
                stats.cache_hits += 1
            else:
                params = extract_params(body)
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', csv_file.name)
                item = {
                    'title': title,
                    'authors': authors,
                    'body': body,
                    'category': classify_leaf(source_text, taxonomy),
                    'date': date_match.group(1) if date_match else '',
                    'source': f'{repo}/{csv_file.name}',
                    'url': url,
                    'intelligence_type': spec['kind'],
                    'is_alert': is_alert(title, body, params),
                    'key_parameters': params,
                    'content_hash': content_hash,
                    'cache_hit': False,
                }
                cache[cache_key] = item
                stats.cache_misses += 1
            records.append(item)
            stats.records_total += 1
            stats.records_by_type[spec['kind']] += 1

    records.sort(key=lambda item: (item.get('date', ''), item.get('title', '')), reverse=True)
    out_root.mkdir(parents=True, exist_ok=True)
    dump_json(out_root / 'processed' / 'cache.json', cache)
    dump_json(out_root / 'processed' / 'intelligence.json', {'meta': stats.to_dict(), 'records': records, 'knowledge': []})
    dump_json(out_root / 'meta.json', stats.to_dict())
    dump_json(out_root / 'knowledge' / 'index.json', {'documents': []})
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
