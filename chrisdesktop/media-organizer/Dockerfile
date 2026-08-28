FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lookup.py .
COPY organizer.py .

CMD ["python", "-u", "organizer.py"]
