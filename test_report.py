import unittest
from decimal import Decimal as D
from datetime import date
import report

def run(on):
    cs, w = report.build("data", on)
    return {c.contract_id: c for c in cs}, w

class T(unittest.TestCase):
    def test_check_date(self):
        c, _ = run(date(2026, 11, 10))
        d1, d2 = c["D-1"], c["D-2"]
        self.assertEqual(d1.paid_total, D("315000.00"))   # возврат 50 000 вычтен, очередь не считается
        self.assertEqual(d1.remaining, D("185000.00"))
        self.assertEqual(d1.queued, D("150000.00"))
        self.assertEqual(d1.unallocated, D("15000.00"))
        self.assertEqual(d2.paid_total, D("75000.00"))    # платёж 12.11 ещё не ушёл
        self.assertEqual(d2.remaining, D("15000.00"))
        w23 = [w for w in d2.works if w.work_id == "W-23"][0]
        self.assertEqual(w23.state, "planned")            # принята только 11.11

    def test_refund_not_yet_returned(self):
        c, _ = run(date(2026, 9, 5))                      # возврат был 10.09
        self.assertEqual(c["D-1"].paid_total, D("350000.00"))

    def test_overpay_later(self):
        c, w = run(date(2026, 11, 12))
        self.assertEqual(c["D-2"].overpaid, D("5000.00"))

    def test_contract_not_signed_yet(self):
        c, _ = run(date(2026, 9, 1))
        self.assertNotIn("D-2", c)

    def test_sum_invariant(self):
        for on in (date(2026, 10, 1), date(2026, 11, 10), date(2026, 11, 20)):
            cs, _ = report.build("data", on)
            for c in cs:
                allocated = sum((w.paid for w in c.works), D("0"))
                self.assertEqual(allocated + c.unallocated, c.paid_total)

if __name__ == "__main__":
    unittest.main()
