from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.clock import CutQuery, cut_query, parse_day
from app.errors import Problem
from app.reasons import REASONS
from app.modules.m1_intake.contract import ID, MAX_CENTS
from app.modules.m8_writeoff import service
from app.store import Store, get_store

router = APIRouter(prefix="/api/writeoff", tags=["核销收回"])


def text_id(value: str) -> str:
    value = value.strip()
    if not ID.fullmatch(value):
        raise Problem("INVALID_ID", REASONS["INVALID_ID"])
    return value


def cents(value: int | None) -> int | None:
    if value is not None and not 0 < value <= MAX_CENTS:
        raise Problem("INVALID_AMOUNT", REASONS["INVALID_AMOUNT"])
    return value


def day(value: str):
    try:
        return parse_day(value.strip())
    except ValueError as exc:
        raise Problem("INVALID_DATE", REASONS["INVALID_DATE"]) from exc


class WriteoffRequest(BaseModel):
    source_id: str
    invoice_id: str
    effective: str
    known: str
    voucher_ref: str
    amount_cents: int | None = None
    note: str = ""


class RecoveryRequest(BaseModel):
    source_id: str
    invoice_id: str
    receipt_ref: str
    amount_cents: int
    received_on: str
    note: str = ""


class RewriteoffRequest(BaseModel):
    source_id: str
    recovery_id: str
    voucher_ref: str
    amount_cents: int
    note: str = ""


@router.get("")
def register(source_id: str, cut: CutQuery, store: Store = Depends(get_store)):
    return service.reconcile(store, source_id, cut)


@router.post("")
def create_writeoff(request: WriteoffRequest, store: Store = Depends(get_store)):
    return service.write_off(store, request.source_id, text_id(request.invoice_id),
                             cut_query(request.effective, request.known), text_id(request.voucher_ref),
                             cents(request.amount_cents), request.note.strip())


@router.post("/recoveries")
def create_recovery(request: RecoveryRequest, store: Store = Depends(get_store)):
    return service.recover(store, request.source_id, text_id(request.invoice_id), text_id(request.receipt_ref),
                           cents(request.amount_cents), day(request.received_on), request.note.strip())


@router.post("/rewriteoffs")
def create_rewriteoff(request: RewriteoffRequest, store: Store = Depends(get_store)):
    return service.rewrite_off(store, request.source_id, text_id(request.recovery_id), text_id(request.voucher_ref),
                               cents(request.amount_cents), request.note.strip())
