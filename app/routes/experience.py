"""体验组队路由"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.experience import experience_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/experience", tags=["experience"])


class ExperienceJoinRequest(BaseModel):
    email: str = Field(..., description="用户邮箱")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def experience_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """体验组队页面"""
    try:
        from app.main import templates

        remaining_spots = await experience_service.get_total_available_slots(db)
        return templates.TemplateResponse(
            request,
            "user/experience.html",
            {
                "request": request,
                "remaining_spots": remaining_spots,
            },
        )
    except Exception:
        logger.exception("渲染体验组队页面失败")
        return HTMLResponse(
            content="<h1>页面加载失败</h1><p>系统暂时不可用，请稍后重试。</p>",
            status_code=500,
        )


@router.get("/spots")
async def get_experience_spots(db: AsyncSession = Depends(get_db)):
    """返回体验池剩余席位"""
    try:
        remaining_spots = await experience_service.get_total_available_slots(db)
        return {"success": True, "remaining_spots": remaining_spots}
    except Exception:
        logger.exception("获取体验池席位失败")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": "获取席位失败，请稍后重试"},
        )


@router.post("/join")
async def join_experience(
    payload: ExperienceJoinRequest,
    db: AsyncSession = Depends(get_db),
):
    """提交邮箱并自动拉入体验组队"""
    try:
        result = await experience_service.join_experience(payload.email, db)

        if result.get("success"):
            return JSONResponse(content=result)

        error_code = result.get("error_code")
        if error_code == "invalid_email":
            status_code = status.HTTP_400_BAD_REQUEST
        elif error_code == "all_full":
            status_code = status.HTTP_409_CONFLICT
        elif error_code == "join_failed":
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

        return JSONResponse(status_code=status_code, content=result)

    except HTTPException:
        raise
    except Exception:
        logger.exception("体验组队拉人失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="体验组队失败，请稍后重试",
        )
