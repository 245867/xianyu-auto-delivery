import { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ArrowUpRight,
  CheckCircle2,
  Clock3,
  Eye,
  Image,
  PackageOpen,
  RefreshCw,
  Search,
  ShoppingBag,
  Sparkles,
  Store,
  TrendingUp,
  TriangleAlert,
  Users,
} from 'lucide-react';
import { getAccountDetails, getItems } from '../services/api';
import AccountConnectionBadge from './AccountConnectionBadge';
import { useDiagnosisReports, usePolishStatus } from '../lib/exposure';
import type { Diagnosis, DiagnosisItem } from '../lib/exposure';
import type { AccountDetail, Item } from '../types';

const card = 'ios-card rounded-xl';

interface LoadState {
  loaded: boolean;
  refreshing: boolean;
  error: string;
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function shortTime(value?: string | null) {
  if (!value) return '暂无记录';
  return value.replace('T', ' ').slice(0, 16);
}

function accountName(account?: AccountDetail) {
  return account?.nickname || account?.remark || account?.id || '未命名账号';
}

function countLabel(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('zh-CN')
    : '—';
}

function itemKey(cookieId: string, itemId: string) {
  return JSON.stringify([cookieId, String(itemId)]);
}

function sumPresent(values: Array<number | null | undefined>) {
  const present = values.filter(
    (value): value is number => typeof value === 'number' && Number.isFinite(value),
  );
  return present.length ? present.reduce((sum, value) => sum + value, 0) : null;
}

function ItemThumbnail({ url }: { url?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-gray-100 dark:bg-[#25292E]">
      {url && !failed ? (
        <img
          src={url}
          alt=""
          className="h-full w-full object-cover"
          onError={() => setFailed(true)}
        />
      ) : (
        <Image className="h-5 w-5 text-gray-400" aria-hidden="true" />
      )}
    </div>
  );
}

export default function ExposureOverview() {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [accountState, setAccountState] = useState<LoadState>({
    loaded: false,
    refreshing: false,
    error: '',
  });
  const [itemState, setItemState] = useState<LoadState>({
    loaded: false,
    refreshing: false,
    error: '',
  });
  const [accountFilter, setAccountFilter] = useState('');
  const [visibilityFilter, setVisibilityFilter] = useState<
    'all' | 'attention' | 'diagnosed' | 'pending'
  >('all');
  const [searchText, setSearchText] = useState('');
  const [refreshSequence, setRefreshSequence] = useState(0);
  const polishState = usePolishStatus();
  const polish = polishState.status;
  const diagnosis = useDiagnosisReports(
    accounts.map(account => account.id),
    refreshSequence,
  );

  useEffect(() => {
    let active = true;
    setAccountState(current => ({
      ...current,
      refreshing: true,
      error: '',
    }));
    void getAccountDetails()
      .then(value => {
        if (active) setAccounts(value);
      })
      .catch((reason: unknown) => {
        if (active) {
          setAccountState(current => ({
            ...current,
            error: errorMessage(reason, '账号读取失败'),
          }));
        }
      })
      .finally(() => {
        if (active) {
          setAccountState(current => ({
            ...current,
            loaded: true,
            refreshing: false,
          }));
        }
      });
    return () => { active = false; };
  }, [refreshSequence]);

  useEffect(() => {
    let active = true;
    setItemState(current => ({
      ...current,
      refreshing: true,
      error: '',
    }));
    void getItems()
      .then(value => {
        if (active) setItems(value);
      })
      .catch((reason: unknown) => {
        if (active) {
          setItemState(current => ({
            ...current,
            error: errorMessage(reason, '商品读取失败'),
          }));
        }
      })
      .finally(() => {
        if (active) {
          setItemState(current => ({
            ...current,
            loaded: true,
            refreshing: false,
          }));
        }
      });
    return () => { active = false; };
  }, [refreshSequence]);

  const accountMap = useMemo(
    () => new Map(accounts.map(account => [account.id, account])),
    [accounts],
  );
  const reportByItem = useMemo(() => {
    const rows = new Map<string, { report: Diagnosis; row: DiagnosisItem }>();
    for (const [accountId, report] of Object.entries(diagnosis.reports)) {
      for (const row of report.items ?? []) {
        rows.set(itemKey(accountId, row.item_id), { report, row });
      }
    }
    return rows;
  }, [diagnosis.reports]);
  const lastLogByItem = useMemo(() => {
    const logs = new Map<string, NonNullable<typeof polish>['recent_logs'][number]>();
    for (const log of polish?.recent_logs ?? []) {
      logs.set(itemKey(log.cookie_id, log.item_id), log);
    }
    return logs;
  }, [polish?.recent_logs]);

  const accountSummaries = polish?.last_summary?.accounts ?? {};
  const schedule = polish?.settings
    ? [polish.settings.hour, polish.settings.minute]
      .map(value => String(value).padStart(2, '0'))
      .join(':')
    : null;
  const planStatus = !polishState.loaded
    ? '正在读取计划'
    : polishState.error
      ? '计划读取失败'
      : polish?.running
        ? '正在执行'
        : !polish?.settings
          ? '暂无计划'
          : !polish.settings.enabled
            ? '计划已关闭'
            : polish.ran_today
              ? '今日已执行'
              : '等待执行';

  const accountMetrics = accounts.map(account => {
    const accountItems = items.filter(item => item.cookie_id === account.id);
    const diagnosed = accountItems.filter(item =>
      reportByItem.has(itemKey(item.cookie_id, item.item_id)),
    );
    const rows = diagnosed.map(item =>
      reportByItem.get(itemKey(item.cookie_id, item.item_id))!.row,
    );
    const summary = accountSummaries[account.id];
    return {
      account,
      items: accountItems,
      diagnosed,
      summary,
      browse: sumPresent(rows.map(row => row.browse)),
      want: sumPresent(rows.map(row => row.want)),
      sold: sumPresent(rows.map(row => row.sold)),
      failedLogs: accountItems.filter(item => {
        const log = lastLogByItem.get(itemKey(item.cookie_id, item.item_id));
        return Boolean(log && !log.success);
      }).length,
      highSignals: rows.filter(row =>
        row.signals?.some(signal => signal.level === 'high' && signal.code !== 'fresh'),
      ).length,
    };
  });

  const reportAvailable = (item: Item) =>
    reportByItem.has(itemKey(item.cookie_id, item.item_id));
  const accountItems = items.filter(item =>
    !accountFilter || item.cookie_id === accountFilter,
  );
  const normalizedSearch = searchText.trim().toLocaleLowerCase('zh-CN');
  const visibleItems = accountItems.filter(item => {
    if (normalizedSearch) {
      const searchable = `${item.item_title || ''} ${item.item_id} ${accountName(accountMap.get(item.cookie_id))}`
        .toLocaleLowerCase('zh-CN');
      if (!searchable.includes(normalizedSearch)) return false;
    }
    if (visibilityFilter === 'diagnosed') return reportAvailable(item);
    if (visibilityFilter === 'pending') return !reportAvailable(item);
    if (visibilityFilter === 'attention') {
      const row = reportByItem.get(itemKey(item.cookie_id, item.item_id))?.row;
      const log = lastLogByItem.get(itemKey(item.cookie_id, item.item_id));
      return !row
        || Boolean(log && !log.success)
        || row.classify === 'cull'
        || row.classify === 'zombie'
        || Boolean(row.signals?.some(signal =>
          signal.level === 'high' && signal.code !== 'fresh',
        ));
    }
    return true;
  });

  const diagnosedCount = items.filter(reportAvailable).length;
  const pendingCount = items.length - diagnosedCount;
  const failedLogs = items.filter(item => {
    const log = lastLogByItem.get(itemKey(item.cookie_id, item.item_id));
    return Boolean(log && !log.success);
  }).length;
  const highSignalCount = accountMetrics.reduce(
    (sum, account) => sum + account.highSignals,
    0,
  );
  const diagnosisErrorCount = Object.keys(diagnosis.errors).length;
  const totalSuccess = Object.values(accountSummaries)
    .reduce((sum, summary) => sum + summary.success, 0);
  const totalFail = Object.values(accountSummaries)
    .reduce((sum, summary) => sum + summary.fail, 0);
  const coverageRate = items.length ? Math.round(diagnosedCount / items.length * 100) : 0;
  const activityRows = items.flatMap(item => {
    const hit = reportByItem.get(itemKey(item.cookie_id, item.item_id));
    if (!hit) return [];
    const { row, report } = hit;
    if (
      typeof row.sold !== 'number'
      && typeof row.want !== 'number'
      && typeof row.browse !== 'number'
    ) {
      return [];
    }
    return [{
      item,
      row,
      report,
      account: accountMap.get(item.cookie_id),
      sold: typeof row.sold === 'number' && Number.isFinite(row.sold) ? row.sold : null,
      want: typeof row.want === 'number' && Number.isFinite(row.want) ? row.want : null,
      browse: typeof row.browse === 'number' && Number.isFinite(row.browse) ? row.browse : null,
    }];
  }).sort((left, right) =>
    (right.sold ?? -1) - (left.sold ?? -1)
    || (right.want ?? -1) - (left.want ?? -1)
    || (right.browse ?? -1) - (left.browse ?? -1)
    || (left.item.item_title || left.item.item_id).localeCompare(
      right.item.item_title || right.item.item_id,
      'zh-CN',
    ),
  );
  const topActivityRows = activityRows.slice(0, 8);
  const salesSnapshotReports = Object.values(diagnosis.reports)
    .filter(report => report.sales?.ok);
  const reportedOrderTotal = sumPresent(
    salesSnapshotReports.map(report => report.sales?.orders),
  );

  const attentionEntries = [
    ...(accountState.error ? [{
      tone: 'red',
      label: '账号数据',
      title: '账号列表读取失败',
      detail: accountState.error,
    }] : []),
    ...(itemState.error ? [{
      tone: 'red',
      label: '商品数据',
      title: '商品列表读取失败',
      detail: itemState.error,
    }] : []),
    ...(diagnosisErrorCount ? [{
      tone: 'amber',
      label: '诊断接口',
      title: `${diagnosisErrorCount} 个账号没有可用报告`,
      detail: Object.values(diagnosis.errors)[0] || '接口未返回诊断数据',
    }] : []),
    ...(failedLogs ? [{
      tone: 'red',
      label: '擦亮结果',
      title: `${failedLogs} 件商品最近擦亮失败`,
      detail: '结果已经标在对应商品卡片上。',
    }] : []),
    ...(highSignalCount ? [{
      tone: 'amber',
      label: '诊断信号',
      title: `${highSignalCount} 件商品存在高危信号`,
      detail: '在“优先处理”筛选中查看具体商品与建议。',
    }] : []),
    ...(!diagnosisErrorCount && pendingCount ? [{
      tone: 'blue',
      label: '诊断缺口',
      title: `${pendingCount} 件商品还没有诊断数据`,
      detail: '完成下一轮店铺诊断后会直接回填到商品卡片。',
    }] : []),
  ].slice(0, 4);

  return (
    <section className="space-y-5 animate-fade-in">
      <header className="flex flex-col justify-between gap-4 pr-14 lg:flex-row lg:items-end">
        <div>
          <p className="text-[11px] font-black uppercase tracking-[0.22em] text-gray-400">
            EXPOSURE CONTROL
          </p>
          <h2 className="mt-1 text-3xl font-black tracking-[-0.04em] text-gray-950 dark:text-white">
            曝光运营
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500 dark:text-gray-400">
            擦亮计划、批次结果、诊断指标和异常信号直接落在账号与商品上。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex min-h-10 items-center gap-2 rounded-xl border border-gray-200 bg-white px-3 text-xs font-bold text-gray-700 dark:border-gray-700 dark:bg-[#1B1E22] dark:text-gray-200">
            <span className={`h-2 w-2 rounded-full ${
              polishState.error
                ? 'bg-rose-500'
                : polish?.running
                  ? 'bg-blue-500'
                  : polish?.settings?.enabled
                    ? 'bg-emerald-500'
                    : 'bg-gray-400'
            }`} />
            {planStatus}
          </span>
          <button
            type="button"
            onClick={() => setRefreshSequence(value => value + 1)}
            disabled={accountState.refreshing || itemState.refreshing}
            className="ios-btn-secondary inline-flex min-h-10 items-center gap-2 rounded-xl px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] disabled:opacity-60 dark:bg-[#25292E] dark:text-white"
          >
            <RefreshCw
              className={`h-4 w-4 ${
                accountState.refreshing || itemState.refreshing ? 'animate-spin' : ''
              }`}
              aria-hidden="true"
            />
            刷新账号与商品
          </button>
          <a
            href="/shop_diagnosis"
            className="ios-btn-primary inline-flex min-h-10 items-center gap-2 rounded-xl px-4 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black"
          >
            运行店铺诊断
            <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
          </a>
        </div>
      </header>

      {(polishState.error || accountState.error || itemState.error || diagnosisErrorCount > 0) && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/50 dark:bg-[#342F1F] dark:text-amber-100"
        >
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="min-w-0">
            <p className="font-bold">部分状态没有读取成功，已有数据仍保留在页面。</p>
            <p className="mt-1 break-words text-xs leading-5 opacity-80">
              {[
                polishState.error ? `擦亮状态：${polishState.error}` : '',
                accountState.error ? `账号：${accountState.error}` : '',
                itemState.error ? `商品：${itemState.error}` : '',
                diagnosisErrorCount ? `诊断：${Object.values(diagnosis.errors)[0]}` : '',
              ].filter(Boolean).join('；')}
            </p>
          </div>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
        <section className="relative overflow-hidden rounded-[28px] bg-[#191B1F] p-5 text-white shadow-[0_24px_70px_rgba(17,19,21,0.16)] sm:p-6">
          <div className="pointer-events-none absolute -right-24 -top-28 h-80 w-80 rounded-full bg-[#FFE815]/15 blur-3xl" />
          <div className="pointer-events-none absolute bottom-0 left-0 h-px w-full bg-gradient-to-r from-transparent via-[#FFE815]/60 to-transparent" />
          <div className="relative">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-[10px] font-black uppercase tracking-[0.22em] text-white/45">
                  SHOP SIGNAL BOARD
                </p>
                <h3 className="mt-2 text-2xl font-black tracking-tight">店铺曝光健康面</h3>
                <p className="mt-1 text-sm leading-6 text-white/55">
                  使用实际浏览、想要、成交和诊断信号，不把未知曝光数补成估算值。
                </p>
              </div>
              <span className={`rounded-full px-3 py-1.5 text-xs font-bold ${
                planStatus === '正在执行'
                  ? 'bg-blue-300 text-[#111827]'
                  : planStatus === '计划读取失败'
                    ? 'bg-rose-300 text-[#3C1119]'
                    : polish?.settings?.enabled
                      ? 'bg-[#FFE815] text-black'
                      : 'bg-white/10 text-white/70'
              }`}>
                {planStatus}
              </span>
            </div>

            <div className="mt-6 grid gap-3 sm:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
              <div className="relative overflow-hidden rounded-2xl bg-[#FFE815] p-5 text-black">
                <span className="text-[10px] font-black uppercase tracking-[0.16em] text-black/50">
                  诊断覆盖
                </span>
                <div className="mt-3 flex items-end gap-2">
                  <strong className="text-5xl font-black tracking-[-0.05em] tabular-nums">
                    {itemState.loaded && !itemState.error ? coverageRate : '—'}
                  </strong>
                  <span className="pb-1 text-lg font-black">%</span>
                </div>
                <p className="mt-2 text-xs font-semibold text-black/60">
                  {itemState.loaded ? `${diagnosedCount} / ${items.length} 件商品有报告` : '正在读取商品'}
                </p>
                <div className="mt-4 h-2 overflow-hidden rounded-full bg-black/15">
                  <div
                    className="h-full rounded-full bg-black transition-[width] duration-500"
                    style={{ width: `${coverageRate}%` }}
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-2.5">
                <div className="rounded-2xl border border-white/10 bg-white/[0.065] p-4">
                  <Clock3 className="h-4 w-4 text-[#FFE815]" aria-hidden="true" />
                  <p className="mt-3 text-[10px] font-bold text-white/55">每日计划</p>
                  <p className="mt-1 text-xl font-black tabular-nums">{schedule ?? '—'}</p>
                </div>
                <div className="rounded-2xl border border-white/10 bg-white/[0.065] p-4">
                  <CheckCircle2 className="h-4 w-4 text-emerald-300" aria-hidden="true" />
                  <p className="mt-3 text-[10px] font-bold text-white/55">最近成功</p>
                  <p className="mt-1 text-xl font-black tabular-nums">
                    {polish?.last_summary ? totalSuccess : '—'}
                  </p>
                </div>
                <div className="rounded-2xl border border-white/10 bg-white/[0.065] p-4">
                  <Activity className="h-4 w-4 text-rose-300" aria-hidden="true" />
                  <p className="mt-3 text-[10px] font-bold text-white/55">最近失败</p>
                  <p className="mt-1 text-xl font-black tabular-nums">
                    {polish?.last_summary ? totalFail : '—'}
                  </p>
                </div>
                <div className="rounded-2xl border border-white/10 bg-white/[0.065] p-4">
                  <Eye className="h-4 w-4 text-blue-300" aria-hidden="true" />
                  <p className="mt-3 text-[10px] font-bold text-white/55">待诊断</p>
                  <p className="mt-1 text-xl font-black tabular-nums">
                    {itemState.loaded && !itemState.error ? pendingCount : '—'}
                  </p>
                </div>
              </div>
            </div>

            <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-white/10 pt-4">
              <p className="text-xs text-white/50">
                最近执行 {polish?.last_run ? shortTime(polish.last_run) : '尚无记录'}
              </p>
              <a
                href="/polish"
                className="inline-flex min-h-10 items-center gap-2 rounded-xl bg-white/[0.07] px-3 text-xs font-bold text-[#FFE815] hover:bg-white/[0.12] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815]"
              >
                管理擦亮计划
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </a>
            </div>
          </div>
        </section>

        <section className={`${card} overflow-hidden`} aria-labelledby="attention-title">
          <div className="flex items-start justify-between gap-3 border-b border-gray-100 p-5 dark:border-gray-700">
            <div>
              <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">
                ACTION QUEUE
              </p>
              <h3 id="attention-title" className="mt-1 text-lg font-black text-gray-950 dark:text-white">
                优先处理
              </h3>
            </div>
            <span className="rounded-full bg-gray-100 px-2.5 py-1 text-[10px] font-bold text-gray-600 dark:bg-[#25292E] dark:text-gray-300">
              {attentionEntries.length} 项
            </span>
          </div>

          <div className="divide-y divide-gray-100 dark:divide-gray-700">
            {attentionEntries.map((entry, index) => (
              <article key={`${entry.label}-${index}`} className="flex gap-3 px-4 py-3.5">
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                  entry.tone === 'red'
                    ? 'bg-rose-500'
                    : entry.tone === 'amber'
                      ? 'bg-amber-500'
                      : 'bg-blue-500'
                }`} />
                <div className="min-w-0">
                  <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-gray-400">
                    {entry.label}
                  </p>
                  <p className="mt-0.5 text-sm font-bold text-gray-900 dark:text-white">
                    {entry.title}
                  </p>
                  <p className="mt-1 line-clamp-2 text-[11px] leading-5 text-gray-500 dark:text-gray-400">
                    {entry.detail}
                  </p>
                </div>
              </article>
            ))}
            {!attentionEntries.length && (
              <div className="flex min-h-40 flex-col items-center justify-center px-5 text-center">
                <CheckCircle2 className="h-7 w-7 text-emerald-500" aria-hidden="true" />
                <p className="mt-2 text-sm font-bold text-gray-900 dark:text-white">
                  当前没有明确异常
                </p>
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  数据缺失和擦亮失败都会在这里单独标出。
                </p>
              </div>
            )}
          </div>
        </section>
      </div>

      <section className="space-y-3" aria-labelledby="activity-ranking-title">
        <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
          <div>
            <div className="flex items-center gap-2">
              <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">
                ACTIVITY RANKING
              </p>
              <TrendingUp className="h-4 w-4 text-amber-500" aria-hidden="true" />
            </div>
            <h3 id="activity-ranking-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white">
              成交热度排行
            </h3>
            <p className="mt-1 max-w-3xl text-sm leading-5 text-gray-500 dark:text-gray-400">
              按商品详情累计成交优先排序，再用想要与浏览打破同分。榜单不等于本期成交额。
            </p>
          </div>
          <div className="flex flex-wrap gap-2 text-[10px] font-bold">
            <span className="rounded-full bg-amber-100 px-2.5 py-1.5 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200">
              {activityRows.length} 件有诊断热度
            </span>
            <span className="rounded-full bg-blue-100 px-2.5 py-1.5 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200">
              {reportedOrderTotal === null
                ? '本期订单汇总未返回'
                : `报告内本期订单 ${reportedOrderTotal}`}
            </span>
          </div>
        </div>

        <div className={`${card} overflow-hidden`}>
          {(itemState.refreshing && !itemState.loaded)
            || (!itemState.loaded || !diagnosis.loaded) ? (
              <div className="grid gap-px bg-gray-100 dark:bg-gray-700 md:grid-cols-2" aria-label="正在生成成交热度排行">
                {[0, 1, 2, 3].map(index => (
                  <div key={index} className="h-24 animate-pulse bg-white p-4 dark:bg-[#1B1E22]">
                    <div className="h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-800" />
                    <div className="mt-4 h-9 w-full rounded-xl bg-gray-100 dark:bg-gray-800" />
                  </div>
                ))}
              </div>
            ) : topActivityRows.length ? (
              <div className="divide-y divide-gray-100 dark:divide-gray-700">
                {topActivityRows.map((entry, index) => (
                  <article key={itemKey(entry.item.cookie_id, entry.item.item_id)} className="grid gap-3 px-4 py-4 sm:grid-cols-[42px_minmax(0,1fr)_auto] sm:items-center">
                    <div className={`flex h-10 w-10 items-center justify-center rounded-xl text-sm font-black tabular-nums ${
                      index < 3
                        ? 'bg-emerald-500 text-black'
                        : 'bg-gray-100 text-gray-500 dark:bg-[#25292E] dark:text-gray-300'
                    }`}>
                      {String(index + 1).padStart(2, '0')}
                    </div>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-black text-gray-950 dark:text-white">
                        {entry.row.title || entry.item.item_title || entry.item.item_id}
                      </p>
                      <p className="mt-1 truncate text-[10px] text-gray-500 dark:text-gray-400">
                        {accountName(entry.account)} · {entry.row.classify_label || '待分类'} · 诊断 {shortTime(entry.report._at)}
                      </p>
                    </div>
                    <div className="grid grid-cols-3 gap-4 text-right sm:min-w-[250px]">
                      <div>
                        <p className="text-[9px] font-bold text-gray-400">累计成交</p>
                        <p className="mt-1 text-base font-black tabular-nums text-gray-950 dark:text-white">
                          {countLabel(entry.sold)}
                        </p>
                      </div>
                      <div>
                        <p className="text-[9px] font-bold text-gray-400">想要</p>
                        <p className="mt-1 text-base font-black tabular-nums text-gray-950 dark:text-white">
                          {countLabel(entry.want)}
                        </p>
                      </div>
                      <div>
                        <p className="text-[9px] font-bold text-gray-400">浏览</p>
                        <p className="mt-1 text-base font-black tabular-nums text-gray-950 dark:text-white">
                          {countLabel(entry.browse)}
                        </p>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className="flex min-h-40 flex-col items-center justify-center px-6 text-center">
                <TrendingUp className="h-8 w-8 text-gray-300 dark:text-gray-600" aria-hidden="true" />
                <p className="mt-3 text-sm font-black text-gray-950 dark:text-white">
                  {items.length ? '暂时没有可排名的诊断热度' : '还没有同步商品'}
                </p>
                <p className="mt-1 max-w-md text-xs leading-5 text-gray-500 dark:text-gray-400">
                  {items.length
                    ? '运行店铺诊断并成功读取累计成交、想要或浏览后，这里会自动生成榜单。'
                    : '完成商品同步后，诊断得到的成交热度会直接显示在这里。'}
                </p>
              </div>
            )}
        </div>

        <div className="flex flex-col justify-between gap-2 rounded-xl border border-gray-200 bg-white px-4 py-3 text-[11px] leading-5 text-gray-500 dark:border-gray-700 dark:bg-[#1B1E22] dark:text-gray-400 sm:flex-row">
          <span>商品累计成交来自详情接口，适合观察商品长期热度。</span>
          <span className="font-bold text-gray-700 dark:text-gray-200">
            {reportedOrderTotal === null
              ? '本期真实订单接口未提供可用汇总'
              : `已读取 ${salesSnapshotReports.length} 个报告的订单汇总`}
          </span>
        </div>
      </section>

      <section className="space-y-3" aria-labelledby="account-status-title">
        <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-end">
          <div>
            <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">
              ACCOUNT LAYER
            </p>
            <h3 id="account-status-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white">
              账号执行状态
            </h3>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              每个账号的计划、商品覆盖、需求信号与最近批次直接显示。
            </p>
          </div>
          <p className="text-xs font-bold tabular-nums text-gray-500 dark:text-gray-400">
            {accountState.loaded ? `${accounts.length} 个账号` : '正在读取账号'}
          </p>
        </div>

        {!accountState.loaded && (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="正在读取账号">
            {[0, 1].map(index => (
              <div key={index} className={`${card} h-56 animate-pulse p-5`}>
                <div className="h-5 w-2/3 rounded bg-gray-100 dark:bg-gray-800" />
                <div className="mt-5 h-20 rounded-xl bg-gray-100 dark:bg-gray-800" />
                <div className="mt-4 h-6 w-1/2 rounded bg-gray-100 dark:bg-gray-800" />
              </div>
            ))}
          </div>
        )}

        {accountState.loaded && Boolean(accountMetrics.length) && (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {accountMetrics.map(metric => {
              const { account, summary } = metric;
              const coverage = metric.items.length
                ? Math.round(metric.diagnosed.length / metric.items.length * 100)
                : 0;
              const batchTotal = summary ? summary.success + summary.fail : 0;
              const successRate = batchTotal
                ? Math.round(summary!.success / batchTotal * 100)
                : null;
              return (
                <article key={account.id} className={`${card} overflow-hidden`}>
                  <div className="flex items-start justify-between gap-3 border-b border-gray-100 p-4 dark:border-gray-700">
                    <div className="flex min-w-0 items-center gap-3">
                      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[#FFE815] text-sm font-black text-black">
                        {accountName(account).slice(0, 1)}
                      </div>
                      <div className="min-w-0">
                        <p className="truncate text-sm font-black text-gray-950 dark:text-white">
                          {accountName(account)}
                        </p>
                        <p className="mt-0.5 truncate text-[11px] tabular-nums text-gray-500 dark:text-gray-400">
                          {account.id}
                        </p>
                      </div>
                    </div>
                    <AccountConnectionBadge account={account} />
                  </div>

                  <div className="p-4">
                    <div className="flex items-center justify-between gap-3 rounded-xl bg-[#191B1F] px-3 py-2.5 text-white">
                      <span className="text-[11px] font-semibold text-white/55">今日擦亮计划</span>
                      <span className="text-xs font-black tabular-nums text-[#FFE815]">
                        {polishState.loaded
                          ? polish?.settings?.enabled
                            ? `每天 ${schedule}`
                            : '已关闭'
                          : '读取中'}
                      </span>
                    </div>

                    <div className="mt-4">
                      <div className="flex items-center justify-between text-[11px]">
                        <span className="font-semibold text-gray-500 dark:text-gray-400">诊断覆盖</span>
                        <span className="font-black tabular-nums text-gray-900 dark:text-white">
                          {metric.diagnosed.length} / {metric.items.length}
                        </span>
                      </div>
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
                        <div
                          className="h-full rounded-full bg-[#FFE815]"
                          style={{ width: `${coverage}%` }}
                        />
                      </div>
                    </div>

                    <div className="mt-4 grid grid-cols-3 divide-x divide-gray-100 border-y border-gray-100 py-3 dark:divide-gray-700 dark:border-gray-700">
                      <div className="px-2 text-center">
                        <p className="text-[10px] text-gray-400">浏览</p>
                        <p className="mt-1 text-lg font-black tabular-nums text-gray-950 dark:text-white">
                          {countLabel(metric.browse)}
                        </p>
                      </div>
                      <div className="px-2 text-center">
                        <p className="text-[10px] text-gray-400">想要</p>
                        <p className="mt-1 text-lg font-black tabular-nums text-gray-950 dark:text-white">
                          {countLabel(metric.want)}
                        </p>
                      </div>
                      <div className="px-2 text-center">
                        <p className="text-[10px] text-gray-400">成交</p>
                        <p className="mt-1 text-lg font-black tabular-nums text-gray-950 dark:text-white">
                          {countLabel(metric.sold)}
                        </p>
                      </div>
                    </div>

                    <div className="mt-4 flex items-end justify-between gap-3">
                      <div>
                        <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-gray-400">
                          最近批次
                        </p>
                        <p className="mt-1 text-xs font-bold text-gray-800 dark:text-gray-200">
                          {summary
                            ? `成功 ${summary.success} · 失败 ${summary.fail}`
                            : '暂无批次记录'}
                        </p>
                      </div>
                      <div className="text-right">
                        <p className="text-xl font-black tabular-nums text-gray-950 dark:text-white">
                          {successRate === null ? '—' : `${successRate}%`}
                        </p>
                        <p className="text-[10px] text-gray-400">成功率</p>
                      </div>
                    </div>

                    {(metric.highSignals > 0 || metric.failedLogs > 0) && (
                      <div className="mt-4 flex flex-wrap gap-2">
                        {metric.highSignals > 0 && (
                          <span className="rounded-full bg-amber-100 px-2.5 py-1 text-[10px] font-bold text-amber-800 dark:bg-amber-950/50 dark:text-amber-200">
                            {metric.highSignals} 件高危
                          </span>
                        )}
                        {metric.failedLogs > 0 && (
                          <span className="rounded-full bg-rose-100 px-2.5 py-1 text-[10px] font-bold text-rose-800 dark:bg-rose-950/50 dark:text-rose-200">
                            {metric.failedLogs} 次擦亮失败
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}

        {accountState.loaded && !accountState.error && !accounts.length && (
          <div className={`${card} flex min-h-44 flex-col items-center justify-center px-6 text-center`}>
            <Users className="h-7 w-7 text-gray-300 dark:text-gray-600" aria-hidden="true" />
            <p className="mt-3 text-sm font-bold text-gray-900 dark:text-white">暂无店铺账号</p>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              先在账号管理中完成闲鱼账号接入。
            </p>
          </div>
        )}
      </section>

      <section className="space-y-4" aria-labelledby="product-exposure-title">
        <div className="flex flex-col justify-between gap-4 xl:flex-row xl:items-end">
          <div>
            <p className="text-[10px] font-black uppercase tracking-[0.18em] text-gray-400">
              ITEM LAYER
            </p>
            <h3 id="product-exposure-title" className="mt-1 text-xl font-black text-gray-950 dark:text-white">
              商品曝光面板
            </h3>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              浏览、想要、成交、诊断结论和擦亮结果直接显示在每张商品卡上。
            </p>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <label className="relative block w-full lg:w-[260px]">
              <span className="sr-only">搜索商品或账号</span>
              <Search
                className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
                aria-hidden="true"
              />
              <input
                value={searchText}
                onChange={event => setSearchText(event.target.value)}
                placeholder="搜索商品、商品 ID 或账号"
                className="min-h-10 w-full rounded-xl border border-gray-200 bg-white pl-9 pr-3 text-sm text-gray-900 outline-none focus:border-[#FFE815] focus:ring-2 focus:ring-[#FFE815]/25 dark:border-gray-700 dark:bg-[#1B1E22] dark:text-white"
              />
            </label>
            <label className="sr-only" htmlFor="exposure-account">筛选账号</label>
            <select
              id="exposure-account"
              value={accountFilter}
              onChange={event => setAccountFilter(event.target.value)}
              className="min-h-10 rounded-xl border border-gray-200 bg-white px-3 text-sm text-gray-800 focus:border-[#FFE815] focus:outline-none focus:ring-2 focus:ring-[#FFE815]/25 dark:border-gray-700 dark:bg-[#1B1E22] dark:text-gray-100"
            >
              <option value="">全部账号</option>
              {accounts.map(account => (
                <option key={account.id} value={account.id}>{accountName(account)}</option>
              ))}
            </select>
            <div
              className="inline-flex flex-wrap rounded-xl border border-gray-200 bg-white p-1 dark:border-gray-700 dark:bg-[#1B1E22]"
              role="group"
              aria-label="商品诊断筛选"
            >
              {([
                ['all', '全部'],
                ['attention', '优先处理'],
                ['diagnosed', '已诊断'],
                ['pending', '待诊断'],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={visibilityFilter === value}
                  onClick={() => setVisibilityFilter(value)}
                  className={`min-h-8 rounded-lg px-2.5 text-xs font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] ${
                    visibilityFilter === value
                      ? 'bg-[#FFE815] text-black'
                      : 'text-gray-500 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-[#25292E] dark:hover:text-white'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            当前显示 {visibleItems.length} / {accountItems.length} 件商品
          </p>
          <p className="text-xs font-bold text-gray-500 dark:text-gray-400">
            诊断覆盖 {diagnosedCount} / {items.length}
          </p>
        </div>

        {!itemState.loaded && (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="正在读取商品">
            {[0, 1, 2].map(index => (
              <div key={index} className={`${card} h-64 animate-pulse p-5`}>
                <div className="h-14 w-14 rounded-xl bg-gray-100 dark:bg-gray-800" />
                <div className="mt-4 h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-800" />
                <div className="mt-6 h-20 rounded-xl bg-gray-100 dark:bg-gray-800" />
                <div className="mt-4 h-8 w-1/2 rounded bg-gray-100 dark:bg-gray-800" />
              </div>
            ))}
          </div>
        )}

        {itemState.loaded && Boolean(visibleItems.length) && (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {visibleItems.map(item => {
              const key = itemKey(item.cookie_id, item.item_id);
              const hit = reportByItem.get(key);
              const row = hit?.row;
              const log = lastLogByItem.get(key);
              const diagnosisError = diagnosis.errors[item.cookie_id];
              const signal = row?.signals?.find(value => value.code !== 'fresh');
              const badge = diagnosisError
                ? { label: '诊断异常', className: 'bg-rose-100 text-rose-800 dark:bg-rose-950/50 dark:text-rose-200' }
                : row?.stale
                  ? { label: '上次数据', className: 'bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200' }
                  : row?.classify_label
                    ? {
                        label: row.classify_label,
                        className: row.classify === 'cull' || row.classify === 'zombie'
                          ? 'bg-rose-100 text-rose-800 dark:bg-rose-950/50 dark:text-rose-200'
                          : row.classify === 'potential' || row.classify === 'hot'
                            ? 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200'
                            : 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200',
                      }
                    : { label: '待诊断', className: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300' };
              return (
                <article key={key} className={`${card} group flex min-w-0 flex-col overflow-hidden`}>
                  <div className="flex gap-3 border-b border-gray-100 p-4 dark:border-gray-700">
                    <ItemThumbnail url={item.item_image} />
                    <div className="min-w-0 flex-1">
                      <h4 className="line-clamp-2 text-sm font-black leading-5 text-gray-950 dark:text-white">
                        {item.item_title || item.item_id}
                      </h4>
                      <p className="mt-1 truncate text-[11px] text-gray-500 dark:text-gray-400">
                        {accountName(accountMap.get(item.cookie_id))} · {item.item_id}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <span className={`rounded-full px-2 py-1 text-[10px] font-bold ${badge.className}`}>
                          {badge.label}
                        </span>
                        {item.item_price && (
                          <span className="text-[11px] font-black tabular-nums text-gray-700 dark:text-gray-200">
                            ¥{item.item_price}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="grid grid-cols-3 divide-x divide-gray-100 px-2 py-4 dark:divide-gray-700">
                    <div className="px-2 text-center">
                      <p className="text-[10px] font-semibold text-gray-500 dark:text-gray-400">浏览</p>
                      <p className="mt-1 text-2xl font-black tabular-nums text-gray-950 dark:text-white">
                        {countLabel(row?.browse)}
                      </p>
                    </div>
                    <div className="px-2 text-center">
                      <p className="text-[10px] font-semibold text-gray-500 dark:text-gray-400">想要</p>
                      <p className="mt-1 text-2xl font-black tabular-nums text-gray-950 dark:text-white">
                        {countLabel(row?.want)}
                      </p>
                    </div>
                    <div className="px-2 text-center">
                      <p className="text-[10px] font-semibold text-gray-500 dark:text-gray-400">成交</p>
                      <p className="mt-1 text-2xl font-black tabular-nums text-gray-950 dark:text-white">
                        {countLabel(row?.sold)}
                      </p>
                    </div>
                  </div>

                  <div className="mt-auto space-y-2.5 bg-gray-50/80 p-4 dark:bg-[#202328]">
                    <div className="flex items-start justify-between gap-3 text-xs">
                      <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
                        <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                        近期擦亮
                      </span>
                      <span className={`text-right font-bold ${
                        log
                          ? log.success
                            ? 'text-emerald-700 dark:text-emerald-300'
                            : 'text-rose-700 dark:text-rose-300'
                          : 'text-gray-500 dark:text-gray-400'
                      }`}>
                        {log ? `${log.success ? '成功' : '失败'} · ${shortTime(log.time)}` : '暂无记录'}
                      </span>
                    </div>
                    <div className="flex items-start justify-between gap-3 text-xs">
                      <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
                        <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                        诊断时间
                      </span>
                      <span className="text-right font-medium text-gray-700 dark:text-gray-200">
                        {row && hit ? shortTime(hit.report._at) : diagnosisError ? '读取失败' : '尚未诊断'}
                      </span>
                    </div>
                    {signal && (
                      <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 dark:border-amber-900/50 dark:bg-[#342F1F]">
                        <p className="text-[10px] font-bold text-amber-800 dark:text-amber-200">
                          {signal.title}
                        </p>
                        <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-amber-700/80 dark:text-amber-100/70">
                          {signal.action}
                        </p>
                      </div>
                    )}
                    {diagnosisError && (
                      <p className="line-clamp-2 rounded-xl bg-rose-50 px-3 py-2 text-[10px] leading-4 text-rose-700 dark:bg-[#352327] dark:text-rose-200">
                        {diagnosisError}
                      </p>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}

        {itemState.loaded && !itemState.error && !visibleItems.length && (
          <div className={`${card} flex min-h-52 flex-col items-center justify-center px-6 text-center`}>
            <PackageOpen className="h-8 w-8 text-gray-300 dark:text-gray-600" aria-hidden="true" />
            <p className="mt-3 text-sm font-black text-gray-950 dark:text-white">
              {items.length ? '当前筛选没有商品' : '还没有同步商品'}
            </p>
            <p className="mt-1 max-w-sm text-xs leading-5 text-gray-500 dark:text-gray-400">
              {items.length
                ? '清空搜索、切换账号或更换诊断状态。'
                : '完成商品同步后，浏览、想要、成交和擦亮结果会直接显示在这里。'}
            </p>
            {items.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  setSearchText('');
                  setAccountFilter('');
                  setVisibilityFilter('all');
                }}
                className="mt-4 min-h-10 rounded-xl bg-[#FFE815] px-4 text-xs font-bold text-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black"
              >
                清除全部筛选
              </button>
            )}
          </div>
        )}

        <div className="flex flex-col justify-between gap-2 rounded-xl border border-gray-200 bg-white px-4 py-3 text-xs leading-5 text-gray-500 dark:border-gray-700 dark:bg-[#1B1E22] dark:text-gray-400 sm:flex-row">
          <span className="flex items-start gap-2">
            <Store className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            当前没有平台实际曝光数，页面只使用店铺诊断能取得的浏览、想要和成交字段。
          </span>
          <span className="flex items-center gap-2 font-bold text-gray-700 dark:text-gray-200">
            <ShoppingBag className="h-4 w-4" aria-hidden="true" />
            成交字段为商品详情累计成交
          </span>
        </div>
      </section>
    </section>
  );
}
