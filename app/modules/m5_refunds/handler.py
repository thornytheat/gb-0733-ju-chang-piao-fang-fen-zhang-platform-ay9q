from fastapi import APIRouter, Depends, Request

from app.browse import Browse, isolated, snapshot_for

router = APIRouter(prefix='/api/refunds')


def landing_rows(snapshot):
    rows = []
    for refund in snapshot['refunds']:
        for line in refund.get('allocations', []):
            rows.append({**line, 'refund_id': refund['refund_id'], 'refunded_at': refund['refunded_at'],
                         'refunded_cents': -refund['reversal_cents'], 'whole_order': refund.get('whole_order', False)})
    return rows


@router.get('/{run_id}')
def refunds(request: Request, run_id: str, browse: Browse = Depends()):
    snapshot = snapshot_for(request, run_id)
    rows = browse.choose({'accepted': snapshot['refunds'], 'shares': landing_rows(snapshot),
                          'isolated': isolated(snapshot, 'refunds.csv')})
    return browse.page(rows, original=snapshot['publication']['publication_id'])
