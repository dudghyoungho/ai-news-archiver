# links/utils.py

from collections import Counter
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from django.utils import timezone

# 1. 키워드 -> 분야 매핑 (이 부분만 조금 신경 써서 채워주면 됩니다)
CATEGORY_KEYWORDS = {
    'TECH': ['AI', '반도체', '애플', '삼성', 'IT', '개발', '코딩', '소프트웨어', '테크', '모바일', '게임', '과학'],
    'ECONOMY': ['주식', '투자', '금리', '부동산', '시장', '환율', '은행', '경제', '재테크', '코스피', '나스닥'],
    'POLITICS': ['대통령', '국회', '선거', '정당', '법안', '정책', '외교', '북한', '총선', '의원'],
    'SOCIETY': ['사건', '사고', '날씨', '교통', '교육', '환경', '복지', '노동', '인권'],
    'CULTURE': ['영화', '드라마', '여행', '음식', '책', '예술', '공연', '연예', '스포츠'],
    'GENERAL': []
}

# 2. 분야별 페르소나(칭호) 정의
PERSONA_TITLES = {
    'TECH': {
        'lv.1': '💾 IT 꿈나무',
        'lv.2': '💻 판교의 등대',
        'lv.3': '🤖 미래에서 온 터미네이터'
    },
    'ECONOMY': {
        'lv.1': '🪙 저금통 요정',
        'lv.2': '📈 차트 분석가',
        'lv.3': '🐺 여의도의 늑대'
    },
    'POLITICS': {
        'lv.1': '📰 조간신문 독자',
        'lv.2': '⚖️ 여의도 평론가',
        'lv.3': '👑 킹메이커'
    },
    'SOCIETY': {
        'lv.1': '👀 이웃집 관찰자',
        'lv.2': '📢 사회부 기자',
        'lv.3': '🌍 세상을 바꾸는 활동가'
    },
    'CULTURE': {
        'lv.1': '🍿 팝콘 러버',
        'lv.2': '🎨 힙한 영감 수집가',
        'lv.3': '🍷 고독한 미식가'
    },
    'GENERAL': { # 특정 분야가 두드러지지 않을 때
        'lv.1': '🌱 뉴스 입문자',
        'lv.2': '📚 잡학다식 척척박사',
        'lv.3': '🧠 걸어다니는 백과사전'
    }
}

def determine_persona(completed_links):
    """
    읽은 기사들의 태그를 분석하여 페르소나(칭호, 설명, 이모지)를 반환합니다.
    """
    if not completed_links.exists():
        return {'title': '👻 투명한 유령', 'desc': '아직 읽은 기사가 없어요!'}

    # 1. 태그 수집
    all_tags = []
    for link in completed_links:
        if link.tags:
            all_tags.extend(link.tags)
    
    total_read_count = completed_links.count()
    
    # 2. 분야별 점수 계산
    scores = {key: 0 for key in CATEGORY_KEYWORDS.keys()}
    scores['GENERAL'] = 0 # 매핑 안 된 태그용

    for tag in all_tags:
        matched = False
        for category, keywords in CATEGORY_KEYWORDS.items():
            # 태그가 키워드를 포함하면 해당 카테고리 점수 UP
            if any(k in tag for k in keywords):
                scores[category] += 1
                matched = True
                break
        if not matched:
            scores['GENERAL'] += 0.5 # 기타 태그는 점수를 조금 낮게

    # 3. 1등 분야(Dominant Category) 선정
    # 가장 높은 점수를 가진 카테고리를 찾음
    dominant_category = max(scores, key=scores.get)
    
    # 만약 1등 점수가 너무 낮거나(3점 미만), 전체 비중의 20%도 안 되면 -> GENERAL 처리
    if scores[dominant_category] < 3:
        dominant_category = 'GENERAL'

    # 4. 레벨 산정 (읽은 개수 기준)
    if total_read_count < 10:
        level = 'lv.1'
    elif total_read_count < 50:
        level = 'lv.2'
    else:
        level = 'lv.3'

    # 5. 최종 칭호 반환
    return {
        'title': PERSONA_TITLES[dominant_category][level],
        'category': dominant_category,
        'level': level,
        'read_count': total_read_count
    }

def analyze_knowledge_gap(user):
    """
    유저의 읽은 기사 데이터를 분석하여 강점(Strong)과 약점(Weak) 카테고리를 반환합니다.
    """
    from .models import Link
    from collections import Counter

    # 1. 완료된 기사의 모든 태그 수집
    completed_links = Link.objects.filter(user=user, status='COMPLETED')
    
    if not completed_links.exists():
        # 데이터가 아예 없을 경우 기본값 반환 (테크/경제 위주로 시작 제안)
        return ['TECH'], ['ECONOMY', 'POLITICS']

    all_tags = []
    for link in completed_links:
        if link.tags:
            all_tags.extend(link.tags)

    # 2. 카테고리별 점수 산정 (CATEGORY_KEYWORDS 활용)
    cat_scores = {k: 0 for k in CATEGORY_KEYWORDS.keys() if k != 'GENERAL'}
    
    for tag in all_tags:
        for cat, keywords in CATEGORY_KEYWORDS.items():
            if cat == 'GENERAL': continue
            if any(k in tag for k in keywords):
                cat_scores[cat] += 1
                break

    # 3. 정렬 및 분류
    # 점수가 높은 순서대로 리스트 생성 [(카테고리, 점수), ...]
    sorted_cats = sorted(cat_scores.items(), key=lambda x: x[1], reverse=True)
    
    # 강점: 가장 점수가 높은 1~2개
    strong_interests = [cat for cat, score in sorted_cats if score > 0][:2]
    
    # 약점: 점수가 0이거나 가장 낮은 카테고리
    # (이미 잘 아는 분야는 제외하고 선택)
    weak_interests = [cat for cat, score in sorted_cats[::-1][:2]]

    # 만약 모든 분야가 0점이라면 기본값 할당
    if not strong_interests:
        strong_interests = ['TECH']
        weak_interests = ['ECONOMY', 'POLITICS']

    return strong_interests, weak_interests

def is_within_six_months(date_str):
    """
    네이버 pubDate 문자열을 받아 6개월 이내인지 확인합니다.
    예: 'Wed, 07 Jan 2026 14:10:00 +0900'
    """
    try:
        # 다양한 포맷 대응을 위해 dateutil.parser 사용
        pub_date = date_parser.parse(date_str)
        
        # timezone-aware 처리
        if timezone.is_naive(pub_date):
            pub_date = timezone.make_aware(pub_date)
            
        six_months_ago = timezone.now() - timedelta(days=180)
        return pub_date >= six_months_ago
    except Exception:
        # 파싱 실패 시 보수적으로 False 반환
        return False

# links/utils.py 에 추가

def is_too_similar(new_title, existing_titles, threshold=0.5):
    """
    새 기사 제목과 기존 제목들의 유사도를 비교하여 중복 여부를 판단합니다.
    """
    new_words = set(new_title.split())
    for title in existing_titles:
        existing_words = set(title.split())
        # 자카드 유사도 계산 (교집합 / 합집합)
        intersection = new_words.intersection(existing_words)
        union = new_words.union(existing_words)
        similarity = len(intersection) / len(union) if union else 0
        if similarity > threshold:
            return True
    return False