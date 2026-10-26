# ELE2API

ElevenLabs video task gateway with a Pip-style account pool, proxied browser login, task console, settings, and API documentation. Each account keeps its own browser profile, authenticated session, and proxy. Video requests follow the normal web upload, material validation, submission, and result-query workflow.

## Start

Copy `.env.example` to `.env` and set different, random API, administrator, synchronization, and cost synchronization keys. Create the external Docker network `my-shared-net` if it does not already exist, then pull the published image and start the service:

```sh
docker compose pull ele2api
docker compose up -d --no-build ele2api
```

The console listens on port `8817`. Sign in using `ELE_ADMIN_TOKEN`, add an account with its proxy, complete the normal website login in the embedded browser, and verify the account. Captchas and other interactive verification remain manual. Account sessions and tasks are persisted in `./data`; keep this directory private and backed up.

Administrators may import an existing authorized Firebase session through `POST /api/accounts` using `name`, `proxy_url`, `access_token`, `refresh_token`, `token_expires_at` (Unix seconds), `firebase_api_key`, `cookie`, and `user_agent`, then call `POST /api/accounts/{id}/check`. Credential fields are write-only in the management API. Each account retains an isolated browser for normal website verification and generation CAPTCHA. Normal callers use only the service API key, never website credentials.

An individual account may also provide `login_email` and `login_password` to queue a normal browser login. Batch import accepts one `email|password|optional proxy` per line; tabs and `----` separators are supported. Passwords are stored only in the private account database, never task requests or import results. The independent login queue permits two simultaneous logins and at most 100 pending jobs. CAPTCHA or additional verification is left to the normal website and shown as `verification_required`; use web login to finish it. `POST /api/accounts/{id}/login` retries an account, and `GET /api/accounts/login-results` returns sanitized progress. Interrupted logins become `login_required` after restart and are not replayed automatically.

Password login also completes the website's initial setup through its normal controls: ElevenCreative, the adult confirmation and required terms, creator/video preferences, and Image & Video. The paid-plan offer is skipped; marketing checkboxes are not selected. A Firebase token alone does not make an account ready: the service verifies `GET /v1/user/internal` reports `is_onboarding_completed: true` before enabling it. An unfinished or unrecognized setup stays disabled as `onboarding_required`. Manual web-login completion uses the same setup check. Ordinary background account checks only read readiness and never navigate through setup while a task is running.

When proxy assignment is enabled and an account has no explicit proxy, the gateway chooses the least-used proxy port from `20001–20022` and `20031–20040`, rotating ties. The default base is `socks5://host.docker.internal`; the console can change this base or disable automatic assignment. Explicit account proxies remain unchanged.

## Video API

Use `Authorization: Bearer <ELE_API_KEY>` or `X-API-Key`.

```http
POST /v1/videos
Content-Type: application/json
Idempotency-Key: your-unique-request-id
```

```json
{
  "model": "sd-2-5-480p",
  "prompt": "Use image1 for the landscape, video1 for motion, and audio1 for the sound reference.",
  "duration": 4,
  "resolution": "480p",
  "aspect_ratio": "16:9",
  "image_urls": ["https://example.com/image.png"],
  "video_urls": ["https://example.com/reference.mp4"],
  "audio_urls": ["https://example.com/reference.mp3"]
}
```

The response contains a `ele_...` task ID. Query `GET /v1/videos/{id}` until completion; successful outputs appear in `data[].url`. `GET /v1/videos/{id}/content` redirects to the first result. Standard Seedance `content` entries containing `text`, `image_url`, `video_url`, and `audio_url` are accepted without extra parameter wrappers.

Prompt aliases such as `视频1`, `video2`, `v3`, `视4`, `图片1`, `图1`, `img2`, `Image3`, `Audio1`, and `音频2` become the upstream typed prompt references and display document when the corresponding material exists. References remain one-based within each media type. Out-of-range references remain ordinary text by default. Material limits are enforced before upload, and duration checks use returned upload metadata in seconds.

The console's **严格校验提示词素材编号** setting (`strict_prompt_media_references`) defaults to `false`. Enable it to reject out-of-range material numbers before a new task is queued. The settings API accepts only a JSON boolean for this field. Each task keeps the setting used when accepted; later setting changes do not alter queued tasks or replay an accepted idempotency key.

Direct media URLs explicitly labeled as reference images, videos, or audio in the prompt are included in the corresponding material list before model limits are checked. Repeated links reuse the same material; links already supplied in the material fields are not added again. Matching source URLs in the prompt are bound to the uploaded upstream references. Ordinary webpage links remain text. Prefer the explicit `image_urls`, `video_urls`, and `audio_urls` fields for unambiguous requests.

An idempotency key belongs to one normalized request. Reusing it for a different request is rejected. A generation submission is never retried after an ambiguous network error. `submission_unknown` means the original task requires verification; it must not trigger automatic failover or a second generation. If original task IDs are available, administrators can use `POST /api/tasks/{id}/resume` to resume querying that same task.

## Models

Aliases without a resolution suffix default to **720p**. `GET /v1/models` returns actual resolutions, durations, ratios, material limits, and compatibility notes.

| Family | Public models | Reference limits |
|---|---|---|
| Seedance 2.0 | `sd-2-0`, `sd-2-0-1080p`, `sd-2-0-4k`, `sd-2-0-fast`, `sd-2-0-mini` | Up to 9 images, 3 audio clips, 3 videos; 12 materials total |
| Seedance 2.5 | `sd-2-5`, `sd-2-5-480p`, `sd-2-5-1080p`, `sd-2-5-480p-preview` | 30 images, 10 audio clips, 10 videos |
| Wan 3.0 | `wan-3.0`, `wan-3.0-480p`, `wan-3.0-1080p` | See model capabilities |
| MiniMax H3 | `minimax-h3-768p` | See model capabilities |
| Kling 4.0 | `kling-4.0`, `kling-4.0-1080p` | See model capabilities |
| Vidu Q4 | No captured upstream mapping | Requests are rejected until a supported mapping is observed |

The observed website configuration exposes Seedance 2.5 without a separate preview version; `sd-2-5-480p-preview` is an explicitly marked compatibility alias for its 480p mode. Configuration-derived combinations are distinguished from actual generation acceptance results. `GET /v1/models` is the capability source for this gateway; unsupported vendor mappings are not invented.

Availability also depends on the account and region. In the captured account, Seedance 2.0 US and 2.5 US are available; Mini/Fast and Kling 4 are region restricted. Wan 3.0 has a frontend schema but is absent from that account's available-model list. Vidu Q4 has no observed upstream mapping. Account availability is checked before material download; a catalog entry alone does not promise that every account can generate it. MiniMax H3's public `768p` alias selects actual `768p`.

## Seedance 2.5 default video

`use_default_reference_video` defaults to `true`. After all standard input fields, Seedance content entries and explicitly labeled prompt references have been parsed, a Seedance 2.5 request with **no caller video** receives one local two-second neutral white video. Existing caller videos are never extended by this feature. `default_reference_video_resolution` selects `480p` (default), `720p` or `1080p`; the clip matches the requested aspect ratio and has no audio. These are gateway defaults, not an assertion of the vendor's minimum dimensions or a guaranteed discount for every prompt.

The selected settings and inserted material are saved with the accepted task. Later changes affect only new requests. Original caller requests remain unchanged, and hidden default material does not become a caller-addressable `video1`. FFmpeg generates and FFprobe validates each ratio/resolution clip in private persistent `data/reference-videos/`; generated clips are excluded from Git. Failure to prepare the clip rejects the request before queueing or generation. This option is independent of the setting that permits caller video references.

For Seedance 2.5 with video references or first/last frames, the upstream uses automatic aspect ratio following those materials. The gateway preserves the requested target ratio and also records `effective_aspect_ratio: "auto"`; the console shows this distinction. The generated default clip matches the target ratio.

## Credits and errors

The account balance is used for account management. It is not treated as a task debit. A task reports credits only when there is task-specific evidence; otherwise `credits_consumed` is `null` and the console shows an unknown source. Purchase prices can be maintained separately for the cost reference view.

The captured protocol does not establish an automatic daily reward endpoint, so the service does not invent one. Interactive verification remains in the normal website flow.

Public errors use a bounded normalization vocabulary. Administrators can inspect both the normalized response and the upstream diagnostic in task details. Clear-completed removes terminal task records and their associated local data while preserving accounts and unresolved tasks.

Failures come from that exact generation's returned error fields. Upstream copyright restrictions use the existing content-policy response; source download timeouts use the material-download response.

Tasks prepare their materials concurrently within the configured worker and account limits. A global limit of four material preparations and two uploads bounds network and memory use. Uploads of identical bytes share one receipt only within the same account and material type. Generation submission is never retried by these upload controls.

Public reference files are fetched anonymously using a direct connection first, with the account proxy as a fallback for temporary download failures. Account verification, upstream uploads, generation, and polling continue to use the account proxy and its isolated authenticated session. Source GET attempts are bounded; partial files are discarded between attempts. Failures identify the material type, index, transport phase, and received byte count without exposing signed URLs. Overlapping requests for the same source share one preparation only within the same account and material type; later requests read the source again so mutable links are not cached indefinitely.

## GitHub Actions and preproduction

`.github/workflows/deploy-test.yml` validates the application and builds/pushes its image on GitHub-hosted runners. The `ele2api-pre` self-hosted runner only pulls the published image, restarts this service, and configures the Cloudflared route. It does not build an image on the deployment host.

Prepare a private `.env` in `DEPLOY_PATH`, register the repository runner with label `ele2api-pre`, and configure its Docker and Cloudflared permissions. Workflow deployment variables are `DEPLOY_PATH`, `DEPLOY_SERVICE`, `IMAGE_REGISTRY`, `IMAGE_NAMESPACE`, `IMAGE_NAME`, `IMAGE_TAG`, `ELE2API_HOSTNAME`, `ELE2API_ORIGIN`, and `CLOUDFLARED_CONFIG`. GHCR uses the workflow's scoped `GITHUB_TOKEN`; external SSH deployments should use `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, and `DEPLOY_PORT`.

Services on the shared Docker network can use `http://ele2api:8817`. The LAN console is available at the host IP and mapped port. Existing environment files, account data, and compose configuration are preserved on subsequent deployments; deployment waits rather than interrupting active tasks or logins.

Raw protocol captures, authenticated sessions, personal media, environment files, databases, and private deployment receipts are excluded from this public repository.
