from datetime import datetime, timezone
from google.genai import types

import gemini_service
import radar_quality
import radar_request_job as radar_job
import radar_service
from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from db import db_conn
from media_duration import measure_video_duration
from models import RadarAssessment
from radar_logs import add_radar_log

PROFILE_VERSION="mass_global_ai_v8_brands"
TARGET_MATCHES=75
MIN_AI_CHECKS_BEFORE_EARLY_STOP=120
AI_ANALYZE_LIMIT=420
KEEP_LIMIT=60
SEARCH_LIMIT=64
HASHTAG_LIMIT=20

SEARCH_QUERY=(
"AI, ИИ, искусственный интеллект, нейросеть, нейронка, generative AI, GenAI, AI video, AI generated video, "
"нейроюмор, ии юмор, AI юмор, нейросеть прикол, AI прикол, нейровидео юмор, ии видео юмор, "
"AI бабушка юмор, нейросеть бабушка, AI дед юмор, нейросеть дед, AI деревня юмор, нейросеть деревня, "
"AI муж жена юмор, AI семья юмор, AI животные юмор, AI comedy, AI funny video, AI generated comedy, "
"AI generated funny, AI humor, AI skit, AI short comedy, AI meme video, AI absurd video, AI viral video, "
"AI slop, funny AI slop, AI grandma, AI grandpa, AI old people funny, AI family comedy, AI couple comedy, "
"AI animals funny, AI dog funny, AI cat funny, AI baby funny, AI village comedy, AI interview funny, "
"AI POV funny, Omni AI video, Gemini Omni, Google Flow Omni, Grok AI video, Grok video, xAI Grok, "
"Gemini AI video, Google Gemini video, Google AI video, ChatGPT AI video, OpenAI video, GPT Image video, "
"Veo 3 comedy, Veo 3 funny, Veo3 AI video, Veo 3.1 video, Google Veo, Google Flow AI, "
"Kling AI comedy, Kling AI video, Seedance comedy, Seedance AI video, Sora comedy, Sora AI video, "
"Runway AI comedy, Runway AI video, Hailuo AI funny, Minimax AI video funny, Flux AI video, "
"Midjourney AI video, Luma AI video, Dream Machine AI, Pika AI video, Higgsfield AI video, HeyGen AI video, "
"Nano Banana AI, Nano Banana video"
)
HASHTAGS_V7=[
"ai","ии","искусственныйинтеллект","нейронка","нейронки","нейросеть","нейросети","genai","generativeai",
"нейроюмор","ииюмор","аиюмор","нейровидео","иивидео","аивидео","нейроконтент","нейромем",
"нейросетьюмор","нейросетьприкол","нейрослоп","ииконтент","генерацияии","иигенерация",
"aicomedy","aihumor","aifunny","funnyai","aivideo","aivideos","aigeneratedvideo","aigeneratedvideos",
"aigenerated","aicontent","aicreator","aislop","aislopvideo","aimeme","aimemes","aiskit","aishorts",
"aishortvideo","aiabsurd","aiviral","viralai","aistory","aipov","aigrandma","aigrandpa","aifamily",
"aicouple","aibaby","aikids","aianimals","aianimal","aidog","aicat","aivillage","aiinterview",
"omni","omniai","geminiomni","googleflow","googleflowai","flowai",
"grok","grokai","grokvideo","xai","xaigrok",
"gemini","geminiai","googleai","googlegemini","geminivideo",
"chatgpt","openai","openaiart","gpt","gptimage","gptvideo",
"veo","veo3","veo31","veo3ai","veo3video","googleveo","googleveo3",
"klingai","klingvideo","klingaivideo","seedance","seedanceai","seedancevideo",
"sora","soraai","soravideo","runwayai","runwayml","runwayvideo",
"hailuoai","hailuo","minimaxai","minimaxvideo",
"flux","fluxai","fluxvideo","midjourney","midjourneyai",
"lumaai","lumadreammachine","dreammachine","pika","pikaai","pikaart",
"higgsfield","higgsfieldai","heygen","heygenai","hedra","hedraai",
"nanobanana","nanobananaai","recraftai","ideogramai"
]
KEYWORD_TERMS=[
"AI","ИИ","искусственный интеллект","нейросеть","generative AI","GenAI","AI video","AI generated video",
"AI comedy","AI funny","AI generated video","AI slop","AI meme","AI skit","AI absurd","AI viral",
"AI grandma","AI grandpa","AI family","AI couple","AI baby","AI animals","AI dog","AI cat","AI village",
"AI interview","AI POV","Omni AI","Gemini Omni","Google Flow Omni","Grok AI","Grok video","xAI Grok",
"Gemini AI video","Google AI video","ChatGPT AI video","OpenAI video","GPT Image video",
"Veo 3 funny","Veo 3 comedy","Veo 3.1","Google Veo","Google Flow AI",
"Kling AI funny","Kling AI video","Seedance funny","Seedance AI","Sora funny","Sora AI video",
"Runway AI","Hailuo AI","Minimax AI","Flux AI","Midjourney AI","Luma AI","Dream Machine AI","Pika AI",
"Higgsfield AI","HeyGen AI","Nano Banana AI",
"нейроюмор","ии юмор","нейровидео","нейросеть прикол","AI бабушка","AI дед","AI деревня","AI животные"
]

_ORIGINAL_SAVE_POST=radar_quality._legacy_save_post
_ORIGINAL_PREPARE=radar_job._prepare_candidates
_ORIGINAL_PROCESS_AI=radar_job._process_one_ai
_ORIGINAL_FINALIZE=radar_job._finalize
_ORIGINAL_SNAPSHOT=radar_job.save_radar_snapshot
_ORIGINAL_FORENSIC_PROMPT=gemini_service.forensic_system_prompt
_ORIGINAL_PRODUCTION_PROMPT=gemini_service.production_system_prompt
_ORIGINAL_AUDIT_PROMPT=gemini_service.audit_system_prompt
_APPLIED=False
_snapshot_calls=0

LANGUAGE_FORENSIC_OVERRIDE="""
LANGUAGE ADAPTATION OVERRIDE — HIGHER PRIORITY THAN ANY EARLIER RUSSIAN-ONLY WORDING.
First detect the actual source language and put it in detected_language.
If speech is already Russian, exact_russian_text is the faithful Russian transcription supported by audio.
If speech is NOT Russian, DO NOT reject or ignore it. Translate/adapt every spoken turn into concise natural colloquial Russian while preserving speaker ownership, intent, joke, emotional tone, turn order and approximate speaking duration. Store that target Russian adaptation in exact_russian_text. Prefer wording with similar spoken length so the Russian lip sync fits the original start/end window. Do not add extra lines or explanations.
If the source has no speech, keep dialogue_turns empty and preserve the visual gag.
""".strip()

LANGUAGE_PRODUCTION_OVERRIDE="""
RUSSIAN LOCALIZATION OVERRIDE — ABSOLUTE.
The final generated video is always localized for a Russian-speaking audience.
If detected_language is not Russian, every source dialogue turn must be translated/adapted into natural spoken Russian. Preserve the same visible speaker, meaning, joke/punchline, emotion, turn order, pause structure and approximate speaking duration. Keep Russian wording compact enough to fit the original lip-sync window; never lengthen the total clip. Do not output the foreign wording in Block 3 or Block 4.
If the source is already Russian, preserve the supported Russian wording instead of paraphrasing it.
Silent visual gags remain silent; never invent dialogue only to make them Russian.
""".strip()

LANGUAGE_AUDIT_OVERRIDE="""
LOCALIZATION QA OVERRIDE.
A non-Russian source is NOT an audit failure. For non-Russian speech, dialogue_text_and_order_ok means the package contains a natural Russian translation/adaptation that preserves source meaning, joke, speaker ownership, order and timing window. Fail the audit if foreign dialogue leaks into the final package, if a speaker is changed, or if the Russian line is so long that it cannot fit the source timing.
""".strip()

def forensic_system_prompt_v7(owned,expected_duration=None): return _ORIGINAL_FORENSIC_PROMPT(owned,expected_duration)+"\n\n"+LANGUAGE_FORENSIC_OVERRIDE
def production_system_prompt_v7(owned,expected_duration=None): return _ORIGINAL_PRODUCTION_PROMPT(owned,expected_duration)+"\n\n"+LANGUAGE_PRODUCTION_OVERRIDE
def audit_system_prompt_v7(expected_duration=None): return _ORIGINAL_AUDIT_PROMPT(expected_duration)+"\n\n"+LANGUAGE_AUDIT_OVERRIDE

def _build_mass_sources():
    sources={
        "popular_ai":{"actor_id":radar_job.APIFY_SEARCH_ACTOR,"input":{"search":SEARCH_QUERY,"searchType":"popular","searchLimit":SEARCH_LIMIT}},
        "ai_hashtags":{"actor_id":radar_job.APIFY_HASHTAG_ACTOR,"input":{"hashtags":HASHTAGS_V7,"resultsType":"reels","resultsLimit":HASHTAG_LIMIT}},
        "ai_keywords":{"actor_id":radar_job.APIFY_HASHTAG_ACTOR,"input":{"hashtags":KEYWORD_TERMS,"keywordSearch":True,"resultsType":"reels","resultsLimit":12}},
    }
    tracked=radar_job._tracked_creators()[:100]
    if tracked: sources["known_ai_creators"]={"actor_id":radar_job.APIFY_CREATOR_ACTOR,"input":{"username":tracked,"resultsLimit":12,"onlyPostsNewerThan":"7 days","skipPinnedPosts":True,"includeTranscript":False,"includeDownloadedVideo":False}}
    for source in sources.values(): source.update(run_id="",status="NOT_STARTED",dataset_id="",status_message="",started_at="")
    return sources

def _is_v7_source_set(job):
    sources=job.get("sources") or {}; popular=sources.get("popular_ai") or {}
    return (popular.get("input") or {}).get("search")==SEARCH_QUERY

def _reset_stale_job_to_v7(job,stage):
    job["profile"]=PROFILE_VERSION; job["phase"]="queued"; job["sources"]=_build_mass_sources(); job["candidates"]=[]; job["stats"]={}; job["result"]={}; job["error"]=""; job["current_ai_index"]=None; job["current_ai_post_url"]=""
    radar_job._persist(job)
    add_radar_log("Старый незавершённый radar job автоматически сброшен: начинаю новый GLOBAL AI v8 brand discovery вместо продолжения старой выборки.",stage=stage,details={"profile":PROFILE_VERSION,"sources":list(job["sources"].keys())})
    return job

def matches_v6(a):
    humor_ok=bool(a.is_comedy_scene or a.one_clear_joke_or_twist or (a.simple_situation and a.strong_first_frame))
    repeatable=bool(a.reproducible_format and (a.simple_situation or a.one_clear_joke_or_twist or a.is_comedy_scene))
    return all([a.is_ai_video,humor_ok,not a.is_tutorial_or_review,not a.is_talking_head,repeatable])

def _hard_duration_reject(measured):
    return RadarAssessment(is_russian=False,is_ai_video=False,is_comedy_scene=False,is_tutorial_or_review=False,is_talking_head=False,simple_situation=False,strong_first_frame=False,one_clear_joke_or_twist=False,characters_count=0,scene_description="",characters=[],joke="",hook="",ending="",reproducible_format=False,reason=f"Фактическая длительность MP4 {measured:.2f} сек не входит в диапазон {RADAR_MIN_DURATION_SEC:.1f}–{RADAR_MAX_DURATION_SEC:.2f} сек")

def classify_radar_video_v6(file_path,caption=""):
    measured=float(measure_video_duration(file_path,fallback=0) or 0)
    if measured<RADAR_MIN_DURATION_SEC or measured>RADAR_MAX_DURATION_SEC: return _hard_duration_reject(measured)
    def run(client,uploaded):
        prompt=f"""Ты high-recall классификатор коротких AI-комедийных/абсурдных Reels для радара повторяемых вирусных механик.
Цель — НЕ искать только русский исходник. Цель — находить сильные AI-механики по всему миру, которые можно пересобрать как реалистичное видео с русским липсингом.
PASS если: само видео AI; есть короткий развлекательный бит/сценка/абсурд/визуальный гэг/реакция/поворот; механику реально повторить; это не tutorial/review и не обычная реальная съёмка.
ЯЗЫК: is_russian честно отражает исходник, но НЕ является причиной REJECT. Любой язык допустим: production потом переводит реплики на русский с сохранением тайминга, спикеров и панчлайна. Безречевые визуальные гэги тоже допустимы.
is_comedy_scene TRUE также для одного AI-персонажа, однокадрового абсурда и короткой странной ситуации. one_clear_joke_or_twist TRUE для панчлайна, нелепого действия, неожиданной реакции или визуального поворота. is_talking_head TRUE только для обычного РЕАЛЬНОГО автора/эксперта; AI-персонаж в камеру внутри гэга НЕ talking head. simple_situation TRUE если механику можно пересказать в 1–2 предложениях. reproducible_format TRUE если можно заменить персонажа/локацию/реплику, сохранив структуру.
Не отклоняй только потому, что ролик примитивный, кринжовый, дешёвый, без диалога, с одним персонажем или не на русском.
ЖЁСТКИЙ REJECT: не-AI реальная съёмка/реальный мем; tutorial/review; продукт/пейзаж без развлекательной механики; музыкальный монтаж без понятного действия; формат невозможно воспроизвести.
Смотри всё видео и слушай аудио. Caption — только вторичный сигнал.\nInstagram caption:\n{caption[:2000]}""".strip()
        response=client.models.generate_content(model=gemini_service.RADAR_MODEL,contents=types.Content(parts=[gemini_service.video_part(uploaded,gemini_service.RADAR_VIDEO_FPS),types.Part(text=prompt)]),config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="minimal"),response_mime_type="application/json",response_schema=RadarAssessment))
        return gemini_service.parse_response(response,RadarAssessment)
    return gemini_service.with_uploaded_file(file_path,run)

def top_eligible_v6(row):
    duration=float(row.get("duration_sec") or 0); score=float(row.get("viral_score_v2") or 0); views=int(row.get("views") or 0); likes=int(row.get("likes") or 0); comments=int(row.get("comments") or 0); vph=float(row.get("views_per_hour") or 0)
    if duration<RADAR_MIN_DURATION_SEC or duration>RADAR_MAX_DURATION_SEC or score<8: return False
    return views>=100 or likes>=1 or comments>=1 or vph>=200

def _save_post_and_learn_ai_creator(conn,item,assessment):
    _ORIGINAL_SAVE_POST(conn,item,assessment)
    if not assessment or not assessment.is_ai_video or assessment.is_tutorial_or_review or not item.get("creator"): return
    now=datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO tracked_creators(username,first_seen_at,last_seen_at,best_views_per_hour,matching_reels,followers_count,usual_views,sample_size) VALUES(?,?,?,?,0,?,?,0) ON CONFLICT(username) DO UPDATE SET last_seen_at=excluded.last_seen_at,best_views_per_hour=MAX(tracked_creators.best_views_per_hour,excluded.best_views_per_hour),followers_count=CASE WHEN excluded.followers_count>0 THEN excluded.followers_count ELSE tracked_creators.followers_count END,usual_views=CASE WHEN excluded.usual_views>0 THEN excluded.usual_views ELSE tracked_creators.usual_views END""",(item.get("creator",""),now,now,float(item.get("views_per_hour") or 0),int(item.get("followers_count") or 0),float(item.get("creator_usual_views") or 0)))

def _prepare_candidates_v6(client,job):
    if job.get("profile")!=PROFILE_VERSION and not _is_v7_source_set(job): return _reset_stale_job_to_v7(job,"migration")
    job=_ORIGINAL_PREPARE(client,job); job["profile"]=PROFILE_VERSION; candidates=job.get("candidates") or []
    for item in candidates: item["ai_done"]=False; item["ai_match"]=False; item["ai_attempts"]=0; item["ai_error"]=""; item.pop("assessment",None)
    job.setdefault("stats",{})["ai_total"]=len(candidates); radar_job._persist(job)
    add_radar_log("GLOBAL AI queue prepared: все кандидаты будут перепроверены Gemini; язык исходника не ограничивает PASS.",stage="filter",details={"ai_total":len(candidates),"target_matches":TARGET_MATCHES,"profile":PROFILE_VERSION})
    return job

def _snapshot_throttled():
    global _snapshot_calls
    _snapshot_calls+=1
    if _snapshot_calls==1 or _snapshot_calls%10==0: return _ORIGINAL_SNAPSHOT()
    return True

def _process_one_ai_v6(job):
    if job.get("profile")!=PROFILE_VERSION: return _reset_stale_job_to_v7(job,"migration")
    job=_ORIGINAL_PROCESS_AI(job); candidates=job.get("candidates") or []; done=sum(1 for x in candidates if x.get("ai_done")); matched=sum(1 for x in candidates if x.get("ai_done") and x.get("ai_match"))
    if job.get("phase")=="ai" and done>=MIN_AI_CHECKS_BEFORE_EARLY_STOP and matched>=TARGET_MATCHES:
        job["phase"]="finalizing"; job.setdefault("stats",{})["early_stop_after_ai"]=done; job["stats"]["early_stop_matches"]=matched; radar_job._persist(job)
        add_radar_log(f"Цель достигнута: {matched} подходящих AI-роликов после {done} проверок.",stage="gemini-radar",details={"ai_done":done,"matched":matched,"target":TARGET_MATCHES})
    return job

def _rebuild_checked_rows_from_job(job):
    rebuilt=0
    with db_conn() as conn:
        for item in job.get("candidates") or []:
            payload=item.get("assessment")
            if not(item.get("ai_done") and isinstance(payload,dict)): continue
            try: radar_quality.save_post_preserve_ai(conn,item,RadarAssessment.model_validate(payload)); rebuilt+=1
            except Exception: continue
        conn.commit()
    return rebuilt

def _finalize_v6(job):
    rebuilt=_rebuild_checked_rows_from_job(job); add_radar_log(f"Перед TOP восстановлено {rebuilt} Gemini-вердиктов.",stage="finalizing"); result=_ORIGINAL_FINALIZE(job)
    try: _ORIGINAL_SNAPSHOT()
    except Exception as exc: add_radar_log(f"Финальный snapshot не сохранён: {exc}",level="WARN",stage="snapshot")
    return result

def apply_growth_overrides():
    global _APPLIED
    if _APPLIED: return
    _APPLIED=True
    radar_job.RADAR_AI_ANALYZE_LIMIT=AI_ANALYZE_LIMIT; radar_job.RADAR_KEEP_LIMIT=KEEP_LIMIT; radar_service.RADAR_KEEP_LIMIT=KEEP_LIMIT
    radar_job._build_sources=_build_mass_sources; gemini_service.classify_radar_video=classify_radar_video_v6; radar_job.matches=matches_v6; radar_service.matches=matches_v6
    radar_quality.top_eligible=top_eligible_v6; radar_job.top_eligible=top_eligible_v6; radar_quality._legacy_save_post=_save_post_and_learn_ai_creator
    radar_job._prepare_candidates=_prepare_candidates_v6; radar_job._process_one_ai=_process_one_ai_v6; radar_job._finalize=_finalize_v6; radar_job.save_radar_snapshot=_snapshot_throttled
    gemini_service.forensic_system_prompt=forensic_system_prompt_v7; gemini_service.production_system_prompt=production_system_prompt_v7; gemini_service.audit_system_prompt=audit_system_prompt_v7
    add_radar_log("GLOBAL AI v8 включён: международный AI discovery + крупнейшие AI-бренды/модели, нерусские ролики допустимы, production всегда локализует речь на русский, до 420 Gemini-проверок.",stage="startup",details={"profile":PROFILE_VERSION,"duration_min":RADAR_MIN_DURATION_SEC,"duration_max":RADAR_MAX_DURATION_SEC,"search_terms":SEARCH_QUERY.count(",")+1,"hashtags":len(HASHTAGS_V7),"keyword_terms":len(KEYWORD_TERMS),"search_limit_per_term":SEARCH_LIMIT,"hashtag_limit_per_tag":HASHTAG_LIMIT,"ai_analyze_limit":AI_ANALYZE_LIMIT,"keep_limit":KEEP_LIMIT,"target_matches":TARGET_MATCHES})