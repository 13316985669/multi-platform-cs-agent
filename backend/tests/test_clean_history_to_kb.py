"""历史对话清洗：可发送答案、留资收口、复核文件。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.clean_history_to_kb import OUTPUT_FILES, clean_dialogues, sample_rows, self_check, write_outputs
from scripts.cluster_knowledge import build_replacements, classify_faq


def test_self_check_covers_sendable_paths():
    self_check()


def test_opening_menu_is_dropped_and_similar_scripts_merge():
    rows = [
        {"id": 1, "title": "能介绍一下咱们的服务吗？", "category": "注册", "question": "能介绍一下咱们的服务吗？",
         "answer": "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"},
        {"id": 2, "title": "能介绍一下咱们的服务吗", "category": "注册", "question": "能介绍一下咱们的服务吗",
         "answer": "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"},
        {"id": 3, "title": "留电话", "category": "留资话术", "question": "注册",
         "answer": "您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"},
        {"id": 4, "title": "活动", "category": "营销话术", "question": "注册",
         "answer": "现在是我们10周年庆典3980的活动价(含3900的官费)，包含秘书服务，秘书地址，绿盒子全套"},
    ]
    assert classify_faq(rows[0]["question"], rows[0]["answer"]) is None
    created, disabled = build_replacements(rows)
    assert set(disabled) == {1, 2, 3, 4}
    titles = {item["title"] for item in created}
    assert "能介绍一下咱们的服务吗？" not in titles
    assert "怎么留电话" in titles
    assert "现在有什么活动价" in titles
    assert any("3980" in item["content"] and item["category"] == "营销话术" for item in created)


def test_write_outputs_creates_eight_files(tmp_path):
    write_outputs(clean_dialogues(sample_rows()), tmp_path)
    for name in OUTPUT_FILES:
        assert (tmp_path / name).exists(), name
