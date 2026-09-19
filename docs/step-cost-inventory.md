# What every model-running step costs (L2 M4 / SCRUM-346)

63 model-running steps across 16 agents. Rates from `seed_models.py`; steps from `engine_stages.json`.

**This is a rate table, not a spend table.** sorted by 0.75*input + 0.25*output per 1M tokens -- a RATE, not a spend: the token volumes M4 needs to rank by dollars live only in run telemetry.

| # | agent | step | work | model | $/1M in | $/1M out |
|---|---|---|---|---|---|---|
| 1 | landing-builder-agent | `03-blueprint` | unclassified | `claude-opus-4-8` | 5.00 | 25.00 |
| 2 | landing-builder-agent | `08-craft-verdict` | creative | `claude-opus-4-8` | 5.00 | 25.00 |
| 3 | newsletter-agent | `09-draft-post` | creative | `claude-opus-4-8` | 5.00 | 25.00 |
| 4 | newsletter-agent | `09z-regenerate-post` | unclassified | `claude-opus-4-8` | 5.00 | 25.00 |
| 5 | newsletter-agent | `15c-editor-verdict` | creative | `claude-opus-4-8` | 5.00 | 25.00 |
| 6 | blog-agent | `09-draft-post` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 7 | blog-agent | `09z-regenerate-post` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 8 | branded-shorts-agent | `06-highlights` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 9 | branded-shorts-agent | `08a-plan-graphics` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 10 | campaign-orchestrator | `07-generate-strategy-plan` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 11 | instagram-agent | `00b2-write-client-brief` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 12 | instagram-agent | `00c3-write-design-brief` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 13 | instagram-agent | `00c4-design-template` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 14 | instagram-agent | `00c7-repair-template` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 15 | instagram-agent | `00d2-derive-visual-direction` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 16 | instagram-agent | `04i-propose-angles` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 17 | instagram-agent | `04n-design-concept` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 18 | instagram-agent | `05-write-copy` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 19 | instagram-agent | `05f-author-custom-archetype` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 20 | intel-report-agent | `02-generate-report` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 21 | intel-report-agent | `02a-regenerate-report` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 22 | linkedin-agent | `09-draft-post` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 23 | linkedin-agent | `09z-regenerate-post` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 24 | newsletter-agent | `08b-plan-edition` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 25 | reddit-agent | `04a-plan-channel` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 26 | reputation-agent | `08a-voice-batch-cycle` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 27 | reputation-agent | `tag` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 28 | seo-geo-agent | `02a-draft-prompt-set-agent` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 29 | seo-geo-agent | `13-draft-fixes` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 30 | seo-geo-agent | `14-draft-narrative` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 31 | seo-geo-agent | `14z-regenerate-narrative` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 32 | tiktok-editing-agent | `06-highlights` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 33 | tiktok-editing-agent | `08a-plan-graphics` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 34 | x-agent | `10-draft-post` | creative | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 35 | x-agent | `10z-regenerate-post` | unclassified | `claude-sonnet-4-6` | 3.00 | 15.00 |
| 36 | blog-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 37 | branded-shorts-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 38 | campaign-orchestrator | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 39 | instagram-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 40 | landing-builder-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 41 | linkedin-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 42 | newsletter-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 43 | reddit-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 44 | reputation-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 45 | tiktok-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 46 | tiktok-clipping-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 47 | tiktok-content-design-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 48 | tiktok-editing-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 49 | x-agent | `guardrail-verify` | mechanical | `claude-haiku-4-5-20251001` | 1.00 | 5.00 |
| 50 | instagram-agent | `00c6-review-template-set` | unclassified | `gemini-3.8-flash` | ? | ? |
| 51 | instagram-agent | `04b-research-extract-facts` | mechanical | `gemini-3.8-flash` | ? | ? |
| 52 | instagram-agent | `04b3-extract-entities` | mechanical | `gemini-3.8-flash` | ? | ? |
| 53 | instagram-agent | `06-vet-images` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 54 | instagram-agent | `06h2-vet-floor-images` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 55 | instagram-agent | `08b-visual-qa` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 56 | landing-builder-agent | `04-build` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 57 | landing-builder-agent | `09-fix` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 58 | tiktok-agent | `01d-topic-scout` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 59 | tiktok-agent | `03a-moment` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 60 | tiktok-clipping-agent | `01d-topic-scout` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 61 | tiktok-clipping-agent | `03a-moment` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 62 | tiktok-content-design-agent | `01d-topic-scout` | unclassified | `gemini-3.1-pro-preview` | ? | ? |
| 63 | tiktok-content-design-agent | `03a-moment` | unclassified | `gemini-3.1-pro-preview` | ? | ? |

## 14 step(s) on a model deliberately left unpriced

In `UNPRICED` (`gemini-3.1-pro-preview`, `gemini-3.8-flash`) — somebody looked and recorded that no primary source publishes a rate. That is a defensible choice per model; what this table adds is how much of the fleet now depends on it, which is the number that decides whether it stays defensible.

* instagram-agent · `00c6-review-template-set` → `gemini-3.8-flash`
* instagram-agent · `04b-research-extract-facts` → `gemini-3.8-flash`
* instagram-agent · `04b3-extract-entities` → `gemini-3.8-flash`
* instagram-agent · `06-vet-images` → `gemini-3.1-pro-preview`
* instagram-agent · `06h2-vet-floor-images` → `gemini-3.1-pro-preview`
* instagram-agent · `08b-visual-qa` → `gemini-3.1-pro-preview`
* landing-builder-agent · `04-build` → `gemini-3.1-pro-preview`
* landing-builder-agent · `09-fix` → `gemini-3.1-pro-preview`
* tiktok-agent · `01d-topic-scout` → `gemini-3.1-pro-preview`
* tiktok-agent · `03a-moment` → `gemini-3.1-pro-preview`
* tiktok-clipping-agent · `01d-topic-scout` → `gemini-3.1-pro-preview`
* tiktok-clipping-agent · `03a-moment` → `gemini-3.1-pro-preview`
* tiktok-content-design-agent · `01d-topic-scout` → `gemini-3.1-pro-preview`
* tiktok-content-design-agent · `03a-moment` → `gemini-3.1-pro-preview`

## M6's starting list: mechanical steps, dearest first

Classified from the step's own id (`verify-*`, `classify-*`, ...), which is a **guess**. It is a list to argue with, not a list to act on: a step's model should change because somebody looked at what it does.

| agent | step | model | $/1M in |
|---|---|---|---|
| blog-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| branded-shorts-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| campaign-orchestrator | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| instagram-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| landing-builder-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| linkedin-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| newsletter-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| reddit-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| reputation-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| tiktok-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| tiktok-clipping-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| tiktok-content-design-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| tiktok-editing-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |
| x-agent | `guardrail-verify` | `claude-haiku-4-5-20251001` | 1.00 |

## Steps per model

| model | steps | $/1M in |
|---|---|---|
| `claude-sonnet-4-6` | 30 | 3.00 |
| `claude-haiku-4-5-20251001` | 14 | 1.00 |
| `gemini-3.1-pro-preview` | 11 | ? |
| `claude-opus-4-8` | 5 | 5.00 |
| `gemini-3.8-flash` | 3 | ? |

