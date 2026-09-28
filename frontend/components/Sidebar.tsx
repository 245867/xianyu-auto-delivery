import React from 'react';
import { LayoutDashboard, Users, ShoppingBag, CreditCard, Settings, LogOut, Box, MessageSquare, ShieldBan } from 'lucide-react';

interface SidebarProps {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  onLogout: () => void;
}

const Sidebar: React.FC<SidebarProps> = ({ activeTab, setActiveTab, onLogout }) => {
  const menuItems = [
    { id: 'dashboard', icon: LayoutDashboard, label: '仪表盘' },
    { id: 'accounts', icon: Users, label: '账号管理' },
    { id: 'orders', icon: ShoppingBag, label: '订单管理' },
    { id: 'cards', icon: CreditCard, label: '卡密库存' },
    { id: 'items', icon: Box, label: '商品列表' },
    { id: 'keywords', icon: MessageSquare, label: '关键词管理' },
    { id: 'blacklist', icon: ShieldBan, label: '黑名单管理' },
    { id: 'settings', icon: Settings, label: '系统与AI' },
  ];

  return (
    <div className="w-64 h-screen fixed left-0 top-0 bg-white dark:bg-[#1B1E22] border-r border-gray-100 dark:border-gray-700 flex flex-col justify-between z-20 shadow-[4px_0_24px_rgba(0,0,0,0.02)]">
      <div className="p-5 overflow-y-auto">
        <div className="flex items-center gap-3 mb-7 px-2">
          <div className="w-10 h-10 bg-[#FFE815] dark:bg-[#30343a] rounded-lg flex items-center justify-center border border-yellow-300 dark:border-gray-600">
            <span className="text-black font-extrabold text-xl">闲</span>
          </div>
          <h1 className="text-xl font-extrabold tracking-tight text-gray-900 dark:text-white">闲鱼智控 <span className="text-xs bg-black text-[#FFE815] px-1.5 py-0.5 rounded ml-1">PRO</span></h1>
        </div>

        <nav className="space-y-1.5">
          {menuItems.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => setActiveTab(item.id)}
                className={`w-full flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors duration-150 group relative ${
                  isActive 
                    ? 'bg-[#FFF7B2] dark:bg-[#2B2F35] border-yellow-300 dark:border-[#464c55] text-black dark:text-white font-bold' 
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-[#25292E] hover:text-gray-900 dark:hover:text-white'
                }`}
              >
                <Icon className={`w-5 h-5 transition-colors ${isActive ? 'text-gray-900 dark:text-gray-100' : 'text-gray-400 group-hover:text-gray-600 dark:group-hover:text-gray-200'}`} />
                <span className="text-sm tracking-wide">{item.label}</span>
              </button>
            );
          })}
        </nav>
      </div>

      <div className="p-5 border-t border-gray-50 dark:border-gray-700">
        <button 
          onClick={onLogout}
          className="w-full flex items-center gap-3 px-4 py-3 text-gray-500 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors duration-150 font-medium"
        >
          <LogOut className="w-5 h-5" />
          <span className="text-sm">退出登录</span>
        </button>
      </div>
    </div>
  );
};

export default Sidebar;
