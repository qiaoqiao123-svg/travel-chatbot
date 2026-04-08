'''
This program requires the following modules:
- python-telegram-bot==22.5
- urllib3==2.6.2
'''
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
import configparser
import logging

from ChatGPT_HKBU import ChatGPT, get_hk_weather_forecast  # 导入天气函数

gpt = None


def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    logging.info('INIT: Loading configuration...')
    config = configparser.ConfigParser()
    config.read('config.ini')
    global gpt
    gpt = ChatGPT(config)

    logging.info('INIT: Connecting the Telegram bot...')
    app = ApplicationBuilder().token(config['TELEGRAM']['ACCESS_TOKEN']).build()

    logging.info('INIT: Registering the message handler...')
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, callback))

    logging.info('INIT: Initialization done!')
    app.run_polling()


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("UPDATE: " + str(update))
    loading_message = await update.message.reply_text('Thinking...')

    user_text = update.message.text

    # 判断是否为天气查询（简单关键词匹配）
    weather_keywords = ["天气", "气温", "温度", "会不会下雨", "湿度", "风力",
                        "预报", "天文台", "离岛", "旺角", "香港", "明天", "今天", "未来"]
    is_weather_query = any(kw in user_text for kw in weather_keywords)

    if is_weather_query:
        # 获取真实天气预报
        weather_info = get_hk_weather_forecast()
        # 将天气信息与用户问题拼接，要求 AI 基于真实数据回答
        modified_user_message = (
            f"用户问：{user_text}\n\n"
            f"【以下是来自香港天文台的权威天气预报】\n{weather_info}\n\n"
            f"请严格基于上述天气数据回答用户的问题，不要编造数据。"
            f"如果用户询问特定区域（如旺角东、离岛区），可以基于预报数据给出合理推断，并提醒用户参考天文台实时信息。"
        )
    else:
        modified_user_message = user_text

    response = gpt.submit(modified_user_message)
    await loading_message.edit_text(response)


if __name__ == '__main__':
    main()