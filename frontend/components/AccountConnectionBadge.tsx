import type { AccountDetail } from '../types';

const labels = {
  connected: '在线',
  disconnected: '未连接',
  connecting: '连接中',
  reconnecting: '重连中',
  failed: '连接失败',
  closed: '连接已关闭',
} as const;

export default function AccountConnectionBadge({ account }: { account: AccountDetail }) {
  const state = account.connection_state;
  const tone = !account.enabled
    ? 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200'
    : state === 'connected'
      ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200'
      : 'bg-amber-100 text-amber-900 dark:bg-amber-950/50 dark:text-amber-200';

  return (
    <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-bold ${tone}`}>
      {account.enabled ? (state ? labels[state] : '连接状态未知') : '已暂停'}
    </span>
  );
}
