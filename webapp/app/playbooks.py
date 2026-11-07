"""Decline-code playbooks: what to do per failure, per RBI rules."""
import time

# RBI e-mandate retry windows: retries allowed at ~24h, 72h and day 7.
RBI_RETRY_OFFSETS_H = [24, 72, 168]

PLAYBOOKS = {
    "INSUFFICIENT_FUNDS": "funds",
    "MANDATE_PAUSED": "repair",
    "MANDATE_EXPIRED": "repair",
    "AUTHENTICATION_REQUIRED": "afa",
    "CARD_EXPIRED": "renew_card",
    "BANK_DOWN": "technical",
    "TECHNICAL_ERROR": "technical",
    "DO_NOT_HONOR": "nudge",
    "UNKNOWN": "nudge",
}

RETRY_OFFSETS = {
    "funds": RBI_RETRY_OFFSETS_H,      # salary-week aligned in the scheduler
    "nudge": [24, 72, 168],
    "technical": [0, 1, 4],            # immediate, +1h, +4h
    "repair": [],                      # never retry; human must repair the mandate
    "afa": [],                         # never retry blind; send auth link
    "renew_card": [],
}

TEMPLATES = {
    "funds": "Hi {name}, your {brand} renewal of Rs {amount} didn't go through "
             "(low balance). Top up and tap to pay now: {link}",
    "repair": "Hi {name}, your UPI Autopay for {brand} needs one quick approval to "
              "stay active. Set it up in 30 seconds: {link}",
    "afa": "Hi {name}, your bank needs you to approve the {brand} payment of "
           "Rs {amount}. Approve here: {link}",
    "nudge": "Hi {name}, your {brand} payment didn't go through. One tap to retry: {link}",
    "renew_card": "Hi {name}, your card saved for {brand} is expiring. Update it "
                  "here so nothing breaks: {link}",
}


def playbook_for(decline_code: str) -> str:
    return PLAYBOOKS.get((decline_code or "UNKNOWN").upper().replace(" ", "_"), "nudge")


def retry_offsets_for(playbook: str) -> list[int]:
    return RETRY_OFFSETS.get(playbook, [])


def render_message(playbook: str, *, name: str, brand: str, amount: float, link: str) -> str:
    return TEMPLATES[playbook].format(name=name, brand=brand, amount=amount, link=link)


def next_retry_at(playbook: str, from_ts: float | None = None) -> list[float]:
    base = from_ts if from_ts is not None else time.time()
    return [base + h * 3600 for h in retry_offsets_for(playbook)]
