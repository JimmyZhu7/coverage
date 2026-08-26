"""Give every blank-slugged firm a real slug, then make blank impossible.

`Firm.slug` is `unique=True`, which permits exactly ONE blank row — legal, and
wrong in every way that matters. A blank-slugged firm cannot be addressed by
`Firm.objects.get(slug=...)`, drops out of every slug-keyed map the app builds
(`seed_firm_dates`'s `firm_by_slug`, `import_email_patterns`'s `by_slug`), and
sits directly in the path of `seed_directory`'s `update_or_create(slug=...)`,
which would adopt it for whichever YAML row happened to carry a blank `id`.

One existed: id 218, "Citadel Securities", in the founder's database. No code
path in this repo can produce it — `ingest._FirmResolver` uses
`slugify(name) or "firm"`, `scrape.py` uses the board catalog's slug,
`seed_directory` skips a row whose `id` is blank, `seed_mail_domains` uses its
CREATABLE_FIRMS key, and both scripts under `scripts/` slugify the name. Its
shape is exactly `Firm.objects.create(name=..., tracks=..., regions=...)` with
`slug` omitted: Django fills a CharField-derived field with `""` and `status`
takes the model default. It sits in an id band (209-218) of firms present in
neither `boards.py` nor `data/seeds/firms.yaml`, i.e. rows added by hand.

So the fix has two halves, and the second is the one that lasts:

1. Backfill. Derived from the row's own name rather than hardcoded, because a
   migration that names one firm fixes one database. `slugify("Citadel
   Securities")` is `citadel-securities`, which is the slug
   `directory/_mail_domains.py` already declares for it — after this,
   `seed_mail_domains` resolves the row by slug instead of falling back to a
   name match.
2. A CHECK constraint, in Postgres, so the guard also covers `manage.py
   shell`, `dbshell`, and a raw INSERT — the paths the original row came
   through, and the ones a model-level validator would never see.
"""

from django.db import migrations, models
from django.utils.text import slugify


def fill_blank_slugs(apps, schema_editor):
    Firm = apps.get_model("directory", "Firm")
    blanks = list(Firm.objects.filter(slug="").order_by("id"))
    if not blanks:
        return
    taken = set(Firm.objects.exclude(slug="").values_list("slug", flat=True))
    for firm in blanks:
        # `firm-<pk>` only if the name slugifies to nothing at all (a row named
        # in a non-Latin script, say). Unaddressable-but-unique beats blank.
        base = (slugify(firm.name) or f"firm-{firm.pk}")[:128]
        slug, n = base, 2
        while slug in taken:
            suffix = f"-{n}"
            slug = f"{base[: 128 - len(suffix)]}{suffix}"
            n += 1
        taken.add(slug)
        firm.slug = slug
        firm.save(update_fields=["slug"])


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0010_firm_domains_help_text"),
    ]

    operations = [
        # Backfill first: the constraint cannot be added while a blank row is
        # still in the table.
        migrations.RunPython(fill_blank_slugs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="firm",
            constraint=models.CheckConstraint(
                condition=~models.Q(slug=""), name="firm_slug_not_blank"
            ),
        ),
    ]
