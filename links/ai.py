import os
import json
import logging
from openai import OpenAI

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