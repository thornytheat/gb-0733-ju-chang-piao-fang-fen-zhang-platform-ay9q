from collections import Counter

from app.modules.m6_reconcile.ledger import settlement_rows

MONEY_COLUMNS = ('allocated_cents', 'refunded_cents', 'net_cents', 'payable_cents', 'unsettled_cents')


def summarize_sides(shows, scans, ledger):
    scan_counts = Counter(row['show'] for row in scans)
    by_show = {key: {**{field: show[field] for field in ('show', 'title', 'venue', 'cancelled_at')},
                     **dict.fromkeys(MONEY_COLUMNS, 0), 'scan_count': scan_counts[key], 'shares': 0,
                     'clawback_cents': 0}
               for key, show in sorted(shows.items())}
    for row in ledger:
        summary = by_show[row['show']]
        summary['shares'] += 1
        for field in MONEY_COLUMNS:
            summary[field] += row[field]
        # 已演出并已结算的场次被冲正的部分，需要向该场馆追回；未演或停演场次只减少未结。
        if row['scan_id'] and not row['cancelled_at']:
            summary['clawback_cents'] += row['refunded_cents']
    by_venue = {}
    for summary in by_show.values():
        summary['gap_cents'] = summary['allocated_cents'] - summary['payable_cents']
        summary['difference_cents'] = summary['net_cents'] - summary['payable_cents'] - summary['unsettled_cents']
        venue = by_venue.setdefault(summary['venue'], {'venue': summary['venue'], 'shows': 0, 'scan_count': 0,
                                                       **dict.fromkeys(MONEY_COLUMNS, 0), 'clawback_cents': 0})
        for field in (*MONEY_COLUMNS, 'scan_count', 'clawback_cents'):
            venue[field] += summary[field]
        venue['shows'] += 1
    return list(by_show.values()), list(by_venue.values())


def funding_checks(tickets, refunds, ledger, unassigned):
    refunds_by_ticket, payable_by_ticket, pending_by_ticket = Counter(), Counter(), Counter()
    for entry in refunds:
        refunds_by_ticket[entry['ticket']] += -entry['reversal_cents']
    for row in ledger:
        payable_by_ticket[row['ticket']] += row['payable_cents']
        pending_by_ticket[row['ticket']] += row['unsettled_cents']
    for row in unassigned:
        pending_by_ticket[row['ticket']] += row['unsettled_cents']
    entries = []
    for key, ticket in sorted(tickets.items()):
        net = ticket['paid_cents'] - refunds_by_ticket[key]
        entries.append({'ticket': key, 'sale_line': ticket['line'], 'paid_cents': ticket['paid_cents'],
                        'refunded_cents': refunds_by_ticket[key], 'net_cents': net,
                        'payable_cents': payable_by_ticket[key], 'unsettled_cents': pending_by_ticket[key],
                        'difference_cents': net - payable_by_ticket[key] - pending_by_ticket[key]})
    return entries


def count_sources(stats, accepted, isolates):
    issue_count = Counter(entry['source'] for entry in isolates)
    counts = []
    for source, facts in stats.items():
        explained = accepted[source] + issue_count[source] + facts['blank'] + facts['headers']
        counts.append({'source': source, **facts, 'accepted': accepted[source], 'isolated': issue_count[source],
                       'difference': facts['rows'] - explained})
    return counts


def refund_breakdown(refunds, shows):
    """整单退款的逐场落点汇总，供页面和接口显示与下载。"""
    orders = []
    for refund in refunds:
        if not refund.get('whole_order'):
            continue
        lines = [{'show': line['show'], 'title': shows[line['show']]['title'],
                  'venue': shows[line['show']]['venue'], 'refunded_cents': -line['reversal_cents'],
                  'reversal_cents': line['reversal_cents'], 'cumulative_cents': line['cumulative_cents'],
                  'remaining_cents': line['remaining_cents'], 'cap_cents': line['original_cents']}
                 for line in refund.get('allocations', [])]
        orders.append({'refund_id': refund['refund_id'], 'ticket': refund['ticket'],
                       'refunded_at': refund['refunded_at'], 'refunded_cents': -refund['reversal_cents'],
                       'cancelled_at': refund.get('cancelled_at'), 'lines': lines})
    return orders


def reconcile(tickets, shows, scans, refunds, publication, sources):
    ledger = settlement_rows(publication, shows, scans, refunds)
    unassigned = publication['unassigned']
    by_show, by_venue = summarize_sides(shows, scans, ledger)
    by_ticket = funding_checks(tickets, refunds, ledger, unassigned)
    paid = sum(row['paid_cents'] for row in by_ticket)
    refunded = sum(-row['reversal_cents'] for row in refunds)
    allocated = sum(row['allocated_cents'] for row in publication['allocations'])
    unallocated = sum(row['unsettled_cents'] for row in unassigned)
    payable = sum(row['payable_cents'] for row in by_venue)
    unsettled = sum(row['unsettled_cents'] for row in by_venue) + unallocated
    clawback = sum(row['clawback_cents'] for row in by_venue)
    whole = sum(-row['reversal_cents'] for row in refunds if row.get('whole_order'))
    difference = paid - refunded - payable - unsettled
    checks = {
        'source_rows': all(row['difference'] == 0 for row in sources),
        'original_allocation': paid == allocated + unallocated,
        'reversal_cap': all(0 <= row['refunded_cents'] <= row['allocated_cents'] for row in ledger)
                        and sum(row['refunded_cents'] for row in ledger) == refunded,
        'refund_shares': all(
            sum(-line['reversal_cents'] for line in row.get('allocations', [])) == -row['reversal_cents']
            and len({line['show'] for line in row.get('allocations', [])}) == len(row.get('allocations', []))
            for row in refunds),
        'show_balances': all(row['difference_cents'] == 0 for row in by_show),
        'ticket_balances': all(row['difference_cents'] == 0 for row in by_ticket),
        'funding': difference == 0 and sum(row['payable_cents'] for row in by_ticket) == payable,
    }
    totals = {'paid_cents': paid, 'allocated_cents': allocated, 'unallocated_cents': unallocated,
              'refunded_cents': refunded, 'net_cents': paid - refunded, 'payable_cents': payable,
              'unsettled_cents': unsettled, 'difference_cents': difference,
              'gap_cents': allocated - payable, 'clawback_cents': clawback,
              'whole_refund_cents': whole, 'whole_refund_count': sum(1 for row in refunds if row.get('whole_order')),
              'scan_count': len(scans), 'ticket_count': len(tickets)}
    return {'balanced': all(checks.values()), 'checks': checks, 'totals': totals, 'ledger': ledger,
            'shows': by_show, 'venues': by_venue, 'tickets': by_ticket, 'sources': sources,
            'refund_orders': refund_breakdown(refunds, shows)}
