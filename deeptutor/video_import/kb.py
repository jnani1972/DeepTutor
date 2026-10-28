"""Knowledge-base adapter: reuses the router's create/add+index functions."""

from __future__ import annotations

from pathlib import Path

from deeptutor.knowledge.add_documents import add_documents
from deeptutor.knowledge.initializer import initialize_knowledge_base
from deeptutor.multi_user.knowledge_access import current_kb_manager


async def add_notes_to_kb(kb_name: str, notes_files: list[Path]) -> None:
    manager = current_kb_manager()
    base_dir = str(manager.base_dir)
    files = [str(path) for path in notes_files]
    if kb_name in manager.list_knowledge_bases():
        await add_documents(kb_name, files, base_dir=base_dir)
    else:
        await initialize_knowledge_base(kb_name, files, base_dir=base_dir)
