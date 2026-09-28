import React, { useEffect, useState } from 'react';
import { getAIProviderStatus, getSystemSettings, updateSystemSettings, type AIProviderStatus } from '../services/api';
import { SystemSettings } from '../types';
import {
  Bot, Save, Lock, Sparkles, Mail, Settings as SettingsIcon,
  Eye, EyeOff, RefreshCw, Database, ShieldBan, AlertTriangle, Loader2
} from 'lucide-react';

const Settings: React.FC = () => {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState('');

  // Password visibility states
  const [showSmtpPassword, setShowSmtpPassword] = useState(false);
  const [aiProvider, setAiProvider] = useState<AIProviderStatus | null>(null);

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    setLoading(true);
    setLoadError('');
    try {
      const [systemResult, providerResult] = await Promise.allSettled([
        getSystemSettings(),
        getAIProviderStatus(),
      ]);
      if (systemResult.status === 'fulfilled') {
        setSettings(systemResult.value);
      } else {
        setLoadError(systemResult.reason instanceof Error ? systemResult.reason.message : '系统配置读取失败');
      }
      if (providerResult.status === 'fulfilled') {
        setAiProvider(providerResult.value);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
      if(!settings) return;
      setSaving(true);
      try {
        await updateSystemSettings(settings);
        alert('系统配置已保存');
      } catch (e) {
        alert('保存失败：' + (e as Error).message);
      } finally {
        setSaving(false);
      }
  };

  if (!settings) {
    return (
      <div className="min-h-[420px] flex items-center justify-center">
        {loading ? (
          <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        ) : (
          <div className="w-full max-w-md rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/30 p-5 text-center">
            <AlertTriangle className="w-6 h-6 mx-auto text-red-500 mb-3" />
            <div className="font-bold text-red-700 dark:text-red-300">{loadError || '配置读取失败'}</div>
            <button type="button" onClick={() => void loadSettings()} className="mt-4 h-10 px-4 rounded-lg border border-red-200 dark:border-red-800 text-sm font-bold text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-950/50">重新加载</button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto space-y-6 animate-fade-in pb-20">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 bg-gray-100 rounded-2xl flex items-center justify-center">
              <SettingsIcon className="w-6 h-6 text-gray-600" />
          </div>
          <div>
              <h2 className="text-2xl font-extrabold text-gray-900">系统设置</h2>
              <p className="text-gray-500 mt-1 text-sm font-medium">配置全局自动化规则与系统参数</p>
          </div>
        </div>
        <button
          onClick={loadSettings}
          className="px-4 py-2 bg-gray-100 hover:bg-gray-200 rounded-xl font-bold text-gray-700 flex items-center gap-2 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
          刷新
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Left Column */}
        <div className="space-y-8">
          {/* Basic Settings */}
          <section className="space-y-4">
            <h3 className="text-lg font-extrabold text-gray-800 flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-gray-100 text-gray-600">
                    <Database className="w-4 h-4" />
                </div>
                基础设置
            </h3>

            <div className="ios-card rounded-lg p-5 bg-white space-y-3">
              <div className="flex items-center justify-between p-4 bg-gray-50 rounded-xl">
                <div>
                  <div className="font-bold text-gray-900">允许用户注册</div>
                  <div className="text-xs text-gray-500 mt-1">开启后允许新用户注册账号</div>
                </div>
                <button
                  onClick={() => setSettings({...settings, registration_enabled: !settings.registration_enabled})}
                  className={`w-14 h-8 rounded-full transition-all relative ${
                    settings.registration_enabled ? 'bg-[#FFE815]' : 'bg-gray-300'
                  }`}
                >
                  <div
                    className={`w-6 h-6 bg-white rounded-full absolute top-1 transition-all shadow-md ${
                      settings.registration_enabled ? 'left-7' : 'left-1'
                    }`}
                  />
                </button>
              </div>

              <div className="flex items-center justify-between p-4 bg-gray-50 rounded-xl">
                <div>
                  <div className="font-bold text-gray-900">显示默认登录信息</div>
                  <div className="text-xs text-gray-500 mt-1">登录页面显示默认账号密码提示</div>
                </div>
                <button
                  onClick={() => setSettings({...settings, show_default_login_info: !settings.show_default_login_info})}
                  className={`w-14 h-8 rounded-full transition-all relative ${
                    settings.show_default_login_info ? 'bg-[#FFE815]' : 'bg-gray-300'
                  }`}
                >
                  <div
                    className={`w-6 h-6 bg-white rounded-full absolute top-1 transition-all shadow-md ${
                      settings.show_default_login_info ? 'left-7' : 'left-1'
                    }`}
                  />
                </button>
              </div>

              <div className="flex items-center justify-between p-4 bg-gray-50 rounded-xl">
                <div>
                  <div className="font-bold text-gray-900">登录滑动验证码</div>
                  <div className="text-xs text-gray-500 mt-1">开启后账号密码登录需要完成滑动验证</div>
                </div>
                <button
                  onClick={() => setSettings({...settings, login_captcha_enabled: !settings.login_captcha_enabled})}
                  className={`w-14 h-8 rounded-full transition-all relative ${
                    settings.login_captcha_enabled ? 'bg-[#FFE815]' : 'bg-gray-300'
                  }`}
                >
                  <div
                    className={`w-6 h-6 bg-white rounded-full absolute top-1 transition-all shadow-md ${
                      settings.login_captcha_enabled ? 'left-7' : 'left-1'
                    }`}
                  />
                </button>
              </div>

              <div className="flex items-center justify-between gap-4 p-4 bg-red-50 rounded-xl border border-red-100">
                <div className="min-w-0">
                  <div className="font-bold text-gray-900 flex items-center gap-2">
                    <ShieldBan className="w-4 h-4 text-red-500 flex-shrink-0" />
                    恶意退款拉黑
                  </div>
                  <div className="text-xs text-gray-500 mt-1">
                    全局开启后，曾退款的买家不再自动发货；关闭时按商品单独开关判断
                  </div>
                </div>
                <button
                  type="button"
                  aria-label="切换全局恶意退款拉黑"
                  onClick={() => setSettings({
                    ...settings,
                    refund_blacklist_global_enabled: !settings.refund_blacklist_global_enabled
                  })}
                  className={`w-14 h-8 rounded-full transition-all relative flex-shrink-0 ${
                    settings.refund_blacklist_global_enabled ? 'bg-[#FFE815]' : 'bg-gray-300'
                  }`}
                >
                  <div
                    className={`w-6 h-6 bg-white rounded-full absolute top-1 transition-all shadow-md ${
                      settings.refund_blacklist_global_enabled ? 'left-7' : 'left-1'
                    }`}
                  />
                </button>
              </div>

              <div className="flex items-center justify-between p-4 bg-gray-50 rounded-xl">
                <div>
                  <div className="font-bold text-gray-900">启用商品自动同步</div>
                  <div className="text-xs text-gray-500 mt-1">定时自动获取商品信息到本地数据库</div>
                </div>
                <button
                  onClick={() => setSettings({...settings, item_sync_enabled: !settings.item_sync_enabled})}
                  className={`w-14 h-8 rounded-full transition-all relative ${
                    settings.item_sync_enabled ? 'bg-[#FFE815]' : 'bg-gray-300'
                  }`}
                >
                  <div
                    className={`w-6 h-6 bg-white rounded-full absolute top-1 transition-all shadow-md ${
                      settings.item_sync_enabled ? 'left-7' : 'left-1'
                    }`}
                  />
                </button>
              </div>

              <div className="space-y-3 px-4">
                <label className="block text-sm font-bold text-gray-800">商品同步间隔（分钟）</label>
                <input
                  type="number"
                  value={Math.round((settings.item_sync_interval || 600) / 60)}
                  onChange={(e) => {
                    const minutes = parseInt(e.target.value) || 10;
                    setSettings({...settings, item_sync_interval: minutes * 60});
                  }}
                  className="w-full ios-input px-4 py-3 rounded-xl"
                  min="1"
                  max="1440"
                />
                <p className="text-xs text-gray-500">建议：10-60分钟</p>
              </div>

              <div className="space-y-3 px-4">
                <label className="block text-sm font-bold text-gray-800">每次最多同步页数</label>
                <input
                  type="number"
                  value={settings.item_sync_max_pages || 5}
                  onChange={(e) => setSettings({...settings, item_sync_max_pages: parseInt(e.target.value) || 5})}
                  className="w-full ios-input px-4 py-3 rounded-xl"
                  min="1"
                  max="50"
                />
                <p className="text-xs text-gray-500">每页20个商品</p>
              </div>
            </div>
          </section>

          {/* AI provider configuration */}
          <section className="space-y-4">
            <h3 className="text-lg font-extrabold text-gray-800 flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-[#FFE815] text-black">
                    <Sparkles className="w-4 h-4" />
                </div>
                AI 智能回复配置
            </h3>

            <div className="ios-card rounded-lg p-6 bg-white dark:bg-[#22252a] space-y-5">
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 bg-gray-50 dark:bg-[#181a1e] rounded-lg">
                  <div className="text-xs text-gray-500 mb-1">模型</div>
                  <div className="font-bold text-gray-900 dark:text-white break-words">{aiProvider?.model_name || '未配置'}</div>
                </div>
                <div className="p-4 bg-gray-50 dark:bg-[#181a1e] rounded-lg">
                  <div className="text-xs text-gray-500 mb-1">接口协议</div>
                  <div className="font-bold text-gray-900 dark:text-white uppercase">{aiProvider?.wire_api || '未配置'}</div>
                </div>
              </div>

              <div className={`p-4 rounded-lg border text-sm font-bold ${aiProvider?.configured ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-amber-50 border-amber-200 text-amber-700'}`}>
                {aiProvider?.configured ? '根目录 AI 配置已就绪' : '根目录 AI 配置不完整'}
              </div>

              <div className="space-y-3">
                <label className="block text-sm font-bold text-gray-800">默认自动回复内容</label>
                <textarea
                  className="w-full ios-input px-4 py-3 rounded-xl min-h-[100px] text-sm resize-none"
                  value={settings.default_reply || ''}
                  onChange={e => setSettings({...settings, default_reply: e.target.value})}
                  placeholder="设置默认的自动回复内容..."
                ></textarea>
              </div>

            </div>
          </section>
        </div>

        {/* Right Column */}
        <div className="space-y-8">
          {/* SMTP Settings */}
          <section className="space-y-4">
            <h3 className="text-lg font-extrabold text-gray-800 flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-blue-100 text-blue-600">
                    <Mail className="w-4 h-4" />
                </div>
                SMTP 邮件配置
            </h3>

            <div className="ios-card rounded-lg p-5 bg-white space-y-5">
              <p className="text-sm text-gray-500">配置SMTP服务器用于发送注册验证码等邮件通知</p>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-3">
                  <label className="block text-sm font-bold text-gray-800">SMTP服务器</label>
                  <input
                    type="text"
                    value={settings.smtp_server || ''}
                    onChange={e => setSettings({...settings, smtp_server: e.target.value})}
                    placeholder="smtp.qq.com"
                    className="w-full ios-input px-4 py-3 rounded-xl text-sm"
                  />
                </div>
                <div className="space-y-3">
                  <label className="block text-sm font-bold text-gray-800">SMTP端口</label>
                  <input
                    type="number"
                    value={settings.smtp_port || 587}
                    onChange={e => setSettings({...settings, smtp_port: parseInt(e.target.value)})}
                    placeholder="587"
                    className="w-full ios-input px-4 py-3 rounded-xl text-sm"
                  />
                </div>
              </div>

              <div className="space-y-3">
                <label className="block text-sm font-bold text-gray-800">发件邮箱</label>
                <input
                  type="email"
                  value={settings.smtp_user || ''}
                  onChange={e => setSettings({...settings, smtp_user: e.target.value})}
                  placeholder="your-email@qq.com"
                  className="w-full ios-input px-4 py-3 rounded-xl text-sm"
                />
              </div>

              <div className="space-y-3">
                <label className="block text-sm font-bold text-gray-800">邮箱密码/授权码</label>
                <div className="relative">
                  <input
                    type={showSmtpPassword ? 'text' : 'password'}
                    value={settings.smtp_password || ''}
                    onChange={e => setSettings({...settings, smtp_password: e.target.value})}
                    placeholder="输入密码或授权码"
                    className="w-full ios-input px-4 py-3 pr-12 rounded-xl text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => setShowSmtpPassword(!showSmtpPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 p-2 text-gray-400 hover:text-gray-600 transition-colors"
                  >
                    {showSmtpPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
                <p className="text-xs text-gray-500">QQ邮箱需要使用授权码</p>
              </div>

              <div className="space-y-3">
                <label className="block text-sm font-bold text-gray-800">发件人显示名（可选）</label>
                <input
                  type="text"
                  value={settings.smtp_from || ''}
                  onChange={e => setSettings({...settings, smtp_from: e.target.value})}
                  placeholder="闲鱼自动回复系统"
                  className="w-full ios-input px-4 py-3 rounded-xl text-sm"
                />
              </div>
            </div>
          </section>
        </div>
      </div>

      {/* Save Button */}
      <div className="fixed bottom-6 right-6 z-30">
        <button
            onClick={handleSave}
            disabled={saving}
            className="ios-btn-primary px-5 py-3 rounded-lg text-sm shadow-md dark:shadow-none flex items-center gap-2 transition-colors disabled:opacity-70"
        >
            <Save className="w-4 h-4" />
            {saving ? '保存中...' : '保存所有配置'}
        </button>
      </div>
    </div>
  );
};

export default Settings;
