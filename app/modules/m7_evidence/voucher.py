from app.modules.m7_evidence.csv_out import make_csv

FIELDS = ('refund_id', 'ticket', 'refunded_at', 'whole_order', 'refunded_cents', 'show', 'title', 'venue',
          'reversal_cents', 'cap_cents', 'cumulative_cents', 'remaining_cents')


def refund_allocation_rows(snapshot):
    """本期每笔退款落在哪些场，供财务下载核对。"""
    shows = {show['show']: show for show in snapshot['shows']}
    rows = []
    for refund in snapshot['refunds']:
        for line in refund.get('allocations') or legacy_line(refund):
            show = shows[line['show']]
            rows.append({
                'refund_id': refund['refund_id'], 'ticket': refund['ticket'],
                'refunded_at': refund['refunded_at'], 'whole_order': refund.get('whole_order', False),
                'refunded_cents': -refund['reversal_cents'], 'show': line['show'],
                'title': show['title'], 'venue': show['venue'],
                'reversal_cents': line['reversal_cents'], 'cap_cents': line['original_cents'],
                'cumulative_cents': line.get('cumulative_cents'), 'remaining_cents': line.get('remaining_cents'),
            })
    return rows


def legacy_line(refund):
    """旧快照只有单据级场次，没有逐场落点时按原分账回填。"""
    if refund.get('show') is None:
        return []
    return [{'show': refund['show'], 'reversal_cents': refund['reversal_cents'],
             'original_cents': refund.get('original_cents'), 'cumulative_cents': refund.get('cumulative_cents'),
             'remaining_cents': refund.get('remaining_cents')}]


def refund_voucher(snapshot):
    """本期退款凭证 CSV，逐笔逐场列出冲正额与剩余额度。"""
    return make_csv(refund_allocation_rows(snapshot), FIELDS)
