from django.db import migrations

# Kept as literals: a migration must not change meaning when accounts/roles.py does.
GROUPS = ["Coordinator", "Program admin", "Read only"]


def create_groups(apps, schema_editor):
    """
    Create the three staff groups.

    Their permissions are not set here. Permissions don't exist yet while
    migrations run, so accounts.roles.sync_role_permissions applies them
    after every migrate (see accounts/apps.py).
    """
    Group = apps.get_model("auth", "Group")
    for name in GROUPS:
        Group.objects.get_or_create(name=name)


def remove_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=GROUPS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_groups, remove_groups)]
