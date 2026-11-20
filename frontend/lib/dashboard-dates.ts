export type DashboardTimeRange = 'today' | 'yesterday' | '3days' | '7days' | '30days' | 'custom';

export type DashboardDateRange = { start_date: string; end_date: string };

const RANGE_DAYS = { today: 1, yesterday: 1, '3days': 3, '7days': 7, '30days': 30 } as const;

function localDateOffset(now: Date, offset: number): string {
  const date = new Date(now);
  date.setDate(date.getDate() + offset);
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

export function dashboardDateRanges(
  range: DashboardTimeRange,
  custom: DashboardDateRange | null,
  now = new Date(),
): { current: DashboardDateRange; previous: DashboardDateRange | null } {
  if (range === 'custom' && custom) {
    return { current: custom, previous: null };
  }

  const preset = range === 'custom' ? '7days' : range;
  const days = RANGE_DAYS[preset];
  const endOffset = preset === 'yesterday' ? -1 : 0;
  const startOffset = endOffset - days + 1;
  return {
    current: {
      start_date: localDateOffset(now, startOffset),
      end_date: localDateOffset(now, endOffset),
    },
    previous: {
      start_date: localDateOffset(now, startOffset - days),
      end_date: localDateOffset(now, endOffset - days),
    },
  };
}
