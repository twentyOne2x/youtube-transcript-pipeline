PROJECT ?= media-just-skyline-474622
REGION ?= us-central1
TAG ?= latest
MEDIA_BUCKET ?= media-just-skyline-474622-e1
IMAGE_PREFIX ?= gcr.io/$(PROJECT)
SERVICES := youtube_webhook metadata_enricher mp3_downloader diarization_worker warehouse_ingestion \
	diarization_indexer pumpfun_publisher pumpfun_downloader binance_crawler binance_downloader

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  make venv                 # Create local virtualenv (.venv)"
	@echo "  make install              # Install dependencies into .venv"
	@echo "  make test                 # Run pytest suite inside .venv"
	@echo "  make build SERVICE=name   # Build a service container (uses services/<name>/Dockerfile)"
	@echo "  make build-all            # Build containers for all services"
	@echo "  make deploy SERVICE=name  # Deploy service to Cloud Run (requires gcloud auth)"

.PHONY: venv
venv:
	python3 -m venv .venv

.PHONY: install
install: venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt

.PHONY: test
test:
	. .venv/bin/activate && PYTHONPATH=. pytest

.PHONY: build
build:
	@if [ -z "$(SERVICE)" ]; then echo "Usage: make build SERVICE=<service_name>"; exit 1; fi
	docker build -t $(IMAGE_PREFIX)/$(SERVICE):$(TAG) -f services/$(SERVICE)/Dockerfile .

.PHONY: build-all
build-all:
	for svc in $(SERVICES); do \
		docker build -t $(IMAGE_PREFIX)/$$svc:$(TAG) -f services/$$svc/Dockerfile . || exit 1; \
	done

.PHONY: deploy
deploy:
	@if [ -z "$(SERVICE)" ]; then echo "Usage: make deploy SERVICE=<service_name>"; exit 1; fi
	gcloud run deploy $(SERVICE) \
		--image $(IMAGE_PREFIX)/$(SERVICE):$(TAG) \
		--project $(PROJECT) \
		--region $(REGION) \
		--platform managed \
		--allow-unauthenticated \
		--update-env-vars GCP_PROJECT=$(PROJECT),MEDIA_BUCKET=$(MEDIA_BUCKET)
