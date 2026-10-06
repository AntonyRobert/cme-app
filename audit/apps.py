from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "audit"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from .log import record

        def log_sign_in(sender, request, user, **kwargs):
            record("admin.signed_in", user, user=user, request=request)

        user_logged_in.connect(log_sign_in, dispatch_uid="audit.log_sign_in", weak=False)
