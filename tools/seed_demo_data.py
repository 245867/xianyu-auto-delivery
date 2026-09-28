# -*- coding: utf-8 -*-
"""
为 README 截图生成演示数据。

写入独立的演示库（默认 data/demo_shot.db），绝不触碰真实的 data/xianyu_data.db。
所有数据均为虚构：账号、商品、卡密、订单、买家全部是编造的示例内容。

用法：
    python tools/seed_demo_data.py [演示库路径]
"""
import os
import sys
import io
import json
import shutil
import sqlite3
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DB = os.path.join(ROOT, "data", "xianyu_data.db")
DST_DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "demo_shot.db")

COOKIE_A = "demo_account_main"
COOKIE_B = "demo_account_sub"
COOKIE_C = "demo_account_idle"
NOW = datetime.now()


def ts(days_ago=0, hours=0):
    return (NOW - timedelta(days=days_ago, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _local_hash(password: str, iterations: int = 310000) -> str:
    """按后端相同的 PBKDF2-SHA256 方案生成密码哈希（仅用于本地演示库登录）。"""
    import hashlib
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def main():
    if os.path.exists(DST_DB):
        os.remove(DST_DB)

    # 用真实库的“表结构”做模板，但清空所有数据，避免任何真实内容泄露到演示库
    shutil.copyfile(SRC_DB, DST_DB)
    c = sqlite3.connect(DST_DB)
    c.row_factory = sqlite3.Row

    tables = [r[0] for r in c.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    for t in tables:
        c.execute(f'DELETE FROM "{t}"')
    c.commit()
    print(f"[1/6] 已复制表结构并清空 {len(tables)} 张表 -> {DST_DB}")

    # ---------- 管理员账号（沿用真实库的账号名，密码哈希按本地配置重算，便于截图登录） ----------
    src = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row
    u = src.execute("select * from users limit 1").fetchone()
    admin_pw = ""
    try:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(ROOT, "global_config.yml"), encoding="utf-8"))
        admin_pw = str(cfg.get("AUTH", {}).get("admin_password", ""))
    except Exception as exc:
        print(f"      读取 global_config.yml 失败（将沿用原哈希）: {exc}")

    if u:
        cols = [d[0] for d in u.keys()] and list(u.keys())
        vals = [u[k] for k in cols]
        if admin_pw:
            vals[cols.index("password_hash")] = _local_hash(admin_pw)
        c.execute(f'INSERT INTO users ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                  vals)
    src.close()

    # ---------- 账号（3 个：2 个在线、1 个离线；Cookie 全部是假串） ----------
    accounts = [
        (COOKIE_A, "demo_cookie_value_main_do_not_use", 1, "主号·数码专营", 1, 10, "demo_main", "", 0, ts(30)),
        (COOKIE_B, "demo_cookie_value_sub_do_not_use", 1, "副号·虚拟充值", 1, 10, "demo_sub", "", 0, ts(22)),
        (COOKIE_C, "demo_cookie_value_idle_do_not_use", 1, "备用号·测试", 0, 10, "demo_idle", "", 0, ts(9)),
    ]
    c.executemany(
        "INSERT INTO cookies (id,value,user_id,remark,auto_confirm,pause_duration,username,password,"
        "show_browser,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", accounts)
    print(f"[2/6] 写入 {len(accounts)} 个演示账号")

    # ---------- 卡密分组 ----------
    cards = [
        # name, type, api_config, text_content, data_content, image_url, description,
        # enabled, delay, is_multi_spec, spec_name, spec_value, after_enabled, after_msg
        ("视频会员月卡", "data", None, None,
         "\n".join(f"VIP-MONTH-{i:04d}-{chr(65 + i % 26)}{i * 7 % 97:02d}" for i in range(1, 13)),
         None, "视频平台会员 30 天兑换码，虚拟商品", 1, 3, 0, None, None, 1,
         "已发货，请及时兑换，兑换后不支持退款~"),
        ("网盘超级会员年卡", "data", None, None,
         "\n".join(f"PAN-YEAR-{i:04d}-X{i * 13 % 89:02d}" for i in range(1, 9)),
         None, "网盘超级会员 12 个月激活码", 1, 5, 0, None, None, 1,
         "激活码已发出，登录后在个人中心兑换即可。"),
        ("自动发货说明", "text", None,
         "感谢购买！本商品为虚拟商品，付款后系统自动发货。\n如未收到，请回复「人工」联系客服。",
         None, None, "固定文本，用于发货前说明", 1, 1, 0, None, None, 0, None),
        ("支付二维码", "image", None, None, None,
         "/static/uploads/images/1.png",
         "图片类型发货：发送收款/引导二维码", 1, 2, 0, None, None, 0, None),
        ("游戏点卡实时取卡", "api", json.dumps(
            {"url": "https://api.example.com/card/get", "method": "GET",
             "params": {"order_id": "{order_id}", "item_id": "{item_id}"},
             "headers": {"Authorization": "Bearer demo-token"}, "timeout": 10},
            ensure_ascii=False), None, None, None,
            "API 类型：发货瞬间向上游实时请求卡密", 1, 0, 0, None, None, 1,
            "点卡已到账，请到游戏内查收。"),
        ("多规格·季卡", "data", None, None,
         "\n".join(f"VIP-QUARTER-{i:04d}" for i in range(1, 7)),
         None, "按规格发货示例（季卡）", 1, 3, 1, "时长", "季卡", 0, None),
    ]
    for i, row in enumerate(cards, 1):
        c.execute(
            "INSERT INTO cards (name,type,api_config,text_content,data_content,image_url,description,"
            "enabled,delay_seconds,is_multi_spec,spec_name,spec_value,user_id,created_at,updated_at,"
            "after_delivery_enabled,after_delivery_message)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
            (*row[:12], ts(20 - i), ts(3), row[12], row[13]))
    print(f"[3/6] 写入 {len(cards)} 个卡密分组")

    # ---------- 商品（含封面、开关、绑定卡密） ----------
    items = [
        ("1076481540371", "视频平台会员 1 个月 官方直充 秒到账", "18.80", 1, 1, 1, 1, 1),
        ("1076481540388", "网盘超级会员 12 个月 支持多设备", "158.00", 2, 1, 1, 1, 1),
        ("1076481540402", "游戏点卡 100 元面值 自动发货", "96.50", 5, 1, 0, 1, 1),
        ("1076481540417", "视频平台会员 3 个月 季卡 优惠装", "49.90", 6, 1, 1, 0, 1),
        ("1076481540425", "音乐会员 月卡 学生认证可用", "12.00", 1, 1, 1, 1, 0),
        ("1076481540433", "办公软件年度订阅 正版激活", "88.00", 3, 0, 0, 0, 0),
    ]
    for i, (iid, title, price, card_id, multi_qty, ai_on, auto_on, black_on) in enumerate(items, 1):
        c.execute(
            "INSERT INTO item_info (cookie_id,item_id,item_title,item_description,item_category,"
            "item_price,item_detail,is_multi_spec,created_at,updated_at,multi_quantity_delivery,"
            "refund_blacklist_enabled,item_image,ai_reply_enabled,auto_delivery_enabled,delivery_card_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (COOKIE_A if i <= 4 else COOKIE_B, iid, title,
             f"虚拟商品自动发货，{title}。下单后请留意闲鱼消息，系统会自动发送卡密。",
             "50023914", price, json.dumps({"brand": "demo", "delivery": "auto"}, ensure_ascii=False),
             1 if i == 4 else 0, ts(15 - i), ts(2), multi_qty, black_on,
             f"/static/uploads/images/{(i % 3) + 1}.png", ai_on, auto_on, card_id))
    print(f"[4/6] 写入 {len(items)} 个商品")

    # ---------- 订单（近 14 天，覆盖全部状态，用于营收趋势与状态筛选） ----------
    statuses = ["processing", "pending_ship", "shipped", "completed", "completed",
                "completed", "refunding", "cancelled"]
    cities = [("张伟", "138****6621", "浙江省 杭州市 西湖区"), ("李娜", "139****3388", "广东省 深圳市 南山区"),
              ("王芳", "137****9910", "江苏省 南京市 鼓楼区"), ("刘强", "135****2244", "四川省 成都市 武侯区"),
              ("陈静", "186****7755", "北京市 朝阳区"), ("杨帆", "188****1166", "上海市 浦东新区"),
              ("赵敏", "159****4433", "湖北省 武汉市 洪山区"), ("周涛", "176****8899", "福建省 厦门市 思明区")]
    n_orders = 0
    for d in range(14):
        for k in range(3):
            idx = (d * 3 + k) % len(items)
            iid, title, price, *_ = items[idx]
            st = statuses[(d + k) % len(statuses)]
            name, phone, addr = cities[(d + k) % len(cities)]
            qty = 2 if (d + k) % 5 == 0 else 1
            amount = f"{float(price) * qty:.2f}"
            c.execute(
                "INSERT INTO orders (order_id,item_id,buyer_id,spec_name,spec_value,quantity,amount,"
                "order_status,cookie_id,is_bargain,created_at,updated_at,receiver_name,receiver_phone,"
                "receiver_address,version,chat_id,system_shipped,receiver_city)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"20{2500000000000 + d * 1000 + k}", iid,
                 f"buyer_{2200 + d * 3 + k}", None, None, str(qty), amount, st,
                 COOKIE_A if idx < 4 else COOKIE_B, 1 if (d + k) % 4 == 0 else 0,
                 ts(d, k * 3), ts(d, k * 3), name, phone, addr, 1,
                 f"chat_{d}_{k}", 1 if st in ("shipped", "completed") else 0,
                 addr.split()[1]))
            n_orders += 1
    print(f"[5/6] 写入 {n_orders} 条订单")

    # ---------- 关键词 / 默认回复 / 发货规则 / 黑名单 / AI 设置 ----------
    c.executemany("INSERT INTO keywords (cookie_id,keyword,reply,item_id,type,image_url) VALUES (?,?,?,?,?,?)", [
        (COOKIE_A, "在吗", "在的，亲~ 商品都是自动发货，付款后系统会立刻发卡密给您。", None, "text", None),
        (COOKIE_A, "发货", "已为您安排自动发货，请稍等几秒查收闲鱼消息。", None, "text", None),
        (COOKIE_A, "便宜", "亲，本店已是全网低价啦，多买可以给您申请优惠哦~", None, "text", None),
        (COOKIE_A, "怎么用", "卡密使用方法：打开对应 App → 我的 → 兑换码 → 粘贴卡密即可。", None, "text", None),
        (COOKIE_B, "充值", "充值类商品发货较快，付款后 1 分钟内到账。", None, "text", None),
        (COOKIE_B, "发票", "本店为虚拟商品，暂不提供发票，请知悉。", None, "text", None),
    ])
    c.executemany(
        "INSERT INTO default_replies (cookie_id,enabled,reply_content,reply_image_url,reply_once,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?)", [
        (COOKIE_A, 1, "您好，本店商品均为自动发货，付款后系统会自动发送卡密。若未收到请回复「人工」。",
         None, 0, ts(25), ts(4)),
        (COOKIE_B, 1, "亲，消息已收到~ 客服在线时间 9:00-22:00，虚拟商品下单后一般秒发。",
         None, 0, ts(18), ts(3)),
    ])
    c.executemany(
        "INSERT INTO delivery_rules (keyword,card_id,delivery_count,enabled,description,delivery_times,"
        "created_at,updated_at,user_id) VALUES (?,?,?,?,?,?,?,?,1)", [
        ("会员 月卡", 1, 1, 1, "标题命中「会员 月卡」时发放视频会员月卡", 1, ts(20), ts(3)),
        ("网盘 年卡", 2, 1, 1, "标题命中「网盘 年卡」时发放网盘年卡", 1, ts(20), ts(3)),
        ("点卡", 5, 1, 1, "标题命中「点卡」时调用 API 实时取卡", 1, ts(12), ts(2)),
        ("季卡", 6, 1, 1, "多规格规则：按规格「季卡」发货", 1, ts(8), ts(1)),
    ])
    c.executemany(
        "INSERT INTO refund_blacklist (buyer_id,cookie_id,first_order_id,first_item_id,last_order_id,"
        "last_item_id,reason,refund_count,created_at,updated_at,is_whitelisted,enabled,note,source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (f"buyer_{2200 + i}", COOKIE_A, f"2025000{i}00", "1076481540371", f"2025000{i}05",
         "1076481540371", reason, cnt, ts(12 - i), ts(2), wl, 1, note, "auto")
        for i, (reason, cnt, wl, note) in enumerate([
            ("付款后申请仅退款，卡密已发出未追回", 2, 0, "多次恶意退款，已加入黑名单"),
            ("以「未收到货」为由退款但卡密已兑换", 1, 0, "凭证已留档"),
            ("退款后再次下单同商品，触发风控", 3, 0, "累计 3 次退款"),
            ("误操作退款，已与卖家沟通", 1, 1, "已加入永久白名单"),
        ])])
    c.executemany(
        "INSERT INTO ai_reply_settings (cookie_id,ai_enabled,model_name,api_key,base_url,"
        "max_discount_percent,max_discount_amount,max_bargain_rounds,custom_prompts,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (COOKIE_A, 1, "gpt-4o-mini", "", "https://api.openai.com/v1", 10, 5, 3,
         "你是闲鱼店铺客服，语气亲切简洁，遇到议价先说明已是低价。", ts(20), ts(2)),
        (COOKIE_B, 1, "gpt-4o-mini", "", "https://api.openai.com/v1", 5, 3, 2,
         "虚拟充值类目，不承诺到账时间以外的内容。", ts(15), ts(2)),
    ])
    c.executemany(
        "INSERT INTO notification_channels (name,user_id,type,config,enabled,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?)", [
        ("店铺邮件通知", 1, "email",
         json.dumps({"smtp_host": "smtp.example.com", "smtp_port": 465,
                     "username": "shop@example.com", "to": "owner@example.com"},
                    ensure_ascii=False), 1, ts(20), ts(2)),
        ("钉钉机器人", 1, "dingtalk",
         json.dumps({"webhook": "https://oapi.dingtalk.com/robot/send?access_token=demo"}, ensure_ascii=False),
         1, ts(18), ts(2)),
        ("飞书机器人", 1, "feishu",
         json.dumps({"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/demo"}, ensure_ascii=False),
         0, ts(10), ts(2)),
    ])
    print("[6/6] 写入关键词 6 条、默认回复 2 条、发货规则 4 条、黑名单 4 条、AI 设置 2 条、通知渠道 3 个")

    # ---------- 账号 × 通知渠道 的启用绑定 ----------
    c.executemany(
        "INSERT INTO message_notifications (cookie_id,channel_id,enabled,created_at,updated_at)"
        " VALUES (?,?,?,?,?)", [
        (COOKIE_A, 1, 1, ts(20), ts(2)),
        (COOKIE_A, 2, 1, ts(18), ts(2)),
        (COOKIE_A, 3, 0, ts(10), ts(2)),
        (COOKIE_B, 1, 1, ts(15), ts(2)),
    ])

    # ---------- 风控日志（图表/列表有内容） ----------
    events = [
        ("auto_delivery", "订单自动发货成功", "已发送卡密", "success", None),
        ("blacklist_hit", "命中退款黑名单，跳过自动发货", "订单已拦截", "success", None),
        ("cookie_refresh", "Token 刷新成功", "账号状态正常", "success", None),
        ("slider_verify", "滑块验证通过", "继续监听消息", "success", None),
        ("auto_delivery", "卡密分组库存不足", "已推送补货通知", "warning", "库存剩余 0"),
        ("message_send", "发送卡密超时，已重试", "第 2 次重试成功", "success", None),
        ("item_sync", "商品同步完成", "新增 2 个商品", "success", None),
        ("auto_delivery", "API 取卡失败", "已跳过本次发货", "failed", "upstream timeout"),
    ]
    rows = []
    for d in range(14):
        for k in range(6):
            et, desc, res, st, err = events[(d * 6 + k) % len(events)]
            rows.append((COOKIE_A if (d + k) % 3 else COOKIE_B, et, desc, res, st, err, ts(d, k)))
    c.executemany(
        "INSERT INTO risk_control_logs (cookie_id,event_type,event_description,processing_result,"
        "processing_status,error_message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        [(*r[:6], r[6], r[6]) for r in rows])
    print(f"      写入 {len(rows)} 条风控日志")

    c.commit()

    # ---------- 校验 ----------
    print("\n=== 演示库内容校验 ===")
    for t in ["users", "cookies", "cards", "item_info", "orders", "keywords",
              "default_replies", "delivery_rules", "refund_blacklist",
              "ai_reply_settings", "notification_channels", "risk_control_logs"]:
        n = c.execute(f'select count(*) from "{t}"').fetchone()[0]
        print(f"  {t:26} {n:5}")

    # 确认没有把真实 cookie 带进来
    real = sqlite3.connect(SRC_DB).execute("select value from cookies limit 1").fetchone()
    if real and real[0]:
        leaked = c.execute("select count(*) from cookies where value = ?", (real[0],)).fetchone()[0]
        print(f"\n真实 Cookie 泄露检查: {'!!! 泄露 !!!' if leaked else '通过（0 条）'}")
    c.close()
    print(f"\n演示库已生成: {DST_DB}")


if __name__ == "__main__":
    main()
