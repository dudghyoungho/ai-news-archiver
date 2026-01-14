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