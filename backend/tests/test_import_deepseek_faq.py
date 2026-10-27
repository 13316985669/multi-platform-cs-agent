"""整理版 FAQ 文本的解析。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.import_deepseek_faq import records_from_text

SAMPLE = """
二、注册香港公司
Q28：顾客：注册香港公司需要什么资料/条件/材料
A：您好，注册香港公司主要涉及：
   1. 公司名称；
   2. 地址（我司提供）；
   3. 经营范围；
   4. 注册资金；
   5. 股东董事相关身份信息。

Q29：顾客：内地人/大陆身份证可以注册吗/需要本人去香港吗
A：可以的，大陆身份证可以注册。注册公司不需要本人亲自去香港。

Q18：顾客：我要注册香港公司/注册香港公司多少钱
A：现在是我们10周年庆典3980的活动价。

一、通用开场
Q3：顾客：能介绍一下咱们的服务吗？
A：您好老板，这边可以提供注册、开户、年审、地址挂靠等服务，您是想咨询什么呢？

Q4：顾客：最近有什么优惠？/活动价多少？
A：现在是我们10周年庆典3980的活动价，含3900的官费。

三、银行开户
Q58：顾客：可以开汇丰/渣打/华侨吗
A：我们这边能直接对接汇丰、渣打。

十一、其他
Q129：顾客：香港公司可以开BVI/开曼吗
A：未明确，留资专员。

八、合作、推广、无效、风险、骚扰
Q90：顾客：骂人/乱码/刷屏
A：忽略。

九、价格、活动、优惠
Q93：顾客：最近有什么优惠？/活动价多少？
A：重复的活动价，不应再入库。
"""


def test_materials_promo_and_opening_are_split():
    records = records_from_text(SAMPLE)
    by_title = {item["title"]: item for item in records}
    materials = next(item for item in records if "注册香港公司需要什么资料" in item["title"])
    assert materials["category"] == "资料"
    assert "1. 公司名称" in materials["answer"]
    assert not materials["answer"].startswith("A")
    assert "股东董事" in materials["answer"]
    assert "条件" not in materials["similar_questions"]
    assert "注册香港公司需要什么条件" in materials["similar_questions"]
    assert all("能介绍一下咱们的服务吗" not in item["title"] for item in records)
    promo = by_title["最近有什么优惠"]
    assert promo["category"] == "营销话术"
    assert "3980" in promo["answer"]
    assert "活动价多少" in promo["similar_questions"]
    assert "重复的活动价" not in promo["answer"]
    assert all(item["title"] != "骂人" for item in records)

    mainland = next(item for item in records if "内地人" in item["title"])
    assert mainland["title"].startswith("内地人/大陆身份证可以注册吗")
    assert "大陆身份证可以注册吗" in mainland["similar_questions"]
    assert "内地人可以注册吗" in mainland["similar_questions"]
    assert "需要本人去香港吗" in mainland["similar_questions"]
    assert all(item["title"] != "内地人" for item in records)

    bank = next(item for item in records if "可以开汇丰" in item["title"])
    assert bank["category"] == "开户"
    assert "可以开渣打吗" in bank["similar_questions"]
    assert "渣打" not in bank["similar_questions"]
    offshore = next(item for item in records if "开BVI" in item["title"])
    assert offshore["category"] == "注册"
    assert "香港公司可以开曼吗" in offshore["similar_questions"]
    assert "香港公司可以开开曼吗" not in offshore["similar_questions"]

    register = next(item for item in records if "我要注册香港公司" in item["title"])
    assert register["category"] == "注册"
