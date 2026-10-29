from fastapi import APIRouter

from medwear_med_api.routers.batches import router as batches_router
from medwear_med_api.routers.export import router as export_router
from medwear_med_api.routers.files import router as files_router
from medwear_med_api.routers.gdpr import router as gdpr_router
from medwear_med_api.routers.patients import router as patients_router
from medwear_med_api.routers.admin import router as admin_router

router = APIRouter(prefix="/v1")
router.include_router(admin_router)
router.include_router(files_router)
router.include_router(patients_router)
router.include_router(batches_router)
router.include_router(gdpr_router)
router.include_router(export_router)
