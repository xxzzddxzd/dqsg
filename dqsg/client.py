from __future__ import annotations

import os
import uuid
import time
import requests

from .crypto import (
    STARTUP_KEY, BASE_URL,
    encrypt_request, decrypt_response, xor_bytes, rsa_public_encrypt,
)
from .serialization import BytesReader, BytesWriter
from .parsers import (
    parse_masterdata_response, parse_startup_response, parse_login_response,
    parse_home_info_response, parse_empty_response, parse_user_model_response,
    parse_start_tutorial_response, parse_result_tutorial_response,
    parse_metric_response,
    build_login_request, build_startup_request,
    build_terms_agree_request, build_home_info_request,
    build_adventure_read_request, build_tutorial_read_request,
    build_feature_intro_read_request, build_set_user_name_request,
    build_save_avatar_request,
    build_metric_adventure_skip_request, build_metric_tutorial_request,
    build_metric_low_fps_request, build_metric_device_request,
    build_in_game_start_request, build_in_game_result_request,
    build_gacha_draw_request, parse_gacha_draw_response,
    parse_gacha_fetch_list_response,
    GACHA_METAL_10, GACHA_NORMAL_10, GACHA_TUTORIAL,
    equipment_rarity, equipment_display_name,
    build_deck_save_equipment_request,
    build_present_receive_request,
    build_playable_guide_read_request,
    build_notice_read_all_normal_notices_request,
    build_notice_detail_request,
    build_release_function_unlock_request,
    build_main_area_read_unlock_request,
    build_weapon_growth_level_request,
    build_area_receive_achievement_reward_request,
    build_mission_get_summary_request,
    build_mission_receive_daily_reward_and_progress_reward_request,
    build_mission_receive_achievement_reward_request,
    build_mission_receive_event_reward_request,
    build_mission_receive_daily_reward_request,
    build_mission_receive_daily_progress_reward_request,
    build_mission_receive_weekly_reward_request,
    build_mission_receive_weekly_progress_reward_request,
    build_mission_panel_fetch_request,
    build_mission_panel_receive_reward_request,
    build_user_rank_receive_reward_request,
    build_advertisement_receive_reward_chance_point_card_point_request,
    build_profile_fetch_request,
    build_album_receive_orb_rank_reward_request,
    build_album_receive_enemy_kill_count_reward_request,
)

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"
_DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "zh-CN,zh-Hans;q=0.9",
    "content-type": "application/json",
    "user-agent": "DQSG/2474139 CFNetwork/1399 Darwin/22.1.0",
}


def _color(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


def _status_text(status: int) -> str:
    text = f"status={status}"
    return _color(text, _GREEN if status == 1 else _RED)


def _http_status_text(status_code: int) -> str:
    text = f"HTTP {status_code}"
    return _color(text, _GREEN if status_code == 200 else _RED)


def _request_param_text(plaintext: bytes) -> str:
    if not plaintext:
        return "-"
    if len(plaintext) <= 16:
        return plaintext.hex()
    return f"<{len(plaintext)} bytes>"


# ==========================================================================
# Account credentials
# ==========================================================================

ACCT1_STORED_KEY = bytes.fromhex("7a2216ecb295277fc270209284894853145c1874f0534c9214fa414d89a93cad")
ACCT1_USER_ID = 87125091589
ACCT1_CLIENT_UUID = "3f583c1d-a510-4dae-8f92-e301c4e509ef"

ACCT2_STARTUP_RANDOM = bytes.fromhex("f0df1e1e516cda89ae745ad2ca48949d3220e5a96609be962a4828626132605b")
ACCT2_AUTH_KEY = bytes.fromhex("86c531bb0ad099786db2b38cc45c93e78951e29a2eec3a8a453d1299c5259dca")
ACCT2_STORED_KEY = xor_bytes(ACCT2_STARTUP_RANDOM, ACCT2_AUTH_KEY)
ACCT2_USER_ID = 36183493676
ACCT2_CLIENT_UUID = "9fcda925-b5c5-4268-aa9a-60e39ec0e513"

ACCT3_STORED_KEY = bytes.fromhex("cba9ef8a631614a113ccb50e929f4fceff57366f5816910587438741beb6956c")
ACCT3_USER_ID = 62925351098
ACCT3_CLIENT_UUID = "d6a93da1-3193-4e8a-8f96-22d3b8ae7747"

ACCT4_STORED_KEY = bytes.fromhex("bd423ddb522964d8799ceacb9b48bced8158ccdcda6d6e36471f2198971ff0c7")
ACCT4_USER_ID = 96317219522
ACCT4_CLIENT_UUID = "eddfQFzGAhkS0kVKHpJYOwx"  # from firebase device token prefix


class DQSGClient:
    def __init__(self, user_id=None, stored_key=None, client_uuid=None):
        self.session = requests.Session()
        self.session.verify = False
        self.session_key = STARTUP_KEY
        self.user_id = int(user_id) if user_id is not None else ACCT2_USER_ID
        if stored_key is None:
            self.stored_key = ACCT2_STORED_KEY
        elif isinstance(stored_key, str):
            self.stored_key = bytes.fromhex(stored_key)
        else:
            self.stored_key = stored_key
        self.client_uuid = client_uuid or ACCT2_CLIENT_UUID
        self.login_key = xor_bytes(self.stored_key, STARTUP_KEY)
        self.request_id = 0
        self.mv = None
        self.last_login_response_raw = None
        self.last_response_raw = None
        self.last_response_endpoint = None
        self.terminal_id = None
        self.startup_random = None
        self.authorization_key = None

    @classmethod
    def new_account(cls):
        obj = cls.__new__(cls)
        obj.session = requests.Session()
        obj.session.verify = False
        obj.session_key = STARTUP_KEY
        obj.user_id = None
        obj.stored_key = None
        obj.client_uuid = str(uuid.uuid4())
        obj.login_key = None
        obj.request_id = 0
        obj.mv = None
        obj.last_login_response_raw = None
        obj.last_response_raw = None
        obj.last_response_endpoint = None
        obj.terminal_id = None
        obj.startup_random = None
        obj.authorization_key = None
        return obj

    @classmethod
    def from_account_record(cls, record: dict):
        obj = cls(
            user_id=record["user_id"],
            stored_key=record["stored_key"],
            client_uuid=record["client_uuid"],
        )
        obj.terminal_id = record.get("terminal_id")
        startup_random = record.get("startup_random")
        obj.startup_random = bytes.fromhex(startup_random) if startup_random else None
        authorization_key = record.get("authorization_key")
        obj.authorization_key = bytes.fromhex(authorization_key) if authorization_key else None
        login_key = record.get("login_key")
        if login_key:
            obj.login_key = bytes.fromhex(login_key)
        return obj

    def export_account_record(self) -> dict:
        if self.user_id is None or self.stored_key is None or not self.client_uuid:
            raise ValueError("Account credentials are incomplete")
        record = {
            "user_id": int(self.user_id),
            "stored_key": self.stored_key.hex(),
            "client_uuid": self.client_uuid,
        }
        if self.terminal_id:
            record["terminal_id"] = self.terminal_id
        if self.startup_random:
            record["startup_random"] = self.startup_random.hex()
        if self.authorization_key:
            record["authorization_key"] = self.authorization_key.hex()
        if self.login_key:
            record["login_key"] = self.login_key.hex()
        return record

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _build_path(self, endpoint: str, with_user=False, with_time=False) -> str:
        self.request_id += 1
        path = f"/{endpoint}?p=i&id={self.request_id}"
        if with_user and self.user_id:
            path += f"&u={self.user_id}"
        if with_time:
            path += f"&t={int(time.time() * 1000)}"
        path += "&l=zh"
        if self.mv:
            path += f"&mv={self.mv}"
        return path

    def _call(self, endpoint: str, plaintext: bytes = b"",
              key: bytes = None, with_user=False, with_time=False) -> bytes:
        if key is None:
            key = self.session_key
        path = self._build_path(endpoint, with_user=with_user, with_time=with_time)
        url = BASE_URL + path
        encrypted = encrypt_request(key, path, plaintext)
        line = f"=== /{endpoint} {_request_param_text(plaintext)}"

        # Save decrypted request body
        safe_endpoint = endpoint.replace("/", "_")
        account_id = str(self.user_id) if self.user_id else "unknown"
        os.makedirs("req", exist_ok=True)
        with open(os.path.join("req", f"req_{safe_endpoint}_{account_id}"), "wb") as f:
            f.write(plaintext)

        last_exc = None
        for attempt in range(1, 4):
            try:
                resp = self.session.post(
                    url,
                    data=encrypted,
                    headers=_DEFAULT_HEADERS,
                )
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
                last_exc = exc
                if attempt >= 3:
                    print(f"{line} req:{type(exc).__name__}")
                    raise
                line += f" req:{type(exc).__name__}(retry)"
                time.sleep(1.0)
        else:
            raise last_exc
        if resp.status_code != 200:
            retry_tag = "(retry)" if resp.status_code >= 500 else ""
            print(f"{line} req:200 res:{resp.status_code}{retry_tag}")
            resp.raise_for_status()
        decrypted = decrypt_response(key, path, resp.content)
        self.last_response_raw = decrypted
        self.last_response_endpoint = endpoint
        print(f"{line} req:200 res:{resp.status_code}")

        # Save decrypted response body
        os.makedirs("res", exist_ok=True)
        with open(os.path.join("res", f"res_{safe_endpoint}_{account_id}"), "wb") as f:
            f.write(decrypted)

        return decrypted

    def call_authenticated(self, endpoint: str, plaintext: bytes = b"") -> bytes:
        return self._call(endpoint, plaintext, with_user=True, with_time=True)

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def masterdata_get_version(self):
        data = self._call("masterdata/get_version", b"")
        resp = parse_masterdata_response(data)
        self.mv = resp["version"]
        print(f"  <- version={resp['version']}, revision={resp['revision']}")
        return resp

    def login_startup(self):
        startup_random = os.urandom(32)
        mask = rsa_public_encrypt(startup_random)
        terminal_id = str(uuid.uuid4()).upper()
        req = build_startup_request(mask, self.client_uuid, terminal_id)
        print(f"  clientUuid={self.client_uuid}")
        print(f"  terminalId={terminal_id}")
        print(f"  startupRandom={startup_random.hex()[:16]}...")
        data = self._call("login/startup", req, key=STARTUP_KEY)
        resp = parse_startup_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        print(f"  <- UserId={resp['UserId']}")
        print(f"  <- AuthorizationKey={resp['AuthorizationKey'].hex()[:16]}...")

        self.user_id = resp["UserId"]
        auth_key = resp["AuthorizationKey"]
        self.terminal_id = terminal_id
        self.startup_random = startup_random
        self.authorization_key = auth_key
        self.stored_key = xor_bytes(startup_random, auth_key)
        self.login_key = xor_bytes(self.stored_key, STARTUP_KEY)
        print(f"  <- storedKey={self.stored_key.hex()[:16]}...")
        print(f"  <- loginKey={self.login_key.hex()[:16]}...")

        return {
            "user_id": self.user_id,
            "stored_key": self.stored_key.hex(),
            "client_uuid": self.client_uuid,
            "startup_random": startup_random.hex(),
            "authorization_key": auth_key.hex(),
            "terminal_id": terminal_id,
        }

    def login_login(self, first_login=False):
        if self.login_key is None:
            raise RuntimeError("login_key is missing; create or load an account first")
        if first_login:
            auth_count = 1
        else:
            random_bytes = os.urandom(32)
            mask = rsa_public_encrypt(random_bytes)
            req = build_login_request(1, mask, self.client_uuid)
            print(f"  [probe] sending auth_count=1 to learn server count...")
            data = self._call("login/login", req, key=self.login_key,
                              with_user=True, with_time=False)
            r = BytesReader(data)
            r.read_int()  # status
            server_count = r.read_int()
            r.read_bytes()  # empty session key
            print(f"  [probe] server count={server_count}")
            auth_count = server_count + 1

        random_bytes = os.urandom(32)
        mask = rsa_public_encrypt(random_bytes)
        req = build_login_request(auth_count, mask, self.client_uuid)
        print(f"  [login] sending auth_count={auth_count}")
        print(f"  loginKey={self.login_key.hex()[:16]}...")
        data = self._call("login/login", req, key=self.login_key,
                          with_user=True, with_time=True)
        self.last_login_response_raw = data
        resp = parse_login_response(data)
        print(f"  <- {_status_text(resp['_status'])}, AuthCount={resp['AuthorizationCount']}")
        if resp["SessionKey"] and len(resp["SessionKey"]) == 32:
            self.session_key = xor_bytes(random_bytes, resp["SessionKey"])
            print(f"  <- sessionKey={self.session_key.hex()[:16]}...")
            print(f"  <- ClientId={resp.get('ClientId', '?')}")
            print(f"  <- InGameSessionId={resp.get('InGameSessionId')}")
            print(f"  <- AssetCdnUrl={resp.get('AssetCdnUrl', '?')}")
        else:
            print(f"  <- WARNING: no valid SessionKey (len={len(resp['SessionKey'])})")
        return resp

    def terms_get(self):
        data = self._call("terms/get_terms_eu", b"",
                          key=STARTUP_KEY, with_user=True, with_time=True)
        print(f"  <- {len(data)} bytes (HTML terms)")
        return data

    def terms_agree(self):
        req = build_terms_agree_request()
        data = self.call_authenticated("terms/terms_agree_eu", req)
        print(f"  <- {len(data)} bytes")
        return data

    def home_fetch_info(self):
        req = build_home_info_request()
        data = self.call_authenticated("home/fetch_info", req)
        resp = parse_home_info_response(data)
        print(f"  <- {_status_text(resp['_status'])}, PresentCount={resp['PresentCount']}")
        notice = resp["Notice"]
        print(f"  <- {len(notice['MandatoryNotices'])} mandatory, {len(notice['HomeBannerNotices'])} banners")
        for b in notice["HomeBannerNotices"]:
            print(f"     [{b['InformationId']}] {b['TitleText']}  ({b['StartAt']} ~ {b['EndAt']})")
        return resp

    def delete_account(self):
        data = self.call_authenticated("user/delete")
        resp = parse_empty_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Tutorial flow
    # ------------------------------------------------------------------

    def in_game_start_tutorial(self):
        data = self.call_authenticated("in_game/start_tutorial")
        resp = parse_start_tutorial_response(data)
        print(f"  <- {_status_text(resp['_status'])}, remaining={resp['_remaining']} bytes")
        return resp

    def in_game_result_tutorial(self):
        data = self.call_authenticated("in_game/result_tutorial")
        resp = parse_result_tutorial_response(data)
        print(f"  <- {_status_text(resp['_status'])}, remaining={resp['_remaining']} bytes")
        return resp

    def adventure_read(self, adventure_master_id: int):
        req = build_adventure_read_request(adventure_master_id)
        data = self.call_authenticated("adventure/read", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def tutorial_read(self, tutorial_step: int):
        req = build_tutorial_read_request(tutorial_step)
        data = self.call_authenticated("tutorial/read", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def feature_intro_read(self, feature_intro_type: int):
        req = build_feature_intro_read_request(feature_intro_type)
        data = self.call_authenticated("feature_intro/read", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def profile_set_user_name(self, name: str):
        req = build_set_user_name_request(name)
        data = self.call_authenticated("profile/set_user_name", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def avatar_save(self, avatar_id=1, body_id=1, face_id=1,
                    eye_color_id=1, skin_color_id=1,
                    hair_id=1, hair_color_id=1, voice_id=1):
        req = build_save_avatar_request(
            avatar_id, body_id, face_id, eye_color_id,
            skin_color_id, hair_id, hair_color_id, voice_id)
        data = self.call_authenticated("avatar/save", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_tutorial(self):
        req = build_metric_tutorial_request()
        data = self.call_authenticated("metric/tutorial", req)
        resp = parse_metric_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_adventure_skip(self, adventure_master_id: int, command_index: int):
        req = build_metric_adventure_skip_request(adventure_master_id, command_index)
        data = self.call_authenticated("metric/adventure_skip", req)
        resp = parse_metric_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_low_fps_prolonged(self, current_fps: float, duration: float, scene_id: str):
        req = build_metric_low_fps_request(current_fps, duration, scene_id)
        data = self.call_authenticated("metric/low_fps_prolonged", req)
        resp = parse_metric_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_device(self, platform: str = "IPhonePlayer",
                      device_tier: str = "recommended",
                      soc_model: str = "arm64e",
                      device_model: str = "iPhone14,3",
                      system_memory_mb: int = 5626):
        req = build_metric_device_request(platform, device_tier, soc_model,
                                          device_model, system_memory_mb)
        data = self.call_authenticated("metric/device", req)
        resp = parse_metric_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Battle (in_game/start, in_game/result)
    # ------------------------------------------------------------------

    def in_game_start(self, stage_master_id: int, deck_index: int = 1,
                      friend_style_id: int = None):
        req = build_in_game_start_request(stage_master_id, deck_index, friend_style_id)
        data = self.call_authenticated("in_game/start", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def in_game_start_raw(self, raw_body: bytes):
        data = self.call_authenticated("in_game/start", raw_body)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def in_game_result(self, stage_master_id: int = None,
                       template_stage_id: int = None,
                       in_game_session_id: int = None,
                       raw_body: bytes = None):
        req = build_in_game_result_request(stage_master_id=stage_master_id,
                                           template_stage_id=template_stage_id,
                                           in_game_session_id=in_game_session_id,
                                           raw_body=raw_body)
        data = self.call_authenticated("in_game/result", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def in_game_skip_stage(self, stage_master_id: int, count: int = 3):
        """Skip a stage multiple times (requires prior clear)."""
        w = BytesWriter()
        w.write_int(stage_master_id)
        w.write_int(count)
        data = self.call_authenticated("in_game/skip_stage", w.to_bytes())
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def matching_room_fetch_multi_data_raw(self, raw_body: bytes):
        data = self.call_authenticated("matching_room/fetch_multi_data", raw_body)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Gacha
    # ------------------------------------------------------------------

    def gacha_fetch_top(self):
        data = self.call_authenticated("gacha/fetch_top")
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def gacha_draw(self, gacha_master_id: int):
        """Draw from a gacha pool. Returns structured reward data."""
        req = build_gacha_draw_request(gacha_master_id)
        data = self.call_authenticated("gacha/draw", req)
        resp = parse_gacha_draw_response(data)
        print(f"  <- {_status_text(resp['_status'])}, {resp['reward_count']} rewards")
        for rw in resp["rewards"]:
            star = '★' * rw['rarity']
            print(f"     {star} {rw['display']}  (mid={rw['content_master_id']})")
        return resp

    def gacha_fetch_list(self):
        data = self.call_authenticated("gacha/fetch_list")
        resp = parse_gacha_fetch_list_response(data)
        print(f"  <- {_status_text(resp['_status'])}, draw_count={resp['draw_count']}, "
              f"pools={len(resp['gacha_ids'])}")
        return resp

    # ------------------------------------------------------------------
    # Deck
    # ------------------------------------------------------------------

    def deck_save_style_equipment(self, raw_body: bytes):
        req = build_deck_save_equipment_request(raw_body)
        data = self.call_authenticated("deck/save_style_equipment", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def deck_save_auto_style_equipment(self, raw_body: bytes):
        req = build_deck_save_equipment_request(raw_body)
        data = self.call_authenticated("deck/save_auto_style_equipment", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Present
    # ------------------------------------------------------------------

    def present_fetch(self):
        data = self.call_authenticated("present/fetch")
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def present_receive(self, present_ids: list[int]):
        req = build_present_receive_request(present_ids)
        data = self.call_authenticated("present/receive", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def playable_guide_read(self, guide_id: int):
        req = build_playable_guide_read_request(guide_id)
        data = self.call_authenticated("playable_guide/read", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def notice_fetch_notices(self):
        data = self.call_authenticated("notice/fetch_notices")
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def notice_read_all_normal_notices(self, notice_ids: list[int]):
        req = build_notice_read_all_normal_notices_request(notice_ids)
        data = self.call_authenticated("notice/read_all_normal_notices", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def notice_fetch_detail(self, notice_id: int):
        req = build_notice_detail_request(notice_id)
        data = self.call_authenticated("notice/fetch_notice_detail", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def billing_update_web_store(self):
        data = self.call_authenticated("billing/update_web_store")
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def release_function_unlock(self, function_id: int):
        req = build_release_function_unlock_request(function_id)
        data = self.call_authenticated("release_function/unlock", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def main_area_read_unlock(self, area_master_id: int, area_difficulty: int):
        req = build_main_area_read_unlock_request(area_master_id, area_difficulty)
        data = self.call_authenticated("main_area/read_unlock", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def area_receive_achievement_reward(self, area_achievement_ids: list[int]):
        req = build_area_receive_achievement_reward_request(area_achievement_ids)
        data = self.call_authenticated("area/receive_achievement_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_get_summary(self):
        req = build_mission_get_summary_request()
        data = self.call_authenticated("mission/get_mission_summary", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_daily_reward_and_progress_reward(
        self,
        mission_ids: list[int],
        progress_reward_id: int,
        raw_body: bytes | None = None,
    ):
        if raw_body is None:
            req = build_mission_receive_daily_reward_and_progress_reward_request(
                mission_ids,
                progress_reward_id,
            )
        else:
            req = raw_body
        data = self.call_authenticated(
            "mission/receive_mission_daily_reward_and_daily_progress_reward",
            req,
        )
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_achievement_reward(self, mission_ids: list[int]):
        req = build_mission_receive_achievement_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_achievement_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_event_reward(self, mission_ids: list[int]):
        req = build_mission_receive_event_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_event_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_daily_reward(self, mission_ids: list[int]):
        req = build_mission_receive_daily_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_daily_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_daily_progress_reward(self, mission_ids: list[int]):
        req = build_mission_receive_daily_progress_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_daily_progress_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_weekly_reward(self, mission_ids: list[int]):
        req = build_mission_receive_weekly_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_weekly_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_weekly_progress_reward(self, mission_ids: list[int]):
        req = build_mission_receive_weekly_progress_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_weekly_progress_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_panel_fetch(self, mission_panel_master_id: int):
        req = build_mission_panel_fetch_request(mission_panel_master_id)
        data = self.call_authenticated("mission_panel/fetch_mission", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_panel_receive_reward(self, mission_panel_master_id: int):
        req = build_mission_panel_receive_reward_request(mission_panel_master_id)
        data = self.call_authenticated("mission_panel/receive_mission_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def user_rank_receive_reward(self):
        req = build_user_rank_receive_reward_request()
        data = self.call_authenticated("user_rank/receive_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def advertisement_receive_reward_chance_point_card_point(self):
        req = build_advertisement_receive_reward_chance_point_card_point_request()
        data = self.call_authenticated("advertisement/receive_reward_chance_point_card_point", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def profile_fetch(self):
        req = build_profile_fetch_request(self.user_id)
        data = self.call_authenticated("profile/fetch", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def weapon_growth_level(self, user_weapon_id: int, consume_content_list: list[tuple[int, int, int]]):
        req = build_weapon_growth_level_request(user_weapon_id, consume_content_list)
        data = self.call_authenticated("weapon/growth_level", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def album_receive_orb_rank_reward(self, reward_ids: list[int]):
        req = build_album_receive_orb_rank_reward_request(reward_ids)
        data = self.call_authenticated("album/receive_orb_rank_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp

    def album_receive_enemy_kill_count_reward(self, reward_ids: list[int]):
        req = build_album_receive_enemy_kill_count_reward_request(reward_ids)
        data = self.call_authenticated("album/receive_enemy_kill_count_reward", req)
        resp = parse_user_model_response(data)
        print(f"  <- {_status_text(resp['_status'])}")
        return resp
