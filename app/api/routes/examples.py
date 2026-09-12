from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import AuthenticatedUser, get_current_user
from app.db.session import get_db_session
from app.models.example import ExampleRecord
from app.schemas.example import ExampleCreate, ExampleRead

router = APIRouter()


@router.get("", response_model=list[ExampleRead], summary="List the current user's records")
async def list_examples(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ExampleRecord]:
    result = await session.scalars(
        select(ExampleRecord)
        .where(ExampleRecord.owner_id == user.subject)
        .order_by(ExampleRecord.id.desc())
    )
    return list(result)


@router.post(
    "",
    response_model=ExampleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a neutral REST example record",
)
async def create_example(
    payload: ExampleCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ExampleRecord:
    record = ExampleRecord(name=payload.name, owner_id=user.subject)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record
