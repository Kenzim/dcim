# DHCP + TFTP runner with control API (start/stop/restart).
# Main DCIM app calls this container's API; config is read from shared volume.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    isc-dhcp-server \
    tftpd-hpa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./

# API port (DHCP 67 and TFTP 69 are used by the daemons)
EXPOSE 9080

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9080"]
