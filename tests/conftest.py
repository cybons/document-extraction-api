import io

import pytest
from PIL import Image

from document_extraction_api.config import Settings
from document_extraction_api.tasks import Invoice


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        anthropic_api_key="test-only",
        model="test-model",
        data_dir=tmp_path,
        clients={
            "pad": {"key": "a" * 32, "tasks": ["invoice.extract", "delivery.extract"]},
            "python": {"key": "b" * 32, "tasks": ["invoice.extract"]},
        },
    )


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new("RGB", (100, 50), "white").save(output, "PNG")
    return output.getvalue()


def invoice():
    return Invoice(
        document_number="TEST-1",
        issue_date="2026-10-27",
        issuer="Synthetic supplier",
        recipient="Synthetic customer",
        currency="JPY",
        subtotal="100",
        tax="10",
        total="110",
        items=[
            {
                "description": "Sample",
                "quantity": "1",
                "unit": None,
                "unit_price": "100",
                "amount": "100",
            }
        ],
        uncertain_fields=[],
        due_date=None,
    )


class FakeProvider:
    def __init__(self, settings, store):
        self.calls = 0

    async def extract(self, job, images):
        self.calls += 1
        return invoice()

    async def close(self):
        pass
