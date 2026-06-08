FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed for easyocr and yt-dlp/ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download easyOCR weights at build time (saves startup and execution time)
RUN python -c "import easyocr; easyocr.Reader(['en'], gpu=False)"

# Copy application code
COPY . .

# Create directory for SQLite cache/db files
RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 7860

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
