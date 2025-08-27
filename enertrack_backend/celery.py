# enertrack_backend/celery.py
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'enertrack_backend.settings')


app = Celery('enertrack_backend')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
