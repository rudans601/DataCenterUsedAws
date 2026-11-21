#!/usr/bin/env bash
# Azure OpenAI 리소스를 만든다 (2026-11-09에 실제로 실행한 절차를 스크립트로 정리).
#   사전 조건: az login 완료, Azure for Students 구독
#   학생 구독은 리전 정책상 koreacentral 등 5개 리전만 허용되고,
#   종량제(GlobalStandard) 모델은 koreacentral에서만 쓸 수 있었다.
#   사용 예: ./scripts/azure-setup.sh kubeops-donkatsu-1234
set -euo pipefail
NAME="${1:?리소스 이름을 넣어 주세요 (전역에서 유일해야 함)}"
RG="${RG:-rg-kubeops-copilot}"
LOC="${LOC:-koreacentral}"

az provider register -n Microsoft.CognitiveServices --wait
az group create -n "$RG" -l "$LOC" --tags project=kubeops-copilot -o none
az cognitiveservices account create -n "$NAME" -g "$RG" -l "$LOC" --kind OpenAI --sku S0 \
  --custom-domain "$NAME" --yes -o none

# 종량제(GlobalStandard) 배포: 기본 요금 없이 사용한 토큰만큼 과금. capacity는 분당 토큰 한도(천 단위).
az cognitiveservices account deployment create -n "$NAME" -g "$RG" --deployment-name gpt-4.1-mini \
  --model-name gpt-4.1-mini --model-version 2025-04-14 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 50 -o none
az cognitiveservices account deployment create -n "$NAME" -g "$RG" --deployment-name text-embedding-3-small \
  --model-name text-embedding-3-small --model-version 1 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 50 -o none

# API 키 대신 로그인한 계정으로 호출할 수 있게 역할을 준다.
SCOPE=$(az cognitiveservices account show -n "$NAME" -g "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$(az ad signed-in-user show --query id -o tsv)" \
  --assignee-principal-type User --role "Cognitive Services OpenAI User" --scope "$SCOPE" -o none

echo "AZURE_OPENAI_ENDPOINT=https://$NAME.openai.azure.com/"
