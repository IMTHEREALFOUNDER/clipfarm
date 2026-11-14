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


def autoclip_cmd(cfg, camp, video, out_dir, n, hooks_rot, extra, subs=False):
    args = [x for x in camp.get("autoclip_args", []) if not (subs and x == "--no-subs")]
    cmd = [sys.executable, str(ROOT / "autoclip.py"), str(video), "--out", str(out_dir),
           "--clips", str(n), *args, *extra]
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


def render_pieces(pieces, out_path, speed_random=False, voice=None, voice_delay=0.0, total=None, bg_volume=1.0):
    """Склеивает куски [(key, path, s, e, has_audio)] в 1920x1080 60fps.
    voice — mp3 с озвучкой: накладывается поверх, звук игры приглушается до bg_volume."""
    import random
    inputs, filt = [], []
    for i, (_, path, s, e, has_audio) in enumerate(pieces):
        sp = random.choice([1.0, 1.0, 1.1]) if speed_random else 1.0
        inputs += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(path)]
        filt.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=60,setsar=1,setpts=PTS/{sp},format=yuv420p[v{i}]")
        if has_audio:
            filt.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                        f"atrim=0:{e - s:.3f},asetpts=PTS-STARTPTS,atempo={sp}[a{i}]")
        else:
            filt.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{(e - s) / sp:.3f}[a{i}]")
    n = len(pieces)
    filt.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    vout, aout = "[vc]", "[ac]"
    if total:
        filt.append(f"[vc]trim=0:{total:.3f},setpts=PTS-STARTPTS[vt]")
        vout = "[vt]"
    if voice:
        inputs += ["-i", str(voice)]
        d = int(voice_delay * 1000)
        filt.append(f"[ac]volume={bg_volume}[bgq]")
        filt.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={d}|{d},volume=1.6[vo]")
        filt.append("[bgq][vo]amix=inputs=2:duration=first:normalize=0[am]")
        aout = "[am]"
    if total:
        filt.append(f"{aout}atrim=0:{total:.3f},asetpts=PTS-STARTPTS[at]")
        aout = "[at]"
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filt),
           "-map", vout, "-map", aout, "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
           "-c:a", "aac", "-b:a", "192k", str(out_path)]
    subprocess.run(cmd, check=True)
    return out_path


def build_montage(shorts, state, mcfg, out_path, recycle=False, cooldown=30):
    """Монтаж: склейка коротких B-roll фрагментов. Свежий режим — каждый кусок один раз."""
    import random
    target = random.uniform(*mcfg.get("target", [45, 60]))
    pmin, pmax = mcfg.get("piece", [5, 9])
    if recycle:
        order = shorts[:]
        random.shuffle(order)
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
    sig = "|".join(f"{k}@{round(s)}" for k, _, s, _, _ in pieces)
    seen = state.setdefault("montage_sigs", [])
    if sig in seen:
        return None, []
    rcfg = mcfg.get("_recycle", {})
    ids = {f"{k}@{int(s // 3)}" for k, _, s, _, _ in pieces}
    cutoff = (dt.date.today() - dt.timedelta(days=rcfg.get("lookback_days", 30))).isoformat()
    hist = [h for h in state.get("montage_history", []) if h["d"] >= cutoff]
    for h in hist:
        if len(ids & set(h["p"])) / max(1, len(ids)) > rcfg.get("max_overlap", 0.34):
            return None, []
    seen.append(sig)
    state["montage_sigs"] = seen[-2000:]
    hist.append({"d": today_str(), "p": sorted(ids)})
    state["montage_history"] = hist
    render_pieces(pieces, out_path, speed_random=recycle)
    for key, _, s, e, _ in pieces:
        state["sources"][key]["used"].append([round(s, 2), round(e, 2), today_str()])
    return out_path, [k for k, *_ in pieces]


def pick_bg_pieces(pool, state, lengths, cooldown_days=3):
    """Фон для истории/списка: куски заданной длины из любых футажей, кроме взятых под фон
    за последние cooldown_days. Монтажный учёт не трогаем — здесь футаж лишь подложка."""
    import random
    bg = state.setdefault("bg_used", {})
    cutoff = (dt.date.today() - dt.timedelta(days=cooldown_days)).isoformat()
    for k in list(bg):
        bg[k] = [u for u in bg[k] if u[2] > cutoff]
    order = pool[:]
    random.shuffle(order)
    pieces, oi, tries = [], 0, 0
    for L in lengths:
        got = None
        while got is None and tries < len(order) * 2 + 10:
            key, path, dur, has_audio = order[oi % len(order)]
            oi += 1
            tries += 1
            if dur < 2.6:
                continue
            excl = [(u[0], u[1]) for u in bg.get(key, [])]
            try:
                win = best_piece(path, min(L, dur - 0.3), excl, rand=True)
            except Exception:
                win = None
            if win:
                got = (key, path, win[0], win[1], has_audio)
        if got is None:  # всё недавно было фоном — берём любой кусок, но не тот же подряд
            key, path, dur, has_audio = order[oi % len(order)]
            oi += 1
            s0 = random.uniform(0.1, max(0.1, dur - L - 0.1))
            got = (key, path, s0, min(dur - 0.05, s0 + L), has_audio)
        pieces.append(got)
        bg.setdefault(got[0], []).append([round(got[2], 2), round(got[3], 2), today_str()])
    return pieces


def pick_content(items, state, key):
    """Следующий текст из банка без повторов, пока не пройдён весь банк."""
    import random
    used = state.setdefault("content_used", {}).setdefault(key, [])
    free = [i for i in range(len(items)) if i not in used]
    if not free:
        used.clear()
        free = list(range(len(items)))
    i = random.choice(free)
    used.append(i)
    return items[i]


def _edge_tts(text, voice, rate, mp3):
    import asyncio
    import edge_tts

    async def run():
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
        except TypeError:  # старые версии edge-tts отдают слова по умолчанию
            comm = edge_tts.Communicate(text, voice, rate=rate)
        words, sents = [], []
        with open(mp3, "wb") as f:
            async for ch in comm.stream():
                if ch["type"] == "audio":
                    f.write(ch["data"])
                elif ch["type"] == "WordBoundary":
                    st = ch["offset"] / 1e7
                    words.append({"start": st, "end": st + ch["duration"] / 1e7, "word": ch["text"]})
                elif ch["type"] == "SentenceBoundary":
                    st = ch["offset"] / 1e7
                    sents.append((st, st + ch["duration"] / 1e7, ch["text"]))
        if not words and sents:
            for st, en, tx in sents:
                ws = tx.split()
                step = (en - st) / max(1, len(ws))
                for j, w in enumerate(ws):
                    words.append({"start": st + j * step, "end": st + (j + 1) * step, "word": w})
        return words
    return asyncio.run(run())


def estimate_words(text, dur, lead=0.12, tail=0.25):
    """Тайминги слов без данных от движка: по длине слова + паузы на знаках препинания."""
    toks = [t for t in text.split() if re.sub(r"\W", "", t)]
    def core(t):
        return len(re.sub(r"\W", "", t)) + 1.5
    def pause(t):
        return 5.0 if re.search(r"[.!?…]$", t) else 2.5 if re.search(r"[,;:—–]$", t) else 0.0
    total = sum(core(t) + pause(t) for t in toks) or 1
    k = max(0.01, dur - lead - tail) / total
    out, t = [], lead
    for tok in toks:
        out.append({"start": round(t, 3), "end": round(t + core(tok) * k, 3), "word": tok})
        t += (core(tok) + pause(tok)) * k
    return out


PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"


def _piper_tts(text, voice_name, wav):
    """Локальная озвучка Piper (работает на сервере GitHub без внешних TTS-сервисов)."""
    import requests
    name, q = voice_name.split(":") if ":" in voice_name else (voice_name, "medium")
    mdir = WORK / "piper"
    mdir.mkdir(parents=True, exist_ok=True)
    onnx = mdir / f"ru_RU-{name}-{q}.onnx"
    for path, url in ((onnx, f"{PIPER_BASE}/{name}/{q}/ru_RU-{name}-{q}.onnx"),
                      (Path(str(onnx) + ".json"), f"{PIPER_BASE}/{name}/{q}/ru_RU-{name}-{q}.onnx.json")):
        if not path.exists() or path.stat().st_size == 0:
            log(f"  скачиваю голос Piper {path.name}…")
            r = requests.get(url, timeout=300)
            r.raise_for_status()
            path.write_bytes(r.content)
    clean = re.sub(r"\s+", " ", text.replace("—", ",").replace("«", "").replace("»", "")).strip()
    last = None
    for cmd in (["piper", "-m", str(onnx), "-f", str(wav)],
                [sys.executable, "-m", "piper", "-m", str(onnx), "-f", str(wav)]):
        try:
            subprocess.run(cmd, input=clean.encode("utf-8"), check=True, capture_output=True, timeout=600)
            if wav.exists() and wav.stat().st_size > 1000:
                return
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            last = e
    raise RuntimeError(f"Piper не сработал: {last}")


def tts(text, content, out_dir):
    """Озвучка: edge-tts (3 попытки, запасной голос) → если не вышло, Piper.
    Возвращает (путь к аудио, слова с таймингами, длительность, движок)."""
    import time
    voice = content.get("voice", "ru-RU-DmitryNeural")
    rate = content.get("rate", "+0%")
    mp3 = out_dir / "voice.mp3"
    for attempt, v in enumerate([voice, voice, content.get("voice_backup", "ru-RU-SvetlanaNeural")]):
        try:
            words = _edge_tts(text, v, rate, mp3)
            dur, _ = probe_info(mp3)
            if dur > 2:
                if not words:
                    words = estimate_words(text, dur)
                return mp3, words, dur, f"edge:{v}"
        except Exception as e:
            log(f"  edge-tts попытка {attempt + 1}: {type(e).__name__}")
        time.sleep(4 + attempt * 4)
    log("  edge-tts недоступен — озвучиваю через Piper")
    wav = out_dir / "voice.wav"
    _piper_tts(text, content.get("piper_voice", "dmitri:medium"), wav)
    dur, _ = probe_info(wav)
    return wav, estimate_words(text, dur), dur, "piper"


def run_autoclip_single(cfg, camp, video, out_dir, title, extra, subs=False):
    dur, _ = probe_info(video)
    seg = out_dir / "seg.txt"
    seg.write_text(f"0:00-{dur:.2f}\n", encoding="utf-8")
    code = subprocess.run(autoclip_cmd(cfg, camp, video, out_dir, 1, [title] if title else [],
                                       ["--segments", str(seg), *extra], subs=subs)).returncode
    manifests = list(out_dir.glob("*.clips.json"))
    got = json.loads(manifests[0].read_text(encoding="utf-8")) if manifests else []
    if code != 0 or not got:
        log(f"⚠️ autoclip не собрал ролик (код {code})")
        return None
    c = got[0]
    c["path"] = str(out_dir / c["file"])
    c["title"] = title or c.get("title", "")
    return c


def fmt_story(cfg, camp, state, longs, shorts, d):
    """Озвученная история: голос + субтитры по словам + геймплей фоном."""
    import random
    content = load_json(ROOT / "content.json", {})
    if not content.get("stories"):
        return None
    st = pick_content(content["stories"], state, "stories")
    d.mkdir(parents=True, exist_ok=True)
    log(f"Озвучка: «{st['title']}»")
    voice, words, vdur, engine = tts(st["text"], content, d)
    log(f"  голос: {engine}, {vdur:.1f} с")
    delay = 0.4  # голос почти сразу — первые секунды решают, досмотрят ли ролик
    total = max(43.0, delay + vdur + 1.5)  # с запасом выше минимума кампании (40 с)
    lengths, acc = [], 0.0
    while acc < total + 0.5:
        L = random.uniform(4.0, 7.0)
        lengths.append(L)
        acc += L
    pieces = pick_bg_pieces(shorts + longs, state, lengths, camp.get("bg_cooldown_days", 3))
    video = d / "story.mp4"
    render_pieces(pieces, video, voice=voice, voice_delay=delay, total=total,
                  bg_volume=content.get("bg_volume", 0.4))
    for w in words:
        w["start"] += delay
        w["end"] += delay
    save_json(d / "story.transcript.json", {"language": "ru", "words": words})
    c = run_autoclip_single(cfg, camp, video, d, st["title"], [], subs=True)
    if c:
        c["source"] = "story"
    return c


def fmt_list(cfg, camp, state, longs, shorts, d):
    """Список: заголовок сверху, пункты по одному на кусок геймплея."""
    import random
    content = load_json(ROOT / "content.json", {})
    if not content.get("lists"):
        return None
    it = pick_content(content["lists"], state, "lists")
    d.mkdir(parents=True, exist_ok=True)
    items = it["items"]
    per = max(8.8, 47.0 / len(items))
    lengths = [per + random.uniform(-0.6, 0.8) for _ in items]
    pieces = pick_bg_pieces(shorts + longs, state, lengths, camp.get("bg_cooldown_days", 3))
    video = d / "list.mp4"
    render_pieces(pieces, video)
    caps, t = [], 0.0
    for i, (p, txt) in enumerate(zip(pieces, items)):
        L = p[3] - p[2]
        caps.append({"start": t + 0.15, "end": t + L - 0.05, "num": f"{i + 1}.", "text": txt})
        t += L
    save_json(d / "captions.json", caps)
    log(f"Список: «{it['title']}»")
    c = run_autoclip_single(cfg, camp, video, d, it["title"], ["--captions", str(d / "captions.json")])
    if c:
        c["source"] = "list"
    return c


def fmt_montage(cfg, camp, state, longs, shorts, d):
    """Монтаж/нарезка из свежего материала; если он кончился — пересборка старого (recycle)."""
    hooks = camp.get("hooks", [])
    offset = state.get("hook_offset", 0)
    mcfg = dict(camp.get("montage", {}), _recycle=camp.get("recycle", {}))
    rcfg = camp.get("recycle", {})
    cooldown = rcfg.get("cooldown_days", 7)
    d.mkdir(parents=True, exist_ok=True)
    # 1) длинный свежий футаж — нарезка
    for key, video, dur, _ in longs:
        sst = state["sources"][key]
        if sst.get("exhausted"):
            continue
        excl = d / f"{key}.exclude.json"
        save_json(excl, [list(x) for x in blocked_ranges(sst["used"], False, cooldown)])
        out_dir = d / key
        subprocess.run(autoclip_cmd(cfg, camp, video, out_dir, 1, rotated(hooks, offset), ["--exclude", str(excl)]))
        manifests = list(out_dir.glob("*.clips.json"))
        got = json.loads(manifests[0].read_text(encoding="utf-8")) if manifests else []
        if not got:
            sst["exhausted"] = True
            continue
        c = got[0]
        sst["used"].append([c["start"], c["end"], today_str()])
        c["path"], c["source"] = str(out_dir / c["file"]), key
        state["hook_offset"] = offset + 1
        return c
    # 2) свежий монтаж, 3) пересборка старого
    has_old = any(len(blocked_ranges(v.get("used", []), True, cooldown)) < len(v.get("used", []))
                  for v in state.get("sources", {}).values())
    modes = [False] + ([True] if rcfg.get("enabled") and has_old else [])
    for recycle in modes:
        for attempt in range(6 if recycle else 1):
            if not shorts:
                break
            try:
                mpath, used_keys = build_montage(shorts, state, mcfg, d / "montage.mp4", recycle=recycle,
                                                 cooldown=cooldown)
            except subprocess.CalledProcessError as e:
                log(f"⚠️ Ошибка склейки: {e}")
                continue
            if not mpath:
                continue
            import random
            extra = ["--fit-zoom", f"{random.uniform(1.5, 1.8):.2f}"] if recycle else []
            hook = rotated(hooks, offset)[0] if hooks else ""
            c = run_autoclip_single(cfg, camp, mpath, d, hook, extra)
            if c:
                c["source"] = "+".join(used_keys)
                state["hook_offset"] = offset + 1
                if recycle and not state.get("recycle_notified"):
                    tg_call("sendMessage", {"text": "♻️ Свежие куски для монтажа закончились — монтажи теперь "
                                                    "пересобираются из старых фрагментов. Истории и списки идут как обычно."})
                    state["recycle_notified"] = True
                return c
    log("Монтаж сейчас недоступен (свежий материал кончился).")
    return None


FORMATS = {"story": fmt_story, "list": fmt_list, "montage": fmt_montage}


def gather_sources(camp, state):
    """Скачивает все футажи (из кэша — мгновенно) и делит на длинные и короткие."""
    mcfg = camp.get("montage", {})
    min_long = mcfg.get("long_threshold", 75)
    longs, shorts, fails = [], [], 0
    for src in expand_sources(campaign_sources(camp), state):
        key = source_key(src)
        sst = state.setdefault("sources", {}).setdefault(key, {"used": [], "exhausted": False})
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
    return longs, shorts


def cut_clips(cfg, state, need, run_dir):
    """Делает `need` роликов, чередуя форматы из campaign.formats (история / список / монтаж)."""
    camp = cfg["campaign"]
    longs, shorts = gather_sources(camp, state)
    log(f"Футажи: длинных {len(longs)}, коротких {len(shorts)}")
    if not longs and not shorts:
        return []
    formats = [f for f in camp.get("formats", ["story", "list", "montage"]) if f in FORMATS]
    clips, attempts = [], 0
    while len(clips) < need and attempts < need * len(formats) + 2:
        fmt = formats[state.get("format_offset", 0) % len(formats)]
        state["format_offset"] = state.get("format_offset", 0) + 1
        attempts += 1
        d = run_dir / f"{len(clips) + 1:02d}_{fmt}_{attempts}"
        log(f"\n=== Ролик {len(clips) + 1}/{need}: формат «{fmt}» ===")
        try:
            c = FORMATS[fmt](cfg, camp, state, longs, shorts, d)
        except Exception as e:
            log(f"⚠️ Формат «{fmt}» не получился: {type(e).__name__}: {str(e)[:200]}")
            c = None
        if c:
            c["format"] = fmt
            clips.append(c)
        save_json(STATE_PATH, state)
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
        # за запуск — не больше max_per_run, чтобы запас на buffer_days набирался за 1–2 дня
        need = max(0, min(acc.get("max_per_run", acc["clips_per_day"]), horizon - len(future)))
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
    if mode == "telegram":
        days = {}
        for a in accounts:
            for iso in sorted(state["accounts"][a["id"]]["scheduled"]):
                t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).astimezone(tz)
                days.setdefault(t.strftime("%d.%m"), []).append(f"{a['id']} {t:%H:%M}")
        report.append("\n🗓 Запас роликов (всё, что уже прислано, но ещё не вышло):")
        report += [f"  {d}: " + ", ".join(v) for d, v in days.items()]
        report.append("Загрузи новые ролики в YouTube Studio с отложенной публикацией на эти даты.")
    msg = "\n".join(report)
    log("\n" + msg)
    if mode != "local":
        tg_call("sendMessage", {"text": msg[:4000], "disable_web_page_preview": "true"})


if __name__ == "__main__":
    main()
