# Provenance

- **Source repository:** https://github.com/stripe/ai
- **Skill path in source:** `skills/stripe-best-practices/`
- **Publisher:** Stripe (official Stripe Team repo — not a third-party reimplementation)
- **Commit SHA fetched:** `cc67124fa6ed230cda567e598dc9b06e17e23f40`
- **Upstream commit date:** 2026-07-22
- **Date fetched:** 2026-07-23
- **License:** MIT License, Copyright (c) 2024–2025 Stripe. Full text preserved in `LICENSE` in this directory, copied verbatim from the source repo's root `LICENSE` file.

## What was copied

Copied verbatim, byte-for-byte except where noted below:
- `SKILL.md`
- `references/billing.md`
- `references/payments.md`
- `references/security.md`
- `references/connect.md`
- `references/tax.md`
- `references/treasury.md`
- `LICENSE`

No other files from the `stripe/ai` monorepo were taken (the source repo bundles several other skills — `stripe-directory`, `stripe-projects`, `upgrade-stripe`, `stripe-docs`, `connect-recommend` — and per-provider copies of all of these under `providers/{claude,codex,cursor,grok}/plugin/skills/`; none of those were installed).

## Modifications made for this project

1. **Rewrote the `description:` frontmatter** (the only substantive change). The
   original description included the bare word "OAuth" and broad phrasing
   ("building marketplaces", "integrating Stripe"). This project's
   `.claude/skills/` directory also hosts a Gmail/OAuth skill and a
   CRM/lead-scoring skill installed by other agents in parallel, so the
   description was narrowed to stay entirely within Stripe/payments/billing
   vocabulary and to explicitly surface the two patterns this project actually
   needs (one-time purchase activating a time-limited entitlement window;
   per-organization/team seat billing), so the skill triggers reliably for
   this app's billing work without colliding with unrelated skills' triggers.
   The body content of `SKILL.md` (routing table, critical rules, key
   documentation) and all `references/*.md` files are untouched.
2. **Added a 4-line attribution blockquote** immediately after the frontmatter,
   pointing back to the source repo, license, and this PROVENANCE file.
   No other body content was added, removed, or reworded.

Nothing else was changed. No code samples, security guidance, or routing
tables were edited.

## Audit verdict

**Clean.** Every file above was read in full before installation. Checked for:
prompt injection (hidden/embedded instructions, HTML comments, unicode
tricks, base64), credential exfiltration, `curl | bash`-style install
scripts, and instructions to skip confirmation or disable safety checks.
None found. A grep sweep for injection-style phrases (`ignore previous`,
`system prompt`, `you are now`, `send ... api key ... to`, `eval(`,
`base64 -d`, `curl | bash`, etc.) and for zero-width/bidi/BOM Unicode
characters across all installed files returned no matches. The one shell
pipe present (`curl ... | gpg --dearmor | sudo tee ...` in the source's
Stripe CLI apt-install instructions, in a testing-guide file that was
*not* installed here) is the standard GPG-verified apt-repo setup pattern,
not an unverified pipe-to-shell.

Two other candidates were fetched to the scratch directory and audited
alongside this one, then rejected in favor of this skill — both were also
clean (no injection/exfiltration found) but weaker fits for this project:
- `jezweb/claude-skills` (`plugins/integrations/skills/stripe-payments`) —
  working code, but tied to Node.js/Cloudflare Workers/Hono, not Python/Flask.
- `wrsmith108/stripe-mcp-skill` — thin (single skill, 1 GitHub star), and
  built around Stripe's remote MCP server / `@stripe/mcp` npm tool-calling
  rather than in-code implementation, which fits this project's custom
  entitlement-window logic poorly.
