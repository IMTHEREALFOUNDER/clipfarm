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
import hashlib
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
    """Короткий хэш вместо ссылки — чтобы state.json в публичном репозитории ничего не раскрывал."""
    fid = drive_file_id(src) if src.startswith("http") else None
    return "src_" + hashlib.sha1((fid or src).encode("utf-8")).hexdigest()[:12]


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
        log(f"Скачиваю футаж {key}…")
        res = gdown.download(id=fid, output=str(out), quiet=True)
        if not res or not out.exists():
            raise RuntimeError(f"Не удалось скачать футаж {key}. Проверь доступ «Все, у кого есть ссылка».")
        return out
    log(f"Скачиваю по ссылке: {src}")
    subprocess.run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
                    "-o", str(out), src], check=True)
    return out


VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


def campaign_sources(camp):
    """Источники берутся из секрета CLIP_SOURCES (если задан), иначе из config.json.
    Так ссылки на футажи не видны в публичном репозитории."""
    env = os.environ.get("CLIP_SOURCES", "").strip()
    if env:
        return [s.strip() for s in re.split(r"[\n,]+", env) if s.strip()]
    return [s for s in camp.get("sources", []) if "ВСТАВЬ" not in s]


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
        cache_path = WORK / "sources" / "_folders.json"
        fcache = load_json(cache_path, {})
        cache = fcache.get(fid)
        files = None
        try:
            import inspect
            import gdown
            # разные версии gdown принимают разные параметры — передаём только поддерживаемые
            params = inspect.signature(gdown.download_folder).parameters
            kw = {"id": fid, "quiet": True}
            if "remaining_ok" in params:
                kw["remaining_ok"] = True
            if "skip_download" in params:  # можно получить список, ничего не скачивая
                items = gdown.download_folder(output=str(WORK / "listing" / fid), skip_download=True, **kw)
                files = [(getattr(it, "id", None), str(getattr(it, "path", it))) for it in items or []]
            else:
                items = gdown.download_folder(output=str(WORK / "sources" / fid), **kw)
                files = [(None, str(p)) for p in items or []]
        except Exception as e:
            log(f"⚠️ Не удалось прочитать папку с футажами: {e}")
        if files is None:
            files = [tuple(x) for x in (cache or [])]
        else:
            fcache[fid] = [list(x) for x in files]
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            save_json(cache_path, fcache)
        vids = [(i, p) for i, p in files if str(p).lower().endswith(VIDEO_EXT)]
        log(f"Папка с футажами: найдено видео — {len(vids)}")
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
def probe_info(path):
    """(длительность, есть ли звук)"""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    d = json.loads(out or "{}")
    dur = float(d.get("format", {}).get("duration", 0) or 0)
    has_audio = any(st.get("codec_type") == "audio" for st in d.get("streams", []))
    return dur, has_audio


def autoclip_cmd(cfg, camp, video, out_dir, n, hooks_rot, extra):
    cmd = [sys.executable, str(ROOT / "autoclip.py"), str(video), "--out", str(out_dir),
           "--clips", str(n), *camp.get("autoclip_args", []), *extra]
    if camp.get("banner"):
        cmd += ["--banner", str(ROOT / camp["banner"])]
    if hooks_rot:
        cmd += ["--top-text", "|".join(hooks_rot)]
    if cfg.get("mode") == "telegram":
        cmd += ["--maxrate", "5M"]  # чтобы файл влез в лимит Telegram (50 МБ)
    return cmd


def rotated(hooks, offset):
    return hooks[offset % len(hooks):] + hooks[:offset % len(hooks)] if hooks else []


def today_str():
    return dt.date.today().isoformat()


def blocked_ranges(used, recycle, cooldown_days):
    """Какие отрезки нельзя брать. Обычный режим — все использованные.
    Режим повтора — только использованные недавно (моложе cooldown_days)."""
    out = []
    cutoff = (dt.date.today() - dt.timedelta(days=cooldown_days)).isoformat()
    for u in used:
        s, e = u[0], u[1]
        when = u[2] if len(u) > 2 else today_str()
        if not recycle or when > cutoff:
            out.append((s, e))
    return out


_ANALYSIS = {}


def best_piece(path, length, excl, cache=None, rand=False):
    """Лучший по экшену кусок длиной `length` внутри короткого файла, не пересекающийся с excl."""
    import random
    import autoclip as AC
    cache = _ANALYSIS if cache is None else cache
    key = str(path)
    if key not in cache:
        wav = WORK / "tmp" / (Path(path).stem + ".wav")
        wav.parent.mkdir(parents=True, exist_ok=True)
        AC.extract_audio(path, wav)
        rms, hop = AC.rms_curve(wav)
        motion, mhop = AC.motion_curve(path)
        cache[key] = (rms, hop, motion, mhop)
    rms, hop, motion, mhop = cache[key]
    dur, _ = probe_info(path)
    if dur < length + 0.3:
        length = dur - 0.3
    if length < 2.5:
        return None
    n = 3 if rand else 1  # в режиме повтора берём один из трёх лучших — больше разнообразия
    got = AC.find_action_moments(rms, hop, motion, mhop, n, length, length, dur, excl, margin=0.3) \
        if dur > length + 0.5 else []
    if got:
        pick = random.choice(got) if rand else got[0]
        return pick[0], pick[1]
    s, e = 0.1, 0.1 + length
    return (s, e) if not AC.overlaps(s, e, excl, margin=0.5) else None


def build_montage(shorts, state, mcfg, out_path, recycle=False, cooldown=30):
    """Склейка нескольких коротких B-roll фрагментов в один исходник для ролика."""
    import random
    target = random.uniform(*mcfg.get("target", [45, 60]))
    pmin, pmax = mcfg.get("piece", [5, 9])
    if recycle:
        order = shorts[:]
        random.shuffle(order)  # новые сочетания и новый порядок
    else:
        start = state.get("montage_offset", 0)
        order = shorts[start % len(shorts):] + shorts[:start % len(shorts)]
    pieces, total = [], 0.0
    for key, path, dur, has_audio in order:
        if total >= target - 1:
            break
        sst = state["sources"][key]
        if sst.get("exhausted") and not recycle:
            continue
        length = min(random.uniform(pmin, pmax), max(3.0, target - total))
        excl = blocked_ranges(sst["used"], recycle, cooldown)
        try:
            win = best_piece(path, length, excl, rand=recycle)
        except Exception as e:
            log(f"  пропускаю футаж {key}: {e}")
            continue
        if not win:
            if not recycle:
                sst["exhausted"] = True
            continue
        pieces.append((key, path, win[0], win[1], has_audio))
        total += win[1] - win[0]
    if not recycle:
        state["montage_offset"] = state.get("montage_offset", 0) + max(1, len(pieces))
    if total < mcfg.get("min_total", 40):
        return None, []
    # одна и та же склейка не повторяется никогда
    sig = "|".join(f"{k}@{round(s)}" for k, _, s, _, _ in pieces)
    seen = state.setdefault("montage_sigs", [])
    if sig in seen:
        return None, []
    seen.append(sig)
    state["montage_sigs"] = seen[-2000:]
    inputs, filt = [], []
    for i, (_, path, s, e, has_audio) in enumerate(pieces):
        inputs += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(path)]
        filt.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=60,setsar=1,format=yuv420p[v{i}]")
        if has_audio:
            filt.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                        f"atrim=0:{e - s:.3f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            filt.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{e - s:.3f}[a{i}]")
    filt.append("".join(f"[v{i}][a{i}]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=1[v][a]")
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filt),
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
           "-c:a", "aac", "-b:a", "192k", str(out_path)]
    subprocess.run(cmd, check=True)
    for key, _, s, e, _ in pieces:
        state["sources"][key]["used"].append([round(s, 2), round(e, 2), today_str()])
    return out_path, [k for k, *_ in pieces]


def gather_sources(camp, state, need, include_exhausted):
    """Скачивает футажи и делит их на длинные (резать) и короткие (монтаж)."""
    mcfg = camp.get("montage", {})
    min_long = mcfg.get("long_threshold", 75)
    longs, shorts, fails = [], [], 0
    for src in expand_sources(campaign_sources(camp), state):
        key = source_key(src)
        sst = state.setdefault("sources", {}).setdefault(key, {"used": [], "exhausted": False})
        if sst.get("exhausted") and not include_exhausted:
            continue
        try:
            video = fetch_source(src, WORK / "sources")
            fails = 0
        except Exception:
            fails += 1
            log(f"⚠️ Не удалось скачать футаж {key} (нет доступа или ссылка не работает)")
            if fails >= 3:
                tg_call("sendMessage", {"text": "⚠️ Конвейер: футажи не скачиваются — нет доступа. "
                                                "Проверь, что папка в CLIP_SOURCES открыта «Все, у кого есть ссылка», "
                                                "и что в ней лежат сами видео, а не ярлыки."})
                log("Три отказа подряд — останавливаю скачивание.")
                break
            continue
        dur, has_audio = probe_info(video)
        sst["duration"] = round(dur, 1)
        (longs if dur >= min_long else shorts).append((key, video, dur, has_audio))
        if len(longs) >= need and len(shorts) >= max(12, need * 6):
            break
    return longs, shorts


def cut_clips(cfg, state, need, run_dir):
    """Нарезает `need` роликов. Сначала из нового материала; если он кончился и включён повтор —
    собирает новые сочетания из фрагментов старше cooldown_days."""
    camp = cfg["campaign"]
    rcfg = camp.get("recycle", {})
    clips = _cut_pass(cfg, state, need, run_dir, recycle=False)
    cooldown = rcfg.get("cooldown_days", 30)
    has_old = any(len(blocked_ranges(v.get("used", []), True, cooldown)) < len(v.get("used", []))
                  for v in state.get("sources", {}).values())
    if len(clips) < need and rcfg.get("enabled", False) and not has_old:
        log(f"Новый материал закончился, а старым фрагментам ещё нет {cooldown} дней — повтор пока недоступен.")
    if len(clips) < need and rcfg.get("enabled", False) and has_old:
        log(f"\nНовый материал закончился — режим повтора (фрагменты старше {cooldown} дней)")
        more = _cut_pass(cfg, state, need - len(clips), run_dir / "recycle", recycle=True, cooldown=cooldown)
        if more and not state.get("recycle_notified"):
            tg_call("sendMessage", {"text": "♻️ Новый материал закончился — конвейер собирает ролики из старых "
                                            "фрагментов в новых сочетаниях. Чтобы ролики были свежее, "
                                            "докинь новые футажи в свою папку."})
            state["recycle_notified"] = True
        clips += more
    elif len(clips) >= need:
        state["recycle_notified"] = False
    return clips


def _cut_pass(cfg, state, need, run_dir, recycle=False, cooldown=30):
    camp = cfg["campaign"]
    mcfg = camp.get("montage", {})
    clips = []
    hooks = camp.get("hooks", [])
    offset = state.get("hook_offset", 0)
    if need <= 0:
        return clips
    longs, shorts = gather_sources(camp, state, need, include_exhausted=recycle)
    log(f"Футажи: длинных {len(longs)}, коротких (для монтажа) {len(shorts)}")

    # 1) длинные — обычная нарезка
    for key, video, dur, _ in longs:
        if len(clips) >= need:
            break
        sst = state["sources"][key]
        excl = run_dir / f"{key}.exclude.json"
        excl.parent.mkdir(parents=True, exist_ok=True)
        save_json(excl, [list(x) for x in blocked_ranges(sst["used"], recycle, cooldown)])
        out_dir = run_dir / key
        log(f"\n=== Нарезка из {key}: нужно ещё {need - len(clips)} ===")
        code = subprocess.run(autoclip_cmd(cfg, camp, video, out_dir, need - len(clips), rotated(hooks, offset),
                                           ["--exclude", str(excl)])).returncode
        manifests = list(out_dir.glob("*.clips.json"))
        got = json.loads(manifests[0].read_text(encoding="utf-8")) if manifests else []
        if code not in (0, 3):
            log(f"⚠️ autoclip завершился с кодом {code}")
        if len(got) < need - len(clips) and not recycle:
            sst["exhausted"] = True
            log(f"Исходник {key} исчерпан")
        for c in got:
            sst["used"].append([c["start"], c["end"], today_str()])
            c["path"] = str(out_dir / c["file"])
            c["source"] = key
            clips.append(c)
        offset += len(got)

    # 2) короткие — монтаж из нескольких B-roll фрагментов
    n, misses = 0, 0
    while len(clips) < need and shorts and misses < 3:
        n += 1
        mdir = run_dir / f"montage_{n:02d}"
        mdir.mkdir(parents=True, exist_ok=True)
        log(f"\n=== Монтаж №{n}{' (повтор)' if recycle else ''} ===")
        try:
            mpath, used_keys = build_montage(shorts, state, mcfg, mdir / f"montage_{n:02d}.mp4",
                                             recycle=recycle, cooldown=cooldown)
        except subprocess.CalledProcessError as e:
            log(f"⚠️ Ошибка склейки: {e}")
            misses += 1
            continue
        if not mpath:
            misses += 1
            if not recycle:
                log("Короткие футажи исчерпаны — новых кусков для монтажа нет.")
                break
            continue
        mdur, _ = probe_info(mpath)
        seg = mdir / "seg.txt"
        seg.write_text(f"0:00-{mdur:.2f}\n", encoding="utf-8")
        code = subprocess.run(autoclip_cmd(cfg, camp, mpath, mdir, 1, rotated(hooks, offset),
                                           ["--segments", str(seg)])).returncode
        manifests = list(mdir.glob("*.clips.json"))
        got = json.loads(manifests[0].read_text(encoding="utf-8")) if manifests else []
        if code != 0 or not got:
            log(f"⚠️ Не удалось собрать ролик из монтажа (код {code})")
            misses += 1
            continue
        c = got[0]
        c["path"] = str(mdir / c["file"])
        c["source"] = "+".join(used_keys)
        clips.append(c)
        offset += 1
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
                                        "Проверь секрет CLIP_SOURCES или добавь новые футажи в папку."})
        sys.exit("Новых роликов нет: проверь секрет CLIP_SOURCES и доступ к папке.")

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
