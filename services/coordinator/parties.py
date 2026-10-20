"""The coordinator's directory of onboarded companies (demo values).

In production this would come from KYC onboarding: legal identity, tax id, verified bank account and the
addresses of each company's authorized approver. Here it is static example data.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.common.config import Settings
from services.common.keys import address_for


@dataclass(frozen=True)
class Party:
    name: str
    role: str  # "supplier" | "buyer" (also the signer role)
    tax_id: str
    bank_account: str

    def signer(self, settings: Settings | None = None) -> str:
        return address_for(self.role, settings)


PARTIES = {
    "Alpha Foods": Party("Alpha Foods", "supplier", "TR-DEMO-1000000001", "SBX-TR-ALPHA-0001"),
    "Beta Markets": Party("Beta Markets", "buyer", "TR-DEMO-2000000002", "SBX-TR-BETA-0001"),
}


def get(name: str) -> Party | None:
    return PARTIES.get(name)
