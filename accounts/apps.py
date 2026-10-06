from django.apps import AppConfig
from django.db.models.signals import post_migrate


def apply_role_permissions(sender, **kwargs):
    from .roles import sync_role_permissions

    sync_role_permissions()


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        # No sender: this must also run after the apps that come later, once
        # their permissions exist. It is idempotent.
        post_migrate.connect(apply_role_permissions, dispatch_uid="accounts.role_permissions")
