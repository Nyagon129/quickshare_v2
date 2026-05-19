"""
去重指纹（Dedupe Fingerprint）工具

## 背景与概念（强烈建议读完）

系统里存在两类“哈希/指纹”，用途不同，千万不要混用：

1) **明文文件哈希（plaintext file hash）**
   - 计算位置：前端浏览器
   - 计算对象：**原始文件（未加密）**的字节序列
   - 算法：SHA-256（hex）
   - 用途：
     - 前端本地缓存（例如：以 hash 作为 key 缓存文件加密密钥）
     - 给后端提供“文件恒等标识”的输入（但不建议后端直接落库明文哈希）

2) **去重指纹（dedupe fingerprint）**
   - 计算位置：后端
   - 输入：user_id + 明文文件哈希 + 服务器端 pepper（秘密hash盐）
   - 算法：HMAC-SHA256（hex）
   - 用途：
     - 后端去重、绑定上传者：同一用户同一文件 => 指纹相同；不同用户即使文件相同 => 指纹不同
     - 降低数据库泄露时的“可关联性/可被字典匹配”的风险

⚠️ 注意：
 - 去重指纹不是加密文件哈希，也不是 chunk 哈希。
 - chunk 哈希（对“加密后的分片”做 SHA-256）仅用于完整性校验，不能用于文件级去重。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from app.config import settings


def derive_dedupe_fingerprint(
    *,
    user_id: Optional[int],
    plaintext_file_hash: str,
) -> str:
    """
    从 (user_id + 明文文件哈希 + 服务器 pepper) 派生去重指纹。

    - 同一 user_id + 同一 plaintext_file_hash => 指纹稳定一致
    - 不同 user_id + 同一 plaintext_file_hash => 指纹不同（用户隔离）
    """
    if not plaintext_file_hash:
        raise ValueError("plaintext_file_hash is required")

    # 归一化：前端算出来是 hex；统一用小写，避免大小写导致误判
    ph = plaintext_file_hash.strip().lower()
    if len(ph) != 64:
        # 允许继续（为了兼容历史/异常情况），但至少保证可用
        # 这里不强制抛错，避免线上因为极端输入直接 500
        pass

    uid = "anonymous" if user_id is None else str(user_id)

    # pepper 是服务器端秘密，用于避免 DB 泄露后被离线字典匹配
    # 你可以在 .env 里配置 DEDUPE_PEPPER；未配置时会回退到 JWT_SECRET_KEY（仍然是服务器秘密）
    pepper = (settings.DEDUPE_PEPPER or settings.JWT_SECRET_KEY or "").encode("utf-8")
    if not pepper:
        import secrets
        pepper = secrets.token_bytes(32)

    msg = f"{uid}:{ph}".encode("utf-8")
    return hmac.new(pepper, msg, hashlib.sha256).hexdigest()


