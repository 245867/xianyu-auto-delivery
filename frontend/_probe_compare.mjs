import fs from 'node:fs';
import { selectObservedMarketEvidence } from './lib/sourcing-quality.ts';
const feed = JSON.parse(fs.readFileSync('G:/have time fish/01-源码/xianyu-auto-delivery/data/_feed_dump.json', 'utf8'));
const listingKey = t => t.normalize('NFKC').replace(/\s+/g, '').toLowerCase();
const marketKeyword = t => t.replace(/【[^】]*】/g, '').trim() || t;
const marketSearchKey = k => k.normalize('NFKC').trim().toLowerCase();
const byKeyword = new Map();
for (const e of feed.market_cache || []) byKeyword.set(marketSearchKey(e.keyword || e.keyword_key || ''), e);
const observations = (feed.market_cache || []).map(e => ({key: marketSearchKey(e.keyword || e.keyword_key || ''), fetchedAt: e.fetched_at, error: e.error, items: (e.items||[]).filter(i => Number.isFinite(i.price) && i.price > 0 && i.itemId && i.itemUrl)}));
const groups = new Map();
for (const q of feed.quotes || []) {
  if (!Number.isFinite(q.price) || q.price <= 0) continue;
  const key = listingKey(q.title);
  const cur = groups.get(key);
  if (!cur || q.price < cur.price) groups.set(key, q);
}
const now = Date.now();
const rows = [];
for (const q of groups.values()) {
  const kw = marketSearchKey(marketKeyword(q.title));
  const result = byKeyword.get(kw);
  const { evidence, items } = selectObservedMarketEvidence(q, observations.map(o => ({...o, exactSearch: o.key === kw})), false, now);
  if (evidence.benchmark) {
    const gap = Math.round(evidence.benchmark.price * 100) - Math.round(q.price * 100);
    rows.push({gap, upstream: q.price, market: evidence.benchmark.price, title: q.title.slice(0,80), kw, sold: evidence.benchmark.detail_sold_count ?? null, want: evidence.benchmark.want_count ?? null, confidence: evidence.confidence, comparable: evidence.comparableCount, valid: evidence.validCount, eligible: evidence.eligibleForProfit});
  }
}
rows.sort((a,b) => b.gap - a.gap);
console.log('benchmark rows', rows.length, 'positive', rows.filter(r=>r.gap>0).length);
for (const r of rows.slice(0, 30)) console.log(JSON.stringify(r));
