from datetime import datetime
from sqlalchemy import Column, Integer, String, BigInteger, DateTime, ForeignKey, Boolean
from .base import Base


class File(Base):
    __tablename__ = 'files'

    id = Column(Integer, autoincrement=True, primary_key=True, comment='文件唯一ID')
    original_name = Column(String(255), nullable=False, comment='文件原始名称')
    stored_name = Column(String(36), nullable=False, comment='存储用UUID文件名')
    size = Column(BigInteger, nullable=False, comment='文件大小(字节)')
    hash = Column(String(64), comment='文件去重指纹(HMAC-SHA256)')
    mime_type = Column(String(100), comment='文件MIME类型')
    uploader_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, comment='分享者用户ID')
    is_invalidated = Column(Boolean, default=False, comment='是否已被废弃')
    created_at = Column(DateTime, comment='创建时间')
    updated_at = Column(DateTime, comment='更新时间')