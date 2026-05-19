from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from app.models.pickup_code import PickupCode
from app.utils.pickup_code import ensure_aware_datetime
from app.services.pickup_code_service import get_pickup_code_by_lookup
from app.services.mapping_service import get_original_lookup_code
from app.services.cache_service import chunk_cache, file_info_cache, encrypted_key_cache
from app.services.pool_service import cleanup_upload_pool, cleanup_download_pool
from app.services.mapping_service import lookup_code_mapping
from app.utils.cache import cache_manager
import logging

logger = logging.getLogger(__name__)


def cleanup_expired_chunks(db: Session = None):
    """
    完整清理所有过期的数据
    
    清理范围：
    1. 数据库中的过期取件码记录
    2. Redis缓存中的过期数据（文件块、文件信息、密钥、映射）
    3. 内存缓存中的过期数据（文件块、文件信息、密钥、映射）
    4. 上传池（upload_pool）中的过期数据
    5. 下载池（download_pool）中的过期数据
    
    策略：基于数据库查询所有过期的取件码，然后清理所有相关数据
    """
    if not db:
        logger.warning("清理任务需要数据库会话，跳过清理")
        return
    
    now = datetime.now(timezone.utc)
    
    try:
        from app.utils.pickup_code import check_and_update_expired_pickup_code
        from app.models.file import File
        from sqlalchemy import text
        from app.services.pool_service import upload_pool, download_pool
        
        # 第一步：查询所有过期的取件码
        logger.info("开始执行定时清理任务...")
        all_pickup_codes = db.query(PickupCode).all()
        expired_pickup_codes = []
        
        for pickup_code in all_pickup_codes:
            # 检查并更新过期状态
            check_and_update_expired_pickup_code(pickup_code, db)
            db.refresh(pickup_code)
            
            if pickup_code.status == "expired":
                expired_pickup_codes.append(pickup_code)
        
        db.commit()
        
        if not expired_pickup_codes:
            logger.info("没有发现过期的取件码")
            # 仍然清理上传池和下载池中过期的数据（基于时间）
            cleanup_upload_pool()
            cleanup_download_pool()
            return
        
        logger.info(f"发现 {len(expired_pickup_codes)} 个过期的取件码，开始清理相关数据...")
        
        # 第二步：收集所有过期的 lookup_code
        expired_lookup_codes = set()
        expired_codes = []
        
        for pickup_code in expired_pickup_codes:
            if pickup_code.code and len(pickup_code.code) >= 6:
                lookup_code = pickup_code.code[:6]
                expired_lookup_codes.add(lookup_code)
                expired_codes.append(pickup_code.code)
        
        logger.info(f"需要清理 {len(expired_lookup_codes)} 个唯一的 lookup_code")
        
        # 第三步：清理所有相关数据
        # 注意：文件块缓存和文件信息缓存使用标识码作为键，需要先获取标识码
        from app.services.mapping_service import get_identifier_code, check_all_pickup_codes_expired_for_file

        # 收集所有标识码（用于检查）
        all_identifier_codes = set()
        for lookup_code in expired_lookup_codes:
            try:
                identifier_code = get_identifier_code(lookup_code, db, "cleanup_identifiers")
                all_identifier_codes.add(identifier_code)
            except Exception as e:
                logger.debug(f"无法获取标识码: lookup_code={lookup_code}, error={e}")

        # 筛选出所有取件码都已过期的标识码（用于清理文件块缓存和文件信息缓存）
        identifier_codes_to_clean = set()
        for identifier_code in all_identifier_codes:
            try:
                # 通过映射服务找到该标识码对应的文件ID
                # 先找到一个映射到该标识码的取件码，然后获取文件ID
                pickup_code_for_identifier = None
                for pc in expired_pickup_codes:
                    if pc.code and len(pc.code) >= 6:
                        lookup_code = pc.code[:6]
                        try:
                            if get_identifier_code(lookup_code, db, "cleanup_verify") == identifier_code:
                                pickup_code_for_identifier = pc
                                break
                        except Exception:
                            pass

                if pickup_code_for_identifier:
                    # 检查该文件的所有取件码是否都已过期
                    if check_all_pickup_codes_expired_for_file(pickup_code_for_identifier.file_id, db):
                        identifier_codes_to_clean.add(identifier_code)
                        logger.info(f"标识码 {identifier_code} 的所有取件码都已过期，可以清理文件缓存")
                    else:
                        logger.info(f"标识码 {identifier_code} 还有未过期的取件码，跳过文件缓存清理")
                else:
                    logger.warning(f"无法找到标识码 {identifier_code} 对应的取件码，跳过清理")
            except Exception as e:
                logger.debug(f"检查标识码过期状态失败: identifier_code={identifier_code}, error={e}")
        
        # 3.1 清理上传池（upload_pool）中的过期数据（使用标识码）
        upload_pool_cleaned = 0
        for identifier_code in list(upload_pool.keys()):
            if identifier_code in identifier_codes_to_clean:
                del upload_pool[identifier_code]
                upload_pool_cleaned += 1
                logger.info(f"清理上传池中的过期数据: identifier_code={identifier_code}")
        
        # 3.2 清理下载池（download_pool）中的过期数据（使用标识码）
        download_pool_cleaned = 0
        for identifier_code in list(download_pool.keys()):
            if identifier_code in identifier_codes_to_clean:
                del download_pool[identifier_code]
                download_pool_cleaned += 1
                logger.info(f"清理下载池中的过期数据: identifier_code={identifier_code}")
        
        # 3.3 清理主缓存（chunk_cache, file_info_cache, encrypted_key_cache）
        # 文件块缓存和文件信息缓存使用标识码作为键（如果可用），否则使用 lookup_code
        # 密钥缓存使用取件码作为键（每个取件码独立存储）
        chunk_cache_cleaned = 0
        file_info_cache_cleaned = 0
        encrypted_key_cache_cleaned = 0
        
        # 清理文件块缓存和文件信息缓存（使用标识码）
        for identifier_code in identifier_codes_to_clean:
            # 获取该标识码对应的所有取件码，找到所有可能的 user_id
            # 通过查询所有取件码，找到映射到该标识码的取件码
            pickup_codes_for_identifier = []
            for pickup_code in expired_pickup_codes:
                if pickup_code.code and len(pickup_code.code) >= 6:
                    lookup_code = pickup_code.code[:6]
                    try:
                        if get_identifier_code(lookup_code, db, "cleanup_collect") == identifier_code:
                            pickup_codes_for_identifier.append(pickup_code)
                    except Exception:
                        pass
            
            user_ids_to_check = [None]  # 检查 None（anonymous）
            
            # 从数据库获取所有可能的 user_id
            for pickup_code in pickup_codes_for_identifier:
                try:
                    file_record = db.query(File).filter(File.id == pickup_code.file_id).first()
                    if file_record and file_record.uploader_id and file_record.uploader_id not in user_ids_to_check:
                        user_ids_to_check.append(file_record.uploader_id)
                except Exception as e:
                    logger.debug(f"无法获取用户ID: {e}")
            
            # 为每个可能的 user_id 清理文件块缓存和文件信息缓存（使用标识码）
            for user_id in user_ids_to_check:
                # 清理文件块缓存（使用标识码）
                if chunk_cache.exists(identifier_code, user_id):
                    chunk_cache.delete(identifier_code, user_id)
                    chunk_cache_cleaned += 1
                    logger.debug(f"清理文件块缓存: identifier_code={identifier_code}, user_id={user_id}")
                
                # 清理文件信息缓存（使用标识码）
                if file_info_cache.exists(identifier_code, user_id):
                    file_info_cache.delete(identifier_code, user_id)
                    file_info_cache_cleaned += 1
                    logger.debug(f"清理文件信息缓存: identifier_code={identifier_code}, user_id={user_id}")
        
        # 对于无法获取标识码的 lookup_code，直接使用 lookup_code 清理缓存
        # 这种情况发生在所有取件码都已过期，无法重建标识码时
        for lookup_code in expired_lookup_codes:
            # 检查是否已经通过 identifier_code 清理过
            identifier_code = None
            try:
                identifier_code = get_identifier_code(lookup_code, db, "cleanup_fallback")
            except Exception:
                pass
            
            # 如果无法获取 identifier_code，说明所有取件码都已过期，直接使用 lookup_code 清理
            if not identifier_code:
                # 获取该 lookup_code 对应的所有取件码，找到所有可能的 user_id
                pickup_codes_for_lookup = [pc for pc in expired_pickup_codes if pc.code and len(pc.code) >= 6 and pc.code[:6] == lookup_code]
                user_ids_to_check = [None]  # 检查 None（anonymous）
                
                # 从数据库获取所有可能的 user_id
                for pickup_code in pickup_codes_for_lookup:
                    try:
                        file_record = db.query(File).filter(File.id == pickup_code.file_id).first()
                        if file_record and file_record.uploader_id and file_record.uploader_id not in user_ids_to_check:
                            user_ids_to_check.append(file_record.uploader_id)
                    except Exception as e:
                        logger.debug(f"无法获取用户ID: {e}")
                
                # 为每个可能的 user_id 清理文件块缓存和文件信息缓存（使用 lookup_code）
                for user_id in user_ids_to_check:
                    # 清理文件块缓存（使用 lookup_code）
                    if chunk_cache.exists(lookup_code, user_id):
                        chunk_cache.delete(lookup_code, user_id)
                        chunk_cache_cleaned += 1
                        logger.debug(f"清理文件块缓存（fallback）: lookup_code={lookup_code}, user_id={user_id}")
                    
                    # 清理文件信息缓存（使用 lookup_code）
                    if file_info_cache.exists(lookup_code, user_id):
                        file_info_cache.delete(lookup_code, user_id)
                        file_info_cache_cleaned += 1
                        logger.debug(f"清理文件信息缓存（fallback）: lookup_code={lookup_code}, user_id={user_id}")
        
        # 清理密钥缓存（使用取件码，每个取件码独立存储）
        for lookup_code in expired_lookup_codes:
            # 获取该 lookup_code 对应的所有取件码，找到所有可能的 user_id
            pickup_codes_for_lookup = [pc for pc in expired_pickup_codes if pc.code and len(pc.code) >= 6 and pc.code[:6] == lookup_code]
            user_ids_to_check = [None]  # 检查 None（anonymous）
            
            # 从数据库获取所有可能的 user_id
            for pickup_code in pickup_codes_for_lookup:
                try:
                    file_record = db.query(File).filter(File.id == pickup_code.file_id).first()
                    if file_record and file_record.uploader_id and file_record.uploader_id not in user_ids_to_check:
                        user_ids_to_check.append(file_record.uploader_id)
                except Exception as e:
                    logger.debug(f"无法获取用户ID: {e}")
            
            # 为每个可能的 user_id 清理密钥缓存（使用取件码）
            for user_id in user_ids_to_check:
                # 清理密钥缓存
                if encrypted_key_cache.exists(lookup_code, user_id):
                    encrypted_key_cache.delete(lookup_code, user_id)
                    encrypted_key_cache_cleaned += 1
                    logger.debug(f"清理密钥缓存: lookup_code={lookup_code}, user_id={user_id}")
        
        # 3.4 清理映射关系（内存和Redis）
        mapping_cleaned = 0
        for lookup_code in expired_lookup_codes:
            # 清理内存映射关系
            if lookup_code in lookup_code_mapping:
                del lookup_code_mapping[lookup_code]
                mapping_cleaned += 1
                logger.debug(f"清理内存映射关系: lookup_code={lookup_code}")
            
            # 清理Redis映射关系
            if cache_manager.exists('lookup_mapping', lookup_code):
                cache_manager.delete('lookup_mapping', lookup_code)
                mapping_cleaned += 1
                logger.debug(f"清理Redis映射关系: lookup_code={lookup_code}")
        
        # 3.5 从数据库查询所有映射键，清理过期的映射关系（双重保险）
        try:
            all_mapping_keys = cache_manager.get_all_keys('lookup_mapping')
            for mapping_key in all_mapping_keys:
                # 检查该映射键对应的取件码是否过期
                pickup_code_obj = get_pickup_code_by_lookup(db, mapping_key)
                if pickup_code_obj:
                    check_and_update_expired_pickup_code(pickup_code_obj, db)
                    db.refresh(pickup_code_obj)
                    if pickup_code_obj.status == "expired":
                        cache_manager.delete('lookup_mapping', mapping_key)
                        mapping_cleaned += 1
                        logger.debug(f"清理Redis中过期映射关系（扫描）: lookup_code={mapping_key}")
                else:
                    # 取件码不存在，清理映射关系
                    cache_manager.delete('lookup_mapping', mapping_key)
                    mapping_cleaned += 1
                    logger.debug(f"清理Redis中无效映射关系（扫描）: lookup_code={mapping_key}")
        except Exception as e:
            logger.warning(f"清理Redis映射关系失败: {e}")
        
        # 第四步：删除数据库中过期的取件码记录
        deleted_count = 0
        for pickup_code in expired_pickup_codes:
            try:
                db.delete(pickup_code)
                deleted_count += 1
                logger.debug(f"删除过期取件码记录: code={pickup_code.code}")
            except Exception as e:
                logger.warning(f"删除取件码 {pickup_code.code} 失败: {e}")
        
        db.commit()
        
        # 第五步：清理上传池和下载池中基于时间过期的数据（补充清理）
        cleanup_upload_pool()
        cleanup_download_pool()
        
        # 记录清理结果
        logger.info(f"定时清理完成: 删除 {deleted_count} 个取件码记录, "
                   f"清理 {chunk_cache_cleaned} 个文件块缓存, "
                   f"{file_info_cache_cleaned} 个文件信息缓存, "
                   f"{encrypted_key_cache_cleaned} 个密钥缓存, "
                   f"{mapping_cleaned} 个映射关系, "
                   f"{upload_pool_cleaned} 个上传池条目, "
                   f"{download_pool_cleaned} 个下载池条目")
        
    except Exception as e:
        logger.error(f"定时清理任务失败: {e}", exc_info=True)
        if db:
            db.rollback()
