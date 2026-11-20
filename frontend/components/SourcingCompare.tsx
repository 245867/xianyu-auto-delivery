import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  ChevronDown,
  CircleDollarSign,
  Clock3,
  Database,
  ExternalLink,
  Flame,
  GitCompareArrows,
  Layers3,
  LockKeyhole,
  PackageSearch,
  Radio,
  RefreshCw,
  Search,
  ShieldCheck,
  Store,
  Tag,
  TriangleAlert,
  Waypoints,
  X,
  Zap,
  Sparkles,
  ShieldAlert,
  CheckCheck,
} from 'lucide-react';
import { generateFiveBlockDesc, auditContent, sanitizeContent } from '../lib/vantasell-generator';
import {
  calculatePriceSpread,
  hasComparableProductIdentity,
  hotKeywordMatchesTitle,
  isFreshQuote,
  isUsableHotQuote,
  prioritizeRejectionReasons,
  selectLowestQuote,
  selectObservedMarketEvidence,
} from '../lib/sourcing-quality';
import type { ListingAssessment, MarketConfidence } from '../lib/sourcing-quality';
import { get, post } from '../lib/request';
import {
  createOpportunityFulfillmentTask,
  generateOpportunityImage,
  getAccountDetails,
  getCards,
  getImageMaterials,
  getMaterialPaths,
  getOpportunityFulfillmentTask,
  recoverOpportunityFulfillmentTask,
} from '../services/api';
import type {
  AccountDetail,
  Card,
  MaterialItem,
  OpportunityFulfillmentDelivery,
  OpportunityFulfillmentEvent,
  OpportunityFulfillmentStatus,
  OpportunityFulfillmentTask,
  OpportunityFulfillmentTaskPayload,
} from '../types';

interface Quote {
  title: string;
  price: number;
  supplier: string;
  url: string;
  source?: string;
  source_label?: string;
  source_url?: string;
  updated_at?: string | null;
  fetched_at?: string | null;
  stale?: boolean;
  link_type?: 'product' | 'source';
}

interface ProviderStatus {
  id: string;
  name: string;
  ok: boolean;
  status?: string;
  quote_count?: number;
  fetched_at?: string | null;
  error?: string;
}

interface MarketSnapshot {
  keyword_key: string;
  keyword: string;
  fetched_at: string | null;
  items: Listing[];
  error?: string;
}

interface QuoteFeed {
  fetched_at: string | null;
  stale: boolean;
  quotes: Quote[];
  market_cache?: MarketSnapshot[];
  providers?: ProviderStatus[];
  hot_keywords?: string[];
  keyword_catalog?: KeywordCatalogItem[];
  catalog_summary?: CatalogSummary;
}

interface KeywordCatalogItem {
  keyword: string;
  category: string;
  origin: 'hot' | 'category';
}

interface CatalogSummary {
  source?: string;
  source_url?: string;
  site_count?: number | null;
  product_count?: number | null;
  offer_count?: number | null;
}

interface GuardStatus {
  cooldown_active: boolean;
  cooldown_remain_seconds: number;
  last_reason?: string;
  last_error?: string;
  consecutive_fail?: number;
}

interface Listing {
  itemId: string;
  itemUrl: string;
  title: string;
  price: number | null;
  imageUrl?: string;
  detail_sold_count?: number;
  sold_count?: number;
  detail_stock?: number;
  sold_out?: boolean;
  view_count?: number;
  want_count?: number;
  browse_count?: number;
  want_source?: string;
  want_stale?: boolean;
}

interface SearchResult {
  ok: boolean;
  error?: string;
  items?: Listing[];
  fetched_at?: string;
  cookie_id?: string;
  summary?: {
    detail_enrich?: { requested: number; success: number; failed: number; error?: string };
  };
}

type AuditTone = 'live' | 'scrape' | 'account' | 'signed' | 'info' | 'offline';

const auditSources: Array<{
  name: string;
  url: string;
  mode: string;
  detail: string;
  tone: AuditTone;
}> = [
  {
    name: '卡券雷达',
    url: 'https://kajiage.com/price-check',
    mode: '实时接口',
    detail: '匿名 JSON 报价，含站点、库存与更新时间',
    tone: 'live',
  },
  {
    name: '卡券集市',
    url: 'https://kaquanjishi.com/price-check',
    mode: '实时接口',
    detail: '会话 Cookie + CSRF 的匿名 JSON 批量查价',
    tone: 'live',
  },
  {
    name: '券上比',
    url: 'https://quanshangbi.com/',
    mode: '页面抓取',
    detail: '匿名搜索页可读，未确认稳定公开 JSON 接口',
    tone: 'scrape',
  },
  {
    name: '智多星',
    url: 'https://www.wisdomstar.site/compare',
    mode: '页面抓取',
    detail: '匿名 SSR 查价比价页，可按关键词读取',
    tone: 'scrape',
  },
  {
    name: '91卡券',
    url: 'https://mai.91kami.com/',
    mode: '采购方案未订购',
    detail: '当前登录账号显示「您还未订购采购方案」。开发指南覆盖卡券管理与提卡，自动报价待核实。',
    tone: 'account',
  },
  {
    name: '亿权益',
    url: 'https://shop.yiquanyi.com/goods?id=17319',
    mode: '可登录核价 · 接口权限待开通',
    detail: '可打开 B 站大会员 1 个月直充商品核价。接口管理显示「当前账号暂无接口操作权限」。',
    tone: 'account',
  },
  {
    name: '白舟智选',
    url: 'https://cargo.wboat.cn/home',
    mode: '授权 API',
    detail: '接口文档公开，实价查询依赖商户 userid 与 sign',
    tone: 'account',
  },
  {
    name: '卡易充',
    url: 'https://kayichong.com/',
    mode: '授权查价',
    detail: '货源报价需要密码或登录，匿名搜索返回签名错误',
    tone: 'account',
  },
  {
    name: '顺势权益',
    url: 'https://shunshiqy.com/',
    mode: '签名门槛',
    detail: '首页可见部分单品价，批量搜索需站点签名',
    tone: 'signed',
  },
  {
    name: '聚合权益商城',
    url: 'https://www.kaquanwang.cn/site/19',
    mode: '导航 + 签名',
    detail: '入口为导航，来源商城匿名搜索仍受签名限制',
    tone: 'signed',
  },
  {
    name: '聚量权益',
    url: 'https://www.kaquanjishi.com/site/84',
    mode: '品牌目录',
    detail: '页面仅收录来源站入口，没有单品报价',
    tone: 'info',
  },
  {
    name: '聚量权益 SUP',
    url: 'https://sup.tigtop.com/',
    mode: '当前失效',
    detail: 'HTTPS 443 当前拒绝连接',
    tone: 'offline',
  },
  {
    name: '阿奇索',
    url: 'https://www.agiso.com/product/aldsidle',
    mode: '履约工具',
    detail: '自动发货工具，不提供采购单价',
    tone: 'info',
  },
  {
    name: '任推邦',
    url: 'https://rentuibang.app/',
    mode: '推广联盟',
    detail: '网盘拉新佣金入口，不是卡券货源比价',
    tone: 'info',
  },
  {
    name: '蜂小推',
    url: 'https://app.fengtuiwl.com/',
    mode: '推广联盟',
    detail: '展示推广佣金，不是商品采购价',
    tone: 'info',
  },
];

const auditToneClass: Record<AuditTone, string> = {
  live: 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900/50 dark:bg-emerald-950/30 dark:text-emerald-200',
  scrape: 'border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-900/50 dark:bg-blue-950/30 dark:text-blue-200',
  account: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-200',
  signed: 'border-orange-200 bg-orange-50 text-orange-800 dark:border-orange-900/50 dark:bg-orange-950/30 dark:text-orange-200',
  info: 'border-gray-200 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-[#25292E] dark:text-gray-300',
  offline: 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-200',
};

const card = 'ios-card rounded-xl';

function listingKey(title: string) {
  return title.normalize('NFKC').replace(/\s+/g, '').toLowerCase();
}

function marketKeyword(title: string) {
  return title.replace(/【[^】]*】/g, '').trim() || title;
}

function marketSearchUrl(title: string) {
  return 'https://www.goofish.com/search?q=' + encodeURIComponent(marketKeyword(title));
}

function validListing(item: Listing) {
  return typeof item.price === 'number' && Number.isFinite(item.price)
    && Boolean(item.itemId) && !item.itemId.startsWith('mock_')
    && item.itemUrl.startsWith('https://www.goofish.com/');
}

function listingContainsTitle(listingTitle: string, quoteTitle: string) {
  return hasComparableProductIdentity(quoteTitle, listingTitle);
}

function marketSearchKey(keyword: string) {
  return keyword.normalize('NFKC').trim().toLowerCase();
}

function lowestListing(items: Listing[]) {
  return items.filter(validListing).reduce<Listing | undefined>(
    (lowest, item) => !lowest || item.price! < lowest.price! ? item : lowest,
    undefined,
  );
}

function quoteLinkLabel(quote: Quote) {
  return quote.link_type === 'product' ? '打开最低报价商品页' : '打开报价来源';
}

function detailNotice(result: SearchResult) {
  const detail = result.summary?.detail_enrich;
  if (!detail?.error) return undefined;
  return detail.success
    ? `部分详情未取到（已更新 ${detail.success} 条） · ${detail.error}`
    : `挂牌已更新，成交详情未取到 · ${detail.error}`;
}

const MARKET_CONFIDENCE_LABELS: Record<MarketConfidence, string> = {
  high: '高互动参照',
  medium: '同规格参照',
  low: '低互动待核',
  invalid: '异常隔离',
};

function marketConfidenceClass(confidence: MarketConfidence) {
  if (confidence === 'high') {
    return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-200';
  }
  if (confidence === 'medium') {
    return 'bg-blue-100 text-blue-800 dark:bg-blue-950/60 dark:text-blue-200';
  }
  if (confidence === 'low') {
    return 'bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-200';
  }
  return 'bg-rose-100 text-rose-800 dark:bg-rose-950/60 dark:text-rose-200';
}

function suspiciousAssessments(assessments: ListingAssessment[]) {
  return assessments
    .filter(item => !item.comparable || !item.priceSane || !item.sellable)
    .sort((left, right) =>
      (left.listing.price ?? Number.POSITIVE_INFINITY)
      - (right.listing.price ?? Number.POSITIVE_INFINITY),
    );
}

function readPrice(value: string) {
  if (!/^\d+(\.\d{1,2})?$/.test(value.trim())) return null;
  const price = Number(value);
  return Number.isFinite(price) ? Math.round(price * 100) : null;
}

function money(cents: number) {
  return `¥${(cents / 100).toFixed(2)}`;
}

function signedMoney(cents: number) {
  if (cents > 0) return `+${money(cents)}`;
  if (cents < 0) return `-${money(Math.abs(cents))}`;
  return money(0);
}

function fetchedLabel(value?: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '尚无成功快照';
}

function categoryTone(category: string) {
  if (/餐饮/.test(category)) {
    return {
      card: 'border-amber-200 bg-amber-50/70 dark:border-amber-900/50 dark:bg-amber-950/20',
      chip: 'bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-200',
    };
  }
  if (/影视/.test(category)) {
    return {
      card: 'border-blue-200 bg-blue-50/70 dark:border-blue-900/50 dark:bg-blue-950/20',
      chip: 'bg-blue-100 text-blue-800 dark:bg-blue-950/60 dark:text-blue-200',
    };
  }
  if (/游戏/.test(category)) {
    return {
      card: 'border-violet-200 bg-violet-50/70 dark:border-violet-900/50 dark:bg-violet-950/20',
      chip: 'bg-violet-100 text-violet-800 dark:bg-violet-950/60 dark:text-violet-200',
    };
  }
  if (/教育|网盘/.test(category)) {
    return {
      card: 'border-emerald-200 bg-emerald-50/70 dark:border-emerald-900/50 dark:bg-emerald-950/20',
      chip: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-200',
    };
  }
  if (/工具/.test(category)) {
    return {
      card: 'border-cyan-200 bg-cyan-50/70 dark:border-cyan-900/50 dark:bg-cyan-950/20',
      chip: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-950/60 dark:text-cyan-200',
    };
  }
  if (/生活/.test(category)) {
    return {
      card: 'border-rose-200 bg-rose-50/70 dark:border-rose-900/50 dark:bg-rose-950/20',
      chip: 'bg-rose-100 text-rose-800 dark:bg-rose-950/60 dark:text-rose-200',
    };
  }
  return {
    card: 'border-gray-200 bg-gray-50/80 dark:border-gray-700 dark:bg-[#202328]',
    chip: 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
  };
}

const FULFILLMENT_DELIVERY_OPTIONS: OpportunityFulfillmentDelivery[] = [
  '无需邮寄',
  '包邮',
  '按距离计费',
  '一口价',
];

const FULFILLMENT_SUCCESS_STATUSES = new Set<OpportunityFulfillmentStatus>([
  'delivery_ready',
]);

const FULFILLMENT_MANUAL_REVIEW_STATUSES = new Set<OpportunityFulfillmentStatus>([
  'submission_unknown_review_required',
  'duplicate_submission',
  'failed',
]);

const FULFILLMENT_RESUMABLE_STATUSES = new Set<OpportunityFulfillmentStatus>([
  'ready_to_publish',
  'inventory_not_ready',
  'publish_throttled',
  'publish_blocked_night',
  'submitted_pending_readback',
  'readback_not_visible',
  'readback_failed',
  'readback_confirmed',
  'sync_confirmed',
  'delivery_bound',
]);

const FULFILLMENT_POLL_STOP_STATUSES = new Set<OpportunityFulfillmentStatus>([
  'delivery_ready',
  'submission_unknown_review_required',
  'duplicate_submission',
  'failed',
  'publish_blocked_guard',
]);

const FULFILLMENT_STATUS_LABELS: Record<OpportunityFulfillmentStatus, string> = {
  draft: '草稿',
  ready_to_publish: '等待发布',
  inventory_not_ready: '库存未就绪',
  publishing: '发布中',
  publish_throttled: '发布节流',
  publish_blocked_night: '夜间暂停发布',
  publish_blocked_guard: '内容检查阻止发布',
  submitted_pending_readback: '已提交，等待回读',
  readback_not_visible: '回读暂不可见',
  readback_failed: '回读失败',
  readback_confirmed: '回读已确认',
  sync_confirmed: '商品同步已确认',
  delivery_bound: '自动发货已绑定',
  delivery_ready: '履约就绪',
  submission_unknown_review_required: '提交状态未知，需人工核对',
  duplicate_submission: '重复提交',
  failed: '任务失败',
};

const CARD_TYPE_LABELS: Record<Card['type'], string> = {
  api: 'API 接口',
  text: '固定文字',
  image: '图片',
  data: '批量卡密',
};

interface FulfillmentSeed {
  quote: Quote;
  listing: Listing;
}

interface FulfillmentFormState {
  cookieId: string;
  cardId: string;
  title: string;
  desc: string;
  imageUrls: string;
  price: string;
  quantity: string;
  delivery: OpportunityFulfillmentDelivery;
  postPrice: string;
  tags: string;
}

function fulfillmentFormFor(quote: Quote, listing: Listing): FulfillmentFormState {
  const defaultPrice = listing.price === null ? '' : String(listing.price);
  return {
    cookieId: '',
    cardId: '',
    title: quote.title,
    desc: generateFiveBlockDesc({
      title: quote.title,
      supplier: quote.supplier || quote.source_label || quote.source,
      price: defaultPrice,
    }),
    imageUrls: listing.imageUrl || '',
    price: defaultPrice,
    quantity: '1',
    delivery: '无需邮寄',
    postPrice: '0',
    tags: '',
  };
}

function dataCardInventory(card?: Card): number {
  if (!card || card.type !== 'data' || !card.data_content) return 0;
  return card.data_content.split('\n').filter(line => line.trim()).length;
}

function fulfillmentStatusClass(status: OpportunityFulfillmentStatus): string {
  if (FULFILLMENT_SUCCESS_STATUSES.has(status)) {
    return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-200';
  }
  if (FULFILLMENT_MANUAL_REVIEW_STATUSES.has(status) || status === 'readback_failed') {
    return 'bg-rose-100 text-rose-800 dark:bg-rose-950/60 dark:text-rose-200';
  }
  if (
    status === 'publish_blocked_guard'
    || status === 'publish_blocked_night'
    || status === 'publish_throttled'
    || status === 'inventory_not_ready'
    || status === 'readback_not_visible'
  ) {
    return 'bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-200';
  }
  return 'bg-blue-100 text-blue-800 dark:bg-blue-950/60 dark:text-blue-200';
}

interface FulfillmentTaskPanelProps {
  task: OpportunityFulfillmentTask;
  events: OpportunityFulfillmentEvent[];
  pollError: string;
  recovering: boolean;
  onRecover: () => void;
}

function FulfillmentTaskPanel({
  task,
  events,
  pollError,
  recovering,
  onRecover,
}: FulfillmentTaskPanelProps) {
  const inventory = task.result.inventory;
  const supplierProcurement = task.result.supplier_procurement;
  const canRecover = FULFILLMENT_RESUMABLE_STATUSES.has(task.status);
  const isReady = FULFILLMENT_SUCCESS_STATUSES.has(task.status);
  const needsManualReview = FULFILLMENT_MANUAL_REVIEW_STATUSES.has(task.status);

  return (
    <section
      className="overflow-hidden rounded-2xl border border-gray-200 bg-gray-50/80 dark:border-gray-700 dark:bg-[#202328]"
      aria-live="polite"
      aria-labelledby="fulfillment-task-title"
    >
      <div className="border-b border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-[#1B1E22] sm:p-5">
        <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
          <div>
            <p className="text-[10px] font-black uppercase tracking-[0.16em] text-gray-400">
              PERSISTENT TASK
            </p>
            <h4 id="fulfillment-task-title" className="mt-1 text-base font-black text-gray-950 dark:text-white">
              履约任务阶段
            </h4>
            <p className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
              {isReady
                ? '后端已确认商品同步、自动发货绑定和库存复核完成。'
                : needsManualReview
                  ? '任务已停止自动推进。请先在闲鱼或商品列表中人工核对，再由后端恢复。'
                  : task.status === 'publish_blocked_guard'
                    ? '发布前内容检查阻止了任务继续。此状态不在后端可恢复集合内。'
                    : '任务已持久化。页面只展示后端返回的阶段，不推断平台已上架。'}
            </p>
          </div>
          <span className={`inline-flex w-fit shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-black ${fulfillmentStatusClass(task.status)}`}>
            {isReady
              ? <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              : needsManualReview || task.status === 'publish_blocked_guard'
                ? <TriangleAlert className="h-3.5 w-3.5" aria-hidden="true" />
                : <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />}
            {FULFILLMENT_STATUS_LABELS[task.status]}
          </span>
        </div>
        <dl className="mt-4 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2.5 dark:border-gray-700 dark:bg-[#25292E]">
            <dt className="font-bold text-gray-400">任务 ID</dt>
            <dd className="mt-1 select-all break-all font-mono text-[11px] text-gray-800 dark:text-gray-200">{task.id}</dd>
          </div>
          <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2.5 dark:border-gray-700 dark:bg-[#25292E]">
            <dt className="font-bold text-gray-400">阶段</dt>
            <dd className="mt-1 truncate font-mono text-[11px] text-gray-800 dark:text-gray-200" title={task.stage}>
              {task.stage || '—'}
            </dd>
          </div>
          <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2.5 dark:border-gray-700 dark:bg-[#25292E]">
            <dt className="font-bold text-gray-400">item_id</dt>
            <dd className="mt-1 select-all break-all font-mono text-[11px] text-gray-800 dark:text-gray-200">
              {task.item_id || '尚未回读'}
            </dd>
          </div>
          <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2.5 dark:border-gray-700 dark:bg-[#25292E]">
            <dt className="font-bold text-gray-400">库存状态</dt>
            <dd className={`mt-1 font-black ${inventory?.ready ? 'text-emerald-700 dark:text-emerald-300' : 'text-amber-700 dark:text-amber-300'}`}>
              {inventory
                ? inventory.ready
                  ? `就绪${inventory.available === undefined ? '' : ` · ${inventory.available} 条`}`
                  : inventory.reason || '未就绪'
                : '等待后端检查'}
            </dd>
          </div>
        </dl>
      </div>

      <div className="space-y-3 p-4 sm:p-5">
        <div className="rounded-xl border border-gray-200 bg-white px-3 py-3 dark:border-gray-700 dark:bg-[#1B1E22]">
          <p className="text-xs font-black text-gray-900 dark:text-white">{task.message || '后端尚未返回阶段说明'}</p>
          <p className="mt-1 text-[10px] tabular-nums text-gray-400">
            {task.status} · 更新 {task.updated_at || task.created_at || '—'}
          </p>
        </div>

        {supplierProcurement && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
            <strong>上游采购：</strong>
            {supplierProcurement.message}
            <span className="ml-1 font-mono text-[10px] opacity-70">
              {supplierProcurement.mode}
            </span>
          </div>
        )}

        {task.result.guard_warnings && task.result.guard_warnings.length > 0 && (
          <div role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
            {task.result.guard_warnings.join('；')}
          </div>
        )}

        {task.error && (
          <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-3 text-xs leading-5 text-rose-900 dark:border-rose-900/60 dark:bg-rose-950/30 dark:text-rose-100">
            <strong>后端错误：</strong>{task.error}
          </div>
        )}

        {pollError && (
          <div role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
            状态读取暂时失败，任务不会被判定成功。{pollError}
          </div>
        )}

        <div>
          <p className="text-[10px] font-black uppercase tracking-[0.14em] text-gray-400">阶段记录</p>
          {events.length > 0 ? (
            <ol className="mt-2 max-h-56 space-y-2 overflow-y-auto pr-1">
              {events.map(event => (
                <li key={event.id} className="rounded-xl border border-gray-200 bg-white px-3 py-2.5 dark:border-gray-700 dark:bg-[#1B1E22]">
                  <div className="flex flex-col justify-between gap-1 sm:flex-row sm:items-center">
                    <span className={`w-fit rounded-full px-2 py-0.5 text-[10px] font-black ${fulfillmentStatusClass(event.status)}`}>
                      {FULFILLMENT_STATUS_LABELS[event.status]}
                    </span>
                    <span className="text-[10px] tabular-nums text-gray-400">{event.created_at}</span>
                  </div>
                  <p className="mt-2 text-xs leading-5 text-gray-700 dark:text-gray-200">{event.message}</p>
                  <p className="mt-1 font-mono text-[10px] text-gray-400">{event.stage}</p>
                </li>
              ))}
            </ol>
          ) : (
            <p className="mt-2 rounded-xl border border-dashed border-gray-300 px-3 py-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
              正在等待后端写入第一条阶段记录。
            </p>
          )}
        </div>

        {canRecover && (
          <button
            type="button"
            onClick={onRecover}
            disabled={recovering}
            className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-gray-300 bg-white px-4 text-xs font-black text-gray-800 transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] disabled:cursor-wait disabled:opacity-50 dark:border-gray-600 dark:bg-[#25292E] dark:text-white dark:hover:bg-[#2D3238]"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${recovering ? 'animate-spin' : ''}`} aria-hidden="true" />
            {recovering ? '正在请求恢复' : '恢复任务'}
          </button>
        )}
      </div>
    </section>
  );
}

type MarketState = Record<string, {
  items?: Listing[];
  error?: string;
  loading?: boolean;
  fetchedAt?: string | null;
  cookieId?: string;
  detailWarning?: string;
}>;

export default function SourcingCompare() {
  const [feed, setFeed] = useState<QuoteFeed>();
  const [feedError, setFeedError] = useState('');
  const [feedOrigin, setFeedOrigin] = useState<'live' | 'snapshot' | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [market, setMarket] = useState<MarketState>({});
  const [keyword, setKeyword] = useState('');
  const [searchLabel, setSearchLabel] = useState('');
  const [listings, setListings] = useState<Listing[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [searchWarning, setSearchWarning] = useState<string>();
  const [cost, setCost] = useState('');
  const [sale, setSale] = useState('');
  const [expenses, setExpenses] = useState('');
  const [displayFilter, setDisplayFilter] = useState('');
  const [onlyCompared, setOnlyCompared] = useState(false);
  const [marketRefresh, setMarketRefresh] = useState<string | null>(null);
  const [autoPaused, setAutoPaused] = useState(false);
  const [autoStatus, setAutoStatus] = useState('');
  const [guard, setGuard] = useState<GuardStatus>();
  const [showAllCategories, setShowAllCategories] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [showAllProfitRows, setShowAllProfitRows] = useState(false);
  const [liveCompareOpen, setLiveCompareOpen] = useState(true);
  const [fulfillmentSeed, setFulfillmentSeed] = useState<FulfillmentSeed | null>(null);
  const [fulfillmentForm, setFulfillmentForm] = useState<FulfillmentFormState>();
  const [fulfillmentAccounts, setFulfillmentAccounts] = useState<AccountDetail[]>([]);
  const [fulfillmentCards, setFulfillmentCards] = useState<Card[]>([]);
  const [fulfillmentMaterials, setFulfillmentMaterials] = useState<MaterialItem[]>([]);
  const [fulfillmentOptionsLoading, setFulfillmentOptionsLoading] = useState(false);
  const [fulfillmentOptionsError, setFulfillmentOptionsError] = useState('');
  const [fulfillmentTask, setFulfillmentTask] = useState<OpportunityFulfillmentTask>();
  const [fulfillmentEvents, setFulfillmentEvents] = useState<OpportunityFulfillmentEvent[]>([]);
  const [fulfillmentSubmitting, setFulfillmentSubmitting] = useState(false);
  const [fulfillmentCreateError, setFulfillmentCreateError] = useState('');
  const [fulfillmentPollError, setFulfillmentPollError] = useState('');
  const [fulfillmentPollVersion, setFulfillmentPollVersion] = useState(0);
  const [fulfillmentRecovering, setFulfillmentRecovering] = useState(false);
  const [fulfillmentMaterialId, setFulfillmentMaterialId] = useState('');
  const [fulfillmentMaterialLoading, setFulfillmentMaterialLoading] = useState(false);
  const [fulfillmentMaterialNotice, setFulfillmentMaterialNotice] = useState('');
  const [fulfillmentMaterialError, setFulfillmentMaterialError] = useState('');
  const [imageGenLoading, setImageGenLoading] = useState(false);
  const [generatedPreviews, setGeneratedPreviews] = useState<string[]>([]);
  const autoAttempted = useRef(new Set<string>());
  const feedRequest = useRef(0);
  const manualFeedLoading = useRef(false);
  const marketRequest = useRef(false);
  const costCents = readPrice(cost);
  const saleCents = readPrice(sale);
  const estimate = costCents !== null && saleCents !== null
    ? calculatePriceSpread(costCents, saleCents, readPrice(expenses))
    : null;
  const selectedFulfillmentCard = fulfillmentCards.find(
    item => String(item.id) === fulfillmentForm?.cardId,
  );

  const openFulfillmentDialog = (quote: Quote, listing: Listing) => {
    setFulfillmentSeed({ quote, listing });
    setFulfillmentForm(fulfillmentFormFor(quote, listing));
    setFulfillmentAccounts([]);
    setFulfillmentCards([]);
    setFulfillmentMaterials([]);
    setFulfillmentOptionsError('');
    setFulfillmentTask(undefined);
    setFulfillmentEvents([]);
    setFulfillmentCreateError('');
    setFulfillmentPollError('');
    setFulfillmentMaterialId('');
    setFulfillmentMaterialLoading(false);
    setFulfillmentMaterialNotice('');
    setFulfillmentMaterialError('');
    setGeneratedPreviews([]);
    setImageGenLoading(false);
  };

  const closeFulfillmentDialog = () => {
    setFulfillmentSeed(null);
    setFulfillmentForm(undefined);
    setFulfillmentTask(undefined);
    setFulfillmentEvents([]);
    setFulfillmentCreateError('');
    setFulfillmentPollError('');
    setGeneratedPreviews([]);
    setImageGenLoading(false);
  };

  useEffect(() => {
    if (!fulfillmentSeed) return;
    let disposed = false;
    setFulfillmentOptionsLoading(true);
    setFulfillmentOptionsError('');

    void (async () => {
      const [accountResult, cardResult, materialResult] = await Promise.allSettled([
        getAccountDetails(),
        getCards(),
        getImageMaterials(200),
      ]);
      if (disposed) return;

      const loadErrors: string[] = [];
      if (accountResult.status === 'fulfilled') {
        setFulfillmentAccounts(accountResult.value);
        const firstEnabledAccount = accountResult.value.find(account => account.enabled === true);
        setFulfillmentForm(current => ({
          ...(current ?? fulfillmentFormFor(fulfillmentSeed.quote, fulfillmentSeed.listing)),
          cookieId: current?.cookieId || firstEnabledAccount?.id || '',
        }));
      } else {
        loadErrors.push(`账号读取失败：${String(accountResult.reason)}`);
      }

      if (cardResult.status === 'fulfilled') {
        const enabledCards = cardResult.value.filter(item => item.enabled === true);
        setFulfillmentCards(enabledCards);
        setFulfillmentForm(current => ({
          ...(current ?? fulfillmentFormFor(fulfillmentSeed.quote, fulfillmentSeed.listing)),
          cardId: current?.cardId || (enabledCards[0] ? String(enabledCards[0].id) : ''),
        }));
      } else {
        loadErrors.push(`卡券读取失败：${String(cardResult.reason)}`);
      }

      if (materialResult.status === 'fulfilled') {
        if (materialResult.value.ok) {
          setFulfillmentMaterials(materialResult.value.items);
        } else {
          loadErrors.push(`素材读取失败：${materialResult.value.error || '后端返回 ok=false'}`);
        }
      } else {
        loadErrors.push(`素材读取失败：${String(materialResult.reason)}`);
      }

      if (loadErrors.length > 0) {
        setFulfillmentOptionsError(loadErrors.join('；'));
      }
      setFulfillmentOptionsLoading(false);
    })();

    return () => {
      disposed = true;
    };
  }, [fulfillmentSeed]);

  useEffect(() => {
    const taskId = fulfillmentTask?.id;
    const status = fulfillmentTask?.status;
    if (!fulfillmentSeed || !taskId || !status || FULFILLMENT_POLL_STOP_STATUSES.has(status)) {
      return;
    }

    let disposed = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const response = await getOpportunityFulfillmentTask(taskId);
        if (disposed) return;
        if (!response.ok) {
          throw new Error('后端返回 ok=false');
        }
        setFulfillmentTask(response.task);
        setFulfillmentEvents(response.events);
        setFulfillmentPollError('');
        if (FULFILLMENT_POLL_STOP_STATUSES.has(response.task.status)) return;
        timer = window.setTimeout(() => { void poll(); }, 1_500);
      } catch (caught) {
        if (disposed) return;
        setFulfillmentPollError(caught instanceof Error ? caught.message : '履约任务状态读取失败');
        timer = window.setTimeout(() => { void poll(); }, 3_000);
      }
    };

    timer = window.setTimeout(() => { void poll(); }, 1_000);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [fulfillmentSeed, fulfillmentTask?.id, fulfillmentTask?.status, fulfillmentPollVersion]);

  const applyFulfillmentMaterial = async () => {
    const materialId = Number(fulfillmentMaterialId);
    if (!Number.isInteger(materialId) || materialId <= 0) {
      setFulfillmentMaterialError('请先选择一个图片素材。');
      return;
    }
    setFulfillmentMaterialLoading(true);
    setFulfillmentMaterialError('');
    setFulfillmentMaterialNotice('');
    try {
      const response = await getMaterialPaths(materialId);
      if (!response.ok || !response.paths?.length) {
        throw new Error(response.error || '素材中没有可用图片路径');
      }
      setFulfillmentForm(current => current ? {
        ...current,
        imageUrls: response.paths!.join('\n'),
      } : current);
      setFulfillmentMaterialNotice(
        `已读取「${response.name || '所选素材'}」的 ${response.paths.length} 张图片路径，可继续编辑。`,
      );
    } catch (caught) {
      setFulfillmentMaterialError(caught instanceof Error ? caught.message : '图片路径读取失败');
    } finally {
      setFulfillmentMaterialLoading(false);
    }
  };

  const handleGenerateImages = async (count: number = 1) => {
    if (!fulfillmentForm) return;
    const title = fulfillmentForm.title.trim();
    if (!title) {
      setFulfillmentCreateError('请先填写发布标题，生图引擎将根据标题智能设计对应主图。');
      return;
    }
    setImageGenLoading(true);
    setFulfillmentCreateError('');
    try {
      const price = Number(fulfillmentForm.price) || 0;
      const tags = fulfillmentForm.tags.split(/[,，\n]/).map(t => t.trim()).filter(Boolean);
      const res = await generateOpportunityImage({
        title,
        price,
        tags,
        count,
        mode: 'auto',
      });
      if (!res.ok || !res.paths?.length) {
        throw new Error(res.error || '生成配图失败');
      }
      setFulfillmentForm(current => current ? {
        ...current,
        imageUrls: res.paths!.join('\n'),
      } : current);
      setGeneratedPreviews(res.urls || []);
      setFulfillmentMaterialNotice(res.message || `已智能生成 ${res.paths.length} 张高转化营销配图`);
    } catch (err) {
      setFulfillmentCreateError(err instanceof Error ? err.message : '智能生图异常');
    } finally {
      setImageGenLoading(false);
    }
  };

  const submitFulfillmentTask = async () => {
    if (!fulfillmentSeed || !fulfillmentForm) return;
    const cookieId = fulfillmentForm.cookieId.trim();
    const cardId = Number(fulfillmentForm.cardId);
    const title = fulfillmentForm.title.trim();
    const desc = fulfillmentForm.desc.trim();
    const imageUrls = fulfillmentForm.imageUrls
      .split(/\r?\n/)
      .map(value => value.trim())
      .filter(Boolean);
    const price = Number(fulfillmentForm.price);
    const quantity = Number(fulfillmentForm.quantity);
    const postPrice = Number(fulfillmentForm.postPrice);
    const tags = fulfillmentForm.tags
      .split(/[,，\n]/)
      .map(value => value.trim())
      .filter(Boolean);

    if (!cookieId) {
      setFulfillmentCreateError('请选择发布账号。');
      return;
    }
    if (!Number.isInteger(cardId) || cardId <= 0) {
      setFulfillmentCreateError('请选择启用的卡券。');
      return;
    }
    if (!title) {
      setFulfillmentCreateError('发布标题不能为空。');
      return;
    }
    if (!desc) {
      setFulfillmentCreateError('发布描述不能为空。');
      return;
    }
    if (imageUrls.length === 0) {
      setFulfillmentCreateError('至少需要一张主图路径或图片 URL。');
      return;
    }
    if (!Number.isFinite(price) || price <= 0) {
      setFulfillmentCreateError('售价必须大于 0。');
      return;
    }
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setFulfillmentCreateError('发布数量必须是大于 0 的整数。');
      return;
    }
    if (!Number.isFinite(postPrice) || postPrice < 0) {
      setFulfillmentCreateError('运费不能小于 0。');
      return;
    }

    const quote = fulfillmentSeed.quote;
    const source: OpportunityFulfillmentTaskPayload['source'] = {
      title: quote.title,
      price: quote.price,
      supplier: quote.supplier,
      url: quote.url,
      ...(quote.source === undefined ? {} : { source: quote.source }),
      ...(quote.source_label === undefined ? {} : { source_label: quote.source_label }),
      ...(quote.source_url === undefined ? {} : { source_url: quote.source_url }),
      ...(quote.updated_at === undefined ? {} : { updated_at: quote.updated_at }),
    };
    const payload: OpportunityFulfillmentTaskPayload = {
      cookie_id: cookieId,
      card_id: cardId,
      title,
      desc,
      imageUrls,
      price,
      quantity,
      delivery: fulfillmentForm.delivery,
      postPrice,
      ...(tags.length > 0 ? { tags } : {}),
      source,
    };

    setFulfillmentSubmitting(true);
    setFulfillmentCreateError('');
    try {
      const response = await createOpportunityFulfillmentTask(payload);
      if (!response.ok || !response.task?.id) {
        throw new Error('后端未返回已持久化的履约任务');
      }
      setFulfillmentTask(response.task);
      setFulfillmentEvents([]);
      setFulfillmentPollError('');
      setFulfillmentPollVersion(version => version + 1);
    } catch (caught) {
      setFulfillmentCreateError(caught instanceof Error ? caught.message : '履约任务创建失败');
    } finally {
      setFulfillmentSubmitting(false);
    }
  };

  const recoverFulfillmentTask = async () => {
    if (!fulfillmentTask) return;
    setFulfillmentRecovering(true);
    setFulfillmentCreateError('');
    setFulfillmentPollError('');
    try {
      const response = await recoverOpportunityFulfillmentTask(fulfillmentTask.id);
      if (!response.ok || !response.task?.id) {
        throw new Error('后端未返回恢复后的履约任务');
      }
      setFulfillmentTask(response.task);
      setFulfillmentPollVersion(version => version + 1);
    } catch (caught) {
      setFulfillmentCreateError(caught instanceof Error ? caught.message : '恢复任务失败');
    } finally {
      setFulfillmentRecovering(false);
    }
  };

  const loadFeed = async (showRefresh = false) => {
    if (manualFeedLoading.current) return;
    const requestId = ++feedRequest.current;
    if (showRefresh) {
      manualFeedLoading.current = true;
      setRefreshing(true);
    }
    try {
      const result = await get<QuoteFeed>(
        '/api/opportunity/sourcing-quotes',
        showRefresh ? { refresh: true } : undefined,
      );
      if (requestId !== feedRequest.current) return;
      if (!result.quotes?.length && result.stale) throw new Error('未取得公开报价');
      setFeed(result);
      setFeedOrigin('live');
      setFeedError('');
    } catch {
      if (requestId !== feedRequest.current) return;
      setFeedError('实时报价读取失败，已加载的报价仅供核对。');
      try {
        const saved = await get<QuoteFeed>('/static/sourcing-quote-snapshot.json');
        if (requestId === feedRequest.current && saved.quotes?.length) {
          setFeed(current => current ?? { ...saved, stale: true });
          setFeedOrigin(current => current ?? 'snapshot');
        }
      } catch {
        // The latest error and any already-loaded quotes stay visible.
      }
    } finally {
      if (showRefresh) {
        manualFeedLoading.current = false;
        setRefreshing(false);
      }
    }
  };

  const loadGuard = async () => {
    try {
      const result = await get<GuardStatus>('/api/opportunity/guard-status');
      setGuard(result);
      return result;
    } catch {
      setGuard(undefined);
      return undefined;
    }
  };

  useEffect(() => {
    void loadFeed();
    void loadGuard();
    const feedTimer = window.setInterval(() => { void loadFeed(); }, 300_000);
    const guardTimer = window.setInterval(() => { void loadGuard(); }, 60_000);
    return () => {
      window.clearInterval(feedTimer);
      window.clearInterval(guardTimer);
    };
  }, []);

  useEffect(() => {
    if (!guard?.cooldown_active) return;
    const minutes = Math.max(1, Math.ceil((guard.cooldown_remain_seconds || 0) / 60));
    setAutoPaused(true);
    setAutoStatus(
      `闲鱼自动匹配已暂停：${guard.last_reason || '登录态或平台风控要求停止请求'}。约 ${minutes} 分钟后解除；请同时确认闲鱼网页登录态。`,
    );
  }, [
    guard?.cooldown_active,
    guard?.cooldown_remain_seconds,
    guard?.last_reason,
  ]);

  const comparisonTime = Date.now();
  const usableFeed = feedOrigin === 'live' && !feedError && !refreshing;
  const providers = feed?.providers ?? [];
  const marketByKeyword = useMemo(() => {
    const observations: MarketState = Object.create(null);
    for (const snapshot of feed?.market_cache ?? []) {
      if (!snapshot.keyword_key) continue;
      observations[snapshot.keyword_key] = {
        items: snapshot.items,
        fetchedAt: snapshot.fetched_at,
        error: snapshot.error || undefined,
      };
    }
    for (const [key, local] of Object.entries(market)) {
      const stored = observations[key];
      const storedTime = Date.parse(stored?.fetchedAt ?? '') || 0;
      const localTime = Date.parse(local.fetchedAt ?? '') || 0;
      const latest = stored && storedTime > localTime ? stored : local;
      observations[key] = {
        ...latest,
        loading: local.loading,
        error: local.error ?? (storedTime >= localTime ? stored?.error : undefined),
      };
    }
    return observations;
  }, [feed?.market_cache, market]);

  const quoteGroups = useMemo(() => {
    const groups = new Map<string, {
      quote: Quote;
      quoteCount: number;
      sources: Set<string>;
      suppliers: Set<string>;
    }>();
    for (const quote of feed?.quotes ?? []) {
      if (!Number.isFinite(quote.price) || quote.price <= 0) continue;
      const key = listingKey(quote.title);
      const current = groups.get(key) ?? {
        quote,
        quoteCount: 0,
        sources: new Set<string>(),
        suppliers: new Set<string>(),
      };
      current.quoteCount += 1;
      current.sources.add(quote.source || quote.source_label || 'unknown');
      current.suppliers.add(quote.supplier || quote.source_label || 'unknown');
      current.quote = selectLowestQuote(current.quote, quote, comparisonTime);
      groups.set(key, current);
    }
    return groups;
  }, [feed?.quotes, comparisonTime]);
  const upstreamRows = [...quoteGroups.values()].map(group => group.quote);
  const hotKeywords = feed?.hot_keywords?.filter(Boolean) ?? [];
  const catalogItems: KeywordCatalogItem[] = feed?.keyword_catalog?.length
    ? feed.keyword_catalog
    : hotKeywords.map(keyword => ({
      keyword,
      category: '公开热词',
      origin: 'hot',
    }));
  const categoryCount = new Set(catalogItems.map(item => item.category)).size;

  const marketObservations = Object.entries(marketByKeyword).map(([key, observation]) => ({
    key,
    fetchedAt: observation.fetchedAt,
    error: observation.error,
    items: (observation.items ?? []).filter(validListing),
  }));
  const compared = upstreamRows.map(quote => {
    const keywordKey = marketSearchKey(marketKeyword(quote.title));
    const result = marketByKeyword[keywordKey];
    const { evidence, items, freshBenchmark, fetchedAt } = selectObservedMarketEvidence(
      quote,
      marketObservations.map(observation => ({
        ...observation,
        exactSearch: observation.key === keywordKey,
      })),
      Boolean(result?.loading || result?.error),
      comparisonTime,
    );
    const benchmark = evidence.benchmark;
    const quoteFresh = usableFeed && isFreshQuote(quote, comparisonTime);
    const spread = quoteFresh && freshBenchmark && typeof benchmark?.price === 'number'
      ? calculatePriceSpread(Math.round(quote.price * 100), Math.round(benchmark.price * 100))
      : null;
    const verified = spread && evidence.eligibleForProfit ? benchmark : null;
    const directReferences = (result?.items ?? []).filter(item =>
      validListing(item) && !listingContainsTitle(item.title, quote.title),
    );
    const reference = lowestListing(directReferences);
    const selectedAssessment = evidence.assessments.find(item =>
      item.listing.itemId === benchmark?.itemId,
    ) ?? suspiciousAssessments(evidence.assessments)[0];
    return {
      quote,
      result,
      evidence,
      benchmark,
      verified,
      quoteFresh,
      spread,
      evidenceCount: items.length,
      directReferences,
      reference,
      marketFetchedAt: fetchedAt,
      cachedFetchedAt: fetchedAt,
      cookieId: result?.cookieId,
      sold: selectedAssessment?.sold ?? null,
      want: selectedAssessment?.want ?? null,
      browse: selectedAssessment?.browse ?? null,
    };
  }).sort((left, right) => {
    const gapLeft = left.verified ? left.spread?.gapCents ?? -Infinity : -Infinity;
    const gapRight = right.verified ? right.spread?.gapCents ?? -Infinity : -Infinity;
    if (gapLeft !== gapRight) return gapRight - gapLeft;
    const leftCoverage = quoteGroups.get(listingKey(left.quote.title))?.suppliers.size ?? 0;
    const rightCoverage = quoteGroups.get(listingKey(right.quote.title))?.suppliers.size ?? 0;
    return (rightCoverage - leftCoverage)
      || (left.quote.price - right.quote.price)
      || left.quote.title.localeCompare(right.quote.title, 'zh-CN');
  });

  const filteredRows = compared.filter(row => {
    const searchKey = listingKey(displayFilter);
    return (!searchKey || listingKey(row.quote.title).includes(searchKey))
      && (!onlyCompared || row.evidence.comparableCount > 0);
  });
  const visible = showAll ? filteredRows : filteredRows.slice(0, 36);
  const profitableRaw = compared.flatMap(row =>
    row.verified && row.spread && row.spread.gapCents > 0
      ? [{ ...row, verified: row.verified, ...row.spread }]
      : [],
  );
  const profitableByListing = new Map<string, (typeof profitableRaw)[number]>();
  for (const row of profitableRaw) {
    const itemId = row.verified.itemId;
    const current = profitableByListing.get(itemId);
    const isMoreSpecific = row.quote.title.length > (current?.quote.title.length ?? -1);
    const isBetterTieBreak = row.quote.title.length === current?.quote.title.length
      && row.gapCents > (current?.gapCents ?? -Infinity);
    if (!current || isMoreSpecific || isBetterTieBreak) {
      profitableByListing.set(itemId, row);
    }
  }
  const profitableRows = [...profitableByListing.values()]
    .sort((left, right) =>
      right.gapCents - left.gapCents
      || (right.marginPercent ?? -1) - (left.marginPercent ?? -1)
      || left.quote.title.localeCompare(right.quote.title, 'zh-CN'),
    );
  const duplicateProfitMatches = profitableRaw.length - profitableRows.length;
  const visibleProfitableRows = showAllProfitRows
    ? profitableRows
    : profitableRows.slice(0, 12);
  const autoTargets = compared
    .filter(row => !displayFilter || listingKey(row.quote.title).includes(listingKey(displayFilter)))
    .slice(0, 12);
  const autoTargetTitles = autoTargets.map(row => row.quote.title).join('\u0000');
  const trustedBenchmarkCount = compared.filter(row => row.verified).length;
  const pendingBenchmarkCount = compared.filter(row =>
    !row.verified
    && (Boolean(row.evidence.benchmark) || row.evidence.suspiciousCount > 0),
  ).length;
  const loadingCount = compared.filter(row => row.result?.loading).length;
  const marketCacheCount = feed?.market_cache?.length ?? 0;
  const rawQuoteCount = feed?.quotes?.length ?? 0;
  const referenceCount = compared.filter(row => Boolean(row.reference)).length;
  const lowestUpstream = upstreamRows.reduce<number | null>(
    (lowest, quote) => lowest === null || quote.price < lowest ? quote.price : lowest,
    null,
  );
  const liveProviderCount = providers.filter(provider => provider.ok).length;
  const hasFreshQuote = compared.some(row => row.quoteFresh);
  const feedStatusHasWarning = !hasFreshQuote || Boolean(feed?.stale);
  const feedStatusLabel = (() => {
    if (refreshing) return '刷新中';
    if (feedOrigin === 'snapshot') return '公开快照';
    if (feedError) return '读取失败';
    if (!feed) return '连接中';
    if (!hasFreshQuote) return '等待新报价';
    return feed.stale ? '部分更新' : '实时读取';
  })();
  const providerQuoteCount = providers.reduce(
    (total, provider) => total + (provider.ok ? provider.quote_count ?? 0 : 0),
    0,
  );
  const bestGap = compared.reduce<number | null>((best, row) => {
    if (!row.verified) return best;
    const gap = row.spread?.gapCents;
    if (gap === undefined) return best;
    return best === null || gap > best ? gap : best;
  }, null);
  const hotSearches = catalogItems.map(item => {
    const rows = upstreamRows.filter(row => hotKeywordMatchesTitle(item.keyword, row.title));
    const usableRows = rows.filter(row =>
      isUsableHotQuote(item.keyword, row.title, row.price, item.category),
    );
    const productRows = usableRows.filter(row => row.link_type === 'product');
    const sourceRows = usableRows.filter(row => row.link_type !== 'product');
    const lowestProduct = productRows.reduce<Quote | undefined>(
      (current, quote) => current ? selectLowestQuote(current, quote, comparisonTime) : quote,
      undefined,
    );
    const lowestSource = sourceRows.reduce<Quote | undefined>(
      (current, quote) => current ? selectLowestQuote(current, quote, comparisonTime) : quote,
      undefined,
    );
    const lowest = lowestProduct ?? lowestSource;
    const sourceAlternative = lowestProduct && lowestSource
      && lowestSource.price < lowestProduct.price
      ? lowestSource
      : undefined;
    const keywordKey = marketSearchKey(item.keyword);
    const marketResult = marketByKeyword[keywordKey];
    const marketSelection = lowest
      ? selectObservedMarketEvidence(
          lowest,
          marketObservations.map(observation => ({
            ...observation,
            exactSearch: observation.key === keywordKey,
          })),
          Boolean(marketResult?.loading || marketResult?.error),
          comparisonTime,
        )
      : null;
    const marketBenchmark = marketSelection?.evidence.benchmark ?? null;
    const freshMarketBenchmark = Boolean(marketSelection?.freshBenchmark && marketBenchmark);
    const marketSpread = freshMarketBenchmark
      && typeof marketBenchmark?.price === 'number'
      && lowest
      && usableFeed
      && isFreshQuote(lowest, comparisonTime)
      ? calculatePriceSpread(
          Math.round(lowest.price * 100),
          Math.round(marketBenchmark.price * 100),
        )
      : null;
    const marketComparable = marketSelection?.evidence.assessments
      .filter(assessment =>
        assessment.comparable
        && assessment.priceSane
        && assessment.sellable
        && typeof assessment.listing.price === 'number',
      ) ?? [];
    const marketLowestComparable = marketComparable.reduce<Listing | null>(
      (current, assessment) =>
        !current || assessment.listing.price! < current.price! ? assessment.listing : current,
      null,
    );
    return {
      ...item,
      quoteCount: usableRows.length,
      excludedQuoteCount: rows.length - usableRows.length,
      lowest,
      sourceAlternative,
      marketResult,
      marketEvidence: marketSelection?.evidence ?? null,
      marketBenchmark,
      marketLowestComparable,
      marketComparableCount: marketComparable.length,
      marketSpread,
      marketVerified: Boolean(
        marketSelection?.evidence.eligibleForProfit
        && freshMarketBenchmark
        && lowest
        && usableFeed
        && isFreshQuote(lowest, comparisonTime),
      ),
      marketFetchedAt: marketSelection?.fetchedAt ?? null,
    };
  });
  const visibleHotSearches = showAllCategories
    ? hotSearches
    : hotSearches.filter(item => item.origin === 'hot');
  const selectedHot = hotSearches.find(item => item.keyword === displayFilter)
    ?? visibleHotSearches[0]
    ?? hotSearches[0];
  const catalogMarketCount = catalogItems.filter(item => {
    const key = listingKey(item.keyword);
    return (feed?.market_cache ?? []).some(snapshot =>
      listingKey(snapshot.keyword).includes(key)
      || key.includes(listingKey(snapshot.keyword)),
    );
  }).length;

  useEffect(() => {
    if (!usableFeed || !autoTargets.length || busy || marketRefresh || autoPaused || guard?.cooldown_active) {
      return;
    }
    const missing = autoTargets.filter(row =>
      !autoAttempted.current.has(row.quote.title)
      && row.quoteFresh
      && !row.verified,
    );
    if (!missing.length) return;
    const next = [...missing].sort((left, right) =>
        (right.evidenceCount - left.evidenceCount)
        || (left.quote.price - right.quote.price))[0];
    const delay = autoAttempted.current.size ? 11_000 : 1_200;
    const timer = window.setTimeout(() => {
      if (marketRequest.current) return;
      autoAttempted.current.add(next.quote.title);
      setAutoStatus(`正在补齐「${next.quote.title}」`);
      void refreshMarket(next.quote, true);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [feed, market, usableFeed, busy, marketRefresh, autoPaused, autoTargetTitles, guard?.cooldown_active]);

  async function refreshAll() {
    if (manualFeedLoading.current) return;
    autoAttempted.current.clear();
    const feedRefresh = loadFeed(true);
    const nextGuard = await loadGuard();
    setAutoPaused(Boolean(nextGuard?.cooldown_active));
    if (!nextGuard?.cooldown_active) setAutoStatus('');
    await feedRefresh;
  }

  async function refreshMarket(quote: Quote, automatic = false, queryOverride?: string) {
    if (marketRequest.current) return;
    marketRequest.current = true;
    const query = queryOverride?.trim() || marketKeyword(quote.title);
    const key = marketSearchKey(query);
    setMarketRefresh(quote.title);
    setMarket(previous => ({
      ...previous,
      [key]: {
        ...previous[key],
        loading: true,
        error: undefined,
        detailWarning: undefined,
      },
    }));
    try {
      const result = await post<SearchResult>('/api/opportunity/analyze', {
        keyword: query,
        detail: true,
        detail_limit: automatic ? 2 : 10,
      });
      if (!result.ok) throw new Error(result.error || '闲鱼查询失败');
      const notice = detailNotice(result);
      setMarket(previous => ({
        ...previous,
        [key]: {
          items: result.items ?? [],
          fetchedAt: result.fetched_at ?? null,
          cookieId: result.cookie_id,
          loading: false,
          detailWarning: notice,
        },
      }));
      if (automatic) setAutoStatus(notice
        ? `「${quote.title}」${notice}`
        : `已更新「${quote.title}」的挂牌`);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '闲鱼查询失败';
      setMarket(previous => ({
        ...previous,
        [key]: {
          ...previous[key],
          error: message,
          loading: false,
        },
      }));
      if (automatic && /风控|验证|冷却|频繁|滑块|登录|令牌/.test(message)) {
        setAutoPaused(true);
        setAutoStatus('自动补齐遇到登录态或风控保护，已暂停并保留上次成功快照。');
        void loadGuard();
      } else if (automatic) {
        setAutoStatus(`「${quote.title}」暂未取到挂牌，继续下一款`);
      }
    } finally {
      marketRequest.current = false;
      setMarketRefresh(null);
    }
  }

  async function search(query: string) {
    query = query.trim();
    if (!query || marketRequest.current) return;
    marketRequest.current = true;
    const key = marketSearchKey(query);
    setBusy(true);
    setError('');
    setSearchWarning(undefined);
    setSearchLabel('');
    setListings([]);
    setMarket(previous => ({
      ...previous,
      [key]: { ...previous[key], loading: true, error: undefined, detailWarning: undefined },
    }));
    try {
      const result = await post<SearchResult>('/api/opportunity/analyze', {
        keyword: query,
        detail: true,
        detail_limit: 5,
      });
      if (!result.ok) throw new Error(result.error || '闲鱼查询失败');
      const notice = detailNotice(result);
      setListings((result.items ?? []).filter(validListing));
      setSearchLabel(query);
      setSearchWarning(notice);
      setMarket(previous => ({
        ...previous,
        [key]: {
          items: result.items ?? [],
          fetchedAt: result.fetched_at ?? null,
          cookieId: result.cookie_id,
          loading: false,
          detailWarning: notice,
        },
      }));
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '闲鱼查询失败';
      setError(message);
      setMarket(previous => ({
        ...previous,
        [key]: { ...previous[key], error: message, loading: false },
      }));
      if (/风控|验证|冷却|频繁|滑块|登录|令牌/.test(message)) {
        setAutoPaused(true);
        setAutoStatus('手动查询触发登录态或风控保护，自动补齐已暂停。');
        void loadGuard();
      }
    } finally {
      marketRequest.current = false;
      setBusy(false);
    }
  }

return (
    <section className="space-y-6 animate-fade-in">
      <header className="flex flex-col justify-between gap-4 pr-14 lg:flex-row lg:items-end">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-[11px] font-black uppercase tracking-[0.24em] text-gray-400">
              MARGIN INTELLIGENCE
            </p>
            <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-bold ${
              feedStatusHasWarning
                ? 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200'
                : 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200'
            }`}>
              <span className={`h-1.5 w-1.5 rounded-full ${feedStatusHasWarning ? 'bg-amber-500' : 'bg-emerald-500'}`} />
              {feedStatusLabel}
            </span>
          </div>
          <h2 className="mt-1 text-3xl font-black tracking-[-0.04em] text-gray-950 dark:text-white">
            货源比价
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500 dark:text-gray-400">
            对比最新上游报价和闲鱼同规格挂牌。比价核对组合、时长、会员等级和交付方式。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a
            href="https://kajiage.com/price-check"
            target="_blank"
            rel="noopener noreferrer"
            className="ios-btn-secondary inline-flex min-h-11 items-center gap-2 rounded-xl px-4 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] focus-visible:ring-offset-2 dark:bg-[#25292E] dark:text-white dark:ring-offset-[#111315]"
          >
            卡券雷达
            <ExternalLink className="h-4 w-4" aria-hidden="true" />
          </a>
          <button
            type="button"
            onClick={() => { void refreshAll(); }}
            disabled={refreshing}
            className="ios-btn-primary inline-flex min-h-11 items-center gap-2 rounded-xl px-4 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] focus-visible:ring-offset-2 disabled:opacity-60 dark:ring-offset-[#111315]"
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} aria-hidden="true" />
            {refreshing ? '刷新中' : '刷新公开报价'}
          </button>
        </div>
      </header>

      {feedError && (
        <div role="status" className="flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900 dark:border-amber-900/50 dark:bg-[#342F1F] dark:text-amber-100">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{feedError}</span>
        </div>
      )}

      {autoStatus && (
        <div
          role="status"
          className={`flex flex-wrap items-center justify-between gap-3 rounded-2xl border px-4 py-3 text-sm font-semibold ${
            autoPaused
              ? 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900/50 dark:bg-[#342F1F] dark:text-amber-100'
              : 'border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-900/50 dark:bg-[#1E2A38] dark:text-blue-100'
          }`}
        >
          <span className="flex items-center gap-2">
            <Radio className="h-4 w-4 shrink-0" aria-hidden="true" />
            {autoStatus}
          </span>
          {autoPaused ? (
            <button
              type="button"
              disabled={Boolean(guard?.cooldown_active)}
              onClick={() => {
                autoAttempted.current.clear();
                setAutoPaused(false);
                setAutoStatus('已恢复逐款补齐闲鱼挂牌');
              }}
              className="min-h-10 rounded-xl border border-amber-400/50 px-3 text-xs font-bold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] disabled:cursor-not-allowed disabled:opacity-55"
            >
              {guard?.cooldown_active ? '冷却中，暂不能继续' : '风控核对完成，继续'}
            </button>
          ) : (
            <span className="text-xs font-medium opacity-70">逐款请求，至少间隔 11 秒</span>
          )}
        </div>
      )}

      <div className="space-y-4">
        <section className="relative overflow-hidden rounded-[28px] bg-[#191B1F] p-5 text-white shadow-[0_24px_70px_rgba(17,19,21,0.16)] sm:p-6">
          <div className="pointer-events-none absolute -right-20 -top-28 h-72 w-72 rounded-full bg-[#FFE815]/15 blur-3xl" />
          <div className="pointer-events-none absolute bottom-0 left-0 h-px w-full bg-gradient-to-r from-transparent via-[#FFE815]/50 to-transparent" />
          <div className="relative">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="text-[11px] font-black uppercase tracking-[0.22em] text-white/65">
                  LIVE OPPORTUNITY BOARD
                </p>
                <h3 className="mt-2 text-2xl font-black tracking-tight">同规格可信价差雷达</h3>
                <p className="mt-1 max-w-xl text-sm leading-6 text-white/70">
                  同规格参照优先采用有详情成交记录的挂牌。想要人数反映需求。
                </p>
              </div>
              <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.06] px-3 py-1.5 text-[11px] font-bold text-white/70">
                <ShieldCheck className="h-3.5 w-3.5 text-[#FFE815]" aria-hidden="true" />
                规格与互动双重校验
              </span>
            </div>

            <div className="mt-7 grid gap-3 md:grid-cols-[minmax(0,1.1fr)_minmax(0,1.5fr)] md:items-stretch">
              <div className="relative overflow-hidden rounded-2xl bg-[#FFE815] p-5 text-black">
                <Zap className="absolute -bottom-6 -right-4 h-32 w-32 text-black/[0.07]" aria-hidden="true" />
                <p className="text-xs font-black uppercase tracking-[0.16em] text-black/50">当前最高挂牌价差</p>
                <p className="mt-3 text-4xl font-black tracking-[-0.05em] tabular-nums sm:text-5xl">
                  {bestGap === null ? '待匹配' : signedMoney(bestGap)}
                </p>
                <p className="mt-3 text-xs font-semibold text-black/55">
                  {trustedBenchmarkCount
                    ? `${trustedBenchmarkCount} 款有可用同规格市场参照`
                    : '当前缓存尚无可用市场参照'}
                </p>
              </div>

              <div className="grid grid-cols-2 gap-2.5">
                <div className="rounded-2xl border border-white/10 bg-white/[0.055] p-4">
                  <p className="text-[10px] font-bold text-white/65">原始公开报价</p>
                  <p className="mt-2 text-2xl font-black tabular-nums">{feed ? rawQuoteCount.toLocaleString('zh-CN') : '—'}</p>
                  <p className="mt-1 text-[10px] text-white/60">{providers.length} 个公开来源合计</p>
                </div>
                <div className="rounded-2xl border border-white/10 bg-white/[0.055] p-4">
                  <p className="text-[10px] font-bold text-white/65">独立规格组</p>
                  <p className="mt-2 text-2xl font-black tabular-nums">{feed ? upstreamRows.length.toLocaleString('zh-CN') : '—'}</p>
                  <p className="mt-1 text-[10px] text-white/60">同标题多供应商已合并</p>
                </div>
                <div className="rounded-2xl border border-white/10 bg-white/[0.055] p-4">
                  <p className="text-[10px] font-bold text-white/65">可信同规格参照</p>
                  <p className="mt-2 text-2xl font-black tabular-nums text-emerald-300">{feed ? trustedBenchmarkCount : '—'}</p>
                  <p className="mt-1 text-[10px] text-white/60">报价有效且详情成交已核验</p>
                </div>
                <div className="rounded-2xl border border-white/10 bg-white/[0.055] p-4">
                  <p className="text-[10px] font-bold text-white/65">待核 / 异常隔离</p>
                  <p className="mt-2 text-2xl font-black tabular-nums text-amber-300">{feed ? pendingBenchmarkCount : '—'}</p>
                  <p className="mt-1 text-[10px] text-white/60">
                    {loadingCount ? `${loadingCount} 项更新中` : `${marketCacheCount} 个关键词快照`}
                  </p>
                </div>
              </div>
            </div>

            <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-white/10 pt-4">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-white/65">
                <span>原始报价最低值 {lowestUpstream === null ? '—' : money(Math.round(lowestUpstream * 100))}（未做同规格核验）</span>
                <span>闲鱼参考样本 {referenceCount}</span>
                <span>更新 {fetchedLabel(feed?.fetched_at)}</span>
              </div>
              <a
                href="#source-audit-title"
                className="inline-flex min-h-10 items-center gap-2 rounded-xl bg-white/[0.07] px-3 text-xs font-bold text-[#FFE815] hover:bg-white/[0.11] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
              >
                查看 {auditSources.length} 个入口结论
                <Waypoints className="h-4 w-4" aria-hidden="true" />
              </a>
            </div>
          </div>
        </section>

        <section className={`${card} overflow-hidden`} aria-labelledby="hot-coverage-title">
          <div className="flex flex-col justify-between gap-4 border-b border-gray-100 p-5 dark:border-gray-700 lg:flex-row lg:items-end">
            <div>
              <div className="flex items-center gap-2">
                <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">MARKET CATALOG</p>
                <Flame className="h-4 w-4 text-amber-500" aria-hidden="true" />
              </div>
              <h3 id="hot-coverage-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white">
                热门搜索与虚拟品类
              </h3>
              <p className="mt-1 max-w-2xl text-xs leading-5 text-gray-500 dark:text-gray-400">
                热词来自卡券雷达公开首页点击量。每个品类先隔离教程、说明页和规格不符报价，再自动对齐闲鱼同规格挂牌。
              </p>
            </div>
            <div className="flex flex-wrap gap-2 text-[10px] font-bold">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 px-2.5 py-1.5 text-gray-700 dark:bg-[#25292E] dark:text-gray-200">
                <Layers3 className="h-3 w-3" aria-hidden="true" />
                {catalogItems.length} 个查询锚点
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-full bg-blue-100 px-2.5 py-1.5 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200">
                <Tag className="h-3 w-3" aria-hidden="true" />
                {categoryCount} 个品类
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 px-2.5 py-1.5 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200">
                <Database className="h-3 w-3" aria-hidden="true" />
                {catalogMarketCount} 个已有闲鱼快照
              </span>
            </div>
          </div>

          {feed?.catalog_summary && (
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-gray-100 bg-gray-50/80 px-5 py-3 text-[11px] text-gray-500 dark:border-gray-700 dark:bg-[#202328] dark:text-gray-400">
              <span className="font-bold text-gray-700 dark:text-gray-200">公开目录快照</span>
              {typeof feed.catalog_summary.site_count === 'number' && (
                <span>{feed.catalog_summary.site_count.toLocaleString('zh-CN')} 个货源站</span>
              )}
              {typeof feed.catalog_summary.product_count === 'number' && (
                <span>{feed.catalog_summary.product_count.toLocaleString('zh-CN')} 件商品</span>
              )}
              {typeof feed.catalog_summary.offer_count === 'number' && (
                <span>{feed.catalog_summary.offer_count.toLocaleString('zh-CN')} 条报价</span>
              )}
              <span className="ml-auto">来源：{feed.catalog_summary.source || '卡券雷达公开首页'}</span>
            </div>
          )}

          <div className="grid gap-3 bg-gray-50/60 p-4 dark:bg-[#17191D] xl:grid-cols-[minmax(300px,0.82fr)_minmax(0,1.18fr)]">
            <div className="grid content-start gap-2 lg:max-h-[760px] lg:overflow-y-auto lg:pr-1">
            {visibleHotSearches.map(({
              keyword,
              category,
              origin,
              quoteCount,
              excludedQuoteCount,
              lowest,
              sourceAlternative,
              marketBenchmark,
              marketLowestComparable,
              marketSpread,
              marketVerified,
              marketResult,
            }, index) => {
              const selected = displayFilter === keyword;
              const tone = categoryTone(category);
              return (
                <article
                  key={`${origin}-${keyword}`}
                  className={`flex flex-col overflow-hidden rounded-2xl border transition-all ${
                    selected
                      ? 'border-[#FFE815] bg-white shadow-[0_0_0_2px_rgba(255,232,21,0.2)] dark:bg-[#202328]'
                      : `${tone.card} hover:border-gray-300 dark:hover:border-gray-600`
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 px-4 pt-3">
                    <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${tone.chip}`}>
                      {category}
                    </span>
                    <span className="text-[10px] font-black tabular-nums text-gray-400">
                      {origin === 'hot' ? `HOT ${String(index + 1).padStart(2, '0')}` : 'CATEGORY'}
                    </span>
                  </div>
                  <button
                    type="button"
                    aria-pressed={selected}
                    onClick={() => {
                      setDisplayFilter(selected ? '' : keyword);
                      setShowAll(false);
                    }}
                    className="flex min-h-12 flex-1 items-center px-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#FFE815]"
                  >
                    <span className="block min-w-0">
                      <span className="block truncate text-base font-black tracking-[-0.02em] text-gray-950 dark:text-white">
                        {keyword}
                      </span>
                      <span className="mt-1 block text-[11px] font-medium text-gray-500 dark:text-gray-400">
                        {quoteCount
                          ? `${quoteCount} 条商品报价${excludedQuoteCount ? ` · 排除 ${excludedQuoteCount} 条内容/说明` : ''}`
                          : '当前只有教程、说明或运营服务，未选择最低价'}
                      </span>
                    </span>
                  </button>
                  <div className="flex items-end justify-between gap-3 border-t border-gray-200/70 px-4 py-2.5 dark:border-gray-700">
                    <div className="min-w-0 flex-1">
                      <p className="line-clamp-2 text-[11px] font-bold leading-4 text-gray-700 dark:text-gray-200" title={lowest?.title}>
                        {lowest?.title || '暂无可信商品报价'}
                      </p>
                      <p className="mt-1 text-[9px] font-bold uppercase tracking-[0.12em] text-gray-400">
                        {lowest?.link_type === 'product' ? '商品直链最低价' : '来源页最低报价'}
                      </p>
                      <p className="mt-0.5 truncate text-[10px] text-gray-500 dark:text-gray-400">
                        {lowest?.supplier || lowest?.source_label || '暂无报价'}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] font-bold">
                        <span className="text-blue-700 dark:text-blue-300">
                          闲鱼 {marketBenchmark?.price === null || marketBenchmark?.price === undefined
                            ? '待匹配'
                            : money(Math.round(marketBenchmark.price * 100))}
                        </span>
                        <span className={
                          (marketSpread?.gapCents ?? 0) > 0
                            ? 'text-emerald-700 dark:text-emerald-300'
                            : (marketSpread?.gapCents ?? 0) < 0
                              ? 'text-rose-700 dark:text-rose-300'
                              : 'text-amber-700 dark:text-amber-300'
                        }>
                          {marketSpread
                            ? `${marketVerified ? '价差' : '待核价差'} ${signedMoney(marketSpread.gapCents)}`
                            : marketResult?.loading
                              ? '闲鱼读取中'
                              : '价差待匹配'}
                        </span>
                        {marketLowestComparable
                          && marketBenchmark
                          && marketLowestComparable.itemId !== marketBenchmark.itemId && (
                          <a
                            href={marketLowestComparable.itemUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-gray-500 underline-offset-2 hover:underline dark:text-gray-400"
                          >
                            最低同规格 {money(Math.round(marketLowestComparable.price! * 100))}
                          </a>
                        )}
                      </div>
                      {sourceAlternative && (
                        <a
                          href={sourceAlternative.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="mt-1 inline-flex text-[10px] font-bold text-blue-700 underline-offset-2 hover:underline dark:text-blue-300"
                        >
                          来源页另见 {money(Math.round(sourceAlternative.price * 100))}
                        </a>
                      )}
                    </div>
                    {lowest ? (
                      <a
                        href={lowest.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex min-h-10 items-center gap-1 rounded-xl bg-[#191B1F] px-3 text-sm font-black tabular-nums text-[#FFE815] hover:bg-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
                        aria-label={`${quoteLinkLabel(lowest)}：${keyword} ${money(Math.round(lowest.price * 100))}`}
                      >
                        {money(Math.round(lowest.price * 100))}
                        <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                      </a>
                    ) : (
                      <span className="min-h-10 rounded-xl bg-gray-100 px-3 py-2.5 text-sm font-black text-gray-400 dark:bg-[#25292E]">
                        —
                      </span>
                    )}
                  </div>
                </article>
              );
            })}
            {!visibleHotSearches.length && (
              <div className="col-span-full flex min-h-32 items-center justify-center text-sm text-gray-400">
                正在读取公开热词与品类锚点
              </div>
            )}
            </div>

            {selectedHot && (
              <aside
                className="relative overflow-hidden rounded-[28px] bg-[#111419] p-5 text-white shadow-[0_22px_70px_rgba(17,20,25,0.22)] ring-1 ring-white/10 sm:p-6 xl:sticky xl:top-4 xl:self-start"
                aria-labelledby="selected-hot-title"
              >
                <div className="pointer-events-none absolute -right-20 -top-24 h-64 w-64 rounded-full bg-[#FFE815]/10 blur-3xl" aria-hidden="true" />
                <div className="pointer-events-none absolute -bottom-28 -left-20 h-56 w-56 rounded-full bg-blue-500/10 blur-3xl" aria-hidden="true" />
                <div className="relative">
                  <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${categoryTone(selectedHot.category).chip}`}>
                          {selectedHot.category}
                        </span>
                        <span className="rounded-full border border-white/10 bg-white/[0.06] px-2.5 py-1 text-[10px] font-bold text-white/65">
                          {selectedHot.origin === 'hot' ? '公开热词' : '品类锚点'}
                        </span>
                      </div>
                      <h4 id="selected-hot-title" className="mt-3 text-3xl font-black tracking-[-0.045em]">
                        {selectedHot.keyword}
                      </h4>
                      <p className="mt-1 text-xs leading-5 text-white/50">
                        {selectedHot.quoteCount} 条可信商品报价
                        {selectedHot.excludedQuoteCount > 0
                          ? `，已隔离 ${selectedHot.excludedQuoteCount} 条教程、说明、规格或面值异常报价`
                          : '，公开报价已完成商品形态过滤'}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button
                        type="button"
                        onClick={() => {
                          if (selectedHot.lowest) {
                            void refreshMarket(selectedHot.lowest, false, selectedHot.keyword);
                          }
                        }}
                        disabled={!selectedHot.lowest || Boolean(marketRefresh)}
                        className="inline-flex min-h-10 items-center gap-1.5 rounded-xl bg-[#FFE815] px-3 text-xs font-black text-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        <RefreshCw className={`h-3.5 w-3.5 ${
                          marketRefresh === selectedHot.lowest?.title ? 'animate-spin' : ''
                        }`} aria-hidden="true" />
                        刷新闲鱼价
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setDisplayFilter(selectedHot.keyword);
                          setShowAll(false);
                        }}
                        className="inline-flex min-h-10 items-center rounded-xl border border-white/10 bg-white/[0.06] px-3 text-xs font-bold text-white/75 hover:bg-white/[0.1] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
                      >
                        看实时明细
                      </button>
                    </div>
                  </div>

                  <div className="mt-6 grid gap-2.5 sm:grid-cols-3">
                    <div className="rounded-2xl border border-white/10 bg-white/[0.055] p-4">
                      <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-white/45">最低上游价</p>
                      {selectedHot.lowest ? (
                        <a
                          href={selectedHot.lowest.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="mt-2 inline-flex items-center gap-1 text-2xl font-black tabular-nums text-[#FFE815] hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
                        >
                          {money(Math.round(selectedHot.lowest.price * 100))}
                          <ExternalLink className="h-4 w-4" aria-hidden="true" />
                        </a>
                      ) : (
                        <p className="mt-2 text-xl font-black text-white/40">暂无</p>
                      )}
                      <p className="mt-1 truncate text-[10px] text-white/45">
                        {selectedHot.lowest?.supplier || selectedHot.lowest?.source_label || '暂无可信来源'}
                      </p>
                    </div>

                    <div className="rounded-2xl border border-white/10 bg-white/[0.055] p-4">
                      <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-white/45">同规格闲鱼价</p>
                      {selectedHot.marketBenchmark ? (
                        <a
                          href={selectedHot.marketBenchmark.itemUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="mt-2 inline-flex items-center gap-1 text-2xl font-black tabular-nums text-white hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
                        >
                          {selectedHot.marketBenchmark.price === null
                            ? '—'
                            : money(Math.round(selectedHot.marketBenchmark.price * 100))}
                          <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
                        </a>
                      ) : (
                        <p className="mt-2 text-xl font-black text-white/40">
                          {selectedHot.marketResult?.loading ? '读取中' : '待匹配'}
                        </p>
                      )}
                      <p className="mt-1 truncate text-[10px] text-white/45">
                        {selectedHot.marketBenchmark?.title
                          || selectedHot.marketResult?.error
                          || '按品牌、规格、时长与交付方式自动匹配'}
                      </p>
                    </div>

                    <div className={`rounded-2xl border p-4 ${
                      (selectedHot.marketSpread?.gapCents ?? 0) > 0
                        ? 'border-emerald-400/20 bg-emerald-400/10'
                        : (selectedHot.marketSpread?.gapCents ?? 0) < 0
                          ? 'border-rose-400/20 bg-rose-400/10'
                          : 'border-white/10 bg-white/[0.055]'
                    }`}>
                      <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-white/45">自动价差</p>
                      <p className={`mt-2 text-2xl font-black tabular-nums ${
                        (selectedHot.marketSpread?.gapCents ?? 0) > 0
                          ? 'text-emerald-300'
                          : (selectedHot.marketSpread?.gapCents ?? 0) < 0
                            ? 'text-rose-300'
                            : 'text-white/45'
                      }`}>
                        {selectedHot.marketSpread
                          ? signedMoney(selectedHot.marketSpread.gapCents)
                          : selectedHot.marketResult?.loading
                            ? '读取中'
                            : '待匹配'}
                      </p>
                      <p className="mt-1 text-[10px] text-white/45">
                        {selectedHot.marketVerified
                          ? '已通过成交或想要信号校验'
                          : selectedHot.marketSpread
                            ? '同规格价格参照，成交仍待核'
                            : '不会把教程或日卡混入月卡'}
                      </p>
                    </div>
                  </div>

                  <div className="mt-3 rounded-2xl border border-white/10 bg-black/20 p-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <p className="text-[10px] font-bold uppercase tracking-[0.14em] text-white/40">当前最低商品</p>
                        <p className="mt-1 line-clamp-2 text-sm font-bold leading-6 text-white/90">
                          {selectedHot.lowest?.title || '没有通过商品形态与规格过滤的上游报价'}
                        </p>
                        <p className="mt-1 text-[10px] text-white/45">
                          {selectedHot.lowest
                            ? `${quoteLinkLabel(selectedHot.lowest)} · ${selectedHot.lowest.source_label || selectedHot.lowest.source || '公开来源'}`
                            : '教程、说明页、运营服务、面值严重不符和泛关键词内容均已隔离'}
                        </p>
                      </div>
                      {selectedHot.lowest && (
                        <a
                          href={selectedHot.lowest.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex min-h-10 shrink-0 items-center gap-1.5 rounded-xl bg-white px-3 text-xs font-black text-[#111419] hover:bg-white/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
                        >
                          打开最低价
                          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                        </a>
                      )}
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-white/10 pt-3 text-[10px] text-white/45">
                      <span className="inline-flex items-center gap-1.5">
                        <ShieldCheck className="h-3.5 w-3.5 text-[#FFE815]" aria-hidden="true" />
                        {selectedHot.marketComparableCount} 条同规格闲鱼样本
                      </span>
                      <span className="inline-flex items-center gap-1.5">
                        <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
                        闲鱼快照 {fetchedLabel(selectedHot.marketFetchedAt)}
                      </span>
                      {selectedHot.marketLowestComparable && (
                        <a
                          href={selectedHot.marketLowestComparable.itemUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 font-bold text-blue-300 hover:underline"
                        >
                          最低同规格挂牌 {money(Math.round(selectedHot.marketLowestComparable.price! * 100))}
                          <ExternalLink className="h-3 w-3" aria-hidden="true" />
                        </a>
                      )}
                    </div>
                    {selectedHot.marketEvidence?.reasons[0] && (
                      <p className="mt-2 line-clamp-2 text-[10px] leading-5 text-white/40">
                        {selectedHot.marketEvidence.reasons[0]}
                      </p>
                    )}
                  </div>
                </div>
              </aside>
            )}
          </div>

          <div className="flex flex-col justify-between gap-3 border-t border-gray-100 px-4 py-3 dark:border-gray-700 sm:flex-row sm:items-center">
            <p className="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
              <Clock3 className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              热门款每 5 分钟汇总一次公开报价；来源页更新时间仍以卡片内为准。
            </p>
            <div className="flex flex-wrap gap-2">
              {hotSearches.some(item => item.origin === 'category') && (
                <button
                  type="button"
                  onClick={() => setShowAllCategories(value => !value)}
                  className="min-h-10 rounded-xl border border-gray-200 bg-white px-3 text-xs font-bold text-gray-700 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:border-gray-700 dark:bg-[#25292E] dark:text-gray-200 dark:hover:bg-[#2D3238]"
                >
                  {showAllCategories ? '收起品类锚点' : `展开 ${hotSearches.filter(item => item.origin === 'category').length} 个品类锚点`}
                </button>
              )}
              {displayFilter && (
                <button
                  type="button"
                  onClick={() => {
                    setDisplayFilter('');
                    setShowAll(false);
                  }}
                  className="min-h-10 rounded-xl bg-[#FFE815] px-3 text-xs font-bold text-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black"
                >
                  清除「{displayFilter}」筛选
                </button>
              )}
            </div>
          </div>
        </section>
      </div>

      <section className="space-y-3" aria-labelledby="profit-rows-title">
        <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">
                POSITIVE MARGIN
              </p>
              <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-[10px] font-bold text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200">
                {profitableRows.length} 款正价差
              </span>
            </div>
            <h3 id="profit-rows-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white">
              可赚商品
            </h3>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-gray-500 dark:text-gray-400">
              上游报价和同规格挂牌在有效期内，且该挂牌有详情成交记录时，正价差才会出现在这里。
              净利待核，需确认平台费、推广、履约和售后成本。
              {duplicateProfitMatches > 0 && ` 同一闲鱼挂牌的 ${duplicateProfitMatches} 条重复上游标题已自动合并。`}
            </p>
          </div>
          {bestGap !== null && bestGap > 0 && (
            <div className="rounded-xl bg-[#191B1F] px-4 py-3 text-white">
              <p className="text-[9px] font-bold uppercase tracking-[0.14em] text-white/60">最高挂牌价差</p>
              <p className="mt-1 text-xl font-black tabular-nums text-[#FFE815]">{signedMoney(bestGap)}</p>
            </div>
          )}
        </div>

        {!feed ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="正在读取可赚商品">
            {[0, 1, 2].map(index => (
              <div key={index} className={`${card} h-56 animate-pulse p-5`}>
                <div className="h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-800" />
                <div className="mt-5 h-16 rounded-xl bg-gray-100 dark:bg-gray-800" />
                <div className="mt-4 h-8 w-1/2 rounded bg-gray-100 dark:bg-gray-800" />
              </div>
            ))}
          </div>
        ) : visibleProfitableRows.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {visibleProfitableRows.map(({
              quote,
              verified,
              evidence,
              gapCents,
              marginPercent,
              sold,
              want,
              browse,
              marketFetchedAt,
            }, index) => (
              <article
                key={`${verified.itemId}-profit`}
                className="group relative overflow-hidden rounded-2xl border border-emerald-200 bg-white p-5 shadow-[0_12px_30px_rgba(15,23,42,0.05)] dark:border-emerald-900/60 dark:bg-[#1B1E22]"
              >
                <div className="pointer-events-none absolute -right-12 -top-16 h-36 w-36 rounded-full bg-emerald-100/70 blur-2xl dark:bg-emerald-950/30" />
                <div className="relative">
                  <div className="flex items-start justify-between gap-3">
                    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-bold ${marketConfidenceClass(evidence.confidence)}`}>
                      <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                      {MARKET_CONFIDENCE_LABELS[evidence.confidence]}
                    </span>
                    <span className="text-[10px] font-black tabular-nums text-gray-400">
                      PROFIT {String(index + 1).padStart(2, '0')}
                    </span>
                  </div>

                  <h4 className="mt-4 line-clamp-2 min-h-10 text-sm font-black leading-5 text-gray-950 dark:text-white">
                    {quote.title}
                  </h4>
                  <p className="mt-1 truncate text-[10px] font-semibold text-gray-500 dark:text-gray-400">
                    {quote.supplier || quote.source_label || '公开货源'}
                    {' · '}
                    {quote.source_label || quote.source || '上游'}
                  </p>

                  <div className="mt-4 grid grid-cols-3 divide-x divide-gray-200 rounded-xl border border-gray-200 bg-gray-50/80 py-3 dark:divide-gray-700 dark:border-gray-700 dark:bg-[#202328]">
                    <a
                      href={quote.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="px-3 text-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#FFE815]"
                      aria-label={`${quoteLinkLabel(quote)}：${money(Math.round(quote.price * 100))}`}
                    >
                      <span className="block text-[9px] font-bold text-gray-400">上游最低</span>
                      <span className="mt-1 inline-flex items-center gap-0.5 text-base font-black tabular-nums text-blue-700 dark:text-blue-300">
                        {money(Math.round(quote.price * 100))}
                        <ExternalLink className="h-3 w-3" aria-hidden="true" />
                      </span>
                    </a>
                    <a
                      href={verified.itemUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="px-3 text-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#FFE815]"
                      title={verified.title}
                    >
                      <span className="block text-[9px] font-bold text-gray-400">闲鱼挂牌价</span>
                      <span className="mt-1 inline-flex items-center gap-0.5 text-base font-black tabular-nums text-gray-950 dark:text-white">
                        {money(Math.round(verified.price! * 100))}
                        <ExternalLink className="h-3 w-3" aria-hidden="true" />
                      </span>
                    </a>
                    <div className="px-3 text-center">
                      <span className="block text-[9px] font-bold text-gray-400">扣费前价差</span>
                      <span className="mt-1 block text-base font-black tabular-nums text-emerald-700 dark:text-emerald-300">
                        {signedMoney(gapCents)}
                      </span>
                    </div>
                  </div>

                  <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                    <p className="text-[10px] font-bold text-emerald-700 dark:text-emerald-300">
                      {marginPercent === null ? '毛利率待核' : `扣费前毛利率 ${marginPercent.toFixed(1)}%`}
                    </p>
                    <p className="text-[10px] tabular-nums text-gray-400">
                      详情成交 {sold ?? '待核'} · 想要 {want ?? '待核'} · 浏览 {browse ?? '待核'}
                    </p>
                  </div>
                  <p className="mt-3 border-t border-gray-100 pt-3 text-[9px] leading-4 text-gray-400 dark:border-gray-700">
                    {evidence.reasons[0]}
                    {' · '}
                    {evidence.validCount} 条同规格有效样本
                    {evidence.benchmarkBasis === 'activity' ? ' · 按成交/想要/浏览择优' : ''}
                    {' · '}
                    上游采集 {fetchedLabel(quote.fetched_at)}
                    {' · '}
                    闲鱼快照 {fetchedLabel(marketFetchedAt)}
                  </p>
                  <button
                    type="button"
                    onClick={() => openFulfillmentDialog(quote, verified)}
                    className="mt-3 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-xl ios-btn-primary px-4 text-xs font-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#111315] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 dark:focus-visible:ring-[#FFE815] dark:focus-visible:ring-offset-[#1B1E22]"
                  >
                    <Zap className="h-4 w-4" aria-hidden="true" />
                    选择履约并发布
                  </button>
                  <p className="mt-2 text-center text-[9px] leading-4 text-gray-400">
                    创建持久任务。供应商自动采购未接通，只有后端返回 delivery_ready 才表示履约就绪。
                  </p>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <div className={`${card} flex min-h-44 flex-col items-center justify-center px-6 text-center`}>
            <CircleDollarSign className="h-8 w-8 text-gray-300 dark:text-gray-600" aria-hidden="true" />
            <p className="mt-3 text-sm font-black text-gray-950 dark:text-white">当前没有正价差商品</p>
            <p className="mt-1 max-w-lg text-xs leading-5 text-gray-500 dark:text-gray-400">
              仍可在比价台核对同规格挂牌、成交记录和上游报价时间。
            </p>
          </div>
        )}

        {profitableRows.length > 12 && (
          <div className="flex justify-center">
            <button
              type="button"
              onClick={() => setShowAllProfitRows(value => !value)}
              className="min-h-10 rounded-xl border border-gray-200 bg-white px-4 text-xs font-bold text-gray-700 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:border-gray-700 dark:bg-[#25292E] dark:text-gray-200 dark:hover:bg-[#2D3238]"
            >
              {showAllProfitRows ? '收起可赚商品' : `查看全部 ${profitableRows.length} 款`}
            </button>
          </div>
        )}
      </section>

      <section className={`${card} overflow-hidden`} aria-labelledby="provider-network-title">
        <div className="flex flex-col justify-between gap-3 border-b border-gray-100 p-5 dark:border-gray-700 lg:flex-row lg:items-center">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 id="provider-network-title" className="text-lg font-black text-gray-950 dark:text-white">实时货源网络</h3>
              <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${
                providers.length && liveProviderCount === providers.length
                  ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200'
                  : 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200'
              }`}>
                {providers.length ? `${liveProviderCount}/${providers.length} 在线` : '连接中'}
              </span>
            </div>
            <p className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
              接口、网页抓取和受限入口分别标记。单个来源失败时保留最近成功数据。
            </p>
          </div>
          <p className="text-xs font-bold tabular-nums text-gray-500 dark:text-gray-400">
            {providerQuoteCount.toLocaleString('zh-CN')} 条报价记录
          </p>
        </div>

        <div className="grid gap-px bg-gray-100 dark:bg-gray-700 md:grid-cols-2 xl:grid-cols-4">
          {providers.length ? providers.map(provider => (
            <article key={provider.id} className="bg-white p-4 dark:bg-[#1B1E22]">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-extrabold text-gray-950 dark:text-white">{provider.name}</p>
                  <p
                    className="mt-1 line-clamp-2 text-[11px] leading-5 text-gray-500 dark:text-gray-400"
                    title={provider.error || provider.status || undefined}
                  >
                    {provider.ok
                      ? `${provider.quote_count ?? 0} 条报价`
                      : provider.error || '本次没有取得可用报价'}
                  </p>
                </div>
                <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-[10px] font-bold ${
                  provider.ok
                    ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200'
                    : 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200'
                }`}>
                  <Radio className="h-3 w-3" aria-hidden="true" />
                  {provider.ok ? '在线' : '异常'}
                </span>
              </div>
              <p className="mt-4 truncate text-[10px] tabular-nums text-gray-400">
                {provider.fetched_at ? `更新 ${fetchedLabel(provider.fetched_at)}` : '等待首次成功'}
              </p>
            </article>
          )) : (
            <div className="col-span-full flex min-h-24 items-center justify-center bg-white px-4 text-sm text-gray-400 dark:bg-[#1B1E22]">
              <RefreshCw className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
              正在读取各来源状态
            </div>
          )}
        </div>
      </section>

      <section className={`${card} overflow-hidden`} aria-labelledby="live-compare-title">
        <div className="bg-[#191B1F] p-4 text-white sm:p-5">
          <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-center">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-[10px] font-black uppercase tracking-[0.2em] text-white/60">PRICE LEDGER</p>
                <span className="inline-flex items-center gap-1 rounded-full bg-[#FFE815]/15 px-2.5 py-1 text-[10px] font-bold text-[#FFE815]">
                  <GitCompareArrows className="h-3 w-3" aria-hidden="true" />
                  同规格与可信度校验后计算
                </span>
              </div>
              <h3 id="live-compare-title" className="mt-1 text-xl font-black">公开报价竞速台</h3>
              <p className="mt-1 text-xs leading-5 text-white/65">
                上游报价有效期 10 分钟，闲鱼挂牌快照有效期 24 小时。点开两侧价格可核对商品规格。
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex min-h-10 items-center rounded-xl border border-white/10 bg-white/[0.06] px-3 text-[10px] font-bold tabular-nums text-white/70">
                {filteredRows.length} 个规格组
              </span>
              <button
                type="button"
                aria-expanded={liveCompareOpen}
                aria-controls="live-compare-body"
                onClick={() => setLiveCompareOpen(value => !value)}
                className="inline-flex min-h-10 items-center gap-2 rounded-xl bg-[#FFE815] px-3 text-xs font-black text-black hover:bg-[#FFF06A] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[#191B1F]"
              >
                {liveCompareOpen ? '收起实时比价台' : '展开实时比价台'}
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${liveCompareOpen ? 'rotate-180' : ''}`}
                  aria-hidden="true"
                />
              </button>
            </div>
          </div>

          {liveCompareOpen && (
            <div className="mt-4 flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
              <label className="relative block w-full">
                <span className="sr-only">筛选已读取的报价</span>
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                <input
                  value={displayFilter}
                  onChange={event => setDisplayFilter(event.target.value)}
                  placeholder="搜索商品、品牌或热词"
                  className="min-h-11 w-full rounded-xl border border-white/10 bg-white/[0.07] pl-9 pr-3 text-sm text-white outline-none transition-colors placeholder:text-white/60 focus:border-[#FFE815]/70 focus:ring-2 focus:ring-[#FFE815]/15"
                />
              </label>
              <div className="flex items-center justify-between gap-3 lg:min-w-[230px]">
                <label className="inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-xl border border-white/10 bg-white/[0.06] px-3 text-xs font-semibold text-white/75 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-[#FFE815]">
                  <input
                    type="checkbox"
                    checked={onlyCompared}
                    onChange={event => setOnlyCompared(event.target.checked)}
                    className="h-4 w-4 accent-[#FFE815]"
                  />
                  仅看已找到同规格样本
                </label>
                <span className="text-[10px] tabular-nums text-white/60">
                  {visible.length} / {filteredRows.length}
                </span>
              </div>
            </div>
          )}
        </div>

        {liveCompareOpen && (
          <div id="live-compare-body">
            <div className="space-y-3 bg-[#F3F4F6] p-3 dark:bg-[#17191D] sm:p-4">
          {feed ? visible.map(({
            quote,
            result,
            evidence,
            benchmark,
            verified,
            directReferences,
            cachedFetchedAt,
            cookieId,
            sold,
            want,
            browse,
          }) => {
            const suspicious = suspiciousAssessments(evidence.assessments);
            const displayAssessment = evidence.assessments.find(item =>
              item.listing.itemId === benchmark?.itemId,
            ) ?? suspicious[0];
            const displayListing = displayAssessment?.listing ?? null;
            const suspiciousLowPrice = suspicious.find(item =>
              item.reasons.some(reason => reason.startsWith('异常低价：')),
            );
            const isPending = Boolean(benchmark) && !verified;
            const isFlagged = Boolean(suspicious.length) && !verified;
            const gap = verified?.price !== null && verified?.price !== undefined
              ? Math.round(verified.price * 100) - Math.round(quote.price * 100)
              : null;
            const isPositive = gap !== null && gap > 0;
            const isNegative = gap !== null && gap < 0;
            const gapLabel = gap !== null
              ? signedMoney(gap)
              : benchmark
                ? '待核'
                : isFlagged
                  ? '异常隔离'
                  : '待匹配';
            const stopReasons = displayAssessment && (
              !displayAssessment.comparable
              || !displayAssessment.priceSane
              || !displayAssessment.sellable
            )
              ? prioritizeRejectionReasons(displayAssessment.reasons)
              : evidence.reasons;
            const coverage = quoteGroups.get(listingKey(quote.title));
            return (
              <article
                key={`${quote.title}-${quote.url}`}
                className={`overflow-hidden rounded-2xl border bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:bg-[#1B1E22] ${
                  isPositive
                    ? 'border-emerald-200 dark:border-emerald-900/60'
                    : isNegative
                      ? 'border-rose-200 dark:border-rose-900/60'
                      : isPending || isFlagged
                        ? 'border-amber-200 dark:border-amber-900/60'
                        : 'border-gray-200 dark:border-gray-700'
                }`}
              >
                <div className="grid lg:grid-cols-[minmax(0,1.25fr)_minmax(170px,0.55fr)_minmax(170px,0.55fr)_minmax(190px,0.62fr)]">
                  <div className="min-w-0 border-b border-gray-100 p-4 lg:border-b-0 lg:border-r dark:border-gray-700 sm:p-5">
                    <div className="flex flex-wrap items-center gap-2">
                      <h4 className="break-words text-sm font-black leading-5 text-gray-950 dark:text-white">
                        {quote.title}
                      </h4>
                      {verified ? (
                        <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-[10px] font-bold ${marketConfidenceClass(evidence.confidence)}`}>
                          <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                          {MARKET_CONFIDENCE_LABELS[evidence.confidence]}
                        </span>
                      ) : result?.loading ? (
                        <span className="rounded-full bg-blue-100 px-2 py-1 text-[10px] font-bold text-blue-800 dark:bg-blue-950/50 dark:text-blue-200">
                          读取中
                        </span>
                      ) : benchmark ? (
                        <span className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-[10px] font-bold ${marketConfidenceClass('low')}`}>
                          <TriangleAlert className="h-3 w-3" aria-hidden="true" />
                          低互动待核
                        </span>
                      ) : isFlagged ? (
                        <span className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-[10px] font-bold ${marketConfidenceClass('invalid')}`}>
                          <TriangleAlert className="h-3 w-3" aria-hidden="true" />
                          异常隔离
                        </span>
                      ) : (
                        <span className="rounded-full bg-gray-100 px-2 py-1 text-[10px] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                          待找到同规格样本
                        </span>
                      )}
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                      <span className="inline-flex items-center gap-1">
                        <Store className="h-3.5 w-3.5" aria-hidden="true" />
                        {quote.supplier || '公开货源'}
                      </span>
                      {coverage && (
                        <span>{coverage.suppliers.size} 家 · {coverage.sources.size} 个平台</span>
                      )}
                      {quote.source_label && quote.source_url && (
                        <a
                          href={quote.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex min-h-8 items-center gap-1 rounded-lg bg-gray-100 px-2 font-bold text-gray-700 hover:text-gray-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:bg-[#25292E] dark:text-gray-300 dark:hover:text-white"
                        >
                          {quote.source_label}
                          <ExternalLink className="h-3 w-3" aria-hidden="true" />
                        </a>
                      )}
                    </div>
                    {(cachedFetchedAt || result?.fetchedAt) && (
                      <p className="mt-3 text-[10px] tabular-nums text-gray-400">
                        闲鱼快照 {fetchedLabel(cachedFetchedAt || result?.fetchedAt)}
                      </p>
                    )}
                    {cookieId && (
                      <p className="mt-1 text-[10px] text-gray-400">
                        本次查询账号 {cookieId}
                      </p>
                    )}
                  </div>

                  <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3 dark:border-gray-700 lg:block lg:border-b-0 lg:border-r lg:px-5 lg:py-5">
                    <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-gray-400">上游最低</p>
                    <div className="text-right lg:mt-2 lg:text-left">
                      <a
                        href={quote.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex min-h-9 items-center gap-1 text-xl font-black tabular-nums text-blue-700 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:text-blue-300"
                      >
                        {money(Math.round(quote.price * 100))}
                        <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
                      </a>
                      <a
                        href={quote.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mt-1 inline-flex min-h-8 items-center text-[10px] font-bold text-gray-500 hover:text-blue-700 dark:text-gray-400 dark:hover:text-blue-300"
                      >
                        打开最低价
                      </a>
                    </div>
                  </div>

                  <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3 dark:border-gray-700 lg:block lg:border-b-0 lg:border-r lg:px-5 lg:py-5">
                    <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-gray-400">
                      {verified
                        ? evidence.confidence === 'high'
                          ? '闲鱼高互动同规格参照'
                          : '闲鱼同规格价格参照'
                        : displayListing
                          ? '闲鱼异常或待核挂牌'
                          : '闲鱼市场参照'}
                    </p>
                    <div className="text-right lg:mt-2 lg:text-left">
                      {displayListing && displayListing.price !== null ? (
                        <>
                          <a
                            href={displayListing.itemUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex min-h-9 items-center gap-1 text-xl font-black tabular-nums text-gray-950 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:text-white"
                            title={displayListing.title}
                          >
                            {money(Math.round(displayListing.price * 100))}
                            <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
                          </a>
                          <p
                            className="mt-1 line-clamp-2 break-words text-[10px] leading-4 text-gray-600 dark:text-gray-300"
                            title={displayListing.title}
                          >
                            {displayListing.title}
                          </p>
                          <p className={`mt-1 text-[10px] font-semibold ${
                            verified
                              ? 'text-emerald-700 dark:text-emerald-300'
                              : 'text-amber-700 dark:text-amber-300'
                          }`}>
                            {verified
                              ? `${evidence.validCount} 条同规格有效样本${
                                evidence.benchmarkBasis === 'activity'
                                  ? '，按成交、想要、浏览择优'
                                  : '，采用价格中位参照'
                              }`
                              : stopReasons[0] || '低互动或异常价差，未进入利润计算'}
                          </p>
                          {!verified && stopReasons.length > 1 && (
                            <p className="mt-1 text-[10px] leading-4 text-gray-400">
                              {stopReasons.slice(1, 3).join('；')}
                            </p>
                          )}
                          <a
                            href={displayListing.itemUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="mt-1 inline-flex min-h-8 items-center text-[10px] font-bold text-amber-700 hover:underline dark:text-amber-300"
                          >
                            {verified ? '打开参照挂牌' : '核对该挂牌'}
                          </a>
                          {suspiciousLowPrice
                            && suspiciousLowPrice.listing.itemId !== displayListing.itemId && (
                            <a
                              href={suspiciousLowPrice.listing.itemUrl}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="mt-1 flex min-h-9 items-center gap-1 rounded-lg border border-amber-200 bg-amber-50 px-2 text-[10px] font-bold text-amber-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200"
                              title={suspiciousLowPrice.listing.title}
                            >
                              <TriangleAlert className="h-3 w-3 shrink-0" aria-hidden="true" />
                              异常低价 {suspiciousLowPrice.listing.price === null
                                ? '价格未知'
                                : money(Math.round(suspiciousLowPrice.listing.price * 100))}，核对该挂牌
                              <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" />
                            </a>
                          )}
                        </>
                      ) : result?.loading ? (
                        <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-gray-400">
                          <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                          读取中
                        </span>
                      ) : (
                        <>
                          <p className="text-sm font-black text-gray-500 dark:text-gray-300">暂无匹配</p>
                          <a
                            href={marketSearchUrl(quote.title)}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="mt-1 inline-flex min-h-8 items-center text-[10px] font-bold text-blue-700 hover:underline dark:text-blue-300"
                          >
                            去闲鱼核对
                          </a>
                        </>
                      )}
                    </div>
                  </div>

                  <div className={`p-4 lg:p-5 ${
                    isPositive
                      ? 'bg-[#173B2E]'
                      : isNegative
                        ? 'bg-[#3C2025]'
                        : isPending || isFlagged
                          ? 'bg-[#3B2F19]'
                          : 'bg-[#191B1F]'
                  } text-white`}>
                    <div className="flex items-start justify-between gap-3">
                      <div>
                  <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-white/65">
                  {verified ? '同规格可信价差' : '价差状态'}
                        </p>
                        <p className={`mt-2 flex items-center gap-1 text-2xl font-black tabular-nums ${
                          isPositive
                            ? 'text-emerald-300'
                            : isNegative
                              ? 'text-rose-300'
                              : isPending || isFlagged
                                ? 'text-amber-200'
                                : 'text-white/65'
                        }`}>
                          {gapLabel}
                          {gap !== null && (isPositive
                            ? <ArrowUpRight className="h-5 w-5" aria-hidden="true" />
                            : isNegative
                              ? <ArrowDownRight className="h-5 w-5" aria-hidden="true" />
                              : null)}
                        </p>
                      </div>
                      <CircleDollarSign className={`h-5 w-5 shrink-0 ${isPositive ? 'text-emerald-300/60' : isNegative ? 'text-rose-300/60' : 'text-white/20'}`} aria-hidden="true" />
                    </div>
                    <p className="mt-3 text-[10px] leading-5 text-white/60">
                      参照成交 {sold ?? '未核验'} · 想要 {want ?? '未核验'} · 浏览 {browse ?? '未核验'}
                    </p>
                    {!verified && stopReasons[0] && (
                      <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-amber-100/70" title={stopReasons.join('；')}>
                        {stopReasons[0]}
                      </p>
                    )}
                    <button
                      type="button"
                      onClick={() => { void refreshMarket(quote); }}
                      disabled={Boolean(marketRefresh)}
                      className="mt-3 inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.07] px-2.5 text-[11px] font-bold text-white/75 transition-colors hover:bg-white/[0.12] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] disabled:opacity-50"
                    >
                      <RefreshCw className={`h-3 w-3 ${marketRefresh === quote.title ? 'animate-spin' : ''}`} aria-hidden="true" />
                      {marketRefresh === quote.title ? '更新中' : '更新此款'}
                    </button>
                  </div>
                </div>

                {result?.error && (
                  <div role="status" className="border-t border-amber-100 bg-amber-50 px-4 py-2.5 text-xs font-medium text-amber-800 dark:border-amber-900/40 dark:bg-[#342F1F] dark:text-amber-200">
                    {result.error}
                  </div>
                )}

                {directReferences.length > 0 && (
                  <details className="group border-t border-gray-100 bg-gray-50/80 dark:border-gray-700 dark:bg-[#202328]">
                    <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-4 text-xs font-bold text-gray-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#FFE815] dark:text-gray-300">
                      <span>
                        {directReferences.length} 条挂牌的品牌组合、时长或交付方式不同，不参与价差
                      </span>
                      <ChevronDown className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180" aria-hidden="true" />
                    </summary>
                    <div className="space-y-2 px-4 pb-4">
                      {directReferences.slice(0, 3).map(listing => (
                        <a
                          key={listing.itemId}
                          href={listing.itemUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="flex min-h-12 items-center justify-between gap-4 rounded-xl border border-gray-200 bg-white px-3 py-2.5 hover:border-gray-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:border-gray-700 dark:bg-[#1B1E22] dark:hover:border-gray-600"
                        >
                          <span className="line-clamp-2 min-w-0 text-xs font-semibold leading-5 text-gray-700 dark:text-gray-200">
                            {listing.title}
                          </span>
                          <span className="shrink-0 text-sm font-black tabular-nums text-gray-950 dark:text-white">
                            {money(Math.round((listing.price ?? 0) * 100))}
                          </span>
                        </a>
                      ))}
                    </div>
                  </details>
                )}
              </article>
            );
          }) : (
            <div
              className="grid min-h-56 gap-3 rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-[#1B1E22] sm:grid-cols-3"
              role="status"
              aria-label="正在读取实时公开报价"
            >
              {[0, 1, 2].map(index => (
                <div key={index} className="animate-pulse rounded-xl bg-gray-100 p-4 dark:bg-[#25292E]">
                  <div className="h-3 w-1/2 rounded bg-gray-200 dark:bg-gray-700" />
                  <div className="mt-5 h-8 w-3/4 rounded bg-gray-200 dark:bg-gray-700" />
                  <div className="mt-8 h-16 rounded-lg bg-gray-200 dark:bg-gray-700" />
                </div>
              ))}
            </div>
          )}

          {feed && !visible.length && (
            <div className="flex min-h-56 flex-col items-center justify-center rounded-2xl border border-dashed border-gray-300 bg-white px-6 text-center dark:border-gray-700 dark:bg-[#1B1E22]">
              <Database className="h-8 w-8 text-gray-300 dark:text-gray-600" aria-hidden="true" />
              <p className="mt-3 text-sm font-black text-gray-900 dark:text-white">没有符合条件的报价</p>
              <p className="mt-1 max-w-md text-xs leading-5 text-gray-500 dark:text-gray-400">
                {onlyCompared ? '关闭“仅看已找到同规格样本”查看全部公开规格。' : '清空筛选或刷新上游公开报价。'}
              </p>
              <button
                type="button"
                onClick={() => {
                  setDisplayFilter('');
                  setOnlyCompared(false);
                }}
                className="mt-4 min-h-10 rounded-xl bg-[#FFE815] px-4 text-xs font-bold text-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#111315]"
              >
                清除全部筛选
              </button>
            </div>
          )}
            </div>

            {filteredRows.length > 36 && (
              <div className="flex flex-col items-center justify-between gap-3 border-t border-gray-200 bg-white px-4 py-3 text-xs text-gray-500 dark:border-gray-700 dark:bg-[#1B1E22] dark:text-gray-400 sm:flex-row">
                <p className="flex items-center gap-2">
                  <ShieldCheck className="h-4 w-4 shrink-0 text-emerald-500" aria-hidden="true" />
                  待核与异常挂牌单独展示，可信价差只取通过规格和互动校验的市场参照。
                </p>
                <button
                  type="button"
                  onClick={() => setShowAll(value => !value)}
                  className="min-h-10 rounded-xl border border-gray-200 bg-gray-50 px-4 font-bold text-gray-700 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:border-gray-700 dark:bg-[#25292E] dark:text-gray-200 dark:hover:bg-[#2D3238]"
                >
                  {showAll ? '收起至 36 款' : '展开全部规格'}
                </button>
              </div>
            )}
          </div>
        )}
      </section>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div className="relative overflow-hidden rounded-[24px] bg-[#FFE815] p-5 text-black">
          <PackageSearch className="absolute -bottom-5 -right-4 h-36 w-36 text-black/[0.07]" aria-hidden="true" />
          <div className="relative">
            <p className="text-[10px] font-black uppercase tracking-[0.18em] text-black/45">MANUAL CHECK</p>
            <h3 className="mt-1 text-xl font-black">手动追踪一个商品</h3>
            <p className="mt-1 max-w-md text-xs leading-5 text-black/60">
              输入你临时看到的商品词，读取闲鱼当前挂牌。保留这个窗口供低频查款。
            </p>
            <div className="relative mt-4">
              <label htmlFor="manual-market-search" className="sr-only">闲鱼商品快速搜索</label>
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-black/45" aria-hidden="true" />
              <input
                id="manual-market-search"
                value={keyword}
                onChange={event => setKeyword(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter') { void search(keyword); }
                }}
                maxLength={100}
                placeholder="例如：肯德基 5元代金券"
                className="min-h-12 w-full rounded-xl border border-black/10 bg-white/75 pl-9 pr-24 text-sm font-medium text-black outline-none placeholder:text-black/35 focus:border-black/30 focus:ring-2 focus:ring-black/10"
              />
              <button
                type="button"
                onClick={() => { void search(keyword); }}
                disabled={busy || !keyword.trim()}
                className="absolute right-1.5 top-1.5 min-h-9 rounded-lg bg-[#191B1F] px-3 text-xs font-bold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white disabled:opacity-45"
              >
                {busy ? '读取中' : '查询'}
              </button>
            </div>
            {error && <p role="alert" className="mt-3 text-sm font-semibold text-red-800">{error}</p>}
            {searchLabel && (
              <p role="status" className="mt-3 text-xs font-semibold text-black/60">
                「{searchLabel}」取得 {listings.length} 条有效挂牌
              </p>
            )}
          </div>
        </div>

        <section className={`${card} p-5`} aria-labelledby="manual-compare-title">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">NET MARGIN</p>
              <h3 id="manual-compare-title" className="mt-1 text-lg font-black text-gray-950 dark:text-white">同规格净差估算</h3>
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">填入两边核对后的实际应付金额。</p>
            </div>
            <GitCompareArrows className="h-5 w-5 text-gray-300 dark:text-gray-600" aria-hidden="true" />
          </div>
          <div className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(150px,0.75fr)] md:items-end">
            <label className="space-y-1.5 text-xs font-semibold text-gray-600 dark:text-gray-300">
              <span>上游同规格应付</span>
              <input
                inputMode="decimal"
                value={cost}
                onChange={event => setCost(event.target.value)}
                placeholder="0.00"
                className="min-h-11 w-full rounded-xl border border-gray-200 bg-gray-50 px-3 text-sm font-black tabular-nums text-gray-950 outline-none transition-colors focus:border-[#FFE815] focus:bg-white focus:ring-2 focus:ring-[#FFE815]/25 dark:border-gray-700 dark:bg-[#25292E] dark:text-white"
              />
            </label>
            <label className="space-y-1.5 text-xs font-semibold text-gray-600 dark:text-gray-300">
              <span>闲鱼同规格应付</span>
              <input
                inputMode="decimal"
                value={sale}
                onChange={event => setSale(event.target.value)}
                placeholder="0.00"
                className="min-h-11 w-full rounded-xl border border-gray-200 bg-gray-50 px-3 text-sm font-black tabular-nums text-gray-950 outline-none transition-colors focus:border-[#FFE815] focus:bg-white focus:ring-2 focus:ring-[#FFE815]/25 dark:border-gray-700 dark:bg-[#25292E] dark:text-white"
              />
            </label>
            <div className="min-h-11 rounded-xl bg-[#191B1F] px-4 py-2.5 text-white">
              <p className="text-[10px] font-bold text-white/65">估算价差</p>
              <p className={`mt-1 text-xl font-black tabular-nums ${
                costCents !== null && saleCents !== null && saleCents - costCents > 0
                  ? 'text-emerald-300'
                  : costCents !== null && saleCents !== null && saleCents - costCents < 0
                    ? 'text-rose-300'
                    : 'text-white'
              }`}>
                {costCents !== null && saleCents !== null ? signedMoney(saleCents - costCents) : '待输入'}
              </p>
            </div>
          </div>
          <p className="mt-3 text-[10px] leading-5 text-gray-400">
            页面价差不含平台服务费、支付优惠和售后成本。
          </p>
        </section>
      </section>

      {listings.length > 0 && (
        <section aria-label="闲鱼搜索结果" className={`${card} overflow-hidden`}>
          <div className="flex flex-col justify-between gap-2 border-b border-gray-100 p-5 dark:border-gray-700 sm:flex-row sm:items-center">
            <div>
              <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">LIVE LISTINGS</p>
              <h3 className="mt-1 text-lg font-black text-gray-950 dark:text-white">「{searchLabel}」闲鱼挂牌</h3>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400">打开后核对时长、等级和交付方式</p>
          </div>
          <div className="grid gap-px bg-gray-100 dark:bg-gray-700 md:grid-cols-2 xl:grid-cols-3">
            {listings.map((item, index) => (
              <a
                key={`${item.itemId}-${index}`}
                href={item.itemUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex min-h-28 flex-col justify-between bg-white p-4 transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#FFE815] dark:bg-[#1B1E22] dark:hover:bg-[#25292E]"
              >
                <p className="line-clamp-2 text-sm font-bold leading-5 text-gray-900 dark:text-white">
                  {item.title}
                </p>
                <div className="mt-4 flex items-end justify-between gap-3">
                  <p className="text-[10px] text-gray-500 dark:text-gray-400">
                    成交 {item.detail_sold_count ?? item.sold_count ?? '—'} · 想要 {item.want_count ?? '—'}
                  </p>
                  <p className="flex items-center gap-1 text-lg font-black tabular-nums text-gray-950 group-hover:text-blue-700 dark:text-white dark:group-hover:text-blue-300">
                    {typeof item.price === 'number' ? money(Math.round(item.price * 100)) : '—'}
                    <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                  </p>
                </div>
              </a>
            ))}
          </div>
        </section>
      )}

      <section className={`${card} overflow-hidden`} aria-labelledby="source-audit-title">
        <div className="flex flex-col justify-between gap-4 bg-gray-50 p-5 dark:bg-[#202328] lg:flex-row lg:items-end">
          <div>
            <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">SOURCE AUDIT</p>
            <h3 id="source-audit-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white">
              {auditSources.length} 个货源入口逐条实查
            </h3>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-gray-500 dark:text-gray-400">
              实查日期 2026-11-17。状态来自匿名只读访问，登录后的等级价没有计入当前自动比价。
            </p>
          </div>
          <div className="flex flex-wrap gap-2 text-[10px] font-bold">
            <span className="rounded-full bg-emerald-100 px-2.5 py-1.5 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200">
              匿名实时 {auditSources.filter(source => source.tone === 'live').length}
            </span>
            <span className="rounded-full bg-blue-100 px-2.5 py-1.5 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200">
              网页读取 {auditSources.filter(source => source.tone === 'scrape').length}
            </span>
            <span className="rounded-full bg-amber-100 px-2.5 py-1.5 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200">
              账户或签名 {auditSources.filter(source => source.tone === 'account' || source.tone === 'signed').length}
            </span>
            <span className="rounded-full bg-gray-200 px-2.5 py-1.5 text-gray-700 dark:bg-gray-700 dark:text-gray-200">
              导航、工具或失效 {auditSources.filter(source => source.tone === 'info' || source.tone === 'offline').length}
            </span>
          </div>
        </div>

        <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-3">
          {auditSources.map(source => (
            <a
              key={source.name}
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              className={`group flex min-h-32 flex-col justify-between rounded-2xl border p-4 transition-transform duration-200 hover:-translate-y-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] ${auditToneClass[source.tone]}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-black">{source.name}</p>
                  <p className="mt-1 text-[10px] font-bold uppercase tracking-[0.12em] opacity-65">{source.mode}</p>
                </div>
                {source.tone === 'account'
                  ? <LockKeyhole className="h-4 w-4 shrink-0 opacity-60" aria-hidden="true" />
                  : source.tone === 'offline'
                    ? <TriangleAlert className="h-4 w-4 shrink-0 opacity-60" aria-hidden="true" />
                    : <ExternalLink className="h-4 w-4 shrink-0 opacity-60 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />}
              </div>
              <p className="mt-4 line-clamp-2 text-xs leading-5 opacity-75">{source.detail}</p>
            </a>
          ))}
        </div>
      </section>

      {fulfillmentSeed && fulfillmentForm && createPortal(
        <div className="modal-overlay-centered">
          <div
            className="modal-container !max-w-5xl"
            role="dialog"
            aria-modal="true"
            aria-labelledby="fulfillment-dialog-title"
            aria-describedby="fulfillment-dialog-description"
          >
            <div className="modal-header">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">
                    FULFILLMENT PIPELINE
                  </p>
                  <h3 id="fulfillment-dialog-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white sm:text-2xl">
                    选择履约并发布
                  </h3>
                  <p id="fulfillment-dialog-description" className="mt-2 max-w-3xl text-xs leading-5 text-gray-500 dark:text-gray-400">
                    先创建持久任务，再由后端完成发布、item_id 回读、商品同步、卡券绑定与库存复核。页面不会把公开报价或任务创建当成上架成功。
                  </p>
                </div>
                <button
                  type="button"
                  onClick={closeFulfillmentDialog}
                  className="shrink-0 rounded-xl p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:text-gray-300 dark:hover:bg-[#25292E] dark:hover:text-white"
                  aria-label="关闭履约弹窗"
                >
                  <X className="h-5 w-5" aria-hidden="true" />
                </button>
              </div>
            </div>

            <div className="modal-body !px-4 sm:!px-6 lg:!px-8">
              <div className="space-y-5">
                {fulfillmentTask && (
                  <FulfillmentTaskPanel
                    task={fulfillmentTask}
                    events={fulfillmentEvents}
                    pollError={fulfillmentPollError}
                    recovering={fulfillmentRecovering}
                    onRecover={() => { void recoverFulfillmentTask(); }}
                  />
                )}

                <section className={`${card} overflow-hidden`} aria-labelledby="fulfillment-source-title">
                  <div className="flex flex-col justify-between gap-3 border-b border-gray-100 p-4 dark:border-gray-700 sm:flex-row sm:items-start">
                    <div className="min-w-0">
                      <p className="text-[10px] font-black uppercase tracking-[0.16em] text-gray-400">SOURCE SNAPSHOT</p>
                      <h4 id="fulfillment-source-title" className="mt-1 line-clamp-2 text-sm font-black text-gray-950 dark:text-white">
                        {fulfillmentSeed.quote.title}
                      </h4>
                      <p className="mt-1 truncate text-[11px] text-gray-500 dark:text-gray-400">
                        {fulfillmentSeed.quote.supplier || '公开货源'}
                        {' · '}
                        {fulfillmentSeed.quote.source_label || fulfillmentSeed.quote.source || '上游报价'}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-2 text-[11px] font-black">
                      <span className="rounded-xl bg-blue-50 px-3 py-2 tabular-nums text-blue-800 dark:bg-blue-950/40 dark:text-blue-200">
                        上游 {money(Math.round(fulfillmentSeed.quote.price * 100))}
                      </span>
                      {fulfillmentSeed.listing.price !== null && (
                        <span className="rounded-xl bg-gray-100 px-3 py-2 tabular-nums text-gray-800 dark:bg-[#25292E] dark:text-gray-200">
                          闲鱼 {money(Math.round(fulfillmentSeed.listing.price * 100))}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-col justify-between gap-2 bg-amber-50 px-4 py-3 text-[11px] leading-5 text-amber-900 dark:bg-amber-950/30 dark:text-amber-100 sm:flex-row">
                    <span>报价快照不等于锁定采购价或实时库存，发布前请核对等级、时长、交付方式与售后成本。</span>
                    <a
                      href={fulfillmentSeed.quote.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex min-h-8 shrink-0 items-center gap-1 font-bold underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
                    >
                      核对上游报价
                      <ExternalLink className="h-3 w-3" aria-hidden="true" />
                    </a>
                  </div>
                </section>

                <section className="space-y-5" aria-labelledby="fulfillment-form-title">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-[10px] font-black uppercase tracking-[0.16em] text-gray-400">PUBLISH INPUT</p>
                      <h4 id="fulfillment-form-title" className="mt-1 text-base font-black text-gray-950 dark:text-white">
                        发布与履约参数
                      </h4>
                    </div>
                    {fulfillmentOptionsLoading && (
                      <span role="status" className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-gray-500 dark:text-gray-400">
                        <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                        读取账号、卡券与素材
                      </span>
                    )}
                  </div>

                  {fulfillmentOptionsError && (
                    <div role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
                      {fulfillmentOptionsError}
                    </div>
                  )}

                  <div className="grid gap-4 md:grid-cols-2">
                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>发布账号 <span className="text-rose-500">*</span></span>
                      <select
                        value={fulfillmentForm.cookieId}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, cookieId: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm"
                      >
                        <option value="">请选择闲鱼账号</option>
                        {fulfillmentAccounts.map(account => (
                          <option key={account.id} value={account.id} disabled={!account.enabled}>
                            {account.nickname || account.remark || account.id} · {account.id}
                            {account.enabled ? '' : '（已停用）'}
                          </option>
                        ))}
                      </select>
                      {!fulfillmentOptionsLoading && fulfillmentAccounts.length === 0 && (
                        <span className="block text-[10px] font-medium leading-4 text-amber-700 dark:text-amber-300">
                          尚未读取到账号。发布账号必须由后端校验归属。
                        </span>
                      )}
                    </label>

                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>自动发货卡券 <span className="text-rose-500">*</span></span>
                      <select
                        value={fulfillmentForm.cardId}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, cardId: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm"
                      >
                        <option value="">请选择已启用卡券</option>
                        {fulfillmentCards.map(item => (
                          <option key={item.id} value={item.id}>
                            {item.name} · {CARD_TYPE_LABELS[item.type]}
                            {item.type === 'data' ? ` · ${dataCardInventory(item)} 条库存` : ''}
                          </option>
                        ))}
                      </select>
                      {!fulfillmentOptionsLoading && fulfillmentCards.length === 0 && (
                        <span className="block text-[10px] font-medium leading-4 text-amber-700 dark:text-amber-300">
                          没有已启用卡券。任务无法进入履约就绪。
                        </span>
                      )}
                    </label>
                  </div>

                  {selectedFulfillmentCard && (
                    <div className={`rounded-xl border px-3 py-3 text-[11px] leading-5 ${
                      selectedFulfillmentCard.type === 'data' && dataCardInventory(selectedFulfillmentCard) === 0
                        ? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100'
                        : 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-[#202328] dark:text-gray-300'
                    }`}>
                      <strong>{CARD_TYPE_LABELS[selectedFulfillmentCard.type]}</strong>
                      {' · '}
                      {selectedFulfillmentCard.type === 'data'
                        ? `当前 data_content 可计数库存 ${dataCardInventory(selectedFulfillmentCard)} 条`
                        : selectedFulfillmentCard.type === 'api'
                          ? 'API 卡券的可用库存只能在后端执行时确认'
                          : '固定内容由后端在创建任务时复核'}
                    </div>
                  )}

                  <div className="grid gap-4">
                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>发布标题 <span className="text-rose-500">*</span></span>
                      <input
                        value={fulfillmentForm.title}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, title: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm"
                      />
                    </label>

                    {(() => {
                      const compliance = auditContent(fulfillmentForm.title, fulfillmentForm.desc);
                      return (
                        <div className="space-y-2">
                          <div className="flex items-center justify-between text-xs font-bold">
                            <span className="flex items-center gap-1.5 text-gray-700 dark:text-gray-300">
                              <span>发布描述 (VantaSell 5段式规范)</span>
                              <span className="text-rose-500">*</span>
                            </span>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                onClick={() => {
                                  const regenerated = generateFiveBlockDesc({
                                    title: fulfillmentForm.title,
                                    supplier: fulfillmentSeed.quote.supplier || fulfillmentSeed.quote.source_label || fulfillmentSeed.quote.source,
                                    price: fulfillmentForm.price,
                                  });
                                  setFulfillmentForm(current => current ? { ...current, desc: regenerated } : current);
                                }}
                                disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                                className="flex items-center gap-1 text-[11px] font-bold text-blue-600 hover:text-blue-700 dark:text-blue-400 hover:underline"
                              >
                                <Sparkles className="w-3.5 h-3.5 text-blue-500" />
                                <span>重新生成5段式文案</span>
                              </button>
                              {compliance.issues.length > 0 && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setFulfillmentForm(current => current ? {
                                      ...current,
                                      title: sanitizeContent(current.title),
                                      desc: sanitizeContent(current.desc),
                                    } : current);
                                  }}
                                  disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                                  className="flex items-center gap-1 text-[11px] font-bold text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950/40 px-2 py-0.5 rounded border border-amber-200 dark:border-amber-800 hover:bg-amber-100 transition-colors"
                                >
                                  <ShieldAlert className="w-3.5 h-3.5 text-amber-500" />
                                  <span>一键安全净化</span>
                                </button>
                              )}
                            </div>
                          </div>

                          <textarea
                            value={fulfillmentForm.desc}
                            onChange={event => setFulfillmentForm(current => current ? { ...current, desc: event.target.value } : current)}
                            disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                            required
                            rows={8}
                            placeholder="填写商品内容、适用范围、发货方式和售后说明"
                            className="w-full resize-y rounded-xl ios-input px-3 py-3 text-sm leading-6"
                          />

                          {/* 静态合规体检反馈 */}
                          <div className={`rounded-xl p-3 text-xs space-y-1.5 border ${
                            compliance.issues.length === 0
                              ? 'bg-emerald-50 dark:bg-emerald-950/20 text-emerald-800 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800/40'
                              : compliance.blockCount > 0
                                ? 'bg-rose-50 dark:bg-rose-950/30 text-rose-800 dark:text-rose-200 border-rose-200 dark:border-rose-800/40'
                                : 'bg-amber-50 dark:bg-amber-950/30 text-amber-800 dark:text-amber-200 border-amber-200 dark:border-amber-800/40'
                          }`}>
                            <div className="flex items-center justify-between font-bold">
                              <span className="flex items-center gap-1.5">
                                {compliance.issues.length === 0 ? (
                                  <>
                                    <CheckCheck className="w-4 h-4 text-emerald-600 dark:text-emerald-400" />
                                    <span>静态合规体检通过：未检出导流词、违禁词及极限用语</span>
                                  </>
                                ) : (
                                  <>
                                    <ShieldAlert className="w-4 h-4 text-rose-600 dark:text-rose-400" />
                                    <span>
                                      合规预警：检出 {compliance.blockCount > 0 ? `${compliance.blockCount} 处高危导流违规` : ''}
                                      {compliance.blockCount > 0 && compliance.warnCount > 0 ? '、' : ''}
                                      {compliance.warnCount > 0 ? `${compliance.warnCount} 处夸大极限词` : ''}
                                    </span>
                                  </>
                                )}
                              </span>
                              {compliance.issues.length > 0 && (
                                <span className="text-[11px] text-gray-500 dark:text-gray-400">
                                  点击右上角「一键安全净化」可自动修复
                                </span>
                              )}
                            </div>
                            {compliance.issues.length > 0 && (
                              <div className="flex flex-wrap gap-1.5 pt-1">
                                {compliance.issues.map((issue, idx) => (
                                  <span
                                    key={idx}
                                    className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-semibold border ${
                                      issue.level === 'block'
                                        ? 'bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-200 border-rose-300 dark:border-rose-700'
                                        : 'bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 border-amber-300 dark:border-amber-700'
                                    }`}
                                  >
                                    <span>{issue.word}</span>
                                    <span className="text-[10px] text-gray-500 dark:text-gray-400">({issue.reason})</span>
                                    <span>➔ 建议改为: {issue.replacement}</span>
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })()}
                  </div>

                  <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>售价 <span className="text-rose-500">*</span></span>
                      <input
                        type="number"
                        min="0.01"
                        step="0.01"
                        inputMode="decimal"
                        value={fulfillmentForm.price}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, price: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm font-black tabular-nums"
                      />
                    </label>
                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>数量 <span className="text-rose-500">*</span></span>
                      <input
                        type="number"
                        min="1"
                        step="1"
                        inputMode="numeric"
                        value={fulfillmentForm.quantity}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, quantity: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm font-black tabular-nums"
                      />
                    </label>
                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>交付方式 <span className="text-rose-500">*</span></span>
                      <select
                        value={fulfillmentForm.delivery}
                        onChange={event => setFulfillmentForm(current => current ? {
                          ...current,
                          delivery: event.target.value as OpportunityFulfillmentDelivery,
                        } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm"
                      >
                        {FULFILLMENT_DELIVERY_OPTIONS.map(option => (
                          <option key={option} value={option}>{option}</option>
                        ))}
                      </select>
                    </label>
                    <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <span>一口价 / 运费</span>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        inputMode="decimal"
                        value={fulfillmentForm.postPrice}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, postPrice: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        className="min-h-11 w-full rounded-xl ios-input px-3 text-sm font-black tabular-nums"
                      />
                    </label>
                  </div>

                  <label className="space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                    <span>标签</span>
                    <input
                      value={fulfillmentForm.tags}
                      onChange={event => setFulfillmentForm(current => current ? { ...current, tags: event.target.value } : current)}
                      disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                      placeholder="多个标签用逗号或换行分隔"
                      className="min-h-11 w-full rounded-xl ios-input px-3 text-sm"
                    />
                  </label>

                  <div className="rounded-2xl border border-gray-200 p-4 dark:border-gray-700">
                    <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
                      <div>
                        <p className="text-sm font-black text-gray-950 dark:text-white">发布图片</p>
                        <p className="mt-1 text-[11px] leading-5 text-gray-500 dark:text-gray-400">
                          可从图片素材库读取绝对路径，也可逐行编辑路径或图片 URL。至少一张。
                        </p>
                      </div>
                      <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
                        <select
                          value={fulfillmentMaterialId}
                          onChange={event => {
                            setFulfillmentMaterialId(event.target.value);
                            setFulfillmentMaterialError('');
                            setFulfillmentMaterialNotice('');
                          }}
                          disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting || fulfillmentMaterialLoading}
                          aria-label="选择图片素材"
                          className="min-h-10 min-w-0 rounded-xl ios-input px-3 text-xs sm:min-w-52"
                        >
                          <option value="">选择图片素材</option>
                          {fulfillmentMaterials.map(material => (
                            <option key={material.id} value={material.id}>
                              {material.name} · {material.file_count} 张
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          onClick={() => { void applyFulfillmentMaterial(); }}
                          disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting || fulfillmentMaterialLoading || !fulfillmentMaterialId}
                          className="inline-flex min-h-10 items-center justify-center gap-2 rounded-xl border border-gray-300 bg-white px-3 text-xs font-black text-gray-800 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] disabled:opacity-50 dark:border-gray-600 dark:bg-[#25292E] dark:text-white dark:hover:bg-[#2D3238]"
                        >
                          <RefreshCw className={`h-3.5 w-3.5 ${fulfillmentMaterialLoading ? 'animate-spin' : ''}`} aria-hidden="true" />
                          {fulfillmentMaterialLoading ? '读取中' : '套用素材图'}
                        </button>
                      </div>
                    </div>

                    {!fulfillmentOptionsLoading && fulfillmentMaterials.length === 0 && (
                      <p className="mt-3 text-[10px] leading-4 text-amber-700 dark:text-amber-300">
                        图片素材库暂无可选项，可直接在下方逐行填写图片路径或 URL。
                      </p>
                    )}
                    {fulfillmentMaterialNotice && (
                      <p role="status" className="mt-3 text-[11px] font-semibold text-emerald-700 dark:text-emerald-300">
                        {fulfillmentMaterialNotice}
                      </p>
                    )}
                    {fulfillmentMaterialError && (
                      <p role="alert" className="mt-3 text-[11px] font-semibold text-rose-700 dark:text-rose-300">
                        {fulfillmentMaterialError}
                      </p>
                    )}

                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                      <span className="text-xs font-bold text-gray-700 dark:text-gray-300">
                        imageUrls，一行一项 <span className="text-rose-500">*</span>
                      </span>
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => handleGenerateImages(1)}
                          disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting || imageGenLoading}
                          className="inline-flex items-center gap-1 rounded-lg bg-amber-500/10 px-2.5 py-1 text-[11px] font-bold text-amber-700 transition hover:bg-amber-500/20 dark:bg-amber-400/10 dark:text-amber-300 dark:hover:bg-amber-400/20 disabled:opacity-50"
                        >
                          <Sparkles className={`h-3 w-3 ${imageGenLoading ? 'animate-spin' : ''}`} />
                          {imageGenLoading ? '生图中...' : '🪄 智能生成主图'}
                        </button>
                        <button
                          type="button"
                          onClick={() => handleGenerateImages(3)}
                          disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting || imageGenLoading}
                          title="生成 3 张成套图：主图 + 自动发货流程图 + 售后质保图"
                          className="inline-flex items-center gap-1 rounded-lg bg-blue-500/10 px-2.5 py-1 text-[11px] font-bold text-blue-700 transition hover:bg-blue-500/20 dark:bg-blue-400/10 dark:text-blue-300 dark:hover:bg-blue-400/20 disabled:opacity-50"
                        >
                          <Sparkles className={`h-3 w-3 ${imageGenLoading ? 'animate-spin' : ''}`} />
                          生成3张全套图
                        </button>
                      </div>
                    </div>

                    {generatedPreviews.length > 0 && (
                      <div className="mt-2 flex gap-2 overflow-x-auto rounded-xl border border-gray-200 bg-gray-50 p-2 dark:border-gray-700 dark:bg-gray-800/40">
                        {generatedPreviews.map((url, i) => (
                          <div key={i} className="group relative h-20 w-20 shrink-0 overflow-hidden rounded-lg border border-gray-300 dark:border-gray-600">
                            <img src={url} alt={`配图 ${i + 1}`} className="h-full w-full object-cover" />
                            <span className="absolute inset-x-0 bottom-0 bg-black/60 py-0.5 text-center text-[9px] font-medium text-white">
                              {i === 0 ? '主图' : i === 1 ? '流程' : '质保'}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}

                    <label className="mt-2 block space-y-1.5 text-xs font-bold text-gray-700 dark:text-gray-300">
                      <textarea
                        value={fulfillmentForm.imageUrls}
                        onChange={event => setFulfillmentForm(current => current ? { ...current, imageUrls: event.target.value } : current)}
                        disabled={Boolean(fulfillmentTask) || fulfillmentSubmitting}
                        required
                        rows={3}
                        placeholder="第一行是主图，后续行按发布顺序排列"
                        className="w-full resize-y rounded-xl ios-input px-3 py-3 font-mono text-xs leading-5"
                      />
                    </label>
                  </div>

                  {fulfillmentCreateError && (
                    <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-3 text-xs leading-5 text-rose-900 dark:border-rose-900/60 dark:bg-rose-950/30 dark:text-rose-100">
                      {fulfillmentCreateError}
                    </div>
                  )}
                </section>
              </div>
            </div>

            <div className="modal-footer">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="max-w-xl text-[10px] leading-4 text-gray-400">
                  提交只创建持久任务。出现 submission_unknown_review_required 时不得自动重发，应先人工核对闲鱼发布记录。
                </p>
                <div className="flex shrink-0 gap-3">
                  <button
                    type="button"
                    onClick={closeFulfillmentDialog}
                    className="min-h-11 flex-1 rounded-xl bg-gray-100 px-4 text-xs font-black text-gray-700 transition-colors hover:bg-gray-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:bg-[#25292E] dark:text-gray-200 dark:hover:bg-[#2D3238] sm:flex-none"
                  >
                    {fulfillmentTask ? '关闭' : '取消'}
                  </button>
                  <button
                    type="button"
                    onClick={() => { void submitFulfillmentTask(); }}
                    disabled={fulfillmentSubmitting || Boolean(fulfillmentTask)}
                    className="inline-flex min-h-11 flex-1 items-center justify-center gap-2 rounded-xl ios-btn-primary px-4 text-xs font-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#111315] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 dark:focus-visible:ring-[#FFE815] dark:focus-visible:ring-offset-[#1B1E22] sm:flex-none"
                  >
                    {fulfillmentSubmitting
                      ? <RefreshCw className="h-4 w-4 animate-spin" aria-hidden="true" />
                      : fulfillmentTask
                        ? <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                        : <Zap className="h-4 w-4" aria-hidden="true" />}
                    {fulfillmentSubmitting ? '创建中' : fulfillmentTask ? '任务已创建' : '创建履约任务'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </section>
  );
}

