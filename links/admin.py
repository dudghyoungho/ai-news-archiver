# links/admin.py
from django.contrib import admin
from .models import Link, UserProfile

@admin.register(Link)
class LinkAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'url', 'status','created_at')

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'last_updated')
    readonly_fields = ('interest_vector',) # 벡터값은 수동 수정 못하게 읽기 전용으로