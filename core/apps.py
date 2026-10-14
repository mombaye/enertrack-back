from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Synchronisation automatique site → scope / contrats / factures (core/site_sync.py).
        from django.db.models.signals import post_save, pre_save

        from .models import Site
        from .site_sync import on_site_post_save, on_site_pre_save

        pre_save.connect(on_site_pre_save, sender=Site, dispatch_uid="core_site_scope")
        post_save.connect(on_site_post_save, sender=Site, dispatch_uid="core_site_contracts")
