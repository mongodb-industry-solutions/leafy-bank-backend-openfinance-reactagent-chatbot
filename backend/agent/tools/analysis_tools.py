"""Tools for the analysis agent — financial data retrieval and evaluation."""

import json
import logging
from typing import Optional

import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from config import OPEN_FINANCE_API_BASE

logger = logging.getLogger(__name__)

http_client = httpx.AsyncClient(base_url=OPEN_FINANCE_API_BASE, timeout=30.0)


async def _get_bearer_token(user_id: str) -> str:
    """Get bearer token for a user from the Open Finance backend."""
    response = await http_client.get(
        "/openfinance/public/get-authorization",
        params={"user_identifier": user_id},
    )
    response.raise_for_status()
    return response.json()["BearerToken"]


# ---------- Tool 1: fetch_external_data ----------


@tool
async def fetch_external_data(consent_id: str, config: RunnableConfig) -> str:
    """Fetch all external bank data authorized by a consent. Returns accounts, loans, transactions, repayment history, and customer identification depending on consent permissions.

    Args:
        consent_id: The consent ID authorizing data retrieval
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await _get_bearer_token(user_id)
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
async def calculate_total_balance(config: RunnableConfig) -> str:
    """Calculate the user's aggregated balance across all internal and external accounts."""
    user_id = config["configurable"]["user_id"]
    try:
        token = await _get_bearer_token(user_id)
        response = await http_client.post(
            "/openfinance/secure/calculate-total-balance-for-user/",
            json={"user_id": user_id},
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
async def calculate_total_debt(config: RunnableConfig) -> str:
    """Calculate the user's aggregated debt across all internal and external products. Used for DTI ratio in portability evaluation."""
    user_id = config["configurable"]["user_id"]
    try:
        token = await _get_bearer_token(user_id)
        response = await http_client.post(
            "/openfinance/secure/calculate-total-debt-for-user/",
            json={"user_id": user_id},
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
        token = await _get_bearer_token(user_id)
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
