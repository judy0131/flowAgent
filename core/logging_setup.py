import logging
import sys
from typing import Literal
from .config import settings  # 导入我们之前创建的配置


def setup_logging(log_level: Literal["debug", "info", "warning", "error", "critical"] = settings.LOG_LEVEL):
    """
    配置应用的核心日志系统。

    Args:
        log_level: 日志的最低级别。
    """

    # 1. 根日志配置
    root_logger = logging.getLogger()
    # 确保根日志级别设置为配置中的级别
    root_logger.setLevel(log_level.upper())

    # 2. 定义日志格式
    formatter = logging.Formatter(
        # 使用颜色、时间、级别、模块名和消息
        fmt="%(levelname)s:     %(asctime)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 3. 创建控制台处理器 (Handler)
    # 使用 StreamHandler 将日志输出到标准错误流 (stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)

    # 4. 清除现有处理器并添加新处理器
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    root_logger.addHandler(console_handler)

    # 5. 针对特定模块进行优化
    # 降低一些冗余模块的日志级别
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 抑制 HTTP 客户端的调试信息
    logging.getLogger("httpcore").setLevel(logging.WARNING)  # 抑制 HTTP 核心库的调试信息

    logging.info(f"Logging initialized with level: {log_level.upper()}")

# 在应用启动时，只需要在 main.py 中调用 setup_logging() 即可。