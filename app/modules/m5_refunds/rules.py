def check_refund(event, tickets, shows, originals, running):
    if event['ticket'] not in tickets:
        return 'TICKET_UNKNOWN', {}
    if event['show'] is None:
        return _check_order(event, tickets, shows, originals, running)
    return _check_show(event, tickets, shows, originals, running)


def _check_show(event, tickets, shows, originals, running):
    ticket, show = event['ticket'], event['show']
    pair = (ticket, show)
    if show not in shows:
        return 'SHOW_UNKNOWN', {}
    original = originals.get(pair)
    if not original:
        return 'REFUND_NO_ORIGINAL', {}
    cancellation = shows[show]['cancelled_at']
    if cancellation is None:
        return 'REFUND_NOT_CANCELLED', {}
    if event['refunded_at'] < max(cancellation, tickets[ticket]['paid_at']):
        return 'REFUND_TOO_EARLY', {'cancelled_at': cancellation}
    cap = original['allocated_cents']
    used = running.get(pair, 0)
    detail = {'original_cents': cap, 'used_cents': used, 'available_cents': cap - used,
              'requested_cents': event['requested_cents']}
    if event['requested_cents'] > detail['available_cents']:
        return 'REFUND_CAP_EXCEEDED', detail
    return None, detail


def _check_order(event, tickets, shows, originals, running):
    """整单退款按原分账比例落场，单场不超过其剩余额度。

    渠道只回传票码和退款额，场次留空。可落场次是这张票在发布时取得原分账的全部场次。
    只要其中一个场次已停演，且退款不早于停演与收款，整单退款即可入账。落场按各场
    原分账金额等比例分摊；某场剩余额度不足时，超出部分只在仍有余量的场次间按原分账
    比例继续分摊。每场累计冲正永不超过自己的原分账，所以退款不会越过原分账余额，
    也不会让未演或后段场次替已演场次承担整笔退款。
    """
    ticket = event['ticket']
    pairs = sorted(pair for pair in originals if pair[0] == ticket)
    if not pairs:
        return 'REFUND_NO_ORIGINAL', {}
    cancellations = [shows[show]['cancelled_at'] for _, show in pairs if shows[show]['cancelled_at']]
    if not cancellations:
        return 'REFUND_NOT_CANCELLED', {}
    trigger = min(cancellations)
    if event['refunded_at'] < max(trigger, tickets[ticket]['paid_at']):
        return 'REFUND_TOO_EARLY', {'cancelled_at': trigger}
    caps = {show: originals[(ticket, show)]['allocated_cents'] - running.get((ticket, show), 0)
            for _, show in pairs}
    detail = {'original_cents': sum(originals[pair]['allocated_cents'] for pair in pairs),
              'available_cents': sum(caps.values()), 'requested_cents': event['requested_cents'],
              'cancelled_at': trigger, 'remaining_by_show': caps}
    if event['requested_cents'] > detail['available_cents']:
        return 'REFUND_CAP_EXCEEDED', detail
    weights = {show: originals[(ticket, show)]['allocated_cents'] for _, show in pairs}
    shares = apportion(event['requested_cents'], weights, caps)
    if shares is None:  # 已由可用额度挡住，保留防守分支避免静默少冲
        return 'REFUND_CAP_EXCEEDED', detail
    detail['shares'] = {show: value for show, value in shares.items() if value}
    return None, detail


def apportion(total, weights, caps):
    """按权重分整分，单场上限为 caps；超额只在仍有余量的场次间按权重再分。

    每轮用最大余数法取整，余数相同按场次键升序，保证同一输入的结果稳定。
    """
    if total > sum(caps.values()):
        return None
    result = dict.fromkeys(weights, 0)
    remaining = total
    active = sorted(show for show in weights if caps[show] > 0 and weights[show] > 0)
    while remaining > 0 and active:
        denominator = sum(weights[show] for show in active)
        floors = {show: remaining * weights[show] // denominator for show in active}
        leftover = remaining - sum(floors.values())
        order = sorted(active, key=lambda show: (-(remaining * weights[show] % denominator), show))
        for show in order[:leftover]:
            floors[show] += 1
        progressed = 0
        for show in active:
            give = min(floors[show], caps[show] - result[show])
            result[show] += give
            remaining -= give
            progressed += give
        active = [show for show in active if result[show] < caps[show]]
        if not progressed:
            break
    return result if remaining == 0 else None
