from fastapi import APIRouter
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.learning import router as learning_router
from app.api.v1.assessment import router as assessment_router
from app.api.v1.realtime import router as realtime_router
from app.api.v1.providers import router as providers_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(learning_router)
router.include_router(assessment_router)
router.include_router(realtime_router)
router.include_router(providers_router)
