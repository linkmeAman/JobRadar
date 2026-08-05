FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY job_radar ./job_radar
COPY config.yaml PROJECT.md README.md ./

RUN mkdir -p /app/data /app/resumes /app/data/backups

CMD ["python", "-m", "job_radar.scheduler"]
