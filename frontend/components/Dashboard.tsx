import React, { useEffect, useState } from 'react';
import StatCard from './StatCard';
import FinancialProfitView from './FinancialProfitView';
import OrderChaserMonitor from './OrderChaserMonitor';
import { AdminStats, OrderAnalytics, OrderStatus } from '../types';
import { getAdminStats, getOrderAnalytics, getValidOrders, getItems, type AnalyticsOrder } from '../services/api';
import { dashboardDateRanges, type DashboardDateRange, type DashboardTimeRange } from '../lib/dashboard-dates';
import { TrendingUp, Users, ShoppingCart, AlertCircle, DollarSign, Activity, Package, ArrowUpRight, Calendar, X, BarChart3, PackageCheck, ExternalLink, Eye, Edit } from 'lucide-react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, BarChart, Bar, LineChart, Line, PieChart, Pie, Cell, Legend } from 'recharts';

// 状态徽章组件
const StatusBadge: React.FC<{ status: OrderStatus }> = ({ status }) => {
  const styles = {
    processing: 'bg-amber-100 dark:bg-amber-950/40 text-amber-800 dark:text-amber-300',
    pending_ship: 'bg-orange-100 dark:bg-orange-950/40 text-orange-800 dark:text-orange-300',
    shipped: 'bg-blue-100 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300',
    completed: 'bg-emerald-100 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300',
    cancelled: 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-300',
    refunding: 'bg-red-100 dark:bg-red-950/40 text-red-600 dark:text-red-300',
  };

  const labels = {
    processing: '处理中',
    pending_ship: '待发货',
    shipped: '已发货',
    completed: '已完成',
    cancelled: '已取消',
    refunding: '退款中',
  };

  return (
    <span className={`inline-flex px-2 py-1 rounded-md text-xs font-bold ${styles[status] || styles.cancelled}`}>
      {labels[status] || status}
    </span>
  );
};

const EMPTY_ANALYTICS: OrderAnalytics = {
  revenue_stats: { total_amount: 0, total_orders: 0 },
  daily_stats: [],
  item_stats: [],
};

const Dashboard: React.FC = () => {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [analytics, setAnalytics] = useState<OrderAnalytics>(EMPTY_ANALYTICS);
  const [timeRange, setTimeRange] = useState<DashboardTimeRange>('7days');
  const [customStartDate, setCustomStartDate] = useState('');
  const [customEndDate, setCustomEndDate] = useState('');
  const [appliedCustomRange, setAppliedCustomRange] = useState<DashboardDateRange | null>(null);
  const [previousAnalytics, setPreviousAnalytics] = useState<OrderAnalytics | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [validOrders, setValidOrders] = useState<AnalyticsOrder[]>([]);
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [analyticsLoading, setAnalyticsLoading] = useState(true);
  const [itemNames, setItemNames] = useState<Record<string, string>>({});
  const [statsError, setStatsError] = useState(false);
  const [analyticsError, setAnalyticsError] = useState(false);
  const [ordersError, setOrdersError] = useState(false);
  const [analysisView, setAnalysisView] = useState<'trend' | 'profit' | 'chase' | 'products' | 'orders'>('trend');
  const COLORS = ['#FFE815', '#3B82F6', '#10B981', '#F59E0B', '#8B5CF6'];
  const customRangeValid = customStartDate && customEndDate && customStartDate <= customEndDate;

  const getTrendPercent = () => {
    const previousAmount = previousAnalytics?.revenue_stats.total_amount;
    if (analyticsLoading || analyticsError || !previousAmount) return undefined;
    const percent = ((analytics.revenue_stats.total_amount - previousAmount) / previousAmount) * 100;
    return `${percent >= 0 ? '+' : ''}${percent.toFixed(1)}%`;
  };

  useEffect(() => {
    let active = true;
    getItems().then(items => {
      if (active) setItemNames(Object.fromEntries(items.map(item => [item.item_id, item.item_title || item.item_id])));
    }).catch(console.error);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    if (timeRange === 'custom' && !appliedCustomRange) {
      return;
    }
    const { current, previous } = dashboardDateRanges(timeRange, appliedCustomRange);
    setAnalyticsLoading(true);
    setOrdersLoading(true);
    setAnalytics(EMPTY_ANALYTICS);
    setValidOrders([]);
    setPreviousAnalytics(null);
    setStatsError(false);
    setAnalyticsError(false);
    setOrdersError(false);

    getAdminStats().then(data => {
      if (active) setStats(data);
    }).catch(() => {
      if (active) {
        setStatsError(true);
        setStats(null);
      }
    });
    getOrderAnalytics(current).then(data => {
      if (active) setAnalytics(data);
    }).catch(() => {
      if (active) setAnalyticsError(true);
    }).finally(() => {
      if (active) setAnalyticsLoading(false);
    });
    getValidOrders(current).then(orders => {
      if (active) setValidOrders(orders);
    }).catch(() => {
      if (active) setOrdersError(true);
    }).finally(() => {
      if (active) setOrdersLoading(false);
    });
    if (previous) {
      getOrderAnalytics(previous).then(data => {
        if (active) setPreviousAnalytics(data);
      }).catch(console.error);
    }

    return () => { active = false; };
  }, [timeRange, appliedCustomRange]);

  const periodPreview = timeRange === 'custom' && !appliedCustomRange;
  const meaningfulOrders = validOrders.filter(order => {
    const query = searchTerm.trim().toLowerCase();
    return [order.order_id, order.item_id, itemNames[order.item_id], order.buyer_id]
      .some(value => value?.toLowerCase().includes(query));
  });

  const chartData = analytics.daily_stats?.map(d => ({
      name: d.date.slice(5), // MM-DD
      amount: d.amount,
      orders: d.order_count,
      avgAmount: d.order_count > 0 ? (d.amount / d.order_count).toFixed(2) : 0
  })) || [];

  // 计算图表数据（在渲染时直接计算）
  const itemStats = analytics.item_stats || [];
  const totalOrders = analytics.revenue_stats.total_orders || 0;
  const totalAmount = analytics.revenue_stats.total_amount || 0;

  // 1. 商品销量排行：按订单数量排序
  const productSalesData = itemStats.length > 0 ? itemStats
    .map(item => ({
      name: (itemNames[item.item_id] || item.item_id).length > 12
        ? (itemNames[item.item_id] || item.item_id).substring(0, 12) + '...'
        : (itemNames[item.item_id] || item.item_id),
      sales: item.order_count
    }))
    .sort((a, b) => b.sales - a.sales)
    .slice(0, 10) : [];

  // 2. 商品下单占比：每个商品的订单数占总订单数的百分比
  const sourceDataData = itemStats.length > 0 ? itemStats
    .map(item => ({
      name: (itemNames[item.item_id] || item.item_id).length > 10
        ? (itemNames[item.item_id] || item.item_id).substring(0, 10) + '...'
        : (itemNames[item.item_id] || item.item_id),
      value: item.order_count,
      percent: totalOrders > 0 ? (item.order_count / totalOrders) * 100 : 0
    }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6)
    .map((item, index) => ({
      ...item,
      color: COLORS[index % COLORS.length]
    })) : [];

  // 3. 商品金额分析：按金额排序，取前5
  const categoryDataData = itemStats.length > 0 ? itemStats
    .map(item => ({
      name: (itemNames[item.item_id] || item.item_id).length > 12
        ? (itemNames[item.item_id] || item.item_id).substring(0, 12) + '...'
        : (itemNames[item.item_id] || item.item_id),
      value: item.total_amount,
      orderCount: item.order_count,
      percentage: totalAmount > 0
        ? (item.total_amount / totalAmount) * 100
        : 0
    }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 5)
    .map((item, index) => ({
      ...item,
      color: COLORS[index % COLORS.length],
      percentage: item.percentage.toFixed(1)
    })) : [];

  const timeRangeOptions = [
    { key: 'today' as DashboardTimeRange, label: '今天' },
    { key: 'yesterday' as DashboardTimeRange, label: '昨天' },
    { key: '3days' as DashboardTimeRange, label: '三天内' },
    { key: '7days' as DashboardTimeRange, label: '7天内' },
    { key: '30days' as DashboardTimeRange, label: '一个月内' },
    { key: 'custom' as DashboardTimeRange, label: '自定义' },
  ];

  return (
    <div className="space-y-4 animate-fade-in">
      {(statsError || analyticsError) && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-800">
          {statsError ? '后台统计暂时无法读取' : '订单统计暂时无法读取，请重新选择日期'}
        </div>
      )}
      <div className="flex flex-col md:flex-row justify-between md:items-center gap-3 pr-14">
        <div>
          <h2 className="text-2xl font-extrabold text-gray-900 dark:text-white">运营概览</h2>
          <p className="text-gray-500 mt-1 text-sm">店铺实时经营数据</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-xs font-bold text-gray-700 dark:text-gray-200 bg-white dark:bg-[#1B1E22] px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${stats ? 'bg-green-500' : 'bg-amber-500'}`}></span>
            {stats ? '后台已响应' : statsError ? '后台数据不可用' : '正在读取后台数据'}
          </div>
        </div>
      </div>

      {/* Time Range Selector */}
      <div className="flex flex-wrap gap-1 p-1 bg-gray-100 dark:bg-[#25292E] rounded-lg w-fit max-w-full">
        {timeRangeOptions.map((option) => (
          <button
            key={option.key}
            onClick={() => setTimeRange(option.key)}
            className={`h-8 px-3 rounded-md text-sm font-bold transition-colors ${
              timeRange === option.key
                ? 'bg-white dark:bg-[#3A3F47] text-gray-900 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-black dark:hover:text-white'
            }`}
          >
            {option.label}
          </button>
        ))}
        {timeRange === 'custom' && (
          <>
            <input
              type="date"
              aria-label="开始日期"
              max={customEndDate || undefined}
              value={customStartDate}
              onChange={(e) => setCustomStartDate(e.target.value)}
              className="px-3 h-8 rounded-md text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1B1E22] focus:outline-none"
            />
            <span className="self-center text-gray-400">-</span>
            <input
              type="date"
              aria-label="结束日期"
              min={customStartDate || undefined}
              value={customEndDate}
              onChange={(e) => setCustomEndDate(e.target.value)}
              className="px-3 h-8 rounded-md text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1B1E22] focus:outline-none"
            />
            <button
              disabled={!customRangeValid}
              onClick={() => setAppliedCustomRange({ start_date: customStartDate, end_date: customEndDate })}
              className="px-3 h-8 rounded-md text-sm font-bold bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 transition-colors disabled:opacity-50"
            >
              应用
            </button>
          </>
        )}
      </div>

      {periodPreview && <p className="text-sm text-gray-500">选择开始和结束日期后应用</p>}

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          title="累计营收 (CNY)"
          value={periodPreview || analyticsLoading || analyticsError ? '—' : `¥${analytics.revenue_stats.total_amount.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`}
          icon={DollarSign}
          colorClass="bg-yellow-400"
          trend={getTrendPercent() || undefined}
        />
        <StatCard
          title="在线账号 / 总数"
          value={stats ? `${stats.active_cookies} / ${stats.total_cookies}` : '—'}
          icon={Users}
          colorClass="bg-blue-500"
        />
        <StatCard
          title="订单数"
          value={periodPreview || analyticsLoading || analyticsError ? '—' : analytics.revenue_stats.total_orders.toLocaleString()}
          icon={ShoppingCart}
          colorClass="bg-orange-500"
        />
        <StatCard
          title="库存卡密余量"
          value={stats?.available_cards ?? '—'}
          icon={Package}
          colorClass="bg-purple-500"
        />
      </div>

      <div className="flex items-center gap-1 p-1 rounded-lg bg-gray-100 dark:bg-[#25292E] w-fit flex-wrap" role="tablist" aria-label="分析视图">
        {([
          ['trend', '营收趋势'],
          ['profit', '利润与ROI (56)'],
          ['chase', '催付与风控 (57)'],
          ['products', '商品分析'],
          ['orders', '订单明细'],
        ] as const).map(([value, label]) => (
          <button key={value} type="button" role="tab" aria-selected={analysisView === value} onClick={() => setAnalysisView(value)} className={`h-9 px-4 rounded-md text-sm font-bold transition-colors ${analysisView === value ? 'bg-white dark:bg-[#3A3F47] text-gray-900 dark:text-white shadow-sm' : 'text-gray-500 dark:text-gray-400'}`}>{label}</button>
        ))}
      </div>

      {/* Main Chart Section */}
      {analysisView === 'trend' && <div className="ios-card p-5 rounded-lg">
        <div className="mb-4">
          <h3 className="text-base font-bold text-gray-900 dark:text-white">营收趋势分析</h3>
          <p className="text-sm text-gray-400 mt-1">最近7天的销售额走势</p>
        </div>
        <div className="h-[290px] w-full">
          {chartData.length === 0 || analytics.revenue_stats.total_amount === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-gray-400">
              <ShoppingCart className="w-16 h-16 mb-4 opacity-20" />
              <p className="text-lg font-medium">暂无营收数据</p>
              <p className="text-sm mt-2">所选时间范围内暂无订单记录</p>
            </div>
          ) : chartData.length <= 2 ? (
            // 数据点少于等于2个时使用美化柱状图
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 30, right: 20, left: -20, bottom: 30 }} barCategoryGap={30}>
                <XAxis
                  dataKey="name"
                  axisLine={false}
                  tickLine={false}
                  tick={{fill: '#374151', fontSize: 14, fontWeight: 600}}
                  dy={10}
                />
                <YAxis
                  axisLine={false}
                  tickLine={false}
                  tick={{fill: '#9CA3AF', fontSize: 13, fontWeight: 500}}
                  tickFormatter={(value) => `¥${value}`}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#1F2937',
                    borderRadius: '16px',
                    border: 'none',
                    boxShadow: '0 20px 40px rgba(0, 0, 0, 0.3)',
                    padding: '12px 16px'
                  }}
                  itemStyle={{ color: '#FFE815', fontWeight: 600 }}
                  formatter={(value) => {
                    const num = Number(value);
                    return `营收: ¥${num.toFixed(2)}`;
                  }}
                />
                <Bar
                  dataKey="amount"
                  fill="#FFE815"
                  radius={[12, 12, 0, 0]}
                  stroke="#000000"
                  strokeWidth={0}
                >
                  {chartData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={['#FFE815', '#FCD34D', '#FBBF24'][index % 3]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            // 数据点多于2个时使用折线图
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorAmount" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#FFE815" stopOpacity={0.5}/>
                    <stop offset="95%" stopColor="#FFE815" stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="name"
                  axisLine={false}
                  tickLine={false}
                  tick={{fill: '#9CA3AF', fontSize: 13, fontWeight: 500}}
                  dy={15}
                />
                <YAxis
                  axisLine={false}
                  tickLine={false}
                  tick={{fill: '#9CA3AF', fontSize: 13, fontWeight: 500}}
                />
                <CartesianGrid vertical={false} stroke="#F3F4F6" strokeDasharray="3 3" />
                <Tooltip
                  contentStyle={{ background: '#1A1A1A', borderRadius: '16px', border: 'none', boxShadow: '0 10px 40px rgba(0,0,0,0.2)' }}
                  itemStyle={{ color: '#FFE815', fontWeight: 600 }}
                  labelStyle={{ color: '#888' }}
                  cursor={{ stroke: '#FFE815', strokeWidth: 2, strokeDasharray: '4 4' }}
                />
                <Area type="monotone" dataKey="amount" stroke="#FACC15" strokeWidth={4} fillOpacity={1} fill="url(#colorAmount)" activeDot={{ r: 8, fill: '#1A1A1A', stroke: "#FFE815", strokeWidth: 2 }} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>}

      {!periodPreview && analysisView === 'profit' && (
        <FinancialProfitView
          validOrders={validOrders}
          analytics={analytics}
          itemNames={itemNames}
          loading={ordersLoading}
        />
      )}

      {!periodPreview && analysisView === 'chase' && (
        <OrderChaserMonitor
          validOrders={validOrders}
          itemNames={itemNames}
        />
      )}

      {!periodPreview && analysisView === 'products' && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="ios-card p-5 rounded-lg">
            <h3 className="font-bold text-base text-gray-900 dark:text-white mb-3">商品销量排行</h3>
            <div className="h-[260px]">
              {productSalesData.length === 0 ? <div className="flex items-center justify-center h-full text-gray-400">暂无数据</div> : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={productSalesData} layout="vertical">
                    <CartesianGrid strokeDasharray="3 3" horizontal vertical={false} stroke="#4B5563" opacity={0.25} />
                    <XAxis type="number" axisLine={false} tickLine={false} tick={{ fill: '#9CA3AF', fontSize: 12 }} />
                    <YAxis type="category" dataKey="name" axisLine={false} tickLine={false} tick={{ fill: '#9CA3AF', fontSize: 12 }} width={90} />
                    <Tooltip />
                    <Bar dataKey="sales" fill="#94A3B8" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>

          <div className="ios-card p-5 rounded-lg">
            <h3 className="font-bold text-base text-gray-900 dark:text-white mb-3">商品下单占比</h3>
            <div className="h-[260px]">
              {sourceDataData.length === 0 ? <div className="flex items-center justify-center h-full text-gray-400">暂无数据</div> : (
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={sourceDataData} cx="50%" cy="46%" innerRadius={50} outerRadius={82} paddingAngle={2} dataKey="value" labelLine={false}>
                      {sourceDataData.map((entry, index) => <Cell key={`cell-${index}`} fill={entry.color} />)}
                    </Pie>
                    <Tooltip />
                    <Legend verticalAlign="bottom" height={30} iconType="circle" formatter={(value) => <span style={{ color: '#9CA3AF', fontWeight: 500 }}>{value}</span>} />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>

          <div className="ios-card p-5 rounded-lg">
            <h3 className="font-bold text-base text-gray-900 dark:text-white mb-3">商品金额 TOP5</h3>
            <div className="h-[260px] overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700">
              {categoryDataData.length === 0 ? <div className="flex items-center justify-center h-full text-gray-400">暂无数据</div> : categoryDataData.map((cat, index) => (
                <div key={cat.name} className="flex items-center justify-between py-3 gap-3">
                  <div className="flex items-center gap-3 min-w-0"><span className="w-6 text-xs font-bold text-gray-400">{index + 1}</span><span className="text-sm font-medium text-gray-700 dark:text-gray-200 truncate">{cat.name}</span></div>
                  <div className="text-right flex-shrink-0"><div className="text-sm font-bold text-gray-900 dark:text-white">¥{cat.value.toLocaleString()}</div><div className="text-xs text-gray-400">{cat.percentage}%</div></div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {!periodPreview && analysisView === 'orders' && (
        <div className="ios-card p-0 rounded-lg bg-white overflow-hidden">
          <div className="p-3 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center bg-gray-50 dark:bg-[#181A1E]">
            <h3 className="font-bold text-base text-gray-900 dark:text-white">参与统计的订单</h3>
            <input placeholder="搜索订单号/商品/买家..." value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} className="ios-input px-3 h-9 rounded-lg text-sm w-64 dark:bg-[#25292E] dark:border-gray-700" type="text" />
          </div>
          <div className="overflow-x-auto max-h-[380px] min-h-[260px]">
            {ordersLoading ? (
              <div className="flex items-center justify-center h-64 text-gray-400"><Activity className="w-5 h-5 animate-spin mr-2" />加载中...</div>
            ) : ordersError ? (
              <div role="alert" className="flex items-center justify-center h-64 text-gray-500">订单明细暂时无法读取，请重新选择日期</div>
            ) : meaningfulOrders.length === 0 ? (
              <div className="flex items-center justify-center h-64 text-gray-400">暂无订单数据</div>
            ) : (
              <table className="w-full text-left border-collapse min-w-[760px]">
                <thead><tr className="bg-gray-50 dark:bg-[#202328] text-gray-500 dark:text-gray-400 text-xs font-bold border-b border-gray-200 dark:border-gray-700"><th className="px-4 py-3">订单信息</th><th className="px-4 py-3">买家</th><th className="px-4 py-3">金额</th><th className="px-4 py-3">状态</th><th className="px-4 py-3 text-right">操作</th></tr></thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                  {meaningfulOrders.map((order) => (
                    <tr key={order.order_id} className="hover:bg-gray-50 dark:hover:bg-[#22262B] transition-colors">
                      <td className="px-4 py-3"><div className="font-bold text-gray-900 dark:text-white text-sm truncate max-w-80">{itemNames[order.item_id] || order.item_id || '未知商品'}</div><div className="text-xs text-gray-500 mt-1 font-mono">{order.order_id} · 数量 {order.quantity || 1}</div></td>
                      <td className="px-4 py-3"><div className="text-sm font-bold text-gray-800 dark:text-gray-100">{order.buyer_id}</div>{order.created_at && <div className="text-xs text-gray-400 mt-1">{order.created_at}</div>}</td>
                      <td className="px-4 py-3 text-sm font-extrabold text-gray-900 dark:text-white">{order.amount ? `¥${String(order.amount).replace(/^¥/, '')}` : '未记录'}</td>
                      <td className="px-4 py-3"><StatusBadge status={order.status} /></td>
                      <td className="px-4 py-3 text-right"><a href={`https://www.goofish.com/order-detail?orderId=${order.order_id}&role=seller`} target="_blank" rel="noopener noreferrer" className="inline-flex text-gray-400 hover:text-amber-600 p-2 rounded-md hover:bg-amber-50 transition-colors" title="查看闲鱼详情"><ExternalLink className="w-4 h-4" /></a></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default Dashboard;
