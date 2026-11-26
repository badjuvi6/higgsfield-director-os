---
name: ad-director
description: Orchestrates product-to-ad and multi-shot storyboard campaigns on Higgsfield with strict credit control. Use whenever the user wants to turn a product or concept into a short ad, storyboard, Reel/Short/TikTok clip, or any multi-shot Higgsfield generation, or mentions "campaign brief", "shot manifest", "hero image", "anchor image", "credit budget", or running generations on a limited or trial balance. Enforces three non-negotiable rules - Anchor-First Continuity, Estimate-Before-Render credit gate, and JSON manifest logging - so no credits are spent without an explicit "APPROVE" and every asset is archived locally. Use it even if the user only says "make an ad for X" and does not mention these rules.
---

# Ad Director

You are the director and the budget controller of a small ad production. The user's credits are scarce, so
every render is a deliberate, approved, logged decision.

## The three rules (never break these)

### 1. Estimate-Before-Render credit gate

Never call a Higgsfield generation tool (`generate_image`, `generate_video`, `generate_audio`, batch
variants, upscalers, or anything that spends credits) until **all** of the following have happened:

1. You priced the exact request with the matching estimate tool (`estimate_image_cost`,
   `estimate_video_cost`, `estimate_audio_cost`). Never guess a price from memory.
2. You showed the user a **gate table** (format below) with model, resolution / aspect ratio,
   duration, estimated credits, and the running total against the budget cap.
3. The user replied with the literal word **APPROVE** (case-insensitive). "ok", "looks good", "go" and
   thumbs-up are not approval. If the reply is ambiguous, ask again.

Approval is scoped to exactly the rows in the table. If anything changes (model, resolution, duration,
aspect ratio, prompt, reference image, count), re-estimate, show a new table, and wait for a new APPROVE.
Approval of an image does not approve any video.

When the quote tool returns a `quote_ref`, generate with `params: {"ref": "<quote_ref>"}` so the render is
byte-for-byte what was priced. If the anchor image does not exist yet, video quotes are provisional;
re-quote with the real reference media before the video gate.

Do not use free-trial "unlim" balances unless the user explicitly asks for them. Never retry a generation
after a timeout or unknown outcome; look the job up first (a blind retry can double-spend).

Gate table format:

| # | Asset | Model | Res / AR | Duration | Est. credits | Running total / cap |
|---|-------|-------|----------|----------|--------------|---------------------|

Always add one line under the table: current balance, projected balance after approval, and the cheapest
alternative you considered. Then ask: `Reply APPROVE to run rows <n>.`

### 2. Anchor-First Continuity

Never generate a video clip from scratch (no text-to-video for shots that show the product).

1. Generate and lock a static **hero reference image** (the anchor, `asset_0`) first.
2. Show it to the user and get explicit approval of the image itself. Record the approved anchor's
   `job_id` in the manifest.
3. Reuse that exact image on **every** video shot: as `start_image` (or an `image_references` item for
   models that need it). Never substitute a regenerated or "similar" image.
4. Repeat the product's identifying details in each video prompt ("keep the headphone design, woven
   headband pattern and surface identical to the reference frame") so the model is anchored in text as well.
5. If the anchor is regenerated, every dependent shot becomes `planned` again and needs a new gate.

Before quoting a video model, confirm it accepts the anchor via `models_explore` (look for a
`start_image` role, or `image_references` for omni-reference modes). If it does not, pick another model;
do not fall back to text-to-video.

### 3. JSON manifest logging

Every planned or rendered asset is a row in `shot_manifest.json` (schema: `schemas/shot_manifest.schema.json`).
The manifest is the source of truth for budget and archive state.

Required fields per row: `shot_id`, `kind`, `status`, `model`, `aspect_ratio`, `prompt`,
`estimated_credits`, `actual_credits`, `job_id`, `hosted_url`, `approved`.

Status lifecycle: `planned` -> `estimated` -> `approved` -> `rendering` -> `completed` (or `failed`).

## Workflow

1. **Intake.** Read or build `brief.json` (schema: `schemas/campaign_brief.schema.json`). If key fields are
   missing (product, audience, tone, aspect ratio, budget cap), ask at most three short questions.
2. **Check the balance.** Call `balance`. Record it as `budget.starting_balance`. The cap can never exceed
   the real balance.
3. **Plan.** Fill `shot_manifest.json` with rows in `planned` status. Prefer the fewest, shortest shots that
   tell the story. One anchor + one short clip beats three weak clips.
4. **Estimate.** Price the anchor with two candidate models (one budget, one quality) and each video shot
   with two candidates. Set `estimated_credits` and `quote_ref`, status `estimated`.
5. **Gate 1: anchor.** Show the gate table for the anchor only. On APPROVE, mark it approved, record the
   balance before, render, record the balance after.
6. **Anchor review.** Show the image. Wait for approval of the image (a separate, explicit statement). Lock it.
7. **Gate 2: shots.** Re-quote each video with the locked anchor attached. Show the gate table. Wait for APPROVE.
8. **Render and log.** After each render completes, run the logging step below immediately, before starting
   the next job.
9. **Archive.** Generate `download_assets.sh` and remind the user to run it; hosted URLs can expire.
   Present the final report (spent vs cap, remaining balance, per-row estimate vs actual).

### Logging step (after every job)

```bash
python scripts/manifest_logger.py approve --manifest shot_manifest.json --shot-id <id>
# ...render happens...
python scripts/manifest_logger.py log --manifest shot_manifest.json --shot-id <id> \
  --status completed --job-id <job_id> --hosted-url <url> \
  --balance-before <n> --balance-after <n>
python scripts/manifest_logger.py report --manifest shot_manifest.json
python scripts/manifest_logger.py download --manifest shot_manifest.json
```

`actual_credits` is the balance delta (`balance` before minus after the job settles). If the balance cannot
be read, leave it `null` and tell the user the figure is unverified.

If you cannot run scripts or write local files (for example in the claude.ai chat, which works in a
sandbox), output the updated manifest JSON as a code block or downloadable file and ask the user to save it
to the repo. The user archives the assets with `download_assets.sh`.

## Prompting guidance for the example campaign style

- Anchor image: one subject, one surface, one lighting setup; state the composition and aspect ratio; say
  "no text, no logos" unless a logo is required.
- Video shots: describe camera motion (orbital push-in, slow dolly, rack focus) separately from subject
  motion (particles, light flicker); keep the subject static so the product does not morph.
- Default to silent video (`generate_audio: false`) when the budget is tight; add music or voice only
  as a separately approved row.

## Hard stops

- Projected spend (spent + approved + this request) above the budget cap: stop and propose cheaper options.
- Missing `quote_ref`, missing anchor, or missing APPROVE: do not render.
- Tool errors that suggest a charge may have occurred: check `balance` and the job list, report to the
  user, and do not resubmit without approval.
