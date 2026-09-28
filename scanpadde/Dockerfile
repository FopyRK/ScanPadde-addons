FROM ghcr.io/home-assistant/base-python:3.12-alpine3.23@sha256:805c2c1ec95ab16cbc1a250bbcaed271f732c02d14cf3b51f3706444e603ad32
LABEL io.hass.version="0.1.12" io.hass.type="app" io.hass.arch="amd64"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/scanpadde
COPY requirements.txt ./
RUN apk add --no-cache tesseract-ocr tesseract-ocr-data-deu tesseract-ocr-data-eng poppler-utils rclone \
 && pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY app ./app
COPY run.sh /run.sh
RUN chmod 0755 /run.sh
# One Python process. No s6 service tree is required; exec forwards SIGTERM directly.
ENTRYPOINT ["/run.sh"]
