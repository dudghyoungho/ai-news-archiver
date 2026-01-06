from django.db import models
from django.contrib.auth.models import User
from pgvector.django import VectorField

class Link(models.Model):
    STATUS_CHOICES = [
        ('PENDING', '대기중'),
        ('PROCESSING', '처리중'),
        ('COMPLETED', '완료'),
        ('FAILED', '실패'),
        ('PARTIAL', '일부완료'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    url = models.URLField(max_length=500)
    naver_oid = models.CharField(max_length=10, blank=True, null=True) 
    naver_aid = models.CharField(max_length=20, blank=True, null=True)

    title = models.CharField(max_length=200, blank=True)
    content = models.TextField(blank=True)       # 본문 (정제된 텍스트)
    summary = models.TextField(blank=True)       # AI 3줄 요약
    # Postgres의 JSONField + Gin Index 활용 예정
    tags = models.JSONField(default=list, blank=True) 

    # 추천 시스템을 위한 벡터 필드 (1536차원)
    embedding = VectorField(dimensions=1536, null=True, blank=True)


    # 3. 메타 데이터 (분석용)
    publisher = models.CharField(max_length=50, blank=True) # 언론사 (예: 연합뉴스)
    published_at = models.DateTimeField(null=True, blank=True) # 기사 발행일
    section = models.CharField(max_length=20, blank=True) # 섹션 (IT, 경제 등)
    image_url = models.URLField(max_length=500, blank=True, null=True)

    # 4. 운영 및 에러 핸들링 (Reliability 핵심 ⭐)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    failed_reason = models.TextField(blank=True) # 에러 메시지 저장
    retry_count = models.PositiveSmallIntegerField(default=0) # 재시도 횟수

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # User별로 같은 기사(oid, aid)는 중복 저장 불가 (DB 레벨 방어)
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'naver_oid', 'naver_aid'], 
                name='unique_naver_news_per_user'
            )
        ]

    def __str__(self):
        return f"[{self.publisher}] {self.title}" if self.title else self.url
    

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    
    # 사용자의 평균 관심사 벡터 (기사 벡터와 동일한 1536차원)
    interest_vector = VectorField(dimensions=1536, null=True, blank=True)
    
    # 마지막으로 벡터가 업데이트된 시각 (Time-Decay 계산용)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s Profile"

# (참고) User 생성 시 자동으로 Profile이 생기도록 Signal을 설정하는 것이 좋습니다.
from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    instance.profile.save()