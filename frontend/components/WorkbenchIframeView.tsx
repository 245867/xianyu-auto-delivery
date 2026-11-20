import React, { useState } from 'react';
import { RefreshCw, ExternalLink, ShieldCheck, Loader2 } from 'lucide-react';

interface WorkbenchIframeViewProps {
  title: string;
  subtitle: string;
  src: string;
  badge?: string;
}

export const WorkbenchIframeView: React.FC<WorkbenchIframeViewProps> = ({
  title,
  subtitle,
  src,
  badge,
}) => {
  const [loading, setLoading] = useState(true);
  const [iframeKey, setIframeKey] = useState(0);

  const handleRefresh = () => {
    setLoading(true);
    setIframeKey(k => k + 1);
  };

  return (
    <div className="space-y-4">
      {/* 顶部工具栏 */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 p-4 bg-white dark:bg-[#1f2227] rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-xl font-black text-gray-900 dark:text-white tracking-tight">{title}</h2>
            {badge && (
              <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-[#FFE815]/20 text-yellow-800 dark:text-yellow-300 border border-yellow-300 dark:border-yellow-600/40">
                {badge}
              </span>
            )}
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{subtitle}</p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleRefresh}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg bg-gray-100 hover:bg-gray-200 dark:bg-[#2c3036] dark:hover:bg-[#383d45] text-gray-700 dark:text-gray-200 transition-colors"
            title="刷新当前视图"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
          <a
            href={src}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg bg-[#FFE815] hover:bg-yellow-400 text-gray-950 shadow-sm transition-colors"
            title="在独立浏览器标签页中打开"
          >
            <ExternalLink className="w-3.5 h-3.5" />
            <span>独立打开</span>
          </a>
        </div>
      </div>

      {/* 内嵌容器 */}
      <div className="relative w-full h-[calc(100vh-210px)] min-h-[600px] bg-white dark:bg-[#1a1c20] rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
        {loading && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-white/80 dark:bg-[#1a1c20]/80 backdrop-blur-sm gap-2">
            <Loader2 className="w-8 h-8 text-[#FFE815] animate-spin" />
            <p className="text-xs font-bold text-gray-500 dark:text-gray-400">正在载入系统视图...</p>
          </div>
        )}
        <iframe
          key={iframeKey}
          src={src}
          title={title}
          onLoad={() => setLoading(false)}
          className="w-full h-full border-0"
        />
      </div>
    </div>
  );
};

export default WorkbenchIframeView;
