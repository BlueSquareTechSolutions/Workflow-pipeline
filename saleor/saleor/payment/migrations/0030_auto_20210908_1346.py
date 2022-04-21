# Generated by Django 3.2.5 on 2021-09-08 13:46

import django.contrib.postgres.indexes
from django.db import migrations, models

import saleor.core.utils.json_serializer


class Migration(migrations.Migration):

    dependencies = [
        ("payment", "0029_alter_payment_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                encoder=saleor.core.utils.json_serializer.CustomJsonEncoder,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="private_metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                encoder=saleor.core.utils.json_serializer.CustomJsonEncoder,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="store_payment_method",
            field=models.CharField(
                choices=[
                    ("on_session", "On session"),
                    ("off_session", "Off session"),
                    ("none", "None"),
                ],
                default="none",
                max_length=11,
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["private_metadata"], name="payment_p_meta_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["metadata"], name="payment_meta_idx"
            ),
        ),
    ]
