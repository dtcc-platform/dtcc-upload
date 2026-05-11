# Ubuntu 24.04 systemd deployment

This unit runs `dtcc-upload` as a dedicated local service user, serves Uvicorn on
`127.0.0.1:8000`, and stores uploaded datasets under `/var/lib/dtcc-upload`.
Put nginx or Caddy in front for TLS and public access.

## Install

```bash
sudo apt update
sudo apt install -y git python3 python3-venv curl
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo install -m 0755 "$HOME/.local/bin/uv" /usr/local/bin/uv

sudo useradd --system --home /srv/dtcc-upload --shell /usr/sbin/nologin dtcc-upload
sudo mkdir -p /srv /etc/dtcc-upload
sudo git clone <repo-url> /srv/dtcc-upload
sudo chown -R dtcc-upload:dtcc-upload /srv/dtcc-upload

cd /srv/dtcc-upload
sudo -u dtcc-upload /usr/local/bin/uv sync --no-dev --locked

sudo cp deploy/systemd/dtcc-upload.service /etc/systemd/system/dtcc-upload.service
sudo cp deploy/systemd/dtcc-upload.env.example /etc/dtcc-upload/dtcc-upload.env
sudo chmod 600 /etc/dtcc-upload/dtcc-upload.env
sudo editor /etc/dtcc-upload/dtcc-upload.env

sudo systemctl daemon-reload
sudo systemctl enable --now dtcc-upload
```

The service uses the generated virtualenv at
`/srv/dtcc-upload/.venv/bin/uvicorn`, so it does not need `uv` at runtime.

## Verify

```bash
sudo systemctl status dtcc-upload
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS -H "Authorization: Bearer <upload-token>" http://127.0.0.1:8000/v1/me
curl -fsS -H "Authorization: Bearer <upload-token>" http://127.0.0.1:8000/v1/datasets
```

Follow logs with:

```bash
sudo journalctl -u dtcc-upload -f
```
