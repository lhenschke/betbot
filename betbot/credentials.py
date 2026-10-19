"""Book login details, kept in the OS credential store (Windows Credential Manager)."""

from __future__ import annotations

import getpass

import keyring

SERVICE = "betbot"


def _key(book: str, account: str) -> str:
    return f"{book}:{account or 'default'}"


def set_credentials(book: str, account: str, email: str, password: str) -> None:
    keyring.set_password(SERVICE, _key(book, account) + ":email", email)
    keyring.set_password(SERVICE, _key(book, account) + ":password", password)


def get_credentials(book: str, account: str) -> tuple[str, str] | None:
    email = keyring.get_password(SERVICE, _key(book, account) + ":email")
    password = keyring.get_password(SERVICE, _key(book, account) + ":password")
    if email and password:
        return email, password
    return None


def prompt_and_store(book: str, account: str) -> None:
    email = input(f"{book} login email/username: ").strip()
    password = getpass.getpass(f"{book} password: ")
    set_credentials(book, account, email, password)
    print(f"Saved {book} credentials for account '{account or 'default'}' in the system credential store.")
