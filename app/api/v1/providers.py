import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


from app.api.v1.access import CLOSED_SESSION_STATUSES, can_access_learning_session
from app.api.v1.users import get_current_user
from app.core import rate_limit
from app.core.config import settings
from app.core.database import get_db
from app.domain.models import CodeExecution, LearningSession, LiveKitTokenAudit, PaymentOrder, User, Enrollment
from app.infrastructure.providers.judge0 import Judge0Client
from app.infrastructure.providers.livekit import LiveKitClient
from app.infrastructure.providers.razorpay import RazorpayClient, ProviderConfigurationError
from app.schemas.providers import (
    CodeExecutionCreate, CodeExecutionResponse, LiveKitTokenRequest,
    LiveKitTokenResponse, PaymentOrderCreate, PaymentOrderResponse,
)

router = APIRouter(prefix="/api/v1", tags=["external-providers"])
logger = logging.getLogger("profexia.providers")

@router.post("/payments/orders", response_model=PaymentOrderResponse, status_code=status.HTTP_201_CREATED)
async def create_payment_order(
    payload: PaymentOrderCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        provider_order = await RazorpayClient().create_order(
            payload.amount, payload.currency, payload.receipt, payload.notes
        )
    except ProviderConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        # Log the exception type only: provider errors can echo request data.
        logger.warning("Razorpay order creation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Razorpay order creation failed")

    order = PaymentOrder(
        user_id=user.id,
        provider="razorpay",
        provider_order_id=str(provider_order["id"]),
        amount=int(provider_order["amount"]),
        currency=str(provider_order["currency"]),
        status="created",
        receipt=provider_order.get("receipt"),
        extra_metadata={"notes": payload.notes or {}},
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order

@router.post("/payments/webhooks/razorpay")
async def razorpay_webhook(request: Request, session: AsyncSession = Depends(get_db)):
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        valid = RazorpayClient.verify_webhook_signature(raw, signature)
    except ProviderConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid Razorpay webhook signature")

    payload = await request.json()
    event = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    provider_order_id = entity.get("order_id")
    if provider_order_id:
        order = await session.scalar(
            select(PaymentOrder).where(PaymentOrder.provider_order_id == provider_order_id)
        )
        if order:
            order.status = "paid" if event in {"payment.captured", "order.paid"} else event or order.status
            order.extra_metadata = {"event": event, "payment_id": entity.get("id")}
            await session.commit()
    return {"received": True}

@router.post("/code-executions", response_model=CodeExecutionResponse, status_code=status.HTTP_201_CREATED)
async def execute_code(
    payload: CodeExecutionCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Protect the paid Judge0 quota from a single account.
    await rate_limit.enforce(
        "code-exec:user", str(user.id),
        settings.rate_limit_code_executions_per_user, settings.rate_limit_code_execution_window_seconds,
        "Code execution limit reached. Please try again later.",
    )
    try:
        result = await Judge0Client().create_submission(payload.language_id, payload.source_code, payload.stdin)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.warning("Judge0 submission failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Judge0 execution failed")

    execution = CodeExecution(
        user_id=user.id, provider="judge0",
        provider_submission_id=result.get("token"),
        language_id=payload.language_id,
        source_code=payload.source_code, stdin=payload.stdin,
        status=(result.get("status") or {}).get("description", "queued"),
        stdout=result.get("stdout"), stderr=result.get("stderr"),
        compile_output=result.get("compile_output"),
        time=result.get("time"), memory=result.get("memory"),
    )
    session.add(execution)
    await session.commit()
    await session.refresh(execution)
    return execution

@router.get("/code-executions/{execution_id}", response_model=CodeExecutionResponse)
async def get_code_execution(
    execution_id: int,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    execution = await session.get(CodeExecution, execution_id)
    if execution is None or execution.user_id != user.id and user.role.name != "admin":
        raise HTTPException(status_code=404, detail="Code execution not found")
    if execution.provider_submission_id and execution.status in {"queued", "In Queue", "Processing"}:
        try:
            result = await Judge0Client().get_submission(execution.provider_submission_id)
            execution.status = (result.get("status") or {}).get("description", execution.status)
            execution.stdout = result.get("stdout")
            execution.stderr = result.get("stderr")
            execution.compile_output = result.get("compile_output")
            execution.time = result.get("time")
            execution.memory = result.get("memory")
            await session.commit()
            await session.refresh(execution)
        except Exception:
            pass
    return execution

def livekit_room_name(session_id: int) -> str:
    """The only LiveKit room a learning session's tokens may grant."""
    return f"profexia-session-{session_id}"


@router.post("/learning-sessions/{session_id}/livekit-token", response_model=LiveKitTokenResponse)
async def create_livekit_token(
    session_id: int,
    payload: LiveKitTokenRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    obj = await db.get(LearningSession, session_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Learning session not found")
    if not await can_access_learning_session(db, obj, user):
        raise HTTPException(status_code=403, detail="You are not a participant of this learning session")
    if obj.status in CLOSED_SESSION_STATUSES:
        raise HTTPException(status_code=409, detail=f"Learning session is {obj.status}")

    room_name = livekit_room_name(obj.id)
    if payload.room_name is not None and payload.room_name != room_name:
        raise HTTPException(status_code=403, detail="room_name does not belong to this learning session")

    # Only the session host (the course teacher) and admins may publish media.
    # Learners always receive subscribe-only tokens, whatever they request.
    can_publish = user.role.name == "admin" or user.id == obj.host_id
    try:
        token = LiveKitClient().create_access_token(str(user.id), room_name, can_publish, True, payload.ttl_minutes)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    audit = LiveKitTokenAudit(user_id=user.id, room_name=room_name, identity=str(user.id), role=user.role.name)
    db.add(audit)
    await db.commit()
    return LiveKitTokenResponse(
        livekit_url=settings.livekit_url,
        room_name=room_name,
        identity=str(user.id),
        token=token,
        expires_in_minutes=payload.ttl_minutes,
    )
