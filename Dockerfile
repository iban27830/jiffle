FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# libGL/libglib нужны opencv-python (headless-вариант не используется).
# ffmpeg нужен для конвертации GIF/WebM в MP4 и сжатия больших видео при экспорте коллекций.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN uv pip install --system -r requirements.txt

COPY . .

EXPOSE 5001
CMD ["python", "run.py"]
