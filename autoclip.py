#!/usr/bin/env python3
"""
autoclip.py — локальная бесплатная автонарезка длинных видео в вертикальные шортсы.

Что делает:
  1. Транскрибирует видео (faster-whisper, по словам, работает на CPU).
  2. Ищет лучшие куски 30–50 сек (громкость, темп речи, «крючки», вопросы и т.д.)
     — или берёт ваши таймкоды из файла (--segments).
  3. Ведёт кадр за лицом (OpenCV) и переводит видео в 9:16 (1080x1920).
  4. Делает динамический зум (наезд на каждой новой фразе).
  5. Накладывает динамические субтитры по 1–3 слова с подсветкой текущего слова.

Пример:
  python autoclip.py podcast.mp4 --lang ru --clips 8
  python autoclip.py podcast.mp4 --segments moments.txt
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

OUT_W, OUT_H = 1080, 1920
OUT_FPS = 30

HOOK_WORDS = {
    "почему", "как", "никогда", "секрет", "правда", "ошибка", "ошибки", "деньги",
    "миллион", "миллиона", "самый", "самая", "честно", "представь", "знаешь", "смотри",
    "внимание", "главное", "проблема", "страшно", "шок", "нельзя", "запомни",
    "why", "how", "never", "secret", "truth", "mistake", "money", "million", "crazy",
    "insane", "nobody", "listen", "imagine", "worst", "best", "stop", "biggest", "wrong",
}
BAD_START = {"и", "а", "но", "так", "потому", "поэтому", "ну", "типа", "вот",
             "and", "but", "so", "because", "or", "um", "uh", "like"}


# ---------------------------------------------------------------- утилиты
def log(msg):
    print(msg, flush=True)


def need_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Не найден {tool}. Установите FFmpeg и добавьте его в PATH (см. инструкцию).")


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    s = d["streams"][0]
    w, h = int(s["width"]), int(s["height"])
    rot = 0
    for sd in s.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if "rotate" in (s.get("tags") or {}):
        rot = int(s["tags"]["rotate"])
    if abs(rot) % 180 == 90:  # ffmpeg сам поворачивает кадр при декодировании
        w, h = h, w
    return w, h, float(d["format"]["duration"])


def fmt_ts(t):
    t = max(0, int(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def parse_ts(s):
    parts = [float(p) for p in s.strip().replace(",", ".").split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


# ---------------------------------------------------------------- шаг 0: источник
def get_source(inp, workdir):
    if re.match(r"^https?://", inp):
        if not shutil.which("yt-dlp"):
            sys.exit("Для ссылок нужен yt-dlp: pip install yt-dlp")
        log("Скачиваю видео по ссылке…")
        tmpl = str(workdir / "%(title).80s.%(ext)s")
        run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]",
             "--merge-output-format", "mp4", "-o", tmpl, "--print", "after_move:filepath", inp],
            stdout=open(workdir / "_dl.txt", "w", encoding="utf-8"))
        path = (workdir / "_dl.txt").read_text(encoding="utf-8").strip().splitlines()[-1]
        return Path(path)
    p = Path(inp)
    if not p.exists():
        sys.exit(f"Файл не найден: {p}")
    return p


# ---------------------------------------------------------------- шаг 1: аудио + транскрипция
def extract_audio(video, wav):
    try:
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(video), "-vn", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    except subprocess.CalledProcessError:  # в видео нет звука — делаем тишину той же длины
        dur = probe(video)[2]
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
             "-t", f"{dur:.3f}", "-c:a", "pcm_s16le", str(wav)])


def rms_curve(wav, hop=0.25):
    with wave.open(str(wav), "rb") as wf:
        sr = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    n = int(sr * hop)
    if len(data) < n:
        return np.array([0.0]), hop
    data = data[: len(data) // n * n].astype(np.float32) / 32768.0
    return np.sqrt((data.reshape(-1, n) ** 2).mean(axis=1)), hop


def transcribe(wav, cache, model_name, lang, device):
    if cache.exists():
        log(f"Беру готовую транскрипцию: {cache.name}")
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("Не установлен faster-whisper: pip install faster-whisper")
    def _run(dev):
        compute = "int8" if dev == "cpu" else "float16"
        log(f"Загружаю модель Whisper «{model_name}» ({dev}, {compute}). Первый раз она скачается…")
        model = WhisperModel(model_name, device=dev, compute_type=compute,
                             cpu_threads=os.cpu_count() or 4)
        segments, info = model.transcribe(
            str(wav), language=lang, word_timestamps=True, vad_filter=True,
            beam_size=5, condition_on_previous_text=False)
        log(f"Язык: {info.language}, длительность {fmt_ts(info.duration)}. Распознаю…")
        words = []
        last_pct = -1
        for seg in segments:
            for w in seg.words or []:
                txt = w.word.strip()
                if txt:
                    words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": txt})
            pct = int(seg.end / max(info.duration, 1) * 100)
            if pct // 5 != last_pct // 5:
                log(f"  транскрипция: {pct}%")
                last_pct = pct
        return {"language": info.language, "words": words}

    try:
        data = _run(device)
    except Exception as e:
        if device == "cpu":
            raise
        log(f"  ! видеокарта не сработала ({e}). Перехожу на процессор…")
        data = _run("cpu")
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# ---------------------------------------------------------------- шаг 2: поиск моментов
def build_sentences(words, max_gap=0.8, soft_len=12.0):
    sents, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 99
        end_punct = w["word"].endswith((".", "!", "?", "…"))
        too_long = (w["end"] - cur[0]["start"] > soft_len) and (w["word"].endswith((",", ";", ":")) or gap > 0.3)
        if end_punct or gap > max_gap or too_long:
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    return [{"start": s[0]["start"], "end": s[-1]["end"], "words": s,
             "text": " ".join(x["word"] for x in s)} for s in sents]


def write_readable_transcript(sents, path):
    lines = [f"[{fmt_ts(s['start'])}] {s['text']}" for s in sents]
    path.write_text("\n".join(lines), encoding="utf-8")


def overlaps(s, e, excluded, margin=2.0):
    return any(s < xe + margin and e > xs - margin for xs, xe in excluded)


def zscore(a):
    a = np.asarray(a, dtype=float)
    sd = a.std()
    return (a - a.mean()) / sd if sd > 1e-9 else np.zeros_like(a)


def find_moments(sents, rms, hop, n_clips, min_len, max_len, keywords, total_dur, excluded=()):
    med = float(np.median(rms[rms > 0])) if np.any(rms > 0) else 1.0
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            dur = sents[j]["end"] - sents[i]["start"]
            if dur > max_len:
                break
            if dur >= min_len and not overlaps(sents[i]["start"], sents[j]["end"], excluded):
                cands.append((i, j))
    if not cands:
        return []

    feats = []
    for i, j in cands:
        s, e = sents[i]["start"], sents[j]["end"]
        ws = [w for k in range(i, j + 1) for w in sents[k]["words"]]
        text = " ".join(w["word"] for w in ws).lower()
        seg = rms[int(s / hop): max(int(e / hop), int(s / hop) + 1)]
        energy = float(np.percentile(seg, 80)) / med if len(seg) else 0
        rate = len(ws) / (e - s)
        gaps = sum(max(0.0, ws[k + 1]["start"] - ws[k]["end"]) for k in range(len(ws) - 1))
        silence = gaps / (e - s)
        punct = (text.count("!") + text.count("?")) / (j - i + 1)
        first = sents[i]["text"].lower()
        first_words = re.findall(r"\w+", first)[:10]
        hook = (1.0 if first.strip().endswith("?") else 0) + (1.0 if HOOK_WORDS & set(first_words) else 0)
        bad = 1.0 if first_words and first_words[0] in BAD_START else 0
        kw = min(3.0, sum(text.count(k) for k in keywords) * 0.75) if keywords else 0
        feats.append((energy, rate, silence, punct, hook, bad, kw))

    f = np.array(feats)
    score = (1.2 * zscore(f[:, 0]) + 0.8 * zscore(f[:, 1]) - 1.0 * zscore(f[:, 2])
             + 0.6 * zscore(f[:, 3]) + 1.0 * f[:, 4] - 0.7 * f[:, 5] + f[:, 6])

    picked = []
    for idx in np.argsort(-score):
        i, j = cands[idx]
        s = max(0.0, sents[i]["start"] - 0.15)
        e = min(total_dur, sents[j]["end"] + 0.35)
        if all(e + 2 < ps or s > pe + 2 for ps, pe, *_ in picked):
            picked.append((s, e, float(score[idx]), sents[i]["text"]))
        if len(picked) >= n_clips:
            break
    return sorted(picked)


def read_segments_file(path, total_dur):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.match(r"([\d:.,]+)\s*[-–—]\s*([\d:.,]+)", line)
        if not m:
            log(f"  пропускаю строку (нужен формат 12:30-13:15): {line}")
            continue
        s, e = parse_ts(m.group(1)), min(parse_ts(m.group(2)), total_dur)
        if e > s:
            out.append((s, e, 0.0, ""))
    return out


def motion_curve(video, hop=0.5):
    """Насколько сильно меняется картинка: среднее отличие кадров каждые hop секунд."""
    w, h = 64, 36
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-an", "-vf",
           f"fps={1 / hop},scale={w}:{h},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    vals, prev = [], None
    while True:
        buf = p.stdout.read(w * h)
        if len(buf) < w * h:
            break
        img = np.frombuffer(buf, np.uint8).astype(np.float32)
        vals.append(float(np.abs(img - prev).mean()) if prev is not None else 0.0)
        prev = img
    p.wait()
    return np.array(vals or [0.0]), hop


def find_action_moments(rms, hop, motion, mhop, n_clips, min_len, max_len, total_dur, excluded=(), margin=2.0):
    """Для геймплея без речи: ищем куски с громкими пиками (выстрелы, взрывы) и активной картинкой."""
    L = min(max_len, max(min_len, (min_len + max_len) / 2))
    if total_dur < min_len:
        return []
    L = min(L, total_dur)
    med = float(np.median(rms[rms > 0])) if np.any(rms > 0) else 1.0
    step = 1.0 if total_dur > 30 else 0.5
    starts = np.arange(min(1.0, max(0.1, total_dur - L - 0.1)), max(0.11, total_dur - L - 0.1), step)
    starts = np.array([x for x in starts if not overlaps(x, x + L, excluded, margin)])
    feats = []
    for s in starts:
        e = s + L
        seg = rms[int(s / hop): int(e / hop)]
        mo = motion[int(s / mhop): int(e / mhop)]
        e80 = float(np.percentile(seg, 80)) / med if len(seg) else 0
        peaks = float((seg > 2.0 * med).mean()) if len(seg) else 0
        mot = float(mo.mean()) if len(mo) else 0
        head = rms[int(s / hop): int((s + 4) / hop)]  # громкое начало = крючок
        hook = float(head.max()) / med if len(head) else 0
        feats.append((e80, peaks, mot, hook))
    if not feats:
        return []
    f = np.array(feats)
    score = zscore(f[:, 0]) + 0.6 * zscore(f[:, 1]) + 0.8 * zscore(f[:, 2]) + 0.5 * zscore(f[:, 3])
    picked = []
    for idx in np.argsort(-score):
        s = float(starts[idx])
        e = s + L
        if all(e + 3 < ps or s > pe + 3 for ps, pe, *_ in picked):
            picked.append((s, e, float(score[idx]), ""))
        if len(picked) >= n_clips:
            break
    return sorted(picked)


def loud_peaks(rms, hop, s, e, min_gap=1.5):
    """Моменты резкого всплеска громкости внутри ролика — для наездов камеры."""
    med = float(np.median(rms[rms > 0])) if np.any(rms > 0) else 1.0
    out, last = [], -99.0
    i0, i1 = int(s / hop), int(e / hop)
    for i in range(max(i0, 1), min(i1, len(rms))):
        if rms[i] > 2.5 * med and rms[i] > 1.6 * rms[i - 1]:
            t = i * hop - s
            if t - last >= min_gap and t > 0.5:
                out.append(t)
                last = t
    return out


# ---------------------------------------------------------------- шаг 3: трекинг лица
def face_track(video, start, dur, src_w, src_h, sample_fps=5):
    """Возвращает (times, xs, ys) — центр лица в долях кадра (0..1), NaN если лица нет."""
    import cv2
    cascade = None
    data_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
    xml = Path(data_dir) / "haarcascade_frontalface_default.xml"
    if xml.exists():
        cascade = cv2.CascadeClassifier(str(xml))
    if cascade is None or cascade.empty():
        log("  ! детектор лиц не найден (поставьте opencv-python<5) — кадрирую по центру")
        return np.array([0.0]), np.array([np.nan]), np.array([np.nan])
    pw = 480
    ph = int(round(src_h * pw / src_w / 2)) * 2
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
           "-an", "-vf", f"fps={sample_fps},scale={pw}:{ph}", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = pw * ph
    times, xs, ys = [], [], []
    prev = None
    k = 0
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        img = np.frombuffer(buf, np.uint8).reshape(ph, pw)
        faces = cascade.detectMultiScale(img, 1.1, 5, minSize=(max(24, ph // 12),) * 2)
        cx = cy = np.nan
        if len(faces):
            faces = sorted(faces, key=lambda f: -f[2] * f[3])
            best = faces[0]
            if prev is not None:  # «липкость»: держимся за того же человека, если он не сильно меньше
                biggest = best[2] * best[3]
                near = min(faces, key=lambda f: abs((f[0] + f[2] / 2) / pw - prev))
                if near[2] * near[3] >= 0.6 * biggest:
                    best = near
            x, y, w, h = best
            cx, cy = (x + w / 2) / pw, (y + h / 2) / ph
            prev = cx
        times.append(k / sample_fps)
        xs.append(cx)
        ys.append(cy)
        k += 1
    p.wait()
    return np.array(times), np.array(xs, dtype=float), np.array(ys, dtype=float)


def smooth_camera(times, vals, n_frames, fps, default=0.5, deadzone=0.05, follow=0.35, avg_sec=0.6):
    """«Ленивая» камера: игнорирует мелкие движения, плавно догоняет крупные."""
    if len(vals) == 0 or np.all(np.isnan(vals)):
        return np.full(n_frames, default)
    v = vals.copy()
    idx = np.where(~np.isnan(v))[0]
    v[: idx[0]] = v[idx[0]]
    for k in range(idx[0] + 1, len(v)):  # заполняем пропуски последним известным
        if np.isnan(v[k]):
            v[k] = v[k - 1]
    # медианный фильтр от случайных ложных срабатываний
    med = np.array([np.median(v[max(0, k - 2): k + 3]) for k in range(len(v))])
    cam = np.empty_like(med)
    pos = med[0]
    for k, target in enumerate(med):
        if abs(target - pos) > deadzone:
            pos += (target - pos) * follow
        cam[k] = pos
    t_out = np.arange(n_frames) / fps
    per_frame = np.interp(t_out, times, cam)
    w = max(1, int(avg_sec * fps))
    kernel = np.ones(w) / w
    padded = np.pad(per_frame, (w // 2, w - 1 - w // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


# ---------------------------------------------------------------- шаг 4: субтитры
def ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def rgb_to_ass(hex_rgb):
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{b}{g}{r}&".upper()


def clean_word(w, upper):
    w = re.sub(r"^[\"'«(]+|[\"'»),.;:…]+$", "", w)
    w = w.replace("{", "").replace("}", "").replace("\\", "")
    return w.upper() if upper else w


def chunk_words(words, max_words=3, max_chars=16):
    chunks, cur = [], []
    for i, w in enumerate(words):
        if cur and sum(len(x["word"]) + 1 for x in cur) + len(w["word"]) > max_chars:
            chunks.append(cur)
            cur = []
        cur.append(w)
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 99
        if (len(cur) >= max_words or gap > 0.4
                or w["word"].endswith((".", "!", "?", ",", "…"))):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def make_ass(words, clip_s, clip_e, path, a, top_text=""):
    ws = [dict(w, start=w["start"] - clip_s, end=w["end"] - clip_s)
          for w in words if w["end"] > clip_s and w["start"] < clip_e]
    ws = [w for w in ws if clean_word(w["word"], False)]
    chunks = chunk_words(ws, a.words)
    hl = rgb_to_ass(a.hl_color)
    y = int(OUT_H * a.sub_y)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{a.font},{a.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,7,3,5,70,70,0,1
Style: Top,{a.font},{a.top_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    dur = clip_e - clip_s
    if top_text:
        tt = top_text.replace("{", "").replace("}", "").replace("\\", "")
        tt = tt.upper() if a.upper else tt
        ty = int(OUT_H * a.top_y)
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Top,,0,0,0,,{{\\an5\\pos({OUT_W // 2},{ty})}}{tt}")
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else dur
        chunk_end = min(nxt, ch[-1]["end"] + 0.5, dur)
        for k, w in enumerate(ch):
            st = w["start"]
            en = ch[k + 1]["start"] if k + 1 < len(ch) else chunk_end
            if en - st < 0.02:
                continue
            parts = []
            for m, x in enumerate(ch):
                t = clean_word(x["word"], a.upper)
                if m == k:
                    parts.append(f"{{\\c{hl}\\fscx112\\fscy112}}{t}{{\\c&HFFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(t)
            text = f"{{\\an5\\pos({OUT_W // 2},{y})}}" + " ".join(parts)
            lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Cap,,0,0,0,,{text}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- шаг 5: рендер
def zoom_curve(n_frames, fps, punch_times, amount):
    """Наезд/отъезд камеры: чередуем 1.0 и 1+amount на каждой новой фразе + лёгкий общий наезд."""
    t = np.arange(n_frames) / fps
    target = np.ones(n_frames)
    if amount > 0 and punch_times:
        level = 0
        pts = sorted(punch_times)
        for k in range(n_frames):
            while pts and t[k] >= pts[0]:
                pts.pop(0)
                level ^= 1
            target[k] = 1.0 + amount * level
    z = np.empty(n_frames)
    cur = target[0]
    alpha = 1 - math.exp(-1 / (fps * 0.12))
    for k in range(n_frames):
        cur += (target[k] - cur) * alpha
        z[k] = cur
    if amount > 0:
        z += 0.04 * t / max(t[-1], 1e-6)  # лёгкий «кен бёрнс»
    return z


def compose_fit(frame, zz, a, fg_h, bg_cache):
    """Весь кадр 16:9 по центру + размытая копия на фоне (формат для геймплея)."""
    import cv2
    h, w = frame.shape[:2]
    # фон: центральная 9:16-область, сильно размытая и затемнённая
    cw = min(w, h * 9 / 16)
    x0 = int((w - cw) / 2)
    small = cv2.resize(frame[:, x0:x0 + int(cw)], (108, 192), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), 6)
    bg = cv2.resize(small, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
    bg = cv2.convertScaleAbs(bg, alpha=0.55, beta=0)
    # передний план: обрезаем бока на fit_zoom, плюс динамический зум
    crop_w = w / (a.fit_zoom * zz)
    crop_h = h / zz
    cx, cy = w / 2, h / 2
    sx = OUT_W / crop_w
    sy = fg_h / crop_h
    M = np.float32([[sx, 0, -(cx - crop_w / 2) * sx], [0, sy, -(cy - crop_h / 2) * sy]])
    fg = cv2.warpAffine(frame, M, (OUT_W, fg_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    y0 = int(OUT_H * a.fit_y - fg_h / 2)
    y0 = min(max(y0, 0), OUT_H - fg_h)
    bg[y0:y0 + fg_h] = fg
    return bg


def render_clip(video, src_w, src_h, s, e, words, sents, out_path, workdir, a, rms=None, hop=0.25,
                top_text=""):
    import cv2
    dur = e - s
    n_frames = int(math.ceil(dur * OUT_FPS)) + 3
    clipdir = workdir / out_path.stem
    clipdir.mkdir(parents=True, exist_ok=True)
    ass_path = clipdir / "subs.ass"
    make_ass(words, s, e, ass_path, a, top_text)

    # 9:16 окно в исходнике при zoom=1 (режим crop)
    if src_w / src_h > 9 / 16:
        base_ch, base_cw = src_h, src_h * 9 / 16
    else:
        base_cw, base_ch = src_w, src_w * 16 / 9
    fg_h = int(round(OUT_W * src_h / (src_w / a.fit_zoom) / 2)) * 2
    fg_h = min(fg_h, OUT_H)

    if a.no_face or a.layout == "fit":
        fx = np.full(n_frames, 0.5)
        fy = np.full(n_frames, np.nan)
    else:
        ts, xs, ys = face_track(video, s, dur, src_w, src_h)
        fx = smooth_camera(ts, xs, n_frames, OUT_FPS)
        fy = smooth_camera(ts, ys, n_frames, OUT_FPS, default=np.nan) if not np.all(np.isnan(ys)) \
            else np.full(n_frames, np.nan)

    punches = [st["start"] - s for st in sents if s + 0.5 < st["start"] < e - 1.0]
    if not punches and rms is not None:
        punches = loud_peaks(rms, hop, s, e)
    z = zoom_curve(n_frames, OUT_FPS, punches, a.zoom)

    fontsdir = ""
    if a.fonts_dir:
        fd = clipdir / "fonts"
        fd.mkdir(exist_ok=True)
        for f in Path(a.fonts_dir).glob("*.[ot]tf"):
            shutil.copy(f, fd / f.name)
        fontsdir = ":fontsdir=fonts"

    inputs = ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(OUT_FPS), "-i", "-",
              "-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", str(Path(video).resolve())]
    if a.banner:
        bw = int(OUT_W * a.banner_width)
        by = f"main_h*{a.banner_y}-overlay_h/2"
        inputs += ["-i", str(Path(a.banner).resolve())]
        fc = (f"[2:v]scale={bw}:-1:flags=lanczos,format=rgba[b];"
              f"[0:v][b]overlay=(main_w-overlay_w)/2:{by}:format=auto[v0];"
              f"[v0]ass=subs.ass{fontsdir},format=yuv420p[v]")
    else:
        fc = f"[0:v]ass=subs.ass{fontsdir},format=yuv420p[v]"

    dec = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
         "-an", "-vf", f"fps={OUT_FPS}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE)
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", fc, "-map", "[v]", "-map", "1:a:0?",
         "-c:v", "libx264", "-preset", a.preset, "-crf", "19", "-r", str(OUT_FPS),
         *(["-maxrate", a.maxrate, "-bufsize", f"{float(a.maxrate[:-1]) * 2:g}{a.maxrate[-1]}"]
           if a.maxrate else []),
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest",
         str(out_path.resolve())],
        stdin=subprocess.PIPE, cwd=str(clipdir))

    fsize = src_w * src_h * 3
    k = 0
    try:
        while True:
            buf = dec.stdout.read(fsize)
            if len(buf) < fsize:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(src_h, src_w, 3)
            i = min(k, n_frames - 1)
            zz = z[i]
            if a.layout == "fit":
                out = compose_fit(frame, zz, a, fg_h, None)
            else:
                cw, ch = base_cw / zz, base_ch / zz
                cx = min(max(fx[i] * src_w, cw / 2), src_w - cw / 2)
                if np.isnan(fy[i]):
                    cy = src_h / 2
                else:  # лицо чуть выше центра вертикального кадра
                    cy = fy[i] * src_h + ch * 0.12
                cy = min(max(cy, ch / 2), src_h - ch / 2)
                sc = OUT_W / cw
                M = np.float32([[sc, 0, -(cx - cw / 2) * sc], [0, sc, -(cy - ch / 2) * sc]])
                out = cv2.warpAffine(frame, M, (OUT_W, OUT_H), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_REPLICATE)
            enc.stdin.write(out.tobytes())
            k += 1
    finally:
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode != 0:
        raise RuntimeError(f"ffmpeg не смог собрать {out_path.name}")


# ---------------------------------------------------------------- пресеты кампаний
PRESETS = {
    # игровые нарезки: весь кадр + размытый фон, 60 FPS, баннер сверху, субтитры снизу
    "game": dict(layout="fit", fps=60, min_len=40, max_len=70, zoom=0.06, sub_y=0.78,
                 banner_y=0.11, top_y=0.25, no_face=True),
    # подкасты / интервью: кроп по лицу, субтитры по центру
    "podcast": dict(layout="crop", fps=30, min_len=30, max_len=50, zoom=0.10, sub_y=0.5,
                    banner_y=0.10, top_y=0.20),
}


# ---------------------------------------------------------------- main
def main():
    global OUT_FPS
    ap = argparse.ArgumentParser(description="Автонарезка длинного видео в вертикальные шортсы с субтитрами.")
    ap.add_argument("input", help="путь к видео или ссылка (нужен yt-dlp)")
    ap.add_argument("--preset", dest="preset_name", choices=list(PRESETS),
                    help="готовые настройки: game (геймплей) или podcast")
    ap.add_argument("--out", default="clips", help="папка для готовых роликов (по умолчанию clips)")
    ap.add_argument("--clips", type=int, default=8, help="сколько роликов сделать")
    ap.add_argument("--min", dest="min_len", type=float, help="мин. длина ролика, сек (30)")
    ap.add_argument("--max", dest="max_len", type=float, help="макс. длина ролика, сек (50)")
    ap.add_argument("--fps", type=int, help="частота кадров результата (30; для STALZONE нужно 60)")
    ap.add_argument("--layout", choices=["crop", "fit"],
                    help="crop — обрезать до 9:16 по лицу; fit — весь кадр + размытый фон (геймплей)")
    ap.add_argument("--fit-zoom", type=float, default=1.3, help="в режиме fit: насколько обрезать бока (1.0 — не резать)")
    ap.add_argument("--fit-y", type=float, default=0.5, help="в режиме fit: высота центра картинки")
    ap.add_argument("--banner", help="PNG-плашка (логотип), накладывается на весь ролик")
    ap.add_argument("--banner-width", type=float, default=0.62, help="ширина плашки в долях кадра")
    ap.add_argument("--banner-y", type=float, help="высота центра плашки (0.1 — сверху)")
    ap.add_argument("--top-text", default="",
                    help="текст-заголовок на весь ролик; варианты через | — будут чередоваться по роликам")
    ap.add_argument("--top-y", type=float, help="высота заголовка")
    ap.add_argument("--top-size", type=int, default=72)
    ap.add_argument("--no-subs", action="store_true", help="без субтитров и распознавания речи (чистый геймплей)")
    ap.add_argument("--model", default="small", help="модель Whisper: tiny/base/small/medium/large-v3")
    ap.add_argument("--lang", default=None, help="язык речи: ru, en… (по умолчанию автоопределение)")
    ap.add_argument("--device", default="cpu", help="cpu или cuda (если есть видеокарта NVIDIA)")
    ap.add_argument("--keywords", default="", help="слова через запятую, которые повышают шанс выбора куска")
    ap.add_argument("--segments", help="файл с таймкодами 12:30-13:15 (по строке на ролик) вместо автопоиска")
    ap.add_argument("--transcribe-only", action="store_true", help="только сделать транскрипт и выйти")
    ap.add_argument("--no-face", action="store_true", default=None, help="не искать лицо, кадрировать по центру")
    ap.add_argument("--zoom", type=float, help="сила динамического зума (0 — выключить)")
    ap.add_argument("--sub-y", type=float, help="высота субтитров: 0.5 — центр, 0.78 — снизу")
    ap.add_argument("--font", default="Arial", help="шрифт субтитров")
    ap.add_argument("--fonts-dir", help="папка с .ttf/.otf, если шрифт не установлен в системе")
    ap.add_argument("--font-size", type=int, default=84)
    ap.add_argument("--hl-color", default="FFE600", help="цвет текущего слова, RGB hex")
    ap.add_argument("--words", type=int, default=3, help="макс. слов в строке субтитров")
    ap.add_argument("--no-upper", dest="upper", action="store_false", help="не делать субтитры капсом")
    ap.add_argument("--x264", dest="preset", default="veryfast", help="скорость кодирования x264")
    ap.add_argument("--maxrate", help="ограничить битрейт видео, напр. 5M (чтобы файл влез в Telegram)")
    ap.add_argument("--exclude", help="JSON-файл с уже использованными отрезками [[start,end],...]")
    a = ap.parse_args()

    # пресет заполняет то, что не задано руками
    base = dict(layout="crop", fps=30, min_len=30, max_len=50, zoom=0.10, sub_y=0.5,
                banner_y=0.10, top_y=0.20, no_face=False)
    base.update(PRESETS.get(a.preset_name, {}))
    for k, v in base.items():
        if getattr(a, k, None) is None:
            setattr(a, k, v)
    OUT_FPS = a.fps
    if a.banner and not Path(a.banner).exists():
        sys.exit(f"Не найден файл плашки: {a.banner}")

    need_ffmpeg()
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "_work"
    workdir.mkdir(exist_ok=True)

    video = get_source(a.input, workdir)
    src_w, src_h, total = probe(video)
    stem = re.sub(r"[^\w\-]+", "_", video.stem)[:60]
    log(f"Видео: {video.name} — {src_w}x{src_h}, {fmt_ts(total)}")
    if src_h < 1080 and a.fps == 60:
        log("  ! исходник ниже 1080p — ролик может не пройти требование FullHD")

    wav = workdir / f"{stem}.wav"
    if not wav.exists():
        log("Достаю звук…")
        extract_audio(video, wav)
    rms, hop = rms_curve(wav)

    words, sents = [], []
    if not a.no_subs:
        data = transcribe(wav, out_dir / f"{stem}.transcript.json", a.model, a.lang, a.device)
        words = data["words"]
        sents = build_sentences(words)
        if sents:
            txt_path = out_dir / f"{stem}.transcript.txt"
            write_readable_transcript(sents, txt_path)
            log(f"Транскрипт с таймкодами: {txt_path}")
    if a.transcribe_only:
        return

    speech_rich = len(words) / max(total / 60, 1e-6) >= 40  # слов в минуту
    excluded = []
    if a.exclude and Path(a.exclude).exists():
        excluded = [tuple(x) for x in json.loads(Path(a.exclude).read_text(encoding="utf-8"))]
        if excluded:
            log(f"Пропускаю {len(excluded)} уже использованных отрезков")
    if a.segments:
        moments = read_segments_file(a.segments, total)
        log(f"Беру {len(moments)} отрезков из {a.segments}")
    elif speech_rich:
        kws = [k.strip().lower() for k in a.keywords.split(",") if k.strip()]
        moments = find_moments(sents, rms, hop, a.clips, a.min_len, a.max_len, kws, total, excluded)
        log(f"Нашёл {len(moments)} моментов (по речи)")
    else:
        log("Речи мало — ищу моменты по экшену (громкость + движение)…")
        motion, mhop = motion_curve(video)
        moments = find_action_moments(rms, hop, motion, mhop, a.clips, a.min_len, a.max_len, total, excluded)
        log(f"Нашёл {len(moments)} моментов (по экшену)")
    if not moments:
        log("Не нашлось новых подходящих отрезков (видео исчерпано или слишком короткое).")
        (out_dir / f"{stem}.clips.json").write_text("[]", encoding="utf-8")
        sys.exit(3)

    titles = [t.strip() for t in a.top_text.split("|") if t.strip()]
    manifest = []
    for n, (s, e, score, first) in enumerate(moments, 1):
        out_path = out_dir / f"{stem}_{n:02d}.mp4"
        text = " ".join(w["word"] for w in words if s <= w["start"] < e)
        title = titles[(n - 1) % len(titles)] if titles else ""
        log(f"[{n}/{len(moments)}] {fmt_ts(s)}–{fmt_ts(e)} ({e - s:.0f} с) → {out_path.name}")
        render_clip(video, src_w, src_h, s, e, words, sents, out_path, workdir, a, rms, hop, title)
        manifest.append({"file": out_path.name, "start": round(s, 2), "end": round(e, 2),
                         "score": round(score, 2), "title": title,
                         "hook": (first or text[:100]).strip(), "text": text})
    (out_dir / f"{stem}.clips.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    log(f"Готово! Ролики в папке: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
