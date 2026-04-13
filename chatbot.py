'''
Telegram Travel Assistant Chatbot with PostgreSQL (Aiven)
Features:
- Attraction recommendation (via HKBU GenAI)
- Real-time weather query (Hong Kong Observatory API)
- User interest memory (PostgreSQL) for personalized recommendations
- Chat logging (PostgreSQL) for data persistence
- Health check endpoint (port 8080)
- Structured JSON logging
'''

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
import configparser
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import time
import psycopg2
import psycopg2.extras
import os
import re

from ChatGPT_HKBU import ChatGPT, get_hk_weather_forecast

gpt = None

# ---------- PostgreSQL connection ----------
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgres://user:password@host:port/db')

def get_db_connection():
    """返回一个 PostgreSQL 连接对象"""
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """创建表结构（如果不存在），并自动修复 user_id 列类型为 BIGINT"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 创建 user_interest 表，user_id 为 BIGINT
    cur.execute('''
        CREATE TABLE IF NOT EXISTS user_interest (
            user_id BIGINT PRIMARY KEY,
            interest TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )
    ''')
    
    # 创建 chat_log 表，user_id 为 BIGINT
    cur.execute('''
        CREATE TABLE IF NOT EXISTS chat_log (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            user_message TEXT NOT NULL,
            bot_response TEXT NOT NULL,
            is_weather_query INTEGER NOT NULL,
            response_time_ms REAL NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL
        )
    ''')
    
    # 检查并修复已存在的表（如果之前用 INTEGER 创建了）
    # 修改 user_interest 表的 user_id 类型
    try:
        cur.execute('''
            ALTER TABLE user_interest 
            ALTER COLUMN user_id TYPE BIGINT
        ''')
        logging.info("Migrated user_interest.user_id to BIGINT")
    except Exception as e:
        # 如果列已经是 BIGINT 或不存在，忽略错误
        pass
    
    try:
        cur.execute('''
            ALTER TABLE chat_log 
            ALTER COLUMN user_id TYPE BIGINT
        ''')
        logging.info("Migrated chat_log.user_id to BIGINT")
    except Exception as e:
        pass
    
    conn.commit()
    cur.close()
    conn.close()
    logging.info("PostgreSQL database initialized (Aiven)")

def save_user_interest(user_id: int, interest: str):
    """保存或更新用户兴趣（upsert）"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO user_interest (user_id, interest, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            interest = EXCLUDED.interest,
            updated_at = EXCLUDED.updated_at
    ''', (user_id, interest, time.time()))
    conn.commit()
    cur.close()
    conn.close()

def get_user_interest(user_id: int):
    """获取用户兴趣，若没有返回 None"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT interest FROM user_interest WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None

def save_chat_log(user_id: int, user_msg: str, bot_resp: str, is_weather: bool, resp_time_ms: float):
    """保存聊天记录"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO chat_log 
        (user_id, user_message, bot_response, is_weather_query, response_time_ms, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (user_id, user_msg, bot_resp[:500], 1 if is_weather else 0, resp_time_ms, time.time()))
    conn.commit()
    cur.close()
    conn.close()

# ---------- Health check server ----------
def start_health_server():
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(b'OK')))
                self.end_headers()
                self.wfile.write(b'OK')
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, format, *args):
            if '/health' not in args[0] if args else True:
                super().log_message(format, *args)
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    logging.info('Health check server started on port 8080')
    server.serve_forever()

# ---------- Main bot logic ----------
def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)
    logging.info('INIT: Loading configuration...')
    config = configparser.ConfigParser()
    config.read('config.ini')
    global gpt
    gpt = ChatGPT(config)

    # 初始化 PostgreSQL 表
    init_db()

    logging.info('INIT: Connecting the Telegram bot...')
    app = ApplicationBuilder().token(config['TELEGRAM']['ACCESS_TOKEN']).build()

    logging.info('INIT: Registering the message handler...')
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, callback))

    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    logging.info('INIT: Initialization done!')
    app.run_polling()

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    user_text = update.message.text
    user_id = update.effective_user.id

    logging.info("UPDATE: " + str(update))
    loading_message = await update.message.reply_text('Thinking...')

    # ---- Step 1: Detect and save user interest ----
    interest_triggers = ["喜欢", "爱好", "兴趣是", "我喜欢", "我爱"]
    interest_keywords = ["爬山", "美食", "历史", "购物", "海滩", "摄影", "露营", "博物馆"]
    saved_interest = None
    if any(trig in user_text for trig in interest_triggers):
        for kw in interest_keywords:
            if kw in user_text:
                saved_interest = kw
                save_user_interest(user_id, saved_interest)
                break

    # ---- Step 2: Detect weather query ----
    weather_keywords = ["天气", "气温", "温度", "会不会下雨", "湿度", "风力",
                        "预报", "天文台", "离岛", "旺角", "香港", "明天", "今天", "未来"]
    is_weather = any(kw in user_text for kw in weather_keywords)

    # ---- Step 3: Retrieve stored interest ----
    user_interest = get_user_interest(user_id)

    # ---- Step 4: Build enhanced prompt ----
    if is_weather:
        weather_info = get_hk_weather_forecast()
        weather_context = f"\n【实时天气数据】\n{weather_info}\n请基于以上数据回答。"
    else:
        weather_context = ""

    interest_hint = ""
    if user_interest:
        interest_hint = f"\n【用户偏好】该用户喜欢{user_interest}。请优先推荐符合此偏好的景点或活动，同时也可以提供其他类型的推荐。"
    if saved_interest and not user_interest:
        interest_hint = f"\n【用户偏好】刚刚用户表示喜欢{saved_interest}。请优先推荐符合此偏好的内容。"

    modified_user_message = f"用户问：{user_text}"
    if interest_hint:
        modified_user_message += interest_hint
    if weather_context:
        modified_user_message += weather_context

    modified_user_message += "\n请回答用户的所有问题（包括景点推荐和天气信息）。如果用户表达了兴趣，请优先推荐相关景点。回答要详细、分类清晰。"

    # 如果用户只表达了兴趣且没有其他问题
    if saved_interest and len(user_text) < 20 and not is_weather and "推荐" not in user_text and "景点" not in user_text:
        await loading_message.edit_text(f"好的，我记下了你喜欢{saved_interest}！下次问景点推荐时会优先考虑。")
        return

    response = gpt.submit(modified_user_message)

    resp_time_ms = (time.time() - start_time) * 1000
    save_chat_log(user_id, user_text, response, is_weather, resp_time_ms)

    log_entry = {
        "timestamp": time.time(),
        "user_id": user_id,
        "user_message": user_text,
        "bot_response": response[:200],
        "response_time_ms": resp_time_ms,
        "weather_query": is_weather,
        "interest": user_interest
    }
    logging.info(json.dumps(log_entry))

    await loading_message.edit_text(response)

if __name__ == '__main__':
    main()