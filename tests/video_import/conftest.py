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
