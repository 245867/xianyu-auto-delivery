# -*- coding: utf-8 -*-
"""收货地址智能反混淆与四级标准化解析引擎。

吸收一尘助手 / 甩手工具箱无货源代拍地址清洗机制：
- 容错提取 11 位国内手机号（清洗 +86、空格、中划线）
- 买家姓名脱敏与异常字符清洗
- 自动补全直辖市与知名二级市（如常熟、义乌、昆山）省市区关系
- 分离详细街道门牌与买家口头备注
"""

import re
from typing import Any, Dict

# 直辖市
MUNICIPALITIES = {"北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市"}

# 常见无省份知名县级市自动映射
SUB_CITY_MAPPING = {
    "常熟": ("江苏省", "苏州市", "常熟市"),
    "昆山": ("江苏省", "苏州市", "昆山市"),
    "张家港": ("江苏省", "苏州市", "张家港市"),
    "太仓": ("江苏省", "苏州市", "太仓市"),
    "江阴": ("江苏省", "无锡市", "江阴市"),
    "宜兴": ("江苏省", "无锡市", "宜兴市"),
    "义乌": ("浙江省", "金华市", "义乌市"),
    "东阳": ("浙江省", "金华市", "东阳市"),
    "诸暨": ("浙江省", "绍兴市", "诸暨市"),
    "慈溪": ("浙江省", "宁波市", "慈溪市"),
    "余姚": ("浙江省", "宁波市", "余姚市"),
    "晋江": ("福建省", "泉州市", "晋江市"),
    "石狮": ("福建省", "泉州市", "石狮市"),
    "南安": ("福建省", "泉州市", "南安市"),
}

PROVINCES = [
    "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省", "江苏省", "浙江省", "安徽省",
    "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省", "广东省", "海南省",
    "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省", "台湾省",
    "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "北京市", "上海市", "天津市", "重庆市", "香港特别行政区", "澳门特别行政区",
]


def extract_phone(raw_text: str) -> str:
    """容错提取 11 位大陆有效手机号。"""
    if not raw_text:
        return ""
    cleaned = re.sub(r"[\s\-\(\)\+]", "", str(raw_text))
    # 去除常见的 +86 或 86 前缀
    if cleaned.startswith("86") and len(cleaned) == 13:
        cleaned = cleaned[2:]
    match = re.search(r"1[3-9]\d{9}", cleaned)
    return match.group(0) if match else ""


def clean_name(raw_name: str) -> str:
    """清洗买家收件人姓名，移除特殊符号与买家头衔。"""
    if not raw_name:
        return ""
    # 过滤 emoji 与标点符号
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5a-zA-Z]", "", str(raw_name))
    cleaned = cleaned.strip()
    return cleaned[:20]


def parse_shipping_address(raw_name: str, raw_phone: str, raw_address: str, raw_city: str = "") -> Dict[str, Any]:
    """解析并标准化闲鱼收货地址。"""
    phone = extract_phone(raw_phone) or extract_phone(raw_address)
    name = clean_name(raw_name)
    addr = str(raw_address or "").strip()

    # 如果地址中包含了电话号码，将电话号码剔除避免污染街道门牌
    if phone:
        addr = addr.replace(phone, " ").strip()

    # 剔除买家口头备注干扰
    addr = re.sub(r"(请发|麻烦发|发|要|顺丰|圆通|中通|申通|韵达|极兔|快递|尽快|急用|谢谢|收)[\s!！,，。]*$", "", addr, flags=re.I).strip()

    province, city, district, street = "", "", "", addr

    # 1. 检查知名子市映射 (如买家直接写 义乌市稠江街道...)
    for sub, (p_map, c_map, d_map) in SUB_CITY_MAPPING.items():
        if sub in addr or sub in raw_city:
            province, city, district = p_map, c_map, d_map
            # 剥离已匹配的前缀
            pattern = rf"^.*?{sub}(市|县)?"
            street = re.sub(pattern, "", street).strip()
            break

    # 2. 检查直辖市
    if not province:
        for m_short, m_full in MUNICIPALITIES.items():
            if addr.startswith(m_short) or raw_city.startswith(m_short):
                province = m_full
                city = m_full
                addr_no_city = re.sub(rf"^{m_short}(市)?", "", addr).strip()
                # 提取区/县
                m_dist = re.match(r"^([^市区县]+(区|县))", addr_no_city)
                if m_dist:
                    district = m_dist.group(1)
                    street = addr_no_city[len(district):].strip()
                else:
                    street = addr_no_city
                break

    # 3. 检查常规省份
    if not province:
        for p in PROVINCES:
            short_p = p.replace("省", "").replace("自治区", "").replace("特别行政区", "")
            if addr.startswith(short_p) or addr.startswith(p):
                province = p
                rem = re.sub(rf"^{p}|^{short_p}", "", addr).strip()
                # 提取市
                m_city = re.match(r"^([^省市区]+(市|自治州|地区|盟))", rem)
                if m_city:
                    city = m_city.group(1)
                    rem_city = rem[len(city):].strip()
                    # 提取区/县
                    m_dist = re.match(r"^([^市区县]+(区|县|市))", rem_city)
                    if m_dist:
                        district = m_dist.group(1)
                        street = rem_city[len(district):].strip()
                    else:
                        street = rem_city
                else:
                    street = rem
                break

    # 如果 raw_city 提供了辅助信息且当前 city 为空
    if not city and raw_city:
        city = raw_city.strip()

    is_valid = bool(name and phone and street and len(street) >= 3)

    return {
        "valid": is_valid,
        "name": name,
        "phone": phone,
        "province": province,
        "city": city,
        "district": district,
        "street": street,
        "full_address": f"{province}{city if city != province else ''}{district}{street}".strip(),
        "reason": "OK" if is_valid else ("缺少收件人" if not name else ("缺少有效手机号" if not phone else "街道地址过短")),
    }
