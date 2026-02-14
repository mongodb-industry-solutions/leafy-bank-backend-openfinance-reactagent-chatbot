import json
import logging
from typing import Optional

import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from config import OPEN_FINANCE_API_BASE

logger = logging.getLogger(__name__)

http_client = httpx.AsyncClient(base_url=OPEN_FINANCE_API_BASE, timeout=30.0)

# Default permissions per consent purpose
PURPOSE_PERMISSIONS = {
    "PERSONAL_LOAN_PORTABILITY": [
        "LOANS_READ",
        "ACCOUNTS_READ",
        "ACCOUNTS_BALANCES_READ",
        "REPAYMENT_HISTORY_READ",
        "CUSTOMER_IDENTIFICATION_READ",
        "TRANSACTIONS_READ",
    ],
    "PAYROLL_LOAN_PORTABILITY": [
        "LOANS_READ",
        "ACCOUNTS_READ",
        "ACCOUNTS_BALANCES_READ",
        "REPAYMENT_HISTORY_READ",
        "CUSTOMER_IDENTIFICATION_READ",
        "TRANSACTIONS_READ",
    ],
    "VEHICLE_LOAN_PORTABILITY": [
        "LOANS_READ",
        "ACCOUNTS_READ",
        "ACCOUNTS_BALANCES_READ",
        "REPAYMENT_HISTORY_READ",
        "CUSTOMER_IDENTIFICATION_READ",
        "TRANSACTIONS_READ",
    ],
    "FINANCIAL_ADVICE": [
        "ACCOUNTS_READ",
        "ACCOUNTS_BALANCES_READ",
        "TRANSACTIONS_READ",
    ],
}


async def _get_bearer_token(user_id: str) -> str:
    """Get bearer token for a user from the Open Finance backend."""
    response = await http_client.get(
        "/openfinance/public/get-authorization",
        params={"user_identifier": user_id},
    )
    response.raise_for_status()
    return response.json()["BearerToken"]


@tool
async def list_institutions() -> str:
    """List all available external banking institutions that users can connect to for data sharing."""
    try:
        response = await http_client.get("/openfinance/secure/institutions/")
        response.raise_for_status()
        data = response.json()
        institutions = data.get("institutions", [])
        names = [inst.get("InstitutionName", "Unknown") for inst in institutions]
        return f"Available institutions: {', '.join(names)}"
    except Exception as e:
        logger.error(f"Error listing institutions: {e}")
        return f"Error fetching institutions: {str(e)}"


@tool
async def get_default_permissions(purpose: str) -> str:
    """Get the default data permissions that will be requested for a given consent purpose.

    Args:
        purpose: The consent purpose. Must be one of: PERSONAL_LOAN_PORTABILITY, PAYROLL_LOAN_PORTABILITY, VEHICLE_LOAN_PORTABILITY, FINANCIAL_ADVICE
    """
    purpose = purpose.upper()
    if purpose not in PURPOSE_PERMISSIONS:
        return f"Unknown purpose '{purpose}'. Valid purposes: {', '.join(PURPOSE_PERMISSIONS.keys())}"

    permissions = PURPOSE_PERMISSIONS[purpose]
    formatted = "\n".join(f"  - {p}" for p in permissions)
    return f"Default permissions for {purpose}:\n{formatted}"


@tool
async def create_consent(
    purpose: str,
    source_institution_name: str,
    expiration_days: int,
    permissions: list[str],
    config: RunnableConfig,
) -> str:
    """Create a new data sharing consent. Only call this AFTER the user has reviewed and confirmed the scope.

    Args:
        purpose: Consent purpose (PERSONAL_LOAN_PORTABILITY, PAYROLL_LOAN_PORTABILITY, VEHICLE_LOAN_PORTABILITY, or FINANCIAL_ADVICE)
        source_institution_name: Name of the external bank to connect to
        expiration_days: How long consent lasts (3-12, or 0 for one-time access)
        permissions: List of approved permissions (e.g. ["LOANS_READ", "ACCOUNTS_READ"])
    """
    user_id = config["configurable"]["user_id"]
    try:
        body = {
            "consumer_id": user_id,
            "purpose": purpose.upper(),
            "source_institution_name": source_institution_name,
            "expiration_days": expiration_days,
            "permissions": permissions,
        }
        response = await http_client.post("/openfinance/secure/consents/", json=body)
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "type": consent.get("ConsentType"),
            "permissions": consent.get("Permissions"),
            "purpose": consent.get("Purpose"),
            "source_institution": consent.get("SourceInstitution", {}).get("InstitutionName"),
            "expiration": consent.get("ExpirationDateTime"),
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Error creating consent: {e.response.text}")
        return f"Error creating consent: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error creating consent: {e}")
        return f"Error creating consent: {str(e)}"


@tool
async def get_consent(consent_id: str) -> str:
    """Get the current details and status of a specific consent.

    Args:
        consent_id: The consent ID (URN format)
    """
    try:
        response = await http_client.get(f"/openfinance/secure/consents/{consent_id}")
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "type": consent.get("ConsentType"),
            "permissions": consent.get("Permissions"),
            "purpose": consent.get("Purpose"),
            "source_institution": consent.get("SourceInstitution", {}).get("InstitutionName"),
            "expiration": consent.get("ExpirationDateTime"),
            "created": consent.get("CreationDateTime"),
        })
    except Exception as e:
        logger.error(f"Error fetching consent: {e}")
        return f"Error fetching consent: {str(e)}"


@tool
async def list_user_consents(config: RunnableConfig) -> str:
    """List all consents for the current user."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.get(
            "/openfinance/secure/consents/",
            params={"consumer_id": user_id},
        )
        response.raise_for_status()
        data = response.json()
        consents = data.get("consents", [])
        if not consents:
            return "No consents found for this user."

        results = []
        for c in consents:
            results.append({
                "consent_id": c.get("ConsentId"),
                "status": c.get("Status"),
                "purpose": c.get("Purpose"),
                "source": c.get("SourceInstitution", {}).get("InstitutionName"),
                "type": c.get("ConsentType"),
            })
        return json.dumps(results)
    except Exception as e:
        logger.error(f"Error listing consents: {e}")
        return f"Error listing consents: {str(e)}"


@tool
async def request_bank_login(consent_id: str, institution_name: str) -> str:
    """Request the user to log in to their external bank. This will pause the conversation and redirect the user to the bank's login page.

    Args:
        consent_id: The consent ID that requires bank authentication
        institution_name: The name of the bank the user needs to log in to
    """
    login_result = interrupt({
        "type": "BANK_LOGIN",
        "consent_id": consent_id,
        "institution_name": institution_name,
    })
    return f"Bank login completed. Result: {json.dumps(login_result)}"


@tool
async def approve_consent(consent_id: str, config: RunnableConfig) -> str:
    """Approve a consent that is in AWAITING_AUTHORISATION status. Only call this after the user explicitly confirms approval.

    Args:
        consent_id: The consent ID to approve
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await _get_bearer_token(user_id)
        response = await http_client.post(
            f"/openfinance/secure/consents/{consent_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "message": "Consent approved successfully.",
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Error approving consent: {e.response.text}")
        return f"Error approving consent: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error approving consent: {e}")
        return f"Error approving consent: {str(e)}"


@tool
async def revoke_consent(consent_id: str, config: RunnableConfig) -> str:
    """Revoke an active consent. This will immediately stop data access from the external bank.

    Args:
        consent_id: The consent ID to revoke
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await _get_bearer_token(user_id)
        response = await http_client.delete(
            f"/openfinance/secure/consents/{consent_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "message": "Consent revoked successfully.",
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Error revoking consent: {e.response.text}")
        return f"Error revoking consent: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        logger.error(f"Error revoking consent: {e}")
        return f"Error revoking consent: {str(e)}"
