import React, { useEffect, useState } from 'react';
import { AdminStats, OrderAnalytics, Order, OrderStatus, Item } from '../types';
import { getAdminStats, getOrderAnalytics, getValidOrders, getItems } from '../services/api';
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

const StatCard: React.FC<{ title: string; value: string | number; icon: React.ElementType; colorClass: string; trend?: string }> = ({ title, value, icon: Icon, colorClass, trend }) => (
  <div className="ios-card p-4 rounded-lg flex items-center justify-between min-h-[92px]">
    <div className="flex items-center gap-3 min-w-0">
      <div className={`w-10 h-10 rounded-lg ${colorClass} bg-opacity-10 flex items-center justify-center flex-shrink-0`}>
        <Icon className={`w-5 h-5 ${colorClass.replace('bg-', 'text-')}`} />
      </div>
      <div className="min-w-0">
        <p className="text-gray-500 text-xs font-medium mb-1">{title}</p>
        <h3 className="text-2xl font-extrabold text-gray-900 dark:text-white font-feature-settings-tnum truncate">{value}</h3>
      </div>
    </div>
    {trend && <span className="text-xs font-bold text-emerald-700 dark:text-emerald-300 bg-emerald-50 dark:bg-emerald-950/40 px-2 py-1 rounded-md flex items-center gap-1">
      <TrendingUp className="w-3 h-3" /> {trend}
    </span>}
  </div>
);

type TimeRange = 'today' | 'yesterday' | '3days' | '7days' | '30days' | 'custom';

const Dashboard: React.FC = () => {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [analytics, setAnalytics] = useState<OrderAnalytics | null>(null);
  const [timeRange, setTimeRange] = useState<TimeRange>('7days');
  const [customStartDate, setCustomStartDate] = useState('');
  const [customEndDate, setCustomEndDate] = useState('');
  const [previousAnalytics, setPreviousAnalytics] = useState<OrderAnalytics | null>(null); // 用于计算趋势

  // 搜索词
  const [searchTerm, setSearchTerm] = useState('');
  // 参与统计的订单列表
  const [validOrders, setValidOrders] = useState<Order[]>([]);
  const [ordersLoading, setOrdersLoading] = useState(false);
  // 商品列表
  const [items, setItems] = useState<Item[]>([]);
  const [itemNames, setItemNames] = useState<Record<string, string>>({});
  const [dashboardError, setDashboardError] = useState('');
  const [analysisView, setAnalysisView] = useState<'trend' | 'products' | 'orders'>('trend');

  const emptyStats: AdminStats = {
    total_users: 0, total_cookies: 0, active_cookies: 0,
    total_cards: 0, total_keywords: 0, total_orders: 0
  };

  const emptyAnalytics: OrderAnalytics = {
    revenue_stats: { total_amount: 0, total_orders: 0 },
    daily_stats: [],
    item_stats: []
  };

  // 颜色配置
  const COLORS = ['#FFE815', '#3B82F6', '#10B981', '#F59E0B', '#8B5CF6'];

  const loadAnalytics = (range: TimeRange) => {
    // 使用本地时间而不是UTC时间
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const todayStr = `${year}-${month}-${day}`;

    let params: { start_date: string; end_date: string };

    switch (range) {
      case 'today':
        // 今天：从今天00:00:00到今天23:59:59
        params = {
          start_date: todayStr,
          end_date: todayStr
        };
        break;
      case 'yesterday':
        // 昨天：从昨天00:00:00到昨天23:59:59
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        const yYear = yesterday.getFullYear();
        const yMonth = String(yesterday.getMonth() + 1).padStart(2, '0');
        const yDay = String(yesterday.getDate()).padStart(2, '0');
        const yesterdayStr = `${yYear}-${yMonth}-${yDay}`;
        params = {
          start_date: yesterdayStr,
          end_date: yesterdayStr
        };
        break;
      case '3days':
        // 3天：从3天前到今天
        const threeDaysAgo = new Date(now);
        threeDaysAgo.setDate(threeDaysAgo.getDate() - 3);
        const tdYear = threeDaysAgo.getFullYear();
        const tdMonth = String(threeDaysAgo.getMonth() + 1).padStart(2, '0');
        const tdDay = String(threeDaysAgo.getDate()).padStart(2, '0');
        params = {
          start_date: `${tdYear}-${tdMonth}-${tdDay}`,
          end_date: todayStr
        };
        break;
      case '7days':
        // 7天：从7天前到今天
        const sevenDaysAgo = new Date(now);
        sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
        const sdYear = sevenDaysAgo.getFullYear();
        const sdMonth = String(sevenDaysAgo.getMonth() + 1).padStart(2, '0');
        const sdDay = String(sevenDaysAgo.getDate()).padStart(2, '0');
        params = {
          start_date: `${sdYear}-${sdMonth}-${sdDay}`,
          end_date: todayStr
        };
        break;
      case '30days':
        // 30天：从30天前到今天
        const thirtyDaysAgo = new Date(now);
        thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
        const tdYear2 = thirtyDaysAgo.getFullYear();
        const tdMonth2 = String(thirtyDaysAgo.getMonth() + 1).padStart(2, '0');
        const tdDay2 = String(thirtyDaysAgo.getDate()).padStart(2, '0');
        params = {
          start_date: `${tdYear2}-${tdMonth2}-${tdDay2}`,
          end_date: todayStr
        };
        break;
      case 'custom':
        // 自定义范围
        if (customStartDate && customEndDate) {
          params = {
            start_date: customStartDate,
            end_date: customEndDate
          };
        } else {
          // 默认7天
          const defaultDaysAgo = new Date(now);
          defaultDaysAgo.setDate(defaultDaysAgo.getDate() - 7);
          const ddYear = defaultDaysAgo.getFullYear();
          const ddMonth = String(defaultDaysAgo.getMonth() + 1).padStart(2, '0');
          const ddDay = String(defaultDaysAgo.getDate()).padStart(2, '0');
          params = {
            start_date: `${ddYear}-${ddMonth}-${ddDay}`,
            end_date: todayStr
          };
        }
        break;
      default:
        // 默认7天
        const defaultStart = new Date(now);
        defaultStart.setDate(defaultStart.getDate() - 7);
        const dsYear = defaultStart.getFullYear();
        const dsMonth = String(defaultStart.getMonth() + 1).padStart(2, '0');
        const dsDay = String(defaultStart.getDate()).padStart(2, '0');
        params = {
          start_date: `${dsYear}-${dsMonth}-${dsDay}`,
          end_date: todayStr
        };
    }

    // 同时获取上一个周期的数据用于趋势对比
    const previousParams = getPreviousPeriodParams(range, now);
    if (previousParams) {
      getOrderAnalytics(previousParams).then(setPreviousAnalytics).catch(console.error);
    }

    getOrderAnalytics(params)
      .then(setAnalytics)
      .catch(error => {
        console.error(error);
        setAnalytics(emptyAnalytics);
        setDashboardError('部分统计数据暂时无法读取，已显示空数据');
      });
  };

  // 获取上一个时间段的参数
  const getPreviousPeriodParams = (range: TimeRange, now: Date) => {
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');

    switch (range) {
      case 'today':
        // 今天对比昨天
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        const yYear = yesterday.getFullYear();
        const yMonth = String(yesterday.getMonth() + 1).padStart(2, '0');
        const yDay = String(yesterday.getDate()).padStart(2, '0');
        return {
          start_date: `${yYear}-${yMonth}-${yDay}`,
          end_date: `${yYear}-${yMonth}-${yDay}`
        };
      case 'yesterday':
        // 昨天对比前天
        const dayBefore = new Date(now);
        dayBefore.setDate(dayBefore.getDate() - 2);
        const dbYear = dayBefore.getFullYear();
        const dbMonth = String(dayBefore.getMonth() + 1).padStart(2, '0');
        const dbDay = String(dayBefore.getDate()).padStart(2, '0');
        return {
          start_date: `${dbYear}-${dbMonth}-${dbDay}`,
          end_date: `${dbYear}-${dbMonth}-${dbDay}`
        };
      case '7days':
        // 7天对比上一个7天
        const prev7DaysEnd = new Date(now);
        prev7DaysEnd.setDate(prev7DaysEnd.getDate() - 7);
        const prev7DaysStart = new Date(prev7DaysEnd);
        prev7DaysStart.setDate(prev7DaysStart.getDate() - 7);
        return {
          start_date: `${prev7DaysStart.getFullYear()}-${String(prev7DaysStart.getMonth() + 1).padStart(2, '0')}-${String(prev7DaysStart.getDate()).padStart(2, '0')}`,
          end_date: `${prev7DaysEnd.getFullYear()}-${String(prev7DaysEnd.getMonth() + 1).padStart(2, '0')}-${String(prev7DaysEnd.getDate()).padStart(2, '0')}`
        };
      case '30days':
        // 30天对比上一个30天
        const prev30DaysEnd = new Date(now);
        prev30DaysEnd.setDate(prev30DaysEnd.getDate() - 30);
        const prev30DaysStart = new Date(prev30DaysEnd);
        prev30DaysStart.setDate(prev30DaysStart.getDate() - 30);
        return {
          start_date: `${prev30DaysStart.getFullYear()}-${String(prev30DaysStart.getMonth() + 1).padStart(2, '0')}-${String(prev30DaysStart.getDate()).padStart(2, '0')}`,
          end_date: `${prev30DaysEnd.getFullYear()}-${String(prev30DaysEnd.getMonth() + 1).padStart(2, '0')}-${String(prev30DaysEnd.getDate()).padStart(2, '0')}`
        };
      default:
        return null;
    }
  };

  // 计算趋势百分比
  const getTrendPercent = () => {
    if (!analytics || !previousAnalytics) return null;

    const currentAmount = analytics.revenue_stats.total_amount;
    const previousAmount = previousAnalytics.revenue_stats.total_amount;

    if (previousAmount === 0) {
      return currentAmount > 0 ? '+100%' : '0%';
    }

    const percent = ((currentAmount - previousAmount) / previousAmount) * 100;
    const sign = percent >= 0 ? '+' : '';
    return `${sign}${percent.toFixed(1)}%`;
  };

  useEffect(() => {
    setDashboardError('');
    getAdminStats()
      .then(setStats)
      .catch(error => {
        console.error(error);
        setStats(emptyStats);
        setDashboardError('部分统计数据暂时无法读取，已显示空数据');
      });
    loadAnalytics(timeRange);
    // 获取商品列表
    getItems().then(items => {
      setItems(items);
      // 建立 item_id 到 item_title 的映射
      const nameMap: Record<string, string> = {};
      items.forEach(item => {
        nameMap[item.item_id] = item.item_title || item.item_id;
      });
      setItemNames(nameMap);
    }).catch(console.error);
  }, [timeRange]);

  // 加载订单列表
  useEffect(() => {
    const { startDate, endDate } = getDatesForRange(timeRange);

    // 获取参与统计的订单列表
    setOrdersLoading(true);
    getValidOrders({ start_date: startDate, end_date: endDate })
      .then(orders => {
        setValidOrders(orders);
      })
      .catch(console.error)
      .finally(() => setOrdersLoading(false));
  }, [timeRange]);

  // 辅助函数：获取时间范围的日期
  const getDatesForRange = (range: TimeRange) => {
    const now = new Date();
    const endDate = now.toISOString().split('T')[0];
    let startDate = endDate;

    if (range === '7days') {
      const sevenDaysAgo = new Date(now);
      sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
      startDate = sevenDaysAgo.toISOString().split('T')[0];
    } else if (range === '30days') {
      const thirtyDaysAgo = new Date(now);
      thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
      startDate = thirtyDaysAgo.toISOString().split('T')[0];
    }
    // 其他范围类似处理...

    return { startDate, endDate };
  };

  if (!stats || !analytics) return <div className="p-8 flex justify-center text-gray-400"><Activity className="w-8 h-8 animate-spin text-[#FFE815]" /></div>;

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
    { key: 'today' as TimeRange, label: '今天' },
    { key: 'yesterday' as TimeRange, label: '昨天' },
    { key: '3days' as TimeRange, label: '三天内' },
    { key: '7days' as TimeRange, label: '7天内' },
    { key: '30days' as TimeRange, label: '一个月内' },
    { key: 'custom' as TimeRange, label: '自定义' },
  ];

  return (
    <div className="space-y-4 animate-fade-in">
      {dashboardError && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-800">
          {dashboardError}
        </div>
      )}
      <div className="flex flex-col md:flex-row justify-between md:items-center gap-3 pr-14">
        <div>
          <h2 className="text-2xl font-extrabold text-gray-900 dark:text-white">运营概览</h2>
          <p className="text-gray-500 mt-1 text-sm">店铺实时经营数据</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-xs font-bold text-gray-700 dark:text-gray-200 bg-white dark:bg-[#1B1E22] px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 flex items-center gap-2">
            <span className="w-2 h-2 bg-green-500 rounded-full"></span>
            系统正常运行
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
              value={customStartDate}
              onChange={(e) => setCustomStartDate(e.target.value)}
              className="px-3 h-8 rounded-md text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1B1E22] focus:outline-none"
            />
            <span className="self-center text-gray-400">-</span>
            <input
              type="date"
              value={customEndDate}
              onChange={(e) => setCustomEndDate(e.target.value)}
              className="px-3 h-8 rounded-md text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1B1E22] focus:outline-none"
            />
            <button
              onClick={() => loadAnalytics('custom')}
              className="px-3 h-8 rounded-md text-sm font-bold bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 transition-colors"
            >
              应用
            </button>
          </>
        )}
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          title="累计营收 (CNY)"
          value={`¥${analytics.revenue_stats.total_amount.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`}
          icon={DollarSign}
          colorClass="bg-yellow-400"
          trend={getTrendPercent() || undefined}
        />
        <StatCard
          title="活跃账号 / 总数"
          value={`${stats.active_cookies} / ${stats.total_cookies}`}
          icon={Users}
          colorClass="bg-blue-500"
        />
        <StatCard
          title="订单数"
          value={analytics.revenue_stats.total_orders.toLocaleString()}
          icon={ShoppingCart}
          colorClass="bg-orange-500"
        />
        <StatCard
          title="库存卡密余量"
          value={stats.total_cards}
          icon={Package}
          colorClass="bg-purple-500"
        />
      </div>

      <div className="flex items-center gap-1 p-1 rounded-lg bg-gray-100 dark:bg-[#25292E] w-fit" role="tablist" aria-label="分析视图">
        {([
          ['trend', '营收趋势'],
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

      {analysisView === 'products' && (
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

      {analysisView === 'orders' && (
        <div className="ios-card p-0 rounded-lg bg-white overflow-hidden">
          <div className="p-3 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center bg-gray-50 dark:bg-[#181A1E]">
            <h3 className="font-bold text-base text-gray-900 dark:text-white">参与统计的订单</h3>
            <input placeholder="搜索订单号/商品/买家..." value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} className="ios-input px-3 h-9 rounded-lg text-sm w-64 dark:bg-[#25292E] dark:border-gray-700" type="text" />
          </div>
          <div className="overflow-x-auto max-h-[380px] min-h-[260px]">
            {ordersLoading ? (
              <div className="flex items-center justify-center h-64 text-gray-400"><Activity className="w-5 h-5 animate-spin mr-2" />加载中...</div>
            ) : validOrders.filter((order) => order.order_id?.toLowerCase().includes(searchTerm.toLowerCase()) || order.item_id?.toLowerCase().includes(searchTerm.toLowerCase()) || order.buyer_id?.toLowerCase().includes(searchTerm.toLowerCase())).length === 0 ? (
              <div className="flex items-center justify-center h-64 text-gray-400">暂无订单数据</div>
            ) : (
              <table className="w-full text-left border-collapse min-w-[760px]">
                <thead><tr className="bg-gray-50 dark:bg-[#202328] text-gray-500 dark:text-gray-400 text-xs font-bold border-b border-gray-200 dark:border-gray-700"><th className="px-4 py-3">订单信息</th><th className="px-4 py-3">买家</th><th className="px-4 py-3">金额</th><th className="px-4 py-3">状态</th><th className="px-4 py-3 text-right">操作</th></tr></thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                  {validOrders.filter((order) => order.order_id?.toLowerCase().includes(searchTerm.toLowerCase()) || order.item_id?.toLowerCase().includes(searchTerm.toLowerCase()) || order.buyer_id?.toLowerCase().includes(searchTerm.toLowerCase())).map((order) => (
                    <tr key={order.order_id} className="hover:bg-gray-50 dark:hover:bg-[#22262B] transition-colors">
                      <td className="px-4 py-3"><div className="font-bold text-gray-900 dark:text-white text-sm truncate max-w-80">{order.item_title || order.item_id || '未知商品'}</div><div className="text-xs text-gray-500 mt-1 font-mono">{order.order_id} · 数量 {order.quantity || 1}</div></td>
                      <td className="px-4 py-3"><div className="text-sm font-bold text-gray-800 dark:text-gray-100">{order.buyer_id}</div>{order.created_at && <div className="text-xs text-gray-400 mt-1">{order.created_at}</div>}</td>
                      <td className="px-4 py-3 text-sm font-extrabold text-gray-900 dark:text-white">¥{order.amount || '0.00'}</td>
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
