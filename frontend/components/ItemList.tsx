import React, { useEffect, useState } from 'react';
import { Item, AccountDetail } from '../types';
import {
  getItems, getAccountDetails, syncItemsFromAccount,
  getCards, deleteItem, updateItemDeliverySettings,
  updateItemMultiSpec, updateItemMultiQuantityDelivery, updateItemRefundBlacklist
} from '../services/api';
import { Box, RefreshCw, ShoppingBag, Edit, Trash2, Plus, ShieldBan, Bot, Truck } from 'lucide-react';

const ItemList: React.FC = () => {
  const [items, setItems] = useState<Item[]>([]);
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [cards, setCards] = useState<any[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [updatingKey, setUpdatingKey] = useState('');
  const [deletingKey, setDeletingKey] = useState('');
  const [showEditModal, setShowEditModal] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  const [selectedItem, setSelectedItem] = useState<Item | null>(null);
  const [editForm, setEditForm] = useState<Partial<Item>>({});
  const [addForm, setAddForm] = useState({
    cookie_id: '',
    item_id: '',
    item_title: '',
    item_price: '',
    item_image: '',
    is_multi_spec: false
  });

  useEffect(() => {
    Promise.all([getAccountDetails(), getItems(), getCards()])
      .then(([accountData, itemData, cardData]) => {
        setAccounts(accountData);
        setItems(itemData);
        setCards(cardData);
      })
      .catch(error => {
        console.error('加载商品设置失败:', error);
        alert('商品数据加载失败，请刷新重试');
      });
  }, []);

  const handleSync = async () => {
      if (!selectedAccount) return alert('请先选择账号');
      setLoading(true);
      await syncItemsFromAccount(selectedAccount);
      getItems().then(setItems);
      setLoading(false);
  };

  const handleEdit = (item: Item) => {
    setSelectedItem(item);
    setEditForm({ ...item });
    setShowEditModal(true);
  };

  const handleSaveEdit = async () => {
    if (!selectedItem) return;
    try {
      const updatedItems = items.map(item =>
        item.cookie_id === selectedItem.cookie_id && item.item_id === selectedItem.item_id
          ? { ...item, ...editForm }
          : item
      );
      setItems(updatedItems);
      setShowEditModal(false);
    } catch (error) {
      console.error('更新商品失败:', error);
      alert('更新失败，请重试');
    }
  };

  const handleDelete = async (item: Item) => {
    if (!confirm(`确认删除商品"${item.item_title}"吗？`)) return;

    const key = `${item.cookie_id}-${item.item_id}`;
    try {
      setDeletingKey(key);
      await deleteItem(item.cookie_id, item.item_id);
      setItems(current => current.filter(i =>
        !(i.cookie_id === item.cookie_id && i.item_id === item.item_id)
      ));
    } catch (error) {
      console.error('删除商品失败:', error);
      const message = error instanceof Error ? error.message : '未知错误';
      alert(`删除失败：${message}`);
    } finally {
      setDeletingKey('');
    }
  };

  const saveItemSettings = async (
    item: Item,
    changes: Partial<Pick<Item, 'ai_reply_enabled' | 'auto_delivery_enabled' | 'delivery_card_id'>>
  ) => {
    const key = `${item.cookie_id}-${item.item_id}-settings`;
    const next = {
      ai_reply_enabled: Object.prototype.hasOwnProperty.call(changes, 'ai_reply_enabled')
        ? changes.ai_reply_enabled ?? null
        : item.ai_reply_enabled ?? null,
      auto_delivery_enabled: Object.prototype.hasOwnProperty.call(changes, 'auto_delivery_enabled')
        ? Boolean(changes.auto_delivery_enabled)
        : Boolean(item.auto_delivery_enabled ?? true),
      delivery_card_id: Object.prototype.hasOwnProperty.call(changes, 'delivery_card_id')
        ? (changes.delivery_card_id ?? null)
        : (item.delivery_card_id ?? null),
    };
    try {
      setUpdatingKey(key);
      await updateItemDeliverySettings(item.cookie_id, item.item_id, next);
      setItems(current => current.map(currentItem =>
        currentItem.cookie_id === item.cookie_id && currentItem.item_id === item.item_id
          ? { ...currentItem, ...next }
          : currentItem
      ));
    } catch (error) {
      console.error('保存商品设置失败:', error);
      alert(`保存商品设置失败：${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      setUpdatingKey('');
    }
  };

  const cycleAiSetting = (item: Item) => {
    const current = item.ai_reply_enabled;
    const next = current === null || current === undefined ? true : current ? false : null;
    saveItemSettings(item, { ai_reply_enabled: next });
  };

  const handleAddItem = async () => {
    try {
      const newItem: Item = {
        ...addForm,
        id: Date.now().toString()
      } as Item;
      setItems([newItem, ...items]);
      setShowAddModal(false);
      setAddForm({
        cookie_id: '',
        item_id: '',
        item_title: '',
        item_price: '',
        item_image: '',
        is_multi_spec: false
      });
    } catch (error) {
      console.error('添加商品失败:', error);
      alert('添加失败，请重试');
    }
  };

  const toggleMultiSpec = async (item: Item) => {
    const nextValue = !Boolean(item.is_multi_spec);
    const key = `${item.cookie_id}-${item.item_id}-spec`;
    try {
      setUpdatingKey(key);
      await updateItemMultiSpec(item.cookie_id, item.item_id, nextValue);
      const updatedItems = items.map(i =>
        i.cookie_id === item.cookie_id && i.item_id === item.item_id
          ? { ...i, is_multi_spec: nextValue }
          : i
      );
      setItems(updatedItems);
    } catch (error) {
      console.error('切换状态失败:', error);
      alert('保存多规格设置失败');
    } finally {
      setUpdatingKey('');
    }
  };

  const toggleMultiQty = async (item: Item) => {
    const nextValue = !Boolean(item.multi_quantity_delivery);
    const key = `${item.cookie_id}-${item.item_id}-quantity`;
    try {
      setUpdatingKey(key);
      await updateItemMultiQuantityDelivery(item.cookie_id, item.item_id, nextValue);
      const updatedItems = items.map(i =>
        i.cookie_id === item.cookie_id && i.item_id === item.item_id
          ? { ...i, multi_quantity_delivery: nextValue }
          : i
      );
      setItems(updatedItems);
    } catch (error) {
      console.error('切换状态失败:', error);
      alert('保存多数量发货设置失败');
    } finally {
      setUpdatingKey('');
    }
  };

  const toggleRefundBlacklist = async (item: Item) => {
    const nextValue = !Boolean(item.refund_blacklist_enabled);
    const key = `${item.cookie_id}-${item.item_id}-refund`;
    try {
      setUpdatingKey(key);
      await updateItemRefundBlacklist(item.cookie_id, item.item_id, nextValue);
      setItems(current => current.map(i =>
        i.cookie_id === item.cookie_id && i.item_id === item.item_id
          ? { ...i, refund_blacklist_enabled: nextValue }
          : i
      ));
    } catch (error) {
      console.error('切换退款拉黑失败:', error);
      alert('保存退款拉黑设置失败');
    } finally {
      setUpdatingKey('');
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-3xl font-bold text-gray-900">商品管理</h2>
          <p className="text-gray-500 mt-2 text-sm">监控并管理所有账号下的闲鱼商品。</p>
        </div>
        <div className="flex gap-3">
            <select
                className="ios-input px-4 py-3 rounded-xl text-sm"
                value={selectedAccount}
                onChange={e => setSelectedAccount(e.target.value)}
            >
                <option value="">选择账号以同步</option>
                {accounts.map(acc => (
                    <option key={acc.id} value={acc.id}>{acc.nickname}</option>
                ))}
            </select>
            <button
                onClick={handleSync}
                disabled={loading || !selectedAccount}
                className="ios-btn-primary flex items-center gap-2 px-6 py-3 rounded-2xl font-bold shadow-lg shadow-yellow-200 disabled:opacity-50"
            >
                <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                同步商品
            </button>
            <button
              onClick={() => setShowAddModal(true)}
              className="px-5 py-3 rounded-2xl font-bold bg-gray-900 text-white hover:bg-gray-800 transition-colors flex items-center gap-2 shadow-lg"
            >
              <Plus className="w-4 h-4" />
              添加商品
            </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
          {items.map(item => (
              <div key={`${item.cookie_id}-${item.item_id}`} className="ios-card p-4 rounded-3xl hover:shadow-lg transition-all group relative">
                  <div className="absolute top-3 right-3 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity z-10">
                      <button
                        onClick={() => handleEdit(item)}
                        className="p-2 bg-white/90 backdrop-blur rounded-lg shadow-md hover:bg-[#FFE815] transition-colors"
                        title="编辑"
                      >
                        <Edit className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(item)}
                        disabled={deletingKey === `${item.cookie_id}-${item.item_id}`}
                        className="p-2 bg-white/90 backdrop-blur rounded-lg shadow-md hover:bg-red-100 text-red-500 transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                        title={deletingKey === `${item.cookie_id}-${item.item_id}` ? '正在删除' : '删除'}
                      >
                        {deletingKey === `${item.cookie_id}-${item.item_id}`
                          ? <RefreshCw className="w-4 h-4 animate-spin" />
                          : <Trash2 className="w-4 h-4" />}
                      </button>
                  </div>
                  <div className="aspect-square bg-gray-100 rounded-2xl mb-4 overflow-hidden relative">
                      {item.item_image ? (
                          <img src={item.item_image} alt="" className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" />
                      ) : (
                          <div className="w-full h-full flex items-center justify-center text-gray-300">
                              <Box className="w-10 h-10" />
                          </div>
                      )}
                      <div className="absolute top-2 left-2 bg-black/50 backdrop-blur-md text-white text-xs font-bold px-2 py-1 rounded-lg">
                          ¥{item.item_price}
                      </div>
                  </div>
                  <h3 className="font-bold text-gray-900 line-clamp-2 text-sm mb-2 h-10">{item.item_title}</h3>
                  <div className="flex justify-between items-center text-xs text-gray-500 mb-2">
                      <span className="bg-gray-100 px-2 py-1 rounded-md truncate max-w-[100px]">ID: {item.item_id}</span>
                  </div>
                  <div className="mt-3 rounded-2xl border border-gray-200 dark:border-[#3B424C] bg-white dark:bg-[#1E2227] p-3 shadow-sm dark:shadow-none">
                    <div className="flex items-center justify-between gap-2 mb-3">
                      <span className="text-xs font-extrabold tracking-wide text-gray-900 dark:text-gray-100 whitespace-nowrap">自动化设置</span>
                      <span className={`text-[10px] font-extrabold px-2 py-1 rounded-md whitespace-nowrap border ${Boolean(item.auto_delivery_enabled ?? true) ? 'bg-green-50 border-green-200 text-green-700 dark:bg-[#164B35] dark:border-[#27704D] dark:text-[#B8F0D2]' : 'bg-gray-100 border-gray-200 text-gray-600 dark:bg-[#343A42] dark:border-[#4A525D] dark:text-gray-200'}`}>
                        自动发货 {Boolean(item.auto_delivery_enabled ?? true) ? '已开启' : '已关闭'}
                      </span>
                    </div>

                    <div className="grid grid-cols-2 gap-2 mb-3">
                      <button
                        onClick={() => cycleAiSetting(item)}
                        disabled={Boolean(updatingKey)}
                        title="跟随账号、开启或关闭商品 AI 回复"
                        className={`min-h-11 rounded-lg border px-2 py-1.5 text-left transition-colors disabled:opacity-50 ${
                          item.ai_reply_enabled === true
                            ? 'bg-blue-50 border-blue-300 text-blue-800 dark:bg-[#17395B] dark:border-[#32699B] dark:text-[#B8DCFF]'
                            : item.ai_reply_enabled === false
                              ? 'bg-gray-50 border-gray-300 text-gray-700 dark:bg-[#292E35] dark:border-[#4A525D] dark:text-gray-200'
                              : 'bg-blue-50 border-blue-200 text-blue-700 dark:bg-[#1E334B] dark:border-[#2D5274] dark:text-[#B8DCFF]'
                        }`}
                      >
                        <span className="flex items-center gap-1 text-[10px] font-bold opacity-80"><Bot className="w-3.5 h-3.5" />AI 回复</span>
                        <span className="block text-[11px] font-extrabold mt-0.5">{item.ai_reply_enabled === true ? '已开启' : item.ai_reply_enabled === false ? '已关闭' : '跟随账号'}</span>
                      </button>
                      <button
                        onClick={() => saveItemSettings(item, { auto_delivery_enabled: !Boolean(item.auto_delivery_enabled ?? true) })}
                        disabled={Boolean(updatingKey)}
                        title="开启后才会为该商品自动发卡"
                        className={`min-h-11 rounded-lg border px-2 py-1.5 text-left transition-colors disabled:opacity-50 ${
                          Boolean(item.auto_delivery_enabled ?? true)
                            ? 'bg-green-50 border-green-300 text-green-800 dark:bg-[#164B35] dark:border-[#27704D] dark:text-[#B8F0D2]'
                            : 'bg-gray-50 border-gray-300 text-gray-700 dark:bg-[#292E35] dark:border-[#4A525D] dark:text-gray-200'
                        }`}
                      >
                        <span className="flex items-center gap-1 text-[10px] font-bold opacity-80"><Truck className="w-3.5 h-3.5" />自动发货</span>
                        <span className="block text-[11px] font-extrabold mt-0.5">{Boolean(item.auto_delivery_enabled ?? true) ? '已开启' : '已关闭'}</span>
                      </button>
                    </div>

                    <label className="block text-[10px] font-bold text-gray-500 dark:text-gray-400 mb-1.5">自动卡密组</label>
                    <select
                      value={item.delivery_card_id ?? ''}
                      disabled={Boolean(updatingKey)}
                      onChange={e => saveItemSettings(item, {
                        delivery_card_id: e.target.value ? Number(e.target.value) : null
                      })}
                      className="w-full min-w-0 h-9 text-[11px] font-bold px-2.5 rounded-lg bg-gray-50 text-gray-800 border border-gray-300 dark:bg-[#292E35] dark:border-[#4A525D] dark:text-gray-100 disabled:opacity-50 truncate"
                      title="指定卡密分组；留空时使用自动发货规则"
                    >
                      <option value="">按关键词规则（未指定分组）</option>
                      {cards.map(card => (
                        <option key={card.id} value={card.id}>
                          {card.name}{card.type === 'data' ? '（批量库存）' : ''}
                        </option>
                      ))}
                    </select>

                    <div className="mt-3 pt-3 border-t border-gray-200 dark:border-[#343A42]">
                      <p className="text-[10px] font-bold text-gray-500 dark:text-gray-400 mb-1.5">商品能力</p>
                      <div className="grid grid-cols-2 gap-2">
                      <button
                        onClick={() => toggleMultiSpec(item)}
                        disabled={Boolean(updatingKey)}
                        className={`text-xs font-bold px-2 py-2 rounded-lg border transition-colors disabled:opacity-50 ${
                          item.is_multi_spec
                            ? 'bg-blue-50 border-blue-300 text-blue-800 dark:bg-[#17395B] dark:border-[#32699B] dark:text-[#B8DCFF]'
                            : 'bg-gray-50 border-gray-300 text-gray-700 dark:bg-[#292E35] dark:border-[#4A525D] dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#343A42]'
                        }`}
                      >
                        多规格
                      </button>
                      <button
                        onClick={() => toggleMultiQty(item)}
                        disabled={Boolean(updatingKey)}
                        className={`text-xs font-bold px-2 py-2 rounded-lg border transition-colors disabled:opacity-50 ${
                          item.multi_quantity_delivery
                            ? 'bg-green-50 border-green-300 text-green-800 dark:bg-[#164B35] dark:border-[#27704D] dark:text-[#B8F0D2]'
                            : 'bg-gray-50 border-gray-300 text-gray-700 dark:bg-[#292E35] dark:border-[#4A525D] dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-[#343A42]'
                        }`}
                      >
                        多数量发货
                      </button>
                      <button
                        onClick={() => toggleRefundBlacklist(item)}
                        disabled={Boolean(updatingKey)}
                        title="曾退款买家不再自动发货"
                        className={`col-span-2 min-h-9 rounded-lg border px-2 py-1.5 text-[11px] font-extrabold transition-colors disabled:opacity-50 flex items-center justify-center gap-1 ${
                          item.refund_blacklist_enabled
                            ? 'bg-red-50 border-red-300 text-red-800 dark:bg-[#542A31] dark:border-[#8C4650] dark:text-[#FFC1C8]'
                            : 'bg-gray-50 border-gray-300 text-gray-700 dark:bg-[#292E35] dark:border-[#4A525D] dark:text-gray-200 hover:bg-red-50 hover:border-red-200 hover:text-red-700 dark:hover:bg-[#3B282D] dark:hover:border-[#75434A] dark:hover:text-[#FFC1C8]'
                        }`}
                      >
                        <ShieldBan className="w-3.5 h-3.5 flex-shrink-0" />
                        退款拉黑
                      </button>
                      </div>
                    </div>
                  </div>
              </div>
          ))}
          {items.length === 0 && (
             <div className="col-span-full py-20 text-center text-gray-400">
                 <ShoppingBag className="w-12 h-12 mx-auto mb-4 opacity-30" />
                 暂无商品数据，请选择账号进行同步
             </div>
          )}
      </div>
    </div>
  );
};

export default ItemList;
