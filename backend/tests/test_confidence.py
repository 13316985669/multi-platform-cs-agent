"""Faithfulness 与 Context Precision。分数不看精排。"""
from app.agent import confidence


def test_context_precision_penalizes_irrelevant_ranked_first():
    score = confidence.context_precision([0, 1, 1])
    assert abs(score - ((0.5 + 2 / 3) / 2)) < 1e-9
    assert score < confidence.PASS_SCORE


def test_context_precision_is_one_when_every_chunk_is_relevant():
    assert confidence.context_precision([1, 1, 1]) == 1.0


def test_context_precision_is_zero_without_relevant_chunks():
    assert confidence.context_precision([]) == 0.0
    assert confidence.context_precision([0, 0]) == 0.0


def test_account_opening_claims_are_not_faithful_to_registration_section():
    registration = (
        "二、香港公司注册资料收集确认\n"
        "1、公司名称\n2、注册资本\n3、经营范围\n4、注册地址\n"
        "①证件照片（护照人像页，身份证正反面）"
    )
    draft = [
        "1、香港公司全套注册资料（CR/BR/NNC1 / 公司章程）",
        "5、开户调查问卷（我司提供模板，需您如实填写签字）",
        "1、公司名称",
    ]
    score = confidence.faithfulness(draft, [{"content": registration}])
    assert score < confidence.PASS_SCORE
    kept = "\n".join(confidence.supported_messages(draft, [{"content": registration}]))
    assert "开户调查问卷" not in kept
    assert "公司章程" not in kept
    assert "公司名称" in kept


def test_similar_question_and_short_overlap_are_not_faithful():
    contexts = [{
        "content": (
            "问：如何获取资料、流程及费用？\n"
            "相似：香港公司如何注册？\n"
            "答：您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"
        ),
    }]
    invented = ["香港公司注册的具体步骤如下："]
    assert confidence.faithfulness(invented, contexts) < confidence.PASS_SCORE
    kept = confidence.supported_messages(invented, contexts)
    assert kept == []

    short = ["可以很快"]
    blob = [{"content": "答：可以不用。"}]
    assert confidence.faithfulness(short, blob) < confidence.PASS_SCORE


def test_verbatim_section_is_fully_faithful():
    section = "1、公司名称\n中文名：\n2、注册资本\n①证件照片"
    score = confidence.faithfulness([section], [{"content": section}])
    assert score == 1.0


def test_risk_tiers_follow_existing_rules():
    assert confidence.risk_tier("行，香港公司我要注册，怎么弄") == "high"
    assert confidence.risk_tier("开户要准备什么资料") == "high"
    assert confidence.risk_tier("做不做香港开户") == "medium"
    assert confidence.risk_tier("哈哈") == "low"
    assert confidence.risk_tier("我叫小明") == "low"
    assert confidence.risk_tier("你们国庆放假吗") == "low"
    assert confidence.risk_tier("我今天要去香港") == "low"
    assert confidence.risk_tier("火星移民政策") == "high"


def test_scope_reply_does_not_invent_fees():
    reply = confidence.scope_reply("做不做香港开户")
    assert reply is not None
    assert "开户" in reply
    assert "费用" not in reply
    assert "3800" not in reply
    assert confidence.scope_reply("做不做火星公司") is None


def test_two_topics_are_ambiguous_until_the_user_names_both():
    contexts = [
        {"content": "问：香港开户资料\n答：1、开户问卷", "source": "香港开户资料"},
        {"content": "二、香港公司注册资料收集确认\n1、公司名称", "source": "注册.md"},
    ]
    assert confidence.diagnose("行，香港公司我要注册，怎么弄", contexts, [1, 1]) == "ambiguous"
    assert confidence.diagnose("行，香港公司我要注册，怎么弄", contexts, [0, 1]) == "noise"
    assert "开户" in confidence.clarify_line(confidence.ambiguous_keys(
        "行，香港公司我要注册，怎么弄", contexts, [1, 1],
    ))
