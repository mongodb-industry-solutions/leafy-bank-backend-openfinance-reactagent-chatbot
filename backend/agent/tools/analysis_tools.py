"""Tools for the analysis agent — financial data retrieval and evaluation."""

import asyncio
import json
import logging
from typing import Optional

import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from http_client import http_client
from agent.tools.auth import get_bearer_token

logger = logging.getLogger(__name__)


# ---------- Internal helper: fetch_external_data ----------
# Not exposed as a tool — calculate_spending_score handles this.


async def fetch_external_data(consent_id: str, config: RunnableConfig) -> str:
    """Fetch all external bank data authorized by a consent. Returns accounts, loans, transactions, repayment history, and customer identification depending on consent permissions."""
    user_id = config["configurable"]["user_id"]
    profile = config["configurable"].get("profile")
    try:
        token = await get_bearer_token(user_id)
        params = {"consent_id": consent_id}
        if profile:
            params["profile"] = profile
        response = await http_client.get(
            f"/openfinance/secure/customers/{user_id}/external-data",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except httpx.HTTPStatusError as e:
        logger.error(f"Error fetching external data: {e.response.text}")
        return f"Error fetching external data: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error fetching external data: {e}")
        return f"Error fetching external data: {str(e)}"


# ---------- Internal helper: fetch_spending_transactions ----------
# Not exposed as a tool — calculate_spending_score handles this.


async def fetch_spending_transactions(config: RunnableConfig) -> str:
    """Fetch ALL transactions for spending analysis. Includes both sent (DEBIT) and received (CREDIT) transactions."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.get(
            f"/leafybank/transactions/secure/spending/{user_id}",
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching spending transactions: {e}")
        return f"Error fetching spending transactions: {str(e)}"


# ---------- Internal helper: get_spending_best_practices ----------
# Not exposed as a tool — calculate_spending_score handles this.


async def get_spending_best_practices() -> str:
    """Get reference data for spending categories with ideal percentage ranges and MCC codes. Use this to categorize transactions and evaluate spending health."""
    try:
        response = await http_client.get("/leafybank/spending/best-practices")
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching spending best practices: {e}")
        return f"Error fetching spending best practices: {str(e)}"


# ---------- Tool 4: fetch_credit_score ----------


@tool
async def fetch_credit_score(config: RunnableConfig) -> str:
    """Fetch the user's credit bureau score. Used for the CreditBureau underwriting path (Personal loans > $1500)."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.get(
            f"/leafybank/customers/{user_id}/credit-score",
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching credit score: {e}")
        return f"Error fetching credit score: {str(e)}"


# ---------- Tool 5: get_underwriting_rules ----------


@tool
async def get_underwriting_rules() -> str:
    """Get loan portability underwriting rules including tier thresholds and rate multipliers for both Spending and CreditBureau paths."""
    try:
        response = await http_client.get(
            "/leafybank/portability/underwriting-rules",
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching underwriting rules: {e}")
        return f"Error fetching underwriting rules: {str(e)}"


# ---------- Tool 6: find_matching_products ----------

# Maps consent purpose → expected loan sub-type for portability validation
_PURPOSE_TO_LOAN_TYPE = {
    "PERSONAL_LOAN_PORTABILITY": "Personal",
    "PAYROLL_LOAN_PORTABILITY": "PayrollDeductible",
    "VEHICLE_LOAN_PORTABILITY": "Vehicle",
}


@tool
async def find_matching_products(
    product_type: str,
    current_rate: float,
    consent_purpose: str,
    loan_sub_type: str,
    current_amount: Optional[float] = None,
) -> str:
    """Find Leafy Bank products with better rates than the user's current product.

    Validates that the loan sub-type from the external data matches the consent
    purpose before searching. Returns a validation error if they don't match.

    Args:
        product_type: Product type to match (Loan or CreditCard)
        current_rate: User's current interest rate to beat
        consent_purpose: The consent purpose (e.g. PERSONAL_LOAN_PORTABILITY)
        loan_sub_type: Loan sub-type from external data (Personal, PayrollDeductible, or Vehicle)
        current_amount: Loan amount for eligibility check
    """
    # Validate loan type matches consent purpose
    expected_type = _PURPOSE_TO_LOAN_TYPE.get(consent_purpose)
    if expected_type and loan_sub_type and loan_sub_type != expected_type:
        return json.dumps({
            "status": "loan_type_mismatch",
            "consent_purpose": consent_purpose,
            "expected_loan_type": expected_type,
            "actual_loan_type": loan_sub_type,
            "message": (
                f"The consent purpose is {consent_purpose} (expects {expected_type} loans), "
                f"but the external loan is {loan_sub_type}. These are different loan types — "
                f"comparing them would produce misleading results. "
                f"Ask the user how they'd like to proceed."
            ),
        })

    try:
        params = {
            "product_type": product_type,
            "current_rate": current_rate,
        }
        if current_amount is not None:
            params["current_amount"] = current_amount
        if loan_sub_type:
            params["loan_sub_type"] = loan_sub_type

        response = await http_client.get(
            "/leafybank/products/secure/match",
            params=params,
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error finding matching products: {e}")
        return f"Error finding matching products: {str(e)}"


# ---------- Tool 7: fetch_internal_accounts ----------


@tool
async def fetch_internal_accounts(config: RunnableConfig) -> str:
    """Fetch all Leafy Bank accounts for the user."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.post(
            "/leafybank/accounts/secure/fetch-accounts-for-user",
            json={"user_identifier": user_id},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching internal accounts: {e}")
        return f"Error fetching internal accounts: {str(e)}"


# ---------- Tool 8: calculate_financial_position ----------


@tool
async def calculate_financial_position(
    user_object_id: str,
    consent_id: str,
    config: RunnableConfig,
    connected_external_accounts: Optional[list[str]] = None,
    connected_external_products: Optional[list[str]] = None,
) -> str:
    """Calculate the user's total balance and total debt in a single call. Returns both aggregated balance (across all internal and external accounts) and aggregated debt (across all internal and external products). Used for DTI ratio in portability evaluation and financial overview.

    Args:
        user_object_id: The user's MongoDB ObjectId (from find_user response _id field)
        consent_id: The consent ID with ACCOUNTS_BALANCES_READ and LOANS_READ permissions
        connected_external_accounts: List of external account IDs to include (from external_data.accounts)
        connected_external_products: List of external product IDs to include (from external_data.products)
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        headers = {"Authorization": f"Bearer {token}"}

        balance_task = http_client.post(
            "/openfinance/secure/calculate-total-balance-for-user/",
            json={
                "user_id": user_object_id,
                "connected_external_accounts": connected_external_accounts or [],
                "consent_id": consent_id,
            },
            headers=headers,
        )
        debt_task = http_client.post(
            "/openfinance/secure/calculate-total-debt-for-user/",
            json={
                "user_id": user_object_id,
                "connected_external_products": connected_external_products or [],
                "consent_id": consent_id,
            },
            headers=headers,
        )

        balance_resp, debt_resp = await asyncio.gather(
            balance_task, debt_task, return_exceptions=True
        )

        result = {}

        if isinstance(balance_resp, Exception):
            logger.error(f"Error calculating total balance: {balance_resp}")
            result["balance_error"] = str(balance_resp)
        else:
            balance_resp.raise_for_status()
            result["total_balance"] = balance_resp.json()

        if isinstance(debt_resp, Exception):
            logger.error(f"Error calculating total debt: {debt_resp}")
            result["debt_error"] = str(debt_resp)
        else:
            debt_resp.raise_for_status()
            result["total_debt"] = debt_resp.json()

        return json.dumps(result)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error calculating financial position: {e.response.text}")
        return f"Error calculating financial position: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error calculating financial position: {e}")
        return f"Error calculating financial position: {str(e)}"


# ---------- Tool 10: fetch_customer_identification ----------


@tool
async def fetch_customer_identification(
    consent_id: str, config: RunnableConfig
) -> str:
    """Fetch external customer identification (KYC) data. Required by underwriting rules for portability evaluation.

    Args:
        consent_id: The consent ID with CUSTOMER_IDENTIFICATION_READ permission
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.get(
            f"/leafybank/customers/{user_id}/identification",
            params={"consent_id": consent_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except httpx.HTTPStatusError as e:
        logger.error(f"Error fetching customer identification: {e.response.text}")
        return f"Error fetching customer identification: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error fetching customer identification: {e}")
        return f"Error fetching customer identification: {str(e)}"


# ---------- Tool 11: find_user ----------


@tool
async def find_user(config: RunnableConfig) -> str:
    """Look up the Leafy Bank internal user profile (name, email, etc.)."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.post(
            "/leafybank/users/secure/find-user",
            json={"user_identifier": user_id},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error finding user: {e}")
        return f"Error finding user: {str(e)}"


# ---------- Tool: analyze_spending ----------


def _build_mcc_to_category(best_practices: list[dict]) -> dict[str, dict]:
    """Build a mapping from MCC code → category info."""
    mcc_map = {}
    for category in best_practices:
        for mcc in category.get("MCCCodes", []):
            mcc_map[mcc] = {
                "CategoryId": category["CategoryId"],
                "CategoryName": category["CategoryName"],
                "IdealPercentage": category["IdealPercentage"],
                "MinPercentage": category["MinPercentage"],
                "MaxPercentage": category["MaxPercentage"],
            }
    return mcc_map


def _categorize_internal_transactions(
    transactions: list[dict], mcc_map: dict[str, dict]
) -> tuple[dict[str, float], float, list[dict]]:
    """Categorize Leafy Bank transactions by MCC and internal transfers.

    Returns (category_totals, total_spending, uncategorized_list).
    """
    category_totals: dict[str, float] = {}
    uncategorized = []
    total_spending = 0.0

    for txn in transactions:
        credit_debit = txn.get("TransactionCreditDebitType", "")
        details = txn.get("TransactionDetails", {})
        txn_type = details.get("TransactionType", "")
        is_internal = details.get("TransactionInternal", False)
        amount = txn.get("TransactionAmount", 0)

        # Internal transfers to savings
        if txn_type == "AccountTransfer" and is_internal:
            category_totals["savings"] = category_totals.get("savings", 0) + amount
            total_spending += amount
            continue

        # Skip CREDIT (income) and non-DEBIT
        if credit_debit != "DEBIT":
            continue

        total_spending += amount

        # Categorize by MCC
        merchant = txn.get("TransactionMerchant", {})
        mcc = merchant.get("MCC", "")
        if mcc and mcc in mcc_map:
            cat_id = mcc_map[mcc]["CategoryId"]
            category_totals[cat_id] = category_totals.get(cat_id, 0) + amount
        else:
            uncategorized.append({
                "description": txn.get("TransactionDescription", ""),
                "amount": amount,
                "merchant": merchant.get("MerchantName", ""),
                "mcc": mcc,
            })

    return category_totals, total_spending, uncategorized


def _normalize_external_transaction(txn: dict) -> dict:
    """Normalize an ISO 20022-aligned external transaction to a flat working format."""
    return {
        "amount": txn.get("Amt", {}).get("value", 0),
        "currency": txn.get("Amt", {}).get("Ccy", "USD"),
        "direction": txn.get("CdtDbtInd", ""),
        "description": txn.get("AddtlNtryInf", ""),
        "merchant_name": txn.get("Cdtr", {}).get("Nm", ""),
        "mcc": txn.get("BkTxCd", {}).get("Prtry", {}).get("Cd", ""),
        "bank": txn.get("Acct", {}).get("Svcr", ""),
        "date": txn.get("BookgDt"),
        "purpose": txn.get("Purp", {}).get("Cd", ""),
    }


def _categorize_external_transactions(
    transactions: list[dict], mcc_map: dict[str, dict]
) -> tuple[dict[str, float], float, list[dict]]:
    """Categorize external bank transactions (ISO 20022 format) by MCC.

    Returns (category_totals, total_spending, uncategorized_list).
    """
    category_totals: dict[str, float] = {}
    uncategorized = []
    total_spending = 0.0

    for txn in transactions:
        norm = _normalize_external_transaction(txn)

        # Skip CREDIT (income)
        if norm["direction"] != "DBIT":
            continue

        amount = norm["amount"]
        total_spending += amount

        mcc = norm["mcc"]
        if mcc and mcc in mcc_map:
            cat_id = mcc_map[mcc]["CategoryId"]
            category_totals[cat_id] = category_totals.get(cat_id, 0) + amount
        else:
            uncategorized.append({
                "description": norm["description"],
                "amount": amount,
                "merchant": norm["merchant_name"],
                "mcc": mcc,
                "bank": norm["bank"],
            })

    return category_totals, total_spending, uncategorized


def _calculate_score(
    category_totals: dict[str, float],
    total_spending: float,
    best_practices: list[dict],
) -> tuple[int, list[dict]]:
    """Calculate a 0-100 spending score and per-category breakdown.

    Scoring: For each category, if actual % is within [min, max] range → 100.
    Each percentage point outside the range costs 5 points for that category.
    Final score is a weighted average using ideal percentages as weights.
    """
    breakdown = []
    weighted_score_sum = 0.0
    total_weight = 0.0

    for category in best_practices:
        cat_id = category["CategoryId"]
        ideal = category["IdealPercentage"]
        min_pct = category["MinPercentage"]
        max_pct = category["MaxPercentage"]
        cat_total = category_totals.get(cat_id, 0)

        actual_pct = (cat_total / total_spending * 100) if total_spending > 0 else 0
        actual_pct = round(actual_pct, 1)

        # Calculate category score
        if min_pct <= actual_pct <= max_pct:
            cat_score = 100
            status = "on_track"
        elif actual_pct < min_pct:
            cat_score = max(0, 100 - (min_pct - actual_pct) * 5)
            status = "under_budget"
        else:
            cat_score = max(0, 100 - (actual_pct - max_pct) * 5)
            status = "over_budget"

        weighted_score_sum += cat_score * ideal
        total_weight += ideal

        breakdown.append({
            "category_id": cat_id,
            "category_name": category["CategoryName"],
            "actual_amount": round(cat_total, 2),
            "actual_percentage": actual_pct,
            "ideal_percentage": ideal,
            "min_percentage": min_pct,
            "max_percentage": max_pct,
            "category_score": round(cat_score, 1),
            "status": status,
        })

    final_score = round(weighted_score_sum / total_weight) if total_weight > 0 else 0
    return final_score, breakdown


@tool
async def analyze_spending(consent_id: str, config: RunnableConfig) -> str:
    """Analyze spending health across all bank accounts. Fetches transactions from both Leafy Bank (internal) and external banks, classifies any uncategorized transactions using MongoDB Atlas Vector Search, and returns a final spending score (0-100) with per-category breakdown. All external data (accounts, loans, repayment history) is included in the response.

    Args:
        consent_id: The consent ID authorizing external data retrieval
    """
    user_id = config["configurable"]["user_id"]
    profile = config["configurable"].get("profile")
    try:
        # --- Step 1: Fetch all data sources concurrently ---
        token = await get_bearer_token(user_id)

        internal_task = http_client.get(
            f"/leafybank/transactions/secure/spending/{user_id}",
        )
        external_params = {"consent_id": consent_id}
        if profile:
            external_params["profile"] = profile
        external_task = http_client.get(
            f"/openfinance/secure/customers/{user_id}/external-data",
            params=external_params,
            headers={"Authorization": f"Bearer {token}"},
        )
        best_practices_task = http_client.get("/leafybank/spending/best-practices")

        internal_resp, external_resp, bp_resp = await asyncio.gather(
            internal_task, external_task, best_practices_task
        )

        internal_resp.raise_for_status()
        external_resp.raise_for_status()
        bp_resp.raise_for_status()

        internal_data = internal_resp.json()
        external_data = external_resp.json()
        best_practices = bp_resp.json()

        internal_transactions = internal_data.get("transactions", []) or []
        external_transactions = external_data.get("transactions", []) or []
        best_practices_list = best_practices.get("categories", []) or []

        logger.info(
            f"Fetched {len(internal_transactions)} internal txns, "
            f"{len(external_transactions)} external txns, "
            f"{len(best_practices_list)} categories"
        )

        # --- Step 2: Categorize all transactions by MCC ---
        mcc_map = _build_mcc_to_category(best_practices_list)

        int_totals, int_spending, int_uncat = _categorize_internal_transactions(
            internal_transactions, mcc_map
        )
        ext_totals, ext_spending, ext_uncat = _categorize_external_transactions(
            external_transactions, mcc_map
        )

        merged_totals: dict[str, float] = {}
        for cat_id in set(list(int_totals.keys()) + list(ext_totals.keys())):
            merged_totals[cat_id] = int_totals.get(cat_id, 0) + ext_totals.get(cat_id, 0)

        total_spending = int_spending + ext_spending
        uncategorized = int_uncat + ext_uncat

        # --- Step 3: Classify uncategorized transactions via vector search ---
        classification_summary = {
            "total_uncategorized": len(uncategorized),
            "newly_classified": 0,
            "still_uncategorized": len(uncategorized),
        }

        if uncategorized:
            try:
                classify_resp = await http_client.post(
                    "/leafybank/mcc/classify",
                    json={"transactions": uncategorized},
                )
                classify_resp.raise_for_status()
                classifications = classify_resp.json().get("classifications", [])

                # Merge classified amounts into category totals
                newly_classified = 0
                still_uncat = 0
                for txn in classifications:
                    cat_id = txn.get("CategoryId", "")
                    amount = txn.get("amount", 0)

                    if cat_id and cat_id != "uncategorized":
                        merged_totals[cat_id] = merged_totals.get(cat_id, 0) + amount
                        newly_classified += 1
                    else:
                        still_uncat += 1

                classification_summary["newly_classified"] = newly_classified
                classification_summary["still_uncategorized"] = still_uncat

                logger.info(
                    f"Classified {newly_classified}/{len(uncategorized)} transactions"
                )

            except Exception as e:
                logger.warning(f"Classification failed, scoring without it: {e}")

        # --- Step 4: Calculate final score (post-classification) ---
        score, breakdown = _calculate_score(
            merged_totals, total_spending, best_practices_list
        )

        # Build external data without transactions (already processed)
        external_data_for_agent = {
            k: v for k, v in external_data.items() if k != "transactions"
        }

        result = {
            "spending_score": score,
            "total_spending": round(total_spending, 2),
            "internal_transaction_count": len(internal_transactions),
            "external_transaction_count": len(external_transactions),
            "category_breakdown": breakdown,
            "classification_summary": classification_summary,
            "external_data": external_data_for_agent,
        }

        return json.dumps(result)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error analyzing spending: {e.response.text}")
        return f"Error analyzing spending: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error analyzing spending: {e}")
        return f"Error analyzing spending: {str(e)}"
