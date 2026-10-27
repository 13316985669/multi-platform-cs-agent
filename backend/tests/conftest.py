"""pytest 配置：隔离的测试数据库与 Chroma 目录（必须在导入 app 模块前设置环境变量）。"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="cs_agent_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["CHROMA_DIR"] = f"{_tmpdir}/chroma"
os.environ["UPLOAD_DIR"] = f"{_tmpdir}/uploads"
os.environ["LLM_API_KEY"] = ""        # 测试不依赖真实 LLM
os.environ["EMBEDDING_API_KEY"] = ""  # 测试走纯 BM25 降级
os.environ["RERANK_API_KEY"] = ""

import pytest

from app.database import init_db, SessionLocal
from app.models import Customer, Conversation


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    init_db()
    # 测试库没有旧切片，跳过启动时的全量重嵌入
    from pathlib import Path

    from app.rag.ingest import INDEX_VERSION
    chroma = Path(os.environ["CHROMA_DIR"])
    chroma.mkdir(parents=True, exist_ok=True)
    (chroma / "index_version.txt").write_text(INDEX_VERSION, encoding="utf-8")
    yield


@pytest.fixture(autouse=True)
def _default_relevance_judge(monkeypatch):
    """测试默认每段都相关。需要指定判定时再覆盖 judge_relevance。"""

    async def all_relevant(_user_text, contexts, *, timeout=4.0):
        return [1] * len(contexts or [])

    monkeypatch.setattr("app.agent.confidence.judge_relevance", all_relevant)


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def conversation(db):
    import uuid
    uid = uuid.uuid4().hex[:8]
    customer = Customer(platform="mock", platform_user_id=f"test_user_{uid}",
                        nickname="测试用户", tags=[])
    db.add(customer)
    db.flush()
    conv = Conversation(customer_id=customer.id, platform="mock",
                        platform_conversation_id=f"conv_{uid}", mode="ai", status="open")
    db.add(conv)
    db.commit()
    return conv
