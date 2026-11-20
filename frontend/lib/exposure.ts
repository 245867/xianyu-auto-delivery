import { useEffect, useState } from 'react';
import { get } from './request';

interface PolishStatus {
  settings: { enabled: boolean; hour: number; minute: number };
  running?: boolean;
  last_run: string;
  ran_today?: boolean;
  last_summary: {
    accounts?: Record<
      string,
      { items?: number; success: number; fail: number; note?: string; warning?: string; risk?: boolean }
    >;
  };
  recent_logs: { cookie_id: string; item_id: string; time: string; success: boolean }[];
}

export interface DiagnosisSignal {
  code: string;
  level: string;
  title: string;
  evidence: string;
  action: string;
  weight: number;
}

export interface DiagnosisItem {
  item_id: string;
  title?: string;
  price?: number | null;
  browse: number | null;
  want: number | null;
  sold: number | null;
  collect?: number | null;
  days?: number | null;
  stale?: boolean;
  classify?: string;
  classify_label?: string;
  want_rate?: number | null;
  top_signal?: string;
  signals?: DiagnosisSignal[];
  prev?: {
    day?: string;
    browse?: number | null;
    want?: number | null;
    collect?: number | null;
    sold?: number | null;
    price?: number | null;
  } | null;
}

export interface Diagnosis {
  ok?: boolean;
  error?: string;
  _at: string;
  items?: DiagnosisItem[];
  sales?: {
    ok?: boolean;
    orders?: number;
    pending_ship?: number;
    refund_orders?: number;
    days?: number;
    reason?: string;
  };
  actions?: {
    scope: string;
    item_id: string;
    title: string;
    action: string;
    level: string;
  }[];
  data_notes?: string[];
}

export function usePolishStatus(): {
  status: PolishStatus | undefined;
  loaded: boolean;
  error: string;
} {
  const [status, setStatus] = useState<PolishStatus>();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    const load = () => get<PolishStatus>('/api/polish/status')
      .then(value => {
        if (!active) return;
        setStatus(value);
        setError('');
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : '擦亮状态读取失败');
      })
      .finally(() => {
        if (active) setLoaded(true);
      });
    void load();
    const timer = window.setInterval(load, 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  return { status, loaded, error };
}

export function useDiagnosisReports(accountIds: string[], refreshSequence = 0): {
  reports: Record<string, Diagnosis>;
  loaded: boolean;
  errors: Record<string, string>;
} {
  const [reports, setReports] = useState<Record<string, Diagnosis>>({});
  const [loaded, setLoaded] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const key = JSON.stringify(accountIds);

  useEffect(() => {
    let active = true;
    const ids: string[] = JSON.parse(key);
    setLoaded(false);
    if (!ids.length) {
      setReports({});
      setErrors({});
      setLoaded(true);
      return () => { active = false; };
    }

    void Promise.allSettled(ids.map(async id => {
      const report = await get<Diagnosis>('/api/shop-diagnosis/last', { cookie_id: id });
      return [id, report] as const;
    })).then(results => {
      if (!active) return;

      const nextReports: Record<string, Diagnosis> = {};
      const nextErrors: Record<string, string> = {};
      results.forEach((result, index) => {
        const id = ids[index];
        if (result.status === 'rejected') {
          nextErrors[id] = result.reason instanceof Error
            ? result.reason.message
            : '诊断请求失败';
          return;
        }
        const [, report] = result.value;
        if (report.ok === false) {
          nextErrors[id] = report.error || '诊断报告不可用';
          return;
        }
        nextReports[id] = report;
      });
      setReports(previous => {
        const retained = Object.fromEntries(
          ids.flatMap(id => previous[id] ? [[id, previous[id]]] : []),
        );
        return { ...retained, ...nextReports };
      });
      setErrors(nextErrors);
      setLoaded(true);
    });

    return () => { active = false; };
  }, [key, refreshSequence]);

  return { reports, loaded, errors };
}
