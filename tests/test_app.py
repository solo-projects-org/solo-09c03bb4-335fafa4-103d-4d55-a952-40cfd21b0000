import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calc, create_app, db, services  # noqa: E402


def csv_bytes(month, rows, header=("月份", "摊位", "品种", "每公斤报价")):
    lines = [",".join(header)]
    for stall, item, price in rows:
        lines.append(f"{month},{stall},{item},{price}")
    return ("\n".join(lines) + "\n").encode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, month, rows, follow=True):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(csv_bytes(month, rows)), f"{month}.csv")},
            content_type="multipart/form-data",
            follow_redirects=follow,
        )

    def table(self):
        resp = self.client.get("/index.csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True).strip().splitlines()
        out = {}
        for line in text[1:]:
            month, pairs, factor, index, status = line.split(",")
            out[month] = {
                "pairs": int(pairs) if pairs else None,
                "factor": float(factor) if factor else None,
                "index": float(index) if index else None,
                "status": status,
            }
        return out

    def conn(self):
        return db.connect(self.db_path)


class TestParsing(Base):
    def test_header_alias_and_english(self):
        raw = b"month,stall,item,price\n2025-01,A,\xe5\x9c\x9f\xe8\xb1\x86,3.5\n"
        res = services.parse_csv(raw)
        self.assertEqual(res.month, "2025-01")
        self.assertEqual(res.rows[0].price, 3.5)

    def test_duplicate_rejected(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3), ("A", "土豆", 4)])
        with self.assertRaises(services.UploadError) as ctx:
            services.parse_csv(raw)
        self.assertIn("只收一条", ctx.exception.message)

    def test_non_positive_price_rejected(self):
        for bad in ("0", "-2", "abc", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(services.UploadError):
                    services.parse_csv(csv_bytes("2025-01", [("A", "土豆", bad)]))

    def test_multiple_months_rejected(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3)]) + b"2025-02,B,\xe5\x9c\x9f\xe8\xb1\x86,3\n"
        with self.assertRaises(services.UploadError):
            services.parse_csv(raw)

    def test_bad_month_format_rejected(self):
        raw = csv_bytes("2025-13", [("A", "土豆", 3)])
        with self.assertRaises(services.UploadError):
            services.parse_csv(raw)

    def test_gbk_file_supported(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3)]).decode("utf-8").encode("gb18030")
        self.assertEqual(services.parse_csv(raw).month, "2025-01")


class TestIndexMath(Base):
    def test_base_month_is_100_and_factor_is_equal_weighted_mean(self):
        self.upload("2025-01", [("A", "土豆", 2.0), ("B", "土豆", 4.0)])
        self.upload("2025-02", [("A", "土豆", 2.2), ("B", "土豆", 4.4)])
        t = self.table()
        self.assertEqual(t["2025-01"]["index"], 100.0)
        self.assertEqual(t["2025-01"]["status"], "基月")
        self.assertEqual(t["2025-02"]["pairs"], 2)
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.1, places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 110.0, places=4)

    def test_only_matching_stall_item_pairs_count(self):
        self.upload("2025-01", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 3.0), ("C", "白菜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 1)
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.5, places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 150.0, places=4)

    def test_chain_compounds(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-03", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-02"]["index"], 200.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["index"], 200.0, places=4)

    def test_missing_month_breaks_chain_and_backfill_restores(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-04", [("A", "土豆", 4.0)])
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "缺月")
        self.assertIsNone(t["2025-03"]["index"])
        self.assertIsNone(t["2025-04"]["index"])
        self.assertEqual(t["2025-04"]["status"], "断链")

        self.upload("2025-03", [("A", "土豆", 4.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["index"], 400.0, places=4)
        self.assertAlmostEqual(t["2025-04"]["index"], 400.0, places=4)
        self.assertEqual(t["2025-04"]["status"], "正常")

    def test_no_pairs_breaks_chain(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("Q", "黄瓜", 9.0)])
        self.upload("2025-03", [("Q", "黄瓜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 0)
        self.assertEqual(t["2025-02"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-03"]["index"])

    def test_earlier_than_base_rejected(self):
        self.upload("2025-03", [("A", "土豆", 1.0)])
        resp = self.upload("2025-01", [("A", "土豆", 1.0)])
        self.assertIn("早于基月", resp.get_data(as_text=True))
        self.assertNotIn("2025-01", self.table())

    def test_base_month_fixed_after_first_upload(self):
        self.upload("2025-03", [("A", "土豆", 1.0)])
        self.upload("2025-05", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "基月")
        self.assertEqual(t["2025-05"]["status"], "断链")

    def test_reupload_replaces_whole_month(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 3.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 1)
        self.assertAlmostEqual(t["2025-02"]["factor"], 3.0, places=6)
        conn = self.conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM prices WHERE month='2025-02'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count, 1)


class TestIncremental(Base):
    def test_correction_only_recomputes_neighbouring_factors(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.0)]:
            self.upload(month, [("A", "土豆", price)])
        before = self.table()
        # 标记 2025-01 的环比不参与（基月），比较 2/3 月
        self.upload("2025-02", [("A", "土豆", 3.0)])
        after = self.table()
        self.assertAlmostEqual(after["2025-02"]["factor"], 3.0, places=6)
        self.assertAlmostEqual(after["2025-02"]["index"], 300.0, places=4)
        # 3 月环比 = 2.0 / 3.0
        self.assertAlmostEqual(after["2025-03"]["factor"], 2.0 / 3.0, places=3)
        self.assertAlmostEqual(after["2025-03"]["index"], 200.0, places=4)
        # 基月未变
        self.assertEqual(before["2025-01"]["index"], after["2025-01"]["index"])

    def test_break_and_repair_via_replace(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.5)]:
            self.upload(month, [("A", "土豆", price)])
        self.upload("2025-02", [("Z", "黄瓜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-02"]["index"])
        self.assertIsNone(t["2025-03"]["index"])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["index"], 250.0, places=4)

    def test_only_two_factors_recomputed_on_replace(self):
        for month, price in [
            ("2025-01", 1.0),
            ("2025-02", 2.0),
            ("2025-03", 2.5),
            ("2025-04", 2.5),
        ]:
            self.upload(month, [("A", "土豆", price)])

        conn = self.conn()
        try:
            with db.transaction(conn):
                # 篡改一个「不应被重算」的环比，重算后应原样保留
                conn.execute(
                    "UPDATE index_months SET mom_factor = 1.5 WHERE month = '2025-04'"
                )
        finally:
            conn.close()

        self.upload("2025-02", [("A", "土豆", 3.0)])
        conn = self.conn()
        try:
            rows = {
                r["month"]: r
                for r in conn.execute("SELECT * FROM index_months").fetchall()
            }
        finally:
            conn.close()
        # 2、3 月环比被重算
        self.assertAlmostEqual(rows["2025-02"]["mom_factor"], 3.0, places=6)
        self.assertAlmostEqual(rows["2025-03"]["mom_factor"], 2.5 / 3.0, places=6)
        # 4 月环比沿用，不被重算
        self.assertAlmostEqual(rows["2025-04"]["mom_factor"], 1.5, places=6)
        # 但指数全部重新串联
        self.assertAlmostEqual(rows["2025-04"]["index_value"], 300.0 * 2.5 / 3.0 * 1.5, places=3)


class TestConsistency(Base):
    def test_index_query_does_not_read_price_details(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        baseline = self.table()
        conn = self.conn()
        try:
            # 直接篡改明细，模拟「明细不可信」的情形
            conn.execute("UPDATE prices SET price = 99 WHERE month='2025-02'")
        finally:
            conn.close()
        self.assertEqual(self.table(), baseline)

    def test_failed_write_rolls_back_prices_and_index(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        before = self.table()
        conn = self.conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # 触发器令写入价格失败
            conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON prices "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()
        resp = self.upload("2025-02", [("A", "土豆", 9.0)])
        self.assertIn("回滚", resp.get_data(as_text=True))
        self.assertEqual(self.table(), before)
        conn = self.conn()
        try:
            price = conn.execute(
                "SELECT price FROM prices WHERE month='2025-02'"
            ).fetchone()["price"]
            uploads = conn.execute(
                "SELECT COUNT(*) c FROM uploads WHERE month='2025-02'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(price, 2.0)
        self.assertEqual(uploads, 1)


class TestSwap(Base):
    def _seed(self):
        # 基月 01，2..6 月价格依次 2/3/6/6/6（只有 05/06 环比应保持不变）
        for month, price in [
            ("2025-01", 1.0),
            ("2025-02", 2.0),
            ("2025-03", 3.0),
            ("2025-04", 6.0),
            ("2025-05", 6.0),
            ("2025-06", 6.0),
        ]:
            self.upload(month, [("A", "土豆", price)])

    def swap(self, token_a, token_b, follow=True):
        return self.client.post(
            "/swap",
            data={"month_a": token_a, "month_b": token_b},
            follow_redirects=follow,
        )

    def batch_tokens(self):
        tokens = {}
        conn = self.conn()
        try:
            for month, batch in db.current_batches(conn).items():
                tokens[month] = f"{month}|{batch['id']}"
        finally:
            conn.close()
        return tokens

    def rows(self):
        conn = self.conn()
        try:
            return {
                r["month"]: dict(r)
                for r in conn.execute("SELECT * FROM index_months").fetchall()
            }
        finally:
            conn.close()

    def test_swap_exchanges_prices_and_updates_index(self):
        self._seed()
        tokens = self.batch_tokens()
        resp = self.swap(tokens["2025-02"], tokens["2025-03"])
        self.assertIn("已对调", resp.get_data(as_text=True))

        t = self.table()
        # 价格互换：2 月变成原 3 月的 3.0，3 月变成原 2 月的 2.0
        self.assertAlmostEqual(t["2025-02"]["factor"], 3.0, places=4)
        self.assertAlmostEqual(t["2025-02"]["index"], 300.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["factor"], 2.0 / 3.0, places=3)
        self.assertAlmostEqual(t["2025-03"]["index"], 200.0, places=4)

        conn = self.conn()
        try:
            prices = {
                m: conn.execute(
                    "SELECT price FROM prices WHERE month=? AND stall='A' AND item='土豆'",
                    (m,),
                ).fetchone()["price"]
                for m in ("2025-02", "2025-03")
            }
        finally:
            conn.close()
        self.assertEqual(prices, {"2025-02": 3.0, "2025-03": 2.0})

    def test_base_month_stays_put(self):
        self._seed()
        tokens = self.batch_tokens()
        self.swap(tokens["2025-01"], tokens["2025-02"])

        t = self.table()
        self.assertEqual(t["2025-01"]["status"], "基月")
        self.assertEqual(t["2025-01"]["index"], 100.0)
        # 1 月现在拿的是原 2 月报价 2.0，2 月拿的是原 1 月报价 1.0
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.0 / 2.0, places=4)
        self.assertAlmostEqual(t["2025-02"]["index"], 50.0, places=4)

        conn = self.conn()
        try:
            base = services.base_month(conn)
            flags = {
                r["id"]: r["is_base_month"]
                for r in conn.execute("SELECT id, is_base_month FROM uploads").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(base, "2025-01")
        self.assertEqual(flags[int(tokens["2025-02"].split("|")[1])], 1)
        self.assertEqual(flags[int(tokens["2025-01"].split("|")[1])], 0)

    def test_original_upload_records_kept_and_effective_month_swapped(self):
        self._seed()
        tokens = self.batch_tokens()
        before = {
            r["id"]: dict(r)
            for r in self.conn().execute("SELECT * FROM uploads").fetchall()
        }
        self.swap(tokens["2025-02"], tokens["2025-03"])

        conn = self.conn()
        try:
            after = {
                r["id"]: dict(r)
                for r in conn.execute("SELECT * FROM uploads").fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(before.keys(), after.keys())  # 未新增/删除上传记录
        id_02 = int(tokens["2025-02"].split("|")[1])
        id_03 = int(tokens["2025-03"].split("|")[1])
        # 原报月不变，生效月互换
        self.assertEqual(after[id_02]["month"], "2025-02")
        self.assertEqual(after[id_02]["effective_month"], "2025-03")
        self.assertEqual(after[id_03]["month"], "2025-03")
        self.assertEqual(after[id_03]["effective_month"], "2025-02")
        # 其它字段（条数、上传时间、文件名）不变
        for col in ("row_count", "uploaded_at", "filename"):
            self.assertEqual(after[id_02][col], before[id_02][col])
            self.assertEqual(after[id_03][col], before[id_03][col])

    def test_only_two_months_and_neighbours_recomputed(self):
        self._seed()
        conn = self.conn()
        try:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE index_months SET mom_factor = 1.234 WHERE month = '2025-05'"
                )
                conn.execute(
                    "UPDATE index_months SET mom_factor = 1.111 WHERE month = '2025-06'"
                )
        finally:
            conn.close()

        tokens = self.batch_tokens()
        self.swap(tokens["2025-02"], tokens["2025-03"])

        t = self.table()
        # 2、3、4 月环比重算
        self.assertAlmostEqual(t["2025-02"]["factor"], 3.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["factor"], 2.0 / 3.0, places=3)
        self.assertAlmostEqual(t["2025-04"]["factor"], 6.0 / 2.0, places=4)
        # 5、6 月环比沿用已有结果
        self.assertAlmostEqual(t["2025-05"]["factor"], 1.234, places=4)
        self.assertAlmostEqual(t["2025-06"]["factor"], 1.111, places=4)
        # 指数仍从基月重新串联
        self.assertAlmostEqual(
            t["2025-06"]["index"], 300.0 * (2 / 3) * 3.0 * 1.234 * 1.111, places=1
        )

    def test_stale_batch_stops_swap_and_keeps_state(self):
        self._seed()
        tokens = self.batch_tokens()
        # 页面渲染后，2 月被重新上传，现行批次改变
        self.upload("2025-02", [("A", "土豆", 9.0)])
        before = self.table()

        resp = self.swap(tokens["2025-02"], tokens["2025-03"])
        text = resp.get_data(as_text=True)
        self.assertIn("已停止对调", text)
        self.assertIn("重新查看", text)
        self.assertEqual(self.table(), before)

        conn = self.conn()
        try:
            price = conn.execute(
                "SELECT price FROM prices WHERE month='2025-02'"
            ).fetchone()["price"]
        finally:
            conn.close()
        self.assertEqual(price, 9.0)

    def test_swap_same_month_or_missing_month_rejected(self):
        self._seed()
        tokens = self.batch_tokens()
        resp = self.swap(tokens["2025-02"], tokens["2025-02"])
        self.assertIn("两个不同的月份", resp.get_data(as_text=True))
        resp = self.swap(f"2024-12|99", tokens["2025-02"])
        self.assertIn("没有现行批次", resp.get_data(as_text=True))

    def test_swap_rolls_back_on_write_failure(self):
        self._seed()
        tokens = self.batch_tokens()
        before = self.table()
        conn = self.conn()
        try:
            conn.execute(
                "CREATE TRIGGER boom BEFORE UPDATE ON uploads "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()

        resp = self.swap(tokens["2025-02"], tokens["2025-03"])
        self.assertIn("已回滚", resp.get_data(as_text=True))
        self.assertEqual(self.table(), before)

        conn = self.conn()
        try:
            price = conn.execute(
                "SELECT price FROM prices WHERE month='2025-02'"
            ).fetchone()["price"]
        finally:
            conn.close()
        self.assertEqual(price, 2.0)

    def test_reupload_after_swap_uses_effective_month_and_new_batch(self):
        self._seed()
        tokens = self.batch_tokens()
        id_02 = int(tokens["2025-02"].split("|")[1])  # 对调后归属 2025-03 的批次
        self.swap(tokens["2025-02"], tokens["2025-03"])

        # 日后重报 2025-03：按生效月整体替换，指数使用新批次
        self.upload("2025-03", [("A", "土豆", 5.0)])

        conn = self.conn()
        try:
            batch = db.current_batches(conn)["2025-03"]
            old = conn.execute(
                "SELECT effective_month, (SELECT COUNT(*) FROM prices p "
                "WHERE p.upload_id = uploads.id) AS n FROM uploads WHERE id = ?",
                (id_02,),
            ).fetchone()
            idx = conn.execute(
                "SELECT pair_count, mom_factor, index_value, source_upload_id "
                "FROM index_months WHERE month='2025-03'"
            ).fetchone()
        finally:
            conn.close()

        self.assertNotEqual(batch["id"], id_02)
        self.assertEqual(batch["month"], "2025-03")  # 原报月 = 生效月
        self.assertEqual(old["n"], 0)  # 旧批次明细被替换，但记录仍在
        self.assertEqual(old["effective_month"], "2025-03")  # 旧批次仍归属 2025-03
        self.assertEqual(idx["source_upload_id"], batch["id"])
        self.assertEqual(idx["pair_count"], 1)
        self.assertAlmostEqual(idx["mom_factor"], 5.0 / 3.0, places=6)

    def test_reupload_base_month_keeps_single_base_batch(self):
        self._seed()
        self.upload("2025-01", [("A", "土豆", 2.0)])
        conn = self.conn()
        try:
            flags = [
                r["is_base_month"]
                for r in conn.execute(
                    "SELECT is_base_month FROM uploads WHERE effective_month='2025-01'"
                ).fetchall()
            ]
            base = services.base_month(conn)
        finally:
            conn.close()
        self.assertEqual(sum(flags), 1)
        self.assertEqual(base, "2025-01")
        t = self.table()
        self.assertEqual(t["2025-01"]["status"], "基月")
        self.assertEqual(t["2025-01"]["index"], 100.0)
        self.assertAlmostEqual(t["2025-02"]["factor"], 2.0 / 2.0, places=4)

    def test_non_adjacent_swap_preserves_months_between(self):
        self._seed()
        conn = self.conn()
        try:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE index_months SET mom_factor = 1.777 WHERE month = '2025-04'"
                )
        finally:
            conn.close()

        tokens = self.batch_tokens()
        self.swap(tokens["2025-02"], tokens["2025-05"])

        t = self.table()
        # 2 月换成原 5 月价 6.0；5 月换成原 2 月价 2.0
        self.assertAlmostEqual(t["2025-02"]["factor"], 6.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["factor"], 3.0 / 6.0, places=4)
        self.assertAlmostEqual(t["2025-05"]["factor"], 2.0 / 6.0, places=4)
        self.assertAlmostEqual(t["2025-06"]["factor"], 6.0 / 2.0, places=4)
        # 3→4、4 月的环比不在对调影响范围内，沿用旧值
        self.assertAlmostEqual(t["2025-04"]["factor"], 1.777, places=4)

    def test_snapshot_reads_index_and_batches_together(self):
        self._seed()
        conn = self.conn()
        try:
            rows, batches = calc.read_snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(rows[-1]["month"], batches[list(batches)[-1]]["effective_month"])
        self.assertTrue(all(r["status"] for r in rows))

    def test_page_shows_original_and_effective_months(self):
        self._seed()
        tokens = self.batch_tokens()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("原报月", html)
        self.assertIn("生效月", html)
        self.assertIn("对调两个月的报价", html)
        self.assertIn(tokens["2025-02"], html)
        self.assertIn(tokens["2025-03"], html)

        self.swap(tokens["2025-02"], tokens["2025-03"])
        html = self.client.get("/").get_data(as_text=True)
        # 对调后两批的原报月不变、生效月互换，页面可核对
        self.assertIn("2025-02|" + tokens["2025-03"].split("|")[1], html)
        self.assertIn("2025-03|" + tokens["2025-02"].split("|")[1], html)


class TestDemo(Base):
    def test_demo_break_and_backfill(self):
        self.client.post("/demo/load", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-04"]["status"], "缺月")
        self.assertEqual(t["2025-05"]["status"], "断链")
        self.client.post("/demo/backfill", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-05"]["status"], "正常")
        self.client.post("/demo/break", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-05"]["index"])
        self.client.post("/demo/repair", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-05"]["status"], "正常")

    def test_demo_correction_updates_downstream(self):
        self.client.post("/demo/load", follow_redirects=True)
        self.client.post("/demo/backfill", follow_redirects=True)
        before = self.table()
        self.client.post("/demo/correct", follow_redirects=True)
        after = self.table()
        self.assertEqual(before["2025-01"], after["2025-01"])
        self.assertNotEqual(before["2025-02"]["factor"], after["2025-02"]["factor"])
        self.assertNotEqual(before["2025-05"]["index"], after["2025-05"]["index"])

    def test_demo_swap_then_resubmit(self):
        self.client.post("/demo/load", follow_redirects=True)
        self.client.post("/demo/backfill", follow_redirects=True)
        before = self.table()
        self.client.post("/demo/swap", follow_redirects=True)
        swapped = self.table()
        # 2、3 月报价互换：2 月变成「3 月价 / 1 月价」，3 月变成「2 月价 / 3 月价」，
        # 4 月分母换成 2 月价；5 月环比（4 月价 / 5 月价）不受影响
        self.assertAlmostEqual(
            swapped["2025-02"]["factor"],
            before["2025-02"]["factor"] * before["2025-03"]["factor"],
            places=3,
        )
        self.assertAlmostEqual(
            swapped["2025-03"]["factor"], 1 / before["2025-03"]["factor"], places=3
        )
        self.assertAlmostEqual(swapped["2025-05"]["factor"], before["2025-05"]["factor"], places=4)
        self.assertEqual(swapped["2025-01"]["status"], "基月")

        conn = self.conn()
        try:
            batches = {m: b["month"] for m, b in db.current_batches(conn).items()}
            count_before = conn.execute("SELECT COUNT(*) c FROM uploads").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(batches["2025-02"], "2025-03")
        self.assertEqual(batches["2025-03"], "2025-02")

        self.client.post("/demo/resubmit", follow_redirects=True)
        conn = self.conn()
        try:
            count_after = conn.execute("SELECT COUNT(*) c FROM uploads").fetchone()["c"]
            batch = db.current_batches(conn)["2025-03"]
            idx = conn.execute(
                "SELECT source_upload_id FROM index_months WHERE month='2025-03'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(count_after, count_before + 1)
        self.assertEqual(batch["month"], "2025-03")
        self.assertEqual(idx["source_upload_id"], batch["id"])


class TestConcurrency(Base):
    def test_reader_never_sees_torn_snapshot(self):
        import threading

        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    # 两个候选版本交替替换：环比 2.0 与 3.0
                    price = 2.0 if i % 2 else 3.0
                    self.upload("2025-02", [("A", "土豆", price)])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(80):
                rows = self.table()
                factor = rows["2025-02"]["factor"]
                index = rows["2025-02"]["index"]
                # 环比与指数必须来自同一次提交
                self.assertAlmostEqual(index, round(100 * factor, 6), places=4)
                self.assertIn(round(factor, 4), (2.0, 3.0))
        finally:
            stop.set()
            thread.join(timeout=20)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
