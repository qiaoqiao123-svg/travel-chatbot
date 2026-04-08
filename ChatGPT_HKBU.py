import requests
import configparser
from datetime import datetime


# A simple client for the ChatGPT REST API
class ChatGPT:
    def __init__(self, config):
        # Read API configuration values from the ini file
        api_key = config['CHATGPT']['API_KEY']
        base_url = config['CHATGPT']['BASE_URL']
        model = config['CHATGPT']['MODEL']
        api_ver = config['CHATGPT']['API_VER']

        # Construct the full REST endpoint URL for chat completions
        self.url = f'{base_url}/deployments/{model}/chat/completions?api-version={api_ver}'

        # Set HTTP headers required for authentication and JSON payload
        self.headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "api-key": api_key,
        }

        # Define the system prompt to guide the assistant’s behavior
        self.system_message = (
            'You are a travel assistant chatbot. You have two main capabilities:\n'
            '1. Recommending tourist attractions (any city, with detailed advice).\n'
            '2. Answering weather queries (current weather or forecast for a given city).\n\n'
            'For attraction recommendations, keep your current helpful style: '
            'categorize, give specific places, include transportation tips, and provide practical advice.\n\n'
            'For weather queries, if the user asks about weather, you will be given real-time weather data '
            'from the Hong Kong Observatory. Use that data to answer accurately and concisely.\n\n'
            'Do not change your attraction recommendation style. Only add weather handling.'
        )

    def submit(self, user_message: str):
        # Build the conversation history: system + user message
        messages = [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": user_message},
        ]

        # Prepare the request payload with generation parameters
        payload = {
            "messages": messages,
            "temperature": 1,
            "max_tokens": 300,  # 增加一点，让天气回答更详细
            "top_p": 1,
            "stream": False
        }

        response = requests.post(self.url, json=payload, headers=self.headers)

        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            return "Error: " + response.text


# ---------- 新增天气函数 ----------
def get_hk_weather_forecast():
    """
    调用香港天文台 API 获取未来7天天气预报，返回格式化的文本。
    """
    url = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        forecast_list = data.get("weatherForecast", [])
        if not forecast_list:
            return "暂时无法获取香港天气预报。"

        lines = []
        for day in forecast_list[:7]:
            date = day.get("forecastDate")
            if date and len(date) == 8:
                date_str = f"{date[4:6]}/{date[6:8]}"  # 转为 MM/DD 格式
            else:
                date_str = date
            week = day.get("week", "")
            weather = day.get("forecastWeather", "")
            temp_min = day.get("forecastMintemp", {}).get("value", "?")
            temp_max = day.get("forecastMaxtemp", {}).get("value", "?")
            wind = day.get("forecastWind", "")
            lines.append(f"{date_str}（{week}）：{weather}，{temp_min}~{temp_max}°C，{wind}")

        return "未来7天香港天气预报：\n" + "\n".join(lines)
    except Exception as e:
        return f"天气服务暂时不可用（{str(e)}）"