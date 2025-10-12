#!/usr/bin/env bash
set -euo pipefail

PROJECT="${PROJECT:-media-just-skyline-474622}"
REGION="${REGION:-us-central1}"
MEDIA_BUCKET="${MEDIA_BUCKET:-media-just-skyline-474622-e1}"
SERVICE_PREFIX="${SERVICE_PREFIX:-youtube-}"

topics=(
  "yt-new-video"
  "mp3-download"
  "mp3-ready"
  "diarization-ready"
  "binance-course"
  "pumpfun-clip"
)

echo "Creating Pub/Sub topics in project ${PROJECT}..."
for topic in "${topics[@]}"; do
  if gcloud pubsub topics describe "${topic}" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "Topic ${topic} already exists."
  else
    gcloud pubsub topics create "${topic}" --project "${PROJECT}"
  fi
done

echo "Ensuring service accounts..."
declare -A service_accounts=(
  ["${SERVICE_PREFIX}webhook"]="Publishes yt-new-video events"
  ["${SERVICE_PREFIX}metadata"]="Subscribes yt-new-video, publishes mp3-download"
  ["${SERVICE_PREFIX}downloader"]="Subscribes mp3-download, publishes mp3-ready"
  ["${SERVICE_PREFIX}diarizer"]="Subscribes mp3-ready, publishes diarization-ready"
  ["${SERVICE_PREFIX}warehouse"]="Subscribes diarization-ready"
  ["${SERVICE_PREFIX}binance-crawler"]="Publishes binance-course events"
  ["${SERVICE_PREFIX}binance-downloader"]="Consumes binance-course, publishes mp3-ready"
  ["${SERVICE_PREFIX}pumpfun-publisher"]="Publishes pumpfun-clip events"
  ["${SERVICE_PREFIX}pumpfun-downloader"]="Consumes pumpfun-clip, publishes mp3-ready"
)

for sa in "${!service_accounts[@]}"; do
  if gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "Service account ${sa} exists."
  else
    gcloud iam service-accounts create "${sa}" --description="${service_accounts[$sa]}" --display-name="${sa}" --project "${PROJECT}"
  fi
done

echo "Granting IAM roles..."
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}webhook@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}metadata@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}metadata@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}diarizer@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}diarizer@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}diarizer@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}warehouse@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}binance-crawler@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}binance-downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}binance-downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}binance-downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}pumpfun-publisher@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}pumpfun-downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}pumpfun-downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SERVICE_PREFIX}pumpfun-downloader@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

echo "Remember to create Pub/Sub subscriptions after deploying services:"
echo "  gcloud pubsub subscriptions create ${SERVICE_PREFIX}metadata --topic=yt-new-video --push-endpoint=https://<webhook-url>/pubsub/push --ack-deadline=10 --project=${PROJECT}"
echo "  gcloud pubsub subscriptions create ${SERVICE_PREFIX}downloader --topic=mp3-download --push-endpoint=https://<downloader-url>/pubsub/push --project=${PROJECT}"
echo "  gcloud pubsub subscriptions create ${SERVICE_PREFIX}diarizer --topic=mp3-ready --push-endpoint=https://<diarizer-url>/pubsub/push --project=${PROJECT}"
echo "  gcloud pubsub subscriptions create ${SERVICE_PREFIX}warehouse --topic=diarization-ready --push-endpoint=https://<warehouse-url>/pubsub/push --project=${PROJECT}"
echo "  gcloud pubsub subscriptions create ${SERVICE_PREFIX}binance-downloader --topic=binance-course --push-endpoint=https://<binance-downloader-url>/pubsub/push --project=${PROJECT}"
echo "  gcloud pubsub subscriptions create ${SERVICE_PREFIX}pumpfun-downloader --topic=pumpfun-clip --push-endpoint=https://<pumpfun-downloader-url>/pubsub/push --project=${PROJECT}"

echo "Set required secrets in Secret Manager:"
echo "  gcloud secrets create youtube-api-key --replication-policy=automatic --project=${PROJECT}"
echo "  gcloud secrets versions add youtube-api-key --data-file=- --project=${PROJECT} < <(echo \"YOUR_API_KEY\")"
echo "Assign Secret Manager access to the relevant service accounts."
echo
echo "Schedule Cloud Run invocations (example using Cloud Scheduler):"
echo "  gcloud scheduler jobs create http pumpfun-publisher --schedule='*/10 * * * *' --uri=https://<pumpfun-publisher-url>/trigger --http-method=POST --oidc-service-account-email=${SERVICE_PREFIX}pumpfun-publisher@${PROJECT}.iam.gserviceaccount.com --project=${PROJECT}"
echo "  gcloud scheduler jobs create http binance-crawler --schedule='0 */6 * * *' --uri=https://<binance-crawler-url>/trigger --http-method=POST --oidc-service-account-email=${SERVICE_PREFIX}binance-crawler@${PROJECT}.iam.gserviceaccount.com --project=${PROJECT}"
