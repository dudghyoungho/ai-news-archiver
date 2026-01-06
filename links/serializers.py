from rest_framework import serializers
from .models import Link

class LinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Link
        fields = [
            'id', 'url', 'title', 'content', 'summary', 'image_url', 
            'publisher', 'published_at', 'status', 'failed_reason', 
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'title', 'content', 'summary', 'image_url', 
            'publisher', 'published_at', 'status', 'failed_reason', 
            'created_at', 'updated_at'
        ]
        # 사용자(User)가 입력하는 건 오직 'url' 뿐입니다. 
        # 나머지는 크롤러가 채우거나 시스템이 관리합니다.