FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared modules
COPY lib/loinc_synonyms.py /app/loinc_synonyms.py

# API app
COPY api/app.py /app/app.py
COPY api/assistant.py /app/assistant.py
COPY api/narrator.py /app/narrator.py
COPY api/analyst.py /app/analyst.py
COPY api/meds.py /app/meds.py
COPY api/patient_profile.py /app/patient_profile.py
COPY api/epic_oauth.py /app/epic_oauth.py

ENV PHV_LIB=/app
ENV HAPI_BASE=http://hapi:8080/fhir
ENV ASSISTANT_DB=/data/chat.db

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
