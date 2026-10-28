"""納品書（親会社）。仮の中身。項目とプロンプトは運用前に調整する。"""

from pydantic import Field

from document_extraction_api.fields import Date, Issue, Number, Record, Text

TASK_ID = "delivery.extract"
NAME = "納品書"


class Item(Record):
    product_code: Text = Field(title="品番")
    product_name: Text = Field(title="品名")
    quantity: Number = Field(title="数量")
    unit: Text = Field(title="単位")
    unit_price: Number = Field(title="単価")
    amount: Number = Field(title="金額")


class OtherField(Record):
    label: Text = Field(title="項目")
    value: Text = Field(title="内容")


class Output(Record):
    document_number: Text = Field(title="伝票番号")
    issue_date: Date = Field(title="発行日")
    issuer_name: Text = Field(title="発行元")
    recipient_name: Text = Field(title="宛先")
    items: list[Item] = Field(title="明細")
    total_amount: Number = Field(title="合計金額")
    other_fields: list[OtherField] = Field(title="そのほかの記載")


def check(data: Output) -> list[Issue]:
    return [
        Issue(f"items[{index}].quantity", "数量がない")
        for index, item in enumerate(data.items)
        if item.quantity is None
    ]
