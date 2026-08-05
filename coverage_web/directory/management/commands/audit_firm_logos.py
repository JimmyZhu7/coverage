"""audit_firm_logos — is every stored mark the same size, and centred?

    python manage.py audit_firm_logos

A board of logos only looks deliberate if they carry the SAME visual weight.
One firm's mark filling its tile while its neighbour's floats at a quarter
scale reads as broken, and that is exactly what shipped: `to_png` used
`thumbnail`, which never upscales, so a 32px favicon sat at 32px in the middle
of a 128px canvas. Macquarie, RBC, Raymond James and TPG all rendered as a
speck, and the owner spotted it.

The fix belongs in `to_png`. THIS exists so the claim "they all match now" is
something the machine checks rather than something I say.

Reported per logo:
  fill    the mark's own bounding box as a fraction of the canvas's long edge.
          1.00 means it touches the edges. `to_png` scales to fill, so
          anything well under 1.0 means the stored file predates the fix or
          the source had baked-in padding trimming could not reach (a white
          border on a non-transparent image, say).
  offset  how far the mark's centre sits from the canvas centre, in pixels.
          Non-zero on one axis is normal and correct for a non-square mark;
          large on both means it is genuinely off-centre.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from PIL import Image

from directory.models import Firm

# Below this the mark is visibly lighter than its neighbours on the board.
MIN_FILL = 0.92
# A square canvas centres to within a rounding pixel; more is a real drift.
MAX_OFFSET = 1.5


class Command(BaseCommand):
    help = "Check every stored firm logo fills its canvas and sits centred."

    def add_arguments(self, parser):
        parser.add_argument("--verbose-all", action="store_true",
                            help="List every logo, not just the problems.")

    def handle(self, *args, **opts):
        rows = []
        for firm in Firm.objects.exclude(logo="").exclude(logo=None).order_by("name"):
            try:
                img = Image.open(firm.logo.path).convert("RGBA")
            except (FileNotFoundError, OSError):
                rows.append((firm, None, None, None, "FILE MISSING"))
                continue

            w, h = img.size
            bbox = img.getchannel("A").getbbox()
            if bbox is None:                       # fully transparent
                rows.append((firm, (w, h), 0.0, (0.0, 0.0), "EMPTY"))
                continue

            left, top, right, bottom = bbox
            fill = max(right - left, bottom - top) / max(w, h)
            off = (abs((left + right) / 2 - w / 2), abs((top + bottom) / 2 - h / 2))

            problems = []
            if (w, h) != (128, 128):
                problems.append(f"not 128x128 ({w}x{h})")
            if fill < MIN_FILL:
                problems.append(f"fills only {fill:.0%}")
            if max(off) > MAX_OFFSET:
                problems.append(f"off-centre by {max(off):.1f}px")
            rows.append((firm, (w, h), fill, off, ", ".join(problems)))

        bad = [r for r in rows if r[4]]
        for firm, size, fill, off, problem in rows:
            if problem or opts["verbose_all"]:
                fillstr = f"{fill:.0%}" if fill is not None else "  - "
                offstr = f"{off[0]:.0f},{off[1]:.0f}" if off else "  -"
                mark = "⚠" if problem else " "
                self.stdout.write(
                    f" {mark} {firm.name[:28]:28} fill {fillstr:>4}  "
                    f"offset {offstr:>7}  {problem}")

        if bad:
            self.stdout.write(self.style.WARNING(
                f"\n{len(bad)} of {len(rows)} need attention — re-run "
                f"`fetch_firm_logos --force` after fixing `to_png`."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\nAll {len(rows)} logos are 128x128, fill their canvas, and sit centred."))
