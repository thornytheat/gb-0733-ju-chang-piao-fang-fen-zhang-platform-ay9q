from fastapi import APIRouter, Depends, Request, Response

from app.browse import Browse, isolated, snapshot_for
from app.modules.m7_evidence.bundle import build_bundle
from app.modules.m7_evidence.voucher import refund_voucher

router = APIRouter(prefix='/api/evidence')


@router.get('/{run_id}')
def evidence(request: Request, run_id: str, browse: Browse = Depends()):
    snapshot = snapshot_for(request, run_id)
    rows = browse.choose({'accepted': isolated(snapshot), 'inputs': snapshot['manifest']['inputs']})
    return browse.page(rows, manifest=snapshot['manifest'], reason_counts=snapshot['reason_counts'],
                       refund_orders=snapshot['funding'].get('refund_orders', []))


@router.get('/{run_id}/export')
def export_bundle(request: Request, run_id: str):
    store = request.app.state.store
    snapshot = store.get(run_id)
    original_id = snapshot['manifest']['origin_run']
    data = build_bundle(snapshot, store.inputs(run_id), store.inputs(original_id))
    return Response(data, media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="theatre-{run_id}.zip"'})


@router.get('/{run_id}/refunds.csv')
def export_refunds(request: Request, run_id: str):
    """本期退款凭证：每笔退款落在哪些场、各场冲正与剩余额度。"""
    body = refund_voucher(snapshot_for(request, run_id))
    return Response(body, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="refund-voucher-{run_id}.csv"'})
