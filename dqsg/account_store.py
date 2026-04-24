from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


ACCOUNT_STORE_PATH = Path(__file__).resolve().parent.parent / ".dqsg_accounts.json"


class AccountStoreError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_store() -> dict:
    return {
        "version": 2,
        "accounts": {},
    }


def _normalize_hex(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _hydrate_record(saved: dict) -> dict:
    credentials = saved.get("credentials")
    if not isinstance(credentials, dict):
        raise AccountStoreError("Account record is missing credentials")

    record = dict(saved)
    record["client_uuid"] = credentials.get("client_uuid")
    record["stored_key"] = credentials.get("stored_key")
    if "terminal_id" in credentials:
        record["terminal_id"] = credentials.get("terminal_id")
    if "startup_random" in credentials:
        record["startup_random"] = credentials.get("startup_random")
    if "authorization_key" in credentials:
        record["authorization_key"] = credentials.get("authorization_key")
    if "login_key" in credentials:
        record["login_key"] = credentials.get("login_key")
    return record


def load_store(path: str | Path = ACCOUNT_STORE_PATH) -> dict:
    store_path = Path(path)
    if not store_path.exists():
        return _default_store()

    data = json.loads(store_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AccountStoreError(f"Invalid account store format: {store_path}")

    if data.get("version") != 2:
        raise AccountStoreError(
            f"Unsupported account store version in {store_path}. Expected version 2."
        )

    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        raise AccountStoreError(f"Invalid account store accounts section: {store_path}")

    return data


def save_account(
    record: dict,
    *,
    path: str | Path = ACCOUNT_STORE_PATH,
    label: str | None = None,
    progress: str | None = None,
    last_command: str | None = None,
    snapshot: dict | None = None,
) -> dict:
    if not record.get("user_id"):
        raise AccountStoreError("Cannot save account without user_id")
    if not record.get("stored_key"):
        raise AccountStoreError("Cannot save account without stored_key")
    if not record.get("client_uuid"):
        raise AccountStoreError("Cannot save account without client_uuid")

    store_path = Path(path)
    store = load_store(store_path)
    accounts = store.setdefault("accounts", {})

    user_id = int(record["user_id"])
    account_id = str(user_id)
    now = _now_iso()

    existing = accounts.get(account_id, {})
    existing_snapshot = existing.get("snapshot")
    if not isinstance(existing_snapshot, dict):
        existing_snapshot = {}
    merged_snapshot = dict(existing_snapshot)
    if snapshot:
        for key, value in snapshot.items():
            if value is None:
                merged_snapshot.pop(key, None)
            else:
                merged_snapshot[key] = value

    existing_credentials = existing.get("credentials")
    if not isinstance(existing_credentials, dict):
        existing_credentials = {}
    credentials = dict(existing_credentials)
    credentials["client_uuid"] = str(record["client_uuid"])
    credentials["stored_key"] = _normalize_hex(record["stored_key"])
    for key in ("terminal_id", "startup_random", "authorization_key", "login_key"):
        if key in record:
            value = record.get(key)
            if value is None:
                credentials.pop(key, None)
            else:
                credentials[key] = _normalize_hex(value)

    existing_provenance = existing.get("provenance")
    if not isinstance(existing_provenance, dict):
        existing_provenance = {}
    provenance = dict(existing_provenance)
    for key in ("source", "imported_at", "crypto_log_ref", "device_container_id", "request_ids"):
        if key in record:
            value = record.get(key)
            if value is None:
                provenance.pop(key, None)
            else:
                provenance[key] = value

    saved = {
        "user_id": user_id,
        "label": label if label is not None else existing.get("label"),
        "progress": progress if progress is not None else existing.get("progress"),
        "last_command": last_command if last_command is not None else existing.get("last_command"),
        "credentials": credentials,
        "snapshot": merged_snapshot,
        "provenance": provenance,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }

    accounts[account_id] = saved
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _hydrate_record(saved)


def list_accounts(path: str | Path = ACCOUNT_STORE_PATH) -> list[dict]:
    store = load_store(path)
    accounts = store.get("accounts", {})
    hydrated = [_hydrate_record(item) for item in accounts.values()]
    return sorted(
        hydrated,
        key=lambda item: (item.get("updated_at") or "", int(item["user_id"])),
        reverse=True,
    )


def resolve_account(account_ref: str, path: str | Path = ACCOUNT_STORE_PATH) -> dict:
    store = load_store(path)
    accounts = store.get("accounts", {})
    if not accounts:
        raise AccountStoreError(f"No saved accounts in {Path(path)}")

    if account_ref == "latest":
        latest = list_accounts(path)
        if latest:
            return latest[0]

    direct = accounts.get(str(account_ref))
    if direct:
        return _hydrate_record(direct)

    matches = [
        _hydrate_record(record)
        for record in accounts.values()
        if record.get("label") == account_ref
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AccountStoreError(
            f"Multiple accounts use label '{account_ref}'. Use user_id instead."
        )

    raise AccountStoreError(
        f"Unknown account '{account_ref}'. Use `python3 client.py accounts` to list saved accounts."
    )


def delete_account_record(account_ref: str, path: str | Path = ACCOUNT_STORE_PATH) -> dict:
    store_path = Path(path)
    store = load_store(store_path)
    accounts = store.get("accounts", {})
    record = resolve_account(account_ref, path=store_path)
    account_id = str(int(record["user_id"]))
    existing = accounts.pop(account_id, None)
    if existing is None:
        raise AccountStoreError(f"Account '{account_ref}' was not found in {store_path}")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _hydrate_record(existing)
