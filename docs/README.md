← [Tankie](../README.md) · *Docs index*

# Tankie — Documentation

This is the documentation index (the **hub**). Every topic page below links
back to the [home](../README.md) and to this index, and every page carries a
"See also" footer — so from **any** doc page you can reach the home, this
index, and every other topic in at most one hop. A machine-readable map of the
whole set is in [`manifest.json`](manifest.json); see [`AGENTS.md`](AGENTS.md)
for how AI agents should navigate.

## Topic pages

| Topic | Page | What it covers |
|---|---|---|
| Hardware | [hardware.md](hardware.md) | Parts BOM + hardware setup / wiring |
| Build | [build.md](build.md) | Firmware build (3 toolchains) + **all** library & Python dependencies |
| WiFi configuration | [wifi-config.md](wifi-config.md) | Config portal + where the network config is stored |
| Firmware & data upload | [ota.md](ota.md) | Flashing firmware + LittleFS data (USB & over the air) |
| Serial control protocol | [serial-protocol.md](serial-protocol.md) | Full NDJSON contract (Pi ↔ ESP) + the Pi-side bridge |
| Testing | [testing.md](testing.md) | The test suite + CI jobs |
| AI control | [ai-control.md](ai-control.md) | AI usage, safety & state feedback, configuration |

## Sub-READMEs (source of truth)

These live next to their code and are the canonical home for their topic; the
pages above link to them rather than duplicating them.

| Area | README |
|---|---|
| Raspberry Pi (video streaming, watchdog, Pi-side flashing) | [raspberry_pi/README.md](../raspberry_pi/README.md) |
| AI control code (LocalAI / LocalAGI) | [ai-control/README.md](../ai-control/README.md) |
| Brain test suite | [tests/brain/README.md](../tests/brain/README.md) |
| Manual / hardware-only tests | [tests/manual/README.md](../tests/manual/README.md) |
| VLM benchmark tool | [tools/vlm-benchmark/README.md](../tools/vlm-benchmark/README.md) |

## Cross-linking convention

- **Top breadcrumb** on every page: `← [Tankie](../README.md) · [Docs index](README.md)`.
- **Bottom "See also"** on every page: the 2–3 most related pages.
- **No duplication:** a fact lives in exactly one place; everywhere else links to it.

---

## See also
- [Tankie (home)](../README.md)
- [manifest.json](manifest.json) · [AGENTS.md](AGENTS.md)
