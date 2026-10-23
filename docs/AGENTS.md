# AGENTS.md — how AI agents should navigate the Tankie docs

You are reading the **Tankie** documentation. This file is the entry point for
AI agents.

## Start here

1. **The hub:** [`README.md`](README.md) (this directory) lists every topic
   page and every sub-README with a one-line description.
2. **The machine-readable map:** [`manifest.json`](manifest.json) — the same
   set as JSON (`id` / `title` / `description` / `links`), so you can
   enumerate and traverse the whole doc set without parsing Markdown.
3. **The home page:** [`../README.md`](../README.md) — the end-user landing
   page (setup + usage).

## Navigation convention (holds on every page)

- Every page has a **top breadcrumb**: `← [Tankie](../README.md) · [Docs index](README.md)`.
- Every page has a **bottom "See also"** footer linking the most related pages.
- Therefore, from **any** page you can reach the home, the docs index, and any
  other topic in at most **one hop**.

## Single-source-of-truth rule

A fact lives in **exactly one** place; everywhere else **links** to it. Do not
copy a fact into a second file — add a link instead. Canonical homes:

| Fact | Canonical home |
|---|---|
| Parts BOM + wiring | [`hardware.md`](hardware.md) |
| Build toolchains + **all** library / Python deps | [`build.md`](build.md) |
| WiFi config storage (EEPROM sector `0x3FB000`) | [`wifi-config.md`](wifi-config.md) |
| Firmware + LittleFS upload | [`ota.md`](ota.md) |
| NDJSON serial contract + Pi bridge | [`serial-protocol.md`](serial-protocol.md) |
| Test suite + CI jobs | [`testing.md`](testing.md) |
| AI usage / safety / config | [`ai-control.md`](ai-control.md) |
| Raspberry Pi streaming / watchdog / Pi-side flashing | [`../raspberry_pi/README.md`](../raspberry_pi/README.md) |
| AI control code (LocalAI / LocalAGI) | [`../ai-control/README.md`](../ai-control/README.md) |
| Brain tests | [`../tests/brain/README.md`](../tests/brain/README.md) |
| Manual / hardware tests | [`../tests/manual/README.md`](../tests/manual/README.md) |
| VLM benchmark | [`../tools/vlm-benchmark/README.md`](../tools/vlm-benchmark/README.md) |

## Conventions

- The **main README** (`../README.md`) is for end users: **no issue numbers or
  issue links** there. Issue provenance belongs in the `docs/*.md` topic pages.
- Links are relative (no `https://github.com/...` for in-repo files).
- Keep the steer sign convention consistent: `steer > 0` = RIGHT, `steer < 0`
  = LEFT (see [`serial-protocol.md`](serial-protocol.md)).
