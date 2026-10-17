#!/usr/bin/env bash
# ==============================================================================
# ComputeCoin — Staging & Production Smoke Test
# ==============================================================================
# Usage:
#   ./scripts/smoke-test.sh [API_URL]
# Example:
#   ./scripts/smoke-test.sh http://localhost:8000
#   ./scripts/smoke-test.sh https://api-staging.computecoin.ai
# ==============================================================================

set -euo pipefail

API_URL="${1:-http://localhost:8000}"
API_URL="${API_URL%/}" # Remove trailing slash

echo "========================================================"
echo " 🔍 ComputeCoin API Smoke Test: $API_URL"
echo "========================================================"

FAILED=0
TEST_EMAIL="smoke_$(date +%s)_$RANDOM@computecoin.ai"
TEST_PASSWORD="TestPassword123!@#"
COOKIE_JAR=$(mktemp)
trap 'rm -f "$COOKIE_JAR"' EXIT

run_check() {
  local name="$1"
  local method="$2"
  local endpoint="$3"
  local expected_status="$4"
  local data="${5:-}"

  local start_time
  start_time=$(date +%s%N)

  local http_code
  if [ -n "$data" ]; then
    http_code=$(curl -s -o /tmp/smoke_out.json -w "%{http_code}" -X "$method" \
      -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
      -H "Content-Type: application/json" \
      -d "$data" \
      "$API_URL$endpoint" || echo "000")
  else
    http_code=$(curl -s -o /tmp/smoke_out.json -w "%{http_code}" -X "$method" \
      -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
      "$API_URL$endpoint" || echo "000")
  fi

  local end_time
  end_time=$(date +%s%N)
  local duration_ms=$(( (end_time - start_time) / 1000000 ))

  if [ "$http_code" -eq "$expected_status" ]; then
    echo "  ✅ [PASS] $name ($endpoint) - ${http_code} in ${duration_ms}ms"
  else
    echo "  ❌ [FAIL] $name ($endpoint) - expected $expected_status, got $http_code in ${duration_ms}ms"
    echo "       Response: $(cat /tmp/smoke_out.json 2>/dev/null | head -c 200)"
    FAILED=$((FAILED + 1))
  fi
}

echo -e "\n1. Basic Connectivity & Health:"
run_check "Root Service Info" "GET" "/" 200
run_check "Health Check (DB + Services)" "GET" "/health" 200

echo -e "\n2. Public Catalog & Smart Contract Registry:"
run_check "GPU Marketplace Catalog" "GET" "/v1/marketplace/machines" 200
run_check "Settlement Contract Registry" "GET" "/v1/settlement/contracts" 200

echo -e "\n3. Authentication & Account Creation:"
run_check "User Registration" "POST" "/auth/register" 201 "{\"email\":\"$TEST_EMAIL\",\"password\":\"$TEST_PASSWORD\"}"
run_check "User Login (Sets Cookies)" "POST" "/auth/login" 200 "{\"email\":\"$TEST_EMAIL\",\"password\":\"$TEST_PASSWORD\"}"

# Extract CSRF token from response
CSRF_TOKEN=$(grep -o '"csrf_token":"[^"]*"' /tmp/smoke_out.json 2>/dev/null | cut -d'"' -f4 || echo "")
ACCESS_TOKEN=$(grep -o '"access_token":"[^"]*"' /tmp/smoke_out.json 2>/dev/null | cut -d'"' -f4 || echo "")

echo -e "\n4. Authenticated Endpoints:"
if [ -n "$ACCESS_TOKEN" ]; then
  # Check /auth/me with Bearer token
  AUTH_CODE=$(curl -s -o /tmp/smoke_out.json -w "%{http_code}" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    "$API_URL/auth/me" || echo "000")
  if [ "$AUTH_CODE" -eq 200 ]; then
    echo "  ✅ [PASS] Current User Profile (/auth/me) - 200 OK"
  else
    echo "  ❌ [FAIL] Current User Profile (/auth/me) - got $AUTH_CODE"
    FAILED=$((FAILED + 1))
  fi

  # Check billing balance
  BILLING_CODE=$(curl -s -o /tmp/smoke_out.json -w "%{http_code}" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    "$API_URL/v1/billing/account" || echo "000")
  if [ "$BILLING_CODE" -eq 200 ]; then
    echo "  ✅ [PASS] Billing Account (/v1/billing/account) - 200 OK"
  else
    echo "  ❌ [FAIL] Billing Account (/v1/billing/account) - got $BILLING_CODE"
    FAILED=$((FAILED + 1))
  fi
fi

echo -e "\n========================================================"
if [ "$FAILED" -eq 0 ]; then
  echo " 🎉 All Smoke Tests PASSED! Deployment is healthy and responsive."
  echo "========================================================"
  exit 0
else
  echo " ❌ $FAILED check(s) FAILED. Inspect error logs above."
  echo "========================================================"
  exit 1
fi
