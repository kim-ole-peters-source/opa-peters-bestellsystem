# Fehlerbehebung: Function Runtimes must have a valid version

Wenn Vercel meldet:

Function Runtimes must have a valid version, for example `now-php@1.0.0`

Dann war in `vercel.json` eine falsche Runtime-Angabe enthalten.

Die Datei `vercel.json` muss so aussehen:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "rewrites": [
    {
      "source": "/(.*)",
      "destination": "/api/index"
    }
  ]
}
```

Wichtig:
- Kein `functions`-Block
- Kein `runtime: python3.12`
- Ziel ist `/api/index`, nicht `/api/index.py`

Danach in GitHub committen und in Vercel erneut deployen.
