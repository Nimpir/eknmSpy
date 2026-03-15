# Discord Transcriber

Discord голос → диаризация → транскрипция → поиск по логам

**Стек:** discord.py · pyannote 3.1 · Whisper large-v3 · SQLite · tkinter

---

## Требования

- Windows 10/11 (или Linux)
- Python 3.11+
- NVIDIA GPU с 6+ GB VRAM (оптимально: RTX 4070 Ti — 12 GB)
- CUDA Toolkit 11.8 или 12.x
- FFmpeg

---

## Установка

### 1. FFmpeg

**Windows:** скачай с https://ffmpeg.org/download.html → добавь в PATH
```
# Проверка:
ffmpeg -version
```

### 2. Python зависимости

```bash
# Клонируй / распакуй проект
cd discord_transcriber

# Создай виртуальное окружение
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux

# PyTorch с CUDA (обязательно ДО остальных пакетов)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# Остальные зависимости
pip install -r requirements.txt
```

### 3. Токены

**Discord Bot Token:**
1. Зайди на https://discord.com/developers/applications
2. Создай новое приложение → вкладка "Bot"
3. Нажми "Reset Token" → скопируй токен
4. В разделе "Privileged Gateway Intents" включи:
   - `Server Members Intent`
   - `Voice States` (должен быть включён автоматически)
5. Пригласи бота на сервер: OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Connect`, `Speak`, `Use Voice Activity`

**Hugging Face Token (для pyannote):**
1. Зарегистрируйся на https://huggingface.co
2. Settings → Access Tokens → New token (read) 
3. Прими условия использования моделей:
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0

### 4. Настройка .env

```bash
cp .env.example .env
# Отредактируй .env — вставь оба токена
```

---

## Запуск

```bash
venv\Scripts\activate
python main.py
```

Откроется GUI.

---

## Использование

### Запись сессии

1. Вкладка **🤖 Бот** → вставь Discord Token → **▶ Запустить бот**
2. В Discord зайди в голосовой канал
3. Напиши в текстовом канале: `/join` → бот подключится и начнёт запись
4. Во время сессии: `/save 5` → бот пришлёт аудио-файл последних 5 минут
5. После сессии: `/leave` → бот отключится, файлы сохранятся в `data/recordings/`

### Обработка (транскрипция)

1. Вкладка **⚙️ Обработка**
2. После `/leave` поля заполнятся автоматически
   - Или выбери `mixed_mono.wav` вручную
   - Укажи Session ID (из вкладки Сессии)
3. Нажми **▶ Запустить обработку**
4. RTX 4070 Ti: ~4.5 часа аудио обрабатывается за 10–15 минут

### Просмотр лога

- Вкладка **📄 Лог** → введи Session ID → Загрузить
- Кнопка **🌐 HTML в браузере** → красивый интерактивный просмотр с поиском
- Файлы также сохраняются в `data/recordings/<имя_сессии>/`:
  - `transcript.txt` — обычный текст
  - `transcript.html` — с цветами по спикерам и встроенным поиском

### Поиск

- Вкладка **🔍 Поиск** → введи запрос → Enter
- Поиск по всем сессиям сразу (full-text search через SQLite FTS5)
- Пример: `алгоритм` — найдёт все фрагменты где упоминался этот слово

---

## Структура проекта

```
discord_transcriber/
├── main.py          ← точка входа
├── gui.py           ← интерфейс (tkinter)
├── bot.py           ← Discord бот
├── processor.py     ← pyannote + Whisper pipeline
├── db.py            ← SQLite база данных
├── requirements.txt
├── .env.example     ← шаблон для токенов
└── data/
    ├── transcripts.db
    └── recordings/
        └── <session_name>/
            ├── User1_123456789.wav   ← аудио каждого участника
            ├── User2_987654321.wav
            ├── mixed_mono.wav        ← смикшированный файл для Whisper
            ├── transcript.txt
            └── transcript.html
```

---

## Известные ограничения

- `discord.py` voice receive работает только на **самохостном боте** (не через OAuth2 third-party)
- Первый запуск скачает модели Whisper (~3 GB) и pyannote (~1 GB) — нужен интернет
- Качество диаризации снижается при сильном эхо / плохих микрофонах

---

## Следующие шаги (опционально)

- [ ] Добавить Telegram бота для удалённого доступа к логам
- [ ] Авто-обработка сразу после `/leave`
- [ ] Экспорт в `.docx`
- [ ] Суммаризация сессии через GPT/Claude API
