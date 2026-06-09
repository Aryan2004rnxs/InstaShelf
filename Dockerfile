FROM python:3.11-slim

# Create a non-root user named "user" with UID 1000
RUN useradd -m -u 1000 user

WORKDIR /home/user/app

# Install system dependencies needed for easyocr and yt-dlp/ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install them globally as root (accessible by all users)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set up environment variables for the user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Switch to the non-root user
USER user

# Pre-download easyOCR weights at build time into the user's home directory
RUN python -c "import easyocr; easyocr.Reader(['en'], gpu=False)"

# Copy application files with user ownership
COPY --chown=user . .

# Pre-create data directory for the SQLite cache/db files
RUN mkdir -p data

EXPOSE 7860

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
