#!/usr/bin/env python3
"""
pipeline.py — ежедневный конвейер нарезок под несколько аккаунтов.

Каждый запуск:
  1. Берёт исходники кампании (ссылки Google Диска / файлы) из config.json.
  2. Нарезает НОВЫЕ моменты (уже использованные отрезки запоминаются в state.json).
  3. Раздаёт ролики по аккаунтам — у каждого аккаунта свои ролики, без повторов.
  4. Публикует:
       mode = "youtube"  — загрузка через YouTube Data API с публикацией по слотам (publishAt);
       mode = "telegram" — присылает готовые ролики с заголовком и временем публикации в Telegram;
       mode = "local"    — только нарезать в папку work/out.
  5. Шлёт отчёт в Telegram: ссылки для отправки в Клипни.

Запуск:  python pipeline.py            (обычный)
         python pipeline.py --dry-run  (ничего не публикует, только нарезка и план)
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"]


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ источники
def drive_file_id(url):
    for pat in (r"/file/d/([\w-]{20,})", r"[?&]id=([\w-]{20,})", r"/open\?id=([\w-]{20,})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def source_key(src):
    fid = drive_file_id(src) if src.startswith("http") else None
    if fid:
        return fid
    return re.sub(r"[^\w\-]+", "_", Path(src).stem if not src.startswith("http") else src)[-60:]


def fetch_source(src, dest_dir):
    """Скачивает исходник (если ещё не скачан) и возвращает путь к файлу."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not src.startswith("http"):
        p = (ROOT / src) if not Path(src).is_absolute() else Path(src)
        if not p.exists():
            raise FileNotFoundError(src)
        return p
    key = source_key(src)
    out = dest_dir / f"{key}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    fid = drive_file_id(src)
    if fid:
        import gdown
        log(f"Скачиваю с Google Диска: {fid}")
        res = gdown.download(id=fid, output=str(out), quiet=False)
        if not res or not out.exists():
            raise RuntimeError(f"Не удалось скачать {src}. Проверь, что у файла доступ «Все, у кого есть ссылка».")
        return out
    log(f"Скачиваю по ссылке: {src}")
    subprocess.run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
                    "-o", str(out), src], check=True)
    return out


VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


def drive_folder_id(url):
    m = re.search(r"/folders/([\w-]{20,})", url)
    return m.group(1) if m else None


def expand_sources(sources, state):
    """Ссылку на папку Google Диска превращает в список ссылок на видео внутри неё (с подпапками)."""
    out = []
    for src in sources:
        fid = drive_folder_id(src) if src.startswith("http") else None
        if not fid:
            out.append(src)
            continue
        cache = state.setdefault("folders", {}).get(fid)
        files = None
        try:
            import gdown
            try:  # gdown >= 5.2 умеет только получить список, ничего не скачивая
                items = gdown.download_folder(id=fid, output=str(WORK / "listing" / fid), quiet=True,
                                              skip_download=True, remaining_ok=True)
                files = [(it.id, it.path) for it in items or []]
            except TypeError:
                items = gdown.download_folder(id=fid, output=str(WORK / "sources" / fid), quiet=False,
                                              remaining_ok=True)
                files = [(None, p) for p in items or []]
        except Exception as e:
            log(f"⚠️ Не удалось прочитать папку {fid}: {e}")
        if files is None:
            files = [tuple(x) for x in (cache or [])]
        else:
            state["folders"][fid] = [list(x) for x in files]
        vids = [(i, p) for i, p in files if str(p).lower().endswith(VIDEO_EXT)]
        log(f"Папка {fid}: найдено видео — {len(vids)}")
        for i, p in sorted(vids, key=lambda x: str(x[1])):
            out.append(f"https://drive.google.com/file/d/{i}/view" if i else str(p))
    return out


# ------------------------------------------------------------------ расписание
def next_slots(slots, count, tz, now, min_lead_min=40, taken=()):
    """Ближайшие `count` слотов публикации (в будущем, не занятые ранее)."""
    out = []
    taken = set(taken)
    day = now.date()
    while len(out) < count and day < now.date() + dt.timedelta(days=60):
        for hhmm in sorted(slots):
            h, m = map(int, hhmm.split(":"))
            t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            iso = t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t > now + dt.timedelta(minutes=min_lead_min) and iso not in taken:
                out.append((t, iso))
                taken.add(iso)
                if len(out) >= count:
                    break
        day += dt.timedelta(days=1)
    return out


# ------------------------------------------------------------------ нарезка
def cut_clips(cfg, state, need, run_dir):
    """Нарезает `need` новых роликов из исходников кампании, обновляя state."""
    camp = cfg["campaign"]
    clips = []
    hooks = camp.get("hooks", [])
    offset = state.get("hook_offset", 0)
    for src in expand_sources(camp["sources"], state):
        if len(clips) >= need:
            break
        key = source_key(src)
        sst = state.setdefault("sources", {}).setdefault(key, {"src": src, "used": [], "exhausted": False})
        if sst.get("exhausted"):
            continue
        try:
            video = fetch_source(src, WORK / "sources")
        except Exception as e:
            log(f"⚠️ Исходник пропущен: {e}")
            continue
        excl = run_dir / f"{key}.exclude.json"
        save_json(excl, sst["used"])
        out_dir = run_dir / key
        rot = hooks[offset % len(hooks):] + hooks[:offset % len(hooks)] if hooks else []
        cmd = [sys.executable, str(ROOT / "autoclip.py"), str(video), "--out", str(out_dir),
               "--clips", str(need - len(clips)), "--exclude", str(excl), *camp.get("autoclip_args", [])]
        if camp.get("banner"):
            cmd += ["--banner", str(ROOT / camp["banner"])]
        if rot:
            cmd += ["--top-text", "|".join(rot)]
        if cfg.get("mode") == "telegram":
            cmd += ["--maxrate", "5M"]  # чтобы файл влез в лимит Telegram (50 МБ)
        log(f"\n=== Нарезка из {key}: нужно ещё {need - len(clips)} ===")
        code = subprocess.run(cmd).returncode
        manifests = list(out_dir.glob("*.clips.json"))
        got = json.loads(manifests[0].read_text(encoding="utf-8")) if manifests else []
        if code not in (0, 3):
            log(f"⚠️ autoclip завершился с кодом {code}")
        if len(got) < need - len(clips):
            sst["exhausted"] = True  # новых моментов больше нет — переходим к следующему исходнику
            log(f"Исходник {key} исчерпан")
        for c in got:
            sst["used"].append([c["start"], c["end"]])
            c["path"] = str(out_dir / c["file"])
            c["source"] = key
            clips.append(c)
        offset += len(got)
    state["hook_offset"] = offset
    return clips


# ------------------------------------------------------------------ публикация
def youtube_client(token):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(token=None, refresh_token=token,
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token", scopes=YT_SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def youtube_upload(yt, path, title, description, tags, publish_at_iso, category="20"):
    from googleapiclient.http import MediaFileUpload
    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags,
                    "categoryId": category},
        "status": {"privacyStatus": "private", "publishAt": publish_at_iso,
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024,
                                                        resumable=True))
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            log(f"    загрузка {int(status.progress() * 100)}%")
    return resp["id"]


def tg_call(method, data=None, files=None):
    import requests
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        log("  (Telegram не настроен — пропускаю)")
        return None
    data = dict(data or {}, chat_id=chat)
    r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=data, files=files, timeout=300)
    if not r.ok:
        log(f"  ⚠️ Telegram {method}: {r.status_code} {r.text[:200]}")
    return r


def tg_send_video(path, caption):
    size_mb = Path(path).stat().st_size / 1e6
    if size_mb > 49:
        tg_call("sendMessage", {"text": f"⚠️ Файл {Path(path).name} {size_mb:.0f} МБ — больше лимита Telegram.\n\n{caption}"})
        return
    with open(path, "rb") as f:
        tg_call("sendVideo", {"caption": caption[:1024], "supports_streaming": "true"}, {"video": f})


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="нарезать и показать план, ничего не публикуя")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    cfg = load_json(args.config, None)
    if cfg is None:
        sys.exit("Нет config.json")
    state = load_json(STATE_PATH, {})
    tz = ZoneInfo(cfg.get("timezone", "Europe/Moscow"))
    now = dt.datetime.now(tz)
    mode = "local" if args.dry_run else cfg.get("mode", "telegram")
    accounts = [a for a in cfg["accounts"] if a.get("enabled", True)]
    if not accounts:
        sys.exit("Нет включённых аккаунтов в config.json")

    # сколько роликов нужно каждому аккаунту (с учётом уже запланированных на будущее)
    plan = []
    for acc in accounts:
        acc_state = state.setdefault("accounts", {}).setdefault(acc["id"], {"scheduled": []})
        future = [s for s in acc_state["scheduled"] if s > now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
        acc_state["scheduled"] = future  # прошедшие слоты больше не держим
        horizon = acc.get("buffer_days", 1) * len(acc["slots"])
        need = max(0, min(acc["clips_per_day"], horizon - len(future)))
        slots = next_slots(acc["slots"], need, tz, now, taken=future)
        plan.append((acc, slots))
    total_need = sum(len(s) for _, s in plan)
    log(f"Режим: {mode}. Нужно роликов: {total_need} " +
        ", ".join(f"{a['id']}={len(s)}" for a, s in plan))
    if total_need == 0:
        log("Очередь заполнена — сегодня ничего не нужно.")
        save_json(STATE_PATH, state)
        return

    run_dir = WORK / "out" / now.strftime("%Y%m%d_%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    clips = cut_clips(cfg, state, total_need, run_dir)
    save_json(STATE_PATH, state)  # сохраняем использованные отрезки сразу
    if not clips:
        tg_call("sendMessage", {"text": "⚠️ Конвейер: новых роликов нет — все исходники исчерпаны. "
                                        "Добавь новые ссылки в config.json → campaign.sources."})
        sys.exit("Новых роликов нет: добавь исходники.")

    camp = cfg["campaign"]
    report = [f"🎬 {camp['name']}: {len(clips)} новых роликов ({now:%d.%m %H:%M})"]
    it = iter(clips)
    yt_cache = {}
    tokens = json.loads(os.environ.get("YT_TOKENS", "{}") or "{}")
    for acc, slots in plan:
        acc_state = state["accounts"][acc["id"]]
        for t_local, iso in slots:
            clip = next(it, None)
            if clip is None:
                break
            hook = clip.get("title") or f"Момент из {camp['name']}"
            fmt = acc.get("title_format", "{hook}")
            if camp["name"].lower() in hook.lower():  # название игры уже есть в хуке — не дублируем
                fmt = "{hook}"
            title = fmt.format(hook=hook, game=camp["name"])
            desc = acc.get("description", camp.get("description", ""))
            when = t_local.strftime("%d.%m %H:%M")
            log(f"[{acc['id']}] {when} — {title}")
            entry = {"account": acc["id"], "file": Path(clip["path"]).name, "title": title,
                     "publish_at": iso, "source": clip["source"], "start": clip["start"], "end": clip["end"],
                     "created": now.isoformat(timespec="minutes")}
            if mode == "youtube":
                tok = tokens.get(acc["id"])
                if not tok:
                    log(f"  ⚠️ Нет токена для аккаунта {acc['id']} в секрете YT_TOKENS — пропускаю")
                    continue
                try:
                    yt = yt_cache.get(acc["id"]) or youtube_client(tok)
                    yt_cache[acc["id"]] = yt
                    vid = youtube_upload(yt, clip["path"], title, desc, camp.get("tags", []), iso)
                except Exception as e:
                    log(f"  ❌ Ошибка загрузки: {e}")
                    report.append(f"❌ {acc['id']} {when}: ошибка загрузки — {str(e)[:150]}")
                    continue
                entry["video_id"] = vid
                report.append(f"✅ {acc['id']} · {when} · https://youtube.com/shorts/{vid}")
            elif mode == "telegram":
                tg_send_video(clip["path"], f"📌 {acc['id']} · выложить {when}\n\n{title}\n\n{desc}")
                report.append(f"📤 {acc['id']} · {when} · {title}")
            else:
                report.append(f"💾 {acc['id']} · {when} · {clip['path']}")
            acc_state["scheduled"].append(iso)
            state.setdefault("log", []).append(entry)
            save_json(STATE_PATH, state)

    state["log"] = state.get("log", [])[-500:]
    save_json(STATE_PATH, state)
    if mode == "youtube":
        report.append("\nПосле времени публикации отправь эти ссылки в Клипни («Отправить ролик»).")
    msg = "\n".join(report)
    log("\n" + msg)
    if mode != "local":
        tg_call("sendMessage", {"text": msg[:4000], "disable_web_page_preview": "true"})


if __name__ == "__main__":
    main()
