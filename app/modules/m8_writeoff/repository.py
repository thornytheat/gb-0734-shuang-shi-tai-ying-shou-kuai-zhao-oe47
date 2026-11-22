WRITEOFF_FIELDS = ("writeoff_id, source_id, invoice_id, customer_id, effective, known,"
                   " pre_balance_cents, written_cents, voucher_ref, note, created_at")
RECOVERY_FIELDS = ("recovery_id, writeoff_id, source_id, invoice_id, customer_id,"
                   " receipt_ref, amount_cents, received_on, note, created_at")
REWRITE_FIELDS = "rewriteoff_id, recovery_id, voucher_ref, amount_cents, note, created_at"


def insert_writeoff(connection, row):
    cursor = connection.execute(f"INSERT INTO writeoffs ({WRITEOFF_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                "ON CONFLICT(writeoff_id) DO NOTHING", tuple(row[key] for key in WRITEOFF_FIELDS.split(", ")))
    return cursor.rowcount == 1


def get_writeoff(connection, writeoff_id):
    row = connection.execute(f"SELECT {WRITEOFF_FIELDS} FROM writeoffs WHERE writeoff_id=?", (writeoff_id,)).fetchone()
    return dict(row) if row else None


def chains(connection, source_id):
    rows = connection.execute(
        f"SELECT {WRITEOFF_FIELDS} FROM writeoffs WHERE source_id=? ORDER BY created_at, writeoff_id", (source_id,))
    return [dict(row) for row in rows]


def insert_recovery(connection, row):
    cursor = connection.execute(f"INSERT INTO recoveries ({RECOVERY_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                "ON CONFLICT(recovery_id) DO NOTHING", tuple(row[key] for key in RECOVERY_FIELDS.split(", ")))
    return cursor.rowcount == 1


def get_recovery(connection, recovery_id):
    row = connection.execute(f"SELECT {RECOVERY_FIELDS} FROM recoveries WHERE recovery_id=?", (recovery_id,)).fetchone()
    return dict(row) if row else None


def recoveries(connection, source_id):
    rows = connection.execute(
        f"SELECT {RECOVERY_FIELDS} FROM recoveries WHERE source_id=? ORDER BY created_at, recovery_id", (source_id,))
    return [dict(row) for row in rows]


def insert_rewriteoff(connection, row):
    cursor = connection.execute(f"INSERT INTO rewriteoffs ({REWRITE_FIELDS}) VALUES (?, ?, ?, ?, ?, ?) "
                                "ON CONFLICT(rewriteoff_id) DO NOTHING", tuple(row[key] for key in REWRITE_FIELDS.split(", ")))
    return cursor.rowcount == 1


def get_rewriteoff(connection, rewriteoff_id):
    row = connection.execute(f"SELECT {REWRITE_FIELDS} FROM rewriteoffs WHERE rewriteoff_id=?", (rewriteoff_id,)).fetchone()
    return dict(row) if row else None


def rewriteoffs(connection, source_id):
    rows = connection.execute(
        f"SELECT {REWRITE_FIELDS} FROM rewriteoffs WHERE recovery_id IN "
        f"(SELECT recovery_id FROM recoveries WHERE source_id=?) ORDER BY created_at, rewriteoff_id", (source_id,))
    return [dict(row) for row in rows]
