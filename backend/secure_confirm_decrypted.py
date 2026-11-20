"""
自动确认发货模块 - 解密版本
这是secure_confirm_ultra.py的解密版本，用于自动确认发货功能
"""

import asyncio
import json
import time
import aiohttp
from loguru import logger
from utils.xianyu_utils import generate_sign, trans_cookies
from mtop_response import is_success, retry_policy, policy_text


class SecureConfirm:
    """自动确认发货类"""

    def __init__(self, session, cookies_str, cookie_id, main_instance=None):
        """
        初始化确认发货实例

        Args:
            session: aiohttp会话对象
            cookies_str: Cookie字符串
            cookie_id: Cookie ID
            main_instance: 主实例对象（XianyuLive）
        """
        self.session = session
        self.cookies_str = cookies_str
        self.cookie_id = cookie_id
        self.main_instance = main_instance

        # 解析cookies
        self.cookies = trans_cookies(cookies_str) if cookies_str else {}

        # Token相关属性
        self.current_token = None
        self.last_token_refresh_time = 0
        self.token_refresh_interval = 3600  # 1小时

    def _safe_str(self, obj):
        """安全字符串转换"""
        try:
            return str(obj)
        except:
            return "无法转换的对象"

    async def _get_real_item_id(self):
        """从数据库中获取一个真实的商品ID"""
        try:
            from db_manager import db_manager
            
            # 获取该账号的商品列表
            items = db_manager.get_items_by_cookie(self.cookie_id)
            if items:
                # 返回第一个商品的ID
                item_id = items[0].get('item_id')
                if item_id:
                    logger.debug(f"【{self.cookie_id}】获取到真实商品ID: {item_id}")
                    return item_id
            
            # 如果该账号没有商品，尝试获取任意一个商品ID
            all_items = db_manager.get_all_items()
            if all_items:
                item_id = all_items[0].get('item_id')
                if item_id:
                    logger.debug(f"【{self.cookie_id}】使用其他账号的商品ID: {item_id}")
                    return item_id
            
            logger.warning(f"【{self.cookie_id}】数据库中没有找到任何商品ID")
            return None
            
        except Exception as e:
            logger.error(f"【{self.cookie_id}】获取真实商品ID失败: {self._safe_str(e)}")
            return None

    async def _update_config_cookies(self):
        """更新数据库中的Cookie配置"""
        try:
            from db_manager import db_manager
            # 更新数据库中的cookies
            db_manager.update_cookie_account_info(self.cookie_id, cookie_value=self.cookies_str)
            logger.debug(f"【{self.cookie_id}】已更新数据库中的Cookie")
        except Exception as e:
            logger.error(f"【{self.cookie_id}】更新数据库Cookie失败: {self._safe_str(e)}")


    async def auto_confirm(self, order_id, item_id=None, retry_count=0):
        """自动确认发货 - 使用真实商品ID刷新token"""
        if retry_count >= 4:  # 最多重试3次
            logger.error("自动确认发货失败，重试次数过多")
            return {"error": "自动确认发货失败，重试次数过多"}

        # 保存item_id供Token刷新使用
        if item_id:
            self._current_item_id = item_id
            logger.debug(f"【{self.cookie_id}】设置当前商品ID: {item_id}")

        # 确保session已创建
        if not self.session:
            raise Exception("Session未创建")

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time() * 1000)),   # 拆解 9：毫秒在括号里，末三位不能恒为 0
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.logistic.consign.dummy',
            'sessionOption': 'AutoLoginOnly',
        }

        data_val = '{"orderId":"' + order_id + '", "tradeText":"","picList":[],"newUnconsign":true}'
        data = {
            'data': data_val,
        }

        # 始终从最新的cookies中获取_m_h5_tk token（刷新后cookies会被更新）
        token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

        if token:
            logger.info(f"使用cookies中的_m_h5_tk token: {token}")
        else:
            logger.warning("cookies中没有找到_m_h5_tk token")

        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign

        try:
            logger.info(f"【{self.cookie_id}】开始自动确认发货，订单ID: {order_id}")
            async with self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.logistic.consign.dummy/1.0/',
                params=params,
                data=data
            ) as response:
                res_json = await response.json()

                # 检查并更新Cookie
                if 'set-cookie' in response.headers:
                    new_cookies = {}
                    for cookie in response.headers.getall('set-cookie', []):
                        if '=' in cookie:
                            name, value = cookie.split(';')[0].split('=', 1)
                            new_cookies[name.strip()] = value.strip()

                    # 更新cookies
                    if new_cookies:
                        self.cookies.update(new_cookies)
                        # 生成新的cookie字符串
                        self.cookies_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
                        # 更新数据库中的Cookie
                        await self._update_config_cookies()
                        logger.debug("已更新Cookie到数据库")

                logger.info(f"【{self.cookie_id}】自动确认发货响应: {res_json}")

                # 检查响应结果
                ret_list = res_json.get('ret') or []
                ret_text = (' | '.join(str(x) for x in ret_list)
                            if isinstance(ret_list, list) else str(ret_list))
                if ret_list and is_success(str(ret_list[0])):
                    logger.info(f"【{self.cookie_id}】✅ 自动确认发货成功，订单ID: {order_id}")
                    return {"success": True, "order_id": order_id, "ret": ret_text}

                error_msg = ret_text or '未知错误'
                policy = retry_policy(error_msg)
                logger.warning(f"【{self.cookie_id}】❌ 自动确认发货失败: {error_msg}"
                               f"（处置：{policy_text(policy)}）")
                # 拆解 9：原来的错误码在这里就被丢掉了 —— 盲目重试三次之后，
                # 上层只能拿到"重试次数过多"，再想按错误类型决定策略也无从谈起。
                # 现在：原始 ret 原样带回；stop / refresh 两类当场返回，不再烧重试次数
                # （订单已发货还反复点确认，等于持续骚扰平台的状态机）。
                if policy in ('stop', 'refresh'):
                    return {"success": False, "error": error_msg, "ret": ret_text,
                            "policy": policy, "order_id": order_id}
                if retry_count < 2:
                    return await self.auto_confirm(order_id, item_id, retry_count + 1)
                return {"success": False, "error": error_msg, "ret": ret_text,
                        "policy": policy, "order_id": order_id}


        except Exception as e:
            logger.error(f"【{self.cookie_id}】自动确认发货API请求异常: {self._safe_str(e)}")
            await asyncio.sleep(0.5)

            # 网络异常也进行重试
            if retry_count < 2:
                logger.info(f"【{self.cookie_id}】网络异常，准备重试...")
                return await self.auto_confirm(order_id, item_id, retry_count + 1)

            return {"error": f"网络异常: {self._safe_str(e)}", "order_id": order_id}
