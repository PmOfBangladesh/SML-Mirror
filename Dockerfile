FROM mysterysd/wzmlx:v3

WORKDIR /usr/src/app

# Install missing system dependencies (aria2 already in base)
RUN apt-get update && apt-get install -y --no-install-recommends \
    qbittorrent-nox \
    ffmpeg \
    rclone \
    p7zip-full \
    curl \
    megatools \
    && rm -rf /var/lib/apt/lists/*

RUN chmod 777 /usr/src/app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]
