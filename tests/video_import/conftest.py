import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "video_import"


@pytest.fixture
def fixture_json():
    def make(name: str):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

        async def extract(_url: str) -> dict:
            return data

        return extract

    return make


@pytest.fixture
def png_bytes() -> bytes:
    import io

    from PIL import Image

    image = Image.new("RGB", (64, 36), (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def jpeg_of():
    import io

    from PIL import Image

    def make(value: int) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (64, 36), (value, value, value)).save(buffer, format="JPEG")
        return buffer.getvalue()

    return make


@pytest.fixture(autouse=True)
def _release_import_locks():
    """Drop every worker lock this test process claimed (as a process exit would)."""
    yield
    from deeptutor.video_import import ownership

    for path in list(ownership._held):
        ownership.release(path)
