"""
缓存工具模块
支持 Redis 和内存字典两种缓存方案
优先使用 Redis，如果 Redis 不可用则回退到内存字典
"""

import json
import pickle
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Dict
import logging

logger = logging.getLogger(__name__)

# 导入时区转换工具函数
from app.utils.pickup_code import ensure_aware_datetime

# 尝试导入 Redis
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

# 从配置导入 Redis 设置
from app.config import settings


class CacheManager:
    """
    缓存管理器
    优先使用 Redis，如果 Redis 不可用则回退到内存字典
    """
    
    def __init__(self):
        self._redis_client = None
        self._use_redis = False
        self._fallback_cache: Dict[str, Any] = {}
        self._redis_configured = REDIS_AVAILABLE and settings.REDIS_ENABLED
        self._last_redis_attempt = 0  # 上次尝试重连的时间戳

        # 尝试初始化 Redis
        self._try_connect_redis()
        if not self._use_redis:
            if self._redis_configured:
                logger.info("Redis 配置了但不可用，使用内存缓存")
            else:
                logger.info("Redis 未启用，使用内存字典缓存")

    def _try_connect_redis(self):
        """尝试连接 Redis（初始化连接 or 断线重连）"""
        if not self._redis_configured:
            return
        import time
        self._last_redis_attempt = time.time()
        try:
            self._redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD if settings.REDIS_PASSWORD else None,
                decode_responses=False,
                socket_connect_timeout=2,
                socket_timeout=2
            )
            self._redis_client.ping()
            self._use_redis = True
            if not hasattr(self, '_redis_was_down'):
                logger.info("✓ Redis 缓存已启用")
            else:
                logger.info("✓ Redis 已恢复连接")
                del self._redis_was_down
        except Exception:
            self._redis_client = None
            self._use_redis = False
            self._redis_was_down = True

    def _should_retry_redis(self) -> bool:
        """检查是否应该尝试重连 Redis（每60秒重试一次）"""
        if self._use_redis or not self._redis_configured:
            return False
        import time
        return (time.time() - self._last_redis_attempt) > 60
    
    def _get_key(self, prefix: str, key: str) -> str:
        """生成 Redis 键名"""
        return f"quickshare:{prefix}:{key}"
    
    def _serialize_value(self, value: Any) -> bytes:
        """序列化值（用于 Redis）"""
        # 对于复杂对象（如字典、列表），使用 pickle
        # 对于简单类型，使用 JSON
        # 对于字符串，直接编码为 UTF-8（不需要 JSON）
        try:
            if isinstance(value, bytes):
                return value
            elif isinstance(value, str):
                # 字符串直接编码，不需要 JSON（因为 JSON 会添加引号）
                return value.encode('utf-8')
            elif isinstance(value, (dict, list)):
                # 检查是否包含不可 JSON 序列化的对象（bytes, datetime 等）
                def has_non_json_types(obj):
                    """递归检查是否包含不可 JSON 序列化的类型"""
                    if isinstance(obj, bytes):
                        return True
                    if isinstance(obj, datetime):
                        return True
                    if isinstance(obj, dict):
                        return any(has_non_json_types(v) for v in obj.values())
                    if isinstance(obj, (list, tuple)):
                        return any(has_non_json_types(item) for item in obj)
                    return False
                
                if has_non_json_types(value):
                    # 包含 bytes 或 datetime，使用 pickle
                    return pickle.dumps(value)
                else:
                    # 可以 JSON 序列化，使用 JSON（更高效）
                    return json.dumps(value, default=str).encode('utf-8')
            else:
                return pickle.dumps(value)
        except (TypeError, ValueError) as e:
            # JSON 序列化失败（可能包含不可序列化的对象），使用 pickle
            logger.debug(f"JSON 序列化失败，使用 pickle: {e}")
            return pickle.dumps(value)
        except Exception as e:
            logger.warning(f"序列化失败，使用 pickle: {e}")
            return pickle.dumps(value)
    
    def _deserialize_value(self, value: bytes) -> Any:
        """反序列化值（从 Redis）"""
        if not value:
            return None
        
        # 首先尝试 pickle（因为 pickle 数据可能包含任意字节，无法用 UTF-8 解码）
        # pickle 数据通常以特定字节开头（如 b'\x80\x03' 或 b'\x80\x04'）
        try:
            # 检查是否是 pickle 数据（pickle 协议 2-5 的常见开头）
            if value.startswith((b'\x80\x02', b'\x80\x03', b'\x80\x04', b'\x80\x05')):
                return pickle.loads(value)
            # 也尝试直接 pickle.loads（某些情况下可能没有标准开头）
            # 但先检查是否包含明显的 pickle 特征
            if b'\x80' in value[:10]:  # 前10个字节中包含 pickle 标记
                try:
                    return pickle.loads(value)
                except Exception:
                    pass  # 不是 pickle，继续尝试其他方法
        except Exception:
            pass  # 不是 pickle，继续尝试其他方法
        
        # 尝试解码为 UTF-8 字符串
        try:
            decoded = value.decode('utf-8')
            # 尝试判断是否是 JSON 格式（以 { 或 [ 开头）
            if decoded.startswith(('{', '[')):
                try:
                    return json.loads(decoded)
                except json.JSONDecodeError:
                    # JSON 解析失败，返回原始字符串
                    return decoded
            else:
                # 不是 JSON 格式，直接返回字符串（可能是 Base64 或其他字符串）
                return decoded
        except UnicodeDecodeError:
            # 无法解码为 UTF-8，最后尝试 pickle（可能是二进制数据）
            try:
                return pickle.loads(value)
            except Exception as e:
                logger.error(f"反序列化失败: {e}, value_length={len(value)}, value_preview={value[:50]}")
                return None
    
    def set(self, prefix: str, key: str, value: Any, expire_at: Optional[datetime] = None) -> bool:
        """
        设置缓存值

        参数:
        - prefix: 缓存前缀（如 'chunk', 'file_info', 'encrypted_key'）
        - key: 缓存键
        - value: 缓存值
        - expire_at: 过期时间（绝对时间）

        返回:
        - True 如果成功，False 如果失败
        """
        cache_key = self._get_key(prefix, key)

        if self._should_retry_redis():
            self._try_connect_redis()

        if self._use_redis and self._redis_client:
            try:
                serialized = self._serialize_value(value)
                
                if expire_at:
                    # 统一转换时区：确保 expire_at 是 offset-aware 的（UTC）
                    expire_at = ensure_aware_datetime(expire_at)
                    # 计算剩余秒数
                    now = datetime.now(timezone.utc)
                    ttl = int((expire_at - now).total_seconds())
                    if ttl > 0:
                        self._redis_client.setex(cache_key, ttl, serialized)
                    else:
                        # 已过期，不存储
                        logger.warning(f"密钥已过期，不存储: key={key}, expire_at={expire_at}, now={now}, ttl={ttl}")
                        return False
                else:
                    self._redis_client.set(cache_key, serialized)
                
                return True
            except Exception as e:
                logger.warning(f"Redis 设置失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix not in self._fallback_cache:
            self._fallback_cache[prefix] = {}
        
        # 统一转换时区：确保 expire_at 是 offset-aware 的（UTC），便于后续比较
        if expire_at:
            expire_at = ensure_aware_datetime(expire_at)
        
        cache_entry = {
            'value': value,
            'expire_at': expire_at
        }
        self._fallback_cache[prefix][key] = cache_entry
        return True
    
    def get(self, prefix: str, key: str) -> Optional[Any]:
        """
        获取缓存值
        
        参数:
        - prefix: 缓存前缀
        - key: 缓存键
        
        返回:
        - 缓存值，如果存在且未过期
        - None，如果不存在或已过期
        """
        cache_key = self._get_key(prefix, key)

        if self._should_retry_redis():
            self._try_connect_redis()

        if self._use_redis and self._redis_client:
            try:
                value = self._redis_client.get(cache_key)
                if value is None:
                    return None
                return self._deserialize_value(value)
            except Exception as e:
                logger.warning(f"Redis 获取失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix not in self._fallback_cache:
            return None
        
        if key not in self._fallback_cache[prefix]:
            return None
        
        cache_entry = self._fallback_cache[prefix][key]
        
        # 检查是否过期（统一时区转换）
        if cache_entry.get('expire_at'):
            expire_at = ensure_aware_datetime(cache_entry['expire_at'])
            now = datetime.now(timezone.utc)
            if now > expire_at:
                # 已过期，删除
                del self._fallback_cache[prefix][key]
                return None
        
        return cache_entry['value']
    
    def delete(self, prefix: str, key: str) -> bool:
        """
        删除缓存值
        
        参数:
        - prefix: 缓存前缀
        - key: 缓存键
        
        返回:
        - True 如果成功，False 如果失败
        """
        cache_key = self._get_key(prefix, key)
        
        if self._use_redis and self._redis_client:
            try:
                self._redis_client.delete(cache_key)
                return True
            except Exception as e:
                logger.warning(f"Redis 删除失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix in self._fallback_cache and key in self._fallback_cache[prefix]:
            del self._fallback_cache[prefix][key]
            return True
        
        return False
    
    def exists(self, prefix: str, key: str) -> bool:
        """
        检查缓存键是否存在
        
        参数:
        - prefix: 缓存前缀
        - key: 缓存键
        
        返回:
        - True 如果存在且未过期，False 如果不存在或已过期
        """
        cache_key = self._get_key(prefix, key)
        
        if self._use_redis and self._redis_client:
            try:
                return self._redis_client.exists(cache_key) > 0
            except Exception as e:
                logger.warning(f"Redis 检查失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix not in self._fallback_cache:
            return False
        
        if key not in self._fallback_cache[prefix]:
            return False
        
        # 检查是否过期（统一时区转换）
        cache_entry = self._fallback_cache[prefix][key]
        if cache_entry.get('expire_at'):
            expire_at = ensure_aware_datetime(cache_entry['expire_at'])
            now = datetime.now(timezone.utc)
            if now > expire_at:
                # 已过期，删除
                del self._fallback_cache[prefix][key]
                return False
        
        return True
    
    def get_all_keys(self, prefix: str) -> list:
        """
        获取指定前缀的所有键
        
        参数:
        - prefix: 缓存前缀
        
        返回:
        - 键列表
        """
        if self._use_redis and self._redis_client:
            try:
                pattern = self._get_key(prefix, "*")
                keys = []
                for key in self._redis_client.scan_iter(match=pattern):
                    # 提取原始键名（去掉前缀）
                    key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                    original_key = key_str.replace(f"quickshare:{prefix}:", "")
                    keys.append(original_key)
                return keys
            except Exception as e:
                logger.warning(f"Redis 获取键列表失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix not in self._fallback_cache:
            return []
        
        # 过滤过期的键（统一时区转换）
        now = datetime.now(timezone.utc)
        valid_keys = []
        for key, cache_entry in list(self._fallback_cache[prefix].items()):
            expire_at = cache_entry.get('expire_at')
            if expire_at:
                expire_at = ensure_aware_datetime(expire_at)
                if now > expire_at:
                    # 已过期，删除
                    del self._fallback_cache[prefix][key]
                    continue
            valid_keys.append(key)
        
        return valid_keys
    
    def clear_prefix(self, prefix: str) -> int:
        """
        清除指定前缀的所有缓存
        
        参数:
        - prefix: 缓存前缀
        
        返回:
        - 删除的键数量
        """
        count = 0
        
        if self._use_redis and self._redis_client:
            try:
                pattern = self._get_key(prefix, "*")
                keys = list(self._redis_client.scan_iter(match=pattern))
                if keys:
                    count = self._redis_client.delete(*keys)
                return count
            except Exception as e:
                logger.warning(f"Redis 清除失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix in self._fallback_cache:
            count = len(self._fallback_cache[prefix])
            del self._fallback_cache[prefix]
        
        return count
    
    def update_expire_at(self, prefix: str, key: str, expire_at: datetime) -> bool:
        """
        更新缓存的过期时间
        
        参数:
        - prefix: 缓存前缀
        - key: 缓存键
        - expire_at: 新的过期时间（绝对时间）
        
        返回:
        - True 如果成功，False 如果失败
        """
        cache_key = self._get_key(prefix, key)
        
        if self._use_redis and self._redis_client:
            try:
                # 检查键是否存在
                if not self._redis_client.exists(cache_key):
                    return False
                
                # 统一转换时区：确保 expire_at 是 offset-aware 的（UTC）
                expire_at = ensure_aware_datetime(expire_at)
                # 计算剩余秒数
                now = datetime.now(timezone.utc)
                ttl = int((expire_at - now).total_seconds())
                if ttl > 0:
                    self._redis_client.expire(cache_key, ttl)
                    return True
                else:
                    # 已过期，删除
                    self._redis_client.delete(cache_key)
                    return False
            except Exception as e:
                logger.warning(f"Redis 更新过期时间失败，回退到内存字典: {e}")
                self._use_redis = False
        
        # 回退到内存字典
        if prefix not in self._fallback_cache:
            return False
        
        if key not in self._fallback_cache[prefix]:
            return False
        
        # 更新过期时间
        self._fallback_cache[prefix][key]['expire_at'] = expire_at
        return True


# 创建全局缓存管理器实例
cache_manager = CacheManager()

