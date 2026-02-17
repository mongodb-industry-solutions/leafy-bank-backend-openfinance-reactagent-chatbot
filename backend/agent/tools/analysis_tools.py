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


# ---------- Tool 1: fetch_external_data ----------


@tool
async def fetch_external_data(consent_id: str, config: RunnableConfig) -> str:
    """Fetch all external bank data authorized by a consent. Returns accounts, loans, transactions, repayment history, and customer identification depending on consent permissions.

    Args:
        consent_id: The consent ID authorizing data retrieval
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.get(
            f"/openfinance/secure/customers/{user_id}/external-data",
            params={"consent_id": consent_id},
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


# ---------- Tool 2: fetch_spending_transactions ----------


@tool
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


# ---------- Tool 3: get_spending_best_practices ----------


@tool
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


@tool
async def find_matching_products(
    product_type: str,
    current_rate: float,
    current_amount: Optional[float] = None,
    loan_sub_type: Optional[str] = None,
) -> str:
    """Find Leafy Bank products with better rates than the user's current product.

    Args:
        product_type: Product type to match (Loan or CreditCard)
        current_rate: User's current interest rate to beat
        current_amount: Loan amount for eligibility check
        loan_sub_type: Loan sub-type (Personal, PayrollDeductible, or Vehicle)
    """
    try:
        params = {
            "product_type": product_type,
            "current_rate": current_rate,
        }
        if current_amount is not None:
            params["current_amount"] = current_amount
        if loan_sub_type is not None:
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


# ---------- Tool 8: calculate_total_balance ----------


@tool
async def calculate_total_balance(
    user_object_id: str,
    consent_id: str,
    config: RunnableConfig,
    connected_external_accounts: Optional[list[str]] = None,
) -> str:
    """Calculate the user's aggregated balance across all internal and external accounts.

    Args:
        user_object_id: The user's MongoDB ObjectId (from find_user response _id field)
        consent_id: The consent ID with ACCOUNTS_BALANCES_READ permission
        connected_external_accounts: List of external account IDs to include (from external_data.accounts)
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.post(
            "/openfinance/secure/calculate-total-balance-for-user/",
            json={
                "user_id": user_object_id,
                "connected_external_accounts": connected_external_accounts or [],
                "consent_id": consent_id,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except httpx.HTTPStatusError as e:
        logger.error(f"Error calculating total balance: {e.response.text}")
        return f"Error calculating total balance: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error calculating total balance: {e}")
        return f"Error calculating total balance: {str(e)}"


# ---------- Tool 9: calculate_total_debt ----------


@tool
async def calculate_total_debt(
    user_object_id: str,
    consent_id: str,
    config: RunnableConfig,
    connected_external_products: Optional[list[str]] = None,
) -> str:
    """Calculate the user's aggregated debt across all internal and external products. Used for DTI ratio in portability evaluation.

    Args:
        user_object_id: The user's MongoDB ObjectId (from find_user response _id field)
        consent_id: The consent ID with LOANS_READ permission
        connected_external_products: List of external product IDs to include (from external_data.products)
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.post(
            "/openfinance/secure/calculate-total-debt-for-user/",
            json={
                "user_id": user_object_id,
                "connected_external_products": connected_external_products or [],
                "consent_id": consent_id,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except httpx.HTTPStatusError as e:
        logger.error(f"Error calculating total debt: {e.response.text}")
        return f"Error calculating total debt: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error calculating total debt: {e}")
        return f"Error calculating total debt: {str(e)}"


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


# ---------- Tool 12: calculate_spending_score ----------


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


def _categorize_external_transactions(
    transactions: list[dict], mcc_map: dict[str, dict]
) -> tuple[dict[str, float], float, list[dict]]:
    """Categorize external bank transactions by MCC.

    Returns (category_totals, total_spending, uncategorized_list).
    """
    category_totals: dict[str, float] = {}
    uncategorized = []
    total_spending = 0.0

    for txn in transactions:
        txn_type = txn.get("TransactionType", "")

        # Skip CREDIT (income)
        if txn_type != "DEBIT":
            continue

        amount = txn.get("TransactionAmount", 0)
        total_spending += amount

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
                "bank": txn.get("TransactionBank", ""),
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
async def calculate_spending_score(consent_id: str, config: RunnableConfig) -> str:
    """Calculate a spending health score (0-100) by analyzing ALL transactions from both Leafy Bank (internal) and external banks. Categorizes every transaction by MCC code against spending best practices. Returns the score, per-category breakdown, and full external data (accounts, loans, etc.) so you do not need to call fetch_external_data separately.

    IMPORTANT: This tool consumes the external data consent. Do NOT call fetch_external_data after using this tool — all external data is included in the response.

    Args:
        consent_id: The consent ID authorizing external data retrieval
    """
    user_id = config["configurable"]["user_id"]
    try:
        # Fetch all three data sources concurrently
        token = await get_bearer_token(user_id)

        internal_task = http_client.get(
            f"/leafybank/transactions/secure/spending/{user_id}",
        )
        external_task = http_client.get(
            f"/openfinance/secure/customers/{user_id}/external-data",
            params={"consent_id": consent_id},
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

        # Unwrap API responses:
        # Internal: {"transactions": [...], "total_count": N}
        # External: {"transactions": [...], "accounts": [...], ...}
        # Best practices: {"categories": [...]}
        internal_transactions = internal_data.get("transactions", []) or []
        external_transactions = external_data.get("transactions", []) or []
        best_practices_list = best_practices.get("categories", []) or []

        logger.info(
            f"Fetched {len(internal_transactions)} internal txns, "
            f"{len(external_transactions)} external txns, "
            f"{len(best_practices_list)} categories"
        )

        # Build MCC → category lookup
        mcc_map = _build_mcc_to_category(best_practices_list)

        # Categorize both transaction sets
        int_totals, int_spending, int_uncat = _categorize_internal_transactions(
            internal_transactions, mcc_map
        )
        ext_totals, ext_spending, ext_uncat = _categorize_external_transactions(
            external_transactions, mcc_map
        )

        # Merge category totals
        merged_totals: dict[str, float] = {}
        for cat_id in set(list(int_totals.keys()) + list(ext_totals.keys())):
            merged_totals[cat_id] = int_totals.get(cat_id, 0) + ext_totals.get(cat_id, 0)

        total_spending = int_spending + ext_spending

        # Calculate score and breakdown
        score, breakdown = _calculate_score(merged_totals, total_spending, best_practices_list)

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
            "uncategorized_transactions": int_uncat + ext_uncat,
            "external_data": external_data_for_agent,
        }

        return json.dumps(result)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error calculating spending score: {e.response.text}")
        return f"Error calculating spending score: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error calculating spending score: {e}")
        return f"Error calculating spending score: {str(e)}"
