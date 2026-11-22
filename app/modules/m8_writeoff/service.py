import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from app.clock import utc_text
from app.errors import Problem
from app.evidence import canonical, digest
from app.modules.m1_intake.service import load
from app.modules.m3_snapshot.service import snapshot
from app.modules.m8_writeoff import repository

SUMS = ("pre_balance_cents", "written_cents", "recovered_cents", "rewrite_cents", "balance_cents")


def stamp():
    return utc_text(datetime.now(timezone.utc))


def key(**parts):
    return digest(canonical(parts))


def balances(writeoffs, recoveries, rewriteoffs):
    rewritten_by_recovery = defaultdict(int)
    for row in rewriteoffs:
        rewritten_by_recovery[row["recovery_id"]] += row["amount_cents"]
    recovered, rewritten = defaultdict(int), defaultdict(int)
    for row in recoveries:
        recovered[row["writeoff_id"]] += row["amount_cents"]
        rewritten[row["writeoff_id"]] += rewritten_by_recovery[row["recovery_id"]]
    return {row["writeoff_id"]: {"recovered_cents": recovered[row["writeoff_id"]],
                                 "rewrite_cents": rewritten[row["writeoff_id"]],
                                 "balance_cents": row["written_cents"] - recovered[row["writeoff_id"]]
                                 + rewritten[row["writeoff_id"]]}
            for row in writeoffs}


def register_rows(connection, source_id):
    writeoffs = repository.chains(connection, source_id)
    recoveries = repository.recoveries(connection, source_id)
    rewriteoffs = repository.rewriteoffs(connection, source_id)
    return writeoffs, recoveries, rewriteoffs, balances(writeoffs, recoveries, rewriteoffs)


def same(existing, record, fields):
    return all(existing[field] == record[field] for field in fields)


def yuan(cents):
    return f"¥{cents / 100:,.2f}"


def write_off(store, source_id, invoice_id, cut, voucher_ref, amount_cents, note):
    data = load(store, source_id)
    row = next((item for item in snapshot(data, cut)["invoices"] if item["invoice_id"] == invoice_id), None)
    if row is None:
        raise Problem("INVOICE_NOT_ACTIVE", "发票在该切点无有效版本", 404)
    balance = row["balance_cents"]
    if balance <= 0:
        raise Problem("WRITEOFF_ZERO_BALANCE", "发票余额已结清，无需核销")
    written = balance if amount_cents is None else amount_cents
    if written > balance:
        raise Problem("WRITEOFF_EXCEEDS", f"核销金额超过切点账面余额（可用 {yuan(balance)}）")
    record = {"writeoff_id": key(source_id=source_id, voucher_ref=voucher_ref), "source_id": source_id,
              "invoice_id": invoice_id, "customer_id": row["customer_id"],
              "effective": cut.effective.isoformat(), "known": utc_text(cut.known),
              "pre_balance_cents": balance, "written_cents": written,
              "voucher_ref": voucher_ref, "note": note, "created_at": stamp()}
    fields = ("invoice_id", "customer_id", "effective", "known", "pre_balance_cents", "written_cents", "note")
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = repository.get_writeoff(connection, record["writeoff_id"])
            if existing is not None:
                if same(existing, record, fields):
                    return {"created": False, "writeoff": existing}
                raise Problem("WRITEOFF_CONFLICT", "核销凭证号已被不同内容占用", 409)
            writeoffs = repository.chains(connection, source_id)
            if any(item["invoice_id"] == invoice_id for item in writeoffs):
                raise Problem("WRITEOFF_CHAIN_EXISTS", "该发票已存在核销链，收回后仍未兑现请登记再次核销", 409)
            repository.insert_writeoff(connection, record)
    except sqlite3.Error as exc:
        raise Problem("WRITEOFF_WRITE_FAILED", "核销登记写入失败，请重试", 503) from exc
    return {"created": True, "writeoff": record}


def recover(store, source_id, invoice_id, receipt_ref, amount_cents, received_on, note):
    load(store, source_id)
    record = {"recovery_id": key(source_id=source_id, receipt_ref=receipt_ref), "source_id": source_id,
              "invoice_id": invoice_id, "receipt_ref": receipt_ref, "amount_cents": amount_cents,
              "received_on": received_on.isoformat(), "note": note, "created_at": stamp()}
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = repository.get_recovery(connection, record["recovery_id"])
            if existing is not None:
                if same(existing, record, ("invoice_id", "amount_cents", "received_on", "note")):
                    return {"created": False, "recovery": existing}
                raise Problem("RECOVERY_CONFLICT", "回单号已登记且内容不一致", 409)
            writeoffs, recoveries, rewriteoffs, sums = register_rows(connection, source_id)
            chain = next((item for item in writeoffs if item["invoice_id"] == invoice_id), None)
            if chain is None:
                raise Problem("RECOVERY_NOT_WRITTEN_OFF", "回单发票无核销记录，请按正常收款流程入账")
            available = sums[chain["writeoff_id"]]["balance_cents"]
            if amount_cents > available:
                raise Problem("RECOVERY_EXCEEDS", f"收回金额超过核销链待收余额（可用 {yuan(available)}）")
            record.update({"writeoff_id": chain["writeoff_id"], "customer_id": chain["customer_id"]})
            repository.insert_recovery(connection, record)
    except sqlite3.Error as exc:
        raise Problem("WRITEOFF_WRITE_FAILED", "收回登记写入失败，请重试", 503) from exc
    return {"created": True, "recovery": record}


def rewrite_off(store, source_id, recovery_id, voucher_ref, amount_cents, note):
    load(store, source_id)
    record = {"rewriteoff_id": key(recovery_id=recovery_id, voucher_ref=voucher_ref), "recovery_id": recovery_id,
              "voucher_ref": voucher_ref, "amount_cents": amount_cents, "note": note, "created_at": stamp()}
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = repository.get_rewriteoff(connection, record["rewriteoff_id"])
            if existing is not None:
                if same(existing, record, ("recovery_id", "amount_cents", "note")):
                    return {"created": False, "rewriteoff": existing}
                raise Problem("REWRITE_CONFLICT", "再次核销凭证号已被不同内容占用", 409)
            recovery = repository.get_recovery(connection, recovery_id)
            if recovery is None or recovery["source_id"] != source_id:
                raise Problem("RECOVERY_NOT_FOUND", "收回凭证不存在", 404)
            rewriteoffs = repository.rewriteoffs(connection, source_id)
            used = sum(row["amount_cents"] for row in rewriteoffs if row["recovery_id"] == recovery_id)
            available = recovery["amount_cents"] - used
            if amount_cents > available:
                raise Problem("REWRITE_EXCEEDS", f"再次核销超过该笔收回的未核销余额（可用 {yuan(available)}）")
            repository.insert_rewriteoff(connection, record)
    except sqlite3.Error as exc:
        raise Problem("WRITEOFF_WRITE_FAILED", "再次核销写入失败，请重试", 503) from exc
    return {"created": True, "rewriteoff": record}


def reconcile(store, source_id, cut):
    data = load(store, source_id)
    open_ar = {row["invoice_id"]: row["balance_cents"] for row in snapshot(data, cut)["invoices"]}
    with store.connect() as connection:
        writeoffs, recoveries, rewriteoffs, sums = register_rows(connection, source_id)
    children = defaultdict(list)
    for row in rewriteoffs:
        children[row["recovery_id"]].append(row)
    recovery_views = defaultdict(list)
    for row in recoveries:
        used = sum(item["amount_cents"] for item in children[row["recovery_id"]])
        recovery_views[row["writeoff_id"]].append(
            {**row, "rewrite_cents": used, "remaining_cents": row["amount_cents"] - used,
             "rewriteoffs": children[row["recovery_id"]]})
    chains, customers, sound = [], {}, True
    for row in writeoffs:
        total = sums[row["writeoff_id"]]
        checks = {"written_within_pre": row["written_cents"] <= row["pre_balance_cents"],
                  "recovered_within_written": total["recovered_cents"] <= row["written_cents"],
                  "rewrite_within_recovered": total["rewrite_cents"] <= total["recovered_cents"]}
        sound = sound and all(checks.values())
        chains.append({"writeoff_id": row["writeoff_id"], "invoice_id": row["invoice_id"],
                       "customer_id": row["customer_id"], "voucher_ref": row["voucher_ref"], "note": row["note"],
                       "created_at": row["created_at"], "cut": {"effective": row["effective"], "known": row["known"]},
                       "pre_balance_cents": row["pre_balance_cents"], "written_cents": row["written_cents"], **total,
                       "open": total["balance_cents"] > 0, "checks": checks,
                       "open_ar_cents": open_ar.get(row["invoice_id"], 0),
                       "recoveries": recovery_views[row["writeoff_id"]]})
        customer = customers.setdefault(row["customer_id"],
                                        {"customer_id": row["customer_id"], "chains": 0, **dict.fromkeys(SUMS, 0)})
        customer["chains"] += 1
        customer["pre_balance_cents"] += row["pre_balance_cents"]
        customer["written_cents"] += row["written_cents"]
        for name in ("recovered_cents", "rewrite_cents", "balance_cents"):
            customer[name] += total[name]
    totals = {name: sum(customer[name] for customer in customers.values()) for name in SUMS}
    return {"cut": cut.public(), "source_id": source_id, "status": "MATCHED" if sound else "DIFFERENCE",
            "totals": {**totals, "chains": len(chains), "customers": len(customers),
                       "open_chains": sum(1 for chain in chains if chain["open"])},
            "customers": sorted(customers.values(), key=lambda item: item["customer_id"]), "chains": chains}
