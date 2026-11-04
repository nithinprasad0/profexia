import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal, engine
from app.domain.models import Role


ROLES = {
    "learner": "Platform learner",
    "teacher": "Platform teacher",
    "admin": "Platform administrator",
}


async def seed():
    async with SessionLocal() as session:
        for name, description in ROLES.items():
            existing = await session.scalar(select(Role).where(Role.name == name))
            if existing is None:
                session.add(Role(name=name, description=description))
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
