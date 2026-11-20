<!-- coderabbit-ai-generated:v1 -->

# Clipstudio

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:d78eea786b12abf4d85e3855b3cf69ee60ffbefdc6e19eff384b1d2d45985111 -->

A video clipping studio with Google-only accounts, one free successfully processed video per account, and optional monthly ($5 USD) or yearly ($60 USD) subscriptions. The verified owner email `xamoxm366@gmail.com` always has free access. Speech recognition runs on your server; processing does not call a paid AI API.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:c036cc3246dbd75643ea0297fef09bbcbebb639599a07a336f4b80128d2966f1 -->

## Speed and source preview update

The interface defaults to **Fast** mode. Balanced retains the original base model. Choose **First 5 minutes** for a quick test of a long video; only that part is searched. Entire video remains the default. **720p** reduces export work. Source downloading still fetches the whole video. Model downloads happen once per model; speed depends on your computer, source duration and clip count.

Source cards now show the YouTube thumbnail, title, channel and duration, with a link to watch the original. Metadata appears once YouTube provides it. Thumbnail loading needs access to YouTube image servers. Processing progress shows transcribed audio time and elapsed transcription time. Completed new projects also offer a transcript text download. Old projects remain readable.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:32505da5b28d5c1c45ae5a187de52b00e41050aa337f2f579aa29e5b37a3de2b -->

For this accounts update, stop the server, replace application files and the dependency manifests, keep your `data` folder, and run `uv sync --frozen --no-dev`. Configure `.env` as described below, then restart with `--env-file .env`. A job already running uses the old version until restart. Older projects without an owner are visible only to the verified owner (or explicit private local mode).

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:0029584fead91a5f2c9d87b5634f9be9460a1dc781f2e5efc4607404c11ef6c6 -->

## Google accounts and payment activation

The UI includes separate **Sign in with Google** and **Sign up with Google** actions using the same secure Google identity flow. There are no email/password accounts. Until Google credentials are supplied, access is closed and the page explains setup is pending. Accounts and entitlements are stored in `data/accounts.sqlite3`; keep this database and `data/session.key` persistent and private. Failed/interrupted jobs refund the trial, while completed jobs permanently consume it, even after project deletion. Trial allowance is per verified Google account, not per person across multiple accounts.

1. Copy `.env.example` to `.env`. In Google Cloud create a Web application OAuth client with the consent screen and redirect URI `http://127.0.0.1:8000/auth/callback` for local use (use your actual HTTPS URL in production). Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `PUBLIC_URL`. In testing mode add authorized test users. Google handles passwords; never enter a Google password into configuration.
2. For **private local clipping without accounts/billing**, explicitly set `CLIPSTUDIO_LOCAL_MODE=1`. This grants local access and is not suitable for a public paid site. Default is 0, with authentication required. No fake Google sign-in is provided.
3. Payments require an eligible payment provider merchant account, account verification, and a receiving bank account. A debit card is not a payout integration. Google Pay is a customer payment method, not a substitute for merchant onboarding. Country and provider eligibility determine support for Google Pay, UPI and recurring payments. This release supplies an optional **Stripe** implementation; if Stripe onboarding is unavailable in your country, choose another provider before launch. There is no manual GPay QR code subscription activation.
4. In Stripe test mode create two active recurring prices: **500 USD cents per month** and **6000 USD cents per year**, interval count 1. Set both price IDs and `STRIPE_SECRET_KEY`. The backend verifies amounts, currency and interval before opening hosted checkout. Set a webhook endpoint at `<PUBLIC_URL>/api/billing/webhook`, subscribed to `invoice.paid`, `customer.subscription.updated` and `customer.subscription.deleted`; store the signing secret as `STRIPE_WEBHOOK_SECRET`. Enable/configure the Stripe customer portal for cancellation. Then use live keys/price IDs only after account approval and live testing. Do not put secrets in Git or chat.
5. Server-verified signed payment events grant access until the current paid period ends. Redirecting to the success page never grants access. Expired access is checked on each request; failed payments do not extend access; immediate cancellation revokes access, while cancellation at period end retains the already paid period. The owner email exemption applies only to the address verified by Google.

**Speed:** Fast mode uses a smaller speech model; optional 5/10-minute analysis limits reduce work and coverage. Frame-rate normalization occurs before rendering filters, and highlight selection avoids repeated copying of the remaining transcript. For a compatible NVIDIA server you may set `WHISPER_DEVICE=cuda` and `WHISPER_COMPUTE_TYPE=float16` after installing matching CUDA/cuDNN libraries. GPU operation has not been verified in this CPU sandbox. No fixed 2- or 4-minute turnaround is guaranteed. Downloads, model loading, video length, captions and concurrent demand all affect time. The UI asks users to have patience.

**Integration verification:** tests cover authentication boundaries, verified owner handling, trial persistence/concurrency, private files, stream uploads, signed billing events, replay and cancellation. Real Google/Stripe credentials are still required to validate live accounts/payments. The pinned local emulator catalog supports Stripe checkout primitives but does not implement subscription retrieval/billing portal or the recurring invoice lifecycle; its Google JWKS is empty and it signs with an internal HS256 secret, which cannot satisfy the production Google ID-token verification flow. No verification checks are disabled to make an emulator pass.

References: [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect), [Stripe account activation](https://docs.stripe.com/get-started/account/activate), [Stripe Google Pay](https://docs.stripe.com/google-pay), [Stripe subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks).

## What it does

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

- Accepts a single YouTube video link or an MP4, MOV, MKV, M4V, or WebM upload.
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:ea1ee19a49467ddc7591df5d85fef978556e134d6ee4c58cb308ce5a04e146f2 -->

- Uses **Whisper tiny** in Fast mode or **Whisper base** in Balanced mode, on your CPU, with word timestamps. Fast mode may reduce caption accuracy.

<!-- /coderabbit-ai-generated:v1 -->
- Ranks excerpts using speech density, distinctive words, and opening language. Ranking uses transparent rules; transcription is the AI component. It does not predict virality.
- Creates up to 5 nonoverlapping clips, each up to 15, 30, or 60 seconds.
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:c177bfc95f691cd760aaee2ac923c87099c98425b84f69f91aee060907bc6734 -->

- Exports H.264/AAC MP4 at **1080 × 1920** or **720 × 1280**, 30 fps, with optional burned-in captions.

<!-- /coderabbit-ai-generated:v1 -->
- Offers a blurred background that keeps the whole picture, or a center crop.
- Shows real processing progress, video previews, downloads, and saved project history.

## Start in VS Code

### 1. Install the tools once

Install [VS Code](https://code.visualstudio.com/), [uv](https://docs.astral.sh/uv/getting-started/installation/), [FFmpeg](https://ffmpeg.org/download.html), and [Node.js LTS](https://nodejs.org/). uv will download Python 3.12 for this project automatically. Node is used by yt-dlp to handle YouTube's JavaScript challenges.

**Windows:** Open PowerShell and run:

```powershell
winget install --id Microsoft.VisualStudioCode -e
winget install --id astral-sh.uv -e
winget install --id Gyan.FFmpeg -e
winget install --id OpenJS.NodeJS.LTS -e
```

Close and reopen VS Code after installing so its terminal sees the updated PATH. If winget is unavailable, use the official download links above.

**macOS with Homebrew:**

```sh
brew install uv ffmpeg node
```

**Ubuntu/Debian:**

```sh
sudo apt-get update
sudo apt-get install -y ffmpeg pipx
pipx install uv
pipx ensurepath
```

Install Node.js LTS using its official installer or instructions, then reopen your terminal. Use Node 22 or newer. FFmpeg must include the `libx264` encoder and `ass` subtitle filter (usual full builds do).

Check your installation in **VS Code → Terminal → New Terminal**:

```sh
uv --version
ffmpeg -version
ffprobe -version
node --version
```

### 2. Open this project

Download the project from GitHub, extract it, and use **File → Open Folder** in VS Code to open the folder containing `pyproject.toml`. Or clone it if you have Git:

```sh
git clone https://github.com/xamocr3466-del/clips.ai.git
cd clips.ai
```

### 3. Install dependencies and start

Run in the VS Code terminal:

```sh
uv sync --frozen --no-dev
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:f700e7447097c5accadd250b26336a5eece7d1bb7f87c05463553a50fc884aea -->

uv run --no-dev uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000

<!-- /coderabbit-ai-generated:v1 -->
```

Open **http://127.0.0.1:8000** in your browser. Leave the terminal running. Press **Ctrl+C** to stop it. Next time, only run the second command.

If VS Code asks for a Python interpreter, select `.venv` (on Windows: `.venv\Scripts\python.exe`; on macOS/Linux: `.venv/bin/python`). No extensions are required; Microsoft's Python extension is optional.

### 4. Make your first clips

1. Paste a public YouTube video link, or choose **Upload a video** and select your video.
2. Choose clip length, number of clips, framing, and captions. Start with **15 seconds, 1 clip** to test your machine.
3. Click **Generate clips**. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:7ada48db736ea101335343311039138a36b66e49712e5caa7f67d05bffc0b4ef -->

The first spoken video downloads the selected free model (tiny for Fast mode, base for Balanced).

<!-- /coderabbit-ai-generated:v1 --> This requires internet; later local uploads can be processed offline after the model is cached.
4. Wait for processing. On a CPU, long videos can take several minutes or longer. Keep the app and terminal open.
5. Preview your clips under **My clips**, check the captions and framing, then click **Download MP4**.
6. Upload the downloaded file to YouTube through YouTube Studio or the YouTube app yourself.

## What “free” and “working” mean

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:8729f8d2fbf7e5e9903d8dde78478e45a2d1b0132f65b4ce56fa1b19107dbb44 -->

Processing uses your server CPU/GPU, memory, disk space, electricity and internet. There is no included free hosting. Google accounts require OAuth configuration; paid subscriptions require an activated merchant account and incur provider fees. This implementation uses one Uvicorn worker and a local SQLite database; use HTTPS, persistent private storage and a reverse proxy for public deployment. The development Preview runs in its sandbox.

<!-- /coderabbit-ai-generated:v1 -->

No tool can guarantee perfect highlights, accurate captions, or access to every YouTube video. Private, age-restricted, DRM-protected, region-restricted, live, or blocked videos may not download. YouTube can reject a cloud IP or require sign-in. **Upload a local copy when a link fails.** Only reuse videos you own or have permission to use.

The app is best for spoken content, such as podcasts, interviews, and tutorials. It does not perform face tracking, semantic LLM editing, manual timeline editing, or automatic YouTube publishing. Center crop can cut off subjects; fit framing preserves the picture. Silent videos return a single opening excerpt without captions. Shorts can be shorter than your selected maximum, and fewer than the requested number may be available when the source has too few suitable nonoverlapping moments.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:6e6201606295d74eabcd35ce33384b6a2f53fd835ff3fbb3f0e79121a6a1447b -->

There is **no fixed application size or duration cap by default**, for the free video as well as paid access. Available server disk space, hosting request limits and processing time still apply. Raw uploads stream directly to disk and reserve 256 MB free space. Set `CLIPSTUDIO_MAX_BYTES` or `CLIPSTUDIO_MAX_DURATION` (seconds) to a positive value to impose an operator limit. There are up to 5 clips per job and 3 queued/active jobs globally. One job runs at a time; use one Uvicorn worker. Resource needs increase with video length and size.

<!-- /coderabbit-ai-generated:v1 -->

## Files and privacy

Exports and project metadata live in `data/<project-id>/`. These files are ignored by Git. Original source downloads/uploads and temporary audio are removed after processing. Captions are burned into the video; the transcript is kept locally as `transcript.json`. Deleting a project in the app deletes its exports and metadata. Whisper's model cache is managed separately by Hugging Face in your user cache.

Restarting preserves finished projects and marks interrupted jobs as failed. Resubmit an interrupted video. To choose another storage location, set `CLIPSTUDIO_DATA` before startup. Videos and transcripts are processed on the machine running the app. YouTube requests and initial model downloads require network access. The optional Google web fonts also use the network; system fonts are used offline.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `uv` or `ffmpeg` is not recognized | Reopen VS Code. Check the executable is on PATH. Install a full FFmpeg build, not the similarly named Python package. |
| YouTube link fails | Use a local video upload. For upstream fixes, run `uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs`, then `uv sync --no-dev` and restart. |
| First video seems slow | The model is downloaded and initialized once. Watch the progress message and terminal. CPU transcription and full HD encoding take time. |
| Model will not download | Check internet access to Hugging Face and free disk space; retry the job. No token is normally needed for the public model. |
| Captions are wrong | Try clearer audio. Whisper may mishear words or languages. Review exports before publishing. |
| No speech found | Use a spoken video. Silent videos get an opening excerpt without captions. |
| Port 8000 is busy | Change `--port 8000` to `--port 8001` and open that port in your browser. |
| Video processing fails | Check the terminal log, FFmpeg subtitle support, and free disk space. Try a normal MP4 under the limits. |

## Development and verification

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check app tests
node --check app/static/app.js
```

The tests cover URL validation, clip bounds and overlap, captions, real FFmpeg exports in both framing modes, API validation, upload limits, file access restrictions, queue limits, and restart recovery. They use synthetic media and do not require a model download or access to YouTube. For full integration verification, upload a spoken video and review the resulting clips; model and YouTube behavior depend on network availability.

Architecture: `app/main.py` manages API jobs and disk persistence, `app/pipeline.py` downloads/transcribes/ranks/renders, and `app/static/` holds the browser interface. Dependencies are pinned in `uv.lock`.

Primary upstream references: [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [yt-dlp and its dependencies](https://github.com/yt-dlp/yt-dlp#dependencies), and [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html).
