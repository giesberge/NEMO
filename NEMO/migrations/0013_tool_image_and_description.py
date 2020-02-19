# Generated by Django 2.2.10 on 2020-02-17 16:59

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('NEMO', '0012_version_2_2_0'),
    ]

    operations = [
        migrations.AddField(
            model_name='tool',
            name='_description',
            field=models.TextField(blank=True, db_column='description', default='', help_text='HTML syntax could be used', null=True),
        ),
        migrations.AddField(
            model_name='tool',
            name='_image',
            field=models.ImageField(blank=True, db_column='image', help_text='An image that represent the tool. Maximum width and height are 500px', upload_to='tool_image'),
        ),
    ]
