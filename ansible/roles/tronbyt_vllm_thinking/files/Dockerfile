FROM python:3.12-slim

# Pillow composes the display frames (gif on the left, tok/s panel on the
# right). Version pinned for reproducible rebuilds; bump deliberately.
RUN pip install --no-cache-dir pillow==12.3.0

WORKDIR /app
