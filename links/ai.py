import os
import json
import logging
from openai import OpenAI

import numpy as np
from django.utils import timezone
from .models import Link, UserProfile

logger = logging.getLogger(__name__)

# 환경 변수에서 키를 가져옴 (없으면 None)
api_key = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

def generate_summary_and_tags(title, content):
    """
    OpenAI gpt-4o-mini 모델을 사용하여 요약 및 태그 생성
    Return:
        {
            "summary": "3줄 요약 텍스트...",
            "tags": ["태그1", "태그2", "태그3"]
        }
    """
    # 키가 없거나 본문이 너무 짧으면 AI 패스
    if not client:
        logger.error("OPENAI_API_KEY not found.")
        return None
    
    if not content or len(content) < 50:
        return None

    try:
        # 1. 프롬프트 정의
        system_prompt = (
            "You are a helpful tech news editor. "
            "Read the provided article and perform the following tasks:\n"
            "1. Summarize the key points in Korean in 3 bullet points.\n"
            "2. Extract 3-5 relevant keywords (tags) for categorization.\n"
            "3. Output must be in valid JSON format with keys: 'summary' (string) and 'tags' (list of strings)."
        )
        
        # 토큰 비용 절감을 위해 본문 앞부분 3000자만 사용 (뉴스 요약엔 충분)
        user_prompt = f"Title: {title}\n\nContent:\n{content[:3000]}"

        # 2. API 호출
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # 가성비 모델
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},  # ★ JSON 모드 강제
            temperature=0.5,
        )

        # 3. 결과 파싱
        raw_json = response.choices[0].message.content
        data = json.loads(raw_json)
        
        return {
            "summary": data.get("summary", ""),
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
        # 텍스트가 너무 길면 에러가 날 수 있으므로 안전하게 자름
        text = text[:8000]
        response = client.embeddings.create(
            input=text,
            model="text-embedding-3-small"
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Embedding Error: {e}")
        return None
    

def update_user_interest_profile(user_id):
    """
    사용자가 읽은 최근 기사들의 벡터를 시간 가중치(Time-Decay)를 적용하여 평균을 냅니다.
    이 '가중 평균 벡터'가 곧 사용자의 현재 관심사(User Profile)가 됩니다.
    """
    try:
        # 1. 최근 읽은(저장한) 기사 50개만 가져오기 (너무 오래된 건 무시)
        recent_links = Link.objects.filter(
            user_id=user_id,
            embedding__isnull=False
        ).order_by('-created_at')[:50]

        if not recent_links:
            return

        # 2. 데이터 준비
        embeddings = []
        weights = []
        now = timezone.now()

        for link in recent_links:
            # 벡터를 numpy 배열로 변환
            vec = np.array(link.embedding, dtype=np.float32)
            
            # 3. 시간 감쇠(Time-Decay) 가중치 계산
            # 공식: 1 / (1 + 0.1 * 경과일수) -> 하루 지날 때마다 비중이 줄어듦
            days_diff = (now - link.created_at).days
            # 시간 차이가 0일보다 작게 나오는 경우(방금 생성) 0으로 보정
            days_diff = max(0, days_diff)
            
            weight = 1.0 / (1.0 + 0.1 * days_diff)

            embeddings.append(vec)
            weights.append(weight)

        # 4. 가중 평균(Weighted Average) 계산
        # (v1*w1 + v2*w2 + ...) / (w1 + w2 + ...)
        if embeddings:
            embeddings_matrix = np.array(embeddings)
            weights_array = np.array(weights).reshape(-1, 1) # 방송(Broadcasting)을 위해 차원 맞춤

            weighted_sum = np.sum(embeddings_matrix * weights_array, axis=0)
            total_weight = np.sum(weights_array)
            
            final_interest_vector = (weighted_sum / total_weight).tolist()

            # 5. DB 업데이트
            profile, created = UserProfile.objects.get_or_create(user_id=user_id)
            profile.interest_vector = final_interest_vector
            profile.save()
            
            print(f"[User Profiling] Updated profile for user {user_id} based on {len(recent_links)} links.")

    except Exception as e:
        print(f"[User Profiling] Error updating profile: {e}")
        

def get_recommendation_keywords(user_summary_text):
    """
    사용자의 최근 관심사(요약 텍스트 모음)를 바탕으로
    네이버 뉴스 검색에 사용할 키워드 3개를 추출합니다.
    """
    if not client:
        return ["IT", "테크", "AI"] # 기본값

    try:
        system_prompt = (
            "You are a helpful assistant for a news recommendation system. "
            "Based on the user's reading history summary, suggest 3 specific Korean search keywords "
            "to find related new articles on Naver News. "
            "Output must be a JSON object with a single key 'keywords' which is a list of strings."
        )
        
        user_prompt = f"User's recent reading history summaries:\n{user_summary_text}"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        raw_json = response.choices[0].message.content
        data = json.loads(raw_json)
        return data.get("keywords", [])[:3]

    except Exception as e:
        logger.error(f"[get_recommendation_keywords] Error: {e}")
        return ["기술", "경제", "사회"] # 에러 시 기본값