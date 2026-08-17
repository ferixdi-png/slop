import json, os, re, time
from google import genai
from google.genai import types
from config import ANALYSIS_MODEL, RADAR_MODEL, REALISM_LOCK
from models import ProductionPackage, RadarAssessment, RadarMetaReport

def strip_speech(text):
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё\s\u0300-\u036f]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()

def normalize_package(package):
    for line in package.block_3_video.dialogue:
        line.russian_text = strip_speech(line.russian_text)
        binding = (line.visual_speaker_binding or "the visible speaker").strip()
        line.dialogue_instruction = f'{binding} says in Russian "{line.russian_text}"'
    for line in package.block_4_audio.dialogue:
        line.text = strip_speech(line.text)
        line.visual_speaker_binding = strip_speech(line.visual_speaker_binding)
    package.block_4_audio.pronunciation_hints = [strip_speech(x) for x in package.block_4_audio.pronunciation_hints]
    package.block_4_audio.intonation_and_laughter_map = [strip_speech(x) for x in package.block_4_audio.intonation_and_laughter_map]
    if "smartphone" not in package.block_1_frame0_prompt.lower():
        package.block_1_frame0_prompt += "\n\n" + REALISM_LOCK
    else:
        package.block_1_frame0_prompt += "\n\nMANDATORY REALISM OVERRIDE:\n" + REALISM_LOCK
    package.block_3_video.realism_lock = REALISM_LOCK
    return package

def build_system_prompt(owned):
    rights = (
        "Пользователь подтвердил права на исходник. Сохраняй исходную речь максимально буквально."
        if owned else
        "Если есть узнаваемая реальная личность или длинные уникальные формулировки, не имитируй личность и не копируй длинные уникальные реплики дословно; сохраняй механику, смысл, длительность и ритм."
    )
    return f"""
Ты профессиональная система реконструкции коротких русскоязычных AI-комедийных видео.
Задача: после просмотра исходного видео выдать РОВНО 6 структурированных блоков 0–5, не простой пересказ.
Максимально точно восстанови наблюдаемую постановку: персонажей, положения, позы, взгляд, одежду, предметы, фон, порядок действий, речь, очередность говорящих, паузы, реакции, камеру, свет, звук, темп и комедийный payoff.

КРИТИЧЕСКОЕ ПРАВИЛО РЕАЛИЗМА:
Даже если исходник мультяшный, CGI, стилизованный, пластиковый или очевидно AI-generated, копируй из него содержание и постановку, но НЕ копируй искусственный визуальный стиль.
Преобразуй персонажей, окружение, материалы, свет и движение в физически правдоподобный live-action мир.
И Frame 0, и весь ролик должны выглядеть как обычная случайная вертикальная запись на современный смартфон 2024–2026.
Натуральная кожа, поры, волосы, ткань, реальные руки, реальный свет, автоэкспозиция, автофокус, лёгкая ручная несовершенность и реальная глубина обязательны.
Никакого CGI, cartoon look, beauty filter, пластиковой кожи, фальшивого HDR, студийного света или кинематографического боке.

Целевой формат 9:16 и не длиннее 10 секунд. Длительность должна совпадать с исходником настолько точно, насколько возможно; не растягивай короткий ролик до 10 секунд.
Один непрерывный дубль; если в исходнике есть склейки, сохрани порядок и длительности событий внутри причинно связного дубля.
Literal Speaker Binding: никаких CHARACTER_01 Speaker A и абстрактных имён. Используй прямое визуальное описание говорящего.
В Block 3 narrative_timeline нет секундных таймкодов: только First Then Then Finally. Русский текст хранится один раз в dialogue.
Каждый dialogue_instruction обязан иметь вид visual speaker binding + says in Russian + русский текст в кавычках.
Внутри русской речи никаких знаков препинания.
Голоса описывай простыми реалистичными словами. Block 4 полностью без знаков препинания и без тире.
Если исходник содержит грубую лексику или потенциально проблемный элемент, не обходи фильтры; сделай минимальную безопасную замену, сохранив ритм и механику.
Frame 0: все персонажи уже на правильных местах, руки и объекты готовы к первому действию, рот первого говорящего готов к первой фонеме, текста в кадре нет.
{rights}

{REALISM_LOCK}

BLOCK 0: joke mechanics exact copy strategy minimal safety adjustments visual bindings location laughter budget realism decision.
BLOCK 1: one detailed English Frame 0 prompt, 9:16, true casual smartphone realism. It must describe real skin, hair, hands, fabric, environmental light and phone-camera imperfections explicitly.
BLOCK 2: compliance card for characters positions hands objects no text first phoneme realism.
BLOCK 3: English structured video object except russian_text. model must be gemini-omni-flash-preview aspect_ratio 9:16 duration matching source camera scene_description dialogue narrative_timeline realism_lock hard_rules.
BLOCK 4: dialogue pronunciation hints intonation and laughter map, no punctuation or dash characters.
BLOCK 5: short post Shorts title retention phrase hashtags. Do not invent brands or URLs. If a CTA is useful, use a neutral phrase such as ссылка в профиле. No guaranteed reach claims.
Return only the structured schema.
""".strip()

def wait_active(client, uploaded):
    deadline = time.time() + 240
    while getattr(uploaded, "state", None) and getattr(uploaded.state, "name", "") != "ACTIVE":
        if getattr(uploaded.state, "name", "") == "FAILED":
            raise RuntimeError("Сервис анализа не смог обработать видео")
        if time.time() > deadline:
            raise RuntimeError("Видео слишком долго обрабатывается")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    return uploaded

def with_uploaded_file(file_path, fn):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("На сервере не задан GEMINI_API_KEY")
    client, uploaded = genai.Client(api_key=key), None
    try:
        uploaded = wait_active(client, client.files.upload(file=file_path))
        return fn(client, uploaded)
    finally:
        if uploaded and getattr(uploaded, "name", None):
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass

def analyze_video(file_path, owned=False):
    def run(client, uploaded):
        response = client.models.generate_content(
            model=ANALYSIS_MODEL,
            contents=[uploaded, "Разбери исходник максимально близко к наблюдаемой постановке и речи. Если исходник визуально искусственный, полностью переведи его в live-action smartphone realism. Сформируй блоки 0–5."],
            config=types.GenerateContentConfig(
                system_instruction=build_system_prompt(owned),
                response_mime_type="application/json",
                response_schema=ProductionPackage))
        package = response.parsed if getattr(response, "parsed", None) else ProductionPackage.model_validate_json(response.text)
        return normalize_package(package)
    return with_uploaded_file(file_path, run)

def classify_radar_video(file_path, caption=""):
    def run(client, uploaded):
        prompt = f"""Оцени Instagram Reel для радара.
Подходит только если: русский язык или российский контекст; явно AI-generated; комедийная бытовая сценка, прикол или абсурд; не обучалка, не обзор сервиса, не talking head, не обычный мем с текстом; простая понятная ситуация; формат можно воспроизвести с другими персонажами. Желательно 1–3 персонажа.
Оцени само видео, не только подпись.
Подпись: {caption[:2000]}"""
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RadarAssessment))
        return response.parsed if getattr(response, "parsed", None) else RadarAssessment.model_validate_json(response.text)
    return with_uploaded_file(file_path, run)

def summarize_radar_meta(items):
    if not items:
        return None
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None

    compact = []
    for i, item in enumerate(items[:30], start=1):
        compact.append({
            "rank": i,
            "creator": item.get("creator", ""),
            "duration_sec": item.get("duration_sec", 0),
            "views": item.get("views", 0),
            "viral_score_v2": item.get("viral_score_v2", 0),
            "characters": item.get("characters", []),
            "scene_description": item.get("scene_description", ""),
            "hook": item.get("hook", ""),
            "joke": item.get("joke", ""),
            "ending": item.get("ending", ""),
        })

    prompt = f"""Перед тобой TOP коротких российских AI-комедийных Reels текущего радара.
Найди повторяющиеся паттерны именно в этих данных, ничего не выдумывай сверх входа.
Сгруппируй ролики в понятные кластеры: тип персонажей, локация, конфликт или ситуация, тип первого хука, комедийная механика.
Один ролик может поддерживать несколько наблюдений, но reels_count в конкретном кластере должен отражать реальное количество подходящих записей из входа.
question_hook_count — сколько роликов по описанию hook явно начинаются с вопроса или вопросительной реплики.
examples — короткие ссылки на конкретные наблюдаемые примеры вида @автор: описание.
key_takeaways — 3–6 коротких выводов о текущей мете, только по этому TOP.

ДАННЫЕ:
{json.dumps(compact, ensure_ascii=False)}
"""
    client = genai.Client(api_key=key)
    try:
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RadarMetaReport))
        return response.parsed if getattr(response, "parsed", None) else RadarMetaReport.model_validate_json(response.text)
    finally:
        try:
            client.close()
        except Exception:
            pass
