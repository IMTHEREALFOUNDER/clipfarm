#!/usr/bin/env python3
"""
auth.py — один раз на каждый YouTube-канал: получить токен для автозагрузки.

1. Положи рядом client_secret.json (скачивается в Google Cloud → Credentials → OAuth client → Desktop app).
2. python auth.py
3. В браузере войди в Google-аккаунт и ВЫБЕРИ НУЖНЫЙ КАНАЛ.
4. Скрипт напечатает название канала и токен — добавь их в секрет YT_TOKENS на GitHub.
"""
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Установи библиотеки: pip install google-auth-oauthlib google-api-python-client")
    secret = Path(__file__).with_name("client_secret.json")
    if not secret.exists():
        sys.exit("Не найден client_secret.json рядом со скриптом (см. README, шаг про Google Cloud).")

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    name = items[0]["snippet"]["title"] if items else "(канал не найден — создай канал на YouTube)"
    cid = items[0]["id"] if items else ""

    acc_id = input("\nКороткое имя аккаунта для config.json (латиницей, например main): ").strip() or "main"
    print("\n" + "=" * 60)
    print(f"Канал: {name}  ({cid})")
    print(f"Аккаунт в config.json: {acc_id}")
    print("\nДобавь в секрет YT_TOKENS (это JSON со всеми каналами) строку:")
    print(json.dumps({acc_id: creds.refresh_token}))
    print("=" * 60)
    print("Если каналов несколько — объедини их в один JSON: {\"main\": \"...\", \"acc2\": \"...\"}")


if __name__ == "__main__":
    main()
