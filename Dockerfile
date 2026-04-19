FROM python:3.12-slim

WORKDIR /app

ARG NLP_ENGINE=spacy
ARG TRANSFORMERS_MODEL=dslim/bert-base-NER

# Install system deps required by spaCy / Presidio / Stanza
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY scripts/quantize_model.py /tmp/quantize_model.py
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$NLP_ENGINE" = "stanza" ]; then \
        python -c "import stanza; stanza.download('en')"; \
    elif [ "$NLP_ENGINE" = "onnx" ]; then \
        python -m spacy download en_core_web_sm && \
        python -c "model='${TRANSFORMERS_MODEL}'; need_export='-onnx' not in model.lower(); print(f'Downloading ONNX model: {model} (export={need_export})'); from optimum.onnxruntime import ORTModelForTokenClassification; from transformers import AutoTokenizer; ORTModelForTokenClassification.from_pretrained(model, export=need_export); AutoTokenizer.from_pretrained(model); print('ONNX model cached successfully')" && \
        python /tmp/quantize_model.py "${TRANSFORMERS_MODEL}" /app/models/onnx-int8 ; \
    elif [ "$NLP_ENGINE" = "transformers" ]; then \
        python -m spacy download en_core_web_sm && \
        python -c "from transformers import AutoTokenizer, AutoModelForTokenClassification; AutoTokenizer.from_pretrained('${TRANSFORMERS_MODEL}'); AutoModelForTokenClassification.from_pretrained('${TRANSFORMERS_MODEL}')"; \
    else \
        python -m spacy download en_core_web_lg; \
    fi

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers ${WEB_CONCURRENCY:-6} \
  --bind 0.0.0.0:8000 \
  --preload"]
