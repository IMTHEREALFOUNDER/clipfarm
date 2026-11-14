# Ответы для формы аудита YouTube API

Форма: **support.google.com/youtube/contact/yt_api_form**

Перед заполнением:
1. Опубликуй политику конфиденциальности (текст в `docs/privacy.md`). Самый простой способ — telegra.ph: вставь текст и опубликуй. Полученная ссылка и есть URL политики.
2. Запиши короткое видео экрана (1–2 минуты): `python auth.py` → экран согласия Google → запуск конвейера → ролик появился в YouTube Studio. Загрузи видео на Google Диск с доступом по ссылке и приложи ссылку в форме. Иногда проверяющие просят такое демо.
3. Возьми **Project number** в Google Cloud: Dashboard → Project info.

В квадратных скобках — то, что подставляешь сам. Отвечай честно: проверяют соответствие реальному использованию.

---

**Reason for filling this form:** Complete a compliance audit to request for additional quota
*(в форме нет отдельного пункта «снять приватность», аудит идёт через этот вариант)*

**Applying as:** As an individual user
**Your Full Legal Name:** [Имя Фамилия латиницей, как в паспорте]
**Organization's Legal Name:** self
**Organization's Primary Website:** [URL политики конфиденциальности]
**Contact email:** [почта Google-аккаунта с каналами]

**API Client name:** ClipFarm Scheduler
**Google Cloud project number:** [номер проекта]
**APIs used:** YouTube Data API v3
**Primary audience / users:** Strictly internal use only
**Approximate number of users:** 1

**Describe your API client (min 100 chars):**
> ClipFarm Scheduler is a private, internal tool used only by me to publish short vertical gameplay clips to my own YouTube channels on a schedule. I edit the clips from gameplay footage provided to me by the game's publisher as part of a creator campaign (with permission to publish). The tool uploads each clip once via videos.insert with a scheduled publishAt time, title, description and tags. It is not offered to any other users, has no public interface, and does not display, analyse, or store YouTube API data other than the video IDs of my own uploads, which I keep in a log to send links to the campaign.

**How do you use YouTube API Services today:**
> videos.insert — uploading my own edited clips (2–3 per day per channel) as private with status.publishAt so they go public at the scheduled time. channels.list (mine=true) — once, during authorization, to confirm which of my channels was selected. No other endpoints are used.

**Do you store YouTube API data?** Only the IDs of videos I uploaded, for my own records. <24 hours for anything else.
**Do you display YouTube data to others?** No
**Do you sell ads / monetize the API client?** No / Not applicable
**Does your app name contain "YouTube"?** No
**How did you hear about the API?** Google Developer Documentation

**Quota requested:** default quota is sufficient (≈10 uploads/day). The audit is needed so that uploads are not restricted to private.

---

Ответ обычно приходит на почту. Если попросят уточнения, пришли мне письмо, помогу ответить.
