import React, { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  BadgeCheck,
  CircleOff,
  Edit3,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  ShieldBan,
  ShieldOff,
  Trash2,
  UserCheck,
  X,
} from 'lucide-react';
import {
  createRefundBlacklist,
  deleteRefundBlacklist,
  getAccountDetails,
  getRefundBlacklist,
  updateRefundBlacklist,
} from '../services/api';
import type { AccountDetail, RefundBlacklistEntry, RefundBlacklistPayload } from '../types';

type StatusFilter = 'all' | 'active' | 'whitelist' | 'disabled';
type EntryStatus = 'active' | 'whitelist' | 'disabled';

interface EntryForm {
  buyer_id: string;
  cookie_id: string;
  order_id: string;
  item_id: string;
  reason: string;
  note: string;
  status: EntryStatus;
}

const emptyForm = (cookieId = ''): EntryForm => ({
  buyer_id: '',
  cookie_id: cookieId,
  order_id: '',
  item_id: '',
  reason: 'manual',
  note: '',
  status: 'active',
});

const entryStatus = (entry: RefundBlacklistEntry): EntryStatus => {
  if (Boolean(entry.is_whitelisted)) return 'whitelist';
  return Boolean(entry.enabled) ? 'active' : 'disabled';
};

const formatDate = (value?: string) => {
  if (!value) return '-';
  const parsed = new Date(value.replace(' ', 'T') + (value.includes('Z') ? '' : 'Z'));
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', { hour12: false });
};

const BlacklistManager: React.FC = () => {
  const [entries, setEntries] = useState<RefundBlacklistEntry[]>([]);
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [keyword, setKeyword] = useState('');
  const [accountFilter, setAccountFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [editing, setEditing] = useState<RefundBlacklistEntry | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState<EntryForm>(emptyForm());

  const loadData = async () => {
    setLoading(true);
    setError('');
    try {
      const [blacklistEntries, accountEntries] = await Promise.all([
        getRefundBlacklist(),
        getAccountDetails(),
      ]);
      setEntries(blacklistEntries);
      setAccounts(accountEntries);
    } catch (requestError) {
      setError((requestError as Error).message || '加载黑名单失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const accountNames = useMemo(
    () => new Map(accounts.map(account => [account.id, account.remark || account.nickname || account.id])),
    [accounts],
  );

  const counts = useMemo(() => ({
    active: entries.filter(entry => entryStatus(entry) === 'active').length,
    whitelist: entries.filter(entry => entryStatus(entry) === 'whitelist').length,
    disabled: entries.filter(entry => entryStatus(entry) === 'disabled').length,
  }), [entries]);

  const filteredEntries = useMemo(() => {
    const normalizedKeyword = keyword.trim().toLowerCase();
    return entries.filter(entry => {
      if (accountFilter !== 'all' && entry.cookie_id !== accountFilter) return false;
      if (statusFilter !== 'all' && entryStatus(entry) !== statusFilter) return false;
      if (!normalizedKeyword) return true;
      return [
        entry.buyer_id,
        entry.cookie_id,
        entry.first_order_id,
        entry.last_order_id,
        entry.first_item_id,
        entry.last_item_id,
        entry.reason,
        entry.note,
      ].some(value => String(value || '').toLowerCase().includes(normalizedKeyword));
    });
  }, [accountFilter, entries, keyword, statusFilter]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm(accountFilter !== 'all' ? accountFilter : accounts[0]?.id || ''));
    setError('');
    setShowModal(true);
  };

  const openEdit = (entry: RefundBlacklistEntry) => {
    setEditing(entry);
    setForm({
      buyer_id: entry.buyer_id,
      cookie_id: entry.cookie_id,
      order_id: entry.last_order_id || '',
      item_id: entry.last_item_id || '',
      reason: entry.reason || '',
      note: entry.note || '',
      status: entryStatus(entry),
    });
    setError('');
    setShowModal(true);
  };

  const statusPayload = (status: EntryStatus) => ({
    enabled: status === 'active',
    is_whitelisted: status === 'whitelist',
  });

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!form.buyer_id.trim() || !form.cookie_id) return;
    setSaving(true);
    setError('');
    try {
      const payload: RefundBlacklistPayload = {
        buyer_id: form.buyer_id.trim(),
        cookie_id: form.cookie_id,
        order_id: form.order_id.trim(),
        item_id: form.item_id.trim(),
        reason: form.reason.trim() || 'manual',
        note: form.note.trim(),
        ...statusPayload(form.status),
      };
      if (editing) {
        await updateRefundBlacklist(editing.id, payload);
        setNotice('记录已更新');
      } else {
        await createRefundBlacklist(payload);
        setNotice('记录已添加');
      }
      setShowModal(false);
      await loadData();
    } catch (requestError) {
      setError((requestError as Error).message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const applyUpdate = async (entry: RefundBlacklistEntry, payload: Partial<RefundBlacklistPayload>, message: string) => {
    setError('');
    try {
      await updateRefundBlacklist(entry.id, payload);
      setNotice(message);
      await loadData();
    } catch (requestError) {
      setError((requestError as Error).message || '操作失败');
    }
  };

  const handleDelete = async (entry: RefundBlacklistEntry) => {
    if (!window.confirm(`确认彻底删除买家 ${entry.buyer_id} 的记录吗？`)) return;
    setError('');
    try {
      await deleteRefundBlacklist(entry.id);
      setNotice('记录已删除');
      await loadData();
    } catch (requestError) {
      setError((requestError as Error).message || '删除失败');
    }
  };

  const statusBadge = (entry: RefundBlacklistEntry) => {
    const status = entryStatus(entry);
    if (status === 'whitelist') {
      return <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 text-xs font-bold"><BadgeCheck className="w-3.5 h-3.5" />永久白名单</span>;
    }
    if (status === 'disabled') {
      return <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-xs font-bold"><CircleOff className="w-3.5 h-3.5" />已解除</span>;
    }
    return <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300 text-xs font-bold"><ShieldBan className="w-3.5 h-3.5" />生效中</span>;
  };

  return (
    <div className="max-w-[1400px] mx-auto space-y-6 animate-fade-in pb-16">
      <div className="flex flex-wrap items-center justify-between gap-4 pr-14">
        <div className="flex items-center gap-4">
          <div className="w-11 h-11 rounded-lg bg-red-50 dark:bg-red-950/40 flex items-center justify-center">
            <ShieldBan className="w-5 h-5 text-red-600 dark:text-red-300" />
          </div>
          <div>
            <h2 className="text-2xl font-extrabold text-gray-900 dark:text-white">黑名单管理</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">本地买家风控记录</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => void loadData()} title="刷新" className="w-10 h-10 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1B1E22] text-gray-600 dark:text-gray-300 flex items-center justify-center hover:bg-gray-50 dark:hover:bg-[#25292E] transition-colors">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button type="button" onClick={openCreate} disabled={!accounts.length} className="h-10 px-4 rounded-lg bg-[#FFE815] text-black font-bold text-sm flex items-center gap-2 hover:bg-yellow-300 disabled:opacity-50 transition-colors">
            <Plus className="w-4 h-4" />添加记录
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="bg-white dark:bg-[#1B1E22] border border-gray-200 dark:border-gray-700 rounded-lg px-5 py-4 flex items-center justify-between">
          <span className="text-sm font-bold text-gray-500 dark:text-gray-400">生效中</span><span className="text-2xl font-extrabold text-red-600 dark:text-red-300">{counts.active}</span>
        </div>
        <div className="bg-white dark:bg-[#1B1E22] border border-gray-200 dark:border-gray-700 rounded-lg px-5 py-4 flex items-center justify-between">
          <span className="text-sm font-bold text-gray-500 dark:text-gray-400">永久白名单</span><span className="text-2xl font-extrabold text-emerald-600 dark:text-emerald-300">{counts.whitelist}</span>
        </div>
        <div className="bg-white dark:bg-[#1B1E22] border border-gray-200 dark:border-gray-700 rounded-lg px-5 py-4 flex items-center justify-between">
          <span className="text-sm font-bold text-gray-500 dark:text-gray-400">已解除</span><span className="text-2xl font-extrabold text-gray-600 dark:text-gray-300">{counts.disabled}</span>
        </div>
      </div>

      {(error || notice) && (
        <div className={`px-4 py-3 rounded-lg border text-sm font-bold ${error ? 'bg-red-50 dark:bg-red-950/40 border-red-200 dark:border-red-900 text-red-700 dark:text-red-300' : 'bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-900 text-emerald-700 dark:text-emerald-300'}`}>
          {error || notice}
        </div>
      )}

      <div className="flex flex-col xl:flex-row xl:items-center gap-3">
        <div className="relative flex-1 min-w-0">
          <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索买家、订单、商品或备注" className="ios-input w-full h-11 pl-10 pr-4 rounded-lg text-sm dark:bg-[#1B1E22] dark:border-gray-700 dark:text-white" />
        </div>
        <select value={accountFilter} onChange={event => setAccountFilter(event.target.value)} className="ios-input h-11 px-3 rounded-lg text-sm min-w-48 dark:bg-[#1B1E22] dark:border-gray-700 dark:text-white">
          <option value="all">全部账号</option>
          {accounts.map(account => <option key={account.id} value={account.id}>{account.remark || account.nickname || account.id}</option>)}
        </select>
        <div className="flex items-center gap-1 p-1 rounded-lg bg-gray-100 dark:bg-[#25292E] overflow-x-auto" role="group" aria-label="状态筛选">
          {([
            ['all', '全部'],
            ['active', '生效中'],
            ['whitelist', '白名单'],
            ['disabled', '已解除'],
          ] as Array<[StatusFilter, string]>).map(([value, label]) => (
            <button key={value} type="button" onClick={() => setStatusFilter(value)} aria-pressed={statusFilter === value} className={`h-9 px-3 rounded-md text-sm font-bold whitespace-nowrap transition-colors ${statusFilter === value ? 'bg-white dark:bg-[#34383F] text-gray-900 dark:text-white shadow-sm' : 'text-gray-500 dark:text-gray-400'}`}>{label}</button>
          ))}
        </div>
      </div>

      <div className="bg-white dark:bg-[#1B1E22] border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
        <div className="overflow-x-auto min-h-[360px]">
          {loading ? (
            <div className="h-[360px] flex items-center justify-center"><Loader2 className="w-6 h-6 animate-spin text-gray-400" /></div>
          ) : filteredEntries.length === 0 ? (
            <div className="h-[360px] flex flex-col items-center justify-center text-gray-400 gap-3"><ShieldOff className="w-9 h-9" /><span className="text-sm font-medium">暂无记录</span></div>
          ) : (
            <table className="w-full min-w-[1050px] text-left border-collapse">
              <thead className="bg-gray-50 dark:bg-[#25292E] border-b border-gray-200 dark:border-gray-700">
                <tr className="text-xs text-gray-500 dark:text-gray-400">
                  <th className="px-5 py-3 font-bold">买家</th>
                  <th className="px-5 py-3 font-bold">账号</th>
                  <th className="px-5 py-3 font-bold">状态</th>
                  <th className="px-5 py-3 font-bold">退款凭证</th>
                  <th className="px-5 py-3 font-bold">原因 / 备注</th>
                  <th className="px-5 py-3 font-bold">更新时间</th>
                  <th className="px-5 py-3 font-bold text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {filteredEntries.map(entry => {
                  const status = entryStatus(entry);
                  return (
                    <tr key={entry.id} className="hover:bg-gray-50/70 dark:hover:bg-[#22262B] transition-colors">
                      <td className="px-5 py-4"><div className="font-bold text-sm text-gray-900 dark:text-white break-all max-w-44">{entry.buyer_id}</div><div className="text-xs text-gray-400 mt-1">退款 {entry.refund_count || 0} 次</div></td>
                      <td className="px-5 py-4"><div className="text-sm font-medium text-gray-700 dark:text-gray-200 max-w-40 truncate" title={entry.cookie_id}>{accountNames.get(entry.cookie_id) || entry.cookie_id}</div><div className="text-xs text-gray-400 mt-1 max-w-40 truncate">{entry.cookie_id}</div></td>
                      <td className="px-5 py-4">{statusBadge(entry)}</td>
                      <td className="px-5 py-4"><div className="text-sm text-gray-700 dark:text-gray-200 max-w-44 truncate" title={entry.last_order_id || ''}>订单：{entry.last_order_id || '-'}</div><div className="text-xs text-gray-400 mt-1 max-w-44 truncate" title={entry.last_item_id || ''}>商品：{entry.last_item_id || '-'}</div></td>
                      <td className="px-5 py-4"><div className="text-sm text-gray-700 dark:text-gray-200 max-w-52 truncate" title={entry.reason}>{entry.reason || '-'}</div><div className="text-xs text-gray-400 mt-1 max-w-52 truncate" title={entry.note || ''}>{entry.note || '无备注'}</div></td>
                      <td className="px-5 py-4 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">{formatDate(entry.updated_at)}</td>
                      <td className="px-5 py-4">
                        <div className="flex items-center justify-end gap-1">
                          <button type="button" onClick={() => openEdit(entry)} title="编辑" aria-label="编辑" className="w-9 h-9 rounded-md flex items-center justify-center text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-white"><Edit3 className="w-4 h-4" /></button>
                          {status !== 'whitelist' && (
                            <button type="button" onClick={() => void applyUpdate(entry, status === 'active' ? { enabled: false } : { enabled: true, is_whitelisted: false }, status === 'active' ? '已解除黑名单' : '黑名单已恢复')} title={status === 'active' ? '解除黑名单' : '重新拉黑'} aria-label={status === 'active' ? '解除黑名单' : '重新拉黑'} className="w-9 h-9 rounded-md flex items-center justify-center text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-white">
                              {status === 'active' ? <ShieldOff className="w-4 h-4" /> : <RotateCcw className="w-4 h-4" />}
                            </button>
                          )}
                          <button type="button" onClick={() => void applyUpdate(entry, status === 'whitelist' ? { is_whitelisted: false, enabled: false } : { is_whitelisted: true, enabled: false }, status === 'whitelist' ? '已移出永久白名单' : '已设为永久白名单')} title={status === 'whitelist' ? '移出永久白名单' : '设为永久白名单'} aria-label={status === 'whitelist' ? '移出永久白名单' : '设为永久白名单'} className={`w-9 h-9 rounded-md flex items-center justify-center ${status === 'whitelist' ? 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-300' : 'text-gray-500 hover:bg-emerald-50 dark:hover:bg-emerald-950/40 hover:text-emerald-600'}`}><UserCheck className="w-4 h-4" /></button>
                          <button type="button" onClick={() => void handleDelete(entry)} title="彻底删除" aria-label="彻底删除" className="w-9 h-9 rounded-md flex items-center justify-center text-gray-500 hover:bg-red-50 dark:hover:bg-red-950/40 hover:text-red-600 dark:hover:text-red-300"><Trash2 className="w-4 h-4" /></button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {showModal && createPortal(
        <div className="fixed inset-0 z-[10001] bg-black/55 backdrop-blur-sm p-4 flex items-center justify-center" role="dialog" aria-modal="true" aria-labelledby="blacklist-modal-title">
          <form onSubmit={handleSubmit} className="w-full max-w-2xl max-h-[90vh] overflow-y-auto bg-white dark:bg-[#1B1E22] border border-gray-200 dark:border-gray-700 rounded-lg shadow-2xl">
            <div className="h-16 px-5 flex items-center justify-between border-b border-gray-200 dark:border-gray-700 sticky top-0 bg-white dark:bg-[#1B1E22] z-10">
              <h3 id="blacklist-modal-title" className="text-lg font-extrabold text-gray-900 dark:text-white">{editing ? '编辑记录' : '添加记录'}</h3>
              <button type="button" onClick={() => setShowModal(false)} title="关闭" aria-label="关闭" className="w-9 h-9 rounded-md flex items-center justify-center text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"><X className="w-4 h-4" /></button>
            </div>
            <div className="p-5 grid grid-cols-1 md:grid-cols-2 gap-4">
              <label className="space-y-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">店铺账号</span><select required value={form.cookie_id} onChange={event => setForm(current => ({ ...current, cookie_id: event.target.value }))} className="ios-input w-full h-11 px-3 rounded-lg text-sm dark:bg-[#25292E] dark:border-gray-700 dark:text-white"><option value="" disabled>请选择账号</option>{accounts.map(account => <option key={account.id} value={account.id}>{account.remark || account.nickname || account.id}</option>)}</select></label>
              <label className="space-y-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">买家 ID</span><input required maxLength={200} value={form.buyer_id} onChange={event => setForm(current => ({ ...current, buyer_id: event.target.value }))} className="ios-input w-full h-11 px-3 rounded-lg text-sm dark:bg-[#25292E] dark:border-gray-700 dark:text-white" /></label>
              <label className="space-y-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">状态</span><select value={form.status} onChange={event => setForm(current => ({ ...current, status: event.target.value as EntryStatus }))} className="ios-input w-full h-11 px-3 rounded-lg text-sm dark:bg-[#25292E] dark:border-gray-700 dark:text-white"><option value="active">生效中</option><option value="disabled">已解除</option><option value="whitelist">永久白名单</option></select></label>
              <label className="space-y-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">原因</span><input maxLength={500} value={form.reason} onChange={event => setForm(current => ({ ...current, reason: event.target.value }))} className="ios-input w-full h-11 px-3 rounded-lg text-sm dark:bg-[#25292E] dark:border-gray-700 dark:text-white" /></label>
              <label className="space-y-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">订单 ID</span><input maxLength={200} value={form.order_id} onChange={event => setForm(current => ({ ...current, order_id: event.target.value }))} className="ios-input w-full h-11 px-3 rounded-lg text-sm dark:bg-[#25292E] dark:border-gray-700 dark:text-white" /></label>
              <label className="space-y-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">商品 ID</span><input maxLength={200} value={form.item_id} onChange={event => setForm(current => ({ ...current, item_id: event.target.value }))} className="ios-input w-full h-11 px-3 rounded-lg text-sm dark:bg-[#25292E] dark:border-gray-700 dark:text-white" /></label>
              <label className="space-y-2 md:col-span-2"><span className="text-sm font-bold text-gray-700 dark:text-gray-200">备注</span><textarea maxLength={1000} rows={4} value={form.note} onChange={event => setForm(current => ({ ...current, note: event.target.value }))} className="ios-input w-full px-3 py-3 rounded-lg text-sm resize-y dark:bg-[#25292E] dark:border-gray-700 dark:text-white" /></label>
            </div>
            {error && <div className="mx-5 mb-4 px-4 py-3 rounded-lg bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300 text-sm font-bold">{error}</div>}
            <div className="h-18 px-5 py-4 flex items-center justify-end gap-2 border-t border-gray-200 dark:border-gray-700">
              <button type="button" onClick={() => setShowModal(false)} className="h-10 px-4 rounded-lg border border-gray-200 dark:border-gray-700 text-sm font-bold text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700">取消</button>
              <button type="submit" disabled={saving || !form.buyer_id.trim() || !form.cookie_id} className="h-10 px-4 rounded-lg bg-[#FFE815] text-black text-sm font-bold flex items-center gap-2 disabled:opacity-50 hover:bg-yellow-300">{saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}{editing ? '保存修改' : '添加记录'}</button>
            </div>
          </form>
        </div>,
        document.body,
      )}
    </div>
  );
};

export default BlacklistManager;
