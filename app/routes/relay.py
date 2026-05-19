"""
服务器中转模式API
使用端到端加密，服务器无法查看文件内容
"""

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Body, Query
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from typing import Optional
from pydantic import BaseModel
import hashlib
import io

from app.utils.response import (
    success_response, created_response,
    not_found_response, bad_request_response, error_response
)
from app.utils.validation import validate_pickup_code
from app.utils.pickup_code import check_and_update_expired_pickup_code, ensure_aware_datetime, DatetimeUtil
from app.extensions import get_db
from app.models.pickup_code import PickupCode
import logging

# 导入认证相关功能
from app.routes.auth import get_current_user

# 导入服务层
from app.services.cache_service import chunk_cache, file_info_cache, encrypted_key_cache
from app.services.mapping_service import lookup_code_mapping, get_original_lookup_code, update_cache_expire_at
from app.services.pool_service import upload_pool, download_pool
from app.services.pickup_code_service import get_pickup_code_by_lookup
from app.services.upload_service import upload_chunk as upload_chunk_service, upload_complete as upload_complete_service
from app.services.download_service import (
    get_file_info as get_file_info_service,
    active_download_sessions,
    download_chunk as download_chunk_service,
    download_chunks_batch as download_chunks_batch_service,
    download_complete as download_complete_service
)
from app.utils.cache import cache_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["服务器中转"], prefix="/relay")


class UploadCompleteRequest(BaseModel):
    """上传完成请求模型"""
    totalChunks: int
    fileSize: int
    fileName: str
    mimeType: str


class DownloadChunksRequest(BaseModel):
    """批量下载请求模型"""
    chunkIndices: list[int]
    sessionId: Optional[str] = None


class DownloadCompleteRequest(BaseModel):
    """下载完成请求模型"""
    session_id: Optional[str] = None




@router.post("/codes/{code}/upload-chunk")
async def upload_chunk(
    code: str,
    chunk_data: UploadFile = File(...),
    chunk_index: Optional[int] = Form(None),
    chunk_index_query: Optional[int] = Query(None, alias="chunk_index"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    上传加密的文件块（流式传输）
    
    注意：chunk_index 可以作为 Form 数据或查询参数传递（兼容性处理）
    """
    # 兼容处理：优先使用 Form 数据，如果没有则使用查询参数
    if chunk_index is None:
        if chunk_index_query is None:
            raise HTTPException(
                status_code=422,
                detail="chunk_index 必须作为 Form 数据或查询参数提供"
            )
        chunk_index = chunk_index_query
    
    # 调用服务层处理业务逻辑
    return await upload_chunk_service(
        code=code,
        chunk_data=chunk_data,
        chunk_index=chunk_index,
        chunk_index_query=chunk_index,
        db=db,
        current_user=current_user
    )


@router.post("/codes/{code}/upload-complete")
async def upload_complete(
    code: str,
    request: UploadCompleteRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    通知服务器上传完成
    
    存储文件元信息，供接收者查询
    
    重要：此接口只用于通知上传完成，不会自动创建取件码。
    取件码必须在用户明确点击"生成取件码"按钮时，通过 /api/v1/codes POST 接口创建。
    """
    # 调用服务层处理业务逻辑
    return await upload_complete_service(
        code=code,
        request=request,
        db=db,
        current_user=current_user
    )


@router.post("/codes/{code}/store-encrypted-key")
async def store_encrypted_key(
    code: str,
    request: Request,
    encryptedKey: str = Body(..., embed=True),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    存储加密后的文件加密密钥
    
    重要概念区分：
    1. 文件加密密钥（原始密钥）：
       - 随机生成的AES-GCM密钥（256位），用于直接加密/解密文件块
       - 客户端生成，不直接发送到服务器
    
    2. 密钥码（6位密钥码）：
       - 取件码的后6位，用于派生密钥来加密/解密文件加密密钥
       - 服务器不接触密钥码，只接收前6位查找码
    
    3. 此接口存储的内容：
       - 用取件码的密钥码派生密钥加密后的文件加密密钥（Base64编码）
       - 服务器无法直接获取原始的文件加密密钥
       - 只有拥有完整取件码的用户才能解密
    
    特点：
    - 密钥已用取件码的密钥码派生密钥加密，服务器无法直接获取原始密钥
    - 只有拥有完整取件码（包括后6位密钥码）的用户才能解密
    - 保持端到端加密的安全性
    """
    logger.info(f"[store-encrypted-key] ========== API 被调用 ==========")
    logger.info(f"[store-encrypted-key] code={code}, key_length={len(encryptedKey) if encryptedKey else 0}")
    logger.info(f"[store-encrypted-key] current_user={current_user.id if current_user else None}")
    logger.info(f"[store-encrypted-key] 请求路径: {request.url.path}, 方法: {request.method}")
    
    # 检查权限：只有登录用户才能调用此接口
    if not current_user:
        return bad_request_response(
            msg="只有登录用户才能调用此接口",
            data={"code": "UNAUTHORIZED", "status": "unauthorized"}
        )
    
    # 验证取件码（服务器只接收6位查找码）
    if not validate_pickup_code(code):
        return bad_request_response(msg="取件码格式错误，必须为6位大写字母或数字")
    
    # 使用查找码查询取件码（服务器只接收6位查找码，不接触后6位密钥码）
    pickup_code = get_pickup_code_by_lookup(db, code)
    if not pickup_code:
        return not_found_response(msg=f"取件码不存在")
    
    # 检查并更新过期状态
    is_expired = check_and_update_expired_pickup_code(pickup_code, db)
    if is_expired or pickup_code.status == "expired":
        return bad_request_response(
            msg="取件码已过期，无法使用",
            data={"code": "EXPIRED", "status": "expired"}
        )
    
    # 提取查找码（前6位）用于缓存键
    # code 就是查找码（6位），直接用作缓存键
    lookup_code = code
    
    # 获取用户ID（用于缓存隔离）
    user_id = current_user.id if current_user else None
    
    # 检查是否存在映射，如果存在则使用原始的 lookup_code 访问缓存
    original_lookup_code = get_original_lookup_code(lookup_code, db)
    
    # 存储加密后的密钥
    # 方案2：每个 lookup_code 存储自己的加密密钥（因为每个取件码的密钥码不同，加密后的密钥也不同）
    # 这样旧取件码可以继续使用旧的加密密钥，新取件码使用新的加密密钥
    # 注意：所有 lookup_code 都映射到同一个 original_lookup_code，共享文件块缓存
    
    # 直接使用 pickup_code.expire_at 作为过期时间（不依赖其他缓存，因为文件块可能还在上传）
    expire_at = ensure_aware_datetime(pickup_code.expire_at)
    now = datetime.now(timezone.utc)
    
    # 检查过期时间是否有效（至少还有1秒才过期）
    if expire_at <= now:
        logger.warning(f"取件码已过期或即将过期，无法存储密钥: lookup_code={lookup_code}, expire_at={expire_at}, now={now}")
        return bad_request_response(
            msg="取件码已过期，无法存储密钥",
            data={"code": "EXPIRED", "status": "expired"}
        )
    
    logger.info(f"准备存储密钥: lookup_code={lookup_code}, user_id={user_id}, key_length={len(encryptedKey)}, expire_at={expire_at}")
    
    # 检查是否已存在该 lookup_code 的密钥
    key_exists = encrypted_key_cache.exists(lookup_code, user_id)
    logger.info(f"检查密钥是否存在: lookup_code={lookup_code}, user_id={user_id}, exists={key_exists}")
    if key_exists:
        # 如果密钥相同，说明是重复上传，更新过期时间
        existing_key = encrypted_key_cache.get(lookup_code, user_id)
        if existing_key == encryptedKey:
            logger.info(f"密钥已存在且相同: lookup_code={lookup_code}, user_id={user_id}, 更新过期时间")
            # 更新该 lookup_code 的密钥过期时间
            encrypted_key_cache.set(lookup_code, encryptedKey, user_id, expire_at)
        else:
            # 密钥不同，说明取件码的密钥码可能已更改，更新密钥
            logger.warning(f"密钥已存在但不同: lookup_code={lookup_code}, user_id={user_id}, 更新密钥")
            encrypted_key_cache.set(lookup_code, encryptedKey, user_id, expire_at)
            logger.info(f"已更新密钥: lookup_code={lookup_code}, user_id={user_id}, key_length={len(encryptedKey)}, expire_at={expire_at}")
    else:
        # 新密钥，直接存储（使用 lookup_code 作为键，每个取件码有自己的加密密钥）
        encrypted_key_cache.set(lookup_code, encryptedKey, user_id, expire_at)
        logger.info(f"存储新密钥成功: lookup_code={lookup_code}, user_id={user_id}, key_length={len(encryptedKey)}, expire_at={expire_at}")
    
    # 最终验证：确保密钥确实已存储
    # 注意：如果使用 Redis，可能存在短暂的延迟，所以先等待一下
    import time
    time.sleep(0.1)  # 等待 100ms，确保 Redis 写入完成
    
    final_check = encrypted_key_cache.exists(lookup_code, user_id)
    logger.info(f"最终验证: lookup_code={lookup_code}, user_id={user_id}, exists={final_check}")
    if not final_check:
        logger.error(f"最终验证失败: lookup_code={lookup_code}, user_id={user_id} 不在缓存中，存储可能失败")
        # 返回错误响应，而不是成功响应
        return JSONResponse(
            status_code=500,
            content=error_response(500, "存储加密密钥失败，请重试")
        )
    
    # 如果复用文件（lookup_code != original_lookup_code），更新文件块和文件信息的过期时间
    # 但保持每个 lookup_code 的加密密钥独立
    if lookup_code != original_lookup_code:
        # 复用文件：更新文件块和文件信息的过期时间（取所有相关取件码中最晚的过期时间）
        update_cache_expire_at(original_lookup_code, pickup_code.expire_at, db, user_id)
        logger.info(f"复用文件: lookup_code={lookup_code}, original_lookup_code={original_lookup_code}, user_id={user_id}, 已更新文件块和文件信息的过期时间")
    
    logger.info(f"加密密钥已存储: lookup_code={lookup_code}, original_lookup_code={original_lookup_code}, key_length={len(encryptedKey)}, expire_at={pickup_code.expire_at}")
    
    return success_response(
        msg="加密密钥已存储",
        data={"code": code, "originalLookupCode": original_lookup_code}
    )


@router.get("/codes/{code}/encrypted-key")
async def get_encrypted_key(
    code: str,
    db: Session = Depends(get_db)
):
    """
    获取加密后的文件加密密钥
    
    重要概念区分：
    1. 文件加密密钥（原始密钥）：
       - 随机生成的AES-GCM密钥（256位），用于直接加密/解密文件块
       - 客户端生成，不直接发送到服务器
    
    2. 密钥码（6位密钥码）：
       - 取件码的后6位，用于派生密钥来加密/解密文件加密密钥
       - 服务器不接触密钥码，只接收前6位查找码
    
    3. 此接口返回的内容：
       - 用取件码的密钥码派生密钥加密后的文件加密密钥（Base64编码）
       - 接收者需要使用完整取件码（包括后6位密钥码）来解密
    
    接收者使用此接口获取加密后的文件加密密钥，然后用取件码的密钥码派生密钥解密
    """
    # 验证取件码（服务器只接收6位查找码）
    if not validate_pickup_code(code):
        return bad_request_response(msg="取件码格式错误，必须为6位大写字母或数字")
    
    # 使用查找码查询取件码（服务器只接收6位查找码，不接触后6位密钥码）
    pickup_code = get_pickup_code_by_lookup(db, code)
    if not pickup_code:
        return not_found_response(msg=f"取件码不存在")
    
    # 检查并更新过期状态
    is_expired = check_and_update_expired_pickup_code(pickup_code, db)
    if is_expired or pickup_code.status == "expired":
        return bad_request_response(
            msg="取件码已过期，无法使用",
            data={"code": "EXPIRED", "status": "expired"}
        )
    
    if pickup_code.status == "completed":
        return bad_request_response(
            msg="取件码已完成，无法使用",
            data={"code": "COMPLETED", "status": "completed"}
        )
    
    # 提取查找码（前6位）用于缓存键
    # code 就是查找码（6位），直接用作缓存键
    lookup_code = code
    
    # 注意：下载时不需要用户ID，因为接收者可能不是上传者
    # 但为了兼容性，我们尝试从取件码关联的文件记录中获取上传者ID
    # 如果无法获取，使用 None（向后兼容）
    user_id = None
    try:
        # 尝试从数据库获取文件的上传者ID
        from app.models.file import File
        file_record = db.query(File).filter(File.id == pickup_code.file_id).first()
        if file_record and file_record.uploader_id:
            user_id = file_record.uploader_id
    except Exception as e:
        logger.debug(f"无法获取上传者ID: {e}")
    
    # 检查是否存在映射，如果存在则使用原始的 lookup_code 访问缓存
    original_lookup_code = get_original_lookup_code(lookup_code, db)
    
    # 检查使用次数限制（只检查已完成的下载次数，不考虑正在进行的下载）
    # 注意：active_count 只在真正开始下载块时才计算，避免误判
    used_count = pickup_code.used_count or 0
    limit_count = pickup_code.limit_count or 3
    
    # 如果已完成的下载次数已达到上限，拒绝新的下载请求
    # 注意：这里不检查 active_count，因为获取密钥时还没有真正开始下载
    if limit_count != 999 and used_count >= limit_count:
        return bad_request_response(
            msg="取件码已达到使用上限，无法继续使用",
            data={
                "code": "LIMIT_REACHED",
                "usedCount": used_count,
                "activeCount": 0,  # 获取密钥时还没有活跃会话
                "limitCount": limit_count,
                "remaining": 0
            }
        )
    
    # 生成下载会话ID（用于跟踪这个下载）
    # 注意：此时不立即添加到 active_download_sessions，只有在真正开始下载块时才添加
    import uuid
    session_id = str(uuid.uuid4())
    logger.info(f"生成下载会话ID: lookup_code={lookup_code}, session_id={session_id}, user_id={user_id}")
    
    # 获取加密后的密钥
    # 方案2：直接使用 lookup_code 查找加密密钥（每个取件码有自己的加密密钥）
    logger.info(f"查找加密密钥: lookup_code={lookup_code}, original_lookup_code={original_lookup_code}, user_id={user_id}")
    
    # 直接使用 lookup_code 查找加密密钥（每个取件码有自己的加密密钥）
    encrypted_key = None
    
    # 先检查 lookup_code 是否存在（尝试使用用户ID）
    key_exists = encrypted_key_cache.exists(lookup_code, user_id)
    logger.info(f"检查 lookup_code 是否存在: lookup_code={lookup_code}, user_id={user_id}, exists={key_exists}")
    
    if key_exists:
        encrypted_key = encrypted_key_cache.get(lookup_code, user_id)
        if encrypted_key:
            logger.info(f"✓ 使用 lookup_code 找到加密密钥: lookup_code={lookup_code}, user_id={user_id}, key_length={len(encrypted_key)}")
        else:
            logger.warning(f"lookup_code 存在但密钥为 None: lookup_code={lookup_code}, user_id={user_id}")
    else:
        # 如果找不到，尝试不使用用户ID（向后兼容，可能旧数据没有用户ID）
        if user_id is not None:
            logger.info(f"尝试不使用用户ID查找: lookup_code={lookup_code}")
            key_exists = encrypted_key_cache.exists(lookup_code, None)
            if key_exists:
                encrypted_key = encrypted_key_cache.get(lookup_code, None)
                if encrypted_key:
                    logger.warning(f"使用向后兼容方式找到密钥: lookup_code={lookup_code}, key_length={len(encrypted_key)}")
                    # 复制到用户ID对应的键
                    expire_at = ensure_aware_datetime(pickup_code.expire_at)
                    encrypted_key_cache.set(lookup_code, encrypted_key, user_id, expire_at)
        
        # 如果还是找不到，尝试使用 original_lookup_code（向后兼容）
        if encrypted_key is None:
            logger.info(f"尝试使用 original_lookup_code 查找: original_lookup_code={original_lookup_code}, user_id={user_id}")
            if encrypted_key_cache.exists(original_lookup_code, user_id):
                encrypted_key = encrypted_key_cache.get(original_lookup_code, user_id)
                if encrypted_key:
                    logger.warning(f"使用 original_lookup_code 找到密钥（向后兼容）: lookup_code={lookup_code}, original_lookup_code={original_lookup_code}, user_id={user_id}, key_length={len(encrypted_key)}")
                    # 同时存储到 lookup_code，确保后续可以直接使用 lookup_code 查找
                    expire_at = ensure_aware_datetime(pickup_code.expire_at)
                    encrypted_key_cache.set(lookup_code, encrypted_key, user_id, expire_at)
                    logger.info(f"已将密钥复制到 lookup_code: {lookup_code}")
        
        if encrypted_key is None:
            logger.warning(f"加密密钥不存在: lookup_code={lookup_code}, original_lookup_code={original_lookup_code}, user_id={user_id}")
            # 返回 404 响应，确保 HTTP 状态码正确
            return JSONResponse(
                status_code=404,
                content=not_found_response(msg="加密密钥不存在，可能尚未上传完成")
            )
    
    # 最终检查
    if encrypted_key is None:
        logger.error(f"最终检查失败: encrypted_key 为 None, lookup_code={lookup_code}")
        return JSONResponse(
            status_code=404,
            content=not_found_response(msg="加密密钥不存在，可能尚未上传完成")
        )
    
    # 确保 encrypted_key 不为 None
    if encrypted_key is None:
        logger.error(f"加密密钥为 None: lookup_code={lookup_code}, original_lookup_code={original_lookup_code}")
        return JSONResponse(
            status_code=500,
            content=error_response(500, "服务器内部错误：加密密钥获取失败")
        )
    
    # 检查加密密钥格式（应该是 Base64 字符串）
    logger.info(f"返回加密密钥: lookup_code={lookup_code}, key_length={len(encrypted_key)}, key_type={type(encrypted_key).__name__}")
    if isinstance(encrypted_key, str):
        # 检查是否是有效的 Base64 字符串（不应该以引号开头/结尾，这是 JSON 序列化的特征）
        if encrypted_key.startswith('"') and encrypted_key.endswith('"'):
            logger.warning(f"警告: 加密密钥被 JSON 引号包装，可能是序列化问题: {encrypted_key[:50]}...")
            # 尝试去除引号
            encrypted_key = encrypted_key.strip('"')
        logger.info(f"加密密钥前50个字符: {encrypted_key[:50] if len(encrypted_key) > 50 else encrypted_key}")
    else:
        logger.warning(f"警告: 加密密钥不是字符串类型: {type(encrypted_key)}")
    
    return success_response(data={
        "encryptedKey": encrypted_key,
        "sessionId": session_id  # 返回会话ID，客户端需要在后续请求中携带
    })


@router.get("/codes/{code}/check-chunks")
async def check_chunks(
    code: str,
    total_chunks: int = Query(..., description="总块数"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    检查文件块是否已存在（用于复用旧文件块，避免重复上传）
    
    发送者使用此接口在上传前检查文件块是否已存在
    如果已存在，可以跳过上传，直接复用
    """
    # 检查权限：只有登录用户才能调用此接口
    if not current_user:
        return bad_request_response(
            msg="只有登录用户才能调用此接口",
            data={"code": "UNAUTHORIZED", "status": "unauthorized"}
        )
    
    # 验证取件码（服务器只接收6位查找码）
    if not validate_pickup_code(code):
        return bad_request_response(msg="取件码格式错误，必须为6位大写字母或数字")
    
    # 使用查找码查询取件码（服务器只接收6位查找码，不接触后6位密钥码）
    pickup_code = get_pickup_code_by_lookup(db, code)
    if not pickup_code:
        return not_found_response(msg=f"取件码不存在")
    
    # 检查并更新过期状态
    is_expired = check_and_update_expired_pickup_code(pickup_code, db)
    if is_expired or pickup_code.status == "expired":
        return bad_request_response(
            msg="取件码已过期，无法使用",
            data={"code": "EXPIRED", "status": "expired"}
        )
    
    # 提取查找码（前6位）用于缓存键
    lookup_code = code
    
    # 获取用户ID（用于缓存隔离）
    user_id = current_user.id if current_user else None
    
    # 检查是否存在映射，如果存在则使用原始的 lookup_code 访问缓存
    original_lookup_code = get_original_lookup_code(lookup_code, db)
    
    # 检查文件块是否存在
    existing_chunks = []
    missing_chunks = []
    
    if chunk_cache.exists(original_lookup_code, user_id):
        chunks = chunk_cache.get(original_lookup_code, user_id)
        for i in range(total_chunks):
            if i in chunks:
                chunk = chunks[i]
                # 检查是否过期（使用取件码的过期时间，绝对时间）
                pickup_expire_at = chunk.get('pickup_expire_at') or chunk.get('expires_at')
                if pickup_expire_at:
                    pickup_expire_at = ensure_aware_datetime(pickup_expire_at)
                    if DatetimeUtil.now() < DatetimeUtil.ensure_aware(pickup_expire_at):
                        existing_chunks.append(i)
                    else:
                        missing_chunks.append(i)
                else:
                    missing_chunks.append(i)
            else:
                missing_chunks.append(i)
    else:
        # 缓存不存在，所有块都需要上传
        missing_chunks = list(range(total_chunks))
    
    return success_response(
        msg="文件块检查完成",
        data={
            "existingChunks": existing_chunks,
            "missingChunks": missing_chunks,
            "totalChunks": total_chunks,
            "allExist": len(missing_chunks) == 0
        }
    )


@router.get("/codes/{code}/file-info")
async def get_file_info(
    code: str,
    db: Session = Depends(get_db)
):
    """
    获取文件信息
    
    接收者使用此接口获取文件元信息
    """
    return await get_file_info_service(code, db)


@router.get("/codes/{code}/download-chunk/{chunk_index}")
async def download_chunk(
    code: str,
    chunk_index: int,
    session_id: Optional[str] = Query(None, description="下载会话ID（从获取加密密钥接口返回）"),
    db: Session = Depends(get_db)
):
    """
    下载加密的文件块（流式传输）
    
    特点：
    - 返回加密后的数据块
    - 客户端解密后重组文件
    - 支持多个接收者同时下载
    - 数据从内存读取，不涉及磁盘
    
    注意：如果提供了session_id，会验证是否是已开始的下载会话
    如果是已开始的下载会话，允许继续下载（即使达到上限）
    """
    # 调用服务层处理业务逻辑
    return await download_chunk_service(
        code=code,
        chunk_index=chunk_index,
        session_id=session_id,
        db=db
    )


@router.post("/codes/{code}/download-chunks")
async def download_chunks_batch(
    code: str,
    request: DownloadChunksRequest,
    db: Session = Depends(get_db)
):
    """
    批量下载文件块（优化性能）
    
    请求体格式:
    {
        "chunkIndices": [0, 1, 2, ...],  # 要下载的块索引列表
        "sessionId": "..."  # 可选的会话ID
    }
    
    返回格式:
    {
        "chunks": {
            "0": {"data": base64_encoded_data, "hash": "...", ...},
            "1": {"data": base64_encoded_data, "hash": "...", ...},
            ...
        },
        "missing": [5, 10],  # 缺失的块索引（如果存在）
        "expired": [3]  # 过期的块索引（如果存在）
    }
    """
    # 调用服务层处理业务逻辑
    return await download_chunks_batch_service(
        code=code,
        request=request.dict(),
        session_id=request.sessionId,
        db=db
    )


@router.post("/codes/{code}/download-complete")
async def download_complete(
    code: str,
    request: DownloadCompleteRequest,
    db: Session = Depends(get_db)
):
    """
    通知服务器下载完成
    
    功能：
    - 清除下载会话记录
    - 增加使用次数
    - 如果达到上限，更新状态为completed
    - 不删除文件块（可能还有其他接收者需要下载）
    """
    # 调用服务层处理业务逻辑
    return await download_complete_service(
        code=code,
        session_id=request.session_id,
        db=db
    )


@router.delete("/codes/{code}/chunks")
async def delete_chunks(
    code: str,
    db: Session = Depends(get_db)
):
    """
    删除所有文件块（传输完成后调用）
    
    特点：
    - 立即从内存中删除
    - 不涉及磁盘操作
    - 确保数据完全清除
    """
    # 验证取件码（服务器只接收6位查找码）
    if not validate_pickup_code(code):
        return bad_request_response(msg="取件码格式错误，必须为6位大写字母或数字")
    
    # 使用查找码查询取件码（服务器只接收6位查找码，不接触后6位密钥码）
    pickup_code = get_pickup_code_by_lookup(db, code)
    if not pickup_code:
        return not_found_response(msg=f"取件码不存在")
    
    # 提取查找码（前6位）用于缓存键
    lookup_code = code

    # 获取用户ID（用于缓存隔离）
    from app.models.file import File
    file_record = db.query(File).filter(File.id == pickup_code.file_id).first()
    user_id = file_record.uploader_id if file_record else None

    # 使用用户隔离的缓存删除
    chunk_cache.delete(lookup_code, user_id)
    file_info_cache.delete(lookup_code, user_id)
    encrypted_key_cache.delete(lookup_code, user_id)

    # 同时清理无用户ID的旧缓存（向后兼容）
    chunk_cache.delete(lookup_code, None)
    file_info_cache.delete(lookup_code, None)
    encrypted_key_cache.delete(lookup_code, None)

    return success_response(msg="所有文件块已删除")

