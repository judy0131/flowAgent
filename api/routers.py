from datetime import datetime
from typing import Optional, List, Any, Dict

import httpx
from asyncmy import errors
from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form, Header, Depends
from langchain_core.messages import HumanMessage, AIMessage
from starlette.responses import StreamingResponse, JSONResponse

import asyncmy
import yaml
from asyncmy.cursors import DictCursor
from asyncmy.pool import Pool

from api.models import ChatRequest, ChatResponse
from agent.agent_service import get_mcp_service
import logging

from core.config import settings
from tools.aes_util import aes_encrypt_cbc

logger = logging.getLogger(__name__)

async_session: Optional[httpx.AsyncClient] = None

# 创建 APIRouter 实例，设置统一前缀 /api
router = APIRouter(
    prefix="/api",
    tags=["FastMCP Chat API"],
)

DB_CONFIG = {
    'host': settings.database.host,
    'port': settings.database.port,
    'user': settings.database.user,
    'password': settings.database.password,
    'database': settings.database.database_name
}

db_pool: Optional[Pool] = None

# --- 异步 DB 连接池管理  ---
async def init_db_pool():
    """初始化全局 asyncmy 数据库连接池。"""
    global db_pool
    # 注意：如果 DB_CONFIG 依赖真实的 settings，这里可能会在没有 DB 环境时失败
    db_pool = await asyncmy.create_pool(**DB_CONFIG, minsize=5, maxsize=10)
    logging.info("数据库连接池初始化成功。")

async def close_db_pool():
    """关闭全局 asyncmy 数据库连接池。"""
    global db_pool
    if db_pool:
        await db_pool.close()
        logging.info("数据库连接池已关闭。")

@router.post("/chat/stream")
async def chat_endpoint_stream(request: ChatRequest):
    """
    流式聊天接口
    """
    logger.info(f"Stream request for session: {request.session_id}")

    mcp_service = get_mcp_service()

    # 定义一个生成器适配器
    async def event_generator():
        try:
            # 调用 Service 的流式方法
            async for token in mcp_service.stream_mcp_agent(request):
                # 直接 yield 文本 (或者封装成 SSE 格式)
                yield token
        except Exception as e:
            yield f"Error: {e}"

    # 返回流式响应
    # media_type="text/plain" 或者是 "text/event-stream" (SSE)
    # 对于简单的打字机效果，text/plain 最简单，前端直接追加字符串即可
    return StreamingResponse(event_generator(), media_type="text/plain")


@router.post("/uploadFile")
async def upload_file(
        # 1. 接收文件
        file: UploadFile = File(..., description="上传的文件"),

        # 2. 接收表单参数
        unzip: Optional[bool] = Form(False),
        token: Optional[str] = Form(None),

        session_id: str = Form(..., alias="session_id"),
        user_id: Optional[str] = Form(None, alias="user_id"),
):
    """
    文件上传接口
    """
    # 确保 DB 连接池已初始化
    if not db_pool:
        logging.warning("数据库连接池未初始化。尝试初始化")
        await init_db_pool()

    # --- Step 1 & 2 & 3: Token 校验逻辑 (复刻 Java 逻辑) ---
    global final_token

    login_info = await jwt_login('admin', 'EyJo4TdK&Fn7')
    if login_info and 'token' in login_info:
        final_token = login_info['token']
    else:
        logging.error("无法获取登录令牌，流程列表请求终止。")
        return None

    if not final_token:
        # 对应 Java: return "无效的Authorization头"
        # FastAPI 推荐抛出 HTTP 异常
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 Authorization 头或 Token 参数"
        )

    # --- Step 4: 准备远程转发 ---

    # 构造远程 URL
    upload_url = f"{settings.PIFLOW_URL}{settings.UPLOAD_FILE_PATH}"

    # 构造表单参数 (MultiValueMap)
    # Java代码: unzip.toString() -> 这里转为字符串 "true"/"false"
    payload = {
        "unzip": str(unzip).lower(),
        "associateType": "4",
        "associateId": session_id,
        "flowPublishId": session_id
    }

    # 读取文件内容 (因为 UploadFile 是 SpooledTemporaryFile，需要读取后转发)
    file_content = await file.read()

    # 构造文件参数 files
    # 格式: {"field_name": (filename, content, content_type)}
    files = {
        "file": (file.filename, file_content, file.content_type)
    }

    # 构造请求头
    headers = {
        "Authorization": f"Bearer {final_token}"
    }

    try:
        # --- Step 5 & 6: 发送 POST 请求到远程服务器 ---
        async with httpx.AsyncClient() as client:
            logger.info(f"Forwarding upload to: {upload_url}")

            response = await client.post(
                upload_url,
                data=payload,
                files=files,
                headers=headers,
                timeout=60.0  # 根据文件大小调整超时时间
            )

            response.raise_for_status()  # 如果状态码不是 2xx，抛出异常

            resp_text = response.text
            logger.info(f"Remote response: {resp_text}")

            # --- Step 7: 解析 JSON 响应 ---
            resp_json = response.json()
            # 假设结构是 {"data": {"filePath": "..."}}
            file_path = resp_json.get("data", {}).get("filePath")

            if not file_path:
                logger.error("Remote response missing filePath")
                raise HTTPException(status_code=500, detail="远程服务未返回文件路径")

            logger.info(f"Extracted filePath: {file_path}")

            # --- Step 8: 保存到数据库 ---

            # 处理 userId 默认值
            final_user_id = user_id if user_id else "001"

            # ---------------------------------------------------------
            # 逻辑：尝试插入，如果 (session_id, file_name) 冲突，则更新 storage_path 和 updated_time
            # ---------------------------------------------------------
            values = (final_user_id, session_id, file.filename, file_path, 0, datetime.now(), datetime.now())

            async with db_pool.acquire() as conn:
                try:
                    async with conn.cursor() as cursor:
                        # 1. 先尝试删除旧记录（逻辑覆盖）
                        delete_sql = "DELETE FROM mcp_user_upload_file WHERE session_id = %s AND file_name = %s"
                        await cursor.execute(delete_sql, (session_id, file.filename))

                        # 2. 插入新记录
                        insert_sql = """
                        INSERT INTO mcp_user_upload_file 
                            (user_id, session_id, file_name, storage_path, is_deleted, created_time, updated_time)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """
                        await cursor.execute(insert_sql, values)

                    await conn.commit()
                    logging.info(f"数据覆盖保存成功！")
                except errors.Error as err:
                    logging.error(f"数据库操作失败: {err}")
                    await conn.rollback()

            # --- Step 9: 通知Agent 文件传好了 ---
            prompt_text = f"我已上传文件，文件名为：{file.filename}。"
            fake_ai_reply = "收到，文件已上传成功。"

            mcp_service = get_mcp_service()
            if mcp_service:
                # 手动构造消息对
                new_messages = [
                    HumanMessage(content=prompt_text),
                    AIMessage(content=fake_ai_reply)
                ]

                # 直接调用 Service 里的 session_manager 存库
                history = mcp_service._session_manager.load_memory(session_id)
                updated_history = history + new_messages
                mcp_service._session_manager.save_complete_history(session_id, updated_history)

            # --- Return: 返回文件路径 ---
            return JSONResponse(content={"code": 200, "data": file_path, "msg": "上传成功"})

    except httpx.RequestError as e:
        logger.error(f"Network error requesting remote server: {e}")
        raise HTTPException(status_code=502, detail="文件上传转发失败: 网络错误")
    except httpx.HTTPStatusError as e:
        logger.error(f"Remote server returned error: {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail="远程文件服务器报错")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"code": 500, "msg": "文件上传失败"}
        )
    finally:
        # 重要：关闭文件句柄
        await file.close()

async def execute_query(sql: str, params: tuple) -> List[Dict[str, Any]]:
    """执行通用的异步查询操作。"""
    # 注意：这里最好加锁或在应用启动时初始化，避免并发下的多次初始化
    if not db_pool:
        logging.warning("数据库连接池未初始化，尝试初始化...")
        await init_db_pool()

    results = []
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cursor:
                await cursor.execute(sql, params)
                results = await cursor.fetchall()
    except asyncmy.Error as err:
        logging.error(f"数据库查询失败: {err} | SQL: {sql}")
        return []

    return results

# 0：异步登录并获取 JWT Token
async def jwt_login(username, password) -> Optional[Dict[str, Any]]:
    if not async_session:
        await init_async_session()

    try:
        # 使用占位符进行加密
        aes_password = aes_encrypt_cbc(password, "ABCDEFGHIJKL_key", "ABCDEFGHIJKLM_iv")

        login_param = {"username": username, "password": aes_password}

        login_url = f'''http://10.0.87.109:6005/piflow-web/jwtLogin'''
        response = await async_session.post(login_url, data=login_param, timeout=10)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logging.error(f"登录失败，HTTP 状态码: {e.response.status_code}, 详情: {e.response.text}")
        return None
    except Exception as e:
        logging.error(f"登录请求发生异常: {e}")
        return None

async def create_async_session() -> httpx.AsyncClient:
    """创建并配置全局 httpx 异步会话。"""
    limits = httpx.Limits(
        max_connections=50,
        max_keepalive_connections=20
    )
    return httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5),
        follow_redirects=True
    )

async def init_async_session():
    """初始化全局 httpx 异步会话。"""
    global async_session
    if async_session is None:
        async_session = await create_async_session()
        logging.info("异步 HTTP 会话初始化成功。")
        if async_session is None:
            logging.error("致命错误：create_async_session 返回 None。")


async def close_async_session():
    """关闭全局 httpx 异步会话。"""
    global async_session
    if async_session:
        await async_session.aclose()
        async_session = None
        logging.info("异步 HTTP 会话已关闭。")