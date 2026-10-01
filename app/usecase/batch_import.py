import csv
import io
import re

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store import batches

HEADER = ["tenant", "order_id", "amount_cents", "currency"]
_AMOUNT_RE = re.compile(r"[0-9]+")


class CsvFormatError(Exception):
    """CSV 结构层面非法：缺表头、列数不符、编码非法、行结构损坏，需整批拒绝。"""


def parse_rows(raw: bytes) -> list[tuple[int, list[str]]]:
    """严格解析 CSV，返回(物理行号, 四列字段)数据行列表；任何结构问题抛 CsvFormatError。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CsvFormatError("csv must be valid UTF-8 text") from error
    reader = csv.reader(io.StringIO(text), strict=True)
    rows: list[tuple[int, list[str]]] = []
    try:
        header = next(reader, None)
        if header is None or [field.strip() for field in header] != HEADER:
            raise CsvFormatError("csv header must be exactly: tenant,order_id,amount_cents,currency")
        for row in reader:
            line_no = reader.line_num
            if len(row) != len(HEADER):
                raise CsvFormatError(f"line {line_no}: expected {len(HEADER)} columns, got {len(row)}")
            rows.append((line_no, row))
    except csv.Error as error:
        # 引号未闭合等损坏行结构。
        raise CsvFormatError(f"malformed csv structure: {error}") from error
    return rows


def _validate_row(declared_tenant: str, fields: list[str]) -> tuple[str, int, str] | str:
    """返回(order_id, amount_cents, currency)表示通过；否则返回错误描述。"""
    tenant, order_id, amount_text, currency = fields
    if not tenant or tenant != declared_tenant:
        return "row tenant must match the tenant declared by the request"
    if not order_id.strip():
        return "order_id is required"
    if _AMOUNT_RE.fullmatch(amount_text) is None or int(amount_text) <= 0:
        return "amount_cents must be a positive integer in the smallest currency unit"
    if currency not in ALLOWED_CURRENCIES:
        return f"unsupported currency: {currency}"
    return order_id, int(amount_text), currency


def accept_batch(tenant: str, batch_id: str, raw: bytes) -> tuple[dict, bool]:
    """受理一个批次，返回(批次结果, 本次是否实际处理)。

    批次身份为（租户, batch_id）：终态批次重放只返回既有结果、不再受理任何行；
    进行中批次按行断点续跑，每行在独立事务内提交，崩溃不回滚已提交行。
    """
    rows = parse_rows(raw)
    _, mode = batches.ensure(tenant, batch_id, len(rows), raw)
    if mode == "terminal":
        # 终态重放：无论本次输入是什么，都只回放该批次既有结果与计数。
        return batches.get(tenant, batch_id), False

    done = batches.recorded_lines(tenant, batch_id)
    for line_no, fields in rows:
        if line_no in done:
            # 断点续跑：已提交行（成功或失败）不重复处理。
            continue
        result = _validate_row(tenant, fields)
        if isinstance(result, str):
            batches.record_failure(tenant, batch_id, line_no, fields[1], batches.INVALID_PARAMETER, result)
            continue
        order_id, amount_cents, currency = result
        try:
            batches.record_success(tenant, batch_id, line_no, order_id, amount_cents, currency)
        except batches.OrderAlreadyExists:
            # 批次外已受理或批次内重复的订单标识：按既有订单处理，不重复受理、不改变其数据。
            batches.record_failure(
                tenant, batch_id, line_no, order_id, batches.ORDER_CONFLICT, "order already accepted"
            )
    return batches.finish(tenant, batch_id), True
