FROM python:3.12-slim

WORKDIR /app

# Build deps for some Python wheels (numpy/onnxruntime arrive prebuilt for
# linux/arm64 + linux/amd64, but a couple of transitive deps still need gcc).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
 && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the rembg u2netp model so the first upload isn't a 4 MB cold
# fetch + 50 MB ONNX runtime warm-up at request time. Bakes the model into
# the image so even an air-gapped container can serve the upload endpoint.
RUN python -c "from rembg import new_session; new_session('u2netp')"

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
COPY api/reminders.py /app/reminders.py

ENV PHV_LIB=/app
ENV HAPI_BASE=http://hapi:8080/fhir
ENV ASSISTANT_DB=/data/chat.db

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
