from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from sqlalchemy import text
from app.utils.response import success_response
from app.extensions import get_db
from app.config import settings

router = APIRouter(tags=["系统状态"])

@router.get("/health")
async def check_health(db: Session = Depends(get_db)):
    """
    服务健康检查
    """
    db_status = "unknown"
    
    try:
        # 使用text()包装SQL语句
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        error_msg = str(e)
        # 截断错误信息，避免太长
        if len(error_msg) > 50:
            error_msg = error_msg[:50] + "..."
        db_status = f"disconnected ({error_msg})"
    
    return success_response(data={
        "status": "healthy",
        "database": db_status,
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "service": "文件闪传系统API",
        "version": settings.APP_VERSION,
        "message": "服务运行正常"
    })
