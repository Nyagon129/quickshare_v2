from datetime import datetime
from sqlalchemy import Column, String, Integer, Enum, DateTime, ForeignKey
from .base import Base


class PickupCode(Base):
    __tablename__ = 'pickup_codes'

    code = Column(String(6), primary_key=True, comment='6位查找码（取件码前6位，大写字母+数字）')
    file_id = Column(Integer, ForeignKey('files.id', ondelete='CASCADE'), nullable=False, comment='关联文件ID')
    status = Column(Enum('waiting', 'transferring', 'completed', 'expired'), default='waiting', comment='取件码状态')
    used_count = Column(Integer, default=0, comment='已使用次数')
    limit_count = Column(Integer, default=3, comment='最大使用次数（999=无限）')
    uploader_ip = Column(String(45), comment='上传者IP')
    expire_at = Column(DateTime, nullable=False, comment='过期时间')
    created_at = Column(DateTime, comment='创建时间')
    updated_at = Column(DateTime, comment='更新时间')