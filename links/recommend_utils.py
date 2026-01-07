from .crawler import parse_naver_ids_and_normalize_url

def normalize_naver_candidate(raw_url):
    """
    네이버 뉴스 URL만 허용하고
    oid/aid 정규화된 URL 반환
    """
    if not raw_url:
        return None
    
    ident = parse_naver_ids_and_normalize_url(raw_url)
    if not ident:
        return None
    
    return {
        "normalized_url": ident.normalized_url,
        "oid": ident.oid,
        "aid": ident.aid,
    }