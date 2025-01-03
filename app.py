# main.py
from flask import Flask, request
import os
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    TemplateSendMessage, ButtonsTemplate, PostbackAction,
    PostbackEvent
)
import schedule
import time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
BUSVISION_BASE_URL = "https://bus-vision.jp/sanco/view/"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 【更新】停留所コードの追加
STOP_CODES = {
    "乙部朝日": "4403",
    "藤枝東": "4372",
    "イオンモール津南": "4356",
    "三重会館前": "4008",
    "堀川町": "4402",
    "津駅前": "4001"
}

# 【新規】Bus-Vision操作用クラス
class BusVisionSession:
    def __init__(self):
        self.session = requests.Session()
        self.last_approach_info = None

    def get_stop_code(self, stop_name):
        return STOP_CODES.get(stop_name)

    def search_bus(self, from_stop, to_stop):
        from_code = self.get_stop_code(from_stop)
        to_code = self.get_stop_code(to_stop)
        
        if not (from_code and to_code):
            return None

        approach_url = f"{BUSVISION_BASE_URL}approach.html"
        params = {
            'stopCdFrom': from_code,
            'stopCdTo': to_code,
            'addSearchDetail': 'false',
            'searchHour': 'null',
            'searchMinute': 'null',
            'searchAD': '-1',
            'searchVehicleTypeCd': 'null',
            'searchCorpCd': 'null',
            'lang': '0'
        }
        
        try:
            response = self.session.get(approach_url, params=params)
            return response.text
        except Exception as e:
            print(f"Error searching bus: {e}")
            return None

    def extract_bus_info(self, html_content):
        if not html_content:
            return None

        soup = BeautifulSoup(html_content, 'html.parser')
        approach_info = soup.find('div', class_='approach-info')
        
        if approach_info:
            current_info = approach_info.get_text(strip=True)
            if current_info != self.last_approach_info:
                self.last_approach_info = current_info
                return current_info
        return None

bus_session = BusVisionSession()
user_settings = {}
user_status = {}

# 【変更なし】Webhook受信処理
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return 'OK'

# 【変更なし】メッセージ表示関数
def show_boarding_options(reply_token):
    buttons_template = ButtonsTemplate(
        title='乗車停留所を選択してください',
        text='以下から選択してください',
        actions=[
            PostbackAction(label='乙部朝日', data='boarding_otobe'),
            PostbackAction(label='藤枝東', data='boarding_fujieda'),
            PostbackAction(label='その他', data='boarding_other')
        ]
    )
    template_message = TemplateSendMessage(
        alt_text='乗車停留所の選択',
        template=buttons_template
    )
    line_bot_api.reply_message(reply_token, template_message)

# 【変更なし】降車停留所選択表示
def show_alighting_options(reply_token):
    buttons_template = ButtonsTemplate(
        title='降車停留所を選択してください',
        text='以下から選択してください',
        actions=[
            PostbackAction(label='乙部朝日', data='alighting_otobe'),
            PostbackAction(label='藤枝東', data='alighting_fujieda'),
            PostbackAction(label='その他', data='alighting_other')
        ]
    )
    template_message = TemplateSendMessage(
        alt_text='降車停留所の選択',
        template=buttons_template
    )
    line_bot_api.reply_message(reply_token, template_message)

# 【変更なし】時刻入力リクエスト
def request_time_setting(reply_token):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="乗車時刻を「HH:MM」の形式で入力してください（例：08:30）")
    )

# 【変更なし】設定確認メッセージ
def confirm_settings(reply_token, user_id):
    settings = user_settings[user_id]
    confirmation_text = f"""
設定が完了しました：
乗車停留所: {settings['boarding']}
降車停留所: {settings['alighting']}
乗車時刻: {settings['time'].strftime('%H:%M')}

乗車時刻の7分前からバスの位置情報の監視を開始します。
"""
    line_bot_api.reply_message(reply_token, TextSendMessage(text=confirmation_text))

# 【更新】メッセージハンドラー
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    message_text = event.message.text
    
    status = user_status.get(user_id, {'state': None})
    
    if message_text == "設定開始":
        show_boarding_options(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_boarding'}
        
    elif status['state'] == 'awaiting_other_boarding':
        if message_text not in STOP_CODES:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ありません。その停留所は見つかりませんでした。\n別の停留所名を入力してください。")
            )
            return
        
        user_settings[user_id] = {'boarding': message_text}
        show_alighting_options(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_alighting'}
        
    elif status['state'] == 'awaiting_other_alighting':
        if message_text not in STOP_CODES:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ありません。その停留所は見つかりませんでした。\n別の停留所名を入力してください。")
            )
            return
        
        user_settings[user_id]['alighting'] = message_text
        request_time_setting(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_time'}
        
    elif status['state'] == 'awaiting_time':
        try:
            time = datetime.strptime(message_text, '%H:%M')
            user_settings[user_id]['time'] = time
            confirm_settings(event.reply_token, user_id)
            user_status[user_id] = {'state': None}
            schedule_bus_check(user_id, time)
        except ValueError:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正しい時刻形式で入力してください（例：08:30）")
            )

# 【更新】バス位置情報チェック関数
def check_bus_location(user_id):
    settings = user_settings.get(user_id)
    if not settings:
        return

    html_content = bus_session.search_bus(
        settings['boarding'],
        settings['alighting']
    )
    
    bus_info = bus_session.extract_bus_info(html_content)
    if bus_info:
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=f"バス位置情報更新:\n{bus_info}")
            )
        except LineBotApiError as e:
            print(f"Error sending LINE message: {e}")

# 【変更なし】スケジュール設定
def schedule_bus_check(user_id, departure_time):
    check_time = departure_time - timedelta(minutes=7)
    schedule.every().day.at(check_time.strftime("%H:%M")).do(
        lambda: check_bus_location_loop(user_id)
    )

# 【更新】チェックループ
def check_bus_location_loop(user_id):
    end_time = user_settings[user_id]['time'] + timedelta(minutes=30)
    while datetime.now() < end_time:
        check_bus_location(user_id)
        time.sleep(15)

if __name__ == "__main__":
    app.run(port=5000)