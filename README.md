# Archiviatore YouTube & Social

[![Build & publish image](https://github.com/Gigiomiccio425/archiviatore-social/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Gigiomiccio425/archiviatore-social/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Web UI self-hosted per scaricare e archiviare video/audio da YouTube, TikTok, Instagram,
Twitter/X e oltre 1000 siti supportati da `yt-dlp`. Pensata per Zima OS / CasaOS.

## Cosa fa

- Incolli uno o piu' link (uno per riga) e scegli **Video (massima qualita')** o **Solo audio (MP3)**
- Download in background con coda, avanzamento live, velocita' ed ETA
- Playlist e canali interi con un flag
- File organizzati in `/downloads/{Piattaforma}/{Autore}/{Titolo}.mp4`
- Metadati in `{Titolo}.mp4.metadata.json` + copertina accanto al file
- Storico su SQLite: annulla, riprova, elimina
- Galleria integrata: anteprime, player nel browser (con seek), download sul telefono, eliminazione

## Struttura

```
downloader/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI: route UI + API REST
│   ├── config.py            # tutte le impostazioni da variabili d'ambiente
│   ├── db.py                # SQLite: storico job e file
│   ├── manager.py           # coda a thread + yt-dlp (il motore)
│   ├── files.py             # galleria, path sicuri, streaming con Range
│   ├── templates/
│   │   └── index.html       # UI: Tailwind CDN + Alpine.js
│   └── static/
│       ├── icon.svg
│       └── manifest.json
├── Dockerfile
├── docker-entrypoint.sh
├── docker-compose.yml       # build locale + metadati x-casaos
├── casaos-app.yml           # da importare nella UI di CasaOS
├── requirements.txt
├── .env.example
├── .dockerignore
└── .gitattributes
```

## Installazione su Zima OS

L'immagine e' compilata da GitHub Actions per `linux/amd64` e `linux/arm64` e
pubblicata su GHCR: sul NAS non serve compilare niente.

```
ghcr.io/gigiomiccio425/archiviatore-social:latest
```

### Metodo A - tile CasaOS con icona (consigliato)

1. crea le cartelle sul NAS:
   ```bash
   ssh root@ZIMA_IP
   mkdir -p /DATA/AppData/archiviatore-social/data /DATA/Media/Downloads
   ```
2. CasaOS -> App Store -> `+` -> *Install a customized app* -> Import
3. incolla il contenuto di [`casaos-app.yml`](casaos-app.yml)

CasaOS scarica l'immagine, avvia il container e mostra la tile con icona e link.

### Metodo B - docker compose via SSH

```bash
ssh root@ZIMA_IP
mkdir -p /DATA/AppData/archiviatore-social/data /DATA/Media/Downloads
cd /DATA/AppData/archiviatore-social
curl -O https://raw.githubusercontent.com/Gigiomiccio425/archiviatore-social/main/casaos-app.yml
docker compose -f casaos-app.yml up -d
```

Per compilare invece l'immagine sul NAS: clona il repo e usa
[`docker-compose.yml`](docker-compose.yml) con `docker compose up -d --build`.

UI su `http://ZIMA_IP:8347`.

### Aggiornare

```bash
docker compose -f casaos-app.yml pull && docker compose -f casaos-app.yml up -d
```

Oppure dalla dashboard CasaOS, se segnala l'aggiornamento.

## Variabili d'ambiente

| Variabile | Default | Cosa fa |
|---|---|---|
| `MAX_WORKERS` | `2` | Download in parallelo |
| `MAX_HEIGHT` | `0` | Altezza massima video, `0` = migliore disponibile |
| `PREFER_MP4` | `true` | Preferisce mp4/h264 (compatibile ovunque) invece di VP9/AV1 |
| `AUDIO_FORMAT` | `mp3` | `mp3`, `m4a`, `opus`, `flac`, `wav` |
| `AUDIO_QUALITY` | `192` | kbps per l'audio |
| `WRITE_METADATA_FILE` | `true` | Scrive il `.metadata.json` accanto al media |
| `WRITE_THUMBNAIL` | `true` | Salva la copertina come file (usata dalla galleria) |
| `EMBED_SUBTITLES` | `false` | Incorpora i sottotitoli nel video |
| `SUBTITLE_LANGS` | `it,en` | Lingue sottotitoli |
| `WINDOWS_SAFE_NAMES` | `true` | Nomi file compatibili con SMB/Windows |
| `INCLUDE_VIDEO_ID` | `false` | Aggiunge ` [id]` al nome file |
| `SKIP_EXISTING` | `true` | Non riscarica se il file esiste gia' |
| `RATE_LIMIT` | vuoto | Limite banda, es. `5M` |
| `COOKIES_FILE` | vuoto | Cookie Netscape per contenuti privati, es. `/data/cookies.txt` |
| `UPDATE_YTDLP` | `true` | Aggiorna `yt-dlp` a ogni avvio del container |
| `TZ` | `Europe/Rome` | Fuso orario |

## Contenuti privati o protetti

Instagram, TikTok e X spesso richiedono una sessione. Esporta i cookie del browser in
formato Netscape (estensione *Get cookies.txt LOCALLY*), salvali in
`/DATA/AppData/archiviatore-social/data/cookies.txt` e imposta
`COOKIES_FILE=/data/cookies.txt` nel compose.

## API REST

| Metodo | Endpoint | Descrizione |
|---|---|---|
| `POST` | `/api/download` | `{"url": "...", "kind": "video\|audio", "playlist": false}` |
| `GET` | `/api/jobs` | Storico + stato live + statistiche |
| `POST` | `/api/jobs/{id}/cancel` | Annulla |
| `POST` | `/api/jobs/{id}/retry` | Rimette in coda |
| `DELETE` | `/api/jobs/{id}` | Rimuove dallo storico |
| `POST` | `/api/jobs/clear` | Pulisce i job conclusi |
| `GET` | `/api/files` | Galleria (`q`, `platform`, `kind`) |
| `GET` | `/api/files/stream?path=` | Streaming con supporto Range |
| `GET` | `/api/files/download?path=` | Download del file |
| `GET` | `/api/files/metadata?path=` | Metadati JSON |
| `DELETE` | `/api/files?path=` | Elimina media + file satellite |

Documentazione interattiva: `http://ZIMA_IP:8347/docs`.

## Sviluppo locale (senza Docker)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8080
```

Serve `ffmpeg` nel PATH, altrimenti niente merge video+audio ne' conversione MP3.

## Note

- La UI usa Tailwind e Alpine da CDN: il **browser** deve avere internet.
  Se l'accesso e' solo offline, scarica i due file in `app/static/` e cambia i `<script>`
  in `index.html`.
- L'app non ha autenticazione: tienila sulla LAN. Se la esponi su internet,
  mettici davanti un reverse proxy con login.
- Scarica solo contenuti che hai il diritto di archiviare.
