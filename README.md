# Dynamic Outro Injection

A small web app that creates on-demand podcast RSS mirrors with custom MP3 endings.

- Paste a public podcast RSS feed and upload an MP3 outro.
- Immediately receive a private-by-obscurity mirror RSS URL. **No episode audio is downloaded during creation.**
- The app periodically refreshes source RSS when a listener fetches the mirror (15-minute refresh interval by default).
- On the first audio GET or HEAD, the app downloads the episode, appends the outro with FFmpeg, and stores the result in a **private Backblaze B2 bucket**.
- Further requests stream the cached file through the web app with `HEAD`, `Range`, `Content-Range`, and `Content-Length` support.
- Creators get a one-time deletion key to remove their mirror and cached audio.

## Architecture

Hostless runs one FastAPI web app and FFmpeg in a Docker container. Backblaze B2 stores outro files, source RSS snapshots, mirror metadata and cached MP3 files; it stays **private**. The podcast clients only see Hostless HTTPS URLs. No Hostless persistent volume or separate database is needed.

## Hostless deployment

1. Create an app from this repository, branch `main`.
2. Use runtime `Python`, framework metadata `FastAPI`, build system **Docker**, Dockerfile path `Dockerfile`.
3. Leave the start command blank; the Dockerfile listens on `0.0.0.0:${PORT}`.
4. Select one replica, with the free 0.5 vCPU / 1024 MB runtime if it is included in your plan. Choose **No volume**.
5. Set these environment variables, entering secrets **only in Hostless**, never in GitHub:

   | Name | Description |
   | --- | --- |
   | `B2_BUCKET` | Existing **private** Backblaze B2 bucket name |
   | `B2_ENDPOINT` | Backblaze S3 endpoint, e.g. `https://s3.eu-central-003.backblazeb2.com` |
   | `B2_KEY_ID` | Bucket-scoped B2 key ID |
   | `B2_APPLICATION_KEY` | Secret bucket-scoped B2 application key |

6. After Hostless assigns a public HTTPS domain, set optional `PUBLIC_BASE_URL=https://your-app-hostname` to ensure links use the canonical origin. You can also set it before creating your first mirror if you know the URL.
7. Keep the Hostless health check as TCP initially; `/health` is also available for HTTP checks.

The Backblaze application key must permit **list, read, write and delete**, including listing object versions to purge cached files. B2's S3-compatible API is used, not the Native API.

## Local development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export B2_BUCKET=your-bucket
export B2_ENDPOINT=https://s3.eu-central-003.backblazeb2.com
export B2_KEY_ID=your-key-id
export B2_APPLICATION_KEY=your-secret-key
uvicorn app.main:app --reload
pytest -q
```

FFmpeg and ffprobe must be installed locally. They are included in the Docker image automatically. On Windows, use PowerShell environment-variable syntax instead of `export`.

## Design limitations and safety

This is a **small public beta**, not yet a hardened multi-tenant production service. The following limitations are intentional and should be resolved or tested before opening it widely.

- **First-request delay:** Generating a long episode can take minutes on a small 0.5-vCPU server. This app waits to deliver the completed MP3. We need to test actual Overcast and Apple Podcasts behaviour to know whether their request timeouts permit it.
- **HEAD requests also generate audio on a cache miss.** Podcast platforms may probe episodes in the background, so real demand can be higher than a person explicitly pressing Play. This is preferable to returning invalid HEAD metadata, but it must be load-tested.
- **Enclosure length estimates:** The source RSS and uploaded outro provide a rough byte-length estimate until the modified MP3 is generated. After generation, both the RSS enclosure and audio HTTP headers use the exact output size. Some podcast clients may not accept an initially estimated RSS enclosure length.
- **Limits:** By default 20 mirrors, 6 mirror creations per connecting IP/hour, 20 audio-generation attempts per day, 8 MB/10-minute outros, 150 MB source episodes, 2 GB current cached audio, 1 GiB monthly audio downloads, and 14-day cache expiry. These are conservative free-tier safeguards, **not a guarantee against charges or abuse**. Counters reset on process restart; the application tracks an approximate monthly audio-download budget in B2, but this does **not** account for every provider operation, guarantee no billing, or enforce atomic distributed limits. Use a reverse-proxy rate limiter, authentication/invite controls, and provider hard caps before wide public launch.
- **SSRF:** Outbound URLs are limited to HTTP(S), customary ports, and public IP addresses checked by the socket resolver, including redirect hops. Still add network egress restrictions when the platform supports them.
- **One replica:** In-process locks serialize audio generation. Do not increase replicas without distributed locks and a persistent job queue.
- **Hostless temporary disk:** `/tmp` holds a maximum of 500 MiB. Inputs are size-limited, but unusually large transcoded outputs can still exhaust the available space. Keep concurrent generation at one and verify the space usage on the live plan.
- **B2 version history:** Backblaze buckets are versioned. Cache eviction permanently deletes specific object versions, while changing RSS/JSON metadata creates previous versions. Configure an appropriate Backblaze lifecycle rule to remove old metadata versions, or storage usage may continue growing despite overwrites.
- **Storage costs:** A private bucket avoids the B2 public-bucket creation requirement, but every stream forwarded via Hostless still downloads bytes from B2. Free allowances can change; not having a payment method on file is safer than assuming usage cannot exceed allowances.
- **Permissions:** Use only podcast/audio content you have rights or permission to modify and make available. Mirrors are unlisted and use unguessable identifiers, but feed and audio endpoints are publicly reachable to anyone holding the URL. This app does not support private source feeds or authentication-protected podcast downloads.
- **Episode duration:** Limited to six hours to stay within Hostless's 500 MiB temporary filesystem.
- **Source RSS coverage:** RSS 2.0 feeds with publicly accessible audio enclosures only. Other podcast formats and unusual RSS extensions are not guaranteed to work.

## File layout

- `app/main.py` - FastAPI handlers, mirror management, generation, quota enforcement, HTTP range proxy.
- `app/rss.py` - defensively parses and rewrites RSS feeds, preserving metadata.
- `app/audio.py` - validates MP3s, stream-copies compatible MP3 files or transcodes with FFmpeg.
- `app/security.py` - bounded HTTP fetching with DNS-rebinding-resistant URL resolution.
- `app/storage.py` - private Backblaze B2 S3 integration and cache cleanup.
- `app/static/index.html` - standalone responsive browser interface.
- `tests/` - RSS, HTTP, security and audio processing tests.

## Privacy

The Backblaze application key is never exposed to browsers. No account is required to create a mirror. Creator deletion keys are returned once and stored only as SHA-256 hashes in B2. Uploaded outros, RSS snapshots and generated audio stay in a private bucket; Hostless streams the requested modified episode publicly when someone has the link.
