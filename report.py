#!/usr/bin/env python3
"""Отчёт по договорам: по каждой работе - цена, выплачено, осталось оплатить.

Запуск:
    python3 report.py --date 10.11.2026
    python3 report.py --date 2026-11-10 --data data --csv-dir out

Только стандартная библиотека, Python 3.9+. Деньги - Decimal, без float.
"""
import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


# ---------- чтение ----------

def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"не могу разобрать дату: {s!r}")


def money(s):
    return Decimal(s.strip()).quantize(CENT)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------- модель ----------

@dataclass
class WorkRow:
    work_id: str
    title: str
    price: Decimal
    state: str                 # done | planned | cancelled - на дату отчёта
    completed_at: date = None
    paid_bound: Decimal = ZERO     # платежи, привязанные к работе
    paid_alloc: Decimal = ZERO     # распределено из платежей без привязки
    @property
    def paid(self):
        return self.paid_bound + self.paid_alloc
    @property
    def remaining(self):
        return ZERO if self.state == "cancelled" else max(self.price - self.paid, ZERO)
    @property
    def overpaid(self):
        base = ZERO if self.state == "cancelled" else self.price
        return max(self.paid - base, ZERO)


@dataclass
class ContractRow:
    contract_id: str
    contractor: str
    amount: Decimal
    works: list = field(default_factory=list)
    paid_total: Decimal = ZERO
    queued: Decimal = ZERO
    unallocated: Decimal = ZERO    # платежи без привязки, не закрытые принятыми работами
    @property
    def active_price(self):
        return sum((w.price for w in self.works if w.state != "cancelled"), ZERO)
    @property
    def cancelled_price(self):
        return sum((w.price for w in self.works if w.state == "cancelled"), ZERO)
    @property
    def remaining(self):
        return max(self.active_price - self.paid_total, ZERO)
    @property
    def overpaid(self):
        return max(self.paid_total - self.active_price, ZERO)


def work_state(w, on):
    """Статус работы на дату отчёта (в выгрузке - статус на момент выгрузки)."""
    if w["status"] == "done" and (parse_date(w["completed_at"]) or date.max) <= on:
        return "done"
    if w["status"] == "cancelled" and (parse_date(w["cancelled_at"]) or date.max) <= on:
        return "cancelled"
    return "planned"


def paid_net(p, on, warnings):
    """Сколько по платежу реально ушло подрядчику и осталось у него на дату."""
    paid_at = parse_date(p["paid_at"])
    if p["status"] in ("paid", "refunded") and paid_at is None:
        warnings.append(f"{p['payment_id']}: статус {p['status']}, но нет paid_at - не учтён")
        return ZERO
    if paid_at is None or paid_at > on:
        return ZERO
    if p["status"] == "refunded":
        ref = parse_date(p["refunded_at"])
        if ref is None:
            warnings.append(f"{p['payment_id']}: refunded без refunded_at - считаю возвращённым")
            return ZERO
        if ref <= on:
            return ZERO
    return money(p["amount"])


def is_queued(p, on):
    """Платёж уже заведён в учёт на дату, но деньги ещё не ушли."""
    created = parse_date(p["created_at"])
    paid_at = parse_date(p["paid_at"])
    return created is not None and created <= on and (paid_at is None or paid_at > on)


def build(data_dir, on):
    d = Path(data_dir)
    contracts = read_csv(d / "contracts.csv")
    works = read_csv(d / "works.csv")
    payments = read_csv(d / "payments.csv")
    warnings = []
    result = []
    cids = {c["contract_id"] for c in contracts}

    for w in works:
        if w["contract_id"] not in cids:
            warnings.append(f"{w['work_id']}: неизвестный договор {w['contract_id']}")
    for p in payments:
        if p["contract_id"] not in cids:
            warnings.append(f"{p['payment_id']}: неизвестный договор {p['contract_id']}")

    for c in contracts:
        signed = parse_date(c["signed_at"])
        if signed and signed > on:
            continue  # на дату отчёта договора ещё нет
        cr = ContractRow(c["contract_id"], c["contractor"], money(c["amount"]))
        by_id = {}
        for w in works:
            if w["contract_id"] != cr.contract_id:
                continue
            row = WorkRow(w["work_id"], w["title"], money(w["price"]),
                          work_state(w, on), parse_date(w["completed_at"]))
            cr.works.append(row)
            by_id[row.work_id] = row

        pool = ZERO  # платежи без привязки к работе
        for p in payments:
            if p["contract_id"] != cr.contract_id:
                continue
            net = paid_net(p, on, warnings)
            if is_queued(p, on):
                cr.queued += money(p["amount"])
            if net == 0:
                continue
            cr.paid_total += net
            wid = p["work_id"].strip()
            if wid:
                if wid not in by_id:
                    warnings.append(f"{p['payment_id']}: работа {wid} не найдена в {cr.contract_id}")
                    pool += net
                else:
                    by_id[wid].paid_bound += net
            else:
                pool += net

        # платежи без привязки закрывают принятые работы по порядку приёмки
        done = sorted((w for w in cr.works if w.state == "done"),
                      key=lambda w: (w.completed_at or date.max, w.work_id))
        for w in done:
            need = max(w.price - w.paid_bound, ZERO)
            take = min(need, pool)
            w.paid_alloc = take
            pool -= take
        cr.unallocated = pool

        if cr.active_price != cr.amount:
            warnings.append(
                f"{cr.contract_id}: сумма договора {cr.amount} не равна сумме работ без отменённых "
                f"{cr.active_price} (отменено на {cr.cancelled_price})")
        if cr.overpaid > 0:
            warnings.append(f"{cr.contract_id}: переплата {cr.overpaid}")
        result.append(cr)
    return result, warnings


# ---------- вывод ----------

def fm(x):
    return f"{x:,.2f}".replace(",", " ")

STATE_RU = {"done": "принята", "planned": "в плане", "cancelled": "отменена"}


def render(contracts, warnings, on):
    out = [f"ОТЧЁТ ПО ДОГОВОРАМ НА {on.strftime('%d.%m.%Y')} (руб.)", ""]
    for c in contracts:
        out.append(f"Договор {c.contract_id} - {c.contractor}. Сумма по договору: {fm(c.amount)}")
        hdr = f"  {'Работа':<8}{'Название':<28}{'Статус':<10}{'Цена':>14}{'Выплачено':>14}{'Осталось':>14}"
        out.append(hdr)
        for w in c.works:
            out.append(f"  {w.work_id:<8}{w.title[:26]:<28}{STATE_RU[w.state]:<10}"
                       f"{fm(w.price):>14}{fm(w.paid):>14}{fm(w.remaining):>14}")
        out.append("  " + "-" * (len(hdr) - 2))
        out.append(f"  Осталось по работам (сумма столбца):   {fm(sum((w.remaining for w in c.works), ZERO)):>14}")
        out.append(f"  Работы без отменённых:                 {fm(c.active_price):>14}")
        out.append(f"  Всего выплачено (с учётом возвратов):  {fm(c.paid_total):>14}")
        out.append(f"    из них закрывают принятые работы:    {fm(c.paid_total - c.unallocated):>14}")
        out.append(f"    аванс, пока не закрытый работами:    {fm(c.unallocated):>14}")
        out.append(f"  ОСТАЛОСЬ ОПЛАТИТЬ по договору:         {fm(c.remaining):>14}")
        if c.overpaid:
            out.append(f"  ПЕРЕПЛАТА:                             {fm(c.overpaid):>14}")
        out.append(f"  Справочно: платежи в очереди (не ушли): {fm(c.queued):>13}")
        out.append("")
    tot = lambda f_: sum((f_(c) for c in contracts), ZERO)
    out.append(f"ИТОГО выплачено: {fm(tot(lambda c: c.paid_total))}   "
               f"осталось оплатить: {fm(tot(lambda c: c.remaining))}   "
               f"в очереди: {fm(tot(lambda c: c.queued))}")
    if warnings:
        out += ["", "ЗАМЕЧАНИЯ:"] + [f"  - {w}" for w in warnings]
    return "\n".join(out)


def write_csv(contracts, out_dir, on):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = on.isoformat()
    with open(out_dir / f"works_{tag}.csv", "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["contract_id", "work_id", "title", "status_on_date", "price",
                     "paid", "paid_by_direct_link", "paid_from_contract_advance", "remaining", "overpaid"])
        for c in contracts:
            for w in c.works:
                wr.writerow([c.contract_id, w.work_id, w.title, w.state, w.price, w.paid,
                             w.paid_bound, w.paid_alloc, w.remaining, w.overpaid])
    with open(out_dir / f"contracts_{tag}.csv", "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["contract_id", "contractor", "contract_amount", "active_works_price",
                     "cancelled_works_price", "paid_total", "unallocated_advance",
                     "remaining", "overpaid", "queued"])
        for c in contracts:
            wr.writerow([c.contract_id, c.contractor, c.amount, c.active_price, c.cancelled_price,
                         c.paid_total, c.unallocated, c.remaining, c.overpaid, c.queued])


def main():
    ap = argparse.ArgumentParser(description="Отчёт по договорам и работам на дату")
    ap.add_argument("--date", required=True, help="дата отчёта: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД")
    ap.add_argument("--data", default="data", help="папка с CSV (по умолчанию data)")
    ap.add_argument("--csv-dir", help="если указано - дополнительно сохранить отчёт в CSV")
    a = ap.parse_args()
    on = parse_date(a.date)
    contracts, warnings = build(a.data, on)
    print(render(contracts, warnings, on))
    if a.csv_dir:
        write_csv(contracts, a.csv_dir, on)
        print(f"\nCSV сохранены в {a.csv_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
