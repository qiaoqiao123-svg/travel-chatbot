'''
Telegram Travel Assistant Chatbot
Features:
- Attraction recommendation (via HKBU GenAI)
- Real-time weather query (Hong Kong Observatory API)
- User interest memory (SQLite) for personalized recommendations
- Chat logging (SQLite) for data persistence
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
import sqlite3
import re

from ChatGPT_HKBU import ChatGPT, get_hk_weather_forecast

gpt = None
DB_PATH = 'chatbot.db'

# ---------- Database functions ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_interest
                 (user_id INTEGER PRIMARY KEY, interest TEXT, updated_at REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  user_message TEXT,
                  bot_response TEXT,
                  is_weather_query INTEGER,
                  response_time_ms REAL,
                  timestamp REAL)''')
    conn.commit()
    conn.close()
    logging.info("Database initialized")

def save_user_interest(user_id: int, interest: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO user_interest (user_id, interest, updated_at) VALUES (?, ?, ?)",
              (user_id, interest, time.time()))
    conn.commit()
    conn.close()

def get_user_interest(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT interest FROM user_interest WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_chat_log(user_id: int, user_msg: str, bot_resp: str, is_weather: bool, resp_time_ms: float):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO chat_log 
                 (user_id, user_message, bot_response, is_weather_query, response_time_ms, timestamp)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id, user_msg, bot_resp[:500], 1 if is_weather else 0, resp_time_ms, time.time()))
    conn.commit()
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

    # ---- Step 1: Detect and save user interest (do NOT interrupt) ----
    interest_triggers = ["喜欢", "爱好", "兴趣是", "我喜欢", "我爱"]
    interest_keywords = ["爬山", "美食", "历史", "购物", "海滩", "摄影", "露营", "博物馆"]
    saved_interest = None
    if any(trig in user_text for trig in interest_triggers):
        for kw in interest_keywords:
            if kw in user_text:
                saved_interest = kw
                save_user_interest(user_id, saved_interest)
                # 保存后不立即回复，而是继续处理用户请求。但可以附加一条提示信息到最终回答中
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

    # 构建兴趣提示
    interest_hint = ""
    if user_interest:
        interest_hint = f"\n【用户偏好】该用户喜欢{user_interest}。请优先推荐符合此偏好的景点或活动，同时也可以提供其他类型的推荐。"
    if saved_interest and not user_interest:
        interest_hint = f"\n【用户偏好】刚刚用户表示喜欢{saved_interest}。请优先推荐符合此偏好的内容。"

    # 如果用户既表达了兴趣又要求推荐/天气，确保两者都处理
    modified_user_message = f"用户问：{user_text}"
    if interest_hint:
        modified_user_message += interest_hint
    if weather_context:
        modified_user_message += weather_context

    # 附加指令：要求回答完整，不要忽略任何部分
    modified_user_message += "\n请回答用户的所有问题（包括景点推荐和天气信息）。如果用户表达了兴趣，请优先推荐相关景点。回答要详细、分类清晰。"

    # 如果用户只表达了兴趣且没有其他问题，可以简短确认（但这里不单独处理，交给LLM也可以）
    # 为了避免LLM过度冗长，如果用户消息只有兴趣表达（无其他内容），可以特殊处理
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