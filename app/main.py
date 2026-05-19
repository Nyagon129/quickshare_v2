from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from .config import settings
from .extensions import engine
from .extensions import SessionLocal
from contextlib import asynccontextmanager
from app.models import Base
import app.routes.health as health_router
import app.routes.codes as codes_router
import app.routes.relay as relay_router
import app.routes.reports as reports_router
import app.routes.auth as auth_router
import logging
import os
import socket
import asyncio
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 配置访问日志过滤器（过滤频繁请求的日志）
class AccessLogFilter(logging.Filter):
    """过滤频繁请求的访问日志"""
    # 需要过滤的路径模式
    FILTERED_PATTERNS = [
        r'/status',  # 状态查询接口
        r'/health',  # 健康检查
        r'/upload-chunk',  # 文件块上传接口
        r'/download-chunk',  # 文件块下载接口
    ]
    
    def filter(self, record):
        # 检查日志消息是否包含被过滤的路径
        message = record.getMessage()
        for pattern in self.FILTERED_PATTERNS:
            if re.search(pattern, message):
                return False  # 过滤掉这条日志
        return True  # 保留其他日志

# 应用启动后配置日志过滤器
access_filter = AccessLogFilter()
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(access_filter)
logger.info("访问日志过滤器已配置：将过滤状态查询、上传/下载块等频繁请求的日志")

# 安全检查：启动时验证关键配置
def _startup_security_check():
    warnings = []
    if settings.JWT_SECRET_KEY == "change-me-in-production":
        warnings.append("JWT_SECRET_KEY 使用默认值，生产环境请务必更改！")
    if not settings.DEDUPE_PEPPER:
        warnings.append("DEDUCE_PEPPER 未设置，去重指纹保护较弱，建议在 .env 中配置随机值")
    if warnings:
        logger.warning("=" * 50)
        logger.warning("⚠ 安全配置警告:")
        for w in warnings:
            logger.warning(f"  - {w}")
        logger.warning("=" * 50)

_startup_security_check()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    - 启动时：创建数据库表（如果不存在）、启动后台任务
    - 关闭时：清理资源、停止后台任务
    
    注意：数据库环境检查已在启动脚本中完成，这里假设环境已就绪
    """
    
    # 启动时：创建数据库表
    # 注意：数据库环境检查已在启动脚本中完成，这里应该能成功
    try:
        logger.info("正在检查并创建数据库表（如果不存在）...")
        Base.metadata.create_all(bind=engine)
        logger.info("数据库表检查完成，所有表已就绪")
    except Exception as e:
        # 如果这里还失败，可能是：
        # 1. 启动脚本的检查有遗漏
        # 2. 数据库环境在启动后发生了变化
        # 3. 数据库权限不足
        error_msg = str(e)
        logger.error("=" * 60)
        logger.error("数据库连接失败，无法创建表")
        logger.error("=" * 60)
        logger.error(f"错误详情: {error_msg}")
        logger.error("")
        logger.error("可能的原因：")
        logger.error("  1. 数据库服务在启动后停止")
        logger.error("  2. 数据库连接配置发生变化")
        logger.error("  3. 数据库用户权限不足（无法创建表）")
        logger.error("  4. 数据库连接数达到上限")
        logger.error("")
        logger.error("建议操作：")
        logger.error("  1. 检查 MySQL 服务是否仍在运行")
        logger.error("  2. 重新运行启动脚本进行环境检查")
        logger.error("  3. 检查数据库用户权限")
        logger.error("=" * 60)
        # 抛出异常，阻止应用启动（应用需要数据库才能正常工作）
        raise RuntimeError(
            f"数据库初始化失败: {error_msg}\n"
            "请检查数据库服务状态和连接配置，然后重新启动应用。"
        ) from e
    
    # 启动后台清理任务
    async def periodic_cleanup():
        """定期清理过期缓存的后台任务"""
        cleanup_interval = 300  # 每5分钟清理一次
        logger.info(f"启动后台清理任务，清理间隔: {cleanup_interval}秒")
        
        while True:
            try:
                await asyncio.sleep(cleanup_interval)
                logger.debug("开始执行定时清理任务...")
                
                # 创建数据库会话
                db = SessionLocal()
                try:
                    # 执行清理
                    from app.services.cleanup_service import cleanup_expired_chunks
                    cleanup_expired_chunks(db)
                    logger.debug("定时清理任务完成")
                except Exception as e:
                    logger.error(f"定时清理任务失败: {e}", exc_info=True)
                finally:
                    db.close()
                    
            except asyncio.CancelledError:
                logger.info("后台清理任务已取消")
                break
            except Exception as e:
                logger.error(f"后台清理任务异常: {e}", exc_info=True)
                # 继续运行，不要因为一次错误就停止
    
    # 启动后台任务
    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("✓ 后台清理任务已启动")
    
    yield
    
    # 关闭时：取消后台任务
    logger.info("正在停止后台清理任务...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("✓ 后台清理任务已停止")
    
    # SQLAlchemy 会自动管理连接池


# 创建FastAPI应用
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# 添加验证错误处理器，显示详细错误信息
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """处理请求验证错误，返回详细的错误信息"""
    # 尝试读取请求体
    body = None
    try:
        # 注意：request.body() 只能读取一次，如果已经读取过会返回空
        # 检查 content-type，如果是 multipart/form-data，不尝试解析为 JSON
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            # FormData 请求，不尝试解析为 JSON
            body = "<FormData>"
        elif hasattr(exc, 'body'):
            body = exc.body
        else:
            # 尝试从请求中读取（仅限 JSON 请求）
            if "application/json" in content_type:
                body_bytes = await request.body()
                if body_bytes:
                    import json
                    body = json.loads(body_bytes.decode('utf-8'))
    except Exception as e:
        logger.warning(f"无法读取请求体: {e}")
        body = "<无法解析>"
    
    error_details = []
    for error in exc.errors():
        field_path = " -> ".join(str(loc) for loc in error.get("loc", []))
        error_details.append({
            "field": field_path,
            "message": error.get("msg", "验证失败"),
            "type": error.get("type", "unknown")
        })
    
    logger.error(f"请求验证失败:")
    logger.error(f"  路径: {request.url.path}")
    logger.error(f"  请求体: {body}")
    logger.error(f"  错误详情: {error_details}")
    
    return JSONResponse(
        status_code=422,
        content={
            "code": 422,
            "msg": "请求数据验证失败",
            "detail": error_details,
            "requestBody": body
        }
    )

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 配置静态文件服务
# 获取项目根目录
# __file__ 是 app/main.py，所以需要向上两级到项目根目录
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
static_dir = os.path.join(project_root, "static")

# 挂载静态文件目录
if os.path.exists(static_dir):
    # 挂载静态资源（CSS、JS、图片等）
    # 添加缓存控制：开发环境禁用缓存，生产环境可以启用
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"静态文件目录已挂载: {static_dir}")
else:
    logger.warning(f"静态文件目录不存在: {static_dir}")

# 注册路由
app.include_router(health_router.router)
app.include_router(codes_router.router, prefix=settings.API_V1_PREFIX)
app.include_router(relay_router.router, prefix=settings.API_V1_PREFIX)
app.include_router(reports_router.router, prefix=settings.API_V1_PREFIX)
app.include_router(auth_router.router, prefix=settings.API_V1_PREFIX)

@app.get("/")
async def root():
    """根路径，返回前端欢迎页"""
    return await welcome_page()

@app.get("/welcome")
async def welcome_page():
    """欢迎页面路由"""
    welcome_path = os.path.join(static_dir, "pages", "welcome", "welcome.html")
    if os.path.exists(welcome_path):
        return FileResponse(
            welcome_path,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"}
        )
    else:
        logger.warning(f"欢迎页文件未找到: {welcome_path}")
        return {
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "health": "/health",
            "error": f"欢迎页文件未找到: {welcome_path}"
        }

@app.get("/index")
async def index_page():
    """主页面路由（文件传输页面）"""
    index_path = os.path.join(static_dir, "pages", "index", "index.html")
    if os.path.exists(index_path):
        return FileResponse(
            index_path,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"}
        )
    else:
        logger.warning(f"主页面文件未找到: {index_path}")
        return {
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "health": "/health",
            "error": f"主页面文件未找到: {index_path}"
        }
