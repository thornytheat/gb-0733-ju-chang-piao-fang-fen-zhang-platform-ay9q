from fastapi import APIRouter, Depends, Request

from app.browse import Browse, snapshot_for

router = APIRouter(prefix='/api/reconcile')


def order_rows(snapshot):
    rows = []
    for order in snapshot['funding'].get('refund_orders', []):
        for line in order['lines']:
            rows.append({**line, 'refund_id': order['refund_id'], 'ticket': order['ticket'],
                         'refunded_at': order['refunded_at'], 'refunded_cents': order['refunded_cents']})
    return rows


@router.get('/{run_id}')
def reconcile(request: Request, run_id: str, browse: Browse = Depends()):
    snapshot = snapshot_for(request, run_id)
    funding = snapshot['funding']
    rows = browse.choose({'accepted': funding['ledger'], 'tickets': funding['tickets'], 'venues': funding['venues'],
                          'shows': funding['shows'], 'orders': order_rows(snapshot), 'sources': funding['sources']})
    return browse.page(rows, totals=funding['totals'], checks=funding['checks'], balanced=funding['balanced'])
