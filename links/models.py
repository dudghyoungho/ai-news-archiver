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
        ('RECOMMENDED', 'AI추천'),
    ]

    RECO_TYPE_CHOICES = [
        ("PERSONAL", "관심사 기반"),
        ("EXPLORE", "탐험"),
    ]

    recommendation_type = models.CharField(
        max_length=20,
        choices=RECO_TYPE_CHOICES,
        blank=True,
        null=True,
        db_index=True,
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    url = models.URLField(max_length=500)
    naver_oid = models.CharField(max_length=10, blank=True, null=True) 
    naver_aid = models.CharField(max_length=20, blank=True, null=True)

    title = models.CharField(max_length=200, blank=True)
    content = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True) 

    embedding = VectorField(dimensions=1536, null=True, blank=True)

    publisher = models.CharField(max_length=50, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    section = models.CharField(max_length=20, blank=True)
    image_url = models.URLField(max_length=500, blank=True, null=True) 

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    failed_reason = models.TextField(blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'naver_oid', 'naver_aid'], 
                name='unique_naver_news_per_user'
            )
        ]

    def __str__(self):
        return f"[{self.publisher}] {self.title}" if self.title else self.url
    
    def save(self, *args, **kwargs):
        if self.status in ['RECOMMENDED', 'COMPLETED'] and "naver.com" not in self.url:
            raise ValueError("AI 요약은 네이버 뉴스 URL만 가능합니다.")
        super().save(*args, **kwargs)
    

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    interest_vector = VectorField(dimensions=1536, null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)
    stats_snapshot = models.JSONField(default=dict, blank=True)
    stats_snapshot_updated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username}'s Profile"

from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    instance.profile.save()