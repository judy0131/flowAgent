# /core/config.py

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, BaseModel  # 确保导入 BaseModel
from typing import Optional, Dict, Any
import os
import yaml
import logging

# ----------------------------------------------------------------------
# 初始化日志和路径常量
# ----------------------------------------------------------------------

# 配置基本的日志，确保在应用日志系统完全初始化前，配置加载的日志能打印出来
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# 定义项目根目录：假设此文件位于 /core 目录下，项目根目录在其父级的父级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_YAML_PATH = os.path.join(PROJECT_ROOT, 'configs', 'config.yaml')


# ----------------------------------------------------------------------
# 辅助函数：加载 YAML 配置
# ----------------------------------------------------------------------
def load_config_from_yaml() -> Dict[str, Any]:
    """
    尝试从项目根目录下的 'configs/config.yaml' 加载配置。
    如果失败，返回空字典。
    """
    if not os.path.exists(CONFIG_YAML_PATH):
        logger.warning(f"配置文件 {CONFIG_YAML_PATH} 不存在。将只使用 .env、环境变量和硬编码默认值。")
        return {}

    try:
        with open(CONFIG_YAML_PATH, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)
            logger.info(f"成功加载配置：{CONFIG_YAML_PATH}")
            return yaml_config
    except Exception as e:
        logger.error(f"加载 YAML 配置文件失败: {e}", exc_info=True)
        return {}


YAML_CONFIG = load_config_from_yaml()


# ----------------------------------------------------------------------
# 配置模型 (Pydantic)
# ----------------------------------------------------------------------

# 🆕 新增 FastMCP Tool Server 配置
class FastMCPToolsSettings(BaseModel):
    """FastMCP Tool Server 的连接配置。"""
    SERVER_URL: str = Field("http://localhost:8000", description="FastMCP Tool Server 的基础 URL")
    CLIENT_ENDPOINT: str = Field("/mcp", description="FastMCP Tool Server 的客户端入口点")


class Settings(BaseSettings):
    """
    应用的主配置模型，从环境变量、.env 文件和 config.yaml 加载。
    """
    # --- LLM Provider
    LLM_PROVIDER: str = Field("alibaba", description="要使用的 LLM 服务商 (openai/alibaba/other)")

    # OpenAI 配置
    OPENAI_API_KEY: str = Field("", description="OpenAI API Key")
    OPENAI_MODEL_NAME: str = Field("gpt-4o-mini", description="OpenAI 模型名称")

    # Alibaba Tongyi 配置
    TONGYI_API_KEY: str = Field("", description="阿里云通义 API Key")
    TONGYI_MODEL_NAME: str = Field("qwen-max", description="通义模型名称")

    # --- FastAPI/Uvicorn 配置 ---
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "info"
    DEBUG: bool = Field(False, description="是否启用调试模式（打印完整会话历史）")

    PIFLOW_URL: str = Field("http://10.0.87.109:6005", description="PiFlow 服务的基础 URL")
    UPLOAD_FILE_PATH: str = Field("/data/uploads", description="上传文件的存储路径")

    # --- 数据库配置 (核心依赖) ---
    class DatabaseSettings(BaseModel):
        host: str = Field("localhost")
        port: int = Field(3306)
        user: str = Field("user")
        password: str = Field("password")
        database_name: str = Field("db_name")

    database: DatabaseSettings = Field(
        default_factory=lambda: Settings.DatabaseSettings(**YAML_CONFIG.get('database', {}))
    )


    # 🆕 FastMCP 配置
    fastmcp: FastMCPToolsSettings = Field(
        default_factory=lambda: FastMCPToolsSettings(**YAML_CONFIG.get('fastmcp', {}))
    )

    model_config = SettingsConfigDict(
        env_file=('.env', '.env.local'),
        env_file_encoding='utf-8',
        extra='ignore'  # 忽略配置中的额外字段
    )


settings = Settings(**YAML_CONFIG)