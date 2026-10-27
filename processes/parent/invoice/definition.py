"""請求書（親会社）。仮の中身。項目とプロンプトは運用前に調整する。"""

from document_extraction_api.fields import Date, Number, Record, Text

TASK_ID = "invoice.extract"


class Item(Record):
    description: Text
    quantity: Number
    unit_price: Number
    amount: Number


class OtherField(Record):
    label: Text
    value: Text


class Output(Record):
    invoice_number: Text
    issue_date: Date
    due_date: Date
    issuer_name: Text
    registration_number: Text
    recipient_name: Text
    items: list[Item]
    subtotal: Number
    tax: Number
    total_amount: Number
    other_fields: list[OtherField]
