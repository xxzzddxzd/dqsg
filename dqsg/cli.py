from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import struct
import time
from pathlib import Path
import requests
import urllib3

from .account_store import (
    ACCOUNT_STORE_PATH,
    AccountStoreError,
    delete_account_record,
    list_accounts,
    resolve_account,
    resolve_last_selected_account,
    save_account,
    set_last_selected_account,
)
from .client import DQSGClient
from .crypto import STARTUP_KEY, xor_bytes
from .parsers import (
    FEATURE_INTRO_HOME_MENU,
    FEATURE_INTRO_STAGE_INFO,
    TUTORIAL_STEP_AVATAR_EDIT,
    TUTORIAL_STEP_GACHA,
    TUTORIAL_STEP_RESUME_GACHA_RESULT,
    TUTORIAL_STEP_RESUME_HOME_UNLOCK,
    TUTORIAL_STEP_RESUME_PREV_DECK_EDIT,
    TUTORIAL_STEP_RESUME_PREV_GACHA,
    TUTORIAL_STEP_RESUME_PREV_HOME_UNLOCK,
    TUTORIAL_STEP_RESUME_PREV_STAGE_FIRST,
    TUTORIAL_STEP_STAGE_FIRST,
    TUTORIAL_STEP_STAGE_PROLOGUE,
    TUTORIAL_STEP_VOICE_SETTING,
    GACHA_METAL_10,
    GACHA_NORMAL_10,
    CONTENT_TYPE_ARMOR,
    CONTENT_TYPE_WEAPON,
    equipment_rarity,
    equipment_display_name,
    equipment_is_metal,
)
from .serialization import BytesReader, BytesWriter

urllib3.disable_warnings()

_TRACKED_GROWTH_MATERIAL_IDS = (
    110430001,
    110420001,
    110410001,
)
_DAILY_HOME_DEVICE_NAME = "iPhone"
_DAILY_HOME_DEVICE_TOKEN = (
    "cMh2rR7hHkr3iUsvV5YyMa:"
    "APA91bE1hTj2f0VY0qqjmThuO709X0Agk39KxzBCp8JPN5oN6trA8Ca1fgHo1AQLkViZjBohSZ8kY6cz"
    "Cf0yT4LcBoLTK9PHYiAnmKG2BZehDZLksDadyY0"
)
_DAILY_HOME_FIREBASE_ID = "3F2B3486E7DA4BADAAC2225E2B5FC775"
_DAILY_HOME_ADJUST_ID = "9027cf9dd1927e65818a7cc24bce9e71"
_DAILY_EXPEDITION_ID = 1
_DAILY_EXPEDITION_MASTER_ID = 105
_DAILY_EXPEDITION_USER_STYLE_ID = 0
_AD_STORE_EXCHANGES = [
    (104000302, 1),
    (104000301, 1),
    (2, 2),
    (3, 1),
]
_DAILY_NOTICE_IDS = [
    89784,
    23615,
    91104,
    87418,
    86697,
    8306,
    53147,
    27765,
    70390,
    83110,
    76046,
    64769,
    71485,
    74792,
    62105,
    2509,
    17572,
    13359,
    32632,
    13596,
    48176,
    19409,
    7556,
    68245,
    80809,
    62197,
    90076,
]

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _color(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


def _status_text(status: int) -> str:
    text = f"status={status}"
    return _color(text, _GREEN if status == 1 else _RED)


def _check(resp, step_name):
    status = resp.get("_status")
    if status != 1:
        raise RuntimeError(f"[{step_name}] failed: {_status_text(status)}, resp={resp}")


def _release_function_unlock_allow_500(client, function_id: int):
    step_name = f"release_function/unlock({function_id})"
    try:
        resp = client.release_function_unlock(function_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 500:
            print(f"  [release_function] {step_name} hit {_color('HTTP 500', _RED)}; continuing")
            return None
        raise
    _check(resp, step_name)
    return resp


def _looks_like_counted_int_pair_list(data: bytes, entry_pos: int) -> bool:
    for entry_index in range(64):
        count_pos = entry_pos - 4 - (entry_index * 8)
        if count_pos < 0:
            break
        count = struct.unpack_from("<i", data, count_pos)[0]
        if not (entry_index < count <= 256):
            continue
        end_pos = count_pos + 4 + (count * 8)
        if end_pos > len(data):
            continue
        valid = True
        for idx in range(count):
            master_id = struct.unpack_from("<i", data, count_pos + 4 + (idx * 8))[0]
            amount = struct.unpack_from("<i", data, count_pos + 8 + (idx * 8))[0]
            if master_id <= 0 or master_id > 500_000_000 or amount < 0 or amount > 1_000_000:
                valid = False
                break
        if valid:
            return True
    return False


def _parse_growth_material_amounts_from_login_response(
    login_response_raw: bytes,
    target_ids: tuple[int, ...] = _TRACKED_GROWTH_MATERIAL_IDS,
) -> dict[int, int]:
    amounts = {master_id: 0 for master_id in target_ids}
    for master_id in target_ids:
        needle = struct.pack("<i", master_id)
        start = 0
        while True:
            pos = login_response_raw.find(needle, start)
            if pos == -1:
                break
            start = pos + 1
            if pos + 8 > len(login_response_raw):
                continue
            amount = struct.unpack_from("<i", login_response_raw, pos + 4)[0]
            if not (0 <= amount <= 1_000_000):
                continue
            if not _looks_like_counted_int_pair_list(login_response_raw, pos):
                continue
            amounts[master_id] = amount
            break
    return amounts


def _store_path(args) -> str:
    return args.accounts_file


def _debug_enabled(args) -> bool:
    return bool(getattr(args, "debug", False))


def _configure_client_debug(client: DQSGClient, args):
    client.debug = _debug_enabled(args)
    client.configure_proxy(
        proxy_url=getattr(args, "proxy", None),
        proxy_api_url=getattr(args, "proxy_api", None),
        country=getattr(args, "proxy_country", None),
        proxy_auto=getattr(args, "proxy_auto", None),
    )
    return client


def _account_ref(record: dict) -> str:
    return record.get("label") or str(record["user_id"])


def _create_saved_account(args) -> dict:
    client = DQSGClient.new_account()
    _configure_client_debug(client, args)
    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")
    print("\n=== login/startup ===")
    creds = client.login_startup()
    saved = _save_client_account(client, args, progress="registered", last_command="register")
    print(f"  created user_id = {creds['user_id']}")
    _print_saved_account(saved, _store_path(args))
    return saved


def _mark_last_selected_account(record: dict, args):
    try:
        set_last_selected_account(record["user_id"], path=_store_path(args))
    except AccountStoreError as exc:
        raise SystemExit(str(exc)) from exc


def _resolve_account_arg_or_prompt(args) -> dict:
    if getattr(args, "account", None):
        try:
            record = resolve_account(args.account, path=_store_path(args))
            _mark_last_selected_account(record, args)
            return record
        except AccountStoreError as exc:
            raise SystemExit(str(exc)) from exc

    try:
        record = resolve_last_selected_account(path=_store_path(args))
    except AccountStoreError as exc:
        raise SystemExit(str(exc)) from exc
    if record is not None:
        return record

    try:
        records = list_accounts(path=_store_path(args))
    except AccountStoreError as exc:
        raise SystemExit(str(exc)) from exc

    print("Select account:")
    for idx, record in enumerate(records, start=1):
        label = record.get("label") or "-"
        progress = record.get("progress") or "-"
        print(f"  {idx}. {record['user_id']}  label={label}  progress={progress}")
    print(f"  {len(records) + 1}. 新增账号")

    raw = input("\nEnter number: ").strip()
    if not raw.isdigit():
        raise SystemExit("Invalid selection.")
    selected = int(raw)
    if 1 <= selected <= len(records):
        record = records[selected - 1]
        _mark_last_selected_account(record, args)
        return record
    if selected == len(records) + 1:
        return _create_saved_account(args)
    raise SystemExit("Selection out of range.")


def _save_client_account(client: DQSGClient, args, *, progress=None, last_command=None, snapshot=None):
    try:
        saved = save_account(
            client.export_account_record(),
            path=_store_path(args),
            label=getattr(args, "label", None),
            progress=progress,
            last_command=last_command,
            snapshot=snapshot,
        )
        _mark_last_selected_account(saved, args)
        return saved
    except (AccountStoreError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _load_client_for_account(args):
    record = _resolve_account_arg_or_prompt(args)
    client = DQSGClient.from_account_record(record)
    _configure_client_debug(client, args)
    return client, record


def _print_saved_account(record: dict, store_path: str):
    print(f"  saved_account = {_account_ref(record)}")
    print(f"  user_id       = {record['user_id']}")
    print(f"  store_file    = {store_path}")


def _read_import_text(args) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    parts = getattr(args, "log_text", None) or []
    text = " ".join(parts).strip()
    if not text:
        raise SystemExit("Provide log text or use --file.")
    return text


def _derive_login_key_from_import_log(text: str, stored_key_hex: str) -> str | None:
    xor_lines = re.findall(r"XorBytes.*?L \(32 bytes\) = ([0-9a-f]{64}).*?R \(32 bytes\) = ([0-9a-f]{64}).*?= \(32 bytes\) = ([0-9a-f]{64})", text, flags=re.S)
    stored_key_hex = stored_key_hex.lower()
    startup_key_hex = STARTUP_KEY.hex()
    for left, right, result in xor_lines:
        left = left.lower()
        right = right.lower()
        result = result.lower()
        if left == stored_key_hex and right == startup_key_hex:
            return result
        if right == stored_key_hex and left == startup_key_hex:
            return result
        if result == stored_key_hex and right == startup_key_hex:
            return left
        if result == stored_key_hex and left == startup_key_hex:
            return right
    return None


def _extract_client_uuid_from_plaintext_hex(text: str) -> str | None:
    for hex_blob in re.findall(r"plaintext \(\d+ bytes\) = ([0-9a-fA-F]+)", text):
        try:
            raw = bytes.fromhex(hex_blob)
        except ValueError:
            continue
        match = re.search(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw)
        if match:
            return match.group(0).decode("ascii")
    return None


def _parse_import_record_from_text(text: str, *, client_uuid_override: str | None = None, label: str | None = None) -> dict:
    user_id_match = re.search(r"userId:\s*(\d+)", text)
    stored_key_match = re.search(r"commonKey \(stored_key\) \(32 bytes\) = ([0-9a-fA-F]{64})", text)
    client_uuid_match = re.search(r"clientUuid[:=]\s*([0-9a-fA-F-]{36})", text)
    terminal_id_match = re.search(r"terminalId[:=]\s*([A-Za-z0-9-]{8,64})", text)

    if not user_id_match:
        raise SystemExit("Could not find userId in import text.")
    if not stored_key_match:
        raise SystemExit("Could not find stored_key in import text.")

    client_uuid = (
        client_uuid_override
        or (client_uuid_match.group(1) if client_uuid_match else None)
        or _extract_client_uuid_from_plaintext_hex(text)
    )
    if not client_uuid:
        raise SystemExit("Could not find client_uuid in import text. Provide --client-uuid.")

    stored_key = stored_key_match.group(1).lower()
    record = {
        "user_id": int(user_id_match.group(1)),
        "stored_key": stored_key,
        "client_uuid": client_uuid,
        "source": "console-import",
        "imported_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "crypto_log_ref": text[:500],
    }
    if label:
        record["label"] = label
    if terminal_id_match:
        record["terminal_id"] = terminal_id_match.group(1)

    login_key = _derive_login_key_from_import_log(text, stored_key)
    if login_key:
        record["login_key"] = login_key
    return record


_SAVED_FLOW_REGISTRY: dict[int, callable] = {}


def _saved_flow(number: int):
    def decorator(func):
        _SAVED_FLOW_REGISTRY[number] = func
        return func
    return decorator


class _StepPrinter:
    def __init__(self, total: int):
        self.total = total
        self.current = 0

    def __call__(self, label: str):
        self.current += 1
        print(f"\n[{self.current}/{self.total}] === {label} ===")


def _prepare_saved_account_runtime(args, *, record: dict | None = None, client: DQSGClient | None = None):
    if record is None or client is None:
        client, record = _load_client_for_account(args)
        print(f"=== account {_account_ref(record)} ===")
        print("=== masterdata/get_version ===")
        resp = client.masterdata_get_version()
        _check(resp, "masterdata/get_version")
    print("\n=== login/login ===")
    login_resp = client.login_login(first_login=False)
    _check(login_resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    return client, record, login_resp, login_snapshot


def _add_result_stat_override_args(parser):
    parser.add_argument(
        "--damage-taken",
        type=int,
        default=None,
        help="Override DamageTaken in the result payload; default is 199",
    )
    parser.add_argument(
        "--damage-taken-count",
        type=int,
        default=None,
        help="Override DamageTakenCount in the result payload; default uses the captured bin value",
    )
    parser.add_argument(
        "--dead-count",
        type=int,
        default=None,
        help="Override DeadCount in the result payload; default is 0",
    )
    parser.add_argument(
        "--clear-time",
        type=int,
        default=None,
        help="Override ClearTime in the result payload; default is 179",
    )


def _result_stat_override_kwargs(args) -> dict:
    return {
        "damage_taken": 199 if args.damage_taken is None else args.damage_taken,
        "damage_taken_count": args.damage_taken_count,
        "dead_count": 0 if args.dead_count is None else args.dead_count,
        "clear_time": 179 if args.clear_time is None else args.clear_time,
    }


def _surrender_resume_session(client: DQSGClient, session_id: int, *, context: str = "battle"):
    print(f"\n=== in_game/surrender ({session_id}) ===")
    print(f"  [resume] {context}: surrender unfinished session before fresh start")
    resp = client.in_game_surrender(session_id)
    _check(resp, "in_game/surrender")
    return resp


def _surrender_login_resume_for_battle(
    client: DQSGClient,
    login_resp: dict | None,
    *,
    context: str = "battle",
):
    session_id = login_resp.get("InGameSessionId") if login_resp else None
    if session_id is None:
        return None
    _surrender_resume_session(client, session_id, context=context)
    login_resp["InGameSessionId"] = None
    return session_id


def _run_daily_home_fetch_info(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("home/fetch_info")
    else:
        print("\n=== home/fetch_info ===")
    resp = client.home_fetch_info(
        device_name=_DAILY_HOME_DEVICE_NAME,
        device_token=_DAILY_HOME_DEVICE_TOKEN,
        firebase_id=_DAILY_HOME_FIREBASE_ID,
        adjust_id=_DAILY_HOME_ADJUST_ID,
    )
    _check(resp, "home/fetch_info")
    return resp


def _run_daily_metric_device(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("metric/device")
    else:
        print("\n=== metric/device ===")
    resp = client.metric_device()
    _check(resp, "metric/device")
    return resp


def _run_daily_notice_read_all_normal_notices(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("notice/read_all_normal_notices")
    else:
        print("\n=== notice/read_all_normal_notices ===")
    resp = client.notice_read_all_normal_notices(_DAILY_NOTICE_IDS)
    _check(resp, "notice/read_all_normal_notices")
    return resp


def _run_daily_expedition_receive_reward(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("expedition/receive_reward")
    else:
        print("\n=== expedition/receive_reward ===")
    resp = client.expedition_receive_reward(_DAILY_EXPEDITION_ID)
    _check(resp, "expedition/receive_reward")
    return resp


def _run_daily_expedition_do_expedition(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("expedition/do_expedition")
    else:
        print("\n=== expedition/do_expedition ===")
    resp = client.expedition_do_expedition(
        expedition_id=_DAILY_EXPEDITION_ID,
        expedition_master_id=_DAILY_EXPEDITION_MASTER_ID,
        user_style_id=_DAILY_EXPEDITION_USER_STYLE_ID,
    )
    _check(resp, "expedition/do_expedition")
    return resp


def _run_ad_store_exchanges(client: DQSGClient):
    for exchange_master_id, count in _AD_STORE_EXCHANGES:
        print(f"\n=== shop_exchange/exchange ({exchange_master_id} x{count}) ===")
        resp = client.shop_exchange_exchange(exchange_master_id, count)
        _check(resp, "shop_exchange/exchange")


def _run_daily_ad_store(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("ad store")
    else:
        print("\n=== ad store ===")
    _run_ad_store_exchanges(client)


def cmd_daily(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    step = _StepPrinter(6)

    _run_daily_home_fetch_info(client, step)
    _run_daily_metric_device(client, step)
    _run_daily_notice_read_all_normal_notices(client, step)
    _run_daily_expedition_receive_reward(client, step)
    _run_daily_expedition_do_expedition(client, step)
    _run_daily_ad_store(client, step)

    saved = _save_client_account(
        client,
        args,
        last_command="daily",
        snapshot=login_snapshot,
    )
    print("\n" + "=" * 50)
    print("Daily requests complete.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def _build_account_snapshot_from_login(client: DQSGClient) -> dict | None:
    snapshot = {
        "last_login_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "login_parse_error": None,
        "billing_parse_error": None,
    }
    if not client.last_login_response_raw:
        return snapshot
    try:
        login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    except Exception as exc:
        snapshot["login_parse_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot
    deck = login_user_model.get("deck")
    if deck:
        snapshot["equipment"] = {
            "user_deck_id": deck["UserDeckId"],
            "style_1": {
                "weapon_id": deck["UserWeaponId1"],
                "armor_shield_id": deck["UserArmorShieldId1"],
                "armor_head_id": deck["UserArmorHeadId1"],
                "armor_upper_id": deck["UserArmorUpperId1"],
                "armor_lower_id": deck["UserArmorLowerId1"],
            },
        }
    inventory = _parse_equipment_inventory_from_user_model(login_user_model)
    three_star = inventory.get(3, [])
    if three_star:
        snapshot["inventory_3star"] = three_star
    else:
        snapshot.pop("inventory_3star", None)
    growth_materials = _parse_growth_material_amounts_from_login_response(
        client.last_login_response_raw,
        _TRACKED_GROWTH_MATERIAL_IDS,
    )
    if any(growth_materials.values()):
        snapshot["growth_materials"] = {
            str(master_id): amount for master_id, amount in growth_materials.items()
        }
    else:
        snapshot.pop("growth_materials", None)
    return snapshot


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_user_status_from_billing_response(response_raw: bytes) -> dict | None:
    if len(response_raw) < 36:
        return None
    candidates = []
    for pos in range(0, len(response_raw) - 36):
        gold = int.from_bytes(response_raw[pos:pos + 4], "little", signed=True)
        last_login_at = int.from_bytes(response_raw[pos + 4:pos + 12], "little", signed=True)
        action_point_full_at = int.from_bytes(response_raw[pos + 12:pos + 20], "little", signed=True)
        action_point = int.from_bytes(response_raw[pos + 20:pos + 24], "little", signed=True)
        current_used_deck_id = int.from_bytes(response_raw[pos + 24:pos + 28], "little", signed=True)
        rank_exp = int.from_bytes(response_raw[pos + 28:pos + 32], "little", signed=True)
        last_received_rank = int.from_bytes(response_raw[pos + 32:pos + 36], "little", signed=True)
        if not (0 <= gold <= 100_000_000):
            continue
        if not (1_600_000_000_000 <= last_login_at <= 2_000_000_000_000):
            continue
        if not (1_600_000_000_000 <= action_point_full_at <= 2_000_000_000_000):
            continue
        if not (0 <= action_point <= 5000):
            continue
        if not (1 <= current_used_deck_id <= 20):
            continue
        if not (0 <= rank_exp <= 10_000_000):
            continue
        if not (1 <= last_received_rank <= 500):
            continue
        candidates.append({
            "gold": gold,
            "action_point": action_point,
            "last_login_at_ms": last_login_at,
            "action_point_full_at_ms": action_point_full_at,
            "current_used_deck_id": current_used_deck_id,
            "rank_exp": rank_exp,
            "last_received_rank": last_received_rank,
            "source": "billing/update_web_store_user_status",
            "anchor_offset": pos,
        })
    if len(candidates) != 1:
        return None
    return candidates[0]


def _refresh_account_snapshot_from_billing(client: DQSGClient, snapshot: dict | None) -> dict:
    if snapshot is None:
        snapshot = {}
    try:
        resp = client.billing_update_web_store()
        _check(resp, "billing/update_web_store")
    except Exception as exc:
        snapshot["billing_parse_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

    try:
        sns_coin = _extract_sns_coin_from_response_tail(client.last_response_raw)
        user_status = _extract_user_status_from_billing_response(client.last_response_raw)
    except Exception as exc:
        snapshot["billing_parse_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

    snapshot["billing_parse_error"] = None
    if sns_coin:
        sns_coin = dict(sns_coin)
        sns_coin["source"] = "billing/update_web_store"
        snapshot["sns_coin"] = sns_coin
    else:
        snapshot.pop("sns_coin", None)

    if user_status:
        snapshot["gold"] = {
            "amount": user_status["gold"],
            "source": user_status["source"],
            "anchor_offset": user_status["anchor_offset"],
        }
        snapshot["action_point"] = {
            "current": user_status["action_point"],
            "full_at_ms": user_status["action_point_full_at_ms"],
            "full_at": _ms_to_iso(user_status["action_point_full_at_ms"]),
            "source": user_status["source"],
            "anchor_offset": user_status["anchor_offset"],
        }
    else:
        snapshot.pop("gold", None)
        snapshot.pop("action_point", None)
        snapshot["billing_parse_error"] = "ValueError: could not locate UserStatus in billing/update_web_store response"
    return snapshot


def _collect_account_rewards(client: DQSGClient) -> dict:
    collected = {
        "adventure_book": 0,
        "daily": 0,
        "daily_progress": 0,
        "weekly": 0,
        "weekly_progress": 0,
        "achievement": 0,
        "event": 0,
        "present": 0,
    }

    print("\n=== user_rank/receive_reward ===")
    resp = client.user_rank_receive_reward()
    _check(resp, "user_rank/receive_reward")
    collected["adventure_book"] = 1

    for round_index in range(1, 6):
        print(f"\n=== mission/get_mission_summary (round {round_index}) ===")
        resp = client.mission_get_summary()
        _check(resp, f"mission/get_mission_summary round {round_index}")
        reward_ids = _parse_mission_summary_reward_ids(client.last_response_raw)

        round_changes = 0

        if reward_ids["daily"]:
            print(f"\n=== mission/receive_mission_daily_reward ({len(reward_ids['daily'])}) ===")
            resp = client.mission_receive_daily_reward(reward_ids["daily"])
            _check(resp, "mission/receive_mission_daily_reward")
            collected["daily"] += len(reward_ids["daily"])
            round_changes += len(reward_ids["daily"])

        if reward_ids["daily_progress"]:
            print(f"\n=== mission/receive_mission_daily_progress_reward ({len(reward_ids['daily_progress'])}) ===")
            resp = client.mission_receive_daily_progress_reward(reward_ids["daily_progress"])
            _check(resp, "mission/receive_mission_daily_progress_reward")
            collected["daily_progress"] += len(reward_ids["daily_progress"])
            round_changes += len(reward_ids["daily_progress"])

        if reward_ids["weekly"]:
            print(f"\n=== mission/receive_mission_weekly_reward ({len(reward_ids['weekly'])}) ===")
            resp = client.mission_receive_weekly_reward(reward_ids["weekly"])
            _check(resp, "mission/receive_mission_weekly_reward")
            collected["weekly"] += len(reward_ids["weekly"])
            round_changes += len(reward_ids["weekly"])

        if reward_ids["weekly_progress"]:
            print(f"\n=== mission/receive_mission_weekly_progress_reward ({len(reward_ids['weekly_progress'])}) ===")
            resp = client.mission_receive_weekly_progress_reward(reward_ids["weekly_progress"])
            _check(resp, "mission/receive_mission_weekly_progress_reward")
            collected["weekly_progress"] += len(reward_ids["weekly_progress"])
            round_changes += len(reward_ids["weekly_progress"])

        if reward_ids["achievement"]:
            print(f"\n=== mission/receive_mission_achievement_reward ({len(reward_ids['achievement'])}) ===")
            resp = client.mission_receive_achievement_reward(reward_ids["achievement"])
            _check(resp, "mission/receive_mission_achievement_reward")
            collected["achievement"] += len(reward_ids["achievement"])
            round_changes += len(reward_ids["achievement"])

        if reward_ids["event"]:
            print(f"\n=== mission/receive_mission_event_reward ({len(reward_ids['event'])}) ===")
            resp = client.mission_receive_event_reward(reward_ids["event"])
            _check(resp, "mission/receive_mission_event_reward")
            collected["event"] += len(reward_ids["event"])
            round_changes += len(reward_ids["event"])

        if round_changes == 0:
            print(f"\nNo more mission rewards in round {round_index}.")
            break

    for round_index in range(1, 6):
        print(f"\n=== present/fetch (round {round_index}) ===")
        resp = client.present_fetch()
        _check(resp, f"present/fetch round {round_index}")
        present_ids = _parse_present_fetch_ids(client.last_response_raw)
        if not present_ids:
            print(f"\nNo more presents in round {round_index}.")
            break

        for batch_index, present_id_batch in enumerate(_chunked(present_ids, 50), start=1):
            print(f"\n=== present/receive batch {batch_index} ({len(present_id_batch)}) ===")
            resp = client.present_receive(present_id_batch)
            _check(resp, f"present/receive batch {batch_index}")
            collected["present"] += len(present_id_batch)

    return collected


def _collect_rewards_and_refresh_snapshot(client: DQSGClient) -> tuple[dict, dict]:
    collected = _collect_account_rewards(client)

    print("\n=== login/login (refresh snapshot) ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login refresh snapshot")
    login_snapshot = _refresh_account_snapshot_from_billing(client, _build_account_snapshot_from_login(client))
    return login_snapshot, collected


def _parse_login_user_model_basics(login_response_raw: bytes) -> dict:
    r = BytesReader(login_response_raw)
    r.read_int()  # status
    r.read_int()  # auth count
    r.read_bytes()  # session key xor bytes
    r.read_string()  # client id
    r.read_nullable_long()  # in-game session id
    r.read_bool()  # perf metrics enabled
    r.read_string()  # asset cdn url

    has_user_model = r.read_bool()
    if not has_user_model:
        raise ValueError("login response did not contain UserModel")

    def skip_list(reader):
        count = r.read_int()
        return [reader() for _ in range(count)]

    def read_nullable_localized_text():
        has_value = r.read_bool()
        if not has_value:
            return None
        return r.read_string()

    def read_user_weapon():
        return {
            "UserWeaponId": r.read_long(),
            "WeaponMasterId": r.read_int(),
            "Level": r.read_int(),
            "LevelExp": r.read_int(),
            "LimitBreakStep": r.read_int(),
            "IsLock": r.read_bool(),
            "AcquiredAt": r.read_long(),
        }

    def read_user_armor():
        return {
            "UserArmorId": r.read_long(),
            "ArmorMasterId": r.read_int(),
            "Level": r.read_int(),
            "LevelExp": r.read_int(),
            "LimitBreakStep": r.read_int(),
            "IsLock": r.read_bool(),
            "AcquiredAt": r.read_long(),
        }

    def read_user_avatar():
        return {
            "AvatarMasterId": r.read_int(),
            "Name": read_nullable_localized_text(),
            "BodyMasterId": r.read_int(),
            "FaceMasterId": r.read_int(),
            "EyeColorMasterId": r.read_int(),
            "SkinColorMasterId": r.read_int(),
            "HairMasterId": r.read_int(),
            "HairColorMasterId": r.read_int(),
            "VoiceMasterId": r.read_int(),
        }

    def read_nullable_int():
        return r.read_nullable_int()

    def read_nullable_long():
        return r.read_nullable_long()

    def read_user_deck():
        deck = {
            "UserDeckId": r.read_int(),
            "Name": read_nullable_localized_text(),
            "StyleMasterId1": read_nullable_int(),
            "StyleMasterId2": read_nullable_int(),
            "StyleMasterId3": read_nullable_int(),
            "UserWeaponId1": read_nullable_long(),
            "UserWeaponId2": read_nullable_long(),
            "UserWeaponId3": read_nullable_long(),
            "UserArmorShieldId1": read_nullable_long(),
            "UserArmorShieldId2": read_nullable_long(),
            "UserArmorShieldId3": read_nullable_long(),
            "UserArmorHeadId1": read_nullable_long(),
            "UserArmorHeadId2": read_nullable_long(),
            "UserArmorHeadId3": read_nullable_long(),
            "UserArmorUpperId1": read_nullable_long(),
            "UserArmorUpperId2": read_nullable_long(),
            "UserArmorUpperId3": read_nullable_long(),
            "UserArmorLowerId1": read_nullable_long(),
            "UserArmorLowerId2": read_nullable_long(),
            "UserArmorLowerId3": read_nullable_long(),
        }
        for _ in range(47):
            read_nullable_long()
        deck["IsLooksEquipment1"] = r.read_bool()
        deck["IsLooksEquipment2"] = r.read_bool()
        deck["IsLooksEquipment3"] = r.read_bool()
        return deck

    weapons = skip_list(read_user_weapon)
    skip_list(lambda: r.read_int())
    skip_list(lambda: (r.read_int(), r.read_int()))
    skip_list(lambda: r.read_long())
    skip_list(lambda: (r.read_int(), r.read_int()))
    skip_list(lambda: r.read_int())
    armors = skip_list(read_user_armor)
    skip_list(lambda: r.read_int())
    avatars = skip_list(read_user_avatar)
    decks = skip_list(read_user_deck)

    return {
        "weapons": weapons,
        "armors": armors,
        "avatar": avatars[0] if avatars else None,
        "deck": decks[0] if decks else None,
    }


def _extract_tutorial_gacha_user_weapon_id(gacha_draw_response_raw: bytes) -> int:
    r = BytesReader(gacha_draw_response_raw)
    status = r.read_int()
    if status != 1:
        raise ValueError(f"gacha/draw failed: status={status}")
    reward_count = r.read_int()
    if reward_count < 1:
        raise ValueError("gacha/draw returned no rewards")
    r.read_int()  # content type
    r.read_int()  # content master id
    r.read_int()  # amount
    user_weapon_id = r.read_nullable_long()
    if user_weapon_id is None:
        raise ValueError("gacha/draw did not return a UserWeaponId")
    return user_weapon_id


def _find_int32_occurrences(data: bytes, value: int) -> list[int]:
    hits = []
    needle = struct.pack("<i", value)
    start = 0
    while True:
        idx = data.find(needle, start)
        if idx == -1:
            return hits
        hits.append(idx)
        start = idx + 1


def _read_nullable_time(r: BytesReader):
    has_value = r.read_bool()
    if not has_value:
        return None
    return r.read_long()


def _read_nullable_localized_text(r: BytesReader):
    has_value = r.read_bool()
    if not has_value:
        return None
    return r.read_string()


def _read_content(r: BytesReader) -> dict:
    return {
        "ContentType": r.read_int(),
        "ContentMasterId": r.read_int(),
        "ContentAmount": r.read_int(),
    }


def _parse_present_fetch_ids(fetch_response_raw: bytes) -> list[int]:
    r = BytesReader(fetch_response_raw)
    status = r.read_int()
    if status != 1:
        raise ValueError(f"present/fetch failed: status={status}")
    count = r.read_int()
    present_ids = []
    for _ in range(count):
        present_ids.append(r.read_int())
        _read_content(r)
        r.read_int()  # PresentRouteType
        _read_nullable_localized_text(r)
        r.read_nullable_string()
        r.read_long()  # PostedAt
        _read_nullable_time(r)
    return present_ids


def _parse_mission_summary_reward_ids(summary_response_raw: bytes) -> dict[str, list[int]]:
    r = BytesReader(summary_response_raw)
    status = r.read_int()
    if status != 1:
        raise ValueError(f"mission/get_mission_summary failed: status={status}")

    def read_summary_list():
        result = []
        count = r.read_int()
        for _ in range(count):
            mission_master_id = r.read_int()
            r.read_int()  # ProgressCount
            is_cleared = r.read_bool()
            is_received_reward = r.read_bool()
            _read_nullable_time(r)  # ClearedAt
            _read_nullable_time(r)  # ReceivedRewardAt
            _read_nullable_time(r)  # ResetAt
            if is_cleared and not is_received_reward:
                result.append(mission_master_id)
        return result

    daily = read_summary_list()
    daily_progress = read_summary_list()
    weekly = read_summary_list()
    weekly_progress = read_summary_list()
    achievement = read_summary_list()
    event = read_summary_list()

    return {
        "daily": daily,
        "daily_progress": daily_progress,
        "weekly": weekly,
        "weekly_progress": weekly_progress,
        "achievement": achievement,
        "event": event,
    }


def _extract_sns_coin_from_response_tail(response_raw: bytes) -> dict | None:
    if len(response_raw) < 21:
        return None
    window_start = max(0, len(response_raw) - 256)
    for pos in range(window_start, len(response_raw) - 21):
        has_value = response_raw[pos]
        if has_value not in (0, 1):
            continue
        free_amount = int.from_bytes(response_raw[pos + 1:pos + 5], "little", signed=True)
        billing_amount = int.from_bytes(response_raw[pos + 5:pos + 9], "little", signed=True)
        updated_at = int.from_bytes(response_raw[pos + 9:pos + 17], "little", signed=True)
        string_len = int.from_bytes(response_raw[pos + 17:pos + 21], "little", signed=True)
        if not (0 <= free_amount <= 200_000 and 0 <= billing_amount <= 200_000):
            continue
        if not (1_600_000_000_000 <= updated_at <= 2_000_000_000_000):
            continue
        if not (0 < string_len <= 64):
            continue
        string_end = pos + 21 + string_len
        if string_end > len(response_raw):
            continue
        suffix = response_raw[pos + 21:string_end]
        if not suffix or any(ch < 32 or ch >= 127 for ch in suffix):
            continue
        return {
            "free": free_amount,
            "billing": billing_amount,
            "total": free_amount + billing_amount,
            "source": "response_tail",
        }
    return None


def _chunked(seq: list[int], size: int):
    for idx in range(0, len(seq), size):
        yield seq[idx:idx + size]


def _build_deck_save_style_equipment_request(
    *,
    user_deck_id: int,
    style_index: int,
    user_weapon_id: int | None,
    user_armor_shield_id: int | None,
    user_armor_head_id: int | None,
    user_armor_upper_id: int | None,
    user_armor_lower_id: int | None,
    user_accessory_id1: int | None = None,
    user_accessory_id2: int | None = None,
    user_orb_id1: int | None = None,
    user_orb_id2: int | None = None,
    user_orb_id3: int | None = None,
    user_orb_id4: int | None = None,
    user_pearl_id1: int | None = None,
    user_pearl_id2: int | None = None,
) -> bytes:
    def write_nullable_long(value):
        if value is None:
            w.write_bool(False)
        else:
            w.write_bool(True)
            w.write_long(value)

    w = BytesWriter()
    w.write_int(user_deck_id)
    w.write_int(style_index)
    write_nullable_long(user_weapon_id)
    write_nullable_long(user_armor_shield_id)
    write_nullable_long(user_armor_head_id)
    write_nullable_long(user_armor_upper_id)
    write_nullable_long(user_armor_lower_id)
    write_nullable_long(user_accessory_id1)
    write_nullable_long(user_accessory_id2)
    write_nullable_long(user_orb_id1)
    write_nullable_long(user_orb_id2)
    write_nullable_long(user_orb_id3)
    write_nullable_long(user_orb_id4)
    write_nullable_long(user_pearl_id1)
    write_nullable_long(user_pearl_id2)
    return w.to_bytes()


def _extract_user_orb_id(login_response_raw: bytes, orb_master_id: int) -> int | None:
    pattern = struct.pack("<i", orb_master_id)
    start = 0
    while True:
        idx = login_response_raw.find(pattern, start)
        if idx == -1:
            return None
        if idx < 8 or idx + 13 > len(login_response_raw):
            start = idx + 1
            continue
        user_orb_id = struct.unpack_from("<q", login_response_raw, idx - 8)[0]
        rank = struct.unpack_from("<i", login_response_raw, idx + 4)[0]
        rank_up_point = struct.unpack_from("<i", login_response_raw, idx + 8)[0]
        is_lock = login_response_raw[idx + 12]
        if (
            user_orb_id > 0
            and 0 < rank <= 100
            and 0 <= rank_up_point <= 10_000
            and is_lock in (0, 1)
        ):
            return user_orb_id
        start = idx + 1


def _build_flow4_auto_style_equipment_request(login_response_raw: bytes) -> bytes:
    login_user_model = _parse_login_user_model_basics(login_response_raw)
    deck = login_user_model["deck"]
    if not deck:
        raise RuntimeError("login/login did not return deck data")
    return _build_deck_save_style_equipment_request(
        user_deck_id=deck["UserDeckId"],
        style_index=1,
        user_weapon_id=deck["UserWeaponId1"],
        user_armor_shield_id=deck["UserArmorShieldId1"],
        user_armor_head_id=deck["UserArmorHeadId1"],
        user_armor_upper_id=deck["UserArmorUpperId1"],
        user_armor_lower_id=deck["UserArmorLowerId1"],
        user_accessory_id1=None,
        user_accessory_id2=None,
        user_orb_id1=_extract_user_orb_id(login_response_raw, 300001),
        user_orb_id2=_extract_user_orb_id(login_response_raw, 200011),
        user_orb_id3=_extract_user_orb_id(login_response_raw, 200001),
        user_orb_id4=None,
        user_pearl_id1=None,
        user_pearl_id2=None,
    )


def _submit_in_game_result_with_resume(
    client: DQSGClient,
    *,
    build_result_body=None,
    stage_master_id: int = None,
    template_stage_id: int = None,
    in_game_session_id: int = None,
    start_response: dict = None,
    damage_taken: int = None,
    damage_taken_count: int = None,
    dead_count: int = None,
    clear_time: int = None,
):
    """Submit in_game/result with automatic retry on HTTP 500.

    On 500, re-logins to detect the unfinished battle. Battle commands must
    surrender that session instead of resuming it with another result payload.

    Two modes:
      - build_result_body: callable(in_game_session_id, start_response) -> bytes
        Used for raw-body submissions (juxiang, yc dungeons).
      - stage_master_id/template_stage_id: template-based submissions.
    """
    max_attempts = 3
    if start_response is None:
        start_response = getattr(client, "last_in_game_start_response", None)
        if (
            start_response is not None
            and stage_master_id is not None
            and start_response.get("_stage_master_id") not in (None, stage_master_id)
        ):
            start_response = None
    current_session_id = in_game_session_id
    if current_session_id is None and start_response is not None:
        current_session_id = start_response.get("SessionId")
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        try:
            if build_result_body is not None:
                raw_body = build_result_body(current_session_id, start_response)
                resp = client.in_game_result(raw_body=raw_body)
            else:
                resp = client.in_game_result(
                    stage_master_id=stage_master_id,
                    template_stage_id=template_stage_id,
                    in_game_session_id=current_session_id,
                    damage_taken=damage_taken,
                    damage_taken_count=damage_taken_count,
                    dead_count=dead_count,
                    clear_time=clear_time,
                )
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 500:
                raise
            last_exc = exc
            if attempt >= max_attempts:
                break
            if client.debug:
                print(
                    f"  [resume] in_game/result hit {_color('HTTP 500', _RED)} "
                    f"on attempt {attempt}/{max_attempts}, "
                    "sleeping 2s before surrendering InGameSessionId"
                )
            time.sleep(2)
            login_resp = client.login_login(first_login=False)
            _check(login_resp, "login/login resume")
            surrendered_session_id = _surrender_login_resume_for_battle(
                client,
                login_resp,
                context="in_game/result HTTP 500",
            )
            if surrendered_session_id is None:
                raise RuntimeError("resume login did not return InGameSessionId") from exc
            raise RuntimeError(
                "in_game/result hit HTTP 500; surrendered unfinished session "
                "instead of resuming it"
            ) from exc
        else:
            _receive_in_game_result_ad_chance(client, resp)
            return resp

    stage_label = stage_master_id or "raw_body"
    raise RuntimeError(
        f"in_game/result({stage_label}) still failed after {max_attempts} attempts"
    ) from last_exc


def _receive_in_game_result_ad_chance(client: DQSGClient, result_resp: dict):
    stage_results = []
    if result_resp.get("StageResult"):
        stage_results.append(result_resp["StageResult"])
    stage_results.extend(result_resp.get("StageResultList") or [])

    if not stage_results:
        print("\n=== advertisement/ad_chance_check: no StageResult ===")
        return

    print(f"\n=== advertisement/ad_chance_check ({len(stage_results)} result(s)) ===")
    for idx, stage_result in enumerate(stage_results, 1):
        _receive_stage_result_ad_chance(client, stage_result, idx if len(stage_results) > 1 else None)


def _receive_stage_result_ad_chance(client: DQSGClient, stage_result: dict, index: int = None):
    orb_master_id = stage_result.get("AdChanceOrbMasterId")
    point_card_amount = stage_result.get("AdChancePointCardPointAmount")
    suffix = f" #{index}" if index is not None else ""

    if orb_master_id is None and point_card_amount is None:
        print(f"  ad chance{suffix}: none")
        return

    if orb_master_id is not None:
        print(f"\n=== advertisement/receive_reward_ad_chance_orb{suffix} ({orb_master_id}) ===")
        resp = client.advertisement_receive_reward_ad_chance_orb(orb_master_id)
        _check(resp, "advertisement/receive_reward_ad_chance_orb")

    if point_card_amount is not None:
        print(f"\n=== advertisement/receive_reward_chance_point_card_point{suffix} ({point_card_amount}) ===")
        resp = client.advertisement_receive_reward_chance_point_card_point()
        _check(resp, "advertisement/receive_reward_chance_point_card_point")


def _run_scored_dungeon(client, stage_master_id, build_result_body, login_resp=None):
    """Run a scored dungeon: in_game/start + in_game/result as a single SOP.

    Handles HTTP 500 on both start and result:
      - If login returned InGameSessionId (unfinished battle), surrenders it.
      - If start gets 500, re-logins, surrenders the unfinished session, then restarts.
      - If result gets 500, re-logins and surrenders instead of resuming.

    Args:
        client: DQSGClient instance (already logged in).
        stage_master_id: Stage to fight.
        build_result_body: callable(in_game_session_id, start_response) -> bytes.
        login_resp: The login/login response dict (to check for existing InGameSessionId).
    """
    if login_resp:
        _surrender_login_resume_for_battle(
            client,
            login_resp,
            context=f"in_game/start({stage_master_id})",
        )
    start_response = None
    existing_session_id = None

    for start_attempt in range(1, 3):
        if client.debug:
            print(f"\n=== in_game/start ({stage_master_id}) ===")
        try:
            resp = client.in_game_start(stage_master_id, deck_index=1)
            _check(resp, "in_game/start")
            start_response = resp
            existing_session_id = resp.get("SessionId")
            break
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 500:
                raise
            if start_attempt >= 2:
                raise
            if client.debug:
                print(
                    f"  [resume] in_game/start hit {_color('HTTP 500', _RED)}, "
                    "refreshing InGameSessionId before surrender"
                )
            login_resp = client.login_login(first_login=False)
            _check(login_resp, "login/login resume")
            surrendered_session_id = _surrender_login_resume_for_battle(
                client,
                login_resp,
                context=f"in_game/start({stage_master_id}) retry",
            )
            if surrendered_session_id is None:
                raise RuntimeError(
                    "resume login did not return InGameSessionId"
                ) from exc

    if client.debug:
        print(f"\n=== in_game/result ({stage_master_id}) ===")
    resp = _submit_in_game_result_with_resume(
        client,
        build_result_body=build_result_body,
        in_game_session_id=existing_session_id,
        start_response=start_response,
    )
    _check(resp, "in_game/result")
    return resp


def cmd_accounts(args):
    try:
        records = list_accounts(path=_store_path(args))
    except AccountStoreError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Account store: {_store_path(args)}")
    if not records:
        print("No saved accounts.")
        return

    for record in records:
        label = record.get("label") or "-"
        progress = record.get("progress") or "-"
        print(
            f"{record['user_id']}  label={label}  "
            f"progress={progress}  updated={record.get('updated_at', '-')}"
        )


def _scan_weapons_from_login_response(login_response_raw: bytes) -> list[dict]:
    """Scan login response for weapon entries by pattern matching.

    Weapon struct (33 bytes, same as armor):
      long(uid) + int(mid) + int(level) + int(levelExp) +
      int(limitBreak) + bool(lock) + long(acquiredAt)

    The positional parser can't reliably find weapons because the server
    may reorder user model fields.  This scanner finds a count + entries
    block where every entry validates as a weapon.
    """
    ENTRY_SIZE = 33  # 8+4+4+4+4+1+8
    data = login_response_raw
    best = []

    for count_pos in range(0, len(data) - 4):
        count = struct.unpack_from('<i', data, count_pos)[0]
        if count < 1 or count > 500:
            continue
        entries_start = count_pos + 4
        if entries_start + count * ENTRY_SIZE > len(data):
            continue

        valid = True
        candidates = []
        for i in range(count):
            ep = entries_start + i * ENTRY_SIZE
            uid = struct.unpack_from('<q', data, ep)[0]
            mid = struct.unpack_from('<i', data, ep + 8)[0]
            lvl = struct.unpack_from('<i', data, ep + 12)[0]
            exp = struct.unpack_from('<i', data, ep + 16)[0]
            lb  = struct.unpack_from('<i', data, ep + 20)[0]
            lock = data[ep + 24]
            acq  = struct.unpack_from('<q', data, ep + 25)[0]

            if uid <= 0 or not (100000 <= mid <= 399999):
                valid = False
                break
            if not (0 <= lvl <= 200 and 0 <= exp <= 100000 and 0 <= lb <= 10 and lock in (0, 1)):
                valid = False
                break
            candidates.append({
                "UserWeaponId": uid,
                "WeaponMasterId": mid,
                "Level": lvl,
                "LevelExp": exp,
                "LimitBreakStep": lb,
                "IsLock": bool(lock),
                "AcquiredAt": acq,
            })

        if valid and len(candidates) > len(best):
            best = candidates

    return best


def cmd_status(args):
    """Show account status: SNS coin balance and equipment inventory."""
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")

    print("\n=== billing/update_web_store ===")
    resp = client.billing_update_web_store()
    _check(resp, "billing/update_web_store")

    # Extract sns_coin
    sns_coin = _extract_sns_coin_from_response_tail(client.last_response_raw)

    # Parse equipment from login
    login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    weapons = login_user_model.get("weapons", [])
    armors = login_user_model.get("armors", [])

    # Fallback: scan binary for weapons when positional parser returns empty
    if not weapons and client.last_login_response_raw:
        weapons = _scan_weapons_from_login_response(client.last_login_response_raw)

    # --- Display ---
    print(f"\n{'='*50}")
    print(f"Account: {_account_ref(record)}  (user_id={record['user_id']})")
    print(f"{'='*50}")

    # SNS Coin
    if sns_coin:
        print(f"  sns_coin  = free:{sns_coin['free']} billing:{sns_coin['billing']} total:{sns_coin['total']}")
    else:
        print("  sns_coin  = (unable to parse)")

    # Equipment
    deck = login_user_model.get("deck")

    # Build equipped id set for marking
    equipped_ids = set()
    if deck:
        for suffix in range(1, 4):
            for key_base in ("UserWeaponId", "UserArmorShieldId", "UserArmorHeadId",
                             "UserArmorUpperId", "UserArmorLowerId"):
                eid = deck.get(f"{key_base}{suffix}")
                if eid:
                    equipped_ids.add(eid)

    if weapons:
        print(f"\n  === Weapons ({len(weapons)}) ===")
        for w in weapons:
            mid = w["WeaponMasterId"]
            uid = w["UserWeaponId"]
            display = equipment_display_name(CONTENT_TYPE_WEAPON, mid)
            lvl = w.get("Level", 0)
            tag = "  [E]" if uid in equipped_ids else ""
            print(f"    {display}  mid={mid}  uid={uid}  lv={lvl}{tag}")
    else:
        print("\n  === Weapons: (none found) ===")

    if armors:
        print(f"\n  === Armors ({len(armors)}) ===")
        for a in armors:
            mid = a["ArmorMasterId"]
            uid = a["UserArmorId"]
            display = equipment_display_name(CONTENT_TYPE_ARMOR, mid)
            lvl = a.get("Level", 0)
            lb = a.get("LimitBreakStep", 0)
            tag = "  [E]" if uid in equipped_ids else ""
            print(f"    {display}  mid={mid}  uid={uid}  lv={lvl}  lb={lb}{tag}")

    print("=" * 50)


def cmd_live(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)

    saved = None
    for flow_number in sorted(_SAVED_FLOW_REGISTRY):
        print(f"\n{'=' * 50}")
        print(f"Running flow{flow_number}")
        saved = _SAVED_FLOW_REGISTRY[flow_number](args, client, record, login_snapshot, login_resp)
        record = saved
        login_snapshot = saved.get("snapshot") if isinstance(saved, dict) else login_snapshot

    print("\n" + "=" * 50)
    print("Live complete. Executed flows in numeric order:")
    print("  " + ", ".join(f"flow{n}" for n in sorted(_SAVED_FLOW_REGISTRY)))
    if saved is not None:
        _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_register(args):
    client = DQSGClient.new_account()
    print("=== masterdata/get_version ===")
    client.masterdata_get_version()
    print("\n=== login/startup ===")
    creds = client.login_startup()
    saved = _save_client_account(client, args, progress="registered", last_command="register")
    print("\n=== login/login (first) ===")
    client.login_login(first_login=True)
    print("\n" + "=" * 50)
    print("New account credentials:")
    print(f"  user_id     = {creds['user_id']}")
    print(f"  stored_key  = {creds['stored_key']}")
    print(f"  client_uuid = {creds['client_uuid']}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_import(args):
    text = _read_import_text(args)
    record = _parse_import_record_from_text(
        text,
        client_uuid_override=getattr(args, "client_uuid", None),
        label=getattr(args, "label", None),
    )
    try:
        saved = save_account(
            record,
            path=_store_path(args),
            label=getattr(args, "label", None),
            progress="imported",
            last_command="import",
        )
        _mark_last_selected_account(saved, args)
    except (AccountStoreError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print("\n" + "=" * 50)
    print("Imported account from console log.")
    print(f"  user_id     = {saved['user_id']}")
    print(f"  stored_key  = {saved['stored_key']}")
    print(f"  client_uuid = {saved['client_uuid']}")
    if saved.get("login_key"):
        print(f"  login_key   = {saved['login_key']}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_delete(args):
    account_ref = args.account
    if not account_ref:
        try:
            records = list_accounts(path=_store_path(args))
        except AccountStoreError as exc:
            raise SystemExit(str(exc)) from exc
        if not records:
            raise SystemExit(f"No saved accounts in {_store_path(args)}")
        print("Select account to delete:")
        for idx, record in enumerate(records, start=1):
            label = record.get("label") or "-"
            progress = record.get("progress") or "-"
            print(f"  {idx}. {record['user_id']}  label={label}  progress={progress}")
        raw = input("\nEnter number: ").strip()
        if not raw.isdigit():
            raise SystemExit("Invalid selection.")
        selected = int(raw)
        if not (1 <= selected <= len(records)):
            raise SystemExit("Selection out of range.")
        account_ref = str(records[selected - 1]["user_id"])

    try:
        record = resolve_account(account_ref, path=_store_path(args))
    except AccountStoreError as exc:
        raise SystemExit(str(exc)) from exc

    client = DQSGClient.from_account_record(record)
    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")
    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")

    print("\n=== user/delete ===")
    resp = client.delete_account()
    if resp["_status"] == 1:
        try:
            deleted = delete_account_record(str(record["user_id"]), path=_store_path(args))
        except AccountStoreError as exc:
            raise SystemExit(f"Account deleted remotely, but failed to update local store: {exc}") from exc
        print("\n" + "=" * 50)
        print(f"Deleted account: {_account_ref(deleted)}")
        print(f"  user_id    = {deleted['user_id']}")
        print(f"  store_file = {_store_path(args)}")
        print("=" * 50)
    else:
        print(f"\nDelete failed: {_status_text(resp['_status'])}")


def cmd_verify(args):
    chlz_dir = "/tmp/chlz_gg5"
    from .parsers import parse_login_response
    from .crypto import decrypt_response

    print("=" * 60)
    print("Verifying against gg5 captured traffic")
    print("=" * 60)

    xor_l1 = bytes.fromhex("7a2216ecb295277fc270209284894853145c1874f0534c9214fa414d89a93cad")
    xor_r1 = STARTUP_KEY
    login_key = xor_bytes(xor_l1, xor_r1)
    print(f"\nLogin key = {login_key.hex()}")

    xor_l2 = bytes.fromhex("746cdb8a67ce975e6123d0c65fdf76e947ad7eb2eecd4f7b24ddf80467fd7938")
    xor_r2 = bytes.fromhex("3bcade7c3a5e1b988c7b7e086dae3e215841977f6c51e19033eccbef19424a13")
    session_key = xor_bytes(xor_l2, xor_r2)
    print(f"Session key = {session_key.hex()}")

    keys = [STARTUP_KEY, login_key, STARTUP_KEY, session_key, session_key, session_key]
    paths = []
    for i in range(6):
        meta = json.load(open(f"{chlz_dir}/{i}-meta.json"))
        path = meta["path"].replace("/ep01000", "") + "?" + meta.get("query", "")
        paths.append(path)

    for i in range(6):
        res_data = open(f"{chlz_dir}/{i}-res.bin", "rb").read()
        key = keys[i]
        key_name = "StartupKey" if key == STARTUP_KEY else ("loginKey" if key == login_key else "sessionKey")
        print(f"\n--- #{i}: {paths[i][:60]}... [{key_name}]")
        try:
            decrypted = decrypt_response(key, paths[i], res_data)
            print(f"    OK ({len(decrypted)} bytes): {decrypted[:64].hex()}")
            if i == 1:
                lr = parse_login_response(decrypted)
                print(f"    SessionKey match: {lr['SessionKey'] == xor_r2}")
        except Exception as e:
            print(f"    FAILED: {e}")


def cmd_flow1(args):
    """New account: register -> tutorial battle -> main menu."""
    client = DQSGClient.new_account()
    step = _StepPrinter(19)

    step("masterdata/get_version")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    step("login/startup (register)")
    creds = client.login_startup()
    if not creds["user_id"]:
        raise RuntimeError("login/startup failed: no user_id")
    print(f"  userId={creds['user_id']}")
    print(f"  storedKey={creds['stored_key'][:16]}...")
    _save_client_account(client, args, progress="registered", last_command="flow1")
    print(f"  account saved to {_store_path(args)}")

    step("login/login (first)")
    resp = client.login_login(first_login=True)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    if not resp.get("SessionKey") or len(resp["SessionKey"]) != 32:
        raise RuntimeError("login/login: no valid SessionKey")
    login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    avatar = login_user_model["avatar"]
    if not avatar:
        raise RuntimeError("login/login did not return avatar data")

    step("terms/get_terms_eu")
    client.terms_get()

    step("terms/terms_agree_eu")
    client.terms_agree()

    step("metric/tutorial #1")
    resp = client.metric_tutorial()
    _check(resp, "metric/tutorial #1")

    step("metric/tutorial #2")
    resp = client.metric_tutorial()
    _check(resp, "metric/tutorial #2")

    step("tutorial/read (VoiceSetting)")
    resp = client.tutorial_read(TUTORIAL_STEP_VOICE_SETTING)
    _check(resp, "tutorial/read VoiceSetting")

    step("profile/set_user_name")
    resp = client.profile_set_user_name("Tester")
    _check(resp, "profile/set_user_name")

    step("profile/set_user_name (confirm)")
    resp = client.profile_set_user_name("Tester")
    _check(resp, "profile/set_user_name confirm")

    step("avatar/save")
    resp = client.avatar_save(
        avatar_id=avatar["AvatarMasterId"],
        body_id=avatar["BodyMasterId"],
        face_id=avatar["FaceMasterId"],
        eye_color_id=avatar["EyeColorMasterId"],
        skin_color_id=avatar["SkinColorMasterId"],
        hair_id=avatar["HairMasterId"],
        hair_color_id=avatar["HairColorMasterId"],
        voice_id=avatar["VoiceMasterId"],
    )
    _check(resp, "avatar/save")

    step("in_game/start_tutorial")
    resp = client.in_game_start_tutorial()
    _check(resp, "in_game/start_tutorial")

    step("metric/adventure_skip (battle cutscenes)")
    for adv_id, cmd_idx in [(110, 27), (140, 22), (141, 22), (220, 15), (240, 24)]:
        resp = client.metric_adventure_skip(adv_id, cmd_idx)
        _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

    step("adventure/read (battle sequence)")
    for adv_id in [290, 310, 330, 340]:
        resp = client.adventure_read(adv_id)
        _check(resp, f"adventure/read({adv_id})")

    step("adventure/read + metric (3601)")
    resp = client.adventure_read(3601)
    _check(resp, "adventure/read(3601)")
    resp = client.metric_adventure_skip(3601, 20)
    _check(resp, "metric/adventure_skip(3601,20)")

    step("adventure/read (3602, 410)")
    resp = client.adventure_read(3602)
    _check(resp, "adventure/read(3602)")
    resp = client.adventure_read(410)
    _check(resp, "adventure/read(410)")

    step("in_game/result_tutorial")
    resp = client.in_game_result_tutorial()
    _check(resp, "in_game/result_tutorial")

    step("adventure/read + metric (100001000001001)")
    resp = client.adventure_read(100001000001001)
    _check(resp, "adventure/read(100001000001001)")
    resp = client.metric_adventure_skip(100001000001001, 18)
    _check(resp, "metric/adventure_skip(100001000001001,18)")

    step("tutorial/read (ResumePrevStageFirst)")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_STAGE_FIRST)
    _check(resp, "tutorial/read ResumePrevStageFirst")

    saved = _save_client_account(
        client,
        args,
        progress="flow1_complete",
        last_command="flow1",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow1 complete. New account:")
    print(f"  user_id     = {creds['user_id']}")
    print(f"  stored_key  = {creds['stored_key']}")
    print(f"  client_uuid = {creds['client_uuid']}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


@_saved_flow(1)
def _flow1_imported_impl(args, client, record, login_snapshot, login_resp):
    """Saved fresh account: continue the recorded flow1 after startup."""
    login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    avatar = login_user_model["avatar"]
    if not avatar:
        raise RuntimeError("login/login did not return avatar data")
    step = _StepPrinter(14)

    step("metric/tutorial #1")
    resp = client.metric_tutorial()
    _check(resp, "metric/tutorial #1")

    step("metric/tutorial #2")
    resp = client.metric_tutorial()
    _check(resp, "metric/tutorial #2")

    step("tutorial/read (VoiceSetting)")
    resp = client.tutorial_read(TUTORIAL_STEP_VOICE_SETTING)
    _check(resp, "tutorial/read VoiceSetting")

    step("profile/set_user_name")
    resp = client.profile_set_user_name("Tester")
    _check(resp, "profile/set_user_name")

    step("profile/set_user_name (confirm)")
    resp = client.profile_set_user_name("Tester")
    _check(resp, "profile/set_user_name confirm")

    step("avatar/save")
    resp = client.avatar_save(
        avatar_id=avatar["AvatarMasterId"],
        body_id=avatar["BodyMasterId"],
        face_id=avatar["FaceMasterId"],
        eye_color_id=avatar["EyeColorMasterId"],
        skin_color_id=avatar["SkinColorMasterId"],
        hair_id=avatar["HairMasterId"],
        hair_color_id=avatar["HairColorMasterId"],
        voice_id=avatar["VoiceMasterId"],
    )
    _check(resp, "avatar/save")

    step("in_game/start_tutorial")
    resp = client.in_game_start_tutorial()
    _check(resp, "in_game/start_tutorial")

    step("metric/adventure_skip (battle cutscenes)")
    for adv_id, cmd_idx in [(110, 27), (140, 22), (141, 22), (220, 15), (240, 24)]:
        resp = client.metric_adventure_skip(adv_id, cmd_idx)
        _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

    step("adventure/read (battle sequence)")
    for adv_id in [290, 310, 330, 340]:
        resp = client.adventure_read(adv_id)
        _check(resp, f"adventure/read({adv_id})")

    step("adventure/read + metric (3601)")
    resp = client.adventure_read(3601)
    _check(resp, "adventure/read(3601)")
    resp = client.metric_adventure_skip(3601, 20)
    _check(resp, "metric/adventure_skip(3601,20)")

    step("adventure/read (3602, 410)")
    resp = client.adventure_read(3602)
    _check(resp, "adventure/read(3602)")
    resp = client.adventure_read(410)
    _check(resp, "adventure/read(410)")

    step("in_game/result_tutorial")
    resp = client.in_game_result_tutorial()
    _check(resp, "in_game/result_tutorial")

    step("adventure/read + metric (100001000001001)")
    resp = client.adventure_read(100001000001001)
    _check(resp, "adventure/read(100001000001001)")
    resp = client.metric_adventure_skip(100001000001001, 18)
    _check(resp, "metric/adventure_skip(100001000001001,18)")

    step("tutorial/read (ResumePrevStageFirst)")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_STAGE_FIRST)
    _check(resp, "tutorial/read ResumePrevStageFirst")

    saved = _save_client_account(
        client,
        args,
        progress="flow1_complete",
        last_command="flow1-imported",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow1 imported complete.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_flow1_imported(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow1_imported_impl(args, client, record, login_snapshot, login_resp)


_STAGE_1_1 = 10101101
_GACHA_TUTORIAL = 800000101

_BATTLE_PRE_CUTSCENES = [
    (100001010012001, 29),
]
_BATTLE_MID_READS = [
    900003010012001,
]
_BATTLE_POST_CUTSCENES = [
    (100001010012003, 19),
    (100001010012004, 26),
]
_RESULT_CUTSCENES = [
    (100001010011001, 25),
]
_HOME_CUTSCENES = [
    (100001010011004, 25),
]


@_saved_flow(2)
def _flow2_impl(args, client, record, login_snapshot, login_resp):
    """Chapter 1-1: battle -> gacha -> equip weapon -> home unlock."""
    resume_session_id = login_resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, login_resp, context="flow2 stage 1-1")
        resume_session_id = None
    step = _StepPrinter(23)
    login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    deck = login_user_model["deck"]
    if not deck:
        raise RuntimeError("login/login did not return deck data")
    tutorial_weapon_id = None

    if resume_session_id:
        step(f"resume existing stage session {resume_session_id}")
    else:
        step("tutorial/read (StageFirst)")
        resp = client.tutorial_read(TUTORIAL_STEP_STAGE_FIRST)
        _check(resp, "tutorial/read StageFirst")

        step("in_game/start (Stage 1-1)")
        resp = client.in_game_start(_STAGE_1_1, deck_index=1)
        _check(resp, "in_game/start")

        step("adventure cutscenes (pre-battle)")
        for adv_id, cmd_idx in _BATTLE_PRE_CUTSCENES:
            resp = client.adventure_read(adv_id)
            _check(resp, f"adventure/read({adv_id})")
            resp = client.metric_adventure_skip(adv_id, cmd_idx)
            _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

        step("adventure/read (battle mid)")
        for adv_id in _BATTLE_MID_READS:
            resp = client.adventure_read(adv_id)
            _check(resp, f"adventure/read({adv_id})")

        step("adventure cutscenes (post-battle)")
        for adv_id, cmd_idx in _BATTLE_POST_CUTSCENES:
            resp = client.adventure_read(adv_id)
            _check(resp, f"adventure/read({adv_id})")
            resp = client.metric_adventure_skip(adv_id, cmd_idx)
            _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

    step("in_game/result")
    resp = _submit_in_game_result_with_resume(
        client,
        stage_master_id=_STAGE_1_1,
        in_game_session_id=resume_session_id,
    )
    _check(resp, "in_game/result")

    step("adventure cutscenes (post-result)")
    for adv_id, cmd_idx in _RESULT_CUTSCENES:
        resp = client.adventure_read(adv_id)
        _check(resp, f"adventure/read({adv_id})")
        resp = client.metric_adventure_skip(adv_id, cmd_idx)
        _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

    step("tutorial/read (ResumePrevGacha)")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_GACHA)
    _check(resp, "tutorial/read ResumePrevGacha")

    step("gacha/fetch_top")
    resp = client.gacha_fetch_top()
    _check(resp, "gacha/fetch_top")

    step("tutorial/read (Gacha)")
    resp = client.tutorial_read(TUTORIAL_STEP_GACHA)
    _check(resp, "tutorial/read Gacha")

    step("gacha/draw (tutorial)")
    resp = client.gacha_draw(_GACHA_TUTORIAL)
    _check(resp, "gacha/draw")
    tutorial_weapon_id = _extract_tutorial_gacha_user_weapon_id(client.last_response_raw)
    print(f"  tutorial UserWeaponId={tutorial_weapon_id}")

    step("tutorial/read (ResumeGachaResult)")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_GACHA_RESULT)
    _check(resp, "tutorial/read ResumeGachaResult")

    step("gacha/fetch_list")
    resp = client.gacha_fetch_list()
    _check(resp, "gacha/fetch_list")

    step("tutorial/read (ResumePrevDeckEdit)")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_DECK_EDIT)
    _check(resp, "tutorial/read ResumePrevDeckEdit")

    step("deck/save_style_equipment")
    if tutorial_weapon_id is None:
        raise RuntimeError("missing tutorial UserWeaponId from gacha/draw response")
    raw_equip = _build_deck_save_style_equipment_request(
        user_deck_id=deck["UserDeckId"],
        style_index=1,
        user_weapon_id=tutorial_weapon_id,
        user_armor_shield_id=deck["UserArmorShieldId1"],
        user_armor_head_id=deck["UserArmorHeadId1"],
        user_armor_upper_id=deck["UserArmorUpperId1"],
        user_armor_lower_id=deck["UserArmorLowerId1"],
    )
    resp = client.deck_save_style_equipment(raw_equip)
    _check(resp, "deck/save_style_equipment")
    _run_flow2_after_deck_edit(client, step)

    saved = _save_client_account(
        client,
        args,
        progress="flow2_complete",
        last_command="flow2",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow2 complete. Chapter 1-1 cleared, weapon equipped.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_flow2(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow2_impl(args, client, record, login_snapshot, login_resp)


_TEMPLATE_STAGE = 10101101
_STAGE_1_2 = 10101102
_STAGE_1_3 = 10101103
_FLOW3_NOTICE_IDS = [
    10560, 12873, 13596, 32632, 7556, 19409, 48176,
    68245, 26582, 80809, 62197, 91104, 17572, 90076,
]
_FLOW3_RELEASE_FUNCTION_ID = 204
_FLOW3_PLAYABLE_GUIDE_ID = 110
_FLOW3_FEATURE_INTRO_ID = 9
_FLOW3_STAGE_1_2_ADVENTURE = 100001010021001
_FLOW3_STAGE_1_2_SKIP_INDEX = 13
_FLOW3_POST_STAGE_1_2_ADVENTURE = 100001010021002
_FLOW3_POST_STAGE_1_2_SKIP_INDEX = 24
_FLOW3_STAGE_1_3_ADVENTURE = 100001010031001
_FLOW3_STAGE_1_3_SKIP_INDEX = 33
_STAGE_1_4 = 10101104
_STAGE_1_5 = 10101105
_STAGE_1_6 = 10101106
_STAGE_1_7 = 10101107
_STAGE_1_8 = 10101108
_STAGE_1_9 = 10101109
_STAGE_1_10 = 10101110
_STAGE_H1_1 = 10101201
_STAGE_H1_2 = 10101202
_STAGE_H1_3 = 10101203
_STAGE_H1_4 = 10101204
_STAGE_H1_5 = 10101205
_STAGE_H1_6 = 10101206
_STAGE_H1_7 = 10101207
_STAGE_H1_8 = 10101208
_STAGE_H1_9 = 10101209
_STAGE_H1_10 = 10101210
_FLOW4_ADVENTURE = 100001010031002
_FLOW4_ADVENTURE_SKIP_INDEX = 25
_FLOW4_PLAYABLE_GUIDE_PRE_STAGE = 120
_FLOW4_FEATURE_INTRO_PRE_STAGE = 13
_FLOW4_PLAYABLE_GUIDE_POST_STAGE = 130
_FLOW4_RELEASE_FUNCTION_IDS_PRE = [206, 203]
_FLOW4_FEATURE_INTRO_POST_STAGE = 14
_FLOW4_AREA_ACHIEVEMENT_IDS = [101101, 101102, 101103]
_FLOW6_AREA_ACHIEVEMENT_IDS = [101201, 101202, 101203]
_FLOW4_DAILY_MISSION_IDS = [10001, 10002, 10003, 10004, 10005]
_FLOW4_DAILY_REWARD_RAW_FIRST = bytes.fromhex("05000000112700001227000013270000142700001527000000000000")
_FLOW4_DAILY_REWARD_RAW_SECOND = bytes.fromhex("00000000050000001127000012270000132700001427000015270000")
_FLOW4_EVENT_MISSION_IDS = [301001, 301026]
_FLOW4_PRESENT_IDS_FIRST = [
    75, 47, 49, 51, 53, 55, 57, 59, 61, 63, 65, 67, 69, 71, 73,
    45, 43, 37, 39, 41, 29, 31, 33, 35, 21, 23, 25, 27,
]
_FLOW4_MISSION_PANEL_ID = 901
_FLOW4_RELEASE_FUNCTION_IDS_POST = []
_FLOW4_FEATURE_INTRO_IDS_POST = [27]
_FLOW4_ALBUM_ORB_REWARD_IDS = [200001, 200011, 300001]
_FLOW4_ALBUM_ENEMY_KILL_REWARD_IDS = [101]
_FLOW4_PRESENT_IDS_SECOND = [79, 81, 77]

_BATTLE_STAGE_CONFIG = {
    "1-1": {
        "stage_master_id": _STAGE_1_1,
        "template_stage_id": _STAGE_1_1,
        "before_start": [
            ("tutorial_read", TUTORIAL_STEP_STAGE_FIRST),
        ],
        "after_start": [
            ("adventure_read", 100001010012001),
            ("metric_adventure_skip", (100001010012001, 29)),
            ("adventure_read", 900003010012001),
            ("adventure_read", 100001010012003),
            ("metric_adventure_skip", (100001010012003, 19)),
            ("adventure_read", 100001010012004),
            ("metric_adventure_skip", (100001010012004, 26)),
        ],
    },
    "1-2": {
        "stage_master_id": _STAGE_1_2,
        "template_stage_id": _STAGE_1_2,
        "before_start": [],
        "after_start": [
            ("adventure_read", _FLOW3_STAGE_1_2_ADVENTURE),
            ("metric_adventure_skip", (_FLOW3_STAGE_1_2_ADVENTURE, _FLOW3_STAGE_1_2_SKIP_INDEX)),
        ],
    },
    "1-3": {
        "stage_master_id": _STAGE_1_3,
        "template_stage_id": _STAGE_1_3,
        "before_start": [],
        "after_start": [
            ("adventure_read", _FLOW3_STAGE_1_3_ADVENTURE),
            ("metric_adventure_skip", (_FLOW3_STAGE_1_3_ADVENTURE, _FLOW3_STAGE_1_3_SKIP_INDEX)),
        ],
    },
    "1-4": {
        "stage_master_id": _STAGE_1_4,
        "template_stage_id": _STAGE_1_3,
        "before_start": [
            ("adventure_read", _FLOW4_ADVENTURE),
            ("metric_adventure_skip", (_FLOW4_ADVENTURE, _FLOW4_ADVENTURE_SKIP_INDEX)),
            ("playable_guide_read", _FLOW4_PLAYABLE_GUIDE_PRE_STAGE),
            ("feature_intro_read", _FLOW4_FEATURE_INTRO_PRE_STAGE),
            ("deck_save_auto_style_equipment", None),
        ],
        "after_start": [],
    },
    "1-5": {
        "stage_master_id": _STAGE_1_5,
        "template_stage_id": _STAGE_1_4,
        "before_start": [],
        "after_start": [],
    },
    "1-6": {
        "stage_master_id": _STAGE_1_6,
        "template_stage_id": _STAGE_1_4,
        "before_start": [],
        "after_start": [],
    },
    "1-7": {
        "stage_master_id": _STAGE_1_7,
        "template_stage_id": _STAGE_1_4,
        "before_start": [],
        "after_start": [],
    },
    "1-8": {
        "stage_master_id": _STAGE_1_8,
        "template_stage_id": _STAGE_1_4,
        "before_start": [],
        "after_start": [],
    },
    "1-9": {
        "stage_master_id": _STAGE_1_9,
        "template_stage_id": _STAGE_1_4,
        "before_start": [],
        "after_start": [],
    },
    "1-10": {
        "stage_master_id": _STAGE_1_10,
        "template_stage_id": _STAGE_1_4,
        "before_start": [],
        "after_start": [],
    },
    "h1-1": {
        "stage_master_id": _STAGE_H1_1,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [
            ("feature_intro_read", 4),
            ("feature_intro_read", 1),
            ("feature_intro_read", 3),
        ],
    },
    "h1-2": {
        "stage_master_id": _STAGE_H1_2,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-3": {
        "stage_master_id": _STAGE_H1_3,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-4": {
        "stage_master_id": _STAGE_H1_4,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-5": {
        "stage_master_id": _STAGE_H1_5,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-6": {
        "stage_master_id": _STAGE_H1_6,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-7": {
        "stage_master_id": _STAGE_H1_7,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-8": {
        "stage_master_id": _STAGE_H1_8,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-9": {
        "stage_master_id": _STAGE_H1_9,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
    "h1-10": {
        "stage_master_id": _STAGE_H1_10,
        "template_stage_id": _STAGE_H1_1,
        "before_start": [],
        "after_start": [],
    },
}

_FLOW4_APPEND_STAGE_KEYS = ["1-5", "1-6", "1-7", "1-8", "1-9", "1-10"]
_FLOW6_STAGE_KEYS = [f"h1-{idx}" for idx in range(1, 11)]
_FLOW5_RELEASE_FUNCTION_IDS_PRE = [304]
_FLOW5_FEATURE_INTRO_IDS_PRE = [27]
_FLOW5_WEAPON_GROWTH_MATERIAL_IDS = _TRACKED_GROWTH_MATERIAL_IDS
_FLOW5_RELEASE_FUNCTION_ID_WEAPON = 101
_FLOW5_ADVENTURE = 100001010101001
_FLOW5_ADVENTURE_SKIP_INDEX = 12
_FLOW5_MAIN_AREA_UNLOCKS = [
    (101, 201),
    (102, 101),
]
_FLOW5_FEATURE_INTRO_ID_MAIN_AREA = 25
_FLOW5_RELEASE_FUNCTION_ID_EXPEDITION = 103
_FLOW5_FEATURE_INTRO_ID_EXPEDITION = 16
_FLOW5_RELEASE_FUNCTION_ID_POST_HOME = 303
_FLOW5_PLAYABLE_GUIDE_ID = 140
_FLOW5_LOW_FPS_SCENE_ID = "Expedition"
_FLOW5_LOW_FPS_CURRENT_FPS = 14.0
_FLOW5_LOW_FPS_DURATION = 3.01611328
_FLOW5_FEATURE_INTRO_ID_FINAL = 30


def _run_battle_stage_step(client: DQSGClient, login_response_raw: bytes | None, step: tuple):
    action, value = step
    if action == "tutorial_read":
        print(f"\n=== tutorial/read ({value}) ===")
        resp = client.tutorial_read(value)
        _check(resp, f"tutorial/read({value})")
        return
    if action == "adventure_read":
        print(f"\n=== adventure/read ({value}) ===")
        resp = client.adventure_read(value)
        _check(resp, f"adventure/read({value})")
        return
    if action == "metric_adventure_skip":
        adv_id, cmd_idx = value
        print(f"\n=== metric/adventure_skip ({adv_id},{cmd_idx}) ===")
        resp = client.metric_adventure_skip(adv_id, cmd_idx)
        _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")
        return
    if action == "playable_guide_read":
        print(f"\n=== playable_guide/read ({value}) ===")
        resp = client.playable_guide_read(value)
        _check(resp, f"playable_guide/read({value})")
        return
    if action == "feature_intro_read":
        print(f"\n=== feature_intro/read ({value}) ===")
        resp = client.feature_intro_read(value)
        _check(resp, f"feature_intro/read({value})")
        return
    if action == "deck_save_auto_style_equipment":
        if not login_response_raw:
            raise RuntimeError("login/login response is required for deck/save_auto_style_equipment")
        print("\n=== deck/save_auto_style_equipment ===")
        resp = client.deck_save_auto_style_equipment(
            _build_flow4_auto_style_equipment_request(login_response_raw)
        )
        _check(resp, "deck/save_auto_style_equipment")
        return
    raise RuntimeError(f"Unsupported battle stage step: {action}")


def _run_battle_stage(client: DQSGClient, stage_key: str, login_response_raw: bytes | None):
    config = _BATTLE_STAGE_CONFIG[stage_key]
    stage_master_id = config["stage_master_id"]
    template_stage_id = config["template_stage_id"]
    print(f"\n=== stage {stage_key} ({stage_master_id}) ===")
    for step in config["before_start"]:
        _run_battle_stage_step(client, login_response_raw, step)
    print(f"\n=== in_game/start ({stage_master_id}) ===")
    resp = client.in_game_start(stage_master_id, deck_index=1)
    _check(resp, f"in_game/start({stage_master_id})")
    for step in config["after_start"]:
        _run_battle_stage_step(client, login_response_raw, step)
    print(f"\n=== in_game/result ({stage_master_id}) ===")
    resp = _submit_in_game_result_with_resume(
        client,
        stage_master_id=stage_master_id,
        template_stage_id=template_stage_id,
    )
    _check(resp, f"in_game/result({stage_master_id})")


def cmd_avatar_save(args):
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)

    print("\n=== avatar/save ===")
    resp = client.avatar_save(
        avatar_id=args.avatar_id,
        body_id=args.body_id,
        face_id=args.face_id,
        eye_color_id=args.eye_color_id,
        skin_color_id=args.skin_color_id,
        hair_id=args.hair_id,
        hair_color_id=args.hair_color_id,
        voice_id=args.voice_id,
    )
    _check(resp, "avatar/save")
    _save_client_account(client, args, last_command="avatar-save", snapshot=login_snapshot)
    print("avatar/save succeeded.")


def cmd_flow1_post_avatar(args):
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)

    print("\n=== in_game/start_tutorial ===")
    resp = client.in_game_start_tutorial()
    _check(resp, "in_game/start_tutorial")

    print("\n=== metric/adventure_skip (battle cutscenes) ===")
    for adv_id, cmd_idx in [(110, 27), (140, 22), (141, 22), (220, 15), (240, 24)]:
        resp = client.metric_adventure_skip(adv_id, cmd_idx)
        _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

    print("\n=== adventure/read (battle sequence) ===")
    for adv_id in [290, 310, 330, 340]:
        resp = client.adventure_read(adv_id)
        _check(resp, f"adventure/read({adv_id})")

    print("\n=== adventure/read + metric (3601) ===")
    resp = client.adventure_read(3601)
    _check(resp, "adventure/read(3601)")
    resp = client.metric_adventure_skip(3601, 20)
    _check(resp, "metric/adventure_skip(3601,20)")

    print("\n=== adventure/read (3602, 410) ===")
    resp = client.adventure_read(3602)
    _check(resp, "adventure/read(3602)")
    resp = client.adventure_read(410)
    _check(resp, "adventure/read(410)")

    print("\n=== in_game/result_tutorial ===")
    resp = client.in_game_result_tutorial()
    _check(resp, "in_game/result_tutorial")

    print("\n=== adventure/read + metric (100001000001001) ===")
    resp = client.adventure_read(100001000001001)
    _check(resp, "adventure/read(100001000001001)")
    resp = client.metric_adventure_skip(100001000001001, 18)
    _check(resp, "metric/adventure_skip(100001000001001,18)")

    print("\n=== tutorial/read (ResumePrevStageFirst) ===")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_STAGE_FIRST)
    _check(resp, "tutorial/read ResumePrevStageFirst")

    saved = _save_client_account(
        client,
        args,
        progress="flow1_complete",
        last_command="flow1-post-avatar",
        snapshot=login_snapshot,
    )
    print("\n" + "=" * 50)
    print("Flow1 post-avatar complete.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_dump_login_response(args):
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)

    if not client.last_login_response_raw:
        raise SystemExit("No login response payload captured")

    out_path = Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(client.last_login_response_raw)
    print(f"wrote {len(client.last_login_response_raw)} bytes to {out_path}")


def cmd_probe_billing_web_store(args):
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")

    print("\n=== billing/update_web_store ===")
    resp = client.billing_update_web_store()
    _check(resp, "billing/update_web_store")

    if not client.last_response_raw:
        raise SystemExit("No billing/update_web_store payload captured")

    out_path = Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(client.last_response_raw)
    print(f"wrote {len(client.last_response_raw)} bytes to {out_path}")

    values = args.needle_int or []
    if values:
        print("\nint32 matches:")
        for value in values:
            hits = _find_int32_occurrences(client.last_response_raw, value)
            hit_text = ", ".join(str(hit) for hit in hits[:20]) if hits else "(none)"
            if len(hits) > 20:
                hit_text += ", ..."
            print(f"  {value}: {len(hits)} hit(s) at {hit_text}")


def cmd_dump_flow2_start(args):
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    _surrender_login_resume_for_battle(client, resp, context="dump-flow2-start")

    print("\n=== tutorial/read (StageFirst) ===")
    resp = client.tutorial_read(TUTORIAL_STEP_STAGE_FIRST)
    _check(resp, "tutorial/read StageFirst")

    print("\n=== in_game/start (Stage 1-1) ===")
    resp = client.in_game_start(_STAGE_1_1, deck_index=1)
    _check(resp, "in_game/start")

    if client.last_response_endpoint != "in_game/start" or not client.last_response_raw:
        raise SystemExit("No in_game/start payload captured")

    out_path = Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(client.last_response_raw)
    print(f"wrote {len(client.last_response_raw)} bytes to {out_path}")


def _run_flow2_after_deck_edit(client: DQSGClient, step: _StepPrinter | None = None):
    if step:
        step("tutorial/read (ResumePrevHomeUnlock)")
    else:
        print("\n=== tutorial/read (ResumePrevHomeUnlock) ===")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_HOME_UNLOCK)
    _check(resp, "tutorial/read ResumePrevHomeUnlock")

    if step:
        step("adventure cutscenes (home intro)")
    else:
        print("\n=== adventure cutscenes (home intro) ===")
    for adv_id, cmd_idx in _HOME_CUTSCENES:
        resp = client.adventure_read(adv_id)
        _check(resp, f"adventure/read({adv_id})")
        resp = client.metric_adventure_skip(adv_id, cmd_idx)
        _check(resp, f"metric/adventure_skip({adv_id},{cmd_idx})")

    if step:
        step("tutorial/read (ResumeHomeUnlock)")
    else:
        print("\n=== tutorial/read (ResumeHomeUnlock) ===")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_HOME_UNLOCK)
    _check(resp, "tutorial/read ResumeHomeUnlock")

    if step:
        step("metric/device")
    else:
        print("\n=== metric/device ===")
    resp = client.metric_device()
    _check(resp, "metric/device")

    if step:
        step("playable_guide/read(100)")
    else:
        print("\n=== playable_guide/read(100) ===")
    resp = client.playable_guide_read(100)
    _check(resp, "playable_guide/read")

    if step:
        step("feature_intro/read (StageInfo)")
    else:
        print("\n=== feature_intro/read (StageInfo) ===")
    resp = client.feature_intro_read(FEATURE_INTRO_STAGE_INFO)
    _check(resp, "feature_intro/read StageInfo")

    if step:
        step("feature_intro/read (HomeMenu)")
    else:
        print("\n=== feature_intro/read (HomeMenu) ===")
    resp = client.feature_intro_read(FEATURE_INTRO_HOME_MENU)
    _check(resp, "feature_intro/read HomeMenu")

    if step:
        step("notice/fetch_notice_detail")
    else:
        print("\n=== notice/fetch_notice_detail ===")
    resp = client.notice_fetch_detail(17572)
    _check(resp, "notice/fetch_notice_detail")


def cmd_flow2_post_gacha(args):
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    deck = login_user_model["deck"]
    if not deck:
        raise RuntimeError("login/login did not return deck data")

    print("\n=== tutorial/read (ResumePrevDeckEdit) ===")
    resp = client.tutorial_read(TUTORIAL_STEP_RESUME_PREV_DECK_EDIT)
    _check(resp, "tutorial/read ResumePrevDeckEdit")

    print("\n=== deck/save_style_equipment ===")
    raw_equip = _build_deck_save_style_equipment_request(
        user_deck_id=deck["UserDeckId"],
        style_index=1,
        user_weapon_id=args.user_weapon_id,
        user_armor_shield_id=deck["UserArmorShieldId1"],
        user_armor_head_id=deck["UserArmorHeadId1"],
        user_armor_upper_id=deck["UserArmorUpperId1"],
        user_armor_lower_id=deck["UserArmorLowerId1"],
    )
    resp = client.deck_save_style_equipment(raw_equip)
    _check(resp, "deck/save_style_equipment")

    _run_flow2_after_deck_edit(client)

    saved = _save_client_account(
        client,
        args,
        progress="flow2_complete",
        last_command="flow2-post-gacha",
        snapshot=login_snapshot,
    )
    print("\n" + "=" * 50)
    print("Flow2 post-gacha complete.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


@_saved_flow(3)
def _flow3_impl(args, client, record, login_snapshot, login_resp):
    """Progress a saved account through the recorded 1-2 -> 1-3 flow."""
    progress = record.get("progress")
    resume_session_id = login_resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, login_resp, context="flow3 stage")
        resume_session_id = None
    step = _StepPrinter(11 if progress != "flow3_stage_1_2" else 2)

    if progress != "flow3_stage_1_2":
        step("notice/fetch_notices")
        resp = client.notice_fetch_notices()
        _check(resp, "notice/fetch_notices")

        step("notice/read_all_normal_notices")
        resp = client.notice_read_all_normal_notices(_FLOW3_NOTICE_IDS)
        _check(resp, "notice/read_all_normal_notices")

        step(f"stage 1-2 ({_STAGE_1_2})")
        if resume_session_id is not None:
            print(f"  resuming existing stage session {resume_session_id}")
        else:
            print("  starting new battle")
            resp = client.in_game_start(_STAGE_1_2, deck_index=1)
            _check(resp, "in_game/start(10101102)")

            print("  adventure/read(100001010021001)")
            resp = client.adventure_read(_FLOW3_STAGE_1_2_ADVENTURE)
            _check(resp, "adventure/read(100001010021001)")

            print("  metric/adventure_skip(100001010021001,13)")
            resp = client.metric_adventure_skip(
                _FLOW3_STAGE_1_2_ADVENTURE,
                _FLOW3_STAGE_1_2_SKIP_INDEX,
            )
            _check(resp, "metric/adventure_skip(100001010021001,13)")

        step("in_game/result (10101102)")
        resp = _submit_in_game_result_with_resume(
            client,
            stage_master_id=_STAGE_1_2,
            template_stage_id=_STAGE_1_2,
            in_game_session_id=resume_session_id,
        )
        _check(resp, "in_game/result(10101102)")
        resume_session_id = None
        saved = _save_client_account(
            client,
            args,
            progress="flow3_stage_1_2",
            last_command="flow3",
            snapshot=login_snapshot,
        )

        step("adventure/read (100001010021002)")
        resp = client.adventure_read(_FLOW3_POST_STAGE_1_2_ADVENTURE)
        _check(resp, "adventure/read(100001010021002)")

        step("metric/adventure_skip (100001010021002,24)")
        resp = client.metric_adventure_skip(
            _FLOW3_POST_STAGE_1_2_ADVENTURE,
            _FLOW3_POST_STAGE_1_2_SKIP_INDEX,
        )
        _check(resp, "metric/adventure_skip(100001010021002,24)")

        step("release_function/unlock(204)")
        _release_function_unlock_allow_500(client, _FLOW3_RELEASE_FUNCTION_ID)

        step("playable_guide/read(110)")
        resp = client.playable_guide_read(_FLOW3_PLAYABLE_GUIDE_ID)
        _check(resp, "playable_guide/read(110)")

        step("feature_intro/read(9)")
        resp = client.feature_intro_read(_FLOW3_FEATURE_INTRO_ID)
        _check(resp, "feature_intro/read(9)")

        print(f"\nSaved progress at {_account_ref(saved)} -> flow3_stage_1_2")

    step(f"stage 1-3 ({_STAGE_1_3})")
    if progress == "flow3_stage_1_2" and resume_session_id is not None:
        print(f"  resuming existing stage session {resume_session_id}")
    else:
        print("  starting new battle")
        resp = client.in_game_start(_STAGE_1_3, deck_index=1)
        _check(resp, "in_game/start(10101103)")

        print("  adventure/read(100001010031001)")
        resp = client.adventure_read(_FLOW3_STAGE_1_3_ADVENTURE)
        _check(resp, "adventure/read(100001010031001)")

        print("  metric/adventure_skip(100001010031001,33)")
        resp = client.metric_adventure_skip(
            _FLOW3_STAGE_1_3_ADVENTURE,
            _FLOW3_STAGE_1_3_SKIP_INDEX,
        )
        _check(resp, "metric/adventure_skip(100001010031001,33)")

    step("in_game/result (10101103)")
    resp = _submit_in_game_result_with_resume(
        client,
        stage_master_id=_STAGE_1_3,
        template_stage_id=_STAGE_1_3,
        in_game_session_id=resume_session_id if progress == "flow3_stage_1_2" else None,
    )
    _check(resp, "in_game/result(10101103)")

    saved = _save_client_account(
        client,
        args,
        progress="flow3_complete",
        last_command="flow3",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow3 complete. Recorded 1-2 -> 1-3 flow cleared.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_flow3(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow3_impl(args, client, record, login_snapshot, login_resp)


@_saved_flow(4)
def _flow4_impl(args, client, record, login_snapshot, login_resp):
    """Progress a saved account through the recorded 1-4 + rewards/tutorial flow."""
    _surrender_login_resume_for_battle(client, login_resp, context="flow4")
    step = _StepPrinter(30)

    step(f"adventure/read({_FLOW4_ADVENTURE})")
    resp = client.adventure_read(_FLOW4_ADVENTURE)
    _check(resp, f"adventure/read({_FLOW4_ADVENTURE})")

    step(f"metric/adventure_skip({_FLOW4_ADVENTURE},{_FLOW4_ADVENTURE_SKIP_INDEX})")
    resp = client.metric_adventure_skip(_FLOW4_ADVENTURE, _FLOW4_ADVENTURE_SKIP_INDEX)
    _check(resp, f"metric/adventure_skip({_FLOW4_ADVENTURE},{_FLOW4_ADVENTURE_SKIP_INDEX})")

    step(f"playable_guide/read({_FLOW4_PLAYABLE_GUIDE_PRE_STAGE})")
    resp = client.playable_guide_read(_FLOW4_PLAYABLE_GUIDE_PRE_STAGE)
    _check(resp, f"playable_guide/read({_FLOW4_PLAYABLE_GUIDE_PRE_STAGE})")

    step(f"feature_intro/read({_FLOW4_FEATURE_INTRO_PRE_STAGE})")
    resp = client.feature_intro_read(_FLOW4_FEATURE_INTRO_PRE_STAGE)
    _check(resp, f"feature_intro/read({_FLOW4_FEATURE_INTRO_PRE_STAGE})")

    step("deck/save_auto_style_equipment")
    resp = client.deck_save_auto_style_equipment(
        _build_flow4_auto_style_equipment_request(client.last_login_response_raw)
    )
    _check(resp, "deck/save_auto_style_equipment")

    step(f"in_game/start({_STAGE_1_4})")
    resp = client.in_game_start(_STAGE_1_4, deck_index=1)
    _check(resp, f"in_game/start({_STAGE_1_4})")

    step(f"in_game/result({_STAGE_1_4})")
    resp = _submit_in_game_result_with_resume(
        client,
        stage_master_id=_STAGE_1_4,
        template_stage_id=_STAGE_1_3,
    )
    _check(resp, f"in_game/result({_STAGE_1_4})")

    step(f"playable_guide/read({_FLOW4_PLAYABLE_GUIDE_POST_STAGE})")
    resp = client.playable_guide_read(_FLOW4_PLAYABLE_GUIDE_POST_STAGE)
    _check(resp, f"playable_guide/read({_FLOW4_PLAYABLE_GUIDE_POST_STAGE})")

    for function_id in _FLOW4_RELEASE_FUNCTION_IDS_PRE:
        step(f"release_function/unlock({function_id})")
        _release_function_unlock_allow_500(client, function_id)

    step(f"feature_intro/read({_FLOW4_FEATURE_INTRO_POST_STAGE})")
    resp = client.feature_intro_read(_FLOW4_FEATURE_INTRO_POST_STAGE)
    _check(resp, f"feature_intro/read({_FLOW4_FEATURE_INTRO_POST_STAGE})")

    step("notice/fetch_notices")
    resp = client.notice_fetch_notices()
    _check(resp, "notice/fetch_notices")

    step("notice/read_all_normal_notices")
    resp = client.notice_read_all_normal_notices(_FLOW3_NOTICE_IDS)
    _check(resp, "notice/read_all_normal_notices")

    step("mission_panel/fetch_mission")
    resp = client.mission_panel_fetch(_FLOW4_MISSION_PANEL_ID)
    _check(resp, "mission_panel/fetch_mission")

    step("mission_panel/receive_mission_reward")
    resp = client.mission_panel_receive_reward(_FLOW4_MISSION_PANEL_ID)
    _check(resp, "mission_panel/receive_mission_reward")

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    for function_id in _FLOW4_RELEASE_FUNCTION_IDS_POST:
        step(f"release_function/unlock({function_id})")
        _release_function_unlock_allow_500(client, function_id)

    for feature_intro_id in _FLOW4_FEATURE_INTRO_IDS_POST:
        step(f"feature_intro/read({feature_intro_id})")
        resp = client.feature_intro_read(feature_intro_id)
        _check(resp, f"feature_intro/read({feature_intro_id})")

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    step("album/receive_enemy_kill_count_reward")
    resp = client.album_receive_enemy_kill_count_reward(_FLOW4_ALBUM_ENEMY_KILL_REWARD_IDS)
    _check(resp, "album/receive_enemy_kill_count_reward")

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    for stage_key in _FLOW4_APPEND_STAGE_KEYS:
        step(f"battle-stage {stage_key}")
        _run_battle_stage(client, stage_key, client.last_login_response_raw)

    step(f"adventure/read({_FLOW5_ADVENTURE})")
    resp = client.adventure_read(_FLOW5_ADVENTURE)
    _check(resp, f"adventure/read({_FLOW5_ADVENTURE})")

    step(f"metric/adventure_skip({_FLOW5_ADVENTURE},{_FLOW5_ADVENTURE_SKIP_INDEX})")
    resp = client.metric_adventure_skip(_FLOW5_ADVENTURE, _FLOW5_ADVENTURE_SKIP_INDEX)
    _check(resp, f"metric/adventure_skip({_FLOW5_ADVENTURE},{_FLOW5_ADVENTURE_SKIP_INDEX})")

    step("area/receive_achievement_reward")
    resp = client.area_receive_achievement_reward(_FLOW4_AREA_ACHIEVEMENT_IDS)
    _check(resp, "area/receive_achievement_reward")

    saved = _save_client_account(
        client,
        args,
        progress="flow4_complete",
        last_command="flow4",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow4 complete. Recorded 1-4 through 1-10 flow cleared.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_flow4(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow4_impl(args, client, record, login_snapshot, login_resp)


@_saved_flow(5)
def _flow5_impl(args, client, record, login_snapshot, login_resp):
    """Progress a saved account through the recorded post-1-10 flow."""
    login_user_model = _parse_login_user_model_basics(client.last_login_response_raw)
    deck = login_user_model.get("deck")
    if not deck or not deck.get("UserWeaponId1"):
        raise RuntimeError("login/login did not return an equipped weapon for flow5")
    step = _StepPrinter(20)

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    for function_id in _FLOW5_RELEASE_FUNCTION_IDS_PRE:
        step(f"release_function/unlock({function_id})")
        _release_function_unlock_allow_500(client, function_id)

    for feature_intro_id in _FLOW5_FEATURE_INTRO_IDS_PRE:
        step(f"feature_intro/read({feature_intro_id})")
        resp = client.feature_intro_read(feature_intro_id)
        _check(resp, f"feature_intro/read({feature_intro_id})")

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    step("album/receive_enemy_kill_count_reward")
    resp = client.album_receive_enemy_kill_count_reward(_FLOW4_ALBUM_ENEMY_KILL_REWARD_IDS)
    _check(resp, "album/receive_enemy_kill_count_reward")

    step("profile/fetch")
    resp = client.profile_fetch()
    _check(resp, "profile/fetch")

    step("mission_panel/fetch_mission")
    resp = client.mission_panel_fetch(_FLOW4_MISSION_PANEL_ID)
    _check(resp, "mission_panel/fetch_mission")

    step("mission_panel/receive_mission_reward")
    resp = client.mission_panel_receive_reward(_FLOW4_MISSION_PANEL_ID)
    _check(resp, "mission_panel/receive_mission_reward")

    step("feature_intro/read(10)")
    resp = client.feature_intro_read(10)
    _check(resp, "feature_intro/read(10)")

    step("weapon/growth_level")
    growth_materials = _parse_growth_material_amounts_from_login_response(
        client.last_login_response_raw,
        _FLOW5_WEAPON_GROWTH_MATERIAL_IDS,
    )
    consume_contents = [
        (1010, master_id, growth_materials.get(master_id, 0))
        for master_id in _FLOW5_WEAPON_GROWTH_MATERIAL_IDS
        if growth_materials.get(master_id, 0) > 0
    ]
    if consume_contents:
        print(f"  using growth materials: {consume_contents}")
        resp = client.weapon_growth_level(deck["UserWeaponId1"], consume_contents)
        _check(resp, "weapon/growth_level")
    else:
        print("  no tracked growth materials found in login/login; skipping")

    step(f"release_function/unlock({_FLOW5_RELEASE_FUNCTION_ID_WEAPON})")
    _release_function_unlock_allow_500(client, _FLOW5_RELEASE_FUNCTION_ID_WEAPON)

    for area_master_id, area_difficulty in _FLOW5_MAIN_AREA_UNLOCKS:
        step(f"main_area/read_unlock({area_master_id},{area_difficulty})")
        resp = client.main_area_read_unlock(area_master_id, area_difficulty)
        _check(resp, f"main_area/read_unlock({area_master_id},{area_difficulty})")

    step(f"feature_intro/read({_FLOW5_FEATURE_INTRO_ID_MAIN_AREA})")
    resp = client.feature_intro_read(_FLOW5_FEATURE_INTRO_ID_MAIN_AREA)
    _check(resp, f"feature_intro/read({_FLOW5_FEATURE_INTRO_ID_MAIN_AREA})")

    step(f"release_function/unlock({_FLOW5_RELEASE_FUNCTION_ID_EXPEDITION})")
    _release_function_unlock_allow_500(client, _FLOW5_RELEASE_FUNCTION_ID_EXPEDITION)

    step(f"feature_intro/read({_FLOW5_FEATURE_INTRO_ID_EXPEDITION})")
    resp = client.feature_intro_read(_FLOW5_FEATURE_INTRO_ID_EXPEDITION)
    _check(resp, f"feature_intro/read({_FLOW5_FEATURE_INTRO_ID_EXPEDITION})")

    step(f"release_function/unlock({_FLOW5_RELEASE_FUNCTION_ID_POST_HOME})")
    _release_function_unlock_allow_500(client, _FLOW5_RELEASE_FUNCTION_ID_POST_HOME)

    step(f"playable_guide/read({_FLOW5_PLAYABLE_GUIDE_ID})")
    resp = client.playable_guide_read(_FLOW5_PLAYABLE_GUIDE_ID)
    _check(resp, f"playable_guide/read({_FLOW5_PLAYABLE_GUIDE_ID})")

    step("metric/low_fps_prolonged")
    resp = client.metric_low_fps_prolonged(
        _FLOW5_LOW_FPS_CURRENT_FPS,
        _FLOW5_LOW_FPS_DURATION,
        _FLOW5_LOW_FPS_SCENE_ID,
    )
    _check(resp, "metric/low_fps_prolonged")

    step(f"feature_intro/read({_FLOW5_FEATURE_INTRO_ID_FINAL})")
    resp = client.feature_intro_read(_FLOW5_FEATURE_INTRO_ID_FINAL)
    _check(resp, f"feature_intro/read({_FLOW5_FEATURE_INTRO_ID_FINAL})")

    saved = _save_client_account(
        client,
        args,
        progress="flow5_complete",
        last_command="flow5",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow5 complete. Recorded post-1-10 flow cleared.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_flow5(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow5_impl(args, client, record, login_snapshot, login_resp)


@_saved_flow(6)
def _flow6_impl(args, client, record, login_snapshot, login_resp):
    """Progress a saved account through chapter-1 hard mode intro, stages, and rewards."""
    _surrender_login_resume_for_battle(client, login_resp, context="flow6")
    step = _StepPrinter(len(_FLOW6_STAGE_KEYS) + 1)
    for stage_key in _FLOW6_STAGE_KEYS:
        step(f"battle-stage {stage_key}")
        _run_battle_stage(client, stage_key, client.last_login_response_raw)

    step("area/receive_achievement_reward")
    resp = client.area_receive_achievement_reward(_FLOW6_AREA_ACHIEVEMENT_IDS)
    _check(resp, "area/receive_achievement_reward")

    saved = _save_client_account(
        client,
        args,
        progress="flow6_complete",
        last_command="flow6",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Flow6 complete. Recorded chapter-1 hard mode 1-1 through 1-10 cleared.")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_flow6(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow6_impl(args, client, record, login_snapshot, login_resp)


def cmd_battle_stage(args):
    client, record = _load_client_for_account(args)
    stage_key = args.stage
    config = _BATTLE_STAGE_CONFIG.get(stage_key)
    if config is None:
        supported = ", ".join(sorted(_BATTLE_STAGE_CONFIG))
        raise SystemExit(f"Unsupported stage '{stage_key}'. Supported: {supported}")

    stage_master_id = config["stage_master_id"]
    template_stage_id = config["template_stage_id"]
    result_stat_kwargs = _result_stat_override_kwargs(args)

    print(f"=== account {_account_ref(record)} ===")
    print("=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    resume_session_id = resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, resp, context=f"battle-stage {stage_key}")
        resume_session_id = None

    total_steps = 1 + len(config["before_start"]) + len(config["after_start"]) + (0 if resume_session_id is not None else 1)
    stepper = _StepPrinter(total_steps)

    if resume_session_id is not None:
        stepper(f"resume stage {stage_key} with InGameSessionId={resume_session_id}")
    else:
        for step_action in config["before_start"]:
            step_label = step_action[0].replace("_", "/")
            if step_action[1] is not None:
                step_label = f"{step_label}({step_action[1]})"
            stepper(step_label)
            _run_battle_stage_step(client, client.last_login_response_raw, step_action)

        stepper(f"in_game/start({stage_master_id})")
        resp = client.in_game_start(stage_master_id, deck_index=1)
        _check(resp, f"in_game/start({stage_master_id})")

        for step_action in config["after_start"]:
            step_label = step_action[0].replace("_", "/")
            if step_action[1] is not None:
                step_label = f"{step_label}({step_action[1]})"
            stepper(step_label)
            _run_battle_stage_step(client, client.last_login_response_raw, step_action)

    stepper(f"in_game/result({stage_master_id})")
    resp = _submit_in_game_result_with_resume(
        client,
        stage_master_id=stage_master_id,
        template_stage_id=template_stage_id,
        in_game_session_id=resume_session_id,
        **result_stat_kwargs,
    )
    _check(resp, f"in_game/result({stage_master_id})")

    saved = _save_client_account(
        client,
        args,
        progress=f"battle_{stage_key}_complete",
        last_command="battle-stage",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print(f"Battle stage complete: {stage_key}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def _parse_story_stage_key(stage_key: str) -> tuple[int, int]:
    text = stage_key.strip().lower()
    if "-" not in text:
        raise SystemExit(f"Invalid story stage '{stage_key}'. Use format like 2-1.")
    left, right = text.split("-", 1)
    if not left.isdigit() or not right.isdigit():
        raise SystemExit(f"Invalid story stage '{stage_key}'. Use format like 2-1.")
    chapter = int(left)
    stage = int(right)
    if chapter <= 0 or stage <= 0:
        raise SystemExit(f"Invalid story stage '{stage_key}'. Chapter and stage must be positive.")
    return chapter, stage


def _story_stage_master_id(chapter: int, stage: int, *, hard: bool) -> int:
    known_story_stage_ids = {
        (False, 2, 1): 10102101,
        (False, 2, 2): 10102151,
        (False, 2, 3): 10102102,
        (False, 2, 4): 10102304,
        (False, 2, 5): 10102103,
        (False, 2, 6): 10102153,
        (False, 2, 7): 10102104,
        (False, 2, 8): 10102154,
        (False, 2, 9): 10102105,
        (False, 2, 10): 10102155,
        (False, 2, 11): 10102351,
        (False, 2, 12): 10102156,
        (False, 2, 13): 10102107,
        (False, 2, 14): 10102157,
        (False, 2, 15): 10102355,
        (False, 2, 16): 10102158,
        (False, 2, 17): 10102109,
        (False, 2, 18): 10102159,
        (False, 2, 19): 10102110,
        (False, 2, 20): 10102160,
        (False, 3, 1): 10103101,
        (False, 3, 2): 10103151,
        (False, 3, 3): 10103102,
        (False, 3, 4): 10103304,
        (False, 3, 5): 10103103,
        (False, 3, 6): 10103153,
        (False, 3, 7): 10103104,
        (False, 3, 8): 10103308,
        (False, 3, 9): 10103105,
        (False, 3, 10): 10103155,
        (False, 3, 11): 10103351,
        (False, 3, 12): 10103156,
        (False, 3, 13): 10103107,
        (False, 3, 14): 10103157,
        (False, 3, 15): 10103355,
        (False, 3, 16): 10103158,
        (False, 3, 17): 10103109,
        (False, 3, 18): 10103159,
        (False, 3, 19): 10103110,
        (False, 3, 20): 10103160,
        (False, 4, 1): 104101,
        (False, 4, 2): 104102,
        (False, 4, 3): 104103,
        (False, 4, 4): 104104,
        (False, 4, 5): 104105,
        (False, 4, 6): 104106,
        (False, 4, 7): 104107,
        (False, 4, 8): 104108,
        (False, 4, 9): 104109,
        (False, 4, 10): 104110,
        (False, 4, 11): 104111,
        (False, 4, 12): 104112,
        (False, 4, 13): 104113,
        (False, 4, 14): 104114,
        (False, 4, 15): 104115,
        (False, 4, 16): 104116,
        (False, 4, 17): 104117,
        (False, 4, 18): 104118,
        (False, 4, 19): 104119,
        (False, 4, 20): 104120,
        (True, 3, 11): 10103206,
    }
    known = known_story_stage_ids.get((hard, chapter, stage))
    if known is not None:
        return known
    if hard:
        # Story hard-mode ids track the corresponding normal-stage id with the
        # hard flag shifted by +100, even for irregular chapter-2 mappings like
        # 10102151 -> 10102251.
        return _story_stage_master_id(chapter, stage, hard=False) + 100
    if chapter >= 4:
        return 100100 + chapter * 1000 + stage
    return int(f"101{chapter:02d}1{stage:02d}")


def _story_default_template_stage(chapter: int) -> int:
    if chapter == 3:
        return 2
    return 1


def _resolve_story_template_stage_id(chapter: int, stage: int, *, hard: bool) -> int:
    from .battle_templates import battle_template_exists

    default_stage = _story_default_template_stage(chapter)
    if chapter >= 4:
        candidates = [_story_stage_master_id(4, 1, hard=False)]
    else:
        candidates = [_story_stage_master_id(chapter, default_stage, hard=hard)]
    candidates.append(_story_stage_master_id(chapter, stage, hard=hard))
    if hard:
        candidates.extend([
            _story_stage_master_id(chapter, default_stage, hard=False),
            _story_stage_master_id(chapter, stage, hard=False),
        ])

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if battle_template_exists(candidate):
            return candidate

    raise FileNotFoundError(
        f"No story template found for chapter {chapter}, stage {stage}, hard={hard}. "
        f"Tried: {sorted(seen)}"
    )


def _run_story_stage_command(args, *, hard: bool):
    client, record = _load_client_for_account(args)
    result_stat_kwargs = _result_stat_override_kwargs(args)
    start_chapter, start_stage = _parse_story_stage_key(args.start_stage)
    if args.end_stage:
        end_chapter, end_stage = _parse_story_stage_key(args.end_stage)
    else:
        end_chapter, end_stage = start_chapter, start_stage
    if start_chapter != end_chapter:
        raise SystemExit("Story stage ranges must stay within one chapter.")
    if end_stage < start_stage:
        raise SystemExit("Story stage range end must be greater than or equal to start.")

    chapter = start_chapter
    stage_numbers = list(range(start_stage, end_stage + 1))
    first_stage_key = f"{chapter}-{start_stage}"
    last_stage_key = f"{chapter}-{end_stage}"
    range_label = first_stage_key if first_stage_key == last_stage_key else f"{first_stage_key}..{last_stage_key}"
    command_name = "jqh" if hard else "jq"
    display_mode = "困难剧情" if hard else "剧情"

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== {display_mode} {range_label} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    resume_session_id = resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, resp, context=f"{display_mode} {range_label}")
        resume_session_id = None

    for stage in stage_numbers:
        stage_key = f"{chapter}-{stage}"
        stage_master_id = _story_stage_master_id(chapter, stage, hard=hard)
        template_stage_id = _resolve_story_template_stage_id(chapter, stage, hard=hard)
        print(f"\n=== {display_mode} {stage_key} ({stage_master_id}) ===")

        if resume_session_id is not None:
            print(f"\n=== resume in_game/session ({resume_session_id}) ===")
        else:
            print(f"\n=== in_game/start ({stage_master_id}) ===")
            resp = client.in_game_start(stage_master_id, deck_index=1)
            _check(resp, f"in_game/start({stage_master_id})")

        print(f"\n=== in_game/result ({stage_master_id}) ===")
        resp = _submit_in_game_result_with_resume(
            client,
            stage_master_id=stage_master_id,
            template_stage_id=template_stage_id,
            in_game_session_id=resume_session_id,
            **result_stat_kwargs,
        )
        _check(resp, f"in_game/result({stage_master_id})")
        resume_session_id = None

    final_stage_key = f"{chapter}-{end_stage}"

    saved = _save_client_account(
        client,
        args,
        progress=f"{command_name}_{final_stage_key}_complete",
        last_command=f"{command_name}-{range_label}",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print(f"{display_mode} complete: {range_label}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_jq(args):
    _run_story_stage_command(args, hard=False)


def cmd_jqh(args):
    _run_story_stage_command(args, hard=True)


_JQHD_MAX_CHAPTER = 15

_JQHD_STAGE_IDS = {
    (chapter, 1): 30242100 + chapter
    for chapter in range(1, _JQHD_MAX_CHAPTER + 1)
}
_JQHD_STAGE_IDS.update({
    (chapter, 2): 30242200 + chapter
    for chapter in range(1, _JQHD_MAX_CHAPTER + 1)
})

_JQHD_TEMPLATE_STAGE_IDS = {
    1: 30242101,
    3: 30242203,
    4: 30242203,
    6: 30242204,
}

_JQHD_QD_TEMPLATE_STAGE_ID = 30243011
_JQHD_QD_DEFAULT_SCORES = {
    1: 8001,
    2: 24000,
    3: 72000,
    4: 216000,
    5: 54001,
}
_JQHD_QD_ENEMIES = {
    "l": {"name": "强敌-龙", "base_stage_id": 30243011},
    "em": {"name": "强敌-恶魔骑士", "base_stage_id": 30243021},
}

_JQHD_SPECIAL_STAGE_IDS = {
    "st": {
        1: {
            "name": "饲堂",
            "stage": 30241101,
            "template": 30241101,
        },
        2: {
            "name": "饲堂",
            "stage": 30241102,
            "template": 30241101,
        },
    },
}


def _jqhd_stage_master_id(chapter: int, stage: int) -> int:
    stage_master_id = _JQHD_STAGE_IDS.get((chapter, stage))
    if stage_master_id is None:
        supported = ", ".join(f"{chapter}-{stage}" for chapter, stage in sorted(_JQHD_STAGE_IDS))
        raise SystemExit(f"Unsupported jqhd stage '{chapter}-{stage}'. Supported: {supported}")
    return stage_master_id


def _jqhd_template_stage_id(chapter: int, stage: int = None) -> int:
    if stage == 1:
        if 14 <= chapter <= _JQHD_MAX_CHAPTER:
            return 30242114
        return 30242101
    if stage == 2:
        if chapter == 14:
            return 30242214
        if 7 <= chapter <= _JQHD_MAX_CHAPTER:
            return 30242211
        return 30242203
    template_stage_id = _JQHD_TEMPLATE_STAGE_IDS.get(chapter)
    if template_stage_id is None:
        supported = ", ".join(str(chapter) for chapter in sorted(_JQHD_TEMPLATE_STAGE_IDS))
        raise SystemExit(f"No jqhd template for chapter {chapter}. Supported chapters: {supported}")
    return template_stage_id


def _jqhd_qd_stage_master_id(enemy_key: str, level: int) -> int:
    if level not in _JQHD_QD_DEFAULT_SCORES:
        supported = ", ".join(str(level) for level in sorted(_JQHD_QD_DEFAULT_SCORES))
        raise SystemExit(f"Unsupported jqhd qd level {level}. Supported: {supported}")
    enemy = _JQHD_QD_ENEMIES.get(enemy_key)
    if enemy is None:
        supported = ", ".join(sorted(_JQHD_QD_ENEMIES))
        raise SystemExit(f"Unsupported jqhd qd type '{enemy_key}'. Supported: {supported}")
    return enemy["base_stage_id"] + level - 1


def cmd_jqhd_qd(args, enemy_key: str, level_text: str):
    from .battle_templates import load_scored_result

    if not level_text.isdigit():
        raise SystemExit("Invalid jqhd qd level. Use format like: jqhd qd l 1")

    enemy_key = enemy_key.lower()
    level = int(level_text)
    enemy = _JQHD_QD_ENEMIES.get(enemy_key)
    if enemy is None:
        supported = ", ".join(sorted(_JQHD_QD_ENEMIES))
        raise SystemExit(f"Unsupported jqhd qd type '{enemy_key}'. Supported: {supported}")
    stage_master_id = _jqhd_qd_stage_master_id(enemy_key, level)
    score = args.score if args.score is not None else _JQHD_QD_DEFAULT_SCORES[level]
    result_stat_kwargs = _result_stat_override_kwargs(args)
    times = args.times
    if times <= 0:
        raise SystemExit("--times must be positive.")
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== {enemy['name']} Lv.{level} ({stage_master_id}) — score={score} ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    resume_session_id = resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, resp, context=f"jqhd qd {enemy_key} {level}")
        resume_session_id = None

    for run_no in range(1, times + 1):
        print(f"\n--- run {run_no}/{times} ---")
        if resume_session_id is not None:
            print(f"\n=== resume in_game/session ({resume_session_id}) ===")
        else:
            print(f"\n=== in_game/start ({stage_master_id}) ===")
            resp = client.in_game_start(stage_master_id, deck_index=1)
            _check(resp, f"in_game/start({stage_master_id})")

        print(f"\n=== in_game/result ({stage_master_id}) ===")
        resp = _submit_in_game_result_with_resume(
            client,
            stage_master_id=stage_master_id,
            build_result_body=lambda sid, start_response: load_scored_result(
                stage_master_id=stage_master_id,
                score=score,
                template_stage_id=_JQHD_QD_TEMPLATE_STAGE_ID,
                in_game_session_id=sid,
                start_response=start_response,
                **result_stat_kwargs,
            ),
            in_game_session_id=resume_session_id,
            **result_stat_kwargs,
        )
        _check(resp, f"in_game/result({stage_master_id})")
        resume_session_id = None

    saved = _save_client_account(
        client,
        args,
        progress=f"jqhd_qd_{enemy_key}_{level}_complete",
        last_command=f"jqhd-qd-{enemy_key}-{level}x{times}",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print(f"{enemy['name']} Lv.{level} complete. Score: {score}. Runs: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_jqhd_special(args, special_key: str, level_text: str):
    from .battle_templates import load_battle_result

    if not level_text.isdigit():
        raise SystemExit(f"Invalid jqhd {special_key} level. Use format like: jqhd {special_key} 1")

    special_key = special_key.lower()
    level = int(level_text)
    stages = _JQHD_SPECIAL_STAGE_IDS.get(special_key)
    if stages is None:
        supported = ", ".join(sorted(_JQHD_SPECIAL_STAGE_IDS))
        raise SystemExit(f"Unsupported jqhd special type '{special_key}'. Supported: {supported}")
    stage_config = stages.get(level)
    if stage_config is None:
        supported = ", ".join(str(level) for level in sorted(stages))
        raise SystemExit(f"Unsupported jqhd {special_key} level {level}. Supported: {supported}")

    stage_master_id = stage_config["stage"]
    template_stage_id = stage_config["template"]
    result_stat_kwargs = _result_stat_override_kwargs(args)
    times = args.times
    if times <= 0:
        raise SystemExit("--times must be positive.")
    client, record = _load_client_for_account(args)

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== 活动剧情 {stage_config['name']} Lv.{level} ({stage_master_id}) ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    resume_session_id = resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, resp, context=f"jqhd {special_key} {level}")
        resume_session_id = None

    for run_no in range(1, times + 1):
        print(f"\n--- run {run_no}/{times} ---")
        if resume_session_id is not None:
            print(f"\n=== resume in_game/session ({resume_session_id}) ===")
        else:
            print(f"\n=== in_game/start ({stage_master_id}) ===")
            resp = client.in_game_start(stage_master_id, deck_index=1)
            _check(resp, f"in_game/start({stage_master_id})")

        print(f"\n=== in_game/result ({stage_master_id}) ===")
        resp = _submit_in_game_result_with_resume(
            client,
            build_result_body=lambda sid, start_response: load_battle_result(
                stage_master_id=stage_master_id,
                template_stage_id=template_stage_id,
                in_game_session_id=sid,
                start_response=start_response,
                **result_stat_kwargs,
            ),
            in_game_session_id=resume_session_id,
            **result_stat_kwargs,
        )
        _check(resp, f"in_game/result({stage_master_id})")
        resume_session_id = None

    saved = _save_client_account(
        client,
        args,
        progress=f"jqhd_{special_key}_{level}_complete",
        last_command=f"jqhd-{special_key}-{level}x{times}",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print(f"活动剧情 {stage_config['name']} Lv.{level} complete. Runs: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_jqhd(args):
    times = args.times
    if times <= 0:
        raise SystemExit("--times must be positive.")

    if args.start_stage.lower() == "qd":
        if not args.end_stage or not args.extra_stage:
            raise SystemExit("jqhd qd requires a type and level. Use format like: jqhd qd l 1")
        cmd_jqhd_qd(args, args.end_stage, args.extra_stage)
        return

    if args.start_stage.lower() == "xl":
        if not args.end_stage:
            raise SystemExit("jqhd xl requires a level. Use format like: jqhd xl 1")
        if args.extra_stage:
            raise SystemExit("jqhd xl accepts one level. Use format like: jqhd xl 1")
        cmd_jqhd_xl(args, args.end_stage)
        return

    if args.start_stage.lower() in _JQHD_SPECIAL_STAGE_IDS:
        if not args.end_stage:
            raise SystemExit(f"jqhd {args.start_stage} requires a level. Use format like: jqhd {args.start_stage} 1")
        if args.extra_stage:
            raise SystemExit(f"jqhd {args.start_stage} accepts one level. Use format like: jqhd {args.start_stage} 1")
        cmd_jqhd_special(args, args.start_stage, args.end_stage)
        return

    if args.start_stage.lower() == "qdtz":
        raise SystemExit("jqhd qdtz was renamed. Use format like: jqhd qd l 1")

    if args.extra_stage:
        raise SystemExit("jqhd stage ranges accept at most two stage keys. Use format like: jqhd 1-1 1-2")

    client, record = _load_client_for_account(args)
    result_stat_kwargs = _result_stat_override_kwargs(args)
    start_chapter, start_stage = _parse_story_stage_key(args.start_stage)
    if args.end_stage:
        end_chapter, end_stage = _parse_story_stage_key(args.end_stage)
    else:
        end_chapter, end_stage = start_chapter, start_stage
    if start_chapter != end_chapter:
        raise SystemExit("jqhd stage ranges must stay within one chapter.")
    if end_stage < start_stage:
        raise SystemExit("jqhd stage range end must be greater than or equal to start.")

    chapter = start_chapter
    stage_numbers = list(range(start_stage, end_stage + 1))
    first_stage_key = f"{chapter}-{start_stage}"
    last_stage_key = f"{chapter}-{end_stage}"
    range_label = first_stage_key if first_stage_key == last_stage_key else f"{first_stage_key}..{last_stage_key}"

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== 活动剧情 {range_label} ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    resume_session_id = resp.get("InGameSessionId")
    if resume_session_id is not None:
        _surrender_login_resume_for_battle(client, resp, context=f"jqhd {range_label}")
        resume_session_id = None

    for stage in stage_numbers:
        for run_no in range(1, times + 1):
            stage_master_id = _jqhd_stage_master_id(chapter, stage)
            template_stage_id = _jqhd_template_stage_id(chapter, stage)
            print(f"\n=== 活动剧情 {chapter}-{stage} ({stage_master_id}) run {run_no}/{times} ===")

            if resume_session_id is not None:
                print(f"\n=== resume in_game/session ({resume_session_id}) ===")
            else:
                print(f"\n=== in_game/start ({stage_master_id}) ===")
                resp = client.in_game_start(stage_master_id, deck_index=1)
                _check(resp, f"in_game/start({stage_master_id})")

            print(f"\n=== in_game/result ({stage_master_id}) ===")
            resp = _submit_in_game_result_with_resume(
                client,
                stage_master_id=stage_master_id,
                template_stage_id=template_stage_id,
                in_game_session_id=resume_session_id,
                **result_stat_kwargs,
            )
            _check(resp, f"in_game/result({stage_master_id})")
            resume_session_id = None

    saved = _save_client_account(
        client,
        args,
        progress=f"jqhd_{last_stage_key}_complete",
        last_command=f"jqhd-{range_label}x{times}",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print(f"活动剧情 complete: {range_label}. Runs per stage: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


@_saved_flow(99)
def _flow99_impl(args, client, record, login_snapshot, login_resp):
    print("\n[1/1] === collect-rewards + refresh snapshot ===")
    login_snapshot, collected = _collect_rewards_and_refresh_snapshot(client)

    saved = _save_client_account(
        client,
        args,
        progress="flow99_complete",
        last_command="flow99",
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print("Reward collection complete.")
    print(
        "  collected = "
        f"adventure_book:{collected['adventure_book']} "
        f"daily:{collected['daily']} "
        f"daily_progress:{collected['daily_progress']} "
        f"weekly:{collected['weekly']} "
        f"weekly_progress:{collected['weekly_progress']} "
        f"achievement:{collected['achievement']} "
        f"event:{collected['event']} "
        f"present:{collected['present']}"
    )
    sns_coin = login_snapshot.get("sns_coin") if isinstance(login_snapshot, dict) else None
    if sns_coin:
        print(
            "  sns_coin  = "
            f"free:{sns_coin['free']} "
            f"billing:{sns_coin['billing']} "
            f"total:{sns_coin['total']}"
        )
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)
    return saved


def cmd_collect_rewards(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow99_impl(args, client, record, login_snapshot, login_resp)


def cmd_flow99(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)
    _flow99_impl(args, client, record, login_snapshot, login_resp)


def cmd_ad(args):
    client, record, login_resp, login_snapshot = _prepare_saved_account_runtime(args)

    action = getattr(args, "action", None) or "reward"

    if action == "reward":
        print("\n=== advertisement/receive_reward_chance_point_card_point ===")
        resp = client.advertisement_receive_reward_chance_point_card_point()
        _check(resp, "advertisement/receive_reward_chance_point_card_point")
        last_command = "ad"
        complete_text = "Advertisement reward request complete."
    elif action == "tx":
        print("\n=== advertisement/receive_reward_ad_chance_orb ===")
        resp = client.advertisement_receive_reward_ad_chance_orb()
        _check(resp, "advertisement/receive_reward_ad_chance_orb")
        last_command = "ad-tx"
        complete_text = "Advertisement expedition reward request complete."
    elif action == "store":
        _run_ad_store_exchanges(client)
        last_command = "ad-store"
        complete_text = "Advertisement store exchanges complete."
    elif action == "gacha":
        gacha_master_id = 500000101
        print(f"\n=== gacha/draw (ad gacha, pool={gacha_master_id}) ===")
        resp = client.gacha_draw(gacha_master_id)
        _check(resp, "gacha/draw ad gacha")
        last_command = "ad-gacha"
        complete_text = "Advertisement gacha draw complete."
    else:
        raise SystemExit(f"Unknown ad action: {action}")

    saved = _save_client_account(
        client,
        args,
        last_command=last_command,
        snapshot=login_snapshot,
    )

    print("\n" + "=" * 50)
    print(complete_text)
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


# ==========================================================================
# Equipment inventory helpers
# ==========================================================================

def _parse_equipment_inventory_from_user_model(user_model: dict) -> dict[int, list[dict]]:
    inventory = {1: [], 2: [], 3: []}

    for weapon in user_model.get("weapons", []):
        mid = weapon["WeaponMasterId"]
        rarity = equipment_rarity(CONTENT_TYPE_WEAPON, mid)
        entry = {
            "content_type": CONTENT_TYPE_WEAPON,
            "content_master_id": mid,
            "user_equipment_id": weapon["UserWeaponId"],
            "rarity": rarity,
            "is_metal": equipment_is_metal(CONTENT_TYPE_WEAPON, mid),
            "display": equipment_display_name(CONTENT_TYPE_WEAPON, mid),
            "level": None,
            "limit_break_step": None,
            "is_lock": weapon["IsLock"],
        }
        if rarity in inventory:
            inventory[rarity].append(entry)

    for armor in user_model.get("armors", []):
        mid = armor["ArmorMasterId"]
        rarity = equipment_rarity(CONTENT_TYPE_ARMOR, mid)
        entry = {
            "content_type": CONTENT_TYPE_ARMOR,
            "content_master_id": mid,
            "user_equipment_id": armor["UserArmorId"],
            "rarity": rarity,
            "is_metal": equipment_is_metal(CONTENT_TYPE_ARMOR, mid),
            "display": equipment_display_name(CONTENT_TYPE_ARMOR, mid),
            "level": armor["Level"],
            "limit_break_step": armor["LimitBreakStep"],
            "is_lock": armor["IsLock"],
        }
        if rarity in inventory:
            inventory[rarity].append(entry)

    for rarity_items in inventory.values():
        rarity_items.sort(key=lambda item: (item["content_type"], item["content_master_id"], item["user_equipment_id"]))
    return inventory


def _parse_equipment_inventory(login_response_raw: bytes):
    """Parse login response to extract weapon + armor inventory grouped by rarity."""
    user_model = _parse_login_user_model_basics(login_response_raw)
    return _parse_equipment_inventory_from_user_model(user_model)


def _print_three_star_inventory(login_response_raw: bytes):
    """Print 3★ weapons and armors from login response."""
    inventory = _parse_equipment_inventory(login_response_raw)
    three_star = inventory.get(3, [])
    if not three_star:
        print("  No 3★ equipment found.")
        return
    print(f"  3★ equipment ({len(three_star)} items):")
    for eq in three_star:
        metal_tag = " [METAL]" if eq["is_metal"] else ""
        level_text = f"  lv={eq['level']}" if eq["level"] is not None else ""
        print(
            f"    {eq['display']}{metal_tag}  mid={eq['content_master_id']}  "
            f"uid={eq['user_equipment_id']}{level_text}"
        )


# ==========================================================================
# cmd_gacha — Gacha control function
# ==========================================================================

_GACHA_POOLS = {
    "metal": GACHA_METAL_10,
    "normal": GACHA_NORMAL_10,
}


def cmd_gacha(args):
    """Execute gacha draws: login -> fetch_list -> draw × count -> summary."""
    client, record = _load_client_for_account(args)
    gacha_type = args.gacha_type
    draw_count = args.count
    gacha_master_id = _GACHA_POOLS.get(gacha_type)
    if gacha_master_id is None:
        raise SystemExit(f"Unknown gacha type: {gacha_type!r}. Use 'metal' or 'normal'.")

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== gacha: {gacha_type} x{draw_count} (pool={gacha_master_id}) ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)

    # Show pre-gacha 3★ inventory
    if client.last_login_response_raw:
        print("\n=== Pre-gacha 3★ inventory ===")
        _print_three_star_inventory(client.last_login_response_raw)

    print("\n=== gacha/fetch_list ===")
    resp = client.gacha_fetch_list()
    _check(resp, "gacha/fetch_list")

    print("\n=== gacha/fetch_top ===")
    resp = client.gacha_fetch_top()
    _check(resp, "gacha/fetch_top")

    # Execute draws
    all_rewards = []
    summary = {1: 0, 2: 0, 3: 0}
    three_star_items = []

    for i in range(1, draw_count + 1):
        print(f"\n=== gacha/draw #{i}/{draw_count} ({gacha_type}) ===")
        resp = client.gacha_draw(gacha_master_id)
        _check(resp, f"gacha/draw #{i}")

        for rw in resp["rewards"]:
            all_rewards.append(rw)
            r = rw["rarity"]
            if r in summary:
                summary[r] += 1
            if r >= 3:
                three_star_items.append(rw)

        # Refresh between draws
        if i < draw_count:
            print(f"\n=== gacha/fetch_top (after draw #{i}) ===")
            client.gacha_fetch_top()

    # Post-draw refresh
    print("\n=== gacha/fetch_top (final) ===")
    client.gacha_fetch_top()

    print("\n=== gacha/fetch_list (final) ===")
    client.gacha_fetch_list()

    # Re-login to get updated inventory
    print("\n=== login/login (refresh) ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login refresh")
    login_snapshot = _build_account_snapshot_from_login(client)

    # Show post-gacha 3★ inventory
    if client.last_login_response_raw:
        print("\n=== Post-gacha 3★ inventory ===")
        _print_three_star_inventory(client.last_login_response_raw)

    saved = _save_client_account(
        client,
        args,
        last_command=f"gacha-{gacha_type}",
        snapshot=login_snapshot,
    )

    # Print summary
    total = sum(summary.values())
    print(f"\n{'='*50}")
    print(f"Gacha complete: {gacha_type} x{draw_count} ({total} items total)")
    print(f"  1★: {summary[1]}")
    print(f"  2★: {summary[2]}")
    print(f"  3★: {summary[3]}")
    if three_star_items:
        print(f"\n  === 3★ items obtained ===")
        for rw in three_star_items:
            metal_tag = " [METAL]" if rw["is_metal"] else ""
            print(f"    {rw['display']}{metal_tag}  mid={rw['content_master_id']}  "
                  f"uid={rw['user_equipment_id']}  new={rw['is_new']}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


# ==========================================================================
# cmd_juxiang — 巨像 (Colossus) boss dungeon
# ==========================================================================

_JUXIANG_STAGE_MASTER_ID = 10151701

# 养成 (Growth) dungeon stage IDs
# Format: {type: {level: {"stage": stage_master_id, "template": template_stage_id}}}
# When template == stage, only the stage's own .bin is needed.
# When template differs, the template's .bin is reused with stage_master_id patched.
_YC_STAGE_IDS = {
    "fj": {
        1: {"stage": 10133111, "template": 10133111},
        2: {"stage": 10133112, "template": 10133111},
        3: {"stage": 10133113, "template": 10133111},
    },
    "wq": {
        1: {"stage": 10134201, "template": 10134201},
        2: {"stage": 10134202, "template": 10134201},
        3: {"stage": 10134203, "template": 10134201},
    },
    "jb": {
        1: {"stage": 10132211, "template": 10132211},
        2: {"stage": 10132212, "template": 10132211},
        3: {"stage": 10132213, "template": 10132211},
    },
    "slm": {
        1: {"stage": 10131101, "template": 10131101, "score_mirror_offsets": [16]},
    },
}


_HD_STAGE_IDS = {
    "jx": {
        1: {
            "stage": 10151701,
            "template": 10151701,
            "template_file": "stage_10151701_hd_jx.bin",
            "default_score": 18000,
        },
    },
    "xmss": {
        1: {
            "stage": 10151702,
            "template": 10151702,
            "default_score": 11000,
        },
    },
    "shn": {
        1: {
            "stage": 10151703,
            "template": 10151702,
            "default_score": 11000,
        },
    },
    "cjmwmg": {
        1: {
            "stage": 50151011,
            "template": 50151011,
            "default_score": 15651,
        },
        2: {
            "stage": 50151021,
            "template": 50151021,
            "default_score": 18443,
        },
    },
    "qdjf": {
        1: {"stage": 30144101, "template": 30144101, "default_score": 8000},
        2: {"stage": 30144201, "template": 30144101, "default_score": 24000},
        3: {"stage": 30144301, "template": 30144101, "default_score": 72000},
        4: {"stage": 30144401, "template": 30144101, "default_score": 216000},
    },
}

_TZ_STAGE_IDS = {
    "st": {
        "hb": {
            1: {"stage": 10041011, "template": 10102101},
            2: {"stage": 10041012, "template": 10102101},
        },
        "em": {
            1: {"stage": 10041031, "template": 10102101},
            2: {"stage": 10041032, "template": 10102101},
        },
        "yx": {
            1: {"stage": 10041041, "template": 10102101},
            2: {"stage": 10041042, "template": 10102101},
        },
    },
}

_XL_CONFIGS = {
    1: {
        "stage": 30161101,
        "template_stage": 30161101,
        "template_file": "stage_30161101_xl.bin",
        "start_body": bytes.fromhex(
            "cd38cc01010000000001cd38cc013b400a007f1300000000000089e5a6be9d0100004ff9babb02000000"
        ),
        "fetch_multi_data_body": bytes.fromhex("cd38cc0100020000007b7d"),
    },
    2: {
        "stage": 30161201,
        "template_stage": 30161101,
        "template_file": "stage_30161101_xl.bin",
        "start_body": bytes.fromhex(
            "3139cc010100000000013139cc013b400a007f1300000000000089e5a6be9d0100004ff9babb02000000"
        ),
        "fetch_multi_data_body": bytes.fromhex("3139cc0100020000007b7d"),
    },
}

_JQHD_XL_CONFIGS = {
    1: {
        "stage": 30261101,
        "template_stage": 30161101,
        "template_file": "stage_30161101_xl.bin",
        "fetch_multi_data_body": bytes.fromhex("6dbfcd0100020000007b7d"),
    },
    2: {
        "stage": 30261102,
        "template_stage": 30161101,
        "template_file": "stage_30161101_xl.bin",
        "fetch_multi_data_body": bytes.fromhex("6ebfcd0100020000007b7d"),
    },
}


def cmd_yc(args):
    """Run a 养成 (Growth) dungeon with a specified score, or skip (tg)."""
    from .battle_templates import load_scored_result, read_template_score

    dungeon_type = args.type
    result_stat_kwargs = _result_stat_override_kwargs(args)
    level_str = args.level
    score = args.score
    times = args.times

    if dungeon_type == "slm1":
        dungeon_type = "slm"
        if level_str is None:
            level_str = "1"

    stages = _YC_STAGE_IDS.get(dungeon_type)
    if not stages:
        raise SystemExit(f"Unknown dungeon type: {dungeon_type}. Available: {list(_YC_STAGE_IDS.keys())}")

    if level_str is None:
        raise SystemExit("level is required, except for shorthand aliases like 'slm1'.")

    type_names = {
        "fj": "防具 (Armor)",
        "wq": "武器 (Weapon)",
        "jb": "金币 (Gold)",
        "slm": "史莱姆 (Slime)",
    }
    display_name = type_names.get(dungeon_type, dungeon_type)

    is_skip = level_str == "tg"

    if is_skip:
        # Skip mode: use the highest level, count from --count (default 3)
        max_level = max(stages.keys())
        stage_config = stages[max_level]
        stage_id = stage_config["stage"]
        skip_count = getattr(args, "count", None) or 3

        client, record = _load_client_for_account(args)
        print(f"=== account {_account_ref(record)} ===")
        print(f"=== 养成 {display_name} 跳过 (skip) Lv.{max_level} ×{skip_count} ===")

        print("\n=== masterdata/get_version ===")
        resp = client.masterdata_get_version()
        _check(resp, "masterdata/get_version")

        print("\n=== login/login ===")
        resp = client.login_login(first_login=False)
        _check(resp, "login/login")
        login_snapshot = _build_account_snapshot_from_login(client)
        _surrender_login_resume_for_battle(client, resp, context=f"yc {dungeon_type} skip")

        print(f"\n=== in_game/skip_stage ({stage_id}, count={skip_count}) ===")
        resp = client.in_game_skip_stage(stage_id, count=skip_count)
        _check(resp, "in_game/skip_stage")
        _receive_in_game_result_ad_chance(client, resp)

        saved = _save_client_account(
            client, args,
            last_command=f"yc-{dungeon_type}-tg",
            snapshot=login_snapshot,
        )
        print(f"\n{'='*50}")
        print(f"养成 {display_name} skip Lv.{max_level} ×{skip_count} complete.")
        _print_saved_account(saved, _store_path(args))
        print("=" * 50)
        return

    # Normal battle mode
    try:
        level = int(level_str)
    except ValueError:
        raise SystemExit(f"Unknown level '{level_str}'. Use a number (1-3) or 'tg' for skip.")

    stage_config = stages.get(level)
    if not stage_config:
        raise SystemExit(f"Unknown level {level} for {dungeon_type}. Available: {sorted(stages.keys())}")
    stage_id = stage_config["stage"]
    template_id = stage_config["template"]
    if score is None:
        score = stage_config.get("default_score")
        if score is None:
            score = read_template_score(
                stage_master_id=stage_id,
                template_stage_id=template_id,
                template_file=stage_config.get("template_file"),
            )

    client, record = _load_client_for_account(args)
    print(f"=== account {_account_ref(record)} ===")
    print(f"=== 养成 {display_name} Lv.{level} — score={score} ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    _surrender_login_resume_for_battle(client, resp, context=f"yc {dungeon_type} {level}")

    for idx in range(1, times + 1):
        if times > 1:
            print(f"\n=== YC run {idx}/{times} ===")
        _run_scored_dungeon(
            client,
            stage_id,
            build_result_body=lambda sid, start_response: load_scored_result(
                stage_master_id=stage_id,
                score=score,
                template_stage_id=template_id,
                in_game_session_id=sid,
                score_mirror_offsets=stage_config.get("score_mirror_offsets"),
                start_response=start_response,
                **result_stat_kwargs,
            ),
            login_resp=resp if idx == 1 else None,
        )

    saved = _save_client_account(
        client, args,
        last_command=f"yc-{dungeon_type}-{level}",
        snapshot=login_snapshot,
    )
    print(f"\n{'='*50}")
    print(f"养成 {display_name} Lv.{level} complete. Score: {score}. Runs: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_hd(args):
    """Run 活动 (Event) scored battles."""
    from .battle_templates import load_scored_result

    event_type = args.type
    result_stat_kwargs = _result_stat_override_kwargs(args)
    level_str = args.level
    times = args.times
    if times <= 0:
        raise SystemExit("--times must be positive.")
    if event_type in {"jx", "xmss", "shn"} and level_str is None:
        level_str = "1"
    if level_str is None:
        raise SystemExit(f"level is required for {event_type}.")
    try:
        level = int(level_str)
    except ValueError:
        raise SystemExit(f"Unknown level '{level_str}'. Use a number.")

    stages = _HD_STAGE_IDS.get(event_type)
    if not stages:
        raise SystemExit(f"Unknown event type: {event_type}. Available: {list(_HD_STAGE_IDS.keys())}")

    stage_config = stages.get(level)
    if not stage_config:
        raise SystemExit(f"Unknown level {level} for {event_type}. Available: {sorted(stages.keys())}")

    stage_id = stage_config["stage"]
    template_id = stage_config["template"]
    score = args.score if args.score is not None else stage_config["default_score"]
    type_names = {
        "qdjf": "强敌交锋",
        "jx": "巨像",
        "xmss": "小魔术师",
        "shn": "食火鸟",
        "cjmwmg": "超级魔物猛攻",
    }
    display_name = type_names.get(event_type, event_type)

    client, record = _load_client_for_account(args)
    print(f"=== account {_account_ref(record)} ===")
    print(f"=== 活动 {display_name} Lv.{level} — score={score} ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    _surrender_login_resume_for_battle(client, resp, context=f"hd {event_type} {level}")

    for idx in range(1, times + 1):
        if times > 1:
            print(f"\n=== HD run {idx}/{times} ===")
        _run_scored_dungeon(
            client,
            stage_id,
            build_result_body=lambda sid, start_response: load_scored_result(
                stage_master_id=stage_id,
                score=score,
                template_stage_id=template_id,
                in_game_session_id=sid,
                score_mirror_offsets=stage_config.get("score_mirror_offsets"),
                template_file=stage_config.get("template_file"),
                start_response=start_response,
                **result_stat_kwargs,
            ),
            login_resp=resp if idx == 1 else None,
        )

    saved = _save_client_account(
        client,
        args,
        last_command=f"hd-{event_type}-{level}x{times}",
        snapshot=login_snapshot,
    )
    print(f"\n{'='*50}")
    print(f"活动 {display_name} Lv.{level} complete. Score: {score}. Runs: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_tz(args):
    """Run 挑战 dungeons."""
    from .battle_templates import load_battle_result

    zone = args.zone
    result_stat_kwargs = _result_stat_override_kwargs(args)
    element = args.element
    times = args.times
    if times <= 0:
        raise SystemExit("--times must be positive.")
    if element is None or args.level is None:
        raise SystemExit("tz st requires: tz st hb 1")
    try:
        level = int(args.level)
    except ValueError:
        raise SystemExit(f"Unknown level '{args.level}'. Use a number.")

    zone_map = _TZ_STAGE_IDS.get(zone)
    if not zone_map:
        raise SystemExit(f"Unknown tz zone: {zone}. Available: {list(_TZ_STAGE_IDS.keys())}")
    element_map = zone_map.get(element)
    if not element_map:
        raise SystemExit(
            f"Unknown tz element '{element}' for zone '{zone}'. Available: {list(zone_map.keys())}"
        )
    stage_config = element_map.get(level)
    if not stage_config:
        raise SystemExit(
            f"Unknown level {level} for tz {zone} {element}. Available: {sorted(element_map.keys())}"
        )

    stage_id = stage_config["stage"]
    template_id = stage_config["template"]
    zone_names = {"st": "饲堂"}
    element_names = {"hb": "寒冰", "em": "恶魔", "yx": "原型杀戮者"}
    display_zone = zone_names.get(zone, zone)
    display_element = element_names.get(element, element)

    client, record = _load_client_for_account(args)
    print(f"=== account {_account_ref(record)} ===")
    print(f"=== 挑战 {display_zone} {display_element} Lv.{level} ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    _surrender_login_resume_for_battle(client, resp, context=f"tz {zone} {element} {level}")

    for idx in range(1, times + 1):
        if times > 1:
            print(f"\n=== TZ run {idx}/{times} ===")
        _run_scored_dungeon(
            client,
            stage_id,
            build_result_body=lambda sid, start_response: load_battle_result(
                stage_master_id=stage_id,
                template_stage_id=template_id,
                in_game_session_id=sid,
                start_response=start_response,
                **result_stat_kwargs,
            ),
            login_resp=resp if idx == 1 else None,
        )

    saved = _save_client_account(
        client,
        args,
        last_command=f"tz-{zone}-{element}-{level}x{times}",
        snapshot=login_snapshot,
    )
    print(f"\n{'='*50}")
    print(f"挑战 {display_zone} {display_element} Lv.{level} complete. Runs: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def _run_xl_command(
    args,
    *,
    configs: dict,
    level: int,
    command_name: str,
    display_name: str,
    progress: str = None,
    last_command: str = None,
):
    from .battle_templates import load_battle_result

    client, record = _load_client_for_account(args)
    result_stat_kwargs = _result_stat_override_kwargs(args)
    times = args.times
    if times <= 0:
        raise SystemExit("--times must be positive.")
    config = configs.get(level)
    if config is None:
        raise SystemExit(f"Unknown {command_name} level: {level}. Available: {sorted(configs)}")
    stage_id = config["stage"]

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== {display_name} {level} ({stage_id}) ×{times} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)
    _surrender_login_resume_for_battle(client, resp, context=command_name)

    for idx in range(1, times + 1):
        print(f"\n=== XL run {idx}/{times} ===")

        print(f"\n=== matching_room/fetch_multi_data ({stage_id}) ===")
        resp = client.matching_room_fetch_multi_data_raw(config["fetch_multi_data_body"])
        _check(resp, "matching_room/fetch_multi_data")

        print(f"\n=== in_game/start ({stage_id}) ===")
        if config.get("start_body") is not None:
            resp = client.in_game_start_raw(config["start_body"])
        else:
            resp = client.in_game_start(stage_id, deck_index=1)
        _check(resp, "in_game/start")

        print(f"\n=== in_game/result ({stage_id}) ===")
        resp = _submit_in_game_result_with_resume(
            client,
            build_result_body=lambda sid, start_response: load_battle_result(
                stage_master_id=stage_id,
                template_stage_id=config["template_stage"],
                in_game_session_id=sid,
                template_file=config["template_file"],
                start_response=start_response,
                **result_stat_kwargs,
            ),
        )
        _check(resp, "in_game/result")

    saved = _save_client_account(
        client,
        args,
        progress=progress,
        last_command=last_command if last_command is not None else f"{command_name}-{level}x{times}",
        snapshot=login_snapshot,
    )
    print(f"\n{'='*50}")
    print(f"{display_name} {level} complete. Runs: {times}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def cmd_xl(args):
    """Run the recorded 协力 stage with captured raw start + result bodies."""
    level = int(args.level)
    _run_xl_command(
        args,
        configs=_XL_CONFIGS,
        level=level,
        command_name="xl",
        display_name="协力 XL",
        last_command=f"xl-{level}",
    )


def cmd_jqhd_xl(args, level_text: str):
    if not level_text.isdigit():
        raise SystemExit("Invalid jqhd xl level. Use format like: jqhd xl 1")
    level = int(level_text)
    _run_xl_command(
        args,
        configs=_JQHD_XL_CONFIGS,
        level=level,
        command_name="jqhd xl",
        display_name="活动协力 XL",
        progress=f"jqhd_xl_{level}_complete",
        last_command=f"jqhd-xl-{level}x{args.times}",
    )


def cmd_juxiang(args):
    """Run the 巨像 (Colossus) boss dungeon with a specified score."""
    from .battle_templates import load_juxiang_result

    client, record = _load_client_for_account(args)
    score = args.score
    result_stat_kwargs = _result_stat_override_kwargs(args)

    print(f"=== account {_account_ref(record)} ===")
    print(f"=== 巨像 (Colossus) — score={score} ===")

    print("\n=== masterdata/get_version ===")
    resp = client.masterdata_get_version()
    _check(resp, "masterdata/get_version")

    print("\n=== login/login ===")
    resp = client.login_login(first_login=False)
    _check(resp, "login/login")
    login_snapshot = _build_account_snapshot_from_login(client)

    # Start + result with 500 retry SOP
    _run_scored_dungeon(
        client,
        _JUXIANG_STAGE_MASTER_ID,
        build_result_body=lambda sid, start_response: load_juxiang_result(
            score=score,
            in_game_session_id=sid,
            **result_stat_kwargs,
        ),
        login_resp=resp,
    )

    saved = _save_client_account(
        client,
        args,
        last_command="juxiang",
        snapshot=login_snapshot,
    )

    print(f"\n{'='*50}")
    print(f"巨像 complete. Score submitted: {score}")
    _print_saved_account(saved, _store_path(args))
    print("=" * 50)


def build_parser():
    parser = argparse.ArgumentParser(description="DQSG automation CLI")
    parser.add_argument(
        "--accounts-file",
        default=str(ACCOUNT_STORE_PATH),
        help="Path to the saved account JSON file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed request/session/retry logs",
    )
    parser.add_argument(
        "--proxy",
        help="Proxy URL for API calls, e.g. http://user:pass@host:port or host:port",
    )
    parser.add_argument(
        "--proxy-api",
        dest="proxy_api",
        help="HTTP endpoint returning a proxy URL; supports {country} and {country_lower}",
    )
    parser.add_argument(
        "--proxy-country",
        default=None,
        help="Country code for --proxy-api, default: DQSG_PROXY_COUNTRY or TW",
    )
    parser.add_argument(
        "--proxy-auto",
        dest="proxy_auto",
        action="store_true",
        default=None,
        help="Automatically fetch a proxy after masterdata/get_version returns HTTP 403",
    )
    parser.add_argument(
        "--no-proxy-auto",
        dest="proxy_auto",
        action="store_false",
        help="Disable automatic proxy fetching on HTTP 403",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    accounts_parser = subparsers.add_parser("accounts", help="List saved accounts")
    accounts_parser.set_defaults(func=cmd_accounts)

    status_parser = subparsers.add_parser("status", help="Show account status: SNS coin and equipment")
    status_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    status_parser.set_defaults(func=cmd_status)

    daily_parser = subparsers.add_parser("daily", help="Run daily home and device requests for a saved account")
    daily_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    daily_parser.set_defaults(func=cmd_daily)

    live_parser = subparsers.add_parser("live", help="Run all registered saved-account flows in numeric order")
    live_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    live_parser.set_defaults(func=cmd_live)

    register_parser = subparsers.add_parser("register", help="Create a new account and save it")
    register_parser.add_argument("--label", help="Optional label stored with the new account")
    register_parser.set_defaults(func=cmd_register)

    import_parser = subparsers.add_parser("import", help="Import account credentials from console log text")
    import_parser.add_argument("log_text", nargs="*", help="Console log text containing userId and stored_key")
    import_parser.add_argument("--file", help="Read console log text from a file")
    import_parser.add_argument("--client-uuid", help="Override client_uuid when it is not present in the log")
    import_parser.add_argument("--label", help="Optional label stored with the imported account")
    import_parser.set_defaults(func=cmd_import)

    delete_parser = subparsers.add_parser("delete", help="Delete a saved account remotely and remove it locally")
    delete_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    delete_parser.set_defaults(func=cmd_delete)

    verify_parser = subparsers.add_parser("verify", help="Verify captured traffic with known keys")
    verify_parser.set_defaults(func=cmd_verify)

    flow1_parser = subparsers.add_parser("flow1", help="Create a new account and finish tutorial battle")
    flow1_parser.add_argument("--label", help="Optional label stored with the new account")
    flow1_parser.set_defaults(func=cmd_flow1)

    flow1_imported_parser = subparsers.add_parser(
        "flow1-imported",
        help="Continue flow1 on an imported fresh account after login/startup",
    )
    flow1_imported_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow1_imported_parser.set_defaults(func=cmd_flow1_imported)

    flow2_parser = subparsers.add_parser("flow2", help="Continue a saved account through chapter 1-1")
    flow2_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow2_parser.set_defaults(func=cmd_flow2)

    avatar_save_parser = subparsers.add_parser("avatar-save", help="Retry avatar/save on a saved account")
    avatar_save_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    avatar_save_parser.add_argument("--avatar-id", type=int, default=1)
    avatar_save_parser.add_argument("--body-id", type=int, default=1)
    avatar_save_parser.add_argument("--face-id", type=int, default=1)
    avatar_save_parser.add_argument("--eye-color-id", type=int, default=1)
    avatar_save_parser.add_argument("--skin-color-id", type=int, default=1)
    avatar_save_parser.add_argument("--hair-id", type=int, default=1)
    avatar_save_parser.add_argument("--hair-color-id", type=int, default=1)
    avatar_save_parser.add_argument("--voice-id", type=int, default=1)
    avatar_save_parser.set_defaults(func=cmd_avatar_save)

    flow1_post_avatar_parser = subparsers.add_parser(
        "flow1-post-avatar",
        help="Continue flow1 for a saved account after avatar/save succeeds",
    )
    flow1_post_avatar_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow1_post_avatar_parser.set_defaults(func=cmd_flow1_post_avatar)

    dump_login_parser = subparsers.add_parser(
        "dump-login-response",
        help="Log into a saved account and write the decrypted login response to a file",
    )
    dump_login_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    dump_login_parser.add_argument("--output", required=True, help="Output file path")
    dump_login_parser.set_defaults(func=cmd_dump_login_response)

    probe_billing_parser = subparsers.add_parser(
        "probe-billing-web-store",
        help="Log in, call billing/update_web_store, and dump the decrypted response",
    )
    probe_billing_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    probe_billing_parser.add_argument("--output", required=True, help="Output file path")
    probe_billing_parser.add_argument(
        "--needle-int",
        dest="needle_int",
        action="append",
        type=int,
        help="Optional int32 value to scan for in the decrypted response; can be repeated",
    )
    probe_billing_parser.set_defaults(func=cmd_probe_billing_web_store)

    dump_flow2_start_parser = subparsers.add_parser(
        "dump-flow2-start",
        help="Run flow2 start steps and write the decrypted in_game/start response to a file",
    )
    dump_flow2_start_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    dump_flow2_start_parser.add_argument("--output", required=True, help="Output file path")
    dump_flow2_start_parser.set_defaults(func=cmd_dump_flow2_start)

    flow2_post_gacha_parser = subparsers.add_parser(
        "flow2-post-gacha",
        help="Continue flow2 after the tutorial gacha draw using a known UserWeaponId",
    )
    flow2_post_gacha_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow2_post_gacha_parser.add_argument("--user-weapon-id", required=True, type=int, help="UserWeaponId returned by gacha/draw")
    flow2_post_gacha_parser.set_defaults(func=cmd_flow2_post_gacha)

    flow3_parser = subparsers.add_parser("flow3", help="Batch clear chapter 1 stages on a saved account")
    flow3_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow3_parser.set_defaults(func=cmd_flow3)

    flow4_parser = subparsers.add_parser("flow4", help="Continue a saved account through the recorded 1-4 to 1-10 flow")
    flow4_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow4_parser.set_defaults(func=cmd_flow4)

    flow5_parser = subparsers.add_parser("flow5", help="Continue a saved account through the recorded post-1-10 flow")
    flow5_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow5_parser.set_defaults(func=cmd_flow5)

    flow6_parser = subparsers.add_parser("flow6", help="Continue a saved account through chapter-1 hard mode 1-1 to 1-10")
    flow6_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow6_parser.set_defaults(func=cmd_flow6)

    battle_stage_parser = subparsers.add_parser(
        "battle-stage",
        help="Fight a recorded stage; surrender unfinished battle sessions before starting a new run",
    )
    battle_stage_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    battle_stage_parser.add_argument("--stage", required=True, help="Recorded stage key such as 1-1, 1-2, 1-3, 1-4")
    _add_result_stat_override_args(battle_stage_parser)
    battle_stage_parser.set_defaults(func=cmd_battle_stage)

    jq_parser = subparsers.add_parser(
        "jq",
        help="Run normal story stages; e.g. jq 2-1 or jq 2-1 2-10",
    )
    jq_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    jq_parser.add_argument("start_stage", help="Story stage key such as 2-1")
    jq_parser.add_argument("end_stage", nargs="?", help="Optional range end such as 2-10")
    _add_result_stat_override_args(jq_parser)
    jq_parser.set_defaults(func=cmd_jq)

    jqh_parser = subparsers.add_parser(
        "jqh",
        help="Run hard story stages; e.g. jqh 2-1 or jqh 2-1 2-10",
    )
    jqh_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    jqh_parser.add_argument("start_stage", help="Story stage key such as 2-1")
    jqh_parser.add_argument("end_stage", nargs="?", help="Optional range end such as 2-10")
    _add_result_stat_override_args(jqh_parser)
    jqh_parser.set_defaults(func=cmd_jqh)

    jqhd_parser = subparsers.add_parser(
        "jqhd",
        help="Run recorded event stages; e.g. jqhd 1-1, jqhd qd l 1, jqhd xl 1, or jqhd st 1",
    )
    jqhd_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    jqhd_parser.add_argument(
        "--score", type=int, default=None,
        help="Score override for jqhd qd; defaults: 1=8001, 2=24000, 3=72000, 4=216000, 5=54001",
    )
    jqhd_parser.add_argument("--times", type=int, default=1, help="Repeat each jqhd stage this many times")
    jqhd_parser.add_argument("start_stage", help="Event stage key such as 1-1, qd, xl, or st")
    jqhd_parser.add_argument("end_stage", nargs="?", help="Optional range end such as 1-10, qd type, or xl level")
    jqhd_parser.add_argument("extra_stage", nargs="?", help="qd level, e.g. jqhd qd l 1")
    _add_result_stat_override_args(jqhd_parser)
    jqhd_parser.set_defaults(func=cmd_jqhd)

    collect_rewards_parser = subparsers.add_parser(
        "collect-rewards",
        help="Alias for flow99: receive rewards and refresh snapshot for a saved account",
    )
    collect_rewards_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    collect_rewards_parser.set_defaults(func=cmd_collect_rewards)

    flow99_parser = subparsers.add_parser("flow99", help="Receive rewards and refresh snapshot for a saved account")
    flow99_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    flow99_parser.set_defaults(func=cmd_flow99)

    ad_parser = subparsers.add_parser(
        "ad",
        help="Run advertisement reward actions for a saved account",
    )
    ad_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    ad_parser.add_argument(
        "action",
        nargs="?",
        choices=["reward", "tx", "store", "gacha"],
        default="reward",
        help="Action to run: reward (default), tx (探险), store, or gacha",
    )
    ad_parser.set_defaults(func=cmd_ad)

    gacha_parser = subparsers.add_parser(
        "gacha",
        help="Execute gacha draws (metal or normal 10-pull)",
    )
    gacha_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    gacha_parser.add_argument(
        "--type", dest="gacha_type", required=True,
        choices=["metal", "normal"],
        help="Gacha pool: 'metal' (3000 diamonds) or 'normal' (10 tickets)",
    )
    gacha_parser.add_argument(
        "--count", type=int, default=1,
        help="Number of 10-pulls to execute (default: 1)",
    )
    gacha_parser.set_defaults(func=cmd_gacha)

    juxiang_parser = subparsers.add_parser(
        "juxiang",
        help="Run the 巨像 (Colossus) boss dungeon with a specified score",
    )
    juxiang_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    juxiang_parser.add_argument(
        "--score", type=int, required=True,
        help="Damage score to submit for the battle result",
    )
    _add_result_stat_override_args(juxiang_parser)
    juxiang_parser.set_defaults(func=cmd_juxiang)

    hd_parser = subparsers.add_parser(
        "hd",
        help="Run 活动 (Event) scored battles",
    )
    hd_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    hd_parser.add_argument("--times", type=int, default=1, help="Repeat the hd stage this many times")
    hd_parser.add_argument(
        "type", choices=["qdjf", "jx", "xmss", "shn", "cjmwmg"],
        help=(
            "Event type: qdjf (强敌交锋), jx (巨像), xmss (小魔术师), "
            "shn (食火鸟), cjmwmg (超级魔物猛攻)"
        ),
    )
    hd_parser.add_argument(
        "level",
        nargs="?",
        choices=["1", "2", "3", "4"],
        help="Difficulty level; optional for jx/xmss/shn",
    )
    hd_parser.add_argument(
        "--score", type=int, default=None,
        help="Score to submit; defaults: qdjf 1=8000, 2=24000, 3=72000, 4=216000; jx=18000; xmss/shn=11000; cjmwmg 1=15651, 2=18443",
    )
    _add_result_stat_override_args(hd_parser)
    hd_parser.set_defaults(func=cmd_hd)

    tz_parser = subparsers.add_parser(
        "tz",
        help="Run 挑战 dungeons",
    )
    tz_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    tz_parser.add_argument("--times", type=int, default=1, help="Repeat the tz stage this many times")
    tz_parser.add_argument(
        "zone",
        choices=["st"],
        help="挑战区域: st (饲堂)",
    )
    tz_parser.add_argument(
        "element",
        nargs="?",
        choices=["hb", "em", "yx"],
        help="属性: hb (寒冰), em (恶魔), yx (原型杀戮者); only used for st",
    )
    tz_parser.add_argument(
        "level",
        nargs="?",
        choices=["1", "2"],
        help="Level; only used for st",
    )
    _add_result_stat_override_args(tz_parser)
    tz_parser.set_defaults(func=cmd_tz)

    xl_parser = subparsers.add_parser(
        "xl",
        help="Run recorded 协力 stages using captured start/result bodies",
    )
    xl_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    xl_parser.add_argument(
        "level",
        choices=["1", "2"],
        help="协力 stage slot",
    )
    xl_parser.add_argument(
        "--times",
        type=int,
        default=1,
        help="Repeat count (default: 1)",
    )
    _add_result_stat_override_args(xl_parser)
    xl_parser.set_defaults(func=cmd_xl)

    yc_parser = subparsers.add_parser(
        "yc",
        help="Run a 养成 (Growth) dungeon with a specified score",
    )
    yc_parser.add_argument("--account", help="Saved account user_id, label, or latest")
    yc_parser.add_argument(
        "type", choices=["fj", "wq", "jb", "slm", "slm1"],
        help="Dungeon type: fj (防具/armor), wq (武器/weapon), jb (金币/gold), slm/slm1 (史莱姆/slime)",
    )
    yc_parser.add_argument(
        "level",
        nargs="?",
        help="Dungeon level (1/2/3) or 'tg' for skip",
    )
    yc_parser.add_argument(
        "--score", type=int, default=None,
        help="Score to submit (required for battle, not needed for tg)",
    )
    yc_parser.add_argument(
        "--count", type=int, default=3,
        help="Skip count for tg mode (default: 3)",
    )
    yc_parser.add_argument(
        "--times", type=int, default=1,
        help="Repeat count for battle mode (default: 1)",
    )
    _add_result_stat_override_args(yc_parser)
    yc_parser.set_defaults(func=cmd_yc)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
