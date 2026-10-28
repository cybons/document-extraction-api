"""請求書（子会社）。仮の中身。項目とプロンプトは運用前に調整する。"""

from pydantic import Field

from document_extraction_api.fields import Date, Number, Record, Text

TASK_ID = "invoice.extract"
NAME = "請求書"


class Item(Record):
    description: Text = Field(title="品名・内容")
    quantity: Number = Field(title="数量")
    unit_price: Number = Field(title="単価")
    amount: Number = Field(title="金額")


class OtherField(Record):
    label: Text = Field(title="項目")
    value: Text = Field(title="内容")


class Output(Record):
    invoice_number: Text = Field(title="請求書番号")
    issue_date: Date = Field(title="発行日")
    due_date: Date = Field(title="支払期日")
    issuer_name: Text = Field(title="発行元")
    registration_number: Text = Field(title="登録番号")
    recipient_name: Text = Field(title="宛先")
    items: list[Item] = Field(title="明細")
    subtotal: Number = Field(title="小計")
    tax: Number = Field(title="消費税")
    total_amount: Number = Field(title="合計金額")
    other_fields: list[OtherField] = Field(title="そのほかの記載")
