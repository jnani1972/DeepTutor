from pathlib import Path

import pytest

from deeptutor.video_import import kb


class _Manager:
    def __init__(self, existing):
        self.base_dir = "/kbs"
        self._existing = existing

    def list_knowledge_bases(self):
        return self._existing


@pytest.mark.asyncio
async def test_creates_kb_when_missing(monkeypatch):
    calls = []

    async def init(name, files, base_dir):
        calls.append(("init", name, files, base_dir))

    monkeypatch.setattr(kb, "current_kb_manager", lambda: _Manager([]))
    monkeypatch.setattr(kb, "initialize_knowledge_base", init)
    await kb.add_notes_to_kb("new", [Path("a.md")])
    assert calls == [("init", "new", ["a.md"], "/kbs")]


@pytest.mark.asyncio
async def test_adds_to_existing_kb(monkeypatch):
    calls = []

    async def add(name, files, base_dir):
        calls.append(("add", name, files, base_dir))
        return 1

    monkeypatch.setattr(kb, "current_kb_manager", lambda: _Manager(["old"]))
    monkeypatch.setattr(kb, "add_documents", add)
    await kb.add_notes_to_kb("old", [Path("a.md")])
    assert calls == [("add", "old", ["a.md"], "/kbs")]
