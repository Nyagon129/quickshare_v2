import os
from pydantic_settings import BaseSettings
from urllib.parse import quote_plus

class Settings(BaseSettings):
    APP_NAME: str = "文件闪传系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # 数据库配置（优先从环境变量读取，如果没有则使用默认值）
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "quick_share_datagrip")
    
    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: list = ["*"]
    
    # Redis 配置（可选，用于缓存和会话存储）
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD", "")
    REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))
    REDIS_ENABLED: bool = os.getenv("REDIS_ENABLED", "false").lower() == "true"

    # JWT配置
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # 去重指纹 pepper（服务器端秘密）
    # - 用途：从 (user_id + 明文文件哈希) 派生 dedupe fingerprint
    # - 建议：生产环境务必配置为随机高熵值，并妥善保管（不要泄露到日志/前端）
    # - 说明：未配置时将回退使用 JWT_SECRET_KEY（仍是服务器端秘密）
    DEDUPE_PEPPER: str = os.getenv("DEDUPE_PEPPER", "")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
    
    @property
    def DATABASE_URL(self) -> str:
        """构建数据库连接URL，自动处理特殊字符"""
        # 对用户名和密码进行URL编码，处理特殊字符
        encoded_user = quote_plus(self.DB_USER)
        encoded_password = quote_plus(self.DB_PASSWORD) if self.DB_PASSWORD else ""
        
        if encoded_password:
            return f"mysql+pymysql://{encoded_user}:{encoded_password}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        else:
            return f"mysql+pymysql://{encoded_user}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"

settings = Settings()
