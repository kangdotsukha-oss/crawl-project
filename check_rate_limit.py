"""
Anthropic API Rate Limit 확인 스크립트
응답 헤더에서 실제 TPM/RPM 한도를 출력합니다.
"""
import os
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not API_KEY:
    print("❌ ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
    exit(1)

response = requests.post(
    "https://api.anthropic.com/v1/messages",
    headers={
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    },
    json={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    },
    timeout=30,
)

print(f"HTTP Status: {response.status_code}\n")
print("=== Rate Limit Headers ===")
rl_headers = {k: v for k, v in response.headers.items() if "ratelimit" in k.lower()}
for k, v in sorted(rl_headers.items()):
    print(f"  {k}: {v}")

if response.status_code == 200:
    print("\n=== 결론 ===")
    rpm_limit = int(response.headers.get("anthropic-ratelimit-requests-limit", 0))
    tpm_limit = int(response.headers.get("anthropic-ratelimit-tokens-limit", 0))
    ipm_limit = int(response.headers.get("anthropic-ratelimit-input-tokens-limit", 0) or 0)
    print(f"  RPM 한도: {rpm_limit:,} requests/min")
    print(f"  TPM 한도: {tpm_limit:,} tokens/min")
    if ipm_limit:
        print(f"  Input TPM 한도: {ipm_limit:,} tokens/min")

    if tpm_limit >= 1_000_000:
        tier = "Tier 4+"
        semaphore = 10
    elif tpm_limit >= 200_000:
        tier = "Tier 2~3"
        semaphore = 5
    elif tpm_limit >= 100_000:
        tier = "Tier 2"
        semaphore = 3
    else:
        tier = "Tier 1"
        semaphore = 2

    print(f"\n  추정 Tier: {tier}")
    print(f"  → 권장 Semaphore: {semaphore}")
