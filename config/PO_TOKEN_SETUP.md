# Word Clips PO Token Setup

Use this only when YouTube anti-bot blocks keep happening (`Sign in to confirm you're not a bot`).

## Option A: file-based (recommended)

1. Copy `config/yt_po_tokens.example.json` to `data/yt_po_tokens.json`.
2. Fill real token values:

```json
{
  "android.gvs": "xxx",
  "web.gvs": "yyy"
}
```

3. Restart API container:

```powershell
docker compose up -d api
```

## Option B: env-based

Set `.env`:

```env
YTDLP_PO_TOKENS=android.gvs=xxx,web.gvs=yyy
```

Then recreate API:

```powershell
docker compose up -d api
```

## Supported keys

- `android.gvs`
- `web.gvs`
- `mweb.gvs`
- `ios.gvs`
- `tv.gvs`

The pipeline auto-picks tokens by active client profile.
