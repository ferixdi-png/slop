import json, os, re, time
from google import genai
from google.genai import types
from config import ANALYSIS_MODEL, RADAR_MODEL, REALISM_LOCK
from models import ProductionPackage, RadarAssessment, RadarMetaReport


def strip_speech(text):
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё\s\u0300-\u036f]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def normalize_package(package, expected_duration=None):
    if expected_duration and expected_duration > 0:
        exact = round(float(expected_duration), 2)
        package.source_duration_sec = exact
        package.block_3_video.exact_duration_sec = exact
        package.block_3_video.duration = f"{exact:.2f} seconds"
        package.block_3_video.duration_lock = (
            f"ABSOLUTE SOURCE DURATION LOCK: the generated video must last exactly {exact:.2f} seconds. "
            "Do not extend, shorten, pad, slow down, speed up or reinterpret the total duration. "
            "All actions, speech, pauses, reactions and the final beat must fit inside the exact source duration."
        )

    for line in package.block_3_video.dialogue:
        line.russian_text = strip_speech(line.russian_text)
        binding = (line.visual_speaker_binding or "the visible speaker").strip()
        line.dialogue_instruction = f'{binding} says in Russian "{line.russian_text}"'

    for line in package.block_4_audio.dialogue:
        line.text = strip_speech(line.text)
        line.visual_speaker_binding = strip_speech(line.visual_speaker_binding)

    package.block_4_audio.pronunciation_hints = [strip_speech(x) for x in package.block_4_audio.pronunciation_hints]
    package.block_4_audio.intonation_and_laughter_map = [strip_speech(x) for x in package.block_4_audio.intonation_and_laughter_map]

    package.block_1_frame0_prompt += "\n\nMANDATORY ABSOLUTE SMARTPHONE REALISM OVERRIDE:\n" + REALISM_LOCK
    package.block_3_video.realism_lock = REALISM_LOCK

    mandatory_hard_rules = [
        "Preserve the exact source duration with zero intentional padding or trimming",
        "Russian speech only whenever the source contains dialogue",
        "Only the visually bound speaking character may move lips during that line",
        "All non-speaking characters keep lips completely closed and motionless",
        "Never duplicate or teleport props characters hands or objects",
        "Every important action happens once only and in the original causal order",
        "Do not inherit cartoon CGI 3D or synthetic rendering from the source",
        "Maintain authentic casual handheld smartphone realism throughout the entire clip",
    ]
    existing = list(package.block_3_video.hard_rules or [])
    for rule in mandatory_hard_rules:
        if rule not in existing:
            existing.append(rule)
    package.block_3_video.hard_rules = existing
    return package


def build_system_prompt(owned, expected_duration=None):
    rights = (
        "Пользователь подтвердил права на исходник. Сохраняй исходную речь максимально буквально."
        if owned else
        "Если есть узнаваемая реальная личность или длинные уникальные формулировки, не имитируй личность и не копируй длинные уникальные реплики дословно; сохраняй механику, смысл, длительность и ритм."
    )
    duration_rule = (
        f"ИЗМЕРЕННАЯ ДЛИТЕЛЬНОСТЬ ИСХОДНИКА: {float(expected_duration):.2f} секунды. "
        f"Это абсолютный lock. Итоговый ролик должен быть РОВНО {float(expected_duration):.2f} секунды. "
        "Никогда не округляй его до 10 секунд и не добавляй пустой хвост."
        if expected_duration and expected_duration > 0
        else
        "Определи фактическую длительность исходника и сохрани её без намеренного растягивания или сокращения."
    )
    return f"""
Ты профессиональная система максимально точной реконструкции коротких русскоязычных AI-комедийных видео.
После просмотра исходного видео выдай РОВНО 6 структурированных блоков 0–5, не простой пересказ.
Максимально точно восстанови наблюдаемую постановку: персонажей, положения, позы, взгляд, одежду, предметы, фон, порядок действий, речь, очередность говорящих, паузы, реакции, камеру, свет, звук, темп и комедийный payoff.

АБСОЛЮТНЫЙ DURATION LOCK:
{duration_rule}
Если исходник 7.60 секунды, пакет и оживление строятся под 7.60 секунды. Если 9.13 секунды, под 9.13 секунды. Никакого автоматического доведения до 10 секунд.
В радар попадают только исходники до 10 секунд.

КРИТИЧЕСКОЕ ПРАВИЛО РЕАЛИЗМА:
Даже если исходник мультяшный, CGI, стилизованный, пластиковый или очевидно AI-generated, копируй из него содержание и постановку, но НЕ копируй искусственный визуальный стиль.
Преобразуй персонажей, окружение, материалы, свет и движение в физически правдоподобный live-action мир.
И Frame 0, и весь ролик должны выглядеть как обычная случайная вертикальная запись на современный смартфон 2024–2026.
Натуральная кожа, поры, волосы, ткань, реальные руки, реальный свет, автоэкспозиция, автофокус, лёгкая ручная несовершенность и реальная глубина обязательны.
Никакого CGI, cartoon look, beauty filter, пластиковой кожи, фальшивого HDR, студийного света или кинематографического боке.

РУССКАЯ РЕЧЬ И SPEAKER LOCK:
Если в исходнике есть речь, целевой ролик должен быть с русской речью.
Literal Speaker Binding: никаких CHARACTER_01 Speaker A и абстрактных имён. Используй прямое физическое описание говорящего.
Каждая реплика хранится только один раз в dialogue.
Каждый dialogue_instruction строго имеет вид visual speaker binding + says in Russian + русский текст в кавычках.
Внутри russian_text никаких знаков препинания.
В момент реплики губы двигает только указанный визуально персонаж. Все остальные персонажи полностью молчат, рот закрыт, губы неподвижны.
Не допускай перекрытия, смешивания или обмена репликами между персонажами.

NARRATIVE TIMELINE:
Никаких секундных таймкодов внутри narrative_timeline. Только последовательный причинный рассказ First Then Then Finally.
Сохраняй исходный порядок и относительную длительность каждого события внутри абсолютной общей длительности.
Каждое важное действие происходит ровно один раз. Не повторяй жест, падение, передачу предмета, поворот или punchline.
Если исходник содержит склейки, воспроизведи наблюдаемую последовательность максимально близко, но не выдумывай дополнительные события.

CONTINUITY AND OBJECT LOCK:
Персонажи сохраняют внешность, одежду, положение и физическую непрерывность между всеми моментами.
Каждый предмет существует в одном экземпляре, если исходник явно не показывает несколько.
Никаких телепортаций, внезапных исчезновений, клонов, дополнительных пальцев, лишних конечностей или самопроизвольной передачи предметов.
Руки должны иметь физически понятную цель и анатомически правдоподобное движение.

CAMERA AND PHYSICAL BEHAVIOR:
Камера должна максимально повторять исходную высоту, дистанцию, направление, композицию и характер движения, но физически выглядеть как реальная handheld smartphone capture.
Опиши естественную микротряску, дрейф кадра, автофокус, автоэкспозицию, перспективу смартфона, реальное motion blur и отсутствие искусственного cinematic bokeh.
Персонажи должны двигаться с реальной массой, инерцией, балансом и контактом с поверхностями.
Окружение должно физически реагировать правдоподобно: ткань, волосы, тени, предметы и контактные точки.

Голоса описывай простыми реалистичными словами. Block 4 полностью без знаков препинания и без тире.
Если исходник содержит грубую лексику или потенциально проблемный элемент, не обходи фильтры; сделай минимальную безопасную замену, сохранив ритм и механику.
Frame 0: все персонажи уже на правильных местах, руки и объекты готовы к первому действию, рот первого говорящего готов к первой фонеме, текста в кадре нет.
{rights}

{REALISM_LOCK}

BLOCK 0: joke mechanics exact copy strategy minimal safety adjustments visual bindings location laughter budget realism decision.
BLOCK 1: один УЛЬТРА-ДЕТАЛЬНЫЙ English Frame 0 prompt 9:16. Он должен описать точное расположение персонажей, позы, выражения, взгляд, руки, предметы, одежду, фон, камеру, свет, skin pores, hair strands, fabric texture, shadows, autofocus/exposure imperfections и готовность к первому движению/первой фонеме.
BLOCK 2: compliance card for characters positions hands objects no text first phoneme realism.
BLOCK 3: максимально подробный production object на английском кроме russian_text. Обязательно заполни model aspect_ratio duration exact_duration_sec duration_lock camera scene_description visual_style camera_realism character_continuity_lock object_lock speaker_lock lip_sync_lock body_behavior hand_behavior environment_behavior motion_consistency forbidden_visual_traits dialogue narrative_timeline realism_lock hard_rules.
BLOCK 3 обязан быть достаточным для оживления Frame 0 без дополнительных пояснений пользователя.
BLOCK 4: dialogue pronunciation hints intonation and laughter map, no punctuation or dash characters.
BLOCK 5: short post Shorts title retention phrase hashtags. Не выдумывай бренды или URL. No guaranteed reach claims.
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


def analyze_video(file_path, owned=False, expected_duration=None):
    def run(client, uploaded):
        duration_text = (
            f"Measured source duration is exactly {float(expected_duration):.2f} seconds. Lock the entire package to that exact duration."
            if expected_duration and expected_duration > 0 else
            "Preserve the exact observed source duration."
        )
        response = client.models.generate_content(
            model=ANALYSIS_MODEL,
            contents=[
                uploaded,
                "Разбери исходник максимально близко к наблюдаемой постановке и речи. "
                "Если исходник визуально искусственный, полностью переведи его в live-action smartphone realism. "
                f"{duration_text} Сформируй блоки 0–5."
            ],
            config=types.GenerateContentConfig(
                system_instruction=build_system_prompt(owned, expected_duration),
                response_mime_type="application/json",
                response_schema=ProductionPackage))
        package = response.parsed if getattr(response, "parsed", None) else ProductionPackage.model_validate_json(response.text)
        return normalize_package(package, expected_duration)
    return with_uploaded_file(file_path, run)


def classify_radar_video(file_path, caption=""):
    def run(client, uploaded):
        prompt = f"""Оцени Instagram Reel для радара.
Подходит только если: русский язык или российский контекст; явно AI-generated; комедийная бытовая сценка, прикол или абсурд; не обучалка, не обзор сервиса, не talking head, не обычный мем с текстом; простая понятная ситуация; формат можно воспроизвести с другими персонажами. Желательно 1–3 персонажа.
Если есть речь, она должна быть русской или естественно относиться к российскому контексту.
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
