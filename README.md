# HarvesterAI Web

Browser-based systematic review extraction tool. PDFs are processed locally in your browser (text extracted via PDF.js) — only the extracted text is sent to the server and on to the AI API. PDF files themselves are never stored on the server.

## Features

- **Client-side PDF extraction** — PDFs never leave your machine
- **Shared job history** — team members can see all jobs and download results
- **Live log streaming** — watch extraction progress in real time via SSE
- **Multi-provider** — DeepSeek, OpenAI, Anthropic
- **Resume-friendly** — each job stores its results on the server
- **No login required** — suitable for a trusted team on a private server

## Setup

```bash
chmod +x run.sh
./run.sh
```

Then open `http://your-server:8000` in a browser.

## Running behind Nginx (recommended)

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off;          # Critical for SSE log streaming
        proxy_read_timeout 3600s;     # Long timeout for large jobs
        client_max_body_size 200M;    # For large PDF text payloads
    }
}
```

## Running as a systemd service

```ini
# /etc/systemd/system/harvesterai.service
[Unit]
Description=HarvesterAI Web
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/harvesterai-web
ExecStart=/path/to/harvesterai-web/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now harvesterai
```

## Questions Excel format

Same format as the desktop version:

| # | Question | Qname | Recommended Answer Options | Additional Instructions | Example Answer 1 | … |
|---|----------|-------|---------------------------|------------------------|------------------|---|

## Notes

- Use `--workers 1` with uvicorn — the in-memory SSE fan-out and job context store are not multi-process safe. If you need multiple workers, replace the in-memory structures with Redis.
- API keys are stored in SQLite (in the job config) — ensure the server is on a trusted network or behind auth if this is a concern.
- Results are stored in `results/{job_id}/` and persist until you delete the job.
