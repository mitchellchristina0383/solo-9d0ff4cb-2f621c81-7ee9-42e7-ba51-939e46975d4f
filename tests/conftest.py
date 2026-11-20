"""pytest 公共夹具：让每个测试用上独立临时状态文件的 JobStore。

生产环境 app.main 从 app.state 导入单例 store；这里把它替换成
指向 tmp_path 的实例，保证测试绝不触碰工作目录下的真实状态文件，
且每个用例之间互不污染。
"""
from __future__ import annotations

import pytest

from app import main as main_module
from app.state import JobStore


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    store = JobStore(state_file=tmp_path / "tape_state.json")
    monkeypatch.setattr(main_module, "store", store)
    yield store
