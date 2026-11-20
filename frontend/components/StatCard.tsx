import React from 'react';
import { TrendingUp } from 'lucide-react';

interface StatCardProps {
  title: string;
  value: string | number;
  icon: React.ElementType;
  colorClass: string;
  trend?: string;
}

const StatCard: React.FC<StatCardProps> = ({ title, value, icon: Icon, colorClass, trend }) => (
  <div className="ios-card flex min-h-[104px] min-w-0 flex-col justify-between rounded-lg p-4">
    <div className="flex min-w-0 items-start justify-between gap-3">
      <p className="min-w-0 text-xs font-medium leading-5 text-gray-500 dark:text-gray-400">{title}</p>
      <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${colorClass} bg-opacity-10`}>
        <Icon className={`h-4 w-4 ${colorClass.replace('bg-', 'text-')}`} aria-hidden="true" />
      </div>
    </div>
    <div className="mt-3 flex min-w-0 items-end justify-between gap-2">
      <h3 className="min-w-0 break-words text-2xl font-extrabold leading-none text-gray-900 tabular-nums dark:text-white">
        {value}
      </h3>
      {trend && (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-emerald-50 px-2 py-1 text-xs font-bold text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
          <TrendingUp className="h-3 w-3" aria-hidden="true" />
          {trend}
        </span>
      )}
    </div>
  </div>
);

export default StatCard;
