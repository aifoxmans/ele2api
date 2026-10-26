# ElevenLabs protocol integration

This document describes the ElevenLabs browser flow recorded on 2026-10-26 and the model schemas in the loaded public frontend. All examples below are synthetic. Recordings, credentials, account identifiers, personal prompts, media URLs, and challenge responses remain outside this repository.

## Authentication and account isolation

The website origin is https://elevenlabs.io and the observed regional API origin is https://api.us.elevenlabs.io. API calls use the account's current Firebase ID token in the Authorization bearer header. The browser also sends x-generation-actor: User and x-generation-surface: Image Video.

The account's private authorization fields are access_token, refresh_token, token_expires_at (Unix seconds), firebase_api_key, cookie, and user_agent. These fields are hidden from public account responses. Existing browser authorization is collected from the account's Firebase session. A valid new session replaces the saved session; an empty browser result cannot erase an existing valid token.

Firebase refresh uses the normal refresh_token grant at https://securetoken.googleapis.com/v1/token with the application's API key query parameter. Token refresh uses the selected account's proxy. Expiring browser authorization can fall back to this normal refresh flow. A generation request is never replayed to refresh authorization.

Each account has an isolated browser profile, HTTP session, proxy, and upload cache. HTTP sessions disable environment proxy inheritance. Shared preparation and upload limits bound resource use while preserving each task's material order. Only overlapping downloads for the same account, material kind, and exact source share preparation; cached upload receipts also require the same account, kind, and content digest.

Anonymous caller-media downloads use separate sessions without ElevenLabs authorization headers or cookies. Authenticated API and upload traffic continues through the selected account's proxy.

## CAPTCHA, consent, and observed risk controls

Every recorded generation request included hcaptcha_token. The website loads the normal hCaptcha client and executes a generation widget. The service obtains a fresh one-use token through that client. Challenge execution is serialized within the account browser; tokens are neither cached nor reused. A required human challenge is surfaced for completion in the account browser. Missing browser authorization fails before media preparation.

The generation token and authentication headers are excluded from persisted task request audits. The public API does not accept caller-supplied authorization or challenge values as video-generation parameters.

The recording also contains:

- GET /v1/content/terms and the user's normal POST /v1/content/terms acceptance with policy_reminder_accepted.
- POST /v1/content/generations/check-references, returning references_allowed.
- GET /v1/content-generation/models-available?purpose=generation, including regional, workspace-approval, and synthetic-content restrictions.

The service checks current model access before uploading materials. A model missing from that account's directory, region-restricted, synthetic-restricted, or disapproved by its workspace is rejected locally. The service preserves the website's normal validation and challenge flow.

## Material uploads

The observed upload endpoint is:

    POST /v1/assets/internal

The multipart form contains asset (the binary file) and name (the display filename). A successful asset response includes:

    id
    status
    mime_type
    content_url
    thumbnail_url
    file_properties:
      width
      height
      duration_secs
      size_bytes
      has_audio

The upload receipt must identify a finalized asset. If processing is pending, bounded GET /v1/assets/internal/{asset_id} reads confirm the same asset and its finalized metadata before generation. A mismatched asset identifier or unfinished processing fails preparation.

duration_secs is measured in seconds. The task's references use content_asset_id from the upload receipt. Per-file durations, combined durations, and model-specific dimensions are validated against the observed frontend schema. A source URL or an unfinished upload is insufficient to bind a reference.

## Caller parameters and prompt references

Public requests accept standard prompt and image/video/audio URL fields, as well as Seedance content entries with text, image_url, video_url, and audio_url. The service normalizes these into ordered material lists. Direct URLs explicitly labeled as reference images, videos, or audio can supplement those lists without duplicating or reordering existing entries.

Numbered Chinese and English labels, including image1, img2, video1, v2, Audio1, and their Chinese equivalents, bind to the corresponding one-based caller material index. Their upstream representation is structured JSON:

    {"type": "image_reference", "reference": {"content_asset_id": "example-image-asset"}}

The analogous types are video_reference and audio_reference. Plain text uses a text part. display_prompt is a ProseMirror document containing paragraph, text, and prompt-tag nodes. Prompt-tag params.index is zero-based and params.asset contains the same asset reference. User text remains JSON text; the service does not create HTML prompt tags.

The administrator's strict_prompt_media_references setting is snapshotted with the task. Its default is false. In loose mode, labels outside the caller's actual material range remain plain text, including explicit at-sign labels and index zero. Valid labels still bind. Strict mode rejects invalid explicit references. Caller request fields cannot override this policy. A valid declared reference without a matching upload receipt always fails.

## Default Seedance 2.5 reference video

The administrator can enable use_default_reference_video and choose default_reference_video_resolution. The default policy is enabled at 480p.

After collecting explicit fields, Seedance content, and labeled inline media, the normalizer inserts one internal two-second white, silent H.264 reference clip only for Seedance 2.5 tasks with no caller video. The clip matches the target aspect ratio and configured reference resolution. It is uploaded through the same asset API and receives an ordinary content_asset_id.

Tasks snapshot fixed_reference_video, default_reference_video, and prompt_reference_counts. The internal clip is excluded from caller prompt-reference numbering. It does not become the user's Video1. Existing caller videos prevent insertion, including when the caller has already supplied the maximum number.

Seedance 2.5 follows reference-video or start/end-frame aspect ratio. For these tasks, normalized.aspect_ratio retains the requested target and effective_aspect_ratio is auto; the submitted aspect_ratio is auto. The default clip's dimensions preserve the requested target ratio. With caller-provided video or frames, actual output framing follows those materials. Without video or frames, the requested ratio is submitted directly.

## Generation submission and identity

The normal sequence is:

1. Check account-specific model availability.
2. Upload and validate reference assets.
3. POST /v1/content/generations/check-references with model_id and model_parameters.
4. POST /v1/content/generations/price with model_id, model_parameters, and generations_count.
5. Obtain a fresh challenge token through the normal browser widget.
6. Submit exactly once to POST /v1/content/generations.

The generation body contains prompt, display_prompt, model_id, generations_count, model_parameters, and the temporary hcaptcha_token. The following synthetic example illustrates the non-secret structure; challenge handling is performed by the authorized browser:

    {
      "model_id": "bytedance-seedance-v2-us",
      "generations_count": 1,
      "prompt": [
        {"type": "text", "text": "Animate this landscape: "},
        {"type": "image_reference", "reference": {"content_asset_id": "example-image-asset"}}
      ],
      "display_prompt": {
        "type": "doc",
        "content": [{
          "type": "paragraph",
          "content": [
            {"type": "text", "text": "Animate this landscape: "},
            {
              "type": "prompt-tag",
              "attrs": {
                "id": "image-1",
                "type": "image-reference",
                "label": "Image 1",
                "params": {"index": 0, "asset": {"content_asset_id": "example-image-asset"}}
              }
            }
          ]
        }]
      },
      "model_parameters": {
        "aspect_ratio": "16:9",
        "duration_secs": 4,
        "resolution": "480p",
        "generate_audio": true,
        "reference_images": [{"content_asset_id": "example-image-asset"}]
      }
    }

Reference mode uses reference_images, reference_videos, and reference_audios. Explicit image-to-video and first/last-frame modes use start_frame and end_frame. These frame fields cannot be combined with reference arrays. More than two caller images select reference mode. The optional default Seedance 2.5 clip also selects reference mode.

Only observed parameters are sent. Minimax H3 uses enhance_prompt and exposes no generate_audio toggle. Kling 4 exposes neither a generation-audio toggle nor audio reference inputs. Seedance regional US models expose no seed parameter; requests cannot invent one.

A successful response returns a new session id, iterations, and the accepted generations within an iteration. The service binds thread_id to the session id and run_id to the sole generation id. Missing, conflicting, or multiple generation identifiers leave submission uncertain.

Generation POSTs are never automatically retried. Network errors, unreadable responses, HTTP 408/425/429/5xx, and conflicting acceptance information remain unknown. A complete explicit business rejection is distinguished from an uncertain submission. Unknown outcomes cannot trigger duplicate generation.

## Polling, original results, and cost evidence

The service polls:

    GET /v1/content/generations/{generation_id}

Both id and generation_session_id must match the accepted task. The recorded browser also uses GET /v1/content/generations/{generation_id}/status for streaming status. Ordinary detail polling is sufficient for the service.

pending and processing remain in progress. failed states preserve error_message or failure_reason. Only completed tasks yield result URLs. The service selects the original content_url, with download_url as a fallback, and does not substitute input assets, previews, or capped-resolution files. Actual output size and duration come from file_properties.

GET /v1/user/subscription provides character_limit and character_count for account credit availability. These account totals are not used to infer task charges.

POST /v1/content/generations/price returns credits, price_cents, cost_fiat_currency, and promotion. This is a quote and is stored as estimated cost. The matching completed generation's price.credits supplies task-associated final cost evidence. Failed tasks without a clear charged receipt retain unknown consumption. No balance-difference calculation is labeled as an actual task charge.

No daily check-in or reward-claim endpoint was observed in the ElevenLabs recording. The service reports that manual attention is required and does not fabricate a claim request or successful reward receipt.

## Model mappings and evidence

The following mappings come from the captured model directory and loaded frontend schemas. Account availability is checked separately before uploads. Default gateway resolution is 720p unless a resolution-specific alias specifies another tier.

| Public family | Observed upstream model | Images / audio / video | Total limit | Duration |
|---|---|---|---|---|
| sd-2-0, 1080p, 4k | bytedance-seedance-v2-us | 9 / 3 / 3 | 12 | 4-15s |
| sd-2-0-fast | bytedance-seedance-v2-fast | 9 / 3 / 3 | 12 | 4-15s |
| sd-2-0-mini | bytedance-seedance-v2-mini | 9 / 3 / 3 | 12 | 4-15s |
| sd-2-5, 480p, 1080p | bytedance-seedance-v2.5-us | 30 / 10 / 10 | 50 | 4-30s |
| wan-3.0, 480p, 1080p | wan-3.0 | 10 / 5 / 5 | Per-type limits | 2-30s |
| minimax-h3-768p | minimax-h3, 768p tier | 9 / 3 / 3 | 12 | 5-15s |
| kling-4.0, 1080p | kling-4 | 10 / 0 / 5 | Per-type limits | 3-30s |
| vidu-q4, 540p | No observed mapping | Unconfirmed | Unconfirmed | Unconfirmed |

Seedance 2's separate type limits do not permit all 9 images, 3 audio files, and 3 videos together: that combination exceeds its twelve-material total. Audio references require an image or video. Its combined video duration must be at least two seconds; combined video and combined audio duration each have a 15.1-second ceiling. Mini/Fast support 480p and 720p. Standard 2.0 additionally supports 1080p and 4k.

Seedance 2.5 supports 480p, 720p, and 1080p. Its separate reference limits allow fifty materials in total. Combined video duration must be at least two seconds, and combined video and combined audio duration each have a 30.1-second ceiling. The public sd-2-5-480p-preview name is an explicit compatibility alias for the observed 2.5 regional model at 480p; no separate preview identifier was observed.

Minimax H3's frontend exposes 480p, 768p, 2K, and 4K. The public 768p alias requests the actual 768p tier. Each audio/video reference must be at least two seconds and no longer than 15.1 seconds, with separate combined audio/video ceilings of 15.1 seconds. Audio requires a visual reference.

Wan 3.0's frontend exposes 480p, 720p, and 1080p. Separate combined audio and video durations are limited to fifteen seconds. Its static schema contains no 21:9 ratio.

Kling 4 reference videos require a minimum dimension of 700 pixels and an aspect ratio between 0.4 and 2.5. Each video is 3-30.05 seconds and the combined video duration is at most thirty seconds. Its ratios are auto, 16:9, 9:16, 1:1, and 21:9.

## Availability and verification boundaries

The captured account directory marked Seedance 2.0 Mini, Seedance 2.0 Fast, and Kling 4 as region-restricted. Seedance 2.0 US, Seedance 2.5 US, and Minimax H3 were available. Wan 3.0 has a real frontend definition but was absent from the captured account's available-model directory. These conditions can vary by account and region, so the service checks the current directory.

No Vidu Q4 identifier was found in the captured model directory or inspected frontend model registry. Its requested public names remain explicitly unsupported and have no invented upstream capabilities.

The recording contains completed generations for Seedance 2.0 US, Seedance 2.5 US, and Minimax H3, each with five images, two audio references, and one video reference. Offline reconstruction reproduces their typed prompt parts and submitted model parameters. Frontend capability declarations do not establish successful live generation for every advertised parameter combination or for a region-restricted model.