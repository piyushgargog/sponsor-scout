import uuid

from .base import EmailProvider, SendResult


class MockMailer(EmailProvider):
    """Demo mode: records the message in memory and 'sends' nothing."""
    name = "mock"

    def __init__(self):
        self.outbox = []

    def send(self, account, to, subject, body, from_name=None):
        mid = f"mock-{uuid.uuid4().hex[:12]}"
        self.outbox.append({"id": mid, "to": to, "subject": subject, "body": body, "from": account["email"]})
        return SendResult(mid, f"thread-{mid}")
