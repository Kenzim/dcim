FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    samba samba-common-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY media_runner/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY media_runner/main.py ./main.py
COPY media_runner/smb.conf /etc/samba/smb.conf
COPY runner_common ./runner_common
COPY isos/ /opt/seed/

ENV PYTHONUNBUFFERED=1
EXPOSE 9083 445

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9083"]
