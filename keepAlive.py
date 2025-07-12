from flask import Flask
from threading import Thread
import requests
import time
import os

app = Flask('')


@app.route('/')
def home():
    return "I'm alive!"


def run():
    app.run(host='0.0.0.0', port=8080)


def ping_self():
    while True:
        try:
            url = "https://test-ujwm.onrender.com"
            requests.get(url)
            print("✅ Self-ping sent")
        except Exception as e:
            print(f"⚠️ Self-ping failed: {e}")
        time.sleep(240)  # 4 minutes


def keep_alive():
    Thread(target=run).start()
    Thread(target=ping_self).start()
