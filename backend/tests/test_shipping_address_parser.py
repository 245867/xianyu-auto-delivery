import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shipping_address_parser import clean_name, extract_phone, parse_shipping_address


def test_extract_phone_various_formats():
    assert extract_phone("13800138000") == "13800138000"
    assert extract_phone("+86 139-1234-5678") == "13912345678"
    assert extract_phone("86-159 0000 1111") == "15900001111"
    assert extract_phone("(186) 6666-8888") == "18666668888"
    assert extract_phone("无手机号") == ""


def test_clean_name():
    assert clean_name(" 张三(请发中通) ") == "张三请发中通"
    assert clean_name("✨李四老板✨") == "李四老板"
    assert clean_name("") == ""


def test_parse_standard_province_address():
    res = parse_shipping_address(
        raw_name="王小明",
        raw_phone="13812345678",
        raw_address="广东省深圳市南山区高新南道科技园A栋501",
    )
    assert res["valid"] is True
    assert res["name"] == "王小明"
    assert res["phone"] == "13812345678"
    assert res["province"] == "广东省"
    assert res["city"] == "深圳市"
    assert res["district"] == "南山区"
    assert "高新南道科技园A栋501" in res["street"]


def test_parse_municipality_address():
    res = parse_shipping_address(
        raw_name="陈女士",
        raw_phone="+86 136-9999-8888",
        raw_address="北京市朝阳区望京SOHO塔1-2003 麻烦发顺丰急用谢谢",
    )
    assert res["valid"] is True
    assert res["province"] == "北京市"
    assert res["city"] == "北京市"
    assert res["district"] == "朝阳区"
    assert "望京SOHO" in res["street"]
    # 验证末尾口头备注被剔除
    assert "麻烦发顺丰急用谢谢" not in res["street"]


def test_parse_sub_city_auto_mapping():
    # 买家只写“义乌市...”，未提供浙江省和金华市
    res = parse_shipping_address(
        raw_name="刘老板",
        raw_phone="15000009999",
        raw_address="义乌市稠江街道兴业路88号3楼",
    )
    assert res["valid"] is True
    assert res["province"] == "浙江省"
    assert res["city"] == "金华市"
    assert res["district"] == "义乌市"
    assert "稠江街道兴业路88号3楼" in res["street"]


def test_invalid_address_rejection():
    # 缺少手机号
    res = parse_shipping_address(raw_name="张三", raw_phone="", raw_address="北京市朝阳区1号")
    assert res["valid"] is False
    assert "缺少有效手机号" in res["reason"]

    # 街道地址过短
    res2 = parse_shipping_address(raw_name="李四", raw_phone="13800138000", raw_address="1")
    assert res2["valid"] is False
    assert "过短" in res2["reason"]
