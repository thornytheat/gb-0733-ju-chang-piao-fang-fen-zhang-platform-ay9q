from app.pipeline import compute
from app.modules.m5_refunds.rules import apportion


def with_refunds(files, rows, header_override=None):
    header = header_override or b'refund_id,ticket,show,refunded_at,amount,unit\n'
    body = b''.join(','.join(str(value) for value in row).encode() + b'\n' for row in rows)
    return compute('whole-order', 'unit', {**files, 'refunds.csv': header + body})


def test_whole_order_lands_on_every_show_in_proportion_and_respects_each_cap(files):
    snapshot = with_refunds(files, [('W-1', '00107', '', '2026-11-19T19:30:00+08:00', '5000', '分')])
    refund, = snapshot['refunds']
    assert refund['whole_order'] is True
    rows = {line['show']: line for line in refund['allocations']}
    assert set(rows) == {'A-LAKE', 'B-RAIN', 'C-DUSK'}
    assert sum(line['reversal_cents'] for line in refund['allocations']) == -5000
    assert all(-line['reversal_cents'] <= line['original_cents'] for line in refund['allocations'])
    # 三个场次原分账接近，尾部差异按场次键稳定裁决
    assert [(show, rows[show]['reversal_cents']) for show in ('A-LAKE', 'B-RAIN', 'C-DUSK')] == [
        ('A-LAKE', -1667), ('B-RAIN', -1667), ('C-DUSK', -1666),
    ]
    assert all(line['remaining_cents'] >= 0 for line in refund['allocations'])


def test_whole_order_overflows_exhausted_show_onto_still_available_shows(files):
    """C-DUSK 已退满，整单退款只能落在仍有余额的场次，且不越过 A-LAKE 原分账。"""
    rows = [
        ('C-FULL', '00107', 'C-DUSK', '2026-11-19T18:30:00+08:00', '3333', '分'),
        ('W-2', '00107', '', '2026-11-19T19:30:00+08:00', '5000', '分'),
    ]
    snapshot = with_refunds(files, rows)
    whole = next(row for row in snapshot['refunds'] if row['refund_id'] == 'W-2')
    shares = {line['show']: -line['reversal_cents'] for line in whole['allocations']}
    assert shares == {'A-LAKE': 2500, 'B-RAIN': 2500}
    assert all(line['remaining_cents'] >= 0 for line in whole['allocations'])
    caps = {row['show']: row for row in snapshot['funding']['shows']}
    assert caps['A-LAKE']['refunded_cents'] == 2500
    assert caps['C-DUSK']['refunded_cents'] == 3333


def test_whole_order_never_exceeds_ticket_balance_across_retries_and_revisions(files):
    rows = [
        ('W-A', '00107', '', '2026-11-19T19:30:00+08:00', '5000', '分'),
        ('W-B', '00107', '', '2026-11-19T19:40:00+08:00', '6000', '分'),
    ]
    snapshot = with_refunds(files, rows)
    assert [row['refund_id'] for row in snapshot['refunds']] == ['W-A']
    exceeded = next(row for row in snapshot['quarantine'] if row['key'] == 'W-B')
    assert exceeded['code'] == 'REFUND_CAP_EXCEEDED'
    assert exceeded['detail']['available_cents'] == 5001
    assert snapshot['funding']['balanced']


def test_duplicate_channel_retry_with_new_id_is_not_charged_twice(files):
    when = '2026-11-19T19:30:00+08:00'
    rows = [('W-1', '00107', '', when, '5000', '分'), ('W-RETRY', '00107', '', when, '5000', '分')]
    snapshot = with_refunds(files, rows)
    assert [row['refund_id'] for row in snapshot['refunds']] == ['W-1']
    retry = next(row for row in snapshot['quarantine'] if row['key'] == 'W-RETRY')
    assert retry['code'] == 'REFUND_RETRY' and retry['detail']['retained_refund'] == 'W-1'
    assert snapshot['funding']['totals']['refunded_cents'] == 5000


def test_explicit_show_refund_still_targets_only_that_show(files):
    rows = [('S-1', '00116', 'C-DUSK', '2026-11-19T19:10:00+08:00', '100', '分')]
    snapshot = with_refunds(files, rows)
    refund, = snapshot['refunds']
    assert refund['whole_order'] is False
    assert [(line['show'], line['reversal_cents']) for line in refund['allocations']] == [('C-DUSK', -100)]


def test_show_omitted_column_is_a_whole_order_channel_feed(files):
    header = b'refund_id,ticket,refunded_at,amount,unit\n'
    snapshot = with_refunds(files, [('W-1', '00107', '2026-11-19T19:30:00+08:00', '5000', '分')], header_override=header)
    refund, = snapshot['refunds']
    assert refund['whole_order'] is True and refund['show'] is None
    assert len(refund['allocations']) == 3


def test_failed_revision_keeps_previous_published_refund_result(client, files):
    first = client.post('/api/runs', data={'book': 'whole-ledger'},
                        files=[('files', (name, data, 'text/csv')) for name, data in files.items()])
    assert first.status_code == 201
    run_id = first.json()['run_id']
    before = client.get(f'/api/refunds/{run_id}').json()['items']
    broken = {**files, 'refunds.csv': files['refunds.csv'].replace(
        b'R107-01,00107,C-DUSK,2026-11-19T18:00:00+08:00,10,\xe5\x85\x83',
        b'R107-01,00107,C-DUSK,2026-11-19T18:00:00+08:00,99,\xe5\x85\x83')}
    failed = client.post('/api/runs', data={'book': 'whole-ledger'},
                         files=[('files', (name, data, 'text/csv')) for name, data in broken.items()])
    assert (failed.status_code, failed.json()['code']) == (409, 'REFUND_CHANGED')
    after = client.get(f'/api/refunds/{run_id}').json()['items']
    assert after == before


def test_refund_voucher_downloads_every_landing_with_caps(client):
    response = client.get('/api/evidence/sample/refunds.csv')
    assert response.status_code == 200
    assert response.headers['content-disposition'] == 'attachment; filename="refund-voucher-sample.csv"'
    header, *lines = response.text.lstrip('﻿').splitlines()
    assert header == ('refund_id,ticket,refunded_at,whole_order,refunded_cents,show,title,venue,'
                      'reversal_cents,cap_cents,cumulative_cents,remaining_cents')
    whole = [line.split(',') for line in lines if line.startswith('R107-WHOLE')]
    assert [(row[5], row[8]) for row in whole] == [('A-LAKE', '-2500'), ('B-RAIN', '-2500')]
    assert all(int(row[10]) <= int(row[9]) for row in whole)


def test_reconcile_orders_view_lists_whole_refund_landings(client):
    data = client.get('/api/reconcile/sample', params={'view': 'orders'}).json()
    assert data['total'] == 2
    assert {row['refund_id'] for row in data['items']} == {'R107-WHOLE'}
    assert {(row['show'], row['reversal_cents'], row['refunded_cents']) for row in data['items']} == {
        ('A-LAKE', -2500, 5000), ('B-RAIN', -2500, 5000),
    }


def test_home_shows_refund_landings_venue_net_and_clawback(client):
    home = client.get('/').text
    for text in ('整单退款落场', '各馆净应收与待追回', 'R107-WHOLE', '待追回'):
        assert text in home
    assert '¥50.00' in home and '25.00' in home


def test_apportion_is_stable_conservative_and_capped():
    weights = {'A': 5000, 'B': 3000, 'C': 2000}
    caps = {'A': 5000, 'B': 100, 'C': 2000}
    first = apportion(2100, weights, caps)
    assert first == apportion(2100, weights, caps)
    assert sum(first.values()) == 2100
    assert all(first[show] <= caps[show] for show in weights)
    assert first['B'] == 100
    assert apportion(7101, weights, caps) is None

def test_whole_order_requires_a_cancelled_show_in_the_order(files):
    rows = [('W-1', '00110', '', '2026-11-19T19:30:00+08:00', '100', '分')]
    snapshot = with_refunds(files, rows)
    assert snapshot['refunds'] == []
    issue = next(row for row in snapshot['quarantine'] if row['key'] == 'W-1')
    assert issue['code'] == 'REFUND_NOT_CANCELLED'


def test_blank_show_sentinel_is_treated_as_whole_order(files):
    rows = [('W-1', '00107', '-', '2026-11-19T19:30:00+08:00', '100', '分')]
    snapshot = with_refunds(files, rows)
    refund, = snapshot['refunds']
    assert refund['whole_order'] is True
    assert {line['show'] for line in refund['allocations']} == {'A-LAKE', 'B-RAIN', 'C-DUSK'}


def test_whole_order_beyond_any_remaining_cap_is_isolated_without_consuming(files):
    rows = [('W-1', '00116', '', '2026-11-19T19:30:00+08:00', '10000', '分')]
    snapshot = with_refunds(files, rows)
    assert snapshot['refunds'] == []
    issue = next(row for row in snapshot['quarantine'] if row['key'] == 'W-1')
    assert issue['code'] == 'REFUND_CAP_EXCEEDED'
    assert issue['detail']['available_cents'] == 501
    assert next(row for row in snapshot['funding']['shows'] if row['show'] == 'C-DUSK')['refunded_cents'] == 0
