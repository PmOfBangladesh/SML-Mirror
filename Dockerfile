FROM ubuntu:22.04

# Prevent interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /usr/src/app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core
    python3 python3-pip python3-venv python3-dev \
    git curl wget ca-certificates \
    # Download managers
    aria2 \
    # Torrent
    qbittorrent-nox \
    # NZB
    sabnzbdplus \
    # Media processing
    ffmpeg \
    # Archives
    p7zip-full unzip \
    # Rclone
    rclone \
    # Mega
    megatools \
    # CPU limit for NZB
    cpulimit \
    # Misc
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set up Python virtual environment
RUN python3 -m venv /usr/src/app/.venv
ENV PATH="/usr/src/app/.venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY . .

# Ensure scripts are executable
RUN chmod +x start.sh setpkgs.sh

# Expose ports for web interface
EXPOSE 80 8080

CMD ["bash", "start.sh"]
