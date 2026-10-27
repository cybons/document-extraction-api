"""納品書（子会社）。仮の中身。項目とプロンプトは運用前に調整する。"""

from document_extraction_api.fields import Date, Issue, Number, Record, Text

TASK_ID = "delivery.extract"


class Item(Record):
    product_code: Text
    product_name: Text
    quantity: Number
    unit: Text
    unit_price: Number
    amount: Number


class OtherField(Record):
    label: Text
    value: Text


class Output(Record):
    document_number: Text
    issue_date: Date
    issuer_name: Text
    recipient_name: Text
    items: list[Item]
    total_amount: Number
    other_fields: list[OtherField]


def check(data: Output) -> list[Issue]:
    return [
        Issue(f"items[{index}].quantity", "数量がない")
        for index, item in enumerate(data.items)
        if item.quantity is None
    ]
