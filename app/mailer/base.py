from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SendResult:
    message_id: str
    thread_id: str | None = None


class MailError(Exception):
    pass


class MailAuthError(MailError):
    """Credentials revoked/expired; the user must reconnect the account."""


class EmailProvider(ABC):
    """Everything Gmail-specific stays behind this interface."""
    name = "base"

    @abstractmethod
    def send(self, account: dict, to: str, subject: str, body: str, from_name: str | None = None) -> SendResult:
        ...
