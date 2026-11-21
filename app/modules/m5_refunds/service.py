from app.contracts import cents, key, optional_key, timestamp
from app.deduplicate import unique_records
from app.errors import Problem, RowError
from app.modules.m5_refunds.rules import check_refund


def refund_records(rows, isolates):
    for row in rows:
        values = row.values
        try:
            entry = {field: key(values[field]) for field in ('refund_id', 'ticket')}
            entry.update(show=optional_key(values.get('show', '')), refunded_at=timestamp(values['refunded_at']),
                         requested_cents=cents(values['amount'], values['unit']))
        except RowError as exc:
            isolates.add(row, exc.code, values['refund_id'])
            continue
        yield entry, row


def reverse(rows, tickets, shows, publication, isolates):
    originals = {(row['ticket'], row['show']): row for row in publication['allocations']}
    running, approved, retries = {}, [], {}
    candidates = sorted(unique_records(refund_records(rows, isolates), lambda item: item['refund_id'],
                                       'REFUND_ID_DUPLICATE', 'REFUND_ID_CONFLICT', isolates),
                        key=lambda item: (item['refunded_at'], item['refund_id']))
    for event in candidates:
        # 渠道整单退款只回传票码和金额，重试可能换单号重送；同一票、同刻、同额的整单退款只入账一次。
        signature = (event['ticket'], event['refunded_at'], event['requested_cents']) if event['show'] is None else None
        if signature is not None and signature in retries:
            isolates.add_record(event, 'REFUND_RETRY', event['refund_id'], retained_refund=retries[signature])
            continue
        code, detail = check_refund(event, tickets, shows, originals, running)
        if code:
            isolates.add_record(event, code, event['refund_id'], **detail)
            continue
        approved.append(settle(event, detail, shows, originals, running, publication))
        if signature is not None:
            retries[signature] = event['refund_id']
    return approved


def settle(event, detail, shows, originals, running, publication):
    """把一笔退款落到各场，并登记该场的累计冲正；同一场只累加一次。"""
    ticket = event['ticket']
    single = event['show'] is not None
    shares = {event['show']: event['requested_cents']} if single else detail['shares']
    allocations = []
    for show in sorted(shares):
        pair = (ticket, show)
        original = originals[pair]
        used = running.get(pair, 0)
        running[pair] = used + shares[show]
        allocations.append({
            'ticket': ticket, 'show': show, 'venue': shows[show]['venue'],
            'original_cents': original['allocated_cents'], 'used_cents': used,
            'reversal_cents': -shares[show], 'cumulative_cents': running[pair],
            'remaining_cents': original['allocated_cents'] - running[pair],
            'right_line': original['right_line'], 'show_line': shows[show]['line'],
        })
    if single:
        line = allocations[0]
        summary = {'original_cents': line['original_cents'], 'cumulative_cents': line['cumulative_cents'],
                   'remaining_cents': line['remaining_cents'], 'right_line': line['right_line'],
                   'show_line': line['show_line']}
    else:
        ticket_cap = sum(original['allocated_cents'] for pair, original in originals.items() if pair[0] == ticket)
        ticket_used = sum(running[pair] for pair in running if pair[0] == ticket)
        summary = {'original_cents': ticket_cap, 'cumulative_cents': ticket_used,
                   'remaining_cents': ticket_cap - ticket_used}
    summary.update(used_cents=detail['used_cents'] if single else detail['original_cents'] - detail['available_cents'],
                   available_cents=detail['available_cents'])
    return {**event, **detail, **summary, 'reversal_cents': -event['requested_cents'],
            'publication_id': publication['publication_id'],
            'cancelled_at': shows[event['show']]['cancelled_at'] if single else detail['cancelled_at'],
            'whole_order': not single, 'allocations': allocations}


def refund_signature(event):
    """已过账冲正的业务事实与逐场落点；旧快照没有逐场落点时退回单据级场次。"""
    allocations = event.get('allocations') or [{
        'ticket': event['ticket'], 'show': event['show'],
        'reversal_cents': event.get('reversal_cents'),
    }]
    return (event['ticket'], event.get('show'), event['refunded_at'], event.get('reversal_cents'),
            event.get('publication_id'), event.get('cancelled_at'),
            tuple(sorted((line['show'], line['reversal_cents']) for line in allocations)))


def preserve_approved(prior, proposed):
    """已过账的冲正不能被旧版或改写后的输入清除，逐场落点也必须一致。"""
    by_id = {row['refund_id']: refund_signature(row) for row in proposed}
    for event in prior:
        if by_id.get(event['refund_id']) != refund_signature(event):
            raise Problem(409, 'REFUND_CHANGED', {'refund_id': event['refund_id']})
