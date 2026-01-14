import os
import json
import logging
from openai import OpenAI

import numpy as np
from django.utils import timezone
from .models import Link, UserProfile

logger = logging.getLogger(__name__)

api_key = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

def generate_summary_and_tags(title, content):
    """
    OpenAI gpt-4o-mini 모델을 사용하여 요약 및 태그 생성
    Return:
        {
            "summary": "• 첫 번째 요약...\n\n• 두 번째 요약...\n\n• 세 번째 요약...",
            "tags": ["태그1", "태그2", "태그3"]
        }
    """
    if not client:
        logger.error("OPENAI_API_KEY not found.")
        return None
    
    if not content or len(content) < 50:
        return None

    try:
        system_prompt = (
            "You are a helpful tech news editor. "
            "Read the provided article and perform the following tasks:\n"
            "1. Summarize the key points in Korean.\n"
            "2. Extract 3-5 relevant keywords (tags).\n"
            "3. Output must be in valid JSON format."
            "4. [IMPORTANT] The key 'summary' must be a JSON Array of 3 strings. "
            "Do not include numbering or bullets inside the strings."
        )
        
        user_prompt = f"Title: {title}\n\nContent:\n{content[:3000]}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.5,
        )

        raw_json = response.choices[0].message.content
        data = json.loads(raw_json)
        
        raw_summary = data.get("summary", "")
        formatted_summary = ""

        if isinstance(raw_summary, list):
            formatted_summary = "• " + "\n\n• ".join(raw_summary)
        elif isinstance(raw_summary, str):
            formatted_summary = raw_summary.strip()

        return {
            "summary": formatted_summary,
            "tags": data.get("tags", [])
        }

    except Exception as e:
        logger.error(f"OpenAI API Error: {e}")
        return None

    
def get_embedding(text):
    """
    텍스트를 입력받아 1536차원의 벡터 리스트를 반환
    """
    if not client:
        return None
        
    try:
        text = text[:8000]
        response = client.embeddings.create(
            input=text,
            model="text-embedding-3-small"
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Embedding Error: {e}")
        return None
    

def get_embeddings_batch(text_list):
    """
    여러 개의 텍스트를 한 번의 API 호출로 벡터화합니다. (비용/시간 절감 핵심)
    """
    if not client or not text_list:
        return []
    
    sanitized_list = [t[:8000] for t in text_list]

    try:
        response = client.embeddings.create(
            input=sanitized_list,
            model="text-embedding-3-small"
        )
        return [item.embedding for item in response.data]
    except Exception as e:
        logger.error(f"Batch Embedding Error: {e}")
        return [None] * len(text_list)

def update_user_interest_profile(user_id):
    """
    사용자가 읽은 최근 기사들의 벡터를 시간 가중치(Time-Decay)를 적용하여 평균을 냅니다.
    이 '가중 평균 벡터'가 곧 사용자의 현재 관심사(User Profile)가 됩니다.
    """
    try:
        recent_links = Link.objects.filter(
            user_id=user_id,
            embedding__isnull=False
        ).order_by('-created_at')[:50]

        if not recent_links:
            return

        embeddings = []
        weights = []
        now = timezone.now()

        for link in recent_links:
            vec = np.array(link.embedding, dtype=np.float32)
            days_diff = (now - link.created_at).days
            days_diff = max(0, days_diff)
            
            weight = 1.0 / (1.0 + 0.1 * days_diff)

            embeddings.append(vec)
            weights.append(weight)

        if embeddings:
            embeddings_matrix = np.array(embeddings)
            weights_array = np.array(weights).reshape(-1, 1)

            weighted_sum = np.sum(embeddings_matrix * weights_array, axis=0)
            total_weight = np.sum(weights_array)
            
            final_interest_vector = (weighted_sum / total_weight).tolist()

            profile, created = UserProfile.objects.get_or_create(user_id=user_id)
            profile.interest_vector = final_interest_vector
            profile.save()
            
            print(f"[User Profiling] Updated profile for user {user_id} based on {len(recent_links)} links.")

    except Exception as e:
        print(f"[User Profiling] Error updating profile: {e}")


def get_recommendation_keywords(short_term_text, long_term_context):
    """
    사용자의 최근 관심사(요약 텍스트 모음)를 바탕으로
    네이버 뉴스 검색에 사용할 키워드 3개를 추출합니다.
    """
    if not client:
        return ["IT", "테크", "AI"]

    try:
        system_prompt = (
            "You are a sophisticated news recommendation curator.\n"
            "INPUT DATA:\n"
            "1. Short-term Interest: Articles read TODAY (Transient trends/Spikes).\n"
            "2. Long-term Interest: User's top tags & Representative articles from history (Core taste).\n\n"
            "TASK:\n"
            "Generate 3 Korean search keywords based on the following strategy to ensure diversity:\n"
            "- Keyword 1: Based on Short-term Interest (Trending now)\n"
            "- Keyword 2: Based on Long-term Interest (Deep dive into core taste)\n"
            "- Keyword 3: A Mix of both OR a new related sub-topic.\n\n"
            "OUTPUT must be a JSON object with a single key 'keywords' which is a list of strings."
        )
        
        user_prompt = (
            f"Short-term (Today):\n{short_term_text}\n\n"
            f"Long-term (History):\n{long_term_context}")

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.7
        )

        raw_json = response.choices[0].message.content
        data = json.loads(raw_json)
        keywords = data.get("keywords", [])[:3]

        while len(keywords) < 3:
            keywords.append("최신 뉴스")
        
        return keywords

    except Exception as e:
        logger.error(f"[get_recommendation_keywords] Error: {e}")
        return ["기술", "경제", "사회"]
    


def analyze_user_interest(representative_articles):
    """
    대표 기사 목록을 받아 사용자의 관심사 패턴을 문장으로 분석합니다.
    """
    if not client or not representative_articles:
        return "데이터가 부족하여 분석할 수 없습니다."

    context_text = "\n".join([f"- {title}" for title in representative_articles])

    try:
        system_prompt = (
            "You are a personal knowledge analyst. "
            "Analyze the user's reading list and provide a specific, insightful description of their current intellectual interests in Korean. "
            "Do not just list the topics. Explain 'Why' they might be interested or 'How' these topics connect. "
            "Use a polite and professional tone (해요체). Keep it under 200 characters."
        )
        
        user_prompt = f"User's Core Reading List:\n{context_text}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )

        return response.choices[0].message.content

    except Exception as e:
        logger.error(f"[Analysis Error] {e}")
        return "분석 중 오류가 발생했습니다."
    

def get_exploration_keywords(strong_cats, weak_cats):
    """
    사용자의 강점과 약점을 바탕으로 탐험적 학습을 위한 키워드를 생성합니다.
    """
    if not client: return []

    # 프롬프트 구성
    prompt = f"""
    당신은 사용자의 지식 스펙트럼을 넓혀주는 '지식 큐레이터'입니다.
    
    [사용자 정보]
    - 잘 아는 분야 (Strong): {', '.join(strong_cats)}
    - 생소한 분야 (Weak): {', '.join(weak_cats)}
    
    [생성 가이드라인]
    1. '브릿지 키워드': Strong 분야의 관점에서 Weak 분야를 탐구할 수 있는 융합형 주제 2개를 만드세요.
    2. '와일드카드 키워드': Strong과 상관없이 Weak 분야에서 근본적이고 심도 있는 지식을 다루는 주제 1개를 만드세요.
    3. 모든 키워드는 '최근 6개월 이내의 분석 리포트나 심층 기사'가 검색될 수 있도록 구체적이어야 합니다.
    4. "이유", "전망", "분석", "원리", "영향"과 같은 단어를 적절히 섞어 정보 밀도가 높은 결과를 유도하세요.

    [출력 형식]
    - 오직 키워드만 쉼표(,)로 구분하여 한 줄로 출력하세요. (예: 키워드1, 키워드2, 키워드3)
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a professional knowledge curator assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
        )
        content = response.choices[0].message.content.strip()
        keywords = [k.strip() for k in content.split(',') if k.strip()]
        return keywords
    except Exception as e:
        print(f"GPT Keyword Generation Error: {e}")
        return []