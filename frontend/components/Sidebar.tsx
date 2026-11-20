import React, { useState } from 'react';
import {
  LayoutDashboard,
  Users,
  ShoppingBag,
  CreditCard,
  Settings,
  LogOut,
  Box,
  MessageSquare,
  ShieldBan,
  TrendingUp,
  Search,
  Truck,
  HeartHandshake,
  Activity,
  Send,
  Layers,
  X,
  ChevronDown,
  ChevronRight,
  Sparkles,
  ShieldCheck,
} from 'lucide-react';

interface SidebarProps {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  onLogout: () => void;
  mobileOpen: boolean;
  onClose: () => void;
}

interface MenuItem {
  id: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  badge?: string;
  badgeColor?: string;
}

interface MenuGroup {
  id: string;
  title: string;
  icon?: React.ComponentType<{ className?: string }>;
  items: MenuItem[];
}

const Sidebar: React.FC<SidebarProps> = ({ activeTab, setActiveTab, onLogout, mobileOpen, onClose }) => {
  // 商业闭源工作台五大业务分组（学习自闲管家、阿奇索·鱼店长分组矩阵）
  const menuGroups: MenuGroup[] = [
    {
      id: 'console',
      title: '工作台与数据',
      items: [
        { id: 'dashboard', icon: LayoutDashboard, label: '概览仪表盘' },
        { id: 'exposure', icon: TrendingUp, label: '曝光流量大盘', badge: '实时' },
      ],
    },
    {
      id: 'fulfillment',
      title: '订单与履约中心',
      items: [
        { id: 'orders', icon: ShoppingBag, label: '订单管理' },
        { id: 'fulfillment', icon: Truck, label: '发货可靠账本', badge: '底座', badgeColor: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' },
        { id: 'cards', icon: CreditCard, label: '卡密与库存' },
        { id: 'outbox', icon: Send, label: '发件合规箱', badge: '防封', badgeColor: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300' },
      ],
    },
    {
      id: 'marketing',
      title: '商品与货源营销',
      items: [
        { id: 'items', icon: Box, label: '在售商品' },
        { id: 'sourcing', icon: Search, label: '货源监控比价', badge: '雷达', badgeColor: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300' },
        { id: 'templates', icon: Layers, label: '批量发布与裂变', badge: '队列' },
        { id: 'keywords', icon: MessageSquare, label: '自动回复词库' },
      ],
    },
    {
      id: 'crm',
      title: '客情服务与风控',
      items: [
        { id: 'care', icon: HeartHandshake, label: '催款好评关怀', badge: '回款' },
        { id: 'blacklist', icon: ShieldBan, label: '跨店防白嫖黑名单', badge: '联防', badgeColor: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300' },
        { id: 'diagnosis', icon: Activity, label: '系统诊断看门狗', badge: '守护' },
      ],
    },
    {
      id: 'system',
      title: '系统与矩阵配置',
      items: [
        { id: 'accounts', icon: Users, label: '矩阵店铺账号' },
        { id: 'settings', icon: Settings, label: '系统与 AI 配置' },
      ],
    },
  ];

  // 折叠分组状态管理（默认全部展开，支持用户自由折叠）
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

  const toggleGroup = (groupId: string) => {
    setCollapsedGroups(prev => ({ ...prev, [groupId]: !prev[groupId] }));
  };

  return (
    <>
      {mobileOpen && (
        <button
          type="button"
          onClick={onClose}
          className="fixed inset-0 z-30 bg-black/45 backdrop-blur-[2px] md:hidden"
          aria-label="关闭导航菜单"
        />
      )}
      <aside
        id="app-sidebar"
        aria-label="主导航"
        className={`fixed left-0 top-0 z-40 flex h-dvh w-64 flex-col justify-between border-r border-gray-100 bg-white shadow-[4px_0_24px_rgba(0,0,0,0.08)] transition-transform duration-200 ease-out dark:border-gray-700/60 dark:bg-[#1B1E22] md:z-20 md:translate-x-0 ${
          mobileOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {/* 顶部 Logo 与系统标识 */}
        <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
          <div className="flex items-center gap-3 px-5 pt-5 pb-4 border-b border-gray-50 dark:border-gray-800">
            <div className="w-9 h-9 bg-[#FFE815] dark:bg-[#30343a] rounded-lg flex items-center justify-center border border-yellow-300 dark:border-gray-600 shadow-sm">
              <span className="text-black font-extrabold text-lg">闲</span>
            </div>
            <div>
              <div className="flex items-center gap-1.5">
                <h1 className="text-base font-extrabold tracking-tight text-gray-900 dark:text-white">闲鱼智控</h1>
                <span className="text-[10px] font-black bg-black text-[#FFE815] px-1.5 py-0.5 rounded shadow-sm">PRO</span>
              </div>
              <p className="text-[11px] text-gray-400 dark:text-gray-500 font-medium">商业闭源自动化工作台</p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="ml-auto flex h-8 w-8 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none dark:text-gray-400 dark:hover:bg-[#25292E] dark:hover:text-white md:hidden"
              aria-label="关闭导航菜单"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>

          {/* 分组式层级导航菜单 */}
          <nav className="flex-1 overflow-y-auto px-3 py-3 space-y-4 text-xs select-none custom-scrollbar">
            {menuGroups.map(group => {
              const isCollapsed = !!collapsedGroups[group.id];
              const hasActiveItem = group.items.some(it => it.id === activeTab);

              return (
                <div key={group.id} className="space-y-1">
                  {/* 分组标题头 */}
                  <div
                    onClick={() => toggleGroup(group.id)}
                    className="flex items-center justify-between px-2.5 py-1 text-[11px] font-bold text-gray-400 dark:text-gray-500 uppercase tracking-wider cursor-pointer hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
                  >
                    <span className="flex items-center gap-1.5">
                      {group.title}
                      {hasActiveItem && (
                        <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 dark:bg-yellow-500 animate-pulse" />
                      )}
                    </span>
                    <button type="button" className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200">
                      {isCollapsed ? <ChevronRight className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                    </button>
                  </div>

                  {/* 分组子菜单项 */}
                  {!isCollapsed && (
                    <div className="space-y-0.5 pl-0.5">
                      {group.items.map(item => {
                        const Icon = item.icon;
                        const isActive = activeTab === item.id;
                        return (
                          <button
                            key={item.id}
                            type="button"
                            onClick={() => {
                              setActiveTab(item.id);
                              onClose();
                            }}
                            className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs transition-all duration-150 group relative ${
                              isActive
                                ? 'bg-[#FFF7B2] dark:bg-[#2B2F35] text-gray-900 dark:text-white font-bold shadow-sm border border-yellow-300/80 dark:border-gray-600'
                                : 'text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-[#24272D] hover:text-gray-900 dark:hover:text-gray-200 border border-transparent'
                            }`}
                          >
                            <Icon
                              className={`w-4 h-4 transition-colors shrink-0 ${
                                isActive
                                  ? 'text-gray-900 dark:text-yellow-400'
                                  : 'text-gray-400 group-hover:text-gray-600 dark:group-hover:text-gray-300'
                              }`}
                            />
                            <span className="truncate tracking-normal text-left">{item.label}</span>
                            {item.badge && (
                              <span
                                className={`ml-auto text-[10px] font-bold px-1.5 py-0.2 rounded shrink-0 ${
                                  item.badgeColor || 'bg-[#FFE815]/30 text-yellow-900 dark:bg-yellow-900/30 dark:text-yellow-300'
                                }`}
                              >
                                {item.badge}
                              </span>
                            )}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </nav>
        </div>

        {/* 底部系统状态与退出登录 */}
        <div className="p-3 border-t border-gray-100 dark:border-gray-800 bg-gray-50/50 dark:bg-[#181a1e]">
          <div className="flex items-center justify-between px-2 py-1.5 mb-2 rounded bg-white dark:bg-[#22252a] border border-gray-100 dark:border-gray-700/60 shadow-xs">
            <div className="flex items-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span className="text-[11px] font-medium text-gray-500 dark:text-gray-400">守护服务就绪 (8080)</span>
            </div>
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
          </div>

          <button
            type="button"
            onClick={() => {
              onLogout();
              onClose();
            }}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 text-gray-500 hover:text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-950/30 rounded-lg transition-colors duration-150 font-medium text-xs"
          >
            <LogOut className="w-3.5 h-3.5" />
            <span>退出登录</span>
          </button>
        </div>
      </aside>
    </>
  );
};

export default Sidebar;
