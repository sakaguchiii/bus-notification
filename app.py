from flask import Flask, request
import os
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import LineBotApiError
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

STOP_CODES = {
    "乙部朝日": "4403",
    "藤枝東": "4372",
    "イオンモール津南": "4356",
    "三重会館前": "4008",
    "堀川町": "4402",
    "津駅前": "4001"
}

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

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return 'OK'

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

def request_time_setting(reply_token):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="乗車時刻を「HH:MM」の形式で入力してください（例：08:30）")
    )

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    message_text = event.message.text
    
    status = user_status.get(user_id, {'state': None})
    
    # 「設定開始」と入力した場合、乗車停留所選択ボタンを表示
    if message_text == "設定開始":
        show_boarding_options(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_boarding'}
        
    # 「その他（乗車）」を選んだ後、ユーザーが手入力する流れ
    elif status['state'] == 'awaiting_other_boarding':
        if message_text not in STOP_CODES:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ありません。その停留所は見つかりませんでした。\n別の停留所名を入力してください。")
            )
            return
        # 入力が正しかったら乗車停留所を保存
        user_settings[user_id] = {'boarding': message_text}
        show_alighting_options(event.reply_token)
        user_status[user_id] = {'state': 'awaiting_alighting'}
        
    # 「その他（降車）」を選んだ後、ユーザーが手入力する流れ
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
        
    # 時刻入力待ち
    elif status['state'] == 'awaiting_time':
        try:
            t = datetime.strptime(message_text, '%H:%M')
            user_settings[user_id]['time'] = t
            confirm_settings(event.reply_token, user_id)
            user_status[user_id] = {'state': None}
            schedule_bus_check(user_id, t)
        except ValueError:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正しい時刻形式で入力してください（例：08:30）")
            )

########################################
# 新規追加: PostbackEvent 用ハンドラー
########################################
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    status = user_status.get(user_id, {'state': None})
    
    # 乗車停留所の選択時
    if status['state'] == 'awaiting_boarding':
        if data == 'boarding_otobe':
            user_settings[user_id] = {'boarding': '乙部朝日'}
            show_alighting_options(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_alighting'}
        elif data == 'boarding_fujieda':
            user_settings[user_id] = {'boarding': '藤枝東'}
            show_alighting_options(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_alighting'}
        elif data == 'boarding_other':
            # 「その他」を選んだ場合、テキスト入力で乗車停留所を受け取る
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="乗車する停留所名を入力してください。")
            )
            user_status[user_id] = {'state': 'awaiting_other_boarding'}

    # 降車停留所の選択時
    elif status['state'] == 'awaiting_alighting':
        if data == 'alighting_otobe':
            user_settings[user_id]['alighting'] = '乙部朝日'
            request_time_setting(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_time'}
        elif data == 'alighting_fujieda':
            user_settings[user_id]['alighting'] = '藤枝東'
            request_time_setting(event.reply_token)
            user_status[user_id] = {'state': 'awaiting_time'}
        elif data == 'alighting_other':
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="降車する停留所名を入力してください。")
            )
            user_status[user_id] = {'state': 'awaiting_other_alighting'}

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

def schedule_bus_check(user_id, departure_time):
    """
    当日の「departure_time の7分前」に1回だけ実行するスケジュールを設定する。
    もし departure_time がすでに過ぎている場合は、ここでどうするか別途対応が必要。
    """
    # 今の日時
    now = datetime.now()

    # 乗車予定の時刻が今日の何時何分かを確認
    # 例: departure_time が 18:30 なら 18:30-7分 = 18:23
    check_time_obj = departure_time - timedelta(minutes=7)

    # "HH:MM" 形式の文字列を作る(例: "18:23")
    schedule_time_str = check_time_obj.strftime("%H:%M")

    # schedule ライブラリでは「every().day.at(HH:MM)」が基本。
    # デフォルトだと「すでに過ぎた時間なら翌日実行」になる可能性があるため、
    # 自力で 'next_run' を当日の特定日時に設定する。
    
    job = schedule.every().day.at(schedule_time_str).do(
        lambda: check_bus_location_loop(user_id, departure_time, job)
    )

    # 当日の年月日を now.date() で取得し、そこに check_time_obj の「時:分」を合わせる
    # 「今日の 18:23」を next_run に設定。
    # ただし、もし now > 18:23 を過ぎていたら時間がズレるので注意。
    target_datetime = datetime.combine(now.date(), check_time_obj.time())

    # もしすでに過ぎている場合はどうする？ -> ここでキャンセルする/翌日にする etc.
    if target_datetime < now:
        print("[DEBUG] すでに設定時刻を過ぎています。今回は実行しません。")
        return

    # ジョブの「次回実行時刻」を強制的に当日の target_datetime にする
    job.next_run = target_datetime

    print(f"[DEBUG] スケジュール設定: 当日 {job.next_run} に1回だけチェックを実行予定")


def check_bus_location_loop(user_id, departure_time, job):
    """
    実際にバス位置情報を15秒おきに監視するが、
    今回は乗車時刻 + 5分まで監視し終わったらジョブをキャンセル。
    """
    print(f"[DEBUG] 監視開始: user_id={user_id}")

    end_time = departure_time + timedelta(minutes=5)
    while datetime.now() < end_time:
        check_bus_location(user_id)
        time.sleep(15)

    # ループを抜けたら、このジョブをキャンセルして再実行しないようにする
    schedule.cancel_job(job)
    print("[DEBUG] 監視終了＆当日ジョブキャンセル")


if __name__ == "__main__":
    app.run(port=5000)
