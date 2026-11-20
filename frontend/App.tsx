import React, { useState, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import Dashboard from './components/Dashboard';
import AccountList from './components/AccountList';
import OrderList from './components/OrderList';
import CardList from './components/CardList';
import ItemList from './components/ItemList';
import Settings from './components/Settings';
import Keywords from './components/Keywords';
import BlacklistManager from './components/BlacklistManager';
import ExposureOverview from './components/ExposureOverview';
import SourcingCompare from './components/SourcingCompare';
import WorkbenchIframeView from './components/WorkbenchIframeView';
import { getAuthMode, login, verifyToken, type AuthMode } from './services/api';
import { ShieldCheck, ArrowRight, Loader2, User, Lock, KeyRound, Menu, Moon, Sun } from 'lucide-react';

const App: React.FC = () => {
  const [isLoggedIn, setIsLoggedIn] = useState(false);
  const [activeTab, setActiveTab] = useState('dashboard');
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [accessToken, setAccessToken] = useState('');
  const [loginMethod, setLoginMethod] = useState<'password' | 'token'>('password');
  const [authMode, setAuthMode] = useState<AuthMode>({
    token_login_enabled: false,
    token_only_enabled: false,
    password_login_enabled: true,
  });
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isDark, setIsDark] = useState(() => {
    const savedTheme = localStorage.getItem('theme');
    if (savedTheme) return savedTheme === 'dark';
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
  });

  useEffect(() => {
    document.documentElement.classList.toggle('dark', isDark);
    localStorage.setItem('theme', isDark ? 'dark' : 'light');
  }, [isDark]);

  const themeToggle = (
    <button
      type="button"
      onClick={() => setIsDark(current => !current)}
      className="fixed right-5 top-5 z-[10000] w-10 h-10 rounded-lg bg-white/90 dark:bg-[#25282d] border border-gray-200 dark:border-gray-700 shadow-sm backdrop-blur flex items-center justify-center text-gray-700 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-[#343941] transition-colors"
      title={isDark ? '切换为浅色主题' : '切换为暗色主题'}
      aria-label={isDark ? '切换为浅色主题' : '切换为暗色主题'}
    >
      {isDark ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
    </button>
  );

  // Check auth on mount
  useEffect(() => {
      getAuthMode().then(mode => {
          setAuthMode(mode);
          if (mode.token_only_enabled) setLoginMethod('token');
      }).catch(() => undefined);
      const token = localStorage.getItem('auth_token');
      if (token) {
          verifyToken()
            .then(() => setIsLoggedIn(true))
            .catch(() => {
              localStorage.removeItem('auth_token');
              setIsLoggedIn(false);
            })
            .finally(() => setCheckingAuth(false));
      } else {
          setCheckingAuth(false);
      }
      
      const handleLogout = () => setIsLoggedIn(false);
      window.addEventListener('auth:logout', handleLogout);
      return () => window.removeEventListener('auth:logout', handleLogout);
  }, []);

  const handleLogin = async (e: React.FormEvent) => {
      e.preventDefault();
      setLoginLoading(true);
      setLoginError('');
      
      try {
          const res = await login(
            loginMethod === 'token' ? { access_token: accessToken } : { username, password }
          );
          if (res.success && res.token) {
              localStorage.setItem('auth_token', res.token);
              setIsLoggedIn(true);
          } else {
              setLoginError(res.message || '登录失败');
          }
      } catch (err) {
          setLoginError('无法连接服务器');
      } finally {
          setLoginLoading(false);
      }
  };

  if (checkingAuth) {
      return (
          <div className="min-h-screen flex items-center justify-center bg-[#f5f5f7]">
              {themeToggle}
              <Loader2 className="w-8 h-8 text-[#FFE815] animate-spin" />
          </div>
      );
  }

  // Login Screen Component
  if (!isLoggedIn) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#F4F5F7] dark:bg-[#17191d] p-4 relative overflow-hidden font-sans transition-colors">
        {themeToggle}
        <div className="bg-white dark:bg-[#22252a] p-8 md:p-10 rounded-lg shadow-xl w-full max-w-md border border-gray-200 dark:border-gray-700 relative z-10 animate-fade-in">
          
          {/* Header with Logo */}
          <div className="text-center mb-10">
             <div className="w-24 h-24 bg-[#FFE815] rounded-[2rem] flex items-center justify-center shadow-xl shadow-yellow-200 mx-auto mb-6 transform rotate-[-6deg] hover:rotate-0 transition-all duration-500 cursor-pointer group">
                <span className="text-black font-extrabold text-5xl group-hover:scale-110 transition-transform">闲</span>
             </div>
             <h2 className="text-3xl font-extrabold text-gray-900 dark:text-white mb-2">欢迎回来</h2>
             <p className="text-gray-500 dark:text-gray-400 font-medium">闲鱼智能自动发货与管家系统</p>
          </div>

          {authMode.token_login_enabled && !authMode.token_only_enabled && (
            <div className="grid grid-cols-2 gap-1 p-1 mb-5 rounded-lg bg-gray-100 dark:bg-[#181a1e]" role="tablist" aria-label="登录方式">
              <button type="button" onClick={() => setLoginMethod('password')} className={`h-10 rounded-md text-sm font-bold transition-colors ${loginMethod === 'password' ? 'bg-white dark:bg-[#30343a] text-gray-900 dark:text-white shadow-sm' : 'text-gray-500'}`}>账号密码</button>
              <button type="button" onClick={() => setLoginMethod('token')} className={`h-10 rounded-md text-sm font-bold transition-colors ${loginMethod === 'token' ? 'bg-white dark:bg-[#30343a] text-gray-900 dark:text-white shadow-sm' : 'text-gray-500'}`}>Token</button>
            </div>
          )}
          
          <form onSubmit={handleLogin} className="space-y-5">
            {loginMethod === 'password' && authMode.password_login_enabled ? <div className="space-y-4">
                <div className="relative group">
                    <User className="absolute left-5 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-400 group-focus-within:text-black transition-colors" />
                    <input 
                        type="text" 
                        placeholder="管理员账号" 
                        value={username}
                        onChange={e => setUsername(e.target.value)}
                        className="w-full ios-input pl-14 pr-6 py-4.5 rounded-2xl text-base h-14"
                    />
                </div>
                <div className="relative group">
                    <Lock className="absolute left-5 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-400 group-focus-within:text-black transition-colors" />
                    <input 
                        type="password" 
                        placeholder="密码" 
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        className="w-full ios-input pl-14 pr-6 py-4.5 rounded-2xl text-base h-14"
                    />
                </div>
            </div> : <div className="relative group">
                <KeyRound className="absolute left-5 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-400 group-focus-within:text-black dark:group-focus-within:text-white transition-colors" />
                <input
                    type="password"
                    autoComplete="current-password"
                    placeholder="访问 Token"
                    value={accessToken}
                    onChange={e => setAccessToken(e.target.value)}
                    className="w-full ios-input pl-14 pr-6 py-4 rounded-xl text-base h-14 dark:bg-[#181a1e] dark:text-white dark:border-gray-700"
                />
            </div>}
            
            {loginError && (
                <div className="p-3 rounded-xl bg-red-50 text-red-500 text-sm text-center font-bold flex items-center justify-center gap-2">
                    <ShieldCheck className="w-4 h-4" /> {loginError}
                </div>
            )}

            <button 
              type="submit" 
              disabled={loginLoading}
              className="w-full ios-btn-primary h-14 rounded-2xl text-lg shadow-xl shadow-yellow-200 mt-2 flex items-center justify-center gap-2 group disabled:opacity-70"
            >
              {loginLoading ? <Loader2 className="w-5 h-5 animate-spin" /> : <>立即登录 <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" /></>}
            </button>
          </form>
          
          <div className="mt-8 pt-6 border-t border-gray-100 dark:border-gray-700">
             <div className="text-center">
                 <span className="text-xs text-gray-400 font-medium tracking-widest uppercase">
                    Xianyu Auto-Dispatch Pro v2.5
                 </span>
             </div>
          </div>
        </div>
      </div>
    );
  }

  // Main App Layout
  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard': return <Dashboard />;
      case 'accounts': return <AccountList />;
      case 'orders': return <OrderList />;
      case 'fulfillment':
        return (
          <WorkbenchIframeView
            title="发货可靠性台账"
            subtitle="逐卡租约幂等管理、发货后24h异常回看、订单定位与两本账监控"
            src="/static/delivery_reliability.html"
            badge="P0 履约底座"
          />
        );
      case 'cards': return <CardList />;
      case 'items': return <ItemList />;
      case 'templates':
        return (
          <WorkbenchIframeView
            title="模板与批量发布"
            subtitle="批量上架批次、参数变量填充、快鱼式 failurePolicy (skip/pause) 队列控制流"
            src="/static/templates.html"
            badge="批量队列"
          />
        );
      case 'exposure': return <ExposureOverview />;
      case 'sourcing': return <SourcingCompare />;
      case 'care':
        return (
          <WorkbenchIframeView
            title="智能催收与评价关怀"
            subtitle="发货满24h催收货闭环、自动好评回读与小红花自动化"
            src="/static/care.html"
            badge="资金回款"
          />
        );
      case 'diagnosis':
        return (
          <WorkbenchIframeView
            title="店铺诊断与看门狗状态"
            subtitle="全店健康体检、七类后台核心线程心跳守护与实时熔断状态"
            src="/static/shop_diagnosis.html"
            badge="实时守护"
          />
        );
      case 'outbox':
        return (
          <WorkbenchIframeView
            title="回复发件箱与合规闸"
            subtitle="三档合规分流（auto直发 / draft扣下待审 / block丢弃），敏感词人工放行"
            src="/static/reply_outbox.html"
            badge="合规防封"
          />
        );
      case 'keywords': return <Keywords />;
      case 'blacklist': return <BlacklistManager />;
      case 'settings': return <Settings />;
      default: return <Dashboard />;
    }
  };

  return (
    <div className="flex min-h-screen overflow-x-hidden bg-[#F4F5F7] dark:bg-[#111315] text-[#111] dark:text-gray-100">
      {themeToggle}
      <button
        type="button"
        onClick={() => setSidebarOpen(true)}
        className="fixed left-4 top-4 z-30 flex h-10 w-10 items-center justify-center rounded-lg border border-gray-200 bg-white/90 text-gray-700 shadow-sm backdrop-blur hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE815] dark:border-gray-700 dark:bg-[#25282d] dark:text-gray-100 dark:hover:bg-[#343941] md:hidden"
        aria-label="打开导航菜单"
        aria-controls="app-sidebar"
        aria-expanded={sidebarOpen}
      >
        <Menu className="h-5 w-5" aria-hidden="true" />
      </button>
      <Sidebar 
        activeTab={activeTab} 
        setActiveTab={setActiveTab} 
        mobileOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onLogout={() => {
            localStorage.removeItem('auth_token');
            setIsLoggedIn(false);
        }} 
      />
      
      <main className="relative h-dvh min-w-0 flex-1 overflow-y-auto scroll-smooth p-4 pt-16 sm:p-6 sm:pt-16 md:ml-64 md:p-8 dark:bg-[#111315]">
        <div className="mx-auto max-w-[1400px] pb-10">
            {renderContent()}
        </div>
      </main>
    </div>
  );
};

export default App;
