from concurrent.futures import ThreadPoolExecutor

import pytest


def write_off(client, query, invoice, voucher, amount=None):
    body = {'source_id': query['source_id'], 'invoice_id': invoice, 'voucher_ref': voucher,
            'effective': query['effective'], 'known': query['known']}
    if amount is not None:
        body['amount_cents'] = amount
    return client.post('/api/writeoff', json=body)


def recover(client, query, invoice, receipt, amount, received_on='2026-03-15'):
    return client.post('/api/writeoff/recoveries', json={'source_id': query['source_id'], 'invoice_id': invoice,
                                                         'receipt_ref': receipt, 'amount_cents': amount,
                                                         'received_on': received_on})


def rewrite(client, query, recovery_id, voucher, amount):
    return client.post('/api/writeoff/rewriteoffs', json={'source_id': query['source_id'], 'recovery_id': recovery_id,
                                                          'voucher_ref': voucher, 'amount_cents': amount})


def register(client, query):
    reply = client.get('/api/writeoff', params=query)
    assert reply.status_code == 200
    return reply.json()


def chain(data, invoice):
    return next(row for row in data['chains'] if row['invoice_id'] == invoice)


def customer(data, customer_id):
    return next(row for row in data['customers'] if row['customer_id'] == customer_id)


@pytest.fixture
def written_off(client, query):
    # 长期拖欠客户已核销：AR-1006 欠 300.00，AR-1003 欠 900.00，AR-1008 欠 100.40
    for invoice, voucher in (('AR-1006', 'WO-001'), ('AR-1003', 'WO-002'), ('AR-1008', 'WO-003')):
        reply = write_off(client, query, invoice, voucher)
        assert reply.status_code == 200 and reply.json()['created'] is True
    return query


def test_writeoff_captures_pre_balance_and_customer_summary(client, query):
    reply = write_off(client, query, 'AR-1006', 'WO-001')
    assert reply.status_code == 200 and reply.json()['created'] is True
    voucher = reply.json()['writeoff']
    assert (voucher['pre_balance_cents'], voucher['written_cents']) == (30000, 30000)
    assert (voucher['cut'] if 'cut' in voucher else voucher)['effective'] == '2026-01-31'
    data = register(client, query)
    assert data['status'] == 'MATCHED'
    row = customer(data, '06620')
    assert (row['pre_balance_cents'], row['written_cents'], row['balance_cents']) == (30000, 30000, 30000)
    entry = chain(data, 'AR-1006')
    assert entry['open'] is True and entry['open_ar_cents'] == 30000
    assert entry['cut'] == {'effective': '2026-01-31', 'known': '2026-02-06T04:00:00.000000Z'}


def test_partial_writeoff_blocks_second_chain(client, query):
    reply = write_off(client, query, 'AR-1009', 'WO-101', amount=10000)
    assert reply.status_code == 200
    assert reply.json()['writeoff']['pre_balance_cents'] == 20000
    again = write_off(client, query, 'AR-1009', 'WO-102', amount=5000)
    assert (again.status_code, again.json()['code']) == (409, 'WRITEOFF_CHAIN_EXISTS')


def test_writeoff_rejects_settled_unknown_and_exceeding(client, query):
    settled = write_off(client, query, 'AR-1005', 'WO-201')
    missing = write_off(client, query, 'AR-9999', 'WO-202')
    exceeding = write_off(client, query, 'AR-1006', 'WO-203', amount=30001)
    assert (settled.status_code, settled.json()['code']) == (422, 'WRITEOFF_ZERO_BALANCE')
    assert (missing.status_code, missing.json()['code']) == (404, 'INVOICE_NOT_ACTIVE')
    assert (exceeding.status_code, exceeding.json()['code']) == (422, 'WRITEOFF_EXCEEDS')


def test_writeoff_rerun_idempotent_but_changed_content_conflicts(client, query):
    first = write_off(client, query, 'AR-1006', 'WO-001').json()
    rerun = write_off(client, query, 'AR-1006', 'WO-001').json()
    assert rerun['created'] is False and rerun['writeoff']['writeoff_id'] == first['writeoff']['writeoff_id']
    conflict = write_off(client, query, 'AR-1003', 'WO-001')
    assert (conflict.status_code, conflict.json()['code']) == (409, 'WRITEOFF_CONFLICT')
    data = register(client, query)
    assert data['totals']['chains'] == 1 and data['totals']['written_cents'] == 30000


def test_recovery_lands_on_invoice_and_stays_traceable(client, written_off):
    reply = recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000)
    assert reply.status_code == 200 and reply.json()['created'] is True
    recovery = reply.json()['recovery']
    data = register(client, written_off)
    entry = chain(data, 'AR-1006')
    assert recovery['writeoff_id'] == entry['writeoff_id']
    assert entry['recoveries'][0]['receipt_ref'] == 'RCPT-7788'
    assert (entry['recovered_cents'], entry['balance_cents'], entry['open']) == (30000, 0, False)
    row = customer(data, '06620')
    assert (row['pre_balance_cents'], row['written_cents'], row['recovered_cents'], row['balance_cents']) == (30000, 30000, 30000, 0)
    assert data['status'] == 'MATCHED'


def test_recovery_rerun_does_not_restore_twice(client, written_off):
    first = recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000).json()
    assert first['created'] is True
    for _ in range(2):
        rerun = recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000).json()
        assert rerun['created'] is False and rerun['recovery']['recovery_id'] == first['recovery']['recovery_id']
    data = register(client, written_off)
    assert data['totals']['recovered_cents'] == 30000
    assert chain(data, 'AR-1006')['balance_cents'] == 0


def test_recovery_same_receipt_with_different_content_conflicts(client, written_off):
    recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000)
    changed = recover(client, written_off, 'AR-1006', 'RCPT-7788', 20000)
    other_invoice = recover(client, written_off, 'AR-1003', 'RCPT-7788', 10000)
    assert (changed.status_code, changed.json()['code']) == (409, 'RECOVERY_CONFLICT')
    assert (other_invoice.status_code, other_invoice.json()['code']) == (409, 'RECOVERY_CONFLICT')
    assert register(client, written_off)['totals']['recovered_cents'] == 30000


def test_recovery_concurrent_posts_leave_one_restore(client, written_off):
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000), range(4)))
    assert [reply.status_code for reply in replies] == [200] * 4
    assert sum(reply.json()['created'] for reply in replies) == 1
    assert register(client, written_off)['totals']['recovered_cents'] == 30000


def test_recovery_validates_chain_balance(client, written_off):
    over = recover(client, written_off, 'AR-1003', 'RCPT-901', 90001)
    plain = recover(client, written_off, 'AR-1001', 'RCPT-902', 10000)
    assert (over.status_code, over.json()['code']) == (422, 'RECOVERY_EXCEEDS')
    assert (plain.status_code, plain.json()['code']) == (422, 'RECOVERY_NOT_WRITTEN_OFF')
    assert recover(client, written_off, 'AR-1006', 'RCPT-903', 30000).json()['created'] is True
    settled = recover(client, written_off, 'AR-1006', 'RCPT-904', 100)
    assert (settled.status_code, settled.json()['code']) == (422, 'RECOVERY_EXCEEDS')


def test_rewriteoff_keeps_chain_traceable_and_balanced(client, written_off):
    recovery = recover(client, written_off, 'AR-1008', 'RCPT-555', 10040).json()['recovery']
    reply = rewrite(client, written_off, recovery['recovery_id'], 'RWO-1', 4000)
    assert reply.status_code == 200 and reply.json()['created'] is True
    data = register(client, written_off)
    entry = chain(data, 'AR-1008')
    assert (entry['recovered_cents'], entry['rewrite_cents'], entry['balance_cents']) == (10040, 4000, 4000)
    view = entry['recoveries'][0]
    assert view['rewriteoffs'][0]['rewriteoff_id'] == reply.json()['rewriteoff']['rewriteoff_id']
    assert view['rewriteoffs'][0]['recovery_id'] == recovery['recovery_id']
    assert (view['rewrite_cents'], view['remaining_cents']) == (4000, 6040)
    rerun = rewrite(client, written_off, recovery['recovery_id'], 'RWO-1', 4000).json()
    assert rerun['created'] is False
    over = rewrite(client, written_off, recovery['recovery_id'], 'RWO-2', 6041)
    missing = rewrite(client, written_off, 'x' * 64, 'RWO-3', 100)
    assert (over.status_code, over.json()['code']) == (422, 'REWRITE_EXCEEDS')
    assert (missing.status_code, missing.json()['code']) == (404, 'RECOVERY_NOT_FOUND')
    assert register(client, written_off)['status'] == 'MATCHED'


def test_recovery_never_returns_invoice_to_aging(client, written_off, params):
    source = {'source_id': written_off['source_id']}
    before = {name: client.get(f'/api/{name}', params={**written_off}).json() for name in ('snapshot', 'aging')}
    recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000)
    recover(client, written_off, 'AR-1003', 'RCPT-7799', 50000)
    after = {name: client.get(f'/api/{name}', params={**written_off}).json() for name in ('snapshot', 'aging')}
    assert after == before
    assert after['aging']['total_cents'] == 340065
    entry = chain(register(client, written_off), 'AR-1006')
    assert entry['open_ar_cents'] == 30000


def test_tampered_recovery_breaks_reconciliation(client, written_off):
    recovery = recover(client, written_off, 'AR-1006', 'RCPT-7788', 30000).json()['recovery']
    store = client.app.state.store
    with store.connect() as connection:
        connection.execute('UPDATE recoveries SET amount_cents = amount_cents + 1 WHERE recovery_id=?',
                           (recovery['recovery_id'],))
    data = register(client, written_off)
    assert data['status'] == 'DIFFERENCE'
    assert chain(data, 'AR-1006')['checks']['recovered_within_written'] is False


def test_register_isolated_per_source(client, query, branch_raw):
    other = client.post('/api/intake', files={'file': ('branch.csv', branch_raw)}).json()['source_id']
    write_off(client, query, 'AR-1006', 'WO-001')
    data = client.get('/api/writeoff', params={**query, 'source_id': other}).json()
    assert data['status'] == 'MATCHED' and data['chains'] == [] and data['totals']['written_cents'] == 0
    stray = recover(client, {**query, 'source_id': other}, 'AR-1006', 'RCPT-7788', 30000)
    assert (stray.status_code, stray.json()['code']) == (422, 'RECOVERY_NOT_WRITTEN_OFF')


def test_request_validation_and_missing_source(client, query):
    bad_ref = write_off(client, query, 'AR-1006', 'bad ref!')
    zero = write_off(client, query, 'AR-1006', 'WO-001', amount=0)
    bad_day = client.post('/api/writeoff/recoveries', json={'source_id': query['source_id'], 'invoice_id': 'AR-1006',
                                                            'receipt_ref': 'RCPT-1', 'amount_cents': 100,
                                                            'received_on': '2026-13-01'})
    missing = [client.get('/api/writeoff', params={**query, 'source_id': 'missing'}),
               write_off(client, {**query, 'source_id': 'missing'}, 'AR-1006', 'WO-001'),
               recover(client, {**query, 'source_id': 'missing'}, 'AR-1006', 'RCPT-1', 100)]
    assert (bad_ref.status_code, bad_ref.json()['code']) == (422, 'INVALID_ID')
    assert (zero.status_code, zero.json()['code']) == (422, 'INVALID_AMOUNT')
    assert (bad_day.status_code, bad_day.json()['code']) == (422, 'INVALID_DATE')
    assert [(reply.status_code, reply.json()['code']) for reply in missing] == [(404, 'SOURCE_NOT_FOUND')] * 3


def test_workbench_serves_writeoff_page(client):
    reply = client.get('/')
    assert reply.status_code == 200
    assert 'data-page="writeoff"' in reply.text and 'href="#writeoff"' in reply.text
