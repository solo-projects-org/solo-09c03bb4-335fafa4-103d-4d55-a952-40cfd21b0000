"""内置演示数据：一键复现「缺月 → 断链 → 补报接续 / 更正恢复」。

演示数据直接以 CSV 文本生成，再走与网页上传完全相同的业务路径，
因此演示结果与真实上传一致。
"""

from __future__ import annotations

import csv
import io

from . import db, services

MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]

STALLS = ["A-01", "A-02", "B-01"]
ITEMS = ["大白菜", "土豆", "西红柿"]

# 每月相对基月的价格倍率
SCALE = {
    "2025-01": 1.00,
    "2025-02": 1.05,
    "2025-03": 1.12,
    "2025-04": 1.09,
    "2025-05": 1.18,
}

BASE_PRICES = {
    ("A-01", "大白菜"): 4.00,
    ("A-01", "土豆"): 3.00,
    ("A-02", "大白菜"): 5.00,
    ("A-02", "西红柿"): 7.00,
    ("B-01", "土豆"): 3.00,
    ("B-01", "西红柿"): 6.00,
}

BREAK_STALLS = ["X-01", "Y-01"]
BREAK_PRICES = {
    ("X-01", "大白菜"): 5.10,
    ("X-01", "土豆"): 4.05,
    ("Y-01", "西红柿"): 7.25,
}



class DemoError(Exception):
    pass


def _price(stall: str, item: str, month: str) -> float | None:
    base = BASE_PRICES.get((stall, item))
    if base is None:
        return None
    return round(base * SCALE[month], 2)


def build_csv(
    month: str, layout: str = "normal", prices: dict[tuple[str, str], float] | None = None
) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月份", "摊位", "品种", "每公斤报价"])
    if prices is not None:
        source = prices
    elif layout == "normal":
        source = {
            (stall, item): _price(stall, item, month)
            for stall in STALLS
            for item in ITEMS
        }
    else:
        source = dict(BREAK_PRICES)
    for (stall, item), price in source.items():
        if price is None:
            continue
        writer.writerow([month, stall, item, f"{price:.2f}"])
    return buf.getvalue()


def _upload(conn, month: str, layout: str = "normal") -> None:
    raw = build_csv(month, layout).encode("utf-8")
    services.process_upload(conn, raw, f"demo-{month}-{layout}.csv")


def reset(conn) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM prices")
        conn.execute("DELETE FROM index_months")
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('prices','uploads')")


def _require_demo(conn) -> None:
    base = services.base_month(conn)
    if base is None:
        raise DemoError("请先点「载入示例数据」")
    if base != MONTHS[0]:
        raise DemoError("当前数据不是示例数据，请先点「载入示例数据」")
    have = set(db.current_batches(conn))
    if "2025-02" not in have or "2025-03" not in have:
        raise DemoError("示例数据不完整，请重新载入")


def run(action: str, conn) -> str:
    if action == "reset":
        reset(conn)
        return "演示数据已清空，可重新载入示例。"

    if action == "load":
        reset(conn)
        for month in MONTHS:
            if month == "2025-04":
                continue  # 故意缺 4 月
            _upload(conn, month)
        return "已载入 2025 年 1、2、3、5 月报价：4 月缺月，5 月因此断链。"

    _require_demo(conn)

    if action == "backfill":
        if "2025-04" in db.current_batches(conn):
            raise DemoError("4 月已有报价，无需补报（可先点「载入示例数据」重置）")
        _upload(conn, "2025-04")
        return "已补报 2025-04：4 月环比接通，5 月指数随之恢复。"

    if action == "break":
        _upload(conn, "2025-03", layout="break")
        return "已把 2025-03 换成完全不同摊位的报价：3 月起无可比报价对，指数清空。"

    if action == "repair":
        _upload(conn, "2025-03")
        return "已更正 2025-03 报价：只重算 3、4 月环比，指数自 3 月起重连。"

    if action == "correct":
        # 只更正部分摊位：环比不会被整体抵消，后续指数会真实变化
        lines = build_csv("2025-02").splitlines()
        rows = [lines[0]]
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = next(csv.reader([line]))
            if parts[1] == "A-01":
                parts[3] = f"{float(parts[3]) * 1.20:.2f}"
            rows.append(",".join(parts))
        raw = ("\n".join(rows) + "\n").encode("utf-8")
        services.process_upload(conn, raw, "demo-2025-02-corrected.csv")
        return (
            "已把 2025-02 中 A-01 摊位报价上调 20%：只重算 2、3 月环比，"
            "2 月及之后各月指数随之更新。"
        )

    if action == "swap":
        current = db.current_batches(conn)
        for month in ("2025-02", "2025-03"):
            if month not in current:
                raise DemoError(f"{month} 没有现行批次，请先点「载入示例数据」")
        result = services.swap_months(
            conn,
            "2025-02",
            "2025-03",
            expected={
                "2025-02": current["2025-02"]["id"],
                "2025-03": current["2025-03"]["id"],
            },
        )
        return (
            f"已对调 {result['month_a']} 与 {result['month_b']} 的报价："
            "原始上传记录保留，批次归属与指数一并更新；只重算 2、3、4 月环比。"
        )

    if action == "resubmit":
        # 对调后按生效月重报 2025-03：在该月当前报价基础上整体上调 10%，
        # 走正常上传路径，整体替换该月报价，指数改用新批次。
        if "2025-03" not in db.current_batches(conn):
            raise DemoError("2025-03 没有现行批次，请先点「载入示例数据」")
        current_prices = db.month_prices(conn, "2025-03")
        source = {key: round(price * 1.10, 2) for key, price in current_prices.items()}
        raw = build_csv("2025-03", prices=source).encode("utf-8")
        services.process_upload(conn, raw, "demo-2025-03-resubmitted.csv")
        return (
            "已按生效月重报 2025-03（当前报价整体上调 10%）：该月报价替换为新批次，"
            "指数使用新批次，只重算 3、4 月环比。"
        )

    raise DemoError("未知的演示操作")
