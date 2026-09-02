"""rotate_gmail_tokens — re-encrypt every stored Gmail refresh token under
the newest `GMAIL_LIVE_TOKEN_KEY`.

WHY THIS EXISTS (`audit-security.md` finding 9). The refresh token is
encrypted with a single static Fernet key and there was no rotation path at
all: changing the key made every stored token unreadable, which reads to a
student as "Coverage silently disconnected my Gmail". So the key could not be
rotated, which is the same as saying it could never be retired if it leaked.

THE PROCEDURE, four steps, also in `docs/gmail-live-setup.md`:

  1. Generate a new key: `python -c "from cryptography.fernet import Fernet;
     print(Fernet.generate_key().decode())"`.
  2. Set `GMAIL_LIVE_TOKEN_KEY="<new>,<old>"` — NEW FIRST — on every service
     that talks to Gmail, and restart them. Everything written from now on
     uses the new key; everything already stored still decrypts under the old.
  3. `python manage.py rotate_gmail_tokens`. Idempotent, so run it again if it
     is interrupted.
  4. Once it reports every row re-encrypted, drop the old key:
     `GMAIL_LIVE_TOKEN_KEY="<new>"`. Restart. The old key is now retired.

STEP 4 IS THE ONE THAT CAN HURT, and only if it is taken early: a row that has
not been re-encrypted becomes unreadable the moment the old key leaves the
list. That is why step 3 prints the count and why `--check` exists — run it
before step 4 and it will tell you whether anything is still on an older key
without writing anything at all.

    python manage.py rotate_gmail_tokens [--check]

WRITES ONE COLUMN. `refresh_token_encrypted`, and nothing else — no
`updated_at` touch, no status change, no Google call. A rotation must not look
like a reconnect to any other part of the system.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from capture import gmail_live
from capture.models import GmailConnection


class Command(BaseCommand):
    help = "Re-encrypt stored Gmail refresh tokens under the newest key."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Report what would be re-encrypted and write nothing. Run "
                 "this before dropping the old key from the list.",
        )

    def handle(self, *args, **opts):
        keys = gmail_live.token_keys()
        if not keys:
            self.stdout.write(
                "GMAIL_LIVE_TOKEN_KEY is empty — nothing to rotate.")
            return
        self.stdout.write(
            f"{len(keys)} key(s) configured; encrypting under the first."
        )
        if len(keys) == 1:
            self.stdout.write(
                "Only one key is set, so this is a no-op re-encrypt rather "
                "than a rotation. Put the NEW key first and the OLD key "
                "second before running it for real."
            )

        # `all_objects` with an explicit predicate-free read, and it has to be:
        # a rotation is a deployment-wide operation over every tenant's row,
        # run from a command line with no request and no user. This is the
        # shape the tenancy ratchet exists to make somebody look at, and this
        # comment is that look.
        rows = list(
            GmailConnection.all_objects.order_by("pk")
            .values_list("pk", "refresh_token_encrypted")
        )
        if not rows:
            self.stdout.write("0 connections stored — nothing to rotate.")
            return

        rotated, already, failed = 0, 0, 0
        for pk, ciphertext in rows:
            if not ciphertext:
                continue
            try:
                fresh = gmail_live.rotate_token(ciphertext)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                # One unreadable row must not stop the others: a token
                # encrypted under a key nobody has any more is already lost,
                # and the answer is a reconnect, not an aborted rotation.
                failed += 1
                self.stderr.write(
                    f"connection {pk}: could not re-encrypt ({exc}). That row "
                    f"needs a reconnect; the rest are unaffected."
                )
                continue
            if fresh == ciphertext:
                # MultiFernet.rotate always produces new bytes (a fresh
                # timestamp and IV), so this branch is unreachable in
                # practice. Kept because "nothing changed" must never be
                # silently counted as "rotated".
                already += 1
                continue
            if not opts["check"]:
                GmailConnection.all_objects.filter(pk=pk).update(
                    refresh_token_encrypted=fresh)
            rotated += 1

        verb = "would re-encrypt" if opts["check"] else "re-encrypted"
        self.stdout.write(
            f"{verb} {rotated} of {len(rows)} connection(s); "
            f"{already} unchanged, {failed} unreadable."
        )
        if not opts["check"] and not failed:
            self.stdout.write(
                "Safe to drop the old key from GMAIL_LIVE_TOKEN_KEY once "
                "every service has restarted on the new list."
            )
