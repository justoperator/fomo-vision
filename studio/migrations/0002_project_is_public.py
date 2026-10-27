from django.db import migrations, models


def ensure_is_public_column(apps, schema_editor):
    project_model = apps.get_model("studio", "Project")
    column_names = {
        column.name
        for column in schema_editor.connection.introspection.get_table_description(
            schema_editor.connection.cursor(),
            project_model._meta.db_table,
        )
    }
    if "is_public" not in column_names:
        field = models.BooleanField(default=False)
        field.set_attributes_from_name("is_public")
        field.model = project_model
        schema_editor.add_field(
            project_model,
            field,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    ensure_is_public_column,
                    reverse_code=migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="project",
                    name="is_public",
                    field=models.BooleanField(default=False),
                ),
            ],
        ),
    ]
