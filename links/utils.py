# links/utils.py

from collections import Counter

# 1. 키워드 -> 분야 매핑 (이 부분만 조금 신경 써서 채워주면 됩니다)
CATEGORY_KEYWORDS = {
    'TECH': ['AI', '반도체', '애플', '삼성', 'IT', '개발', '코딩', '소프트웨어', '테크', '모바일', '게임', '과학'],
    'ECONOMY': ['주식', '투자', '금리', '부동산', '시장', '환율', '은행', '경제', '재테크', '코스피', '나스닥'],
    'POLITICS': ['대통령', '국회', '선거', '정당', '법안', '정책', '외교', '북한', '총선', '의원'],
    'SOCIETY': ['사건', '사고', '날씨', '교통', '교육', '환경', '복지', '노동', '인권'],
    'CULTURE': ['영화', '드라마', '여행', '음식', '책', '예술', '공연', '연예', '스포츠']
}

# 2. 분야별 페르소나(칭호) 정의
PERSONA_TITLES = {
    'TECH': {
        'lvl1': '💾 IT 꿈나무',
        'lvl2': '💻 판교의 등대',
        'lvl3': '🤖 미래에서 온 터미네이터'
    },
    'ECONOMY': {
        'lvl1': '🪙 저금통 요정',
        'lvl2': '📈 차트 분석가',
        'lvl3': '🐺 여의도의 늑대'
    },
    'POLITICS': {
        'lvl1': '📰 조간신문 독자',
        'lvl2': '⚖️ 여의도 평론가',
        'lvl3': '👑 킹메이커'
    },
    'SOCIETY': {
        'lvl1': '👀 이웃집 관찰자',
        'lvl2': '📢 사회부 기자',
        'lvl3': '🌍 세상을 바꾸는 활동가'
    },
    'CULTURE': {
        'lvl1': '🍿 팝콘 러버',
        'lvl2': '🎨 힙한 영감 수집가',
        'lvl3': '🍷 고독한 미식가'
    },
    'GENERAL': { # 특정 분야가 두드러지지 않을 때
        'lvl1': '🌱 뉴스 입문자',
        'lvl2': '📚 잡학다식 척척박사',
        'lvl3': '🧠 걸어다니는 백과사전'
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
        level = 'lvl1'
    elif total_read_count < 50:
        level = 'lvl2'
    else:
        level = 'lvl3'

    # 5. 최종 칭호 반환
    return {
        'title': PERSONA_TITLES[dominant_category][level],
        'category': dominant_category,
        'level': level,
        'read_count': total_read_count
    }